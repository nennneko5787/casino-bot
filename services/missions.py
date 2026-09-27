"""ミッション: 様々な行動で通貨を稼ぐソシャゲ式ミッション。

周期: hourly / daily / weekly / monthly / once(恒常)。
- hourly〜monthly は JST 境界でリセット。期限内に受け取らないと失効する。
- once は累積・無期限・1回限り。

行動イベント: message(発言) / vc_minute(VC滞在・分) / game(カジノ・オセロ)
/ trade(株売買) / trade_buy(株購入) / xp(XP獲得量) / level(レベルアップ回数)
/ sumanko(隠しキーワード発言)。
ゲーム側Cog・on_message・on_voice_state_update から record_event() を呼ぶ。
報酬は apply_income() 経由のため借金があれば自動返済に充当される。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from services.database import DBService

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

PERIODS = ("hourly", "daily", "weekly", "monthly", "once")

PERIOD_LABEL = {
    "hourly": "アワー",
    "daily": "デイリー",
    "weekly": "ウィークリー",
    "monthly": "マンスリー",
    "once": "恒常",
}

RESET_NOTE = {
    "hourly": "毎時0分(JST)にリセット",
    "daily": "毎日0時(JST)にリセット",
    "weekly": "毎週月曜0時(JST)にリセット",
    "monthly": "毎月1日0時(JST)にリセット",
    "once": "無期限・1回限り",
}

# message系ミッションの対象チャンネル種別
CHANNEL_ANY = "any"  # 全チャンネル
CHANNEL_CONFIG = "config"  # 管理者が /mission-admin channel で設定したチャンネル
CONFIG_CHANNEL_KEY = "mission_channel_id"

_SCHEMA_READY = False


@dataclass(frozen=True, slots=True)
class MissionDef:
    id: str
    period: str
    event: str
    target: int
    reward: int
    title: str
    desc: str
    channel: str = CHANNEL_ANY
    hidden: bool = False  # True: 達成するまで一覧に表示されない (隠しミッション)


MISSIONS: list[MissionDef] = [
    # ---- アワー ----
    MissionDef("H1", "hourly", "message", 3, 30, "チャットで3通送る", "どこでもOK"),
    MissionDef("H2", "hourly", "vc_minute", 5, 30, "VCに5分いる", "通話に5分滞在"),
    MissionDef(
        "H3", "hourly", "game", 1, 30, "ゲームで1回遊ぶ", "スロット・BJなど何でも"
    ),
    MissionDef("H4", "hourly", "xp", 40, 30, "XPを40稼ぐ", "チャット・VCで獲得"),
    # ---- デイリー ----
    MissionDef("D1", "daily", "message", 10, 100, "チャットで10通送る", "どこでもOK"),
    MissionDef(
        "D2",
        "daily",
        "message",
        5,
        120,
        "指定chで5通送る",
        "管理者設定のchのみ",
        CHANNEL_CONFIG,
    ),
    MissionDef("D3", "daily", "vc_minute", 30, 100, "VCに30分いる", "累積30分"),
    MissionDef("D4", "daily", "game", 3, 100, "ゲームで3回遊ぶ", "累積3プレイ"),
    MissionDef("D5", "daily", "trade", 1, 100, "株を1回取引する", "売買どちらでも"),
    MissionDef("D6", "daily", "xp", 150, 100, "XPを150稼ぐ", "チャット・VCで獲得"),
    MissionDef("D7", "daily", "level", 1, 150, "レベルアップする", "1回レベルアップ"),
    # ---- ウィークリー ----
    MissionDef("W1", "weekly", "message", 50, 400, "チャットで50通送る", "どこでもOK"),
    MissionDef(
        "W2",
        "weekly",
        "message",
        20,
        500,
        "指定chで20通送る",
        "管理者設定のchのみ",
        CHANNEL_CONFIG,
    ),
    MissionDef("W3", "weekly", "vc_minute", 180, 400, "VCに3時間いる", "累積180分"),
    MissionDef("W4", "weekly", "game", 10, 400, "ゲームで10回遊ぶ", "累積10プレイ"),
    MissionDef("W5", "weekly", "xp", 800, 400, "XPを800稼ぐ", "チャット・VCで獲得"),
    MissionDef("W6", "weekly", "level", 2, 500, "2回レベルアップする", "累積2回"),
    # ---- マンスリー ----
    MissionDef(
        "M1", "monthly", "message", 200, 1500, "チャットで200通送る", "どこでもOK"
    ),
    MissionDef("M2", "monthly", "vc_minute", 600, 1500, "VCに10時間いる", "累積600分"),
    MissionDef("M3", "monthly", "game", 30, 1000, "ゲームで30回遊ぶ", "累積30プレイ"),
    MissionDef("M4", "monthly", "xp", 3000, 1500, "XPを3000稼ぐ", "チャット・VCで獲得"),
    MissionDef("M5", "monthly", "level", 5, 1500, "5回レベルアップする", "累積5回"),
    # ---- 恒常 ----
    MissionDef("P1", "once", "game", 1, 100, "初めてゲームで遊ぶ", "何でも1プレイ"),
    MissionDef("P2", "once", "trade_buy", 1, 200, "初めて株を買う", "購入1回"),
    MissionDef("P3", "once", "message", 100, 500, "累計100通送る", "どこでもOK"),
    MissionDef("P4", "once", "vc_minute", 300, 500, "累計VC5時間", "累計300分"),
    MissionDef("P5", "once", "game", 50, 1000, "累計50回遊ぶ", "累計50プレイ"),
    MissionDef("P6", "once", "xp", 5000, 800, "累計XP5000稼ぐ", "チャット・VCで獲得"),
    MissionDef(
        "P7", "once", "xp", 20000, 2000, "累計XP20000稼ぐ", "チャット・VCで獲得"
    ),
    MissionDef("P8", "once", "level", 3, 500, "3回レベルアップする", "累計3回"),
    MissionDef("P9", "once", "level", 10, 1500, "10回レベルアップする", "累計10回"),
    # ---- 隠し (達成まで非表示) ----
    MissionDef(
        "S1",
        "once",
        "sumanko",
        1,
        500,
        "すまんこと送る",
        "隠しミッション発見！",
        hidden=True,
    ),
]

BY_ID: dict[str, MissionDef] = {m.id: m for m in MISSIONS}


def period_key(period: str, now: datetime | None = None) -> str:
    """周期の現在キー。hourly以外はJSTの日付境界で切る。"""
    now = now or datetime.now(JST)
    if period == "hourly":
        return now.strftime("%Y-%m-%dT%H")
    if period == "daily":
        return now.strftime("%Y-%m-%d")
    if period == "weekly":
        iso_year, iso_week, _ = now.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if period == "monthly":
        return now.strftime("%Y-%m")
    if period == "once":
        return "all"
    raise ValueError(f"unknown period: {period}")


def _now_iso() -> str:
    return datetime.now(JST).isoformat()


async def ensure_schema() -> None:
    """初回のみテーブルを作成 (alembic未適用でも動くよう保険)。"""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS mission_progress ("
        "user_id BIGINT NOT NULL, mission_id TEXT NOT NULL, "
        "period_key TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, "
        "completed INTEGER NOT NULL DEFAULT 0, claimed INTEGER NOT NULL DEFAULT 0, "
        "updated_at TEXT NOT NULL, "
        "PRIMARY KEY (user_id, mission_id, period_key))"
    )
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS vc_sessions ("
        "user_id BIGINT PRIMARY KEY, join_at TEXT NOT NULL, "
        "last_credit TEXT NOT NULL)"
    )
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS mission_config ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    await DBService.pool.commit()
    _SCHEMA_READY = True


async def get_mission_channel() -> int | None:
    """指定chミッション対象のチャンネルID。未設定なら None。"""
    await ensure_schema()
    cursor = await DBService.pool.execute(
        "SELECT value FROM mission_config WHERE key = ?", (CONFIG_CHANNEL_KEY,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    if not row:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


async def set_mission_channel(channel_id: int | None) -> None:
    await ensure_schema()
    if channel_id is None:
        await DBService.pool.execute(
            "DELETE FROM mission_config WHERE key = ?", (CONFIG_CHANNEL_KEY,)
        )
    else:
        await DBService.pool.execute(
            "INSERT INTO mission_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (CONFIG_CHANNEL_KEY, str(channel_id)),
        )
    await DBService.pool.commit()


async def record_event(
    user_id: int, event: str, count: int = 1, channel_id: int | None = None
) -> None:
    """行動を記録し、対応ミッションの進捗を進める。失敗しても呼び出し側は握りつぶす想定。"""
    if count < 1:
        return
    await ensure_schema()
    now = datetime.now(JST)
    targets = [m for m in MISSIONS if m.event == event]
    if not targets:
        return
    config_channel: int | None = None
    if any(m.channel == CHANNEL_CONFIG for m in targets):
        config_channel = await get_mission_channel()
    for m in targets:
        if m.channel == CHANNEL_CONFIG and (
            config_channel is None or channel_id != config_channel
        ):
            continue
        key = period_key(m.period, now)
        cursor = await DBService.pool.execute(
            "SELECT progress, claimed FROM mission_progress "
            "WHERE user_id = ? AND mission_id = ? AND period_key = ?",
            (user_id, m.id, key),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row:
            new_progress = min(m.target, row["progress"] + count)
            await DBService.pool.execute(
                "UPDATE mission_progress SET progress = ?, completed = ?, "
                "updated_at = ? WHERE user_id = ? AND mission_id = ? "
                "AND period_key = ?",
                (
                    new_progress,
                    1 if new_progress >= m.target else 0,
                    _now_iso(),
                    user_id,
                    m.id,
                    key,
                ),
            )
        else:
            new_progress = min(m.target, count)
            await DBService.pool.execute(
                "INSERT INTO mission_progress "
                "(user_id, mission_id, period_key, progress, completed, claimed, "
                "updated_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
                (
                    user_id,
                    m.id,
                    key,
                    new_progress,
                    1 if new_progress >= m.target else 0,
                    _now_iso(),
                ),
            )
    await DBService.pool.commit()


async def get_status(user_id: int) -> list[dict]:
    """全ミッションの現在周期の状態を MISSIONS 順で返す。"""
    await ensure_schema()
    now = datetime.now(JST)
    channel = await get_mission_channel()
    result: list[dict] = []
    for m in MISSIONS:
        key = period_key(m.period, now)
        cursor = await DBService.pool.execute(
            "SELECT progress, completed, claimed FROM mission_progress "
            "WHERE user_id = ? AND mission_id = ? AND period_key = ?",
            (user_id, m.id, key),
        )
        row = await cursor.fetchone()
        await cursor.close()
        result.append(
            {
                "id": m.id,
                "period": m.period,
                "event": m.event,
                "target": m.target,
                "reward": m.reward,
                "title": m.title,
                "desc": m.desc,
                "channel": m.channel,
                "hidden": m.hidden,
                "channel_id": channel if m.channel == CHANNEL_CONFIG else None,
                "available": m.channel != CHANNEL_CONFIG or channel is not None,
                "period_key": key,
                "progress": row["progress"] if row else 0,
                "completed": bool(row and row["completed"]),
                "claimed": bool(row and row["claimed"]),
            }
        )
    return result


async def claim(user_id: int, mission_id: str) -> tuple[MissionDef, int, int]:
    """報酬を受け取る。戻り値は (ミッション, 報酬額, 借金への自動返済額)。

    未達成・受取済み・期限切れ(別周期)は ValueError。
    """
    from services.loan import apply_income

    await ensure_schema()
    key = mission_id.strip().upper()
    m = BY_ID.get(key)
    if not m:
        raise ValueError(f"{mission_id} というミッションはありません")
    period = period_key(m.period)
    cursor = await DBService.pool.execute(
        "SELECT progress, claimed FROM mission_progress "
        "WHERE user_id = ? AND mission_id = ? AND period_key = ?",
        (user_id, m.id, period),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if not row or row["progress"] < m.target:
        raise ValueError(f"`{m.id}` はまだ達成していません")
    if row["claimed"]:
        raise ValueError(f"`{m.id}` は受取済みです")
    await DBService.pool.execute(
        "UPDATE mission_progress SET claimed = 1, updated_at = ? "
        "WHERE user_id = ? AND mission_id = ? AND period_key = ?",
        (_now_iso(), user_id, m.id, period),
    )
    await DBService.pool.commit()
    repaid, _ = await apply_income(user_id, m.reward)
    return m, m.reward, repaid


# ---------- VC滞在の追跡 ----------


async def vc_join(user_id: int, now: datetime | None = None) -> None:
    """VC入室を記録。既に記録があれば上書きしない (二重入室対策)。"""
    await ensure_schema()
    moment = (now or datetime.now(JST)).isoformat()
    await DBService.pool.execute(
        "INSERT OR IGNORE INTO vc_sessions (user_id, join_at, last_credit) "
        "VALUES (?, ?, ?)",
        (user_id, moment, moment),
    )
    await DBService.pool.commit()


async def vc_leave(user_id: int, now: datetime | None = None) -> int:
    """VC退室を精算。戻り値は加算した分(整数)。"""
    await ensure_schema()
    moment = now or datetime.now(JST)
    cursor = await DBService.pool.execute(
        "SELECT last_credit FROM vc_sessions WHERE user_id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    if not row:
        return 0
    try:
        last = datetime.fromisoformat(row["last_credit"])
    except ValueError:
        last = moment
    minutes = int((moment - last).total_seconds() // 60)
    await DBService.pool.execute(
        "DELETE FROM vc_sessions WHERE user_id = ?", (user_id,)
    )
    await DBService.pool.commit()
    if minutes > 0:
        try:
            await record_event(user_id, "vc_minute", minutes)
        except Exception:
            logger.exception("VC滞在の記録に失敗 user=%s", user_id)
    return max(0, minutes)


async def flush_vc_sessions(now: datetime | None = None) -> int:
    """接続中の全員に経過分を加算 (5分ごとの定期精算用)。戻り値は加算した人数。"""
    await ensure_schema()
    moment = now or datetime.now(JST)
    cursor = await DBService.pool.execute(
        "SELECT user_id, last_credit FROM vc_sessions"
    )
    rows = await cursor.fetchall()
    await cursor.close()
    credited = 0
    for r in rows:
        try:
            last = datetime.fromisoformat(r["last_credit"])
        except ValueError:
            continue
        minutes = int((moment - last).total_seconds() // 60)
        if minutes <= 0:
            continue
        try:
            await record_event(r["user_id"], "vc_minute", minutes)
        except Exception:
            logger.exception("VC滞在の定期精算に失敗 user=%s", r["user_id"])
            continue
        new_last = (last + timedelta(minutes=minutes)).isoformat()
        await DBService.pool.execute(
            "UPDATE vc_sessions SET last_credit = ? WHERE user_id = ?",
            (new_last, r["user_id"]),
        )
        credited += 1
    if credited:
        await DBService.pool.commit()
    return credited
