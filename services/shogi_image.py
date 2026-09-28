"""将棋盤を1枚の画像として描画する。

9x9 + 駒台(両側)。markers {(r,c): 'a'-'z'} のマスは駒の右下に
小さなアルファベットを重ね、対応ボタンと連動させる。
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TILE = 96
MARGIN = 48
STAND_W = 150
ROWLABEL_W = 34
CELL_W = STAND_W - 12
CELL_H = 80
CELL_GAP = 88
BG = (30, 34, 38)
BOARD_C = (240, 200, 130)
LINE_C = (60, 40, 20)
LABEL = (255, 255, 255)
SENTE_C = (20, 20, 20)
GOTE_C = (150, 20, 20)
MARK_C = (30, 120, 255)

_REPOS = Path(__file__).resolve().parents[1]
_FONT_PATH = _REPOS / "assets" / "fonts" / "ipaexg.ttf"

_KANJI = {
    "P": "歩", "L": "香", "N": "桂", "S": "銀", "G": "金",
    "B": "角", "R": "飛", "K": "王",
    "+P": "と", "+L": "杏", "+N": "圭", "+S": "全",
    "+B": "馬", "+R": "竜",
}


def _font(size: int):
    try:
        if _FONT_PATH.exists():
            return ImageFont.truetype(str(_FONT_PATH), size)
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _norm(markers) -> dict[tuple[int, int], str]:
    if not markers:
        return {}
    if isinstance(markers, (list, tuple)):
        return {
            (int(r), int(c)): chr(ord("a") + i) if i < 26 else str(i + 1)
            for i, (r, c) in enumerate(markers)
        }
    out: dict[tuple[int, int], str] = {}
    for i, (pos, v) in enumerate(markers.items()):
        key = (int(pos[0]), int(pos[1]))
        if isinstance(v, int) and 0 <= v < 26:
            out[key] = chr(ord("a") + v)
        elif isinstance(v, str) and v:
            out[key] = v.lower() if len(v) == 1 else v
        else:
            out[key] = chr(ord("a") + i) if i < 26 else str(i + 1)
    return out


def _norm_stand(marks) -> dict[tuple[int, str], str]:
    """駒台マーカー {(side, kind): 'a'-'z'} の正規化。"""
    if not marks:
        return {}
    if isinstance(marks, (list, tuple)):
        return {
            (int(s), str(k)): chr(ord("a") + i) if i < 26 else str(i + 1)
            for i, (s, k) in enumerate(marks)
        }
    out: dict[tuple[int, str], str] = {}
    for i, (pos, v) in enumerate(marks.items()):
        key = (int(pos[0]), str(pos[1]))
        if isinstance(v, int) and 0 <= v < 26:
            out[key] = chr(ord("a") + v)
        elif isinstance(v, str) and v:
            out[key] = v.lower() if len(v) == 1 else v
        else:
            out[key] = chr(ord("a") + i) if i < 26 else str(i + 1)
    return out


def _centered_text(d, cx: float, cy: float, s: str, font, fill) -> None:
    bb = d.textbbox((0, 0), s, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    d.text((cx - tw / 2 - bb[0], cy - th / 2 - bb[1]), s, font=font, fill=fill)


def render_shogi_image(
    board: list[list[tuple[int, str] | None]],
    hands: list | None = None,
    markers=None,
    turn: int = 0,
    stand_marks=None,
) -> io.BytesIO:
    marks = _norm(markers)
    smarks = _norm_stand(stand_marks)
    ox = MARGIN + STAND_W + ROWLABEL_W
    oy = MARGIN
    w = ox + TILE * 9 + 12 + STAND_W + MARGIN
    h = MARGIN + TILE * 9 + MARGIN
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    kanji_font = _font(44)
    count_font = _font(26)
    name_font = _font(26)
    turn_font = _font(22)
    label_font = _font(24)
    alpha_font = _font(28)

    # 座標ラベル (中央揃え)
    for c in range(9):
        _centered_text(d, ox + c * TILE + TILE / 2, oy / 2, str(c + 1),
                       label_font, LABEL)
    for r in range(9):
        _centered_text(d, ox - ROWLABEL_W / 2, oy + r * TILE + TILE / 2,
                       "一二三四五六七八九"[r], label_font, LABEL)

    for r in range(9):
        for c in range(9):
            x0, y0 = ox + c * TILE, oy + r * TILE
            hi = (r, c) in marks
            d.rectangle([x0, y0, x0 + TILE, y0 + TILE],
                        fill=(255, 235, 170) if hi else BOARD_C,
                        outline=LINE_C, width=2)
            cell = board[r][c]
            if cell is not None:
                color, kind = cell
                txt = _KANJI.get(kind, "?")
                col = SENTE_C if color == 0 else GOTE_C
                bb = d.textbbox((0, 0), txt, font=kanji_font)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                d.text((x0 + (TILE - tw) / 2 - bb[0], y0 + (TILE - th) / 2 - bb[1]),
                        txt, font=kanji_font, fill=col)
                if color == 1:  # 後手は向きが分かるよう下線
                    d.line([x0 + 14, y0 + TILE - 10, x0 + TILE - 14, y0 + TILE - 10],
                            fill=col, width=3)
            if hi:
                letter = marks[(r, c)].upper()
                bb = d.textbbox((0, 0), letter, font=alpha_font)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                bx0, by0 = x0 + TILE - tw - 12, y0 + TILE - th - 8
                d.rectangle([bx0 - 3, by0 - 1, bx0 + tw + 5, by0 + th + 3], fill=MARK_C)
                d.text((bx0 - bb[0], by0 - bb[1]), letter, font=alpha_font, fill=(255, 255, 255))

    # 駒台 (2段組: 1行目=名前+手番 / 駒セル=漢字左上+枚数右下にセル内収納)
    if hands is not None:
        order = ["R", "B", "G", "S", "N", "L", "P"]
        for side, sx in ((1, MARGIN), (0, ox + TILE * 9 + 12)):
            name = "後手" if side == 1 else "先手"
            cur = "▼手番" if side == turn else ""
            if side == 1:
                ty = oy
                d.text((sx, ty), name, font=name_font, fill=LABEL)
                if cur:
                    nb = d.textbbox((0, 0), name, font=name_font)
                    d.text((sx, ty + (nb[3] - nb[1]) + 2), cur,
                           font=turn_font, fill=(255, 220, 120))
                y_top = oy + 78
                ys = [y_top + i * CELL_GAP for i in range(len(order))]
            else:
                bb = d.textbbox((0, 0), name, font=name_font)
                nh = bb[3] - bb[1]
                ty = oy + TILE * 9 - nh - 2
                name_h = nh + 2
                if cur:
                    cb = d.textbbox((0, 0), cur, font=turn_font)
                    ch = cb[3] - cb[1]
                    d.text((sx, ty - ch - 2), cur,
                           font=turn_font, fill=(255, 220, 120))
                    name_h += ch + 2
                d.text((sx, ty), name, font=name_font, fill=LABEL)
                y_base = oy + TILE * 9 - CELL_H - name_h - 14
                ys = [y_base - i * CELL_GAP for i in range(len(order))]
            hand = hands[side]
            for i, k in enumerate(order):
                n = int(hand.get(k, 0)) if hasattr(hand, "get") else 0
                if n <= 0:
                    continue
                y = ys[i]
                d.rectangle([sx, y, sx + CELL_W, y + CELL_H], fill=BOARD_C,
                            outline=LINE_C, width=2)
                t = _KANJI.get(k, "?")
                d.text((sx + 8, y + 4), t, font=count_font,
                        fill=GOTE_C if side == 1 else SENTE_C)
                cnt = f"×{n}"
                bb = d.textbbox((0, 0), cnt, font=count_font)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                d.text((sx + CELL_W - tw - 8 - bb[0], y + CELL_H - th - 6 - bb[1]),
                        cnt, font=count_font, fill=(40, 40, 40))
                if (side, k) in smarks:
                    letter = smarks[(side, k)].upper()
                    bb = d.textbbox((0, 0), letter, font=alpha_font)
                    tw, th = bb[2] - bb[0], bb[3] - bb[1]
                    bx0, by0 = sx + CELL_W - tw - 8, y + 4
                    d.rectangle([bx0 - 3, by0 - 1, bx0 + tw + 5, by0 + th + 3],
                                fill=MARK_C)
                    d.text((bx0 - bb[0], by0 - bb[1]), letter,
                           font=alpha_font, fill=(255, 255, 255))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
