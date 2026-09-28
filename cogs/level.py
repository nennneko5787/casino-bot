"""レベリング: チャット・VC滞在でXPを稼ぎ、レベルアップで通貨報酬。

一般: /level [対象] (レベルカード表示。番付は /ranking level)
管理者: /level-admin add|reset (XP付与・初期化)
チャット1通ごとに変動XP (15〜25+長文ボーナス、60秒CD、短文・連投半減)、
VC1分ごとに変動XP (8〜12)。レベル式は 5*Lv^2+50*Lv+100 の二次カーブ。
"""

import asyncio
import io
import logging
import os
from contextlib import suppress
from datetime import datetime

import discord
import dotenv
from discord import app_commands
from discord.ext import commands, tasks

from services import levels, missions
from services.admin import admin_only
from services.level_card import (
    render_level_card,
    render_levelup_card,
)
from services.levels import JST, REWARD_PER_LEVEL
from services.loan import apply_income, repay_note
from services.message import buildAmountText, buildGetAmountText

dotenv.load_dotenv()

logger = logging.getLogger(__name__)

BAR_WIDTH = 12

def _log_channel_id() -> int | None:
    """envのlog_channelをint化。未設定・不正値はNone。"""
    raw = os.environ.get("log_channel", "").strip()
    return int(raw) if raw.isdigit() else None


async def _avatar_bytes(member: discord.Member | discord.User) -> bytes | None:
    try:
        avatar = getattr(member, "display_avatar", None)
        if isinstance(avatar, discord.Asset):
            return await avatar.read()
    except Exception:
        logger.exception("アバター取得に失敗")
    return None


async def _send_levelup(
    sendable,
    member: discord.Member | discord.User,
    old: int,
    new: int,
    gained: int,
    reward: int,
    repaid: int = 0,
) -> None:
    """レベルアップ通知を画像で送る。失敗時はEmbedにフォールバック。"""
    if sendable is None:
        logger.warning("レベルアップ通知先チャンネルが取得できませんでした")
        return
    mention = getattr(member, "mention", None) or str(member)
    name = getattr(member, "display_name", str(member))
    try:
        av = await _avatar_bytes(member)
        buf: io.BytesIO = await asyncio.to_thread(
            render_levelup_card, name, old, new, gained, reward, av, repaid
        )
        await sendable.send(
            content=f"{mention} レベルアップ！",
            file=discord.File(buf, filename="levelup.png"),
        )
    except Exception:
        logger.exception("レベルアップ画像の送信に失敗")
        with suppress(Exception):
            await sendable.send(
                embed=build_levelup_embed(mention, old, new, gained, reward, repaid)
            )


def progress_bar(cur: int, need: int) -> str:
    filled = min(BAR_WIDTH, int(cur / need * BAR_WIDTH)) if need > 0 else 0
    return "▓" * filled + "░" * (BAR_WIDTH - filled)


def build_level_embed(
    member: discord.Member | discord.User, info: dict
) -> discord.Embed:
    name = getattr(member, "display_name", str(member))
    avatar = getattr(member, "display_avatar", None)
    embed = discord.Embed(
        title=f"Lv.{info['level']} {name}",
        description=(
            f"{progress_bar(info['current'], info['need'])} "
            f"{info['current']}/{info['need']} XP\n"
            f"総XP: `{info['xp']}` ／ 次まであと `{info['remaining']}`"
        ),
        color=discord.Color.blurple(),
    )
    rank_text = f"#{info['rank']}" if info["rank"] else "圏外"
    embed.add_field(
        name="ランク・活動",
        value=(
            f"順位: `{rank_text}`\n"
            f"発言数: `{info['messages']}`\n"
            f"VC滞在: `{info['vc_minutes']}分`"
        ),
        inline=False,
    )
    if isinstance(avatar, discord.Asset):
        embed.set_thumbnail(url=avatar.url)
    embed.set_footer(text="チャット・VCでXPを稼ごう！ /mission でもXP系報酬あり")
    return embed


def build_levelup_embed(
    mention: str, old: int, new: int, gained: int, reward: int, repaid: int
) -> discord.Embed:
    up = new - old
    need_next = levels.xp_for_next(new)
    embed = discord.Embed(
        title="🎉 レベルアップ！",
        description=(
            f"{mention} が **Lv.{old} → Lv.{new}** に上がった！ (+{up}Lv)\n"
            f"今回の獲得XP: `+{gained}XP`\n"
            f"次のレベルまで: `{need_next}XP`"
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="レベルアップ報酬",
        value=f"```patch\n{buildGetAmountText(reward, md=True)}\n```"
        + repay_note(repaid),
        inline=False,
    )
    return embed


class LevelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await levels.ensure_schema()
        await missions.ensure_schema()
        self.vc_flush.start()

    async def cog_unload(self):
        self.vc_flush.cancel()

    # ---------- 内部ヘルパー ----------

    async def _grant_reward_and_missions(
        self, user_id: int, gained: int, old: int, new: int
    ) -> tuple[int, int]:
        """ミッション記録 + レベルアップ報酬付与。(報酬額, 自動返済額) を返す。"""
        if gained > 0:
            with suppress(Exception):
                await missions.record_event(user_id, "xp", gained)
        if new > old:
            with suppress(Exception):
                await missions.record_event(user_id, "level", new - old)
        reward = 0
        repaid = 0
        if new > old:
            reward = sum(levels.level_reward(lv) for lv in range(old + 1, new + 1))
            try:
                repaid, _ = await apply_income(user_id, reward)
            except Exception:
                logger.exception("レベルアップ報酬の付与に失敗 user=%s", user_id)
                reward, repaid = 0, 0
        return reward, repaid

    async def _notify_channel(
        self, guild: discord.Guild | None
    ) -> discord.abc.Messageable | None:
        """レベルアップ通知先。キャッシュになければfetchする。"""
        if guild is None:
            return None
        cid = _log_channel_id()
        if cid is None:
            return None
        ch = guild.get_channel(cid)
        if ch is None:
            with suppress(discord.DiscordException):
                ch = await guild.fetch_channel(cid)
        if not isinstance(ch, discord.abc.Messageable):
            return None
        return ch

    # ---------- リスナー ----------

    @commands.Cog.listener("on_message")
    async def onLevelMessage(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        try:
            gained, old, new, _ = await levels.add_chat_xp(
                message.author.id, message.content
            )
        except Exception:
            logger.exception("チャットXPの付与に失敗")
            return
        if gained <= 0:
            return
        try:
            reward, repaid = await self._grant_reward_and_missions(
                message.author.id, gained, old, new
            )
        except Exception:
            logger.exception("レベル報酬・ミッション記録に失敗")
            return
        if new > old:
            with suppress(Exception):
                ch = await self._notify_channel(message.guild)
                await _send_levelup(
                    ch, message.author, old, new, gained, reward, repaid
                )

    @commands.Cog.listener("on_voice_state_update")
    async def onLevelVoice(
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
        try:
            if before_id is not None:
                _, gained, old, new = await levels.vc_leave(member.id, now)
            else:
                _, gained, old, new = 0, 0, 0, 0
            if after_id is not None:
                await levels.vc_join(member.id, now)
            if before_id is None:
                return
        except Exception:
            logger.exception("VC-XPの出入り記録に失敗")
            return
        if gained <= 0:
            return
        try:
            reward, repaid = await self._grant_reward_and_missions(
                member.id, gained, old, new
            )
            await self._grant_reward_and_missions(member.id, gained, old, new)
        except Exception:
            logger.exception("VC-XPの報酬記録に失敗")
            return
        if new > old and member.guild is not None:
            with suppress(Exception):
                ch = await self._notify_channel(member.guild)
                await _send_levelup(ch, member, old, new, gained, reward, repaid)

    @tasks.loop(minutes=5.0)
    async def vc_flush(self):
        try:
            results = await levels.flush_vc_sessions()
        except Exception:
            logger.exception("VC-XPの定期精算に失敗")
            return
        for user_id, minutes, gained, old, new in results:
            try:
                reward, repaid = await self._grant_reward_and_missions(
                    user_id, gained, old, new
                )
                await self._grant_reward_and_missions(user_id, gained, old, new)
            except Exception:
                logger.exception("VC-XPの報酬記録に失敗 user=%s", user_id)
                continue
            if new > old:
                with suppress(Exception):
                    for guild in self.bot.guilds:
                        member = guild.get_member(user_id)
                        if member is None:
                            continue
                        ch = await self._notify_channel(guild)
                        if ch is None:
                            continue
                        await _send_levelup(
                            ch, member, old, new, gained, reward, repaid
                        )
                        break

    @vc_flush.before_loop
    async def _before_flush(self):
        await self.bot.wait_until_ready()

    # ---------- コマンド ----------

    @commands.hybrid_command("level", brief="レベル・XPを確認します")
    @app_commands.rename(member="対象")
    @app_commands.describe(member="省略時は自分。指定すると他メンバーのレベルを表示")
    @commands.guild_only()
    async def levelCommand(
        self, ctx: commands.Context, member: discord.Member | None = None
    ):
        target = member or ctx.author
        info = await levels.get_info(target.id)
        name = getattr(target, "display_name", str(target))
        try:
            av = await _avatar_bytes(target)
            buf = await asyncio.to_thread(
                render_level_card, name, info, av, reward_per_level=REWARD_PER_LEVEL
            )
            await ctx.reply(file=discord.File(buf, filename="level.png"))
        except Exception:
            logger.exception("レベル画像の描画に失敗")
            await ctx.reply(embed=build_level_embed(target, info))

    @commands.hybrid_group(
        name="level-admin", brief="※管理者専用 レベル情報を操作します"
    )
    @admin_only()
    @commands.guild_only()
    async def levelAdmin(self, ctx: commands.Context):
        await ctx.reply(
            "サブコマンドを指定してください: add / reset",
            ephemeral=True,
        )

    @levelAdmin.command(name="add", brief="※管理者専用 XPを付与します")
    @admin_only()
    @commands.guild_only()
    @app_commands.rename(member="対象", amount="xp量")
    @app_commands.describe(amount="付与するXP量 (1以上)", member="付与対象")
    async def levelAddCommand(
        self, ctx: commands.Context, member: discord.Member, amount: int
    ):
        if amount < 1:
            await ctx.reply("XP量は1以上にしてください", ephemeral=True)
            return
        gained, old, new = await levels.add_xp(member.id, amount)
        reward, repaid = await self._grant_reward_and_missions(
            member.id, gained, old, new
        )
        msg = f"{member.mention} に `{gained}XP` を付与しました (Lv.{old} → Lv.{new})"
        if new > old:
            msg += f"\n報酬 `{buildAmountText(reward)}`" + repay_note(repaid)
        await ctx.reply(msg)

    @levelAdmin.command(name="reset", brief="※管理者専用 レベル情報を初期化します")
    @admin_only()
    @commands.guild_only()
    @app_commands.rename(member="対象")
    async def levelResetCommand(self, ctx: commands.Context, member: discord.Member):
        await levels.reset_user(member.id)
        await ctx.reply(f"{member.mention} のレベル情報を初期化しました")


async def setup(bot: commands.Bot):
    await bot.add_cog(LevelCog(bot))
