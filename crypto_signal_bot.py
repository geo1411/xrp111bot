import os, math, requests, pandas as pd, numpy as np
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------- Config ----------------
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_INTERVAL = "1h"
DEFAULT_WATCHLIST = ["XRPUSDT","XLMUSDT","ADAUSDT","BTCUSDT","ETHUSDT","LINKUSDT"]
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
USER_PREFS = {}
SUBSCRIBERS_PATH = os.path.expanduser("~/bots/xrp111bot/subscribers.json")
BOT_USERNAME = None
BOT_VERSION = "1.6.0-github-clean"

# ---------------- Token ----------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN","")

# ---------------- Indicators ----------------
def ema(s, span): return s.ewm(span=span, adjust=False).mean()
def rsi(s, period=14):
    d=s.diff(); g=(d.where(d>0,0)).rolling(period).mean(); l=(-d.where(d<0,0)).rolling(period).mean()
    rs=g/l.replace(0,np.nan); return (100-(100/(1+rs))).fillna(50)
def macd(s, fast=12, slow=26, signal=9):
    ef,es=ema(s,fast),ema(s,slow); m=ef-es; sig=ema(m,signal); return m,sig,m-sig
def true_range(h,l,c):
    pc=c.shift(1); return pd.concat([h-l,(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
def atr(h,l,c,period=14): return true_range(h,l,c).rolling(period).mean()

# ---------------- Utils ----------------
def normalize_symbol(s:str)->str:
    s=s.strip().upper()
    return s if s.endswith(("USDT","USDC","BUSD","USD","TRY","EUR")) else f"{s}USDT"

def _load_subscribers():
    try:
        import json
        with open(SUBSCRIBERS_PATH,"r") as f: return set(json.load(f))
    except Exception:
        return set()
SUBSCRIBERS=_load_subscribers()
def _save_subscribers():
    try:
        import json, os
        os.makedirs(os.path.dirname(SUBSCRIBERS_PATH), exist_ok=True)
        with open(SUBSCRIBERS_PATH,"w") as f: json.dump(sorted(list(SUBSCRIBERS)), f)
    except Exception: pass

# ---------------- Data ----------------
def fetch_klines(symbol, interval, limit=400):
    r=requests.get(BINANCE_KLINES_URL, params={"symbol":symbol.upper(),"interval":interval,"limit":limit}, timeout=20)
    r.raise_for_status()
    data=r.json()
    cols=["open_time","open","high","low","close","volume","close_time","quote","trades","tbb","tbq","ignore"]
    df=pd.DataFrame(data,columns=cols)
    for col in ["open","high","low","close","volume"]: df[col]=df[col].astype(float)
    df["ts"]=pd.to_datetime(df["close_time"],unit="ms",utc=True)
    return df

def compute_signal(symbol, interval):
    df=fetch_klines(symbol, interval)
    close,high,low=df["close"],df["high"],df["low"]
    df["ema20"],df["ema50"]=ema(close,20),ema(close,50)
    df["rsi14"]=rsi(close,14)
    m,s,h=macd(close); df["macd_hist"]=h
    df["atr14"]=atr(high,low,close,14)

    last=df.iloc[-1]
    price=float(last["close"])
    ema20=float(last["ema20"]) if not math.isnan(last["ema20"]) else price
    ema50=float(last["ema50"]) if not math.isnan(last["ema50"]) else price
    rsi_v=float(last["rsi14"]) if not math.isnan(last["rsi14"]) else 50
    macd_h=float(last["macd_hist"]) if not math.isnan(last["macd_hist"]) else 0
    atr_v=float(last["atr14"]) if not math.isnan(last["atr14"]) else 0

    long_bias=(price>ema20>ema50) and (rsi_v>50) and (macd_h>0)
    short_bias=(price<ema20<ema50) and (rsi_v<50) and (macd_h<0)
    side="BUY" if long_bias else "SELL" if short_bias else "WAIT"

    stop=tp1=tp2=None
    if atr_v>0 and side in ("BUY","SELL"):
        if side=="BUY": stop=price-1.5*atr_v; tp1=price+1.0*atr_v; tp2=price+2.0*atr_v
        else:           stop=price+1.5*atr_v; tp1=price-1.0*atr_v; tp2=price-2.0*atr_v

    return side,{
        "symbol":symbol.upper(),"interval":interval,"price":round(price,6),
        "ema20":round(ema20,6),"ema50":round(ema50,6),
        "rsi14":round(rsi_v,2),"macd_hist":round(macd_h,6),
        "atr14":round(atr_v,6),"ts": df.iloc[-1]["ts"].strftime("%Y-%m-%d %H:%M UTC"),
        "stop":None if stop is None else round(stop,6),
        "tp1":None if tp1 is None else round(tp1,6),
        "tp2":None if tp2 is None else round(tp2,6)
    }

# ---------------- Commands ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    USER_PREFS.setdefault(uid, {"symbol":DEFAULT_SYMBOL,"interval":DEFAULT_INTERVAL,"watchlist":DEFAULT_WATCHLIST.copy()})
    await update.message.reply_text(
        "👋 Welcome to xrp111Bot\n"
        "Use /set <SYMBOL> <INTERVAL> (e.g. /set BTCUSDT 1h)\n"
        "Use /watchlist XRP BTC ETH ADA LINK SOL (or full pairs)\n"
        "Then /signal (one) or /watch (list)\n\nNFA."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/set <SYMBOL> <INTERVAL>\n"
        "/signal\n"
        "/watchlist [SYMBOLS…]\n"
        "/watchadd SYMBOLS…\n"
        "/watchrm SYMBOLS…\n"
        "/watch\n"
        "/share\n"
        "/subscribe /unsubscribe\n"
        "/version"
    )

async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"xrp111Bot version: {BOT_VERSION}")

async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; args=context.args
    if len(args)<2:
        await update.message.reply_text("Format: /set <SYMBOL> <INTERVAL>"); return
    symbol=normalize_symbol(args[0]); interval=args[1]
    USER_PREFS.setdefault(uid,{})
    USER_PREFS[uid]["symbol"]=symbol
    USER_PREFS[uid]["interval"]=interval
    USER_PREFS[uid].setdefault("watchlist",DEFAULT_WATCHLIST.copy())
    await update.message.reply_text(f"✅ Set to {symbol} on {interval}")

async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; args=context.args
    USER_PREFS.setdefault(uid, {"symbol":DEFAULT_SYMBOL,"interval":DEFAULT_INTERVAL,"watchlist":DEFAULT_WATCHLIST.copy()})
    if not args:
        wl=USER_PREFS[uid].get("watchlist",DEFAULT_WATCHLIST)
        await update.message.reply_text("📜 Watchlist: "+", ".join(wl)); return
    symbols=[normalize_symbol(a) for a in args]
    seen=set(); wl=[]
    for s in symbols:
        if s not in seen:
            wl.append(s); seen.add(s)
        if len(wl)>=20: break
    USER_PREFS[uid]["watchlist"]=wl
    await update.message.reply_text("✅ Watchlist set: "+", ".join(wl))

async def cmd_watchadd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; args=context.args
    USER_PREFS.setdefault(uid, {"symbol":DEFAULT_SYMBOL,"interval":DEFAULT_INTERVAL,"watchlist":DEFAULT_WATCHLIST.copy()})
    if not args:
        await update.message.reply_text("Format: /watchadd SYMBOLS…"); return
    wl=USER_PREFS[uid].get("watchlist",DEFAULT_WATCHLIST).copy()
    added=[]
    for a in args:
        s=normalize_symbol(a)
        if s not in wl and len(wl)<20:
            wl.append(s); added.append(s)
    USER_PREFS[uid]["watchlist"]=wl
    await update.message.reply_text(("✅ Added: "+", ".join(added)+"\nCurrent: "+", ".join(wl)) if added else "No new symbols added")

async def cmd_watchrm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; args=context.args
    USER_PREFS.setdefault(uid, {"symbol":DEFAULT_SYMBOL,"interval":DEFAULT_INTERVAL,"watchlist":DEFAULT_WATCHLIST.copy()})
    if not args:
        await update.message.reply_text("Format: /watchrm SYMBOLS…"); return
    wl=USER_PREFS[uid].get("watchlist",DEFAULT_WATCHLIST).copy()
    targets=set(normalize_symbol(a) for a in args)
    before=set(wl)
    wl=[s for s in wl if s not in targets]
    USER_PREFS[uid]["watchlist"]=wl
    removed=before-set(wl)
    await update.message.reply_text(("🗑 Removed: "+", ".join(sorted(removed))+"\nCurrent: "+", ".join(wl)) if removed else "Nothing removed")

async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    prefs=USER_PREFS.get(uid, {"symbol":DEFAULT_SYMBOL,"interval":DEFAULT_INTERVAL,"watchlist":DEFAULT_WATCHLIST.copy()})
    s,i=prefs.get("symbol",DEFAULT_SYMBOL),prefs.get("interval",DEFAULT_INTERVAL)
    try:
        side,info=compute_signal(s,i)
        lines=[
            f"📈 {info['symbol']} [{i}] — {info['ts']}",
            f"Px {info['price']}",
            f"EMA20/50 {info['ema20']} / {info['ema50']}",
            f"RSI {info['rsi14']} | MACD {info['macd_hist']} | ATR {info['atr14']}",
            f"Signal: {side}"
        ]
        if info["stop"] is not None: lines.append(f"Stop {info['stop']}")
        if info["tp1"]  is not None: lines.append(f"TP1/TP2 {info['tp1']}/{info['tp2']}")
        lines.append("NFA.")
        await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    prefs=USER_PREFS.get(uid, {"symbol":DEFAULT_SYMBOL,"interval":DEFAULT_INTERVAL,"watchlist":DEFAULT_WATCHLIST.copy()})
    wl=prefs.get("watchlist",DEFAULT_WATCHLIST)[:20]; i=prefs.get("interval",DEFAULT_INTERVAL)
    out=[f"🧭 Watchlist [{i}]:"]
    for s in wl:
        try:
            side,info=compute_signal(s,i)
            out.append(f"• {info['symbol']}: {side} | Px {info['price']} | RSI {info['rsi14']} | MACD {info['macd_hist']} | ATR {info['atr14']}")
        except Exception as e:
            out.append(f"• {s}: error — {e}")
    out.append("NFA.")
    await update.message.reply_text("\n".join(out), disable_web_page_preview=True)

async def build_watchlist_summary(uid:int)->str:
    prefs=USER_PREFS.get(uid, {"symbol":DEFAULT_SYMBOL,"interval":DEFAULT_INTERVAL,"watchlist":DEFAULT_WATCHLIST.copy()})
    wl=prefs.get("watchlist",DEFAULT_WATCHLIST)[:10]; i=prefs.get("interval",DEFAULT_INTERVAL)
    out=[f"🧭 Watchlist [{i}] (for sharing):"]
    for s in wl:
        try:
            side,info=compute_signal(s,i)
            out.append(f"• {info['symbol']}: {side} | Px {info['price']} | RSI {info['rsi14']} | MACD {info['macd_hist']} | ATR {info['atr14']}")
        except Exception as e:
            out.append(f"• {s}: error — {e}")
    out.append("NFA.")
    return "\n".join(out)

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id=update.effective_chat.id
    SUBSCRIBERS.add(chat_id); _save_subscribers()
    await update.message.reply_text("✅ Subscribed.")

async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id=update.effective_chat.id
    if chat_id in SUBSCRIBERS:
        SUBSCRIBERS.remove(chat_id); _save_subscribers()
        await update.message.reply_text("✅ Unsubscribed.")
    else:
        await update.message.reply_text("You were not subscribed.")

async def cmd_share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME
    uid=update.effective_user.id
    summary=await build_watchlist_summary(uid)
    if not BOT_USERNAME:
        me=await context.bot.get_me(); BOT_USERNAME=me.username
    link=f"https://t.me/{BOT_USERNAME}"
    await update.message.reply_text(summary + f"\nInvite a friend: {link}")

# ---------------- Main ----------------
def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN not set")
    app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("help",cmd_help))
    app.add_handler(CommandHandler("version",cmd_version))
    app.add_handler(CommandHandler("set",cmd_set))
    app.add_handler(CommandHandler("watchlist",cmd_watchlist))
    app.add_handler(CommandHandler("watchadd",cmd_watchadd))
    app.add_handler(CommandHandler("watchrm",cmd_watchrm))
    app.add_handler(CommandHandler("signal",cmd_signal))
    app.add_handler(CommandHandler("watch",cmd_watch))
    app.add_handler(CommandHandler("subscribe",cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe",cmd_unsubscribe))
    app.add_handler(CommandHandler("share",cmd_share))
    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__": main()
