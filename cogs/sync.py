"""同期: スラッシュコマンドの手動同期 (プレフィックス専用)。

起動時の自動同期はしない (レート制限・起動高速化のため)。
更新時は管理者が c#sync を実行して同期する。
"""

import discord
from discord.ext import commands

from services.admin import admin_only


class SyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command("sync", brief="※管理者専用 スラッシュコマンドを同期します")
    @admin_only()
    @commands.guild_only()
    async def syncCommand(self, ctx: commands.Context):
        synced = await self.bot.tree.sync()
        await ctx.reply(
            embed=discord.Embed(
                title="🔄 同期完了",
                description=f"{len(synced)}件のスラッシュコマンドを同期しました",
                color=discord.Color.green(),
            )
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SyncCog(bot))
