"""番付: /ranking net|poor|level のサブコマンド群。

net: 総資産 (残高 + 株評価額 − 借金) の上位。
poor: 総資産の下位。
level: XP上位のレベル番付。
サブコマンドなしの /ranking は net と同じ。
"""

import asyncio
import logging

import discord
from discord.ext import commands

from services import levels
from services.level_card import render_level_ranking
from services.levels import RANKING_LIMIT as LEVEL_RANKING_LIMIT
from services.loan import RANKING_LIMIT, net_worth_ranking
from services.message import buildAmountText

logger = logging.getLogger(__name__)

MEDALS = ["🥇", "🥈", "🥉"]


class RankingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _display_name(self, guild: discord.Guild | None, user_id: int) -> str:
        if guild:
            member = guild.get_member(user_id)
            if member:
                return member.display_name
        try:
            user = await self.bot.fetch_user(user_id)
            return user.display_name
        except discord.DiscordException:
            return f"<@{user_id}>"

    async def _build(self, ctx: commands.Context, *, poor: bool) -> discord.Embed:
        rows = await net_worth_ranking(RANKING_LIMIT, poor=poor)
        lines = []
        for i, (user_id, net) in enumerate(rows):
            name = await self._display_name(ctx.guild, user_id)
            if poor:
                rank = f"ワースト{i + 1}"
            else:
                rank = MEDALS[i] if i < len(MEDALS) else f"{i + 1}位"
            lines.append(f"{rank} {name}: {buildAmountText(net)}")
        desc = "\n".join(lines) if lines else "対象者がいません"
        title = "逆長者番付📉" if poor else "長者番付👑"
        return discord.Embed(
            title=f"{title} (総資産TOP{RANKING_LIMIT})",
            description=desc,
            color=discord.Color.gold() if not poor else discord.Color.dark_grey(),
        ).set_footer(text="総資産 = 残高 + 株評価額 − 借金")

    @commands.hybrid_group(
        name="ranking", brief="各種番付を表示します", invoke_without_command=True
    )
    @commands.guild_only()
    async def ranking(self, ctx: commands.Context):
        await ctx.reply(embed=await self._build(ctx, poor=False))

    @ranking.command(name="net", brief="長者番付を表示します")
    @commands.guild_only()
    async def rankingNetCommand(self, ctx: commands.Context):
        await ctx.reply(embed=await self._build(ctx, poor=False))

    @ranking.command(name="poor", brief="逆長者番付を表示します")
    @commands.guild_only()
    async def rankingPoorCommand(self, ctx: commands.Context):
        await ctx.reply(embed=await self._build(ctx, poor=True))

    @ranking.command(name="level", brief="レベルランキングを表示します")
    @commands.guild_only()
    async def rankingLevelCommand(self, ctx: commands.Context):
        rows = await levels.get_ranking(LEVEL_RANKING_LIMIT)
        cards: list[dict] = []
        for r in rows:
            if ctx.guild:
                m = ctx.guild.get_member(r["user_id"])
                if m is not None:
                    name = m.display_name
                else:
                    try:
                        u = await self.bot.fetch_user(r["user_id"])
                        name = u.display_name
                    except discord.DiscordException:
                        name = f"<@{r['user_id']}>"
            else:
                name = f"<@{r['user_id']}>"
            cards.append({"name": name, "level": r["level"], "xp": r["xp"]})
        try:
            buf = await asyncio.to_thread(
                render_level_ranking, cards, limit=LEVEL_RANKING_LIMIT
            )
            await ctx.reply(file=discord.File(buf, filename="level_ranking.png"))
        except Exception:
            logger.exception("ランキング画像の描画に失敗")
            lines = []
            for i, r in enumerate(rows):
                rank = MEDALS[i] if i < len(MEDALS) else f"{i + 1}位"
                lines.append(
                    f"{rank} {cards[i]['name']}: Lv.{r['level']} (`{r['xp']}XP`)"
                )
            desc = "\n".join(lines) if lines else "対象者がいません"
            embed = discord.Embed(
                title=f"レベルランキング🏆 (TOP{LEVEL_RANKING_LIMIT})",
                description=desc,
                color=discord.Color.gold(),
            )
            embed.set_footer(
                text=f"Lv式: 5*Lv^2+50*Lv+100 / チャット{levels.CHAT_MIN_XP}〜"
                f"{levels.CHAT_MAX_XP}XP・VC1分{levels.VC_MIN_XP_PER_MIN}〜"
                f"{levels.VC_MAX_XP_PER_MIN}XP"
            )
            await ctx.reply(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(RankingCog(bot))
