"""借金: 借入・返済・入金の自動天引き・総資産番付の集計。

借金残高の上限は MAX_DEBT、手数料は FEE_RATE 上乗せ (整数演算で確定)。
入金はすべて借金返済優先 (apply_income) のため踏み倒し不可。
"""

from __future__ import annotations

from services.database import DBService
from services.message import buildAmountText

MAX_DEBT = 1000
FEE_RATE = 0.10
RANKING_LIMIT = 10


async def _balance_debt(user_id: int) -> tuple[int, int]:
    """(残高, 借金残高) を返す。行が無ければ作成 (初期残高100)。"""
    cursor = await DBService.pool.execute(
        "SELECT amount, debt FROM users WHERE id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        await DBService.pool.execute("INSERT INTO users(id) VALUES (?)", (user_id,))
        await DBService.pool.commit()
        return 100, 0
    return row["amount"], row["debt"]


async def get_debt(user_id: int) -> int:
    _, debt = await _balance_debt(user_id)
    return debt


async def borrowable(user_id: int) -> int:
    """あと借りられる元本 (手数料込みで残高上限に収まる額)。"""
    _, debt = await _balance_debt(user_id)
    room = MAX_DEBT - debt
    if room <= 0:
        return 0
    # (a*110+99)//100 <= room を満たす最大の a (整数演算)
    return (room * 10) // 11


async def borrow(user_id: int, amount: int) -> tuple[int, int]:
    """借入。戻り値は (受取額, 借金加算額)。上限・不正値は ValueError。"""
    if amount < 1:
        raise ValueError("借りる額は1以上にしてください")
    if amount > MAX_DEBT:
        raise ValueError(f"一度に借りられるのは{MAX_DEBT}までです")
    # 手数料込み (×1.1切り上げ) を整数演算で確定させる
    debt_add = (amount * 110 + 99) // 100
    _, debt = await _balance_debt(user_id)
    if debt + debt_add > MAX_DEBT:
        rest = await borrowable(user_id)
        raise ValueError(
            f"借金残高の上限({MAX_DEBT})を超えます。今借りられるのは{rest}までです"
        )
    await DBService.pool.execute(
        "UPDATE users SET amount = amount + ?, debt = debt + ? WHERE id = ?",
        (amount, debt_add, user_id),
    )
    await DBService.pool.commit()
    return amount, debt_add


async def repay(user_id: int, amount: int) -> tuple[int, int]:
    """返済。戻り値は (返済額, 残債)。"""
    if amount < 1:
        raise ValueError("返済額は1以上にしてください")
    balance, debt = await _balance_debt(user_id)
    if debt <= 0:
        raise ValueError("借金はありません")
    if balance <= 0:
        raise ValueError("残高がありません")
    paid = min(amount, debt, balance)
    await DBService.pool.execute(
        "UPDATE users SET amount = amount - ?, debt = debt - ? WHERE id = ?",
        (paid, paid, user_id),
    )
    await DBService.pool.commit()
    return paid, debt - paid


async def apply_income(user_id: int, amount: int) -> tuple[int, int]:
    """入金を借金返済優先で配分。戻り値は (返済充当額, 残高加算額)。

    amount <= 0 (負け分の減算など) や借金なしの場合はそのまま残高に加減算。
    """
    _, debt = await _balance_debt(user_id)
    if amount <= 0 or debt <= 0:
        await DBService.pool.execute(
            "UPDATE users SET amount = amount + ? WHERE id = ?",
            (amount, user_id),
        )
        await DBService.pool.commit()
        return 0, amount
    repaid = min(debt, amount)
    rest = amount - repaid
    await DBService.pool.execute(
        "UPDATE users SET amount = amount + ?, debt = debt - ? WHERE id = ?",
        (rest, repaid, user_id),
    )
    await DBService.pool.commit()
    return repaid, rest


def repay_note(repaid: int) -> str:
    """自動返済が発生した旨の表示用メモ。なければ空文字。"""
    if repaid > 0:
        return f"\n（借金{buildAmountText(repaid)}を自動返済しました）"
    return ""


async def net_worth_ranking(limit: int = RANKING_LIMIT, *, poor: bool = False) -> list:
    """総資産 (残高 + 株評価額 − 借金) の番付。[(user_id, net)] を順位順で返す。"""
    order = "ASC" if poor else "DESC"
    cursor = await DBService.pool.execute(
        "SELECT u.id AS id, "
        "u.amount + COALESCE(SUM(h.qty * s.price), 0) - u.debt AS net "
        "FROM users u "
        "LEFT JOIN holdings h ON h.user_id = u.id "
        "LEFT JOIN stocks s ON s.ticker = h.ticker "
        f"GROUP BY u.id ORDER BY net {order} LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [(r["id"], r["net"]) for r in rows]
