import asyncio
import faulthandler
import logging
import os
import signal
from contextlib import asynccontextmanager
from http.client import HTTPException

import discord
import dotenv
from discord.ext import commands
from fastapi import FastAPI, Header
from pydantic import BaseModel

from services.database import DBService
from services.money import getUser, saveUser

dotenv.load_dotenv()

logger = logging.getLogger(__name__)

# 起動時に順番に読み込むcog。setup_hook は login() 内で呼ばれるため、
# ここで止まると on_ready まで来ない (= コマンドが一切反応しなくなる)。
EXTENSIONS = (
    "cogs.error",
    "cogs.help",
    "cogs.slot",
    "cogs.stake",
    "cogs.highlow",
    "cogs.othello",
    "cogs.shogi",
    "cogs.chess",
    "cogs.ai_chat",
    "cogs.stats",
    "cogs.payment",
    "cogs.blackjack",
    "cogs.stock",
    "cogs.company",
    "cogs.ranking",
    "cogs.loan",
    "cogs.mission",
    "cogs.level",
    "cogs.sync",
)

# othello の絵文字取得 (wait_for 60+120秒) を許容する上限。
# 超過したcog は TimeoutError で起動を中断し、固まったまま放置しない。
EXT_LOAD_TIMEOUT = 300.0

intents = discord.Intents.all()
bot = commands.Bot("c#", intents=intents, help_command=None)

discord.utils.setup_logging()

# 起動中の無応答を調査するため `kill -USR1 <pid>` で全スレッドのスタックをdump できるようにする
if hasattr(signal, "SIGUSR1"):
    faulthandler.register(signal.SIGUSR1, all_threads=True)


@bot.event
async def setup_hook():
    from services.cooldown import bot_check, interaction_check

    bot.add_check(bot_check)
    # インスタンス属性への代入なので self は束縛されず、引数1つの関数で正しく動く
    bot.tree.interaction_check = interaction_check  # ty: ignore[invalid-assignment]
    for ext in EXTENSIONS:
        # 止まった cog を特定できるよう、読み込み直前に必ず1行ログを出す
        logger.info("cogs: %s を読み込み中", ext)
        await asyncio.wait_for(bot.load_extension(ext), timeout=EXT_LOAD_TIMEOUT)
    logger.info("cogs: すべてのcogの読み込みが完了しました")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await DBService.connect()
    task = asyncio.create_task(bot.start(os.getenv("discord") or ""))
    yield
    task.cancel()
    await DBService.pool.close()


app = FastAPI(lifespan=lifespan)


class UserData(BaseModel):
    id: int
    amount: int


@app.get("/api/users/{userId:int}")
async def getUserStatus(userId: int):
    guild = bot.get_guild(1431182673119809536)
    if not guild:
        raise HTTPException(500, "guild not found")

    member = await guild.fetch_member(userId)
    if not member:
        raise HTTPException(404, "member not found")

    userData = await getUser(member)
    return UserData(id=userData.id, amount=userData.amount)


class SendMoneyModel(BaseModel):
    amount: int


class SendMoneyResponse(BaseModel):
    paid: int
    user: UserData


@app.post("/api/users/{userId:int}/wallet")
async def sendMoney(
    userId: int,
    model: SendMoneyModel,
    secret: str = Header(..., alias="x-super-secret"),
) -> SendMoneyResponse:
    if not secret == os.environ["secret_key"]:
        raise HTTPException(403)

    guild = bot.get_guild(1431182673119809536)
    if not guild:
        raise HTTPException(500, "guild not found")

    member = await guild.fetch_member(userId)
    if not member:
        raise HTTPException(404, "member not found")

    userData = await getUser(member)
    userData.amount += model.amount
    await saveUser(userData)

    return SendMoneyResponse(
        paid=model.amount, user=UserData(id=userData.id, amount=userData.amount)
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=7777)
