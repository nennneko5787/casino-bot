import asyncio
import math
import os
import random

import dotenv
from discord import app_commands
from discord.ext import commands

from objects.exceptions import AmountNotEnough, EmojiNotFound, YouMustDie
from services.message import buildGetAmountText
from services.money import getUser, saveUser

dotenv.load_dotenv()


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

        slotRotating = f"{self.slot}" * 3
        message = await ctx.reply(content=slotRotating)

        slotOutputs = random.choices(
            self.emojis,
            weights=self.weights,
            k=3,
        )

        await asyncio.sleep(1)

        for i in range(3):
            await asyncio.sleep(0.35)

            await message.edit(
                content=("".join(slotOutputs[: i + 1]) + f"{self.slot}" * (2 - i))
            )

        multiplier = self.getMultiplier(slotOutputs)

        if multiplier > 0:
            reward = math.ceil(amount * multiplier)
        else:
            reward = -amount

        userData.amount += reward
        await saveUser(userData)

        await message.edit(
            content=(
                f"{''.join(slotOutputs)}\n"
                "```patch\n"
                f"{buildGetAmountText(reward, md=True)}\n"
                "```"
            )
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SlotCog(bot))
