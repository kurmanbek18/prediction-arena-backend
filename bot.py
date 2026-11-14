import os
import asyncio
import random
import httpx
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import Column, Integer, String, Float, ForeignKey, select
import uvicorn

# --- Подключение к Neon PostgreSQL ---
DATABASE_URL = "postgresql+asyncpg://neondb_owner:npg_ExoNs2fyWLi1@ep-solitary-tooth-bdmpamjk-pooler.c-8.us-east-2.aws.neon.tech/neondb?sslmode=require"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

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
    telegram_id = Column(String, unique=True, index=True)
    username = Column(String, nullable=True)
    balance = Column(Float, default=1000.0)
    xp = Column(Integer, default=0)
    level = Column(String, default="Хомяк-новичок")
    language = Column(String, default="ru")
    portfolios = relationship("Portfolio", back_populates="user")

class Token(Base):
    __tablename__ = "tokens"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, unique=True, index=True)
    name = Column(String)
    price = Column(Float)
    change_24h = Column(Float, default=0.0)

class Portfolio(Base):
    __tablename__ = "portfolios"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    token_id = Column(Integer, ForeignKey("tokens.id"))
    amount = Column(Float, default=0.0)
    avg_buy_price = Column(Float, default=0.0)
    user = relationship("User", back_populates="portfolios")
    token = relationship("Token")

# --- Фоновый движок биржи ---
cached_btc_price = 65000.0

async def market_simulation_loop(session_maker):
    while True:
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=3.0)
                if res.status_code == 200:
                    global cached_btc_price
                    cached_btc_price = float(res.json()["price"])
        except:
            pass

        async with session_maker() as session:
            result = await session.execute(select(Token))
            tokens = result.scalars().all()
            for token in tokens:
                if token.symbol == "BTC":
                    token.price = cached_btc_price
                else:
                    change = random.uniform(-0.02, 0.021)
                    old = token.price
                    token.price = max(0.000001, round(token.price * (1 + change), 6))
                    token.change_24h = round(((token.price - old) / old) * 100, 2)
            await session.commit()
        await asyncio.sleep(7)

# --- FastAPI Приложение ---
app = FastAPI(title="Crypto Arena Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async with async_session_maker() as session:
        res = await session.execute(select(Token))
        if not res.scalars().all():
            initial = [
                ("USDT", "Tether", 1.0), ("TON", "Toncoin", 5.5), ("BTC", "Bitcoin", 65000.0),
                ("ETH", "Ethereum", 3500.0), ("DOGE", "Dogecoin", 0.15), ("SOL", "Solana", 140.0),
                ("NOT", "Notcoin", 0.008), ("PEPE", "Pepe", 0.00001)
            ]
            for s, n, p in initial:
                session.add(Token(symbol=s, name=n, price=p))
            await session.commit()

    asyncio.create_task(market_simulation_loop(async_session_maker))

# Пинг для Render
@app.get("/ping")
def ping():
    return {"status": "ok"}

# API: Получить или создать юзера
@app.get("/api/user/{telegram_id}")
async def get_user(telegram_id: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = res.scalar_one_or_none()
    if not user:
        user = User(telegram_id=telegram_id, username=f"User_{telegram_id[-4:]}")
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return {"telegram_id": user.telegram_id, "balance": user.balance, "xp": user.xp, "level": user.level, "language": user.language}

# API: Список монет
@app.get("/api/tokens")
async def get_tokens(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Token))
    return res.scalars().all()

# API: Таблица лидеров
@app.get("/api/leaderboard")
async def leaderboard(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(User).order_by(User.xp.desc()).limit(10))
    return [{"username": u.username, "xp": u.xp, "level": u.level} for u in res.scalars().all()]

# Раздача фронтенда (index.html)
@app.get("/")
def serve_index():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"message": "Backend is running, but index.html is missing!"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("bot:app", host="0.0.0.0", port=port)
