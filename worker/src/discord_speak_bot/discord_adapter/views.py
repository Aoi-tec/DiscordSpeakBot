"""Embeds and interactive components (buttons / select menus) for /yomiage."""

import time

import discord

from ..settings.models import resolve

COLOR_OK = discord.Color.from_rgb(67, 181, 129)
COLOR_WARN = discord.Color.from_rgb(250, 166, 26)
COLOR_ERROR = discord.Color.from_rgb(240, 71, 71)
COLOR_INFO = discord.Color.from_rgb(88, 101, 242)

SCOPE_LABEL = {"global": "全サーバー共通", "guild": "このサーバーのみ", "system": "Botの既定値"}
SPEED_MIN, SPEED_MAX = 50, 200
PAGE_SIZE = 25
SKIP_DENIED = "Botと同じVCに参加しているか、管理権限が必要です。"
CLEAR_DENIED = "クリアは管理者、または管理者が許可したロールを持つ人だけが実行できます。"


# --------------------------------------------------------------------------- helpers


def excerpt(text, limit=40):
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(" ".join(text.split())))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def bar(value, total, width=10):
    total = max(total, 1)
    filled = min(width, round(width * value / total))
    return "▰" * filled + "▱" * (width - filled)


def age(job):
    seconds = max(0, int(time.monotonic() - job.received_at))
    if seconds < 60:
        return f"{seconds}秒前"
    return f"{seconds // 60}分前" if seconds < 3600 else f"{seconds // 3600}時間前"


def voice_name(voices, voice_id):
    voice = voices.get(voice_id) if voice_id else None
    return f"{voice.name} (`{voice_id}`)" if voice else "未設定"


def channel_label(guild, channel_id):
    channel = guild.get_channel(int(channel_id)) if guild else None
    return f"#{channel.name}"[:100] if channel else f"(削除済み) {channel_id}"


def notice(text, *, ok=True):
    return discord.Embed(description=text, color=COLOR_OK if ok else COLOR_ERROR)


def engine_state(runtime):
    if runtime.engine_ready:
        return "🟢 準備完了", COLOR_OK
    if runtime.last_error or runtime.restart_requested:
        return f"🔴 エラー `{runtime.last_error or 'RESTART_REQUIRED'}`", COLOR_ERROR
    return "🟡 モデル読み込み中", COLOR_WARN


def usable_voices(runtime, user_id):
    """Public voices plus the personal voices this user is allowed to use."""
    voices = runtime.store.get("voices").voices
    return {vid: voice for vid, voice in voices.items() if voice.usable_by(str(user_id))}


def voice_label(voice):
    return f"🔒 {voice.name}" if voice.allowed_user_ids else voice.name


def user_settings(runtime, user_id, guild_id):
    settings, sources = resolve(
        runtime.store.get("system"), runtime.store.get("users"), user_id, guild_id
    )
    return settings, sources


def add_settings_fields(embed, runtime, user_id, guild_id):
    """Three columns: global layer / this-server layer / value actually used here."""
    voices = runtime.store.get("voices").voices
    user = runtime.store.get("users").users.get(user_id)
    layers = {
        "global": user.global_settings if user else None,
        "guild": user.guilds.get(guild_id) if user else None,
    }
    settings, sources = user_settings(runtime, user_id, guild_id)
    effective = runtime.speech_settings(user_id, guild_id)

    def column(layer, fallback):
        voice = layer.voice_id if layer and layer.voice_id else None
        speed = layer.speed_percent if layer and layer.speed_percent is not None else None
        return (
            f"モデル: {voices[voice].name if voice in voices else fallback}\n"
            f"速度: {f'{speed}%' if speed is not None else fallback}"
        )

    embed.add_field(name="🌐 全サーバー共通", value=column(layers["global"], "既定値"))
    embed.add_field(name="🏠 このサーバーのみ", value=column(layers["guild"], "共通を使用"))
    embed.add_field(
        name="✅ このサーバーで使う値",
        value=(
            f"モデル: **{voices[effective.voice_id].name if effective.voice_id in voices else '未設定'}**"
            f"\n速度: **{settings.speed_percent}%**"
        ),
    )
    return sources


def settings_embed(runtime, user_id, guild_id, title="🎤 あなたの読み上げ設定", text=None):
    embed = discord.Embed(title=title, description=text, color=COLOR_INFO)
    add_settings_fields(embed, runtime, user_id, guild_id)
    embed.set_footer(text="「このサーバーのみ」の設定があれば、共通設定より優先されます")
    return embed


# --------------------------------------------------------------------------- embeds


def status_embed(bot, guild, user):
    runtime = bot.runtime
    guild_id = str(guild.id)
    config = runtime.store.get("guilds").guilds.get(guild_id)
    snapshot = runtime.scheduler.snapshot(guild_id)
    client = guild.voice_client
    connected = bool(client and client.is_connected())
    engine_text, color = engine_state(runtime)
    if color == COLOR_OK and not connected:
        color = COLOR_WARN

    embed = discord.Embed(title="📢 読み上げステータス", color=color)
    embed.add_field(
        name="🔊 ボイスチャンネル",
        value=client.channel.mention if connected else "未接続 — `/yomiage join`",
        inline=True,
    )
    embed.add_field(name="🤖 音声エンジン", value=engine_text, inline=True)
    channels = config.text_channel_ids if config else []
    embed.add_field(
        name=f"📝 読み上げチャンネル ({len(channels)})",
        value=" ".join(f"<#{c}>" for c in channels) or "未設定 — `/yomiage add channel`",
        inline=False,
    )
    playing = snapshot["playing"]
    embed.add_field(
        name="▶️ 再生中",
        value=f"<@{playing.user_id}> {excerpt(playing.text)}" if playing else "なし",
        inline=False,
    )
    waiting = len(snapshot["text"])
    ready = len(snapshot["generated"])
    limit = min(config.max_queue if config else 10, runtime.config.queue.max_text_queue)
    embed.add_field(
        name="📦 キュー",
        value=(
            f"`{bar(waiting, limit)}` {waiting}/{limit}\n"
            f"⏳ 待機 **{waiting}** ・ ⚙️ 生成中 **{int(bool(snapshot['generating']))}**"
            f" ・ ✅ 再生待ち **{ready}**"
        ),
        inline=False,
    )
    add_settings_fields(embed, runtime, str(user.id), guild_id)
    metrics = runtime.metrics
    last = f"{metrics['ttfa_ms']:.0f} ms" if metrics.get("ttfa_ms") is not None else "—"
    embed.add_field(
        name="⏱ パフォーマンス",
        value=(
            f"直近の生成時間 **{last}** ・ 完了 {metrics['completed']} ・ 失敗 {metrics['failed']}"
        ),
        inline=False,
    )
    embed.set_footer(text="🔄 で最新の状態に更新 / 📋 でキューの中身を表示")
    return embed


def queue_embed(bot, guild):
    runtime = bot.runtime
    guild_id = str(guild.id)
    config = runtime.store.get("guilds").guilds.get(guild_id)
    snapshot = runtime.scheduler.snapshot(guild_id)
    rows = []
    if snapshot["playing"]:
        rows.append(("▶️", "再生中", snapshot["playing"]))
    rows += [("✅", "再生待ち", job) for job in snapshot["generated"]]
    if snapshot["generating"]:
        rows.append(("⚙️", "生成中", snapshot["generating"]))
    rows += [("⏳", "待機", job) for job in snapshot["text"]]

    limit = min(config.max_queue if config else 10, runtime.config.queue.max_text_queue)
    waiting = len(snapshot["text"])
    embed = discord.Embed(
        title="📋 読み上げキュー",
        color=COLOR_INFO if rows else COLOR_OK,
        description=f"`{bar(waiting, limit)}` 待機 {waiting}/{limit}",
    )
    if not rows:
        embed.description += "\n\n読み上げ待ちはありません ✨"
    shown = rows[:15]
    lines = [
        f"`{index:>2}` {icon} <@{job.user_id}> {excerpt(job.text, 50)}  ·  *{label}・{age(job)}*"
        for index, (icon, label, job) in enumerate(shown, start=1)
    ]
    if lines:
        embed.add_field(name="順番", value="\n".join(lines)[:1024], inline=False)
    if len(rows) > len(shown):
        embed.set_footer(text=f"ほか {len(rows) - len(shown)} 件")
    else:
        embed.set_footer(text="⏭ スキップ: 同じVCの参加者・管理者 / 🗑 クリア: 管理者・許可ロール")
    return embed


def models_embed(runtime, user_id, guild_id):
    voices = usable_voices(runtime, user_id)
    default = runtime.store.get("system").defaults.voice_id
    settings = runtime.speech_settings(user_id, guild_id)
    embed = discord.Embed(
        title=f"🎙 使用できるモデル ({len(voices)})",
        color=COLOR_INFO,
        description="下のメニューから選ぶと、そのままあなたの読み上げモデルに設定されます。"
        if voices
        else "登録されているモデルがありません。HostのVoice Managerから登録してください。",
    )
    lines = []
    for voice_id, voice in sorted(voices.items(), key=lambda item: item[1].name):
        marks = ("✅ " if voice_id == settings.voice_id else "▫️ ") + (
            "⭐" if voice_id == default else ""
        )
        name = discord.utils.escape_markdown(voice.name)
        personal = " 🔒あなた専用" if voice.allowed_user_ids else ""
        lines.append(f"{marks} **{name}** `{voice_id}`{personal}")
    for start in range(0, len(lines), 15):
        embed.add_field(
            name="​" if start else "モデル",
            value="\n".join(lines[start : start + 15]),
            inline=False,
        )
    embed.set_footer(
        text="✅ 現在あなたが使用中 ・ ⭐ Botの既定モデル ・ 🔒 使用者限定の専用ボイス"
    )
    return embed


def speed_embed(runtime, user_id, guild_id, scope, text=None):
    settings, _ = user_settings(runtime, user_id, guild_id)
    speed = settings.speed_percent
    embed = discord.Embed(
        title="⏩ 読み上げ速度",
        color=COLOR_INFO,
        description=(
            (f"{text}\n\n" if text else "")
            + f"**{speed}%**  `{bar(speed - SPEED_MIN, SPEED_MAX - SPEED_MIN, 15)}`\n"
            f"{SPEED_MIN}% ～ {SPEED_MAX}%（100%が等速）・ボタンの変更先: **{SCOPE_LABEL[scope]}**"
        ),
    )
    add_settings_fields(embed, runtime, user_id, guild_id)
    return embed


# --------------------------------------------------------------------------- views


class OwnedView(discord.ui.View):
    """Only the invoking user can press; components are disabled after timeout."""

    def __init__(self, owner_id, timeout=300):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.origin = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "このボタンはコマンドを実行した人だけが使えます。", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.origin:
            try:
                await self.origin.edit_original_response(view=self)
            except discord.HTTPException:
                pass


class StatusView(OwnedView):
    def __init__(self, bot, owner_id):
        super().__init__(owner_id)
        self.bot = bot

    @discord.ui.button(emoji="🔄", label="更新", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction, button):
        await interaction.response.edit_message(
            embed=status_embed(self.bot, interaction.guild, interaction.user), view=self
        )

    @discord.ui.button(emoji="📋", label="キューを見る", style=discord.ButtonStyle.primary)
    async def show_queue(self, interaction, button):
        view = QueueView(self.bot, self.owner_id)
        view.origin = self.origin
        self.stop()
        await interaction.response.edit_message(
            embed=queue_embed(self.bot, interaction.guild), view=view
        )


class QueueView(OwnedView):
    def __init__(self, bot, owner_id):
        super().__init__(owner_id)
        self.bot = bot

    async def _allowed(self, interaction, allowed, text):
        if allowed:
            return True
        await interaction.response.send_message(embed=notice(text, ok=False), ephemeral=True)
        return False

    async def _redraw(self, interaction):
        await interaction.response.edit_message(
            embed=queue_embed(self.bot, interaction.guild), view=self
        )

    @discord.ui.button(emoji="🔄", label="更新", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction, button):
        await self._redraw(interaction)

    @discord.ui.button(emoji="⏭", label="スキップ", style=discord.ButtonStyle.primary)
    async def skip(self, interaction, button):
        allowed = self.bot.can_manage(interaction) or self.bot.in_bot_voice_channel(interaction)
        if await self._allowed(interaction, allowed, SKIP_DENIED):
            self.bot.skip(str(interaction.guild_id))
            await self._redraw(interaction)

    @discord.ui.button(emoji="🗑", label="クリア", style=discord.ButtonStyle.danger)
    async def clear(self, interaction, button):
        if await self._allowed(interaction, self.bot.can_clear(interaction), CLEAR_DENIED):
            self.bot.runtime.clear(str(interaction.guild_id))
            await self._redraw(interaction)

    @discord.ui.button(emoji="📊", label="ステータス", style=discord.ButtonStyle.secondary)
    async def show_status(self, interaction, button):
        view = StatusView(self.bot, self.owner_id)
        view.origin = self.origin
        self.stop()
        await interaction.response.edit_message(
            embed=status_embed(self.bot, interaction.guild, interaction.user), view=view
        )


def scope_button(view, row):
    other = "guild" if view.scope == "global" else "global"
    button = discord.ui.Button(
        emoji="🏠" if view.scope == "guild" else "🌐",
        label=f"変更先: {SCOPE_LABEL[view.scope]}（押すと{SCOPE_LABEL[other]}に切替）",
        style=discord.ButtonStyle.success,
        row=row,
    )

    async def callback(interaction):
        view.scope = other
        view.build()
        await interaction.response.edit_message(embed=view.render(), view=view)

    button.callback = callback
    return button


class VoicePickerView(OwnedView):
    """Select menu of models, paginated by 25 (Discord's select-menu limit)."""

    def __init__(self, commands, owner_id, guild_id, scope, page=0):
        super().__init__(owner_id)
        self.commands, self.guild_id, self.scope = commands, guild_id, scope
        self.voices = sorted(
            usable_voices(commands.bot.runtime, owner_id).items(), key=lambda item: item[1].name
        )
        self.pages = max(1, -(-len(self.voices) // PAGE_SIZE))
        self.page = min(page, self.pages - 1)
        self.build()

    def build(self):
        self.clear_items()
        current = self.commands.bot.runtime.speech_settings(str(self.owner_id), self.guild_id)
        chunk = self.voices[self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE]
        if not chunk:
            return
        select = discord.ui.Select(
            placeholder=f"モデルを選択（{SCOPE_LABEL[self.scope]}）",
            options=[
                discord.SelectOption(
                    label=voice_label(voice)[:100],
                    value=voice_id,
                    description=voice_id,
                    default=voice_id == current.voice_id,
                    emoji="🎙",
                )
                for voice_id, voice in chunk
            ],
        )
        select.callback = self.on_select
        self.add_item(select)
        self.add_item(scope_button(self, row=2))
        if self.pages > 1:
            previous = discord.ui.Button(emoji="◀", disabled=self.page == 0, row=1)
            label = discord.ui.Button(label=f"{self.page + 1}/{self.pages}", disabled=True, row=1)
            following = discord.ui.Button(emoji="▶", disabled=self.page >= self.pages - 1, row=1)
            previous.callback = lambda interaction: self.turn(interaction, -1)
            following.callback = lambda interaction: self.turn(interaction, 1)
            for item in (previous, label, following):
                self.add_item(item)

    def render(self, text=None):
        return settings_embed(
            self.commands.bot.runtime,
            str(self.owner_id),
            self.guild_id,
            title="🎙 モデルを選択",
            text=text
            or f"メニューから選ぶと **{SCOPE_LABEL[self.scope]}** の設定として保存されます。",
        )

    async def turn(self, interaction, delta):
        self.page = max(0, min(self.pages - 1, self.page + delta))
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_select(self, interaction):
        voice_id = interaction.data["values"][0]
        text = self.commands.apply_setting(
            interaction.user.id, self.guild_id, "voice_id", voice_id, self.scope
        )
        self.build()
        await interaction.response.edit_message(embed=self.render(text), view=self)


class SpeedView(OwnedView):
    def __init__(self, commands, owner_id, guild_id, scope):
        super().__init__(owner_id)
        self.commands, self.guild_id, self.scope = commands, guild_id, scope
        self.build()

    def build(self):
        self.clear_items()
        for label, delta, style in (
            ("−10", -10, discord.ButtonStyle.secondary),
            ("−5", -5, discord.ButtonStyle.secondary),
            ("100%", None, discord.ButtonStyle.primary),
            ("+5", 5, discord.ButtonStyle.secondary),
            ("+10", 10, discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(label=label, style=style, row=0)
            button.callback = self.make_callback(delta)
            self.add_item(button)
        self.add_item(scope_button(self, row=1))

    def render(self, text=None):
        return speed_embed(
            self.commands.bot.runtime, str(self.owner_id), self.guild_id, self.scope, text
        )

    def make_callback(self, delta):
        async def callback(interaction):
            runtime = self.commands.bot.runtime
            current, _ = user_settings(runtime, str(self.owner_id), self.guild_id)
            value = 100 if delta is None else current.speed_percent + delta
            value = max(SPEED_MIN, min(SPEED_MAX, value))
            text = self.commands.apply_setting(
                interaction.user.id, self.guild_id, "speed_percent", value, self.scope
            )
            await interaction.response.edit_message(embed=self.render(text), view=self)

        return callback


class ConfirmView(OwnedView):
    def __init__(self, owner_id, on_confirm, label="初期化する"):
        super().__init__(owner_id, timeout=60)
        self.on_confirm = on_confirm
        self.confirm.label = label

    @discord.ui.button(label="初期化する", emoji="⚠️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self.stop()
        await interaction.response.defer()
        embed = await self.on_confirm(interaction)
        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(
            embed=notice("キャンセルしました。変更はありません。"), view=None
        )


class ChannelRemoveView(OwnedView):
    def __init__(self, commands, owner_id, guild, channel_ids):
        super().__init__(owner_id)
        self.commands = commands
        select = discord.ui.Select(
            placeholder="読み上げを止めるチャンネルを選択",
            min_values=1,
            max_values=min(len(channel_ids), PAGE_SIZE),
            options=[
                discord.SelectOption(label=channel_label(guild, cid), value=cid, emoji="📝")
                for cid in channel_ids[:PAGE_SIZE]
            ],
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction):
        removed = self.commands.remove_channels(
            str(interaction.guild_id), interaction.data["values"]
        )
        self.stop()
        await interaction.response.edit_message(
            embed=notice("読み上げ対象から削除しました: " + " ".join(f"<#{c}>" for c in removed)),
            view=None,
        )
