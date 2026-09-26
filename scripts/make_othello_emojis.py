"""オセロ盤面タイルのカスタム絵文字用 PNG を生成する。

盤面の崩れ防止のため全タイルを同一サイズ (128x128) の正方形にする。
生成物: othello_empty / othello_black / othello_white / othello_a〜othello_z

使い方:
    uv run scripts/make_othello_emojis.py [--out othello_emojis]

生成後に Discord 開発者ポータル (または絵文字管理) からアプリ絵文字として
アップロードし、ID を .env に設定する:
    othello_black=<黒石の絵文字ID>
    othello_white=<白石の絵文字ID>
    othello_empty=<空マスの絵文字ID>
    othello_letters=<A〜Zの絵文字IDをカンマ区切りで26個>
"""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 128
CENTER = SIZE // 2
STONE_RADIUS = 52

BOARD_GREEN = (0, 132, 61)
BOARD_DARK = (0, 90, 42)
BLACK_TOP = (90, 90, 95)
BLACK_BOTTOM = (5, 5, 8)
WHITE_TOP = (255, 255, 255)
WHITE_BOTTOM = (175, 175, 180)


def lerp(a: int, b: int, t: float) -> int:
    return round(a + (b - a) * t)


def lerp_color(top: tuple, bottom: tuple, t: float) -> tuple:
    return tuple(lerp(a, b, t) for a, b in zip(top, bottom))


def base_square() -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BOARD_GREEN)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, SIZE - 1, SIZE - 1], outline=BOARD_DARK, width=6)
    return img


def draw_stone(img: Image.Image, top: tuple, bottom: tuple) -> None:
    draw = ImageDraw.Draw(img)
    # 上から光が当たる radial 風グラデーション (同心円で近似)
    for r in range(STONE_RADIUS, 0, -1):
        t = r / STONE_RADIUS
        # 中心より少し上を明るくする
        color = lerp_color(top, bottom, t**0.7)
        draw.ellipse(
            [CENTER - r, CENTER - r - 3, CENTER + r, CENTER + r - 3],
            fill=color,
        )


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 古い Pillow (size 引数なし)
        return ImageFont.load_default()


def draw_letter(img: Image.Image, letter: str) -> None:
    draw = ImageDraw.Draw(img)
    font = load_font(84)
    bbox = draw.textbbox((0, 0), letter, font=font, stroke_width=2)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        (CENTER - w / 2 - bbox[0], CENTER - h / 2 - bbox[1]),
        letter,
        font=font,
        fill=(255, 255, 255),
        stroke_width=2,
        stroke_fill=BOARD_DARK,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="othello_emojis")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    empty = base_square()
    empty.save(out / "othello_empty.png")

    black = base_square()
    draw_stone(black, BLACK_TOP, BLACK_BOTTOM)
    black.save(out / "othello_black.png")

    white = base_square()
    draw_stone(white, WHITE_TOP, WHITE_BOTTOM)
    white.save(out / "othello_white.png")

    for i in range(26):
        letter = chr(ord("A") + i)
        tile = base_square()
        draw_letter(tile, letter)
        tile.save(out / f"othello_{letter.lower()}.png")

    print(f"29 files written to {out.resolve()}")
    print("アップロード後、A→Z の順でIDを並べて othello_letters に設定してください。")


if __name__ == "__main__":
    main()
