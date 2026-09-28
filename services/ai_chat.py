"""AIチャット: Gemini (主) + OpenRouter (予備) のロールプレイチャット。

- モデルは DB設定 > env > 既定 gemini-2.5-flash-lite の順で解決。
  `gemini-` で始まるIDはGemini、他はOpenRouterで叩く。
  /ai-admin model (管理者限定) で再起動なしに切り替えられる。
- Gemini無料枠を守るため、日次カウンタ (太平洋時間0時リセット) と
  全体RPMスロットル (最小間隔) を持つ。上限到達・429時はその日
  Geminiを休止してOpenRouterに自動フォールバックする。
- 料金は固定×通貨価値指数連動: price = ceil(BASE * 100 / index)。
  通貨安(指数低)→高額、通貨高→割安。残高不足は LookupError。
  AI接続失敗時は徴収済み料金を返金する。
- OpenRouterのsafetySettingsは送ると400になるため送らない。
  Gemini側は HarmCategory を BLOCK_ONLY_HIGH に緩めてRP誤爆を減らす。
- ユーザー毎に履歴保持 (直近 HISTORY_KEEP 件)。/ai clear で削除可。
- system指示は管理者既定 + ユーザー別persona上書き。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import dotenv

from services.database import DBService

dotenv.load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash-lite"
MODEL = os.environ.get("AI_MODEL", DEFAULT_MODEL)
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/free")
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
API_URL = os.environ.get("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_URL = os.environ.get(
    "GEMINI_API_URL", "https://generativelanguage.googleapis.com/v1beta"
)
GEMINI_TEMPERATURE = float(os.environ.get("GEMINI_TEMPERATURE", "1.0"))
GEMINI_MAX_TOKENS = int(os.environ.get("GEMINI_MAX_TOKENS", "512"))
GEMINI_SAFETY_THRESHOLD = os.environ.get("GEMINI_SAFETY_THRESHOLD", "BLOCK_ONLY_HIGH")
# 無料枠防御: 日次上限 (余裕を見て定格1000より少なめ) と全体RPM間隔。
GEMINI_DAILY_LIMIT = int(os.environ.get("GEMINI_DAILY_LIMIT", "900"))
GEMINI_MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL_SEC", "4.0"))
BASE_PRICE = int(os.environ.get("AI_BASE_PRICE", "10"))
HISTORY_KEEP = int(os.environ.get("AI_HISTORY_KEEP", "20"))
TIMEOUT = float(os.environ.get("AI_TIMEOUT_SEC", "60"))

GEMINI_SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)

BUILTIN_SYSTEM = (
    "あなたはカジノサーバーの常連客として振る舞うロールプレイAIです。"
    "フレンドリーで少しふざけた口調で、日本語で短めに返答してください。"
)

_tables_ensured = False


async def ensure_tables() -> None:
    global _tables_ensured
    if _tables_ensured:
        return
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS ai_histories ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT NOT NULL, "
        "role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    await DBService.pool.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_histories_user "
        "ON ai_histories (user_id, id)"
    )
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS ai_personas ("
        "user_id BIGINT PRIMARY KEY, system_prompt TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS ai_global (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    await DBService.pool.commit()
    _tables_ensured = True


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def current_index() -> float:
    """最新の通貨価値指数。データなしは100。"""
    from services import stocks

    try:
        points = await stocks.get_currency_index(1)
    except Exception:  # noqa: BLE001 - DB不調時は指数100扱いで継続
        return 100.0
    if not points:
        return 100.0
    return float(points[-1][1])


def price_for(index: float) -> int:
    """固定×指数の料金。index低(通貨安)→高額。最低1。"""
    idx = max(10.0, min(index, 1000.0))
    return max(1, math.ceil(BASE_PRICE * 100.0 / idx))


async def current_price() -> tuple[int, float]:
    index = await current_index()
    return price_for(index), index


async def charge(user_id: int, price: int) -> None:
    """残高から徴収。不足時は LookupError (Cog側で AmountNotEnough に変換)。"""
    await ensure_tables()
    cursor = await DBService.pool.execute(
        "SELECT amount FROM users WHERE id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        await DBService.pool.execute("INSERT INTO users(id) VALUES (?)", (user_id,))
        await DBService.pool.commit()
        balance = 100
    else:
        balance = row["amount"]
    if balance < price:
        raise LookupError(f"残高不足: 必要 {price}")
    await DBService.pool.execute(
        "UPDATE users SET amount = amount - ? WHERE id = ?", (price, user_id)
    )
    await DBService.pool.commit()


async def get_history(user_id: int, limit: int = HISTORY_KEEP) -> list[dict]:
    await ensure_tables()
    cursor = await DBService.pool.execute(
        "SELECT role, content FROM ai_histories WHERE user_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (user_id, max(1, min(limit, 100))),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    history = [{"role": r["role"], "content": r["content"]} for r in rows]
    history.reverse()
    return history


async def append_history(user_id: int, role: str, content: str) -> None:
    await ensure_tables()
    await DBService.pool.execute(
        "INSERT INTO ai_histories (user_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, role, content[:4000], _now()),
    )
    await DBService.pool.execute(
        "DELETE FROM ai_histories WHERE user_id = ? AND id NOT IN "
        "(SELECT id FROM ai_histories WHERE user_id = ? "
        "ORDER BY id DESC LIMIT ?)",
        (user_id, user_id, HISTORY_KEEP),
    )
    await DBService.pool.commit()


async def clear_history(user_id: int) -> int:
    await ensure_tables()
    cursor = await DBService.pool.execute(
        "DELETE FROM ai_histories WHERE user_id = ?", (user_id,)
    )
    n = cursor.rowcount if cursor.rowcount else 0
    await DBService.pool.commit()
    await cursor.close()
    return n


async def get_persona(user_id: int) -> str | None:
    await ensure_tables()
    cursor = await DBService.pool.execute(
        "SELECT system_prompt FROM ai_personas WHERE user_id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row["system_prompt"] if row else None


async def set_persona(user_id: int, prompt: str) -> None:
    await ensure_tables()
    await DBService.pool.execute(
        "INSERT INTO ai_personas (user_id, system_prompt, updated_at) "
        "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
        "system_prompt = excluded.system_prompt, updated_at = excluded.updated_at",
        (user_id, prompt[:2000], _now()),
    )
    await DBService.pool.commit()


async def clear_persona(user_id: int) -> None:
    await ensure_tables()
    await DBService.pool.execute(
        "DELETE FROM ai_personas WHERE user_id = ?", (user_id,)
    )
    await DBService.pool.commit()


async def get_global_system() -> str | None:
    await ensure_tables()
    cursor = await DBService.pool.execute(
        "SELECT value FROM ai_global WHERE key = 'system_default'"
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row["value"] if row else None


async def set_global_system(prompt: str) -> None:
    await ensure_tables()
    await DBService.pool.execute(
        "INSERT INTO ai_global (key, value) VALUES ('system_default', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (prompt[:2000],),
    )
    await DBService.pool.commit()


async def get_model() -> str | None:
    """DBに保存されたモデルID。未設定ならNone。"""
    await ensure_tables()
    cursor = await DBService.pool.execute(
        "SELECT value FROM ai_global WHERE key = 'model'"
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row["value"] if row else None


async def set_model(model: str) -> None:
    """モデルIDをDBに保存 (再起動なしで切替、管理者限定コマンド用)。"""
    model = model.strip()
    if not model:
        raise ValueError("モデルIDを入力してください")
    await ensure_tables()
    await DBService.pool.execute(
        "INSERT INTO ai_global (key, value) VALUES ('model', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (model[:200],),
    )
    await DBService.pool.commit()


async def resolve_model() -> str:
    """DB設定 > env(AI_MODEL) > 既定 の順でモデルIDを解決。"""
    db = await get_model()
    return db or MODEL


def is_gemini_model(model: str) -> bool:
    """Geminiで叩くべきモデルIDか。"""
    m = model.strip().lower()
    return m.startswith(("gemini-", "models/"))


async def resolve_openrouter_model() -> str:
    """フォールバック用のOpenRouterモデルID。DB値がGemini系ならenv/既定を使う。"""
    db = await get_model()
    if db and not is_gemini_model(db):
        return db
    return OPENROUTER_MODEL


# ---------- Gemini無料枠の防御 ----------


class GeminiThrottled(Exception):
    """分間制限・一時的な429。今回は見送り (フォールバック対象)。"""


class GeminiDayExhausted(Exception):
    """日次上限に到達。その日はGeminiを使わない。"""


def _pt_now():
    """太平洋時間の現在時刻。RPDリセット境界の基準。"""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/Los_Angeles"))


def _pt_today_key() -> str:
    return "gemini_rpd:" + _pt_now().date().isoformat()


async def gemini_usage_today() -> int:
    """今日 (太平洋時間) のGemini使用回数。"""
    await ensure_tables()
    cursor = await DBService.pool.execute(
        "SELECT value FROM ai_global WHERE key = ?", (_pt_today_key(),)
    )
    row = await cursor.fetchone()
    await cursor.close()
    try:
        return int(row["value"]) if row else 0
    except (ValueError, TypeError):
        return 0


async def gemini_bump() -> int:
    """使用回数を+1して返す。"""
    await ensure_tables()
    key = _pt_today_key()
    await DBService.pool.execute(
        "INSERT INTO ai_global (key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
        (key,),
    )
    await DBService.pool.commit()
    return await gemini_usage_today()


async def gemini_suspended() -> bool:
    """その日のGemini休止フラグが立っているか。"""
    await ensure_tables()
    cursor = await DBService.pool.execute(
        "SELECT value FROM ai_global WHERE key = 'gemini_rest_until'"
    )
    row = await cursor.fetchone()
    await cursor.close()
    if not row:
        return False
    try:
        until = datetime.fromisoformat(row["value"])
    except ValueError:
        return False
    moment = until
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return datetime.now(UTC) < moment


async def gemini_suspend_until_next_reset() -> None:
    """次の太平洋時間0時までGeminiを休止する。"""
    await ensure_tables()
    pt = _pt_now()
    next_midnight = (pt + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    await DBService.pool.execute(
        "INSERT INTO ai_global (key, value) VALUES ('gemini_rest_until', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (next_midnight.astimezone(UTC).isoformat(),),
    )
    await DBService.pool.commit()


_gemini_lock = asyncio.Lock()
_gemini_last_call = 0.0
_gemini_cooldown_until = 0.0


async def gemini_available() -> bool:
    """今回Geminiを使ってよいか (キー・休止・日次上限で判定)。"""
    if not GEMINI_API_KEY:
        return False
    if await gemini_suspended():
        return False
    if time.monotonic() < _gemini_cooldown_until:
        return False
    return await gemini_usage_today() < GEMINI_DAILY_LIMIT


async def _gemini_throttle() -> None:
    """全体RPMスロットル。最小間隔が空くまで待つ。"""
    global _gemini_last_call
    async with _gemini_lock:
        now = time.monotonic()
        wait = GEMINI_MIN_INTERVAL - (now - _gemini_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _gemini_last_call = time.monotonic()


def is_safety_dump(reply: str) -> bool:
    """モデル側の安全判定ダンプのみの応答か。通常の雑談に両マーカーは出ない。"""
    low = reply.lower()
    if "user safety" not in low or "response safety" not in low:
        return False
    rest = re.sub(r"user safety|response safety", "", low)
    rest = re.sub(r"[^a-z]", "", rest)
    return rest in ("safe", "safesafe") or len(rest) < 10


async def resolve_system(user_id: int) -> str:
    """ユーザーpersona > 管理者既定(DB) > env > ビルトイン。"""
    persona = await get_persona(user_id)
    if persona:
        return persona
    glob = await get_global_system()
    if glob:
        return glob
    env = os.environ.get("OPENROUTER_SYSTEM_DEFAULT", "").strip()
    return env or BUILTIN_SYSTEM


def _extract_error_detail(code: int, body: bytes) -> str:
    """OpenRouterのエラーボディから理由文を抜き出す。なければ空文字。"""
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
    return ""


def _post(payload: dict) -> str:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return res.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read()
        detail = _extract_error_detail(e.code, body)
        logger.warning(
            "OpenRouter HTTP %s: %s (model=%s)",
            e.code, detail or body[:500], payload.get("model"),
        )
        if detail:
            raise ValueError(f"AIへの接続に失敗しました ({e.code}: {detail})") from e
        raise ValueError(f"AIへの接続に失敗しました ({e})") from e


def _post_gemini(model: str, payload: dict) -> str:
    model_id = model.strip().removeprefix("models/")
    url = f"{GEMINI_API_URL}/models/{model_id}:generateContent"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return res.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        global _gemini_cooldown_until
        body = e.read()
        detail = _extract_error_detail(e.code, body)
        logger.warning(
            "Gemini HTTP %s: %s (model=%s)",
            e.code, detail or body[:500], model,
        )
        if e.code == 429:
            # 分間制限は短いクールダウン、日次上限ならその日は休止する。
            _gemini_cooldown_until = time.monotonic() + 65.0
            raise GeminiThrottled(
                "Geminiが混み合っています。代替モデルで応答します"
            ) from e
        if detail:
            raise ValueError(f"AIへの接続に失敗しました ({e.code}: {detail})") from e
        raise ValueError(f"AIへの接続に失敗しました ({e})") from e


def _gemini_contents(
    system: str, history: list[dict], text: str
) -> tuple[dict | None, list[dict]]:
    """system指示と履歴をGemini形式に変換する。"""
    instruction = {"parts": [{"text": system}]} if system else None
    contents: list[dict] = []
    for h in history:
        role = "model" if h.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": h.get("content", "")}]})
    contents.append({"role": "user", "parts": [{"text": text}]})
    return instruction, contents


def _gemini_payload(
    model: str, system: str, history: list[dict], text: str
) -> dict:
    instruction, contents = _gemini_contents(system, history, text)
    payload: dict = {
        "contents": contents,
        "generationConfig": {
            "temperature": GEMINI_TEMPERATURE,
            "maxOutputTokens": GEMINI_MAX_TOKENS,
        },
        "safetySettings": [
            {"category": cat, "threshold": GEMINI_SAFETY_THRESHOLD}
            for cat in GEMINI_SAFETY_CATEGORIES
        ],
    }
    if instruction:
        payload["systemInstruction"] = instruction
    return payload


def _gemini_parse(raw: str) -> str:
    """generateContent応答から本文を抜き出す。安全ブロックは ValueError。"""
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ValueError("AIの応答を解釈できませんでした") from e
    try:
        feedback = data.get("promptFeedback") or {}
        if (feedback.get("blockReason") or "") not in ("", "BLOCK_REASON_UNSPECIFIED"):
            raise ValueError("AIが安全フィルタで止めました。別の言い方で試してね")
        candidate = (data.get("candidates") or [])[0]
        if (candidate.get("finishReason") or "") == "SAFETY":
            raise ValueError("AIが安全フィルタで止めました。別の言い方で試してね")
        parts = (candidate.get("content") or {}).get("parts") or []
        reply = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    except (KeyError, IndexError, AttributeError) as e:
        raise ValueError("AIの応答を解釈できませんでした") from e
    if not reply:
        raise ValueError("AIから空の応答が返りました")
    return reply


async def refund(user_id: int, price: int) -> None:
    """徴収済み料金を返金する (AI失敗時用)。"""
    if price < 1:
        return
    await ensure_tables()
    await DBService.pool.execute(
        "INSERT OR IGNORE INTO users(id) VALUES (?)", (user_id,)
    )
    await DBService.pool.execute(
        "UPDATE users SET amount = amount + ? WHERE id = ?", (price, user_id)
    )
    await DBService.pool.commit()


async def gemini_ask(
    model: str, system: str, history: list[dict], text: str
) -> str:
    """Geminiで1往復。429は GeminiThrottled、日次上限は GeminiDayExhausted。"""
    if not GEMINI_API_KEY:
        raise GeminiDayExhausted("GEMINI_API_KEY が未設定です")
    if await gemini_suspended():
        raise GeminiDayExhausted("Geminiは本日上限のため休止中です")
    if await gemini_usage_today() >= GEMINI_DAILY_LIMIT:
        await gemini_suspend_until_next_reset()
        raise GeminiDayExhausted("Geminiは本日上限のため休止中です")
    await _gemini_throttle()
    payload = _gemini_payload(model, system, history, text)
    try:
        raw = await asyncio.to_thread(_post_gemini, model, payload)
    except (GeminiThrottled, ValueError):
        raise
    except Exception as e:
        raise ValueError(f"AIへの接続に失敗しました ({e})") from e
    await gemini_bump()
    return _gemini_parse(raw)


async def openrouter_ask(
    model: str, system: str, history: list[dict], text: str
) -> str:
    """OpenRouterで1往復。失敗は ValueError。"""
    if not API_KEY:
        raise ValueError(
            "OPENROUTER_API_KEY が未設定です。管理者に連絡してください"
        )
    messages = [{"role": "system", "content": system}]
    messages += history
    messages.append({"role": "user", "content": text})
    payload = {"model": model, "messages": messages}
    try:
        raw = await asyncio.to_thread(_post, payload)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"AIへの接続に失敗しました ({e})") from e
    try:
        data = json.loads(raw)
        reply = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, ValueError, AttributeError) as e:
        raise ValueError("AIの応答を解釈できませんでした") from e
    if not reply:
        raise ValueError("AIから空の応答が返りました")
    if is_safety_dump(reply):
        logger.warning("AI safety dump (user model=%s)", payload["model"])
        raise ValueError(
            "AIが安全判定のみを返しました。別の言い方で試してね"
        )
    return reply


async def _store_roundtrip(user_id: int, text: str, reply: str) -> None:
    await append_history(user_id, "user", text)
    await append_history(user_id, "assistant", reply)


async def ask(user_id: int, text: str) -> tuple[str, bool]:
    """履歴+system付きで1往復。戻り値は (本文, 代替モデル使用か)。

    Gemini (主) → OpenRouter (予備) の順に試す。両方使えない場合は ValueError。
    """
    system = await resolve_system(user_id)
    history = await get_history(user_id)
    model = await resolve_model()
    if is_gemini_model(model) and await gemini_available():
        try:
            reply = await gemini_ask(model, system, history, text)
        except (GeminiThrottled, GeminiDayExhausted, ValueError):
            pass
        else:
            await _store_roundtrip(user_id, text, reply)
            return reply, False
    fallback_model = await resolve_openrouter_model()
    reply = await openrouter_ask(fallback_model, system, history, text)
    await _store_roundtrip(user_id, text, reply)
    return reply, is_gemini_model(model)
