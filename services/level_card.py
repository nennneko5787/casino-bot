"""レベル系の画像レンダラ (Pillow)。

/level のレベルカード、/level-ranking の番付、レベルアップ通知を
おしゃれなダーク×ネオン風のカード画像として描画する。
"""

from __future__ import annotations

import io
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FONT_PATH = _REPO_ROOT / "assets" / "fonts" / "ipaexg.ttf"

W = 1200

# パレット
BG_TOP = (20, 21, 48)
BG_MID = (28, 32, 78)
BG_BOT = (16, 14, 40)
CARD = (30, 33, 68)
CARD_EDGE = (255, 255, 255, 28)
TEXT_MAIN = (245, 247, 255)
TEXT_SUB = (170, 180, 210)
GOLD = (255, 205, 92)
GOLD_DEEP = (232, 150, 40)
CYAN = (64, 224, 255)
VIOLET = (150, 110, 255)
PINK = (255, 110, 200)


def _font(size: int) -> ImageFont.FreeTypeFont:
    if _FONT_PATH.exists():
        return ImageFont.truetype(str(_FONT_PATH), size)
    return ImageFont.load_default()  # type: ignore[return-value]


def _v_gradient(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h))
    # 3-stop gradient: top->mid (0-55%), mid->bot (55-100%)
    px = img.load()
    assert px is not None
    for y in range(h):
        t = y / max(h - 1, 1)
        if t < 0.55:
            k = t / 0.55
            c = tuple(int(BG_TOP[i] + (BG_MID[i] - BG_TOP[i]) * k) for i in range(3))
        else:
            k = (t - 0.55) / 0.45
            c = tuple(int(BG_MID[i] + (BG_BOT[i] - BG_MID[i]) * k) for i in range(3))
        for x in range(w):
            px[x, y] = c
    return img


def _glow(img: Image.Image, xy: tuple[int, int], r: int, color: tuple[int, int, int], alpha: int = 60) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x, y = xy
    d.ellipse([x - r, y - r, x + r, y + r], fill=color + (alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(80))
    img.alpha_composite(layer) if img.mode == "RGBA" else None


def _base(h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = _v_gradient(W, h).convert("RGBA")
    # ネオングロー (別レイヤーに描いて合成するため半透明OK)
    _glow(img, (W - 120, 90), 260, (64, 224, 255), 55)
    _glow(img, (120, h - 80), 300, (255, 110, 200), 48)
    _glow(img, (W // 2, h // 2), 420, (120, 90, 255), 30)
    d = ImageDraw.Draw(img, "RGBA")
    # 外枠 (不透明色で描く: 半透明の図形描画は置換になるため)
    d.rounded_rectangle([6, 6, W - 7, h - 7], radius=36, outline=(96, 102, 160), width=2)
    return img, ImageDraw.Draw(img, "RGBA")


def _text_shadow(d: ImageDraw.ImageDraw, xy: tuple[float, float], s: str, font, fill, anchor: str = "la"):
    x, y = xy
    d.text((x + 1, y + 2), s, font=font, fill=(0, 0, 0, 140), anchor=anchor)
    d.text((x, y), s, font=font, fill=fill, anchor=anchor)


def _fit_font(text: str, max_w: int, start: int, d: ImageDraw.ImageDraw) -> ImageFont.FreeTypeFont:
    size = start
    while size > 18:
        f = _font(size)
        bb = d.textbbox((0, 0), text, font=f)
        if bb[2] - bb[0] <= max_w:
            return f
        size -= 4
    return _font(18)


def _avatar(img_circle_size: int, avatar_bytes: bytes | None) -> Image.Image:
    s = img_circle_size
    if avatar_bytes:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
            # 正方形に中央クロップ
            side = min(av.size)
            x0 = (av.width - side) // 2
            y0 = (av.height - side) // 2
            av = av.crop((x0, y0, x0 + side, y0 + side)).resize((s, s), Image.LANCZOS)
        except Exception:  # noqa: BLE001 - アバター失敗時はデフォルト画像にフォールバック
            av = None  # type: ignore[assignment]
    else:
        av = None  # type: ignore[assignment]
    if av is None:
        av = Image.new("RGB", (s, s), (52, 58, 110))
        d = ImageDraw.Draw(av)
        d.ellipse([s * 0.3, s * 0.22, s * 0.7, s * 0.55], fill=(200, 210, 235))
        d.arc([s * 0.2, s * 0.55, s * 0.8, s * 1.05], 200, 340, fill=(200, 210, 235), width=max(6, s // 28))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, s, s], fill=255)
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(av, (0, 0))
    out.putalpha(mask)
    return out


def _ring(base: Image.Image, cx: int, cy: int, r: int, width: int, c1, c2) -> None:
    # グラデーションリング: 角度ごとに色を補間して弧を描く
    d = ImageDraw.Draw(base, "RGBA")
    steps = 120
    for i in range(steps):
        t0, t1 = i / steps, (i + 1) / steps
        col = tuple(int(c1[k] + (c2[k] - c1[k]) * t0) for k in range(3)) + (255,)
        d.arc([cx - r, cy - r, cx + r, cy + r], start=t0 * 360, end=t1 * 360 + 1, fill=col, width=width)


def _progress_bar(img: Image.Image, d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, ratio: float) -> None:
    ratio = max(0.0, min(1.0, ratio))
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=(12, 14, 36))
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, outline=(70, 78, 130), width=2)
    if ratio <= 0:
        return
    ih = h - 6
    iw = w - 6
    fw = max(ih, int(iw * ratio))
    grad = Image.new("RGBA", (fw, ih), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for px in range(fw):
        t = px / max(fw - 1, 1)
        if t < 0.5:
            k = t / 0.5
            c = (int(CYAN[0] + (VIOLET[0] - CYAN[0]) * k), int(CYAN[1] + (VIOLET[1] - CYAN[1]) * k), int(CYAN[2] + (VIOLET[2] - CYAN[2]) * k), 255)
        else:
            k = (t - 0.5) / 0.5
            c = (int(VIOLET[0] + (PINK[0] - VIOLET[0]) * k), int(VIOLET[1] + (PINK[1] - VIOLET[1]) * k), int(VIOLET[2] + (PINK[2] - VIOLET[2]) * k), 255)
        gd.line([(px, 0), (px, ih)], fill=c)
    mask = Image.new("L", (fw, ih), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, fw, ih], radius=ih // 2, fill=255)
    grad.putalpha(mask)
    img.alpha_composite(grad, (x + 3, y + 3))


def _pill(d: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], text: str, font, fg=(20, 22, 48), bg=(255, 205, 92)) -> None:
    d.rounded_rectangle(xy, radius=(xy[3] - xy[1]) // 2, fill=bg)
    d.rounded_rectangle(xy, radius=(xy[3] - xy[1]) // 2, outline=(255, 235, 180), width=2)
    cx = (xy[0] + xy[2]) / 2
    cy = (xy[1] + xy[3]) / 2
    d.text((cx, cy - 1), text, font=font, fill=fg, anchor="mm")


# ---------------- /level ----------------

def render_level_card(
    display_name: str,
    info: dict,
    avatar_bytes: bytes | None = None,
    *,
    reward_per_level: int = 50,
) -> io.BytesIO:
    """レベルカード (1200x640) を返す。info は services.levels.get_info の戻り値。"""
    H = 640
    img, d = _base(H)

    level: int = info.get("level", 1)
    need: int = info.get("need", 1)
    cur: int = info.get("current", 0)
    xp: int = info.get("xp", 0)
    remaining: int = info.get("remaining", 0)
    rank = info.get("rank")
    messages: int = info.get("messages", 0)
    vc_minutes: int = info.get("vc_minutes", 0)
    ratio = (cur / need) if need else 0
    pct = ratio * 100
    next_reward = (level + 1) * reward_per_level

    # ヘッダー
    _text_shadow(d, (64, 40), "LEVEL CARD", _font(34), (255, 205, 92, 255))
    rank_text = f"RANK #{rank}" if rank else "RANK — OUT OF RANGE"
    _pill(d, (W - 400, 34, W - 64, 88), rank_text, _font(30), fg=(20, 22, 48),
          bg=(255, 205, 92) if rank and rank <= 3 else (225, 232, 255))

    # アバター
    AX, AY, AS = 84, 150, 250
    _ring(img, AX + AS // 2, AY + AS // 2, AS // 2 + 12, 10, GOLD, PINK)
    img.alpha_composite(_avatar(AS, avatar_bytes), (AX, AY))
    # レベルバッジ
    bx0, by0, bx1, by1 = AX + AS - 110, AY + AS - 64, AX + AS + 40, AY + AS + 10
    _pill(d, (bx0, by0, bx1, by1), f"Lv.{level}", _font(38), fg=(20, 22, 48), bg=(255, 205, 92))

    # 名前 & XP
    nx = AX + AS + 48
    name_font = _fit_font(display_name, W - nx - 80, 68, d)
    _text_shadow(d, (nx, 168), display_name, name_font, TEXT_MAIN + (255,))
    _text_shadow(d, (nx, 168 + 92), f"TOTAL {xp:,} XP  •  NEXT +{next_reward:,} pt", _font(30), TEXT_SUB + (255,))

    # 進捗バー
    bar_x, bar_y, bar_w, bar_h = 64, 440, W - 128, 52
    _progress_bar(img, d, bar_x, bar_y, bar_w, bar_h, ratio)
    pct_text = f"{pct:05.1f}%"
    d.text((bar_x + bar_w / 2, bar_y + bar_h / 2 - 1), pct_text, font=_font(30), fill=(255, 255, 255, 255), anchor="mm",
           stroke_width=2, stroke_fill=(20, 20, 50, 200))
    _text_shadow(d, (bar_x + 4, bar_y + bar_h + 12), f"{cur:,} / {need:,} XP", _font(32), TEXT_MAIN + (255,))
    rt = f"あと {remaining:,} XP"
    bb = d.textbbox((0, 0), rt, font=_font(32))
    _text_shadow(d, (bar_x + bar_w - (bb[2] - bb[0]) - 4, bar_y + bar_h + 12), rt, _font(32), GOLD + (255,))

    # ステータス3枠
    stats = [
        ("MESSAGES", f"{messages:,}", "発言"),
        ("VC TIME", f"{vc_minutes:,}", "分滞在"),
        ("NEXT REWARD", f"{next_reward:,}", "pt / LvUP"),
    ]
    cw = (W - 128 - 2 * 20) // 3
    for i, (label, value, sub) in enumerate(stats):
        x0 = 64 + i * (cw + 20)
        d.rounded_rectangle([x0, 530 - 22, x0 + cw, H - 56], radius=22, fill=(22, 26, 60))
        d.rounded_rectangle([x0, 530 - 22, x0 + cw, H - 56], radius=22, outline=(70, 78, 130), width=2)
        d.text((x0 + cw / 2, 524), label, font=_font(24), fill=TEXT_SUB + (255,), anchor="mm")
        _text_shadow(d, (x0 + cw / 2, 556), value, _font(44), TEXT_MAIN + (255,), anchor="mm")
        d.text((x0 + cw / 2, 556 + 2), value, font=_font(44), fill=(0, 0, 0, 0), anchor="mm")
        d.text((x0 + cw / 2, 600 - 14), sub, font=_font(22), fill=TEXT_SUB + (255,), anchor="mm")

    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    buf.seek(0)
    return buf


def render_level_ranking(rows: list[dict], *, limit: int = 10) -> io.BytesIO:
    """レベルランキング画像。rows は {name, level, xp} を含む。"""
    rows = rows[:limit]
    row_h, gap = 92, 12
    header_h, footer_h = 220, 84
    H = header_h + len(rows) * (row_h + gap) + footer_h + 20
    H = max(H, 480)
    img, d = _base(H)

    _text_shadow(d, (64, 44), "LEVEL RANKING", _font(52), GOLD + (255,))
    _text_shadow(d, (64, 44 + 66), f"TOP {limit}  :  5*Lv^2+50*Lv+100 の二次カーブ", _font(28), TEXT_SUB + (255,))
    _pill(d, (W - 300, 48, W - 64, 104), "SEASON", _font(30), fg=(20, 22, 48), bg=(255, 205, 92))

    y = header_h
    medal_bg = [(255, 205, 92), (220, 228, 245), (232, 160, 100)]
    for i, r in enumerate(rows):
        x0, x1 = 64, W - 64
        y0, y1 = y, y + row_h
        highlight = i == 0
        d.rounded_rectangle([x0, y0, x1, y1], radius=24, fill=(52, 42, 20) if highlight else (24, 28, 64))
        edge = GOLD if highlight else (112, 120, 170)
        d.rounded_rectangle([x0, y0, x1, y1], radius=24, outline=edge, width=3 if highlight else 2)
        # 順位サークル
        cx, cy, cr = x0 + 58, (y0 + y1) // 2, 30
        bg = medal_bg[i] if i < 3 else (60, 66, 120)
        d.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=bg)
        d.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], outline=(230, 236, 255), width=2)
        rank_s = str(i + 1)
        d.text((cx, cy - 1), rank_s, font=_font(34), fill=(30, 25, 10, 255) if i < 3 else (255, 255, 255, 255), anchor="mm")
        # 名前
        name = str(r.get("name", "?"))
        nf = _fit_font(name, 560, 38, d)
        _text_shadow(d, (x0 + 108, cy - 22), name, nf, TEXT_MAIN + (255,))
        d.text((x0 + 108, cy + 22), f"{int(r.get('xp', 0)):,} XP", font=_font(26), fill=TEXT_SUB + (255,))
        # Lvバッジ右
        lv_s = f"Lv.{r.get('level', 1)}"
        bb = d.textbbox((0, 0), lv_s, font=_font(36))
        bw = bb[2] - bb[0] + 44
        _pill(d, (x1 - bw - 20, cy - 26, x1 - 20, cy + 26), lv_s, _font(36))
        y += row_h + gap

    if not rows:
        _text_shadow(d, (W // 2, header_h + 60), "対象者がいません", _font(36), TEXT_SUB + (255,), anchor="mm")

    d.text((W // 2, H - 52), "チャット 15〜25XP ・ VC 1分 8〜12XP  •  報酬は借金の自動返済つき", font=_font(24),
           fill=TEXT_SUB + (255,), anchor="mm")
    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    buf.seek(0)
    return buf


def render_levelup_card(
    display_name: str,
    old: int,
    new: int,
    gained: int,
    reward: int,
    avatar_bytes: bytes | None = None,
    repaid: int = 0,
) -> io.BytesIO:
    """レベルアップ通知カード (1200x520 / 返済ありはx560)。"""
    H = 560 if repaid > 0 else 520
    img, d = _base(H)
    rng = random.Random(old * 1000 + new)
    # 紙吹雪
    for _ in range(140):
        x, yy = rng.randint(20, W - 20), rng.randint(20, H - 20)
        c = rng.choice([GOLD, CYAN, PINK, VIOLET, (255, 255, 255)])
        r = rng.randint(3, 7)
        d.ellipse([x - r, yy - r, x + r, yy + r], fill=c + (200,))

    _text_shadow(d, (W // 2, 66), "LEVEL UP!", _font(58), GOLD + (255,), anchor="mm")
    # アバター
    AS = 190
    AX = 90
    AY = 160
    _ring(img, AX + AS // 2, AY + AS // 2, AS // 2 + 10, 9, GOLD, PINK)
    img.alpha_composite(_avatar(AS, avatar_bytes), (AX, AY))

    nx = AX + AS + 44
    nf = _fit_font(display_name, W - nx - 80, 52, d)
    _text_shadow(d, (nx, 168), display_name, nf, TEXT_MAIN + (255,))
    _text_shadow(d, (nx, 168 + 74), f"Lv.{old}  →  Lv.{new}  (+{new - old})", _font(52), GOLD + (255,))
    _text_shadow(d, (nx, 168 + 74 + 66), f"獲得 +{gained:,} XP  /  報酬 {reward:,} pt", _font(32), TEXT_SUB + (255,))
    if repaid > 0:
        _text_shadow(d, (nx, 168 + 74 + 66 + 44), f"うち借金へ {repaid:,} pt 自動返済", _font(26), TEXT_SUB + (255,))

    # 下部バー風メッセージ
    d.rounded_rectangle([64, H - 120, W - 64, H - 52], radius=24, fill=(18, 22, 52))
    d.rounded_rectangle([64, H - 120, W - 64, H - 52], radius=24, outline=GOLD, width=2)
    d.text((W // 2, H - 86), "おめでとう! 次のレベルも /mission と一緒に駆け上がろう!", font=_font(30),
           fill=TEXT_MAIN + (255,), anchor="mm")
    _ = math.pi  # (unused safeguard for future arcs)
    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    buf.seek(0)
    return buf
