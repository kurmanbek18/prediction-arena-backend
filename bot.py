"""
STYLE HUB — Backend v7 (FastAPI + SQLAlchemy + PostgreSQL/SQLite + Telegram Bot).
Генерирует 900 популярных моделей обуви и кроссовок в КР/Бишкеке с логикой «Узнать цену»,
поддерживает редактирование/добавление/удаление товаров, массовое добавление и заявки под запрос.
"""
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, quote

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import Boolean, Column, Float, Integer, LargeBinary, String, Text, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("style_hub")
BASE = Path(__file__).parent

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    x.strip()
    for x in os.getenv("ADMIN_IDS", os.getenv("ADMIN_TELEGRAM_ID", "")).split(",")
    if x.strip()
}
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
APP_LINK = os.getenv("APP_LINK", "").strip().rstrip("/")
HOOK_SECRET = hashlib.sha256(("hook" + BOT_TOKEN).encode()).hexdigest()[:32]

DEFAULTS = {
    "shop_name": "STYLE HUB",
    "whatsapp": "",
    "pay_text": "Цену уточняем перед подтверждением заказа. Оплата после согласования модели, размера и стоимости.",
    "delivery_text": "Подбираем нужную модель в Бишкеке и по КР. Оставьте заявку — найдём вариант, уточним цену и свяжемся с вами.",
    "qr": "",
}

STATUSES = {
    "new": "🔎 Новый запрос",
    "searching": "🕵️ Ищем вариант",
    "quoted": "💬 Цена отправлена",
    "confirmed": "✅ Подтверждён",
    "paid": "💰 Оплачен",
    "shipped": "🚚 Отправлен",
    "done": "🎉 Доставлен",
    "cancel": "❌ Отменён",
}

CLIENT_MSG = {
    "searching": "🕵️ По запросу #{id} начали поиск пары и уточняем наличие.",
    "quoted": "💬 По запросу #{id} нашли вариант и цену — менеджер скоро свяжется с вами.",
    "confirmed": "✅ Заказ #{id} подтверждён! Менеджер напишет вам по оплате и доставке.",
    "paid": "💰 Оплата по заказу #{id} получена. Спасибо, что выбрали STYLE HUB!",
    "shipped": "🚚 Заказ #{id} отправлен. Ожидайте доставку.",
    "done": "🎉 Заказ #{id} доставлен. Спасибо за обращение в STYLE HUB!",
    "cancel": "❌ Заказ #{id} отменён. Напишите нам, если нужно подобрать другую пару.",
}

url = os.getenv("DATABASE_URL", "").strip()
if not url:
    log.warning("DATABASE_URL не задан — используется локальная SQLite БД (style_hub.db)")
    url = "sqlite+aiosqlite:///./style_hub.db"
else:
    url = url.split("?")[0].replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

kw = {"pool_pre_ping": True}
if "asyncpg" in url:
    kw.update(
        connect_args={"ssl": True, "statement_cache_size": 0},
        pool_size=5,
        max_overflow=5,
        pool_recycle=300,
    )

engine = create_async_engine(url, **kw)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


async def get_db():
    async with Session() as s:
        yield s


class Product(Base):
    __tablename__ = "shop_products"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    brand = Column(String, default="")
    price = Column(Float, default=0.0)
    old_price = Column(Float, default=0.0)
    image = Column(String, nullable=False)
    images = Column(Text, default="[]")
    badge = Column(String, default="")
    sizes = Column(String, default="39,40,41,42,43,44,45")
    category = Column(String, default="")
    description = Column(Text, default="")
    active = Column(Boolean, default=True)
    created = Column(Float, default=lambda: time.time())


class Category(Base):
    __tablename__ = "shop_categories"
    slug = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    pos = Column(Integer, default=0)


class Image(Base):
    __tablename__ = "shop_images"
    id = Column(Integer, primary_key=True)
    mime = Column(String)
    data = Column(LargeBinary)


class Order(Base):
    __tablename__ = "shop_orders"
    id = Column(Integer, primary_key=True)
    uid = Column(String, index=True)
    username = Column(String)
    items = Column(Text)
    total = Column(Float, default=0.0)
    phone = Column(String)
    address = Column(String)
    comment = Column(String, default="")
    status = Column(String, default="new")
    created = Column(Float, default=lambda: time.time())


class Setting(Base):
    __tablename__ = "shop_settings"
    key = Column(String, primary_key=True)
    value = Column(Text, default="")


def clean(v, n):
    return str(v or "").strip()[:n]


def esc(v):
    return html.escape(str(v or ""))


def wa_phone(phone):
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 10 and d.startswith("0"):
        return "996" + d[1:]
    if len(d) == 9:
        return "996" + d
    return d if len(d) >= 11 else ""


def valid_img(u):
    return isinstance(u, str) and (u.startswith("/img/") or u.startswith("https://")) and len(u) < 500


async def get_settings(db):
    s = dict(DEFAULTS)
    for r in (await db.execute(select(Setting))).scalars():
        s[r.key] = r.value
    if not s.get("shop_name"):
        s["shop_name"] = "STYLE HUB"
    return s


async def get_cats(db):
    rows = (await db.execute(select(Category).order_by(Category.pos, Category.slug))).scalars()
    return [{"slug": c.slug, "name": c.name} for c in rows]


def prod_dict(p):
    try:
        imgs = [i for i in json.loads(p.images or "[]") if valid_img(i)]
    except Exception:
        imgs = []
    if not imgs and p.image:
        imgs = [p.image]
    price_val = float(p.price or 0)
    old_val = float(p.old_price or 0)
    on_request = price_val <= 0
    return {
        "id": p.id,
        "title": p.title,
        "brand": p.brand or "",
        "price": price_val,
        "price_label": "Узнать цену" if on_request else f"{price_val:,.0f} сом",
        "on_request": on_request,
        "old_price": old_val,
        "image": imgs[0] if imgs else "",
        "images": imgs,
        "badge": p.badge or ("Узнать цену" if on_request else ""),
        "sizes": [x for x in (p.sizes or "").split(",") if x],
        "category": p.category or "",
        "description": p.description or "",
        "active": bool(p.active),
        "created": p.created,
    }


def order_dict(o):
    try:
        parsed_items = json.loads(o.items or "[]")
    except Exception:
        parsed_items = []
    return {
        "id": o.id,
        "username": o.username,
        "items": parsed_items,
        "total": float(o.total or 0),
        "on_request": float(o.total or 0) <= 0,
        "phone": o.phone,
        "address": o.address,
        "comment": o.comment,
        "status": o.status,
        "created": o.created,
        "wa": wa_phone(o.phone),
    }


async def auth(request: Request):
    init = request.headers.get("X-Init-Data", "")
    if not BOT_TOKEN or not init:
        raise HTTPException(401, "Откройте магазин через Telegram")
    data = dict(parse_qsl(init, keep_blank_values=True))
    got = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
        raise HTTPException(401, "Ошибка авторизации Telegram")
    try:
        old = time.time() - int(data.get("auth_date", 0)) > 86400
        u = json.loads(data["user"])
    except Exception:
        raise HTTPException(401, "Ошибка авторизации Telegram")
    if old:
        raise HTTPException(401, "Сессия устарела, откройте магазин заново")
    return {"id": str(u["id"]), "name": u.get("first_name") or u.get("username") or "Клиент"}


async def admin(who=Depends(auth)):
    if who["id"] not in ADMIN_IDS:
        raise HTTPException(403, "Доступ запрещён")
    return who


async def tg(method, **payload):
    if not BOT_TOKEN:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload)
            return r.json()
    except Exception as e:
        log.warning("tg %s failed: %s", method, e)


def order_text(o):
    try:
        items = json.loads(o.items or "[]")
    except Exception:
        items = []
    lines = "\n".join(f"• {esc(i['title'])} — р.{esc(i['size'])} × {i['qty']}" for i in items)
    total_str = "Узнать цену по запросу" if (o.total or 0) <= 0 else f"{o.total:,.0f} сом"
    summary_for_search = " | ".join(f"{i['title']} (р.{i['size']} ×{i['qty']})" for i in items)
    t = (
        f"🔎 <b>Заявка #{o.id} · STYLE HUB</b> · {STATUSES.get(o.status, o.status)}\n\n"
        f"{lines}\n\n"
        f"💰 Цена: <b>{total_str}</b>\n"
        f"👤 Клиент: {esc(o.username)}\n"
        f"📞 Телефон: {esc(o.phone)}\n"
        f"📍 Город / адрес: {esc(o.address)}\n"
        f"📦 Поиск: <code>{esc(summary_for_search)}</code>"
    )
    return t + (f"\n💬 Комментарий: {esc(o.comment)}" if o.comment else "")


def order_kb(o):
    b = lambda t, s: {"text": t, "callback_data": f"st:{o.id}:{s}"}
    rows = [
        [b("🕵️ Ищем", "searching"), b("💬 Цена дана", "quoted")],
        [b("✅ Подтвердить", "confirmed"), b("💰 Оплачен", "paid")],
        [b("🚚 Отправлен", "shipped"), b("🎉 Доставлен", "done")],
        [b("❌ Отменить", "cancel")],
    ]
    w = wa_phone(o.phone)
    if w:
        try:
            items = json.loads(o.items or "[]")
        except Exception:
            items = []
        msg_preview = ", ".join(f"{i['title']} (р.{i['size']})" for i in items)
        wa_text = quote(f"Здравствуйте! По заявке #{o.id} в STYLE HUB: {msg_preview}. Уточняем наличие и цену.")
        rows.append([{"text": "💬 Написать клиенту в WhatsApp", "url": f"https://wa.me/{w}?text={wa_text}"}])
    return {"inline_keyboard": rows}


async def notify_admins(o):
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст — уведомления некому отправлять")
    for aid in ADMIN_IDS:
        await tg(
            "sendMessage",
            chat_id=aid,
            text="🔔 Новый запрос в STYLE HUB!\n\n" + order_text(o),
            parse_mode="HTML",
            reply_markup=order_kb(o),
        )


async def apply_status(db, order_id, status):
    o = await db.get(Order, order_id)
    if not o or status not in STATUSES:
        return None
    o.status = status
    await db.commit()
    if status in CLIENT_MSG:
        await tg("sendMessage", chat_id=o.uid, text=CLIENT_MSG[status].format(id=o.id))
    return o


async def on_callback(cq):
    if str(cq["from"]["id"]) not in ADMIN_IDS:
        return await tg("answerCallbackQuery", callback_query_id=cq["id"], text="Нет доступа", show_alert=True)
    try:
        _, oid, st = cq["data"].split(":")
        async with Session() as db:
            o = await apply_status(db, int(oid), st)
    except Exception:
        return await tg("answerCallbackQuery", callback_query_id=cq["id"], text="Ошибка")
    if not o:
        return await tg("answerCallbackQuery", callback_query_id=cq["id"], text="Заявка не найдена")
    await tg("answerCallbackQuery", callback_query_id=cq["id"], text=STATUSES[st])
    m = cq.get("message") or {}
    await tg(
        "editMessageText",
        chat_id=m["chat"]["id"],
        message_id=m["message_id"],
        text=order_text(o),
        parse_mode="HTML",
        reply_markup=order_kb(o),
    )


async def on_message(m):
    t, chat = (m.get("text") or "").strip(), m["chat"]["id"]
    if t.startswith("/id"):
        return await tg("sendMessage", chat_id=chat, text=f"Ваш Telegram ID: {m['from']['id']}")
    if t.startswith("/start"):
        kb = {"inline_keyboard": [[{"text": "👟 Открыть STYLE HUB", "web_app": {"url": PUBLIC_URL}}]]} if PUBLIC_URL else None
        await tg(
            "sendMessage",
            chat_id=chat,
            reply_markup=kb,
            text=(
                "Добро пожаловать в <b>STYLE HUB</b> (Кыргызстан / Бишкек)!\n"
                "Кроссовки, спортивная обувь, классика и повседневные пары.\n"
                "Нажмите «Узнать цену» на любой модели — мы уточним наличие, стоимость и свяжемся с вами."
            ),
            parse_mode="HTML",
        )


SEED_CATS = [
    ("sneakers", "Кроссовки", 1),
    ("sport", "Спорт и зал", 2),
    ("classic", "Классика и туфли", 3),
    ("everyday", "Повседневные", 4),
]

PHOTO_POOL = [
    "https://images.unsplash.com/photo-1584735935682-2f2b69dff9d2?w=800",
    "https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?w=800",
    "https://images.unsplash.com/photo-1549298916-b41d501d3772?w=800",
    "https://images.unsplash.com/photo-1539185441755-769473a23570?w=800",
    "https://images.unsplash.com/photo-1551107696-a4b0c5a0d9a2?w=800",
    "https://images.unsplash.com/photo-1608231387042-66d1773070a5?w=800",
    "https://images.unsplash.com/photo-1514989940723-e8e51635b782?w=800",
    "https://images.unsplash.com/photo-1600185365926-3a2ce3cdb9eb?w=800",
    "https://images.unsplash.com/photo-1525966222134-fcfa99b8ae77?w=800",
    "https://images.unsplash.com/photo-1560769629-975ec94e6a86?w=800",
    "https://images.unsplash.com/photo-1560343090-f0409e92791a?w=800",
    "https://images.unsplash.com/photo-1614252235316-8c857d38b5f4?w=800",
]

BASE_TEMPLATES = [
    ("Mizuno Wave Momentum", "Mizuno · Япония", "sport", "39,40,41,42,43,44,45", "Спортивная модель с эмблемой Runbird для зала, волейбола и тренировок."),
    ("Mizuno Wave Lightning", "Mizuno · Япония", "sport", "39,40,41,42,43,44,45", "Лёгкая игровая пара для волейбола и зала. Цену уточняем по запросу."),
    ("Loro Piana Minimalist Walk", "Loro Piana · Италия", "classic", "39,40,41,42,43,44", "Лаконичная модель без крупных внешних логотипов под классику и повседневный стиль."),
    ("Gucci Screener / Ace", "Gucci · Италия", "classic", "38,39,40,41,42,43,44", "Популярная модель в классическом и повседневном стиле. Уточним наличие и цену."),
    ("Guess 1981 Кеды", "Guess · США", "everyday", "38,39,40,41,42,43,44", "Базовые кеды с фирменным знаком 1981 для города, джинсов и повседневной носки."),
    ("Louis Vuitton Run Away", "Louis Vuitton · Франция", "sneakers", "39,40,41,42,43,44", "Спортивно-классические кроссовки с фирменными деталями. Уточним размер и цену."),
    ("Nike Court Royale 2 Low", "Nike · США", "classic", "38,39,40,41,42,43,44", "Классические низкие кеды с белым Swoosh для повседневного образа."),
    ("Nike Air Max 90", "Nike · США", "sneakers", "39,40,41,42,43,44,45", "Культовая модель с воздушной вставкой Air в пятке и логотипом Swoosh."),
    ("Nike Invincible Run / React", "Nike · США", "sport", "39,40,41,42,43,44,45", "Мягкая беговая и тренировочная модель с амортизацией и серебристым Swoosh."),
    ("Nike Zoom HyperAce / Зал", "Nike · США", "sport", "39,40,41,42,43,44,45", "Игровая модель для зала с хорошей фиксацией стопы для волейбола и баскетбола."),
    ("Nike Dunk Low Retro", "Nike · США", "sneakers", "37,38,39,40,41,42,43,44", "Востребованная в КР модель кроссовок на каждый день. Уточним наличие и цену."),
    ("Nike Air Force 1 '07", "Nike · США", "sneakers", "37,38,39,40,41,42,43,44,45", "Базовая модель кроссовок для города. Подберём размер и сообщим цену."),
    ("Nike Air Jordan 4 Retro", "Jordan · США", "sneakers", "39,40,41,42,43,44,45", "Популярная линейка кроссовок в Бишкеке. Уточним цену и доступные размеры."),
    ("New Balance 9060", "New Balance · США", "sneakers", "38,39,40,41,42,43,44", "Объёмный силуэт и комфорт на каждый день. Найдём пару и уточним стоимость."),
    ("New Balance 530", "New Balance · США", "everyday", "37,38,39,40,41,42,43,44", "Лёгкая модель на каждый день. Уточним наличие нужного размера и цену."),
    ("Adidas Yeezy Boost 350 V2", "Adidas · Германия", "sneakers", "39,40,41,42,43,44,45", "Популярная модель для повседневной носки. Цену уточняем по запросу."),
    ("Классические чёрные полуботинки", "Classic Style", "classic", "39,40,41,42,43,44", "Аккуратные чёрные полуботинки без броских логотипов под классику и офис."),
    ("Повседневные кроссовки Basic", "STYLE HUB · База", "everyday", "37,38,39,40,41,42,43,44", "Универсальная повседневная модель. Подберём вариант и сообщим цену."),
]

COLORWAYS = [
    "White / Black", "Triple Black", "Triple White", "Grey / White", "Navy / White",
    "Red / Black", "Beige / Cream", "Olive / Black", "Blue / White", "Sand / Brown",
]


def build_900_catalog(count: int = 900):
    items = []
    for idx in range(count):
        base_title, brand, cat, sizes, desc = BASE_TEMPLATES[idx % len(BASE_TEMPLATES)]
        cycle_num = (idx // len(BASE_TEMPLATES)) + 1
        color = COLORWAYS[(idx // len(BASE_TEMPLATES)) % len(COLORWAYS)]
        title = f"{base_title} · {color}" if cycle_num > 1 else base_title
        img = PHOTO_POOL[idx % len(PHOTO_POOL)]
        items.append(
            Product(
                title=clean(title, 120),
                brand=clean(brand, 60),
                price=0.0,
                old_price=0.0,
                image=img,
                images=json.dumps([img]),
                sizes=sizes,
                category=cat,
                description=clean(f"{desc} Наличие и цену уточняем под запрос в Бишкеке / КР.", 1500),
                badge="Узнать цену",
                active=True,
            )
        )
    return items


@asynccontextmanager
async def lifespan(app):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    for col, ddl in (("images", "TEXT DEFAULT '[]'"), ("badge", "VARCHAR DEFAULT ''")):
        try:
            async with engine.begin() as conn:
                ine = "IF NOT EXISTS " if "asyncpg" in url else ""
                await conn.execute(text(f"ALTER TABLE shop_products ADD COLUMN {ine}{col} {ddl}"))
        except Exception:
            pass
    async with Session() as s:
        if not (await s.execute(select(Category.slug).limit(1))).first():
            s.add_all([Category(slug=a, name=b, pos=c) for a, b, c in SEED_CATS])
        if not (await s.execute(select(Product.id).limit(1))).first():
            s.add_all(build_900_catalog(900))
        await s.commit()

    if BOT_TOKEN and PUBLIC_URL:
        await tg("setWebhook", url=f"{PUBLIC_URL}/tg/webhook/{HOOK_SECRET}", allowed_updates=["message", "callback_query"])
        await tg("setChatMenuButton", menu_button={"type": "web_app", "text": "STYLE HUB", "web_app": {"url": PUBLIC_URL}})
    else:
        log.warning("BOT_TOKEN или PUBLIC_URL не заданы — бот и уведомления выключены")
    yield


app = FastAPI(title="STYLE HUB — Бишкек", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
async def index():
    return FileResponse(BASE / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/img/{iid}")
async def get_img(iid: int, db: AsyncSession = Depends(get_db)):
    im = await db.get(Image, iid)
    if not im:
        raise HTTPException(404)
    return Response(im.data, media_type=im.mime, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/init")
async def api_init(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        who = await auth(request)
    except HTTPException:
        who = None
    rows = (await db.execute(select(Product).where(Product.active == True).order_by(Product.id.desc()))).scalars().all()
    cfg = await get_settings(db)
    cfg["app_link"] = APP_LINK
    return {
        "cfg": cfg,
        "cats": await get_cats(db),
        "products": [prod_dict(p) for p in rows],
        "authed": bool(who),
        "is_admin": bool(who and who["id"] in ADMIN_IDS),
    }


@app.post("/api/orders")
async def create_order(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    phone, address, comment = clean(d.get("phone"), 32), clean(d.get("address"), 200), clean(d.get("comment"), 500)
    if len(re.sub(r"\D", "", phone)) < 9:
        raise HTTPException(400, "Укажите корректный номер телефона")
    if len(address) < 2:
        raise HTTPException(400, "Укажите город или район")
    items = d.get("items")
    if not isinstance(items, list) or not items or len(items) > 20:
        raise HTTPException(400, "Выберите хотя бы одну модель")
    try:
        ids = {int(i["id"]) for i in items}
        prods = {
            p.id: p
            for p in (await db.execute(select(Product).where(Product.id.in_(ids), Product.active == True))).scalars()
        }
        lines, total = [], 0.0
        for i in items:
            p, size, qty = prods.get(int(i["id"])), str(i.get("size") or "42"), int(i.get("qty") or 1)
            allowed_sizes = [x for x in (p.sizes or "").split(",") if x] if p else []
            if not p or (allowed_sizes and size not in allowed_sizes) or not 1 <= qty <= 10:
                raise ValueError
            p_price = float(p.price or 0)
            lines.append(
                {
                    "id": p.id,
                    "title": p.title,
                    "size": size,
                    "qty": qty,
                    "price": p_price,
                    "on_request": p_price <= 0,
                    "image": prod_dict(p)["image"],
                }
            )
            total += p_price * qty
    except Exception:
        raise HTTPException(400, "Одна из моделей недоступна. Обновите страницу и повторите")

    o = Order(
        uid=who["id"],
        username=who["name"],
        items=json.dumps(lines, ensure_ascii=False),
        total=total,
        phone=phone,
        address=address,
        comment=comment,
        status="new",
    )
    db.add(o)
    await db.commit()
    await notify_admins(o)
    return {"id": o.id, "total": total, "on_request": total <= 0}


@app.get("/api/my_orders")
async def my_orders(db: AsyncSession = Depends(get_db), who=Depends(auth)):
    rows = (
        await db.execute(select(Order).where(Order.uid == who["id"]).order_by(Order.id.desc()).limit(30))
    ).scalars()
    return [order_dict(o) for o in rows]


@app.get("/api/admin/orders")
async def admin_orders(db: AsyncSession = Depends(get_db), _=Depends(admin)):
    rows = (await db.execute(select(Order).order_by(Order.id.desc()).limit(150))).scalars()
    return [order_dict(o) for o in rows]


@app.post("/api/admin/orders/{oid}/status")
async def admin_status(oid: int, d: dict, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    o = await apply_status(db, oid, d.get("status"))
    if not o:
        raise HTTPException(404, "Заявка не найдена")
    return order_dict(o)


@app.get("/api/admin/products")
async def admin_products(db: AsyncSession = Depends(get_db), _=Depends(admin)):
    rows = (await db.execute(select(Product).order_by(Product.id.desc()).limit(300))).scalars()
    return [prod_dict(p) for p in rows]


@app.post("/api/admin/products")
async def admin_save_product(d: dict, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    raw_price = d.get("price")
    raw_old = d.get("old_price")
    try:
        price = 0.0 if raw_price in (None, "", "null") else float(raw_price)
        old = 0.0 if raw_old in (None, "", "null") else float(raw_old)
    except Exception:
        raise HTTPException(400, "Цена должна быть числом или пустой")
    if price < 0 or old < 0:
        raise HTTPException(400, "Цена не может быть отрицательной")

    title = clean(d.get("title"), 120)
    sizes = ",".join(x.strip() for x in str(d.get("sizes", "39,40,41,42,43,44")).split(",") if x.strip())
    imgs = d.get("images") if isinstance(d.get("images"), list) else []
    imgs = [i for i in imgs if valid_img(i)][:12]
    if not title or not sizes:
        raise HTTPException(400, "Заполните название и размеры")
    if not imgs:
        raise HTTPException(400, "Добавьте хотя бы одно фото")

    if d.get("id"):
        p = await db.get(Product, int(d["id"]))
        if not p:
            raise HTTPException(404, "Модель не найдена")
    else:
        p = Product()
        db.add(p)

    p.title = title
    p.brand = clean(d.get("brand"), 60)
    p.price = price
    p.old_price = old
    p.sizes = sizes
    p.image = imgs[0]
    p.images = json.dumps(imgs)
    badge_clean = clean(d.get("badge"), 20)
    p.badge = badge_clean if badge_clean else ("Узнать цену" if price <= 0 else "")
    p.category = clean(d.get("category"), 40)
    p.description = clean(d.get("description"), 1500)
    p.active = bool(d.get("active", True))
    await db.commit()
    return prod_dict(p)


@app.post("/api/admin/products/bulk")
async def admin_bulk_products(d: dict, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    """Массовое добавление моделей строками: Название | Бренд | Категория | URL_фото | Размеры."""
    added = 0
    raw_text = str(d.get("lines") or "")
    default_img = PHOTO_POOL[0]
    for idx, line in enumerate(raw_text.splitlines()[:300]):
        parts = [x.strip() for x in line.split("|")]
        if not parts or not parts[0]:
            continue
        title = clean(parts[0], 120)
        brand = clean(parts[1] if len(parts) > 1 else "", 60)
        cat = clean(parts[2] if len(parts) > 2 else "sneakers", 40)
        img = parts[3] if len(parts) > 3 and valid_img(parts[3]) else PHOTO_POOL[idx % len(PHOTO_POOL)]
        sizes = clean(parts[4] if len(parts) > 4 and parts[4] else "39,40,41,42,43,44", 120)
        db.add(
            Product(
                title=title,
                brand=brand,
                price=0.0,
                old_price=0.0,
                image=img or default_img,
                images=json.dumps([img or default_img]),
                sizes=sizes,
                category=cat,
                description="Подбор под запрос в Бишкеке / КР. Наличие и цену уточняем.",
                badge="Узнать цену",
                active=True,
            )
        )
        added += 1
    await db.commit()
    return {"added": added}


@app.delete("/api/admin/products/{pid}")
async def admin_delete_product(pid: int, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    await db.execute(delete(Product).where(Product.id == pid))
    await db.commit()
    return {"ok": True}


@app.post("/api/admin/categories")
async def admin_save_cat(d: dict, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    name = clean(d.get("name"), 30)
    if not name:
        raise HTTPException(400, "Введите название категории")
    if d.get("slug"):
        c = await db.get(Category, str(d["slug"]))
        if not c:
            raise HTTPException(404, "Категория не найдена")
        c.name = name
    else:
        db.add(Category(slug=f"c{int(time.time() * 1000)}", name=name, pos=int(time.time()) % 1_000_000))
    await db.commit()
    return await get_cats(db)


@app.delete("/api/admin/categories/{slug}")
async def admin_delete_cat(slug: str, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    await db.execute(delete(Category).where(Category.slug == slug))
    await db.execute(update(Product).where(Product.category == slug).values(category=""))
    await db.commit()
    return await get_cats(db)


@app.post("/api/admin/upload")
async def admin_upload(d: dict, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    m = re.match(r"data:(image/(?:jpeg|png|webp));base64,(.+)$", str(d.get("data", "")), re.S)
    if not m:
        raise HTTPException(400, "Неверный формат изображения")
    raw = base64.b64decode(m.group(2))
    if len(raw) > 2_000_000:
        raise HTTPException(400, "Фото слишком большое")
    im = Image(mime=m.group(1), data=raw)
    db.add(im)
    await db.commit()
    return {"url": f"/img/{im.id}"}


@app.post("/api/admin/settings")
async def admin_settings(d: dict, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    for k in DEFAULTS:
        if k not in d:
            continue
        v = clean(d[k], 1000)
        if k == "whatsapp":
            v = re.sub(r"\D", "", v)
        row = await db.get(Setting, k)
        if row:
            row.value = v
        else:
            db.add(Setting(key=k, value=v))
    await db.commit()
    s = await get_settings(db)
    s["app_link"] = APP_LINK
    return s


@app.post("/tg/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if not hmac.compare_digest(secret, HOOK_SECRET):
        raise HTTPException(403)
    u = await request.json()
    try:
        if "callback_query" in u:
            await on_callback(u["callback_query"])
        elif "message" in u:
            await on_message(u["message"])
    except Exception:
        log.exception("webhook error")
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
