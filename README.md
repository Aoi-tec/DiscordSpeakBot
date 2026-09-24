# DiscordSpeakBot

Windows 11で動くDiscord向けローカルTTS。RustのトレイHostがPython Workerを監視し、Qwen3-TTS BaseによるVoice Clone音声をGuild別に再生します。

現在は初期MVPです。設定管理・キュー・認証API・Discordアダプター・Qwenアダプター・Rust Hostを実装しています。GPU/Discordの実接続と遅延目標は未検証です。

- [セットアップと起動](docs/setup.md)
- [実装状況・既知の制限](docs/implementation.md)
- [詳細設計](docs/design.md)
- [元の要件](build.md)

## 開発用の起動

Python 3.12が必要です。このフォルダの`dist/tts-host.exe`はビルド済みです。Hostをソースからビルドする場合はRust stableとVisual Studio C++ Build Toolsも用意します。

```powershell
./scripts/setup-worker.ps1 -Dev
./scripts/start-host.ps1 -Fake
```

`-Fake`は無音のテストエンジンです。Discord接続やモデルダウンロードは行いません。初期設定ではトレイへ最小化するため、トレイの`Settings / Status`から開いてください。

## テスト

```powershell
./.venv/Scripts/python.exe -m pytest worker -q
./.venv/Scripts/python.exe scripts/smoke-worker.py
cargo check --locked --manifest-path host/Cargo.toml
```

設定・音声・TokenはGit管理しません。実行データは`%LOCALAPPDATA%/DiscordSpeakBot`、TokenはWindows Credential Managerへ保存します。
