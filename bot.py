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

# --- Подключение к Neon PostgreSQL ---
DATABASE_URL = "postgresql+asyncpg://neondb_owner:npg_ExoNs2fyWLi1@ep-solitary-tooth-bdmpamjk-pooler.c-8.us-east-2.aws.neon.tech/neondb"

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

async def get_db():
    async with async_session_maker() as session:
        yield session

# --- Расширенная Модель Пользователя ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(String, unique=True, nullable=True)
    username = Column(String, nullable=True)
    balance = Column(Float, default=1000.0)
    xp = Column(Integer, default=0)
    level = Column(String, default="Хомяк-новичок")
    streak = Column(Integer, default=1)
    last_login = Column(DateTime, default=datetime.utcnow)

# --- FastAPI приложение ---
app = FastAPI(title="Crypto Arena Backend Pro")

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

# --- 1. Рыночные цены в реальном времени ---
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

# --- 2. Профиль и Авторизация ---
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

# --- 3. Игровая механика: Сделки / Прогнозы (Long / Short) ---
@app.post("/api/trade")
async def make_trade(data: dict, db: AsyncSession = Depends(get_db)):
    telegram_id = data.get("telegram_id")
    amount = float(data.get("amount", 100))
    direction = data.get("direction") # "up" или "down"
    
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalars().first()
    
    if not user or user.balance < amount:
        raise HTTPException(status_code=400, detail="Недостаточно средств на балансе")
    
    # Симуляция исхода сделки (в продакшене привязывается к таймеру графика)
    is_win = random.choice([True, False])
    
    if is_win:
        profit = amount * 1.8
        user.balance += (profit - amount)
        user.xp += 50
        result_msg = f"Победа! Вы выиграли {profit} USDT"
    else:
        user.balance -= amount
        user.xp = max(0, user.xp - 20)
        result_msg = f"Поражение. Вы потеряли {amount} USDT"
        
    # Проверка апгрейда уровня
    if user.xp > 500:
        user.level = "Крипто-Трейдер"
    if user.xp > 2000:
        user.level = "Крипто-Титан"

    await db.commit()
    return {"success": is_win, "message": result_msg, "new_balance": user.balance, "xp": user.xp, "level": user.level}

# --- 4. Таблица лидеров ---
@app.get("/api/leaderboard")
async def get_leaderboard(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(desc(User.balance)).limit(10))
    users = result.scalars().all()
    return [{"username": u.username or "Аноним", "balance": u.balance, "xp": u.xp, "level": u.level} for u in users]

if __name__ == "__main__":
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
