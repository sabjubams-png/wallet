#!/usr/bin/env python3
"""
VAULT Backend API + Telegram Bot в одном файле
Запуск: python server.py

Эндпоинты:
  GET  /api/user/{uid}          — получить данные пользователя
  POST /api/user/{uid}/sync     — синхронизировать кошельки из WebApp
  POST /api/user/{uid}/tx       — добавить транзакцию
  GET  /api/prices              — курсы криптовалют (кеш 2 мин)
  GET  /api/admin/users         — все пользователи (только для админа)
"""

import os, json, time, logging, hashlib, hmac, threading, asyncio
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import parse_qsl
from functools import wraps

# pip install flask flask-cors python-telegram-bot aiohttp requests
from flask import Flask, request, jsonify, abort
from flask_cors import CORS
import requests as req

# ─── CONFIG ───────────────────────────────────────────────────────────
BOT_TOKEN  = os.getenv("BOT_TOKEN",  "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-name.github.io/vault")
ADMIN_IDS  = [int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip()]
PORT       = int(os.getenv("PORT", 5000))
DB_FILE    = "vault_db.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vault")

COINS = ["BTC","ETH","USDT","SOL","TON","BNB","MATIC","AVAX"]
ICONS = {"BTC":"₿","ETH":"Ξ","USDT":"₮","SOL":"◎","TON":"⬦","BNB":"◈","MATIC":"◆","AVAX":"▲"}
COINGECKO = {
    "BTC":"bitcoin","ETH":"ethereum","USDT":"tether","SOL":"solana",
    "TON":"the-open-network","BNB":"binancecoin","MATIC":"matic-network","AVAX":"avalanche-2"
}
FALLBACK_PRICES = {
    "USD":{"BTC":85420,"ETH":3320,"USDT":1.0,"SOL":182,"TON":5.4,"BNB":580,"MATIC":0.85,"AVAX":36},
    "EUR":{"BTC":79500,"ETH":3090,"USDT":0.93,"SOL":169,"TON":5.0,"BNB":540,"MATIC":0.79,"AVAX":33},
    "RUB":{"BTC":7860000,"ETH":305000,"USDT":92,"SOL":16750,"TON":497,"BNB":53400,"MATIC":78,"AVAX":3310},
    "GBP":{"BTC":68300,"ETH":2660,"USDT":0.80,"SOL":145,"TON":4.3,"BNB":464,"MATIC":0.68,"AVAX":29},
}
FALLBACK_CHANGES = {"BTC":-1.2,"ETH":2.4,"USDT":0.01,"SOL":5.1,"TON":3.3,"BNB":1.8,"MATIC":-2.1,"AVAX":0.7}

# ─── DATABASE ─────────────────────────────────────────────────────────
_db_lock = threading.Lock()

def load_db() -> dict:
    with _db_lock:
        try:
            with open(DB_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"users": {}, "txs": [], "prices_cache": {}, "prices_ts": 0}

def save_db(db: dict):
    with _db_lock:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)

def get_user(db: dict, uid: int) -> dict:
    sid = str(uid)
    if sid not in db["users"]:
        db["users"][sid] = {
            "id": uid, "username": None, "first_name": "Гость",
            "wallets": [],  # будет заполнено из WebApp
            "registered": _now(), "last_seen": _now(),
        }
    return db["users"][sid]

def _now() -> str:
    return datetime.utcnow().isoformat()

def _rnd_hex(n: int) -> str:
    import random
    return ''.join(random.choices('0123456789abcdef', k=n))

def active_wallet(user: dict) -> Optional[dict]:
    wallets = user.get("wallets", [])
    awid = user.get("active_wallet_id")
    for w in wallets:
        if w["id"] == awid:
            return w
    return wallets[0] if wallets else None

# ─── PRICE CACHE ──────────────────────────────────────────────────────
_price_cache = {"data": None, "ts": 0}
_PRICE_TTL = 120  # seconds

def get_prices() -> dict:
    now = time.time()
    if _price_cache["data"] and now - _price_cache["ts"] < _PRICE_TTL:
        return _price_cache["data"]
    try:
        ids = ",".join(COINGECKO.values())
        vs  = "usd,eur,rub,gbp"
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies={vs}&include_24hr_change=true"
        r = req.get(url, timeout=8)
        r.raise_for_status()
        raw = r.json()
        prices  = {"USD":{},"EUR":{},"RUB":{},"GBP":{}}
        changes = {}
        for coin, cgid in COINGECKO.items():
            d = raw.get(cgid, {})
            prices["USD"][coin] = d.get("usd", FALLBACK_PRICES["USD"].get(coin,0))
            prices["EUR"][coin] = d.get("eur", FALLBACK_PRICES["EUR"].get(coin,0))
            prices["RUB"][coin] = d.get("rub", FALLBACK_PRICES["RUB"].get(coin,0))
            prices["GBP"][coin] = d.get("gbp", FALLBACK_PRICES["GBP"].get(coin,0))
            changes[coin] = round(d.get("usd_24h_change", FALLBACK_CHANGES.get(coin,0)), 2)
        result = {"prices": prices, "changes": changes, "ts": int(now), "source": "coingecko"}
        _price_cache["data"] = result
        _price_cache["ts"]   = now
        log.info("Prices fetched from CoinGecko")
        return result
    except Exception as e:
        log.warning(f"CoinGecko failed: {e}, using fallback")
        result = {"prices": FALLBACK_PRICES, "changes": FALLBACK_CHANGES, "ts": int(now), "source": "fallback"}
        if not _price_cache["data"]:
            _price_cache["data"] = result
            _price_cache["ts"]   = now
        return _price_cache["data"] or result

# ─── TELEGRAM initData VERIFICATION ──────────────────────────────────
def verify_init_data(init_data_raw: str) -> Optional[dict]:
    """Верифицирует Telegram WebApp initData и возвращает user dict."""
    try:
        params = dict(parse_qsl(init_data_raw, keep_blank_values=True))
        received_hash = params.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            return None
        # Check auth_date (max 1 hour old)
        auth_date = int(params.get("auth_date", 0))
        if time.time() - auth_date > 3600:
            return None
        user_json = params.get("user", "{}")
        return json.loads(user_json)
    except Exception:
        return None

def get_uid_from_request() -> Optional[int]:
    """Извлекает user_id из заголовка X-Init-Data или X-User-Id (dev mode)."""
    init_data = request.headers.get("X-Init-Data", "")
    if init_data:
        user = verify_init_data(init_data)
        if user:
            return int(user["id"])
    # Dev/fallback: просто передаём user_id (менее безопасно)
    uid_header = request.headers.get("X-User-Id", "")
    if uid_header and uid_header.isdigit():
        return int(uid_header)
    return None

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = get_uid_from_request()
        if uid is None:
            return jsonify({"error": "unauthorized"}), 401
        kwargs["auth_uid"] = uid
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = get_uid_from_request()
        if uid not in ADMIN_IDS:
            return jsonify({"error": "forbidden"}), 403
        kwargs["auth_uid"] = uid
        return f(*args, **kwargs)
    return decorated

# ─── FLASK APP ────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, origins=["*"])  # Allow GitHub Pages

@app.after_request
def add_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data, X-User-Id"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return resp

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "ts": _now()})

# ── PRICES ──────────────────────────────────────────────────────────
@app.route("/api/prices")
def api_prices():
    return jsonify(get_prices())

# ── USER DATA ────────────────────────────────────────────────────────
@app.route("/api/user/<int:uid>", methods=["GET"])
def api_get_user(uid):
    # Check auth: only the user themselves or admin can read
    auth_uid = get_uid_from_request()
    if auth_uid != uid and auth_uid not in ADMIN_IDS:
        return jsonify({"error": "forbidden"}), 403
    db = load_db()
    user = get_user(db, uid)
    save_db(db)
    return jsonify({"ok": True, "user": _safe_user(user)})

@app.route("/api/user/<int:uid>/sync", methods=["POST", "OPTIONS"])
def api_sync_user(uid):
    """WebApp отправляет сюда полный стейт при каждом изменении."""
    if request.method == "OPTIONS":
        return "", 204
    auth_uid = get_uid_from_request()
    if auth_uid != uid and auth_uid not in ADMIN_IDS:
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(force=True, silent=True) or {}
    db = load_db()
    user = get_user(db, uid)
    # Update user meta
    if "first_name" in body:  user["first_name"]  = body["first_name"]
    if "username"   in body:  user["username"]    = body["username"]
    if "photo_url"  in body:  user["photo_url"]   = body["photo_url"]
    # Sync wallets (replace)
    if "wallets" in body:
        user["wallets"] = body["wallets"]
    if "active_wallet_id" in body:
        user["active_wallet_id"] = body["active_wallet_id"]
    if "currency" in body:
        user["currency"] = body["currency"]
    user["last_seen"] = _now()
    save_db(db)
    return jsonify({"ok": True})

@app.route("/api/user/<int:uid>/tx", methods=["POST", "OPTIONS"])
def api_add_tx(uid):
    """Добавить транзакцию в историю на сервере."""
    if request.method == "OPTIONS":
        return "", 204
    auth_uid = get_uid_from_request()
    if auth_uid != uid and auth_uid not in ADMIN_IDS:
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(force=True, silent=True) or {}
    db   = load_db()
    tx   = {
        "id":          body.get("id", _rnd_hex(8)),
        "uid":         uid,
        "type":        body.get("type", "unknown"),
        "coin":        body.get("coin", ""),
        "amount":      float(body.get("amount", 0)),
        "usd":         float(body.get("usd", 0)),
        "wallet_id":   body.get("wallet_id", ""),
        "wallet_name": body.get("wallet_name", ""),
        "to":          body.get("to", ""),
        "note":        body.get("note", ""),
        "ts":          body.get("ts", _now()),
    }
    db["txs"].append(tx)
    if len(db["txs"]) > 10000:
        db["txs"] = db["txs"][-10000:]
    save_db(db)
    return jsonify({"ok": True, "tx": tx})

@app.route("/api/user/<int:uid>/txs", methods=["GET"])
def api_get_txs(uid):
    auth_uid = get_uid_from_request()
    if auth_uid != uid and auth_uid not in ADMIN_IDS:
        return jsonify({"error": "forbidden"}), 403
    db  = load_db()
    txs = [t for t in reversed(db["txs"]) if t["uid"] == uid]
    return jsonify({"ok": True, "txs": txs[:200]})

# ── ADMIN ENDPOINTS ──────────────────────────────────────────────────
@app.route("/api/admin/users", methods=["GET"])
def api_admin_users():
    auth_uid = get_uid_from_request()
    if auth_uid not in ADMIN_IDS:
        return jsonify({"error": "forbidden"}), 403
    db = load_db()
    users = []
    for u in db["users"].values():
        w = active_wallet(u)
        users.append({
            "id":         u["id"],
            "name":       u.get("first_name","?"),
            "username":   u.get("username"),
            "last_seen":  u.get("last_seen",""),
            "registered": u.get("registered",""),
            "wallet_count": len(u.get("wallets",[])),
            "balances":   w.get("balances",{}) if w else {},
        })
    return jsonify({"ok": True, "users": users, "total": len(users)})

@app.route("/api/admin/give", methods=["POST"])
def api_admin_give():
    """Выдать монеты пользователю (вызывается из бота)."""
    # Бот передаёт секретный ключ
    secret = request.headers.get("X-Admin-Secret", "")
    if secret != os.getenv("ADMIN_SECRET", "change_me_please"):
        return jsonify({"error": "forbidden"}), 403
    body  = request.get_json(force=True, silent=True) or {}
    uid   = int(body.get("uid", 0))
    coin  = body.get("coin", "").upper()
    amt   = float(body.get("amount", 0))
    mode  = body.get("mode", "add")  # "add" or "set"
    if not uid or coin not in COINS or amt < 0:
        return jsonify({"error": "bad params"}), 400
    db   = load_db()
    user = get_user(db, uid)
    w    = active_wallet(user)
    if not w:
        # Create default wallet if none
        w = {"id": _rnd_hex(8), "name": "Основной", "address": "0x"+_rnd_hex(40),
             "balances": {c: 0.0 for c in COINS}}
        user["wallets"] = [w]
        user["active_wallet_id"] = w["id"]
    old = w["balances"].get(coin, 0.0)
    if mode == "set":
        w["balances"][coin] = round(amt, 8)
    else:
        w["balances"][coin] = round(old + amt, 8)
    # Add tx record
    db["txs"].append({
        "id": _rnd_hex(8), "uid": uid, "type": "received",
        "coin": coin, "amount": amt, "usd": amt * get_prices()["prices"]["USD"].get(coin,0),
        "wallet_id": w["id"], "wallet_name": w.get("name",""),
        "to": "", "note": "Выдано администратором", "ts": _now()
    })
    save_db(db)
    return jsonify({"ok": True, "old": old, "new": w["balances"][coin], "coin": coin})

@app.route("/api/admin/stats", methods=["GET"])
def api_admin_stats():
    auth_uid = get_uid_from_request()
    if auth_uid not in ADMIN_IDS:
        return jsonify({"error": "forbidden"}), 403
    db = load_db()
    now_str = (datetime.utcnow()-timedelta(days=7)).isoformat()
    active_7d = sum(1 for u in db["users"].values() if u.get("last_seen","") > now_str)
    totals = {c: 0.0 for c in COINS}
    for u in db["users"].values():
        for w in u.get("wallets", []):
            for c, v in w.get("balances", {}).items():
                if c in totals:
                    totals[c] += v
    return jsonify({
        "ok": True,
        "total_users": len(db["users"]),
        "active_7d": active_7d,
        "total_txs": len(db["txs"]),
        "total_balances": {k: round(v,4) for k,v in totals.items()},
    })

def _safe_user(u: dict) -> dict:
    """Remove sensitive data before returning to client."""
    return {
        "id":              u["id"],
        "first_name":      u.get("first_name",""),
        "username":        u.get("username",""),
        "photo_url":       u.get("photo_url",""),
        "wallets":         u.get("wallets",[]),
        "active_wallet_id":u.get("active_wallet_id",""),
        "currency":        u.get("currency","USD"),
        "registered":      u.get("registered",""),
        "last_seen":       u.get("last_seen",""),
    }

# ─── TELEGRAM BOT ─────────────────────────────────────────────────────
def run_bot():
    """Запускаем бота в отдельном потоке."""
    try:
        from telegram import (Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo)
        from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
    except ImportError:
        log.warning("python-telegram-bot не установлен, бот не запущен")
        return

    ADMIN_SECRET = os.getenv("ADMIN_SECRET", "change_me_please")

    def _call_give(uid: int, coin: str, amount: float, mode: str = "add") -> dict:
        try:
            r = req.post(
                f"http://localhost:{PORT}/api/admin/give",
                json={"uid": uid, "coin": coin, "amount": amount, "mode": mode},
                headers={"X-Admin-Secret": ADMIN_SECRET},
                timeout=5
            )
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    def _find_user_in_db(query: str) -> Optional[dict]:
        db = load_db()
        q  = query.lstrip("@").lower()
        for u in db["users"].values():
            if str(u["id"]) == q: return u
            if (u.get("username","") or "").lower() == q: return u
        return None

    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        db = load_db()
        u  = update.effective_user
        user = get_user(db, u.id)
        user["username"]   = u.username
        user["first_name"] = u.first_name or "Гость"
        user["last_seen"]  = _now()
        save_db(db)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔓 Открыть VAULT", web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton("💼 Баланс", callback_data="cb_bal"),
             InlineKeyboardButton("📊 Курсы",  callback_data="cb_prices")],
        ])
        await update.message.reply_text(
            f"⬡ <b>VAULT</b> · Крипто-кошелёк\n\n"
            f"Привет, <b>{u.first_name or 'друг'}</b>! 👋\n"
            f"Нажми кнопку ниже чтобы открыть кошелёк.",
            parse_mode="HTML", reply_markup=kb
        )

    async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        db = load_db()
        user = get_user(db, update.effective_user.id)
        user["last_seen"] = _now()
        save_db(db)
        wallets = user.get("wallets", [])
        if not wallets:
            await update.message.reply_text(
                "💼 Кошелёк ещё не создан.\nОткрой VAULT и он появится автоматически!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔓 Открыть VAULT", web_app=WebAppInfo(url=WEBAPP_URL))
                ]])
            )
            return
        prices = get_prices()["prices"]["USD"]
        lines  = ["💼 <b>Твои кошельки</b>\n"]
        for w in wallets:
            is_active = w["id"] == user.get("active_wallet_id")
            mark = "▶ " if is_active else "  "
            total = sum((w.get("balances",{}).get(c,0))*prices.get(c,0) for c in COINS)
            lines.append(f"{mark}<b>{w['name']}</b>  ≈ <code>${total:.2f}</code>")
            for c in COINS:
                bal = w.get("balances",{}).get(c,0)
                if bal > 0.00001:
                    lines.append(f"    {ICONS[c]} {c}: <code>{bal:.6f}</code>")
            lines.append("")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔓 Открыть VAULT", web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton("🔄 Обновить", callback_data="cb_bal")],
        ])
        await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)

    async def cmd_prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        p = get_prices()
        usd = p["prices"]["USD"]
        chg = p["changes"]
        lines = [f"📊 <b>Курсы</b>  <i>{datetime.utcnow().strftime('%H:%M UTC')}</i>\n"]
        for c in COINS:
            price = usd.get(c,0)
            ch    = chg.get(c,0)
            arrow = "📈" if ch >= 0 else "📉"
            lines.append(f"{arrow} {ICONS[c]} <b>{c}</b>  <code>${price:,.2f}</code>  ({'+' if ch>=0 else ''}{ch}%)")
        src = "CoinGecko" if p.get("source")=="coingecko" else "Офлайн"
        lines.append(f"\n<i>Источник: {src}</i>")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить", callback_data="cb_prices")]])
        await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)

    async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("⛔"); return
        db = load_db()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Юзеры",    callback_data="adm_users"),
             InlineKeyboardButton("📊 Стата",    callback_data="adm_stats")],
            [InlineKeyboardButton("🎁 Выдать",   callback_data="adm_give"),
             InlineKeyboardButton("✏️ Установить",callback_data="adm_set")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="adm_bc")],
        ])
        await update.message.reply_text(
            f"🔧 <b>VAULT Admin</b>\n\n"
            f"👥 Юзеров: <b>{len(db['users'])}</b>  📋 TX: <b>{len(db['txs'])}</b>",
            parse_mode="HTML", reply_markup=kb
        )

    async def cmd_give(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("⛔"); return
        args = ctx.args  # /give @user COIN amount
        if len(args) < 3:
            await update.message.reply_text(
                "📌 <b>Использование:</b>\n<code>/give @username BTC 0.01</code>\n<code>/give USER_ID ETH 0.5</code>",
                parse_mode="HTML"); return
        target = _find_user_in_db(args[0])
        if not target:
            await update.message.reply_text(f"❌ Юзер '{args[0]}' не найден."); return
        coin = args[1].upper()
        if coin not in COINS:
            await update.message.reply_text(f"❌ Монета '{coin}' не поддерживается."); return
        try:
            amount = float(args[2]); assert amount > 0
        except:
            await update.message.reply_text("❌ Неверная сумма."); return
        result = _call_give(target["id"], coin, amount, "add")
        if result.get("ok"):
            name = target.get("first_name","?")
            uname = f"@{target['username']}" if target.get("username") else str(target["id"])
            # Notify user
            try:
                await ctx.bot.send_message(
                    target["id"],
                    f"💰 <b>Вам начислено!</b>\n\n"
                    f"{ICONS[coin]} <b>+{amount} {coin}</b>\n"
                    f"Баланс: <code>{result['old']:.6f}</code> → <code>{result['new']:.6f}</code>\n\n"
                    f"Открой VAULT чтобы увидеть обновлённый баланс 👇",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔓 Открыть VAULT", web_app=WebAppInfo(url=WEBAPP_URL))
                    ]])
                )
            except Exception as e:
                log.warning(f"Can't notify user {target['id']}: {e}")
            await update.message.reply_text(
                f"✅ <b>Выдано!</b>\n"
                f"{name} {uname}\n"
                f"{ICONS[coin]} {coin}: {result['old']:.6f} → <b>{result['new']:.6f}</b>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(f"❌ Ошибка: {result.get('error')}")

    async def cmd_setbalance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("⛔"); return
        args = ctx.args
        if len(args) < 3:
            await update.message.reply_text(
                "📌 <b>Использование:</b>\n<code>/setbalance @user BTC 1.5</code>",
                parse_mode="HTML"); return
        target = _find_user_in_db(args[0])
        if not target:
            await update.message.reply_text("❌ Юзер не найден."); return
        coin = args[1].upper()
        if coin not in COINS:
            await update.message.reply_text(f"❌ '{coin}' не поддерживается."); return
        try:
            amount = float(args[2]); assert amount >= 0
        except:
            await update.message.reply_text("❌ Неверная сумма."); return
        result = _call_give(target["id"], coin, amount, "set")
        if result.get("ok"):
            await update.message.reply_text(
                f"✅ {target.get('first_name','?')}: {coin} установлен в <b>{amount}</b>",
                parse_mode="HTML"
            )
            try:
                await ctx.bot.send_message(
                    target["id"],
                    f"💱 <b>Ваш баланс изменён администратором</b>\n\n"
                    f"{ICONS[coin]} {coin}: <code>{result['old']:.6f}</code> → <code>{result['new']:.6f}</code>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔓 Открыть VAULT", web_app=WebAppInfo(url=WEBAPP_URL))
                    ]])
                )
            except: pass
        else:
            await update.message.reply_text(f"❌ {result.get('error')}")

    async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("⛔"); return
        try:
            r = req.get(
                f"http://localhost:{PORT}/api/admin/stats",
                headers={"X-User-Id": str(ADMIN_IDS[0])},
                timeout=5
            ).json()
            tb = r.get("total_balances",{})
            lines = [
                "📊 <b>Статистика VAULT</b>\n",
                f"👥 Юзеров: <b>{r.get('total_users',0)}</b>",
                f"🟢 Активных (7д): <b>{r.get('active_7d',0)}</b>",
                f"📋 Транзакций: <b>{r.get('total_txs',0)}</b>",
                "\n<b>Суммарные балансы:</b>",
            ] + [f"  {ICONS[c]} {c}: <code>{tb.get(c,0):.4f}</code>" for c in COINS if tb.get(c,0)>0]
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")

    async def adm_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q    = update.callback_query
        uid  = q.from_user.id
        data = q.data
        await q.answer()

        if data == "cb_bal":
            db   = load_db()
            user = get_user(db, uid)
            wallets = user.get("wallets", [])
            if not wallets:
                await q.edit_message_text("💼 Кошелёк не найден. Открой VAULT!")
                return
            prices = get_prices()["prices"]["USD"]
            lines  = ["💼 <b>Твои кошельки</b>\n"]
            for w in wallets:
                is_active = w["id"] == user.get("active_wallet_id")
                total = sum((w.get("balances",{}).get(c,0))*prices.get(c,0) for c in COINS)
                mark = "▶ " if is_active else "  "
                lines.append(f"{mark}<b>{w['name']}</b>  ≈ <code>${total:.2f}</code>")
                for c in COINS:
                    bal = w.get("balances",{}).get(c,0)
                    if bal > 0.00001:
                        lines.append(f"    {ICONS[c]} {c}: <code>{bal:.6f}</code>")
                lines.append("")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔓 Открыть VAULT", web_app=WebAppInfo(url=WEBAPP_URL))],
                [InlineKeyboardButton("🔄 Обновить", callback_data="cb_bal")],
            ])
            try:
                await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
            except: pass

        elif data == "cb_prices":
            p   = get_prices()
            usd = p["prices"]["USD"]
            chg = p["changes"]
            lines = [f"📊 <b>Курсы</b>\n"]
            for c in COINS:
                pr = usd.get(c,0)
                ch = chg.get(c,0)
                lines.append(f"{'📈' if ch>=0 else '📉'} {ICONS[c]} <b>{c}</b>  <code>${pr:,.2f}</code>  ({'+' if ch>=0 else ''}{ch}%)")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить", callback_data="cb_prices")]])
            try:
                await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
            except: pass

        elif data == "adm_users" and uid in ADMIN_IDS:
            db    = load_db()
            users = list(db["users"].values())
            lines = [f"👥 <b>Юзеры ({len(users)})</b>\n"]
            for u in users[:20]:
                un = f"@{u['username']}" if u.get("username") else str(u["id"])
                w  = active_wallet(u)
                bal = f"₿{w['balances'].get('BTC',0):.4f}" if w else "—"
                lines.append(f"• <b>{u.get('first_name','?')}</b> {un}  {bal}")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="adm_back")]])
            try:
                await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
            except: pass

        elif data == "adm_stats" and uid in ADMIN_IDS:
            db = load_db()
            now_s = (datetime.utcnow()-timedelta(days=7)).isoformat()
            active = sum(1 for u in db["users"].values() if u.get("last_seen","")>now_s)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="adm_back")]])
            await q.edit_message_text(
                f"📊 <b>Статистика</b>\n\n"
                f"👥 Всего: <b>{len(db['users'])}</b>\n"
                f"🟢 Активных 7д: <b>{active}</b>\n"
                f"📋 TX: <b>{len(db['txs'])}</b>",
                parse_mode="HTML", reply_markup=kb
            )

        elif data == "adm_back" and uid in ADMIN_IDS:
            db = load_db()
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 Юзеры",    callback_data="adm_users"),
                 InlineKeyboardButton("📊 Стата",    callback_data="adm_stats")],
                [InlineKeyboardButton("🎁 Выдать",   callback_data="adm_give"),
                 InlineKeyboardButton("✏️ Установить",callback_data="adm_set")],
                [InlineKeyboardButton("📢 Broadcast", callback_data="adm_bc")],
            ])
            await q.edit_message_text(
                f"🔧 <b>VAULT Admin</b>\n\n"
                f"👥 Юзеров: <b>{len(db['users'])}</b>  📋 TX: <b>{len(db['txs'])}</b>",
                parse_mode="HTML", reply_markup=kb
            )

        elif data in ("adm_give","adm_set") and uid in ADMIN_IDS:
            action = "give" if data=="adm_give" else "set"
            ctx.user_data["adm_action"] = action
            ctx.user_data["adm_step"]   = "user"
            try:
                await q.edit_message_text(
                    f"{'🎁 Выдать' if action=='give' else '✏️ Установить'}\n\nВведи @username или ID:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="adm_back")]])
                )
            except: pass

        elif data == "adm_bc" and uid in ADMIN_IDS:
            ctx.user_data["adm_step"] = "bc_msg"
            try:
                await q.edit_message_text(
                    "📢 <b>Broadcast</b>\nВведи текст:",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="adm_back")]])
                )
            except: pass

    async def adm_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid  = update.effective_user.id
        if uid not in ADMIN_IDS: return
        step   = ctx.user_data.get("adm_step")
        action = ctx.user_data.get("adm_action","give")
        text   = update.message.text.strip()

        if step == "user":
            target = _find_user_in_db(text)
            if not target:
                await update.message.reply_text("❌ Юзер не найден. Попробуй снова:"); return
            ctx.user_data["adm_target"] = target
            coins_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{ICONS[c]} {c}", callback_data=f"adm_coin_{c}") for c in COINS[:4]],
                [InlineKeyboardButton(f"{ICONS[c]} {c}", callback_data=f"adm_coin_{c}") for c in COINS[4:]],
            ])
            await update.message.reply_text(
                f"✅ {target.get('first_name','?')}\nВыбери монету:",
                reply_markup=coins_kb
            )
            ctx.user_data["adm_step"] = "coin_wait"

        elif step == "amount":
            try:
                amount = float(text); assert amount >= 0
            except:
                await update.message.reply_text("❌ Введи число:"); return
            target = ctx.user_data.get("adm_target")
            coin   = ctx.user_data.get("adm_coin")
            result = _call_give(target["id"], coin, amount, "add" if action=="give" else "set")
            ctx.user_data["adm_step"] = None
            if result.get("ok"):
                await update.message.reply_text(
                    f"✅ {'Выдано' if action=='give' else 'Установлено'}: <b>{amount} {coin}</b> → {target.get('first_name','?')}",
                    parse_mode="HTML"
                )
                try:
                    await ctx.bot.send_message(
                        target["id"],
                        f"💰 <b>Баланс обновлён!</b>\n{ICONS[coin]} {coin}: {result['old']:.6f} → <b>{result['new']:.6f}</b>",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔓 Открыть VAULT",web_app=WebAppInfo(url=WEBAPP_URL))]])
                    )
                except: pass
            else:
                await update.message.reply_text(f"❌ {result.get('error')}")

        elif step == "bc_msg":
            db   = load_db()
            uids = list(db["users"].keys())
            sent = 0
            for u_str in uids:
                try:
                    await ctx.bot.send_message(int(u_str), f"📢 {text}", parse_mode="HTML")
                    sent += 1
                except: pass
            ctx.user_data["adm_step"] = None
            await update.message.reply_text(f"✅ Отправлено {sent}/{len(uids)}")

    async def adm_coin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q    = update.callback_query
        await q.answer()
        if q.from_user.id not in ADMIN_IDS: return
        coin = q.data.split("_")[-1]
        ctx.user_data["adm_coin"] = coin
        ctx.user_data["adm_step"] = "amount"
        action = ctx.user_data.get("adm_action","give")
        await q.edit_message_text(
            f"{'Выдать' if action=='give' else 'Установить'} <b>{coin}</b> для <b>{ctx.user_data['adm_target'].get('first_name','?')}</b>\n\n"
            f"{'Введи количество:' if action=='give' else 'Введи новый баланс:'}",
            parse_mode="HTML"
        )

    async def run():
        telegram_app = Application.builder().token(BOT_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start",       cmd_start))
        telegram_app.add_handler(CommandHandler("balance",     cmd_balance))
        telegram_app.add_handler(CommandHandler("prices",      cmd_prices))
        telegram_app.add_handler(CommandHandler("admin",       cmd_admin))
        telegram_app.add_handler(CommandHandler("give",        cmd_give))
        telegram_app.add_handler(CommandHandler("setbalance",  cmd_setbalance))
        telegram_app.add_handler(CommandHandler("stats",       cmd_stats))
        telegram_app.add_handler(CallbackQueryHandler(adm_coin_cb, pattern=r"^adm_coin_"))
        telegram_app.add_handler(CallbackQueryHandler(adm_cb))
        telegram_app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_IDS),
            adm_text
        ))
        log.info("Bot started")
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        # Keep alive
        while True:
            await asyncio.sleep(3600)

    # Run in new event loop in thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run())

if __name__ == "__main__":
    log.info(f"Starting VAULT server on port {PORT}")
    log.info(f"Admins: {ADMIN_IDS}")
    log.info(f"WebApp: {WEBAPP_URL}")
    # Warm up prices
    threading.Thread(target=get_prices, daemon=True).start()
    # Start bot in background thread
    if BOT_TOKEN != "YOUR_BOT_TOKEN_HERE":
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
    else:
        log.warning("BOT_TOKEN not set, bot disabled")
    # Start Flask
    app.run(host="0.0.0.0", port=PORT, debug=False)
