use crate::events::WakeSender;
use crate::platform::{self, OwnedHandle};
use anyhow::{bail, Context, Result};
use base64::Engine;
use reqwest::blocking::Client;
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    io::Write,
    os::windows::process::CommandExt,
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::mpsc::Receiver,
    time::{Duration, Instant},
};
use uuid::Uuid;

#[derive(Clone)]
pub struct Options {
    pub python: PathBuf,
    pub data: PathBuf,
    pub fake: bool,
}
pub enum Action {
    Restart,
    Stop,
    /// Generic API call; the result comes back as `Event::Result(tag, value)`.
    Call {
        tag: String,
        method: &'static str,
        path: String,
        body: Option<Value>,
        revision: Option<u64>,
    },
    /// Generate and play a preview, optionally with a specific voice.
    Test {
        text: String,
        voice_id: Option<String>,
    },
    /// Register a voice. Any audio format ffmpeg understands is converted to PCM WAV.
    ImportVoice {
        id: String,
        name: String,
        path: String,
        text: String,
        allowed_user_ids: Vec<String>,
    },
}
pub enum Event {
    Status(Value),
    Result(String, Value),
    Error(String),
    Stopped,
}

struct Worker {
    child: Child,
    _job: OwnedHandle,
    key: String,
    port: u16,
    instance: String,
    cpu_status: Value,
}
impl Worker {
    fn start(options: &Options) -> Result<Self> {
        let system: Value = std::fs::read(options.data.join("system.json"))
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or(json!({}));
        let port = system["api"]["port"].as_u64().unwrap_or(8765) as u16;
        let key = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        let mut command = Command::new(&options.python);
        let logs = options.data.join("logs");
        std::fs::create_dir_all(&logs).context("Cannot create Worker log directory")?;
        let startup_log = std::fs::File::create(logs.join("worker-startup.log"))
            .context("Cannot create Worker startup log")?;
        command
            .args(["-m", "discord_speak_bot", "--data-dir"])
            .arg(&options.data)
            .args(["serve", "--credentials-stdin"])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::from(startup_log))
            .creation_flags(0x08000000);
        if options.fake {
            command.arg("--fake");
        }
        let discord_token = if options.fake {
            String::new()
        } else {
            platform::read_token()?
        };
        let mut child = command.spawn().with_context(|| {
            format!(
                "Cannot start Python Worker at {}. Run setup-worker.ps1 first.",
                options.python.display()
            )
        })?;
        let job = match platform::child_job(
            &child,
            system["performance"]["process_priority"] != "normal",
        ) {
            Ok(job) => job,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        let custom: Vec<u32> = system["performance"]["cpu_set_ids"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_u64().and_then(|n| u32::try_from(n).ok()))
                    .collect()
            })
            .unwrap_or_default();
        let cpu_status = match platform::cpu_sets(
            &child,
            system["performance"]["cpu_mode"]
                .as_str()
                .unwrap_or("automatic"),
            &custom,
        ) {
            Ok(ids) => json!({"effective_cpu_set_ids": ids}),
            Err(e) => json!({"effective_cpu_set_ids": [], "warning": e.to_string()}),
        };
        let mut secrets =
            serde_json::to_vec(&json!({"ipc_token": key, "discord_token": discord_token}))?;
        secrets.push(b'\n');
        let written = child
            .stdin
            .take()
            .context("Missing child stdin")?
            .write_all(&secrets);
        secrets.fill(0);
        if let Err(error) = written {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error.into());
        }
        Ok(Self {
            child,
            _job: job,
            key,
            port,
            instance: String::new(),
            cpu_status,
        })
    }

    fn call(
        &self,
        client: &Client,
        method: &str,
        path: &str,
        body: Option<&Value>,
        revision: Option<u64>,
    ) -> Result<Value> {
        if !path.starts_with('/') || path.starts_with("/internal/v1/") {
            bail!("Settings path must look like /guilds/<Guild ID> (without /internal/v1)");
        }
        let url = format!("http://127.0.0.1:{}/internal/v1{}", self.port, path);
        let mut request = client.request(method.parse()?, url).bearer_auth(&self.key);
        if let Some(value) = body {
            request = request.json(value);
        }
        if let Some(revision) = revision {
            request = request.header("If-Match", format!("\"{revision}\""));
        }
        if method == "POST" {
            request = request.header("Idempotency-Key", Uuid::new_v4().to_string());
        }
        let response = request.send()?;
        let status = response.status();
        let value: Value = response.json()?;
        if !status.is_success() {
            let message = value["error"]["message"]
                .as_str()
                .or_else(|| value["detail"].as_str())
                .map(str::to_owned)
                .unwrap_or_else(|| value.to_string());
            bail!("API {} {} {}: {}", method, path, status, message);
        }
        Ok(value)
    }

    fn stop(&mut self, client: &Client) {
        self.stop_with_budget(client, Duration::from_secs(10));
    }

    fn stop_with_budget(&mut self, client: &Client, budget: Duration) {
        let deadline = Instant::now() + budget;
        let _ = self.call(client, "POST", "/worker/shutdown", Some(&json!({})), None);
        while Instant::now() < deadline {
            if matches!(self.child.try_wait(), Ok(Some(_))) {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        self.force_stop();
    }

    fn force_stop(&mut self) {
        // Kill the entire owned process tree before waiting on the Python launcher.
        unsafe {
            windows_sys::Win32::System::JobObjects::TerminateJobObject(self._job.0, 1);
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        self.force_stop();
    }
}

/// Read a reference recording and return 24 kHz mono 16-bit PCM WAV bytes.
///
/// Phones often save AAC/M4A with a .wav name, so every file goes through ffmpeg
/// (system.json tts.ffmpeg_path). Without ffmpeg, a real RIFF/WAV file is sent unchanged.
fn load_reference_audio(options: &Options, path: &str) -> Result<Vec<u8>> {
    let path = path.trim().trim_matches('"');
    let size = std::fs::metadata(path)
        .with_context(|| format!("File not found: {path}"))?
        .len();
    if size > 100 * 1024 * 1024 {
        bail!("Audio file is too large (max 100MB)");
    }
    let system: Value = std::fs::read(options.data.join("system.json"))
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or(json!({}));
    let ffmpeg = system["tts"]["ffmpeg_path"].as_str().unwrap_or("ffmpeg");
    let converted = Command::new(ffmpeg)
        .args(["-hide_banner", "-loglevel", "error", "-i"])
        .arg(path)
        .args([
            "-vn",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-f",
            "s16le",
            "-c:a",
            "pcm_s16le",
            "pipe:1",
        ])
        .stdin(Stdio::null())
        .stderr(Stdio::piped())
        .creation_flags(0x08000000)
        .output();
    match converted {
        Ok(out) if out.status.success() && !out.stdout.is_empty() => {
            Ok(wav_from_pcm(&out.stdout, 24000))
        }
        Ok(out) => bail!(
            "ffmpeg could not read the audio: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ),
        Err(_) => {
            let bytes = std::fs::read(path)?;
            if !bytes.starts_with(b"RIFF") {
                bail!(
                    "Not a WAV file and ffmpeg was not found (set tts.ffmpeg_path) to convert it"
                );
            }
            Ok(bytes)
        }
    }
}

fn wav_from_pcm(pcm: &[u8], rate: u32) -> Vec<u8> {
    let mut wav = Vec::with_capacity(44 + pcm.len());
    let data = pcm.len() as u32;
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&(36 + data).to_le_bytes());
    wav.extend_from_slice(b"WAVEfmt ");
    wav.extend_from_slice(&16u32.to_le_bytes());
    wav.extend_from_slice(&1u16.to_le_bytes()); // PCM
    wav.extend_from_slice(&1u16.to_le_bytes()); // mono
    wav.extend_from_slice(&rate.to_le_bytes());
    wav.extend_from_slice(&(rate * 2).to_le_bytes());
    wav.extend_from_slice(&2u16.to_le_bytes());
    wav.extend_from_slice(&16u16.to_le_bytes());
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&data.to_le_bytes());
    wav.extend_from_slice(pcm);
    wav
}

pub fn run(options: Options, rx: Receiver<Action>, tx: WakeSender<Event>) {
    let client = match Client::builder()
        .timeout(Duration::from_secs(5))
        .no_proxy()
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(Event::Error(e.to_string()));
            return;
        }
    };
    let mut worker: Option<Worker> = None;
    let mut restart_at = Some(Instant::now());
    let mut started = Instant::now();
    let mut checked = Instant::now();
    let mut preview_checked = Instant::now();
    let mut failures = 0;
    let mut attempts = VecDeque::new();
    let mut previews: VecDeque<String> = VecDeque::new();
    loop {
        match rx.recv_timeout(Duration::from_millis(100)) {
            Ok(Action::Stop) | Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                if let Some(mut w) = worker.take() {
                    w.stop(&client);
                }
                let _ = tx.send(Event::Stopped);
                return;
            }
            Ok(Action::Restart) => {
                if let Some(mut w) = worker.take() {
                    w.stop(&client);
                }
                attempts.clear();
                failures = 0;
                restart_at = Some(Instant::now());
            }
            Ok(action) => {
                let result = match worker.as_ref() {
                    None => Err(anyhow::anyhow!("Worker is not running")),
                    Some(w) => match action {
                        Action::Call {
                            tag,
                            method,
                            path,
                            body,
                            revision,
                        } => w
                            .call(&client, method, &path, body.as_ref(), revision)
                            .map(|v| (tag, v)),
                        Action::Test { text, voice_id } => {
                            let mut body = json!({"text": text});
                            if let Some(id) = voice_id {
                                body["settings"] = json!({"voice_id": id});
                            }
                            w.call(&client, "POST", "/tts/test", Some(&body), None)
                                .map(|v| ("/tts/test".into(), v))
                        }
                        Action::ImportVoice {
                            id,
                            name,
                            path,
                            text,
                            allowed_user_ids,
                        } => load_reference_audio(&options, &path).and_then(|wav| {
                            let encoded = base64::engine::general_purpose::STANDARD.encode(wav);
                            w.call(
                                &client,
                                "POST",
                                "/voices",
                                Some(&json!({
                                    "voice_id": id,
                                    "name": name,
                                    "reference_text": text,
                                    "wav_base64": encoded,
                                    "allowed_user_ids": allowed_user_ids,
                                })),
                                None,
                            )
                            .map(|v| ("voice_added".into(), v))
                        }),
                        _ => unreachable!(),
                    },
                };
                let _ = tx.send(match result {
                    Ok((p, v)) => {
                        if p == "/tts/test" {
                            if let Some(id) = v["operation_id"].as_str() {
                                previews.push_back(id.to_owned());
                            }
                        }
                        Event::Result(p, v)
                    }
                    Err(e) => Event::Error(e.to_string()),
                });
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
        }
        if restart_at.is_some_and(|at| Instant::now() >= at) {
            restart_at = None;
            match Worker::start(&options) {
                Ok(w) => {
                    worker = Some(w);
                    started = Instant::now();
                    failures = 0;
                }
                Err(e) => {
                    let _ = tx.send(Event::Error(e.to_string()));
                    failures = 5;
                }
            }
        }
        if let Some(w) = worker.as_mut() {
            if matches!(w.child.try_wait(), Ok(Some(_))) {
                failures = 5;
            }
            if checked.elapsed() >= Duration::from_secs(2) {
                checked = Instant::now();
                match w.call(&client, "GET", "/status", None, None) {
                    Ok(mut value) => {
                        let id = value["process_instance_id"].as_str().unwrap_or("");
                        if w.instance.is_empty() {
                            w.instance = id.to_string();
                        }
                        if id != w.instance {
                            failures = 5;
                        } else {
                            failures = 0;
                            if value["generation_age_seconds"].as_f64().unwrap_or(0.0)
                                > value["inference_timeout_seconds"].as_f64().unwrap_or(120.0)
                            {
                                failures = 5;
                            }
                            if value["restart_requested"] == true
                                || (value["state"] == "starting"
                                    && started.elapsed() > Duration::from_secs(180))
                            {
                                failures = 5;
                            }
                        }
                        value["host_cpu"] = w.cpu_status.clone();
                        value["python_executable"] = json!(options.python);
                        let _ = tx.send(Event::Status(value));
                    }
                    Err(_)
                        if !w.instance.is_empty()
                            || started.elapsed() > Duration::from_secs(180) =>
                    {
                        failures += 1
                    }
                    Err(_) => {}
                }
            }
            if !previews.is_empty() && preview_checked.elapsed() >= Duration::from_millis(250) {
                preview_checked = Instant::now();
                if let Some(id) = previews.front().cloned() {
                    match w.call(&client, "GET", &format!("/operations/{id}"), None, None) {
                        Ok(value) if value["state"] == "completed" => {
                            previews.pop_front();
                            let result: Result<()> = (|| {
                                let audio = client
                                    .get(format!(
                                        "http://127.0.0.1:{}/internal/v1/operations/{id}/audio",
                                        w.port
                                    ))
                                    .bearer_auth(&w.key)
                                    .send()?
                                    .error_for_status()?
                                    .bytes()?;
                                let directory = options.data.join("runtime");
                                std::fs::create_dir_all(&directory)?;
                                let path = directory.join("preview.wav");
                                std::fs::write(&path, audio)?;
                                platform::play_wav(&path)
                            })();
                            let _ = tx.send(match result {
                                Ok(()) => {
                                    Event::Result("preview".into(), json!({"state":"playing"}))
                                }
                                Err(e) => Event::Error(e.to_string()),
                            });
                        }
                        Ok(value) if value["state"] == "failed" => {
                            previews.pop_front();
                            let _ = tx.send(Event::Error("Preview generation failed".into()));
                        }
                        Err(_) => {
                            previews.pop_front();
                        }
                        _ => {}
                    }
                }
            }
        }
        if failures >= 5 {
            if let Some(mut w) = worker.take() {
                w.stop(&client);
            }
            let now = Instant::now();
            while attempts
                .front()
                .is_some_and(|at: &Instant| now.duration_since(*at) > Duration::from_secs(600))
            {
                attempts.pop_front();
            }
            attempts.push_back(now);
            if attempts.len() >= 5 {
                restart_at = None;
                let _ = tx.send(Event::Error(format!(
                    "Worker recovery stopped after five failures. Check {} and Restart Worker.",
                    options.data.join("logs/worker-startup.log").display()
                )));
            } else {
                let delay = [2, 5, 15, 30, 60][attempts.len() - 1];
                restart_at = Some(now + Duration::from_secs(delay));
                let _ = tx.send(Event::Error(format!(
                    "Worker failed; restarting in {delay}s"
                )));
            }
            failures = 0;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pcm_is_wrapped_in_a_valid_wav_header() {
        let wav = wav_from_pcm(&[1, 0, 2, 0], 24000);
        assert_eq!(&wav[0..4], b"RIFF");
        assert_eq!(u32::from_le_bytes(wav[4..8].try_into().unwrap()), 40);
        assert_eq!(&wav[36..40], b"data");
        assert_eq!(u32::from_le_bytes(wav[40..44].try_into().unwrap()), 4);
        assert_eq!(wav.len(), 48);
    }

    #[test]
    fn exit_before_worker_start_emits_stopped_and_wakes_ui() {
        let (tx, rx) = std::sync::mpsc::channel();
        let (events, received) = std::sync::mpsc::channel();
        tx.send(Action::Stop).unwrap();
        let wake = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let flag = wake.clone();
        run(
            Options {
                python: "missing-python.exe".into(),
                data: "unused".into(),
                fake: true,
            },
            rx,
            WakeSender::new(events, move || {
                flag.store(true, std::sync::atomic::Ordering::SeqCst);
            }),
        );
        assert!(matches!(received.try_recv(), Ok(Event::Stopped)));
        assert!(wake.load(std::sync::atomic::Ordering::SeqCst));
    }

    #[test]
    #[ignore = "requires TTS_TEST_PYTHON"]
    fn unresponsive_worker_is_force_stopped() -> Result<()> {
        let python = std::env::var("TTS_TEST_PYTHON")?;
        let child = Command::new(python)
            .args(["-c", "import time; time.sleep(60)"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(0x08000000)
            .spawn()?;
        let job = platform::child_job(&child, false)?;
        // Reserve an unserved port to make the shutdown HTTP request time out.
        let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
        let mut worker = Worker {
            child,
            _job: job,
            key: "test".into(),
            port: listener.local_addr()?.port(),
            instance: String::new(),
            cpu_status: json!({}),
        };
        let client = Client::builder()
            .timeout(Duration::from_millis(100))
            .no_proxy()
            .build()?;
        let start = Instant::now();
        worker.stop_with_budget(&client, Duration::from_millis(300));
        assert!(start.elapsed() < Duration::from_secs(3));
        assert!(worker.child.try_wait()?.is_some());
        Ok(())
    }

    #[test]
    #[ignore = "requires TTS_TEST_PYTHON and installed Worker or PYTHONPATH"]
    fn worker_process_lifecycle() -> Result<()> {
        let python = std::env::var("TTS_TEST_PYTHON")?;
        let data = std::env::temp_dir().join(format!("tts-host-test-{}", Uuid::new_v4()));
        std::fs::create_dir(&data)?;
        let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
        let port = listener.local_addr()?.port();
        drop(listener);
        std::fs::write(
            data.join("system.json"),
            serde_json::to_vec(&json!({"api":{"port":port}}))?,
        )?;
        let options = Options {
            python: python.into(),
            data: data.clone(),
            fake: true,
        };
        let client = Client::builder()
            .timeout(Duration::from_secs(1))
            .no_proxy()
            .build()?;
        let mut worker = Worker::start(&options)?;
        let deadline = Instant::now() + Duration::from_secs(20);
        let mut ready = false;
        while Instant::now() < deadline {
            if let Ok(value) = worker.call(&client, "GET", "/status", None, None) {
                if value["engine_ready"] == true {
                    ready = true;
                    break;
                }
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        worker.stop(&client);
        assert!(ready, "Worker never became ready");
        assert!(worker.child.try_wait()?.is_some());
        drop(worker);
        // Only remove the uniquely-created test directory under temp_dir.
        std::fs::remove_dir_all(data)?;
        Ok(())
    }
}
