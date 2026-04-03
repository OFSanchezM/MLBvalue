import asyncio
import logging
import math
import os
import aiosqlite
import httpx
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- CONFIGURACIÓN ---
TOKEN = os.getenv("TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

if not TOKEN or not ODDS_API_KEY:
    raise RuntimeError("Faltan variables de entorno: TOKEN o ODDS_API_KEY")

MLB_BASE = "https://statsapi.mlb.com/api/v1"
DB_PATH = "mlb_bot.db"
VALUE_MINIMO = 0.05
INTERVALO_MONITOR = 600

# Limitar peticiones simultáneas a la API (Evita baneos de IP)
MAX_CONCURRENT_REQUESTS = 5
api_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("MLB-BOT")

# --- BASE DE DATOS (ASÍNCRONA) ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS suscriptores (chat_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS juegos_alertados (game_id INTEGER PRIMARY KEY, alerted_at TEXT);
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game TEXT, pick TEXT, cuota REAL, prob REAL, value REAL, fecha TEXT
        );
        """)
        await db.commit()

async def get_subs():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id FROM suscriptores") as cursor:
            return [r[0] async for r in cursor]

async def add_sub(cid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO suscriptores VALUES (?)", (cid,))
        await db.commit()

async def del_sub(cid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM suscriptores WHERE chat_id=?", (cid,))
        await db.commit()

async def ya_alertado(gid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM juegos_alertados WHERE game_id=?", (gid,)) as cursor:
            return await cursor.fetchone() is not None

async def marcar_alerta(gid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO juegos_alertados VALUES (?,?)", 
                         (gid, datetime.now().isoformat()))
        await db.commit()

# --- HTTP HELPERS ---
async def _get_json(client, url, params=None):
    """Helper con semáforo y manejo de errores centralizado"""
    async with api_semaphore:
        try:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Error en petición a {url}: {e}")
            return None

# --- STATS DE MLB ---
async def get_pitcher_stats(client, pid):
    if not pid: return 4.50, "R"
    data = await _get_json(client, f"{MLB_BASE}/people/{pid}?hydrate=stats(group=[pitching],type=[season])")
    try:
        player = data["people"][0]
        era = float(player["stats"][0]["splits"][0]["stat"]["era"])
        hand = player["pitchHand"]["code"]
        return era, hand
    except (KeyError, IndexError, TypeError):
        return 4.50, "R"

async def get_team_stats(client, team_id):
    """Agrupa peticiones de equipo para mayor eficiencia"""
    # Usamos gather para obtener bullpen y OPS en paralelo
    urls = [
        f"{MLB_BASE}/teams/{team_id}/stats?stats=season&group=pitching&pitcherStat=relief",
        f"{MLB_BASE}/teams/{team_id}/stats?stats=season&group=hitting"
    ]
    results = await asyncio.gather(*[_get_json(client, url) for url in urls])
    
    try:
        bp_era = float(results[0]["stats"][0]["splits"][0]["stat"]["era"])
    except: bp_era = 4.20
    
    try:
        team_ops = float(results[1]["stats"][0]["splits"][0]["stat"]["ops"])
    except: team_ops = 0.720
    
    return bp_era, team_ops

async def get_win_pct(client, team_id):
    data = await _get_json(client, f"{MLB_BASE}/standings?leagueId=103,104")
    if not data: return 0.5
    try:
        for record in data.get("records", []):
            for t in record.get("teamRecords", []):
                if t["team"]["id"] == team_id:
                    return float(t["winningPercentage"])
    except: pass
    return 0.5

# --- MODELO LÓGICO ---
def calculate_ratio(a, b, invert=False):
    try:
        # Evitamos log(0) sumando un pequeño epsilon
        log_a, log_b = math.log(a + 0.01), math.log(b + 0.01)
        if invert:
            return log_b / (log_a + log_b)
        return log_a / (log_a + log_b)
    except: return 0.5

def prob_model(stats_h, stats_a):
    # Desempaquetamos tuplas de stats
    era_h, bp_h, ops_h, win_h = stats_h
    era_a, bp_a, ops_a, win_a = stats_a
    
    p = (
        calculate_ratio(era_h, era_a, True) * 0.30 +
        calculate_ratio(bp_h, bp_a, True) * 0.25 +
        calculate_ratio(ops_h, ops_a) * 0.25 +
        calculate_ratio(win_h, win_a) * 0.15 +
        0.54 * 0.05 # Home Field Advantage
    )
    return p

def ajustar_prob(prob, opponent_hand, team_ops):
    multiplier = 0.8 if opponent_hand == "L" else 0.5
    prob += (team_ops - 0.720) * multiplier
    return max(min(prob, 0.92), 0.08)

# --- PROCESO PRINCIPAL ---
async def monitor(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando escaneo de juegos...")
    
    # Cliente persistente para toda la ráfaga de peticiones
    async with httpx.AsyncClient(timeout=15) as client:
        odds_data = await _get_json(client, "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds", 
                                  params={"apiKey": ODDS_API_KEY, "regions": "eu", "markets": "h2h"})
        sched = await _get_json(client, f"{MLB_BASE}/schedule?sportId=1")
        
        if not odds_data or not sched:
            logger.warning("No se pudo obtener datos de las APIs.")
            return

        subs = await get_subs()
        if not subs: return

        for date in sched.get("dates", []):
            for game in date.get("games", []):
                gid = game["gamePk"]
                if await ya_alertado(gid): continue

                h_team = game["teams"]["home"]
                a_team = game["teams"]["away"]
                h_name, a_name = h_team["team"]["name"], a_team["team"]["name"]

                # Buscar cuotas
                ch, ca = None, None
                for o_game in odds_data:
                    if h_name.lower() in o_game["home_team"].lower():
                        try:
                            outcomes = o_game["bookmakers"][0]["markets"][0]["outcomes"]
                            # Asegurar que el orden sea correcto (Home vs Away)
                            for out in outcomes:
                                if out["name"].lower() in h_name.lower(): ch = out["price"]
                                else: ca = out["price"]
                        except: continue
                
                if not ch or not ca: continue

                # Recopilación masiva de datos (Asíncrona y Paralela)
                h_pid = h_team.get("probablePitcher", {}).get("id")
                a_pid = a_team.get("probablePitcher", {}).get("id")

                # Ejecutamos todo lo necesario para este juego en paralelo
                results = await asyncio.gather(
                    get_pitcher_stats(client, h_pid),
                    get_pitcher_stats(client, a_pid),
                    get_team_stats(client, h_team["team"]["id"]),
                    get_team_stats(client, a_team["team"]["id"]),
                    get_win_pct(client, h_team["team"]["id"]),
                    get_win_pct(client, a_team["team"]["id"])
                )

                (era_h, mano_h), (era_a, mano_a), (bp_h, ops_h), (bp_a, ops_a), w_h, w_a = results

                # Modelo
                ph = prob_model((era_h, bp_h, ops_h, w_h), (era_a, bp_a, ops_a, w_a))
                pa = 1 - ph
                
                ph = ajustar_prob(ph, mano_a, ops_h)
                pa = ajustar_prob(pa, mano_h, ops_a)

                vh, va = ph * ch - 1, pa * ca - 1
                best_v = max(vh, va)

                if best_v >= VALUE_MINIMO:
                    pick, cuota, p = (h_name, ch, ph) if vh > va else (a_name, ca, pa)
                    
                    msg = (
                        f"🔥 *VALUE BET ENCONTRADA*\n\n"
                        f"⚾ {a_name} @ {h_name}\n"
                        f"📊 Probabilidad: `{round(p*100, 1)}%` (Cuota Justa: {round(1/p, 2)})\n"
                        f"💰 Cuota Bookie: `{cuota}`\n"
                        f"📈 **Value: +{round(best_v*100, 1)}%**\n\n"
                        f"🏆 **PICK: {pick}**\n\n"
                        f"🧠 _Pitchers:_ {era_h}({mano_h}) vs {era_a}({mano_a})\n"
                        f"📉 _Bullpen ERA:_ {bp_h} vs {bp_a}"
                    )

                    for s in subs:
                        try: await context.bot.send_message(s, msg, parse_mode="Markdown")
                        except: pass
                    
                    await marcar_alerta(gid)
                    logger.info(f"Alerta enviada para {h_name} vs {a_name}")

# --- COMANDOS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_sub(update.effective_chat.id)
    await update.message.reply_text("✅ Bot activado. Recibirás alertas cuando encuentre valor en la MLB.")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await del_sub(update.effective_chat.id)
    await update.message.reply_text("❌ Bot desactivado.")

async def post_init(app):
    await init_db()
    app.job_queue.run_repeating(monitor, interval=INTERVALO_MONITOR, first=5)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    
    logger.info("Bot iniciado...")
    app.run_polling()