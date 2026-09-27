"""ミッション一覧の画像レンダラ (Pillow)。

周期ごとの1ページをダーク×ネオン風のボード画像として描画する。
絵文字はフォントに無いため、状態はテキストバッジで表現する。
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FONT_PATH = _REPO_ROOT / "assets" / "fonts" / "ipaexg.ttf"

W = 1200

TEXT_MAIN = (245, 247, 255)
TEXT_SUB = (170, 180, 210)
GOLD = (255, 205, 92)
CYAN = (64, 224, 255)
VIOLET = (150, 110, 255)
PINK = (255, 110, 200)
GREEN = (88, 220, 150)
GREY = (130, 140, 165)

PERIOD_ACCENT: dict[str, tuple[int, int, int]] = {
    "hourly": (200, 208, 225),
    "daily": (88, 220, 150),
    "weekly": (110, 170, 255),
    "monthly": (190, 130, 255),
    "once": (255, 205, 92),
}

BG_TOP = (20, 21, 48)
BG_MID = (28, 32, 78)
BG_BOT = (16, 14, 40)


def _font(size: int):
    if _FONT_PATH.exists():
        return ImageFont.truetype(str(_FONT_PATH), size)
    return ImageFont.load_default()


def _base(h: int, accent) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    # 縦グラデ
    grad = Image.new("RGB", (W, h))
    px = grad.load()
    assert px is not None
    for y in range(h):
        t = y / max(h - 1, 1)
        c = tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3))
        for x in range(W):
            px[x, y] = c
    img = grad.convert("RGBA")
    layer = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.ellipse([W - 380, -160, W + 160, 320], fill=accent + (46,))
    ld.ellipse([-220, h - 320, 320, h + 120], fill=(255, 110, 200, 36))
    layer = layer.filter(ImageFilter.GaussianBlur(70))
    img.alpha_composite(layer)
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([6, 6, W - 7, h - 7], radius=36, outline=(96, 102, 160), width=2)
    # 上部アクセントライン
    d.rounded_rectangle([6, 6, W - 7, 14], radius=7, fill=accent)
    return img, ImageDraw.Draw(img, "RGBA")


def _shadow(d, xy, s, font, fill, anchor="la"):
    x, y = xy
    d.text((x + 2, y + 3), s, font=font, fill=(0, 0, 0, 140), anchor=anchor)
    d.text((x, y), s, font=font, fill=fill, anchor=anchor)


def _fit(text: str, max_w: int, start: int, d) -> ImageFont.FreeTypeFont:
    size = start
    while size > 16:
        f = _font(size)
        bb = d.textbbox((0, 0), text, font=f)
        if bb[2] - bb[0] <= max_w:
            return f
        size -= 2
    return _font(16)


def render_mission_page(
    *,
    period: str,
    period_label: str,
    reset_note: str,
    entries: list[dict],
    channel_text: str,
    page_idx: int,
    total_pages: int,
    amount_name: str = "pt",
) -> io.BytesIO:
    accent = PERIOD_ACCENT.get(period, (150, 170, 255))
    row_h, gap = 170, 14
    header_h, footer_h = 268, 96
    H = header_h + max(len(entries), 1) * (row_h + gap) + footer_h + 24
    img, d = _base(H, accent)

    # ヘッダー
    _shadow(d, (64, 44), f"{period_label} MISSION", _font(50), accent + (255,))
    _shadow(d, (64, 44 + 62), reset_note, _font(26), TEXT_SUB + (255,))
    _shadow(d, (64, 44 + 62 + 36), f"期限内に受け取らないと失効 (恒常は除く) / 指定ch: {channel_text}", _font(24), TEXT_SUB + (255,))
    if period == "once":
        _shadow(d, (64, 44 + 62 + 36 + 34), "? 隠しミッションもあるかも... ?", _font(24), GOLD + (255,))

    # ページインジケータ
    dots = "  ".join("●" if i == page_idx else "○" for i in range(total_pages))
    bb = d.textbbox((0, 0), dots, font=_font(26))
    d.text((W - 64 - (bb[2] - bb[0]), 56), dots, font=_font(26), fill=accent + (255,))
    pg = f"{page_idx + 1} / {total_pages}"
    bb2 = d.textbbox((0, 0), pg, font=_font(26))
    d.text((W - 64 - (bb2[2] - bb2[0]), 92), pg, font=_font(26), fill=TEXT_SUB + (255,))

    y = header_h
    if not entries:
        d.rounded_rectangle([64, y, W - 64, y + 120], radius=22, fill=(22, 26, 60, 220))
        d.text((W // 2, y + 60), "この周期のミッションはありません", font=_font(30), fill=TEXT_SUB + (255,), anchor="mm")
        y += 134
    for e in entries:
        x0, x1 = 64, W - 64
        y0, y1 = y, y + row_h
        completed = bool(e.get("completed"))
        claimed = bool(e.get("claimed"))
        available = bool(e.get("available", True))
        if not available:
            edge = (120, 130, 160)
            bar_col = (90, 100, 130)
            badge, badge_bg, badge_fg = "LOCK", (70, 78, 110), (225, 232, 255)
        elif claimed:
            edge = (88, 220, 150)
            bar_col = GREEN
            badge, badge_bg, badge_fg = "DONE", (88, 220, 150), (12, 30, 20)
        elif completed:
            edge = GOLD
            bar_col = GOLD
            badge, badge_bg, badge_fg = "GIFT", GOLD, (40, 28, 8)
        else:
            edge = (112, 120, 170)
            bar_col = accent
            badge, badge_bg, badge_fg = "GO", accent, (16, 18, 40)

        d.rounded_rectangle([x0, y0, x1, y1], radius=22, fill=(23, 27, 62))
        d.rounded_rectangle([x0, y0, x1, y1], radius=22, outline=edge, width=3 if completed and not claimed else 2)

        # バッジ
        bx0, by0, bx1, by1 = x0 + 20, y0 + 18, x0 + 128, y0 + 58
        d.rounded_rectangle([bx0, by0, bx1, by1], radius=20, fill=badge_bg)
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        d.text((cx, cy - 1), badge, font=_font(24), fill=badge_fg, anchor="mm")
        # ID
        d.text((bx1 + 14, by0 + 2), f"[{e.get('id', '?')}]", font=_font(24), fill=TEXT_SUB + (255,))

        # タイトル + 報酬 (報酬を先に決めてタイトル幅を確保し、横方向の被りも防ぐ)
        title = str(e.get("title", ""))
        reward_s = f"+{int(e.get('reward', 0)):,}{amount_name}"
        rb = d.textbbox((0, 0), reward_s, font=_font(30))
        rw = rb[2] - rb[0] + 40
        rx1 = x1 - 20
        rx0 = rx1 - rw
        title_max_w = max(200, rx0 - (x0 + 20) - 16)
        tf = _fit(title, title_max_w, 32, d)
        _shadow(d, (x0 + 20, y0 + 62), title, tf, TEXT_MAIN + (255,))
        d.rounded_rectangle([rx0, y0 + 18, rx1, y0 + 62], radius=22, fill=(255, 205, 92, 255))
        d.text(((rx0 + rx1) / 2, (y0 + 18 + y0 + 62) / 2 - 1), reward_s, font=_font(30), fill=(40, 28, 8, 255), anchor="mm")

        # 進捗バー (タイトルと1行分空けて重ならないように配置)
        prog = int(e.get("progress", 0))
        target = max(int(e.get("target", 1)), 1)
        ratio = max(0.0, min(1.0, prog / target))
        bar_x, bar_y, bar_w, bar_h = x0 + 20, y0 + 108, x1 - x0 - 220, 20
        d.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=10, fill=(10, 12, 32))
        fw = int(bar_w * ratio)
        if fw > 0:
            d.rounded_rectangle([bar_x, bar_y, bar_x + max(fw, bar_h), bar_y + bar_h], radius=10, fill=bar_col)
        cnt = f"{prog:,}/{target:,}"
        d.text((bar_x + bar_w + 12, bar_y + bar_h / 2 - 1), cnt, font=_font(26), fill=TEXT_MAIN + (255,), anchor="lm")

        # 説明 + 状態
        if not available:
            tail = "指定ch未設定のため進行しません"
        elif claimed:
            tail = "受取済み"
        elif completed:
            tail = "達成! ボタンで受け取れます"
        else:
            tail = str(e.get("desc", ""))
        desc = f"{e.get('desc', '')} / {tail}" if (completed or claimed or not available) else str(e.get("desc", ""))
        df = _fit(desc, x1 - x0 - 40, 24, d)
        d.text((x0 + 20, y1 - 32), desc, font=df, fill=(GOLD + (255,)) if (completed and not claimed) else (TEXT_SUB + (255,)))

        y += row_h + gap

    d.text((W // 2, H - 56), "メニュー・左右ボタンで周期切替 / GIFTはボタンで受取", font=_font(24), fill=TEXT_SUB + (255,), anchor="mm")
    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    buf.seek(0)
    return buf
