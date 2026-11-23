"""
STYLE HUB — Backend v8.4 (FastAPI + SQLAlchemy async + PostgreSQL/Neon или SQLite + Telegram Bot).
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
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    case,
    delete,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
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
AUTH_MAX_AGE = 72 * 3600

DEFAULTS = {
    "shop_name": "STYLE HUB",
    "whatsapp": "",
    "pay_text": "Цену называем до оплаты. Оплата после того, как согласуем модель, размер и наличие.",
    "delivery_text": "Работаем в Бишкеке и по КР: проверяем наличие у поставщиков и связываемся с вами в Telegram или WhatsApp.",
    "qr": "",
}

STATUSES = {
    "new": "🆕 Новая заявка",
    "searching": "🔎 Проверяем наличие",
    "quoted": "💬 Цена отправлена",
    "confirmed": "✅ Подтверждён",
    "paid": "💰 Оплачен",
    "shipped": "🚚 Отправлен",
    "done": "🎉 Доставлен",
    "cancel": "❌ Отменён",
}

CLIENT_MSG = {
    "searching": "🔎 По заявке #{id} проверяем наличие и размер у поставщиков.",
    "quoted": "💬 По заявке #{id} уточнили вариант и цену — менеджер скоро напишет вам.",
    "confirmed": "✅ Заказ #{id} подтверждён! Менеджер свяжется с вами по оплате и доставке.",
    "paid": "💰 Оплата по заказу #{id} получена. Спасибо, что выбрали STYLE HUB!",
    "shipped": "🚚 Заказ #{id} отправлен. Ожидайте доставку.",
    "done": "🎉 Заказ #{id} доставлен. Спасибо за покупку в STYLE HUB!",
    "cancel": "❌ Заказ #{id} отменён. Напишите нам, если нужно подобрать другую пару.",
}

CATEGORY_FALLBACK_IMG = {
    "sneakers": "https://images.unsplash.com/photo-1549298916-b41d501d3772?w=800",
    "sport": "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=800",
    "classic": "https://images.unsplash.com/photo-1614252235316-8c857d38b5f4?w=800",
    "everyday": "https://images.unsplash.com/photo-1600185365483-26d7a4cc7519?w=800",
}

url = os.getenv("DATABASE_URL", "").strip()
if not url:
    log.warning("DATABASE_URL не задан — используется локальная SQLite БД (style_hub.db)")
    url = "sqlite+aiosqlite:///./style_hub.db"
else:
    url = url.split("?")[0].replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

IS_PG = "asyncpg" in url
kw = {"pool_pre_ping": True}
if IS_PG:
    kw.update(
        connect_args={"ssl": True, "statement_cache_size": 0, "timeout": 20},
        pool_size=5,
        max_overflow=5,
        pool_recycle=240,
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
    sku = Column(String, index=True, default="")
    title = Column(String, nullable=False)
    brand = Column(String, default="")
    color = Column(String, default="")
    tags = Column(String, default="")
    price = Column(Float, default=0.0)
    old_price = Column(Float, default=0.0)
    image = Column(String, default="")
    images = Column(Text, default="[]")
    badge = Column(String, default="")
    sizes = Column(String, default="39,40,41,42,43,44,45")
    category = Column(String, default="", index=True)
    description = Column(Text, default="")
    search = Column(Text, default="")
    active = Column(Boolean, default=True, index=True)
    is_top = Column(Boolean, default=False, index=True)
    top_pos = Column(Integer, default=0)
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
    req_id = Column(String, default="", index=True)
    created = Column(Float, default=lambda: time.time())


class Setting(Base):
    __tablename__ = "shop_settings"
    key = Column(String, primary_key=True)
    value = Column(Text, default="")


class Favorite(Base):
    __tablename__ = "shop_favorites"
    id = Column(Integer, primary_key=True)
    uid = Column(String, index=True, nullable=False)
    pid = Column(Integer, index=True, nullable=False)
    created = Column(Float, default=lambda: time.time())
    __table_args__ = (UniqueConstraint("uid", "pid", name="uq_shop_fav"),)


def clean(v, n):
    return str(v or "").strip()[:n]


def esc(v):
    return html.escape(str(v or ""))


def esc_like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def norm(s):
    return re.sub(r"\s+", " ", str(s or "").lower().replace("ё", "е")).strip()


def wa_phone(phone):
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 10 and d.startswith("0"):
        return "996" + d[1:]
    if len(d) == 9:
        return "996" + d
    return d if len(d) >= 11 else ""


def valid_img(u):
    return isinstance(u, str) and (u.startswith("/img/") or u.startswith("https://")) and len(u) < 500


def to_num(v, default=0.0):
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        raise ValueError("bool")
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[\s\u00a0]", "", str(v))
    s = re.sub(r"(сом|som|kgs|c)$", "", s, flags=re.I)
    if not s:
        return default
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if re.fullmatch(r"\d+,\d{1,2}", s) else s.replace(",", "")
    return float(s)


def parse_sizes(v):
    raw = ",".join(map(str, v)) if isinstance(v, list) else str(v or "")
    out = []
    for part in re.split(r"[,;/\s]+", raw.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d{2})-(\d{2})", part)
        if m and int(m[1]) <= int(m[2]) <= 60:
            out += [str(i) for i in range(int(m[1]), int(m[2]) + 1)]
        else:
            out.append(part[:6])
    uniq = []
    for x in out:
        if x not in uniq:
            uniq.append(x)
    return ",".join(uniq[:40])


def parse_images(v):
    if isinstance(v, list):
        parts = [str(x).strip() for x in v]
    else:
        parts = [x.strip() for x in re.split(r"[\n\r|;\s]+", str(v or ""))]
    out = []
    for u in parts:
        if valid_img(u) and u not in out:
            out.append(u)
    return out[:12]


def to_bool(v, default=False):
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "да", "+", "on", "x")


BRAND_ALIASES = {
    "nike": "найк", "adidas": "адидас", "new balance": "нью баланс ньюбаланс", "mizuno": "мизуно",
    "gucci": "гуччи", "jordan": "джордан", "puma": "пума", "reebok": "рибок", "asics": "асикс",
    "vans": "вансы ванс", "converse": "конверс", "guess": "гесс гэс", "louis vuitton": "луи виттон",
    "loro piana": "лоро пиана", "yeezy": "изи йизи", "balenciaga": "баленсиага", "fila": "фила",
    "under armour": "андер армор", "skechers": "скетчерс", "salomon": "саломон", "timberland": "тимберленд",
    "li-ning": "ли нинг лининг",
}


def make_search(title, brand, color, tags, cat_name, sku, sizes_csv):
    base = norm(" ".join(str(x or "") for x in (title, brand, color, tags, cat_name, sku)))
    alias = " ".join(ru for en, ru in BRAND_ALIASES.items() if en in base)
    sizes = " ".join(f"size{x}" for x in (sizes_csv or "").split(",") if x)
    return (base + " " + alias + " " + sizes).strip()[:1800] or "-"


def brand_name(b):
    return (b or "").split("·")[0].strip()


def money(v):
    return f"{float(v):,.0f}".replace(",", " ")


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
    if not imgs and valid_img(p.image):
        imgs = [p.image]
    price_val = float(p.price or 0)
    on_request = price_val <= 0
    return {
        "id": p.id,
        "sku": p.sku or "",
        "title": p.title,
        "brand": p.brand or "",
        "brand_name": brand_name(p.brand),
        "color": p.color or "",
        "tags": p.tags or "",
        "price": price_val,
        "price_label": "Уточним цену" if on_request else f"{money(price_val)} сом",
        "on_request": on_request,
        "old_price": float(p.old_price or 0),
        "image": imgs[0] if imgs else "",
        "images": imgs,
        "badge": p.badge or "",
        "sizes": [x for x in (p.sizes or "").split(",") if x],
        "category": p.category or "",
        "description": p.description or "",
        "active": bool(p.active),
        "is_top": bool(p.is_top),
        "top_pos": int(p.top_pos or 0),
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
        old = time.time() - int(data.get("auth_date", 0)) > AUTH_MAX_AGE
        u = json.loads(data["user"])
    except Exception:
        raise HTTPException(401, "Ошибка авторизации Telegram")
    if old:
        raise HTTPException(401, "Сессия устарела — закройте и снова откройте магазин в Telegram")
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
        return None


def order_items(o):
    try:
        return json.loads(o.items or "[]")
    except Exception:
        return []


def supplier_line(o):
    items = order_items(o)
    if items:
        parts = [f"{i.get('title')} (р.{i.get('size')} ×{i.get('qty')})" for i in items]
        base = ", ".join(parts)
    else:
        base = "Пара по описанию клиента"
    extra = f" — {o.comment}" if o.comment else ""
    return f"{base}{extra} ({o.address})"


def order_text(o):
    items = order_items(o)
    lines = "\n".join(f"• {esc(i.get('title'))} — р.{esc(i.get('size'))} × {i.get('qty')}" for i in items)
    if not lines:
        lines = "• Пара по описанию клиента"
    total_str = "уточняем у поставщика" if (o.total or 0) <= 0 else f"{o.total:,.0f} сом"
    t = (
        f"🆕 <b>Заявка #{o.id} · STYLE HUB</b> · {STATUSES.get(o.status, o.status)}\n\n"
        f"{lines}\n\n"
        f"💰 Цена: <b>{total_str}</b>\n"
        f"👤 Клиент: {esc(o.username)}\n"
        f"📞 Телефон: {esc(o.phone)}\n"
        f"📍 Город / адрес: {esc(o.address)}\n"
        f"📋 Поставщику: <code>{esc(supplier_line(o))}</code>"
    )
    return t + (f"\n💬 Комментарий: {esc(o.comment)}" if o.comment else "")


def order_kb(o):
    b = lambda t, s: {"text": t, "callback_data": f"st:{o.id}:{s}"}
    rows = [
        [b("🔎 Наличие", "searching"), b("💬 Цена дана", "quoted")],
        [b("✅ Подтвердить", "confirmed"), b("💰 Оплачен", "paid")],
        [b("🚚 Отправлен", "shipped"), b("🎉 Доставлен", "done")],
        [b("❌ Отменить", "cancel")],
    ]
    w = wa_phone(o.phone)
    if w:
        preview = ", ".join(f"{i.get('title')} (р.{i.get('size')})" for i in order_items(o)) or "выбор пары"
        wa_text = quote(f"Здравствуйте! Пишем из STYLE HUB по заявке #{o.id}: {preview}. Уточнили детали и готовы ответить.")
        rows.append([{"text": "💬 Написать клиенту в WhatsApp", "url": f"https://wa.me/{w}?text={wa_text}"}])
    return {"inline_keyboard": rows}


async def notify_admins(o):
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст — уведомления некому отправлять")
    for aid in ADMIN_IDS:
        await tg(
            "sendMessage",
            chat_id=aid,
            text="🔔 Новая заявка в STYLE HUB!\n\n" + order_text(o),
            parse_mode="HTML",
            reply_markup=order_kb(o),
        )


async def apply_status(db, order_id, status):
    o = await db.get(Order, order_id)
    if not o or status not in STATUSES:
        return None
    o.status = status
    await db.commit()
    notified = True
    if status in CLIENT_MSG and o.uid:
        res = await tg("sendMessage", chat_id=o.uid, text=CLIENT_MSG[status].format(id=o.id))
        notified = bool(res and res.get("ok"))
    return o, notified


async def on_callback(cq):
    if str(cq["from"]["id"]) not in ADMIN_IDS:
        return await tg("answerCallbackQuery", callback_query_id=cq["id"], text="Нет доступа", show_alert=True)
    try:
        _, oid, st = cq["data"].split(":")
        async with Session() as db:
            res = await apply_status(db, int(oid), st)
    except Exception:
        return await tg("answerCallbackQuery", callback_query_id=cq["id"], text="Ошибка")
    if not res:
        return await tg("answerCallbackQuery", callback_query_id=cq["id"], text="Заявка не найдена")
    o, notified = res
    tip = STATUSES[st] if notified else f"{STATUSES[st]} (клиенту не доставлено)"
    await tg("answerCallbackQuery", callback_query_id=cq["id"], text=tip[:180])
    m = cq.get("message") or {}
    if m.get("chat") and m.get("message_id"):
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
                "Добро пожаловать в <b>STYLE HUB</b> — кроссовки и обувь в Бишкеке и по КР.\n"
                "Выбирайте модель и размер в каталоге, добавляйте в избранное или отправляйте заявку — "
                "мы проверим наличие и свяжемся с вами."
            ),
            parse_mode="HTML",
        )


SEED_CATS = [
    ("sneakers", "Кроссовки", 1),
    ("sport", "Спорт и зал", 2),
    ("classic", "Классика и туфли", 3),
    ("everyday", "Повседневные", 4),
]


async def migrate():
    ine = "IF NOT EXISTS " if IS_PG else ""
    false = "FALSE" if IS_PG else "0"
    cols = [
        ("shop_products", "images", "TEXT DEFAULT '[]'"),
        ("shop_products", "badge", "VARCHAR DEFAULT ''"),
        ("shop_products", "sku", "VARCHAR DEFAULT ''"),
        ("shop_products", "color", "VARCHAR DEFAULT ''"),
        ("shop_products", "tags", "VARCHAR DEFAULT ''"),
        ("shop_products", "search", "TEXT DEFAULT ''"),
        ("shop_products", "is_top", f"BOOLEAN DEFAULT {false}"),
        ("shop_products", "top_pos", "INTEGER DEFAULT 0"),
        ("shop_orders", "req_id", "VARCHAR DEFAULT ''"),
    ]
    for table, col, ddl in cols:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ine}{col} {ddl}"))
        except Exception:
            pass
    idx = [
        ("ix_shop_products_sku", "shop_products", "sku"),
        ("ix_shop_products_category", "shop_products", "category"),
        ("ix_shop_products_active", "shop_products", "active"),
        ("ix_shop_products_is_top", "shop_products", "is_top"),
        ("ix_shop_orders_req_id", "shop_orders", "req_id"),
    ]
    for name, table, col in idx:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})"))
        except Exception as e:
            log.warning("index %s: %s", name, e)


async def backfill():
    async with Session() as s:
        cats = {c["slug"]: c["name"] for c in await get_cats(s)}
        while True:
            rows = (
                await s.execute(select(Product).where(or_(Product.search.is_(None), Product.search == "")).limit(300))
            ).scalars().all()
            if not rows:
                break
            for p in rows:
                p.search = make_search(p.title, p.brand, p.color, p.tags, cats.get(p.category, ""), p.sku, p.sizes)
            await s.commit()


@asynccontextmanager
async def lifespan(app):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate()
    async with Session() as s:
        if not (await s.execute(select(Category.slug).limit(1))).first():
            s.add_all([Category(slug=a, name=b, pos=c) for a, b, c in SEED_CATS])
            await s.commit()
    try:
        await backfill()
    except Exception:
        log.exception("backfill failed")
    if BOT_TOKEN and PUBLIC_URL:
        await tg("setWebhook", url=f"{PUBLIC_URL}/tg/webhook/{HOOK_SECRET}", allowed_updates=["message", "callback_query"])
        await tg("setChatMenuButton", menu_button={"type": "web_app", "text": "STYLE HUB", "web_app": {"url": PUBLIC_URL}})
    else:
        log.warning("BOT_TOKEN или PUBLIC_URL не заданы — бот и уведомления выключены")
    yield


app = FastAPI(title="STYLE HUB", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=800)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def no_store_api(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
async def index():
    return FileResponse(BASE / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/health/db")
async def health_db(db: AsyncSession = Depends(get_db)):
    await db.execute(text("SELECT 1"))
    return {"ok": True, "db": True}


@app.get("/img/{iid}")
async def get_img(iid: int, db: AsyncSession = Depends(get_db)):
    im = await db.get(Image, iid)
    if not im:
        raise HTTPException(404)
    return Response(im.data, media_type=im.mime, headers={"Cache-Control": "public, max-age=31536000, immutable"})


PRICE_FIRST = case((Product.price > 0, 0), else_=1)
ORDERS = {
    "new": (Product.id.desc(),),
    "top": (Product.is_top.desc(), Product.top_pos.desc(), Product.id.desc()),
    "price_asc": (PRICE_FIRST, Product.price.asc(), Product.id.desc()),
    "price_desc": (Product.price.desc(), Product.id.desc()),
}


def catalog_filters(q="", cat="", brand="", price="all"):
    w = []
    if cat and cat != "all":
        w.append(Product.category == cat)
    if brand:
        w.append(Product.brand.like(f"{esc_like(brand)}%", escape="\\"))
    if price == "priced":
        w.append(Product.price > 0)
    elif price == "request":
        w.append(or_(Product.price <= 0, Product.price.is_(None)))
    for tok in norm(q).split()[:6]:
        if re.fullmatch(r"\d{2}", tok) and 35 <= int(tok) <= 50:
            tok = f"size{tok}"
        w.append(Product.search.like(f"%{esc_like(tok)}%", escape="\\"))
    return w


@app.get("/api/init")
async def api_init(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        who = await auth(request)
    except HTTPException:
        who = None
    cfg = await get_settings(db)
    cfg["app_link"] = APP_LINK
    cfg.pop("qr", None)

    agg = {}
    for b, c in (await db.execute(select(Product.brand, func.count(Product.id)).where(Product.active == True).group_by(Product.brand))).all():  # noqa: E712
        n = brand_name(b)
        if n:
            agg[n] = agg.get(n, 0) + c
    brands = [{"name": n, "count": c} for n, c in sorted(agg.items(), key=lambda x: (-x[1], x[0]))][:60]

    top_rows = (
        await db.execute(select(Product).where(Product.active == True, Product.is_top == True).order_by(*ORDERS["top"]).limit(12))  # noqa: E712
    ).scalars().all()
    total = (await db.execute(select(func.count(Product.id)).where(Product.active == True))).scalar() or 0  # noqa: E712

    favs = []
    if who:
        favs = [r for r in (await db.execute(select(Favorite.pid).where(Favorite.uid == who["id"]).order_by(Favorite.id.desc()))).scalars()]
    return {
        "cfg": cfg,
        "cats": await get_cats(db),
        "brands": brands,
        "top": [prod_dict(p) for p in top_rows],
        "total": total,
        "favs": favs,
        "authed": bool(who),
        "is_admin": bool(who and who["id"] in ADMIN_IDS),
    }


@app.get("/api/products")
async def api_products(
    q: str = "",
    cat: str = "",
    brand: str = "",
    price: str = "all",
    sort: str = "new",
    top: int = 0,
    ids: str = "",
    offset: int = 0,
    limit: int = 24,
    db: AsyncSession = Depends(get_db),
):
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    where = [Product.active == True] + catalog_filters(q, cat, brand, price)  # noqa: E712
    if top:
        where.append(Product.is_top == True)  # noqa: E712
    if ids:
        id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()][:100]
        if not id_list:
            return {"items": [], "total": 0}
        where.append(Product.id.in_(id_list))
    total = (await db.execute(select(func.count(Product.id)).where(*where))).scalar() or 0
    order = ORDERS.get(sort, ORDERS["new"])
    rows = (await db.execute(select(Product).where(*where).order_by(*order).offset(offset).limit(limit))).scalars().all()
    return {"items": [prod_dict(p) for p in rows], "total": total}


async def fav_ids(db, uid):
    return [r for r in (await db.execute(select(Favorite.pid).where(Favorite.uid == uid).order_by(Favorite.id.desc()))).scalars()]


@app.get("/api/favorites")
async def get_favorites(db: AsyncSession = Depends(get_db), who=Depends(auth)):
    return {"ids": await fav_ids(db, who["id"])}


@app.post("/api/favorites/sync")
async def sync_favorites(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    incoming = [int(x) for x in (d.get("ids") or []) if str(x).isdigit()][:300]
    have = set(await fav_ids(db, who["id"]))
    new = [i for i in incoming if i not in have]
    if new:
        exist = set((await db.execute(select(Product.id).where(Product.id.in_(new)))).scalars())
        for pid in new:
            if pid in exist:
                db.add(Favorite(uid=who["id"], pid=pid))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
    return {"ids": await fav_ids(db, who["id"])}


@app.post("/api/favorites/{pid}")
async def set_favorite(pid: int, d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    on = bool(d.get("on", True))
    row = (await db.execute(select(Favorite).where(Favorite.uid == who["id"], Favorite.pid == pid))).scalars().first()
    if on and not row and await db.get(Product, pid):
        db.add(Favorite(uid=who["id"], pid=pid))
    elif not on and row:
        await db.delete(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
    return {"on": on}


@app.post("/api/orders")
async def create_order(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    phone, address, comment = clean(d.get("phone"), 32), clean(d.get("address"), 200), clean(d.get("comment"), 500)
    req_id = clean(d.get("req_id"), 64)
    if len(re.sub(r"\D", "", phone)) < 9:
        raise HTTPException(400, "Укажите корректный номер телефона")
    if len(address) < 2:
        raise HTTPException(400, "Укажите город или район")

    if req_id:
        dup = (await db.execute(select(Order).where(Order.uid == who["id"], Order.req_id == req_id))).scalars().first()
        if dup:
            return {"id": dup.id, "total": float(dup.total or 0), "on_request": float(dup.total or 0) <= 0, "duplicate": True}

    recent = (
        await db.execute(select(func.count(Order.id)).where(Order.uid == who["id"], Order.created > time.time() - 60))
    ).scalar() or 0
    if recent >= 5:
        raise HTTPException(429, "Слишком много заявок подряд. Подождите минуту.")

    items = d.get("items") or []
    if not isinstance(items, list) or len(items) > 20:
        raise HTTPException(400, "Слишком много позиций в заявке")
    if not items and len(comment) < 4:
        raise HTTPException(400, "Выберите модель или напишите пожелания в комментарии")

    lines, total = [], 0.0
    try:
        ids = {int(i["id"]) for i in items}
        prods = {}
        if ids:
            prods = {
                p.id: p
                for p in (await db.execute(select(Product).where(Product.id.in_(ids), Product.active == True))).scalars()  # noqa: E712
            }
        for i in items:
            p, size, qty = prods.get(int(i["id"])), str(i.get("size") or ""), int(i.get("qty") or 1)
            allowed = [x for x in (p.sizes or "").split(",") if x] if p else []
            if not p or not size or (allowed and size not in allowed) or not 1 <= qty <= 10:
                raise ValueError
            price = float(p.price or 0)
            lines.append({
                "id": p.id,
                "title": p.title,
                "size": size,
                "qty": qty,
                "price": price,
                "on_request": price <= 0,
                "image": prod_dict(p)["image"],
            })
            total += price * qty
    except Exception:
        raise HTTPException(400, "Одна из моделей или размеров недоступна. Обновите страницу и повторите")

    o = Order(
        uid=who["id"],
        username=who["name"],
        items=json.dumps(lines, ensure_ascii=False),
        total=total,
        phone=phone,
        address=address,
        comment=comment,
        status="new",
        req_id=req_id,
    )
    db.add(o)
    await db.commit()
    await notify_admins(o)
    return {"id": o.id, "total": total, "on_request": total <= 0}


@app.get("/api/my_orders")
async def my_orders(db: AsyncSession = Depends(get_db), who=Depends(auth)):
    rows = (await db.execute(select(Order).where(Order.uid == who["id"]).order_by(Order.id.desc()).limit(30))).scalars()
    return [order_dict(o) for o in rows]


@app.get("/api/admin/stats")
async def admin_stats(db: AsyncSession = Depends(get_db), _=Depends(admin)):
    cnt = lambda *w: select(func.count(Product.id)).where(*w)
    total = (await db.execute(cnt())).scalar() or 0
    active = (await db.execute(cnt(Product.active == True))).scalar() or 0  # noqa: E712
    top = (await db.execute(cnt(Product.active == True, Product.is_top == True))).scalar() or 0  # noqa: E712
    orders_total = (await db.execute(select(func.count(Order.id)))).scalar() or 0
    orders_new = (await db.execute(select(func.count(Order.id)).where(Order.status == "new"))).scalar() or 0
    return {
        "products_total": total,
        "products_active": active,
        "top_count": top,
        "orders_total": orders_total,
        "orders_new": orders_new,
    }


@app.get("/api/admin/products")
async def admin_products(
    q: str = "",
    cat: str = "",
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _=Depends(admin),
):
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    where = []
    if cat and cat != "all":
        where.append(Product.category == cat)
    for tok in norm(q).split()[:5]:
        where.append(Product.search.like(f"%{esc_like(tok)}%", escape="\\"))
    total = (await db.execute(select(func.count(Product.id)).where(*where))).scalar() or 0
    rows = (
        await db.execute(select(Product).where(*where).order_by(Product.is_top.desc(), Product.id.desc()).offset(offset).limit(limit))
    ).scalars().all()
    return {"items": [prod_dict(p) for p in rows], "total": total}


@app.post("/api/admin/products")
async def admin_save_product(d: dict, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    try:
        price = to_num(d.get("price"), 0.0)
        old = to_num(d.get("old_price"), 0.0)
    except Exception:
        raise HTTPException(400, "Цена должна быть числом или пустой")
    if price < 0 or old < 0:
        raise HTTPException(400, "Цена не может быть отрицательной")

    title = clean(d.get("title"), 120)
    sizes = parse_sizes(d.get("sizes", "39,40,41,42,43,44"))
    imgs = parse_images(d.get("images") if d.get("images") is not None else d.get("image"))
    if not title or not sizes:
        raise HTTPException(400, "Заполните название и размеры")
    if not imgs:
        raise HTTPException(400, "Добавьте хотя бы одно фото (https://... или загрузите файл)")

    if d.get("id"):
        p = await db.get(Product, int(d["id"]))
        if not p:
            raise HTTPException(404, "Модель не найдена")
    else:
        p = Product()
        db.add(p)

    cats = {c["slug"]: c["name"] for c in await get_cats(db)}
    cat_slug = clean(d.get("category"), 40)
    p.sku = clean(d.get("sku"), 40)
    p.title = title
    p.brand = clean(d.get("brand"), 60)
    p.color = clean(d.get("color"), 60)
    p.tags = clean(d.get("tags"), 200)
    p.price = price
    p.old_price = old
    p.sizes = sizes
    p.image = imgs[0]
    p.images = json.dumps(imgs)
    badge_clean = clean(d.get("badge"), 20)
    p.badge = "" if badge_clean == "Узнать цену" else badge_clean
    p.category = cat_slug
    p.description = clean(d.get("description"), 1500)
    p.is_top = to_bool(d.get("is_top"), False)
    p.top_pos = int(to_num(d.get("top_pos"), 0))
    p.active = to_bool(d.get("active"), True)
    p.search = make_search(p.title, p.brand, p.color, p.tags, cats.get(cat_slug, ""), p.sku, p.sizes)
    await db.commit()
    return prod_dict(p)


@app.post("/api/admin/import")
async def admin_import_products(d: dict, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    raw_items = d.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(400, "Передайте список моделей в поле items")
    default_cat = clean(d.get("category") or "sneakers", 40)
    default_top = to_bool(d.get("is_top"), False)
    cats = {c["slug"]: c["name"] for c in await get_cats(db)}
    added = 0
    for item in raw_items[:500]:
        if not isinstance(item, dict):
            continue
        title = clean(item.get("title"), 120)
        if not title:
            continue
        try:
            price = max(0.0, to_num(item.get("price"), 0.0))
        except Exception:
            price = 0.0
        sizes = parse_sizes(item.get("sizes") or "39,40,41,42,43,44") or "39,40,41,42,43,44"
        cat_slug = clean(item.get("category") or default_cat or "sneakers", 40)
        fallback_img = CATEGORY_FALLBACK_IMG.get(cat_slug, CATEGORY_FALLBACK_IMG["sneakers"])
        imgs = parse_images(item.get("images") if item.get("images") is not None else item.get("image"))
        img = imgs[0] if imgs else fallback_img
        brand = clean(item.get("brand"), 60)
        color = clean(item.get("color"), 60)
        tags = clean(item.get("tags"), 200)
        sku = clean(item.get("sku"), 40)
        is_top = to_bool(item.get("is_top"), default_top)
        db.add(
            Product(
                sku=sku,
                title=title,
                brand=brand,
                color=color,
                tags=tags,
                price=price,
                old_price=0.0,
                image=img,
                images=json.dumps([img]),
                badge=clean(item.get("badge"), 20),
                sizes=sizes,
                category=cat_slug,
                description=clean(item.get("description") or "Проверим наличие и цену у поставщика.", 1500),
                search=make_search(title, brand, color, tags, cats.get(cat_slug, ""), sku, sizes),
                active=True,
                is_top=is_top,
                top_pos=1 if is_top else 0,
            )
        )
        added += 1
    await db.commit()
    return {"added": added}


@app.delete("/api/admin/products")
async def admin_delete_all_products(cat: str = "", db: AsyncSession = Depends(get_db), _=Depends(admin)):
    cat_slug = clean(cat, 40)
    if cat_slug and cat_slug != "all":
        target_ids = [
            r for r in (await db.execute(select(Product.id).where(Product.category == cat_slug))).scalars()
        ]
        if target_ids:
            await db.execute(delete(Favorite).where(Favorite.pid.in_(target_ids)))
        res = await db.execute(delete(Product).where(Product.category == cat_slug))
    else:
        await db.execute(delete(Favorite))
        res = await db.execute(delete(Product))
    await db.commit()
    return {"ok": True, "deleted": int(res.rowcount or 0)}


@app.delete("/api/admin/products/{pid}")
async def admin_delete_product(pid: int, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    await db.execute(delete(Favorite).where(Favorite.pid == pid))
    await db.execute(delete(Product).where(Product.id == pid))
    await db.commit()
    return {"ok": True}


@app.get("/api/admin/orders")
async def admin_orders(db: AsyncSession = Depends(get_db), _=Depends(admin)):
    rows = (await db.execute(select(Order).order_by(Order.id.desc()).limit(150))).scalars()
    return [order_dict(o) for o in rows]


@app.post("/api/admin/orders/{oid}/status")
async def admin_status(oid: int, d: dict, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    res = await apply_status(db, oid, d.get("status"))
    if not res:
        raise HTTPException(404, "Заявка не найдена")
    o, notified = res
    out = order_dict(o)
    out["notified"] = notified
    return out


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
        raise HTTPException(400, "Неверный формат изображения (нужны JPG, PNG или WEBP)")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except Exception:
        raise HTTPException(400, "Повреждённый файл изображения")
    if not raw or len(raw) > 2_500_000:
        raise HTTPException(400, "Фото слишком большое (максимум 2.5 МБ)")
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
    s.pop("qr", None)
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
