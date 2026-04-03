import asyncio
import logging
import math
import os
import aiosqlite
import httpx
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

# --- CONFIGURACIÓN ---
TOKEN = os.getenv("TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
MLB_BASE = "https://statsapi.mlb.com/api/v1"
DB_PATH = "mlb_bot.db"  # Recuerda usar /data/mlb_bot.db si usas Volumen en Railway
VALUE_MINIMO = 0.05

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("MLB-BOT")

# --- TECLADOS ---
MENU_PRINCIPAL = ReplyKeyboardMarkup([
    [KeyboardButton("📅 Partidos de Hoy"), KeyboardButton("📈 Picks con Valor")],
    [KeyboardButton("📜 Historial"), KeyboardButton("💎 Premium")]
], resize_keyboard=True)

# --- BASE DE DATOS ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS suscriptores (chat_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS juegos_alertados (game_id INTEGER PRIMARY KEY, pitcher_h TEXT, pitcher_a TEXT);
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game TEXT, pick TEXT, cuota REAL, prob REAL, value REAL, fecha TEXT
        );
        """)
        await db.commit()

# --- UTILIDADES DE API ---
async def fetch_json(url, params=None):
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Error API {url}: {e}")
            return None

# --- LÓGICA DE MODELO MATEMÁTICO ---
def calcular_probabilidad(era_h, era_a, bp_h, bp_a, ops_h, ops_a, mano_contraria, ops_propio):
    # Modelo basado en Logaritmos de ERA y eficiencia de OPS
    p_base = (math.log(era_a + 0.1) / (math.log(era_h + 0.1) + math.log(era_a + 0.1))) * 0.4 + \
             (math.log(bp_a + 0.1) / (math.log(bp_h + 0.1) + math.log(bp_a + 0.1))) * 0.3 + \
             (ops_h / (ops_h + ops_a + 0.1)) * 0.3
    
    # Ajuste por mano del Pitcher (L/R)
    factor_ajuste = 0.8 if mano_contraria == "L" else 0.5
    p_final = p_base + (ops_propio - 0.720) * factor_ajuste
    return max(min(p_final, 0.92), 0.08)

# --- MANEJADORES DE COMANDOS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO suscriptores VALUES (?)", (update.effective_chat.id,))
        await db.commit()
    await update.message.reply_text(
        "⚾ **MLB Premium Analyzer**\nBienvenido. Explora los partidos o espera mis alertas de valor.",
        reply_markup=MENU_PRINCIPAL, parse_mode="Markdown"
    )

async def boton_partidos_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await fetch_json(f"{MLB_BASE}/schedule?sportId=1")
    if not data or not data.get("dates"):
        await update.message.reply_text("No hay partidos programados para hoy.")
        return

    keyboard = []
    for game in data["dates"][0]["games"]:
        h, a = game["teams"]["home"]["team"]["name"], game["teams"]["away"]["team"]["name"]
        gid = game["gamePk"]
        keyboard.append([InlineKeyboardButton(f"🆚 {a} @ {h}", callback_data=f"det_{gid}")])
    
    await update.message.reply_text("Selecciona un partido para analizar:", reply_markup=InlineKeyboardMarkup(keyboard))

# --- DETALLE INTERACTIVO ---
async def callback_detalle_juego(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    game_id = query.data.split("_")[1]
    
    # Obtenemos datos en tiempo real
    g = await fetch_json(f"{MLB_BASE}/game/{game_id}/feed/live")
    if not g: return

    try:
        data = g["gameData"]
        h_name, a_name = data["teams"]["home"]["name"], data["teams"]["away"]["name"]
        p_h = data["probablePitchers"].get("home", {}).get("fullName", "Por confirmar")
        p_a = data["probablePitchers"].get("away", {}).get("fullName", "Por confirmar")
        
        status = data["status"]["abstractGameState"]
        hora = data["datetime"].get("time", "")

        resumen = (
            f"🏟 **{a_name} @ {h_name}**\n"
            f"🕒 Estado: {status} ({hora})\n\n"
            f"👤 **P. Away:** {p_a}\n"
            f"👤 **P. Home:** {p_h}\n\n"
            "🔍 _Analizando estadísticas de abridores y bullpen..._"
        )
        
        # Botón para volver
        kb = [[InlineKeyboardButton("⬅️ Volver a la lista", callback_data="list_games")]]
        await query.edit_message_text(resumen, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    except:
        await query.edit_message_text("Datos no disponibles en este momento.")

# --- MONITOR DE ALERTAS (BACKGROUND) ---
async def monitor_task(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Escaneando cambios en lineups y cuotas...")
    # Aquí iría la lógica de comparación de pitchers y envío de alertas a 'suscriptores'
    # Si detectas un cambio de pitcher respecto a la DB -> Enviar alerta.
    pass

# --- OTROS MENÚS ---
async def handle_text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📈 Picks con Valor":
        await update.message.reply_text("Buscando errores en las cuotas... ⏳")
        # Lógica para mostrar solo juegos donde (Prob * Cuota) > 1.05
    elif text == "📜 Historial":
        await update.message.reply_text("Consultando últimos resultados... 📚")
    elif text == "💎 Premium":
        await update.message.reply_text("💎 **MLB VIP**\n- Alertas instantáneas\n- Stake sugerido\nContactar a @Admin")

# --- INICIO DEL APP ---
async def post_init(app):
    await init_db()
    app.job_queue.run_repeating(monitor_task, interval=600, first=10)

if __name__ == "__main__":
    if not TOKEN:
        logger.error("No se encontró el TOKEN de Telegram.")
        exit()

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Text("📅 Partidos de Hoy"), boton_partidos_hoy))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_menu))
    app.add_handler(CallbackQueryHandler(callback_detalle_juego, pattern="^det_"))
    app.add_handler(CallbackQueryHandler(boton_partidos_hoy, pattern="^list_games"))

    logger.info("Bot en línea y esperando interacciones.")
    app.run_polling()