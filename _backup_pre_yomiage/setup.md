# セットアップ

## 1. WorkerとHost

Windows 11 x64とPython 3.12を用意します。このフォルダにはビルド済み`dist/tts-host.exe`も配置しています。リポジトリのルートで実行します。

```powershell
./scripts/setup-worker.ps1 -Dev
./scripts/start-host.ps1 -Fake
```

setup-workerはリポジトリ内`.venv`へ依存を入れ、`%LOCALAPPDATA%/DiscordSpeakBot`へ設定を初期作成します。既存設定を初期化し直すことはありません。稼働中のWorkerと同じdata-dirへ設定作成・CLIインポートを実行すると排他ロックで拒否されます。

Hostは右下の通知領域に常駐します。`Settings / Status`で画面、`Restart Worker`で再起動、`Exit`でHostとWorkerを終了します。画面右上の×は非表示にするだけです。初期設定の`start_minimized`はtrueです。

開発用エンジンでは試聴ボタンで短い無音WAVを生成します。状態の`development_mode=true`で確認できます。開発用エンジンで実際の音声生成性能を測定しないでください。

## 2. Qwen環境の準備

通常のセットアップにはPyTorch、Qwen本体、モデル重みを含めていません。まず[PyTorch公式インストール案内](https://pytorch.org/get-started/locally/)でWindowsとCUDAに適した組み合わせを確認し、`.venv/Scripts/python.exe -m pip`を使ってインストールしてください。GPU依存は未検証のため固定版を推測していません。

その後、CUDA Graph版Qwenアダプターの依存を導入します。従来の`qwen-tts`と互換版の`qwen-tts-hf`は同じPythonパッケージ名を使うため、以前の環境から更新する場合は先に`qwen-tts`をアンインストールします。

```powershell
./.venv/Scripts/python.exe -m pip uninstall -y qwen-tts
./.venv/Scripts/python.exe -m pip install -e './worker[tts]'
./.venv/Scripts/python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available())"
nvidia-smi --query-gpu=name,uuid,memory.total --format=csv,noheader
ffmpeg -version
```

[Qwen公式README](https://github.com/QwenLM/Qwen3-TTS/blob/main/README.md)に従って`Qwen/Qwen3-TTS-12Hz-1.7B-Base`（または0.6B-Base）をローカルへダウンロードします。推論には[faster-qwen3-tts](https://github.com/andimarafioti/faster-qwen3-tts)のCUDA Graph版を使用し、起動時にウォームアップします。FlashAttentionは必須ではありません。Workerは通常起動時のネットワークダウンロードを禁止しているため、モデルの依存資産も事前に揃えてください。モデルrevisionとPyTorch/CUDA/Qwenのバージョンを実機確認後に記録してください。

HostのSettings pathを`/settings/system`にして`Fetch`します。JSON内の次の値を変更して`Save JSON patch`、続いて`Restart Worker`を実行します。

```json
{
  "tts": {
    "model_path": "D:/models/Qwen3-TTS-12Hz-1.7B-Base",
    "gpu_uuid": "GPU-実際に確認したUUID",
    "dtype": "bfloat16",
    "ffmpeg_path": "ffmpeg"
  }
}
```

上のJSONは部分更新にも使用できます。CUDAの番号0は固定指定せず、UUIDで選択した1台だけをWorkerに見せます。モデルやdtypeの変更はWorker再起動が必要です。以降の起動では`start-host.ps1`の`-Fake`を外してください。

## 3. Voiceを登録

Hostの`Add Voice`に次を入力します。

- Voice ID: `type-a`等。半角英数字・ハイフン・アンダースコア。
- Reference WAV full path: 1〜30秒、16bit PCM、mono/stereoのWAV。
- Exact reference transcript: WAVで実際に話している文。

`Import Voice`で管理領域へコピーします。登録時は形式を検証し、Qwenプロファイルは初回生成時に作成してRAMへキャッシュします。続いて`/settings/system`を再Fetchし、`defaults.voice_id`を登録したIDに設定します。

```json
{"defaults":{"voice_id":"type-a","speed_percent":100,"volume_percent":80}}
```

`Generate preview`は非同期生成の完了後にHost PCで試聴します。Discordには送りません。`runtime/preview.wav`は直近の試聴ファイルとして次回試聴で上書きされます。

Host停止中はCLIでも登録できます。

```powershell
./.venv/Scripts/python.exe -m discord_speak_bot import-voice type-a D:/voices/reference.wav --text 'こんにちは。' --default
```

## 4. Discordの準備

Discord Developer PortalでBotを作成し、Message Content Intentを有効にします。BotとApplication CommandsのスコープでGuildへ招待します。対象Text ChannelのView Channel、対象Voice ChannelのConnect/Speak等を付与します。Administratorは不要です。

Tokenはチャットや設定JSONへ貼らず、Hostの`Discord Token`欄へ入力して`Save Token`します。Windows Credential Managerの`DiscordSpeakBot/DiscordToken`に保存されます。`Restart Worker`で読み込み直します。

初回はGuild管理権限を持つ人が参加先の通常VCに入り、読み上げ対象のテキストチャンネルから`/tts join`を実行できます。そのテキストチャンネルとVCをGuild設定として保存して参加します。すでにGuild設定がある場合、`/tts join`は登録済みVCを使い、設定を上書きしません。

Hostで手動設定する場合は、Settings pathに`/guilds/実際のGuild ID`を入力してFetchします。未登録の場合もrevisionが返ります。エディターを次の部分設定で置き換え、Saveします。

```json
{
  "enabled": true,
  "text_channel_id": "実際のテキストチャンネルID",
  "voice_channel_id": "実際のボイスチャンネルID",
  "auto_rejoin": true
}
```

IDは実際には数字のみの文字列です。HostのGuild ID欄で`join`、またはDiscordから`/tts join`を実行します。Botがreadyでモデル・Voiceが有効なら、対象Text Channelの新規投稿が読み上げられます。

コマンド: `/voice voice_id scope`、`/speed percent scope`、`/volume percent scope`、`/tts reset field`、`/tts join|leave|skip|clear|status`。scopeはglobalまたはguild。本人の設定だけを変更できます。join/leave/clearはManage Guildまたは設定済み管理ロール、skipはそれに加えて同じVC内のユーザーに許可します。

Voice接続はDAVE対応のdiscord.py 2.7.1を固定した開発用ロックファイルで確認していますが、実際の接続テストは別途必要です。[discord.pyリリース情報](https://github.com/Rapptz/discord.py/blob/master/docs/whats_new.rst)

## 5. 設定、ログ、CPU

全設定はdata-dir内のJSONが正本です。Hostが稼働中の変更はAPIを使ってください。更新競合（412）の場合は再Fetchします。保存成功後にだけRAMへ反映します。設定破損時は正常な`.bak`へ復旧し、両方不正な場合は起動を停止します。

`performance.cpu_mode`はautomatic / low_impact / custom。low_impactは列挙したCPUトポロジーから1物理コアのSMTペアを選択し、Workerだけへ適用します。customはWindows CPU Set IDの配列`cpu_set_ids`です。論理CPU番号とは別です。失敗時はAutomaticとして起動し、Statusのhost_cpuに警告を表示します。ゲームとの干渉・音声欠落の有無は実測してください。

ログは`logs/worker.log`とローテーションファイルです。本文、参照音声、Tokenは記録しません。Statusの`last_error`はエラー分類、`generation_age_seconds`は処理中ジョブの経過時間です。初回ロード中は最大180秒の猶予、生成ハングは既定120秒でHostが復旧します。10分間に5回失敗すると自動復旧を停止します。

Windows自動起動のGUI連携、設定専用フォーム、GPU使用量表示、Voice削除のGUIは後続実装です。現段階では`startup.start_with_windows`は予約設定で、自動登録は行いません。

## 6. 開発と検証

Hostをソースからビルドする場合はRust stableとVisual Studioの「C++によるデスクトップ開発」を導入し、`cargo build --release --locked --manifest-path host/Cargo.toml`を実行してください。

```powershell
./.venv/Scripts/python.exe -m pytest worker -q
./.venv/Scripts/python.exe -m ruff check --config worker/pyproject.toml worker scripts
./.venv/Scripts/python.exe scripts/smoke-worker.py
cargo check --locked --manifest-path host/Cargo.toml
```

HostからWorkerを起動する結合テスト:

```powershell
$env:TTS_TEST_PYTHON = (Resolve-Path ./.venv/Scripts/python.exe).Path
cargo test --locked --manifest-path host/Cargo.toml -- --include-ignored
```

このテストは一時data-dirと空きポートを使い、開発用エンジンで起動・認証・正常終了を確認します。Tokenや本番の設定は使いません。

