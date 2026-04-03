import logging
import os
import math
import asyncio
import aiosqlite
import httpx
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

# CONFIG
TOKEN = os.getenv("TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
MLB_BASE = "https://statsapi.mlb.com/api/v1"
DB_PATH = "mlb_bot.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MLB-BOT")

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

# HTTP
async def get_json(url, params=None):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        return r.json()

# STATS
async def era_pitcher(pid):
    if not pid: return 4.50
    try:
        d = await get_json(f"{MLB_BASE}/people/{pid}?hydrate=stats(group=[pitching],type=[season])")
        return float(d["people"][0]["stats"][0]["splits"][0]["stat"]["era"])
    except: return 4.50

async def mano_pitcher(pid):
    if not pid: return "R"
    try:
        d = await get_json(f"{MLB_BASE}/people/{pid}")
        return d["people"][0]["pitchHand"]["code"]
    except: return "R"

async def bullpen(team):
    try:
        d = await get_json(f"{MLB_BASE}/teams/{team}/stats", {"stats":"season","group":"pitching","pitcherStat":"relief"})
        return float(d["stats"][0]["splits"][0]["stat"]["era"])
    except: return 4.20

async def ops(team):
    try:
        d = await get_json(f"{MLB_BASE}/teams/{team}/stats", {"stats":"season","group":"hitting"})
        return float(d["stats"][0]["splits"][0]["stat"]["ops"])
    except: return 0.720

async def winpct(team):
    try:
        d = await get_json(f"{MLB_BASE}/standings", {"leagueId":"103,104"})
        for r in d["records"]:
            for t in r["teamRecords"]:
                if t["team"]["id"] == team:
                    return float(t["winningPercentage"])
    except: pass
    return 0.5

# MODELO
def ratio(a,b,inv=False):
    if inv: return math.log(b)/(math.log(a)+math.log(b))
    return math.log(a)/(math.log(a)+math.log(b))

def prob_model(era_h,era_a,bp_h,bp_a,ops_h,ops_a,w_h,w_a):
    return (
        ratio(era_h,era_a,True)*0.30 +
        ratio(bp_h,bp_a,True)*0.25 +
        ratio(ops_h,ops_a)*0.25 +
        ratio(w_h,w_a)*0.15 +
        0.54*0.05
    )

def ajuste_mano(prob, mano_pitcher, ops_equipo):
    if mano_pitcher == "L":
        ajuste = (ops_equipo - 0.720) * 0.8
    else:
        ajuste = (ops_equipo - 0.720) * 0.5
    return max(min(prob + ajuste, 0.92), 0.08)

def value(prob,cuota): return prob*cuota-1

# ODDS
async def get_odds():
    return await get_json(
        "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds",
        {"apiKey":ODDS_API_KEY,"regions":"eu","markets":"h2h"}
    )

def buscar_cuota(home,away,odds):
    for g in odds:
        try:
            if g["home_team"].lower()==home.lower() and g["away_team"].lower()==away.lower():
                o=g["bookmakers"][0]["markets"][0]["outcomes"]
                ch=next(x["price"] for x in o if x["name"].lower()==home.lower())
                ca=next(x["price"] for x in o if x["name"].lower()==away.lower())
                return ch,ca
        except: continue
    return None,None

# START
async def start(update,context):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO suscriptores VALUES (?)",(update.effective_chat.id,))
        await db.commit()
    await update.message.reply_text("⚾ BOT ACTIVO",reply_markup=MENU)

# PARTIDOS
async def partidos(update,context):
    data=await get_json(f"{MLB_BASE}/schedule",{"sportId":"1"})
    kb=[]
    for d in data.get("dates",[]):
        for g in d.get("games",[]):
            h=g["teams"]["home"]["team"]["name"]
            a=g["teams"]["away"]["team"]["name"]
            kb.append([InlineKeyboardButton(f"{a} @ {h}",callback_data=f"g_{g['gamePk']}")])
    await update.message.reply_text("📅 Partidos:",reply_markup=InlineKeyboardMarkup(kb))

# DETALLE
async def detalle(update,context):
    q=update.callback_query
    await q.answer()

    gid=q.data.split("_")[1]
    sched=await get_json(f"{MLB_BASE}/schedule",{"sportId":"1"})

    game=None
    for d in sched.get("dates",[]):
        for g in d.get("games",[]):
            if str(g["gamePk"])==gid: game=g

    if not game:
        await q.edit_message_text("No encontrado");return

    h=game["teams"]["home"]["team"]
    a=game["teams"]["away"]["team"]

    odds=await get_odds()
    ch,ca=buscar_cuota(h["name"],a["name"],odds)
    if not ch: await q.edit_message_text("Sin cuotas");return

    era_h,era_a,bp_h,bp_a,ops_h,ops_a,w_h,w_a,mano_h,mano_a=await asyncio.gather(
        era_pitcher(h.get("probablePitcher",{}).get("id")),
        era_pitcher(a.get("probablePitcher",{}).get("id")),
        bullpen(h["id"]),bullpen(a["id"]),
        ops(h["id"]),ops(a["id"]),
        winpct(h["id"]),winpct(a["id"]),
        mano_pitcher(h.get("probablePitcher",{}).get("id")),
        mano_pitcher(a.get("probablePitcher",{}).get("id")),
    )

    ph=prob_model(era_h,era_a,bp_h,bp_a,ops_h,ops_a,w_h,w_a)
    pa=1-ph

    ph=ajuste_mano(ph,mano_a,ops_h)
    pa=ajuste_mano(pa,mano_h,ops_a)

    vh,va=value(ph,ch),value(pa,ca)

    if vh>va: pick,cuota,prob,val=h["name"],ch,ph,vh
    else: pick,cuota,prob,val=a["name"],ca,pa,va

    txt=(
        f"⚾ {a['name']} @ {h['name']}\n\n"
        f"📊 Prob: {round(prob*100,1)}%\n"
        f"💰 Cuota: {cuota}\n"
        f"📈 Value: +{round(val*100,1)}%\n\n"
        f"🏆 PICK: {pick}\n\n"
        f"🧠 Pitchers:\n"
        f"{round(era_h,2)} ({mano_h}) vs {round(era_a,2)} ({mano_a})\n"
        f"📉 Bullpen: {round(bp_h,2)} vs {round(bp_a,2)}\n"
        f"⚾ OPS: {round(ops_h,3)} vs {round(ops_a,3)}"
    )

    await q.edit_message_text(txt)

# PICKS
async def picks(update,context):
    data=await get_json(f"{MLB_BASE}/schedule",{"sportId":"1"})
    odds=await get_odds()
    txt="🔥 PICKS\n\n"

    for d in data.get("dates",[]):
        for g in d.get("games",[]):
            h=g["teams"]["home"]["team"]
            a=g["teams"]["away"]["team"]

            ch,ca=buscar_cuota(h["name"],a["name"],odds)
            if not ch or ch>5 or ca>5: continue

            era_h,era_a,bp_h,bp_a,ops_h,ops_a,w_h,w_a,mano_h,mano_a=await asyncio.gather(
                era_pitcher(h.get("probablePitcher",{}).get("id")),
                era_pitcher(a.get("probablePitcher",{}).get("id")),
                bullpen(h["id"]),bullpen(a["id"]),
                ops(h["id"]),ops(a["id"]),
                winpct(h["id"]),winpct(a["id"]),
                mano_pitcher(h.get("probablePitcher",{}).get("id")),
                mano_pitcher(a.get("probablePitcher",{}).get("id")),
            )

            ph=prob_model(era_h,era_a,bp_h,bp_a,ops_h,ops_a,w_h,w_a)
            pa=1-ph

            ph=ajuste_mano(ph,mano_a,ops_h)
            pa=ajuste_mano(pa,mano_h,ops_a)

            vh,va=value(ph,ch),value(pa,ca)

            if vh>va: pick,cuota,prob,val=h["name"],ch,ph,vh
            else: pick,cuota,prob,val=a["name"],ca,pa,va

            if val<0.03 or val>0.25: continue

            txt+=f"{a['name']} @ {h['name']}\n🏆 {pick}\n💰 {cuota} | +{round(val*100,1)}%\n\n"

    await update.message.reply_text(txt if txt!="🔥 PICKS\n\n" else "No hay value")

# MENU
async def menu(update,context):
    t=update.message.text
    if t=="📅 Partidos de Hoy": await partidos(update,context)
    elif t=="📈 Picks con Valor": await picks(update,context)

# MAIN
async def post_init(app): await init_db()

if __name__=="__main__":
    app=ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,menu))
    app.add_handler(CallbackQueryHandler(detalle,pattern="g_"))
    app.run_polling()