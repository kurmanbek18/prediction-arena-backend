import os
import asyncio
import random
import httpx
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer, String, Float, DateTime, select, desc
from datetime import datetime
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
    level = Column(String, default="Хомяк-новичок Академии")

class CustomCoin(Base):
    __tablename__ = "custom_coins"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    symbol = Column(String, nullable=False)
    creator = Column(String, nullable=False)
    price = Column(Float, default=1.0)
    market_cap = Column(Float, default=1000.0)

app = FastAPI(title="Crypto Arena Pro Ultimate")

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

@app.post("/api/trade")
async def make_trade(data: dict, db: AsyncSession = Depends(get_db)):
    telegram_id = data.get("telegram_id")
    amount = float(data.get("amount", 100))
    direction = data.get("direction")
    
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalars().first()
    if not user or user.balance < amount:
        raise HTTPException(status_code=400, detail="Недостаточно средств")
    
    # Случайное событие рынка: шанс хакерской атаки или скама
    event_chance = random.random()
    if event_chance < 0.05: # 5% шанс взлома протокола
        user.balance -= (amount * 0.5)
        await db.commit()
        return {"success": False, "message": "⚠️ Внимание! Биржу атаковали хакеры, часть сделки заморожена (-50% ставки). Учите риск-менеджмент!", "new_balance": user.balance}

    is_win = random.choice([True, False])
    if is_win:
        profit = amount * 1.8
        user.balance += (profit - amount)
        user.xp += 50
        msg = f"🟢 Успешный прогноз! Прибыль: +{profit - amount} USDT"
    else:
        user.balance -= amount
        user.xp = max(0, user.xp - 20)
        msg = f"🔴 Ошибка прогноза. Потеряно: -{amount} USDT"

    await db.commit()
    return {"success": is_win, "message": msg, "new_balance": user.balance, "xp": user.xp, "level": user.level}

# Экономика пользовательских монет (Микро-экономика / Синдикаты)
@app.get("/api/economy/coins")
async def get_custom_coins(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(CustomCoin).limit(20))
    coins = result.scalars().all()
    return [{"id": c.id, "name": c.name, "symbol": c.symbol, "creator": c.creator, "price": c.price} for c in coins]

@app.post("/api/economy/create")
async def create_custom_coin(data: dict, db: AsyncSession = Depends(get_db)):
    name = data.get("name")
    symbol = data.get("symbol")
    creator = data.get("creator", "Аноним")
    if not name or not symbol:
        raise HTTPException(status_code=400, detail="Заполните название и тикер")
    
    coin = CustomCoin(name=name, symbol=symbol.upper(), creator=creator, price=random.uniform(0.5, 5.0))
    db.add(coin)
    await db.commit()
    return {"success": True, "message": f"Монета ${symbol.upper()} успешно запущена в своей экосистеме!"}

if __name__ == "__main__":
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
