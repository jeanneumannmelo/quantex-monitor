#!/usr/bin/env python3
"""
HYPERLIQUID MATRIX COPY TRADER — Web Dashboard
Flask + SocketIO backend
"""

import asyncio
import json
import time
import urllib.request
import threading
from collections import deque
from datetime import datetime, timezone

import websockets
from flask import Flask, render_template
from flask_socketio import SocketIO
from hybrid_monitor import start_monitor, get_state as hybrid_get_state

# ── Config ────────────────────────────────────────────────────────────────────

WS_URL        = "wss://api.hyperliquid.xyz/ws"
REST_URL      = "https://api.hyperliquid.xyz/info"
STATS_URL     = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
STARTING_BAL  = 1000.0
MAX_POS_PCT   = 0.08
MAX_POSITIONS = 5
MIN_COPY_WR   = 85  # só copia traders com win rate >= 85%

TOP_TRADERS = [
    {"addr": "0xa5b0edf6b55128e0ddae8e51ac538c3188401d41", "label": "ETH-KING",   "pnl": 19_756_455, "wr": 100},
    {"addr": "0x6c8512516ce5669d35113a11ca8b8de322fd84f6", "label": "ETH-BULL",   "pnl": 10_750_234, "wr": 100},
    {"addr": "0x61ceef212ff4a86933c69fb6aca2fe35d8f2a62b", "label": "MULTI-X",    "pnl":  8_644_790, "wr":  69},
    {"addr": "0xa31441e058492bc7cfffda9aa7623c407ae83a81", "label": "OIL-SHORT",  "pnl":  6_689_555, "wr": 100},
    {"addr": "0xeadc152ac1014ace57c6b353f89adf5faffe9d55", "label": "JUP-TRADER", "pnl":  5_425_306, "wr": 100},
    {"addr": "0x5b5d51203a0f9079f8aeb098a6523a13f298c060", "label": "BTC-HUNTER", "pnl":  3_858_449, "wr": 100},
    {"addr": "0x469e9a7f624b04c24f0e64edf8d8a277e6bf58a5", "label": "BTC-LONG",   "pnl":  3_605_115, "wr": 100},
    {"addr": "0xfc667adba8d4837586078f4fdcdc29804337ca06", "label": "OIL-SCALPEL","pnl":  2_881_279, "wr":  47},
    {"addr": "0x985f02b19dbc062e565c981aac5614baf2cf501f", "label": "OIL-BEAST",  "pnl":  2_786_332, "wr": 100},
    {"addr": "0x939f95036d2e7b4d3e80f2b2d3ec1b82b4ca7b74", "label": "HYPE-RIDER", "pnl":  2_739_340, "wr": 100},
]

ADDR_TO_TRADER = {t["addr"].lower(): t for t in TOP_TRADERS}

# ── Estado Global ─────────────────────────────────────────────────────────────

state = {
    "balance":       STARTING_BAL,
    "positions":     {},
    "closed_trades": deque(maxlen=100),
    "pnl_history":   deque(maxlen=120),
    "last_prices":   {},
    "known_fills":   {t["addr"].lower(): set() for t in TOP_TRADERS},
    "trader_last":   {t["addr"].lower(): 0 for t in TOP_TRADERS},
    "recent_opens":  {},
    "copy_count":    0,
    "wins":          0,
    "losses":        0,
    "ws_ok":         False,
}
lock = threading.Lock()

app    = Flask(__name__)
sio    = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


# ── REST ──────────────────────────────────────────────────────────────────────

def rest_post(payload):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(REST_URL, data=data,
           headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def fetch_fills(addr):
    try:
        return rest_post({"type": "userFills", "user": addr})
    except Exception:
        return []


# ── Paper Trading Engine ──────────────────────────────────────────────────────

def try_copy_trade(fill, trader):
    direction = fill.get("dir", "")
    if not direction.startswith("Open"):
        return
    if trader.get("wr", 0) < MIN_COPY_WR:
        return
    coin    = fill["coin"]
    px      = float(fill["px"])
    side    = "LONG" if "Long" in direction else "SHORT"
    pos_key = f"{coin}:{side}"

    with lock:
        if pos_key in state["positions"] or len(state["positions"]) >= MAX_POSITIONS:
            return
        alloc = round(state["balance"] * MAX_POS_PCT, 2)
        if alloc < 1.0:
            return
        state["balance"]  -= alloc
        state["positions"][pos_key] = {
            "coin": coin, "side": side,
            "entry_px": px, "size_usd": alloc,
            "trader": trader["label"], "ts": fill["time"],
        }
        state["copy_count"] += 1
        state["last_prices"][coin] = px

    ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
    sio.emit("feed", {
        "type": "copy", "ts": ts,
        "trader": trader["label"], "coin": coin,
        "side": side, "px": str(px), "alloc": f"{alloc:.0f}",
    })


def try_close_trade(fill, trader):
    direction = fill.get("dir", "")
    if not direction.startswith("Close"):
        return
    coin    = fill["coin"]
    px      = float(fill["px"])
    side    = "LONG" if "Long" in direction else "SHORT"
    pos_key = f"{coin}:{side}"

    with lock:
        if pos_key not in state["positions"]:
            return
        pos      = state["positions"].pop(pos_key)
        entry_px = pos["entry_px"]
        size_usd = pos["size_usd"]
        pnl      = size_usd * (px - entry_px) / entry_px if side == "LONG" else \
                   size_usd * (entry_px - px) / entry_px
        state["balance"] += size_usd + pnl
        ts_str = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        state["closed_trades"].appendleft({
            "ts": ts_str, "coin": coin, "side": side,
            "entry": entry_px, "exit": px, "pnl": round(pnl, 4),
            "trader": trader["label"],
        })
        if pnl > 0:
            state["wins"]   += 1
        else:
            state["losses"] += 1
        state["pnl_history"].append(round(state["balance"] - STARTING_BAL, 4))

    sio.emit("feed", {
        "type": "close_copy", "ts": datetime.now(tz=timezone.utc).strftime("%H:%M:%S"),
        "trader": trader["label"], "coin": coin, "pnl": str(round(pnl, 2)),
    })


def process_fill(fill, addr):
    trader = ADDR_TO_TRADER.get(addr.lower())
    if not trader:
        return

    tid = str(fill.get("tid") or fill.get("oid", "")) + str(fill.get("time", ""))
    with lock:
        if tid in state["known_fills"][addr.lower()]:
            return
        state["known_fills"][addr.lower()].add(tid)
        state["last_prices"][fill["coin"]] = float(fill["px"])
        state["trader_last"][addr.lower()] = time.time()

        # Rastrear opens recentes para detector de convergência
        if fill.get("dir", "").startswith("Open"):
            coin = fill["coin"]
            side = "LONG" if "Long" in fill["dir"] else "SHORT"
            key  = f"{coin}:{side}"
            now_ms = time.time() * 1000
            state["recent_opens"].setdefault(key, [])
            state["recent_opens"][key] = [
                e for e in state["recent_opens"][key]
                if now_ms - e["ts"] < 120_000
            ]
            if not any(e["trader"] == trader["label"] for e in state["recent_opens"][key]):
                state["recent_opens"][key].append({"trader": trader["label"], "ts": now_ms})

    ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
    sio.emit("feed", {
        "ts": ts, "trader": trader["label"],
        "coin": fill["coin"], "px": fill["px"],
        "dir": fill.get("dir", ""),
        "closed_pnl": fill.get("closedPnl", "0"),
    })

    try_copy_trade(fill, trader)
    try_close_trade(fill, trader)


# ── Broadcast state loop ──────────────────────────────────────────────────────

def build_state():
    with lock:
        bal      = state["balance"]
        positions = dict(state["positions"])
        closed   = list(state["closed_trades"])
        pnl_h    = list(state["pnl_history"])
        last_px  = dict(state["last_prices"])
        t_last   = dict(state["trader_last"])
        r_opens  = dict(state["recent_opens"])
        copies   = state["copy_count"]
        wins     = state["wins"]
        losses   = state["losses"]

    # Unrealized PnL
    unrealized = 0.0
    pos_list   = []
    for pk, pos in positions.items():
        cur = last_px.get(pos["coin"], pos["entry_px"])
        pnl = pos["size_usd"] * (cur - pos["entry_px"]) / pos["entry_px"] if pos["side"] == "LONG" else \
              pos["size_usd"] * (pos["entry_px"] - cur) / pos["entry_px"]
        unrealized += pnl
        pos_list.append({**pos, "cur_px": cur, "pnl": round(pnl, 4)})

    # Patterns
    now_ms = time.time() * 1000
    patterns = []
    for key, entries in r_opens.items():
        fresh = [e for e in entries if now_ms - e["ts"] < 120_000]
        if len(fresh) >= 2:
            coin, side = key.split(":")
            patterns.append({
                "coin": coin, "side": side, "n": len(fresh),
                "traders": [e["trader"] for e in fresh],
            })
    patterns.sort(key=lambda x: -x["n"])

    # Traders status
    now = time.time()
    traders = [{
        "label": t["label"], "pnl": t["pnl"], "wr": t["wr"],
        "active": (now - t_last.get(t["addr"].lower(), 0)) < 300,
    } for t in TOP_TRADERS]

    return {
        "balance":      round(bal, 4),
        "unrealized":   round(unrealized, 4),
        "n_positions":  len(positions),
        "positions":    pos_list,
        "closed_trades": closed[:20],
        "closed_count": len(closed),
        "pnl_history":  pnl_h[-60:],
        "copy_count":   copies,
        "wins":         wins,
        "losses":       losses,
        "patterns":     patterns[:3],
        "traders":      traders,
    }


def state_broadcaster():
    while True:
        try:
            sio.emit("state", build_state())
        except Exception:
            pass
        time.sleep(1)


# ── WebSocket ─────────────────────────────────────────────────────────────────

async def ws_manager():
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as ws:
                for t in TOP_TRADERS:
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "userFills", "user": t["addr"]},
                    }))
                with lock:
                    state["ws_ok"] = True
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        if msg.get("channel") != "userFills":
                            continue
                        d    = msg.get("data", {})
                        user = d.get("user", "")
                        for fill in d.get("fills", []):
                            process_fill(fill, user)
                    except Exception:
                        pass
        except Exception:
            with lock:
                state["ws_ok"] = False
            await asyncio.sleep(5)


def start_ws():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ws_manager())


# ── Polling fallback ──────────────────────────────────────────────────────────

def polling_thread():
    last = {t["addr"].lower(): 0 for t in TOP_TRADERS}
    while True:
        for t in TOP_TRADERS:
            addr = t["addr"].lower()
            if time.time() - last[addr] < 10:
                continue
            last[addr] = time.time()
            try:
                fills = fetch_fills(t["addr"])
                for fill in fills[:15]:
                    process_fill(fill, t["addr"])
            except Exception:
                pass
            time.sleep(0.3)
        time.sleep(2)


# ── Pre-load histórico ────────────────────────────────────────────────────────

def preload():
    print("  Carregando histórico dos top 10 traders...")
    for t in TOP_TRADERS:
        try:
            fills = fetch_fills(t["addr"])
            for f in fills[:20]:
                process_fill(f, t["addr"])
        except Exception:
            pass
        time.sleep(0.2)
    with lock:
        state["pnl_history"].append(0.0)
    print(f"  Pronto. {sum(len(v) for v in state['known_fills'].values())} fills carregados.")


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/state")
def api_state():
    from flask import jsonify
    return jsonify(build_state())

@app.route("/api/closed")
def api_closed():
    from flask import jsonify
    with lock:
        return jsonify(list(state["closed_trades"]))

@app.route("/backtest")
def backtest_page():
    return render_template("backtest.html")

@app.route("/api/backtest")
def api_backtest():
    import os
    from flask import jsonify
    path = "/tmp/backtest_result.json"
    if not os.path.exists(path):
        return jsonify({"error": "rode python3 backtest.py primeiro"}), 404
    with open(path) as f:
        return jsonify(json.load(f))

@app.route("/polymarket")
def polymarket_page():
    return render_template("polymarket_backtest.html")

@app.route("/hybrid")
def hybrid_page():
    return render_template("hybrid.html")

@app.route("/api/hybrid")
def api_hybrid():
    from flask import jsonify
    return jsonify(hybrid_get_state())

@app.route("/api/polymarket-backtest")
def api_polymarket_backtest():
    import os
    from flask import jsonify
    path = "/tmp/polymarket_backtest.json"
    if not os.path.exists(path):
        return jsonify({"error": "rode python3 polymarket_backtest.py primeiro"}), 404
    with open(path) as f:
        return jsonify(json.load(f))


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    preload()

    threading.Thread(target=start_ws, daemon=True).start()
    threading.Thread(target=polling_thread, daemon=True).start()
    threading.Thread(target=state_broadcaster, daemon=True).start()
    start_monitor()

    print("\n  ▓▓▓  HYPERLIQUID MATRIX COPY TRADER  ▓▓▓")
    print("  Dashboard: http://localhost:5000\n")

    sio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
