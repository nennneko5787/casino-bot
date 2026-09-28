"""チェス: CPU戦 / 指名対戦 / 参加者募集の3モード。

オセロ同様、動かせる駒に盤面画像右下アルファベット+対応ボタンを振る。
駒選択→移動先選択の2段階で、移動先選択中はキャンセル可能。
キャスリング・アンパサン・プロモーション・チェック/詰み対応 (services/chess_engine)。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import suppress
from sqlite3 import Error as SQLiteError

import discord
from discord import app_commands
from discord.ext import commands

from objects.exceptions import AmountNotEnough, CasinoBaseException, YouMustDie
from services import chess_engine as eng
from services import missions
from services.chess_image import render_chess_image
from services.loan import apply_income, repay_note
from services.message import buildAmountText, buildGetAmountText
from services.money import getUser, saveUser

logger = logging.getLogger(__name__)

DIFFICULTY_MULT = {"easy": 1.5, "normal": 1.9, "hard": 2.5}
DIFFICULTY_NAME = {"easy": "かんたん", "normal": "ふつう", "hard": "つよい"}
PVP_RAKE = 0.95
MOVES_PER_PAGE = 20
BOARD_IMAGE_NAME = "chess.png"
MOVE_TIMEOUT = 86400.0  # 1手あたりの放置制限 (24h。操作のたびにリセットされる)
GAME_LIMIT_SECONDS = 86400.0  # 対局全体の打ち切り (開始から24hで引き分け返金)

_GLYPH = {"K": "♚", "Q": "♛", "R": "♜", "B": "♝", "N": "♞", "P": "♟"}
_PROMO_NAME = {"Q": "クイーン", "R": "ルーク", "B": "ビショップ", "N": "ナイト"}


def move_letter(i: int) -> str:
    return chr(0x1F1E6 + i) if i < 26 else f"[{i + 1}]"


def sq_name(r: int, c: int) -> str:
    return f"{chr(ord('a') + c)}{8 - r}"


def piece_name(board, r: int, c: int) -> str:
    p = board[r][c]
    if p is None:
        return "?"
    side = "白" if p.isupper() else "黒"
    return f"{side}{_GLYPH[p.upper()]}"


class ChessPickButton(discord.ui.Button):
    def __init__(self, label: str, idx: int, row: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, ChessGameView)
        await view.pick(interaction, self.idx)


class ChessPageButton(discord.ui.Button):
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
        assert isinstance(view, ChessGameView)
        await view.flip_page(interaction, self.delta)


class ChessCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="キャンセル", style=discord.ButtonStyle.primary, row=4)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, ChessGameView)
        await view.cancel_pick(interaction)


class ChessResignButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="降参する", style=discord.ButtonStyle.danger, row=4)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, ChessGameView)
        await view.do_resign(interaction)


class ChessPromoButton(discord.ui.Button):
    def __init__(self, label: str, promo: str | None, moves: list, row: int = 0):
        style = discord.ButtonStyle.primary if promo else discord.ButtonStyle.danger
        super().__init__(label=label, style=style, row=row)
        self.promo = promo
        self.moves = moves

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, ChessPromoView)
        await view.choose(interaction, self.promo, self.moves)


class ChessPromoView(discord.ui.View):
    def __init__(self, cog: ChessCog, game_id: int, moves: list):
        super().__init__(timeout=MOVE_TIMEOUT)
        self.cog = cog
        self.game_id = game_id
        self.message: discord.Message | None = None
        for pr in ("Q", "R", "B", "N"):
            self.add_item(ChessPromoButton(_PROMO_NAME[pr], pr, moves))
        self.add_item(ChessPromoButton("キャンセル", None, moves, row=1))

    async def choose(self, interaction, promo: str | None, moves: list):
        game = self.cog.games.get(self.game_id)
        if not game or not await self.cog.check_turn(interaction, game):
            return
        await interaction.response.defer()
        message = interaction.message
        assert message is not None
        if self.cog._expired(game):
            await self.cog.settle(message, game, self.game_id, winner_side=None)
            return
        if promo is None:
            view = ChessGameView(self.cog, self.game_id, phase="piece")
            view.message = interaction.message
            await self.cog.show_board(interaction.message, game, self.game_id, view, notice="キャンセルしました")
            return
        move = next(m for m in moves if m.get("promo") == promo)
        game["state"] = eng.apply_move(game["state"], move)
        note = f"{sq_name(*move['from'])}→{sq_name(*move['to'])}に移動({_PROMO_NAME[promo]}に昇格)"
        await self.cog.advance(interaction.message, game, self.game_id, notice=note)

    async def on_timeout(self):
        pass


class ChessGameView(discord.ui.View):
    def __init__(self, cog: ChessCog, game_id: int, phase: str = "piece",
                 sel=None, page: int = 0, notice: str = ""):
        super().__init__(timeout=MOVE_TIMEOUT)
        self.cog = cog
        self.game_id = game_id
        self.phase = phase
        self.sel = sel
        self.page = page
        self.notice = notice
        self.message: discord.Message | None = None
        self._build()

    def _game(self):
        return self.cog.games.get(self.game_id)

    def _options(self) -> list:
        game = self._game()
        if not game:
            return []
        if self.phase == "piece":
            return eng.movable_pieces(game["state"])
        assert self.sel is not None
        return eng.dests_for(game["state"], self.sel)

    @property
    def page_count(self) -> int:
        return max(1, (len(self._page_items()) + MOVES_PER_PAGE - 1) // MOVES_PER_PAGE)

    def _page_items(self) -> list:
        """ページング対象の一覧。piece=駒マス / dest=移動先グループ(to毎)。"""
        game = self._game()
        if not game:
            return []
        if self.phase == "piece":
            return self._options()
        return self._dest_groups()

    def _build(self):
        items = self._page_items()
        self.page = min(self.page, self.page_count - 1)
        page_items = items[self.page * MOVES_PER_PAGE:(self.page + 1) * MOVES_PER_PAGE]
        for i, _ in enumerate(page_items):
            idx = self.page * MOVES_PER_PAGE + i
            self.add_item(ChessPickButton(move_letter(i), idx, row=i // 5))
        if self.page_count > 1:
            self.add_item(ChessPageButton("◀", -1, disabled=self.page == 0))
            self.add_item(
                ChessPageButton("▶", 1, disabled=self.page >= self.page_count - 1)
            )
        if self.phase == "dest":
            self.add_item(ChessCancelButton())
        self.add_item(ChessResignButton())

    def markers(self) -> dict:
        game = self._game()
        if not game:
            return {}
        items = self._page_items()
        page_items = items[self.page * MOVES_PER_PAGE:(self.page + 1) * MOVES_PER_PAGE]
        marks = {}
        for i, opt in enumerate(page_items):
            sq = opt if self.phase == "piece" else opt[0]["to"]
            marks[sq] = chr(ord("a") + i)
        return marks

    def option_labels(self) -> list[str]:
        game = self._game()
        if not game:
            return []
        board = game["state"]["board"]
        items = self._page_items()
        page_items = items[self.page * MOVES_PER_PAGE:(self.page + 1) * MOVES_PER_PAGE]
        if self.phase == "piece":
            return [f"{sq_name(r, c)}{piece_name(board, r, c)}" for r, c in page_items]
        return [f"{sq_name(*g[0]['to'])}へ"
                + ("(昇格先を選択)" if g[0].get("promo") else "")
                for g in page_items]

    def _dest_groups(self) -> list[list]:
        """to ごとに moves をまとめた一覧 (表示順)。"""
        groups: list[list] = []
        for m in self._options():
            for g in groups:
                if g[0]["to"] == m["to"]:
                    g.append(m)
                    break
            else:
                groups.append([m])
        return groups

    async def flip_page(self, interaction: discord.Interaction, delta: int):
        game = self._game()
        if not game or not await self.cog.check_turn(interaction, game):
            return
        await interaction.response.defer()
        message = interaction.message
        assert message is not None
        if self.cog._expired(game):
            await self.cog.settle(message, game, self.game_id, winner_side=None)
            return
        view = ChessGameView(
            self.cog, self.game_id, phase=self.phase, sel=self.sel,
            page=self.page + delta, notice=self.notice,
        )
        view.message = message
        await self.cog.show_board(message, game, self.game_id, view, notice=view.notice)

    async def pick(self, interaction: discord.Interaction, idx: int):
        game = self._game()
        if not game or not await self.cog.check_turn(interaction, game):
            return
        await interaction.response.defer()
        message = interaction.message
        assert message is not None
        if self.cog._expired(game):
            await self.cog.settle(message, game, self.game_id, winner_side=None)
            return
        if self.phase == "piece":
            opts = self._options()
            if idx >= len(opts):
                await interaction.followup.send("その手は選べません", ephemeral=True)
                return
            sel = opts[idx]
            dests = eng.dests_for(game["state"], sel)
            if not dests:
                await interaction.followup.send("その駒は動けません", ephemeral=True)
                return
            board = game["state"]["board"]
            view = ChessGameView(self.cog, self.game_id, phase="dest", sel=sel,
                                 notice=f"{sq_name(*sel)}{piece_name(board, *sel)}を選択中。移動先を選んでね(キャンセル可)")
            view.message = message
            await self.cog.show_board(message, game, self.game_id, view, notice=view.notice)
        else:
            groups = self._dest_groups()
            if idx >= len(groups):
                await interaction.followup.send("その手は選べません", ephemeral=True)
                return
            same = groups[idx]
            if len(same) > 1 and same[0].get("promo"):
                view = ChessPromoView(self.cog, self.game_id, same)
                view.message = message
                self.cog._set_view(game, view)
                marks = {same[0]["to"]: "a"}
                await message.edit(
                    embed=self.cog.build_game_embed(game, marks, f"{sq_name(*same[0]['to'])}で何に昇格する?"),
                    attachments=[self.cog.board_file(game, marks)],
                    view=view,
                )
                return
            game["state"] = eng.apply_move(game["state"], same[0])
            m = same[0]
            note = f"{sq_name(*m['from'])}→{sq_name(*m['to'])}に移動"
            await self.cog.advance(message, game, self.game_id, notice=note)

    async def cancel_pick(self, interaction: discord.Interaction):
        game = self._game()
        if not game or not await self.cog.check_turn(interaction, game):
            return
        await interaction.response.defer()
        message = interaction.message
        assert message is not None
        view = ChessGameView(self.cog, self.game_id, phase="piece", notice="キャンセルしました。駒を選び直してね")
        view.message = message
        await self.cog.show_board(message, game, self.game_id, view, notice=view.notice)

    async def do_resign(self, interaction: discord.Interaction):
        game = self._game()
        if not game:
            await interaction.response.send_message("このゲームは既に終了しています", ephemeral=True)
            return
        if interaction.user.id not in (game["white_id"], game["black_id"]):
            await interaction.response.send_message("このゲームの参加者ではありません", ephemeral=True)
            return
        await interaction.response.defer()
        message = interaction.message
        assert message is not None
        await self.cog.settle(message, game, self.game_id, resigned_id=interaction.user.id)

    async def on_timeout(self):
        game = self.cog.games.get(self.game_id)
        if game is not None and game.get("view") is not self:
            return  # 旧Viewの遅延発火。現行の対局には触らない
        game = self.cog.games.pop(self.game_id, None)
        if game:
            for uid in {game["white_id"], game["black_id"]}:
                if uid == 0:
                    continue
                with suppress(discord.DiscordException, SQLiteError, CasinoBaseException):
                    await apply_income(uid, game["bet"])
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            with suppress(discord.DiscordException):
                await self.message.edit(content="タイムアウトのため中止しました（掛け金は返金）", view=self)


class ChessChallengeView(discord.ui.View):
    def __init__(self, cog: ChessCog, host_id: int, guest_id: int, bet: int, host_first: str):
        super().__init__(timeout=MOVE_TIMEOUT)
        self.cog = cog
        self.host_id = host_id
        self.guest_id = guest_id
        self.bet = bet
        self.host_first = host_first
        self.message: discord.Message | None = None

    @discord.ui.button(label="参加する", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.guest_id:
            await interaction.response.send_message("あなたへの指名ではありません", ephemeral=True)
            return
        self.cog.lobbies.discard(self.host_id)
        self.cog.lobby_views.pop(self.host_id, None)
        if self.cog.is_busy(self.host_id) or self.cog.is_busy(self.guest_id):
            await interaction.response.send_message("対戦者の都合で開始できませんでした", ephemeral=True)
            await self._expire("対戦を開始できませんでした")
            return
        host_data = await getUser(await self.cog.bot.fetch_user(self.host_id))
        guest_data = await getUser(interaction.user)
        if host_data.amount < self.bet or guest_data.amount < self.bet:
            await interaction.response.send_message("残高が足りないため開始できません", ephemeral=True)
            await self._expire("残高不足のため中止しました")
            return
        await interaction.response.defer()
        host_data.amount -= self.bet
        guest_data.amount -= self.bet
        await saveUser(host_data)
        await saveUser(guest_data)
        lock = self.host_first == "host" if self.host_first in ("host", "guest") else random.random() < 0.5
        message = interaction.message
        assert message is not None
        await self.cog.start_pvp(message, self.host_id, self.guest_id, self.bet, lock)
        self.stop()

    @discord.ui.button(label="断る", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.guest_id:
            await interaction.response.send_message("あなたへの指名ではありません", ephemeral=True)
            return
        await interaction.response.defer()
        self.cog.lobbies.discard(self.host_id)
        self.cog.lobby_views.pop(self.host_id, None)
        await self._expire("指名は断られました")

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


class ChessOpenLobbyView(discord.ui.View):
    def __init__(self, cog: ChessCog, host_id: int, bet: int, host_first: str):
        super().__init__(timeout=MOVE_TIMEOUT)
        self.cog = cog
        self.host_id = host_id
        self.bet = bet
        self.host_first = host_first
        self.message: discord.Message | None = None

    @discord.ui.button(label="対戦する！", style=discord.ButtonStyle.success, emoji="⚔️")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.bot:
            await interaction.response.send_message("Botは参加できません", ephemeral=True)
            return
        if interaction.user.id == self.host_id:
            await interaction.response.send_message("主催者は参加ボタンではなく開始を待ってください", ephemeral=True)
            return
        self.cog.lobbies.discard(self.host_id)
        self.cog.lobby_views.pop(self.host_id, None)
        if self.cog.is_busy(interaction.user.id) or self.cog.is_busy(self.host_id):
            await interaction.response.send_message("対戦を開始できませんでした", ephemeral=True)
            await self._expire("対戦を開始できませんでした")
            return
        host_data = await getUser(await self.cog.bot.fetch_user(self.host_id))
        guest_data = await getUser(interaction.user)
        if host_data.amount < self.bet or guest_data.amount < self.bet:
            await interaction.response.send_message("残高が足りないため開始できません", ephemeral=True)
            await self._expire("残高不足のため中止しました")
            return
        await interaction.response.defer()
        host_data.amount -= self.bet
        guest_data.amount -= self.bet
        await saveUser(host_data)
        await saveUser(guest_data)
        if self.host_first == "host":
            host_white = True
        elif self.host_first == "guest":
            host_white = False
        else:
            host_white = random.random() < 0.5
        white = self.host_id if host_white else interaction.user.id
        black = interaction.user.id if host_white else self.host_id
        message = interaction.message
        assert message is not None
        await self.cog.start_pvp_direct(message, white, black, self.bet)
        self.stop()

    @discord.ui.button(label="募集を取り消す", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host_id:
            await interaction.response.send_message("主催者のみ取り消せます", ephemeral=True)
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


class ChessRematchView(discord.ui.View):
    def __init__(self, cog: ChessCog, author_id: int, bet: int, difficulty: str, player_first: bool):
        super().__init__(timeout=MOVE_TIMEOUT)
        self.cog = cog
        self.author_id = author_id
        self.bet = bet
        self.difficulty = difficulty
        self.player_first = player_first

    @discord.ui.button(label="もう1度プレイ", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("あなたのゲームではありません", ephemeral=True)
            return
        if self.cog.is_busy(self.author_id):
            await interaction.response.send_message("進行中のゲームがあります", ephemeral=True)
            return
        user_data = await getUser(interaction.user)
        if user_data.amount < self.bet:
            await interaction.response.send_message(f"残高が足りません\n\n残高: {buildAmountText(user_data.amount)}", ephemeral=True)
            return
        await interaction.response.defer()
        user_data.amount -= self.bet
        await saveUser(user_data)
        message = interaction.message
        assert message is not None
        await self.cog.start_cpu(message, self.author_id, self.bet, self.difficulty, self.player_first)


class ChessCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: dict[int, dict] = {}
        self.lobbies: set[int] = set()
        self.lobby_views: dict[int, ChessChallengeView | ChessOpenLobbyView] = {}
        self._next_game_id = 1

    def busy_reason(self, user_id: int) -> str | None:
        if user_id in self.lobbies:
            return "募集中"
        if any(g["white_id"] == user_id or g["black_id"] == user_id for g in self.games.values()):
            return "対局中"
        return None

    def is_busy(self, user_id: int) -> bool:
        return self.busy_reason(user_id) is not None

    def _new_game_id(self) -> int:
        gid = self._next_game_id
        self._next_game_id += 1
        return gid

    @staticmethod
    def _set_view(game: dict, view: discord.ui.View | None) -> None:
        """現行Viewを差し替え、古いViewの放置タイマーを止める。

        切替前のViewを止めないと、古いタイマーが後から発火して
        進行中の対局を中止・返金・上書きしてしまう。
        """
        old = game.get("view")
        if old is not None and old is not view:
            with suppress(Exception):
                old.stop()
        game["view"] = view

    @staticmethod
    def _expired(game: dict) -> bool:
        """開始から24h超過ならTrue (対局全体の打ち切り)。"""
        return time.monotonic() - game.get("started_at", time.monotonic()) >= GAME_LIMIT_SECONDS

    async def check_turn(self, interaction: discord.Interaction, game: dict) -> bool:
        if interaction.user.id not in (game["white_id"], game["black_id"]):
            await interaction.response.send_message("このゲームの参加者ではありません", ephemeral=True)
            return False
        turn_id = game["white_id"] if game["state"]["turn"] == "w" else game["black_id"]
        if interaction.user.id != turn_id:
            await interaction.response.send_message("あなたの手番ではありません", ephemeral=True)
            return False
        return True

    @staticmethod
    def board_file(game: dict, markers: dict | None = None) -> discord.File:
        buf = render_chess_image(game["state"]["board"], markers or {})
        return discord.File(buf, filename=BOARD_IMAGE_NAME)

    def build_game_embed(self, game: dict, markers: dict, notice: str = "") -> discord.Embed:
        white_id, black_id = game["white_id"], game["black_id"]
        turn_id = white_id if game["state"]["turn"] == "w" else black_id
        if game["mode"] == "cpu":
            player_id = white_id or black_id
            title = f"チェス [CPU: {DIFFICULTY_NAME[game['difficulty']]}]♔♚"
            desc_head = f"<@{player_id}> vs 🤖CPU"
            payout_text = buildAmountText(int(game["bet"] * game["mult"]))
        else:
            title = "チェス [対人戦]♔♚"
            desc_head = f"⬜<@{white_id}> vs ⬛<@{black_id}>"
            payout_text = buildAmountText(int(game["bet"] * 2 * PVP_RAKE))
        turn_text = "🤖CPU" if turn_id == 0 else f"<@{turn_id}>"
        check = " (チェック！)" if eng.in_check_board(game["state"]["board"], game["state"]["turn"], game["state"]["ep"]) else ""
        desc = f"{desc_head}\n手番: {turn_text}{check}"
        if notice:
            desc += f"\n{notice}"
        embed = discord.Embed(title=title, description=desc, color=discord.Color.random())
        embed.add_field(name="掛け金 (1人あたり)", value=buildAmountText(game["bet"]))
        embed.add_field(name="勝ち時ペイアウト", value=payout_text)
        embed.set_footer(text="駒ボタンを押して選択→移動先ボタンで移動。移動先選択中はキャンセル可。1手・対局とも24hで打切。")
        embed.set_image(url=f"attachment://{BOARD_IMAGE_NAME}")
        return embed

    def _options_text(self, view: ChessGameView) -> str:
        labels = view.option_labels()
        lines = []
        for i, lab in enumerate(labels):
            lines.append(f"{move_letter(i)}: {lab}")
        if view.page_count > 1:
            lines.append(f"(p.{view.page + 1}/{view.page_count} ◀▶で切替)")
        return "\n".join(lines) if lines else "選択肢がありません"

    async def show_board(self, message: discord.Message, game: dict, gid: int, view: ChessGameView, notice: str = ""):
        self._set_view(game, view)
        markers = view.markers()
        embed = self.build_game_embed(game, markers, notice)
        kind = "駒を選んでね" if view.phase == "piece" else "移動先を選んでね(キャンセル可)"
        embed.add_field(name=f"選択肢 ({kind})", value=self._options_text(view)[:1000], inline=False)
        await message.edit(embed=embed, attachments=[self.board_file(game, markers)], view=view)

    async def start_cpu(self, message: discord.Message, player_id: int, bet: int, difficulty: str, player_first: bool):
        gid = self._new_game_id()
        self.games[gid] = {
            "state": eng.new_state(),
            "white_id": player_id if player_first else 0,
            "black_id": 0 if player_first else player_id,
            "bet": bet, "mode": "cpu", "difficulty": difficulty,
            "mult": DIFFICULTY_MULT[difficulty], "player_first": player_first,
            "started_at": time.monotonic(), "view": None,
        }
        with suppress(Exception):
            await missions.record_event(player_id, "game")
        await self.advance(message, self.games[gid], gid,
                           notice="ゲーム開始！あなたは" + ("⬜白(先手)" if player_first else "⬛黒(後手)"))

    async def start_pvp_direct(self, message: discord.Message, white_id: int, black_id: int, bet: int):
        gid = self._new_game_id()
        self.games[gid] = {"state": eng.new_state(), "white_id": white_id,
                           "black_id": black_id, "bet": bet, "mode": "pvp",
                           "started_at": time.monotonic(), "view": None}
        with suppress(Exception):
            await missions.record_event(white_id, "game")
            await missions.record_event(black_id, "game")
        await self.advance(message, self.games[gid], gid, notice="対戦開始！⬜白からです")

    async def start_pvp(self, message, host_id, guest_id, bet, host_white: bool):
        await self.start_pvp_direct(message, host_id if host_white else guest_id,
                                    guest_id if host_white else host_id, bet)

    async def advance(self, message: discord.Message, game: dict, gid: int, notice: str = ""):
        if self._expired(game):
            await self.settle(message, game, gid, winner_side=None)
            return
        while True:
            over, winner = eng.is_game_over(game["state"])
            if over:
                await self.settle(message, game, gid, winner_side=winner)
                return
            turn_id = game["white_id"] if game["state"]["turn"] == "w" else game["black_id"]
            if turn_id == 0:
                await asyncio.sleep(0.8)
                difficulty = game["difficulty"]
                if difficulty == "hard":
                    move = await asyncio.to_thread(eng.cpu_choose, game["state"], difficulty)
                else:
                    move = eng.cpu_choose(game["state"], difficulty)
                game["state"] = eng.apply_move(game["state"], move)
                notice = f"🤖CPUは{sq_name(*move['from'])}→{sq_name(*move['to'])}に移動"
                continue
            view = ChessGameView(self, gid, notice=notice)
            view.message = message
            await self.show_board(message, game, gid, view, notice=notice)
            return

    async def settle(self, message, game, gid, winner_side: str | None = None, resigned_id: int | None = None):
        self._set_view(game, None)
        bet = game["bet"]
        self.games.pop(gid, None)
        if resigned_id is not None:
            if game["mode"] == "cpu":
                result = "lose"
            else:
                winner_id = game["black_id"] if resigned_id == game["white_id"] else game["white_id"]
                result = "win"
        elif winner_side is None:
            result = "draw"
            winner_id = None
        else:
            winner_id = game["white_id"] if winner_side == "w" else game["black_id"]
            if game["mode"] == "cpu":
                result = "win" if winner_id != 0 else "lose"
            else:
                result = "win"

        if game["mode"] == "cpu":
            player_id = game["white_id"] or game["black_id"]
            if result == "win":
                payout_amount = int(bet * game["mult"])
                profit = payout_amount - bet
                repaid, _ = await apply_income(player_id, payout_amount)
                desc_extra = f"あなたの勝ち！\n```patch\n{buildGetAmountText(profit, md=True)}\n```" + repay_note(repaid)
                color = discord.Color.green()
            elif result == "draw":
                repaid, _ = await apply_income(player_id, bet)
                desc_extra = "引き分け！掛け金は返金されました" + repay_note(repaid)
                color = discord.Color.gold()
            else:
                reason = "降参しました" if resigned_id else "あなたの負け..."
                desc_extra = f"{reason}\n```patch\n{buildGetAmountText(-bet, md=True)}\n```"
                color = discord.Color.red()
            view: discord.ui.View | None = ChessRematchView(self, player_id, bet, game["difficulty"], game["player_first"])
        else:
            if result == "win":
                assert winner_id is not None
                payout_amount = int(bet * 2 * PVP_RAKE)
                profit = payout_amount - bet
                repaid, _ = await apply_income(winner_id, payout_amount)
                win_text = "の勝ち！" + ("（相手が降参）" if resigned_id else "")
                desc_extra = f"<@{winner_id}>{win_text}\n```patch\n{buildGetAmountText(profit, md=True)}\n```" + repay_note(repaid)
                color = discord.Color.green()
            else:
                for uid in (game["white_id"], game["black_id"]):
                    await apply_income(uid, bet)
                desc_extra = "引き分け！掛け金は両者に返金されました"
                color = discord.Color.gold()
            view = None

        embed = discord.Embed(title="チェス [対局終了]",
                              description=f"⬜<@{game['white_id']}> vs ⬛<@{game['black_id']}>\n{desc_extra}",
                              color=color)
        embed.add_field(name="掛け金 (1人あたり)", value=buildAmountText(bet))
        embed.set_image(url=f"attachment://{BOARD_IMAGE_NAME}")
        await message.edit(embed=embed, attachments=[self.board_file(game)], view=view)

    @commands.hybrid_command("chess", brief="チェスでCPUと勝負します")
    @app_commands.rename(bet="掛け金", difficulty="強さ", first="先手")
    @app_commands.describe(bet="賭ける額", difficulty="CPUの強さ", first="Trueであなたが白(先手)")
    @app_commands.choices(difficulty=[
        app_commands.Choice(name="かんたん (勝ち×1.5)", value="easy"),
        app_commands.Choice(name="ふつう (勝ち×1.9)", value="normal"),
        app_commands.Choice(name="つよい (勝ち×2.5)", value="hard"),
    ])
    @commands.guild_only()
    async def chessCommand(self, ctx: commands.Context, bet: int = 100,
                           difficulty: app_commands.Choice[str] | None = None, first: bool = True):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply("賭けのないチェスほどつまらないものはないよ", ephemeral=True)
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
        self.games[gid] = {
            "state": eng.new_state(),
            "white_id": ctx.author.id if first else 0,
            "black_id": 0 if first else ctx.author.id,
            "bet": bet, "mode": "cpu", "difficulty": diff,
            "mult": DIFFICULTY_MULT[diff], "player_first": first,
            "started_at": time.monotonic(), "view": None,
        }
        with suppress(Exception):
            await missions.record_event(ctx.author.id, "game")
        await self.advance(msg, self.games[gid], gid,
                           notice="ゲーム開始！あなたは" + ("⬜白(先手)" if first else "⬛黒(後手)"))

    @commands.hybrid_command("chess-vs", brief="チェスで指定メンバーと対戦します")
    @app_commands.rename(bet="掛け金", opponent="相手", host_first="先手後手")
    @app_commands.describe(bet="賭ける額 (両者が同額)", opponent="対戦相手", host_first="どちらが白(先手)か")
    @app_commands.choices(host_first=[
        app_commands.Choice(name="ランダム", value="random"),
        app_commands.Choice(name="自分が白", value="host"),
        app_commands.Choice(name="相手が白", value="guest"),
    ])
    @commands.guild_only()
    async def chessVsCommand(self, ctx: commands.Context, bet: int, opponent: discord.Member,
                             host_first: app_commands.Choice[str] | None = None):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply("賭けのないチェスほどつまらないものはないよ", ephemeral=True)
            return
        if opponent.bot:
            await ctx.reply("Botとは対戦できません", ephemeral=True)
            return
        if opponent.id == ctx.author.id:
            await ctx.reply("自分自身とは対戦できません", ephemeral=True)
            return
        busy = [(uid, label) for uid, label in ((ctx.author.id, "あなた"), (opponent.id, "相手")) if self.is_busy(uid)]
        if busy:
            detail = "・".join(f"{label}(<@{uid}>: {self.busy_reason(uid)})" for uid, label in busy)
            await ctx.reply(f"開始できません: {detail} が進行中・募集中です", ephemeral=True)
            return
        user_data = await getUser(ctx.author)
        if user_data.amount < bet:
            raise AmountNotEnough()
        self.lobbies.add(ctx.author.id)
        order = host_first.value if host_first else "random"
        view = ChessChallengeView(self, ctx.author.id, opponent.id, bet, order)
        self.lobby_views[ctx.author.id] = view
        await ctx.reply("OK", ephemeral=True)
        msg = await ctx.channel.send(
            f"{opponent.mention} さん、<@{ctx.author.id}> からのチェス対戦指名です！\n"
            f"掛け金: {buildAmountText(bet)} (勝者は {buildAmountText(int(bet * 2 * PVP_RAKE))} を獲得)",
            view=view)
        view.message = msg

    @commands.hybrid_command("chess-open", brief="チェスの対戦相手を募集します")
    @app_commands.rename(bet="掛け金", host_first="先手後手")
    @app_commands.describe(bet="賭ける額 (両者が同額)", host_first="どちらが白(先手)か")
    @app_commands.choices(host_first=[
        app_commands.Choice(name="ランダム", value="random"),
        app_commands.Choice(name="自分が白", value="host"),
        app_commands.Choice(name="相手が白", value="guest"),
    ])
    @commands.guild_only()
    async def chessOpenCommand(self, ctx: commands.Context, bet: int,
                               host_first: app_commands.Choice[str] | None = None):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply("賭けのないチェスほどつまらないものはないよ", ephemeral=True)
            return
        if self.is_busy(ctx.author.id):
            await ctx.reply("進行中・募集中のゲームがあります", ephemeral=True)
            return
        user_data = await getUser(ctx.author)
        if user_data.amount < bet:
            raise AmountNotEnough()
        self.lobbies.add(ctx.author.id)
        order = host_first.value if host_first else "random"
        view = ChessOpenLobbyView(self, ctx.author.id, bet, order)
        self.lobby_views[ctx.author.id] = view
        await ctx.reply("OK", ephemeral=True)
        msg = await ctx.channel.send(
            f"{ctx.author.mention} がチェスの対戦相手を募集中！やりたい人はボタンを押してください⚔️\n"
            f"掛け金: {buildAmountText(bet)} (勝者は {buildAmountText(int(bet * 2 * PVP_RAKE))} を獲得)",
            view=view)
        view.message = msg

    @commands.hybrid_command("chess-cancel", brief="自分の募集中ロビーを強制取り消しします")
    @commands.guild_only()
    async def chessCancelCommand(self, ctx: commands.Context):
        self.lobbies.discard(ctx.author.id)
        view = self.lobby_views.pop(ctx.author.id, None)
        if view is None:
            await ctx.reply("取り消せる募集中ロビーはありません", ephemeral=True)
            return
        await view._expire("募集は取り消されました")
        await ctx.reply("募集中ロビーを取り消しました", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChessCog(bot))
