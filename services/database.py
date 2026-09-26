import aiosqlite


class DBService:
    pool: aiosqlite.Connection

    @classmethod
    async def connect(cls):
        cls.pool = await aiosqlite.connect("database.db")
        cls.pool.row_factory = aiosqlite.Row

    @classmethod
    async def close(cls):
        await cls.pool.close()
