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
    sync::mpsc::{Receiver, Sender},
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
    Fetch(String),
    Save(String, Value, u64),
    Test(String),
    Guild(String, String),
    ImportVoice(String, String, String),
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
        command
            .args(["-m", "discord_speak_bot", "--data-dir"])
            .arg(&options.data)
            .args(["serve", "--credentials-stdin"])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(0x08000000);
        if options.fake {
            command.arg("--fake");
        }
        let discord_token = if options.fake {
            String::new()
        } else {
            platform::read_token()?
        };
        let mut child = command
            .spawn()
            .context("Cannot start Python Worker. Run setup-worker.ps1 first.")?;
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
            bail!(
                "API {}: {}",
                status,
                value["error"]["message"]
                    .as_str()
                    .unwrap_or("Request failed")
            );
        }
        Ok(value)
    }

    fn stop(&mut self, client: &Client) {
        let _ = self.call(client, "POST", "/worker/shutdown", Some(&json!({})), None);
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if matches!(self.child.try_wait(), Ok(Some(_))) {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub fn run(options: Options, rx: Receiver<Action>, tx: Sender<Event>) {
    let client = match Client::builder()
        .timeout(Duration::from_secs(2))
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
    let mut failures = 0;
    let mut attempts = VecDeque::new();
    let mut preview: Option<String> = None;
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
                        Action::Fetch(path) => {
                            w.call(&client, "GET", &path, None, None).map(|v| (path, v))
                        }
                        Action::Save(path, body, rev) => w
                            .call(&client, "PATCH", &path, Some(&body), Some(rev))
                            .map(|v| (path, v)),
                        Action::Test(text) => w
                            .call(
                                &client,
                                "POST",
                                "/tts/test",
                                Some(&json!({"text": text})),
                                None,
                            )
                            .map(|v| ("/tts/test".into(), v)),
                        Action::Guild(id, action) => w
                            .call(
                                &client,
                                "POST",
                                &format!("/guilds/{id}/actions/{action}"),
                                Some(&json!({})),
                                None,
                            )
                            .map(|v| ("action".into(), v)),
                        Action::ImportVoice(id, path, text) => (|| {
                            if std::fs::metadata(&path)?.len() > 20 * 1024 * 1024 {
                                bail!("Reference WAV exceeds 20MiB");
                            }
                            let bytes = std::fs::read(path)?;
                            let encoded = base64::engine::general_purpose::STANDARD.encode(bytes);
                            w.call(&client, "POST", "/voices", Some(&json!({"voice_id": id, "name": id, "reference_text": text, "wav_base64": encoded})), None).map(|v| ("/voices".into(), v))
                        })(),
                        _ => unreachable!(),
                    },
                };
                let _ = tx.send(match result {
                    Ok((p, v)) => {
                        if p == "/tts/test" {
                            preview = v["operation_id"].as_str().map(str::to_owned);
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
                if let Some(id) = preview.clone() {
                    match w.call(&client, "GET", &format!("/operations/{id}"), None, None) {
                        Ok(value) if value["state"] == "completed" => {
                            preview = None;
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
                            preview = None;
                            let _ = tx.send(Event::Error("Preview generation failed".into()));
                        }
                        Err(_) => {
                            preview = None;
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
                let _ = tx.send(Event::Error("Worker recovery stopped after five failures. Fix configuration, then Restart Worker.".into()));
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
