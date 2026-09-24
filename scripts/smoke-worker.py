"""Offline process integration test: no Discord login, GPU, model download or audio playback."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="tts-smoke-") as folder:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        Path(folder, "system.json").write_text(json.dumps({"api": {"port": port}}))
        key = secrets.token_urlsafe(32)
        environment = dict(os.environ, PYTHONPATH=str(root / "worker/src"))
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "discord_speak_bot",
                "--data-dir",
                folder,
                "serve",
                "--fake",
                "--credentials-stdin",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=environment,
        )
        try:
            process.stdin.write((json.dumps({"ipc_token": key}) + "\n").encode())
            process.stdin.close()
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}/internal/v1",
                headers={"Authorization": "Bearer " + key},
                trust_env=False,
                timeout=2,
            ) as client:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("Worker exited before readiness")
                    try:
                        if client.get("/status").json()["engine_ready"]:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Worker startup timed out")
                assert (
                    client.get("/health", headers={"Authorization": "invalid"}).status_code == 401
                )
                response = client.post(
                    "/tts/test",
                    json={"text": "テスト"},
                    headers={"Idempotency-Key": "smoke-preview"},
                )
                response.raise_for_status()
                operation = response.json()["operation_id"]
                for _ in range(100):
                    if client.get(f"/operations/{operation}").json()["state"] == "completed":
                        break
                    time.sleep(0.02)
                audio = client.get(f"/operations/{operation}/audio")
                assert audio.content[:4] == b"RIFF"
                client.post(
                    "/worker/shutdown", headers={"Idempotency-Key": "smoke-stop"}
                ).raise_for_status()
            assert process.wait(timeout=10) == 0
            print("PASS: process startup, authentication, preview WAV, graceful shutdown")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stderr.close()


if __name__ == "__main__":
    main()
