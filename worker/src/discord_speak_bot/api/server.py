import base64
import binascii
import hashlib
import secrets
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from ..settings.models import Overrides, SpeechSettings, dump, resolve
from ..settings.store import RevisionConflict, StoreError, merge
from ..tts.voices import register_voice, remove_voice

PREFIX = "/internal/v1"


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def revision(request):
    value = request.headers.get("if-match")
    if value is None:
        raise APIError(428, "REVISION_REQUIRED", "If-Matchが必要です")
    if not value.startswith('"') or not value.endswith('"') or not value[1:-1].isdigit():
        raise APIError(422, "INVALID_REVISION", "If-Matchは引用符付きrevisionを指定してください")
    return int(value[1:-1])


def valid_id(value):
    if not value.isascii() or not value.isdigit() or len(value) > 20:
        raise APIError(422, "INVALID_ID", "Discord IDの形式が不正です")


async def body(request):
    try:
        value = await request.json()
    except ValueError:
        raise APIError(422, "INVALID_JSON", "JSONを指定してください") from None
    if not isinstance(value, dict):
        raise APIError(422, "INVALID_JSON", "JSON objectが必要です")
    return value


def create_app(runtime, token: str):
    if len(token) < 32:
        raise ValueError("IPC tokenは32文字以上必要です")
    store = runtime.store

    @asynccontextmanager
    async def lifespan(app):
        await runtime.start()
        yield
        await runtime.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def error_response(status, code, message):
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": uuid.uuid4().hex,
                    "retryable": status in (429, 503),
                }
            },
        )

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in ("127.0.0.1", "localhost") or "origin" in request.headers:
            return error_response(403, "FORBIDDEN_ORIGIN", "ローカルHost専用APIです")
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            return error_response(401, "UNAUTHORIZED", "認証が必要です")
        size = 0
        chunks = []
        limit = (
            28 * 1024**2
            if request.method == "POST" and request.url.path == PREFIX + "/voices"
            else 65536
        )
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return error_response(413, "BODY_TOO_LARGE", "本文のサイズ上限を超えています")
            chunks.append(chunk)
        request._body = b"".join(chunks)
        return await call_next(request)

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RevisionConflict)
    async def conflict(request, exc):
        return error_response(412, "REVISION_CONFLICT", str(exc))

    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        return error_response(
            422, "VALIDATION_ERROR", "設定の型、項目名、値の範囲を確認してください"
        )

    @app.exception_handler(OSError)
    @app.exception_handler(StoreError)
    async def storage_error(request, exc):
        return error_response(
            503, "STORAGE_ERROR", "設定を保存できません。ディスクと権限を確認してください"
        )

    @app.get(PREFIX + "/health")
    async def health():
        return {"alive": True, "process_instance_id": runtime.instance_id}

    @app.get(PREFIX + "/status")
    async def status():
        return runtime.status()

    @app.get(PREFIX + "/settings/system")
    async def get_system():
        return dump(store.get("system"))

    @app.patch(PREFIX + "/settings/system")
    async def patch_system(request: Request):
        patch = await body(request)
        if set(patch) - {"startup", "performance", "tts", "queue", "api", "defaults"}:
            raise APIError(422, "INVALID_FIELD", "変更できない項目です")
        voice = (
            patch.get("defaults", {}).get("voice_id")
            if isinstance(patch.get("defaults", {}), dict)
            else None
        )
        if voice and voice not in store.get("voices").voices:
            raise APIError(422, "VOICE_NOT_FOUND", "Voiceが登録されていません")
        model = store.update("system", revision(request), lambda target: merge(target, patch))
        return {"settings": dump(model), "restart_required": bool(set(patch) - {"defaults"})}

    @app.get(PREFIX + "/guilds")
    async def get_guilds():
        return {**dump(store.get("guilds")), "runtime": runtime.scheduler.status()}

    @app.get(PREFIX + "/guilds/{guild_id}")
    async def get_guild(guild_id: str):
        valid_id(guild_id)
        model = store.get("guilds")
        return {
            "revision": model.revision,
            "settings": dump(model.guilds[guild_id]) if guild_id in model.guilds else None,
        }

    @app.patch(PREFIX + "/guilds/{guild_id}")
    async def patch_guild(guild_id: str, request: Request):
        valid_id(guild_id)
        patch = await body(request)
        previous = store.get("guilds").guilds.get(guild_id)
        result = store.update(
            "guilds",
            revision(request),
            lambda target: merge(target["guilds"].setdefault(guild_id, {}), patch),
        )
        current = result.guilds[guild_id]
        changed_channel = previous and (
            previous.text_channel_id != current.text_channel_id
            or previous.voice_channel_id != current.voice_channel_id
        )
        if not current.enabled or changed_channel:
            runtime.aggregator.clear(guild_id)
            runtime.scheduler.disconnect(guild_id)
            if runtime.discord:
                await runtime.discord.leave(guild_id)
        return {"revision": result.revision, "settings": dump(current)}

    def user_value(user_id, guild_id=None):
        valid_id(user_id)
        if guild_id:
            valid_id(guild_id)
        users = store.get("users")
        user = users.users.get(user_id)
        effective, sources = resolve(store.get("system"), users, user_id, guild_id or "0")
        override = (
            (user.guilds.get(guild_id) if guild_id else user.global_settings) if user else None
        )
        return {
            "revision": users.revision,
            "overrides": dump(override) if override else {},
            "effective": dump(effective),
            "sources": sources,
        }

    async def patch_user(user_id, request, guild_id=None):
        valid_id(user_id)
        if guild_id:
            valid_id(guild_id)
        patch = dump(Overrides.model_validate(await body(request)))
        if patch.get("voice_id") and patch["voice_id"] not in store.get("voices").voices:
            raise APIError(422, "VOICE_NOT_FOUND", "Voiceが登録されていません")

        def mutate(target):
            user = target["users"].setdefault(user_id, {"global_settings": {}, "guilds": {}})
            layer = user["guilds"].setdefault(guild_id, {}) if guild_id else user["global_settings"]
            merge(layer, patch)

        store.update("users", revision(request), mutate)
        return user_value(user_id, guild_id)

    @app.get(PREFIX + "/users/{user_id}")
    async def get_user(user_id: str):
        return user_value(user_id)

    @app.patch(PREFIX + "/users/{user_id}")
    async def update_user(user_id: str, request: Request):
        return await patch_user(user_id, request)

    @app.get(PREFIX + "/guilds/{guild_id}/users/{user_id}")
    async def get_guild_user(guild_id: str, user_id: str):
        return user_value(user_id, guild_id)

    @app.patch(PREFIX + "/guilds/{guild_id}/users/{user_id}")
    async def update_guild_user(guild_id: str, user_id: str, request: Request):
        return await patch_user(user_id, request, guild_id)

    @app.delete(PREFIX + "/guilds/{guild_id}/users/{user_id}/overrides/{field}")
    async def reset(guild_id: str, user_id: str, field: str, request: Request):
        valid_id(guild_id)
        valid_id(user_id)
        if field not in Overrides.model_fields:
            raise APIError(422, "INVALID_FIELD", "設定項目が不正です")

        def mutate(target):
            layer = target["users"].get(user_id, {}).get("guilds", {}).get(guild_id, {})
            layer.pop(field, None)

        store.update("users", revision(request), mutate)
        return user_value(user_id, guild_id)

    @app.get(PREFIX + "/voices")
    async def voices():
        return dump(store.get("voices"))

    @app.post(PREFIX + "/voices", status_code=201)
    async def add_voice(request: Request):
        data = await body(request)

        async def action():
            if set(data) != {"voice_id", "name", "reference_text", "wav_base64"} or not all(
                isinstance(value, str) for value in data.values()
            ):
                raise APIError(422, "INVALID_VOICE", "Voiceの項目を確認してください")
            try:
                wav = base64.b64decode(data["wav_base64"], validate=True)
                result = register_voice(
                    store, data["voice_id"], data["name"], data["reference_text"], wav
                )
            except (ValueError, binascii.Error):
                raise APIError(
                    422,
                    "INVALID_VOICE",
                    "Voice ID・名前・参照テキスト・1〜30秒のPCM WAVを確認してください",
                ) from None
            return {
                "revision": result.revision,
                "voice_id": data["voice_id"],
                "profile_state": "lazy",
            }

        return await idempotent(request, action)

    @app.delete(PREFIX + "/voices/{voice_id}")
    async def delete_voice(voice_id: str, request: Request):
        try:
            result = remove_voice(store, runtime, voice_id, revision(request))
        except ValueError:
            raise APIError(409, "VOICE_IN_USE", "設定またはジョブがVoiceを参照しています") from None
        except KeyError:
            raise APIError(404, "NOT_FOUND", "Voiceが存在しません") from None
        return {"revision": result.revision}

    # POST idempotency is serialized by the event loop. Actions hold a per-key future.
    import asyncio

    operations = {}

    async def idempotent(request, action):
        key = request.headers.get("idempotency-key")
        if not key or len(key) > 128:
            raise APIError(422, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Keyが必要です")
        fingerprint = hashlib.sha256(request.url.path.encode() + await request.body()).hexdigest()
        now = time.monotonic()
        for old, entry in list(operations.items()):
            if now - entry[0] > 600 and entry[2].done():
                del operations[old]
        if key in operations:
            _, saved, future = operations[key]
            if saved != fingerprint:
                raise APIError(409, "IDEMPOTENCY_CONFLICT", "キーが別操作に使用されています")
            return await asyncio.shield(future)
        if len(operations) >= 1024:
            raise APIError(429, "TOO_MANY_OPERATIONS", "操作数の上限です")
        future = asyncio.get_running_loop().create_future()
        operations[key] = (now, fingerprint, future)
        try:
            result = await action()
            future.set_result(result)
            return result
        except BaseException:
            operations.pop(key, None)
            future.cancel()
            raise

    @app.post(PREFIX + "/tts/test", status_code=202)
    async def test_tts(request: Request):
        data = await body(request)

        async def action():
            if (
                set(data) - {"text", "settings"}
                or not isinstance(data.get("text"), str)
                or not 1 <= len(data["text"].strip()) <= runtime.config.queue.max_text_chars
            ):
                raise APIError(422, "INVALID_TEXT", "試聴テキストの長さを確認してください")
            settings = SpeechSettings.model_validate(
                data.get("settings", dump(store.get("system").defaults))
            )
            try:
                operation_id = runtime.submit_preview(data["text"], settings)
            except RuntimeError as exc:
                raise APIError(
                    429 if str(exc) == "PREVIEW_BUSY" else 503, str(exc), "現在試聴を実行できません"
                ) from None
            except ValueError:
                raise APIError(422, "VOICE_NOT_FOUND", "Voiceが登録されていません") from None
            return {"operation_id": operation_id}

        return await idempotent(request, action)

    @app.get(PREFIX + "/operations/{operation_id}")
    async def operation(operation_id: str):
        runtime._prune_operations()
        value = runtime.operations.get(operation_id)
        if value is None:
            raise APIError(404, "NOT_FOUND", "操作が存在しないか期限切れです")
        return {"operation_id": operation_id, "state": value["state"], "error": value["error"]}

    @app.get(PREFIX + "/operations/{operation_id}/audio")
    async def operation_audio(operation_id: str):
        await operation(operation_id)
        audio = runtime.preview_wav(operation_id)
        if audio is None:
            raise APIError(409, "NOT_READY", "音声が準備できていません")
        return Response(audio, media_type="audio/wav")

    @app.post(PREFIX + "/guilds/{guild_id}/actions/{name}")
    async def guild_action(guild_id: str, name: str, request: Request):
        valid_id(guild_id)

        async def action():
            if name not in ("join", "leave", "clear", "skip"):
                raise APIError(404, "NOT_FOUND", "操作が存在しません")
            if runtime.discord is None:
                raise APIError(503, "DISCORD_NOT_READY", "Discordに接続していません")
            try:
                await runtime.discord.action(guild_id, name)
            except (ValueError, RuntimeError):
                raise APIError(
                    409, "ACTION_FAILED", "チャンネル設定と接続状態を確認してください"
                ) from None
            return {"accepted": True}

        return await idempotent(request, action)

    @app.post(PREFIX + "/worker/shutdown")
    async def shutdown(request: Request):
        async def action():
            runtime.stopping.set()
            return {"accepted": True}

        return await idempotent(request, action)

    return app
