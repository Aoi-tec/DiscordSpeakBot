#![windows_subsystem = "windows"]
mod events;
mod pages;
mod paths;
mod platform;
mod supervisor;

use eframe::egui;
use raw_window_handle::{HasWindowHandle, RawWindowHandle};
use serde_json::{json, Value};
use std::{path::PathBuf, sync::mpsc, time::Duration};
use supervisor::{Action, Event, Options};
use tray_icon::{
    menu::{Menu, MenuEvent, MenuItem},
    Icon, MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent,
};

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Page {
    Status,
    Discord,
    Speech,
    Servers,
    Voices,
    Advanced,
}

impl Page {
    const ALL: [Page; 6] = [
        Page::Status,
        Page::Discord,
        Page::Speech,
        Page::Servers,
        Page::Voices,
        Page::Advanced,
    ];

    fn label(self) -> &'static str {
        match self {
            Page::Status => "📊  ステータス",
            Page::Discord => "🔑  Discord",
            Page::Speech => "🔊  読み上げ設定",
            Page::Servers => "🖥  サーバー",
            Page::Voices => "🎙  ボイス",
            Page::Advanced => "🧩  詳細 (JSON)",
        }
    }
}

/// Form for registering a new voice.
#[derive(Default)]
pub struct NewVoice {
    pub id: String,
    pub name: String,
    pub path: String,
    pub text: String,
    pub allowed: String,
}

/// Inline editor for an existing voice.
pub struct VoiceEdit {
    pub id: String,
    pub name: String,
    pub text: String,
    pub allowed: String,
}

pub struct Host {
    tx: mpsc::Sender<Action>,
    rx: mpsc::Receiver<Event>,
    menu_rx: mpsc::Receiver<MenuEvent>,
    _tray: TrayIcon,
    show: MenuItem,
    _restart: MenuItem,
    exit: MenuItem,
    quitting: bool,
    _mutex: platform::OwnedHandle,

    pub page: Page,
    pub message: Option<(String, bool)>,
    pub status: Value,
    pub instance: String,

    // /settings/system
    pub system_rev: u64,
    pub system_orig: Value,
    pub system_edit: Value,
    pub restart_hint: bool,
    // /voices
    pub voices_rev: u64,
    pub voices: Value,
    pub new_voice: NewVoice,
    pub edit_voice: Option<VoiceEdit>,
    pub confirm_delete: Option<String>,
    pub preview: String,
    // /guilds/<id>
    pub guild_id: String,
    pub guild_rev: u64,
    pub guild_edit: Value,
    pub guild_loaded: String,
    // Discord token
    pub token: String,
    // Advanced raw editor
    pub raw_path: String,
    pub raw_editor: String,
    pub raw_rev: u64,
    pub raw_result: String,
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
            for family in [egui::FontFamily::Proportional, egui::FontFamily::Monospace] {
                fonts
                    .families
                    .entry(family)
                    .or_default()
                    .push("japanese".into());
            }
        }
        cc.egui_ctx.set_fonts(fonts);
        cc.egui_ctx.style_mut(|style| {
            style.spacing.item_spacing = egui::vec2(8.0, 8.0);
            style.spacing.button_padding = egui::vec2(10.0, 5.0);
        });
        let show = MenuItem::new("Settings / Status", true, None);
        let restart = MenuItem::new("Restart Worker", true, None);
        let exit = MenuItem::new("Exit", true, None);
        let menu = Menu::new();
        menu.append_items(&[&show, &restart, &exit])?;
        let icon = Icon::from_rgba([60, 130, 230, 255].repeat(32 * 32), 32, 32)?;
        let tray = TrayIconBuilder::new()
            .with_menu(Box::new(menu))
            // Left click opens the window, right click opens the menu.
            .with_menu_on_left_click(false)
            .with_tooltip("DiscordSpeakBot")
            .with_icon(icon)
            .build()?;
        let (tx, commands) = mpsc::channel();
        let (events, rx) = mpsc::channel();
        let wake_ctx = cc.egui_ctx.clone();
        let events = events::WakeSender::new(events, move || wake_ctx.request_repaint());
        let (menu_tx, menu_rx) = mpsc::channel();
        let wake_ctx = cc.egui_ctx.clone();
        let menu_tx = events::WakeSender::new(menu_tx, move || wake_ctx.request_repaint());
        // Native window handle, so the tray can un-hide the window without update().
        let hwnd = match cc.window_handle().map(|handle| handle.as_raw()) {
            Ok(RawWindowHandle::Win32(handle)) => handle.hwnd.get(),
            _ => 0,
        };
        let (show_id, restart_id, exit_id) =
            (show.id().clone(), restart.id().clone(), exit.id().clone());
        let tray_tx = tx.clone();
        MenuEvent::set_event_handler(Some(move |event: MenuEvent| {
            if event.id == show_id || event.id == exit_id {
                // Exit also needs a visible window: the Stopped -> Close step runs in update().
                platform::show_window(hwnd);
            }
            if event.id == restart_id {
                // Handled here because update() does not run while the window is hidden.
                let _ = tray_tx.send(Action::Restart);
            } else {
                let _ = menu_tx.send(event);
            }
        }));
        TrayIconEvent::set_event_handler(Some(move |event: TrayIconEvent| match event {
            TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            }
            | TrayIconEvent::DoubleClick {
                button: MouseButton::Left,
                ..
            } => platform::show_window(hwnd),
            _ => {}
        }));
        std::thread::spawn(move || supervisor::run(options, commands, events));
        Ok(Self {
            tx,
            rx,
            menu_rx,
            _tray: tray,
            show,
            _restart: restart,
            exit,
            quitting: false,
            _mutex: mutex,
            page: Page::Status,
            message: None,
            status: json!({}),
            instance: String::new(),
            system_rev: 0,
            system_orig: json!({}),
            system_edit: json!({}),
            restart_hint: false,
            voices_rev: 0,
            voices: json!({}),
            new_voice: NewVoice::default(),
            edit_voice: None,
            confirm_delete: None,
            preview: "こんにちは。音声のテストです。".into(),
            guild_id: String::new(),
            guild_rev: 0,
            guild_edit: json!({}),
            guild_loaded: String::new(),
            token: String::new(),
            raw_path: "/settings/system".into(),
            raw_editor: String::new(),
            raw_rev: 0,
            raw_result: String::new(),
        })
    }

    pub fn call(
        &self,
        tag: &str,
        method: &'static str,
        path: &str,
        body: Option<Value>,
        revision: Option<u64>,
    ) {
        let _ = self.tx.send(Action::Call {
            tag: tag.into(),
            method,
            path: path.into(),
            body,
            revision,
        });
    }

    pub fn send(&self, action: Action) {
        let _ = self.tx.send(action);
    }

    pub fn info(&mut self, text: impl Into<String>) {
        self.message = Some((text.into(), true));
    }

    pub fn error(&mut self, text: impl Into<String>) {
        self.message = Some((text.into(), false));
    }

    pub fn reload_all(&self) {
        self.call("system", "GET", "/settings/system", None, None);
        self.call("voices", "GET", "/voices", None, None);
    }

    pub fn load_guild(&mut self, id: &str) {
        self.guild_id = id.to_string();
        self.guild_loaded.clear();
        self.call("guild", "GET", &format!("/guilds/{id}"), None, None);
    }

    pub fn request_exit(&mut self, ctx: &egui::Context) {
        if !self.quitting {
            self.quitting = true;
            if self.tx.send(Action::Stop).is_err() {
                // Supervisor has already exited: no Stopped notification can arrive.
                ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            }
        }
    }

    fn handle_result(&mut self, tag: String, value: Value) {
        match tag.as_str() {
            "system" | "system_saved" => {
                let restart = value["restart_required"].as_bool().unwrap_or(false);
                let mut data = value.get("settings").unwrap_or(&value).clone();
                self.system_rev = data["revision"].as_u64().unwrap_or(0);
                if let Some(map) = data.as_object_mut() {
                    map.remove("revision");
                    map.remove("schema_version");
                }
                self.system_orig = data.clone();
                self.system_edit = data;
                if tag == "system_saved" {
                    self.restart_hint |= restart;
                    self.info(if restart {
                        "保存しました。反映には「Workerを再起動」が必要です。"
                    } else {
                        "保存しました。"
                    });
                }
            }
            "voices" => {
                self.voices_rev = value["revision"].as_u64().unwrap_or(0);
                self.voices = value["voices"].clone();
            }
            "voice_added" | "voice_updated" | "voice_deleted" => {
                self.info(match tag.as_str() {
                    "voice_added" => "ボイスを登録しました。",
                    "voice_updated" => "ボイスを更新しました。",
                    _ => "ボイスを削除しました。",
                });
                if tag == "voice_added" {
                    self.new_voice = NewVoice::default();
                }
                self.edit_voice = None;
                self.confirm_delete = None;
                self.reload_all();
            }
            "guild" | "guild_saved" => {
                self.guild_rev = value["revision"].as_u64().unwrap_or(0);
                let mut merged = pages::guild_template();
                if let (Some(target), Some(source)) =
                    (merged.as_object_mut(), value["settings"].as_object())
                {
                    for (key, item) in source {
                        target.insert(key.clone(), item.clone());
                    }
                }
                self.guild_edit = merged;
                self.guild_loaded = self.guild_id.clone();
                if tag == "guild_saved" {
                    self.info("サーバー設定を保存しました。");
                }
            }
            "action" => self.info("操作を実行しました。"),
            "/tts/test" => self.info("試聴を生成しています…"),
            "preview" => self.info("試聴を再生しています。"),
            "raw" | "raw_saved" => {
                self.raw_result = serde_json::to_string_pretty(&value).unwrap_or_default();
                let mut data = value.get("settings").unwrap_or(&value).clone();
                if data.is_null() {
                    data = json!({});
                }
                self.raw_rev = data["revision"]
                    .as_u64()
                    .or(value["revision"].as_u64())
                    .unwrap_or(0);
                if let Some(map) = data.as_object_mut() {
                    map.remove("revision");
                    map.remove("schema_version");
                }
                self.raw_editor = serde_json::to_string_pretty(&data).unwrap_or_default();
                if tag == "raw_saved" {
                    self.info("保存しました。");
                }
            }
            _ => {}
        }
    }
}

impl eframe::App for Host {
    fn update(&mut self, ctx: &egui::Context, _: &mut eframe::Frame) {
        ctx.request_repaint_after(Duration::from_millis(500));
        while let Ok(event) = self.menu_rx.try_recv() {
            if event.id == self.show.id() {
                ctx.send_viewport_cmd(egui::ViewportCommand::Visible(true));
                ctx.send_viewport_cmd(egui::ViewportCommand::Focus);
            }
            if event.id == self.exit.id() && !self.quitting {
                self.request_exit(ctx);
            }
        }
        while let Ok(event) = self.rx.try_recv() {
            match event {
                Event::Status(value) => {
                    let id = value["process_instance_id"]
                        .as_str()
                        .unwrap_or("")
                        .to_string();
                    if !id.is_empty() && id != self.instance {
                        // Worker (re)started: settings and voices may have changed.
                        self.instance = id;
                        self.restart_hint = false;
                        self.reload_all();
                        if !self.guild_id.is_empty() {
                            let guild = self.guild_id.clone();
                            self.load_guild(&guild);
                        }
                    }
                    self.status = value;
                }
                Event::Error(error) => self.error(error),
                Event::Result(tag, value) => self.handle_result(tag, value),
                Event::Stopped => {
                    self.quitting = true;
                    ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                    return;
                }
            }
        }
        if ctx.input(|i| i.viewport().close_requested()) && !self.quitting {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            ctx.send_viewport_cmd(egui::ViewportCommand::Visible(false));
        }
        // Dropping an audio file anywhere opens the voice registration form.
        let dropped: Vec<PathBuf> = ctx.input(|i| {
            i.raw
                .dropped_files
                .iter()
                .filter_map(|file| file.path.clone())
                .collect()
        });
        if let Some(path) = dropped.into_iter().next() {
            self.page = Page::Voices;
            pages::use_dropped_file(&mut self.new_voice, &path);
        }

        egui::SidePanel::left("navigation")
            .resizable(false)
            .exact_width(180.0)
            .show(ctx, |ui| {
                ui.add_space(8.0);
                ui.heading("DiscordSpeakBot");
                ui.add_space(4.0);
                pages::state_badge(ui, &self.status);
                ui.separator();
                for page in Page::ALL {
                    let selected = self.page == page;
                    let button = egui::Button::new(egui::RichText::new(page.label()).size(15.0))
                        .selected(selected)
                        .min_size(egui::vec2(ui.available_width(), 32.0));
                    if ui.add(button).clicked() && !selected {
                        self.page = page;
                        match page {
                            Page::Speech | Page::Voices => self.reload_all(),
                            Page::Servers if !self.guild_id.is_empty() => {
                                let guild = self.guild_id.clone();
                                self.load_guild(&guild);
                            }
                            _ => {}
                        }
                    }
                }
                ui.with_layout(egui::Layout::bottom_up(egui::Align::Min), |ui| {
                    ui.add_space(8.0);
                    if self.quitting {
                        ui.label("停止しています…");
                    } else if ui
                        .add_sized([ui.available_width(), 28.0], egui::Button::new("⏻  終了"))
                        .clicked()
                    {
                        self.request_exit(ctx);
                    }
                    let mut restart = egui::Button::new("🔄  Workerを再起動");
                    if self.restart_hint {
                        restart = restart.fill(egui::Color32::from_rgb(180, 120, 20));
                    }
                    if ui
                        .add_sized([ui.available_width(), 28.0], restart)
                        .clicked()
                    {
                        self.send(Action::Restart);
                        self.info("Workerを再起動しています…");
                    }
                    ui.small("×で閉じてもトレイに残ります");
                });
            });

        egui::TopBottomPanel::bottom("message").show(ctx, |ui| {
            let mut clear = false;
            ui.horizontal(|ui| match &self.message {
                Some((text, ok)) => {
                    let color = if *ok {
                        egui::Color32::from_rgb(70, 170, 110)
                    } else {
                        egui::Color32::from_rgb(220, 80, 80)
                    };
                    ui.colored_label(color, if *ok { "✔" } else { "⚠" });
                    ui.label(text);
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        clear = ui.small_button("✕").clicked();
                    });
                }
                None => {
                    ui.weak("準備完了");
                }
            });
            if clear {
                self.message = None;
            }
        });

        egui::CentralPanel::default().show(ctx, |ui| {
            egui::ScrollArea::vertical()
                .auto_shrink([false, false])
                .show(ui, |ui| {
                    ui.set_max_width(760.0);
                    match self.page {
                        Page::Status => self.page_status(ui),
                        Page::Discord => self.page_discord(ui),
                        Page::Speech => self.page_speech(ui),
                        Page::Servers => self.page_servers(ui),
                        Page::Voices => self.page_voices(ui),
                        Page::Advanced => self.page_advanced(ui),
                    }
                });
        });
    }
}

fn main() -> eframe::Result<()> {
    let mut args = std::env::args().skip(1);
    let mut python = std::env::current_exe()
        .ok()
        .and_then(|path| paths::project_python(&path))
        .unwrap_or_else(|| PathBuf::from("python.exe"));
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
            .with_inner_size([980.0, 760.0])
            .with_min_inner_size([760.0, 520.0])
            .with_drag_and_drop(true)
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
