import os, math, requests, pandas as pd, numpy as np
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_INTERVAL = "1h"
DEFAULT_WATCHLIST = ["XRPUSDT","XLMUSDT","ADAUSDT","BTCUSDT","ETHUSDT","LINKUSDT"]
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
USER_PREFS = {}

BOT_TOKEN = os.getenv("BOT_TOKEN","")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

def ema(s, span): return s.ewm(span=span, adjust=False).mean()
def rsi(s, period=14):
    d=s.diff(); g=(d.where(d>0,0)).rolling(period).mean(); l=(-d.where(d<0,0)).rolling(period).mean()
    rs=g/l.replace(0,np.nan); return (100-(100/(1+rs))).fillna(50)
def macd(s, fast=12, slow=26, signal=9):
    ef,es=ema(s,fast),ema(s,slow); m=ef-es; sig=ema(m,signal); return m,sig,m-sig
def true_range(h,l,c):
    pc=c.shift(1); return pd.concat([h-l,(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
def atr(h,l,c,period=14): return true_range(h,l,c).rolling(period).mean()

def normalize_symbol(s:str)->str:
    s=s.strip().upper()
    return s if s.endswith(("USDT","USDC","BUSD","USD","TRY","EUR")) else f"{s}USDT"

def fetch_klines(symbol, interval, limit=400):
    r=requests.get(BINANCE_KLINES_URL, params={"symbol":symbol.upper(),"interval":interval,"limit":limit}, timeout=15)
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
    import math
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
        "atr14":round(atr_v,6),"ts": last["ts"].strftime("%Y-%m-%d %H:%M UTC"),
        "stop":None if stop is None else round(stop,6),
        "tp1":None if tp1 is None else round(tp1,6),
        "tp2":None if tp2 is None else round(tp2,6)
    }

tg_app = Application.builder().token(BOT_TOKEN).build()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    USER_PREFS.setdefault(uid, {"symbol":DEFAULT_SYMBOL,"interval":DEFAULT_INTERVAL,"watchlist":DEFAULT_WATCHLIST.copy()})
    await update.message.reply_text("xrp111Bot webhook ready. Use /set /watchlist /watch /signal")

async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; args=context.args
    if len(args)<2:
        return await update.message.reply_text("Format: /set <SYMBOL> <INTERVAL>")
    USER_PREFS.setdefault(uid,{})
    USER_PREFS[uid]["symbol"]=normalize_symbol(args[0])
    USER_PREFS[uid]["interval"]=args[1]
    USER_PREFS[uid].setdefault("watchlist",DEFAULT_WATCHLIST.copy())
    await update.message.reply_text("Updated. Try /signal or /watch")

async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; args=context.args
    USER_PREFS.setdefault(uid, {"symbol":DEFAULT_SYMBOL,"interval":DEFAULT_INTERVAL,"watchlist":DEFAULT_WATCHLIST.copy()})
    if not args:
        return await update.message.reply_text("Watchlist: "+", ".join(USER_PREFS[uid]["watchlist"]))
    wl=[]; seen=set()
    for a in args:
        s=normalize_symbol(a)
        if s not in seen and len(wl)<20:
            wl.append(s); seen.add(s)
    USER_PREFS[uid]["watchlist"]=wl
    await update.message.reply_text("Watchlist set: "+", ".join(wl))

async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    prefs=USER_PREFS.get(uid, {"symbol":DEFAULT_SYMBOL,"interval":DEFAULT_INTERVAL,"watchlist":DEFAULT_WATCHLIST.copy()})
    i=prefs["interval"]; wl=prefs["watchlist"][:10]
    out=[f"Watchlist [{i}]:"]
    for s in wl:
        try:
            side,info=compute_signal(s,i)
            out.append(f"{info['symbol']}: {side} | Px {info['price']} | RSI {info['rsi14']} | MACD {info['macd_hist']} | ATR {info['atr14']}")
        except Exception as e:
            out.append(f"{s}: error — {e}")
    await update.message.reply_text("\n".join(out), disable_web_page_preview=True)

async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    prefs=USER_PREFS.get(uid, {"symbol":DEFAULT_SYMBOL,"interval":DEFAULT_INTERVAL,"watchlist":DEFAULT_WATCHLIST.copy()})
    s,i=prefs["symbol"],prefs["interval"]
    try:
        side,info=compute_signal(s,i)
        lines=[f"{info['symbol']} [{i}] — {info['ts']}",
               f"Px {info['price']}",
               f"EMA20/50 {info['ema20']} / {info['ema50']}",
               f"RSI {info['rsi14']} | MACD {info['macd_hist']} | ATR {info['atr14']}",
               f"Signal: {side}"]
        await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

tg_app.add_handler(CommandHandler("start",cmd_start))
tg_app.add_handler(CommandHandler("set",cmd_set))
tg_app.add_handler(CommandHandler("watchlist",cmd_watchlist))
tg_app.add_handler(CommandHandler("watch",cmd_watch))
tg_app.add_handler(CommandHandler("signal",cmd_signal))

app = FastAPI()

@app.get("/")
async def root():
    return {"ok": True, "msg": "xrp111bot webhook"}

@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
