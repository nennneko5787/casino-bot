"""借金: /loan borrow|repay|status。入金は自動で返済に充当される。

借金残高には1000ごとのウォールがあり、壁到達で次の1000が解放される。
手数料は残債1000までの分が10%、1000超の分が20%。
"""

import discord
from discord import app_commands
from discord.ext import commands

from services import loan
from services.loan import FEE_RATE, OVER_FEE_RATE, WALL_STEP, debt_wall
from services.message import buildAmountText, buildGetAmountText


class LoanCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_group(name="loan", brief="借金をします")
    @commands.guild_only()
    async def loan(self, ctx: commands.Context):
        await ctx.reply(
            "サブコマンドを指定してください: borrow / repay / status",
            ephemeral=True,
        )

    @loan.command(name="borrow", brief="お金を借ります (手数料10%・1000超は20%)")
    @app_commands.rename(amount="借りる額")
    @app_commands.describe(
        amount=f"1以上。借金残高のウォール({WALL_STEP}ごと)まで借りられます"
    )
    @commands.guild_only()
    async def loanBorrowCommand(self, ctx: commands.Context, amount: int):
        try:
            received, debt_add = await loan.borrow(ctx.author.id, amount)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        debt = await loan.get_debt(ctx.author.id)
        wall = debt_wall(debt)
        fee_note = (
            f"(手数料: 残債{WALL_STEP}までの分{int(FEE_RATE * 100)}%"
            f"・超過分{int(OVER_FEE_RATE * 100)}%: 借金+{buildAmountText(debt_add)})\n"
        )
        await ctx.reply(
            embed=discord.Embed(
                title="借金💸",
                description=(
                    f"<@{ctx.author.id}>\n"
                    f"{buildAmountText(received)}を借りました\n"
                    f"{fee_note}"
                    f"現在の借金残高: {buildAmountText(debt)}"
                    f" (ウォール: {buildAmountText(wall)})\n"
                    "※入金があると自動で返済に充当されます"
                ),
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )

    @loan.command(name="repay", brief="借金を返済します")
    @app_commands.rename(amount="返済額")
    @app_commands.describe(amount="返す額")
    @commands.guild_only()
    async def loanRepayCommand(self, ctx: commands.Context, amount: int):
        try:
            paid, remaining = await loan.repay(ctx.author.id, amount)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(
            embed=discord.Embed(
                title="返済✨",
                description=(
                    f"<@{ctx.author.id}>\n"
                    f"```patch\n{buildGetAmountText(paid, md=True)}\n```\n"
                    f"借金残高: {buildAmountText(remaining)}"
                ),
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

    @loan.command(name="status", brief="借金の状態を確認します")
    @commands.guild_only()
    async def loanStatusCommand(self, ctx: commands.Context):
        debt = await loan.get_debt(ctx.author.id)
        rest = await loan.borrowable(ctx.author.id)
        wall = debt_wall(debt)
        await ctx.reply(
            embed=discord.Embed(
                title="借金状況💸",
                description=(
                    f"<@{ctx.author.id}>\n"
                    f"借金残高: {buildAmountText(debt)}\n"
                    f"あと借りられる額: {buildAmountText(rest)}\n"
                    f"(現在のウォール: {buildAmountText(wall)}"
                    f"・手数料{int(FEE_RATE * 100)}%"
                    f"/{WALL_STEP}超は{int(OVER_FEE_RATE * 100)}%)"
                ),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(LoanCog(bot))
