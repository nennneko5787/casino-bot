"""ハイアンドロー: 次のカードが HIGH か LOW かを予想し続けて倍率を積む。"""

import random
from contextlib import suppress

import discord
from discord import app_commands
from discord.ext import commands

from objects.exceptions import AmountNotEnough, YouMustDie
from services import missions
from services.loan import apply_income, repay_note
from services.message import buildAmountText, buildGetAmountText
from services.money import getUser, saveUser

HI_LO_EDGE = 0.98

SUITS = ["♠️", "♥️", "♦️", "♣️"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def _origin(interaction: discord.Interaction) -> discord.Message:
    """操作元のメッセージ。ボタン操作では必ず存在する前提。"""
    message = interaction.message
    assert message is not None
    return message


def draw_card() -> tuple[int, str]:
    """(rank 1~13, suit) をランダムに1枚引く。"""
    return random.randint(1, 13), random.choice(SUITS)


def card_text(rank: int, suit: str) -> str:
    return f"{suit} {RANKS[rank - 1]}"


def prob_high(rank: int) -> float:
    return (13 - rank) / 13


def prob_low(rank: int) -> float:
    return (rank - 1) / 13


def round_multiplier(prob: float) -> float:
    return (1 / prob) * HI_LO_EDGE


def build_game_embed(
    author_id: int,
    bet: int,
    rank: int,
    suit: str,
    multiplier: float,
    history: list[tuple[int, str]],
) -> discord.Embed:
    embed = discord.Embed(
        title="ハイアンドロー🃏",
        description=f"<@{author_id}>",
        color=discord.Color.random(),
    )
    embed.add_field(name="掛け金", value=buildAmountText(bet))
    embed.add_field(name="現在のカード", value=f"**{card_text(rank, suit)}**")
    embed.add_field(name="現在の倍率", value=f"×{multiplier:.2f}")

    ph, pl = prob_high(rank), prob_low(rank)
    high_info = (
        f"×{round_multiplier(ph):.2f} ({ph * 100:.1f}%)" if ph > 0 else "選べません"
    )
    low_info = (
        f"×{round_multiplier(pl):.2f} ({pl * 100:.1f}%)" if pl > 0 else "選べません"
    )
    embed.add_field(name="HIGH のペイアウト", value=high_info)
    embed.add_field(name="LOW のペイアウト", value=low_info)
    embed.add_field(
        name="見込みペイアウト", value=buildAmountText(int(bet * multiplier))
    )
    if history:
        embed.add_field(
            name="履歴",
            value=" → ".join(card_text(r, s) for r, s in history[-5:]),
            inline=False,
        )
    embed.set_footer(text="同ランクは負け。キャッシュアウトで確定！")
    return embed


class HighLowView(discord.ui.View):
    def __init__(self, cog: "HighLowCog", author_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self._refresh_buttons()

    def _refresh_buttons(self):
        game = self.cog.games.get(self.author_id)
        if not game:
            return
        rank = game["rank"]
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id in (
                "hl-high",
                "hl-low",
            ):
                item.disabled = (
                    (prob_high(rank) <= 0)
                    if item.custom_id == "hl-high"
                    else (prob_low(rank) <= 0)
                )
            if isinstance(item, discord.ui.Button) and item.custom_id == "hl-cashout":
                item.disabled = game["multiplier"] <= 1.0

    async def _check(self, interaction: discord.Interaction) -> dict | None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのゲームではありません", ephemeral=True
            )
            return None
        game = self.cog.games.get(self.author_id)
        if not game:
            await interaction.response.send_message(
                "このゲームは既に終了しています", ephemeral=True
            )
            return None
        return game

    async def _guess(self, interaction: discord.Interaction, is_high: bool):
        game = await self._check(interaction)
        if not game:
            return
        rank, bet, multiplier = game["rank"], game["bet"], game["multiplier"]
        prob = prob_high(rank) if is_high else prob_low(rank)
        if prob <= 0:
            await interaction.response.send_message(
                "その選択はできません", ephemeral=True
            )
            return

        await interaction.response.defer()
        mult = round_multiplier(prob)
        next_rank, next_suit = draw_card()
        win = (next_rank > rank) if is_high else (next_rank < rank)

        if win:
            game["multiplier"] = multiplier * mult
            game["history"].append((game["rank"], game["suit"]))
            game["rank"], game["suit"] = next_rank, next_suit
            await _origin(interaction).edit(
                embed=build_game_embed(
                    self.author_id,
                    bet,
                    next_rank,
                    next_suit,
                    game["multiplier"],
                    game["history"],
                ),
                view=HighLowView(self.cog, self.author_id),
            )
        else:
            # 同ランク含め負け。掛け金は開始時に徴収済み
            del self.cog.games[self.author_id]
            guess = "HIGH" if is_high else "LOW"
            embed = discord.Embed(
                title="ハイアンドロー [ゲームオーバー]",
                description=(
                    f"<@{self.author_id}>\n"
                    f"{card_text(rank, game['suit'])} → **{card_text(next_rank, next_suit)}**\n"
                    f"{guess} 予想は外れ...\n"
                    f"```patch\n{buildGetAmountText(-bet, md=True)}\n```"
                ),
                color=discord.Color.red(),
            )
            embed.add_field(name="掛け金", value=buildAmountText(bet))
            embed.add_field(name="到達倍率", value=f"×{multiplier:.2f}")
            await _origin(interaction).edit(
                embed=embed,
                view=HighLowResultView(self.cog, self.author_id, bet),
            )

    @discord.ui.button(
        label="HIGH⬆️", style=discord.ButtonStyle.success, custom_id="hl-high"
    )
    async def high(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._guess(interaction, True)

    @discord.ui.button(
        label="LOW⬇️", style=discord.ButtonStyle.primary, custom_id="hl-low"
    )
    async def low(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._guess(interaction, False)

    @discord.ui.button(
        label="キャッシュアウト",
        style=discord.ButtonStyle.danger,
        custom_id="hl-cashout",
    )
    async def cashout(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        game = await self._check(interaction)
        if not game:
            return
        if game["multiplier"] <= 1.0:
            await interaction.response.send_message(
                "1回以上当ててからキャッシュアウトしてください", ephemeral=True
            )
            return
        await interaction.response.defer()
        bet, multiplier = game["bet"], game["multiplier"]
        payout_amount = int(bet * multiplier)
        profit = payout_amount - bet
        repaid, _ = await apply_income(interaction.user.id, payout_amount)
        del self.cog.games[self.author_id]
        embed = discord.Embed(
            title="ハイアンドロー [ペイアウト]💰",
            description=(
                f"<@{self.author_id}>\n"
                f"```patch\n{buildGetAmountText(profit, md=True)}\n```"
                + repay_note(repaid)
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(name="掛け金", value=buildAmountText(bet))
        embed.add_field(name="確定倍率", value=f"×{multiplier:.2f}")
        embed.add_field(name="ペイアウト", value=buildAmountText(payout_amount))
        await _origin(interaction).edit(
            embed=embed,
            view=HighLowResultView(self.cog, self.author_id, bet),
        )

    async def on_timeout(self):
        self.cog.games.pop(self.author_id, None)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


class HighLowResultView(discord.ui.View):
    def __init__(self, cog: "HighLowCog", author_id: int, bet: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.bet = bet

    @discord.ui.button(label="もう1度プレイ", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのゲームではありません", ephemeral=True
            )
            return
        if self.author_id in self.cog.games:
            await interaction.response.send_message(
                "進行中のゲームがあります", ephemeral=True
            )
            return
        user_data = await getUser(interaction.user)
        if user_data.amount < self.bet:
            await interaction.response.send_message(
                f"残高が足りません\n\n残高: {buildAmountText(user_data.amount)}",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        user_data.amount -= self.bet
        await saveUser(user_data)
        rank, suit = draw_card()
        self.cog.games[self.author_id] = {
            "bet": self.bet,
            "rank": rank,
            "suit": suit,
            "multiplier": 1.0,
            "history": [],
        }
        # ミッション: プレー回数を記録 (失敗してもゲームは続行)
        with suppress(Exception):
            await missions.record_event(self.author_id, "game")
        await _origin(interaction).edit(
            embed=build_game_embed(self.author_id, self.bet, rank, suit, 1.0, []),
            view=HighLowView(self.cog, self.author_id),
        )


class HighLowCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: dict[int, dict] = {}

    @commands.hybrid_command("highlow", brief="ハイアンドローで勝負します")
    @app_commands.rename(bet="掛け金")
    @app_commands.describe(bet="賭ける額")
    @commands.guild_only()
    async def highlowCommand(self, ctx: commands.Context, bet: int = 100):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply(
                "賭けのないハイアンドローほどつまらないものはないよ", ephemeral=True
            )
            return
        if ctx.author.id in self.games:
            await ctx.reply("進行中のゲームがあります", ephemeral=True)
            return
        user_data = await getUser(ctx.author)
        if user_data.amount < bet:
            raise AmountNotEnough()

        user_data.amount -= bet
        await saveUser(user_data)
        rank, suit = draw_card()
        self.games[ctx.author.id] = {
            "bet": bet,
            "rank": rank,
            "suit": suit,
            "multiplier": 1.0,
            "history": [],
        }
        # ミッション: プレー回数を記録 (失敗してもゲームは続行)
        with suppress(Exception):
            await missions.record_event(ctx.author.id, "game")
        await ctx.reply(
            embed=build_game_embed(ctx.author.id, bet, rank, suit, 1.0, []),
            view=HighLowView(self, ctx.author.id),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(HighLowCog(bot))
