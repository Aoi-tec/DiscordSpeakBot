# 初期実装の状況

2026-09-24。対象はMVPを中心に、3 Guild対応に必要な共通基盤まで。

## 実装済み

- Pythonパッケージ、CLI、data-dir排他ロック、スキーマ検証、部分継承、revision競合、原子的JSON保存とbak復旧。
- 固定期限での同一話者集約、フィルター、長文分割、ラウンドロビン、キュー個数/PCM予算、TTL、skip/clear/leave後の古い結果破棄。
- localhost認証API、Host/Origin検証、本文サイズ上限、設定更新、操作の重複排除、非同期試聴、WAV取得、Voice登録と参照中削除の拒否。
- discord.py接続アダプター、GuildごとのPCM再生、Slash設定・操作、権限確認、再接続処理。
- Qwen Baseアダプター、CUDA UUID選択、ローカルモデル限定、プロンプトRAMキャッシュ、FFmpegによる速度/音量/48kHz PCM変換。
- RustトレイHost、設定JSON編集、Token用Credential Manager、継承stdinでの秘密受け渡し、Job Object、Worker監視・再起動上限、CPU Sets、Voice追加、ローカル試聴。
- 開発用無音エンジン、Pythonテスト、別プロセスのスモークテスト、Host/Worker結合テスト、セットアップスクリプト。

## 設計からの段階的な差分

| 項目 | 初期実装 | 後続 |
|---|---|---|
| Voiceプロファイル | 初回生成、RAMキャッシュ | モデル世代付き安全なディスクキャッシュ、事前生成・再生成 |
| Voice登録API | base64 JSON、最大20MiB WAV、同期201、形式検証後に登録 | アップロード専用ストリームと非同期プロファイル準備 |
| Voice削除 | API、参照保護、元ファイルは復旧用に保持 | GUIと明示的なファイル掃除 |
| 設定GUI | revision付きJSON編集 | 項目別フォーム・継承元の専用表示 |
| Windows自動起動 | 予約設定のみ | GUIから起動登録・解除 |
| システム設定反映 | defaults以外は再起動必要 | 個別の動的反映 |
| reload API | 未公開。HostのRestart Workerを使用 | 安全なモデル/Voice単体reload |
| 破損設定 | bakで復旧、両方不正なら起動拒否 | 管理APIだけ起動する修復モード |
| ログ | 最大10MiB×14ファイル、標準テキスト | JSON Lines、日数ベースの保持、統計UI |
| GPU監視 | 生成時間・準備状態を表示 | デバイス/プロセス別VRAM表示 |
| 推論並列度 | 1、3 Guildまで公平に共有 | 実測後に動的batch |
| 音声出力 | 全文生成完了後に再生 | ストリーミングの検証 |

`start_with_windows`は予約値であり、保存しても現時点ではレジストリを変更しません。GUIでVoice削除や再生成をできるようには見せていません。原資料の全Phaseが完成した状態ではありません。

## 検証範囲

Pythonの設定・API・キュー・Voice入力・Discordコマンド構築を自動テストしています。開発用エンジンで、別プロセス起動からHTTP認証・試聴WAV生成・正常終了までを検証します。RustはWindowsターゲットでコンパイルとHost/Worker結合テストを実施します。

Bot Token、参照音声、Qwenモデルの組み合わせを使う実際の音声生成・Discord送信、GPUピーク、ゲーム/VR併用の遅延、GUI操作の目視検証は未実施です。1秒以内の発話開始を達成したとは扱いません。

## 次の実機確認

1. PyTorch/CUDA/Qwenとダウンロード済みBaseモデルを用意する。
2. 参照WAVと書き起こしを登録し、Hostで試聴する。
3. モデルロード時間、初回プロファイル生成時間、2回目以降のTTFAとVRAMを測る。
4. 1 Guildで参加・投稿読み上げ・skip・clear・切断復旧を確認する。
5. 3 Guildとゲーム併用、low_impact/automaticの比較を行い、依存とモデルrevisionを固定する。

依存の公式仕様: [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)、[Discord Voice/DAVE](https://github.com/discord/discord-api-docs/blob/main/developers/topics/voice-connections.mdx)、[tray-icon](https://docs.rs/tray-icon/0.21.3/tray_icon/)。
