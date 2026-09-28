"""将棋: CPU戦 / 指名対戦 / 参加者募集の3モード。

オセロ同様、動かせる駒に盤面画像右下アルファベット+対応ボタンを振る。
駒選択→移動先選択の2段階で、移動先選択中はキャンセル可能。
持ち駒打ち・成り・王手/詰み対応 (services/shogi_engine)。
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
from services import missions
from services import shogi_engine as eng
from services.loan import apply_income, repay_note
from services.message import buildAmountText, buildGetAmountText
from services.money import getUser, saveUser
from services.shogi_image import render_shogi_image

logger = logging.getLogger(__name__)

DIFFICULTY_MULT = {"easy": 1.5, "normal": 1.9, "hard": 2.5}
DIFFICULTY_NAME = {"easy": "かんたん", "normal": "ふつう", "hard": "つよい"}
PVP_RAKE = 0.95
MOVES_PER_PAGE = 20
BOARD_IMAGE_NAME = "shogi.png"
RANK_KANJI = "一二三四五六七八九"
MOVE_TIMEOUT = 86400.0  # 1手あたりの放置制限 (24h。操作のたびにリセットされる)
GAME_LIMIT_SECONDS = 86400.0  # 対局全体の打ち切り (開始から24hで引き分け返金)

_KANJI = {
    "P": "歩", "L": "香", "N": "桂", "S": "銀", "G": "金",
    "B": "角", "R": "飛", "K": "王",
    "+P": "と", "+L": "杏", "+N": "圭", "+S": "全",
    "+B": "馬", "+R": "竜",
}


def move_letter(i: int) -> str:
    return chr(0x1F1E6 + i) if i < 26 else f"[{i + 1}]"


def sq_name(r: int, c: int) -> str:
    return f"{9 - c}{RANK_KANJI[r]}"


def sel_name(state: dict, sel) -> str:
    if sel[0] == "hand":
        return f"持{_KANJI.get(sel[1], '?')}"
    r, c = sel
    cell = state["board"][r][c]
    k = cell[1] if cell else "?"
    return f"{sq_name(r, c)}{_KANJI.get(k, '?')}"


class ShogiPickButton(discord.ui.Button):
    def __init__(self, label: str, idx: int, row: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, ShogiGameView)
        await view.pick(interaction, self.idx)


class ShogiCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="キャンセル", style=discord.ButtonStyle.primary, row=4)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, ShogiGameView)
        await view.cancel_pick(interaction)


class ShogiResignButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="降参する", style=discord.ButtonStyle.danger, row=4)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, ShogiGameView)
        await view.do_resign(interaction)


class ShogiPromoteButton(discord.ui.Button):
    def __init__(self, label: str, promote: bool | None, moves: list):
        style = (
            discord.ButtonStyle.primary if promote
            else discord.ButtonStyle.secondary if promote is False
            else discord.ButtonStyle.danger
        )
        super().__init__(label=label, style=style, row=0)
        self.promote = promote
        self.moves = moves

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        assert isinstance(view, ShogiPromoView)
        await view.choose(interaction, self.promote, self.moves)


class ShogiPromoView(discord.ui.View):
    """成る/不成の選択。キャンセルで駒選択に戻る。"""

    def __init__(self, cog: ShogiCog, game_id: int, moves: list):
        super().__init__(timeout=MOVE_TIMEOUT)
        self.cog = cog
        self.game_id = game_id
        self.message: discord.Message | None = None
        for label, pr in (("成る", True), ("不成", False)):
            self.add_item(ShogiPromoteButton(label, pr, moves))
        self.add_item(ShogiPromoteButton("キャンセル", None, moves))

    async def choose(self, interaction, promote: bool | None, moves: list):
        game = self.cog.games.get(self.game_id)
        if not game or not await self.cog.check_turn(interaction, game):
            return
        await interaction.response.defer()
        message = interaction.message
        assert message is not None
        if self.cog._expired(game):
            await self.cog.settle(message, game, self.game_id, winner_color=None)
            return
        if promote is None:
            view = ShogiGameView(self.cog, self.game_id, phase="piece")
            view.message = interaction.message
            await self.cog.show_board(interaction.message, game, self.game_id, view, notice="キャンセルしました")
            return
        move = next(m for m in moves if m.get("promote") == promote)
        game["state"] = eng.apply_move(game["state"], move)
        fr = move["from"]
        if fr is None:
            note = f"{_KANJI.get(move['kind'], '?')}を{sq_name(*move['to'])}に打った"
        else:
            note = f"{sq_name(*fr)}→{sq_name(*move['to'])}に移動" + ("(成)" if promote else "")
        await self.cog.advance(interaction.message, game, self.game_id, notice=note)

    async def on_timeout(self):
        pass


class ShogiGameView(discord.ui.View):
    """phase=piece: 駒選択 / phase=dest: 移動先選択(キャンセル可)。"""

    def __init__(self, cog: ShogiCog, game_id: int, phase: str = "piece",
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
        return eng.dests_for(game["state"], self.sel)

    @property
    def page_count(self) -> int:
        return max(1, (len(self._options()) + MOVES_PER_PAGE - 1) // MOVES_PER_PAGE)

    def _build(self):
        opts = self._options()
        self.page = min(self.page, self.page_count - 1)
        page_opts = opts[self.page * MOVES_PER_PAGE:(self.page + 1) * MOVES_PER_PAGE]
        for i, _ in enumerate(page_opts):
            idx = self.page * MOVES_PER_PAGE + i
            self.add_item(ShogiPickButton(move_letter(idx), idx, row=i // 5))
        if self.phase == "dest":
            self.add_item(ShogiCancelButton())
        self.add_item(ShogiResignButton())

    def markers(self) -> dict:
        game = self._game()
        if not game:
            return {}
        if self.phase == "piece":
            marks = {}
            for i, sel in enumerate(self._options()):
                if isinstance(sel, tuple) and sel and sel[0] != "hand" and i < 26:
                    marks[sel] = chr(ord("a") + i)
            return marks
        marks = {}
        for i, m in enumerate(self._options()):
            if i < 26:
                marks[m["to"]] = chr(ord("a") + i)
        return marks

    def option_labels(self) -> list[str]:
        game = self._game()
        if not game:
            return []
        if self.phase == "piece":
            return [sel_name(game["state"], s) for s in self._options()]
        labels = []
        for m in self._options():
            to = sq_name(*m["to"])
            if m["from"] is None:
                labels.append(f"{to}に打つ")
            else:
                labels.append(f"{to}へ" + ("(成/不成選択)" if _needs_promo_choice(game['state'], m) else ""))
        return labels

    async def pick(self, interaction: discord.Interaction, idx: int):
        game = self._game()
        if not game or not await self.cog.check_turn(interaction, game):
            return
        opts = self._options()
        if idx >= len(opts):
            await interaction.response.send_message("その手は選べません", ephemeral=True)
            return
        await interaction.response.defer()
        message = interaction.message
        assert message is not None
        if self.cog._expired(game):
            await self.cog.settle(message, game, self.game_id, winner_color=None)
            return
        if self.phase == "piece":
            sel = opts[idx]
            dests = eng.dests_for(game["state"], sel)
            if not dests:
                await interaction.followup.send("その駒は動けません", ephemeral=True)
                return
            view = ShogiGameView(self.cog, self.game_id, phase="dest", sel=sel,
                                 notice=f"{sel_name(game['state'], sel)}を選択中。移動先を選んでね(キャンセル可)")
            view.message = message
            await self.cog.show_board(message, game, self.game_id, view, notice=view.notice)
        else:
            # dests_for の要素は move dict。同じ to で成/不成が分かれる場合あり
            same = [m for m in opts if m["to"] == opts[idx]["to"]]
            if len(same) > 1:
                view = ShogiPromoView(self.cog, self.game_id, same)
                view.message = message
                self.cog._set_view(game, view)
                marks = {same[0]["to"]: "a"}
                await message.edit(
                    embed=self.cog.build_game_embed(game, marks, f"{sq_name(*same[0]['to'])}で成る?"),
                    attachments=[self.cog.board_file(game, marks)],
                    view=view,
                )
                return
            game["state"] = eng.apply_move(game["state"], opts[idx])
            m = opts[idx]
            if m["from"] is None:
                note = f"{_KANJI.get(m['kind'], '?')}を{sq_name(*m['to'])}に打った"
            else:
                note = f"{sq_name(*m['from'])}→{sq_name(*m['to'])}に移動" + ("(成)" if m.get("promote") else "")
            await self.cog.advance(message, game, self.game_id, notice=note)

    async def cancel_pick(self, interaction: discord.Interaction):
        game = self._game()
        if not game or not await self.cog.check_turn(interaction, game):
            return
        await interaction.response.defer()
        message = interaction.message
        assert message is not None
        view = ShogiGameView(self.cog, self.game_id, phase="piece", notice="キャンセルしました。駒を選び直してね")
        view.message = message
        await self.cog.show_board(message, game, self.game_id, view, notice=view.notice)

    async def do_resign(self, interaction: discord.Interaction):
        game = self._game()
        if not game:
            await interaction.response.send_message("このゲームは既に終了しています", ephemeral=True)
            return
        if interaction.user.id not in (game["sente_id"], game["gote_id"]):
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
            for uid in {game["sente_id"], game["gote_id"]}:
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


def _needs_promo_choice(state: dict, move: dict) -> bool:
    if move["from"] is None or move.get("promote"):
        return False
    same_to = [m for m in eng.dests_for(state, move["from"]) if m["to"] == move["to"]]
    return len(same_to) > 1


class ShogiChallengeView(discord.ui.View):
    def __init__(self, cog: ShogiCog, host_id: int, guest_id: int, bet: int, host_first: str):
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


class ShogiOpenLobbyView(discord.ui.View):
    def __init__(self, cog: ShogiCog, host_id: int, bet: int, host_first: str):
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
            host_sente = True
        elif self.host_first == "guest":
            host_sente = False
        else:
            host_sente = random.random() < 0.5
        sente = self.host_id if host_sente else interaction.user.id
        gote = interaction.user.id if host_sente else self.host_id
        message = interaction.message
        assert message is not None
        await self.cog.start_pvp_direct(message, sente, gote, self.bet)
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


class ShogiRematchView(discord.ui.View):
    def __init__(self, cog: ShogiCog, author_id: int, bet: int, difficulty: str, player_first: bool):
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


class ShogiCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: dict[int, dict] = {}
        self.lobbies: set[int] = set()
        self.lobby_views: dict[int, ShogiChallengeView | ShogiOpenLobbyView] = {}
        self._next_game_id = 1

    def busy_reason(self, user_id: int) -> str | None:
        if user_id in self.lobbies:
            return "募集中"
        if any(g["sente_id"] == user_id or g["gote_id"] == user_id for g in self.games.values()):
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
        if interaction.user.id not in (game["sente_id"], game["gote_id"]):
            await interaction.response.send_message("このゲームの参加者ではありません", ephemeral=True)
            return False
        turn_id = game["sente_id"] if game["state"]["turn"] == 0 else game["gote_id"]
        if interaction.user.id != turn_id:
            await interaction.response.send_message("あなたの手番ではありません", ephemeral=True)
            return False
        return True

    @staticmethod
    def board_file(game: dict, markers: dict | None = None) -> discord.File:
        buf = render_shogi_image(game["state"]["board"], game["state"]["hands"], markers or {}, game["state"]["turn"])
        return discord.File(buf, filename=BOARD_IMAGE_NAME)

    def build_game_embed(self, game: dict, markers: dict, notice: str = "") -> discord.Embed:
        sente_id, gote_id = game["sente_id"], game["gote_id"]
        turn_id = sente_id if game["state"]["turn"] == 0 else gote_id
        if game["mode"] == "cpu":
            player_id = sente_id or gote_id
            title = f"将棋 [CPU: {DIFFICULTY_NAME[game['difficulty']]}]☖☗"
            desc_head = f"<@{player_id}> vs 🤖CPU"
            payout_text = buildAmountText(int(game["bet"] * game["mult"]))
        else:
            title = "将棋 [対人戦]☖☗"
            desc_head = f"☗<@{sente_id}> vs ☖<@{gote_id}>"
            payout_text = buildAmountText(int(game["bet"] * 2 * PVP_RAKE))
        turn_text = "🤖CPU" if turn_id == 0 else f"<@{turn_id}>"
        check = " (王手！)" if eng.in_check(game["state"]["board"], game["state"]["turn"]) else ""
        desc = f"{desc_head}\n手番: {turn_text}{check}"
        if notice:
            desc += f"\n{notice}"
        embed = discord.Embed(title=title, description=desc, color=discord.Color.random())
        embed.add_field(name="掛け金 (1人あたり)", value=buildAmountText(game["bet"]))
        embed.add_field(name="勝ち時ペイアウト", value=payout_text)
        embed.set_footer(text="駒ボタンを押して選択→移動先ボタンで移動。移動先選択中はキャンセル可。1手・対局とも24hで打切。")
        embed.set_image(url=f"attachment://{BOARD_IMAGE_NAME}")
        return embed

    def _options_text(self, view: ShogiGameView) -> str:
        labels = view.option_labels()
        lines = []
        for i, lab in enumerate(labels[view.page * MOVES_PER_PAGE:(view.page + 1) * MOVES_PER_PAGE]):
            idx = view.page * MOVES_PER_PAGE + i
            lines.append(f"{move_letter(idx)}: {lab}")
        return "\n".join(lines) if lines else "選択肢がありません"

    async def show_board(self, message: discord.Message, game: dict, gid: int, view: ShogiGameView, notice: str = ""):
        self._set_view(game, view)
        markers = view.markers()
        embed = self.build_game_embed(game, markers, notice)
        kind = "駒を選んでね" if view.phase == "piece" else "移動先を選んでね(キャンセル可)"
        embed.add_field(name=f"選択肢 ({kind})", value=self._options_text(view)[:1000], inline=False)
        await message.edit(embed=embed, attachments=[self.board_file(game, markers)], view=view)

    async def start_cpu(self, message: discord.Message, player_id: int, bet: int, difficulty: str, player_first: bool):
        gid = self._new_game_id()
        self.games[gid] = {
            "state": eng.new_state(), "turn": 0,
            "sente_id": player_id if player_first else 0,
            "gote_id": 0 if player_first else player_id,
            "bet": bet, "mode": "cpu", "difficulty": difficulty,
            "mult": DIFFICULTY_MULT[difficulty], "player_first": player_first,
            "started_at": time.monotonic(), "view": None,
        }
        with suppress(Exception):
            await missions.record_event(player_id, "game")
        await self.advance(message, self.games[gid], gid,
                           notice="ゲーム開始！あなたは" + ("☗先手" if player_first else "☖後手"))

    async def start_pvp_direct(self, message: discord.Message, sente_id: int, gote_id: int, bet: int):
        gid = self._new_game_id()
        self.games[gid] = {"state": eng.new_state(), "turn": 0,
                           "sente_id": sente_id, "gote_id": gote_id, "bet": bet, "mode": "pvp",
                           "started_at": time.monotonic(), "view": None}
        with suppress(Exception):
            await missions.record_event(sente_id, "game")
            await missions.record_event(gote_id, "game")
        await self.advance(message, self.games[gid], gid, notice="対戦開始！☗先手からです")

    async def start_pvp(self, message, host_id, guest_id, bet, host_sente: bool):
        await self.start_pvp_direct(message, host_id if host_sente else guest_id,
                                    guest_id if host_sente else host_id, bet)

    async def advance(self, message: discord.Message, game: dict, gid: int, notice: str = ""):
        if self._expired(game):
            await self.settle(message, game, gid, winner_color=None)
            return
        while True:
            over, winner = eng.is_game_over(game["state"])
            if over:
                await self.settle(message, game, gid, winner_color=winner)
                return
            turn_id = game["sente_id"] if game["state"]["turn"] == 0 else game["gote_id"]
            if turn_id == 0:
                await asyncio.sleep(0.8)
                difficulty = game["difficulty"]
                if difficulty == "hard":
                    move = await asyncio.to_thread(eng.cpu_choose, game["state"], difficulty)
                else:
                    move = eng.cpu_choose(game["state"], difficulty)
                game["state"] = eng.apply_move(game["state"], move)
                if move["from"] is None:
                    notice = f"🤖CPUは{_KANJI.get(move['kind'], '?')}を{sq_name(*move['to'])}に打った"
                else:
                    notice = f"🤖CPUは{sq_name(*move['from'])}→{sq_name(*move['to'])}に移動"
                continue
            view = ShogiGameView(self, gid, notice=notice)
            view.message = message
            await self.show_board(message, game, gid, view, notice=notice)
            return

    async def settle(self, message, game, gid, winner_color: int | None = None, resigned_id: int | None = None):
        self._set_view(game, None)
        bet = game["bet"]
        self.games.pop(gid, None)
        if resigned_id is not None:
            if game["mode"] == "cpu":
                result = "lose"
            else:
                winner_id = game["gote_id"] if resigned_id == game["sente_id"] else game["sente_id"]
                result = "win"
        elif winner_color is None:
            result = "draw"
            winner_id = None
        else:
            winner_id = game["sente_id"] if winner_color == 0 else game["gote_id"]
            if game["mode"] == "cpu":
                result = "win" if winner_id != 0 else "lose"
            else:
                result = "win"

        if game["mode"] == "cpu":
            player_id = game["sente_id"] or game["gote_id"]
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
            view: discord.ui.View | None = ShogiRematchView(self, player_id, bet, game["difficulty"], game["player_first"])
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
                for uid in (game["sente_id"], game["gote_id"]):
                    await apply_income(uid, bet)
                desc_extra = "引き分け！掛け金は両者に返金されました"
                color = discord.Color.gold()
            view = None

        embed = discord.Embed(title="将棋 [対局終了]",
                              description=f"☗<@{game['sente_id']}> vs ☖<@{game['gote_id']}>\n{desc_extra}",
                              color=color)
        embed.add_field(name="掛け金 (1人あたり)", value=buildAmountText(bet))
        embed.set_image(url=f"attachment://{BOARD_IMAGE_NAME}")
        await message.edit(embed=embed, attachments=[self.board_file(game)], view=view)

    @commands.hybrid_command("shogi", brief="将棋でCPUと勝負します")
    @app_commands.rename(bet="掛け金", difficulty="強さ", first="先手")
    @app_commands.describe(bet="賭ける額", difficulty="CPUの強さ", first="Trueであなたが先手")
    @app_commands.choices(difficulty=[
        app_commands.Choice(name="かんたん (勝ち×1.5)", value="easy"),
        app_commands.Choice(name="ふつう (勝ち×1.9)", value="normal"),
        app_commands.Choice(name="つよい (勝ち×2.5)", value="hard"),
    ])
    @commands.guild_only()
    async def shogiCommand(self, ctx: commands.Context, bet: int = 100,
                           difficulty: app_commands.Choice[str] | None = None, first: bool = True):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply("賭けのない将棋ほどつまらないものはないよ", ephemeral=True)
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
            "state": eng.new_state(), "turn": 0,
            "sente_id": ctx.author.id if first else 0,
            "gote_id": 0 if first else ctx.author.id,
            "bet": bet, "mode": "cpu", "difficulty": diff,
            "mult": DIFFICULTY_MULT[diff], "player_first": first,
            "started_at": time.monotonic(), "view": None,
        }
        with suppress(Exception):
            await missions.record_event(ctx.author.id, "game")
        await self.advance(msg, self.games[gid], gid,
                           notice="ゲーム開始！あなたは" + ("☗先手" if first else "☖後手"))

    @commands.hybrid_command("shogi-vs", brief="将棋で指定メンバーと対戦します")
    @app_commands.rename(bet="掛け金", opponent="相手", host_first="先手後手")
    @app_commands.describe(bet="賭ける額 (両者が同額)", opponent="対戦相手", host_first="どちらが先手か")
    @app_commands.choices(host_first=[
        app_commands.Choice(name="ランダム", value="random"),
        app_commands.Choice(name="自分が先手", value="host"),
        app_commands.Choice(name="相手が先手", value="guest"),
    ])
    @commands.guild_only()
    async def shogiVsCommand(self, ctx: commands.Context, bet: int, opponent: discord.Member,
                             host_first: app_commands.Choice[str] | None = None):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply("賭けのない将棋ほどつまらないものはないよ", ephemeral=True)
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
        view = ShogiChallengeView(self, ctx.author.id, opponent.id, bet, order)
        self.lobby_views[ctx.author.id] = view
        await ctx.reply("OK", ephemeral=True)
        msg = await ctx.channel.send(
            f"{opponent.mention} さん、<@{ctx.author.id}> からの将棋対戦指名です！\n"
            f"掛け金: {buildAmountText(bet)} (勝者は {buildAmountText(int(bet * 2 * PVP_RAKE))} を獲得)",
            view=view)
        view.message = msg

    @commands.hybrid_command("shogi-open", brief="将棋の対戦相手を募集します")
    @app_commands.rename(bet="掛け金", host_first="先手後手")
    @app_commands.describe(bet="賭ける額 (両者が同額)", host_first="どちらが先手か")
    @app_commands.choices(host_first=[
        app_commands.Choice(name="ランダム", value="random"),
        app_commands.Choice(name="自分が先手", value="host"),
        app_commands.Choice(name="相手が先手", value="guest"),
    ])
    @commands.guild_only()
    async def shogiOpenCommand(self, ctx: commands.Context, bet: int,
                               host_first: app_commands.Choice[str] | None = None):
        if bet < 0:
            raise YouMustDie()
        if bet < 1:
            await ctx.reply("賭けのない将棋ほどつまらないものはないよ", ephemeral=True)
            return
        if self.is_busy(ctx.author.id):
            await ctx.reply("進行中・募集中のゲームがあります", ephemeral=True)
            return
        user_data = await getUser(ctx.author)
        if user_data.amount < bet:
            raise AmountNotEnough()
        self.lobbies.add(ctx.author.id)
        order = host_first.value if host_first else "random"
        view = ShogiOpenLobbyView(self, ctx.author.id, bet, order)
        self.lobby_views[ctx.author.id] = view
        await ctx.reply("OK", ephemeral=True)
        msg = await ctx.channel.send(
            f"{ctx.author.mention} が将棋の対戦相手を募集中！やりたい人はボタンを押してください⚔️\n"
            f"掛け金: {buildAmountText(bet)} (勝者は {buildAmountText(int(bet * 2 * PVP_RAKE))} を獲得)",
            view=view)
        view.message = msg

    @commands.hybrid_command("shogi-cancel", brief="自分の募集中ロビーを強制取り消しします")
    @commands.guild_only()
    async def shogiCancelCommand(self, ctx: commands.Context):
        self.lobbies.discard(ctx.author.id)
        view = self.lobby_views.pop(ctx.author.id, None)
        if view is None:
            await ctx.reply("取り消せる募集中ロビーはありません", ephemeral=True)
            return
        await view._expire("募集は取り消されました")
        await ctx.reply("募集中ロビーを取り消しました", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ShogiCog(bot))
