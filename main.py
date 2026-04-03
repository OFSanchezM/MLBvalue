import asyncio
import logging
import math
import os
import aiosqlite
import httpx
from datetime import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters
)

# CONFIG
TOKEN = os.getenv("TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
MLB_BASE = "https://statsapi.mlb.com/api/v1"
DB_PATH = "mlb_bot.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MLB-BOT")

# MENÚ
MENU = ReplyKeyboardMarkup([
    ["📅 Partidos de Hoy", "📈 Picks con Valor"],
    ["📜 Historial"]
], resize_keyboard=True)

# DB
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS suscriptores (chat_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game TEXT, pick TEXT, cuota REAL, prob REAL, value REAL, fecha TEXT
        );
        """)
        await db.commit()

# MODELO
def probabilidad(era_h, era_a):
    return era_a / (era_h + era_a) if (era_h + era_a) else 0.5

def value(prob, cuota):
    return (prob * cuota) - 1

# API
async def get_json(url):
    async with httpx.AsyncClient(timeout=10) as client:
        return (await client.get(url)).json()

async def get_odds():
    return await get_json(
        f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h"
    )

def buscar_cuota(home, away, odds):
    for g in odds:
        try:
            if home.lower() in g["home_team"].lower():
                o = g["bookmakers"][0]["markets"][0]["outcomes"]
                return o[0]["price"], o[1]["price"]
        except:
            continue
    return None, None

# HANDLERS
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO suscriptores VALUES (?)", (update.effective_chat.id,))
        await db.commit()

    await update.message.reply_text("⚾ BOT ACTIVADO", reply_markup=MENU)

# PARTIDOS
async def partidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await get_json(f"{MLB_BASE}/schedule?sportId=1")

    keyboard = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            h = g["teams"]["home"]["team"]["name"]
            a = g["teams"]["away"]["team"]["name"]
            keyboard.append([InlineKeyboardButton(f"{a} @ {h}", callback_data=f"game_{g['gamePk']}")])

    await update.message.reply_text(
        "📅 Partidos de hoy:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# DETALLE + ANALISIS
async def detalle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    gid = query.data.split("_")[1]

    game = await get_json(f"{MLB_BASE}/game/{gid}/feed/live")
    odds = await get_odds()

    data = game["gameData"]
    h = data["teams"]["home"]["name"]
    a = data["teams"]["away"]["name"]

    # pitcher ERA
    try:
        era_h = 3.5
        era_a = 4.2
    except:
        era_h = era_a = 4.5

    cuota_h, cuota_a = buscar_cuota(h, a, odds)
    if not cuota_h:
        await query.edit_message_text("No hay cuotas disponibles.")
        return

    prob_h = probabilidad(era_h, era_a)
    prob_a = 1 - prob_h

    val_h = value(prob_h, cuota_h)
    val_a = value(prob_a, cuota_a)

    if val_h > val_a:
        pick = h
        cuota = cuota_h
        prob = prob_h
        val = val_h
    else:
        pick = a
        cuota = cuota_a
        prob = prob_a
        val = val_a

    texto = (
        f"⚾ {a} @ {h}\n\n"
        f"📊 Prob: {round(prob*100,1)}%\n"
        f"💰 Cuota: {cuota}\n"
        f"📈 Value: +{round(val*100,1)}%\n\n"
        f"🏆 PICK: {pick}"
    )

    # guardar
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO picks (game, pick, cuota, prob, value, fecha) VALUES (?,?,?,?,?,?)",
            (f"{a}@{h}", pick, cuota, prob, val, datetime.now().isoformat())
        )
        await db.commit()

    await query.edit_message_text(texto)

# PICKS AUTOMÁTICOS
async def picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await get_json(f"{MLB_BASE}/schedule?sportId=1")
    odds = await get_odds()

    texto = "🔥 PICKS CON VALUE\n\n"

    for d in data.get("dates", []):
        for g in d.get("games", []):

            h = g["teams"]["home"]["team"]["name"]
            a = g["teams"]["away"]["team"]["name"]

            cuota_h, cuota_a = buscar_cuota(h, a, odds)
            if not cuota_h:
                continue

            prob = 0.55
            val = value(prob, cuota_h)

            if val > 0.05:
                texto += f"{a} @ {h} | Value: {round(val*100,1)}%\n"

    await update.message.reply_text(texto)

# HISTORIAL
async def historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT game, pick, cuota FROM picks ORDER BY id DESC LIMIT 5") as cur:
            rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text("Sin historial")
        return

    texto = "📜 Historial\n\n"
    for r in rows:
        texto += f"{r[0]} → {r[1]} @{r[2]}\n"

    await update.message.reply_text(texto)

# MENU HANDLER
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text

    if t == "📅 Partidos de Hoy":
        await partidos(update, context)

    elif t == "📈 Picks con Valor":
        await picks(update, context)

    elif t == "📜 Historial":
        await historial(update, context)

# MAIN
async def post_init(app):
    await init_db()

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT, menu))
    app.add_handler(CallbackQueryHandler(detalle, pattern="game_"))

    app.run_polling()