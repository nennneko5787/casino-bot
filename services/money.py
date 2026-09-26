import sqlite3

import aiosqlite
import discord

from objects.exceptions import AccountCreationFailed, CasinoBaseException
from objects.user import User
from services.database import DBService


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
                user.amount,
                user.id,
            ),
        )
    ).fetchone()
    await DBService.pool.commit()

    if not row:
        raise CasinoBaseException()

    return User(**dict(row))
