//! Settings pages. Every page is a single vertical column of labelled fields.
use crate::{platform, supervisor::Action, Host, NewVoice, Page, VoiceEdit};
use eframe::egui::{self, Color32, RichText};
use serde_json::{json, Value};
use std::{ops::RangeInclusive, path::Path};

const GREEN: Color32 = Color32::from_rgb(70, 170, 110);
const ORANGE: Color32 = Color32::from_rgb(220, 150, 40);
const RED: Color32 = Color32::from_rgb(220, 80, 80);

// ---------------------------------------------------------------- layout helpers

fn title(ui: &mut egui::Ui, text: &str, subtitle: &str) {
    ui.add_space(6.0);
    ui.heading(text);
    if !subtitle.is_empty() {
        ui.weak(subtitle);
    }
    ui.add_space(6.0);
}

fn section(ui: &mut egui::Ui, heading: &str, add: impl FnOnce(&mut egui::Ui)) {
    egui::Frame::group(ui.style())
        .inner_margin(egui::Margin::same(12))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.label(RichText::new(heading).strong().size(16.0));
            ui.add_space(4.0);
            add(ui);
        });
    ui.add_space(10.0);
}

/// One vertical field: label, widget, optional hint underneath.
fn field(ui: &mut egui::Ui, label: &str, hint: &str, add: impl FnOnce(&mut egui::Ui)) {
    ui.label(RichText::new(label).strong());
    add(ui);
    if !hint.is_empty() {
        ui.small(RichText::new(hint).weak());
    }
    ui.add_space(6.0);
}

fn int_value(ui: &mut egui::Ui, value: &mut Value, range: RangeInclusive<i64>, suffix: &str) {
    let mut number = value.as_i64().unwrap_or(*range.start());
    let slider = egui::Slider::new(&mut number, range).suffix(suffix);
    if ui.add(slider).changed() {
        *value = json!(number);
    }
}

fn bool_value(ui: &mut egui::Ui, value: &mut Value, text: &str) {
    let mut flag = value.as_bool().unwrap_or(false);
    if ui.checkbox(&mut flag, text).changed() {
        *value = json!(flag);
    }
}

fn text_value(ui: &mut egui::Ui, value: &mut Value, hint: &str) {
    let mut text = value.as_str().unwrap_or("").to_string();
    let edit = egui::TextEdit::singleline(&mut text)
        .hint_text(hint)
        .desired_width(f32::INFINITY);
    if ui.add(edit).changed() {
        *value = json!(text);
    }
}

fn choice_value(ui: &mut egui::Ui, id: &str, value: &mut Value, options: &[(&str, &str)]) {
    let current = value.as_str().unwrap_or("").to_string();
    let shown = options
        .iter()
        .find(|(key, _)| *key == current)
        .map(|(_, label)| *label)
        .unwrap_or(current.as_str())
        .to_string();
    egui::ComboBox::from_id_salt(id)
        .selected_text(shown)
        .width(260.0)
        .show_ui(ui, |ui| {
            for (key, label) in options {
                if ui.selectable_label(current == *key, *label).clicked() {
                    *value = json!(key);
                }
            }
        });
}

pub fn parse_ids(text: &str) -> Vec<String> {
    let mut ids: Vec<String> = Vec::new();
    for part in text.split(|c: char| !c.is_ascii_digit()) {
        if (1..=20).contains(&part.len()) && !ids.iter().any(|id| id == part) {
            ids.push(part.to_string());
        }
    }
    ids
}

fn ids_text(value: &Value) -> String {
    value
        .as_array()
        .map(|items| {
            items
                .iter()
                .map(|item| match item {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .collect::<Vec<_>>()
                .join("\n")
        })
        .unwrap_or_default()
}

/// Multi-line ID list stored as a JSON array. The raw text is kept between frames so
/// typing a newline or comma is not immediately normalised away.
fn id_list_value(ui: &mut egui::Ui, key: &str, value: &mut Value, numeric: bool) {
    let id = ui.make_persistent_id(key);
    let canonical = ids_text(value);
    let mut text = ui
        .data_mut(|d| d.get_temp::<(String, String)>(id))
        .filter(|(saved, _)| *saved == canonical)
        .map(|(_, text)| text)
        .unwrap_or_else(|| canonical.clone());
    let edit = egui::TextEdit::multiline(&mut text)
        .desired_rows(2)
        .desired_width(f32::INFINITY)
        .hint_text("1行に1つ（カンマ・スペース区切りも可）");
    if ui.add(edit).changed() {
        let ids = parse_ids(&text);
        *value = if numeric {
            json!(ids
                .iter()
                .filter_map(|s| s.parse::<u64>().ok())
                .collect::<Vec<_>>())
        } else {
            json!(ids)
        };
    }
    let canonical = ids_text(value);
    ui.data_mut(|d| d.insert_temp(id, (canonical, text)));
}

fn valid_voice_id(id: &str) -> bool {
    let mut chars = id.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_alphanumeric())
        && id.len() <= 64
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

fn shorten(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        text.to_string()
    } else {
        text.chars().take(limit).collect::<String>() + "…"
    }
}

pub fn guild_template() -> Value {
    json!({
        "enabled": true,
        "text_channel_ids": [],
        "auto_rejoin": false,
        "max_queue": 10,
        "merge_window_ms": 200,
        "skip_urls": true,
        "skip_codeblocks": true,
        "read_bot_messages": false,
        "announce_voice_state": true,
        "admin_role_ids": [],
        "clear_by_role": false,
        "clear_role_ids": [],
    })
}

pub fn use_dropped_file(form: &mut NewVoice, path: &Path) {
    form.path = path.display().to_string();
    let stem = path
        .file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    if form.id.is_empty() {
        let id: String = stem
            .chars()
            .filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-')
            .take(64)
            .collect();
        if valid_voice_id(&id) {
            form.id = id;
        }
    }
    if form.name.is_empty() {
        form.name = stem.chars().take(80).collect();
    }
}

pub fn state_badge(ui: &mut egui::Ui, status: &Value) {
    let (text, color) = match status["state"].as_str() {
        None => ("● Worker接続待ち", Color32::GRAY),
        Some("ready") => ("● 稼働中", GREEN),
        Some("degraded") => ("● 一部不調", ORANGE),
        Some("starting") => ("● 起動中", Color32::GRAY),
        Some("stopping") => ("● 停止中", Color32::GRAY),
        Some(_) => ("● エラー", RED),
    };
    ui.colored_label(color, RichText::new(text).strong());
}

fn yes_no(ui: &mut egui::Ui, value: &Value, yes: &str, no: &str) {
    if value.as_bool().unwrap_or(false) {
        ui.colored_label(GREEN, yes);
    } else {
        ui.colored_label(ORANGE, no);
    }
}

enum VoiceOp {
    Preview(String),
    MakeDefault(String),
    Edit(VoiceEdit),
    AskDelete(String),
    Delete(String),
    SaveEdit,
    Cancel,
}

// ---------------------------------------------------------------- pages

impl Host {
    pub fn page_status(&mut self, ui: &mut egui::Ui) {
        title(ui, "ステータス", "Botの動作状況です（2秒ごとに更新）。");
        let status = self.status.clone();
        section(ui, "Worker", |ui| {
            egui::Grid::new("status_grid")
                .num_columns(2)
                .spacing([24.0, 8.0])
                .show(ui, |ui| {
                    ui.label("状態");
                    state_badge(ui, &status);
                    ui.end_row();
                    ui.label("音声エンジン");
                    yes_no(ui, &status["engine_ready"], "準備完了", "読み込み中 / 停止");
                    ui.end_row();
                    ui.label("Discord");
                    yes_no(ui, &status["discord_connected"], "接続中", "未接続");
                    ui.end_row();
                    ui.label("直近の生成時間");
                    ui.label(match status["metrics"]["ttfa_ms"].as_f64() {
                        Some(ms) => format!("{ms:.0} ms"),
                        None => "—".into(),
                    });
                    ui.end_row();
                    ui.label("生成 完了 / 失敗");
                    ui.label(format!(
                        "{} / {}",
                        status["metrics"]["completed"].as_u64().unwrap_or(0),
                        status["metrics"]["failed"].as_u64().unwrap_or(0)
                    ));
                    ui.end_row();
                    ui.label("最後のエラー");
                    match status["last_error"].as_str() {
                        Some(error) => ui.colored_label(RED, error),
                        None => ui.label("なし"),
                    };
                    ui.end_row();
                    if status["development_mode"] == true {
                        ui.label("モード");
                        ui.colored_label(ORANGE, "開発モード（無音）");
                        ui.end_row();
                    }
                });
        });
        let mut open_guild = None;
        section(ui, "参加中のサーバー", |ui| {
            let guilds = status["discord_guilds"]
                .as_array()
                .cloned()
                .unwrap_or_default();
            if guilds.is_empty() {
                ui.weak("Discordに接続すると表示されます。");
            }
            for guild in guilds {
                let id = guild["id"].as_str().unwrap_or("").to_string();
                let runtime = &status["guilds"][&id];
                ui.horizontal(|ui| {
                    ui.label(RichText::new(guild["name"].as_str().unwrap_or("?")).strong());
                    if runtime["connected"] == true {
                        ui.colored_label(GREEN, "🔊 VC接続中");
                        ui.weak(format!(
                            "待機 {} ・ 再生待ち {}",
                            runtime["text"].as_u64().unwrap_or(0),
                            runtime["generated"].as_u64().unwrap_or(0)
                        ));
                    } else {
                        ui.weak("VC未接続");
                    }
                    if ui.small_button("設定を開く").clicked() {
                        open_guild = Some(id.clone());
                    }
                });
            }
        });
        if let Some(id) = open_guild {
            self.page = Page::Servers;
            self.load_guild(&id);
        }
        ui.collapsing("詳細（JSON）", |ui| {
            ui.monospace(serde_json::to_string_pretty(&status).unwrap_or_default());
        });
    }

    pub fn page_discord(&mut self, ui: &mut egui::Ui) {
        title(ui, "Discord", "Botトークンの設定です。");
        let mut saved = None;
        section(ui, "Botトークン", |ui| {
            field(
                ui,
                "トークン",
                "Windows資格情報マネージャーに保存されます。保存後に「Workerを再起動」してください。",
                |ui| {
                    ui.add(
                        egui::TextEdit::singleline(&mut self.token)
                            .password(true)
                            .desired_width(f32::INFINITY),
                    );
                },
            );
            if ui
                .add_enabled(!self.token.trim().is_empty(), egui::Button::new("💾 保存"))
                .clicked()
            {
                saved = Some(platform::write_token(self.token.trim()));
                self.token.clear();
            }
        });
        match saved {
            Some(Ok(())) => {
                self.restart_hint = true;
                self.info("トークンを保存しました。「Workerを再起動」で反映されます。");
            }
            Some(Err(error)) => self.error(error.to_string()),
            None => {}
        }
        section(ui, "Discord側の準備", |ui| {
            ui.label("1. Developer Portal で Message Content Intent を有効にする");
            ui.label("2. bot と applications.commands のスコープでサーバーに招待する");
            ui.label("3. 読み上げチャンネルの閲覧、VCの接続・発言を許可する");
            ui.label("4. Discordで /yomiage join を実行する");
        });
    }

    pub fn page_speech(&mut self, ui: &mut egui::Ui) {
        title(
            ui,
            "読み上げ設定",
            "Bot全体の設定です。ユーザーが自分で設定していない項目にはここの値が使われます。",
        );
        if self
            .system_edit
            .as_object()
            .is_none_or(|map| map.is_empty())
        {
            ui.label("読み込み中…（Workerが起動していない場合は表示されません）");
            if ui.button("再読み込み").clicked() {
                self.reload_all();
            }
            return;
        }
        let changed = self.system_edit != self.system_orig;
        self.save_bar(ui, changed, "speech_top");
        let public: Vec<(String, String)> = self
            .voices
            .as_object()
            .map(|voices| {
                voices
                    .iter()
                    .filter(|(_, v)| {
                        v["allowed_user_ids"]
                            .as_array()
                            .is_none_or(|ids| ids.is_empty())
                    })
                    .map(|(id, v)| (id.clone(), v["name"].as_str().unwrap_or(id).to_string()))
                    .collect()
            })
            .unwrap_or_default();
        let edit = &mut self.system_edit;
        section(ui, "既定の読み上げ", |ui| {
            field(
                ui,
                "既定ボイス",
                "自分のボイスを選んでいない人に使われます（専用ボイスは選べません）。",
                |ui| {
                    let options: Vec<(&str, &str)> = public
                        .iter()
                        .map(|(id, name)| (id.as_str(), name.as_str()))
                        .collect();
                    choice_value(
                        ui,
                        "default_voice",
                        &mut edit["defaults"]["voice_id"],
                        &options,
                    );
                },
            );
            field(ui, "速度", "100% が等速です。", |ui| {
                int_value(ui, &mut edit["defaults"]["speed_percent"], 50..=200, " %")
            });
            field(ui, "音量", "", |ui| {
                int_value(ui, &mut edit["defaults"]["volume_percent"], 0..=100, " %")
            });
        });
        section(ui, "読み上げ待ちと長さ", |ui| {
            let queue = &mut edit["queue"];
            field(
                ui,
                "サーバーごとの読み上げ待ち上限",
                "",
                |ui| int_value(ui, &mut queue["max_text_queue"], 1..=100, " 件"),
            );
            field(
                ui,
                "連続投稿をまとめる時間",
                "同じ人の連投を1回で読みます。",
                |ui| int_value(ui, &mut queue["merge_window_ms"], 0..=2000, " ms"),
            );
            field(ui, "1回に読む最大文字数", "", |ui| {
                int_value(ui, &mut queue["max_text_chars"], 10..=1000, " 文字")
            });
            field(
                ui,
                "長文の最大分割数",
                "これを超えた部分は省略されます。",
                |ui| int_value(ui, &mut queue["max_segments"], 1..=10, " 回"),
            );
            field(
                ui,
                "読み上げ待ちの有効期限",
                "古くなったメッセージは読みません。",
                |ui| int_value(ui, &mut queue["job_ttl_seconds"], 1..=300, " 秒"),
            );
            field(ui, "1回の音声の最大長", "", |ui| {
                int_value(ui, &mut queue["max_audio_seconds"], 1..=60, " 秒")
            });
            field(ui, "先に生成しておく数", "", |ui| {
                int_value(ui, &mut queue["max_generated_queue"], 1..=10, " 件")
            });
            field(ui, "生成済み音声のメモリ上限", "", |ui| {
                int_value(ui, &mut queue["audio_budget_mib"], 16..=512, " MiB")
            });
        });
        section(
            ui,
            "音声エンジン（保存後に再起動が必要）",
            |ui| {
                let tts = &mut edit["tts"];
                field(
                    ui,
                    "モデルのフォルダ",
                    "ダウンロード済みのQwen3-TTS Baseモデル",
                    |ui| {
                        text_value(
                            ui,
                            &mut tts["model_path"],
                            "D:\\...\\Qwen3-TTS-12Hz-1.7B-Base",
                        )
                    },
                );
                field(
                    ui,
                    "GPU UUID",
                    "nvidia-smi -L で確認できます。",
                    |ui| text_value(ui, &mut tts["gpu_uuid"], "GPU-xxxxxxxx-..."),
                );
                field(ui, "精度", "", |ui| {
                    choice_value(
                        ui,
                        "dtype",
                        &mut tts["dtype"],
                        &[("bfloat16", "bfloat16（推奨）"), ("float16", "float16")],
                    )
                });
                field(ui, "最大トークン数", "", |ui| {
                    int_value(ui, &mut tts["max_new_tokens"], 32..=2048, "")
                });
                field(ui, "生成のタイムアウト", "", |ui| {
                    int_value(ui, &mut tts["inference_timeout_seconds"], 10..=600, " 秒")
                });
                field(
                    ui,
                    "ffmpeg のパス",
                    "PATHが通っていれば ffmpeg のままでOK",
                    |ui| text_value(ui, &mut tts["ffmpeg_path"], "ffmpeg"),
                );
            },
        );
        section(
            ui,
            "パフォーマンス（保存後に再起動が必要）",
            |ui| {
                let performance = &mut edit["performance"];
                field(ui, "CPUの使い方", "", |ui| {
                    choice_value(
                        ui,
                        "cpu_mode",
                        &mut performance["cpu_mode"],
                        &[
                            ("automatic", "自動"),
                            ("low_impact", "低負荷（ゲーム併用向け）"),
                            ("custom", "CPU Setを指定"),
                        ],
                    )
                });
                if performance["cpu_mode"] == "custom" {
                    field(
                        ui,
                        "CPU Set ID",
                        "Windows の CPU Set ID（論理CPU番号とは別）",
                        |ui| {
                            id_list_value(ui, "cpu_set_ids", &mut performance["cpu_set_ids"], true)
                        },
                    );
                }
                field(ui, "プロセス優先度", "", |ui| {
                    choice_value(
                        ui,
                        "priority",
                        &mut performance["process_priority"],
                        &[("below_normal", "通常以下（推奨）"), ("normal", "通常")],
                    )
                });
            },
        );
        section(ui, "起動", |ui| {
            bool_value(
                ui,
                &mut edit["startup"]["start_minimized"],
                "起動時にウィンドウを出さずトレイに入れる",
            );
        });
        self.save_bar(ui, changed, "speech_bottom");
    }

    fn save_bar(&mut self, ui: &mut egui::Ui, changed: bool, id: &str) {
        ui.push_id(id, |ui| {
            ui.horizontal(|ui| {
                if ui
                    .add_enabled(changed, egui::Button::new("💾 変更を保存"))
                    .clicked()
                {
                    let mut patch = serde_json::Map::new();
                    for key in ["defaults", "queue", "tts", "performance", "startup", "api"] {
                        if self.system_edit[key] != self.system_orig[key] {
                            patch.insert(key.into(), self.system_edit[key].clone());
                        }
                    }
                    self.call(
                        "system_saved",
                        "PATCH",
                        "/settings/system",
                        Some(Value::Object(patch)),
                        Some(self.system_rev),
                    );
                }
                if ui
                    .add_enabled(changed, egui::Button::new("↩ 元に戻す"))
                    .clicked()
                {
                    self.system_edit = self.system_orig.clone();
                }
                if changed {
                    ui.colored_label(ORANGE, "未保存の変更があります");
                }
            });
        });
        ui.add_space(6.0);
    }

    pub fn page_servers(&mut self, ui: &mut egui::Ui) {
        title(ui, "サーバー", "サーバーごとの読み上げ設定です。");
        let guilds = self.status["discord_guilds"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        let mut select = None;
        section(ui, "サーバーを選択", |ui| {
            let current = guilds
                .iter()
                .find(|g| g["id"] == self.guild_id.as_str())
                .and_then(|g| g["name"].as_str())
                .map(str::to_owned)
                .unwrap_or_else(|| {
                    if self.guild_id.is_empty() {
                        "選択してください".into()
                    } else {
                        self.guild_id.clone()
                    }
                });
            egui::ComboBox::from_id_salt("guild_select")
                .selected_text(current)
                .width(320.0)
                .show_ui(ui, |ui| {
                    for guild in &guilds {
                        let id = guild["id"].as_str().unwrap_or("");
                        let label = format!("{}  ({id})", guild["name"].as_str().unwrap_or("?"));
                        if ui.selectable_label(self.guild_id == id, label).clicked() {
                            select = Some(id.to_string());
                        }
                    }
                });
            if guilds.is_empty() {
                ui.weak("Discordに接続すると、Botが参加しているサーバーが表示されます。");
            }
        });
        if let Some(id) = select {
            self.load_guild(&id);
        }
        if self.guild_id.is_empty() {
            return;
        }
        if self.guild_loaded != self.guild_id {
            ui.label("読み込み中…");
            return;
        }
        let guild_id = self.guild_id.clone();
        let edit = &mut self.guild_edit;
        section(ui, "基本", |ui| {
            bool_value(ui, &mut edit["enabled"], "このサーバーで読み上げる");
            bool_value(
                ui,
                &mut edit["announce_voice_state"],
                "入退室を本人のボイスで案内する（「〇〇さんが入室しました」）",
            );
            bool_value(
                ui,
                &mut edit["auto_rejoin"],
                "Bot再起動時に前回のVCへ自動で参加する",
            );
            bool_value(ui, &mut edit["read_bot_messages"], "他のBotの投稿も読む");
            bool_value(ui, &mut edit["skip_urls"], "URLを読まない");
            bool_value(ui, &mut edit["skip_codeblocks"], "コードブロックを読まない");
        });
        section(ui, "読み上げチャンネル", |ui| {
            field(
                ui,
                "チャンネルID",
                "Discordでチャンネルを右クリック →「IDをコピー」。/yomiage add channel でも追加できます。",
                |ui| id_list_value(ui, "text_channels", &mut edit["text_channel_ids"], false),
            );
        });
        section(ui, "読み上げ待ち", |ui| {
            field(
                ui,
                "このサーバーの読み上げ待ち上限",
                "",
                |ui| int_value(ui, &mut edit["max_queue"], 1..=100, " 件"),
            );
            field(ui, "連続投稿をまとめる時間", "", |ui| {
                int_value(ui, &mut edit["merge_window_ms"], 0..=2000, " ms")
            });
        });
        section(ui, "権限", |ui| {
            field(
                ui,
                "管理ロールID",
                "サーバー管理権限がなくても、このロールの人は管理者として扱います。",
                |ui| id_list_value(ui, "admin_roles", &mut edit["admin_role_ids"], false),
            );
            bool_value(
                ui,
                &mut edit["clear_by_role"],
                "ロールでの /yomiage clear を許可する",
            );
            if edit["clear_by_role"] == true {
                field(ui, "clear を許可するロールID", "", |ui| {
                    id_list_value(ui, "clear_roles", &mut edit["clear_role_ids"], false)
                });
            }
        });
        let mut action = None;
        let mut save = false;
        section(ui, "操作", |ui| {
            ui.horizontal(|ui| {
                for (name, label) in [
                    ("join", "🔊 前回のVCに参加"),
                    ("leave", "👋 退出"),
                    ("skip", "⏭ スキップ"),
                    ("clear", "🗑 読み上げ待ちを削除"),
                ] {
                    if ui.button(label).clicked() {
                        action = Some(name);
                    }
                }
            });
        });
        ui.horizontal(|ui| {
            save = ui.button("💾 サーバー設定を保存").clicked();
            if ui.button("↩ 読み込み直す").clicked() {
                action = Some("reload");
            }
        });
        if save {
            self.call(
                "guild_saved",
                "PATCH",
                &format!("/guilds/{guild_id}"),
                Some(self.guild_edit.clone()),
                Some(self.guild_rev),
            );
        }
        match action {
            Some("reload") => self.load_guild(&guild_id),
            Some(name) => self.call(
                "action",
                "POST",
                &format!("/guilds/{guild_id}/actions/{name}"),
                Some(json!({})),
                None,
            ),
            None => {}
        }
    }

    pub fn page_voices(&mut self, ui: &mut egui::Ui) {
        title(
            ui,
            "ボイス",
            "読み上げに使う声の登録・変更・削除です。専用ボイスは指定した人しか使えません。",
        );
        section(ui, "試聴に使う文章", |ui| {
            ui.add(egui::TextEdit::singleline(&mut self.preview).desired_width(f32::INFINITY));
        });

        let default = self.system_orig["defaults"]["voice_id"]
            .as_str()
            .unwrap_or("")
            .to_string();
        let mut voices: Vec<(String, Value)> = self
            .voices
            .as_object()
            .map(|map| map.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
            .unwrap_or_default();
        voices.sort_by(|a, b| a.1["name"].as_str().cmp(&b.1["name"].as_str()));
        let mut op = None;
        section(
            ui,
            &format!("登録済みのボイス（{}）", voices.len()),
            |ui| {
                if voices.is_empty() {
                    ui.weak("まだありません。下のフォームから登録してください。");
                }
                for (id, voice) in &voices {
                    let allowed = voice["allowed_user_ids"]
                        .as_array()
                        .cloned()
                        .unwrap_or_default();
                    let personal = !allowed.is_empty();
                    egui::Frame::new()
                    .fill(ui.visuals().faint_bg_color)
                    .corner_radius(6)
                    .inner_margin(egui::Margin::same(10))
                    .show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        ui.horizontal(|ui| {
                            ui.label(
                                RichText::new(voice["name"].as_str().unwrap_or(id))
                                    .strong()
                                    .size(16.0),
                            );
                            if *id == default {
                                ui.colored_label(ORANGE, "⭐ 既定");
                            }
                            if personal {
                                ui.colored_label(Color32::from_rgb(120, 140, 230), "🔒 専用");
                            } else {
                                ui.weak("🌐 全員が使える");
                            }
                        });
                        ui.weak(format!("ID: {id}"));
                        if personal {
                            ui.label(format!("使える人: {}", ids_text(&json!(allowed)).replace('\n', ", ")));
                        }
                        ui.small(
                            RichText::new(format!(
                                "参照テキスト: {}",
                                shorten(voice["reference_text"].as_str().unwrap_or(""), 60)
                            ))
                            .weak(),
                        );
                        ui.horizontal(|ui| {
                            if ui.button("▶ 試聴").clicked() {
                                op = Some(VoiceOp::Preview(id.clone()));
                            }
                            let can_default = *id != default && !personal;
                            if ui
                                .add_enabled(can_default, egui::Button::new("⭐ 既定にする"))
                                .on_disabled_hover_text("専用ボイスは既定にできません")
                                .clicked()
                            {
                                op = Some(VoiceOp::MakeDefault(id.clone()));
                            }
                            if ui.button("✏ 編集").clicked() {
                                op = Some(VoiceOp::Edit(VoiceEdit {
                                    id: id.clone(),
                                    name: voice["name"].as_str().unwrap_or("").into(),
                                    text: voice["reference_text"].as_str().unwrap_or("").into(),
                                    allowed: ids_text(&json!(allowed)),
                                }));
                            }
                            if ui.button("🗑 削除").clicked() {
                                op = Some(VoiceOp::AskDelete(id.clone()));
                            }
                        });
                        if self.confirm_delete.as_deref() == Some(id.as_str()) {
                            ui.colored_label(
                                RED,
                                "本当に削除しますか？ このボイスを選んでいた人は既定ボイスに戻ります。",
                            );
                            ui.horizontal(|ui| {
                                let delete = egui::Button::new(
                                    RichText::new("削除する").color(Color32::WHITE),
                                )
                                .fill(RED);
                                if ui.add(delete).clicked() {
                                    op = Some(VoiceOp::Delete(id.clone()));
                                }
                                if ui.button("やめる").clicked() {
                                    op = Some(VoiceOp::Cancel);
                                }
                            });
                        }
                        if let Some(edit) = self.edit_voice.as_mut().filter(|e| e.id == *id) {
                            ui.separator();
                            field(ui, "表示名", "", |ui| {
                                ui.add(
                                    egui::TextEdit::singleline(&mut edit.name)
                                        .desired_width(f32::INFINITY),
                                );
                            });
                            field(
                                ui,
                                "使えるユーザーID",
                                "空欄なら全員が使えます。指定するとその人だけのボイスになります。",
                                |ui| {
                                    ui.add(
                                        egui::TextEdit::multiline(&mut edit.allowed)
                                            .desired_rows(2)
                                            .desired_width(f32::INFINITY)
                                            .hint_text("例: 123456789012345678"),
                                    );
                                },
                            );
                            field(
                                ui,
                                "参照テキスト",
                                "変更すると声のプロファイルを作り直します。",
                                |ui| {
                                    ui.add(
                                        egui::TextEdit::multiline(&mut edit.text)
                                            .desired_rows(2)
                                            .desired_width(f32::INFINITY),
                                    );
                                },
                            );
                            ui.horizontal(|ui| {
                                if ui.button("💾 保存").clicked() {
                                    op = Some(VoiceOp::SaveEdit);
                                }
                                if ui.button("キャンセル").clicked() {
                                    op = Some(VoiceOp::Cancel);
                                }
                            });
                        }
                    });
                    ui.add_space(6.0);
                }
            },
        );
        match op {
            Some(VoiceOp::Preview(id)) => {
                self.send(Action::Test {
                    text: self.preview.clone(),
                    voice_id: Some(id),
                });
                self.info("試聴を依頼しました…");
            }
            Some(VoiceOp::MakeDefault(id)) => self.call(
                "system_saved",
                "PATCH",
                "/settings/system",
                Some(json!({"defaults": {"voice_id": id}})),
                Some(self.system_rev),
            ),
            Some(VoiceOp::Edit(edit)) => {
                self.confirm_delete = None;
                self.edit_voice = Some(edit);
            }
            Some(VoiceOp::AskDelete(id)) => {
                self.edit_voice = None;
                self.confirm_delete = Some(id);
            }
            Some(VoiceOp::Delete(id)) => self.call(
                "voice_deleted",
                "DELETE",
                &format!("/voices/{id}"),
                None,
                Some(self.voices_rev),
            ),
            Some(VoiceOp::SaveEdit) => {
                if let Some(edit) = &self.edit_voice {
                    if edit.name.trim().is_empty() || edit.text.trim().is_empty() {
                        self.error("表示名と参照テキストは空にできません。");
                    } else {
                        self.call(
                            "voice_updated",
                            "PATCH",
                            &format!("/voices/{}", edit.id),
                            Some(json!({
                                "name": edit.name.trim(),
                                "reference_text": edit.text.trim(),
                                "allowed_user_ids": parse_ids(&edit.allowed),
                            })),
                            Some(self.voices_rev),
                        );
                    }
                }
            }
            Some(VoiceOp::Cancel) => {
                self.edit_voice = None;
                self.confirm_delete = None;
            }
            None => {}
        }

        let mut register = false;
        let form = &mut self.new_voice;
        let exists = self.voices.get(form.id.as_str()).is_some();
        section(ui, "➕ 新しいボイスを登録", |ui| {
            field(
                ui,
                "① 音声ファイル",
                "このウィンドウにファイルをドラッグ＆ドロップできます。wav / m4a / mp3 など（自動で変換）。雑音の少ない1〜30秒の声。",
                |ui| {
                    ui.add(
                        egui::TextEdit::singleline(&mut form.path)
                            .hint_text("D:\\voices\\taro.m4a")
                            .desired_width(f32::INFINITY),
                    );
                },
            );
            field(
                ui,
                "② ボイスID",
                "英数字・_・- のみ（例: taro）",
                |ui| {
                    ui.add(egui::TextEdit::singleline(&mut form.id).hint_text("taro"));
                },
            );
            field(
                ui,
                "③ 表示名",
                "Discordのモデル一覧に表示される名前",
                |ui| {
                    ui.add(
                        egui::TextEdit::singleline(&mut form.name)
                            .hint_text("たろうの声")
                            .desired_width(f32::INFINITY),
                    );
                },
            );
            field(
                ui,
                "④ 参照テキスト",
                "音声で話している内容を一字一句そのまま入力してください。",
                |ui| {
                    ui.add(
                        egui::TextEdit::multiline(&mut form.text)
                            .desired_rows(3)
                            .desired_width(f32::INFINITY),
                    );
                },
            );
            field(
                ui,
                "⑤ 使えるユーザーID（任意）",
                "空欄なら全員。本人のDiscordユーザーIDを入れると、その人専用のボイスになります。",
                |ui| {
                    ui.add(
                        egui::TextEdit::multiline(&mut form.allowed)
                            .desired_rows(2)
                            .desired_width(f32::INFINITY)
                            .hint_text("例: 123456789012345678"),
                    );
                },
            );
            let mut problems = Vec::new();
            if form.path.trim().is_empty() {
                problems.push("音声ファイルを指定してください");
            } else if !Path::new(form.path.trim().trim_matches('"')).is_file() {
                problems.push("音声ファイルが見つかりません");
            }
            if !valid_voice_id(form.id.trim()) {
                problems.push("ボイスIDは英数字で始まる英数字・_・- にしてください");
            } else if exists {
                problems.push("このボイスIDは登録済みです");
            }
            if form.text.trim().is_empty() {
                problems.push("参照テキストを入力してください");
            }
            for problem in &problems {
                ui.colored_label(ORANGE, format!("・{problem}"));
            }
            register = ui
                .add_enabled(problems.is_empty(), egui::Button::new("✅ 登録する"))
                .clicked();
        });
        if register {
            let form = &self.new_voice;
            let name = if form.name.trim().is_empty() {
                form.id.trim()
            } else {
                form.name.trim()
            };
            self.send(Action::ImportVoice {
                id: form.id.trim().into(),
                name: name.into(),
                path: form.path.trim().into(),
                text: form.text.trim().into(),
                allowed_user_ids: parse_ids(&form.allowed),
            });
            self.info("ボイスを登録しています…");
        }
    }

    pub fn page_advanced(&mut self, ui: &mut egui::Ui) {
        title(
            ui,
            "詳細（JSON）",
            "APIのJSONを直接編集します。通常は他のページを使ってください。",
        );
        let mut fetch = false;
        let mut save = false;
        section(ui, "エディター", |ui| {
            field(
                ui,
                "パス",
                "例: /settings/system, /guilds/<サーバーID>, /users/<ユーザーID>, /voices",
                |ui| {
                    ui.add(
                        egui::TextEdit::singleline(&mut self.raw_path).desired_width(f32::INFINITY),
                    );
                },
            );
            ui.horizontal(|ui| {
                fetch = ui.button("読み込み").clicked();
                save = ui.button("保存（PATCH）").clicked();
                ui.weak(format!("revision: {}", self.raw_rev));
            });
            ui.add(
                egui::TextEdit::multiline(&mut self.raw_editor)
                    .code_editor()
                    .desired_rows(16)
                    .desired_width(f32::INFINITY),
            );
        });
        if fetch {
            self.call("raw", "GET", &self.raw_path.clone(), None, None);
        }
        if save {
            match serde_json::from_str::<Value>(&self.raw_editor) {
                Ok(value) => self.call(
                    "raw_saved",
                    "PATCH",
                    &self.raw_path.clone(),
                    Some(value),
                    Some(self.raw_rev),
                ),
                Err(error) => self.error(format!("JSONの形式が正しくありません: {error}")),
            }
        }
        ui.collapsing("最後の応答", |ui| {
            ui.monospace(&self.raw_result);
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_are_parsed_from_free_text() {
        assert_eq!(
            parse_ids("123, 456\n789 123 abc 12345678901234567890123"),
            vec!["123", "456", "789"]
        );
    }

    #[test]
    fn voice_id_rules_match_the_worker() {
        assert!(valid_voice_id("taro_01-a"));
        assert!(!valid_voice_id("_taro"));
        assert!(!valid_voice_id("た"));
        assert!(!valid_voice_id(""));
        assert!(!valid_voice_id(&"a".repeat(65)));
    }

    #[test]
    fn dropped_file_fills_id_and_name() {
        let mut form = NewVoice::default();
        use_dropped_file(&mut form, Path::new("C:/voices/taro voice.m4a"));
        assert_eq!(form.id, "tarovoice");
        assert_eq!(form.name, "taro voice");
        assert!(form.path.ends_with("taro voice.m4a"));
    }
}
