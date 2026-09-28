"""将棋エンジン (Discord依存なし)。

盤: 9x9、board[r][c] は None か (color, kind)。
color: 0=先手(上向き= r が減る方向に進む)、1=後手。
kind: 'P','L','N','S','G','B','R','K' + 成駒 '+P','+L','+N','+S','+B','+R'。
持ち駒: hands[color] は {'P','L','N','S','G','B','R'} の枚数辞書。

合法手は (from, to, promote) または drop (from=None, kind) を返す。
二歩・打ち歩詰めの厳密判定は省き、二歩・段端への歩/香/桂打ち制限のみ実施。
"""

from __future__ import annotations

import random
from collections import Counter

HAND_KINDS = ("P", "L", "N", "S", "G", "B", "R")

# 金の動き (先手視点)。後手は反転。
_GOLD = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, 0)]
_SILVER = [(-1, -1), (-1, 0), (-1, 1), (1, -1), (1, 1)]
_KING = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
_KNIGHT = [(-2, -1), (-2, 1)]

PIECE_VALUES = {
    "P": 1, "L": 3, "N": 4, "S": 5, "G": 6, "B": 8, "R": 10, "K": 0,
    "+P": 6, "+L": 6, "+N": 6, "+S": 6, "+B": 11, "+R": 13,
}


def new_state() -> dict:
    back = ["L", "N", "S", "G", "K", "G", "S", "N", "L"]
    board: list[list[tuple[int, str] | None]] = [[None] * 9 for _ in range(9)]
    for c, k in enumerate(back):
        board[0][c] = (1, k)
        board[8][c] = (0, k)
    board[1][1] = (1, "R")
    board[1][7] = (1, "B")
    board[7][1] = (0, "B")
    board[7][7] = (0, "R")
    for c in range(9):
        board[2][c] = (1, "P")
        board[6][c] = (0, "P")
    return {"board": board, "turn": 0, "hands": [Counter(), Counter()]}


def _flip(dirs: list[tuple[int, int]], color: int) -> list[tuple[int, int]]:
    if color == 0:
        return dirs
    return [(-dr, -dc) for dr, dc in dirs]


def _slide_moves(
    board, r: int, c: int, color: int, dirs: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    out = []
    for dr, dc in _flip(dirs, color):
        nr, nc = r + dr, c + dc
        while 0 <= nr < 9 and 0 <= nc < 9:
            if board[nr][nc] is None:
                out.append((nr, nc))
            else:
                if board[nr][nc][0] != color:
                    out.append((nr, nc))
                break
            nr += dr
            nc += dc
    return out


def _step_moves(
    board, r: int, c: int, color: int, dirs: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    out = []
    for dr, dc in _flip(dirs, color):
        nr, nc = r + dr, c + dc
        if 0 <= nr < 9 and 0 <= nc < 9 and (board[nr][nc] is None or board[nr][nc][0] != color):
            out.append((nr, nc))
    return out


def piece_moves(board, r: int, c: int) -> list[tuple[int, int]]:
    """王手無視の擬似合法手 (移動先のみ)。"""
    cell = board[r][c]
    if cell is None:
        return []
    color, kind = cell
    if kind == "P":
        return _step_moves(board, r, c, color, [(-1, 0)])
    if kind == "L":
        return _slide_moves(board, r, c, color, [(-1, 0)])
    if kind == "N":
        return _step_moves(board, r, c, color, _KNIGHT)
    if kind == "S":
        return _step_moves(board, r, c, color, _SILVER)
    if kind in ("G", "+P", "+L", "+N", "+S"):
        return _step_moves(board, r, c, color, _GOLD)
    if kind == "K":
        return _step_moves(board, r, c, color, _KING)
    if kind == "B":
        return _slide_moves(board, r, c, color, [(-1, -1), (-1, 1), (1, -1), (1, 1)])
    if kind == "R":
        return _slide_moves(board, r, c, color, [(-1, 0), (1, 0), (0, -1), (0, 1)])
    if kind == "+B":
        return _slide_moves(board, r, c, color, [(-1, -1), (-1, 1), (1, -1), (1, 1)]) + _step_moves(
            board, r, c, color, [(-1, 0), (1, 0), (0, -1), (0, 1)]
        )
    if kind == "+R":
        return _slide_moves(board, r, c, color, [(-1, 0), (1, 0), (0, -1), (0, 1)]) + _step_moves(
            board, r, c, color, [(-1, -1), (-1, 1), (1, -1), (1, 1)]
        )
    return []


def _king_pos(board, color: int) -> tuple[int, int] | None:
    for r in range(9):
        for c in range(9):
            if board[r][c] == (color, "K"):
                return (r, c)
    return None


def in_check(board, color: int) -> bool:
    kp = _king_pos(board, color)
    if kp is None:
        return True
    for r in range(9):
        for c in range(9):
            cell = board[r][c]
            if cell is not None and cell[0] != color and kp in piece_moves(board, r, c):
                return True
    return False


def _in_zone(r: int, color: int) -> bool:
    return r <= 2 if color == 0 else r >= 6


def _must_promote(kind: str, color: int, to_r: int) -> bool:
    last = 0 if color == 0 else 8
    last2 = 1 if color == 0 else 7
    if kind == "P" or kind == "L":
        return to_r == last
    if kind == "N":
        return to_r == last or to_r == last2
    return False


def _can_promote(kind: str, color: int, from_r: int, to_r: int) -> bool:
    if kind in ("G", "K", "+P", "+L", "+N", "+S", "+B", "+R"):
        return False
    return _in_zone(from_r, color) or _in_zone(to_r, color)


def _apply_on(board, move: dict):
    nb = [[cell for cell in row] for row in board]
    if move["from"] is None:
        nb[move["to"][0]][move["to"][1]] = (move["color"], move["kind"])
    else:
        fr, fc = move["from"]
        tr, tc = move["to"]
        piece = nb[fr][fc]
        assert piece is not None
        kind = piece[1]
        if move.get("promote"):
            kind = "+" + kind
        nb[fr][fc] = None
        nb[tr][tc] = (move["color"], kind)
    return nb


def _captured_kind(kind: str) -> str:
    return kind[1] if kind.startswith("+") else kind


def legal_moves(state: dict) -> list[dict]:
    """完全合法手 (自玉放置手を除外)。drop は promote=False。"""
    board = state["board"]
    turn = state["turn"]
    hands = state["hands"][turn]
    moves: list[dict] = []
    for r in range(9):
        for c in range(9):
            cell = board[r][c]
            if cell is None or cell[0] != turn:
                continue
            for tr, tc in piece_moves(board, r, c):
                if board[tr][tc] is not None and board[tr][tc] == (turn, "K"):
                    continue  # 玉取りは禁止
                base = {
                    "from": (r, c), "to": (tr, tc),
                    "color": turn, "kind": cell[1],
                }
                if _must_promote(cell[1], turn, tr):
                    cands = [{"promote": True}]
                elif _can_promote(cell[1], turn, r, tr):
                    cands = [{"promote": False}, {"promote": True}]
                else:
                    cands = [{"promote": False}]
                for extra in cands:
                    m = dict(base)
                    m.update(extra)
                    nb = _apply_on(board, m)
                    if not in_check(nb, turn):
                        moves.append(m)
    # 打ち駒
    for kind in HAND_KINDS:
        if hands.get(kind, 0) <= 0:
            continue
        for tr in range(9):
            for tc in range(9):
                if board[tr][tc] is not None:
                    continue
                if kind == "P":
                    # 二歩
                    if any(
                        board[rr][tc] == (turn, "P") for rr in range(9)
                    ):
                        continue
                    if tr == (0 if turn == 0 else 8):
                        continue
                if kind == "L" and tr == (0 if turn == 0 else 8):
                    continue
                if kind == "N" and tr in (
                    (0, 1) if turn == 0 else (8, 7)
                ):
                    continue
                m = {
                    "from": None, "to": (tr, tc), "color": turn,
                    "kind": kind, "promote": False,
                }
                nb = _apply_on(board, m)
                if not in_check(nb, turn):
                    moves.append(m)
    return moves


def movable_pieces(state: dict) -> list[tuple[int, int] | tuple[str, str]]:
    """駒選択UI用: 動かせる盤上駒 (r,c) と打てる持駒 ('hand', kind)。"""
    out: list = []
    seen_hand: set[str] = set()
    for m in legal_moves(state):
        if m["from"] is None:
            if m["kind"] not in seen_hand:
                seen_hand.add(m["kind"])
                out.append(("hand", m["kind"]))
        else:
            if m["from"] not in out:
                out.append(m["from"])
    # 盤上→行順、持駒→後ろ
    board_part = sorted([p for p in out if isinstance(p, tuple) and isinstance(p[0], int)])
    hand_part = sorted([p for p in out if isinstance(p, tuple) and p[0] == "hand"])
    return board_part + hand_part


def dests_for(state: dict, sel) -> list[dict]:
    """選択駒に対する合法手一覧。sel は (r,c) か ('hand', kind)。"""
    res = []
    for m in legal_moves(state):
        if m["from"] is None:
            if sel[0] == "hand" and m["kind"] == sel[1]:
                res.append(m)
        else:
            if m["from"] == sel:
                res.append(m)
    return res


def apply_move(state: dict, move: dict) -> dict:
    board = state["board"]
    turn = state["turn"]
    hands = [Counter(state["hands"][0]), Counter(state["hands"][1])]
    if move["from"] is None:
        hands[turn][move["kind"]] -= 1
        if hands[turn][move["kind"]] <= 0:
            del hands[turn][move["kind"]]
    else:
        tr, tc = move["to"]
        target = board[tr][tc]
        if target is not None and target[0] != turn:
            hands[turn][_captured_kind(target[1])] += 1
    nb = _apply_on(board, move)
    return {"board": nb, "turn": 1 - turn, "hands": hands}


def is_game_over(state: dict) -> tuple[bool, int | None]:
    """(終了か, 勝者color or None)。手番側が合法手なし=詰み負け。"""
    if legal_moves(state):
        return False, None
    if in_check(state["board"], state["turn"]):
        return True, 1 - state["turn"]
    return True, None  # ステイルメイトは引き分け扱い


def evaluate(state: dict, color: int) -> float:
    score = 0.0
    for r in range(9):
        for c in range(9):
            cell = state["board"][r][c]
            if cell is not None:
                v = PIECE_VALUES.get(cell[1], 0)
                score += v if cell[0] == color else -v
    for k, n in state["hands"][color].items():
        score += PIECE_VALUES.get(k, 0) * n * 1.1
    for k, n in state["hands"][1 - color].items():
        score -= PIECE_VALUES.get(k, 0) * n * 1.1
    return score


def cpu_choose(state: dict, difficulty: str) -> dict:
    moves = legal_moves(state)
    if difficulty == "easy":
        return random.choice(moves)
    scored = []
    for m in moves:
        ns = apply_move(state, m)
        val = evaluate(ns, state["turn"])
        if difficulty == "hard":
            # 相手の最善応手を1手読み
            opp = legal_moves(ns)
            if opp:
                val -= max(evaluate(apply_move(ns, o), state["turn"]) for o in opp[:20]) * 0.8 if len(opp) > 20 else max(
                    evaluate(apply_move(ns, o), state["turn"]) for o in opp
                ) * 0.8
        scored.append((val, random.random(), m))
    return max(scored)[2]
