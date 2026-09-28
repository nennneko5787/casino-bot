"""AIチャット: OpenRouter経由のロールプレイチャット。

料金は市場価値(通貨指数)連動の固定式で、不足時はエラー。
メンションで呼び出し (リプライのみでは反応しない)、ユーザー毎に履歴保持・削除可。
system指示は管理者既定 + ユーザー別persona。
メンション反応は envのai_channel で指定されたチャンネル (とそのスレッド) のみ。
未設定時は全チャンネルで反応する。スラッシュコマンドは制限対象外。
"""

from __future__ import annotations

import logging
import os
from contextlib import suppress

import discord
from discord import app_commands
from discord.ext import commands

from objects.exceptions import AmountNotEnough
from services import ai_chat as ai
from services.admin import admin_only
from services.cooldown import check_message_rate
from services.message import buildAmountText

logger = logging.getLogger(__name__)


def _ai_channel_id() -> int | None:
    """envのai_channelをint化。未設定・不正値はNone (全chで反応)。"""
    raw = os.environ.get("ai_channel", "").strip()
    return int(raw) if raw.isdigit() else None


def _in_ai_channel(message: discord.Message) -> bool:
    """AIチャット対象チャンネルか。スレッド内は親チャンネルで判定する。"""
    allowed = _ai_channel_id()
    if allowed is None:
        return True
    channel = message.channel
    if channel.id == allowed:
        return True
    parent = getattr(channel, "parent", None)
    return parent is not None and parent.id == allowed


def _chunks(text: str, limit: int = 1900) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or ["(空の応答)"]


class AiCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        with suppress(Exception):
            await ai.ensure_tables()

    async def _run(self, user_id: int, text: str) -> tuple[str, bool]:
        price, _ = await ai.current_price()
        try:
            await ai.charge(user_id, price)
        except LookupError:
            raise AmountNotEnough()
        try:
            return await ai.ask(user_id, text)
        except ValueError:
            with suppress(Exception):
                await ai.refund(user_id, price)
            raise

    async def _reply_long(self, target, text: str):
        parts = _chunks(text)
        first = await target.reply(parts[0])
        for part in parts[1:]:
            first = await first.reply(part)
        return first

    @commands.hybrid_group(name="ai", brief="AIチャットをします (有料)")
    @commands.guild_only()
    async def ai(self, ctx: commands.Context):
        await ctx.reply(
            "サブコマンドを指定してください: chat / price / clear / persona",
            ephemeral=True,
        )

    @ai.command(name="chat", brief="AIとチャットします (有料)")
    @app_commands.rename(text="メッセージ")
    @app_commands.describe(text="AIへのメッセージ")
    @commands.guild_only()
    async def aiChatCommand(self, ctx: commands.Context, *, text: str):
        if not text.strip():
            await ctx.reply("メッセージを入力してください", ephemeral=True)
            return
        price, index = await ai.current_price()
        try:
            reply, fallback = await self._run(ctx.author.id, text)
        except AmountNotEnough:
            raise
        except ValueError as e:
            await ctx.reply(f"{e}\n-# 料金は返金されました", ephemeral=True)
            return
        note = " (代替モデルで応答)" if fallback else ""
        await ctx.reply(
            f"{reply}\n-# 料金: {buildAmountText(price)} (通貨指数{index:.1f}連動){note}"
        )

    @ai.command(name="price", brief="AIチャットの現在料金を表示します")
    @commands.guild_only()
    async def aiPriceCommand(self, ctx: commands.Context):
        price, index = await ai.current_price()
        await ctx.reply(
            f"AIチャット料金: {buildAmountText(price)} /往復\n"
            f"(通貨価値指数 {index:.1f} 連動: 指数が低い=通貨安ほど高額)\n"
            f"モデル: `{await ai.resolve_model()}`",
            ephemeral=True,
        )

    @ai.command(name="clear", brief="自分のAI履歴を削除します")
    @commands.guild_only()
    async def aiClearCommand(self, ctx: commands.Context):
        n = await ai.clear_history(ctx.author.id)
        await ctx.reply(f"履歴を削除しました ({n}件)", ephemeral=True)

    @ai.command(name="persona", brief="自分専用のRP指示を設定・確認します")
    @app_commands.rename(text="指示文")
    @app_commands.describe(text="空にすると現在の設定を表示。clearと書くと解除")
    @commands.guild_only()
    async def aiPersonaCommand(self, ctx: commands.Context, *, text: str = ""):
        text = text.strip()
        if not text:
            cur = await ai.get_persona(ctx.author.id)
            if cur:
                await ctx.reply(f"あなたのpersona:\n```\n{cur}\n```", ephemeral=True)
            else:
                await ctx.reply(
                    "persona未設定 (管理者既定を使用中)。`clear`で解除、文章で設定。",
                    ephemeral=True,
                )
            return
        if text.lower() == "clear":
            await ai.clear_persona(ctx.author.id)
            await ctx.reply("personaを解除し、管理者既定に戻しました", ephemeral=True)
            return
        await ai.set_persona(ctx.author.id, text)
        await ctx.reply("personaを設定しました", ephemeral=True)

    @commands.hybrid_group(name="ai-admin", brief="※管理者専用 AI設定をします")
    @admin_only()
    @commands.guild_only()
    async def aiAdmin(self, ctx: commands.Context):
        await ctx.reply(
            "サブコマンドを指定してください: system / model",
            ephemeral=True,
        )

    @aiAdmin.command(name="system", brief="※管理者専用 全体既定のRP指示を設定します")
    @app_commands.rename(text="指示文")
    @app_commands.describe(text="空にすると現在の設定を表示")
    @admin_only()
    @commands.guild_only()
    async def aiAdminSystemCommand(self, ctx: commands.Context, *, text: str = ""):
        text = text.strip()
        if not text:
            cur = await ai.get_global_system()
            await ctx.reply(
                f"全体既定:\n```\n{cur or '(未設定: env/ビルトインを使用)'}\n```",
                ephemeral=True,
            )
            return
        await ai.set_global_system(text)
        await ctx.reply("全体既定のRP指示を設定しました", ephemeral=True)

    @aiAdmin.command(name="model", brief="※管理者専用 モデルを確認・変更します")
    @app_commands.describe(text="空にすると現在の設定を表示。ID指定で即時切替")
    @admin_only()
    @commands.guild_only()
    async def aiAdminModelCommand(self, ctx: commands.Context, *, text: str = ""):
        text = text.strip()
        if not text:
            cur = await ai.resolve_model()
            await ctx.reply(
                f"モデル: `{cur}`\nID指定で再起動なしに切り替えられます",
                ephemeral=True,
            )
            return
        try:
            await ai.set_model(text)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(f"モデルを `{text}` に切り替えました", ephemeral=True)

    @commands.Cog.listener("on_message")
    async def onMessage(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        bot_user = self.bot.user
        if bot_user is None:
            return
        # メンション必須。リプライのみ (メンションなし) では反応しない。
        if bot_user not in message.mentions:
            return
        # envで指定されたチャンネル (ai_channel) 以外では反応しない。
        if not _in_ai_channel(message):
            return
        # コマンド本文の除去
        text = message.content
        for m in message.mentions:
            text = text.replace(m.mention, "")
        text = text.replace("@everyone", "").replace("@here", "").strip()
        if not text:
            await message.reply("メッセージ本文を入れてメンションしてね")
            return
        retry = check_message_rate(message.author.id, "ai-chat")
        if retry > 0:
            await message.reply(f"速すぎるよ！あと{retry:.1f}秒待ってね")
            return
        price, _ = await ai.current_price()
        async with message.channel.typing():
            try:
                reply, fallback = await self._run(message.author.id, text)
            except AmountNotEnough:
                await message.reply(f"{buildAmountText(0)}が足りません…もとい残高が足りません (料金: {buildAmountText(price)})")
                return
            except ValueError as e:
                await message.reply(f"{e}\n-# 料金は返金されました")
                return
        if fallback:
            reply = f"{reply}\n-# 代替モデルで応答しました"
        await self._reply_long(message, reply)


async def setup(bot: commands.Bot):
    await bot.add_cog(AiCog(bot))
