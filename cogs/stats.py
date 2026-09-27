import logging

import discord
import dotenv
from discord import app_commands
from discord.ext import commands

from services.levels import get_info as get_level_info
from services.loan import get_debt
from services.message import buildAmountText
from services.money import getUser
from services.stocks import get_portfolio

dotenv.load_dotenv()

logger = logging.getLogger(__name__)


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command("stats", brief="残高を確認します")
    @app_commands.rename(member="対象")
    @app_commands.describe(member="省略時は自分。指定すると他メンバーの残高を表示")
    async def statsCommand(
        self, ctx: commands.Context, member: discord.Member | None = None
    ):
        target = member or ctx.author
        userData = await getUser(target)
        debt = await get_debt(target.id)
        portfolio = await get_portfolio(target.id)
        stock_value = sum(item["market"] for item in portfolio)
        lines = [
            f"所持金: `{buildAmountText(userData.amount)}`",
            f"株評価額: `{buildAmountText(stock_value)}`",
        ]
        if debt > 0:
            lines.append(f"借金: `{buildAmountText(debt)}`")
        lines.append(
            f"総資産: `{buildAmountText(userData.amount + stock_value - debt)}`"
        )
        try:
            lv = await get_level_info(target.id)
            lines.append(f"レベル: `Lv.{lv['level']} ({lv['xp']}XP)`")
        except Exception:
            logger.exception("レベル情報の取得に失敗")
        await ctx.reply(
            embed=discord.Embed(description="\n".join(lines)).set_author(
                name=target.display_name, icon_url=target.display_avatar
            )
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
