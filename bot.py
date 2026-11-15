"""
Crypto Arena — бэкенд (FastAPI + Neon Postgres).

Все «правила мира» лежат в CFG, LEVELS, LESSONS, SYMBOLS, LEAGUES — меняй их, а не логику.
Переменные окружения на Render:
    DATABASE_URL  — строка подключения Neon (postgresql://...)
    BOT_TOKEN     — токен бота из @BotFather (включает проверку подписи Telegram; очень рекомендуется)
    SPORTSDB_KEY  — необязательно, по умолчанию бесплатный ключ "3"
"""
import os, re, time, json, hmac, hashlib, asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import httpx
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Column, Integer, String, Float, Text, select, update, text, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

# ═══════════════════════ ПРАВИЛА МИРА (меняй здесь) ═══════════════════════
CFG = dict(
    START_BALANCE=1000.0,        # стартовый капитал нового игрока, USDT
    TRADE_FEE=0.001,             # комиссия биржи на реальные монеты (0.1%)
    AMM_FEE=0.01,                # комиссия пула игровых токенов (1%) — уходит создателю токена
    COIN_CREATE_COST=100.0,      # цена запуска токена, USDT (идёт в ликвидность пула)
    COIN_SUPPLY=1_000_000.0,     # общая эмиссия токена
    FOUNDER_SHARE=0.10,          # доля основателя (остальное — в пул)
    DUEL_FEE=0.05,               # комиссия арены с банка дуэли
    DUEL_DURATIONS=[60, 300, 900],
    DUEL_WAIT_TTL=1800,          # через сколько сек. непринятая дуэль отменяется
    MIN_BET=10.0,
    DAILY_BASE=25.0,             # ежедневная награда; растёт со стриком
    FOOTBALL_ODDS={"1": 2.2, "X": 3.0, "2": 2.9},  # намеренно с «маржой букмекера» — это урок
    XP_TRADE=5, XP_DUEL_WIN=20, XP_DUEL_PLAY=5, XP_BET_WIN=15, XP_BET_PLAY=3,
)

# тикер в игре -> пара на Binance
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "TON": "TONUSDT",
           "BNB": "BNBUSDT", "XRP": "XRPUSDT", "DOGE": "DOGEUSDT"}

# id лиги в TheSportsDB -> название
LEAGUES = {"4328": "Премьер-лига", "4335": "Ла Лига", "4331": "Бундеслига",
           "4332": "Серия A", "4480": "Лига чемпионов"}

LEVELS = [(0, "Новичок"), (150, "Ученик"), (500, "Трейдер"), (1200, "Аналитик"),
          (3000, "Мастер рынка"), (7000, "Элитный Трейдер")]

LESSONS = [
    {"id": "inflation", "emoji": "🖨️", "title": "Инфляция: почему деньги дешевеют",
     "text": "Если государство или проект печатает много новых денег, а товаров столько же, то каждая единица денег покупает меньше. Это инфляция. Поэтому в игре ежедневные награды ограничены — если раздавать бесконечно, USDT обесценится. У Bitcoin эмиссия жёстко ограничена 21 млн монет, поэтому его называют «цифровым золотом».",
     "q": "Что произойдёт с ценами, если денег в обороте станет больше, а товаров столько же?",
     "options": ["Цены вырастут", "Цены упадут", "Ничего не изменится"], "answer": 0,
     "explain": "Больше денег гонятся за тем же количеством товаров — цены растут.", "xp": 40, "usdt": 20},
    {"id": "supply_demand", "emoji": "⚖️", "title": "Спрос и предложение",
     "text": "Цена — это точка встречи покупателей и продавцов. Хотят купить больше, чем продают — цена растёт. Хотят продать больше — падает. Новости, страх и жадность меняют спрос за секунды; поэтому крипта так скачет. Посмотри на график BTC: каждая свеча — борьба двух сторон.",
     "q": "Все хотят купить монету, а продавцов мало. Что будет с ценой?",
     "options": ["Вырастет", "Упадёт", "Останется прежней"], "answer": 0,
     "explain": "Высокий спрос при малом предложении поднимает цену.", "xp": 40, "usdt": 20},
    {"id": "marketcap", "emoji": "🏢", "title": "Капитализация: что значит «дорогая» монета",
     "text": "Цена одной монеты мало что говорит. Важна капитализация = цена × количество монет. У монеты за $0.0001 с 1 трлн штук капитализация $100 млн, а у монеты за $100 с 10 тыс. штук — всего $1 млн. «Дешёвая» монета не значит, что она может легко вырасти в 100 раз.",
     "q": "Монета стоит $2, всего выпущено 1 000 000 штук. Какая капитализация?",
     "options": ["$500 000", "$2 000 000", "$1 000 000"], "answer": 1,
     "explain": "2 × 1 000 000 = 2 000 000.", "xp": 50, "usdt": 25},
    {"id": "risk", "emoji": "🎢", "title": "Риск и волатильность",
     "text": "Волатильность — насколько сильно скачет цена. Чем выше потенциальная прибыль, тем выше риск потерь. Убыток в 50% требует прибыли в 100%, чтобы вернуться в ноль (100 → 50 → 100). Никогда не вкладывай всё в одну идею и не ставь деньги, потеря которых тебя разрушит.",
     "q": "Актив упал на 50%. Сколько нужно роста, чтобы вернуться к исходной цене?",
     "options": ["50%", "100%", "150%"], "answer": 1,
     "explain": "От 50 до 100 — это рост вдвое, то есть +100%.", "xp": 50, "usdt": 25},
    {"id": "diversify", "emoji": "🧺", "title": "Диверсификация",
     "text": "Не клади все яйца в одну корзину. Если у тебя 5 разных активов и один обвалился, остальные смягчат удар. Откройте профиль: если почти всё в одной монете — портфель рискованный. Диверсификация не гарантирует прибыль, но защищает от катастрофы.",
     "q": "Зачем держать несколько разных активов?",
     "options": ["Чтобы платить меньше комиссий", "Чтобы падение одного не уничтожило весь капитал", "Чтобы цены росли быстрее"], "answer": 1,
     "explain": "Диверсификация снижает риск, а не гарантирует рост.", "xp": 50, "usdt": 25},
    {"id": "liquidity", "emoji": "💧", "title": "Ликвидность и проскальзывание",
     "text": "Ликвидность — сколько денег в «пуле», чтобы можно было купить или продать без сильного влияния на цену. У игровых токенов цена считается формулой x·y=k: чем больше ты покупаешь относительно пула, тем хуже цена — это проскальзывание. Маленький пул = токен легко «двигать» и легко обрушить. Так устроены реальные DeFi-биржи (Uniswap, STON.fi).",
     "q": "Что произойдёт, если купить на большую сумму токен с крошечным пулом?",
     "options": ["Цена исполнения будет заметно хуже", "Купишь по самой низкой цене", "Комиссия отменится"], "answer": 0,
     "explain": "Большая покупка двигает цену по кривой — это и есть проскальзывание.", "xp": 60, "usdt": 30},
    {"id": "odds", "emoji": "🎲", "title": "Коэффициенты и маржа букмекера",
     "text": "Коэффициент 2.20 означает вероятность примерно 1/2.2 = 45%. Сложи вероятности трёх исходов матча (1, X, 2): если вышло больше 100% — разница это маржа букмекера, его гарантированный заработок. Поэтому в долгую выигрывает не игрок, а организатор. В футбольных ставках игры маржа около 13% — проверь сам.",
     "q": "Какая вероятность исхода заложена в коэффициент 4.0?",
     "options": ["40%", "25%", "4%"], "answer": 1,
     "explain": "1 / 4.0 = 0.25, то есть 25%.", "xp": 60, "usdt": 30},
    {"id": "zero_sum", "emoji": "⚔️", "title": "Игра с нулевой суммой и комиссия",
     "text": "В дуэли победитель забирает ставку проигравшего — деньги не создаются, а перетекают. Но арена берёт комиссию, поэтому даже при честных 50/50 шансах ожидаемый результат отрицателен: ты платишь за участие. То же с биржами и казино: частые сделки съедает комиссия.",
     "q": "Шансы 50/50, но арена берёт комиссию. Каков результат в долгой серии дуэлей?",
     "options": ["Игрок в плюсе", "Игрок в минусе из-за комиссий", "Ровно ноль"], "answer": 1,
     "explain": "Выигрыши и проигрыши уравновешиваются, а комиссия каждый раз уменьшает капитал.", "xp": 60, "usdt": 30},
]
LESSON_BY_ID = {l["id"]: l for l in LESSONS}

# ═══════════════════════ БАЗА ДАННЫХ ═══════════════════════
raw = os.getenv("DATABASE_URL", "").strip()
if not raw:
    raise RuntimeError("Задай переменную окружения DATABASE_URL на Render")
raw = raw.replace("postgres://", "postgresql://", 1).split("?")[0]
if raw.startswith("postgresql://"):
    raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
engine = create_async_engine(
    raw + "?prepared_statement_cache_size=0",
    connect_args={"ssl": True, "statement_cache_size": 0},
    pool_pre_ping=True, pool_size=5, max_overflow=5,
)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


async def get_db():
    async with async_session_maker() as session:
        yield session


class User(Base):
    __tablename__ = "users"  # та же таблица, что была раньше — игроки сохраняются
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(String, unique=True, index=True)
    username = Column(String, default="Трейдер")
    balance = Column(Float, default=CFG["START_BALANCE"])
    xp = Column(Integer, default=0)
    level = Column(String, default="Новичок")  # устарело, уровень считается из xp
    lessons = Column(Text, default="")
    last_daily = Column(Float, default=0)
    streak = Column(Integer, default=0)


class Holding(Base):
    __tablename__ = "arena_holdings"
    id = Column(Integer, primary_key=True)
    uid = Column(String, index=True, nullable=False)
    symbol = Column(String, index=True, nullable=False)
    amount = Column(Float, default=0)
    avg_price = Column(Float, default=0)


class Coin(Base):
    __tablename__ = "arena_coins"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    symbol = Column(String, unique=True, nullable=False)
    description = Column(String, default="")
    creator_id = Column(String, nullable=False)
    creator_name = Column(String, nullable=False)
    reserve_usdt = Column(Float, nullable=False)
    reserve_token = Column(Float, nullable=False)
    fees_earned = Column(Float, default=0)
    created_at = Column(Float, default=time.time)


class Tick(Base):
    __tablename__ = "arena_ticks"
    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True)
    ts = Column(Integer)
    price = Column(Float)


class Duel(Base):
    __tablename__ = "arena_duels"
    id = Column(Integer, primary_key=True)
    creator_id = Column(String, nullable=False)
    creator_name = Column(String, nullable=False)
    opponent_id = Column(String)
    opponent_name = Column(String)
    coin = Column(String, nullable=False)
    prediction = Column(String, nullable=False)  # прогноз создателя: up/down
    amount = Column(Float, nullable=False)
    duration = Column(Integer, nullable=False)
    status = Column(String, default="waiting", index=True)  # waiting/active/finished/cancelled
    entry_price = Column(Float)
    exit_price = Column(Float)
    ends_at = Column(Float)
    winner_id = Column(String)
    prize = Column(Float, default=0)
    created_at = Column(Float, default=time.time)


class Bet(Base):
    __tablename__ = "arena_bets"
    id = Column(Integer, primary_key=True)
    uid = Column(String, index=True, nullable=False)
    event_id = Column(String, index=True, nullable=False)
    event_name = Column(String, nullable=False)
    pick = Column(String, nullable=False)  # 1 / X / 2
    amount = Column(Float, nullable=False)
    odds = Column(Float, nullable=False)
    kickoff = Column(Float, nullable=False)
    status = Column(String, default="open")  # open/won/lost/void
    created_at = Column(Float, default=time.time)


# create_all не добавляет колонки в уже существующую таблицу — делаем это вручную
MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS lessons TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily DOUBLE PRECISION DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS streak INTEGER DEFAULT 0",
]

# ═══════════════════════ ВНЕШНИЕ ДАННЫЕ ═══════════════════════
http = httpx.AsyncClient(timeout=8, headers={"User-Agent": "crypto-arena/2.0"})
_cache: dict = {}
BINANCE_BASES = ["https://data-api.binance.vision", "https://api.binance.com"]  # vision не блокирует US-серверы Render
SPORTSDB_KEY = os.getenv("SPORTSDB_KEY", "3")
INTERVALS = {"1m", "5m", "15m", "1h", "4h", "1d"}


async def cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = await fn()
    except Exception:
        if hit:  # при сбое отдаём устаревшее
            return hit[1]
        raise
    _cache[key] = (now, val)
    return val


async def binance(path, params):
    last = None
    for base in BINANCE_BASES:
        try:
            r = await http.get(base + path, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
    raise last


async def sportsdb(path, params):
    r = await http.get(f"https://www.thesportsdb.com/api/v1/json/{SPORTSDB_KEY}/{path}", params=params)
    r.raise_for_status()
    return r.json()


async def fetch_market():
    syms = json.dumps(list(SYMBOLS.values()), separators=(",", ":"))
    rows = await binance("/api/v3/ticker/24hr", {"symbols": syms})
    by = {r["symbol"]: r for r in rows}
    return {k: {"price": float(by[v]["lastPrice"]), "change": float(by[v]["priceChangePercent"]),
                "high": float(by[v]["highPrice"]), "low": float(by[v]["lowPrice"]),
                "volume": float(by[v]["quoteVolume"])} for k, v in SYMBOLS.items() if v in by}


async def get_market():
    try:
        return await cached("market", 5, fetch_market)
    except Exception:
        raise HTTPException(503, "Биржа временно недоступна, попробуйте через минуту")


def kickoff_ts(e):
    ts = e.get("strTimestamp") or f"{e.get('dateEvent')}T{e.get('strTime') or '00:00:00'}"
    try:
        return datetime.fromisoformat(ts.replace("Z", "")).replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


async def football_events():
    async def one(lid, name):
        try:
            data = await sportsdb("eventsnextleague.php", {"id": lid})
        except Exception:
            return []
        out = []
        for e in data.get("events") or []:
            ko = kickoff_ts(e)
            if ko and ko > time.time() + 60:
                out.append({"id": e["idEvent"], "league": name, "home": e["strHomeTeam"],
                            "away": e["strAwayTeam"], "kickoff": ko})
        return out
    res = await asyncio.gather(*[one(k, v) for k, v in LEAGUES.items()])
    ev = sorted(sum(res, []), key=lambda x: x["kickoff"])[:40]
    for e in ev:
        e["odds"] = CFG["FOOTBALL_ODDS"]
    return ev


# ═══════════════════════ АВТОРИЗАЦИЯ TELEGRAM ═══════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()


async def auth(request: Request):
    """Возвращает (telegram_id, имя). С BOT_TOKEN подпись initData проверяется криптографически."""
    init = request.headers.get("X-Init-Data", "")
    if BOT_TOKEN:
        if not init:
            raise HTTPException(401, "Откройте приложение через Telegram")
        data = dict(parse_qsl(init, keep_blank_values=True))
        h = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), h):
            raise HTTPException(401, "Неверная подпись Telegram")
        if time.time() - int(data.get("auth_date", 0)) > 86400 * 3:
            raise HTTPException(401, "Сессия устарела, откройте приложение заново")
        u = json.loads(data["user"])
        return str(u["id"]), u.get("first_name") or u.get("username")
    # режим разработки (BOT_TOKEN не задан): доверяем клиенту
    if init:
        try:
            u = json.loads(dict(parse_qsl(init))["user"])
            return str(u["id"]), u.get("first_name")
        except Exception:
            pass
    test = request.headers.get("X-Test-User")
    if test:
        return test[:40], None
    raise HTTPException(401, "Нет идентификатора пользователя")


# ═══════════════════════ ХЕЛПЕРЫ ═══════════════════════
async def load_user(db, uid, name=None, lock=False):
    q = select(User).where(User.telegram_id == uid)
    if lock:
        q = q.with_for_update()
    u = (await db.execute(q)).scalars().first()
    if not u:
        u = User(telegram_id=uid, username=(name or f"Trader_{uid[-4:]}")[:24], balance=CFG["START_BALANCE"], xp=0)
        db.add(u)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            u = (await db.execute(select(User).where(User.telegram_id == uid))).scalars().first()
    return u


def level_of(xp):
    cur, nxt = LEVELS[0], None
    for i, (need, name) in enumerate(LEVELS):
        if xp >= need:
            cur = (need, name)
            nxt = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
    return {"name": cur[1], "xp": xp, "from": cur[0], "to": nxt[0] if nxt else None}


def coin_price(c):
    return c.reserve_usdt / c.reserve_token


async def snapshot_prices(db):
    prices = {k: v["price"] for k, v in (await get_market()).items()}
    for c in (await db.execute(select(Coin))).scalars():
        prices[c.symbol] = coin_price(c)
    return prices


async def add_balance(db, uid, delta, xp=0):
    await db.execute(update(User).where(User.telegram_id == uid)
                     .values(balance=User.balance + delta, xp=User.xp + xp))


async def me_payload(db, u):
    try:
        prices = await snapshot_prices(db)
    except HTTPException:
        prices = {}
    hs = (await db.execute(select(Holding).where(Holding.uid == u.telegram_id, Holding.amount > 1e-12))).scalars().all()
    items, hv = [], 0.0
    for h in hs:
        p = prices.get(h.symbol, h.avg_price)
        hv += h.amount * p
        items.append({"symbol": h.symbol, "amount": h.amount, "avg": h.avg_price, "price": p,
                      "value": h.amount * p, "pnl": (p - h.avg_price) * h.amount})
    return {"id": u.telegram_id, "username": u.username, "balance": u.balance, "xp": u.xp,
            "level": level_of(u.xp), "streak": u.streak or 0,
            "lessons": [x for x in (u.lessons or "").split(",") if x], "holdings": items,
            "networth": u.balance + hv, "daily_ready": time.time() - (u.last_daily or 0) > 20 * 3600}


def duel_dict(d, uid):
    is_creator = d.creator_id == uid
    my_side = d.prediction if is_creator else ("down" if d.prediction == "up" else "up")
    res = None
    if d.status == "finished":
        res = "tie" if not d.winner_id else ("win" if d.winner_id == uid else "lose")
    return {"id": d.id, "creator": d.creator_name, "opponent": d.opponent_name, "coin": d.coin,
            "prediction": d.prediction, "my_side": my_side, "is_creator": is_creator,
            "amount": d.amount, "duration": d.duration, "status": d.status, "entry": d.entry_price,
            "exit": d.exit_price, "ends_at": d.ends_at, "result": res, "prize": d.prize}


# ═══════════════════════ ПРИЛОЖЕНИЕ ═══════════════════════
@asynccontextmanager
async def lifespan(app):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for s in MIGRATIONS:
            await conn.execute(text(s))
    task = asyncio.create_task(settle_loop())
    yield
    task.cancel()
    await http.aclose()


app = FastAPI(title="Crypto Arena", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
@app.get("/health")
async def health():
    return {"ok": True}


# ───────── Профиль ─────────
@app.get("/api/me")
async def api_me(db: AsyncSession = Depends(get_db), who=Depends(auth)):
    u = await load_user(db, *who)
    await db.commit()
    return await me_payload(db, u)


@app.post("/api/user/update_name")
async def update_name(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    name = re.sub(r"\s+", " ", str(d.get("username", ""))).strip()[:24]
    if len(name) < 2:
        raise HTTPException(400, "Имя слишком короткое")
    u = await load_user(db, who[0], who[1], lock=True)
    u.username = name
    await db.commit()
    return {"success": True, "username": name}


@app.post("/api/daily")
async def daily(db: AsyncSession = Depends(get_db), who=Depends(auth)):
    u = await load_user(db, who[0], who[1], lock=True)
    now = time.time()
    gap = now - (u.last_daily or 0)
    if gap < 20 * 3600:
        raise HTTPException(400, f"Следующая награда через {int((20 * 3600 - gap) // 3600) + 1} ч.")
    u.streak = (u.streak or 0) + 1 if gap < 48 * 3600 else 1
    reward = CFG["DAILY_BASE"] + min(u.streak, 7) * 5
    u.balance += reward
    u.last_daily = now
    u.xp += 10
    await db.commit()
    return {"reward": reward, "streak": u.streak}


@app.get("/api/leaderboard")
async def leaderboard(db: AsyncSession = Depends(get_db), who=Depends(auth)):
    prices = await snapshot_prices(db)
    users = (await db.execute(select(User).order_by(User.balance.desc()).limit(300))).scalars().all()
    hv = {}
    for h in (await db.execute(select(Holding).where(Holding.amount > 1e-12))).scalars():
        hv[h.uid] = hv.get(h.uid, 0) + h.amount * prices.get(h.symbol, h.avg_price)
    rows = [{"name": u.username, "worth": u.balance + hv.get(u.telegram_id, 0),
             "level": level_of(u.xp or 0)["name"], "me": u.telegram_id == who[0]} for u in users]
    return sorted(rows, key=lambda r: -r["worth"])[:20]


# ───────── Рынок и графики (Binance) ─────────
@app.get("/api/market")
async def market():
    return await get_market()


@app.get("/api/klines/{sym}")
async def klines(sym: str, interval: str = "15m", limit: int = 120):
    sym = sym.upper()
    if sym not in SYMBOLS or interval not in INTERVALS:
        raise HTTPException(400, "Неверный актив или интервал")
    limit = max(10, min(limit, 300))

    async def f():
        rows = await binance("/api/v3/klines", {"symbol": SYMBOLS[sym], "interval": interval, "limit": limit})
        return [{"time": int(r[0] / 1000), "open": float(r[1]), "high": float(r[2]),
                 "low": float(r[3]), "close": float(r[4])} for r in rows]
    try:
        return await cached(f"k:{sym}:{interval}:{limit}", 8, f)
    except Exception:
        raise HTTPException(503, "График временно недоступен")


# ───────── Торговля (бумажная, по реальным ценам) ─────────
@app.post("/api/trade")
async def trade(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    sym, side = str(d.get("symbol", "")).upper(), d.get("side")
    if side not in ("buy", "sell"):
        raise HTTPException(400, "Неверное действие")
    try:
        usdt, qty, pct = float(d.get("usdt") or 0), float(d.get("qty") or 0), float(d.get("pct") or 0)
    except ValueError:
        raise HTTPException(400, "Неверная сумма")
    u = await load_user(db, who[0], who[1], lock=True)
    coin, price = None, None
    if sym in SYMBOLS:
        m = (await get_market()).get(sym)
        if not m:
            raise HTTPException(503, "Цена актива сейчас недоступна")
        price = m["price"]
    else:
        coin = (await db.execute(select(Coin).where(Coin.symbol == sym).with_for_update())).scalars().first()
        if not coin:
            raise HTTPException(404, "Актив не найден")
    h = (await db.execute(select(Holding).where(Holding.uid == u.telegram_id, Holding.symbol == sym)
                          .with_for_update())).scalars().first()
    slip, pnl = 0.0, None

    if side == "buy":
        if usdt < 1 or usdt > u.balance + 1e-9:
            raise HTTPException(400, "Недостаточно средств")
        if coin:
            fee = usdt * CFG["AMM_FEE"]
            dx = usdt - fee
            k = coin.reserve_usdt * coin.reserve_token
            spot = coin_price(coin)
            new_ru = coin.reserve_usdt + dx
            new_rt = k / new_ru
            q = coin.reserve_token - new_rt
            execp = dx / q
            slip = (execp / spot - 1) * 100
            coin.reserve_usdt, coin.reserve_token = new_ru, new_rt
            coin.fees_earned += fee
            await add_balance(db, coin.creator_id, fee)
            db.add(Tick(symbol=sym, ts=int(time.time()), price=coin_price(coin)))
        else:
            fee = usdt * CFG["TRADE_FEE"]
            q = (usdt - fee) / price
            execp = price
        u.balance -= usdt
        if h:
            h.avg_price = (h.amount * h.avg_price + q * execp) / (h.amount + q)
            h.amount += q
        else:
            db.add(Holding(uid=u.telegram_id, symbol=sym, amount=q, avg_price=execp))
        msg = f"Куплено {q:.6g} {sym} по ${execp:.6g}. Комиссия ${fee:.2f}."
    else:
        if not h or h.amount <= 1e-12:
            raise HTTPException(400, "У вас нет этого актива")
        if pct > 0:
            qty = h.amount if pct >= 100 else h.amount * pct / 100
        if qty <= 0 or qty > h.amount * (1 + 1e-9):
            raise HTTPException(400, "Неверное количество")
        qty = min(qty, h.amount)
        if coin:
            k = coin.reserve_usdt * coin.reserve_token
            spot = coin_price(coin)
            gross = coin.reserve_usdt - k / (coin.reserve_token + qty)
            fee = gross * CFG["AMM_FEE"]
            out = gross - fee
            coin.reserve_usdt -= gross
            coin.reserve_token += qty
            coin.fees_earned += fee
            await add_balance(db, coin.creator_id, fee)
            execp = out / qty
            slip = (1 - execp / spot) * 100
            db.add(Tick(symbol=sym, ts=int(time.time()), price=coin_price(coin)))
        else:
            gross = qty * price
            fee = gross * CFG["TRADE_FEE"]
            out = gross - fee
            execp = out / qty
        pnl = (execp - h.avg_price) * qty
        h.amount -= qty
        if h.amount < 1e-12:
            h.amount = 0
        u.balance += out
        msg = f"Продано {qty:.6g} {sym} по ${execp:.6g}. Комиссия ${fee:.2f}. Результат: {pnl:+.2f} USDT."
    if abs(slip) >= 0.5:
        msg += f" Проскальзывание {abs(slip):.1f}%: ваш объём сдвинул цену в пуле."
    u.xp += CFG["XP_TRADE"]
    await db.commit()
    return {"ok": True, "msg": msg, "pnl": pnl}


# ───────── Игровые токены (AMM-пул x·y=k) ─────────
@app.get("/api/coins")
async def coins_list(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Coin).order_by(Coin.id.desc()).limit(50))).scalars().all()
    return [{"symbol": c.symbol, "name": c.name, "description": c.description, "creator": c.creator_name,
             "price": coin_price(c), "mcap": coin_price(c) * CFG["COIN_SUPPLY"],
             "liquidity": c.reserve_usdt, "fees": c.fees_earned} for c in rows]


@app.post("/api/coins/create")
async def coin_create(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    name = re.sub(r"\s+", " ", str(d.get("name", ""))).strip()[:30]
    sym = str(d.get("symbol", "")).strip().upper()
    desc = str(d.get("description", "")).strip()[:140]
    if len(name) < 3 or not re.fullmatch(r"[A-Z0-9]{3,6}", sym) or sym in SYMBOLS or sym == "USDT":
        raise HTTPException(400, "Название от 3 символов, тикер — 3–6 латинских букв/цифр")
    u = await load_user(db, who[0], who[1], lock=True)
    cost = CFG["COIN_CREATE_COST"]
    if u.balance < cost:
        raise HTTPException(400, f"Нужно {cost:.0f} USDT для запуска токена")
    total = CFG["COIN_SUPPLY"]
    founder = total * CFG["FOUNDER_SHARE"]
    coin = Coin(name=name, symbol=sym, description=desc, creator_id=u.telegram_id, creator_name=u.username,
                reserve_usdt=cost, reserve_token=total - founder)
    u.balance -= cost
    db.add(coin)
    db.add(Holding(uid=u.telegram_id, symbol=sym, amount=founder, avg_price=coin_price(coin)))
    db.add(Tick(symbol=sym, ts=int(time.time()), price=coin_price(coin)))
    u.xp += 30
    try:
        await db.commit()
    except IntegrityError:
        raise HTTPException(400, "Такое название или тикер уже заняты")
    return {"success": True, "message": f"Токен ${sym} запущен! Вы получили {founder:,.0f} монет основателя."}


@app.get("/api/coins/{sym}/history")
async def coin_history(sym: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Tick).where(Tick.symbol == sym.upper()).order_by(Tick.id.desc()).limit(300))).scalars().all()
    by_time = {}
    for r in reversed(rows):
        by_time[r.ts] = r.price
    pts = [{"time": t, "value": v} for t, v in sorted(by_time.items())]
    if len(pts) == 1:
        pts.insert(0, {"time": pts[0]["time"] - 60, "value": pts[0]["value"]})
    return pts


# ───────── Дуэли ─────────
@app.post("/api/duels/create")
async def duel_create(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    coin, pred = str(d.get("coin", "")).upper(), d.get("prediction")
    try:
        amount, duration = float(d.get("amount", 0)), int(d.get("duration", 300))
    except ValueError:
        raise HTTPException(400, "Неверные параметры")
    if coin not in SYMBOLS or pred not in ("up", "down") or duration not in CFG["DUEL_DURATIONS"]:
        raise HTTPException(400, "Неверные параметры дуэли")
    if amount < CFG["MIN_BET"]:
        raise HTTPException(400, f"Минимальная ставка {CFG['MIN_BET']:.0f} USDT")
    u = await load_user(db, who[0], who[1], lock=True)
    if u.balance < amount:
        raise HTTPException(400, "Недостаточно средств")
    waiting = (await db.execute(select(Duel).where(Duel.creator_id == u.telegram_id, Duel.status == "waiting"))).scalars().all()
    if len(waiting) >= 3:
        raise HTTPException(400, "Не больше 3 ожидающих дуэлей одновременно")
    u.balance -= amount
    db.add(Duel(creator_id=u.telegram_id, creator_name=u.username, coin=coin, prediction=pred,
                amount=amount, duration=duration))
    await db.commit()
    return {"success": True, "message": "Вызов брошен! Ставка заморожена до принятия или отмены."}


@app.get("/api/duels")
async def duels_list(db: AsyncSession = Depends(get_db), who=Depends(auth)):
    uid = who[0]
    open_rows = (await db.execute(select(Duel).where(Duel.status == "waiting", Duel.creator_id != uid)
                                  .order_by(Duel.id.desc()).limit(30))).scalars().all()
    mine = (await db.execute(select(Duel).where(or_(Duel.creator_id == uid, Duel.opponent_id == uid),
                                                Duel.status != "cancelled")
                             .order_by(Duel.id.desc()).limit(15))).scalars().all()
    return {"open": [duel_dict(x, uid) for x in open_rows], "mine": [duel_dict(x, uid) for x in mine]}


@app.post("/api/duels/join")
async def duel_join(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    u = await load_user(db, who[0], who[1], lock=True)
    duel = (await db.execute(select(Duel).where(Duel.id == int(d.get("duel_id", 0))).with_for_update())).scalars().first()
    if not duel or duel.status != "waiting":
        raise HTTPException(400, "Дуэль уже недоступна")
    if duel.creator_id == u.telegram_id:
        raise HTTPException(400, "Нельзя принять собственный вызов")
    if u.balance < duel.amount:
        raise HTTPException(400, "Недостаточно средств")
    m = (await get_market()).get(duel.coin)
    if not m:
        raise HTTPException(503, "Цена актива сейчас недоступна")
    price = m["price"]
    u.balance -= duel.amount
    duel.opponent_id, duel.opponent_name = u.telegram_id, u.username
    duel.status, duel.entry_price, duel.ends_at = "active", price, time.time() + duel.duration
    await db.commit()
    return {"success": True, "message": f"Дуэль началась! Цена входа ${price:,.4f}. Итог через {duel.duration // 60 or 1} мин."}


@app.post("/api/duels/cancel")
async def duel_cancel(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    duel = (await db.execute(select(Duel).where(Duel.id == int(d.get("duel_id", 0))).with_for_update())).scalars().first()
    if not duel or duel.creator_id != who[0] or duel.status != "waiting":
        raise HTTPException(400, "Нельзя отменить")
    duel.status = "cancelled"
    await add_balance(db, duel.creator_id, duel.amount)
    await db.commit()
    return {"success": True}


async def settle_duels(db):
    now = time.time()
    stale = (await db.execute(select(Duel).where(Duel.status == "waiting", Duel.created_at < now - CFG["DUEL_WAIT_TTL"])
                              .with_for_update(skip_locked=True))).scalars().all()
    for d in stale:
        d.status = "cancelled"
        await add_balance(db, d.creator_id, d.amount)
    due = (await db.execute(select(Duel).where(Duel.status == "active", Duel.ends_at <= now)
                            .with_for_update(skip_locked=True))).scalars().all()
    if due:
        try:
            prices = await get_market()
        except HTTPException:
            await db.commit()
            return
        for d in due:
            if d.coin not in prices:
                continue
            ex = prices[d.coin]["price"]
            d.exit_price, d.status = ex, "finished"
            if ex == d.entry_price:  # ничья — возврат ставок
                d.winner_id = None
                await add_balance(db, d.creator_id, d.amount, CFG["XP_DUEL_PLAY"])
                await add_balance(db, d.opponent_id, d.amount, CFG["XP_DUEL_PLAY"])
                continue
            trend = "up" if ex > d.entry_price else "down"
            creator_wins = d.prediction == trend
            win, lose = (d.creator_id, d.opponent_id) if creator_wins else (d.opponent_id, d.creator_id)
            d.winner_id = win
            d.prize = d.amount * 2 * (1 - CFG["DUEL_FEE"])
            await add_balance(db, win, d.prize, CFG["XP_DUEL_WIN"])
            await add_balance(db, lose, 0, CFG["XP_DUEL_PLAY"])
    await db.commit()


# ───────── Футбол ─────────
@app.get("/api/football/events")
async def fb_events():
    try:
        return await cached("fb", 600, football_events)
    except Exception:
        return []


@app.post("/api/football/bet")
async def fb_bet(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    pick = d.get("pick")
    try:
        amount = float(d.get("amount", 0))
    except ValueError:
        raise HTTPException(400, "Неверная сумма")
    if pick not in CFG["FOOTBALL_ODDS"] or amount < CFG["MIN_BET"]:
        raise HTTPException(400, f"Минимальная ставка {CFG['MIN_BET']:.0f} USDT")
    ev = next((e for e in await fb_events() if e["id"] == str(d.get("event_id"))), None)
    if not ev or ev["kickoff"] < time.time() + 60:
        raise HTTPException(400, "Приём ставок на этот матч закрыт")
    u = await load_user(db, who[0], who[1], lock=True)
    if u.balance < amount:
        raise HTTPException(400, "Недостаточно средств")
    u.balance -= amount
    db.add(Bet(uid=u.telegram_id, event_id=ev["id"], event_name=f"{ev['home']} — {ev['away']}", pick=pick,
               amount=amount, odds=CFG["FOOTBALL_ODDS"][pick], kickoff=ev["kickoff"]))
    await db.commit()
    return {"success": True, "message": f"Ставка принята: {pick}, потенциальный выигрыш {amount * CFG['FOOTBALL_ODDS'][pick]:.2f} USDT"}


@app.get("/api/football/bets")
async def fb_bets(db: AsyncSession = Depends(get_db), who=Depends(auth)):
    rows = (await db.execute(select(Bet).where(Bet.uid == who[0]).order_by(Bet.id.desc()).limit(20))).scalars().all()
    return [{"event": b.event_name, "pick": b.pick, "amount": b.amount, "odds": b.odds,
             "status": b.status, "kickoff": b.kickoff} for b in rows]


FINISHED = {"Match Finished", "FT", "AET", "PEN", "Finished"}


async def settle_bets(db):
    now = time.time()
    bets = (await db.execute(select(Bet).where(Bet.status == "open", Bet.kickoff < now - 7200)
                             .limit(40).with_for_update(skip_locked=True))).scalars().all()
    looked = {}
    for b in bets:
        if b.event_id not in looked:
            try:
                looked[b.event_id] = ((await sportsdb("lookupevent.php", {"id": b.event_id})).get("events") or [None])[0]
            except Exception:
                looked[b.event_id] = None
        e, result = looked[b.event_id], None
        if e and (e.get("strStatus") or "") in FINISHED and e.get("intHomeScore") not in (None, "") \
                and e.get("intAwayScore") not in (None, ""):
            h, a = int(e["intHomeScore"]), int(e["intAwayScore"])
            result = "1" if h > a else "2" if a > h else "X"
        elif now > b.kickoff + 3 * 86400:
            result = "void"
        if result is None:
            continue
        if result == "void":
            b.status, payout, xp = "void", b.amount, 0
        elif result == b.pick:
            b.status, payout, xp = "won", b.amount * b.odds, CFG["XP_BET_WIN"]
        else:
            b.status, payout, xp = "lost", 0, CFG["XP_BET_PLAY"]
        await add_balance(db, b.uid, payout, xp)
    await db.commit()


async def settle_loop():
    n = 0
    while True:
        try:
            async with async_session_maker() as db:
                await settle_duels(db)
                if n % 12 == 0:
                    await settle_bets(db)
        except Exception as e:
            print("settle error:", e)
        n += 1
        await asyncio.sleep(10)


# ───────── Обучение ─────────
@app.get("/api/lessons")
async def lessons(db: AsyncSession = Depends(get_db), who=Depends(auth)):
    u = await load_user(db, *who)
    done = set((u.lessons or "").split(","))
    return [{"id": l["id"], "emoji": l["emoji"], "title": l["title"], "text": l["text"], "q": l["q"],
             "options": l["options"], "xp": l["xp"], "usdt": l["usdt"], "done": l["id"] in done} for l in LESSONS]


@app.post("/api/lessons/complete")
async def lesson_complete(d: dict, db: AsyncSession = Depends(get_db), who=Depends(auth)):
    l = LESSON_BY_ID.get(d.get("lesson_id"))
    if not l:
        raise HTTPException(404, "Урок не найден")
    u = await load_user(db, who[0], who[1], lock=True)
    done = [x for x in (u.lessons or "").split(",") if x]
    correct = d.get("answer") == l["answer"]
    reward = False
    if correct and l["id"] not in done:
        done.append(l["id"])
        u.lessons = ",".join(done)
        u.xp += l["xp"]
        u.balance += l["usdt"]
        reward = True
    await db.commit()
    return {"correct": correct, "explain": l["explain"], "rewarded": reward, "xp": l["xp"], "usdt": l["usdt"]}


if __name__ == "__main__":
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
