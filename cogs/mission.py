"""ミッション: 行動で通貨を稼ぐ。周期別ページ + 受取ボタン。

一般: /mission (進捗確認・報酬受取)
管理者: /mission-admin channel|unset-channel|status (指定chの設定)
発言・VC滞在はリスナーで自動記録、ゲーム回数は各Cogから記録する。
"""

import asyncio
import io
import logging
from contextlib import suppress
from datetime import datetime

import discord
from discord.ext import commands, tasks

from services import missions
from services.loan import repay_note
from services.message import (
    amountName,
    buildAmountText,
    buildGetAmountText,
)
from services.mission_card import render_mission_page
from services.missions import JST, PERIOD_LABEL, RESET_NOTE

logger = logging.getLogger(__name__)

PERIOD_ORDER = ["hourly", "daily", "weekly", "monthly", "once"]
PERIOD_EMOJI = {
    "hourly": "⏳",
    "daily": "📅",
    "weekly": "📆",
    "monthly": "🗓️",
    "once": "🏆",
}
PERIOD_COLOR = {
    "hourly": discord.Color.light_grey(),
    "daily": discord.Color.green(),
    "weekly": discord.Color.blue(),
    "monthly": discord.Color.purple(),
    "once": discord.Color.gold(),
}

BAR_WIDTH = 10


def progress_bar(progress: int, target: int) -> str:
    filled = min(BAR_WIDTH, int(progress / target * BAR_WIDTH)) if target > 0 else 0
    return "▓" * filled + "░" * (BAR_WIDTH - filled)


def build_mission_embeds(
    status: list[dict], channel_id: int | None
) -> list[discord.Embed]:
    """周期ごとのEmbedを PERIOD_ORDER 順で返す。

    hiddenミッションは達成 (または受取済み) になるまで一覧に出さない。
    """
    channel_text = f"<#{channel_id}>" if channel_id else "未設定"
    embeds: list[discord.Embed] = []
    for period in PERIOD_ORDER:
        entries = [
            e
            for e in status
            if e["period"] == period
            and not (e.get("hidden") and not e["completed"] and not e["claimed"])
        ]
        description = (
            f"{RESET_NOTE[period]}\n"
            "期限内に受け取らないと報酬は失効します (恒常は除く)\n"
            f"指定ch: {channel_text}"
        )
        if period == "once":
            description += "\n❓隠しミッションもあるかも…？"
        embed = discord.Embed(
            title=f"{PERIOD_EMOJI[period]} {PERIOD_LABEL[period]}ミッション",
            description=description,
            color=PERIOD_COLOR[period],
        )
        for e in entries:
            if not e["available"]:
                mark, tail = "🔒", "\n指定ch未設定のため進行しません"
            elif e["claimed"]:
                mark, tail = "✅", "\n受取済み"
            elif e["completed"]:
                mark, tail = "🎁", "\n達成！下のボタンで受け取れます"
            else:
                mark, tail = "▶", ""
            embed.add_field(
                name=f"{mark} `{e['id']}` {e['title']} (+{buildAmountText(e['reward'])})",
                value=(
                    f"{progress_bar(e['progress'], e['target'])} "
                    f"{e['progress']}/{e['target']}\n{e['desc']}{tail}"
                ),
                inline=False,
            )
        embed.set_footer(text="◀ ▶・メニューで周期切替 / 🎁はボタンで受取")
        embeds.append(embed)
    return embeds


def _page_entries(status: list[dict], period: str) -> list[dict]:
    return [
        e
        for e in status
        if e["period"] == period
        and not (e.get("hidden") and not e["completed"] and not e["claimed"])
    ]


def _render_page_image(
    status: list[dict], channel_id: int | None, page: int
) -> io.BytesIO:
    period = PERIOD_ORDER[page]
    channel_text = f"<#{channel_id}>" if channel_id else "未設定"
    return render_mission_page(
        period=period,
        period_label=f"{PERIOD_LABEL[period]}",
        reset_note=RESET_NOTE[period],
        entries=_page_entries(status, period),
        channel_text=channel_text,
        page_idx=page,
        total_pages=len(PERIOD_ORDER),
        amount_name=amountName,
    )


async def _make_page_file(
    status: list[dict], channel_id: int | None, page: int
) -> discord.File:
    buf = await asyncio.to_thread(_render_page_image, status, channel_id, page)
    return discord.File(buf, filename=f"mission_{PERIOD_ORDER[page]}.png")


class MissionClaimButton(discord.ui.Button):
    def __init__(self, mission_id: str, title: str):
        super().__init__(
            label=f"受取 {mission_id}",
            style=discord.ButtonStyle.success,
            emoji="🎁",
            row=1,
        )
        self.mission_id = mission_id
        self.title = title

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, MissionView)
        await view.claim_mission(interaction, self.mission_id)


class MissionPeriodSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=f"{PERIOD_EMOJI[p]} {PERIOD_LABEL[p]}",
                value=str(i),
            )
            for i, p in enumerate(PERIOD_ORDER)
        ]
        super().__init__(placeholder="周期を選択...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, MissionView)
        if not await view.check_user(interaction):
            return
        await interaction.response.defer()
        if self.values:
            view.page = int(self.values[0])
        await view.render(interaction)


class MissionView(discord.ui.View):
    """周期ページ送り + 受取ボタン。操作はコマンド実行者のみ。画像付き。"""

    def __init__(
        self, author_id: int, status: list[dict], channel_id: int | None = None
    ):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.status = status
        self.channel_id = channel_id
        self.page = 1  # デイリーを初期表示
        self.period_select = MissionPeriodSelect()
        self.add_item(self.period_select)
        self._sync()

    def _sync(self):
        for item in list(self.children):
            if isinstance(item, MissionClaimButton):
                self.remove_item(item)
        period = PERIOD_ORDER[self.page]
        for e in self.status:
            if (
                e["period"] == period
                and e["available"]
                and e["completed"]
                and not e["claimed"]
            ):
                with suppress(ValueError, discord.DiscordException):
                    self.add_item(MissionClaimButton(e["id"], e["title"]))
        self.period_select.placeholder = (
            f"{PERIOD_EMOJI[period]} {PERIOD_LABEL[period]}ミッション"
        )
        for i, option in enumerate(self.period_select.options):
            option.default = i == self.page

    async def check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "コマンド実行者のみ操作できます", ephemeral=True
            )
            return False
        return True

    async def render(self, interaction: discord.Interaction):
        self._sync()
        try:
            file = await _make_page_file(self.status, self.channel_id, self.page)
        except Exception:
            logger.exception("ミッション画像の描画に失敗")
            # フォールバック: Embed表示
            embeds = build_mission_embeds(self.status, self.channel_id)
            message = interaction.message
            if message is not None:
                await message.edit(embed=embeds[self.page], view=self)
            else:
                await interaction.response.edit_message(
                    embed=embeds[self.page], view=self
                )
            return
        message = interaction.message
        if message is not None:
            await message.edit(attachments=[file], view=self)
        else:
            await interaction.response.edit_message(attachments=[file], view=self)

    async def refresh(self, interaction: discord.Interaction):
        try:
            self.status = await missions.get_status(self.author_id)
            self.channel_id = await missions.get_mission_channel()
        except Exception:
            logger.exception("ミッションの再読込に失敗")
            await interaction.followup.send(
                "ミッションの再読込に失敗しました", ephemeral=True
            )
            return
        await self.render(interaction)

    async def claim_mission(self, interaction: discord.Interaction, mission_id: str):
        if not await self.check_user(interaction):
            return
        await interaction.response.defer()
        try:
            m, reward, repaid = await missions.claim(interaction.user.id, mission_id)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception:
            logger.exception("ミッション受取に失敗")
            await interaction.followup.send(
                "受取中にエラーが発生しました", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"🎁 `{m.id}` {m.title}\n"
            f"```patch\n{buildGetAmountText(reward, md=True)}\n```"
            + repay_note(repaid),
            ephemeral=True,
        )
        await self.refresh(interaction)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary, row=2)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_user(interaction):
            return
        await interaction.response.defer()
        self.page = (self.page - 1) % len(PERIOD_ORDER)
        await self.render(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary, row=2)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_user(interaction):
            return
        await interaction.response.defer()
        self.page = (self.page + 1) % len(PERIOD_ORDER)
        await self.render(interaction)

    @discord.ui.button(label="更新", style=discord.ButtonStyle.secondary, row=2)
    async def reload(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_user(interaction):
            return
        await interaction.response.defer()
        await self.refresh(interaction)

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True


class MissionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await missions.ensure_schema()
        self.vc_flush.start()

    async def cog_unload(self):
        self.vc_flush.cancel()

    @tasks.loop(minutes=5.0)
    async def vc_flush(self):
        try:
            await missions.flush_vc_sessions()
        except Exception:
            logger.exception("VC滞在の定期精算に失敗")

    @vc_flush.before_loop
    async def _before_flush(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener("on_message")
    async def onMissionMessage(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        with suppress(Exception):
            await missions.record_event(
                message.author.id, "message", 1, channel_id=message.channel.id
            )
        # 隠しミッション: 「すまんこ」と一言送る (前後の空白は無視)
        if message.content.strip() == "すまんこ":
            with suppress(Exception):
                await missions.record_event(message.author.id, "sumanko", 1)

    @commands.Cog.listener("on_voice_state_update")
    async def onMissionVoice(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return
        before_id = before.channel.id if before.channel else None
        after_id = after.channel.id if after.channel else None
        if before_id == after_id:
            return
        now = datetime.now(JST)
        with suppress(Exception):
            if before_id is not None:
                await missions.vc_leave(member.id, now)
            if after_id is not None:
                await missions.vc_join(member.id, now)

    @commands.hybrid_command("mission", brief="ミッションを確認・報酬を受け取ります")
    @commands.guild_only()
    async def missionCommand(self, ctx: commands.Context):
        status = await missions.get_status(ctx.author.id)
        channel_id = await missions.get_mission_channel()
        view = MissionView(ctx.author.id, status, channel_id)
        try:
            file = await _make_page_file(status, channel_id, view.page)
        except Exception:
            logger.exception("ミッション画像の描画に失敗")
            embeds = build_mission_embeds(status, channel_id)
            await ctx.reply(embed=embeds[view.page], view=view)
            return
        await ctx.reply(file=file, view=view)

    @commands.hybrid_group(
        name="mission-admin", brief="※管理者専用 ミッション設定をします"
    )
    @commands.has_guild_permissions(administrator=True)
    @commands.guild_only()
    async def missionAdmin(self, ctx: commands.Context):
        await ctx.reply(
            "サブコマンドを指定してください: channel / unset-channel / status",
            ephemeral=True,
        )

    @missionAdmin.command(name="channel", brief="※管理者専用 指定chを設定します")
    @commands.has_guild_permissions(administrator=True)
    @commands.guild_only()
    async def missionChannelCommand(
        self, ctx: commands.Context, ch: discord.TextChannel
    ):
        await missions.set_mission_channel(ch.id)
        await ctx.reply(f"指定チャンネルを <#{ch.id}> に設定しました")

    @missionAdmin.command(name="unset-channel", brief="※管理者専用 指定chを解除します")
    @commands.has_guild_permissions(administrator=True)
    @commands.guild_only()
    async def missionUnsetChannelCommand(self, ctx: commands.Context):
        await missions.set_mission_channel(None)
        await ctx.reply("指定チャンネルを解除しました")

    @missionAdmin.command(name="status", brief="※管理者専用 設定を確認します")
    @commands.has_guild_permissions(administrator=True)
    @commands.guild_only()
    async def missionStatusCommand(self, ctx: commands.Context):
        channel_id = await missions.get_mission_channel()
        text = f"<#{channel_id}>" if channel_id else "未設定"
        await ctx.reply(
            f"指定チャンネル: {text}\nミッション数: {len(missions.MISSIONS)}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MissionCog(bot))
