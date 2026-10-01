"""通貨供給が SQLite の int64 上限を超えてしまった経済を縮尺調整する。

無限増殖していた蛇口 (配当・ロイヤリティ) の是正で供給は抑えられるが、
既に SQLite INTEGER の max (9223372036854775807) を超えた残高は
`SELECT SUM(amount) FROM users` が integer overflow で落ちるため、
_snapshot 記録も通貨価値指数も止まったままになる。
このスクリプトは全金額を一定の規則で縮め、DB を正常な範囲に戻す。

変換は「膝付き線形圧縮」:

    f(x) = x                     (x <= K)   ← 一切失わない
    f(x) = K + (x - K) / S       (x >  K)   ← 線形に圧縮

この形は strict に単調増加なので順序 (順位) が完全に保存され、
K 以下は完全に非損失。単純な一律除算や「100 で底止め」と違い、
小額所持者 (1000 円だけの人など) が圧縮されて損をしない。

使い方:
    # 1. まず現状を見る (DB は書き換えない)
    uv run scripts/rescale_economy.py

    # 2. K を決めたら適用 (knee= 据え置きライン、scale= 圧縮倍率)
    uv run scripts/rescale_economy.py --apply --knee 10000000 --scale 10000000

適用前に database.db を database.db.bak.<UTC時刻> へ自動バックアップする。
SQLite は本来 1 命令 = 1 トランザクションなので、途中で失敗しても
元の値には戻らない (バックアップから戻すこと)。
"""

import argparse
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DB_PATH = Path("database.db")

# 縮尺調整の対象 (table, column, 最小値)。
# 最小値は「0 未満にしない」「价格为 1 未満にしない」ための保険。
TARGETS: list[tuple[str, str, int]] = [
    ("users", "amount", 0),
    ("users", "debt", 0),
    ("stocks", "price", 1),
    ("holdings", "avg_cost", 1),
    ("company_accounts", "balance", 0),
    ("company_accounts", "tax_arrears", 0),
    ("company_ledger", "amount", 0),
    ("withdraw_requests", "amount", 0),
    ("market_snapshots", "total_supply", 0),
    ("stock_history", "price", 1),
]


def compress(value: int, knee: int, scale: int) -> int:
    """膝付き線形圧縮。knee 以下は不変、それ以上は線形に圧縮する。"""
    if value <= knee:
        return value
    # 丸めは中央値丸め (系統的な下振れを避ける)
    return knee + (value - knee + scale // 2) // scale


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not table_exists(conn, table):
        return False
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    return column in cols


def percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
    return ordered[idx]


def report(conn: sqlite3.Connection) -> None:
    """dry-run: 分布と変換結果を表示する。"""
    if not table_exists(conn, "users"):
        print("users テーブルがありません。")
        return
    amounts = [r[0] or 0 for r in conn.execute("SELECT amount FROM users")]
    debts = [r[0] or 0 for r in conn.execute("SELECT debt FROM users")]
    total = sum(amounts)
    print(f"ユーザー数: {len(amounts)}")
    print(f"通貨総量 (Python側で集計): {total:,}")
    print()
    print("残高の分布:")
    for p in (0.0, 0.1, 0.5, 0.9, 0.99, 1.0):
        print(f"  p{p * 100:<5.0f} {percentile(amounts, p):>25,}")
    print()
    if debts:
        print("借金の分布:")
        for p in (0.5, 0.9, 1.0):
            print(f"  p{p * 100:<5.0f} {percentile(debts, p):>25,}")
        print(f"  借金あり: {sum(1 for d in debts if d > 0)} 人")
        print()
    print("推定変換倍率 (目標 1e12):", max(1, -(-total // 10**12)))


def apply_scale(conn: sqlite3.Connection, knee: int, scale: int) -> dict:
    counts: dict[str, int] = {}
    for table, column, floor in TARGETS:
        if not column_exists(conn, table, column):
            continue
        rows = conn.execute(
            f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL"
        ).fetchall()
        updates = []
        for rowid, value in rows:
            new_value = max(floor, compress(int(value), knee, scale))
            if new_value != value:
                updates.append((new_value, table, rowid, column))
        for new_value, tbl, rowid, col in updates:
            conn.execute(
                f"UPDATE {tbl} SET {col} = ? WHERE rowid = ?", (new_value, rowid)
            )
        counts[f"{table}.{column}"] = len(updates)
    conn.commit()
    return counts


def backup() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = DB_PATH.with_name(f"{DB_PATH.name}.bak.{stamp}")
    shutil.copy2(DB_PATH, dest)
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="実際に書き換える")
    parser.add_argument("--knee", type=int, default=10**7, help="据え置きライン")
    parser.add_argument("--scale", type=int, default=10**7, help="圧縮倍率")
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} がありません。リポジトリ直下で実行してください。")

    conn = sqlite3.connect(DB_PATH)
    try:
        if not args.apply:
            print("=== dry-run (DB は書き換えません) ===")
            report(conn)
            print()
            print(f"適用するには: --apply --knee {args.knee} --scale {args.scale}")
            return

        if args.knee < 0 or args.scale < 1:
            raise SystemExit("--knee は 0 以上、--scale は 1 以上を指定してください。")

        dest = backup()
        print(f"バックアップしました: {dest}")
        counts = apply_scale(conn, args.knee, args.scale)
        print(f"knee={args.knee} scale={args.scale} で縮尺調整しました。")
        for key, n in counts.items():
            if n:
                print(f"  {key}: {n} 行")
        print()
        print("=== 適用後の分布 ===")
        report(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
