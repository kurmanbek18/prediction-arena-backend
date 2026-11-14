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

# --- Модели БД ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(String, unique=True, nullable=True)
    username = Column(String, nullable=True)
    balance = Column(Float, default=1000.0)
    xp = Column(Integer, default=0)
    level = Column(String, default="Хомяк-новичок")

class Duel(Base):
    __tablename__ = "duels"
    id = Column(Integer, primary_key=True, index=True)
    creator_id = Column(String, nullable=False)
    creator_name = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(String, default="waiting") # waiting, finished
    winner_name = Column(String, nullable=True)

# --- FastAPI приложение ---
app = FastAPI(title="Crypto Arena Pro + Duels")

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

# --- Рынок ---
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

# --- Профиль ---
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

# --- Сделки (Лонг/Шорт) ---
@app.post("/api/trade")
async def make_trade(data: dict, db: AsyncSession = Depends(get_db)):
    telegram_id = data.get("telegram_id")
    amount = float(data.get("amount", 100))
    direction = data.get("direction")
    
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalars().first()
    
    if not user or user.balance < amount:
        raise HTTPException(status_code=400, detail="Недостаточно средств")
    
    is_win = random.choice([True, False])
    if is_win:
        profit = amount * 1.8
        user.balance += (profit - amount)
        user.xp += 50
        msg = f"Победа! +{profit - amount} USDT"
    else:
        user.balance -= amount
        user.xp = max(0, user.xp - 20)
        msg = f"Поражение. -{amount} USDT"

    await db.commit()
    return {"success": is_win, "message": msg, "new_balance": user.balance, "xp": user.xp, "level": user.level}

# --- ДУЭЛИ 1 на 1 ---
@app.post("/api/duels/create")
async def create_duel(data: dict, db: AsyncSession = Depends(get_db)):
    telegram_id = data.get("telegram_id")
    amount = float(data.get("amount", 50))
    
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalars().first()
    if not user or user.balance < amount:
        raise HTTPException(status_code=400, detail="Недостаточно средств для дуэли")
    
    user.balance -= amount
    duel = Duel(creator_id=telegram_id, creator_name=user.username, amount=amount, status="waiting")
    db.add(duel)
    await db.commit()
    return {"success": True, "message": "Дуэль создана! Ожидаем соперника."}

@app.get("/api/duels")
async def get_duels(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Duel).where(Duel.status == "waiting"))
    duels = result.scalars().all()
    return [{"id": d.id, "creator": d.creator_name, "amount": d.amount} for d in duels]

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
        raise HTTPException(status_code=400, detail="Недостаточно средств для участия")
        
    opponent.balance -= duel.amount
    
    # Случайный победитель дуэли
    winner = random.choice([duel.creator_name, opponent.username])
    duel.status = "finished"
    duel.winner_name = winner
    
    # Находим победителя и отдаем ему двойной банк
    win_sum = duel.amount * 2
    if winner == opponent.username:
        opponent.balance += win_sum
    else:
        creator_res = await db.execute(select(User).where(User.telegram_id == duel.creator_id))
        creator = creator_res.scalars().first()
        if creator:
            creator.balance += win_sum
            
    await db.commit()
    return {"success": True, "winner": winner, "prize": win_sum}

# --- Таблица лидеров ---
@app.get("/api/leaderboard")
async def get_leaderboard(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(desc(User.balance)).limit(10))
    users = result.scalars().all()
    return [{"username": u.username or "Аноним", "balance": u.balance, "xp": u.xp, "level": u.level} for u in users]

if __name__ == "__main__":
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
