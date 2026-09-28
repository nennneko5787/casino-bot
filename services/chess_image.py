"""チェス盤を1枚の画像として描画する。

8x8 + 座標。markers {(r,c): 'a'-'z'} のマス右下にアルファベットを重ね、
対応ボタンと連動させる。
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

TILE = 96
MARGIN = 44
BG = (30, 34, 38)
LIGHT = (240, 217, 181)
DARK = (181, 136, 99)
LABEL = (255, 255, 255)
MARK_C = (30, 120, 255)
HL = (150, 200, 255)

_GLYPH = {
    "K": "♚", "Q": "♛", "R": "♜", "B": "♝", "N": "♞", "P": "♟",
}


def _font(size: int):
    try:
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


def render_chess_image(board, markers=None) -> io.BytesIO:
    marks = _norm(markers)
    size = MARGIN + TILE * 8 + MARGIN
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)
    piece_font = _font(56)
    label_font = _font(24)
    alpha_font = _font(28)

    for c in range(8):
        t = chr(ord("a") + c)
        d.text((MARGIN + c * TILE + TILE // 2 - 8, (MARGIN - 24) // 2),
               t, font=label_font, fill=LABEL)
    for r in range(8):
        t = str(8 - r)
        d.text(((MARGIN - 20) // 2, MARGIN + r * TILE + TILE // 2 - 12),
               t, font=label_font, fill=LABEL)

    for r in range(8):
        for c in range(8):
            x0, y0 = MARGIN + c * TILE, MARGIN + r * TILE
            base = LIGHT if (r + c) % 2 == 0 else DARK
            if (r, c) in marks:
                base = HL
            d.rectangle([x0, y0, x0 + TILE, y0 + TILE], fill=base)
            p = board[r][c]
            if p is not None:
                g = _GLYPH[p.upper()]
                col = (250, 250, 250) if p.isupper() else (15, 15, 15)
                bb = d.textbbox((0, 0), g, font=piece_font)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                # 黒駒は白縁で視認性確保
                d.text((x0 + (TILE - tw) / 2 - bb[0], y0 + (TILE - th) / 2 - bb[1]),
                       g, font=piece_font, fill=col,
                       stroke_width=1, stroke_fill=(80, 80, 80))
            if (r, c) in marks:
                letter = marks[(r, c)].upper()
                bb = d.textbbox((0, 0), letter, font=alpha_font)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                bx0, by0 = x0 + TILE - tw - 12, y0 + TILE - th - 8
                d.rectangle([bx0 - 3, by0 - 1, bx0 + tw + 5, by0 + th + 3], fill=MARK_C)
                d.text((bx0 - bb[0], by0 - bb[1]), letter, font=alpha_font,
                       fill=(255, 255, 255))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
