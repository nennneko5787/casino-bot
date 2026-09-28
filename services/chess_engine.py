"""チェスエンジン (Discord依存なし)。

盤: 8x8、board[r][c] は None か 'P','N','B','R','Q','K'(白) / 小文字(黒)。
白が下 (r=6,7) で r- 方向に進む。state は board/turn/castling/ep/halfmove。
castling: {'K','Q','k','q'} の残り権利集合。ep: アンパサン標的 (r,c) or None。
手: {'from':(r,c),'to':(r,c),'promo': None|'Q'|'R'|'B'|'N'}。
キャスリング・アンパサン・プロモーション・チェック/詰み/ステイルメイト対応。
"""

from __future__ import annotations

import random

VALUES = {"P": 1, "N": 3, "B": 3, "R": 5, "Q": 9, "K": 0}

_KNIGHT = [(-2, -1), (-2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2), (2, -1), (2, 1)]
_KING = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
_DIAG = [(-1, -1), (-1, 1), (1, -1), (1, 1)]
_LINE = [(-1, 0), (1, 0), (0, -1), (0, 1)]


def new_state() -> dict:
    back = ["R", "N", "B", "Q", "K", "B", "N", "R"]
    board: list[list[str | None]] = [[None] * 8 for _ in range(8)]
    board[0] = [p.lower() for p in back]
    board[1] = ["p"] * 8
    board[6] = ["P"] * 8
    board[7] = list(back)
    return {
        "board": board, "turn": "w",
        "castling": {"K", "Q", "k", "q"}, "ep": None, "halfmove": 0,
    }


def _is_white(p: str) -> bool:
    return p.isupper()


def _side(p: str) -> str:
    return "w" if _is_white(p) else "b"


def _king_pos(board, turn: str) -> tuple[int, int] | None:
    want = "K" if turn == "w" else "k"
    for r in range(8):
        for c in range(8):
            if board[r][c] == want:
                return (r, c)
    return None


def _attacked(board, r: int, c: int, by: str, ep: tuple | None) -> bool:
    # ポーン
    dr = -1 if by == "w" else 1
    for dc in (-1, 1):
        nr, nc = r + dr, c + dc
        if 0 <= nr < 8 and 0 <= nc < 8:
            p = board[nr][nc]
            if p is not None and _side(p) == by and p.upper() == "P":
                return True
    # ナイト・キング
    for steps, kinds in ((_KNIGHT, {"N"}), (_KING, {"K"})):
        for sdr, sdc in steps:
            nr, nc = r + sdr, c + sdc
            if 0 <= nr < 8 and 0 <= nc < 8:
                p = board[nr][nc]
                if p is not None and _side(p) == by and p.upper() in kinds:
                    return True
    # スライダー
    for dirs, kinds in ((_DIAG, {"B", "Q"}), (_LINE, {"R", "Q"})):
        for sdr, sdc in dirs:
            nr, nc = r + sdr, c + sdc
            while 0 <= nr < 8 and 0 <= nc < 8:
                p = board[nr][nc]
                if p is not None:
                    if _side(p) == by and p.upper() in kinds:
                        return True
                    break
                nr += sdr
                nc += sdc
    return False


def in_check_board(board, turn: str, ep) -> bool:
    kp = _king_pos(board, turn)
    if kp is None:
        return True
    return _attacked(board, kp[0], kp[1], "b" if turn == "w" else "w", ep)


def _pseudo(board, r: int, c: int, castling: set, ep) -> list[dict]:
    p = board[r][c]
    if p is None:
        return []
    turn = _side(p)
    kind = p.upper()
    out: list[dict] = []

    def add(tr: int, tc: int, promo=None):
        out.append({"from": (r, c), "to": (tr, tc), "promo": promo})

    if kind == "P":
        fwd = -1 if turn == "w" else 1
        start = 6 if turn == "w" else 1
        last = 0 if turn == "w" else 7
        nr = r + fwd
        if 0 <= nr < 8 and board[nr][c] is None:
            if nr == last:
                for pr in ("Q", "R", "B", "N"):
                    add(nr, c, pr)
            else:
                add(nr, c)
                if r == start and board[r + 2 * fwd][c] is None:
                    add(r + 2 * fwd, c)
        for dc in (-1, 1):
            nc = c + dc
            if 0 <= nr < 8 and 0 <= nc < 8:
                t = board[nr][nc]
                if t is not None and _side(t) != turn and t.upper() != "K":
                    if nr == last:
                        for pr in ("Q", "R", "B", "N"):
                            add(nr, nc, pr)
                    else:
                        add(nr, nc)
                if ep == (nr, nc):
                    add(nr, nc)
    elif kind == "N":
        for dr, dc in _KNIGHT:
            nr, nc = r + dr, c + dc
            if 0 <= nr < 8 and 0 <= nc < 8:
                t = board[nr][nc]
                if t is None or (_side(t) != turn and t.upper() != "K"):
                    add(nr, nc)
    elif kind == "K":
        for dr, dc in _KING:
            nr, nc = r + dr, c + dc
            if 0 <= nr < 8 and 0 <= nc < 8:
                t = board[nr][nc]
                if t is None or (_side(t) != turn and t.upper() != "K"):
                    add(nr, nc)
        # キャスリング (通過マス含め攻撃下でないこと)
        home = 7 if turn == "w" else 0
        foe = "b" if turn == "w" else "w"
        if r == home and c == 4:
            short = "K" if turn == "w" else "k"
            long = "Q" if turn == "w" else "q"
            if short in castling and board[home][5] is None and board[home][6] is None:
                rook = board[home][7]
                if (
                    rook is not None
                    and rook.upper() == "R"
                    and _side(rook) == turn
                    and not any(_attacked(board, home, cc, foe, ep) for cc in (4, 5, 6))
                ):
                    add(home, 6)
            if long in castling and board[home][3] is None and board[home][2] is None and board[home][1] is None:
                rook = board[home][0]
                if (
                    rook is not None
                    and rook.upper() == "R"
                    and _side(rook) == turn
                    and not any(_attacked(board, home, cc, foe, ep) for cc in (4, 3, 2))
                ):
                    add(home, 2)
    else:
        dirs = []
        if kind in ("B", "Q"):
            dirs += _DIAG
        if kind in ("R", "Q"):
            dirs += _LINE
        for dr, dc in dirs:
            nr, nc = r + dr, c + dc
            while 0 <= nr < 8 and 0 <= nc < 8:
                t = board[nr][nc]
                if t is None:
                    add(nr, nc)
                else:
                    if _side(t) != turn and t.upper() != "K":
                        add(nr, nc)
                    break
                nr += dr
                nc += dc
    return out


def _apply(board, move: dict, turn: str):
    nb = [[p for p in row] for row in board]
    (fr, fc), (tr, tc) = move["from"], move["to"]
    p = nb[fr][fc]
    assert p is not None
    # アンパサン取り
    if p.upper() == "P" and (tr, tc) == _ep_of(board, turn) and nb[tr][tc] is None:
        nb[fr][tc] = None
    nb[fr][fc] = None
    if move.get("promo"):
        nb[tr][tc] = move["promo"] if turn == "w" else move["promo"].lower()
    else:
        nb[tr][tc] = p
    # キャスリングのルーク移動
    if p.upper() == "K" and abs(tc - fc) == 2:
        home = fr
        if tc == 6:
            nb[home][5], nb[home][7] = nb[home][7], None
        else:
            nb[home][3], nb[home][0] = nb[home][0], None
    return nb


def _ep_of(board, turn: str):
    return None  # _apply 内では state.ep を渡すため未使用


def legal_moves(state: dict) -> list[dict]:
    board, turn = state["board"], state["turn"]
    castling, ep = state["castling"], state["ep"]
    moves: list[dict] = []
    for r in range(8):
        for c in range(8):
            p = board[r][c]
            if p is None or _side(p) != turn:
                continue
            for m in _pseudo(board, r, c, castling, ep):
                # 玉取り手は除外
                t = board[m["to"][0]][m["to"][1]]
                if t is not None and t.upper() == "K":
                    continue
                # アンパサン時の実盤でチェック確認
                nb = _apply_with_ep(board, m, turn, ep)
                if not in_check_board(nb, turn, None):
                    moves.append(m)
    return moves


def _apply_with_ep(board, move: dict, turn: str, ep):
    nb = [[p for p in row] for row in board]
    (fr, fc), (tr, tc) = move["from"], move["to"]
    p = nb[fr][fc]
    assert p is not None
    if p.upper() == "P" and (tr, tc) == ep and nb[tr][tc] is None:
        nb[fr][tc] = None
    nb[fr][fc] = None
    if move.get("promo"):
        nb[tr][tc] = move["promo"] if turn == "w" else move["promo"].lower()
    else:
        nb[tr][tc] = p
    if p.upper() == "K" and abs(tc - fc) == 2:
        if tc == 6:
            nb[fr][5], nb[fr][7] = nb[fr][7], None
        else:
            nb[fr][3], nb[fr][0] = nb[fr][0], None
    return nb


def apply_move(state: dict, move: dict) -> dict:
    board, turn = state["board"], state["turn"]
    nb = _apply_with_ep(board, move, turn, state["ep"])
    (fr, fc), (tr, tc) = move["from"], move["to"]
    p = board[fr][fc]
    castling = set(state["castling"])
    # 権利剥奪
    if p is not None and p.upper() == "K":
        castling -= {"K", "Q"} if turn == "w" else {"k", "q"}
    for sq, right in (((7, 0), "Q"), ((7, 7), "K"), ((0, 0), "q"), ((0, 7), "k")):
        if (fr, fc) == sq or (tr, tc) == sq:
            castling.discard(right)
    # 新ep (2歩のみ)
    ep = None
    if p is not None and p.upper() == "P" and abs(tr - fr) == 2:
        ep = ((fr + tr) // 2, fc)
    foe = "b" if turn == "w" else "w"
    return {
        "board": nb, "turn": foe, "castling": castling,
        "ep": ep, "halfmove": state["halfmove"] + 1,
    }


def movable_pieces(state: dict) -> list[tuple[int, int]]:
    seen: list[tuple[int, int]] = []
    for m in legal_moves(state):
        if m["from"] not in seen:
            seen.append(m["from"])
    return seen


def dests_for(state: dict, sel: tuple[int, int]) -> list[dict]:
    return [m for m in legal_moves(state) if m["from"] == sel]


def is_game_over(state: dict) -> tuple[bool, str | None]:
    """(終了か, 'w'/'b'/None=引分)。"""
    if legal_moves(state):
        return False, None
    if in_check_board(state["board"], state["turn"], state["ep"]):
        return True, "b" if state["turn"] == "w" else "w"
    return True, None


def evaluate(state: dict, turn: str) -> float:
    score = 0.0
    for r in range(8):
        for c in range(8):
            p = state["board"][r][c]
            if p is not None:
                v = VALUES[p.upper()]
                score += v if _side(p) == turn else -v
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
            opp = legal_moves(ns)
            if opp:
                sample = opp[:24] if len(opp) > 24 else opp
                val -= max(evaluate(apply_move(ns, o), state["turn"]) for o in sample) * 0.8
        scored.append((val + (0.5 if m.get("promo") else 0.0), random.random(), m))
    return max(scored)[2]
