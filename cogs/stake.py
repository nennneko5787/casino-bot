"""situ_stake.py を Cog 化 + 通貨対応したダイス / マインズ。"""

import random

import discord
from discord import app_commands
from discord.ext import commands

from objects.exceptions import AmountNotEnough, YouMustDie
from services.loan import apply_income, repay_note
from services.message import buildAmountText, buildGetAmountText
from services.money import getUser, saveUser

DICE_HOUSE_EDGE = 0.99

MINES_ROWS = 5
MINES_COLS = 4
MINES_CELLS = MINES_ROWS * MINES_COLS
MINES_HOUSE_EDGE = 0.99


def _origin(interaction: discord.Interaction) -> discord.Message:
    """操作元のメッセージ。ボタン/モーダル操作では必ず存在する前提。"""
    message = interaction.message
    assert message is not None
    return message


# ---------- ダイス用ロジック ----------


def dice_payout(rollover: int) -> float:
    chance = 100 - rollover
    return (100 / chance) * DICE_HOUSE_EDGE


def play_dice(bet: int, target: int) -> dict:
    roll = round(random.uniform(0, 100), 2)
    win = roll > target
    chance = 100 - target
    payout = (100 / chance) * DICE_HOUSE_EDGE
    profit = (bet * payout if win else 0) - bet
    return {"rolled": roll, "win": win, "payout": payout, "profit": int(profit)}


def build_dice_setup_embed(author_id: int, bet: int, rollover: int) -> discord.Embed:
    payout = dice_payout(rollover)
    profit = int(bet * payout - bet)
    embed = discord.Embed(
        title="ダイス🎲",
        description=f"<@{author_id}>",
        color=discord.Color.random(),
    )
    embed.add_field(name="掛け金", value=f"{buildAmountText(bet)}")
    embed.add_field(
        name="ロールオーバー", value=f"{rollover} (ペイアウト: ×{payout:.2f})"
    )
    embed.add_field(name="勝ちの利益", value=f"{buildAmountText(profit)}")
    return embed


class DiceBetModal(discord.ui.Modal, title="掛け金を変更"):
    bet_input = discord.ui.TextInput(
        label="掛け金", placeholder="200", style=discord.TextStyle.short
    )

    def __init__(self, author_id: int, rollover: int):
        super().__init__()
        self.author_id = author_id
        self.rollover = rollover

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのゲームではありません", ephemeral=True
            )
            return
        if not self.bet_input.value.isdigit():
            await interaction.response.send_message(
                "数字を入力してください", ephemeral=True
            )
            return
        bet = int(self.bet_input.value)
        if bet < 1:
            await interaction.response.send_message(
                "賭けの無いダイスよりつまらないものはないよ", ephemeral=True
            )
            return
        await interaction.response.defer()
        await _origin(interaction).edit(
            embed=build_dice_setup_embed(self.author_id, bet, self.rollover),
            view=DiceSetupView(self.author_id, bet, self.rollover),
        )


class DiceRolloverModal(discord.ui.Modal, title="ロールオーバーを変更"):
    rollover_input = discord.ui.TextInput(
        label="ロールオーバー (1~99)",
        placeholder="50",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=2,
    )

    def __init__(self, author_id: int, bet: int):
        super().__init__()
        self.author_id = author_id
        self.bet = bet

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのゲームではありません", ephemeral=True
            )
            return
        if not self.rollover_input.value.isdigit():
            await interaction.response.send_message(
                "数字を入力してください", ephemeral=True
            )
            return
        rollover = int(self.rollover_input.value)
        if not 1 <= rollover <= 99:
            await interaction.response.send_message(
                "ロールオーバーは1~99で指定してください", ephemeral=True
            )
            return
        await interaction.response.defer()
        await _origin(interaction).edit(
            embed=build_dice_setup_embed(self.author_id, self.bet, rollover),
            view=DiceSetupView(self.author_id, self.bet, rollover),
        )


class DiceSetupView(discord.ui.View):
    def __init__(self, author_id: int, bet: int, rollover: int):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.bet = bet
        self.rollover = rollover

    async def _check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのゲームではありません", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="掛け金を変更", style=discord.ButtonStyle.primary)
    async def change_bet(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(
            DiceBetModal(self.author_id, self.rollover)
        )

    @discord.ui.button(label="ロールオーバーを変更", style=discord.ButtonStyle.danger)
    async def change_rollover(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(
            DiceRolloverModal(self.author_id, self.bet)
        )

    @discord.ui.button(label="ロール！", style=discord.ButtonStyle.success)
    async def roll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        bet, target = self.bet, self.rollover
        if bet < 1:
            await interaction.response.send_message(
                "賭けの無いダイスよりつまらないものはないよ", ephemeral=True
            )
            return

        user_data = await getUser(interaction.user)
        if user_data.amount < bet:
            await interaction.response.send_message(
                f"残高が足りません\n\n残高: {buildAmountText(user_data.amount)}",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        user_data.amount -= bet
        result = play_dice(bet, target)

        if result["win"]:
            win_amount = int(bet * result["payout"])
            profit = win_amount - bet
            await saveUser(user_data)  # 先に掛け金分を確定させる
            repaid, _ = await apply_income(interaction.user.id, win_amount)
            embed = discord.Embed(
                title="ダイス🎲",
                description=(
                    f"勝ち！\n**{result['rolled']}** でロールオーバー (**{target}**) を超えました！\n"
                    f"```patch\n{buildGetAmountText(profit, md=True)}\n```"
                    + repay_note(repaid)
                ),
                color=discord.Color.green(),
            )
            embed.add_field(name="利益", value=buildAmountText(profit))
        else:
            await saveUser(user_data)
            embed = discord.Embed(
                title="ダイス🎲",
                description=(
                    f"負け...\n**{result['rolled']}** でロールオーバー (**{target}**) を超えませんでした\n"
                    f"```patch\n{buildGetAmountText(-bet, md=True)}\n```"
                ),
                color=discord.Color.red(),
            )
            embed.add_field(name="損失", value=buildAmountText(bet))

        await _origin(interaction).edit(
            embed=embed, view=DiceResultView(self.author_id, bet, target)
        )


class DiceResultView(discord.ui.View):
    def __init__(self, author_id: int, bet: int, rollover: int):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.bet = bet
        self.rollover = rollover

    @discord.ui.button(label="もう1度プレイ", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのゲームではありません", ephemeral=True
            )
            return
        await interaction.response.defer()
        await _origin(interaction).edit(
            embed=build_dice_setup_embed(self.author_id, self.bet, self.rollover),
            view=DiceSetupView(self.author_id, self.bet, self.rollover),
        )


# ---------- マインズ用ロジック ----------


def create_board(num_mines: int) -> list[list[int]]:
    board = [[0 for _ in range(MINES_COLS)] for _ in range(MINES_ROWS)]
    for mine in random.sample(range(MINES_CELLS), num_mines):
        board[mine // MINES_COLS][mine % MINES_COLS] = 1
    return board


def count_bombs(board: list[list[int]]) -> int:
    return sum(cell for row in board for cell in row)


def generate_payout_table(total_cells: int, num_mines: int) -> list[float]:
    payouts = [1.0]
    remaining_cells = total_cells
    remaining_safe = total_cells - num_mines
    current = 1.0
    for _ in range(remaining_safe):
        current /= remaining_safe / remaining_cells
        current *= MINES_HOUSE_EDGE
        payouts.append(current)
        remaining_cells -= 1
        remaining_safe -= 1
    return payouts


def calculate_payout(revealed: list[list[bool]], board: list[list[int]]) -> float:
    opened = sum(1 for row in revealed for cell in row if cell)
    return generate_payout_table(MINES_CELLS, count_bombs(board))[opened]


def opened_safe_count(revealed: list[list[bool]]) -> int:
    return sum(1 for row in revealed for cell in row if cell)


def build_mines_setup_embed(author_id: int, bet: int, mines: int) -> discord.Embed:
    embed = discord.Embed(
        title="マインズ", description=f"<@{author_id}>", color=discord.Color.random()
    )
    embed.add_field(name="掛け金", value=f"{buildAmountText(bet)}")
    embed.add_field(name="爆弾の数", value=f"{mines}")
    return embed


class MinesBetModal(discord.ui.Modal, title="掛け金を変更"):
    bet_input = discord.ui.TextInput(
        label="掛け金", placeholder="100", style=discord.TextStyle.short
    )

    def __init__(self, cog: "StakeCog", author_id: int, mines: int):
        super().__init__()
        self.cog = cog
        self.author_id = author_id
        self.mines = mines

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのゲームではありません", ephemeral=True
            )
            return
        if not self.bet_input.value.isdigit():
            await interaction.response.send_message(
                "数字を入力してください", ephemeral=True
            )
            return
        bet = int(self.bet_input.value)
        if bet < 1:
            await interaction.response.send_message(
                "賭けのないマインズほどつまらないものはないよ", ephemeral=True
            )
            return
        await interaction.response.defer()
        await _origin(interaction).edit(
            embed=build_mines_setup_embed(self.author_id, bet, self.mines),
            view=MinesSetupView(self.cog, self.author_id, bet, self.mines),
        )


class MinesBombsModal(discord.ui.Modal, title="爆弾の数を変更"):
    bombs_input = discord.ui.TextInput(
        label="数 (1~19)",
        placeholder="3",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=2,
    )

    def __init__(self, cog: "StakeCog", author_id: int, bet: int):
        super().__init__()
        self.cog = cog
        self.author_id = author_id
        self.bet = bet

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのゲームではありません", ephemeral=True
            )
            return
        if not self.bombs_input.value.isdigit():
            await interaction.response.send_message(
                "数字を入力してください", ephemeral=True
            )
            return
        bombs = int(self.bombs_input.value)
        if not 1 <= bombs <= MINES_CELLS - 1:
            await interaction.response.send_message(
                f"爆弾は1~{MINES_CELLS - 1}個で指定してください", ephemeral=True
            )
            return
        await interaction.response.defer()
        await _origin(interaction).edit(
            embed=build_mines_setup_embed(self.author_id, self.bet, bombs),
            view=MinesSetupView(self.cog, self.author_id, self.bet, bombs),
        )


class MinesPayoutButton(discord.ui.Button):
    def __init__(self, disabled: bool = False):
        super().__init__(
            label="ペイアウト",
            style=discord.ButtonStyle.danger,
            row=4,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, MinesBoardView)
        await view.on_payout(interaction)


class MinesCellButton(discord.ui.Button):
    def __init__(self, row: int, col: int):
        super().__init__(
            label="🔸",
            style=discord.ButtonStyle.primary,
            row=row,
        )
        self.cell_row = row
        self.cell_col = col

    async def callback(self, interaction: discord.Interaction):
        view: MinesBoardView = self.view  # ty: ignore[invalid-assignment]
        await view.on_cell_open(interaction, self.cell_row, self.cell_col)


class MinesBoardView(discord.ui.View):
    def __init__(self, cog: "StakeCog", author_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self._build()

    def _build(self):
        game = self.cog.mines_games.get(self.author_id)
        revealed = game["revealed"] if game else None
        for r in range(MINES_ROWS):
            for c in range(MINES_COLS):
                if revealed and revealed[r][c]:
                    self.add_item(
                        discord.ui.Button(
                            label="💎",
                            style=discord.ButtonStyle.success,
                            disabled=True,
                            row=r,
                        )
                    )
                else:
                    self.add_item(MinesCellButton(r, c))
        # 1マスも開いていない間はペイアウト不可 (旧仕様踏襲)
        disabled = bool(game and opened_safe_count(game["revealed"]) == 0)
        self.add_item(MinesPayoutButton(disabled=disabled))

    async def _check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのゲームではありません", ephemeral=True
            )
            return False
        if self.author_id not in self.cog.mines_games:
            await interaction.response.send_message(
                "このゲームは既に終了しています", ephemeral=True
            )
            return False
        return True

    async def on_cell_open(self, interaction: discord.Interaction, row: int, col: int):
        if not await self._check(interaction):
            return
        game = self.cog.mines_games[self.author_id]
        board, revealed, bet, num_mines = (
            game["board"],
            game["revealed"],
            game["bet"],
            game["mines"],
        )
        if revealed[row][col]:
            await interaction.response.send_message(
                "そのマスは既に開いています", ephemeral=True
            )
            return

        await interaction.response.defer()
        revealed[row][col] = True

        if board[row][col] == 1:
            # 爆弾: 掛け金は開始時に徴収済み
            del self.cog.mines_games[self.author_id]
            embed = discord.Embed(
                title="マインズ [ゲームオーバー]",
                description=(
                    f"<@{self.author_id}>\n"
                    f"```patch\n{buildGetAmountText(-bet, md=True)}\n```"
                ),
                color=discord.Color.red(),
            )
            embed.add_field(name="掛け金", value=buildAmountText(bet))
            embed.add_field(name="爆弾の数", value=str(num_mines))
            await _origin(interaction).edit(
                embed=embed,
                view=MinesResultView(
                    self.cog, self.author_id, bet, num_mines, revealed, board
                ),
            )
            return

        payout = calculate_payout(revealed, board)
        payout_amount = int(bet * payout)
        total_safe = MINES_CELLS - num_mines

        if opened_safe_count(revealed) >= total_safe:
            # 全クリア
            repaid, _ = await apply_income(interaction.user.id, payout_amount)
            del self.cog.mines_games[self.author_id]
            embed = discord.Embed(
                title="マインズ [ゲームクリア]🎉",
                description=(
                    f"<@{self.author_id}>\n"
                    f"```patch\n{buildGetAmountText(payout_amount - bet, md=True)}\n```"
                    + repay_note(repaid)
                ),
                color=discord.Color.gold(),
            )
            embed.add_field(name="掛け金", value=buildAmountText(bet))
            embed.add_field(name="爆弾の数", value=str(num_mines))
            embed.add_field(name="ペイアウト", value=buildAmountText(payout_amount))
            await _origin(interaction).edit(
                embed=embed,
                view=MinesResultView(
                    self.cog,
                    self.author_id,
                    bet,
                    num_mines,
                    revealed,
                    board,
                    cleared=True,
                ),
            )
            return

        embed = discord.Embed(
            title="マインズ",
            description=f"<@{self.author_id}>",
            color=discord.Color.green(),
        )
        embed.add_field(name="掛け金", value=buildAmountText(bet))
        embed.add_field(name="爆弾の数", value=str(num_mines))
        embed.add_field(name="ペイアウト", value=buildAmountText(payout_amount))
        await _origin(interaction).edit(
            embed=embed, view=MinesBoardView(self.cog, self.author_id)
        )

    async def on_payout(self, interaction: discord.Interaction):
        if not await self._check(interaction):
            return
        game = self.cog.mines_games[self.author_id]
        board, revealed, bet, num_mines = (
            game["board"],
            game["revealed"],
            game["bet"],
            game["mines"],
        )
        if opened_safe_count(revealed) == 0:
            await interaction.response.send_message(
                "1マス以上開いてからペイアウトしてください", ephemeral=True
            )
            return
        await interaction.response.defer()
        payout = calculate_payout(revealed, board)
        payout_amount = int(bet * payout)
        repaid, _ = await apply_income(interaction.user.id, payout_amount)
        del self.cog.mines_games[self.author_id]
        embed = discord.Embed(
            title="マインズ [ペイアウト]",
            description=(
                f"<@{self.author_id}>\n"
                f"```patch\n{buildGetAmountText(payout_amount - bet, md=True)}\n```"
                + repay_note(repaid)
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(name="掛け金", value=buildAmountText(bet))
        embed.add_field(name="爆弾の数", value=str(num_mines))
        embed.add_field(name="ペイアウト", value=buildAmountText(payout_amount))
        await _origin(interaction).edit(
            embed=embed,
            view=MinesResultView(
                self.cog, self.author_id, bet, num_mines, revealed, board
            ),
        )


class MinesResultView(discord.ui.View):
    """終了後の全開示ボード + もう1度プレイボタン。"""

    def __init__(
        self,
        cog: "StakeCog",
        author_id: int,
        bet: int,
        mines: int,
        revealed: list[list[bool]],
        board: list[list[int]],
        cleared: bool = False,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.bet = bet
        self.mines = mines
        for r in range(MINES_ROWS):
            for c in range(MINES_COLS):
                if revealed[r][c]:
                    label, style = (
                        ("💣", discord.ButtonStyle.danger)
                        if board[r][c] == 1
                        else ("💎", discord.ButtonStyle.success)
                    )
                else:
                    label, style = (
                        ("💣", discord.ButtonStyle.secondary)
                        if board[r][c] == 1
                        else ("💎", discord.ButtonStyle.secondary)
                    )
                self.add_item(
                    discord.ui.Button(label=label, style=style, disabled=True, row=r)
                )

    @discord.ui.button(label="もう1度プレイ", style=discord.ButtonStyle.primary, row=4)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのゲームではありません", ephemeral=True
            )
            return
        await interaction.response.defer()
        await _origin(interaction).edit(
            embed=build_mines_setup_embed(self.author_id, self.bet, self.mines),
            view=MinesSetupView(self.cog, self.author_id, self.bet, self.mines),
        )


class MinesSetupView(discord.ui.View):
    def __init__(self, cog: "StakeCog", author_id: int, bet: int, mines: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.bet = bet
        self.mines = mines

    async def _check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのゲームではありません", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="掛け金を変更", style=discord.ButtonStyle.primary)
    async def change_bet(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(
            MinesBetModal(self.cog, self.author_id, self.mines)
        )

    @discord.ui.button(label="爆弾の数を変更", style=discord.ButtonStyle.danger)
    async def change_bombs(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(
            MinesBombsModal(self.cog, self.author_id, self.bet)
        )

    @discord.ui.button(label="マインズ開始！", style=discord.ButtonStyle.success)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        bet, num_mines = self.bet, self.mines
        if bet < 1:
            await interaction.response.send_message(
                "賭けのないマインズほどつまらないものはないよ", ephemeral=True
            )
            return
        if not 1 <= num_mines <= MINES_CELLS - 1:
            await interaction.response.send_message(
                f"爆弾は1~{MINES_CELLS - 1}個で指定してください", ephemeral=True
            )
            return

        user_data = await getUser(interaction.user)
        if user_data.amount < bet:
            await interaction.response.send_message(
                f"残高が足りません\n\n残高: {buildAmountText(user_data.amount)}",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        user_data.amount -= bet
        await saveUser(user_data)

        board = create_board(num_mines)
        revealed = [[False for _ in range(MINES_COLS)] for _ in range(MINES_ROWS)]
        self.cog.mines_games[self.author_id] = {
            "board": board,
            "revealed": revealed,
            "bet": bet,
            "mines": num_mines,
        }
        embed = discord.Embed(
            title="マインズ",
            description=f"<@{self.author_id}>",
            color=discord.Color.random(),
        )
        embed.add_field(name="掛け金", value=buildAmountText(bet))
        embed.add_field(name="爆弾の数", value=str(num_mines))
        embed.add_field(name="ペイアウト", value=buildAmountText(bet))
        await _origin(interaction).edit(
            embed=embed, view=MinesBoardView(self.cog, self.author_id)
        )


# ---------- Cog 本体 ----------


class StakeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mines_games: dict[int, dict] = {}

    @commands.hybrid_command("dice", brief="ダイスで勝負します")
    @app_commands.rename(bet="掛け金", rollover="ロールオーバー")
    @app_commands.describe(bet="賭ける額", rollover="この数を超えたら勝ち (1~99)")
    @commands.guild_only()
    async def diceCommand(
        self, ctx: commands.Context, bet: int = 200, rollover: int = 50
    ):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply(
                "賭けの無いダイスよりつまらないものはないよ", ephemeral=True
            )
            return
        if not 1 <= rollover <= 99:
            await ctx.reply("ロールオーバーは1~99で指定してください", ephemeral=True)
            return
        user_data = await getUser(ctx.author)
        if user_data.amount < bet:
            raise AmountNotEnough()
        await ctx.reply(
            embed=build_dice_setup_embed(ctx.author.id, bet, rollover),
            view=DiceSetupView(ctx.author.id, bet, rollover),
        )

    @commands.hybrid_command("mines", brief="マインズで勝負します")
    @app_commands.rename(bet="掛け金", mines="爆弾の数")
    @app_commands.describe(bet="賭ける額", mines="爆弾の数 (1~19)")
    @commands.guild_only()
    async def minesCommand(self, ctx: commands.Context, bet: int = 100, mines: int = 3):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply(
                "賭けのないマインズほどつまらないものはないよ", ephemeral=True
            )
            return
        if not 1 <= mines <= MINES_CELLS - 1:
            await ctx.reply(
                f"爆弾は1~{MINES_CELLS - 1}個で指定してください", ephemeral=True
            )
            return
        user_data = await getUser(ctx.author)
        if user_data.amount < bet:
            raise AmountNotEnough()
        await ctx.reply(
            embed=build_mines_setup_embed(ctx.author.id, bet, mines),
            view=MinesSetupView(self, ctx.author.id, bet, mines),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(StakeCog(bot))
