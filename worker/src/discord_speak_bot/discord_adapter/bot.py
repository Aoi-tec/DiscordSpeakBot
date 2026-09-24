import asyncio
import io
import logging
from typing import Literal

import discord
from discord import app_commands

from ..settings.models import Overrides, dump

log = logging.getLogger("worker")


class PCMSource(discord.AudioSource):
    def __init__(self, pcm):
        self.stream = io.BytesIO(pcm)

    def read(self):
        frame = self.stream.read(3840)
        return frame.ljust(3840, b"\0") if frame else b""

    def cleanup(self):
        self.stream.close()


class SpeakBot(discord.Client):
    def __init__(self, runtime):
        intents = discord.Intents.none()
        intents.guilds = intents.guild_messages = intents.message_content = intents.voice_states = (
            True
        )
        super().__init__(intents=intents)
        self.runtime = runtime
        self.tree = app_commands.CommandTree(self)
        self.action_locks = {}
        self.reconnect_tasks = {}
        self.intentional_leave = set()
        self.install_commands()

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        for guild_id, settings in self.runtime.store.get("guilds").guilds.items():
            if settings.enabled and settings.auto_rejoin:
                self.schedule_rejoin(guild_id)

    async def on_disconnect(self):
        for guild_id in list(self.runtime.scheduler.guilds):
            self.runtime.clear(guild_id)

    async def on_message(self, message):
        if not message.guild or message.author.id == self.user.id:
            return
        self.runtime.ingest(
            str(message.guild.id),
            str(message.channel.id),
            str(message.author.id),
            str(message.id),
            message.clean_content,
            is_bot=message.author.bot,
        )

    async def on_voice_state_update(self, member, before, after):
        if member.id != self.user.id:
            return
        guild_id = str(member.guild.id)
        if before.channel != after.channel:
            self.runtime.aggregator.clear(guild_id)
            self.runtime.scheduler.disconnect(guild_id)
        config = self.runtime.store.get("guilds").guilds.get(guild_id)
        if after.channel and config and str(after.channel.id) == config.voice_channel_id:
            self.runtime.scheduler.guild(guild_id).connected = True
        elif not after.channel and guild_id not in self.intentional_leave:
            self.schedule_rejoin(guild_id)

    def schedule_rejoin(self, guild_id):
        task = self.reconnect_tasks.get(guild_id)
        if task and not task.done():
            return

        async def reconnect():
            for delay in (2, 5, 15, 30, 60):
                await asyncio.sleep(delay)
                settings = self.runtime.store.get("guilds").guilds.get(guild_id)
                if (
                    self.runtime.stopping.is_set()
                    or guild_id in self.intentional_leave
                    or not settings
                    or not settings.enabled
                    or not settings.auto_rejoin
                ):
                    return
                try:
                    await self.join(guild_id)
                    return
                except (ValueError, discord.DiscordException, asyncio.TimeoutError):
                    log.warning("voice_reconnect_failed guild_id=%s", guild_id)

        self.reconnect_tasks[guild_id] = asyncio.create_task(reconnect())

    async def join(self, guild_id):
        async with self.action_locks.setdefault(guild_id, asyncio.Lock()):
            settings = self.runtime.store.get("guilds").guilds.get(guild_id)
            guild = self.get_guild(int(guild_id))
            if (
                not self.is_ready()
                or not settings
                or not settings.enabled
                or not settings.voice_channel_id
                or not guild
            ):
                raise ValueError("Guild設定またはDiscord接続を確認してください")
            channel = guild.get_channel(int(settings.voice_channel_id))
            if not isinstance(channel, discord.VoiceChannel):
                raise ValueError("通常のVoice Channelを指定してください")
            self.intentional_leave.discard(guild_id)
            if guild.voice_client:
                await guild.voice_client.move_to(channel)
            else:
                await channel.connect(timeout=15, reconnect=True, self_deaf=True)
            self.runtime.scheduler.guild(guild_id).connected = True

    async def leave(self, guild_id):
        self.intentional_leave.add(guild_id)
        self.runtime.aggregator.clear(guild_id)
        self.runtime.scheduler.disconnect(guild_id)
        async with self.action_locks.setdefault(guild_id, asyncio.Lock()):
            guild = self.get_guild(int(guild_id))
            if guild and guild.voice_client:
                guild.voice_client.stop()
                await guild.voice_client.disconnect(force=True)

    async def action(self, guild_id, name):
        if name == "join":
            await self.join(guild_id)
        elif name == "leave":
            await self.leave(guild_id)
        elif name == "clear":
            self.runtime.clear(guild_id)
        elif name == "skip":
            guild = self.get_guild(int(guild_id))
            if self.runtime.scheduler.skip(guild_id) == "playing" and guild and guild.voice_client:
                guild.voice_client.stop()

    def pump_playback(self):
        loop = asyncio.get_running_loop()
        for guild_id, queue in list(self.runtime.scheduler.guilds.items()):
            guild = self.get_guild(int(guild_id))
            client = guild.voice_client if guild else None
            if not client or not client.is_connected():
                if queue.connected:
                    self.runtime.aggregator.clear(guild_id)
                    self.runtime.scheduler.disconnect(guild_id)
                continue
            if client.is_playing() or client.is_paused():
                continue
            audio = self.runtime.scheduler.take_audio(guild_id)
            if not audio:
                continue

            def after(error, gid=guild_id, request_id=audio.job.request_id):
                loop.call_soon_threadsafe(
                    self.runtime.scheduler.playback_done, gid, request_id, bool(error)
                )

            try:
                client.play(PCMSource(audio.pcm), after=after)
                log.info("playback_start request_id=%s guild_id=%s", audio.job.request_id, guild_id)
            except (discord.DiscordException, RuntimeError):
                self.runtime.scheduler.playback_done(guild_id, audio.job.request_id, True)

    def can_manage(self, interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        settings = self.runtime.store.get("guilds").guilds.get(str(interaction.guild_id))
        roles = settings.admin_role_ids if settings else []
        return interaction.user.guild_permissions.manage_guild or any(
            str(role.id) in roles for role in interaction.user.roles
        )

    def install_commands(self):
        async def update_setting(interaction, field, value, scope):
            if scope == "guild" and not interaction.guild_id:
                await interaction.response.send_message(
                    "Guild内で実行してください。", ephemeral=True
                )
                return
            if field == "voice_id" and value not in self.runtime.store.get("voices").voices:
                await interaction.response.send_message("未登録のVoiceです。", ephemeral=True)
                return
            patch = dump(Overrides.model_validate({field: value}))
            await interaction.response.defer(ephemeral=True)
            store = self.runtime.store

            def mutate(target):
                user = target["users"].setdefault(
                    str(interaction.user.id), {"global_settings": {}, "guilds": {}}
                )
                layer = (
                    user["guilds"].setdefault(str(interaction.guild_id), {})
                    if scope == "guild"
                    else user["global_settings"]
                )
                layer.update(patch)

            store.update("users", store.get("users").revision, mutate)
            await interaction.followup.send(f"{scope}: {field} = {value}", ephemeral=True)

        @self.tree.command(name="voice", description="自分の読み上げVoiceを設定")
        async def voice(
            interaction: discord.Interaction,
            voice_id: str,
            scope: Literal["global", "guild"] = "global",
        ):
            await update_setting(interaction, "voice_id", voice_id, scope)

        @self.tree.command(name="speed", description="自分の読み上げ速度を設定（100が等速）")
        async def speed(
            interaction: discord.Interaction,
            percent: app_commands.Range[int, 50, 200],
            scope: Literal["global", "guild"] = "global",
        ):
            await update_setting(interaction, "speed_percent", percent, scope)

        @self.tree.command(name="volume", description="自分の読み上げ音量を設定")
        async def volume(
            interaction: discord.Interaction,
            percent: app_commands.Range[int, 0, 100],
            scope: Literal["global", "guild"] = "global",
        ):
            await update_setting(interaction, "volume_percent", percent, scope)

        group = app_commands.Group(name="tts", description="読み上げの操作")

        @group.command(name="reset", description="Guild内の自分の設定をGlobalからの継承へ戻す")
        @app_commands.guild_only()
        async def reset(
            interaction: discord.Interaction,
            field: Literal["voice_id", "speed_percent", "volume_percent"],
        ):
            await interaction.response.defer(ephemeral=True)
            store = self.runtime.store

            def mutate(target):
                layer = (
                    target["users"]
                    .get(str(interaction.user.id), {})
                    .get("guilds", {})
                    .get(str(interaction.guild_id), {})
                )
                layer.pop(field, None)

            store.update("users", store.get("users").revision, mutate)
            await interaction.followup.send("Global設定の継承へ戻しました。", ephemeral=True)

        @group.command(name="status", description="読み上げの状態")
        @app_commands.guild_only()
        async def status(interaction: discord.Interaction):
            state = self.runtime.scheduler.status().get(str(interaction.guild_id), {})
            await interaction.response.send_message(
                f"TTS ready: {self.runtime.engine_ready}\n{state}", ephemeral=True
            )

        async def run_action(interaction, name):
            allowed = self.can_manage(interaction)
            if (
                name == "skip"
                and interaction.guild
                and isinstance(interaction.user, discord.Member)
            ):
                client = interaction.guild.voice_client
                allowed = allowed or bool(
                    client
                    and interaction.user.voice
                    and interaction.user.voice.channel == client.channel
                )
            if not allowed:
                await interaction.response.send_message(
                    "この操作を実行する権限がありません。", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            await self.action(str(interaction.guild_id), name)
            await interaction.followup.send(f"{name}を実行しました。", ephemeral=True)

        def command(name, description):
            async def callback(interaction: discord.Interaction):
                await run_action(interaction, name)

            return app_commands.Command(name=name, description=description, callback=callback)

        for name, description in [
            ("join", "設定されたVCへ参加"),
            ("leave", "VCから退出"),
            ("skip", "現在の読み上げをスキップ"),
            ("clear", "待機中の読み上げを削除"),
        ]:
            group.add_command(command(name, description))
        self.tree.add_command(group)

        @self.tree.error
        async def on_error(interaction, error):
            log.error("command_failed type=%s", type(error).__name__)
            text = "処理に失敗しました。設定・接続状態・権限を確認してください。"
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)

    async def close(self):
        for task in self.reconnect_tasks.values():
            task.cancel()
        await asyncio.gather(*self.reconnect_tasks.values(), return_exceptions=True)
        for client in list(self.voice_clients):
            client.stop()
            await client.disconnect(force=True)
        await super().close()
