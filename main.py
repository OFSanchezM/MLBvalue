import os
import logging
import asyncio
import httpx
import aiosqlite
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ================= CONFIG =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DB_PATH = os.getenv("DB_PATH", "bot.db")
MLB = "https://statsapi.mlb.com/api/v1"

# ================= DB =================
async def init_db(_app=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS historial (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT DEFAULT (datetime('now')),
            game_id TEXT UNIQUE,
            local TEXT,
            visitante TEXT,
            pick TEXT,
            cuota REAL,
            prob REAL,
            value REAL,
            resultado TEXT DEFAULT 'PENDIENTE',
            profit REAL DEFAULT 0
        )
        """)
        await db.commit()
    logger.info("DB inicializada correctamente")

# ================= HTTP =================
async def get_json(url, params=None):
    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

# ================= STATS =================
async def era(pid):
    if not pid:
        logger.debug("Pitcher ID no disponible, usando ERA por defecto")
        return 4.50
    try:
        d = await get_json(f"{MLB}/people/{pid}?hydrate=stats(group=[pitching],type=[season])")
        return float(d["people"][0]["stats"][0]["splits"][0]["stat"]["era"])
    except Exception as e:
        logger.warning(f"Error obteniendo ERA para pitcher {pid}: {e}")
        return 4.50

async def ops(team):
    try:
        d = await get_json(f"{MLB}/teams/{team}/stats", {"stats": "season", "group": "hitting"})
        return float(d["stats"][0]["splits"][0]["stat"]["ops"])
    except Exception as e:
        logger.warning(f"Error obteniendo OPS para equipo {team}: {e}")
        return 0.720

async def bullpen(team):
    try:
        d = await get_json(
            f"{MLB}/teams/{team}/stats",
            {"stats": "season", "group": "pitching", "pitcherStat": "relief"}
        )
        return float(d["stats"][0]["splits"][0]["stat"]["era"])
    except Exception as e:
        logger.warning(f"Error obteniendo bullpen ERA para equipo {team}: {e}")
        return 4.20

# ================= CONTEXTO =================
async def racha(team):
    start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        data = await get_json(f"{MLB}/schedule", {"sportId": 1, "startDate": start})
    except Exception as e:
        logger.warning(f"Error obteniendo racha para equipo {team}: {e}")
        return 0.5

    wins = 0
    games = 0
    for d in data.get("dates", []):
        for g in d.get("games", []):
            if g["status"]["detailedState"] == "Final":
                h = g["teams"]["home"]
                a = g["teams"]["away"]
                if h["team"]["id"] == team:
                    games += 1
                    if h["isWinner"]:
                        wins += 1
                if a["team"]["id"] == team:
                    games += 1
                    if a["isWinner"]:
                        wins += 1

    return wins / games if games else 0.5

async def resultado_ayer(team):
    ayer = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        data = await get_json(f"{MLB}/schedule", {"sportId": 1, "date": ayer})
    except Exception as e:
        logger.warning(f"Error obteniendo resultado de ayer para equipo {team}: {e}")
        return None

    for d in data.get("dates", []):
        for g in d.get("games", []):
            if g["status"]["detailedState"] == "Final":
                h = g["teams"]["home"]
                a = g["teams"]["away"]
                if h["team"]["id"] == team:
                    return h["isWinner"]
                if a["team"]["id"] == team:
                    return a["isWinner"]
    return None

# ================= MODELO =================
def modelo(era_h, era_a, ops_h, ops_a, bp_h, bp_a, r_h, r_a):
    score = 0.5
    score += (era_a - era_h) * 0.10
    score += (ops_h - ops_a) * 1.8
    score += (bp_a - bp_h) * 0.06
    score += (r_h - r_a) * 0.25
    return max(min(score, 0.80), 0.20)

def value(prob, cuota):
    return prob - (1 / cuota)

# ================= ODDS =================
async def get_odds():
    try:
        return await get_json(
            "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds",
            {"apiKey": ODDS_API_KEY, "regions": "eu", "markets": "h2h"}
        )
    except Exception as e:
        logger.error(f"Error obteniendo odds: {e}")
        return []

def cuota(home, data):
    for g in data:
        try:
            if g["home_team"].lower() == home.lower():
                o = g["bookmakers"][0]["markets"][0]["outcomes"]
                return o[0]["price"], o[1]["price"]
        except Exception:
            pass
    return None, None

# ================= ANALISIS =================
async def analizar(g, odds_data):
    h = g["teams"]["home"]["team"]
    a = g["teams"]["away"]["team"]
    game_id = str(g.get("gamePk", ""))

    ch, ca = cuota(h["name"], odds_data)
    if not ch:
        logger.debug(f"Sin odds para {h['name']} vs {a['name']}, se omite")
        return None

    pid_h = g["teams"]["home"].get("probablePitcher", {}).get("id")
    pid_a = g["teams"]["away"].get("probablePitcher", {}).get("id")

    (era_h, era_a, ops_h, ops_a, bp_h, bp_a, rh, ra, res_h, res_a) = await asyncio.gather(
        era(pid_h), era(pid_a),
        ops(h["id"]), ops(a["id"]),
        bullpen(h["id"]), bullpen(a["id"]),
        racha(h["id"]), racha(a["id"]),
        resultado_ayer(h["id"]), resultado_ayer(a["id"])
    )

    ph = modelo(era_h, era_a, ops_h, ops_a, bp_h, bp_a, rh, ra)
    pa = 1 - ph

    if res_h is False:
        ph *= 1.15
    if res_a is False:
        pa *= 1.15

    ph, pa = ph / (ph + pa), pa / (ph + pa)
    vh, va = value(ph, ch), value(pa, ca)

    if vh > va:
        return h["name"], a["name"], h["name"], ch, vh, game_id
    else:
        return h["name"], a["name"], a["name"], ca, va, game_id

# ================= PERSISTENCIA =================
async def guardar_pick(local, visitante, pick, cuota_val, prob, val, game_id):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR IGNORE INTO historial (game_id, local, visitante, pick, cuota, prob, value)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (game_id, local, visitante, pick, cuota_val, prob, val))
            await db.commit()
        logger.info(f"Pick guardado: {pick} @ {cuota_val} (value {round(val*100,1)}%)")
    except Exception as e:
        logger.error(f"Error guardando pick {game_id}: {e}")

async def actualizar_resultados():
    ayer = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, game_id, pick, cuota FROM historial
            WHERE resultado = 'PENDIENTE' AND date(fecha) = ?
        """, (ayer,)) as cursor:
            pendientes = await cursor.fetchall()

    if not pendientes:
        logger.info("Sin picks pendientes para actualizar")
        return

    try:
        schedule = await get_json(f"{MLB}/schedule", {"sportId": 1, "date": ayer})
    except Exception as e:
        logger.error(f"Error obteniendo schedule de ayer: {e}")
        return

    resultados_map = {}
    for d in schedule.get("dates", []):
        for g in d.get("games", []):
            if g["status"]["detailedState"] == "Final":
                gid = str(g["gamePk"])
                h = g["teams"]["home"]
                a = g["teams"]["away"]
                resultados_map[gid] = h["team"]["name"] if h["isWinner"] else a["team"]["name"]

    async with aiosqlite.connect(DB_PATH) as db:
        for row in pendientes:
            winner = resultados_map.get(row["game_id"])
            if not winner:
                logger.warning(f"Sin resultado para game_id {row['game_id']}")
                continue

            if winner.lower() == row["pick"].lower():
                resultado, profit = "WIN", round(row["cuota"] - 1, 3)
            else:
                resultado, profit = "LOSS", -1.0

            await db.execute(
                "UPDATE historial SET resultado = ?, profit = ? WHERE id = ?",
                (resultado, profit, row["id"])
            )
            logger.info(f"Pick {row['id']}: {resultado} | profit: {profit}")
        await db.commit()

# ================= TELEGRAM =================
menu = ReplyKeyboardMarkup([
    ["📈 Picks", "📊 ROI"],
    ["📜 Historial"]
], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔥 SISTEMA PRO ACTIVO", reply_markup=menu)

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Analizando partidos...")

    try:
        schedule_data = await get_json(f"{MLB}/schedule", {"sportId": "1"})
    except Exception as e:
        logger.error(f"Error obteniendo schedule: {e}")
        await update.message.reply_text("❌ Error obteniendo el calendario. Intentá más tarde.")
        return

    odds_data = await get_odds()
    if not odds_data:
        await update.message.reply_text("❌ Error obteniendo las cuotas. Intentá más tarde.")
        return

    txt = "🔥 PICKS DEL DÍA\n\n"
    picks_encontrados = 0

    for d in schedule_data.get("dates", []):
        for g in d.get("games", []):
            try:
                r = await analizar(g, odds_data)
            except Exception as e:
                logger.error(f"Error analizando partido: {e}")
                continue

            if not r:
                continue

            local, visitante, pick, cu, val, game_id = r

            if val < 0.03:
                continue

            picks_encontrados += 1
            txt += f"🏟 {visitante} @ {local}\n"
            txt += f"🏆 Pick: {pick}\n"
            txt += f"💰 Cuota: {cu} | Value: {round(val*100, 1)}%\n\n"

            await guardar_pick(local, visitante, pick, cu, 0.0, val, game_id)

    if picks_encontrados == 0:
        txt += "Sin picks con value suficiente hoy."

    await update.message.reply_text(txt)

async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM historial ORDER BY id DESC LIMIT 10"
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await update.message.reply_text("📜 Sin historial todavía.")
        return

    txt = "📜 ÚLTIMOS 10 PICKS\n\n"
    for r in rows:
        emoji = "✅" if r["resultado"] == "WIN" else ("❌" if r["resultado"] == "LOSS" else "⏳")
        profit_str = f"+{r['profit']:.2f}u" if r["profit"] > 0 else f"{r['profit']:.2f}u"
        txt += f"{emoji} {r['local']} vs {r['visitante']}\n"
        txt += f"   Pick: {r['pick']} @ {r['cuota']} → {r['resultado']} ({profit_str})\n\n"

    await update.message.reply_text(txt)

async def cmd_roi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT resultado, profit FROM historial WHERE resultado != 'PENDIENTE'"
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await update.message.reply_text("📊 Sin datos resueltos todavía.")
        return

    total_profit = sum(r["profit"] for r in rows)
    total = len(rows)
    wins = sum(1 for r in rows if r["resultado"] == "WIN")
    winrate = round(wins / total * 100, 1) if total else 0

    txt = (
        f"📊 ESTADÍSTICAS\n\n"
        f"Total picks: {total}\n"
        f"Ganados: {wins} ({winrate}%)\n"
        f"Profit total: {'+' if total_profit >= 0 else ''}{round(total_profit, 2)}u\n"
        f"ROI: {round(total_profit / total * 100, 1)}%"
    )
    await update.message.reply_text(txt)

# ================= JOB DIARIO =================
async def job_actualizar_resultados(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Ejecutando actualizacion diaria de resultados...")
    await actualizar_resultados()

# ================= MAIN =================
def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(init_db)   # init_db corre dentro del event loop de PTB
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)picks"), cmd_picks))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)historial"), cmd_historial))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)roi"), cmd_roi))

    app.job_queue.run_daily(
        job_actualizar_resultados,
        time=datetime.strptime("08:00", "%H:%M").time()
    )

    logger.info("Bot iniciado")
    app.run_polling()   # sincronico — PTB maneja su propio event loop

if __name__ == "__main__":
    main()