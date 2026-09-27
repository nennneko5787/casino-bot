"""株価エンジン: 幾何ランダムウォーク + 売買 + 管理者銘柄追加。

各銘柄は独立した乱数で更新されるため、同じように上がり下がりしない。
mu/sigma は管理者が数値で直接指定できる (原案) ほか、
簡単プリセット (おとなしい/ふつう/荒い) で sigma を決めることもできる。
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from services.database import DBService

TICKER_RE = re.compile(r"^[A-Z0-9]{1,10}$")
HISTORY_KEEP = 200

# 簡単プリセット: sigma の値のみ決める (mu は別途数値指定、省略時 0)
VOL_PRESETS: dict[str, float] = {
    "calm": 0.02,  # おとなしい
    "normal": 0.05,  # ふつう
    "wild": 0.10,  # 荒い
}

# おまかせランダム用の値域
RANDOM_MU_RANGE = (-0.002, 0.003)
RANDOM_IMPACTS = (0.0002, 0.0003, 0.0005, 0.0008, 0.001, 0.002)


def random_preset(rng: random.Random | None = None) -> tuple[float, float, float]:
    """おまかせランダムの (mu, sigma, impact) を生成する。"""
    rng = rng or random.Random()
    mu = round(rng.uniform(*RANDOM_MU_RANGE), 6)
    sigma = rng.choice(list(VOL_PRESETS.values()))
    impact = rng.choice(RANDOM_IMPACTS)
    return mu, sigma, impact


@dataclass(kw_only=True, slots=True)
class Stock:
    ticker: str
    display_name: str
    price: int
    mu: float
    sigma: float
    impact: float
    is_active: bool


def normalize_ticker(raw: str) -> str:
    """入力を ticker 形式に正規化。不正なら ValueError。

    オートコンプリートの表示名 (例: "NEKO (800通貨) [取扱停止]") や
    ポートフォリオ表示 (例: "`NEKO`") をコピペしても通るよう、
    余分な文字が混ざっている場合は先頭の英数字トークンを抜き出す。
    """
    cleaned = raw.strip().upper().strip("`'\"")
    if TICKER_RE.match(cleaned):
        return cleaned
    if re.search(r"[^A-Z0-9]", cleaned):
        m = re.search(r"[A-Z0-9]{1,10}", cleaned)
        if m:
            return m.group(0)
    raise ValueError("tickerは英数字1〜10文字で指定してください")


def step_price(price: int, mu: float, sigma: float, rng: random.Random) -> int:
    """幾何ランダムウォークで1歩進める。下限1。"""
    shock = rng.gauss(0.0, 1.0)
    next_price = price * math.exp((mu - sigma * sigma / 2) + sigma * shock)
    return max(1, round(next_price))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_stock(row) -> Stock:
    return Stock(
        ticker=row["ticker"],
        display_name=row["display_name"],
        price=row["price"],
        mu=row["mu"],
        sigma=row["sigma"],
        impact=row["impact"],
        is_active=bool(row["is_active"]),
    )


async def get_stocks(*, active_only: bool = False) -> list[Stock]:
    sql = "SELECT * FROM stocks"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY ticker"
    cursor = await DBService.pool.execute(sql)
    rows = await cursor.fetchall()
    await cursor.close()
    return [_row_to_stock(r) for r in rows]


async def get_stock(ticker: str) -> Stock | None:
    cursor = await DBService.pool.execute(
        "SELECT * FROM stocks WHERE ticker = ?", (ticker,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    return _row_to_stock(row) if row else None


async def get_history(ticker: str, limit: int = 100) -> list[tuple[str, int]]:
    """(created_at, price) を古い順で返す。"""
    limit = max(1, min(limit, HISTORY_KEEP))
    cursor = await DBService.pool.execute(
        "SELECT created_at, price FROM stock_history "
        "WHERE ticker = ? ORDER BY id DESC LIMIT ?",
        (ticker, limit),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [(r["created_at"], r["price"]) for r in reversed(rows)]


async def apply_impact(ticker: str, qty: int) -> tuple[int, float]:
    """需給影響を即時反映。qty>0=買い(上昇)/qty<0=売り(下落)。

    戻り値は (新価格, 変動率)。履歴にも点を打つ。
    """
    ticker = normalize_ticker(ticker)
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    rate = stock.impact * qty
    new_price = max(1, round(stock.price * (1 + rate)))
    now = _now()
    await DBService.pool.execute(
        "UPDATE stocks SET price = ?, updated_at = ? WHERE ticker = ?",
        (new_price, now, ticker),
    )
    await DBService.pool.execute(
        "INSERT INTO stock_history (ticker, price, created_at) VALUES (?, ?, ?)",
        (ticker, new_price, now),
    )
    await DBService.pool.commit()
    return new_price, (new_price - stock.price) / stock.price


def check_impact(impact: float) -> float:
    """impact の範囲チェック。"""
    if not 0.0 <= impact <= 0.01:
        raise ValueError("impactは0〜0.01の範囲で指定してください")
    return impact


async def tick_once(rng: random.Random | None = None) -> list[Stock]:
    """上場中 (is_active=1) の全銘柄を独立した乱数で1歩進める。"""
    rng = rng or random.Random()
    stocks = await get_stocks(active_only=True)
    now = _now()
    updated: list[Stock] = []
    for stock in stocks:
        new_price = step_price(stock.price, stock.mu, stock.sigma, rng)
        await DBService.pool.execute(
            "UPDATE stocks SET price = ?, updated_at = ? WHERE ticker = ?",
            (new_price, now, stock.ticker),
        )
        await DBService.pool.execute(
            "INSERT INTO stock_history (ticker, price, created_at) VALUES (?, ?, ?)",
            (stock.ticker, new_price, now),
        )
        await DBService.pool.execute(
            "DELETE FROM stock_history WHERE ticker = ? AND id NOT IN "
            "(SELECT id FROM stock_history WHERE ticker = ? "
            "ORDER BY id DESC LIMIT ?)",
            (stock.ticker, stock.ticker, HISTORY_KEEP),
        )
        stock.price = new_price
        updated.append(stock)
    await DBService.pool.commit()
    return updated


async def add_ticker(
    ticker: str,
    price: int,
    mu: float = 0.0,
    sigma: float = 0.05,
    impact: float = 0.0005,
) -> Stock:
    """管理者用: 新規銘柄を追加。重複・不正値は例外。"""
    ticker = normalize_ticker(ticker)
    if price < 1:
        raise ValueError("開始価格は1以上にしてください")
    if not -1.0 <= mu <= 1.0:
        raise ValueError("muは-1.0〜1.0の範囲で指定してください")
    if not 0.0 < sigma <= 1.0:
        raise ValueError("sigmaは0より大きく1.0以下で指定してください")
    check_impact(impact)
    if await get_stock(ticker):
        raise ValueError(f"{ticker} は既に存在します")
    now = _now()
    await DBService.pool.execute(
        "INSERT INTO stocks "
        "(ticker, display_name, price, mu, sigma, impact, is_active, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        (ticker, ticker, price, mu, sigma, impact, now),
    )
    await DBService.pool.execute(
        "INSERT INTO stock_history (ticker, price, created_at) VALUES (?, ?, ?)",
        (ticker, price, now),
    )
    await DBService.pool.commit()
    stock = await get_stock(ticker)
    assert stock is not None
    return stock


async def set_active(ticker: str, active: bool) -> Stock:
    ticker = normalize_ticker(ticker)
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    await DBService.pool.execute(
        "UPDATE stocks SET is_active = ?, updated_at = ? WHERE ticker = ?",
        (1 if active else 0, _now(), ticker),
    )
    await DBService.pool.commit()
    stock.is_active = active
    return stock


async def update_params(
    ticker: str,
    mu: float | None = None,
    sigma: float | None = None,
    impact: float | None = None,
) -> Stock:
    ticker = normalize_ticker(ticker)
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    if mu is not None:
        if not -1.0 <= mu <= 1.0:
            raise ValueError("muは-1.0〜1.0の範囲で指定してください")
        stock.mu = mu
    if sigma is not None:
        if not 0.0 < sigma <= 1.0:
            raise ValueError("sigmaは0より大きく1.0以下で指定してください")
        stock.sigma = sigma
    if impact is not None:
        stock.impact = check_impact(impact)
    await DBService.pool.execute(
        "UPDATE stocks SET mu = ?, sigma = ?, impact = ?, updated_at = ? "
        "WHERE ticker = ?",
        (stock.mu, stock.sigma, stock.impact, _now(), ticker),
    )
    await DBService.pool.commit()
    return stock


async def resolve_sigma(sigma: float | None, preset: str | None) -> float:
    """簡単プリセットと数値指定の解決。preset優先、どちらも無ければ既定0.05。

    preset="random" の場合は3段階からランダムに選ぶ。
    """
    if preset is not None:
        if preset == "random":
            return random.Random().choice(list(VOL_PRESETS.values()))
        if preset not in VOL_PRESETS:
            raise ValueError("presetは calm/normal/wild/random から選んでください")
        return VOL_PRESETS[preset]
    if sigma is not None:
        if not 0.0 < sigma <= 1.0:
            raise ValueError("sigmaは0より大きく1.0以下で指定してください")
        return sigma
    return VOL_PRESETS["normal"]


async def buy(user_id: int, ticker: str, qty: int) -> tuple[int, int, float]:
    """購入。戻り値は (約定単価, 合計金額, 需給変動率)。

    残高の増減も直接SQLで行うため Discord オブジェクトは不要。
    残高不足時は LookupError (Cog側で AmountNotEnough に変換する)。
    約定後に需給影響を即時反映する (約定単価は影響前の価格)。
    """
    ticker = normalize_ticker(ticker)
    if qty < 1:
        raise ValueError("数量は1以上にしてください")
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    if not stock.is_active:
        raise ValueError(f"{ticker} は現在取扱停止中です")

    # ダミーMemberなしで残高を扱うため、money層と同じSQLで直接読む
    cursor = await DBService.pool.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    if not row:
        await DBService.pool.execute("INSERT INTO users(id) VALUES (?)", (user_id,))
        await DBService.pool.commit()
        balance = 100
    else:
        balance = row["amount"]

    cost = stock.price * qty
    if balance < cost:
        # 金額表示は呼び出し側で AmountNotEnough に変換させる
        raise LookupError(f"残高不足: 必要 {cost}")

    # 残高更新は money 層の User dataclass を介さず直接SQL (Bot未起動の検証でも動く)
    await DBService.pool.execute(
        "UPDATE users SET amount = amount - ? WHERE id = ?", (cost, user_id)
    )
    cursor = await DBService.pool.execute(
        "SELECT * FROM holdings WHERE user_id = ? AND ticker = ?",
        (user_id, ticker),
    )
    holding = await cursor.fetchone()
    await cursor.close()
    if holding:
        new_qty = holding["qty"] + qty
        new_avg = (holding["avg_cost"] * holding["qty"] + cost) // new_qty
        await DBService.pool.execute(
            "UPDATE holdings SET qty = ?, avg_cost = ? "
            "WHERE user_id = ? AND ticker = ?",
            (new_qty, new_avg, user_id, ticker),
        )
    else:
        await DBService.pool.execute(
            "INSERT INTO holdings (user_id, ticker, qty, avg_cost) VALUES (?, ?, ?, ?)",
            (user_id, ticker, qty, stock.price),
        )
    await DBService.pool.commit()
    _, rate = await apply_impact(ticker, qty)
    return stock.price, cost, rate


async def sell(user_id: int, ticker: str, qty: int) -> tuple[int, int, int, float]:
    """売却。戻り値は (約定単価, 受取金額, 借金への自動返済額, 需給変動率).

    上場廃止銘柄も売却は可能。受取金額は借金返済優先で配分される。
    約定後に需給影響を即時反映する (約定単価は影響前の価格)。
    """
    from services.loan import apply_income

    ticker = normalize_ticker(ticker)
    if qty < 1:
        raise ValueError("数量は1以上にしてください")
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    cursor = await DBService.pool.execute(
        "SELECT * FROM holdings WHERE user_id = ? AND ticker = ?",
        (user_id, ticker),
    )
    holding = await cursor.fetchone()
    await cursor.close()
    if not holding or holding["qty"] < qty:
        raise ValueError(f"{ticker} を {qty}株保有していません")
    proceeds = stock.price * qty
    new_qty = holding["qty"] - qty
    if new_qty == 0:
        await DBService.pool.execute(
            "DELETE FROM holdings WHERE user_id = ? AND ticker = ?",
            (user_id, ticker),
        )
    else:
        await DBService.pool.execute(
            "UPDATE holdings SET qty = ? WHERE user_id = ? AND ticker = ?",
            (new_qty, user_id, ticker),
        )
    await DBService.pool.commit()
    repaid, _ = await apply_income(user_id, proceeds)
    _, rate = await apply_impact(ticker, -qty)
    return stock.price, proceeds, repaid, rate


async def get_portfolio(user_id: int) -> list[dict]:
    """保有一覧 (現在値・評価額・損益付き)。"""
    cursor = await DBService.pool.execute(
        "SELECT h.ticker, h.qty, h.avg_cost, s.price, s.is_active "
        "FROM holdings h LEFT JOIN stocks s ON s.ticker = h.ticker "
        "WHERE h.user_id = ? ORDER BY h.ticker",
        (user_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    result = []
    for r in rows:
        price = r["price"] if r["price"] is not None else 0
        market = price * r["qty"]
        cost = r["avg_cost"] * r["qty"]
        result.append(
            {
                "ticker": r["ticker"],
                "qty": r["qty"],
                "avg_cost": r["avg_cost"],
                "price": price,
                "market": market,
                "profit": market - cost,
                "is_active": bool(r["is_active"])
                if r["is_active"] is not None
                else False,
            }
        )
    return result
