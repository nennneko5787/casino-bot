"""オセロ盤面を1枚の画像として描画する。

絵文字の集まり (othello_emojis/*.png) を 8x8 に並べて PNG 化する。
座標ラベル (A-H / 1-8) と、置けるマスのヒント文字 (A-Z) も画像内に描く。
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image

TILE = 128
MARGIN = 56  # 座標ラベル用の余白
BG = (30, 34, 38)
LABEL = (255, 255, 255)

_REPO_ROOT = Path(__file__).resolve().parents[1]
EMOJI_DIR = _REPO_ROOT / "othello_emojis"


@lru_cache(maxsize=1)
def _tiles() -> dict[str, Image.Image]:
    """othello_emojis の PNG をメモリにキャッシュ。"""

    def load(name: str) -> Image.Image:
        img = Image.open(EMOJI_DIR / name).convert("RGB")
        if img.size != (TILE, TILE):
            img = img.resize((TILE, TILE))
        return img

    tiles: dict[str, Image.Image] = {
        "empty": load("othello_empty.png"),
        "black": load("othello_black.png"),
        "white": load("othello_white.png"),
    }
    for i in range(26):
        letter = chr(ord("a") + i)
        tiles[letter] = load(f"othello_{letter}.png")
    return tiles


def _normalize_markers(
    markers: dict[tuple[int, int], object] | list[tuple[int, int]] | tuple | None,
) -> dict[tuple[int, int], str]:
    """呼び出し側の形式ゆれを {(r, c): 'a'-'z'} に正規化する。

    - list/tuple: 読む順に A, B, C... を割り当て (27手目以降は座標タイル=empty扱い)
    - dict: 値が int ならその番目の文字、1文字strならその文字、
      それ以外 (絵文字文字列など) は読む順の文字に読み替え
    """
    if not markers:
        return {}
    if isinstance(markers, (list, tuple)):
        return {
            (r, c): chr(ord("a") + i) if i < 26 else "empty"
            for i, (r, c) in enumerate(markers)
        }
    ordered = list(markers.items())
    result: dict[tuple[int, int], str] = {}
    for i, (pos, value) in enumerate(ordered):
        key = (int(pos[0]), int(pos[1]))
        if isinstance(value, int) and 0 <= value < 26:
            result[key] = chr(ord("a") + value)
        elif isinstance(value, str) and len(value) == 1 and "a" <= value.lower() <= "z":
            result[key] = value.lower()
        else:
            result[key] = chr(ord("a") + i) if i < 26 else "empty"
    return result


def render_board_image(
    board: list[list[int]],
    markers: dict[tuple[int, int], object]
    | list[tuple[int, int]]
    | tuple
    | None = None,
) -> io.BytesIO:
    """盤面 (8x8, 0=空/1=黒/2=白) を1枚の PNG として返す。"""
    tiles = _tiles()
    marks = _normalize_markers(markers)

    size = TILE * 8 + MARGIN
    img = Image.new("RGB", (size, size), BG)

    for r in range(8):
        for c in range(8):
            if board[r][c] == 1:
                tile = tiles["black"]
            elif board[r][c] == 2:
                tile = tiles["white"]
            elif (r, c) in marks:
                tile = tiles.get(marks[(r, c)], tiles["empty"])
            else:
                tile = tiles["empty"]
            img.paste(tile, (MARGIN + c * TILE, MARGIN + r * TILE))

    # 座標ラベル (A-H / 1-8) を余白に描画
    from PIL import ImageDraw, ImageFont

    try:
        label_font = ImageFont.load_default(size=32)
    except TypeError:
        label_font = ImageFont.load_default()
    draw = ImageDraw.Draw(img)
    for c in range(8):
        label = chr(ord("A") + c)
        bbox = draw.textbbox((0, 0), label, font=label_font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = MARGIN + c * TILE + (TILE - w) / 2
        draw.text((x, (MARGIN - h) / 2 - bbox[1]), label, font=label_font, fill=LABEL)
    for r in range(8):
        label = str(r + 1)
        bbox = draw.textbbox((0, 0), label, font=label_font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        y = MARGIN + r * TILE + (TILE - h) / 2
        draw.text(
            ((MARGIN - w) / 2 - bbox[0], y - bbox[1] * 0),
            label,
            font=label_font,
            fill=LABEL,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
