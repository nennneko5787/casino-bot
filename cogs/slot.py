import asyncio
import math
import os
import random
from contextlib import suppress

import discord
import dotenv
from discord import app_commands
from discord.ext import commands

from objects.exceptions import AmountNotEnough, EmojiNotFound, YouMustDie
from services.loan import apply_income, repay_note
from services.message import buildGetAmountText
from services.money import getUser

dotenv.load_dotenv()


class SlotRetryView(discord.ui.View):
    def __init__(self, cog: "SlotCog", author_id: int, amount: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.amount = amount
        self.spinning = False

    @discord.ui.button(
        label="もう一度引く", style=discord.ButtonStyle.success, emoji="🔁"
    )
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのスロットではありません", ephemeral=True
            )
            return
        if self.spinning:
            await interaction.response.send_message(
                "回転中です...しばらくお待ちください", ephemeral=True
            )
            return

        message = interaction.message
        assert message is not None

        self.spinning = True
        button.disabled = True
        with suppress(discord.DiscordException):
            await message.edit(view=self)
        # spin 側で編集するため先に defer して二重応答を防ぐ (済みなら無視)
        with suppress(discord.DiscordException):
            await interaction.response.defer()

        try:
            await self.cog.spin(message, interaction.user, self.amount, view=self)
        except AmountNotEnough:
            await interaction.followup.send("所持金が足りません。", ephemeral=True)
        except YouMustDie:
            await interaction.followup.send("※対策済みです", ephemeral=True)
        except Exception:  # noqa: BLE001 - ボタン操作では予期せぬ失敗も画面に返す
            await interaction.followup.send(
                "スロットの実行中にエラーが発生しました。", ephemeral=True
            )
        finally:
            self.spinning = False
            button.disabled = False
            with suppress(discord.DiscordException):
                await message.edit(view=self)


class SlotCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.slot = await self.bot.fetch_application_emoji(
            int(os.environ["slot_emoji"])
        )
        self.gatiiku = await self.bot.fetch_application_emoji(
            int(os.environ["gatiiku_emoji"])
        )

        if not self.slot or not self.gatiiku:
            raise EmojiNotFound()

        self.slotEmojis = [
            ("🐱", 20, 1.1),
            ("🐥", 20, 1.1),
            ("🍇", 15, 1.5),
            ("🍋", 15, 1.5),
            ("🈵", 12, 2),
            ("🗿", 12, 2),
            ("🐰", 10, 3),
            ("🐀", 10, 3),
            ("🍎", 6, 7),
            ("✨", 4, 12),
            ("🍷", 3, 20),
            (str(self.gatiiku), 1, 50),
        ]

        self.emojis = [emoji for emoji, _, _ in self.slotEmojis]
        self.weights = [weight for _, weight, _ in self.slotEmojis]
        self.multipliers = {
            emoji: multiplier for emoji, _, multiplier in self.slotEmojis
        }

    def getMultiplier(self, outputs: list[str]) -> float:
        unique = set(outputs)

        # 3つ揃い
        if len(unique) == 1:
            return self.multipliers[outputs[0]]

        # 🗿 + 🍷
        if "🗿" in unique and "🍷" in unique:
            return 30

        # 🍎 + 🍋 + 🍇
        if {"🍎", "🍋", "🍇"} <= unique:
            return 10

        # ✨ + 🍷
        if "✨" in unique and "🍷" in unique:
            return 15

        # 🐱 + 🐀
        if "🐱" in unique and "🐀" in unique:
            return 8

        # 🈵 + 🗿
        if "🈵" in unique and "🗿" in unique:
            return 5

        # 🍎 + 🍎
        if outputs.count("🍎") >= 2:
            return 5

        # ✨ + ✨
        if outputs.count("✨") >= 2:
            return 8

        # 🍷 + 🍷
        if outputs.count("🍷") >= 2:
            return 15

        # gatiiku + 高レア絵文字
        if str(self.gatiiku) in unique:
            if "🍷" in unique:
                return 40

            if "✨" in unique:
                return 25

            if "🍎" in unique:
                return 15

        # 同じ絵柄が2つ
        if len(unique) == 2:
            for emoji in unique:
                if outputs.count(emoji) == 2:
                    return self.multipliers[emoji]

        return 0

    async def spin(
        self,
        message: discord.Message,
        user: discord.User | discord.Member,
        amount: int,
        *,
        view: SlotRetryView | None = None,
    ) -> int:
        """スロットを1回回して結果を message に反映する。戻り値は reward。"""
        if amount < 0:
            raise YouMustDie()
        userData = await getUser(user)
        if userData.amount < amount:
            raise AmountNotEnough()

        slotOutputs = random.choices(
            self.emojis,
            weights=self.weights,
            k=3,
        )

        await message.edit(content=f"{self.slot}" * 3, view=view)

        await asyncio.sleep(1)

        for i in range(3):
            await asyncio.sleep(0.35)

            await message.edit(
                content=("".join(slotOutputs[: i + 1]) + f"{self.slot}" * (2 - i)),
                view=view,
            )

        multiplier = self.getMultiplier(slotOutputs)

        if multiplier > 0:
            reward = math.ceil(amount * multiplier)
        else:
            reward = -amount

        repaid, _ = await apply_income(user.id, reward)

        if view is None:
            view = SlotRetryView(self, user.id, amount)

        await message.edit(
            content=(
                f"{''.join(slotOutputs)}\n"
                "```patch\n"
                f"{buildGetAmountText(reward, md=True)}\n"
                "```" + repay_note(repaid)
            ),
            view=view,
        )
        return reward

    @commands.hybrid_command("slot", brief="スロットを引きます")
    @app_commands.rename(amount="賭ける額")
    @app_commands.describe(amount="賭ける事ができます")
    @commands.guild_only()
    async def slotCommand(
        self,
        ctx: commands.Context,
        amount: int,
    ):
        userData = await getUser(ctx.author)

        if amount < 0:
            raise YouMustDie()
        if userData.amount < amount:
            raise AmountNotEnough()

        message = await ctx.reply(content=f"{self.slot}" * 3)

        await self.spin(message, ctx.author, amount)


async def setup(bot: commands.Bot):
    await bot.add_cog(SlotCog(bot))
