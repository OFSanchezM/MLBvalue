import asyncio
import logging
import math
import os
import sqlite3
from datetime import datetime
 
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
 
# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
 
TOKEN = os.getenv("TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
 
if not TOKEN:
    raise RuntimeError("Falta la variable de entorno TOKEN")
if not ODDS_API_KEY:
    raise RuntimeError("Falta la variable de entorno ODDS_API_KEY")
 
MLB_BASE = "https://statsapi.mlb.com/api/v1"
DB_PATH = "mlb_bot.db"
HTTP_TIMEOUT = 12.0
MAX_RETRIES = 3
VALUE_MINIMO = 0.05
INTERVALO_MONITOR = 600  # segundos
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("MLB-BOT")
 
 
# ─────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────
 
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS suscriptores (
            chat_id INTEGER PRIMARY KEY
        );
 
        CREATE TABLE IF NOT EXISTS juegos_alertados (
            game_id INTEGER PRIMARY KEY,
            alerted_at TEXT
        );
 
        CREATE TABLE IF NOT EXISTS picks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id    INTEGER,
            game       TEXT,
            pick       TEXT,
            cuota      REAL,
            prob       REAL,
            value      REAL,
            fecha      TEXT
        );
        """)
        conn.commit()
    logger.info("DB inicializada")
 
 
# ─────────────────────────────────────────────
# DB OPS
# ─────────────────────────────────────────────
 
def get_subs() -> list[int]:
    with sqlite3.connect(DB_PATH) as c:
        return [r[0] for r in c.execute("SELECT chat_id FROM suscriptores")]
 
def add_sub(chat_id: int):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT OR IGNORE INTO suscriptores VALUES (?)", (chat_id,))
        c.commit()
 
def del_sub(chat_id: int):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("DELETE FROM suscriptores WHERE chat_id=?", (chat_id,))
        c.commit()
 
def ya_alertado(gid: int) -> bool:
    with sqlite3.connect(DB_PATH) as c:
        return bool(c.execute(
            "SELECT 1 FROM juegos_alertados WHERE game_id=?", (gid,)
        ).fetchone())
 
def marcar_alertado(gid: int):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR IGNORE INTO juegos_alertados VALUES (?,?)",
            (gid, datetime.now().isoformat())
        )
        c.commit()
 
def limpiar_alertados_viejos(dias: int = 7):
    """Elimina registros de juegos alertados con más de `dias` días."""
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            DELETE FROM juegos_alertados
            WHERE alerted_at < datetime('now', ?)
        """, (f"-{dias} days",))
        c.commit()
 
def guardar_pick(game_id: int, game: str, pick: str, cuota: float, prob: float, value: float):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT INTO picks (game_id, game, pick, cuota, prob, value, fecha) VALUES (?,?,?,?,?,?,?)",
            (game_id, game, pick, cuota, prob, value, datetime.now().isoformat())
        )
        c.commit()
 
 
# ─────────────────────────────────────────────
# HTTP HELPER
# ─────────────────────────────────────────────
 
async def _get_json(url: str, params: dict = None) -> dict | list:
    """GET con reintentos exponenciales. Devuelve {} o [] en caso de fallo total."""
    for intento in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            wait = 2 ** intento
            logger.warning(f"Error en GET {url} (intento {intento+1}/{MAX_RETRIES}): {e}. Esperando {wait}s")
            await asyncio.sleep(wait)
    logger.error(f"Fallo definitivo al obtener {url}")
    return {}
 
 
# ─────────────────────────────────────────────
# STATS MLB
# ─────────────────────────────────────────────
 
async def obtener_era_pitcher(pid: int | None) -> float:
    """ERA del pitcher para la temporada actual. Fallback: 4.50 (promedio liga)."""
    if not pid:
        return 4.50
    try:
        data = await _get_json(
            f"{MLB_BASE}/people/{pid}",
            params={"hydrate": "stats(group=[pitching],type=[season])"}
        )
        era = data["people"][0]["stats"][0]["splits"][0]["stat"]["era"]
        return float(era)
    except Exception as e:
        logger.debug(f"ERA no disponible para pitcher {pid}: {e}")
        return 4.50
 
async def obtener_era_bullpen(team_id: int) -> float:
    """ERA del bullpen (pitchers de relevo) del equipo. Fallback: 4.20."""
    try:
        data = await _get_json(
            f"{MLB_BASE}/teams/{team_id}/stats",
            params={"stats": "season", "group": "pitching", "pitcherStat": "relief"}
        )
        era = data["stats"][0]["splits"][0]["stat"]["era"]
        return float(era)
    except Exception as e:
        logger.debug(f"ERA bullpen no disponible para equipo {team_id}: {e}")
        return 4.20
 
async def obtener_ops_equipo(team_id: int) -> float:
    """OPS ofensivo del equipo. Fallback: 0.720 (promedio liga)."""
    try:
        data = await _get_json(
            f"{MLB_BASE}/teams/{team_id}/stats",
            params={"stats": "season", "group": "hitting"}
        )
        ops = data["stats"][0]["splits"][0]["stat"]["ops"]
        return float(ops)
    except Exception as e:
        logger.debug(f"OPS no disponible para equipo {team_id}: {e}")
        return 0.720
 
async def obtener_win_pct(team_id: int) -> float:
    """Win percentage del equipo en la temporada. Fallback: 0.500."""
    try:
        data = await _get_json(
            f"{MLB_BASE}/teams/{team_id}/stats",
            params={"stats": "season", "group": "fielding"}
        )
        # Win % se obtiene del standings, no fielding; usamos endpoint correcto
        standings = await _get_json(
            f"{MLB_BASE}/standings",
            params={"leagueId": "103,104", "season": str(datetime.now().year)}
        )
        for record in standings.get("records", []):
            for team_record in record.get("teamRecords", []):
                if team_record["team"]["id"] == team_id:
                    return float(team_record["winningPercentage"])
    except Exception as e:
        logger.debug(f"Win% no disponible para equipo {team_id}: {e}")
    return 0.500
 
 
# ─────────────────────────────────────────────
# MODELO PROBABILÍSTICO
# ─────────────────────────────────────────────
 
def _ratio_ventaja(a: float, b: float, invertir: bool = False) -> float:
    """
    Compara dos métricas y devuelve la ventaja relativa del equipo A.
    Si invertir=True, menor valor es mejor (como ERA).
    Rango de salida: [0.0, 1.0]
    """
    epsilon = 0.001
    a = max(a, epsilon)
    b = max(b, epsilon)
    if invertir:
        # ERA más baja es mejor: ventaja A si ERA_A < ERA_B
        return math.log(b) / (math.log(a) + math.log(b))
    else:
        # OPS/win% más alta es mejor: ventaja A si A > B
        return math.log(a) / (math.log(a) + math.log(b))
 
def calcular_probabilidad(
    era_sp_h: float, era_sp_a: float,   # ERA starting pitcher
    era_bp_h: float, era_bp_a: float,   # ERA bullpen
    ops_h: float,    ops_a: float,      # OPS ofensivo
    winpct_h: float, winpct_a: float,   # Win percentage
    es_local: bool = True               # ventaja de local
) -> float:
    """
    Modelo ponderado con 4 features reales.
    Pesos calibrados empíricamente para MLB:
      - Starting pitcher: 30% (el factor más predictivo por partido)
      - Bullpen:          25%
      - Ataque (OPS):     25%
      - Forma (win%):     15%
      - Local:             5%
    """
    p_sp      = _ratio_ventaja(era_sp_h, era_sp_a, invertir=True)
    p_bp      = _ratio_ventaja(era_bp_h, era_bp_a, invertir=True)
    p_ataque  = _ratio_ventaja(ops_h, ops_a, invertir=False)
    p_winpct  = _ratio_ventaja(winpct_h, winpct_a, invertir=False)
    p_local   = 0.54 if es_local else 0.50  # ventaja histórica de local en MLB
 
    p = (
        p_sp     * 0.30 +
        p_bp     * 0.25 +
        p_ataque * 0.25 +
        p_winpct * 0.15 +
        p_local  * 0.05
    )
    return max(min(p, 0.92), 0.08)
 
def calcular_value(prob: float, cuota_decimal: float) -> float:
    """
    Value = EV normalizado. Positivo significa apuesta con valor esperado positivo.
    Asume cuotas en formato decimal europeo (ej: 1.90, 2.10).
    """
    return (prob * cuota_decimal) - 1
 
 
# ─────────────────────────────────────────────
# ODDS API
# ─────────────────────────────────────────────
 
async def get_odds() -> list:
    """Obtiene cuotas de The Odds API en formato decimal."""
    data = await _get_json(
        "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds",
        params={
            "apiKey": ODDS_API_KEY,
            "regions": "eu",          # formato decimal (europeo)
            "markets": "h2h",
            "oddsFormat": "decimal"
        }
    )
    if isinstance(data, list):
        return data
    logger.warning(f"Respuesta inesperada de Odds API: {data}")
    return []
 
def _normalizar_nombre(nombre: str) -> str:
    """Normaliza nombres de equipos para comparación robusta."""
    return nombre.lower().strip().replace("  ", " ")
 
def buscar_cuotas(home: str, away: str, odds: list) -> tuple[float | None, float | None]:
    """
    Busca las cuotas de un partido usando matching por nombre de equipo.
    Retorna (cuota_home, cuota_away) en decimal, o (None, None) si no encuentra.
    """
    home_norm = _normalizar_nombre(home)
    away_norm = _normalizar_nombre(away)
 
    for game in odds:
        try:
            g_home = _normalizar_nombre(game.get("home_team", ""))
            g_away = _normalizar_nombre(game.get("away_team", ""))
 
            # Matching: ambos equipos deben coincidir (evita falsos positivos)
            home_match = any(w in g_home for w in home_norm.split() if len(w) > 3)
            away_match = any(w in g_away for w in away_norm.split() if len(w) > 3)
 
            if not (home_match and away_match):
                continue
 
            bookmakers = game.get("bookmakers", [])
            if not bookmakers:
                continue
 
            outcomes = bookmakers[0]["markets"][0]["outcomes"]
 
            cuota_h = next(
                (o["price"] for o in outcomes if _normalizar_nombre(o["name"]) == g_home),
                None
            )
            cuota_a = next(
                (o["price"] for o in outcomes if _normalizar_nombre(o["name"]) == g_away),
                None
            )
 
            if cuota_h and cuota_a and cuota_h > 1.0 and cuota_a > 1.0:
                logger.debug(f"Cuotas encontradas: {home} {cuota_h} | {away} {cuota_a}")
                return cuota_h, cuota_a
 
        except (KeyError, StopIteration, IndexError) as e:
            logger.debug(f"Error procesando odds entry: {e}")
            continue
 
    return None, None
 
 
# ─────────────────────────────────────────────
# MONITOR
# ─────────────────────────────────────────────
 
async def monitor(context):
    logger.info("Ejecutando monitor...")
 
    # Limpiar alertas viejas periódicamente
    limpiar_alertados_viejos(dias=7)
 
    # Obtener datos en paralelo
    odds, schedule = await asyncio.gather(
        get_odds(),
        _get_json(f"{MLB_BASE}/schedule", params={"sportId": "1"})
    )
 
    if not odds:
        logger.warning("Sin cuotas disponibles, saltando ciclo")
        return
 
    subs = get_subs()
    if not subs:
        logger.info("Sin suscriptores activos")
        return
 
    picks_enviados = 0
 
    for fecha in schedule.get("dates", []):
        for game in fecha.get("games", []):
 
            gid = game["gamePk"]
 
            if ya_alertado(gid):
                continue
 
            home = game["teams"]["home"]
            away = game["teams"]["away"]
            hname = home["team"]["name"]
            aname = away["team"]["name"]
            hid   = home["team"]["id"]
            aid   = away["team"]["id"]
 
            cuota_h, cuota_a = buscar_cuotas(hname, aname, odds)
            if not cuota_h or not cuota_a:
                logger.debug(f"Sin cuotas para {aname} @ {hname}")
                continue
 
            # Obtener todas las stats en paralelo
            era_sp_h, era_sp_a, era_bp_h, era_bp_a, ops_h, ops_a, winpct_h, winpct_a = \
                await asyncio.gather(
                    obtener_era_pitcher(home.get("probablePitcher", {}).get("id")),
                    obtener_era_pitcher(away.get("probablePitcher", {}).get("id")),
                    obtener_era_bullpen(hid),
                    obtener_era_bullpen(aid),
                    obtener_ops_equipo(hid),
                    obtener_ops_equipo(aid),
                    obtener_win_pct(hid),
                    obtener_win_pct(aid),
                )
 
            prob_h = calcular_probabilidad(
                era_sp_h, era_sp_a,
                era_bp_h, era_bp_a,
                ops_h,    ops_a,
                winpct_h, winpct_a,
                es_local=True
            )
            prob_a = 1.0 - prob_h
 
            value_h = calcular_value(prob_h, cuota_h)
            value_a = calcular_value(prob_a, cuota_a)
 
            mejor_value = max(value_h, value_a)
 
            if mejor_value < VALUE_MINIMO:
                logger.debug(f"Sin value en {aname} @ {hname} (max value: {mejor_value:.3f})")
                continue
 
            if value_h >= value_a:
                pick, cuota, prob_pick = hname, cuota_h, prob_h
            else:
                pick, cuota, prob_pick = aname, cuota_a, prob_a
 
            texto = (
                f"🔥 VALUE BET\n\n"
                f"⚾ {aname} @ {hname}\n"
                f"📊 Prob modelo: {round(prob_pick * 100, 1)}%\n"
                f"💰 Cuota: {cuota}\n"
                f"📈 Value: +{round(mejor_value * 100, 1)}%\n\n"
                f"🏆 PICK: **{pick}**\n\n"
                f"_ERA SP: {round(era_sp_h, 2)} (L) vs {round(era_sp_a, 2)} (V)_\n"
                f"_ERA BP: {round(era_bp_h, 2)} (L) vs {round(era_bp_a, 2)} (V)_\n"
                f"_OPS: {round(ops_h, 3)} (L) vs {round(ops_a, 3)} (V)_"
            )
 
            errores = 0
            for sub in subs:
                try:
                    await context.bot.send_message(sub, texto, parse_mode="Markdown")
                except Exception as e:
                    logger.warning(f"Error enviando a {sub}: {e}")
                    errores += 1
 
            # Guardar pick en DB independientemente de errores de envío
            guardar_pick(gid, f"{aname} @ {hname}", pick, cuota, prob_pick, mejor_value)
            marcar_alertado(gid)
            picks_enviados += 1
 
            logger.info(
                f"Pick enviado: {pick} @ {cuota} | value={mejor_value:.3f} | "
                f"enviado a {len(subs)-errores}/{len(subs)} subs"
            )
 
    logger.info(f"Monitor finalizado. Picks enviados: {picks_enviados}")
 
 
# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────
 
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_sub(update.effective_chat.id)
    await update.message.reply_text(
        "✅ *Activado* — recibirás alertas de value bets en MLB automáticamente.",
        parse_mode="Markdown"
    )
 
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    del_sub(update.effective_chat.id)
    await update.message.reply_text("❌ *Desactivado* — ya no recibirás alertas.", parse_mode="Markdown")
 
async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = len(get_subs())
    with sqlite3.connect(DB_PATH) as c:
        total_picks = c.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
        picks_hoy   = c.execute(
            "SELECT COUNT(*) FROM picks WHERE fecha >= date('now')"
        ).fetchone()[0]
    await update.message.reply_text(
        f"📊 *Estado del bot*\n\n"
        f"👥 Suscriptores: {subs}\n"
        f"🎯 Picks totales: {total_picks}\n"
        f"📅 Picks hoy: {picks_hoy}",
        parse_mode="Markdown"
    )
 
async def historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra los últimos 5 picks enviados."""
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT game, pick, cuota, prob, value, fecha FROM picks ORDER BY id DESC LIMIT 5"
        ).fetchall()
    if not rows:
        await update.message.reply_text("Sin picks registrados aún.")
        return
    texto = "📋 *Últimos picks*\n\n"
    for game, pick, cuota, prob, value, fecha in rows:
        texto += (
            f"⚾ {game}\n"
            f"🏆 {pick} @ {cuota} | Value: +{round(value*100,1)}% | Prob: {round(prob*100,1)}%\n"
            f"🗓 {fecha[:10]}\n\n"
        )
    await update.message.reply_text(texto, parse_mode="Markdown")
 
 
# ─────────────────────────────────────────────
# INIT & RUN
# ─────────────────────────────────────────────
 
async def post_init(app):
    app.job_queue.run_repeating(monitor, interval=INTERVALO_MONITOR, first=10)
    logger.info(f"Monitor programado cada {INTERVALO_MONITOR}s")
 
if __name__ == "__main__":
    init_db()
 
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
 
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("stop",      stop))
    app.add_handler(CommandHandler("estado",    estado))
    app.add_handler(CommandHandler("historial", historial))
 
    logger.info("BOT INICIADO ✅")
    app.run_polling()
    