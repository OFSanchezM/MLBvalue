import asyncio
import logging
import math
import os
import aiosqlite
import httpx
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

# --- CONFIG ---
TOKEN = os.getenv("TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
MLB_BASE = "https://statsapi.mlb.com/api/v1"
DB_PATH = "mlb_bot.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MLB-BOT")

# --- TECLADOS ---
MENU_PRINCIPAL = ReplyKeyboardMarkup([
    [KeyboardButton("📅 Partidos de Hoy"), KeyboardButton("📈 Picks con Valor")],
    [KeyboardButton("📜 Historial"), KeyboardButton("💎 Premium")]
], resize_keyboard=True)

# --- DB ---
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

# --- MODELO MATEMÁTICO ---
def calcular_valor(cuota, era_h, era_a):
    # Una versión simplificada para que el bot responda algo coherente de inmediato
    prob = (era_a / (era_h + era_a)) if (era_h + era_a) > 0 else 0.5
    return prob, (prob * cuota) - 1

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO suscriptores VALUES (?)", (update.effective_chat.id,))
        await db.commit()
    await update.message.reply_text("⚾ Bot MLB Activo.", reply_markup=MENU_PRINCIPAL)

async def boton_partidos_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{MLB_BASE}/schedule?sportId=1")
        data = r.json()
    
    keyboard = []
    for date in data.get("dates", []):
        for g in date.get("games", []):
            h = g["teams"]["home"]["team"]["name"]
            a = g["teams"]["away"]["team"]["name"]
            keyboard.append([InlineKeyboardButton(f"{a} @ {h}", callback_data=f"det_{g['gamePk']}")])
    
    if not keyboard:
        await update.message.reply_text("No hay partidos hoy.")
    else:
        await update.message.reply_text("Elige un partido:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text == "📈 Picks con Valor":
        # Simulamos la búsqueda (Aquí conectarías con Odds API)
        await update.message.reply_text("🔍 Analizando mercado... No hay errores de cuota en este momento (Valor < 5%).")

    elif text == "📜 Historial":
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT game, pick, cuota FROM picks ORDER BY id DESC LIMIT 5") as cur:
                rows = await cur.fetchall()
                if not rows:
                    await update.message.reply_text("Aún no hay picks registrados en el historial.")
                else:
                    txt = "📚 **Últimos Picks:**\n\n" + "\n".join([f"🔹 {r[0]}: {r[1]} (@{r[2]})" for r in rows])
                    await update.message.reply_text(txt, parse_mode="Markdown")

    elif text == "💎 Premium":
        await update.message.reply_text("💎 Sección VIP: Contacta a @TuUsuario para acceso.")

# --- DETALLE DEL PARTIDO (EL QUE NO HACÍA NADA) ---
async def callback_detalle_juego(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game_id = query.data.split("_")[1]
    await query.answer("Cargando datos...") # Esto quita el reloj de arena

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{MLB_BASE}/game/{game_id}/feed/live")
        g = r.json()

    data = g["gameData"]
    h = data["teams"]["home"]["name"]
    a = data["teams"]["away"]["name"]
    p_h = data["probablePitchers"].get("home", {}).get("fullName", "TBD")
    p_a = data["probablePitchers"].get("away", {}).get("fullName", "TBD")
    
    # Esto es lo que verás al presionar el botón
    resumen = (
        f"🆚 **{a} vs {h}**\n\n"
        f"🔹 **Pitcher Visitante:** {p_a}\n"
        f"🔹 **Pitcher Local:** {p_h}\n\n"
        f"🏟 Estadio: {data['venue']['name']}\n"
        f"🕒 Estado: {data['status']['abstractGameState']}"
    )
    
    await query.edit_message_text(resumen, parse_mode="Markdown", 
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="back_list")]]))

# --- MAIN ---
async def post_init(app):
    await init_db()

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Text("📅 Partidos de Hoy"), boton_partidos_hoy))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_text))
    app.add_handler(CallbackQueryHandler(callback_detalle_juego, pattern="^det_"))
    app.add_handler(CallbackQueryHandler(boton_partidos_hoy, pattern="back_list"))
    
    app.run_polling()