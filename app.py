#!/usr/bin/env python3
"""
QUANTEX — Polymarket Copy Trader Dashboard
Flask + SocketIO backend (Polymarket only)
"""

import json
import os
import time
import threading
import logging

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import polymarket_live as pm_live

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("quantex")

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/Users/mac/matrix_dashboard/config.json")

def load_config() -> dict:
    """
    Carrega configuração: prioridade env vars (Heroku) > config.json (local).
    Env vars usam o prefixo PM_ e STARTING_BALANCE.
    """
    base: dict = {
        "pm_private_key": "", "pm_address": "", "pm_funder": "",
        "pm_sig_type": 1, "pm_api_key": "", "pm_api_secret": "", "pm_api_pass": "",
        "starting_balance": 2000.0, "strategies": {},
    }

    # 1) Tenta carregar config.json
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                base.update(json.load(f))
        except Exception as e:
            log.warning(f"Não foi possível ler config.json: {e}")

    # 2) Env vars sobrescrevem (útil no Heroku)
    env_map = {
        "PM_PRIVATE_KEY":  "pm_private_key",
        "PM_ADDRESS":      "pm_address",
        "PM_FUNDER":       "pm_funder",
        "PM_SIG_TYPE":     "pm_sig_type",
        "PM_API_KEY":      "pm_api_key",
        "PM_API_SECRET":   "pm_api_secret",
        "PM_API_PASS":     "pm_api_pass",
        "STARTING_BALANCE":"starting_balance",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        if val:
            base[cfg_key] = int(val) if cfg_key == "pm_sig_type" else \
                            float(val) if cfg_key == "starting_balance" else val

    return base


def save_config(cfg: dict):
    """Salva config.json se o filesystem estiver disponível (local), ignora no Heroku."""
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log.warning(f"Não foi possível salvar config.json (Heroku?): {e}")


app = Flask(__name__)
sio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")


# ── State broadcaster ─────────────────────────────────────────────────────────
def state_broadcaster():
    while True:
        try:
            sio.emit("pm_state", pm_live.get_pm_state())
        except Exception as e:
            log.debug(f"state_broadcaster erro: {e}")
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
    save_config(cfg)
    pm_live._init_constants()
    log.info("Config atualizado e parâmetros recarregados")
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
    save_config(cfg)
    ok = pm_live.init(sio, key)
    return jsonify({"connected": ok, "state": pm_live.get_pm_state()})


@app.route("/api/polymarket-backtest")
def api_polymarket_backtest():
    path = "/tmp/polymarket_backtest.json"
    if not os.path.exists(path):
        return jsonify({"error": "rode python3 polymarket_backtest.py primeiro"}), 404
    with open(path) as f:
        return jsonify(json.load(f))


@app.route("/health")
def health():
    """Health check para Heroku / uptime monitors."""
    state = pm_live.get_pm_state()
    return jsonify({
        "ok":        True,
        "connected": state.get("connected", False),
        "balance":   state.get("balance", 0),
        "positions": len(state.get("live_positions", [])),
        "copies":    state.get("copies", 0),
    })


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=state_broadcaster, daemon=True).start()

    cfg = load_config()
    pm_key = cfg.get("pm_private_key", "")
    pm_live.start_pm_live(sio, pm_key)

    port = int(os.environ.get("PORT", 5000))
    log.info(f"▓▓▓  QUANTEX — Polymarket Copy Trader  ▓▓▓")
    log.info(f"Dashboard: http://localhost:{port}")

    sio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
