"""/yomiage slash command tree."""

import asyncio
import logging

import discord
from discord import app_commands

from ..settings.models import MAX_TEXT_CHANNELS, Overrides, dump
from . import views
from .bot import BotPermissionError, GuildLimitError, ensure_guild

log = logging.getLogger("worker")

SCOPES = [
    app_commands.Choice(name="全サーバー共通", value="global"),
    app_commands.Choice(name="このサーバーのみ", value="guild"),
]
READABLE = (discord.TextChannel, discord.VoiceChannel)


def listeners(channel):
    return [member for member in channel.members if not member.bot] if channel else []


class Yomiage(app_commands.Group):
    add = app_commands.Group(name="add", description="読み上げ対象を追加します")
    remove = app_commands.Group(name="remove", description="読み上げ対象を削除します")
    all_group = app_commands.Group(name="all", description="すべての範囲に対する操作")
    permission = app_commands.Group(name="permission", description="権限の設定（管理者）")

    def __init__(self, bot):
        super().__init__(
            name="yomiage", description="テキストチャンネル読み上げBot", guild_only=True
        )
        self.bot = bot

    # ------------------------------------------------------------------ helpers

    @property
    def store(self):
        return self.bot.runtime.store

    async def reply(self, interaction, embed, view=None, *, ephemeral=True):
        kwargs = {"embed": embed, "ephemeral": ephemeral}
        if view is not None:
            kwargs["view"] = view
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)
        if view is not None:
            view.origin = interaction

    async def deny(self, interaction, text):
        await self.reply(interaction, views.notice(text, ok=False))

    def resolve_model(self, query, user_id):
        voices = views.usable_voices(self.bot.runtime, user_id)
        if query in voices:
            return query, None
        folded = query.casefold()
        exact = [vid for vid, voice in voices.items() if voice.name.casefold() == folded]
        if len(exact) == 1:
            return exact[0], None
        partial = [
            vid
            for vid, voice in voices.items()
            if folded in vid.casefold() or folded in voice.name.casefold()
        ]
        if len(partial) == 1:
            return partial[0], None
        if not partial:
            return (
                None,
                f"「{query}」に一致するモデルがありません。`/yomiage list` で確認できます。",
            )
        names = "、".join(voices[vid].name for vid in partial[:5])
        return None, f"「{query}」に一致するモデルが複数あります: {names}"

    def apply_setting(self, user_id, guild_id, field, value, scope):
        """Save one personal setting in the chosen layer and return a message."""
        patch = dump(Overrides.model_validate({field: value}))
        user_id = str(user_id)
        if field == "voice_id":
            voice = self.store.get("voices").voices.get(value)
            if voice is None or not voice.usable_by(user_id):
                return "⛔ このモデルは使用できません（他の人の専用ボイスです）。"

        def mutate(target):
            user = target["users"].setdefault(user_id, {"global_settings": {}, "guilds": {}})
            layer = (
                user["guilds"].setdefault(guild_id, {})
                if scope == "guild"
                else user["global_settings"]
            )
            layer.update(patch)

        self.store.update("users", self.store.get("users").revision, mutate)
        if field == "voice_id":
            voice = self.store.get("voices").voices[value]
            text = f"🎙 モデルを **{discord.utils.escape_markdown(voice.name)}** に変更しました"
        else:
            text = f"⏩ 読み上げ速度を **{value}%** に変更しました"
        text += f"（{views.SCOPE_LABEL[scope]}）"
        user = self.store.get("users").users[user_id]
        override = user.guilds.get(guild_id)
        if scope == "global" and override and getattr(override, field) is not None:
            text += (
                "\n⚠️ このサーバーでは「このサーバーのみ」の設定が優先されます。"
                "共通設定を使うには `/yomiage reset` を実行してください。"
            )
        return text

    def remove_channels(self, guild_id, channel_ids):
        removed = []

        def mutate(target):
            guild = target["guilds"].get(guild_id)
            if not guild:
                return
            channels = guild.setdefault("text_channel_ids", [])
            for channel_id in channel_ids:
                if channel_id in channels:
                    channels.remove(channel_id)
                    removed.append(channel_id)

        self.store.update("guilds", self.store.get("guilds").revision, mutate)
        return removed

    def read_channels(self, guild_id):
        config = self.store.get("guilds").guilds.get(guild_id)
        return list(config.text_channel_ids) if config else []

    def can_control_playback(self, interaction):
        return self.bot.can_manage(interaction) or self.bot.in_bot_voice_channel(interaction)

    # ------------------------------------------------------------------ voice connection

    @app_commands.command(name="join", description="あなたがいるボイスチャンネルに参加します")
    async def join(self, interaction: discord.Interaction):
        guild = interaction.guild
        guild_id = str(guild.id)
        voice = getattr(interaction.user, "voice", None)
        channel = voice.channel if voice else None
        if not isinstance(channel, discord.VoiceChannel):
            return await self.deny(
                interaction, "先にボイスチャンネルへ参加してから実行してください。"
            )
        client = guild.voice_client
        if client and client.channel == channel:
            return await self.reply(
                interaction, views.notice(f"すでに {channel.mention} で読み上げ中です。")
            )
        if client and listeners(client.channel) and not self.bot.can_manage(interaction):
            return await self.deny(
                interaction,
                f"Botは現在 {client.channel.mention} で読み上げ中です。"
                "移動させるには管理権限が必要です。",
            )
        added_here = None
        if not self.read_channels(guild_id) and isinstance(interaction.channel, READABLE):
            added_here = str(interaction.channel_id)
        try:
            ensure_guild(
                self.store,
                guild_id,
                text_channel_id=added_here,
                voice_channel_id=str(channel.id),
            )
        except GuildLimitError as exc:
            return await self.deny(interaction, str(exc))
        await interaction.response.defer(thinking=True)
        try:
            await self.bot.join(guild_id, channel)
        except (ValueError, BotPermissionError) as exc:
            return await interaction.edit_original_response(embed=views.notice(str(exc), ok=False))
        except discord.Forbidden:
            return await interaction.edit_original_response(
                embed=views.notice("Botに対象VCの接続・発言権限がありません。", ok=False)
            )
        except (discord.DiscordException, asyncio.TimeoutError) as exc:
            log.warning("voice_join_failed guild_id=%s type=%s", guild_id, type(exc).__name__)
            return await interaction.edit_original_response(
                embed=views.notice(
                    "VC接続に失敗しました。時間をおいて再度お試しください。", ok=False
                )
            )
        channels = self.read_channels(guild_id)
        embed = discord.Embed(
            title="🔊 読み上げを開始しました",
            description=f"{channel.mention} に参加しました。",
            color=views.COLOR_OK,
        )
        embed.add_field(
            name="📝 読み上げチャンネル",
            value=" ".join(f"<#{c}>" for c in channels) or "未設定 — `/yomiage add channel`",
            inline=False,
        )
        if added_here:
            embed.set_footer(text="初回のため、このチャンネルを読み上げ対象に追加しました")
        if not self.bot.runtime.engine_ready:
            state, _ = views.engine_state(self.bot.runtime)
            embed.add_field(name="🤖 音声エンジン", value=state, inline=False)
            embed.color = views.COLOR_WARN
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="leave", description="ボイスチャンネルから退出します")
    async def leave(self, interaction: discord.Interaction):
        guild = interaction.guild
        client = guild.voice_client
        if not client:
            return await self.deny(interaction, "Botはボイスチャンネルに参加していません。")
        channel = client.channel
        if listeners(channel) and not self.can_control_playback(interaction):
            return await self.deny(interaction, "Botと同じVCに参加しているか、管理権限が必要です。")
        await interaction.response.defer(thinking=True)
        await self.bot.leave(str(guild.id))
        await interaction.edit_original_response(
            embed=views.notice(f"👋 {channel.mention} から退出しました。")
        )

    # ------------------------------------------------------------------ read channels

    @add.command(name="channel", description="読み上げ対象のチャンネルを追加します")
    @app_commands.describe(channel="追加するチャンネル（省略時はこのチャンネル）")
    async def add_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.VoiceChannel | None = None,
    ):
        target = channel or interaction.channel
        if not isinstance(target, READABLE):
            return await self.deny(
                interaction, "テキストチャンネルかVCのチャットを指定してください。"
            )
        guild_id = str(interaction.guild_id)
        channels = self.read_channels(guild_id)
        if str(target.id) in channels:
            return await self.reply(
                interaction, views.notice(f"{target.mention} はすでに読み上げ対象です。")
            )
        if len(channels) >= MAX_TEXT_CHANNELS:
            return await self.deny(
                interaction, f"読み上げチャンネルは最大{MAX_TEXT_CHANNELS}件までです。"
            )
        if not target.permissions_for(interaction.guild.me).view_channel:
            return await self.deny(
                interaction,
                f"Botが {target.mention} を閲覧できません。チャンネル権限を確認してください。",
            )
        try:
            ensure_guild(self.store, guild_id, text_channel_id=str(target.id))
        except GuildLimitError as exc:
            return await self.deny(interaction, str(exc))
        channels = self.read_channels(guild_id)
        embed = views.notice(f"📝 {target.mention} を読み上げ対象に追加しました。")
        embed.add_field(
            name=f"読み上げチャンネル ({len(channels)})",
            value=" ".join(f"<#{c}>" for c in channels),
        )
        await self.reply(interaction, embed)

    @remove.command(name="channel", description="読み上げ対象のチャンネルを削除します")
    @app_commands.describe(
        channel="削除するチャンネル（省略時はこのチャンネル、または一覧から選択）"
    )
    async def remove_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.VoiceChannel | None = None,
    ):
        guild_id = str(interaction.guild_id)
        channels = self.read_channels(guild_id)
        if not channels:
            return await self.deny(interaction, "読み上げ対象のチャンネルはありません。")
        target = str((channel or interaction.channel).id)
        if channel is None and target not in channels:
            view = views.ChannelRemoveView(self, interaction.user.id, interaction.guild, channels)
            embed = discord.Embed(
                title="📝 読み上げチャンネルの削除",
                description="削除するチャンネルを選んでください（複数選択可）。",
                color=views.COLOR_INFO,
            )
            return await self.reply(interaction, embed, view)
        if target not in channels:
            return await self.deny(interaction, f"<#{target}> は読み上げ対象ではありません。")
        self.remove_channels(guild_id, [target])
        await self.reply(interaction, views.notice(f"<#{target}> を読み上げ対象から削除しました。"))

    # ------------------------------------------------------------------ user settings

    @app_commands.command(name="list", description="使用できるモデルの一覧を表示します")
    async def list_models(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        embed = views.models_embed(self.bot.runtime, str(interaction.user.id), guild_id)
        view = None
        if views.usable_voices(self.bot.runtime, interaction.user.id):
            view = views.VoicePickerView(self, interaction.user.id, guild_id, "global")
        await self.reply(interaction, embed, view)

    @app_commands.command(name="voice", description="読み上げモデルを変更します")
    @app_commands.describe(
        model="モデル名またはID（省略すると一覧から選べます）", scope="設定を適用する範囲"
    )
    @app_commands.choices(scope=SCOPES)
    async def voice(
        self,
        interaction: discord.Interaction,
        model: str | None = None,
        scope: app_commands.Choice[str] | None = None,
    ):
        guild_id = str(interaction.guild_id)
        scope_value = scope.value if scope else "global"
        if not views.usable_voices(self.bot.runtime, interaction.user.id):
            return await self.deny(interaction, "使用できるモデルがありません。")
        if model:
            voice_id, error = self.resolve_model(model.strip(), interaction.user.id)
            if error:
                return await self.deny(interaction, error)
            text = self.apply_setting(
                interaction.user.id, guild_id, "voice_id", voice_id, scope_value
            )
            embed = views.settings_embed(
                self.bot.runtime, str(interaction.user.id), guild_id, text=text
            )
            return await self.reply(interaction, embed)
        view = views.VoicePickerView(self, interaction.user.id, guild_id, scope_value)
        await self.reply(interaction, view.render(), view)

    @voice.autocomplete("model")
    async def model_autocomplete(self, interaction: discord.Interaction, current: str):
        folded = current.casefold()
        voices = sorted(
            views.usable_voices(self.bot.runtime, interaction.user.id).items(),
            key=lambda item: item[1].name,
        )
        return [
            app_commands.Choice(name=f"{views.voice_label(voice)} ({vid})"[:100], value=vid)
            for vid, voice in voices
            if folded in vid.casefold() or folded in voice.name.casefold()
        ][:25]

    @app_commands.command(name="speed", description="読み上げ速度を変更します（100%が等速）")
    @app_commands.describe(
        percent="速度（50〜200%。省略するとボタンで調整）", scope="設定を適用する範囲"
    )
    @app_commands.choices(scope=SCOPES)
    async def speed(
        self,
        interaction: discord.Interaction,
        percent: app_commands.Range[int, 50, 200] | None = None,
        scope: app_commands.Choice[str] | None = None,
    ):
        guild_id = str(interaction.guild_id)
        scope_value = scope.value if scope else "global"
        text = None
        if percent is not None:
            text = self.apply_setting(
                interaction.user.id, guild_id, "speed_percent", percent, scope_value
            )
        view = views.SpeedView(self, interaction.user.id, guild_id, scope_value)
        await self.reply(interaction, view.render(text), view)

    # ------------------------------------------------------------------ playback

    @app_commands.command(name="skip", description="現在の読み上げをスキップします")
    async def skip(self, interaction: discord.Interaction):
        if not self.can_control_playback(interaction):
            return await self.deny(interaction, "Botと同じVCに参加しているか、管理権限が必要です。")
        guild_id = str(interaction.guild_id)
        snapshot = self.bot.runtime.scheduler.snapshot(guild_id)
        if not (snapshot["playing"] or self.bot.runtime.scheduler.pending_count(guild_id)):
            return await self.reply(interaction, views.notice("スキップする読み上げはありません。"))
        order = [snapshot["playing"], *snapshot["generated"], snapshot["generating"]]
        target = next(job for job in [*order, *snapshot["text"]] if job)
        self.bot.skip(guild_id)
        await self.reply(
            interaction,
            views.notice(f"⏭ スキップしました: <@{target.user_id}> {views.excerpt(target.text)}"),
        )

    @app_commands.command(
        name="clear",
        description="このサーバーの読み上げ待ちをすべて削除します（管理者・許可ロール）",
    )
    async def clear(self, interaction: discord.Interaction):
        if not self.bot.can_clear(interaction):
            return await self.deny(interaction, views.CLEAR_DENIED)
        guild_id = str(interaction.guild_id)
        count = self.bot.runtime.scheduler.pending_count(guild_id)
        count += int(guild_id in self.bot.runtime.aggregator.pending)
        self.bot.runtime.clear(guild_id)
        await self.reply(interaction, views.notice(f"🗑 読み上げ待ち {count} 件を削除しました。"))

    # ------------------------------------------------------------------ reset (personal)

    @app_commands.command(
        name="reset", description="このサーバーでのあなたの設定を初期化します（共通設定に戻す）"
    )
    async def reset(self, interaction: discord.Interaction):
        user_id, guild_id = str(interaction.user.id), str(interaction.guild_id)
        if self.bot.runtime.reset_user(user_id, guild_id):
            text = "♻️ このサーバーでのあなたの設定を削除しました。全サーバー共通の設定を使います。"
        else:
            text = "このサーバー専用の設定はありません。全サーバー共通の設定を使っています。"
        await self.reply(
            interaction, views.settings_embed(self.bot.runtime, user_id, guild_id, text=text)
        )

    @all_group.command(name="reset", description="あなたの設定をすべて（全サーバー分）初期化します")
    async def all_reset(self, interaction: discord.Interaction):
        user_id, guild_id = str(interaction.user.id), str(interaction.guild_id)

        async def confirmed(_):
            self.bot.runtime.reset_user(user_id)
            return views.settings_embed(
                self.bot.runtime,
                user_id,
                guild_id,
                text="♻️ あなたの設定をすべて初期化しました。Botの既定値を使います。",
            )

        embed = discord.Embed(
            title="⚠️ あなたの設定をすべて初期化しますか？",
            description=(
                "・全サーバー共通のモデル/速度\n"
                "・すべてのサーバーの「このサーバーのみ」のモデル/速度\n\n"
                "を削除し、Botの既定値に戻します。他の人やサーバーの設定には影響しません。"
            ),
            color=views.COLOR_WARN,
        )
        views.add_settings_fields(embed, self.bot.runtime, user_id, guild_id)
        await self.reply(interaction, embed, views.ConfirmView(interaction.user.id, confirmed))

    # ------------------------------------------------------------------ permission

    @permission.command(
        name="clear", description="ロールで /yomiage clear を許可するかを設定します（管理者）"
    )
    @app_commands.describe(
        enabled="True: 許可ロールでもクリア可能 / False: 管理者のみ",
        role="許可するロール（enabled=Trueで追加、enabled=Falseで指定ロールだけ解除）",
    )
    async def permission_clear(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        role: discord.Role | None = None,
    ):
        if not self.bot.can_manage(interaction):
            return await self.deny(interaction, "この設定には「サーバー管理」権限が必要です。")
        guild_id = str(interaction.guild_id)
        try:
            ensure_guild(self.store, guild_id)
        except GuildLimitError as exc:
            return await self.deny(interaction, str(exc))

        def mutate(target):
            guild = target["guilds"][guild_id]
            roles = guild.setdefault("clear_role_ids", [])
            if role is None:
                guild["clear_by_role"] = enabled
            elif enabled:
                guild["clear_by_role"] = True
                if str(role.id) not in roles:
                    roles.append(str(role.id))
            elif str(role.id) in roles:
                roles.remove(str(role.id))

        self.store.update("guilds", self.store.get("guilds").revision, mutate)
        config = self.store.get("guilds").guilds[guild_id]
        embed = discord.Embed(
            title="🔐 クリア権限",
            color=views.COLOR_OK if config.clear_by_role else views.COLOR_INFO,
        )
        embed.add_field(
            name="ロールでの許可",
            value="✅ True（管理者＋許可ロール）"
            if config.clear_by_role
            else "⛔ False（管理者のみ）",
        )
        embed.add_field(
            name="許可ロール",
            value=" ".join(f"<@&{r}>" for r in config.clear_role_ids) or "なし",
        )
        if config.clear_by_role and not config.clear_role_ids:
            embed.set_footer(text="許可ロールが未設定のため、現在は管理者のみがクリアできます")
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ info

    @app_commands.command(name="status", description="読み上げの状況を表示します")
    async def status(self, interaction: discord.Interaction):
        embed = views.status_embed(self.bot, interaction.guild, interaction.user)
        await self.reply(interaction, embed, views.StatusView(self.bot, interaction.user.id))

    @app_commands.command(name="queue", description="このサーバーの読み上げ待ちを表示します")
    async def queue(self, interaction: discord.Interaction):
        embed = views.queue_embed(self.bot, interaction.guild)
        await self.reply(interaction, embed, views.QueueView(self.bot, interaction.user.id))
