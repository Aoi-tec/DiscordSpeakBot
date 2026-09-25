"""Discord client: voice connection lifecycle and playback.

Slash commands live in commands.py, embeds/buttons in views.py.
"""

import asyncio
import io
import logging

import discord
from discord import app_commands

from ..settings.models import MAX_GUILDS

log = logging.getLogger("worker")


class GuildLimitError(ValueError):
    pass


class BotPermissionError(ValueError):
    pass


def ensure_guild(store, guild_id, *, text_channel_id=None, voice_channel_id=None):
    """Create or update a guild entry. Returns True when a read channel was newly added."""
    document = store.get("guilds")
    if guild_id not in document.guilds and len(document.guilds) >= MAX_GUILDS:
        raise GuildLimitError(
            f"このBotは同時に最大{MAX_GUILDS}サーバーまでしか設定できません。"
            "他サーバーで `/yomiage reset` を実行してから再度お試しください。"
        )
    added = False

    def mutate(target):
        nonlocal added
        guild = target["guilds"].setdefault(guild_id, {"enabled": True})
        guild["enabled"] = True
        channels = guild.setdefault("text_channel_ids", [])
        if text_channel_id and text_channel_id not in channels:
            channels.append(text_channel_id)
            added = True
        if voice_channel_id:
            guild["voice_channel_id"] = voice_channel_id

    store.update("guilds", document.revision, mutate)
    return added


def configure_first_join(store, guild_id, text_channel_id, voice_channel_id):
    """Backward-compatible helper: only configures a guild that has no settings yet."""
    if guild_id in store.get("guilds").guilds:
        return
    ensure_guild(
        store, guild_id, text_channel_id=text_channel_id, voice_channel_id=voice_channel_id
    )
    log.info(
        "guild_configured_by_join guild_id=%s text_channel_id=%s voice_channel_id=%s",
        guild_id,
        text_channel_id,
        voice_channel_id,
    )


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
        self._owner_ids = None
        self.install_commands()

    # ------------------------------------------------------------------ lifecycle

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        log.info("discord_ready guild_ids=%s", [str(guild.id) for guild in self.guilds])
        for guild_id, settings in self.runtime.store.get("guilds").guilds.items():
            if settings.enabled and settings.auto_rejoin:
                self.schedule_rejoin(guild_id)

    async def on_guild_join(self, guild):
        log.info("discord_guild_join guild_id=%s", guild.id)

    async def on_guild_remove(self, guild):
        log.info("discord_guild_remove guild_id=%s", guild.id)

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
            # Someone joined/left/moved: leave when no human is left with the bot.
            await self.leave_if_alone(member.guild)
            self.announce_voice_state(member, before, after)
            return
        guild_id = str(member.guild.id)
        if before.channel != after.channel:
            self.runtime.aggregator.clear(guild_id)
            self.runtime.scheduler.disconnect(guild_id)
        if after.channel:
            # Any voice channel counts as connected, including after being moved by a
            # moderator (previously only the configured channel re-enabled reading).
            self.runtime.scheduler.guild(guild_id).connected = True
            self.remember_voice_channel(guild_id, str(after.channel.id))
            self.runtime.notify()
            # Covers being moved into an empty channel and auto-rejoin into an empty channel.
            await self.leave_if_alone(member.guild)
        elif guild_id not in self.intentional_leave:
            self.schedule_rejoin(guild_id)

    def announce_voice_state(self, member, before, after):
        if member.bot or before.channel == after.channel:
            return
        client = member.guild.voice_client
        channel = client.channel if client else None
        if channel is None:
            return
        name = member.display_name
        if after.channel == channel:
            text = f"{name}さんが入室しました"
        elif before.channel == channel:
            text = f"{name}さんが退室しました"
        else:
            return
        self.runtime.announce(str(member.guild.id), str(member.id), text)

    async def leave_if_alone(self, guild):
        client = guild.voice_client
        channel = client.channel if client else None
        if not channel or any(not member.bot for member in channel.members):
            return
        log.info("voice_auto_leave guild_id=%s channel_id=%s", guild.id, channel.id)
        # leave() marks the guild as intentionally left, so auto_rejoin does not bring it back.
        await self.leave(str(guild.id))

    def remember_voice_channel(self, guild_id, channel_id):
        store = self.runtime.store
        settings = store.get("guilds").guilds.get(guild_id)
        if settings and settings.voice_channel_id != channel_id:
            store.update(
                "guilds",
                store.get("guilds").revision,
                lambda target: target["guilds"][guild_id].update(voice_channel_id=channel_id),
            )

    async def is_owner(self, user):
        if self._owner_ids is None:
            info = await self.application_info()
            if info.team:
                self._owner_ids = {member.id for member in info.team.members}
            else:
                self._owner_ids = {info.owner.id}
        return user.id in self._owner_ids

    # ------------------------------------------------------------------ voice

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

    async def join(self, guild_id, channel=None):
        """Join `channel`, or the last remembered voice channel when omitted."""
        async with self.action_locks.setdefault(guild_id, asyncio.Lock()):
            settings = self.runtime.store.get("guilds").guilds.get(guild_id)
            guild = self.get_guild(int(guild_id))
            if not self.is_ready() or not guild:
                raise ValueError("Discordに接続していません。しばらくしてから再度お試しください。")
            if channel is None:
                if not settings or not settings.enabled or not settings.voice_channel_id:
                    raise ValueError("接続先のVCが未設定です。")
                channel = guild.get_channel(int(settings.voice_channel_id))
            if not isinstance(channel, discord.VoiceChannel):
                raise ValueError("通常のボイスチャンネルに参加してから実行してください。")
            permissions = channel.permissions_for(guild.me)
            missing = [
                label
                for label, ok in (
                    ("チャンネルを見る", permissions.view_channel),
                    ("接続", permissions.connect),
                    ("発言", permissions.speak),
                )
                if not ok
            ]
            if missing:
                raise BotPermissionError(
                    f"Botに {channel.mention} の権限が足りません: " + "、".join(missing)
                )
            self.intentional_leave.discard(guild_id)
            if guild.voice_client:
                if guild.voice_client.channel != channel:
                    await guild.voice_client.move_to(channel)
            else:
                await channel.connect(timeout=15, reconnect=True, self_deaf=True)
            self.runtime.scheduler.guild(guild_id).connected = True
            self.remember_voice_channel(guild_id, str(channel.id))
            self.runtime.notify()
            return channel

    async def leave(self, guild_id):
        self.intentional_leave.add(guild_id)
        self.runtime.aggregator.clear(guild_id)
        self.runtime.scheduler.disconnect(guild_id)
        async with self.action_locks.setdefault(guild_id, asyncio.Lock()):
            guild = self.get_guild(int(guild_id))
            if guild and guild.voice_client:
                guild.voice_client.stop()
                await guild.voice_client.disconnect(force=True)

    def skip(self, guild_id):
        guild = self.get_guild(int(guild_id))
        result = self.runtime.scheduler.skip(guild_id)
        if result == "playing" and guild and guild.voice_client:
            guild.voice_client.stop()
        self.runtime.notify()
        return result

    async def action(self, guild_id, name):
        """Actions requested by the local Host API."""
        if name == "join":
            await self.join(guild_id)
        elif name == "leave":
            await self.leave(guild_id)
        elif name == "clear":
            self.runtime.clear(guild_id)
        elif name == "skip":
            self.skip(guild_id)

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
                loop.call_soon_threadsafe(self.runtime.playback_done, gid, request_id, bool(error))

            try:
                client.play(PCMSource(audio.pcm), after=after)
                log.info("playback_start request_id=%s guild_id=%s", audio.job.request_id, guild_id)
            except (discord.DiscordException, RuntimeError):
                self.runtime.playback_done(guild_id, audio.job.request_id, True)

    # ------------------------------------------------------------------ commands

    def can_manage(self, interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        settings = self.runtime.store.get("guilds").guilds.get(str(interaction.guild_id))
        roles = settings.admin_role_ids if settings else []
        return interaction.user.guild_permissions.manage_guild or any(
            str(role.id) in roles for role in interaction.user.roles
        )

    def can_clear(self, interaction):
        """Administrators, or (when enabled per guild) members holding a clear role."""
        if self.can_manage(interaction):
            return True
        settings = self.runtime.store.get("guilds").guilds.get(str(interaction.guild_id))
        if not settings or not settings.clear_by_role:
            return False
        roles = getattr(interaction.user, "roles", [])
        return any(str(role.id) in settings.clear_role_ids for role in roles)

    def in_bot_voice_channel(self, interaction):
        guild = interaction.guild
        client = guild.voice_client if guild else None
        voice = getattr(interaction.user, "voice", None)
        return bool(client and voice and voice.channel == client.channel)

    def install_commands(self):
        from .commands import Yomiage

        self.tree.add_command(Yomiage(self))

        @self.tree.error
        async def on_error(interaction, error):
            cause = getattr(error, "original", error)
            log.error("command_failed type=%s cause=%s", type(error).__name__, type(cause).__name__)
            text = "処理に失敗しました。設定・接続状態・権限を確認してください。"
            if isinstance(error, app_commands.NoPrivateMessage):
                text = "サーバー内で実行してください。"
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
