"""AIチャット: OpenRouter経由のロールプレイチャット。

- モデルは DB設定 > env OPENROUTER_MODEL > 既定 openrouter/free の順で解決。
  /ai-admin model (管理者限定) で再起動なしに切り替えられる。
- 料金は固定×通貨価値指数連動: price = ceil(BASE * 100 / index)。
  通貨安(指数低)→高額、通貨高→割安。残高不足は LookupError。
  AI接続失敗時は徴収済み料金を返金する。
- モデル側の安全判定ダンプのみの応答はエラー扱い (返金対象)。
  ※OpenRouterのchat completionsはsafetySettingsを受け付けない
  (送ると400) ため、安全設定の緩和は行わない。
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
import urllib.error
import urllib.request
from datetime import UTC, datetime

import dotenv

from services.database import DBService

dotenv.load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "openrouter/free"
MODEL = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
API_URL = os.environ.get("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")
BASE_PRICE = int(os.environ.get("AI_BASE_PRICE", "10"))
HISTORY_KEEP = int(os.environ.get("AI_HISTORY_KEEP", "20"))
TIMEOUT = float(os.environ.get("AI_TIMEOUT_SEC", "60"))

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
    """DB設定 > env > 既定 の順でモデルIDを解決。"""
    db = await get_model()
    return db or MODEL


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


async def ask(user_id: int, text: str) -> str:
    """履歴+system付きで1往復。APIキー未設定は ValueError。"""
    if not API_KEY:
        raise ValueError(
            "OPENROUTER_API_KEY が未設定です。管理者に連絡してください"
        )
    system = await resolve_system(user_id)
    history = await get_history(user_id)
    messages = [{"role": "system", "content": system}]
    messages += history
    messages.append({"role": "user", "content": text})
    payload = {"model": await resolve_model(), "messages": messages}
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
        logger.warning("AI safety dump (user=%s model=%s)", user_id, payload["model"])
        raise ValueError(
            "AIが安全判定のみを返しました。別の言い方で試してね"
        )
    await append_history(user_id, "user", text)
    await append_history(user_id, "assistant", reply)
    return reply
