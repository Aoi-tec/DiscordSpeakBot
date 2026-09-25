import argparse
import asyncio
import getpass
import json
import logging
import os
import secrets
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .settings.store import ConfigStore, DataLock
from .tts.voices import register_voice


def default_data_dir():
    return (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "DiscordSpeakBot"
    )


def import_voice(args, store):
    source = args.wav.resolve()
    if source.stat().st_size > 20 * 1024**2:
        raise ValueError("参照WAVの上限は20MiBです")
    register_voice(store, args.voice_id, args.name or args.voice_id, args.text, source.read_bytes())
    if args.default:
        system = store.get("system")
        store.update(
            "system",
            system.revision,
            lambda target: target["defaults"].update(voice_id=args.voice_id),
        )
    print("Voiceを登録しました。初回生成時にプロファイルを作成します。")


async def run(args, store, credentials):
    import uvicorn

    from .api.server import create_app
    from .runtime import Runtime
    from .tts.engine import FakeEngine
    from .tts.qwen import QwenEngine

    config = store.get("system")
    engine = (
        FakeEngine() if args.fake else QwenEngine(config.tts, store, config.queue.max_audio_seconds)
    )
    runtime = Runtime(store, engine, fake=args.fake)
    app = create_app(runtime, credentials["ipc_token"])
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=config.api.port, access_log=False, log_config=None
        )
    )

    async def discord_main():
        from .discord_adapter.bot import SpeakBot

        runtime.discord = SpeakBot(runtime)
        try:
            await runtime.discord.start(credentials["discord_token"])
        except Exception as exc:
            runtime.last_error = f"DISCORD_{type(exc).__name__}"
            logging.getLogger("worker").error("discord_stopped type=%s", type(exc).__name__)

    task = (
        asyncio.create_task(discord_main())
        if credentials.get("discord_token") and not args.fake
        else None
    )

    async def watch_stop():
        await runtime.stopping.wait()
        server.should_exit = True

    watcher = asyncio.create_task(watch_stop())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        if task:
            task.cancel()
        await asyncio.gather(watcher, *([task] if task else []), return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="DiscordSpeakBot Worker")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初期設定ファイルの作成")
    serve = sub.add_parser("serve")
    serve.add_argument(
        "--fake", action="store_true", help="無音の開発エンジン。Discordへ接続しません"
    )
    serve.add_argument(
        "--credentials-stdin", action="store_true", help="Host専用: stdinから秘密情報を受け取る"
    )
    voice = sub.add_parser("import-voice", help="Worker停止中に参照Voiceを登録")
    voice.add_argument("voice_id")
    voice.add_argument("wav", type=Path)
    voice.add_argument("--text", required=True)
    voice.add_argument("--name")
    voice.add_argument("--default", action="store_true")
    args = parser.parse_args()
    lock = DataLock(args.data_dir)
    try:
        store = ConfigStore(args.data_dir)
        if args.command == "init":
            print(f"設定を初期化しました: {store.root}")
            return
        if args.command == "import-voice":
            import_voice(args, store)
            return
        logs = store.root / "logs"
        logs.mkdir(exist_ok=True)
        handler = RotatingFileHandler(
            logs / "worker.log", maxBytes=10 * 1024**2, backupCount=13, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
        if args.credentials_stdin:
            credentials = json.loads(sys.stdin.readline(65536))
            if not isinstance(credentials, dict) or not isinstance(
                credentials.get("ipc_token"), str
            ):
                raise ValueError("Host credentialsが不正です")
        else:
            token = getpass.getpass("IPC Token（空欄で一時生成）: ") or secrets.token_urlsafe(32)
            # The generated key is intentionally not printed or persisted.
            credentials = {
                "ipc_token": token,
                "discord_token": "" if args.fake else getpass.getpass("Discord Bot Token: "),
            }
        asyncio.run(run(args, store, credentials))
    finally:
        lock.close()


if __name__ == "__main__":
    main()
