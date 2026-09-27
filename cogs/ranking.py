"""番付: 総資産 (残高 + 株評価額 − 借金) の上位・下位を表示。"""

import discord
from discord.ext import commands

from services.loan import RANKING_LIMIT, net_worth_ranking
from services.message import buildAmountText

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

    @commands.hybrid_command("ranking", brief="長者番付を表示します")
    @commands.guild_only()
    async def rankingCommand(self, ctx: commands.Context):
        await ctx.reply(embed=await self._build(ctx, poor=False), ephemeral=True)

    @commands.hybrid_command("poor-ranking", brief="逆長者番付を表示します")
    @commands.guild_only()
    async def poorRankingCommand(self, ctx: commands.Context):
        await ctx.reply(embed=await self._build(ctx, poor=True), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RankingCog(bot))
