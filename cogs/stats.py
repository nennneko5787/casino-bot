import discord
import dotenv
from discord.ext import commands

from services.message import buildAmountText
from services.money import getUser

dotenv.load_dotenv()


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command("stats", brief="あなたの状態を確認します")
    async def slotCommand(self, ctx: commands.Context):
        userData = await getUser(ctx.author)
        await ctx.reply(
            embed=discord.Embed(
                description=f"所持金: `{buildAmountText(userData.amount)}`"
            ).set_author(
                name=ctx.author.display_name, icon_url=ctx.author.display_avatar
            )
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
