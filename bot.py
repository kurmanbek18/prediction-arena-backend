import os
import asyncio
import random
import httpx
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer, String, Float, select
import uvicorn

DATABASE_URL = "postgresql+asyncpg://neondb_owner:npg_ExoNs2fyWLi1@ep-solitary-tooth-bdmpamjk-pooler.c-8.us-east-2.aws.neon.tech/neondb"

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

async def get_db():
    async with async_session_maker() as session:
        yield session

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(String, unique=True, nullable=True)
    username = Column(String, default="Трейдер")
    balance = Column(Float, default=1000.0)
    xp = Column(Integer, default=0)
    level = Column(String, default="Элитный Трейдер")

class CustomCoin(Base):
    __tablename__ = "custom_coins"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    symbol = Column(String, nullable=False)
    creator = Column(String, nullable=False)
    price = Column(Float, default=1.0)
    treasury = Column(Float, default=1000.0)

class Duel(Base):
    __tablename__ = "duels"
    id = Column(Integer, primary_key=True, index=True)
    creator_id = Column(String, nullable=False)
    creator_name = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    target_coin = Column(String, default="BTC")
    prediction = Column(String, default="up")
    status = Column(String, default="waiting")
    winner_name = Column(String, nullable=True)

app = FastAPI(title="Crypto Arena Pro Ultimate Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@app.get("/api/market")
async def get_market_prices():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin,ethereum,solana,toncoin", "vs_currencies": "usd", "include_24hr_change": "true"}
            )
            data = response.json()
            return {
                "BTC": {"price": data.get("bitcoin", {}).get("usd", 65000), "change": data.get("bitcoin", {}).get("usd_24h_change", 2.5)},
                "ETH": {"price": data.get("ethereum", {}).get("usd", 3500), "change": data.get("ethereum", {}).get("usd_24h_change", 1.8)},
                "SOL": {"price": data.get("solana", {}).get("usd", 150), "change": data.get("solana", {}).get("usd_24h_change", -0.5)},
                "TON": {"price": data.get("toncoin", {}).get("usd", 5.5), "change": data.get("toncoin", {}).get("usd_24h_change", 4.1)}
            }
    except Exception:
        return {
            "BTC": {"price": 65000.0, "change": 2.5},
            "ETH": {"price": 3500.0, "change": 1.8},
            "SOL": {"price": 150.0, "change": -0.5},
            "TON": {"price": 5.5, "change": 4.1}
        }

@app.get("/api/user/{telegram_id}")
async def get_user(telegram_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalars().first()
    if not user:
        user = User(telegram_id=telegram_id, username=f"Trader_{telegram_id[-4:]}", balance=1000.0, xp=100)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user

@app.post("/api/user/update_name")
async def update_username(data: dict, db: AsyncSession = Depends(get_db)):
    telegram_id = data.get("telegram_id")
    new_name = data.get("username")
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalars().first()
    if user and new_name:
        user.username = new_name
        await db.commit()
        return {"success": True, "username": user.username}
    raise HTTPException(status_code=400, detail="Ошибка смены имени")

@app.get("/api/economy/coins")
async def get_custom_coins(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(CustomCoin).limit(20))
    coins = result.scalars().all()
    return [{"id": c.id, "name": c.name, "symbol": c.symbol, "creator": c.creator, "price": c.price, "treasury": c.treasury} for c in coins]

@app.post("/api/economy/create")
async def create_custom_coin(data: dict, db: AsyncSession = Depends(get_db)):
    name = data.get("name")
    symbol = data.get("symbol")
    creator = data.get("creator", "Аноним")
    if not name or not symbol:
        raise HTTPException(status_code=400, detail="Заполните поля")
    coin = CustomCoin(name=name, symbol=symbol.upper(), creator=creator, price=round(random.uniform(1.0, 10.0), 2), treasury=5000.0)
    db.add(coin)
    await db.commit()
    return {"success": True, "message": f"Синдикат ${symbol.upper()} успешно развернут!"}

@app.post("/api/duels/create")
async def create_duel(data: dict, db: AsyncSession = Depends(get_db)):
    telegram_id = data.get("telegram_id")
    amount = float(data.get("amount", 50))
    coin = data.get("coin", "BTC")
    prediction = data.get("prediction", "up")
    
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalars().first()
    if not user or user.balance < amount:
        raise HTTPException(status_code=400, detail="Недостаточно средств")
    
    user.balance -= amount
    duel = Duel(creator_id=telegram_id, creator_name=user.username, amount=amount, target_coin=coin, prediction=prediction, status="waiting")
    db.add(duel)
    await db.commit()
    return {"success": True, "message": "Дуэль создана!"}

@app.get("/api/duels")
async def get_duels(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Duel).where(Duel.status == "waiting"))
    duels = result.scalars().all()
    return [{"id": d.id, "creator": d.creator_name, "amount": d.amount, "coin": d.target_coin, "prediction": d.prediction} for d in duels]

@app.post("/api/duels/join")
async def join_duel(data: dict, db: AsyncSession = Depends(get_db)):
    duel_id = data.get("duel_id")
    telegram_id = data.get("telegram_id")
    
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    opponent = result.scalars().first()
    duel_res = await db.execute(select(Duel).where(Duel.id == duel_id))
    duel = duel_res.scalars().first()
    
    if not duel or duel.status != "waiting":
        raise HTTPException(status_code=400, detail="Дуэль недоступна")
    if not opponent or opponent.balance < duel.amount:
        raise HTTPException(status_code=400, detail="Недостаточно средств")
        
    opponent.balance -= duel.amount
    market_trend = random.choice(["up", "down"])
    winner = duel.creator_name if duel.prediction == market_trend else opponent.username
    
    duel.status = "finished"
    duel.winner_name = winner
    prize = duel.amount * 2
    
    if winner == opponent.username:
        opponent.balance += prize
    else:
        creator_res = await db.execute(select(User).where(User.telegram_id == duel.creator_id))
        creator = creator_res.scalars().first()
        if creator: creator.balance += prize

    await db.commit()
    return {"success": True, "winner": winner, "trend": market_trend, "prize": prize}

if __name__ == "__main__":
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
