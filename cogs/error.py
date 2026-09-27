"""全コマンドのエラーハンドリング。

プレフィックス実行のエラーは on_command_error に、
スラッシュ実行 (hybrid含む) のエラーは CommandTree.on_error に流れる。
両方に同じ分岐を登録しないと、スラッシュ側の CasinoBaseException が
汎用メッセージでしか表示されない。
"""

import traceback
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

import discord
import dotenv
from discord import app_commands
from discord.ext import commands

from objects.exceptions import CasinoBaseException

dotenv.load_dotenv()


def _unwrap(error: BaseException) -> BaseException:
    if isinstance(error, commands.HybridCommandError):
        error = error.original  # ty: ignore[invalid-assignment]
    if isinstance(
        error, (commands.CommandInvokeError, app_commands.CommandInvokeError)
    ):
        error = error.original  # ty: ignore[invalid-assignment]
    return error


async def _reply_text(
    send: Callable[..., Awaitable[Any]], text: str, *, ephemeral: bool = True
) -> None:
    with suppress(discord.DiscordException):
        await send(text, ephemeral=ephemeral)


class ErrorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._prev_tree_error = None

    async def cog_load(self):
        # スラッシュ実行時のエラーは tree 側に流れるため専用ハンドラを登録
        self._prev_tree_error = self.bot.tree.on_error
        self.bot.tree.on_error = self.onTreeError

    async def cog_unload(self):
        if self.bot.tree.on_error is self.onTreeError:  # ty: ignore[unresolved-attribute]
            self.bot.tree.on_error = self._prev_tree_error

    @commands.Cog.listener("on_command_error")
    async def onCommandError(self, ctx: commands.Context, error: commands.CommandError):
        error = _unwrap(error)

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

    async def onTreeError(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        unwrapped = _unwrap(error)

        if isinstance(
            unwrapped,
            (
                commands.MissingPermissions,
                app_commands.MissingPermissions,
                app_commands.CheckFailure,
            ),
        ):
            text = "このコマンドを実行する権限がありません。"
        elif isinstance(
            unwrapped,
            (commands.MissingRequiredArgument, app_commands.TransformerError),
        ):
            text = "必要な引数が足りません。"
        elif isinstance(unwrapped, CasinoBaseException):
            text = f"{unwrapped.message} コード: `{unwrapped.code}`"
        else:
            traceback.print_exception(unwrapped)
            text = "コマンドの実行中にエラーが発生しました。"

        if interaction.response.is_done():
            await _reply_text(interaction.followup.send, text)
        else:
            await _reply_text(interaction.response.send_message, text)


async def setup(bot: commands.Bot):
    await bot.add_cog(ErrorCog(bot))
