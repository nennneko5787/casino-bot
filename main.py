import asyncio
import os
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


intents = discord.Intents.all()
bot = commands.Bot("c#", intents=intents, help_command=None)

discord.utils.setup_logging()


@bot.event
async def setup_hook():
    from services.cooldown import bot_check, interaction_check

    bot.add_check(bot_check)
    # インスタンス属性への代入なので self は束縛されず、引数1つの関数で正しく動く
    bot.tree.interaction_check = interaction_check  # ty: ignore[invalid-assignment]
    await bot.load_extension("cogs.error")
    await bot.load_extension("cogs.help")
    await bot.load_extension("cogs.slot")
    await bot.load_extension("cogs.stake")
    await bot.load_extension("cogs.highlow")
    await bot.load_extension("cogs.othello")
    await bot.load_extension("cogs.shogi")
    await bot.load_extension("cogs.chess")
    await bot.load_extension("cogs.ai_chat")
    await bot.load_extension("cogs.stats")
    await bot.load_extension("cogs.payment")
    await bot.load_extension("cogs.blackjack")
    await bot.load_extension("cogs.stock")
    await bot.load_extension("cogs.company")
    await bot.load_extension("cogs.ranking")
    await bot.load_extension("cogs.loan")
    await bot.load_extension("cogs.mission")
    await bot.load_extension("cogs.level")
    await bot.load_extension("cogs.sync")


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
