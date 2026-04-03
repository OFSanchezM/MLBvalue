import logging
import os
import aiosqlite
import httpx
from datetime import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup
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

# MENU
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

# UTIL
async def get_json(url):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        return r.json()

async def get_odds():
    return await get_json(
        f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h"
    )

def value(prob, cuota):
    return (prob * cuota) - 1

# 🔥 CUOTAS CORRECTAS
def buscar_cuota(home, away, odds):
    for g in odds:
        try:
            if g["home_team"].lower() == home.lower() and g["away_team"].lower() == away.lower():
                outcomes = g["bookmakers"][0]["markets"][0]["outcomes"]

                cuota_h = None
                cuota_a = None

                for o in outcomes:
                    if o["name"].lower() == home.lower():
                        cuota_h = o["price"]
                    elif o["name"].lower() == away.lower():
                        cuota_a = o["price"]

                return cuota_h, cuota_a
        except:
            continue

    return None, None

# START
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

            keyboard.append([
                InlineKeyboardButton(f"{a} @ {h}", callback_data=f"game_{g['gamePk']}")
            ])

    if not keyboard:
        await update.message.reply_text("No hay partidos hoy.")
    else:
        await update.message.reply_text(
            "📅 Partidos de hoy:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# 🔥 DETALLE SIN ERROR
async def detalle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        gid = query.data.split("_")[1]

        sched = await get_json(f"{MLB_BASE}/schedule?sportId=1")

        game_data = None
        for d in sched.get("dates", []):
            for g in d.get("games", []):
                if str(g["gamePk"]) == gid:
                    game_data = g
                    break

        if not game_data:
            await query.edit_message_text("❌ Partido no encontrado")
            return

        h = game_data["teams"]["home"]["team"]["name"]
        a = game_data["teams"]["away"]["team"]["name"]

        odds = await get_odds()
        cuota_h, cuota_a = buscar_cuota(h, a, odds)

        if not cuota_h:
            await query.edit_message_text("❌ No hay cuotas")
            return

        # MODELO SIMPLE
        prob_h = 0.55
        prob_a = 0.45

        val_h = value(prob_h, cuota_h)
        val_a = value(prob_a, cuota_a)

        if val_h > val_a:
            pick, cuota, prob, val = h, cuota_h, prob_h, val_h
        else:
            pick, cuota, prob, val = a, cuota_a, prob_a, val_a

        texto = (
            f"⚾ {a} @ {h}\n\n"
            f"📊 Prob: {round(prob*100,1)}%\n"
            f"💰 Cuota: {cuota}\n"
            f"📈 Value: +{round(val*100,1)}%\n\n"
            f"🏆 PICK: {pick}"
        )

        # GUARDAR
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO picks (game, pick, cuota, prob, value, fecha) VALUES (?,?,?,?,?,?)",
                (f"{a}@{h}", pick, cuota, prob, val, datetime.now().isoformat())
            )
            await db.commit()

        await query.edit_message_text(texto)

    except Exception as e:
        logger.error(e)
        await query.edit_message_text("❌ Error cargando datos")

# 🔥 PICKS CON GANADOR + FILTRO
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

            # 🔥 FILTRO
            if cuota_h > 5 or cuota_a > 5:
                continue

            prob_h = 0.55
            prob_a = 0.45

            val_h = value(prob_h, cuota_h)
            val_a = value(prob_a, cuota_a)

            if val_h > val_a:
                pick, cuota, prob, val = h, cuota_h, prob_h, val_h
            else:
                pick, cuota, prob, val = a, cuota_a, prob_a, val_a

            if val > 0.05:
                texto += (
                    f"{a} @ {h}\n"
                    f"🏆 {pick}\n"
                    f"💰 {cuota} | 📈 +{round(val*100,1)}%\n\n"
                )

                # GUARDAR
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT INTO picks (game, pick, cuota, prob, value, fecha) VALUES (?,?,?,?,?,?)",
                        (f"{a}@{h}", pick, cuota, prob, val, datetime.now().isoformat())
                    )
                    await db.commit()

    if texto == "🔥 PICKS CON VALUE\n\n":
        texto = "No hay picks hoy"

    await update.message.reply_text(texto)

# HISTORIAL
async def historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT game, pick, cuota FROM picks ORDER BY id DESC LIMIT 5") as cur:
            rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text("Sin historial aún.")
        return

    texto = "📜 HISTORIAL\n\n"
    for r in rows:
        texto += f"{r[0]} → {r[1]} @{r[2]}\n"

    await update.message.reply_text(texto)

# MENU
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu))
    app.add_handler(CallbackQueryHandler(detalle, pattern="game_"))

    logger.info("BOT INICIADO")
    app.run_polling()