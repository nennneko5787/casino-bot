"""オセロ: CPU戦 / 指名対戦 / 参加者募集の3モード。"""

import asyncio
import logging
import os
import random
from contextlib import suppress
from copy import deepcopy
from sqlite3 import Error as SQLiteError

import discord
import dotenv
from discord import app_commands
from discord.ext import commands

from objects.exceptions import AmountNotEnough, CasinoBaseException, YouMustDie
from services.message import buildAmountText, buildGetAmountText
from services.money import getUser, saveUser
from services.othello_image import render_board_image

dotenv.load_dotenv()

logger = logging.getLogger(__name__)

BLACK = 1
WHITE = 2

DIRS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

WEIGHTS = [
    [120, -20, 20, 5, 5, 20, -20, 120],
    [-20, -40, -5, -5, -5, -5, -40, -20],
    [20, -5, 15, 3, 3, 15, -5, 20],
    [5, -5, 3, 3, 3, 3, -5, 5],
    [5, -5, 3, 3, 3, 3, -5, 5],
    [20, -5, 15, 3, 3, 15, -5, 20],
    [-20, -40, -5, -5, -5, -5, -40, -20],
    [120, -20, 20, 5, 5, 20, -20, 120],
]

DIFFICULTY_MULT = {"easy": 1.5, "normal": 1.9, "hard": 2.5}
DIFFICULTY_NAME = {"easy": "かんたん", "normal": "ふつう", "hard": "つよい"}
PVP_RAKE = 0.95  # 対人戦の勝者取り分 (5%は控除)

COL_FW = "ＡＢＣＤＥＦＧＨ"
ROW_FW = "１２３４５６７８"
REGIONAL_BASE = 0x1F1E6  # 🇦
MOVES_PER_PAGE = 20  # ボタン4行分。残り1行はページ送り/降参用
BOARD_IMAGE_NAME = "othello.png"  # Embed側は attachment://othello.png で参照


def move_letter(i: int) -> str:
    """0 -> 🇦, 1 -> 🇧, ... のリージョナルインジケータ。"""
    return chr(REGIONAL_BASE + i)


def unicode_tiles() -> dict:
    """カスタム絵文字が無い場合の代替タイル。"""
    return {
        "black": "⚫",
        "white": "⚪",
        "empty": "🟩",
        "letters": [move_letter(i) for i in range(26)],
    }


def _env_id(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else None


# ---------- エンジン ----------


def new_board() -> list[list[int]]:
    board = [[0] * 8 for _ in range(8)]
    board[3][3] = WHITE
    board[3][4] = BLACK
    board[4][3] = BLACK
    board[4][4] = WHITE
    return board


def flips_for(
    board: list[list[int]], color: int, r: int, c: int
) -> list[tuple[int, int]]:
    if board[r][c] != 0:
        return []
    opp = 3 - color
    flips: list[tuple[int, int]] = []
    for dr, dc in DIRS:
        line: list[tuple[int, int]] = []
        nr, nc = r + dr, c + dc
        while 0 <= nr < 8 and 0 <= nc < 8 and board[nr][nc] == opp:
            line.append((nr, nc))
            nr += dr
            nc += dc
        if line and 0 <= nr < 8 and 0 <= nc < 8 and board[nr][nc] == color:
            flips.extend(line)
    return flips


def legal_moves(board: list[list[int]], color: int) -> list[tuple[int, int]]:
    return [(r, c) for r in range(8) for c in range(8) if flips_for(board, color, r, c)]


def apply_move(board: list[list[int]], color: int, r: int, c: int) -> list[list[int]]:
    flips = flips_for(board, color, r, c)
    if not flips:
        raise ValueError("illegal move")
    nb = deepcopy(board)
    nb[r][c] = color
    for fr, fc in flips:
        nb[fr][fc] = color
    return nb


def count_discs(board: list[list[int]]) -> tuple[int, int]:
    black = sum(row.count(BLACK) for row in board)
    white = sum(row.count(WHITE) for row in board)
    return black, white


def is_game_over(board: list[list[int]]) -> bool:
    return not legal_moves(board, BLACK) and not legal_moves(board, WHITE)


def evaluate(board: list[list[int]], color: int) -> float:
    opp = 3 - color
    score = 0.0
    for r in range(8):
        for c in range(8):
            if board[r][c] == color:
                score += WEIGHTS[r][c]
            elif board[r][c] == opp:
                score -= WEIGHTS[r][c]
    score += (len(legal_moves(board, color)) - len(legal_moves(board, opp))) * 8
    return score


def negamax(
    board: list[list[int]],
    color: int,
    depth: int,
    alpha: float,
    beta: float,
) -> float:
    moves = legal_moves(board, color)
    opp_moves = legal_moves(board, 3 - color)
    if not moves and not opp_moves:
        mine, theirs = (
            count_discs(board) if color == BLACK else count_discs(board)[::-1]
        )
        if mine > theirs:
            return 100000 + mine - theirs
        if mine < theirs:
            return -100000 - (theirs - mine)
        return 0
    if depth == 0:
        return evaluate(board, color)
    if not moves:
        return -negamax(board, 3 - color, depth - 1, -beta, -alpha)
    best = float("-inf")
    for r, c in sorted(moves, key=lambda m: WEIGHTS[m[0]][m[1]], reverse=True):
        value = -negamax(
            apply_move(board, color, r, c), 3 - color, depth - 1, -beta, -alpha
        )
        best = max(best, value)
        alpha = max(alpha, value)
        if alpha >= beta:
            break
    return best


def cpu_choose(board: list[list[int]], color: int, difficulty: str) -> tuple[int, int]:
    moves = legal_moves(board, color)
    if difficulty == "easy":
        return random.choice(moves)
    if difficulty == "normal":
        scored = [
            (
                len(flips_for(board, color, r, c)) * 2 + WEIGHTS[r][c],
                random.random(),
                (r, c),
            )
            for r, c in moves
        ]
        return max(scored)[2]
    # hard: 先読み3手
    ordered = sorted(moves, key=lambda m: WEIGHTS[m[0]][m[1]], reverse=True)
    best_move, best_value = ordered[0], float("-inf")
    for r, c in ordered:
        value = -negamax(
            apply_move(board, color, r, c),
            3 - color,
            2,
            float("-inf"),
            float("-inf") * -1,
        )
        if value > best_value:
            best_value, best_move = value, (r, c)
    return best_move


# ---------- 表示 ----------


def coord_name(r: int, c: int) -> str:
    return f"{chr(ord('A') + c)}{r + 1}"


def render_board(
    board: list[list[int]],
    tiles: dict,
    hints: dict[tuple[int, int], str] | None = None,
) -> str:
    """hints: {(r, c): 表示タイル}。置けるマスに文字タイルを表示する。

    カスタム絵文字はコードブロック内では描画されないため fences 無し。
    全タイルが正方形の絵文字なら盤面が揃う。
    """
    hints = hints or {}
    lines = ["＼ " + " ".join(COL_FW)]
    for r in range(8):
        line = ROW_FW[r] + " "
        for c in range(8):
            if board[r][c] == BLACK:
                line += tiles["black"]
            elif board[r][c] == WHITE:
                line += tiles["white"]
            elif (r, c) in hints:
                line += hints[(r, c)]
            else:
                line += tiles["empty"]
        lines.append(line)
    return "\n".join(lines)


def color_emoji(color: int) -> str:
    return "⚫" if color == BLACK else "⚪"


# ---------- 進行中ゲームの View ----------


def button_label(i: int, r: int, c: int) -> str:
    """盤面の文字に対応するボタンラベル。27手目以降は座標表記 (実質起きない想定の保険)。"""
    return move_letter(i) if i < 26 else coord_name(r, c)


class OthelloMoveButton(discord.ui.Button):
    def __init__(self, label: str, r: int, c: int, row: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)
        self.move_label = label
        self.cell = (r, c)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, OthelloGameView)
        await view.do_move(interaction, self.move_label, self.cell[0], self.cell[1])


class OthelloPageButton(discord.ui.Button):
    def __init__(self, label: str, delta: int, disabled: bool):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            row=4,
            disabled=disabled,
        )
        self.delta = delta

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, OthelloGameView)
        await view.flip_page(interaction, self.delta)


class OthelloResignButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="降参する", style=discord.ButtonStyle.danger, row=4)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, OthelloGameView)
        await view.do_resign(interaction)


class OthelloGameView(discord.ui.View):
    """置けるマス = 盤面の文字🇦〜 + 対応ボタンを押して着手。20手/ページでページ送り対応。"""

    def __init__(
        self, cog: "OthelloCog", game_id: int, page: int = 0, notice: str = ""
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.game_id = game_id
        self.page = page
        self.notice = notice
        self.message: discord.Message | None = None
        self._build()

    def _game(self) -> dict | None:
        return self.cog.games.get(self.game_id)

    def _ordered_moves(self) -> list[tuple[int, int]]:
        game = self._game()
        if not game:
            return []
        return legal_moves(game["board"], game["turn"])

    @property
    def page_count(self) -> int:
        return max(
            1, (len(self._ordered_moves()) + MOVES_PER_PAGE - 1) // MOVES_PER_PAGE
        )

    def _build(self):
        moves = self._ordered_moves()
        self.page = min(self.page, self.page_count - 1)
        page_moves = moves[
            self.page * MOVES_PER_PAGE : (self.page + 1) * MOVES_PER_PAGE
        ]
        for i, (r, c) in enumerate(page_moves):
            idx = self.page * MOVES_PER_PAGE + i
            self.add_item(OthelloMoveButton(button_label(idx, r, c), r, c, row=i // 5))
        if self.page_count > 1:
            self.add_item(OthelloPageButton("◀", -1, disabled=self.page == 0))
            self.add_item(
                OthelloPageButton("▶", 1, disabled=self.page >= self.page_count - 1)
            )
        self.add_item(OthelloResignButton())

    async def _turn_game(self, interaction: discord.Interaction) -> dict | None:
        """参加者 & 手番チェック。NG時は ephemeral 送信して None。"""
        game = self._game()
        if not game:
            await interaction.response.send_message(
                "このゲームは既に終了しています", ephemeral=True
            )
            return None
        if interaction.user.id not in (game["black_id"], game["white_id"]):
            await interaction.response.send_message(
                "このゲームの参加者ではありません", ephemeral=True
            )
            return None
        turn_id = game["black_id"] if game["turn"] == BLACK else game["white_id"]
        if interaction.user.id != turn_id:
            await interaction.response.send_message(
                "あなたの手番ではありません", ephemeral=True
            )
            return None
        return game

    async def do_move(
        self, interaction: discord.Interaction, label: str, r: int, c: int
    ):
        game = await self._turn_game(interaction)
        if not game:
            return
        if not flips_for(game["board"], game["turn"], r, c):
            await interaction.response.send_message(
                "そこには置けません", ephemeral=True
            )
            return
        await interaction.response.defer()
        game["board"] = apply_move(game["board"], game["turn"], r, c)
        game["turn"] = 3 - game["turn"]
        message = interaction.message
        assert message is not None
        await self.cog.advance(
            message,
            game,
            self.game_id,
            notice=f"{label}({coord_name(r, c)}) に着手",
        )

    async def flip_page(self, interaction: discord.Interaction, delta: int):
        game = await self._turn_game(interaction)
        if not game:
            return
        await interaction.response.defer()
        view = OthelloGameView(
            self.cog, self.game_id, page=self.page + delta, notice=self.notice
        )
        message = interaction.message
        assert message is not None
        view.message = message
        hints = self.cog.hint_markers(game, self.cog.tiles)
        await message.edit(
            embed=self.cog.build_game_embed(
                game,
                hints,
                self.notice,
                view.page,
                view.page_count,
            ),
            attachments=[self.cog.board_file(game, hints)],
            view=view,
        )

    async def do_resign(self, interaction: discord.Interaction):
        game = self._game()
        if not game:
            await interaction.response.send_message(
                "このゲームは既に終了しています", ephemeral=True
            )
            return
        if interaction.user.id not in (game["black_id"], game["white_id"]):
            await interaction.response.send_message(
                "このゲームの参加者ではありません", ephemeral=True
            )
            return
        await interaction.response.defer()
        message = interaction.message
        assert message is not None
        await self.cog.settle(
            message,
            game,
            self.game_id,
            winner_id=None,
            resigned_id=interaction.user.id,
        )

    async def on_timeout(self):
        game = self.cog.games.pop(self.game_id, None)
        if game:
            # タイムアウトは中止扱いで全額返金 (ベストエフォート)
            for uid in {game["black_id"], game["white_id"]}:
                if uid == 0:  # CPU のダミーID
                    continue
                with suppress(
                    discord.DiscordException, SQLiteError, CasinoBaseException
                ):
                    user = await self.cog.bot.fetch_user(uid)
                    data = await getUser(user)
                    data.amount += game["bet"]
                    await saveUser(data)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            with suppress(discord.DiscordException):
                await self.message.edit(
                    content="タイムアウトのため中止しました（掛け金は返金）", view=self
                )


# ---------- 募集・指名ロビー ----------


class ChallengeView(discord.ui.View):
    """指名対戦: 指名された相手だけが参加/辞退できる。"""

    def __init__(
        self, cog: "OthelloCog", host_id: int, guest_id: int, bet: int, host_first: str
    ):
        super().__init__(timeout=60)
        self.cog = cog
        self.host_id = host_id
        self.guest_id = guest_id
        self.bet = bet
        self.host_first = host_first
        self.message: discord.Message | None = None

    @discord.ui.button(label="参加する", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.guest_id:
            await interaction.response.send_message(
                "あなたへの指名ではありません", ephemeral=True
            )
            return
        # 自分のロビー登録を先に外す (is_busy が自分のロビーに反応しないよう)
        self.cog.lobbies.discard(self.host_id)
        self.cog.lobby_views.pop(self.host_id, None)
        if self.cog.is_busy(self.host_id) or self.cog.is_busy(self.guest_id):
            await interaction.response.send_message(
                "対戦者の都合で開始できませんでした", ephemeral=True
            )
            await self._expire("対戦を開始できませんでした")
            return
        host_data = await getUser(await self.cog.bot.fetch_user(self.host_id))
        guest_data = await getUser(interaction.user)
        if host_data.amount < self.bet or guest_data.amount < self.bet:
            await interaction.response.send_message(
                "残高が足りないため開始できません", ephemeral=True
            )
            await self._expire("残高不足のため中止しました")
            return
        await interaction.response.defer()
        host_data.amount -= self.bet
        guest_data.amount -= self.bet
        await saveUser(host_data)
        await saveUser(guest_data)
        lock = self._decide_first()
        message = interaction.message
        assert message is not None
        await self.cog.start_pvp(message, self.host_id, self.guest_id, self.bet, lock)
        # 開始後はロビーのタイムアウトを止める (期限切れ表示で盤面を上書きしないよう)
        self.stop()

    @discord.ui.button(label="断る", style=discord.ButtonStyle.danger)
    async def decline(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.guest_id:
            await interaction.response.send_message(
                "あなたへの指名ではありません", ephemeral=True
            )
            return
        await interaction.response.defer()
        self.cog.lobbies.discard(self.host_id)
        self.cog.lobby_views.pop(self.host_id, None)
        await self._expire("指名は断られました")

    def _decide_first(self) -> bool:
        if self.host_first == "host":
            return True
        if self.host_first == "guest":
            return False
        return random.random() < 0.5

    async def _expire(self, text: str):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            with suppress(discord.DiscordException):
                await self.message.edit(content=text, view=self)
        self.stop()

    async def on_timeout(self):
        self.cog.lobbies.discard(self.host_id)
        self.cog.lobby_views.pop(self.host_id, None)
        await self._expire("応答がなかったため指名は期限切れになりました")


class OpenLobbyView(discord.ui.View):
    """参加者募集: やりたい人がボタンを押して参加。"""

    def __init__(self, cog: "OthelloCog", host_id: int, bet: int, host_first: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.host_id = host_id
        self.bet = bet
        self.host_first = host_first
        self.message: discord.Message | None = None

    @discord.ui.button(label="対戦する！", style=discord.ButtonStyle.success, emoji="⚔️")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.bot:
            await interaction.response.send_message(
                "Botは参加できません", ephemeral=True
            )
            return
        if interaction.user.id == self.host_id:
            await interaction.response.send_message(
                "主催者は参加ボタンではなく開始を待ってください", ephemeral=True
            )
            return
        # 自分のロビー登録を先に外す (is_busy が自分のロビーに反応しないよう)
        self.cog.lobbies.discard(self.host_id)
        self.cog.lobby_views.pop(self.host_id, None)
        if self.cog.is_busy(interaction.user.id) or self.cog.is_busy(self.host_id):
            await interaction.response.send_message(
                "対戦を開始できませんでした", ephemeral=True
            )
            await self._expire("対戦を開始できませんでした")
            return
        host_data = await getUser(await self.cog.bot.fetch_user(self.host_id))
        guest_data = await getUser(interaction.user)
        if host_data.amount < self.bet or guest_data.amount < self.bet:
            await interaction.response.send_message(
                "残高が足りないため開始できません", ephemeral=True
            )
            await self._expire("残高不足のため中止しました")
            return
        await interaction.response.defer()
        host_data.amount -= self.bet
        guest_data.amount -= self.bet
        await saveUser(host_data)
        await saveUser(guest_data)
        if self.host_first == "host":
            host_is_black = True
        elif self.host_first == "guest":
            host_is_black = False
        else:
            host_is_black = random.random() < 0.5
        black_id = self.host_id if host_is_black else interaction.user.id
        white_id = interaction.user.id if host_is_black else self.host_id
        message = interaction.message
        assert message is not None
        await self.cog.start_pvp_direct(message, black_id, white_id, self.bet)
        # 開始後はロビーのタイムアウトを止める (期限切れ表示で盤面を上書きしないよう)
        self.stop()

    @discord.ui.button(label="募集を取り消す", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "主催者のみ取り消せます", ephemeral=True
            )
            return
        await interaction.response.defer()
        self.cog.lobbies.discard(self.host_id)
        self.cog.lobby_views.pop(self.host_id, None)
        await self._expire("募集は取り消されました")

    async def _expire(self, text: str):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            with suppress(discord.DiscordException):
                await self.message.edit(content=text, view=self)
        self.stop()

    async def on_timeout(self):
        self.cog.lobbies.discard(self.host_id)
        self.cog.lobby_views.pop(self.host_id, None)
        await self._expire("応募がなかったため募集は期限切れになりました")


class OthelloRematchView(discord.ui.View):
    """CPU戦専用: 同条件でもう1度。"""

    def __init__(
        self,
        cog: "OthelloCog",
        author_id: int,
        bet: int,
        difficulty: str,
        player_first: bool,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.bet = bet
        self.difficulty = difficulty
        self.player_first = player_first

    @discord.ui.button(label="もう1度プレイ", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あなたのゲームではありません", ephemeral=True
            )
            return
        if self.cog.is_busy(self.author_id):
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
        message = interaction.message
        assert message is not None
        await self.cog.start_cpu(
            message,
            self.author_id,
            self.bet,
            self.difficulty,
            self.player_first,
        )


# ---------- Cog ----------


class OthelloCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: dict[int, dict] = {}
        self.lobbies: set[int] = set()
        self.lobby_views: dict[int, ChallengeView | OpenLobbyView] = {}
        self._next_game_id = 1
        self.tiles: dict = unicode_tiles()

    async def cog_load(self):
        """slot.py と同じ方式で盤面タイルのカスタム絵文字を取得する。

        必要な環境変数: othello_black / othello_white / othello_empty (絵文字ID)、
        othello_letters (A〜Zの絵文字IDをカンマ区切りで26個)。
        取得できなければ代替のユニコード表示にフォールバックする。
        """
        try:
            black_id = _env_id("othello_black")
            white_id = _env_id("othello_white")
            empty_id = _env_id("othello_empty")
            letter_ids = [
                part.strip()
                for part in os.environ.get("othello_letters", "").split(",")
            ]
            if (
                not black_id
                or not white_id
                or not empty_id
                or len(letter_ids) != 26
                or not all(part.isdigit() for part in letter_ids)
            ):
                raise ValueError("othello用絵文字の環境変数が不足しています")
            black = await self.bot.fetch_application_emoji(black_id)
            white = await self.bot.fetch_application_emoji(white_id)
            empty = await self.bot.fetch_application_emoji(empty_id)
            letters = await asyncio.gather(
                *(self.bot.fetch_application_emoji(int(part)) for part in letter_ids)
            )
            if not black or not white or not empty or not all(letters):
                raise ValueError("othello用絵文字の取得に失敗しました")
            self.tiles = {
                "black": str(black),
                "white": str(white),
                "empty": str(empty),
                "letters": [str(emoji) for emoji in letters],
            }
            logger.info("othello: カスタム絵文字タイルを使用します")
        except (ValueError, discord.DiscordException) as e:
            logger.warning(
                "othello: カスタム絵文字を使えないため代替表示します (%s)", e
            )

    def busy_reason(self, user_id: int) -> str | None:
        """募集中のロビー持ちなら募集中、対局中なら対局中。どちらでもなければ None。"""
        if user_id in self.lobbies:
            return "募集中"
        if any(
            g["black_id"] == user_id or g["white_id"] == user_id
            for g in self.games.values()
        ):
            return "対局中"
        return None

    def is_busy(self, user_id: int) -> bool:
        return self.busy_reason(user_id) is not None

    def _new_game_id(self) -> int:
        gid = self._next_game_id
        self._next_game_id += 1
        return gid

    # ----- ゲーム開始 -----

    async def start_cpu(
        self,
        message: discord.Message,
        player_id: int,
        bet: int,
        difficulty: str,
        player_first: bool,
    ):
        gid = self._new_game_id()
        black_id = player_id if player_first else 0
        white_id = 0 if player_first else player_id
        self.games[gid] = {
            "board": new_board(),
            "turn": BLACK,
            "black_id": black_id,
            "white_id": white_id,
            "bet": bet,
            "mode": "cpu",
            "difficulty": difficulty,
            "mult": DIFFICULTY_MULT[difficulty],
            "player_first": player_first,
        }
        await self.advance(
            message,
            self.games[gid],
            gid,
            notice="ゲーム開始！あなたは" + ("⚫先手" if player_first else "⚪後手"),
        )

    async def start_pvp_direct(
        self,
        message: discord.Message,
        black_id: int,
        white_id: int,
        bet: int,
    ):
        gid = self._new_game_id()
        self.games[gid] = {
            "board": new_board(),
            "turn": BLACK,
            "black_id": black_id,
            "white_id": white_id,
            "bet": bet,
            "mode": "pvp",
        }
        await self.advance(message, self.games[gid], gid, notice="対戦開始！⚫からです")

    async def start_pvp(
        self,
        message: discord.Message,
        host_id: int,
        guest_id: int,
        bet: int,
        host_is_black: bool,
    ):
        """指名対戦用: host_is_black でホストの色を決める。"""
        black_id = host_id if host_is_black else guest_id
        white_id = guest_id if host_is_black else host_id
        await self.start_pvp_direct(message, black_id, white_id, bet)

    # ----- 進行 -----

    @staticmethod
    def hint_markers(game: dict, tiles: dict) -> dict[tuple[int, int], str]:
        """置けるマス -> 盤面に表示する文字タイル。読む順に割り当てる。"""
        markers: dict[tuple[int, int], str] = {}
        letters = tiles.get("letters", [])
        for i, (r, c) in enumerate(legal_moves(game["board"], game["turn"])):
            markers[(r, c)] = letters[i] if i < len(letters) else "🟨"
        return markers

    @staticmethod
    def board_file(game: dict, hints: dict | None = None) -> discord.File:
        """盤面を1枚画像化した discord.File。Embedは attachment://othello.png を参照。"""
        buf = render_board_image(game["board"], hints or {})
        return discord.File(buf, filename=BOARD_IMAGE_NAME)

    def build_game_embed(
        self, game: dict, hints: dict, notice: str = "", page: int = 0, pages: int = 1
    ) -> discord.Embed:
        black_id, white_id = game["black_id"], game["white_id"]
        turn_id = black_id if game["turn"] == BLACK else white_id
        black_n, white_n = count_discs(game["board"])
        if game["mode"] == "cpu":
            player_id = black_id or white_id
            title = f"オセロ [CPU: {DIFFICULTY_NAME[game['difficulty']]}]⚫⚪"
            desc_head = f"<@{player_id}> vs 🤖CPU"
            payout_text = buildAmountText(int(game["bet"] * game["mult"]))
        else:
            title = "オセロ [対人戦]⚫⚪"
            desc_head = f"⚫<@{black_id}> vs ⚪<@{white_id}>"
            payout_text = buildAmountText(int(game["bet"] * 2 * PVP_RAKE))
        turn_text = "🤖CPU" if turn_id == 0 else f"<@{turn_id}>"
        desc = f"{desc_head}\n手番: {turn_text} {color_emoji(game['turn'])}"
        if notice:
            desc += f"\n{notice}"
        embed = discord.Embed(
            title=title, description=desc, color=discord.Color.random()
        )
        embed.add_field(name="掛け金 (1人あたり)", value=buildAmountText(game["bet"]))
        embed.add_field(name="⚫ / ⚪", value=f"{black_n} / {white_n}")
        embed.add_field(name="勝ち時ペイアウト", value=payout_text)
        footer = "盤面の文字🇦〜と同じボタンを押して着手。パスは自動。"
        if pages > 1:
            footer += f" ({page + 1}/{pages}ページ)"
        if any(v == "🟨" for v in hints.values()):
            footer += " 🟨は座標ボタンで指定。"
        embed.set_footer(text=footer)
        embed.set_image(url=f"attachment://{BOARD_IMAGE_NAME}")
        return embed

    async def advance(
        self,
        message: discord.Message,
        game: dict,
        gid: int,
        notice: str = "",
    ):
        """手番の自動進行 (パス・CPU着手)。最後に盤面を1回編集する。"""
        while True:
            if is_game_over(game["board"]):
                await self.settle(message, game, gid)
                return
            moves = legal_moves(game["board"], game["turn"])
            if not moves:
                game["turn"] = 3 - game["turn"]
                if not legal_moves(game["board"], game["turn"]):
                    await self.settle(message, game, gid)
                    return
                notice += "\n置ける場所がないため自動パスしました"
                continue
            turn_id = game["black_id"] if game["turn"] == BLACK else game["white_id"]
            if turn_id == 0:
                # CPU の番
                await asyncio.sleep(0.8)
                difficulty = game["difficulty"]
                if difficulty == "hard":
                    r, c = await asyncio.to_thread(
                        cpu_choose, game["board"], game["turn"], difficulty
                    )
                else:
                    r, c = cpu_choose(game["board"], game["turn"], difficulty)
                game["board"] = apply_move(game["board"], game["turn"], r, c)
                notice = f"🤖CPUは {coord_name(r, c)} に置きました"
                game["turn"] = 3 - game["turn"]
                continue
            view = OthelloGameView(self, gid, notice=notice)
            view.message = message
            hints = self.hint_markers(game, self.tiles)
            await message.edit(
                embed=self.build_game_embed(
                    game,
                    hints,
                    notice,
                    view.page,
                    view.page_count,
                ),
                attachments=[self.board_file(game, hints)],
                view=view,
            )
            return

    async def settle(
        self,
        message: discord.Message,
        game: dict,
        gid: int,
        winner_id: int | None = None,
        resigned_id: int | None = None,
    ):
        black_n, white_n = count_discs(game["board"])
        bet = game["bet"]
        if resigned_id is not None:
            # 降参: 相手の勝ち
            if game["mode"] == "cpu":
                result = "lose"
            else:
                winner_id = (
                    game["white_id"]
                    if resigned_id == game["black_id"]
                    else game["black_id"]
                )
                result = "win"
        elif winner_id is None:
            if game["mode"] == "cpu":
                mine = black_n if game["black_id"] != 0 else white_n
                theirs = white_n if game["black_id"] != 0 else black_n
                result = (
                    "win" if mine > theirs else ("draw" if mine == theirs else "lose")
                )
            else:
                if black_n == white_n:
                    result = "draw"
                else:
                    result = "win"
                    winner_id = (
                        game["black_id"] if black_n > white_n else game["white_id"]
                    )
        else:
            result = "win"

        self.games.pop(gid, None)

        if game["mode"] == "cpu":
            player_id = game["black_id"] or game["white_id"]
            if result == "win":
                payout_amount = int(bet * game["mult"])
                profit = payout_amount - bet
                user = await self.bot.fetch_user(player_id)
                data = await getUser(user)
                data.amount += payout_amount
                await saveUser(data)
                desc_extra = f"あなたの勝ち！\n```patch\n{buildGetAmountText(profit, md=True)}\n```"
                color = discord.Color.green()
            elif result == "draw":
                user = await self.bot.fetch_user(player_id)
                data = await getUser(user)
                data.amount += bet
                await saveUser(data)
                desc_extra = "引き分け！掛け金は返金されました"
                color = discord.Color.gold()
            else:
                reason = "降参しました" if resigned_id else "あなたの負け..."
                desc_extra = (
                    f"{reason}\n```patch\n{buildGetAmountText(-bet, md=True)}\n```"
                )
                color = discord.Color.red()
            view: discord.ui.View | None = OthelloRematchView(
                self, player_id, bet, game["difficulty"], game["player_first"]
            )
        else:
            if result == "win" and winner_id is not None:
                payout_amount = int(bet * 2 * PVP_RAKE)
                profit = payout_amount - bet
                user = await self.bot.fetch_user(winner_id)
                data = await getUser(user)
                data.amount += payout_amount
                await saveUser(data)
                win_text = "の勝ち！"
                if resigned_id:
                    win_text = "の勝ち！（相手が降参）"
                desc_extra = (
                    f"<@{winner_id}>{win_text}\n"
                    f"```patch\n{buildGetAmountText(profit, md=True)}\n```"
                )
                color = discord.Color.green()
            elif result == "draw":
                for uid in (game["black_id"], game["white_id"]):
                    user = await self.bot.fetch_user(uid)
                    data = await getUser(user)
                    data.amount += bet
                    await saveUser(data)
                desc_extra = "引き分け！掛け金は両者に返金されました"
                color = discord.Color.gold()
            else:
                desc_extra = "決着がつきませんでした"
                color = discord.Color.greyple()
            view = None

        embed = discord.Embed(
            title="オセロ [対局終了]",
            description=(
                f"⚫<@{game['black_id']}> vs ⚪<@{game['white_id']}>\n{desc_extra}"
            ),
            color=color,
        )
        embed.add_field(name="⚫ / ⚪", value=f"{black_n} / {white_n}")
        embed.add_field(name="掛け金 (1人あたり)", value=buildAmountText(bet))
        embed.set_image(url=f"attachment://{BOARD_IMAGE_NAME}")
        await message.edit(
            embed=embed,
            attachments=[self.board_file(game)],
            view=view,
        )

    # ----- コマンド -----

    @commands.hybrid_command("othello", brief="オセロでCPUと勝負します")
    @app_commands.rename(bet="掛け金", difficulty="強さ", first="先手")
    @app_commands.describe(
        bet="賭ける額", difficulty="CPUの強さ", first="Trueであなたが先手"
    )
    @app_commands.choices(
        difficulty=[
            app_commands.Choice(name="かんたん (勝ち×1.5)", value="easy"),
            app_commands.Choice(name="ふつう (勝ち×1.9)", value="normal"),
            app_commands.Choice(name="つよい (勝ち×2.5)", value="hard"),
        ]
    )
    @commands.guild_only()
    async def othelloCommand(
        self,
        ctx: commands.Context,
        bet: int = 100,
        difficulty: app_commands.Choice[str] | None = None,
        first: bool = True,
    ):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply(
                "賭けのないオセロほどつまらないものはないよ", ephemeral=True
            )
            return
        if self.is_busy(ctx.author.id):
            await ctx.reply("進行中・募集中のゲームがあります", ephemeral=True)
            return
        user_data = await getUser(ctx.author)
        if user_data.amount < bet:
            raise AmountNotEnough()
        diff = difficulty.value if difficulty else "normal"

        user_data.amount -= bet
        await saveUser(user_data)
        msg = await ctx.reply("対局を準備中...")
        gid = self._new_game_id()
        black_id = ctx.author.id if first else 0
        white_id = 0 if first else ctx.author.id
        self.games[gid] = {
            "board": new_board(),
            "turn": BLACK,
            "black_id": black_id,
            "white_id": white_id,
            "bet": bet,
            "mode": "cpu",
            "difficulty": diff,
            "mult": DIFFICULTY_MULT[diff],
            "player_first": first,
        }
        await self.advance(
            msg,
            self.games[gid],
            gid,
            notice="ゲーム開始！あなたは" + ("⚫先手" if first else "⚪後手"),
        )

    @commands.hybrid_command("othello-vs", brief="オセロで指定メンバーと対戦します")
    @app_commands.rename(bet="掛け金", opponent="相手", host_first="先手後手")
    @app_commands.describe(
        bet="賭ける額 (両者が同額)",
        opponent="対戦相手",
        host_first="どちらが先手(黒)か",
    )
    @app_commands.choices(
        host_first=[
            app_commands.Choice(name="ランダム", value="random"),
            app_commands.Choice(name="自分が先手", value="host"),
            app_commands.Choice(name="相手が先手", value="guest"),
        ]
    )
    @commands.guild_only()
    async def othelloVsCommand(
        self,
        ctx: commands.Context,
        bet: int,
        opponent: discord.Member,
        host_first: app_commands.Choice[str] | None = None,
    ):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply(
                "賭けのないオセロほどつまらないものはないよ", ephemeral=True
            )
            return
        if opponent.bot:
            await ctx.reply("Botとは対戦できません", ephemeral=True)
            return
        if opponent.id == ctx.author.id:
            await ctx.reply("自分自身とは対戦できません", ephemeral=True)
            return
        busy = [
            (uid, label)
            for uid, label in (
                (ctx.author.id, "あなた"),
                (opponent.id, "相手"),
            )
            if self.is_busy(uid)
        ]
        if busy:
            detail = "・".join(
                f"{label}(<@{uid}>: {self.busy_reason(uid)})" for uid, label in busy
            )
            logger.info("othello-vs blocked: %s", detail)
            await ctx.reply(
                f"開始できません: {detail} が進行中・募集中です", ephemeral=True
            )
            return
        user_data = await getUser(ctx.author)
        if user_data.amount < bet:
            raise AmountNotEnough()

        self.lobbies.add(ctx.author.id)
        order = host_first.value if host_first else "random"
        view = ChallengeView(self, ctx.author.id, opponent.id, bet, order)
        self.lobby_views[ctx.author.id] = view
        await ctx.reply("OK", ephemeral=True)
        msg = await ctx.channel.send(
            f"{opponent.mention} さん、<@{ctx.author.id}> からのオセロ対戦指名です！\n"
            f"掛け金: {buildAmountText(bet)} (勝者は {buildAmountText(int(bet * 2 * PVP_RAKE))} を獲得)",
            view=view,
        )
        view.message = msg

    @commands.hybrid_command("othello-open", brief="オセロの対戦相手を募集します")
    @app_commands.rename(bet="掛け金", host_first="先手後手")
    @app_commands.describe(bet="賭ける額 (両者が同額)", host_first="どちらが先手(黒)か")
    @app_commands.choices(
        host_first=[
            app_commands.Choice(name="ランダム", value="random"),
            app_commands.Choice(name="自分が先手", value="host"),
            app_commands.Choice(name="相手が先手", value="guest"),
        ]
    )
    @commands.guild_only()
    async def othelloOpenCommand(
        self,
        ctx: commands.Context,
        bet: int,
        host_first: app_commands.Choice[str] | None = None,
    ):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply(
                "賭けのないオセロほどつまらないものはないよ", ephemeral=True
            )
            return
        if self.is_busy(ctx.author.id):
            await ctx.reply("進行中・募集中のゲームがあります", ephemeral=True)
            return
        user_data = await getUser(ctx.author)
        if user_data.amount < bet:
            raise AmountNotEnough()

        self.lobbies.add(ctx.author.id)
        order = host_first.value if host_first else "random"
        view = OpenLobbyView(self, ctx.author.id, bet, order)
        self.lobby_views[ctx.author.id] = view
        await ctx.reply("OK", ephemeral=True)
        msg = await ctx.channel.send(
            f"{ctx.author.mention} がオセロの対戦相手を募集中！やりたい人はボタンを押してください⚔️\n"
            f"掛け金: {buildAmountText(bet)} (勝者は {buildAmountText(int(bet * 2 * PVP_RAKE))} を獲得)",
            view=view,
        )
        view.message = msg

    @commands.hybrid_command(
        "othello-cancel", brief="自分の募集中ロビーを強制取り消しします"
    )
    @commands.guild_only()
    async def othelloCancelCommand(self, ctx: commands.Context):
        """詰まったロビーが残った場合の復旧用。対局中の取り消しはできない。"""
        self.lobbies.discard(ctx.author.id)
        view = self.lobby_views.pop(ctx.author.id, None)
        if view is None:
            await ctx.reply("取り消せる募集中ロビーはありません", ephemeral=True)
            return
        await view._expire("募集は取り消されました")
        await ctx.reply("募集中ロビーを取り消しました", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(OthelloCog(bot))
