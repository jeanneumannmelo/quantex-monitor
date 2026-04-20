#!/usr/bin/env python3
"""
QUANTEX — Polymarket Copy Trader Dashboard
Flask + SocketIO backend (Polymarket only)
"""

import json
import time
import threading

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import polymarket_live as pm_live

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = "/Users/mac/matrix_dashboard/config.json"

def load_config():
    import os
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {
        "pm_private_key": "", "pm_address": "",
        "starting_balance": 2000.0,
    }

app = Flask(__name__)
sio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


# ── State broadcaster ─────────────────────────────────────────────────────────
def state_broadcaster():
    while True:
        try:
            sio.emit("pm_state", pm_live.get_pm_state())
        except Exception:
            pass
        time.sleep(1)


# ── Flask routes ──────────────────────────────────────────────────────────────
@app.after_request
def no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/polymarket")
def polymarket_page():
    return render_template("polymarket_backtest.html")


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        cfg = load_config()
        cfg.pop("pm_private_key", None)
        cfg.pop("hl_private_key", None)
        return jsonify(cfg)
    data = request.get_json(force=True) or {}
    cfg = load_config()
    for k in ("starting_balance", "pm_private_key", "pm_address"):
        if k in data:
            cfg[k] = data[k]
    if "strategies" in data:
        cfg.setdefault("strategies", {})
        if "pm" in data["strategies"]:
            cfg["strategies"].setdefault("pm", {}).update(data["strategies"]["pm"])
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    pm_live._init_constants()
    return jsonify({"ok": True})


@app.route("/api/pm/state")
def api_pm_state():
    return jsonify(pm_live.get_pm_state())


@app.route("/api/pm/wallets")
def api_pm_wallets():
    wallets = pm_live.fetch_top_wallets(20)
    return jsonify(wallets)


@app.route("/api/pm/risk")
def api_pm_risk():
    positions = pm_live.pm_state.get("live_positions", [])
    return jsonify([{
        "title":      p.get("title", ""),
        "risk_score": p.get("risk_score", 0),
        "risk_level": p.get("risk_level", "LOW"),
        "pnl_pct":    p.get("pnl_pct", 0),
        "cur_price":  p.get("cur_price", 0),
    } for p in positions])


@app.route("/api/pm/connect", methods=["POST"])
def api_pm_connect():
    data = request.get_json(force=True) or {}
    key  = data.get("private_key", "")
    if not key:
        return jsonify({"error": "private_key obrigatório"}), 400
    cfg = load_config()
    cfg["pm_private_key"] = key
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    ok = pm_live.init(sio, key)
    return jsonify({"connected": ok, "state": pm_live.get_pm_state()})


@app.route("/api/polymarket-backtest")
def api_polymarket_backtest():
    import os
    path = "/tmp/polymarket_backtest.json"
    if not os.path.exists(path):
        return jsonify({"error": "rode python3 polymarket_backtest.py primeiro"}), 404
    with open(path) as f:
        return jsonify(json.load(f))


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=state_broadcaster, daemon=True).start()

    cfg = load_config()
    pm_key = cfg.get("pm_private_key", "")
    pm_live.start_pm_live(sio, pm_key)

    print("\n  ▓▓▓  QUANTEX — Polymarket Copy Trader  ▓▓▓")
    print("  Dashboard: http://localhost:5000\n")

    sio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
