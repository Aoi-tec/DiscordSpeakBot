#![windows_subsystem = "windows"]
mod platform;
mod supervisor;

use eframe::egui;
use serde_json::{json, Value};
use std::{path::PathBuf, sync::mpsc, time::Duration};
use supervisor::{Action, Event, Options};
use tray_icon::{
    menu::{Menu, MenuEvent, MenuItem},
    Icon, TrayIcon, TrayIconBuilder,
};

struct Host {
    tx: mpsc::Sender<Action>,
    rx: mpsc::Receiver<Event>,
    _tray: TrayIcon,
    show: MenuItem,
    restart: MenuItem,
    exit: MenuItem,
    status: String,
    result: String,
    editor: String,
    path: String,
    revision: u64,
    token: String,
    preview: String,
    guild: String,
    quitting: bool,
    voice_id: String,
    wav_path: String,
    reference_text: String,
    _mutex: platform::OwnedHandle,
}

impl Host {
    fn new(
        cc: &eframe::CreationContext<'_>,
        options: Options,
        mutex: platform::OwnedHandle,
    ) -> anyhow::Result<Self> {
        // Use the Windows Japanese font when available.
        let mut fonts = egui::FontDefinitions::default();
        if let Ok(bytes) = std::fs::read("C:/Windows/Fonts/meiryo.ttc") {
            fonts
                .font_data
                .insert("japanese".into(), egui::FontData::from_owned(bytes).into());
            fonts
                .families
                .entry(egui::FontFamily::Proportional)
                .or_default()
                .push("japanese".into());
        }
        cc.egui_ctx.set_fonts(fonts);
        let show = MenuItem::new("Settings / Status", true, None);
        let restart = MenuItem::new("Restart Worker", true, None);
        let exit = MenuItem::new("Exit", true, None);
        let menu = Menu::new();
        menu.append_items(&[&show, &restart, &exit])?;
        let icon = Icon::from_rgba([60, 130, 230, 255].repeat(32 * 32), 32, 32)?;
        let tray = TrayIconBuilder::new()
            .with_menu(Box::new(menu))
            .with_tooltip("DiscordSpeakBot")
            .with_icon(icon)
            .build()?;
        let (tx, commands) = mpsc::channel();
        let (events, rx) = mpsc::channel();
        std::thread::spawn(move || supervisor::run(options, commands, events));
        Ok(Self {
            tx,
            rx,
            _tray: tray,
            show,
            restart,
            exit,
            status: "Starting Worker...".into(),
            result: String::new(),
            editor: String::new(),
            path: "/settings/system".into(),
            revision: 0,
            token: String::new(),
            preview: "こんにちは。音声のテストです。".into(),
            guild: String::new(),
            quitting: false,
            _mutex: mutex,
            voice_id: String::new(),
            wav_path: String::new(),
            reference_text: String::new(),
        })
    }
}

impl eframe::App for Host {
    fn update(&mut self, ctx: &egui::Context, _: &mut eframe::Frame) {
        ctx.request_repaint_after(Duration::from_millis(200));
        while let Ok(event) = MenuEvent::receiver().try_recv() {
            if event.id == self.show.id() {
                ctx.send_viewport_cmd(egui::ViewportCommand::Visible(true));
                ctx.send_viewport_cmd(egui::ViewportCommand::Focus);
            }
            if event.id == self.restart.id() {
                let _ = self.tx.send(Action::Restart);
            }
            if event.id == self.exit.id() && !self.quitting {
                self.quitting = true;
                let _ = self.tx.send(Action::Stop);
            }
        }
        while let Ok(event) = self.rx.try_recv() {
            match event {
                Event::Status(value) => {
                    self.status = serde_json::to_string_pretty(&value).unwrap_or_default()
                }
                Event::Error(error) => self.result = error,
                Event::Result(path, value) => {
                    self.result = serde_json::to_string_pretty(&value).unwrap_or_default();
                    if path == self.path {
                        let mut data = value
                            .get("settings")
                            .filter(|v| !v.is_null())
                            .unwrap_or(&value)
                            .clone();
                        self.revision = data["revision"]
                            .as_u64()
                            .or(value["revision"].as_u64())
                            .unwrap_or(0);
                        if let Some(map) = data.as_object_mut() {
                            map.remove("revision");
                            map.remove("schema_version");
                        }
                        self.editor = serde_json::to_string_pretty(&data).unwrap_or_default();
                    }
                }
                Event::Stopped => {
                    ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                    return;
                }
            }
        }
        if ctx.input(|i| i.viewport().close_requested()) && !self.quitting {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            ctx.send_viewport_cmd(egui::ViewportCommand::Visible(false));
        }
        egui::CentralPanel::default().show(ctx, |ui| {
            ui.heading("DiscordSpeakBot");
            ui.label("Close hides this window. Use Tray > Exit to stop the Worker.");
            ui.horizontal(|ui| {
                if ui.button("Restart Worker").clicked() { let _ = self.tx.send(Action::Restart); }
                if ui.button("Exit").clicked() && !self.quitting { self.quitting = true; let _ = self.tx.send(Action::Stop); }
                if self.quitting { ui.label("Stopping..."); }
            });
            egui::ScrollArea::vertical().show(ui, |ui| {
                ui.collapsing("Status", |ui| { ui.monospace(&self.status); });
                ui.collapsing("Discord Token (Windows Credential Manager)", |ui| {
                    ui.add(egui::TextEdit::singleline(&mut self.token).password(true));
                    if ui.button("Save Token").clicked() {
                        self.result = match platform::write_token(&self.token) { Ok(()) => "Saved. Restart Worker to apply.".into(), Err(e) => e.to_string() };
                        self.token.clear();
                    }
                });
                ui.label("Settings path: /settings/system or /guilds/<id>. Fetch before Save.");
                ui.text_edit_singleline(&mut self.path);
                ui.horizontal(|ui| {
                    if ui.button("Fetch").clicked() { let _ = self.tx.send(Action::Fetch(self.path.clone())); }
                    if ui.button("Save JSON patch").clicked() {
                        match serde_json::from_str::<Value>(&self.editor) {
                            Ok(value) => { let _ = self.tx.send(Action::Save(self.path.clone(), value, self.revision)); }
                            Err(e) => self.result = e.to_string(),
                        }
                    }
                    ui.label(format!("revision: {}", self.revision));
                });
                ui.add(egui::TextEdit::multiline(&mut self.editor).code_editor().desired_rows(14).desired_width(f32::INFINITY));
                ui.separator();
                ui.label("Guild ID"); ui.text_edit_singleline(&mut self.guild);
                ui.horizontal(|ui| {
                    for name in ["join", "leave", "skip", "clear"] {
                        if ui.button(name).clicked() { let _ = self.tx.send(Action::Guild(self.guild.clone(), name.into())); }
                    }
                    if ui.button("Voices").clicked() { let _ = self.tx.send(Action::Fetch("/voices".into())); }
                });
                ui.text_edit_singleline(&mut self.preview);
                if ui.button("Generate preview").clicked() { let _ = self.tx.send(Action::Test(self.preview.clone())); }
                ui.collapsing("Add Voice (16-bit PCM WAV, 1-30 seconds)", |ui| {
                    ui.label("Voice ID (letters, numbers, underscore, hyphen)"); ui.text_edit_singleline(&mut self.voice_id);
                    ui.label("Reference WAV full path"); ui.text_edit_singleline(&mut self.wav_path);
                    ui.label("Exact reference transcript"); ui.text_edit_multiline(&mut self.reference_text);
                    if ui.button("Import Voice").clicked() { let _ = self.tx.send(Action::ImportVoice(self.voice_id.clone(), self.wav_path.clone(), self.reference_text.clone())); }
                    ui.label("After import, set defaults.voice_id in system settings. Profiles are built on first use.");
                });
                ui.label("Result"); ui.monospace(&self.result);
            });
        });
    }
}

fn main() -> eframe::Result<()> {
    let mut args = std::env::args().skip(1);
    let mut python = PathBuf::from("python.exe");
    let mut data = PathBuf::from(std::env::var("LOCALAPPDATA").unwrap_or_else(|_| ".".into()))
        .join("DiscordSpeakBot");
    let mut fake = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--python" => {
                if let Some(value) = args.next() {
                    python = value.into();
                }
            }
            "--data-dir" => {
                if let Some(value) = args.next() {
                    data = value.into();
                }
            }
            "--fake" => fake = true,
            _ => {}
        }
    }
    let mutex = platform::single_instance().map_err(|e| eframe::Error::AppCreation(e.into()))?;
    let config: Value = std::fs::read(data.join("system.json"))
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or(json!({}));
    let minimized = config["startup"]["start_minimized"]
        .as_bool()
        .unwrap_or(false);
    let native = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([850.0, 720.0])
            .with_visible(!minimized),
        ..Default::default()
    };
    eframe::run_native(
        "DiscordSpeakBot",
        native,
        Box::new(move |cc| {
            Host::new(cc, Options { python, data, fake }, mutex)
                .map(|app| Box::new(app) as Box<dyn eframe::App>)
                .map_err(Into::into)
        }),
    )
}
