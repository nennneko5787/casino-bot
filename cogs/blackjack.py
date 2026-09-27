"""ブラックジャック: 標準21点勝負。Hit/Standのみのシンプル仕様。"""

import random
from contextlib import suppress

import discord
from discord import app_commands
from discord.ext import commands

from objects.exceptions import AmountNotEnough, YouMustDie
from services.loan import apply_income, repay_note
from services.message import buildAmountText, buildGetAmountText
from services.money import getUser, saveUser

SUITS = ["♠️", "♥️", "♦️", "♣️"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

BJ_PAYOUT = 2.5  # BJ勝ちは掛け金込み2.5x
WIN_PAYOUT = 2.0  # 通常勝ちは掛け金込み2x


def new_deck() -> list[tuple[int, str]]:
    deck = [(rank, suit) for suit in SUITS for rank in range(1, 14)]
    random.shuffle(deck)
    return deck


def card_text(rank: int, suit: str) -> str:
    return f"{suit}{RANKS[rank - 1]}"


def hand_text(cards: list[tuple[int, str]]) -> str:
    return " ".join(card_text(r, s) for r, s in cards)


def hand_value(cards: list[tuple[int, str]]) -> int:
    """A=1/11調整の最良値。"""
    total = 0
    aces = 0
    for rank, _ in cards:
        if rank == 1:
            aces += 1
            total += 11
        elif rank >= 10:
            total += 10
        else:
            total += rank
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(cards: list[tuple[int, str]]) -> bool:
    return len(cards) == 2 and hand_value(cards) == 21


def is_bust(cards: list[tuple[int, str]]) -> bool:
    return hand_value(cards) > 21


def dealer_play(
    dealer: list[tuple[int, str]], deck: list[tuple[int, str]]
) -> list[tuple[int, str]]:
    """ディーラーは17以上で停止 (ソフト17も停止)。"""
    dealer = list(dealer)
    while hand_value(dealer) < 17 and deck:
        dealer.append(deck.pop())
    return dealer


def judge(player: list[tuple[int, str]], dealer: list[tuple[int, str]]) -> str:
    """player_bj / dealer_bj / win / lose / push を返す。"""
    p_bj, d_bj = is_blackjack(player), is_blackjack(dealer)
    if p_bj and d_bj:
        return "push"
    if p_bj:
        return "player_bj"
    if d_bj:
        return "dealer_bj"
    if is_bust(player):
        return "lose"
    if is_bust(dealer):
        return "win"
    pv, dv = hand_value(player), hand_value(dealer)
    if pv > dv:
        return "win"
    if pv < dv:
        return "lose"
    return "push"


def payout_for(result: str, bet: int) -> int:
    if result == "player_bj":
        return int(bet * BJ_PAYOUT)
    if result == "win":
        return int(bet * WIN_PAYOUT)
    if result == "push":
        return bet
    return 0


def build_play_embed(
    author_id: int, bet: int, player: list, dealer: list, *, hide_hole: bool
) -> discord.Embed:
    pv = hand_value(player)
    if hide_hole:
        dealer_text = f"{card_text(*dealer[0])} ＋伏せ札"
        dealer_title = "ディーラー (??)"
    else:
        dealer_text = hand_text(dealer)
        dealer_title = f"ディーラー ({hand_value(dealer)})"
    embed = discord.Embed(
        title="ブラックジャック🃏",
        description=f"<@{author_id}>",
        color=discord.Color.random(),
    )
    embed.add_field(name="掛け金", value=buildAmountText(bet))
    embed.add_field(name=f"あなた ({pv})", value=hand_text(player), inline=False)
    embed.add_field(name=dealer_title, value=dealer_text, inline=False)
    embed.set_footer(text="Hitで追加 / Standで勝負。21点超過でバースト！")
    return embed


def _origin(interaction: discord.Interaction) -> discord.Message:
    message = interaction.message
    assert message is not None
    return message


class BlackjackView(discord.ui.View):
    def __init__(self, cog: "BlackjackCog", author_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id

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

    async def _hit(self, interaction: discord.Interaction):
        game = await self._check(interaction)
        if not game:
            return
        await interaction.response.defer()
        game["player"].append(game["deck"].pop())
        if is_bust(game["player"]):
            await self.cog.settle(_origin(interaction), self.author_id, "lose")
            return
        if hand_value(game["player"]) == 21:
            # 21点到達は自動Stand
            game["dealer"] = dealer_play(game["dealer"], game["deck"])
            await self.cog.settle(
                _origin(interaction),
                self.author_id,
                judge(game["player"], game["dealer"]),
            )
            return
        await _origin(interaction).edit(
            embed=build_play_embed(
                self.author_id,
                game["bet"],
                game["player"],
                game["dealer"],
                hide_hole=True,
            ),
            view=self,
        )

    async def _stand(self, interaction: discord.Interaction):
        game = await self._check(interaction)
        if not game:
            return
        await interaction.response.defer()
        game["dealer"] = dealer_play(game["dealer"], game["deck"])
        await self.cog.settle(
            _origin(interaction), self.author_id, judge(game["player"], game["dealer"])
        )

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.success, emoji="🃏")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._hit(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.primary, emoji="✋")
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._stand(interaction)

    async def on_timeout(self):
        game = self.cog.games.pop(self.author_id, None)
        if game:
            # タイムアウトは中止扱いで返金 (ベストエフォート)
            with suppress(Exception):
                await apply_income(self.author_id, game["bet"])
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


class BlackjackResultView(discord.ui.View):
    def __init__(self, cog: "BlackjackCog", author_id: int, bet: int):
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
        await self.cog.start_game(_origin(interaction), self.author_id, self.bet)


class BlackjackCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: dict[int, dict] = {}

    async def start_game(self, message: discord.Message, author_id: int, bet: int):
        deck = new_deck()
        player = [deck.pop(), deck.pop()]
        dealer = [deck.pop(), deck.pop()]
        self.games[author_id] = {
            "deck": deck,
            "player": player,
            "dealer": dealer,
            "bet": bet,
        }
        # 初手BJ判定
        result = None
        if is_blackjack(player) or is_blackjack(dealer):
            result = judge(player, dealer)
        if result is not None:
            await self.settle(message, author_id, result)
            return
        await message.edit(
            embed=build_play_embed(author_id, bet, player, dealer, hide_hole=True),
            view=BlackjackView(self, author_id),
        )

    async def settle(self, message: discord.Message, author_id: int, result: str):
        game = self.games.pop(author_id, None)
        if not game:
            return
        bet = game["bet"]
        player, dealer = game["player"], game["dealer"]
        payout = payout_for(result, bet)
        profit = payout - bet

        titles = {
            "player_bj": "ブラックジャック！🎉",
            "dealer_bj": "ディーラーのブラックジャック...",
            "win": "あなたの勝ち！🎉",
            "lose": "あなたの負け...",
            "push": "プッシュ（引き分け）",
        }
        colors = {
            "player_bj": discord.Color.gold(),
            "dealer_bj": discord.Color.red(),
            "win": discord.Color.green(),
            "lose": discord.Color.red(),
            "push": discord.Color.greyple(),
        }
        if result in ("win", "player_bj") and payout:
            repaid, _ = await apply_income(author_id, payout)
        elif result == "push":
            repaid, _ = await apply_income(author_id, bet)
        else:
            repaid = 0

        embed = discord.Embed(
            title=f"ブラックジャック [{titles[result]}]",
            description=(
                f"<@{author_id}>\n"
                f"あなた ({hand_value(player)}): {hand_text(player)}\n"
                f"ディーラー ({hand_value(dealer)}): {hand_text(dealer)}\n"
                f"```patch\n{buildGetAmountText(profit, md=True)}\n```"
                + repay_note(repaid)
            ),
            color=colors[result],
        )
        embed.add_field(name="掛け金", value=buildAmountText(bet))
        embed.add_field(name="ペイアウト", value=buildAmountText(payout))
        await message.edit(embed=embed, view=BlackjackResultView(self, author_id, bet))

    @commands.hybrid_command("blackjack", brief="ブラックジャックで勝負します")
    @app_commands.rename(bet="掛け金")
    @app_commands.describe(bet="賭ける額")
    @commands.guild_only()
    async def blackjackCommand(self, ctx: commands.Context, bet: int = 100):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply(
                "賭けのないブラックジャックほどつまらないものはないよ",
                ephemeral=True,
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
        msg = await ctx.reply("カードを配っています...")
        await self.start_game(msg, ctx.author.id, bet)


async def setup(bot: commands.Bot):
    await bot.add_cog(BlackjackCog(bot))
