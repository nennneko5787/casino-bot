import discord
import dotenv
from discord import app_commands
from discord.ext import commands

from objects.exceptions import AmountNotEnough, YouMustDie
from services.message import buildGetAmountText
from services.money import getUser, saveUser

dotenv.load_dotenv()


class PaymentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        "give", brief="※管理者専用 無から所持金を生成して他のメンバーに付与します"
    )
    @commands.has_guild_permissions(administrator=True)
    @commands.guild_only()
    @app_commands.rename(amount="あげる額", to="対象")
    @app_commands.describe(amount="この額をあげます", to="ここで指定した人にあげます")
    async def giveCommand(self, ctx: commands.Context, amount: int, to: discord.Member):
        userData = await getUser(to)

        userData.amount += amount
        await saveUser(userData)

        await ctx.reply("送金しました", ephemeral=True)

        await ctx.channel.send(
            f"{ctx.author.mention} から\n{to.mention} へ\n```patch\n{buildGetAmountText(amount, md=True)}\n```"
        )

    @commands.hybrid_command("send", brief="他のメンバーに所持金を譲渡します")
    @commands.guild_only()
    @app_commands.rename(amount="あげる額", to="対象")
    @app_commands.describe(amount="この額をあげます", to="ここで指定した人にあげます")
    async def sendCommand(self, ctx: commands.Context, amount: int, to: discord.Member):
        userData = await getUser(to)
        toData = await getUser(to)

        if amount < 0:
            raise YouMustDie()
        if userData.amount < amount:
            raise AmountNotEnough()

        userData.amount -= amount
        toData.amount += amount

        await saveUser(userData)
        await saveUser(toData)

        await ctx.reply("送金しました", ephemeral=True)

        await ctx.channel.send(
            f"{ctx.author.mention} から\n```patch\n{buildGetAmountText(-amount, md=True)}\n\n{to.mention} へ\n```patch\n{buildGetAmountText(amount, md=True)}\n```"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PaymentCog(bot))
