import logging

import aiosqlite

logger = logging.getLogger(__name__)

# 他プロセス (旧インスタンス / alembic / 手元確認) が database.db を
# 掴んでいると "database is locked" で即死する。書き込みを待てるよう長めに取る。
BUSY_TIMEOUT_SEC = 30.0


class DBService:
    pool: aiosqlite.Connection

    @classmethod
    async def connect(cls):
        cls.pool = await aiosqlite.connect("database.db", timeout=BUSY_TIMEOUT_SEC)
        cls.pool.row_factory = aiosqlite.Row
        # WAL: 読み取りと書き込みがmutex にならず、ロック衝突が減る。
        # journal_mode はDBファイルに記録され他プロセスにも効く。
        try:
            await cls.pool.execute("PRAGMA journal_mode=WAL")
            busy_ms = int(BUSY_TIMEOUT_SEC * 1000)
            await cls.pool.execute(f"PRAGMA busy_timeout={busy_ms}")
            await cls.pool.commit()
        except Exception:
            logger.warning("WAL/ busy_timeout の設定に失敗しました", exc_info=True)

    @classmethod
    async def close(cls):
        await cls.pool.close()
