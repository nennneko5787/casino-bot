import traceback

import dotenv
from discord.ext import commands

from objects.exceptions import CasinoBaseException

dotenv.load_dotenv()


class ErrorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener("on_command_error")
    async def onCommandError(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.HybridCommandError):
            error = error.original  # ty: ignore[invalid-assignment]

        if isinstance(error, commands.CommandInvokeError):
            error = error.original  # ty: ignore[invalid-assignment]

        if isinstance(error, commands.CommandNotFound):
            return
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply("必要な引数が足りません。", ephemeral=True)
        elif isinstance(error, commands.MissingPermissions):
            await ctx.reply("このコマンドを実行する権限がありません。", ephemeral=True)
        elif isinstance(error, CasinoBaseException):
            await ctx.reply(f"{error.message} コード: `{error.code}`", ephemeral=True)
        else:
            traceback.print_exception(error)
            await ctx.reply("コマンドの実行中にエラーが発生しました。", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ErrorCog(bot))
