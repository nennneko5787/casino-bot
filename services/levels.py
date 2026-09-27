"""レベリング: チャット・VC滞在でXPを稼ぎ、レベルアップで通貨報酬。

XP付与 (変動制・スパム対策あり):
- チャット: 15〜25のランダム + 長文ボーナス (20文字ごとに+1、最大+10)。
  60秒クールダウン中はXPなし (発言数カウントのみ)。
  5文字未満の短文はXP半減、直前と同一内容はXP半減 (重複スパム対策)。
- VC: 1分あたり 8〜12のランダム。5分ごとの定期精算 + 退室時精算。

レベル式 (MEE6風の二次カーブ):
- Lv -> Lv+1 に必要なXP: 5*Lv^2 + 50*Lv + 100
  (Lv1→2: 155、Lv5→6: 475、Lv10→11: 1100)
- 累計で判定し、複数レベル一気上がりに対応。

報酬は apply_income() 経由のため借金があれば自動返済に充当される。
ミッション連携用に xp / level イベントを record_event() で記録する想定
(呼び出しは cogs/level.py 側で行う)。
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from services.database import DBService

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# ---- チューニング定数 ----
CHAT_MIN_XP = 15
CHAT_MAX_XP = 25
CHAT_COOLDOWN_SEC = 60
CHAT_LENGTH_STEP = 20  # この文字数ごとに+1
CHAT_LENGTH_BONUS_MAX = 10
CHAT_SHORT_LEN = 5  # 未満は短文扱いで半減

VC_MIN_XP_PER_MIN = 8
VC_MAX_XP_PER_MIN = 12

REWARD_PER_LEVEL = 50  # レベルアップ報酬 = 到達Lv × この額
RANKING_LIMIT = 10

_SCHEMA_READY = False


# ---------- レベル式 ----------


def xp_for_next(level: int) -> int:
    """Lv -> Lv+1 に必要なXP。MEE6風の二次カーブ。"""
    level = max(level, 1)
    return 5 * level * level + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    """Lvに到達するのに必要な累計XP。Lv1は0。"""
    if level <= 1:
        return 0
    return sum(xp_for_next(i) for i in range(1, level))


def level_from_xp(xp: int) -> int:
    """累計XPから現在レベルを逆算する。"""
    if xp <= 0:
        return 1
    level = 1
    rest = xp
    while True:
        need = xp_for_next(level)
        if rest < need:
            return level
        rest -= need
        level += 1
        if level > 1000:  # 無限ループ保険
            return level


def _now_iso() -> str:
    return datetime.now(JST).isoformat()


async def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS levels ("
        "user_id BIGINT PRIMARY KEY, "
        "xp INTEGER NOT NULL DEFAULT 0, "
        "level INTEGER NOT NULL DEFAULT 1, "
        "messages INTEGER NOT NULL DEFAULT 0, "
        "vc_minutes INTEGER NOT NULL DEFAULT 0, "
        "last_chat_at TEXT, "
        "last_content TEXT, "
        "updated_at TEXT NOT NULL)"
    )
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS level_vc_sessions ("
        "user_id BIGINT PRIMARY KEY, join_at TEXT NOT NULL, "
        "last_credit TEXT NOT NULL)"
    )
    await DBService.pool.commit()
    _SCHEMA_READY = True


async def _get_row(user_id: int) -> dict:
    await ensure_schema()
    cursor = await DBService.pool.execute(
        "SELECT * FROM levels WHERE user_id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        now = _now_iso()
        await DBService.pool.execute(
            "INSERT INTO levels (user_id, xp, level, messages, vc_minutes, "
            "updated_at) VALUES (?, 0, 1, 0, 0, ?)",
            (user_id, now),
        )
        await DBService.pool.commit()
        return {
            "user_id": user_id,
            "xp": 0,
            "level": 1,
            "messages": 0,
            "vc_minutes": 0,
            "last_chat_at": None,
            "last_content": None,
            "updated_at": now,
        }
    return dict(row)


async def get_info(user_id: int) -> dict:
    """表示用のレベル情報を返す。進捗計算済み。"""
    r = await _get_row(user_id)
    xp: int = r["xp"]
    level: int = r["level"]
    base = total_xp_for_level(level)
    need = xp_for_next(level)
    cur = xp - base
    rank = await get_rank(user_id)
    return {
        "user_id": user_id,
        "xp": xp,
        "level": level,
        "messages": r["messages"],
        "vc_minutes": r["vc_minutes"],
        "base_xp": base,
        "need": need,
        "current": cur,
        "remaining": need - cur,
        "rank": rank,
    }


async def get_rank(user_id: int) -> int | None:
    """XP順の順位 (1始まり)。未登録ならNone。"""
    await ensure_schema()
    cursor = await DBService.pool.execute(
        "SELECT COUNT(*) + 1 AS rank FROM levels "
        "WHERE xp > (SELECT xp FROM levels WHERE user_id = ?)",
        (user_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    # 行自体が無い場合はNone
    cursor2 = await DBService.pool.execute(
        "SELECT 1 FROM levels WHERE user_id = ?", (user_id,)
    )
    exists = await cursor2.fetchone()
    await cursor2.close()
    if not exists:
        return None
    return int(row["rank"])


async def get_ranking(limit: int = RANKING_LIMIT) -> list[dict]:
    """XP上位者を順位順で返す。"""
    await ensure_schema()
    cursor = await DBService.pool.execute(
        "SELECT user_id, xp, level, messages, vc_minutes FROM levels "
        "ORDER BY xp DESC, user_id ASC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [dict(r) for r in rows]


async def add_xp(user_id: int, amount: int) -> tuple[int, int, int]:
    """XPを直接加算。(加算分, 旧Lv, 新Lv) を返す。管理コマンド用。"""
    if amount < 1:
        r = await _get_row(user_id)
        return 0, r["level"], r["level"]
    r = await _get_row(user_id)
    old_level: int = r["level"]
    new_xp: int = r["xp"] + amount
    new_level = level_from_xp(new_xp)
    await DBService.pool.execute(
        "UPDATE levels SET xp = ?, level = ?, updated_at = ? WHERE user_id = ?",
        (new_xp, new_level, _now_iso(), user_id),
    )
    await DBService.pool.commit()
    return amount, old_level, new_level


async def add_chat_xp(user_id: int, content: str) -> tuple[int, int, int, str]:
    """チャット発言のXP付与。(付与XP, 旧Lv, 新Lv, 理由) を返す。

    理由: "ok" / "cooldown" / "short+cooldown" などデバッグ用。
    発言数カウントはクールダウン中も+1する。
    """
    await ensure_schema()
    now = datetime.now(JST)
    r = await _get_row(user_id)

    last_at = None
    if r["last_chat_at"]:
        try:
            last_at = datetime.fromisoformat(r["last_chat_at"])
        except ValueError:
            last_at = None

    text = (content or "").strip()
    if last_at and (now - last_at).total_seconds() < CHAT_COOLDOWN_SEC:
        await DBService.pool.execute(
            "UPDATE levels SET messages = messages + 1, updated_at = ? "
            "WHERE user_id = ?",
            (_now_iso(), user_id),
        )
        await DBService.pool.commit()
        return 0, r["level"], r["level"], "cooldown"

    base = random.randint(CHAT_MIN_XP, CHAT_MAX_XP)
    bonus = min(len(text) // CHAT_LENGTH_STEP, CHAT_LENGTH_BONUS_MAX)
    gained = base + bonus
    reason = "ok"
    # 短文ペナルティ
    if len(text) < CHAT_SHORT_LEN:
        gained = max(1, gained // 2)
        reason = "short"
    # 同一内容連投ペナルティ
    if r["last_content"] is not None and text and text == r["last_content"]:
        gained = max(1, gained // 2)
        reason = "duplicate" if reason == "ok" else reason + "+duplicate"

    old_level: int = r["level"]
    new_xp: int = r["xp"] + gained
    new_level = level_from_xp(new_xp)
    await DBService.pool.execute(
        "UPDATE levels SET xp = ?, level = ?, messages = messages + 1, "
        "last_chat_at = ?, last_content = ?, updated_at = ? "
        "WHERE user_id = ?",
        (new_xp, new_level, now.isoformat(), text[:200], _now_iso(), user_id),
    )
    await DBService.pool.commit()
    return gained, old_level, new_level, reason


async def add_vc_minutes(user_id: int, minutes: int) -> tuple[int, int, int]:
    """VC滞在分のXP付与。(付与XP, 旧Lv, 新Lv) を返す。"""
    if minutes < 1:
        r = await _get_row(user_id)
        return 0, r["level"], r["level"]
    r = await _get_row(user_id)
    gained = sum(
        random.randint(VC_MIN_XP_PER_MIN, VC_MAX_XP_PER_MIN) for _ in range(minutes)
    )
    old_level: int = r["level"]
    new_xp: int = r["xp"] + gained
    new_level = level_from_xp(new_xp)
    await DBService.pool.execute(
        "UPDATE levels SET xp = ?, level = ?, vc_minutes = vc_minutes + ?, "
        "updated_at = ? WHERE user_id = ?",
        (new_xp, new_level, minutes, _now_iso(), user_id),
    )
    await DBService.pool.commit()
    return gained, old_level, new_level


def level_reward(new_level: int) -> int:
    """レベルアップ報酬額。到達Lv × 単価。"""
    return new_level * REWARD_PER_LEVEL


# ---------- VCセッション追跡 (missionsとは独立) ----------


async def vc_join(user_id: int, now: datetime | None = None) -> None:
    await ensure_schema()
    moment = (now or datetime.now(JST)).isoformat()
    await DBService.pool.execute(
        "INSERT OR IGNORE INTO level_vc_sessions "
        "(user_id, join_at, last_credit) VALUES (?, ?, ?)",
        (user_id, moment, moment),
    )
    await DBService.pool.commit()


async def vc_leave(
    user_id: int, now: datetime | None = None
) -> tuple[int, int, int, int]:
    """退室精算。(分, 付与XP, 旧Lv, 新Lv) を返す。"""
    await ensure_schema()
    moment = now or datetime.now(JST)
    cursor = await DBService.pool.execute(
        "SELECT last_credit FROM level_vc_sessions WHERE user_id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if not row:
        r = await _get_row(user_id)
        return 0, 0, r["level"], r["level"]
    try:
        last = datetime.fromisoformat(row["last_credit"])
    except ValueError:
        last = moment
    minutes = int((moment - last).total_seconds() // 60)
    await DBService.pool.execute(
        "DELETE FROM level_vc_sessions WHERE user_id = ?", (user_id,)
    )
    await DBService.pool.commit()
    if minutes <= 0:
        r = await _get_row(user_id)
        return 0, 0, r["level"], r["level"]
    gained, old, new = await add_vc_minutes(user_id, minutes)
    return minutes, gained, old, new


async def flush_vc_sessions(
    now: datetime | None = None,
) -> list[tuple[int, int, int, int, int]]:
    """接続中の全員に経過分を付与。[(user_id, 分, XP, 旧Lv, 新Lv)] を返す。"""
    await ensure_schema()
    moment = now or datetime.now(JST)
    cursor = await DBService.pool.execute(
        "SELECT user_id, last_credit FROM level_vc_sessions"
    )
    rows = await cursor.fetchall()
    await cursor.close()
    results: list[tuple[int, int, int, int, int]] = []
    for r in rows:
        try:
            last = datetime.fromisoformat(r["last_credit"])
        except ValueError:
            continue
        minutes = int((moment - last).total_seconds() // 60)
        if minutes <= 0:
            continue
        try:
            gained, old, new = await add_vc_minutes(r["user_id"], minutes)
        except Exception:
            logger.exception("VC-XPの定期精算に失敗 user=%s", r["user_id"])
            continue
        new_last = (last + timedelta(minutes=minutes)).isoformat()
        await DBService.pool.execute(
            "UPDATE level_vc_sessions SET last_credit = ? WHERE user_id = ?",
            (new_last, r["user_id"]),
        )
        results.append((r["user_id"], minutes, gained, old, new))
    if results:
        await DBService.pool.commit()
    return results


async def reset_user(user_id: int) -> None:
    """指定ユーザーのレベル情報を初期化。"""
    await ensure_schema()
    await DBService.pool.execute("DELETE FROM levels WHERE user_id = ?", (user_id,))
    await DBService.pool.execute(
        "DELETE FROM level_vc_sessions WHERE user_id = ?", (user_id,)
    )
    await DBService.pool.commit()
