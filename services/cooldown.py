"""全コマンド共通レートリミット: ユーザー毎・コマンド毎に7秒で4回。

prefix/hybrid は bot.check、slash は tree.interaction_check から使う。
管理者と c#sync は対象外。超過時は CommandOnCooldown を送出し、
cogs/error.py 側で ephemeral に「あとX秒」と返す。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from discord import app_commands
from discord.ext import commands

from services.admin import is_admin

WINDOW_SECONDS = 7.0
LIMIT = 4

_hits: dict[tuple[int, str], deque[float]] = defaultdict(deque)


def _touch(user_id: int, key: str) -> float:
    """記録して超過時の残り秒数を返す。OK時は0.0。"""
    now = time.monotonic()
    dq = _hits[(user_id, key)]
    while dq and now - dq[0] >= WINDOW_SECONDS:
        dq.popleft()
    if len(dq) >= LIMIT:
        return WINDOW_SECONDS - (now - dq[0])
    dq.append(now)
    return 0.0


def check_message_rate(user_id: int, key: str = "ai-chat") -> float:
    """on_message 起動用 (AIチャット等)。OK時は0.0、超過時は残り秒数。"""
    return _touch(user_id, key)


def _skip_user(user: object) -> bool:
    try:
        return is_admin(user)
    except Exception:  # noqa: BLE001 - 判定不能時は制限側に倒す
        return False


async def bot_check(ctx: commands.Context) -> bool:
    """bot.check 用。管理者・コマンドなしはスルー。"""
    author = getattr(ctx, "author", None)
    if author is None or _skip_user(author):
        return True
    cmd = getattr(ctx, "command", None)
    if cmd is None:
        return True
    if getattr(cmd, "qualified_name", "") == "sync":
        return True
    key = cmd.qualified_name or "unknown"
    retry = _touch(author.id, f"cmd:{key}")
    if retry > 0:
        raise commands.CommandOnCooldown(
            commands.Cooldown(LIMIT, WINDOW_SECONDS), retry,
            commands.BucketType.user,
        )
    return True


async def interaction_check(interaction) -> bool:
    """tree.interaction_check 用。管理者はスルー。"""
    user = getattr(interaction, "user", None)
    if user is None or _skip_user(user):
        return True
    cmd = getattr(interaction, "command", None)
    name = getattr(cmd, "qualified_name", None) or getattr(cmd, "name", None)
    if not name or name == "sync":
        return True
    retry = _touch(user.id, f"cmd:{name}")
    if retry > 0:
        raise app_commands.CommandOnCooldown(
            app_commands.Cooldown(LIMIT, WINDOW_SECONDS), retry,
        )
    return True
