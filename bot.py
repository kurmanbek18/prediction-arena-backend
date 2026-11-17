"""
Sneaker Drop Bishkek — backend v2 (FastAPI + SQLAlchemy + Neon + Telegram bot).
Что умеет:
  • каталог, заказы с пересчётом цены на сервере (клиент не может подделать цену)
  • проверка подписи Telegram initData (без дыр)
  • уведомления о заказах в Telegram всем админам (ADMIN_IDS) + кнопки смены статуса прямо в чате
  • админка из телефона: товары, фото (загрузка с галереи), заказы, настройки (WhatsApp, QR оплаты)
"""
import os, re, json, hmac, time, html, base64, hashlib, logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import Column, Integer, String, Float, Text, Boolean, LargeBinary, select, delete
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shop")
BASE = Path(__file__).parent

# ───────────── Настройки из переменных окружения (Render → Environment) ─────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", os.getenv("ADMIN_TELEGRAM_ID", "")).split(",") if x.strip()}
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
HOOK_SECRET = hashlib.sha256(("hook" + BOT_TOKEN).encode()).hexdigest()[:32]

DEFAULTS = {
    "shop_name": "SNEAKER DROP",
    "whatsapp": "",
    "pay_text": "Реквизиты для оплаты менеджер пришлёт в WhatsApp после подтверждения заказа.",
    "delivery_text": "Доставка по Бишкеку 1–3 дня. Перед оплатой менеджер подтверждает наличие и размер.",
    "qr": "",
}

STATUSES = {
    "new": "🆕 Новый", "confirmed": "✅ Подтверждён", "paid": "💰 Оплачен",
    "shipped": "🚚 Отправлен", "done": "🎉 Доставлен", "cancel": "❌ Отменён",
}
CLIENT_MSG = {
    "confirmed": "✅ Заказ #{id} подтверждён! Менеджер скоро напишет вам по оплате и доставке.",
    "paid": "💰 Оплата по заказу #{id} получена. Спасибо!",
    "shipped": "🚚 Заказ #{id} отправлен. Ожидайте курьера.",
    "done": "🎉 Заказ #{id} доставлен. Спасибо, что выбрали нас!",
    "cancel": "❌ Заказ #{id} отменён. Если это ошибка — напишите менеджеру.",
}

# ───────────── База данных ─────────────
url = os.getenv("DATABASE_URL", "").strip()
if not url:
    url = "sqlite+aiosqlite:///./shop.db"
else:
    url = url.split("?")[0].replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
kw = {"pool_pre_ping": True}
if "asyncpg" in url:
    kw.update(connect_args={"ssl": True, "statement_cache_size": 0}, pool_size=5, max_overflow=5, pool_recycle=300)
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
    price = Column(Float, nullable=False)
    old_price = Column(Float, default=0)
    image = Column(String, nullable=False)
    sizes = Column(String, default="40,41,42,43,44")
    category = Column(String, default="drops")
    description = Column(Text, default="")
    active = Column(Boolean, default=True)
    created = Column(Float, default=time.time)


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
    total = Column(Float)
    phone = Column(String)
    address = Column(String)
    comment = Column(String, default="")
    status = Column(String, default="new")
    created = Column(Float, default=time.time)


class Setting(Base):
    __tablename__ = "shop_settings"
    key = Column(String, primary_key=True)
    value = Column(Text, default="")


# ───────────── Утилиты ─────────────
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


async def get_settings(db):
    s = dict(DEFAULTS)
    for r in (await db.execute(select(Setting))).scalars():
        s[r.key] = r.value
    return s


def prod_dict(p):
    return {"id": p.id, "title": p.title, "brand": p.brand, "price": p.price, "old_price": p.old_price,
            "image": p.image, "sizes": [x for x in p.sizes.split(",") if x], "category": p.category,
            "description": p.description, "active": bool(p.active)}


def order_dict(o):
    return {"id": o.id, "username": o.username, "items": json.loads(o.items), "total": o.total, "phone": o.phone,
            "address": o.address, "comment": o.comment, "status": o.status, "created": o.created,
            "wa": wa_phone(o.phone)}


# ───────────── Авторизация Telegram ─────────────
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
        if time.time() - int(data.get("auth_date", 0)) > 86400:
            raise HTTPException(401, "Сессия устарела, откройте магазин заново")
        u = json.loads(data["user"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Ошибка авторизации Telegram")
    return {"id": str(u["id"]), "name": u.get("first_name") or u.get("username") or "Клиент"}


async def admin(who=Depends(auth)):
    if who["id"] not in ADMIN_IDS:
        raise HTTPException(403, "Доступ запрещён")
    return who


# ───────────── Telegram Bot API ─────────────
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
    items = json.loads(o.items)
    lines = "\n".join(f"• {esc(i['title'])} — р.{esc(i['size'])} × {i['qty']}" for i in items)
    t = (f"🛍 <b>Заказ #{o.id}</b> · {STATUSES.get(o.status, o.status)}\n\n{lines}\n\n"
         f"💰 <b>{o.total:,.0f} сом</b>\n👤 {esc(o.username)}\n📞 {esc(o.phone)}\n📍 {esc(o.address)}")
    return t + (f"\n💬 {esc(o.comment)}" if o.comment else "")


def order_kb(o):
    b = lambda t, s: {"text": t, "callback_data": f"st:{o.id}:{s}"}
    rows = [[b("✅ Подтвердить", "confirmed"), b("💰 Оплачен", "paid")],
            [b("🚚 Отправлен", "shipped"), b("🎉 Доставлен", "done")],
            [b("❌ Отменить", "cancel")]]
    w = wa_phone(o.phone)
    if w:
        rows.append([{"text": "💬 Написать клиенту в WhatsApp", "url": f"https://wa.me/{w}"}])
    return {"inline_keyboard": rows}


async def notify_admins(o):
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст — уведомления некому отправлять")
    for aid in ADMIN_IDS:
        await tg("sendMessage", chat_id=aid, text="🔔 Новый заказ!\n\n" + order_text(o),
                 parse_mode="HTML", reply_markup=order_kb(o))


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
    uid = str(cq["from"]["id"])
    if uid not in ADMIN_IDS:
        return await tg("answerCallbackQuery", callback_query_id=cq["id"], text="Нет доступа", show_alert=True)
    try:
        _, oid, st = cq["data"].split(":")
        async with Session() as db:
            o = await apply_status(db, int(oid), st)
    except Exception:
        return await tg("answerCallbackQuery", callback_query_id=cq["id"], text="Ошибка")
    if not o:
        return await tg("answerCallbackQuery", callback_query_id=cq["id"], text="Заказ не найден")
    await tg("answerCallbackQuery", callback_query_id=cq["id"], text=STATUSES[st])
    m = cq.get("message") or {}
    await tg("editMessageText", chat_id=m["chat"]["id"], message_id=m["message_id"], text=order_text(o),
             parse_mode="HTML", reply_markup=order_kb(o))


async def on_message(m):
    text = (m.get("text") or "").strip()
    chat = m["chat"]["id"]
    if text.startswith("/id"):
        return await tg("sendMessage", chat_id=chat, text=f"Ваш Telegram ID: {m['from']['id']}")
    if text.startswith("/start"):
        kb = {"inline_keyboard": [[{"text": "👟 Открыть магазин", "web_app": {"url": PUBLIC_URL}}]]} if PUBLIC_URL else None
        await tg("sendMessage", chat_id=chat, reply_markup=kb,
                 text="Добро пожаловать! Здесь лимитированные кроссовки с доставкой по Бишкеку.\nНажмите кнопку ниже, чтобы открыть каталог.")


# ───────────── Старт приложения ─────────────
SEED = [
    ("Nike Air Jordan 4 Retro 'Bred Reimagined'", "Nike", 14500, 17000, "https://images.unsplash.com/photo-1584735935682-2f2b69dff9d2?w=700", "40,41,42,43,44,45", "drops", "Легендарная кожаная модель в классической расцветке."),
    ("Adidas Yeezy Boost 350 V2 'Onyx'", "Adidas", 12000, 14000, "https://images.unsplash.com/photo-1608231387042-66d1773070a5?w=700", "41,42,43,44", "lifestyle", "Максимальный комфорт с технологией Primeknit и Boost."),
    ("New Balance 9060 'Sea Salt'", "New Balance", 13500, 15500, "https://images.unsplash.com/photo-1539185441755-769473a23570?w=700", "38,39,40,41,42,43", "lifestyle", "Футуристичный дизайн и премиальные материалы."),
    ("Nike Dunk Low 'Panda'", "Nike", 10500, 12500, "https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?w=700", "39,40,41,42,43,44", "drops", "Самая универсальная и узнаваемая расцветка."),
]


@asynccontextmanager
async def lifespan(app):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as s:
        if not (await s.execute(select(Product.id).limit(1))).first():
            s.add_all([Product(title=a, brand=b, price=c, old_price=d, image=e, sizes=f, category=g, description=h)
                       for a, b, c, d, e, f, g, h in SEED])
            await s.commit()
    if BOT_TOKEN and PUBLIC_URL:
        await tg("setWebhook", url=f"{PUBLIC_URL}/tg/webhook/{HOOK_SECRET}", allowed_updates=["message", "callback_query"])
        await tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Магазин", "web_app": {"url": PUBLIC_URL}})
    else:
        log.warning("BOT_TOKEN или PUBLIC_URL не заданы — бот и уведомления выключены")
    yield


app = FastAPI(title="Sneaker Drop Bishkek", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ───────────── Публичные эндпоинты ─────────────
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
    return {"cfg": await get_settings(db), "products": [prod_dict(p) for p in rows],
            "authed": bool(who), "is_admin": bool(who and who["id"] in ADMIN_IDS)}


@app.post("/api/orders")
async def create_order(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    phone, address, comment = clean(d.get("phone"), 32), clean(d.get("address"), 200), clean(d.get("comment"), 300)
    if len(re.sub(r"\D", "", phone)) < 9:
        raise HTTPException(400, "Укажите корректный номер телефона")
    if len(address) < 5:
        raise HTTPException(400, "Укажите адрес доставки")
    items = d.get("items")
    if not isinstance(items, list) or not items or len(items) > 20:
        raise HTTPException(400, "Корзина пуста")
    try:
        ids = {int(i["id"]) for i in items}
        prods = {p.id: p for p in (await db.execute(select(Product).where(Product.id.in_(ids), Product.active == True))).scalars()}
        lines, total = [], 0.0
        for i in items:
            p, size, qty = prods.get(int(i["id"])), str(i["size"]), int(i["qty"])
            if not p or size not in p.sizes.split(",") or not 1 <= qty <= 10:
                raise ValueError
            lines.append({"id": p.id, "title": p.title, "size": size, "qty": qty, "price": p.price})
            total += p.price * qty
    except Exception:
        raise HTTPException(400, "Один из товаров недоступен. Обновите страницу и повторите")
    o = Order(uid=who["id"], username=who["name"], items=json.dumps(lines, ensure_ascii=False), total=total,
              phone=phone, address=address, comment=comment)
    db.add(o)
    await db.commit()
    await notify_admins(o)
    return {"id": o.id, "total": total}


@app.get("/api/my_orders")
async def my_orders(db: AsyncSession = Depends(get_db), who=Depends(auth)):
    rows = (await db.execute(select(Order).where(Order.uid == who["id"]).order_by(Order.id.desc()).limit(30))).scalars()
    return [order_dict(o) for o in rows]


# ───────────── Админ-эндпоинты ─────────────
@app.get("/api/admin/orders")
async def admin_orders(db: AsyncSession = Depends(get_db), _=Depends(admin)):
    rows = (await db.execute(select(Order).order_by(Order.id.desc()).limit(100))).scalars()
    return [order_dict(o) for o in rows]


@app.post("/api/admin/orders/{oid}/status")
async def admin_status(oid: int, d: dict, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    o = await apply_status(db, oid, d.get("status"))
    if not o:
        raise HTTPException(404, "Заказ не найден")
    return order_dict(o)


@app.get("/api/admin/products")
async def admin_products(db: AsyncSession = Depends(get_db), _=Depends(admin)):
    rows = (await db.execute(select(Product).order_by(Product.id.desc()))).scalars()
    return [prod_dict(p) for p in rows]


@app.post("/api/admin/products")
async def admin_save_product(d: dict, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    try:
        price, old = float(d.get("price")), float(d.get("old_price") or 0)
    except Exception:
        raise HTTPException(400, "Цена должна быть числом")
    title, image = clean(d.get("title"), 120), clean(d.get("image"), 500)
    sizes = ",".join(x.strip() for x in str(d.get("sizes", "")).split(",") if x.strip())
    if not title or price <= 0 or not sizes:
        raise HTTPException(400, "Заполните название, цену и размеры")
    if not (image.startswith("/img/") or image.startswith("https://")):
        raise HTTPException(400, "Добавьте фото товара")
    if d.get("id"):
        p = await db.get(Product, int(d["id"]))
        if not p:
            raise HTTPException(404, "Товар не найден")
    else:
        p = Product()
        db.add(p)
    p.title, p.brand, p.price, p.old_price, p.image, p.sizes = title, clean(d.get("brand"), 40), price, old, image, sizes
    p.category, p.description, p.active = clean(d.get("category"), 20) or "drops", clean(d.get("description"), 1000), bool(d.get("active", True))
    await db.commit()
    return prod_dict(p)


@app.delete("/api/admin/products/{pid}")
async def admin_delete_product(pid: int, db: AsyncSession = Depends(get_db), _=Depends(admin)):
    await db.execute(delete(Product).where(Product.id == pid))
    await db.commit()
    return {"ok": True}


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
    return await get_settings(db)


# ───────────── Вебхук Telegram ─────────────
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
