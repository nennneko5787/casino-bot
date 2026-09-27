"""管理者判定: サーバー管理者またはenv指定ID。

管理者限定コマンドには `@commands.has_guild_permissions(administrator=True)`
の代わりに `@admin_only()` を付ける。`.env` の `admin_user_ids` に
カンマ区切りで書いたユーザーIDも、サーバー管理者と同じ権限で触れる。

例: admin_user_ids=123456789012345678,987654321098765432
"""

from __future__ import annotations

import os

import dotenv
from discord.ext import commands

dotenv.load_dotenv()

ENV_KEY = "admin_user_ids"


def get_admin_user_ids() -> set[int]:
    """envのID一覧をパースする。空文字・不正値は無視する。"""
    ids: set[int] = set()
    for part in os.environ.get(ENV_KEY, "").split(","):
        token = part.strip().strip("<@!>")
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            continue
    return ids


def is_admin(user: object) -> bool:
    """サーバー管理者かenv指定IDならTrue。"""
    if getattr(user, "id", None) in get_admin_user_ids():
        return True
    perms = getattr(user, "guild_permissions", None)
    return bool(perms is not None and perms.administrator)


def admin_only():
    """管理者限定デコレータ。権限不足時は MissingPermissions を送出する。"""

    async def predicate(ctx: commands.Context) -> bool:
        if is_admin(ctx.author):
            return True
        raise commands.MissingPermissions(["administrator"])

    return commands.check(predicate)
