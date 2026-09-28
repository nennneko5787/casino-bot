"""将棋盤を1枚の画像として描画する。

9x9 + 駒台(両側)。markers {(r,c): 'a'-'z'} のマスは駒の右下に
小さなアルファベットを重ね、対応ボタンと連動させる。
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TILE = 96
MARGIN = 44
STAND_W = 96
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
            (int(r), int(c)): chr(ord("a") + i) if i < 26 else "?"
            for i, (r, c) in enumerate(markers)
        }
    out: dict[tuple[int, int], str] = {}
    for i, (pos, v) in enumerate(markers.items()):
        key = (int(pos[0]), int(pos[1]))
        if isinstance(v, int) and 0 <= v < 26:
            out[key] = chr(ord("a") + v)
        elif isinstance(v, str) and len(v) == 1 and "a" <= v.lower() <= "z":
            out[key] = v.lower()
        else:
            out[key] = chr(ord("a") + i) if i < 26 else "?"
    return out


def render_shogi_image(
    board: list[list[tuple[int, str] | None]],
    hands: list | None = None,
    markers=None,
    turn: int = 0,
) -> io.BytesIO:
    marks = _norm(markers)
    w = MARGIN + STAND_W + TILE * 9 + STAND_W + MARGIN
    h = MARGIN + TILE * 9 + MARGIN
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    ox = MARGIN + STAND_W
    oy = MARGIN
    kanji_font = _font(44)
    small_font = _font(30)
    label_font = _font(24)
    alpha_font = _font(28)

    # 座標ラベル
    for c in range(9):
        t = str(c + 1)
        d.text((ox + c * TILE + TILE // 2 - 8, oy - 32), t, font=label_font, fill=LABEL)
    for r in range(9):
        kan = "一二三四五六七八九"[r]
        d.text((ox - 32, oy + r * TILE + TILE // 2 - 14), kan, font=label_font, fill=LABEL)

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

    # 駒台
    if hands is not None:
        order = ["R", "B", "G", "S", "N", "L", "P"]
        for side, sx in ((1, MARGIN), (0, ox + TILE * 9 + 8)):
            name = "後手" if side == 1 else "先手"
            cur = "▼手番" if side == turn else ""
            d.text((sx, oy + (0 if side == 1 else TILE * 9 - 30)),
                   f"{name}{cur}", font=small_font, fill=LABEL)
            hand = hands[side]
            for i, k in enumerate(order):
                n = int(hand.get(k, 0)) if hasattr(hand, "get") else 0
                if n <= 0:
                    continue
                y = oy + (40 + i * 86 if side == 1 else TILE * 9 - 70 - i * 86)
                d.rectangle([sx, y, sx + STAND_W - 16, y + 78], fill=BOARD_C)
                t = _KANJI.get(k, "?")
                d.text((sx + 8, y + 4), t, font=small_font,
                       fill=GOTE_C if side == 1 else SENTE_C)
                d.text((sx + 52, y + 40), f"×{n}", font=small_font, fill=(40, 40, 40))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
