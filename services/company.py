"""会社口座: 金庫・メンバー・招待・引出申請 (設立者承認制)・所得税。

会社 = owner_id 付きの銘柄。金庫 (company_accounts) にメンバーが
預入でき、引出は設立者の承認が必要。設立者本人の引出は即時実行。
金庫には毎日0時(JST)に所得税がかかり、徴収分は焼却する (供給の排水口)。
滞納が続くと自動で取扱停止になる。

残高不足時は LookupError (Cog側で AmountNotEnough に変換する)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from services.database import DBService
from services.money import MAX_BALANCE
from services.stocks import SQLITE_MAX_INT, get_stock, normalize_ticker

JST = timezone(timedelta(hours=9))

# 所得税: 金庫残高のこの割合 (端数切捨て) と最低額の大きい方を毎日徴収。
TAX_RATE = 0.01
TAX_MIN = 100
# 連続滞納がこの回数に達したら自動で取扱停止にする。
TAX_MISSED_LIMIT = 3
# 引出申請の有効期限。
WITHDRAW_EXPIRY_HOURS = 24.0

_schema_ensured = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def ensure_company_schema() -> None:
    """会社口座系テーブルの保険 (alembic未適用でも動く)。"""
    global _schema_ensured
    if _schema_ensured:
        return
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS company_accounts ("
        "ticker TEXT PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, "
        "tax_arrears INTEGER NOT NULL DEFAULT 0, "
        "tax_missed INTEGER NOT NULL DEFAULT 0, "
        "last_taxed_at TEXT NULL, updated_at TEXT NOT NULL)"
    )
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS company_members ("
        "ticker TEXT NOT NULL, user_id BIGINT NOT NULL, "
        "role TEXT NOT NULL DEFAULT 'member', created_at TEXT NOT NULL, "
        "PRIMARY KEY (ticker, user_id))"
    )
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS company_invites ("
        "ticker TEXT NOT NULL, user_id BIGINT NOT NULL, "
        "invited_by BIGINT NOT NULL, created_at TEXT NOT NULL, "
        "PRIMARY KEY (ticker, user_id))"
    )
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS withdraw_requests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, "
        "requester BIGINT NOT NULL, amount INTEGER NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, "
        "decided_at TEXT NULL, decided_by BIGINT NULL)"
    )
    await DBService.pool.execute(
        "CREATE INDEX IF NOT EXISTS ix_withdraw_requests_ticker "
        "ON withdraw_requests (ticker)"
    )
    await DBService.pool.execute(
        "CREATE TABLE IF NOT EXISTS company_ledger ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, "
        "user_id BIGINT NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    await DBService.pool.execute(
        "CREATE INDEX IF NOT EXISTS ix_company_ledger_ticker "
        "ON company_ledger (ticker)"
    )
    await DBService.pool.commit()
    _schema_ensured = True


async def _company_stock(ticker: str):
    """会社銘柄を取得。運営銘柄・不存在は ValueError。"""
    ticker = normalize_ticker(ticker)
    stock = await get_stock(ticker)
    if not stock:
        raise ValueError(f"{ticker} は存在しません")
    if stock.owner_id is None:
        raise ValueError(f"{ticker} は運営銘柄のため会社口座は使えません")
    return stock


def is_owner(stock, user_id: int) -> bool:
    """設立者か。"""
    return stock.owner_id is not None and stock.owner_id == user_id


async def is_member(ticker: str, user_id: int) -> bool:
    """設立者または招待済みメンバーか。"""
    await ensure_company_schema()
    stock = await _company_stock(ticker)
    if is_owner(stock, user_id):
        return True
    cursor = await DBService.pool.execute(
        "SELECT 1 FROM company_members WHERE ticker = ? AND user_id = ?",
        (stock.ticker, user_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def get_account(ticker: str) -> dict:
    """金庫の残高・滞納情報を返す。行がなければ0で作る。"""
    await ensure_company_schema()
    stock = await _company_stock(ticker)
    cursor = await DBService.pool.execute(
        "SELECT * FROM company_accounts WHERE ticker = ?", (stock.ticker,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        now = _now()
        await DBService.pool.execute(
            "INSERT INTO company_accounts (ticker, balance, updated_at) "
            "VALUES (?, 0, ?)",
            (stock.ticker, now),
        )
        await DBService.pool.commit()
        return {"balance": 0, "arrears": 0, "missed": 0, "last_taxed_at": None}
    return {
        "balance": int(row["balance"]),
        "arrears": int(row["tax_arrears"]),
        "missed": int(row["tax_missed"]),
        "last_taxed_at": row["last_taxed_at"],
    }


async def members(ticker: str) -> list[dict]:
    """設立者 + メンバーの一覧。[{user_id, role}]。"""
    await ensure_company_schema()
    stock = await _company_stock(ticker)
    result = [{"user_id": stock.owner_id, "role": "owner"}]
    cursor = await DBService.pool.execute(
        "SELECT user_id, role FROM company_members WHERE ticker = ? "
        "ORDER BY created_at",
        (stock.ticker,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    result.extend([{"user_id": r["user_id"], "role": r["role"]} for r in rows])
    return result


async def invite(owner_id: int, ticker: str, invitee_id: int) -> None:
    """設立者がメンバーを招待する。重複・不正は ValueError。"""
    await ensure_company_schema()
    stock = await _company_stock(ticker)
    if not is_owner(stock, owner_id):
        raise ValueError(f"{stock.ticker} の設立者のみ招待できます")
    if invitee_id == stock.owner_id:
        raise ValueError("設立者は既にメンバーです")
    if await is_member(stock.ticker, invitee_id):
        raise ValueError("既にメンバーです")
    cursor = await DBService.pool.execute(
        "SELECT 1 FROM company_invites WHERE ticker = ? AND user_id = ?",
        (stock.ticker, invitee_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is not None:
        raise ValueError("招待済みです")
    await DBService.pool.execute(
        "INSERT INTO company_invites (ticker, user_id, invited_by, created_at) "
        "VALUES (?, ?, ?, ?)",
        (stock.ticker, invitee_id, owner_id, _now()),
    )
    await DBService.pool.commit()


async def accept(user_id: int, ticker: str) -> None:
    """招待を応諾してメンバーになる。"""
    await ensure_company_schema()
    stock = await _company_stock(ticker)
    cursor = await DBService.pool.execute(
        "DELETE FROM company_invites WHERE ticker = ? AND user_id = ? "
        "RETURNING user_id",
        (stock.ticker, user_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        raise ValueError(f"{stock.ticker} への招待はありません")
    await DBService.pool.execute(
        "INSERT OR IGNORE INTO company_members "
        "(ticker, user_id, role, created_at) VALUES (?, ?, 'member', ?)",
        (stock.ticker, user_id, _now()),
    )
    await DBService.pool.commit()


async def decline(user_id: int, ticker: str) -> None:
    """招待を拒否する。"""
    await ensure_company_schema()
    stock = await _company_stock(ticker)
    cursor = await DBService.pool.execute(
        "DELETE FROM company_invites WHERE ticker = ? AND user_id = ? "
        "RETURNING user_id",
        (stock.ticker, user_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    await DBService.pool.commit()
    if row is None:
        raise ValueError(f"{stock.ticker} への招待はありません")


async def my_invites(user_id: int) -> list[dict]:
    """自分宛ての招待一覧。"""
    await ensure_company_schema()
    cursor = await DBService.pool.execute(
        "SELECT ticker, invited_by, created_at FROM company_invites "
        "WHERE user_id = ? ORDER BY created_at",
        (user_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [
        {
            "ticker": r["ticker"],
            "invited_by": r["invited_by"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def _user_balance(user_id: int) -> int:
    """個人残高。行がなければ作る (初期100)。"""
    cursor = await DBService.pool.execute(
        "SELECT amount FROM users WHERE id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        await DBService.pool.execute("INSERT INTO users(id) VALUES (?)", (user_id,))
        await DBService.pool.commit()
        return 100
    return int(row["amount"])


async def _credit_user(user_id: int, amount: int) -> None:
    """個人残高に加算 (上限 MAX_BALANCE で丸める)。"""
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
    balance = int(row["amount"]) if row else 100
    await DBService.pool.execute(
        "UPDATE users SET amount = ? WHERE id = ?",
        (min(balance + amount, MAX_BALANCE), user_id),
    )


async def deposit(user_id: int, ticker: str, amount: int) -> int:
    """金庫に預け入れる。戻り値は預入後の金庫残高。

    メンバーのみ可。残高不足時は LookupError。
    """
    await ensure_company_schema()
    stock = await _company_stock(ticker)
    if amount < 1:
        raise ValueError("預入額は1以上にしてください")
    if amount > SQLITE_MAX_INT:
        raise ValueError("金額が多すぎます")
    if not await is_member(stock.ticker, user_id):
        raise ValueError(f"{stock.ticker} のメンバーではありません")
    balance = await _user_balance(user_id)
    if balance < amount:
        raise LookupError(f"残高不足: 必要 {amount}")
    account = await get_account(stock.ticker)
    if account["balance"] + amount > SQLITE_MAX_INT:
        raise ValueError("金庫の上限を超えます")
    now = _now()
    await DBService.pool.execute(
        "UPDATE users SET amount = amount - ? WHERE id = ?", (amount, user_id)
    )
    await DBService.pool.execute(
        "UPDATE company_accounts SET balance = balance + ?, updated_at = ? "
        "WHERE ticker = ?",
        (amount, now, stock.ticker),
    )
    await DBService.pool.execute(
        "INSERT INTO company_ledger (ticker, user_id, kind, amount, created_at) "
        "VALUES (?, ?, 'deposit', ?, ?)",
        (stock.ticker, user_id, amount, now),
    )
    await DBService.pool.commit()
    return account["balance"] + amount


async def _pay_withdraw(ticker: str, requester: int, amount: int) -> int:
    """金庫から支払う (残高チェック済み前提)。戻り値は借金への自動返済額。"""
    from services.loan import apply_income

    now = _now()
    await DBService.pool.execute(
        "UPDATE company_accounts SET balance = balance - ?, updated_at = ? "
        "WHERE ticker = ?",
        (amount, now, ticker),
    )
    repaid, _ = await apply_income(requester, amount)
    await DBService.pool.execute(
        "INSERT INTO company_ledger (ticker, user_id, kind, amount, created_at) "
        "VALUES (?, ?, 'withdraw', ?, ?)",
        (ticker, requester, amount, now),
    )
    await DBService.pool.commit()
    return repaid


async def request_withdraw(user_id: int, ticker: str, amount: int) -> dict:
    """引出を申請する。設立者本人は即時実行される。

    メンバーのみ可。戻り値は {request_id / 即時実行時は paid}。
    残高不足時は LookupError ではなく ValueError (金庫の残高不足)。
    """
    await ensure_company_schema()
    await expire_requests()
    stock = await _company_stock(ticker)
    if amount < 1:
        raise ValueError("引出額は1以上にしてください")
    if amount > SQLITE_MAX_INT:
        raise ValueError("金額が多すぎます")
    if not await is_member(stock.ticker, user_id):
        raise ValueError(f"{stock.ticker} のメンバーではありません")
    account = await get_account(stock.ticker)
    if account["balance"] < amount:
        raise ValueError(
            f"金庫の残高が足りません (残高{account['balance']:,})"
        )
    if is_owner(stock, user_id):
        repaid = await _pay_withdraw(stock.ticker, user_id, amount)
        return {"paid": True, "repaid": repaid}
    now = _now()
    cursor = await DBService.pool.execute(
        "INSERT INTO withdraw_requests "
        "(ticker, requester, amount, status, created_at) "
        "VALUES (?, ?, ?, 'pending', ?) RETURNING id",
        (stock.ticker, user_id, amount, now),
    )
    row = await cursor.fetchone()
    await cursor.close()
    await DBService.pool.commit()
    return {"paid": False, "request_id": int(row["id"])}


async def get_request(request_id: int) -> dict | None:
    """引出申請を1件取得。なければNone。"""
    await ensure_company_schema()
    cursor = await DBService.pool.execute(
        "SELECT * FROM withdraw_requests WHERE id = ?", (request_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    return dict(row) if row else None


async def expire_requests() -> int:
    """期限切れのpending申請を失効させる。戻り値は失効件数。"""
    await ensure_company_schema()
    cutoff = (
        datetime.now(UTC) - timedelta(hours=WITHDRAW_EXPIRY_HOURS)
    ).isoformat()
    cursor = await DBService.pool.execute(
        "UPDATE withdraw_requests SET status = 'expired' "
        "WHERE status = 'pending' AND created_at < ?",
        (cutoff,),
    )
    n = cursor.rowcount if cursor.rowcount is not None else 0
    await cursor.close()
    await DBService.pool.commit()
    return int(n)


async def pending_requests(ticker: str) -> list[dict]:
    """会社の未決引出申請一覧 (期限切れは自動失効)。"""
    await ensure_company_schema()
    await expire_requests()
    stock = await _company_stock(ticker)
    cursor = await DBService.pool.execute(
        "SELECT * FROM withdraw_requests WHERE ticker = ? AND status = 'pending' "
        "ORDER BY id",
        (stock.ticker,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [dict(r) for r in rows]


async def decide(
    approver_id: int, request_id: int, approve: bool
) -> dict:
    """引出申請を承認/拒否する。設立者のみ可。

    戻り値は {amount, repaid} (承認時) または {amount} (拒否時)。
    """
    await ensure_company_schema()
    await expire_requests()
    req = await get_request(request_id)
    if req is None:
        raise ValueError("申請が見つかりません")
    if req["status"] != "pending":
        raise ValueError(f"この申請は既に{req['status']}です")
    stock = await _company_stock(req["ticker"])
    if not is_owner(stock, approver_id):
        raise ValueError("設立者のみ承認できます")
    now = _now()
    if approve:
        account = await get_account(stock.ticker)
        if account["balance"] < int(req["amount"]):
            await DBService.pool.execute(
                "UPDATE withdraw_requests SET status = 'denied', "
                "decided_at = ?, decided_by = ? WHERE id = ?",
                (now, approver_id, request_id),
            )
            await DBService.pool.commit()
            raise ValueError("金庫の残高が足りなくなりました (否認しました)")
        repaid = await _pay_withdraw(
            stock.ticker, int(req["requester"]), int(req["amount"])
        )
        await DBService.pool.execute(
            "UPDATE withdraw_requests SET status = 'approved', "
            "decided_at = ?, decided_by = ? WHERE id = ?",
            (now, approver_id, request_id),
        )
        await DBService.pool.commit()
        return {"amount": int(req["amount"]), "repaid": repaid}
    await DBService.pool.execute(
        "UPDATE withdraw_requests SET status = 'denied', "
        "decided_at = ?, decided_by = ? WHERE id = ?",
        (now, approver_id, request_id),
    )
    await DBService.pool.commit()
    return {"amount": int(req["amount"])}


async def dissolve(owner_id: int, ticker: str) -> dict:
    """会社を解散する。設立者のみ可。

    金庫残高を株主に保有株数比例 (端数切捨て・余りは設立者へ) で返金し、
    銘柄・保有・履歴・口座系データを消去する。台帳は監査用に残す。
    戻り値は {distributed: [(user_id, amount)], burned: 0}。
    """
    await ensure_company_schema()
    stock = await _company_stock(ticker)
    if not is_owner(stock, owner_id):
        raise ValueError("設立者のみ解散できます")
    account = await get_account(stock.ticker)
    balance = account["balance"]
    cursor = await DBService.pool.execute(
        "SELECT user_id, qty FROM holdings WHERE ticker = ?", (stock.ticker,)
    )
    holders = [(r["user_id"], int(r["qty"])) for r in await cursor.fetchall()]
    await cursor.close()
    total = sum(q for _, q in holders)
    distributed: list[tuple[int, int]] = []
    if balance > 0 and total > 0:
        paid = 0
        for user_id, qty in holders:
            share = balance * qty // total
            if share > 0:
                await _credit_user(user_id, share)
                paid += share
                distributed.append((user_id, share))
        rest = balance - paid
        if rest > 0:
            await _credit_user(owner_id, rest)
            distributed.append((owner_id, rest))
    elif balance > 0:
        await _credit_user(owner_id, balance)
        distributed.append((owner_id, balance))
    now = _now()
    for user_id, amount in distributed:
        await DBService.pool.execute(
            "INSERT INTO company_ledger (ticker, user_id, kind, amount, "
            "created_at) VALUES (?, ?, 'dissolve', ?, ?)",
            (stock.ticker, user_id, amount, now),
        )
    await DBService.pool.execute(
        "DELETE FROM holdings WHERE ticker = ?", (stock.ticker,)
    )
    await DBService.pool.execute(
        "DELETE FROM stock_history WHERE ticker = ?", (stock.ticker,)
    )
    await DBService.pool.execute(
        "DELETE FROM stocks WHERE ticker = ?", (stock.ticker,)
    )
    await DBService.pool.execute(
        "DELETE FROM company_accounts WHERE ticker = ?", (stock.ticker,)
    )
    await DBService.pool.execute(
        "DELETE FROM company_members WHERE ticker = ?", (stock.ticker,)
    )
    await DBService.pool.execute(
        "DELETE FROM company_invites WHERE ticker = ?", (stock.ticker,)
    )
    await DBService.pool.execute(
        "DELETE FROM withdraw_requests WHERE ticker = ?", (stock.ticker,)
    )
    await DBService.pool.commit()
    return {"distributed": distributed}


def _tax_for(balance: int) -> int:
    """金庫残高に対する所得税額。"""
    return max(TAX_MIN, int(balance * TAX_RATE))


async def collect_tax(now: datetime | None = None) -> dict:
    """全社の所得税を徴収する (日次・JST日付単位で二重徴収しない)。

    納税義務は金庫残高と無関係に毎日発生する (空金庫でも日額が滞納に
    積まれる)。徴収分は焼却する。残高不足分は滞納に積み、連続滞納が
    上限に達したら自動で取扱停止にする。戻り値は {burned, taxed,
    delisted}。
    """
    await ensure_company_schema()
    moment = now or datetime.now(JST)
    today = moment.date().isoformat()
    cursor = await DBService.pool.execute("SELECT * FROM company_accounts")
    accounts = [dict(r) for r in await cursor.fetchall()]
    await cursor.close()
    burned = 0
    taxed: list[dict] = []
    delisted: list[str] = []
    for acc in accounts:
        ticker = acc["ticker"]
        last = acc["last_taxed_at"]
        if last is not None and last[:10] >= today:
            continue
        stock = await get_stock(ticker)
        if stock is None or stock.owner_id is None:
            continue
        balance = int(acc["balance"])
        arrears = int(acc["tax_arrears"])
        missed = int(acc["tax_missed"])
        due = _tax_for(balance) + arrears
        take = min(balance, due)
        rest = due - take
        now_iso = _now()
        if take > 0:
            await DBService.pool.execute(
                "UPDATE company_accounts SET balance = balance - ? "
                "WHERE ticker = ?",
                (take, ticker),
            )
            await DBService.pool.execute(
                "INSERT INTO company_ledger (ticker, user_id, kind, amount, "
                "created_at) VALUES (?, ?, 'tax', ?, ?)",
                (ticker, stock.owner_id, take, now_iso),
            )
            burned += take
        if rest > 0:
            missed += 1
            await DBService.pool.execute(
                "UPDATE company_accounts SET tax_arrears = ?, tax_missed = ?, "
                "last_taxed_at = ?, updated_at = ? WHERE ticker = ?",
                (rest, missed, moment.isoformat(), now_iso, ticker),
            )
            if missed >= TAX_MISSED_LIMIT and stock.is_active:
                await DBService.pool.execute(
                    "UPDATE stocks SET is_active = 0, updated_at = ? "
                    "WHERE ticker = ?",
                    (now_iso, ticker),
                )
                delisted.append(ticker)
        else:
            await DBService.pool.execute(
                "UPDATE company_accounts SET tax_arrears = 0, tax_missed = 0, "
                "last_taxed_at = ?, updated_at = ? WHERE ticker = ?",
                (moment.isoformat(), now_iso, ticker),
            )
        taxed.append({"ticker": ticker, "tax": take, "arrears": rest})
    await DBService.pool.commit()
    return {"burned": burned, "taxed": taxed, "delisted": delisted}


async def pay_arrears(user_id: int, ticker: str, amount: int) -> dict:
    """滞納分を個人資金で追納する。メンバー可。戻り値は {paid, arrears}。"""
    await ensure_company_schema()
    stock = await _company_stock(ticker)
    if amount < 1:
        raise ValueError("追納額は1以上にしてください")
    if not await is_member(stock.ticker, user_id):
        raise ValueError(f"{stock.ticker} のメンバーではありません")
    account = await get_account(stock.ticker)
    if account["arrears"] <= 0:
        raise ValueError("滞納はありません")
    balance = await _user_balance(user_id)
    if balance < amount:
        raise LookupError(f"残高不足: 必要 {amount}")
    paid = min(amount, account["arrears"])
    now = _now()
    await DBService.pool.execute(
        "UPDATE users SET amount = amount - ? WHERE id = ?", (paid, user_id)
    )
    missed = account["missed"]
    arrears = account["arrears"] - paid
    if arrears <= 0:
        missed = 0
    await DBService.pool.execute(
        "UPDATE company_accounts SET tax_arrears = ?, tax_missed = ?, "
        "updated_at = ? WHERE ticker = ?",
        (arrears, missed, now, stock.ticker),
    )
    await DBService.pool.execute(
        "INSERT INTO company_ledger (ticker, user_id, kind, amount, created_at) "
        "VALUES (?, ?, 'tax_pay', ?, ?)",
        (stock.ticker, user_id, paid, now),
    )
    await DBService.pool.commit()
    return {"paid": paid, "arrears": arrears}


async def reopen_company(owner_id: int, ticker: str) -> None:
    """滞納解消後の取扱再開。設立者のみ可。滞納ありでは再開できない。"""
    await ensure_company_schema()
    stock = await _company_stock(ticker)
    if not is_owner(stock, owner_id):
        raise ValueError("設立者のみ再開できます")
    account = await get_account(stock.ticker)
    if account["arrears"] > 0:
        raise ValueError(
            f"滞納が残っています (残{account['arrears']:,})。追納してください"
        )
    await DBService.pool.execute(
        "UPDATE stocks SET is_active = 1, updated_at = ? WHERE ticker = ?",
        (_now(), stock.ticker),
    )
    await DBService.pool.commit()
