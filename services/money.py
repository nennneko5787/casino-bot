import sqlite3

import aiosqlite
import discord

from objects.exceptions import AccountCreationFailed, CasinoBaseException
from objects.user import User
from services.database import DBService

# 1人あたりの残高上限。
# SQLite INTEGER の max (9223372036854775807) は SUM などの集約で
# integer overflow を起こすため、十分手前で頭打ちにする。
MAX_BALANCE = 10**15


async def createUser(
    user: discord.User | discord.Member, *, cursor: aiosqlite.Cursor
) -> sqlite3.Row:
    row = await (
        await DBService.pool.execute(
            "INSERT INTO users(id) VALUES (?) RETURNING *", (user.id,)
        )
    ).fetchone()
    await DBService.pool.commit()

    if not row:
        raise AccountCreationFailed()

    return row


async def getUser(user: discord.User | discord.Member) -> User:
    cursor = await DBService.pool.execute(
        "SELECT * FROM users WHERE id = ?", (user.id,)
    )
    row = await cursor.fetchone()
    await cursor.close()

    if not row:
        row = await createUser(user, cursor=cursor)

    return User(**dict(row))


async def saveUser(user: User) -> User:
    row = await (
        await DBService.pool.execute(
            "UPDATE users SET amount = ? WHERE id = ? RETURNING *",
            (
                min(user.amount, MAX_BALANCE),
                user.id,
            ),
        )
    ).fetchone()
    await DBService.pool.commit()

    if not row:
        raise CasinoBaseException()

    return User(**dict(row))
