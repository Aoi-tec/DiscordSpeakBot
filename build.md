# Discord Local TTS Host 基本設計書

## 1. 目的

Windows PC上で完全バックグラウンド動作するDiscord向けローカルTTSシステムを構築する。

主目的は以下。

- Discordのテキストメッセージを低遅延で音声化
- Qwen3-TTSを利用したZero-shot Voice Clone
- ユーザーごとのVoice・再生速度等の設定
- Guild単位でユーザー設定を上書き可能
- 最大3 Guild程度の同時運用
- RTX 3080へTTS推論を集約
- ゲーム・VRへのCPU負荷干渉を最小化
- Windowsログイン後はタスクトレイ常駐
- 通常運用ではユーザーがアプリ本体を意識しない

---

# 2. 想定実行環境

## Host

- OS: Windows 11
- CPU: Ryzen 9 9950X3D
- RAM: 64GB
- GPU 1: Radeon RX 7900 XTX
  - ゲーム
  - VR
  - 主描画
- GPU 2: GeForce RTX 3080 10GB
  - Qwen3-TTS CUDA推論
  - フェイストラッキング等既存CUDA処理
  - 一部ディスプレイ出力

TTS処理は原則RTX 3080へオフロードする。

CPU側はDiscord Bot、テキスト処理、Queue、Opus、CUDA Dispatch等のみを担当する。

---

# 3. 基本アーキテクチャ

```text
                    Discord
                       │
                  Gateway API
                       │
                       ▼
┌─────────────────────────────────────┐
│ Python Worker                       │
│                                     │
│ Discord Bot                         │
│ Message Aggregator                  │
│ Settings Resolver                   │
│ Global TTS Scheduler                │
│ Voice Profile Manager               │
│ Guild Playback Queues               │
└──────────────────┬──────────────────┘
                   │
                   ▼
            Qwen3-TTS Engine
                   │
                  CUDA
                   │
                   ▼
              RTX 3080
                   │
              Generated PCM
                   │
                   ▼
           Guild Playback Queue
                   │
                  Opus
                   │
                   ▼
             Discord Voice
```

管理部分は別プロセスとする。

```text
┌──────────────────────────┐
│ Rust Host / Supervisor   │
│                          │
│ System Tray              │
│ Settings GUI             │
│ Voice Manager            │
│ Worker Monitor           │
│ CPU Sets                 │
│ GPU/VRAM Monitor         │
│ Logs                     │
└────────────┬─────────────┘
             │
         localhost IPC
             │
             ▼
        Python Worker
```

RustとPythonは同一プロセスへ統合しない。

Python/TTS/CUDA側で障害が発生してもRust GUIを生存させ、Worker単体を再起動可能とする。

---

# 4. 技術構成

| 領域 | 採用技術 |
|---|---|
| GUI | Rust |
| System Tray | Rust |
| Supervisor | Rust |
| Windows CPU制御 | Rust + Win32 API |
| Discord Bot | Python |
| TTS Scheduler | Python |
| Playback Queue | Python |
| Voice Profile | Python |
| TTS | Qwen3-TTS |
| GPU Backend | CUDA |
| GPU | RTX 3080 10GB |
| 設定保存 | JSON |
| IPC | localhost HTTP または Named Pipe |
| Discord Audio | Opus |
| Remote Access | Cloudflare Tunnel（任意） |

---

# 5. プロセス構成

## TTSHost.exe

Rust製。

Windowsログイン時に起動し、System Trayへ常駐する。

責務:

- Python Worker起動
- Worker死活監視
- Worker再起動
- CPU Sets設定
- Process Priority設定
- GPU/VRAM監視
- Queue状態表示
- Settings GUI
- Voice Manager GUI
- Guild Status
- Log Viewer
- Windows自動起動設定

原則としてTTSロジックは持たない。

---

## TTSWorker

Python製。

責務:

- Discord Gateway接続
- Discord Voice接続
- Message受信
- Message Aggregation
- User/Guild設定解決
- Voice Profile読み込み
- Qwen3-TTS制御
- TTS生成Queue
- Guild別Playback Queue
- Opus変換
- Slash Command処理
- localhost API

---

# 6. 設定階層

ユーザー設定は以下の優先順位とする。

```text
GuildUserSettings
        ↓
GlobalUserSettings
        ↓
System Default
```

GuildUserSettingsは完全な設定ではなく、GlobalUserSettingsへのOverrideとして扱う。

例:

```json
{
  "global": {
    "user": "123456",
    "voice": "type-a",
    "speed": 30,
    "volume": 80
  }
}
```

Guild側:

```json
{
  "guild": "999999",
  "user": "123456",
  "voice": "type-b",
  "speed": 100
}
```

実効設定:

```json
{
  "voice": "type-b",
  "speed": 100,
  "volume": 80
}
```

`volume`はGuild側にないためGlobalから継承する。

---

# 7. JSON構成

```text
data/
├─ system.json
├─ users.json
├─ guilds.json
├─ voices.json
├─ runtime/
│  └─ state.json
├─ logs/
│  └─ yyyy-mm-dd.log
└─ voices/
   ├─ type-a/
   │  ├─ reference.wav
   │  ├─ reference.txt
   │  └─ profile.cache
   ├─ type-b/
   │  ├─ reference.wav
   │  ├─ reference.txt
   │  └─ profile.cache
   └─ ...
```

JSONは起動時にRAMへ読み込み、通常の読み上げ処理ではファイルアクセスしない。

設定変更時:

```text
RAM Update
   ↓
.tmpへ書き込み
   ↓
flush
   ↓
atomic rename
   ↓
本JSON置換
```

破損対策として`.bak`を1世代保持する。

---

# 8. system.json

例:

```json
{
  "startup": {
    "start_with_windows": true,
    "start_minimized": true
  },

  "performance": {
    "cpu_mode": "low_impact",
    "physical_cores": 1,
    "use_smt": true,
    "process_priority": "below_normal"
  },

  "tts": {
    "model": "qwen3-tts-1.7b",
    "cuda_device": 0,
    "context_size": 4096,
    "max_parallel": 3,
    "preload_model": true,
    "preload_voices": true
  },

  "queue": {
    "max_text_queue": 10,
    "max_generated_queue": 3,
    "merge_window_ms": 200
  },

  "api": {
    "host": "127.0.0.1",
    "port": 8765
  }
}
```

---

# 9. users.json

```json
{
  "users": {
    "123456": {
      "voice": "type-a",
      "speed": 30,
      "volume": 80,

      "guilds": {
        "999999": {
          "voice": "type-b",
          "speed": 100
        }
      }
    }
  }
}
```

---

# 10. guilds.json

Guild固有設定を保持する。

```json
{
  "guilds": {
    "999999": {
      "enabled": true,
      "text_channel_id": "111111",
      "voice_channel_id": "222222",

      "max_queue": 10,
      "merge_window_ms": 200,

      "skip_urls": true,
      "skip_codeblocks": true,
      "read_bot_messages": false
    }
  }
}
```

---

# 11. voices.json

```json
{
  "voices": {
    "type-a": {
      "name": "Type A",
      "reference_audio": "voices/type-a/reference.wav",
      "reference_text": "こんにちは。",
      "profile_cache": "voices/type-a/profile.cache"
    },

    "type-b": {
      "name": "Type B",
      "reference_audio": "voices/type-b/reference.wav",
      "reference_text": "これは音声サンプルです。",
      "profile_cache": "voices/type-b/profile.cache"
    }
  }
}
```

---

# 12. Voice Management

基本方式はQwen3-TTS Zero-shot Voice Clone。

新規Voice作成フロー:

```text
Voice Manager
    ↓
Voice Name入力
    ↓
Reference WAV指定
    ↓
Reference Text入力
    ↓
Qwen Voice Clone Prompt生成
    ↓
profile.cache保存
    ↓
テスト生成
    ↓
voices.json登録
```

起動時:

```text
voices.json
    ↓
各profile.cacheロード
    ↓
RAMへキャッシュ
```

読み上げごとにReference WAVを再解析しない。

将来はFine-tuned Voiceにも対応可能とする。

---

# 13. Discordメッセージ処理

```text
Discord Message
      ↓
Guild判定
      ↓
対象Text Channel確認
      ↓
Bot/URL/Code等Filter
      ↓
Message Aggregator
      ↓
User Settings Resolve
      ↓
TTS Request生成
      ↓
Global TTS Scheduler
```

---

# 14. Message Aggregator

Discordでは連投をそのまま別音声にすると不自然になるため短時間だけ結合する。

例:

```text
0ms   「あ」
70ms  「そういえば」
130ms 「今日東京行く」
```

`merge_window_ms = 200`

なら、

```text
「あ、そういえば今日東京行く」
```

として一度だけ生成する。

対象は原則:

```text
同一Guild
+
同一User
+
短時間
```

とする。

---

# 15. TTS Scheduler

Qwen3-TTSモデルは1個のみGPUへ常駐させる。

```text
Guild A ─┐
Guild B ─┼── Global Scheduler ── Qwen3-TTS
Guild C ─┘
```

最大同時生成:

```text
max_parallel = 3
```

通常:

```text
1 Request
→ 即時生成
```

ほぼ同時のRequest:

```text
A
B
C
↓
batch / parallel generation
```

4件目以降は待機。

---

# 16. Queue設計

Guildごとに独立Queueを持つ。

```text
Guild A
├─ Text Queue
├─ Generated Queue
└─ Playback

Guild B
├─ Text Queue
├─ Generated Queue
└─ Playback

Guild C
├─ Text Queue
├─ Generated Queue
└─ Playback
```

推奨:

```text
Text Queue:
最大10件

Generated Queue:
最大2～3件

Now Playing:
1件
```

GPUは現在の再生より2～3件先まで生成可能。

それ以上はテキスト状態で待機させ、無駄な音声生成を避ける。

---

# 17. 再生

```text
Qwen3-TTS
   ↓
PCM
   ↓
Guild Audio Queue
   ↓
Opus
   ↓
Discord Voice
```

Guild間のPlayback Queueは完全独立。

Guild Aで大量にQueueが詰まってもGuild B/Cには影響しない。

---

# 18. Discord Command

基本設定はDiscordから変更可能にする。

例:

```text
/voice
/speed
/volume
/tts join
/tts leave
/tts skip
/tts clear
/tts status
```

Global/User設定とGuild Overrideを区別できるようにする。

概念例:

```text
/voice set type-a
```

→ GlobalUserSettings

```text
/voice set type-b guild:true
```

→ GuildUserSettings

---

# 19. localhost API

Python Workerは以下で待機する。

```text
127.0.0.1:8765
```

主要Endpoint案:

```text
GET  /health
GET  /status

GET  /voices
POST /voices/reload

GET  /guilds
GET  /guilds/{guild_id}

GET  /users/{user_id}
PUT  /users/{user_id}

GET  /guilds/{guild_id}/users/{user_id}
PUT  /guilds/{guild_id}/users/{user_id}

POST /tts/test

POST /worker/reload
```

Rust GUIは原則このAPI経由でPython Workerを操作する。

---

# 20. Cloudflare Tunnel

通常のDiscord読み上げ経路では使用しない。

```text
Discord
↕
Bot
↓
localhost
↓
Qwen
```

で完結する。

Cloudflare Tunnelの用途は以下に限定する。

```text
外出先から管理画面へ接続
スマートフォンから設定
将来的に別PCから管理
Remote API
```

原則として内部管理APIを丸ごと公開しない。

```text
/internal/*
```

はlocalhost専用。

外部利用が必要な機能のみ、

```text
/remote/*
```

として分離する。

---

# 21. CPUリソース管理

Rust SupervisorがWindows CPU Sets APIを使用する。

モード:

```text
Automatic
Low Impact
Custom
```

Low Impact:

```text
1 Physical Core
+
SMT 2 Logical Threads
```

をTTS/Bot側へ割り当てる。

Process Priority:

```text
Below Normal
```

を基本とする。

GPU推論主体なのでCPU負荷は最小限とする。

---

# 22. GPUリソース

Qwen3-TTSはRTX 3080へ常駐。

RX 7900 XTXは原則使用しない。

```text
RX7900XTX
└─ Game / VR

RTX3080
├─ Qwen3-TTS
├─ Face Tracking
└─ Display
```

TTS側はVRAMの過剰使用を避ける。

ContextはDiscord用途では、

```text
2048～4096
```

を基本範囲とする。

---

# 23. System Tray

通常はGUIウィンドウを表示しない。

右クリックメニュー例:

```text
Discord TTS
────────────────
● Discord Connected
● Qwen Ready

Settings
Voice Manager
Guild Status
Logs

Reload Voices
Restart Worker
Restart TTS

Exit
```

---

# 24. Settings GUI

主要ページ:

```text
General
Discord
TTS
Performance
Voice
Guild
Logs
About
```

Performanceでは、

```text
CPU Mode
CPU Core Assignment
Process Priority
CUDA Device
VRAM
Parallel Jobs
Context Size
```

を調整可能にする。

---

# 25. Voice Manager

最低限必要な機能:

```text
Voice一覧
Voice追加
Voice削除
Reference WAV選択
Reference Text入力
Profile生成
Profile再生成
テスト文章入力
試聴
Default Voice設定
```

---

# 26. Worker監視

Rust Hostは定期的に、

```text
GET /health
```

を確認する。

一定時間応答なし:

```text
Worker timeout
      ↓
Python Process Kill
      ↓
Worker再起動
      ↓
Qwenロード
      ↓
Voice Cacheロード
      ↓
Discord再接続
```

GUI自体は終了させない。

---

# 27. エラー処理

想定する主要障害:

```text
CUDA OOM
GPU Device Lost
Qwen Engine Crash
Discord Disconnect
Voice Channel Disconnect
Broken Voice Cache
JSON Corruption
Python Worker Crash
```

対応:

```text
復旧可能
→ 自動再試行

Worker異常
→ Worker再起動

設定破損
→ .bak復旧

CUDA OOM
→ Queue停止
→ TTS再起動
→ GUI警告
```

---

# 28. ログ

カテゴリー:

```text
SYSTEM
DISCORD
TTS
VOICE
QUEUE
CUDA
ERROR
```

例:

```text
[21:03:11][DISCORD] Guild 999999 connected
[21:03:15][TTS] Request user=123456 voice=type-a
[21:03:15][TTS] TTFA=284ms
[21:03:15][QUEUE] Guild=999999 queue=2
```

TTFAは必ず記録する。

重要な性能指標:

```text
Message Received
TTS Start
First Audio Generated
Playback Start
Playback End
```

これにより、

```text
Discord受信遅延
TTS TTFA
Queue待ち
Discord再生遅延
```

を分離して計測可能にする。

---

# 29. セキュリティ

Discord Tokenは平文JSONへ直接保存しない。

Windows Credential ManagerまたはDPAPI利用を推奨。

VoiceファイルとUser Settingsは原則ローカルのみ。

localhost API:

```text
127.0.0.1
```

のみListen。

Cloudflare Tunnel利用時は認証を必須とする。

---

# 30. 想定ディレクトリ

```text
DiscordTTS/
│
├─ host/
│  ├─ src/
│  │  ├─ main.rs
│  │  ├─ tray.rs
│  │  ├─ gui.rs
│  │  ├─ worker.rs
│  │  ├─ cpu_sets.rs
│  │  └─ monitoring.rs
│  └─ Cargo.toml
│
├─ worker/
│  ├─ main.py
│  ├─ discord/
│  │  ├─ bot.py
│  │  ├─ commands.py
│  │  └─ voice.py
│  │
│  ├─ tts/
│  │  ├─ engine.py
│  │  ├─ qwen.py
│  │  ├─ scheduler.py
│  │  └─ profiles.py
│  │
│  ├─ queue/
│  │  ├─ text_queue.py
│  │  ├─ audio_queue.py
│  │  └─ aggregator.py
│  │
│  ├─ settings/
│  │  ├─ loader.py
│  │  ├─ resolver.py
│  │  └─ models.py
│  │
│  └─ api/
│     └─ server.py
│
├─ data/
│  ├─ system.json
│  ├─ users.json
│  ├─ guilds.json
│  ├─ voices.json
│  └─ voices/
│
└─ logs/
```

---

# 31. MVP

第一段階では機能を絞る。

```text
Rust Tray
Python Worker
Discord接続
1 Guild
Qwen3-TTS
1 Voice
GlobalUserSettings
GuildUserSettings
Playback Queue
JSON保存
```

これでまず、

```text
Discord投稿
↓
1秒以内を目標に発話開始
```

を成立させる。

---

# 32. Phase 2

```text
3 Guild対応
Voice Manager
複数Voice
Voice Profile Cache
CPU Low Impact Mode
Worker自動復旧
Slash Command設定
Queue最適化
```

---

# 33. Phase 3

```text
Dynamic Batch
Parallel TTS
Cloudflare Remote Management
Fine-tuned Voice
Web UI
統計表示
TTFA Graph
自動GPU負荷調整
```

---

# 34. 実装優先順位

1. Python単体でQwen3-TTSによる音声生成を成立させる
2. Zero-shot Voice Cloneをキャッシュして再利用
3. Discord Botと接続
4. Guild Playback Queue実装
5. GlobalUserSettings / GuildUserSettings実装
6. JSON永続化
7. localhost API実装
8. Rust Tray/Supervisor作成
9. Worker監視・再起動
10. CPU Sets Low Impact Mode
11. Voice Manager
12. 最大3並列化
13. Cloudflare Remote機能

---

# 35. 最終設計方針

システムの責務を以下のように固定する。

```text
Rust
=
Windowsとの接点
管理
監視
GUI
リソース制御

Python
=
Discord
TTSロジック
Queue
Voice
Settings

Qwen3-TTS
=
音声生成

RTX3080
=
推論

JSON
=
永続設定

localhost
=
内部通信

Cloudflare
=
必要時だけ外部入口
```

Discord Bot、TTS、Voice、Guild設定を疎結合にすることで、将来TTSモデルやGPU、Discord処理方式を変更しても他コンポーネントへの影響を最小化する。

本システムは「Discord専用Qwen Bot」ではなく、将来的にはローカルTTSホストとして拡張できる構造を維持する。