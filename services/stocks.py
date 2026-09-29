"""株価エンジン: 幾何ランダムウォーク + 売買 + 銘柄追加。

各銘柄は所属する株式市場 (markets) を持ち、市場の値動きレンジ
(mu/sigma/impact) の中で5分ごとにパラメータが再抽選される。
mu/sigma/impact の個別指定はできない (create/add/paramsでの指定は廃止)。

ユーザーは会社を設立できる (レベル連動枠、設立手数料+投資金、
開始株価=投資金、創業者株の付与 + 売買ロイヤリティ + 値上がり配当あり)。
自社株は買増不可・売却のみ可。投資額ランクは手数料・創業者株・
配当率に効き、値動きには影響しない。
会社には金庫口座 (company_accounts) があり、招待されたメンバーが
預入・引出 (引出は設立者承認制) できる。金庫には毎日所得税がかかる。
"""

from __future__ import annotations

import logging
import math
import random
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from services.database import DBService

logger = logging.getLogger(__name__)

TICKER_RE = re.compile(r"^[A-Z0-9]{1,15}$")
HISTORY_KEEP = 200

# SQLite INTEGER は符号付き64bit (最大 9223372036854775807)。
# 幾何ランダムウォークは指数関数的に膨らむため、上限なしだと
# Python int -> SQLite への変換で OverflowError になる。
# tick が止まると全銘柄が更新されなくなるので価格に上限を設ける。
# 案2: 上限を1兆→1000兆に引き上げ (9.22e18 まで1桁の余裕を残す)。
# 売買の cost=price*qty は別途 SQLITE_MAX_INT チェックで弾く。
SQLITE_MAX_INT = 9223372036854775807
MAX_PRICE = 1_000_000_000_000_000  # 1000兆

# 平均回帰の基準価格・強さ。高額帯ほど mu を下向きに補正し膨張を抑える。
REFERENCE_PRICE = 1000
MEAN_REVERSION_K = 0.002

# 会社設立の手数料の既定 (ランクで割引される場合あり)。
FOUNDING_FEE = 1000

# 設立者メリット (既定。ランクで上乗せされる場合あり)
ROYALTY_RATE = 0.01  # 他人が自社株を売買するたび、代金のこの割合が設立者に入る
FOUNDER_SHARES_PER = 1000  # この投資額ごとに創業者株1株 (最低1株、売却のみ可)
DIVIDEND_RATE = 0.001  # tickで値上がりしたら、上昇分×保有株数×この割合を配当

# パラメータ再抽選の間隔 (tick回数。tick=1分のため5tick=5分ごと)。
RANDOMIZE_EVERY_TICKS = 5

# 既定の所属市場 (migrate/ensureでシードされる)。
DEFAULT_MARKET_ID = "MEOWDAQ"

# 1回の売買で需給により動く上限 (変動率)。自律変動 (sigma由来の
# 数%〜十数%) より小さめにして、売買で暴落/暴騰しないようにする。
MAX_TRADE_IMPACT = 0.03


@dataclass(kw_only=True, slots=True)
class Stock:
    ticker: str
    display_name: str
    price: int
    mu: float
    sigma: float
    impact: float
    is_active: bool
    owner_id: int | None = None  # None=運営銘柄、数値=ユーザー企業の設立者
    market_id: str = DEFAULT_MARKET_ID  # 所属市場
    rank: str | None = None  # 設立時の投資額ランク (S/A/B/C)。運営銘柄はNone
    start_price: int = 100  # 開始価格 (表示用)


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
        m = re.search(r"[A-Z0-9]{1,15}", cleaned)
        if m:
            return m.group(0)
    raise ValueError("tickerは英数字1〜15文字で指定してください")


def step_price(
    price: int,
    mu: float,
    sigma: float,
    rng: random.Random,
    ref_price: float | None = None,
    mean_k: float | None = None,
) -> int:
    """幾何ランダムウォークで1歩進める。下限1・上限MAX_PRICE。

    丸めは確率的 (stochastic rounding) にすることで期待値を保ちつつ、
    価格1が吸着点にならないようにする (1でも毎tick数%の確率で2に脱出)。
    高額帯では平均回帰で mu を下向き補正し、sigma を圧縮して膨張を抑える。
    ref_price/mean_k 指定時は市場の平均回帰設定を使う。
    """
    ref = ref_price if ref_price and ref_price > 0 else REFERENCE_PRICE
    k = MEAN_REVERSION_K if mean_k is None else mean_k
    try:
        eff_mu = mu
        if price > ref:
            eff_mu -= k * math.log(price / ref)
        # 高額帯のボラ圧縮: 10倍ごとに sigma を約1割抑える
        eff_sigma = sigma
        if price > ref * 10:
            eff_sigma = sigma / (
                1.0 + 0.15 * math.log10(price / (ref * 10))
            )
        shock = rng.gauss(0.0, 1.0)
        exact = price * math.exp(
            (eff_mu - eff_sigma * eff_sigma / 2) + eff_sigma * shock
        )
    except (OverflowError, ValueError):
        # exp が発散した場合は方向だけ見て端に張り付ける
        return MAX_PRICE if mu >= 0 else 1
    try:
        stepped = math.floor(exact + rng.random())
    except (OverflowError, ValueError):
        return MAX_PRICE
    return _clamp_price(stepped)


def _clamp_price(v: int) -> int:
    """価格を SQLite に入る範囲 [1, MAX_PRICE] に丸める。"""
    try:
        iv = int(v)
    except (OverflowError, ValueError):
        return MAX_PRICE
    if iv < 1:
        return 1
    if iv > MAX_PRICE:
        return MAX_PRICE
    return iv


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_stock(row) -> Stock:
    try:
        owner_id = row["owner_id"]
    except (KeyError, IndexError):
        owner_id = None

    def _opt(key: str):
        try:
            return row[key]
        except (KeyError, IndexError):
            return None

    start_price = _opt("start_price")
    return Stock(
        ticker=row["ticker"],
        display_name=row["display_name"],
        price=row["price"],
        mu=row["mu"],
        sigma=row["sigma"],
        impact=row["impact"],
        is_active=bool(row["is_active"]),
        owner_id=int(owner_id) if owner_id is not None else None,
        market_id=str(_opt("market_id") or DEFAULT_MARKET_ID),
        rank=_opt("rank"),
        start_price=int(start_price) if start_price is not None else 100,
    )


@dataclass(kw_only=True, slots=True)
class Market:
    """株式市場: 値動きレンジの束。

    mu/sigma/impact は5分ごとにこの範囲で再抽選される。
    jitter は銘柄固有の上乗せ幅 (muに±jitterを加える)。
    """

    id: str
    display_name: str = ""
    description: str = ""
    mu_min: float = -0.001
    mu_max: float = 0.002
    sigma_min: float = 0.02
    sigma_max: float = 0.05
    impact_min: float = 0.0002
    impact_max: float = 0.0006
    jitter: float = 0.0005
    mean_ref_price: float | None = None
    mean_k: float | None = None


MARKET_ID_RE = re.compile(r"^[A-Z0-9]{1,15}$")

# 初期3市場 (migrate/ensureでシードされる正本)。
SEED_MARKETS: list[Market] = [
    Market(
        id="MEOWDAQ", display_name="MEOWDAQ",
        description="メインの穏やかな市場",
        mu_min=-0.001, mu_max=0.002,
        sigma_min=0.02, sigma_max=0.05,
        impact_min=0.0002, impact_max=0.0006, jitter=0.0005,
    ),
    Market(
        id="AABOT", display_name="AABOT",
        description="値動きの荒い市場",
        mu_min=-0.002, mu_max=0.003,
        sigma_min=0.05, sigma_max=0.10,
        impact_min=0.0005, impact_max=0.002, jitter=0.001,
    ),
    Market(
        id="OZETUDO", display_name="OZETUDO",
        description="値動きの鈍い安定市場",
        mu_min=-0.0005, mu_max=0.001,
        sigma_min=0.01, sigma_max=0.03,
        impact_min=0.0001, impact_max=0.0003, jitter=0.0002,
    ),
]

_schema_ensured = False


def normalize_market_id(raw: str) -> str:
    """市場IDを正規化。不正なら ValueError。"""
    cleaned = raw.strip().upper().strip("`'\"")
    if MARKET_ID_RE.match(cleaned):
        return cleaned
    raise ValueError("市場IDは英数字1〜15文字で指定してください")


def _row_to_market(row) -> Market:
    def _num(key: str, default: float) -> float:
        try:
            v = row[key]
        except (KeyError, IndexError):
            return default
        return float(v) if v is not None else default

    def _str(key: str, default: str = "") -> str:
        try:
            v = row[key]
        except (KeyError, IndexError):
            return default
        return str(v) if v is not None else default

    try:
        mean_ref = row["mean_ref_price"]
    except (KeyError, IndexError):
        mean_ref = None
    try:
        mean_k = row["mean_k"]
    except (KeyError, IndexError):
        mean_k = None
    return Market(
        id=row["id"],
        display_name=row["display_name"],
        description=_str("description"),
        mu_min=_num("mu_min", -0.001),
        mu_max=_num("mu_max", 0.002),
        sigma_min=_num("sigma_min", 0.02),
        sigma_max=_num("sigma_max", 0.05),
        impact_min=_num("impact_min", 0.0002),
        impact_max=_num("impact_max", 0.0006),
        jitter=_num("jitter", 0.0),
        mean_ref_price=float(mean_ref) if mean_ref is not None else None,
        mean_k=float(mean_k) if mean_k is not None else None,
    )


async def ensure_market_schema() -> None:
    """markets表 + stocks拡張列の保険 (alembic未適用でも動く)。

    本番DBは古いrevisionで止まっていることがあるため、
    不足列はここで足し、初期3市場をシードする。
    """
    global _schema_ensured
    if _schema_ensured:
        return
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS markets ("
        "id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
        "description TEXT NOT NULL DEFAULT '', "
        "mu_min REAL NOT NULL DEFAULT 0, mu_max REAL NOT NULL DEFAULT 0, "
        "sigma_min REAL NOT NULL DEFAULT 0.05, "
        "sigma_max REAL NOT NULL DEFAULT 0.05, "
        "impact_min REAL NOT NULL DEFAULT 0.0005, "
        "impact_max REAL NOT NULL DEFAULT 0.0005, "
        "jitter REAL NOT NULL DEFAULT 0, "
        "hp_max INTEGER NOT NULL DEFAULT 100, "
        "warning_hp INTEGER NOT NULL DEFAULT 30, "
        "dmg_per_pct REAL NOT NULL DEFAULT 1.5, "
        "recover_per_pct REAL NOT NULL DEFAULT 2.0, "
        "rescue_hp_per_100 REAL NOT NULL DEFAULT 10.0, "
        "rescue_hours REAL NOT NULL DEFAULT 48.0, "
        "zero_grace_hours REAL NOT NULL DEFAULT 48.0, "
        "drop_threshold_pct REAL NOT NULL DEFAULT 5.0, "
        "mean_ref_price REAL NULL, mean_k REAL NULL, "
        "updated_at TEXT NOT NULL)"
    )
    cursor = await DBService.pool.execute("PRAGMA table_info(stocks)")
    cols = {r["name"] for r in await cursor.fetchall()}
    await cursor.close()
    if "market_id" not in cols:
        await DBService.pool.execute("ALTER TABLE stocks ADD COLUMN market_id TEXT NULL")
    if "rank" not in cols:
        await DBService.pool.execute("ALTER TABLE stocks ADD COLUMN rank TEXT NULL")
    now = _now()
    for m in SEED_MARKETS:
        await DBService.pool.execute(
            "INSERT OR IGNORE INTO markets "
            "(id, display_name, description, mu_min, mu_max, sigma_min, sigma_max, "
            "impact_min, impact_max, jitter, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (m.id, m.display_name, m.description, m.mu_min, m.mu_max,
              m.sigma_min, m.sigma_max, m.impact_min, m.impact_max, m.jitter,
              now),
        )
    await DBService.pool.execute(
        "UPDATE stocks SET market_id = ? WHERE market_id IS NULL",
        (DEFAULT_MARKET_ID,),
    )
    await DBService.pool.execute(
        "UPDATE stocks SET rank = 'B' WHERE rank IS NULL"
    )
    await DBService.pool.commit()
    _schema_ensured = True


async def get_markets() -> list[Market]:
    await ensure_market_schema()
    cursor = await DBService.pool.execute("SELECT * FROM markets ORDER BY id")
    rows = await cursor.fetchall()
    await cursor.close()
    return [_row_to_market(r) for r in rows]


async def get_market(market_id: str) -> Market | None:
    await ensure_market_schema()
    market_id = normalize_market_id(market_id)
    cursor = await DBService.pool.execute(
        "SELECT * FROM markets WHERE id = ?", (market_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    return _row_to_market(row) if row else None


async def require_market(market_id: str) -> Market:
    market = await get_market(market_id)
    if market is None:
        raise ValueError(f"市場 {market_id.strip().upper()} は存在しません")
    return market


def _check_market_ranges(m: Market) -> None:
    if m.mu_min > m.mu_max:
        raise ValueError("mu_min は mu_max 以下にしてください")
    if not 0.0 < m.sigma_min <= m.sigma_max <= 1.0:
        raise ValueError("sigma は 0 < min <= max <= 1.0 にしてください")
    if not 0.0 <= m.impact_min <= m.impact_max <= 0.01:
        raise ValueError("impact は 0 <= min <= max <= 0.01 にしてください")
    if m.jitter < 0.0 or m.jitter > 0.01:
        raise ValueError("jitter は 0〜0.01 にしてください")
    if m.mean_ref_price is not None and m.mean_ref_price <= 0:
        raise ValueError("mean_ref_price は正にしてください")
    if m.mean_k is not None and m.mean_k < 0:
        raise ValueError("mean_k は0以上にしてください")


async def create_market(m: Market) -> Market:
    """管理者用: 株式市場を新設。ID重複・範囲不正は例外。"""
    m.id = normalize_market_id(m.id)
    if not m.display_name.strip():
        raise ValueError("表示名を入力してください")
    _check_market_ranges(m)
    await ensure_market_schema()
    if await get_market(m.id):
        raise ValueError(f"市場 {m.id} は既に存在します")
    await DBService.pool.execute(
        "INSERT INTO markets "
        "(id, display_name, description, mu_min, mu_max, sigma_min, sigma_max, "
        "impact_min, impact_max, jitter, "
        "mean_ref_price, mean_k, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (m.id, m.display_name.strip(), m.description[:500],
         m.mu_min, m.mu_max, m.sigma_min, m.sigma_max,
         m.impact_min, m.impact_max, m.jitter,
         m.mean_ref_price, m.mean_k, _now()),
    )
    await DBService.pool.commit()
    created = await get_market(m.id)
    assert created is not None
    return created


async def update_market(market_id: str, **fields) -> Market:
    """管理者用: 市場のパラメータを部分更新。未知キー・不正値は例外。"""
    market = await require_market(market_id)
    allowed = {
        "display_name", "description", "mu_min", "mu_max",
        "sigma_min", "sigma_max", "impact_min", "impact_max", "jitter",
        "mean_ref_price", "mean_k",
    }
    for key in fields:
        if key not in allowed:
            raise ValueError(f"更新できない項目です: {key}")
    for key, value in fields.items():
        setattr(market, key, value)
    if isinstance(market.display_name, str) and not market.display_name.strip():
        raise ValueError("表示名を入力してください")
    _check_market_ranges(market)
    sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
    await DBService.pool.execute(
        f"UPDATE markets SET {sets} WHERE id = ?",
        (*[fields[k] for k in fields], _now(), market.id),
    )
    await DBService.pool.commit()
    updated = await get_market(market.id)
    assert updated is not None
    return updated


async def delete_market(market_id: str) -> None:
    """管理者用: 市場を削除。所属銘柄がある場合は例外 (先にmoveが必要)。"""
    market = await require_market(market_id)
    cursor = await DBService.pool.execute(
        "SELECT COUNT(*) AS n FROM stocks WHERE market_id = ?", (market.id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row and int(row["n"]) > 0:
        raise ValueError(
            f"市場 {market.id} には銘柄が {int(row['n'])} 件あります。"
            "先に全銘柄を他市場へ移動してください"
        )
    await DBService.pool.execute("DELETE FROM markets WHERE id = ?", (market.id,))
    await DBService.pool.commit()


async def move_market(ticker: str, market_id: str) -> Stock:
    """銘柄の所属市場を変更する。値動きパラメータは新市場で引き直す。"""
    ticker = normalize_ticker(ticker)
    market = await require_market(market_id)
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    await DBService.pool.execute(
        "UPDATE stocks SET market_id = ?, mu = ?, sigma = ?, impact = ?, "
        "updated_at = ? WHERE ticker = ?",
        (market.id, *_draw_params(market, random.Random()), _now(), ticker),
    )
    await DBService.pool.commit()
    moved = await get_stock(ticker)
    assert moved is not None
    return moved


def _draw_params(market: Market, rng: random.Random) -> tuple[float, float, float]:
    """市場レンジ + 銘柄jitterで mu/sigma/impact を1組引く。"""
    mu = rng.uniform(market.mu_min, market.mu_max)
    mu += rng.uniform(-market.jitter, market.jitter)
    sigma = rng.uniform(market.sigma_min, market.sigma_max)
    impact = rng.uniform(market.impact_min, market.impact_max)
    return (
        round(mu, 6),
        round(min(max(sigma, 0.0001), 1.0), 6),
        round(min(max(impact, 0.0), 0.01), 6),
    )


async def randomize_params(
    rng: random.Random | None = None,
) -> list[Stock]:
    """上場中銘柄の mu/sigma/impact を所属市場の範囲で再抽選する。

    5分ごとの呼び出しを想定。戻り値は更新後銘柄。
    """
    await ensure_market_schema()
    rng = rng or random.Random()
    markets = {m.id: m for m in await get_markets()}
    stocks = await get_stocks(active_only=True)
    updated: list[Stock] = []
    now = _now()
    for stock in stocks:
        market = markets.get(stock.market_id)
        if market is None:
            continue
        mu, sigma, impact = _draw_params(market, rng)
        await DBService.pool.execute(
            "UPDATE stocks SET mu = ?, sigma = ?, impact = ?, updated_at = ? "
            "WHERE ticker = ?",
            (mu, sigma, impact, now, stock.ticker),
        )
        stock.mu, stock.sigma, stock.impact = mu, sigma, impact
        updated.append(stock)
    await DBService.pool.commit()
    return updated


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
    ordered = list(rows)
    ordered.reverse()
    return [(r["created_at"], r["price"]) for r in ordered]


async def get_currency_index(limit: int = 100) -> list[tuple[str, float]]:
    """通貨価値指数 (古い順)。tick毎のスナップショットから算出する。

    指数 = (起点の平均株価 / 現在の平均株価)
         × (起点の通貨総量 / 現在の通貨総量) × 100。
    株安・通貨供給減で上がる (通貨高)。供給量が記録されていない
    区間は株価成分のみで計算する。
    """
    limit = max(1, min(limit, HISTORY_KEEP))
    cursor = await DBService.pool.execute(
        "SELECT created_at, avg_price, total_supply FROM market_snapshots "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    snaps = [(r["created_at"], r["avg_price"], r["total_supply"]) for r in rows]
    snaps.reverse()
    if not snaps:
        return []
    base_avg, base_sup = snaps[0][1], snaps[0][2]
    if not base_avg or base_avg <= 0:
        return []
    points: list[tuple[str, float]] = []
    for created_at, avg_price, supply in snaps:
        if not avg_price or avg_price <= 0:
            continue
        idx = base_avg / avg_price * 100
        if base_sup > 0 and supply > 0:
            idx *= base_sup / supply
        points.append((created_at, idx))
    return points


def calc_impact_price(price: int, impact: float, qty: int) -> tuple[int, float]:
    """需給影響後の価格を計算する (DB書き込みなし)。

    qty>0=買い(上昇)/qty<0=売り(下落)。apply_impact と同じ式で、
    売買の約定価格を「影響後の価格」にするための純粋関数。
    戻り値は (新価格, 変動率)。
    """
    rate = dampen_impact_rate(impact * qty)
    new_price = _clamp_price(round(price * (1 + rate)))
    if rate != 0.0 and new_price == price:
        # 丸めで同値になる場合も最低1は動かす (価格1の吸着防止)。下限1。
        new_price = _clamp_price(price + (1 if rate > 0 else -1))
    return new_price, rate


async def apply_impact(ticker: str, qty: int) -> tuple[int, float]:
    """需給影響を即時反映。qty>0=買い(上昇)/qty<0=売り(下落)。

    少量注文では impact × 数量どおりに動くが、大量注文は逓減させ、
    MAX_TRADE_IMPACT (±3%) に漸近させる。自律変動 (tick) が主役で
    売買では暴落/暴騰しないようにするための措置。

    戻り値は (新価格, 変動率)。履歴にも点を打つ。
    """
    ticker = normalize_ticker(ticker)
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    new_price, rate = calc_impact_price(stock.price, stock.impact, qty)
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
    # 表示用は理論変動率を返す (価格1の最小1変動で+100%と表示されるのを避ける)
    return new_price, rate


def check_impact(impact: float) -> float:
    """impact の範囲チェック。"""
    if not 0.0 <= impact <= 0.01:
        raise ValueError("impactは0〜0.01の範囲で指定してください")
    return impact


def dampen_impact_rate(raw_rate: float) -> float:
    """線形インパクトを逓減させて上限内に収める。

    raw_rate = impact × qty (符号付き) を
    rate = raw / (1 + |raw| / MAX_TRADE_IMPACT) に変換する。
    - 少量 (|raw| << 上限) ではほぼ raw のまま
    - 大量では ±MAX_TRADE_IMPACT に漸近し、暴落/暴騰を防ぐ
    """
    if raw_rate == 0.0:
        return 0.0
    return raw_rate / (1.0 + abs(raw_rate) / MAX_TRADE_IMPACT)


async def tick_once(
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[Stock]:
    """上場中 (is_active=1) の全銘柄を独立した乱数で1歩進める。

    値上がりしたユーザー企業には創業者配当を付与する。
    戻り値は更新後銘柄の一覧。
    """
    await ensure_market_schema()
    rng = rng or random.Random()
    moment = now or datetime.now(UTC)
    now_iso = moment.isoformat()
    markets = {m.id: m for m in await get_markets()}
    stocks = await get_stocks(active_only=True)
    updated: list[Stock] = []
    for stock in stocks:
        market = markets.get(stock.market_id) or Market(id=stock.market_id)
        try:
            # DBに既に入っている異常値 (上限超え) はまず上限に丸めて回復させる
            if stock.price > MAX_PRICE or stock.price < 1:
                stock.price = _clamp_price(stock.price)
            new_price = _clamp_price(
                step_price(
                    stock.price, stock.mu, stock.sigma, rng,
                    market.mean_ref_price, market.mean_k,
                )
            )
        except Exception:
            # 1銘柄の計算失敗で全体を止めない
            logger.exception("株価tickの計算に失敗 ticker=%s", stock.ticker)
            continue
        await DBService.pool.execute(
            "UPDATE stocks SET price = ?, updated_at = ? WHERE ticker = ?",
            (new_price, now_iso, stock.ticker),
        )
        await DBService.pool.execute(
            "INSERT INTO stock_history (ticker, price, created_at) VALUES (?, ?, ?)",
            (stock.ticker, new_price, now_iso),
        )
        await DBService.pool.execute(
            "DELETE FROM stock_history WHERE ticker = ? AND id NOT IN "
            "(SELECT id FROM stock_history WHERE ticker = ? "
            "ORDER BY id DESC LIMIT ?)",
            (stock.ticker, stock.ticker, HISTORY_KEEP),
        )
        gain = new_price - stock.price
        stock.price = new_price
        updated.append(stock)
        if gain > 0 and stock.owner_id is not None:
            # 創業者配当。失敗してもtick全体は止めない。
            try:
                await _grant_founder_dividend(stock, gain)
            except Exception:
                logger.exception("創業者配当の付与に失敗 ticker=%s", stock.ticker)
    # 通貨価値指数用のスナップショット (平均株価 + 通貨総量) を記録
    cursor = await DBService.pool.execute(
        "SELECT AVG(price) AS a FROM stocks WHERE is_active = 1"
    )
    avg_row = await cursor.fetchone()
    await cursor.close()
    cursor = await DBService.pool.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM users"
    )
    sup_row = await cursor.fetchone()
    await cursor.close()
    await DBService.pool.execute(
        "INSERT INTO market_snapshots (created_at, avg_price, total_supply) "
        "VALUES (?, ?, ?)",
        (
            now_iso,
            avg_row["a"] if avg_row and avg_row["a"] else 0,
            min(sup_row["s"], SQLITE_MAX_INT) if sup_row and sup_row["s"] else 0,
        ),
    )
    await DBService.pool.execute(
        "DELETE FROM market_snapshots WHERE id NOT IN "
        "(SELECT id FROM market_snapshots ORDER BY id DESC LIMIT ?)",
        (HISTORY_KEEP,),
    )
    await DBService.pool.commit()
    return updated


async def add_ticker(
    ticker: str,
    price: int,
    market_id: str = DEFAULT_MARKET_ID,
) -> Stock:
    """管理者用: 新規銘柄を追加。重複・不正値は例外。

    mu/sigma/impact は所属市場のレンジから自動抽選される (指定不可)。
    """
    await ensure_market_schema()
    ticker = normalize_ticker(ticker)
    market = await require_market(market_id)
    if price < 1:
        raise ValueError("開始価格は1以上にしてください")
    if price > MAX_PRICE:
        raise ValueError(f"開始価格は{MAX_PRICE:,}以下にしてください")
    if await get_stock(ticker):
        raise ValueError(f"{ticker} は既に存在します")
    mu, sigma, impact = _draw_params(market, random.Random())
    now = _now()
    await DBService.pool.execute(
        "INSERT INTO stocks "
        "(ticker, display_name, price, mu, sigma, impact, is_active, "
        "market_id, rank, start_price, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, ?)",
        (ticker, ticker, price, mu, sigma, impact,
         market.id, price, now),
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


async def set_active_own(user_id: int, ticker: str, active: bool) -> Stock:
    """設立者用: 自分の会社のみ取扱停止/再開できる。"""
    ticker = normalize_ticker(ticker)
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    if stock.owner_id != user_id:
        raise ValueError(f"{ticker} はあなたの会社ではありません")
    return await set_active(ticker, active)


async def set_price(ticker: str, price: int) -> Stock:
    """管理者用: 価格を直接設定 (救済用)。履歴にも点を打つ。"""
    await ensure_market_schema()
    ticker = normalize_ticker(ticker)
    if price < 1:
        raise ValueError("価格は1以上にしてください")
    if price > MAX_PRICE:
        raise ValueError(f"価格は{MAX_PRICE:,}以下にしてください")
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    now = _now()
    await DBService.pool.execute(
        "UPDATE stocks SET price = ?, updated_at = ? WHERE ticker = ?",
        (price, now, ticker),
    )
    await DBService.pool.execute(
        "INSERT INTO stock_history (ticker, price, created_at) VALUES (?, ?, ?)",
        (ticker, price, now),
    )
    await DBService.pool.commit()
    stock = await get_stock(ticker)
    assert stock is not None
    return stock


@dataclass(frozen=True, slots=True)
class InvestRank:
    """会社設立時の投資額ランク。投資が多いほど優遇される。

    効果は手数料・創業者株・配当率のみ。値動き (mu/sigma/impact) には
    影響しない (値動きは所属市場のレンジから自動抽選)。
    """

    name: str
    fee: int  # 設立手数料
    founder_per: int  # この投資額ごとに創業者株1株
    dividend_rate: float  # 値上がり配当の割合


# (下限, ランク)。上から順に判定する。
INVEST_RANKS: list[tuple[int, InvestRank]] = [
    (50000, InvestRank("S", 500, 500, 0.002)),
    (20000, InvestRank("A", 800, 800, 0.0015)),
    (5000, InvestRank("B", 1000, 1000, 0.001)),
    (1, InvestRank("C", 1000, 1500, 0.0005)),
]


def rank_for_invest(invest: int) -> InvestRank:
    """投資額に対応するランクを返す。1未満は最低ランク。"""
    for lower, rank in INVEST_RANKS:
        if invest >= lower:
            return rank
    return INVEST_RANKS[-1][1]


def dividend_for_rank(rank_name: str | None) -> float:
    """ランク名に対応する配当率。未知・Noneは既定値。"""
    for _, rank in INVEST_RANKS:
        if rank.name == rank_name:
            return rank.dividend_rate
    return DIVIDEND_RATE


def founder_shares_for(invest: int, rank: InvestRank | None = None) -> int:
    """投資額とランクに応じた創業者株数。最低1株。"""
    per = rank.founder_per if rank else FOUNDER_SHARES_PER
    return max(invest // per, 1)


# レベル連動の会社保有枠: 10Lvごとに+1社 (Lv1〜9:1社、Lv10〜19:2社…)、上限5社。
COMPANY_SLOT_STEP = 10
MAX_COMPANIES = 5


def max_companies_for_level(level: int) -> int:
    """レベルに対応する会社保有上限。1未満は1社扱い。

    10Lvごとに+1社 (Lv1〜9:1社、Lv10〜19:2社…)、上限5社。
    """
    level = max(int(level), 1)
    return min(1 + level // COMPANY_SLOT_STEP, MAX_COMPANIES)


def next_slot_level(level: int) -> int | None:
    """次の会社枠が解放されるレベル。上限到達済みならNone。"""
    slots = max_companies_for_level(level)
    if slots >= MAX_COMPANIES:
        return None
    return slots * COMPANY_SLOT_STEP


async def _credit_royalty(owner_id: int | None, base: int) -> int:
    """売買ロイヤリティ (代金×ROYALTY_RATE) を設立者に付与。戻り値は付与額。

    運営負担の鋳造方式 (売主・買主の金額は変わらない)。端数切捨てで
    1未満はスキップする。自分の売買は対象外 (呼び出し側で除外する)。
    """
    if owner_id is None:
        return 0
    royalty = min(int(base * ROYALTY_RATE), SQLITE_MAX_INT)
    if royalty < 1:
        return 0
    await _add_user_amount(owner_id, royalty)
    return royalty


async def _add_user_amount(user_id: int, amount: int) -> None:
    """ユーザー残高を加算 (上限 SQLITE_MAX_INT で丸める)。行がなければ作る。"""
    if amount < 1:
        return
    await DBService.pool.execute(
        "INSERT OR IGNORE INTO users(id) VALUES (?)", (user_id,)
    )
    cursor = await DBService.pool.execute(
        "SELECT amount FROM users WHERE id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    balance = row["amount"] if row else 100
    await DBService.pool.execute(
        "UPDATE users SET amount = ? WHERE id = ?",
        (min(balance + amount, SQLITE_MAX_INT), user_id),
    )
    await DBService.pool.commit()


async def _grant_founder_dividend(stock: Stock, gain: int) -> int:
    """創業者配当: 値上がり分×設立者の保有株数×ランク配当率を付与。

    株を持ち続けるほど配当が増える (売ると将来の配当が減る)。
    戻り値は付与額。値下がり・保有なし・運営銘柄は0。
    """
    if stock.owner_id is None or gain <= 0:
        return 0
    cursor = await DBService.pool.execute(
        "SELECT qty FROM holdings WHERE user_id = ? AND ticker = ?",
        (stock.owner_id, stock.ticker),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if not row or row["qty"] < 1:
        return 0
    rate = dividend_for_rank(stock.rank)
    dividend = min(int(gain * row["qty"] * rate), SQLITE_MAX_INT)
    if dividend < 1:
        return 0
    await _add_user_amount(stock.owner_id, dividend)
    return dividend


async def create_company(
    user_id: int,
    ticker: str,
    invest: int,
    market_id: str = DEFAULT_MARKET_ID,
) -> Stock:
    """ユーザー用: 会社を設立 (レベル連動枠)。

    設立手数料 (ランクで割引) +投資金を徴収。開始株価=投資金。
    mu/sigma/impact は所属市場のレンジから自動抽選され、指定はできない。
    設立者にはランクに応じた創業者株を付与する。
    戻り値は設立した銘柄。
    残高不足時は LookupError (Cog側で AmountNotEnough に変換する)。
    """
    from services import levels as level_service

    await ensure_market_schema()
    ticker = normalize_ticker(ticker)
    market = await require_market(market_id)
    if invest < 1:
        raise ValueError("投資額は1以上にしてください")
    if invest > MAX_PRICE:
        raise ValueError(f"投資額は{MAX_PRICE:,}以下にしてください")
    if await get_stock(ticker):
        raise ValueError(f"{ticker} は既に存在します")
    user_level = 1
    with suppress(Exception):  # レベル取得失敗時はLv.1扱いで継続
        user_level = int((await level_service.get_info(user_id))["level"])
    limit = max_companies_for_level(user_level)
    cursor = await DBService.pool.execute(
        "SELECT COUNT(*) AS n FROM stocks WHERE owner_id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    owned = int(row["n"]) if row else 0
    if owned >= limit:
        nxt = next_slot_level(user_level)
        extra = f" (Lv.{nxt}で次の枠が解放されます)" if nxt else " (上限です)"
        raise ValueError(f"Lv.{user_level}では会社は{limit}社までです{extra}")
    rank = rank_for_invest(invest)
    cost = rank.fee + invest
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
    if balance < cost:
        raise LookupError(f"残高不足: 必要 {cost}")
    mu, sigma, impact = _draw_params(market, random.Random())
    now = _now()
    await DBService.pool.execute(
        "UPDATE users SET amount = amount - ? WHERE id = ?", (cost, user_id)
    )
    await DBService.pool.execute(
        "INSERT INTO stocks "
        "(ticker, display_name, price, mu, sigma, impact, is_active, "
        "owner_id, market_id, rank, start_price, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
        (ticker, ticker, invest, mu, sigma, impact, user_id,
         market.id, rank.name, invest, now),
    )
    await DBService.pool.execute(
        "INSERT INTO stock_history (ticker, price, created_at) VALUES (?, ?, ?)",
        (ticker, invest, now),
    )
    await DBService.pool.execute(
        "INSERT INTO holdings (user_id, ticker, qty, avg_cost) VALUES (?, ?, ?, ?)",
        (user_id, ticker, founder_shares_for(invest, rank), invest),
    )
    await DBService.pool.commit()
    stock = await get_stock(ticker)
    assert stock is not None
    return stock


async def add_investment(
    user_id: int,
    ticker: str,
    invest: int,
) -> tuple[Stock, int]:
    """設立者用: 自分の会社に追加投資 (増資) する。

    投資額の全額を支払い、現在の株価で創業者株を発行する
    (発行株数 = 投資額 // 株価)。株価自体は変わらない
    (時価増資・希薄化で中立のため、錬金にならない)。
    mu/sigma/impact は変わらない。
    戻り値は (銘柄, 発行株数)。
    残高不足時は LookupError (Cog側で AmountNotEnough に変換する)。
    """
    ticker = normalize_ticker(ticker)
    if invest < 1:
        raise ValueError("投資額は1以上にしてください")
    if invest > MAX_PRICE:
        raise ValueError(f"投資額は{MAX_PRICE:,}以下にしてください")
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    if stock.owner_id != user_id:
        raise ValueError(f"{ticker} はあなたの会社ではありません")
    new_shares = invest // stock.price
    if new_shares < 1:
        raise ValueError(
            f"投資額が株価 ({stock.price:,}) に満たないため1株も発行できません"
        )
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
    if balance < invest:
        raise LookupError(f"残高不足: 必要 {invest}")
    cursor = await DBService.pool.execute(
        "SELECT qty, avg_cost FROM holdings WHERE user_id = ? AND ticker = ?",
        (user_id, ticker),
    )
    holding = await cursor.fetchone()
    await cursor.close()
    old_qty = holding["qty"] if holding else 0
    old_avg = holding["avg_cost"] if holding else 0
    total_qty = old_qty + new_shares
    if total_qty > SQLITE_MAX_INT:
        raise ValueError("発行後の保有株数が上限を超えます")
    new_avg = (old_avg * old_qty + invest) // total_qty
    await DBService.pool.execute(
        "UPDATE users SET amount = amount - ? WHERE id = ?", (invest, user_id)
    )
    if holding:
        await DBService.pool.execute(
            "UPDATE holdings SET qty = ?, avg_cost = ? "
            "WHERE user_id = ? AND ticker = ?",
            (total_qty, new_avg, user_id, ticker),
        )
    else:
        await DBService.pool.execute(
            "INSERT INTO holdings (user_id, ticker, qty, avg_cost) VALUES (?, ?, ?, ?)",
            (user_id, ticker, new_shares, new_avg),
        )
    await DBService.pool.commit()
    stock = await get_stock(ticker)
    assert stock is not None
    return stock, new_shares


async def buy(user_id: int, ticker: str, qty: int) -> tuple[int, int, float, int]:
    """購入。戻り値は (約定単価, 合計金額, 需給変動率, ロイヤリティ額)。

    残高の増減も直接SQLで行うため Discord オブジェクトは不要。
    残高不足時は LookupError (Cog側で AmountNotEnough に変換する)。
    約定単価は需給影響反映後の価格 (買い上がった後の値段で買う。
    買ってすぐ売っても押し上げ分で得しないスリッページ方式)。
    他人の会社を買った場合、代金の ROYALTY_RATE が設立者に還元される。
    """
    ticker = normalize_ticker(ticker)
    if qty < 1:
        raise ValueError("数量は1以上にしてください")
    if qty > SQLITE_MAX_INT:
        raise ValueError("数量が多すぎます")
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    if not stock.is_active:
        raise ValueError(f"{ticker} は現在取扱停止中です")
    if stock.owner_id is not None and stock.owner_id == user_id:
        raise ValueError(f"{ticker} は自分の会社なので買えません")

    # 約定価格を先に確定させる (DB書き込み前に残高チェックするため)。
    # 影響後の価格で買うので、自分の買いで上がった分は得にならない。
    exec_price, rate = calc_impact_price(stock.price, stock.impact, qty)

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

    cost = exec_price * qty
    if cost > SQLITE_MAX_INT:
        raise ValueError("数量が多すぎます (合計金額が上限を超えます)")
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
            (user_id, ticker, qty, exec_price),
        )
    now = _now()
    await DBService.pool.execute(
        "UPDATE stocks SET price = ?, updated_at = ? WHERE ticker = ?",
        (exec_price, now, ticker),
    )
    await DBService.pool.execute(
        "INSERT INTO stock_history (ticker, price, created_at) VALUES (?, ?, ?)",
        (ticker, exec_price, now),
    )
    await DBService.pool.commit()
    royalty = await _credit_royalty(stock.owner_id, cost)
    return exec_price, cost, rate, royalty


async def sell(
    user_id: int, ticker: str, qty: int
) -> tuple[int, int, int, float, int]:
    """売却。戻り値は (約定単価, 受取金額, 借金への自動返済額, 需給変動率, ロイヤリティ額).

    上場廃止銘柄も売却は可能。自社株は売却のみ可 (買増は不可)。
    受取金額は借金返済優先で配分される。
    他人の会社の株を売った場合、代金の ROYALTY_RATE が設立者に還元される
    (自分の売買は対象外)。
    約定単価は需給影響反映後の価格 (売り崩した後の値段で売る。
    売ってすぐ買い戻しても値下がり分で得しないスリッページ方式)。
    受取が64bit上限を超える数量はエラーにし、売れる最大株数を提示する
    (超過分を黙って切り捨てない)。
    """
    from services.loan import apply_income

    ticker = normalize_ticker(ticker)
    if qty < 1:
        raise ValueError("数量は1以上にしてください")
    if qty > SQLITE_MAX_INT:
        raise ValueError("数量が多すぎます")
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
    # 約定価格を先に確定させる。売り崩した後の値段で売るので、
    # 大口でも自分の売りで下げた分は得にならない。
    exec_price, rate = calc_impact_price(stock.price, stock.impact, -qty)
    proceeds = exec_price * qty
    if proceeds > SQLITE_MAX_INT:
        max_qty = SQLITE_MAX_INT // exec_price
        if max_qty < 1:
            raise ValueError(
                "数量が多すぎます (1株でも受取が上限を超えます)"
            )
        raise ValueError(
            f"数量が多すぎます (受取が上限を超えます。{max_qty}株までなら可能です)"
        )
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
    now = _now()
    await DBService.pool.execute(
        "UPDATE stocks SET price = ?, updated_at = ? WHERE ticker = ?",
        (exec_price, now, ticker),
    )
    await DBService.pool.execute(
        "INSERT INTO stock_history (ticker, price, created_at) VALUES (?, ?, ?)",
        (ticker, exec_price, now),
    )
    await DBService.pool.commit()
    royalty = 0
    if stock.owner_id is not None and stock.owner_id != user_id:
        royalty = await _credit_royalty(stock.owner_id, proceeds)
    return exec_price, proceeds, repaid, rate, royalty


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
