"""
Sneaker Drop Bishkek Ultimate — Бэкенд дропшиппинг-платформы (FastAPI + SQLAlchemy).
Включает: продвинутый каталог, размеры, корзину, оформление заказов, админ-панель, категории и защиту.
"""
import os
import re
import time
import json
import hmac
import hashlib
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import httpx
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Column, Integer, String, Float, Text, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "123456789")

CFG = dict(
    START_BALANCE=0.0,
    CURRENCY="сом"
)

raw_db_url = os.getenv("DATABASE_URL", "").strip()
if not raw_db_url:
    raw_db_url = "sqlite+aiosqlite:///./sneakers_bishkek_pro.db"
else:
    raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1).split("?")[0]
    if raw_db_url.startswith("postgresql://"):
        raw_db_url = raw_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine_kwargs = {"pool_pre_ping": True}
if "postgresql" in raw_db_url:
    engine_kwargs.update({"connect_args": {"ssl": True, "statement_cache_size": 0}, "pool_size": 5, "max_overflow": 5})

engine = create_async_engine(raw_db_url + ("?prepared_statement_cache_size=0" if "postgresql" in raw_db_url else ""), **engine_kwargs)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

# Функция получения сессии БД
async def get_db():
    async with async_session_maker() as session:
        yield session

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(String, unique=True, index=True)
    username = Column(String, default="Сникерхед")
    balance = Column(Float, default=CFG["START_BALANCE"])

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    brand = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    old_price = Column(Float, default=0)
    image = Column(String, nullable=False)
    sizes = Column(String, default="38,39,40,41,42,43,44,45")
    category = Column(String, default="drops")
    description = Column(Text, default="Лимитированный релиз с прямой доставкой в Бишкек.")

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    uid = Column(String, index=True)
    username = Column(String)
    items_summary = Column(Text)
    total_price = Column(Float)
    phone = Column(String)
    address = Column(String)
    status = Column(String, default="Ожидает подтверждения менеджер")
    created_at = Column(Float, default=time.time)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

async def auth(request: Request):
    init = request.headers.get("X-Init-Data", "")
    if BOT_TOKEN and init:
        data = dict(parse_qsl(init, keep_blank_values=True))
        h = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), h):
            raise HTTPException(401, "Ошибка авторизации Telegram")
        u = json.loads(data["user"])
        return str(u["id"]), u.get("first_name") or u.get("username")
    if init:
        try:
            u = json.loads(dict(parse_qsl(init))["user"])
            return str(u["id"]), u.get("first_name")
        except Exception:
            pass
    test = request.headers.get("X-Test-User", "guest_bishkek_pro")
    return test[:40], "Бишкек Покупатель"

async def load_user(db, uid, name=None):
    q = select(User).where(User.telegram_id == uid)
    u = (await db.execute(q)).scalars().first()
    if not u:
        u = User(telegram_id=uid, username=(name or f"User_{uid[-4:]}")[:24])
        db.add(u)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            u = (await db.execute(select(User).where(User.telegram_id == uid))).scalars().first()
    return u

@asynccontextmanager
async def lifespan(app):
    # 1. Сначала гарантированно создаем все таблицы в БД
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # 2. Затем добавляем стартовые товары, если таблица пустая
    async with async_session_maker() as session:
        count = (await session.execute(select(Product))).scalars().first()
        if not count:
            initial_products = [
                Product(title="Nike Air Jordan 4 Retro 'Bred Reimagined'", brand="Nike", price=14500, old_price=17000, image="https://images.unsplash.com/photo-1584735935682-2f2b69dff9d2?w=600", sizes="40,41,42,43,44,45", category="drops", description="Легендарная кожаная модель в классической расцветке."),
                Product(title="Adidas Yeezy Boost 350 V2 'Onyx'", brand="Adidas", price=12000, old_price=14000, image="https://images.unsplash.com/photo-1608231387042-66d1773070a5?w=600", sizes="41,42,43,44", category="lifestyle", description="Максимальный комфорт с технологией Primeknit и Boost."),
                Product(title="New Balance 9060 'Sea Salt'", brand="New Balance", price=13500, old_price=15500, image="https://images.unsplash.com/photo-1539185441755-769473a23570?w=600", sizes="38,39,40,41,42,43", category="lifestyle", description="Футуристичный дизайн и премиальные материалы."),
                Product(title="Nike Dunk Low 'Panda'", brand="Nike", price=10500, old_price=12500, image="https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?w=600", sizes="39,40,41,42,43,44", category="drops", description="Самая универсальная и узнаваемая расцветка.")
            ]
            session.add_all(initial_products)
            await session.commit()
    yield

app = FastAPI(title="Sneaker Drop Bishkek Ultimate", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/api/me")
async def api_me(db: AsyncSession = Depends(get_db), who=Depends(auth)):
    u = await load_user(db, who[0], who[1])
    await db.commit()
    is_admin = (u.telegram_id == ADMIN_TELEGRAM_ID or u.id == 1)
    return {"id": u.telegram_id, "username": u.username, "balance": u.balance, "is_admin": is_admin}

@app.get("/api/products")
async def get_products(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Product).order_by(Product.id.desc()))).scalars().all()
    return [{"id": p.id, "title": p.title, "brand": p.brand, "price": p.price, "old_price": p.old_price, "image": p.image, "sizes": p.sizes.split(","), "category": p.category, "description": p.description} for p in rows]

@app.post("/api/admin/add_product")
async def add_product(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    u = await load_user(db, who[0], who[1])
    if u.telegram_id != ADMIN_TELEGRAM_ID and u.id != 1:
        raise HTTPException(403, "Доступ запрещен")
    p = Product(
        title=str(d.get("title", "")).strip(),
        brand=str(d.get("brand", "")).strip(),
        price=float(d.get("price", 0)),
        old_price=float(d.get("old_price", 0)),
        image=str(d.get("image", "")).strip(),
        sizes=str(d.get("sizes", "40,41,42,43,44")).strip(),
        category=str(d.get("category", "drops")).strip(),
        description=str(d.get("description", "Новое поступление.")).strip()
    )
    db.add(p)
    await db.commit()
    return {"success": True, "message": "Дроп успешно опубликован!"}

@app.post("/api/orders/create")
async def create_order(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    u = await load_user(db, who[0], who[1])
    phone = str(d.get("phone", "")).strip()
    address = str(d.get("address", "")).strip()
    items = d.get("items", [])
    if not phone or not address or not items:
        raise HTTPException(400, "Заполните телефон, адрес в Бишкеке и выберите товары")
    
    summary = ", ".join([f"{i['title']} [размер: {i['size']}, кол-во: {i['qty']}]" for i in items])
    total = sum([i['price'] * i['qty'] for i in items])
    
    order = Order(uid=u.telegram_id, username=u.username, items_summary=summary, total_price=total, phone=phone, address=address)
    db.add(order)
    await db.commit()
    return {"success": True, "message": "Заказ успешно создан! Менеджер свяжется с вами для подтверждения."}

@app.get("/api/admin/orders")
async def get_orders(db: AsyncSession = Depends(get_db), who=Depends(auth)):
    u = await load_user(db, who[0], who[1])
    if u.telegram_id != ADMIN_TELEGRAM_ID and u.id != 1:
        raise HTTPException(403, "Доступ запрещен")
    rows = (await db.execute(select(Order).order_by(Order.id.desc()))).scalars().all()
    return [{"id": o.id, "username": o.username, "summary": o.items_summary, "total": o.total_price, "phone": o.phone, "address": o.address, "status": o.status} for o in rows]

if __name__ == "__main__":
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
