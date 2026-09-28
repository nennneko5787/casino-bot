"""株価エンジン: 幾何ランダムウォーク + 売買 + 銘柄追加 + 破産。

各銘柄は独立した乱数で更新されるため、同じように上がり下がりしない。
mu/sigma は管理者が数値で直接指定できる (原案) ほか、
簡単プリセット (おとなしい/ふつう/荒い) で sigma を決めることもできる。

価格は整数だが、丸めは確率的 (期待値不変) にすることで
価格1が吸着点にならないようにしている (1でも毎tick数%で脱出できる)。
売買の需給影響も rate≠0 なら最低1は動く。

売買による需給影響は自律変動を主役にするため、逓減 + 上限付き。
少量ならほぼ線形 (impact × 数量) に動くが、大量注文でも
MAX_TRADE_IMPACT (既定±3%) を超えて動くことはない。

ユーザーは会社を設立できる (レベル連動枠、設立手数料1000+投資金、
開始株価=投資金、mu/sigma/impactは投資額ランクで自動決定、
創業者株の付与 + 売買ロイヤリティ + 値上がり配当あり)。
自社株は買増不可・売却のみ可。
開始価格割れ + 下落継続で危険水域に入り、下落したまま6時間続いた会社は
破産 (運営・ユーザー問わず): 保有株は紙くず・会社データは消去・
元オーナーは再設立可。下落が止まって1時間続くか、開始価格以上に
回復すれば水域から脱出する。
"""

from __future__ import annotations

import logging
import math
import random
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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

# 会社設立の手数料 (消滅)。mu ボーナス等の優遇は下の INVEST_RANKS による。
FOUNDING_FEE = 1000

# 設立者メリット
ROYALTY_RATE = 0.01  # 他人が自社株を売買するたび、代金のこの割合が設立者に入る
FOUNDER_SHARES_PER = 1000  # この投資額ごとに創業者株1株 (最低1株、売却のみ可)
DIVIDEND_RATE = 0.001  # tickで値上がりしたら、上昇分×保有株数×この割合を配当

# 破産: 開始価格割れ + 下落継続がこの時間続いたら破産。
# 下落が止まって TREND_CALM_HOURS 続いたら危険水域から脱出する。
BANKRUPT_DANGER_HOURS = 6
TREND_CALM_HOURS = 1
TREND_WINDOW = 30  # 傾向判定に使う直近履歴件数

# 1回の売買で需給により動く上限 (変動率)。自律変動 (sigma由来の
# 数%〜十数%) より小さめにして、売買で暴落/暴騰しないようにする。
MAX_TRADE_IMPACT = 0.03

# 簡単プリセット: sigma の値のみ決める (mu は別途数値指定、省略時 0)
VOL_PRESETS: dict[str, float] = {
    "calm": 0.02,  # おとなしい
    "normal": 0.05,  # ふつう
    "wild": 0.06,  # 荒い (膨張抑制のため 0.10→0.06)
}

# mu のプリセット (平均成長率)
MU_PRESETS: dict[str, float] = {
    "down": -0.001,  # 下降トレンド
    "flat": 0.0,  # 横ばい
    "up": 0.001,  # 上昇トレンド
}

# impact のプリセット (1株あたりの変動率)
IMPACT_PRESETS: dict[str, float] = {
    "dull": 0.0002,  # 鈍感 (動きにくい)
    "normal": 0.0005,  # ふつう
    "sensitive": 0.001,  # 敏感 (動きやすい)
}

# おまかせランダム用の値域
RANDOM_MU_RANGE = (-0.002, 0.003)
RANDOM_IMPACTS = (0.0002, 0.0003, 0.0005, 0.0008, 0.001, 0.002)


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
    start_price: int = 100  # 開始価格 (倒産の危険水域判定の基準)
    floor_since: str | None = None  # 危険水域突入時刻 (水域外ではNone)


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


def step_price(price: int, mu: float, sigma: float, rng: random.Random) -> int:
    """幾何ランダムウォークで1歩進める。下限1・上限MAX_PRICE。

    丸めは確率的 (stochastic rounding) にすることで期待値を保ちつつ、
    価格1が吸着点にならないようにする (1でも毎tick数%の確率で2に脱出)。
    高額帯では平均回帰で mu を下向き補正し、sigma を圧縮して膨張を抑える。
    """
    try:
        eff_mu = mu
        if price > REFERENCE_PRICE:
            eff_mu -= MEAN_REVERSION_K * math.log(price / REFERENCE_PRICE)
        # 高額帯のボラ圧縮: 10倍ごとに sigma を約1割抑える
        eff_sigma = sigma
        if price > REFERENCE_PRICE * 10:
            eff_sigma = sigma / (
                1.0 + 0.15 * math.log10(price / (REFERENCE_PRICE * 10))
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
        start_price=int(start_price) if start_price is not None else 100,
        floor_since=_opt("floor_since"),
    )


def danger_info(stock: Stock, now: datetime | None = None) -> tuple[bool, float]:
    """危険水域の状態を返す (水域内か, 突入からの経過時間h)。

    表示用 (/stock list・chart)。水域 = 開始価格割れ (開始価格1は価格1以下)。
    """
    start = stock.start_price if stock.start_price > 0 else 1
    if not stock.floor_since or not (stock.price < start or stock.price <= 1):
        return False, 0.0
    try:
        since = datetime.fromisoformat(stock.floor_since)
    except ValueError:
        return True, 0.0
    moment = now or datetime.now(UTC)
    hours = max((moment - since).total_seconds() / 3600.0, 0.0)
    return True, hours


def danger_remaining_hours(stock: Stock, now: datetime | None = None) -> float:
    """倒産までの残り猶予時間h。水域外は0.0。"""
    in_danger, elapsed = danger_info(stock, now)
    if not in_danger:
        return 0.0
    return max(BANKRUPT_DANGER_HOURS - elapsed, 0.0)


async def danger_declining(ticker: str, window: int = TREND_WINDOW) -> bool:
    """直近window件が下落傾向ならTrue。前半平均→後半平均で判定。

    データ不足 (4件未満) は下落継続扱い。横ばい・反発ならFalse。
    """
    history = await get_history(ticker, window)
    prices = [p for _, p in history]
    if len(prices) < 4:
        return True
    half = len(prices) // 2
    older = sum(prices[:half]) / half
    newer = sum(prices[half:]) / (len(prices) - half)
    return newer < older


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
    rate = dampen_impact_rate(stock.impact * qty)
    new_price = _clamp_price(round(stock.price * (1 + rate)))
    if rate != 0.0 and new_price == stock.price:
        # 丸めで同値になる場合も最低1は動かす (価格1の吸着防止)。下限1。
        new_price = _clamp_price(stock.price + (1 if rate > 0 else -1))
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
) -> tuple[list[Stock], list[dict], list[dict]]:
    """上場中 (is_active=1) の全銘柄を独立した乱数で1歩進める。

    開始価格割れ + 下落継続で危険水域に入り、下落したまま6時間続いた会社は
    破産させる (運営・ユーザー問わず)。下落が止まって1時間続くか、
    開始価格以上に回復すれば水域から脱出する。
    値上がりしたユーザー企業には創業者配当を付与する。
    戻り値は (更新後銘柄, 破産銘柄情報 [{ticker, owner_id}],
    危険水域突入の警告 [{ticker, owner_id, start_price, price}])。
    """
    rng = rng or random.Random()
    moment = now or datetime.now(UTC)
    now_iso = moment.isoformat()
    stocks = await get_stocks(active_only=True)
    updated: list[Stock] = []
    bankrupted: list[dict] = []
    warned: list[dict] = []
    for stock in stocks:
        try:
            # DBに既に入っている異常値 (上限超え) はまず上限に丸めて回復させる
            if stock.price > MAX_PRICE or stock.price < 1:
                stock.price = _clamp_price(stock.price)
            new_price = _clamp_price(
                step_price(stock.price, stock.mu, stock.sigma, rng)
            )
        except Exception:
            # 1銘柄の計算失敗で全体を止めない
            logger.exception("株価tickの計算に失敗 ticker=%s", stock.ticker)
            continue
        start = stock.start_price if stock.start_price > 0 else 1
        # 開始価格1の銘柄は価格1以下で水域入りする
        in_danger = new_price < start or new_price <= 1
        if in_danger and not stock.floor_since:
            # 危険水域に突入: 時刻を記録して警告する
            floor_new: str | None = now_iso
            warned.append(
                {
                    "ticker": stock.ticker,
                    "owner_id": stock.owner_id,
                    "start_price": start,
                    "price": new_price,
                }
            )
        elif not in_danger:
            # 開始価格以上に回復: 水域から脱出
            floor_new = None
        else:
            # 水域継続: 下落が続いたまま猶予超過なら破産。
            # 下落が止まってしばらく (TREND_CALM_HOURS) したら脱出する。
            floor_new = stock.floor_since
            try:
                since = datetime.fromisoformat(stock.floor_since or "")
            except ValueError:
                since = moment
            age = moment - since
            declining = await danger_declining(stock.ticker)
            if declining and age >= timedelta(hours=BANKRUPT_DANGER_HOURS):
                info = await go_bankrupt(stock.ticker)
                bankrupted.append(info)
                continue
            if not declining and age >= timedelta(hours=TREND_CALM_HOURS):
                floor_new = None  # 下がり止まり → 脱出
        await DBService.pool.execute(
            "UPDATE stocks SET price = ?, floor_since = ?, updated_at = ? "
            "WHERE ticker = ?",
            (new_price, floor_new, now_iso, stock.ticker),
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
        stock.floor_since = floor_new
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
    return updated, bankrupted, warned


async def go_bankrupt(ticker: str) -> dict:
    """破産処理: 保有株は紙くず (無補償)・会社データを消去。

    戻り値は {ticker, owner_id}。元オーナーは再設立できる。
    """
    ticker = normalize_ticker(ticker)
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    await DBService.pool.execute(
        "DELETE FROM holdings WHERE ticker = ?", (ticker,)
    )
    await DBService.pool.execute(
        "DELETE FROM stock_history WHERE ticker = ?", (ticker,)
    )
    await DBService.pool.execute("DELETE FROM stocks WHERE ticker = ?", (ticker,))
    await DBService.pool.commit()
    return {"ticker": ticker, "owner_id": stock.owner_id}


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
    if price > MAX_PRICE:
        raise ValueError(f"開始価格は{MAX_PRICE:,}以下にしてください")
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
        "(ticker, display_name, price, mu, sigma, impact, is_active, "
        "start_price, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (ticker, ticker, price, mu, sigma, impact, price, now),
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
    """管理者用: 価格を直接設定 (価格1張り付き等の救済用)。履歴にも点を打つ。"""
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
        "UPDATE stocks SET price = ?, floor_since = NULL, updated_at = ? "
        "WHERE ticker = ?",
        (price, now, ticker),
    )
    await DBService.pool.execute(
        "INSERT INTO stock_history (ticker, price, created_at) VALUES (?, ?, ?)",
        (ticker, price, now),
    )
    await DBService.pool.commit()
    stock.price = price
    return stock


def founding_bonus_mu(invest: int) -> float:
    """投資額に応じた mu ボーナス。ランク表が正本。"""
    return rank_for_invest(invest).mu_bonus


@dataclass(frozen=True, slots=True)
class InvestRank:
    """会社設立時の投資額ランク。投資が多いほど優遇される。"""

    name: str
    mu_bonus: float
    sigma: float
    impact: float


# (下限, ランク)。上から順に判定する。
INVEST_RANKS: list[tuple[int, InvestRank]] = [
    (50000, InvestRank("S", 0.003, 0.03, 0.0003)),
    (20000, InvestRank("A", 0.002, 0.04, 0.0005)),
    (5000, InvestRank("B", 0.001, 0.05, 0.0008)),
    (1, InvestRank("C", 0.0, 0.06, 0.001)),
]


def rank_for_invest(invest: int) -> InvestRank:
    """投資額に対応するランクを返す。1未満は最低ランク。"""
    for lower, rank in INVEST_RANKS:
        if invest >= lower:
            return rank
    return INVEST_RANKS[-1][1]


def founder_shares_for(invest: int) -> int:
    """投資額に応じた創業者株数。1000ごとに1株、最低1株。"""
    return max(invest // FOUNDER_SHARES_PER, 1)


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
    """創業者配当: 値上がり分×設立者の保有株数×DIVIDEND_RATEを付与。

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
    dividend = min(int(gain * row["qty"] * DIVIDEND_RATE), SQLITE_MAX_INT)
    if dividend < 1:
        return 0
    await _add_user_amount(stock.owner_id, dividend)
    return dividend


async def create_company(
    user_id: int,
    ticker: str,
    invest: int,
) -> Stock:
    """ユーザー用: 会社を設立 (レベル連動枠)。設立手数料1000+投資金を徴収。

    開始株価=投資金。mu/sigma/impactは投資額ランクで自動決定され、
    指定はできない (管理者の add/params のみ数値・プリセット指定可)。
    設立者には投資額1000ごとに1株 (最低1株) の創業者株を付与する。
    戻り値は設立した銘柄。
    残高不足時は LookupError (Cog側で AmountNotEnough に変換する)。
    """
    from services import levels as level_service

    ticker = normalize_ticker(ticker)
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
    cost = FOUNDING_FEE + invest
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
    rank = rank_for_invest(invest)
    mu = min(rank.mu_bonus, 1.0)
    sigma = rank.sigma
    impact = rank.impact
    now = _now()
    await DBService.pool.execute(
        "UPDATE users SET amount = amount - ? WHERE id = ?", (cost, user_id)
    )
    await DBService.pool.execute(
        "INSERT INTO stocks "
        "(ticker, display_name, price, mu, sigma, impact, is_active, "
        "owner_id, start_price, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (ticker, ticker, invest, mu, sigma, impact, user_id, invest, now),
    )
    await DBService.pool.execute(
        "INSERT INTO stock_history (ticker, price, created_at) VALUES (?, ?, ?)",
        (ticker, invest, now),
    )
    await DBService.pool.execute(
        "INSERT INTO holdings (user_id, ticker, qty, avg_cost) VALUES (?, ?, ?, ?)",
        (user_id, ticker, founder_shares_for(invest), invest),
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


def resolve_mu(
    mu: float | None, mu_preset: str | None, rng: random.Random | None = None
) -> float:
    """mu の数値指定とプリセットの解決。プリセット優先、どちらも無ければ既定0。

    mu_preset="random" の場合は -0.002〜+0.003 の一様乱数。
    """
    if mu_preset is not None:
        if mu_preset == "random":
            return round((rng or random.Random()).uniform(*RANDOM_MU_RANGE), 6)
        if mu_preset not in MU_PRESETS:
            raise ValueError("muのプリセットは down/flat/up/random から選んでください")
        return MU_PRESETS[mu_preset]
    if mu is not None:
        if not -1.0 <= mu <= 1.0:
            raise ValueError("muは-1.0〜1.0の範囲で指定してください")
        return mu
    return 0.0


def resolve_impact(
    impact: float | None, impact_preset: str | None, rng: random.Random | None = None
) -> float:
    """impact の数値指定とプリセットの解決。プリセット優先、どちらも無ければ既定。

    impact_preset="random" の場合は候補値からランダムに選ぶ。
    """
    if impact_preset is not None:
        if impact_preset == "random":
            return (rng or random.Random()).choice(RANDOM_IMPACTS)
        if impact_preset not in IMPACT_PRESETS:
            raise ValueError(
                "impactのプリセットは dull/normal/sensitive/random から選んでください"
            )
        return IMPACT_PRESETS[impact_preset]
    if impact is not None:
        return check_impact(impact)
    return 0.0005


async def buy(user_id: int, ticker: str, qty: int) -> tuple[int, int, float, int]:
    """購入。戻り値は (約定単価, 合計金額, 需給変動率, ロイヤリティ額)。

    残高の増減も直接SQLで行うため Discord オブジェクトは不要。
    残高不足時は LookupError (Cog側で AmountNotEnough に変換する)。
    約定後に需給影響を即時反映する (約定単価は影響前の価格)。
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
            (user_id, ticker, qty, stock.price),
        )
    await DBService.pool.commit()
    _, rate = await apply_impact(ticker, qty)
    royalty = await _credit_royalty(stock.owner_id, cost)
    return stock.price, cost, rate, royalty


async def sell(
    user_id: int, ticker: str, qty: int
) -> tuple[int, int, int, float, int]:
    """売却。戻り値は (約定単価, 受取金額, 借金への自動返済額, 需給変動率, ロイヤリティ額).

    上場廃止銘柄も売却は可能。自社株は売却のみ可 (買増は不可)。
    受取金額は借金返済優先で配分される。
    他人の会社の株を売った場合、代金の ROYALTY_RATE が設立者に還元される
    (自分の売買は対象外)。
    約定後に需給影響を即時反映する (約定単価は影響前の価格)。
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
    royalty = 0
    if stock.owner_id is not None and stock.owner_id != user_id:
        royalty = await _credit_royalty(stock.owner_id, proceeds)
    return stock.price, proceeds, repaid, rate, royalty


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
