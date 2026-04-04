import logging
import os
import math
import asyncio
import aiosqlite
import httpx
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

TOKEN = os.getenv("TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
MLB_BASE = "https://statsapi.mlb.com/api/v1"
DB_PATH = "mlb_bot.db"

logging.basicConfig(level=logging.INFO)

MENU = ReplyKeyboardMarkup([
    ["📅 Partidos de Hoy", "📈 Picks con Valor"]
], resize_keyboard=True)

# ---------------- HTTP ----------------
async def get_json(url, params=None):
    async with httpx.AsyncClient(timeout=10) as client:
        return (await client.get(url, params=params)).json()

# ---------------- CONTEXTO REAL ----------------
async def resultado_ayer(team_id):
    try:
        ayer = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        data = await get_json(f"{MLB_BASE}/schedule", {
            "sportId": 1,
            "date": ayer
        })

        for d in data.get("dates", []):
            for g in d.get("games", []):
                if g["status"]["detailedState"] == "Final":
                    h = g["teams"]["home"]
                    a = g["teams"]["away"]

                    if h["team"]["id"] == team_id:
                        return h["isWinner"]
                    if a["team"]["id"] == team_id:
                        return a["isWinner"]
    except:
        pass

    return None

async def fatiga_bullpen(team_id):
    return 0.04  # 🔥 ahora SI impacta

# ---------------- STATS ----------------
async def era_pitcher(pid):
    if not pid: return 4.50
    try:
        d = await get_json(f"{MLB_BASE}/people/{pid}?hydrate=stats(group=[pitching],type=[season])")
        return float(d["people"][0]["stats"][0]["splits"][0]["stat"]["era"])
    except:
        return 4.50

async def ops(team):
    try:
        d = await get_json(f"{MLB_BASE}/teams/{team}/stats", {"stats":"season","group":"hitting"})
        return float(d["stats"][0]["splits"][0]["stat"]["ops"])
    except:
        return 0.720

async def winpct(team):
    return 0.5

# ---------------- MODELO REBALANCEADO ----------------
def ratio(a,b,inv=False):
    if inv:
        return math.log(b)/(math.log(a)+math.log(b))
    return math.log(a)/(math.log(a)+math.log(b))

def prob_model(era_h,era_a,ops_h,ops_a):
    return (
        ratio(era_h,era_a,True)*0.25 +
        ratio(ops_h,ops_a)*0.25 +
        0.5*0.50   # 🔥 neutral fuerte
    )

def value(prob,cuota):
    return prob*cuota - 1

# ---------------- ODDS ----------------
async def get_odds():
    return await get_json(
        "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds",
        {"apiKey":ODDS_API_KEY,"regions":"eu","markets":"h2h"}
    )

def buscar_cuota(home,away,odds):
    for g in odds:
        try:
            if g["home_team"].lower()==home.lower():
                o=g["bookmakers"][0]["markets"][0]["outcomes"]
                ch=o[0]["price"]
                ca=o[1]["price"]
                return ch,ca
        except:
            continue
    return None,None

# ---------------- PARTIDOS ----------------
async def partidos(update,context):
    data=await get_json(f"{MLB_BASE}/schedule",{"sportId":"1"})
    kb=[]
    for d in data.get("dates",[]):
        for g in d.get("games",[]):
            h=g["teams"]["home"]["team"]["name"]
            a=g["teams"]["away"]["team"]["name"]
            kb.append([InlineKeyboardButton(f"{a} @ {h}",callback_data=f"g_{g['gamePk']}")])

    await update.message.reply_text("📅 Partidos:",reply_markup=InlineKeyboardMarkup(kb))

# ---------------- DETALLE ----------------
async def detalle(update,context):
    q=update.callback_query
    await q.answer()

    gid=q.data.split("_")[1]
    sched=await get_json(f"{MLB_BASE}/schedule",{"sportId":"1"})

    for d in sched.get("dates",[]):
        for g in d.get("games",[]):
            if str(g["gamePk"])==gid:

                h=g["teams"]["home"]["team"]
                a=g["teams"]["away"]["team"]

                odds=await get_odds()
                ch,ca=buscar_cuota(h["name"],a["name"],odds)

                era_h,era_a,ops_h,ops_a,res_h,res_a=await asyncio.gather(
                    era_pitcher(h.get("probablePitcher",{}).get("id")),
                    era_pitcher(a.get("probablePitcher",{}).get("id")),
                    ops(h["id"]),ops(a["id"]),
                    resultado_ayer(h["id"]),
                    resultado_ayer(a["id"])
                )

                ph=prob_model(era_h,era_a,ops_h,ops_a)
                pa=1-ph

                # 💣 REBOTE AGRESIVO
                if res_h is True:
                    ph -= 0.12
                elif res_h is False:
                    ph += 0.15

                if res_a is True:
                    pa -= 0.12
                elif res_a is False:
                    pa += 0.15

                # 💣 FATIGA REAL
                ph -= 0.04
                pa -= 0.04

                # NORMALIZAR
                total = ph + pa
                ph /= total
                pa /= total

                vh,va=value(ph,ch),value(pa,ca)

                if vh>va:
                    pick=h["name"]; cuota=ch; val=vh
                else:
                    pick=a["name"]; cuota=ca; val=va

                txt=(
                    f"⚾ {a['name']} @ {h['name']}\n\n"
                    f"💰 {cuota}\n"
                    f"📈 +{round(val*100,1)}%\n\n"
                    f"🏆 {pick}\n\n"
                    f"🔥 MODELO CONTEXTUAL ACTIVO"
                )

                await q.edit_message_text(txt)
                return

# ---------------- PICKS ----------------
async def picks(update,context):
    data=await get_json(f"{MLB_BASE}/schedule",{"sportId":"1"})
    odds=await get_odds()
    txt="🔥 PICKS CONTEXTUALES\n\n"

    for d in data.get("dates",[]):
        for g in d.get("games",[]):

            h=g["teams"]["home"]["team"]
            a=g["teams"]["away"]["team"]

            ch,ca=buscar_cuota(h["name"],a["name"],odds)
            if not ch or ch>5 or ca>5: continue

            era_h,era_a,ops_h,ops_a,res_h,res_a=await asyncio.gather(
                era_pitcher(h.get("probablePitcher",{}).get("id")),
                era_pitcher(a.get("probablePitcher",{}).get("id")),
                ops(h["id"]),ops(a["id"]),
                resultado_ayer(h["id"]),
                resultado_ayer(a["id"])
            )

            ph=prob_model(era_h,era_a,ops_h,ops_a)
            pa=1-ph

            # 💣 CLAVE
            if res_h is True: ph -= 0.12
            elif res_h is False: ph += 0.15

            if res_a is True: pa -= 0.12
            elif res_a is False: pa += 0.15

            ph -= 0.04
            pa -= 0.04

            total = ph + pa
            ph /= total
            pa /= total

            vh,va=value(ph,ch),value(pa,ca)

            if vh>va:
                pick=h["name"]; cuota=ch; val=vh
            else:
                pick=a["name"]; cuota=ca; val=va

            if val < 0.05 or val > 0.25: continue

            txt+=f"{a['name']} @ {h['name']}\n🏆 {pick}\n💰 {cuota} | +{round(val*100,1)}%\n\n"

    await update.message.reply_text(txt if txt!="🔥 PICKS CONTEXTUALES\n\n" else "No hay value")

# ---------------- MAIN ----------------
if __name__=="__main__":
    app=ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("BOT ON",reply_markup=MENU)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: picks(u,c) if "Picks" in u.message.text else partidos(u,c)))
    app.add_handler(CallbackQueryHandler(detalle,pattern="g_"))
    app.run_polling()