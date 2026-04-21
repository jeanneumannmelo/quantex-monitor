#!/usr/bin/env python3
"""
POLYMARKET LIVE — Autenticação L1/L2 via REST direto (sem py-clob-client)
Requer: eth_account (já instalado), requests
"""

import json
import os
import time
import threading
import hashlib
import hmac
import base64
import urllib.request
import urllib.parse
import logging
from datetime import datetime, timezone
from collections import deque

from eth_account import Account
from polymarket_cash import position_size, expected_exit_price, score_wallet

# ── Logger ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_log = logging.getLogger("pm_live")

def _L(msg: str):
    """Log com timestamp — aparece no Heroku logs e terminal local."""
    _log.info(msg)

# ── Endpoints ─────────────────────────────────────────────────────────────────
CLOB_API   = "https://clob.polymarket.com"
DATA_API   = "https://data-api.polymarket.com"
GAMMA_API  = "https://gamma-api.polymarket.com"

CHAIN_ID             = 137
POLL_INTERVAL        = 5
LEADERBOARD_REFRESH  = 300

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/Users/mac/matrix_dashboard/config.json")

def _load_pm_cfg() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("strategies", {}).get("pm", {})
    except Exception:
        return {}

# Globals inicializados por _init_constants() — não editar manualmente
MIN_PRICE            = 0.05
MAX_PRICE            = 0.90
TOP_WALLETS_N        = 10
MIN_WIN_RATE         = 65.0
MIN_EXIT_QUALITY     = 60.0
MIN_TRADES_WALLET    = 30
BLOCKED_SPORTS       = {"Tennis"}
MIN_HOURS_TO_RESOLVE = 6.0
KELLY_FRACTION       = 0.25
MIN_ALLOC            = 5.0
MAX_ALLOC_PCT        = 0.08
CIRCUIT_BREAKER_DD   = 0.15
MAX_DAILY_LOSS_PCT   = 0.08
MAX_CONSECUTIVE_LOSS = 3
ZOMBIE_MAX_DAYS      = 7
ZOMBIE_MAX_PRICE     = 0.06
WR_BIAS_DISCOUNT          = 0.85
PM_ENABLED                = True
MAX_TOTAL_EXPOSURE_PCT    = 0.65
STOP_LOSS_PCT             = -25.0

def _init_constants():
    global MIN_PRICE, MAX_PRICE, TOP_WALLETS_N, MIN_WIN_RATE, MIN_EXIT_QUALITY
    global MIN_TRADES_WALLET, BLOCKED_SPORTS, MIN_HOURS_TO_RESOLVE, KELLY_FRACTION
    global MIN_ALLOC, MAX_ALLOC_PCT, CIRCUIT_BREAKER_DD, MAX_DAILY_LOSS_PCT
    global MAX_CONSECUTIVE_LOSS, ZOMBIE_MAX_DAYS, ZOMBIE_MAX_PRICE
    global WR_BIAS_DISCOUNT, PM_ENABLED, MAX_TOTAL_EXPOSURE_PCT, STOP_LOSS_PCT
    c = _load_pm_cfg()
    MIN_PRICE             = c.get("min_price",             0.05)
    MAX_PRICE             = c.get("max_price",             0.90)
    TOP_WALLETS_N         = c.get("top_wallets_n",         10)
    MIN_WIN_RATE          = c.get("min_win_rate",          65.0)
    MIN_EXIT_QUALITY      = c.get("min_exit_quality",      60.0)
    MIN_TRADES_WALLET     = c.get("min_trades_wallet",     30)
    BLOCKED_SPORTS        = set(c.get("blocked_sports",    ["Tennis"]))
    MIN_HOURS_TO_RESOLVE  = c.get("min_hours_to_resolve",  6.0)
    KELLY_FRACTION        = c.get("kelly_fraction",        0.25)
    MIN_ALLOC             = c.get("min_alloc",             5.0)
    MAX_ALLOC_PCT         = c.get("max_alloc_pct",         0.08)
    CIRCUIT_BREAKER_DD    = c.get("circuit_breaker_dd",    0.15)
    MAX_DAILY_LOSS_PCT    = c.get("max_daily_loss_pct",    0.08)
    MAX_CONSECUTIVE_LOSS  = c.get("max_consecutive_loss",  3)
    ZOMBIE_MAX_DAYS       = c.get("zombie_max_days",       7)
    ZOMBIE_MAX_PRICE      = c.get("zombie_max_price",      0.06)
    WR_BIAS_DISCOUNT          = c.get("wr_bias_discount",          0.85)
    PM_ENABLED                = c.get("enabled",                   True)
    MAX_TOTAL_EXPOSURE_PCT    = c.get("max_total_exposure_pct",    0.65)
    STOP_LOSS_PCT             = c.get("stop_loss_pct",             -25.0)

_init_constants()

# ── Estado global ─────────────────────────────────────────────────────────────
pm_state = {
    "connected":          False,
    "address":            "",
    "api_key":            "",
    "api_secret":         "",
    "api_passphrase":     "",
    "usdc_balance":       0.0,
    "live_positions":     [],       # posições abertas com preço atual (DATA_API)
    "unrealized_pnl":     0.0,      # PnL não realizado de todas as posições abertas
    "positions":          {},       # condition_id -> pos dict
    "closed_trades":      deque(maxlen=50),
    "pnl_history":        deque(maxlen=120),
    "tracked_wallets":    [],
    "wallet_positions":   {},       # address -> set of conditionIds abertos pelo trader
    "wallet_eq_cache":    {},       # username -> avg_eq (do backtest)
    "wallet_wr_cache":    {},       # username -> win_rate (0–1)
    "wins":               0,
    "losses":             0,
    "copies":             0,
    "known_positions":    set(),
    "wallet_entry_cache": {},       # condition_id -> wallet's avg entry price
    # circuit breakers
    "peak_balance":       0.0,
    "session_start":      0.0,
    "daily_loss":         0.0,
    "consecutive_losses": 0,
    "paused":             False,
}
pm_lock         = threading.Lock()
_sio_ref        = None
_account        = None
_wallet_threads = {}   # address -> Thread


# ── Helpers REST ──────────────────────────────────────────────────────────────

def _get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url, body: dict, headers: dict = None, timeout=15):
    import requests
    h = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    if headers:
        h.update(headers)
    r = requests.post(url, json=body, headers=h, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _signed_get(url, timeout=10):
    """GET autenticado com L2 (API Key + HMAC)."""
    import requests
    if not pm_state["api_key"]:
        return _get(url, timeout)
    full_path = url.replace(CLOB_API, "")
    path = full_path.split("?")[0]
    ts   = str(int(time.time()))
    sig  = _hmac_sig("GET", path, "", ts)
    h = {
        "POLY_ADDRESS":    pm_state["address"],
        "POLY_SIGNATURE":  sig,
        "POLY_TIMESTAMP":  ts,
        "POLY_API_KEY":    pm_state["api_key"],
        "POLY_PASSPHRASE": pm_state["api_passphrase"],
        "Content-Type":    "application/json",
        "User-Agent":      "Mozilla/5.0",
    }
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _hmac_sig(method: str, path: str, body: str, ts: str) -> str:
    key = base64.urlsafe_b64decode(pm_state["api_secret"])
    msg = ts + method + path + (body or "")
    if body:
        msg = msg.replace("'", '"')
    h = hmac.new(key, msg.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(h.digest()).decode("utf-8")


# ── Autenticação L1 → deriva credenciais L2 ───────────────────────────────────

def _sign_eip712(ts: str, nonce: int) -> str:
    signed = _account.sign_typed_data(
        domain_data={"name": "ClobAuthDomain", "version": "1", "chainId": CHAIN_ID},
        message_types={"ClobAuth": [
            {"name": "address",   "type": "address"},
            {"name": "timestamp", "type": "string"},
            {"name": "nonce",     "type": "uint256"},
            {"name": "message",   "type": "string"},
        ]},
        message_data={
            "address":   pm_state["address"],
            "timestamp": ts,
            "nonce":     nonce,
            "message":   "This message attests that I control the given wallet",
        },
    )
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def _get_nonce() -> int:
    try:
        data = _get(f"{CLOB_API}/auth/nonce")
        return int(data.get("nonce", 0))
    except Exception:
        return 0


def _derive_api_creds():
    import urllib.request, urllib.error
    nonce = _get_nonce()
    ts    = str(int(time.time()))
    sig   = _sign_eip712(ts, nonce)
    headers = {
        "POLY_ADDRESS":   pm_state["address"],
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": ts,
        "POLY_NONCE":     str(nonce),
        "Content-Type":   "application/json",
        "User-Agent":     "Mozilla/5.0",
    }
    req = urllib.request.Request(
        f"{CLOB_API}/auth/api-key", data=b"{}", headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return data.get("apiKey", ""), data.get("secret", ""), data.get("passphrase", "")


def _load_saved_creds():
    # Prioridade: env vars (Heroku) > config.json (local)
    env_key  = os.environ.get("PM_API_KEY", "")
    env_sec  = os.environ.get("PM_API_SECRET", "")
    env_pass = os.environ.get("PM_API_PASS", "")
    if env_key and env_sec and env_pass:
        return env_key, env_sec, env_pass
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return (
            cfg.get("pm_api_key", ""),
            cfg.get("pm_api_secret", ""),
            cfg.get("pm_api_pass", "") or cfg.get("pm_api_passphrase", ""),
        )
    except Exception:
        return "", "", ""


def init(sio, private_key: str):
    global _sio_ref, _account

    _sio_ref = sio

    try:
        key = private_key.strip()
        if not key.startswith("0x"):
            key = "0x" + key

        _account = Account.from_key(key)
        addr     = _account.address

        with pm_lock:
            pm_state["address"] = addr

        _L(f"[AUTH] Endereço derivado: {addr}")

        api_key, secret, passphrase = _load_saved_creds()

        if not api_key:
            _L("[AUTH] Sem creds salvas — derivando via EIP-712…")
            api_key, secret, passphrase = _derive_api_creds()

        with pm_lock:
            pm_state["api_key"]        = api_key
            pm_state["api_secret"]     = secret
            pm_state["api_passphrase"] = passphrase
            pm_state["connected"]      = bool(api_key)

        if api_key:
            _L(f"[AUTH] ✓ Autenticado — API key: {api_key[:8]}…")
            _refresh_balance()
            with pm_lock:
                bal = pm_state["usdc_balance"]
                pm_state["peak_balance"]  = bal
                pm_state["session_start"] = bal
            return True
        else:
            _L("[AUTH] Auth retornou sem api_key — modo observação")
            return False

    except Exception as e:
        _log.exception(f"[AUTH] Erro de auth: {e}")
        return False


# ── Saldo USDC ────────────────────────────────────────────────────────────────

def _refresh_balance():
    try:
        if pm_state["api_key"]:
            data = _signed_get(f"{CLOB_API}/balance-allowance?asset_type=COLLATERAL&signature_type=1")
            bal  = float(data.get("balance", 0)) / 1e6
        else:
            addr = pm_state["address"]
            data = _get(f"{DATA_API}/profile?address={addr}")
            bal  = float(data.get("portfolioValue", data.get("usdcBalance", 0)))
        with pm_lock:
            pm_state["usdc_balance"] = bal
        _L(f"[SALDO] USDC: ${bal:.2f}")
    except Exception as e:
        _log.warning(f"[SALDO] Erro ao buscar saldo: {e}")
        try:
            addr = pm_state["address"]
            data = _get(f"{DATA_API}/profile?address={addr}")
            bal  = float(data.get("portfolioValue", data.get("usdcBalance", 0)))
            with pm_lock:
                pm_state["usdc_balance"] = bal
        except Exception:
            pass


# ── Risco por posição ─────────────────────────────────────────────────────────

def _compute_pm_risk(pos: dict, balance: float) -> dict:
    """Calcula score de risco 0-100 para uma posição PM."""
    cur = pos.get("cur_price", 0.5)
    price_risk = 1.0 if (cur < 0.15 or cur > 0.85) else (
        0.5 if (cur < 0.25 or cur > 0.75) else 0.0
    )

    time_risk = 0.5
    end_str = pos.get("end_date", "")
    try:
        from datetime import datetime, timezone
        end_dt = datetime.fromisoformat(end_str + "T00:00:00+00:00")
        hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        time_risk = max(0.0, min(1.0, 1.0 - hours_left / 24.0))
    except Exception:
        pass

    invested = pos.get("invested", 0)
    size_risk = min(1.0, (invested / max(balance, 1)) * 3)

    pnl_pct = pos.get("pnl_pct", 0) / 100.0
    pnl_risk = max(0.0, min(1.0, -pnl_pct / 0.20)) if pnl_pct < 0 else 0.0

    score = round((0.30 * price_risk + 0.25 * time_risk + 0.20 * size_risk + 0.25 * pnl_risk) * 100, 1)
    level = "HIGH" if score > 66 else "MEDIUM" if score > 33 else "LOW"
    return {"risk_score": score, "risk_level": level}


# ── Posições ao vivo com PnL ──────────────────────────────────────────────────

def _refresh_live_positions():
    """Busca posições abertas do proxy wallet e calcula PnL não realizado."""
    try:
        import requests as _req
        # Usa env var (Heroku) ou config.json (local), fallback ao address
        funder = os.environ.get("PM_FUNDER", "")
        if not funder:
            try:
                with open(CONFIG_PATH) as f:
                    funder = json.load(f).get("pm_funder", "")
            except Exception:
                pass
        if not funder:
            funder = pm_state["address"]

        r = _req.get(
            f"{DATA_API}/positions",
            params={"user": funder, "sizeThreshold": ".01", "limit": 50},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json()
        if not isinstance(raw, list):
            return

        positions = []
        unrealized = 0.0

        for p in raw:
            entry    = float(p.get("avgPrice", 0) or 0)
            cur      = float(p.get("curPrice", entry) or entry)
            invested = float(p.get("totalBought", 0) or 0)
            cash_pnl = float(p.get("cashPnl", 0) or 0)
            cur_val  = float(p.get("currentValue", 0) or 0)
            title    = p.get("title", "?")
            outcome  = p.get("outcome", "?")
            end_date = p.get("endDate", "")

            if entry <= 0 or invested <= 0:
                continue

            tokens = invested / entry if entry > 0 else 0
            pnl_pct = (cur - entry) / entry * 100 if entry > 0 else 0
            unrealized += cash_pnl

            pos_data = {
                "title":       title[:55],
                "outcome":     outcome,
                "entry":       round(entry, 3),
                "cur_price":   round(cur, 3),
                "invested":    round(invested, 2),
                "cur_value":   round(cur_val, 2),
                "pnl":         round(cash_pnl, 2),
                "pnl_pct":     round(pnl_pct, 1),
                "end_date":    end_date[:10] if end_date else "—",
                "condition_id": p.get("conditionId", ""),
            }
            risk = _compute_pm_risk(pos_data, pm_state.get("usdc_balance", 1))
            pos_data["risk_score"] = risk["risk_score"]
            pos_data["risk_level"] = risk["risk_level"]
            positions.append(pos_data)

        with pm_lock:
            pm_state["live_positions"] = positions
            pm_state["unrealized_pnl"] = round(unrealized, 2)

    except Exception as e:
        _log.warning(f"[POS] Erro ao buscar posições: {e}")


# ── Circuit Breakers ──────────────────────────────────────────────────────────

def _check_circuit_breakers() -> bool:
    """Retorna True se pode operar, False se algum limit foi atingido."""
    with pm_lock:
        if pm_state["paused"]:
            return False
        bal     = pm_state["usdc_balance"]
        peak    = pm_state["peak_balance"]
        start   = pm_state["session_start"]
        consec  = pm_state["consecutive_losses"]
        daily   = pm_state["daily_loss"]

    if peak > 0 and (peak - bal) / peak >= CIRCUIT_BREAKER_DD:
        with pm_lock:
            pm_state["paused"] = True
        _L(f"[CB] ⛔ Circuit breaker: drawdown {((peak-bal)/peak*100):.1f}% — pausado")
        return False

    if start > 0 and daily / start >= MAX_DAILY_LOSS_PCT:
        with pm_lock:
            pm_state["paused"] = True
        _L(f"[CB] ⛔ Circuit breaker: perda diária {(daily/start*100):.1f}% — pausado")
        return False

    if consec >= MAX_CONSECUTIVE_LOSS:
        _L(f"[CB] ⛔ Circuit breaker: {consec} perdas consecutivas — cooldown 15min")
        time.sleep(900)
        with pm_lock:
            pm_state["consecutive_losses"] = 0
        return False

    return True


def _update_peak_balance(pnl: float):
    with pm_lock:
        pm_state["usdc_balance"] += pnl
        bal  = pm_state["usdc_balance"]
        pm_state["peak_balance"] = max(pm_state["peak_balance"], bal)
        if pnl < 0:
            pm_state["daily_loss"]         += abs(pnl)
            pm_state["consecutive_losses"] += 1
        else:
            pm_state["consecutive_losses"] = 0


# ── Leaderboard e Score ───────────────────────────────────────────────────────

def _calc_exit_quality(pos):
    """Calcula exit quality de uma posição fechada (0-100)."""
    try:
        entry    = float(pos.get("avgPrice", 0))
        invested = float(pos.get("totalBought", 0))
        pnl      = float(pos.get("realizedPnl", 0))
        cur      = float(pos.get("curPrice", entry))
    except (TypeError, ValueError):
        return None, None
    if entry <= 0 or entry >= 1 or invested <= 0:
        return None, None
    tokens = invested / entry
    if tokens <= 0:
        return None, None
    exit_price = max(0.0, min(1.0, entry + pnl / tokens))
    if cur >= 0.95:
        denom = 1.0 - entry
        if denom < 0.01:
            return None, "win"
        eq = (exit_price - entry) / denom * 100
        return round(max(0, min(100, eq)), 1), "win"
    if cur <= 0.05:
        if entry < 0.01:
            return None, "loss"
        eq = (entry - exit_price) / entry * 100
        return round(max(0, min(100, eq)), 1), "loss"
    return None, "open"


def _get_closed_positions(address, max_pages=3):
    """Busca até max_pages * 100 posições fechadas de um wallet."""
    import requests as _req
    all_pos = []
    for page in range(max_pages):
        try:
            r = _req.get(
                f"{DATA_API}/closed-positions",
                params={"user": address, "limit": 100, "offset": page * 100,
                        "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            if r.status_code != 200:
                break
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            all_pos.extend(batch)
            if len(batch) < 100:
                break
            time.sleep(0.15)
        except Exception:
            break
    return all_pos


def _analyze_wallets(leaderboard_limit=300, n=TOP_WALLETS_N):
    """
    Busca top wallets do leaderboard, analisa histórico de posições fechadas,
    calcula EQ e WR reais e popula wallet_eq_cache / wallet_wr_cache.
    Só retorna wallets que passam nos filtros MIN_EXIT_QUALITY e MIN_WIN_RATE.
    """
    import requests as _req
    _L(f"[LB] Analisando wallets (buscando top {leaderboard_limit})...")
    lb = []
    page_size = 50
    offset = 0
    try:
        while len(lb) < leaderboard_limit:
            r = _req.get(
                f"{DATA_API}/v1/leaderboard",
                params={"category": "OVERALL", "timePeriod": "MONTH",
                        "orderBy": "PNL", "limit": page_size, "offset": offset},
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=15,
            )
            r.raise_for_status()
            page = r.json()
            if not isinstance(page, list) or not page:
                break
            lb.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
            time.sleep(0.2)
        if not lb:
            return []
    except Exception as e:
        _log.warning(f"[LB] Leaderboard erro: {e}")
        return []
    lb = lb[:leaderboard_limit]
    _L(f"[LB] {len(lb)} entradas carregadas do leaderboard")

    qualified = []
    for i, w in enumerate(lb, 1):
        addr     = w.get("proxyWallet", "")
        username = w.get("userName") or (addr[:12] if addr else "?")
        pnl      = float(w.get("pnl", 0) or 0)
        if not addr:
            continue

        _L(f"[LB] [{i:02d}/{len(lb)}] Analisando {username[:28]}")

        positions = _get_closed_positions(addr)
        time.sleep(0.25)

        if not positions:
            continue

        eq_wins, eq_losses, all_eq = [], [], []
        for pos in positions:
            eq, kind = _calc_exit_quality(pos)
            if eq is None:
                continue
            all_eq.append(eq)
            if kind == "win":
                eq_wins.append(eq)
            elif kind == "loss":
                eq_losses.append(eq)

        if len(all_eq) < MIN_TRADES_WALLET:
            continue

        avg_eq   = sum(all_eq) / len(all_eq)
        win_rate = len(eq_wins) / len(all_eq) * 100

        if win_rate < MIN_WIN_RATE or avg_eq < MIN_EXIT_QUALITY:
            continue

        wallet = {
            "username": username,
            "address":  addr,
            "pnl":      pnl,
            "win_rate": round(win_rate, 1),
            "avg_eq":   round(avg_eq, 1),
            "n_trades": len(all_eq),
        }
        qualified.append(wallet)

        with pm_lock:
            pm_state["wallet_eq_cache"][username] = round(avg_eq, 1)
            pm_state["wallet_wr_cache"][username] = round(win_rate / 100, 4)

    qualified.sort(key=lambda x: -score_wallet(x))
    top = qualified[:n]

    with pm_lock:
        pm_state["tracked_wallets"] = top

    _L(f"[LB] ✓ {len(top)} wallets qualificados (EQ≥{MIN_EXIT_QUALITY}% WR≥{MIN_WIN_RATE}%)")
    for w in top:
        _L(f"[LB]   {w['username'][:28]:<28}  EQ={w['avg_eq']:.0f}%  WR={w['win_rate']:.0f}%  trades={w['n_trades']}")

    return top


def fetch_top_wallets(n=TOP_WALLETS_N):
    """Analisa o leaderboard e atualiza tracked_wallets. Sempre re-analisa."""
    return _analyze_wallets(leaderboard_limit=300, n=n)


def fetch_wallet_positions(address: str):
    try:
        data = _get(f"{DATA_API}/positions?user={address}&sizeThreshold=.1&limit=50")
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ── Classificação de esporte ──────────────────────────────────────────────────

import re as _re
SPORT_BLOCK_RE = {
    "Tennis": _re.compile(r"\b(atp|wta|tennis|wimbledon|french open|australian open|roland garros|madrid open|rome open|miami open|indian wells|monte carlo|davis cup|fed cup|laver cup|qualifier|qualification)\b", _re.I),
    "NHL":    _re.compile(r"\b(nhl|hockey|stanley cup|avalanche|penguins|capitals|bruins|maple leafs|canadiens|rangers|devils|lightning|hurricanes|red wings|predators|blues|blackhawks|wild|jets|flames|oilers|canucks|kraken|sharks|ducks|kings|stars)\b", _re.I),
    "MLB":    _re.compile(r"\b(mlb|baseball|world series|yankees|red sox|dodgers|cubs|astros|mets|braves|cardinals|phillies|padres|brewers|reds|pirates|rockies|diamondbacks|mariners|athletics|rangers|angels|blue jays|rays|orioles|twins|white sox|guardians|royals|tigers|nationals|marlins|giants)\b", _re.I),
    "NFL":    _re.compile(r"\b(nfl|super bowl|chiefs|eagles|cowboys|patriots|49ers|packers|steelers|ravens|bills|bengals|broncos|chargers|raiders|dolphins|jets|commanders|saints|buccaneers|falcons|panthers|bears|lions|vikings|seahawks|rams|cardinals|titans|colts|jaguars|texans)\b", _re.I),
    "NBA":    _re.compile(r"\b(nba|basketball|lakers|celtics|warriors|bucks|heat|nuggets|suns|nets|knicks|76ers|sixers|bulls|raptors|cavaliers|pistons|hornets|hawks|magic|pacers|jazz|thunder|rockets|spurs|clippers|pelicans|grizzlies|timberwolves|blazers|kings|mavericks|mavs)\b", _re.I),
    "Soccer": _re.compile(r"\b(premier league|champions league|la liga|serie a|bundesliga|ligue 1|europa league|fa cup|epl|chelsea|arsenal|liverpool|manchester|man city|man utd|tottenham|newcastle|crystal palace|west ham|everton|fulham|wolves|brighton|aston villa|real madrid|barcelona|atletico|juventus|inter|milan|napoli|psg|bayern|dortmund|ajax|porto|benfica|celtic)\b", _re.I),
    "MMA":    _re.compile(r"\b(ufc|mma|bellator|boxing|fight night|knockout|ko|tko|submission|bout)\b", _re.I),
}

def classify_sport(title: str):
    for sport, pat in SPORT_BLOCK_RE.items():
        if pat.search(title):
            return sport
    return None


_market_end_cache: dict = {}

def _get_market_end_date(condition_id: str) -> float:
    """Retorna timestamp Unix da data de resolução do mercado, ou 0 se não encontrado."""
    if condition_id in _market_end_cache:
        return _market_end_cache[condition_id]
    try:
        import requests as _req
        r = _req.get(
            "https://gamma-api.polymarket.com/markets",
            params={"conditionIds": condition_id},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6,
        )
        data = r.json()
        if data and isinstance(data, list):
            from datetime import datetime, timezone
            end_str = data[0].get("endDate", "")
            if end_str:
                dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                ts = dt.timestamp()
                _market_end_cache[condition_id] = ts
                return ts
    except Exception:
        pass
    _market_end_cache[condition_id] = 0
    return 0


# ── CLOB client ───────────────────────────────────────────────────────────────

def _get_clob_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    # Usa pm_state (já populado no init) — sem ler config.json novamente
    with pm_lock:
        api_key    = pm_state["api_key"]
        api_secret = pm_state["api_secret"]
        api_pass   = pm_state["api_passphrase"]
        address    = pm_state["address"]

    # Chave privada e funder: env vars (Heroku) > config.json (local)
    private_key = os.environ.get("PM_PRIVATE_KEY", "")
    funder      = os.environ.get("PM_FUNDER", "")
    if not private_key or not funder:
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            private_key = private_key or cfg.get("pm_private_key", "")
            funder      = funder      or cfg.get("pm_funder", address)
        except Exception:
            pass
    if not funder:
        funder = address

    creds = ApiCreds(
        api_key        = api_key,
        api_secret     = api_secret,
        api_passphrase = api_pass,
    )
    return ClobClient(
        host           = CLOB_API,
        key            = private_key,
        chain_id       = CHAIN_ID,
        creds          = creds,
        signature_type = 1,
        funder         = funder,
    )


# ── Execução de ordens ────────────────────────────────────────────────────────

def execute_copy_trade(condition_id: str, token_id: str, side: str, price: float,
                       title: str, wallet_username: str = ""):
    if not pm_state["connected"] or not pm_state["api_key"]:
        _emit_feed(f"[sim] {side}", title, price, 0)
        return False

    if not _check_circuit_breakers():
        return False

    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        with pm_lock:
            bal      = pm_state["usdc_balance"]
            raw_wr   = pm_state["wallet_wr_cache"].get(wallet_username, 0.65)
            eq       = pm_state["wallet_eq_cache"].get(wallet_username, 60.0)
            w_entry  = pm_state["wallet_entry_cache"].get(condition_id, price)

        # Fix F: desconto de survivorship bias no win rate reportado
        wr = raw_wr * WR_BIAS_DISCOUNT

        avg_exit = expected_exit_price(price, eq)

        # Fix B: checar slippage — quanto do movimento o trader já capturou
        total_move     = max(avg_exit - w_entry, 1e-6)
        remaining_move = max(avg_exit - price, 0.0)
        slippage_ratio = 1.0 - (remaining_move / total_move)

        if slippage_ratio > 0.30:
            _L(f"[SKIP] Slippage {slippage_ratio*100:.0f}% — {title[:45]}")
            return False

        slippage_factor = remaining_move / total_move

        max_alloc  = bal * MAX_ALLOC_PCT
        skip_ev    = _load_pm_cfg().get("skip_negative_ev", True)
        full_alloc = position_size(
            balance          = bal,
            p_win            = wr,
            entry_price      = price,
            avg_exit_price   = avg_exit,
            kelly_frac       = KELLY_FRACTION,
            min_size         = MIN_ALLOC,
            max_size         = max_alloc,
            skip_negative_ev = skip_ev,
        )

        # Fix A gate: Kelly retornou 0 → EV negativo, não entrar
        if full_alloc == 0.0:
            _L(f"[SKIP] EV negativo (Kelly=0) — {title[:45]}")
            return False

        alloc = max(MIN_ALLOC, round(full_alloc * slippage_factor, 2))

        if alloc > bal:
            _L(f"[SKIP] Saldo insuficiente (alloc=${alloc:.2f} > bal=${bal:.2f})")
            return False

        # Gate de exposição: só conta posições que o BOT copiou (não as manuais do usuário)
        with pm_lock:
            bot_pos        = [p for p in pm_state["live_positions"]
                              if p.get("condition_id") in pm_state["positions"]
                              and p.get("cur_price", 1) > ZOMBIE_MAX_PRICE]
            total_invested = sum(p.get("invested", 0) for p in bot_pos)
        max_exposure = bal * MAX_TOTAL_EXPOSURE_PCT
        hard_cap     = _load_pm_cfg().get("max_positions_hard_cap", 15)
        if len(bot_pos) >= hard_cap:
            _L(f"[GATE] hard_cap ({len(bot_pos)}/{hard_cap}) — skip: {title[:45]}")
            return False
        if total_invested + alloc > max_exposure:
            _L(f"[GATE] exposure ${total_invested+alloc:.0f}/${max_exposure:.0f} ({total_invested/bal*100:.0f}% usado) — skip: {title[:45]}")
            return False

        # Pular mercados neg-risk (spreads esportivos) — usam AMM, não CLOB
        if not token_id:
            _L(f"[SKIP] token_id vazio — {title[:45]}")
            return False
        try:
            import requests as _req
            nr = _req.get(f"{CLOB_API}/neg-risk",
                          params={"token_id": token_id},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=5).json()
            if nr.get("neg_risk") is True:
                _L(f"[SKIP] neg-risk market (AMM) — {title[:45]}")
                return False
        except Exception:
            pass

        client = _get_clob_client()

        order = client.create_market_order(MarketOrderArgs(
            token_id     = token_id,
            amount       = alloc,
            price        = price,
            side         = "BUY",
            fee_rate_bps = 0,
        ))

        resp = client.post_order(order, OrderType.FOK)

        if resp.get("success") or resp.get("orderID"):
            with pm_lock:
                pm_state["usdc_balance"] -= alloc
                pm_state["copies"]       += 1
                pm_state["positions"][condition_id] = {
                    "condition_id":   condition_id,
                    "token_id":       token_id,
                    "question":       title,
                    "side":           side,
                    "entry_price":    price,
                    "size_usd":       alloc,
                    "wallet":         wallet_username,
                    "ts":             time.time(),
                }
            _emit_feed(f"COPY {side}", title, price, alloc)
            _L(f"[TRADE] ✅ COPY {side}  ${alloc:.2f}  @{price:.3f}  WR={wr:.0%}(×{WR_BIAS_DISCOUNT})  EQ={eq:.0f}%  slip={slippage_ratio*100:.0f}%  | {title[:50]}")
            return True
        else:
            _L(f"[TRADE] ❌ Ordem rejeitada: {resp} | {title[:40]}")
            return False

    except Exception as e:
        err = str(e)
        if "does not exist" in err or "FOK" in err or "fully filled" in err:
            _L(f"[TRADE] sem liquidez/orderbook — {title[:40]}")
        elif "owner" in err.lower() or "api key" in err.lower():
            _log.warning(f"[TRADE] Erro de autenticação: endereço/API key incompatíveis — {err[:120]}")
        else:
            _log.warning(f"[TRADE] Erro na ordem: {e} | {title[:40]}")
        return False


def execute_exit_trade(condition_id: str):
    """Fecha posição aberta quando o trader sai do mercado."""
    with pm_lock:
        pos = pm_state["positions"].get(condition_id)

    if not pos:
        return

    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        token_id = pos.get("token_id", "")
        if not token_id:
            print(f"  [PM] Exit: sem token_id para {condition_id[:10]}")
            return

        client  = _get_clob_client()
        size_usd = pos["size_usd"]

        order = client.create_market_order(MarketOrderArgs(
            token_id     = token_id,
            amount       = size_usd,
            price        = 0.5,
            side         = "SELL",
            fee_rate_bps = 0,
        ))

        resp = client.post_order(order, OrderType.FOK)

        # Estimar PnL com base no preço atual de mercado
        try:
            book = client.get_order_book(token_id)
            cur_price = float(book.get("midpoint") or book.get("mid", pos["entry_price"]))
        except Exception:
            cur_price = pos["entry_price"]

        gross_pnl = size_usd * (cur_price - pos["entry_price"]) / pos["entry_price"]
        fee       = size_usd * 0.02
        pnl       = gross_pnl - fee

        with pm_lock:
            if condition_id in pm_state["positions"]:
                del pm_state["positions"][condition_id]
            if pnl > 0:
                pm_state["wins"] += 1
            else:
                pm_state["losses"] += 1
            pm_state["closed_trades"].appendleft({
                "condition_id": condition_id,
                "question":     pos["question"],
                "entry":        pos["entry_price"],
                "exit":         cur_price,
                "size_usd":     size_usd,
                "pnl":          round(pnl, 4),
                "ts":           time.time(),
            })

        _update_peak_balance(pnl)
        sign = "+" if pnl >= 0 else ""
        _emit_feed("EXIT", pos["question"], cur_price, pnl)
        _L(f"[EXIT] {'✅' if pnl>=0 else '❌'}  {sign}${pnl:.2f}  @{cur_price:.3f}  | {pos['question'][:50]}")

    except Exception as e:
        _log.warning(f"[EXIT] Erro no exit: {e}")


# ── Zombie cleanup ───────────────────────────────────────────────────────────

def _cleanup_zombie_positions():
    """
    Fecha posições zumbi em dois passes:

    FAST: posições com preço < ZOMBIE_MAX_PRICE (mercado resolveu contra),
          independente da idade — age em horas, não dias.

    SLOW: posições antigas (> ZOMBIE_MAX_DAYS) com preço baixo e trader saiu.
    """
    now = time.time()
    with pm_lock:
        open_pos = dict(pm_state["positions"])
        live_pos = list(pm_state["live_positions"])
        all_trader_ids = set()
        for ids in pm_state["wallet_positions"].values():
            all_trader_ids |= ids

    # Mapa condition_id → cur_price das posições ao vivo
    live_prices = {lp.get("condition_id"): lp.get("cur_price") for lp in live_pos}

    for cid, pos in open_pos.items():
        cur_price = live_prices.get(cid)
        if cur_price is None:
            continue

        age_hours = (now - pos.get("ts", now)) / 3600

        # FAST: mercado resolveu contra (preço ~0) — fechar imediatamente após 2h
        if cur_price <= ZOMBIE_MAX_PRICE and age_hours >= 2.0:
            _L(f"[ZOMBIE] Fast: age={age_hours:.1f}h price={cur_price:.4f} → {pos['question'][:45]}")
            execute_exit_trade(cid)
            continue

        # SLOW: posição velha, trader saiu, preço baixo
        age_days = age_hours / 24
        if age_days >= ZOMBIE_MAX_DAYS and cid not in all_trader_ids and cur_price <= ZOMBIE_MAX_PRICE:
            _L(f"[ZOMBIE] Slow: age={age_days:.1f}d price={cur_price:.3f} → {pos['question'][:45]}")
            execute_exit_trade(cid)

    # RESOLVED: posições com end_date já passado há mais de 24h (mercado encerrado)
    for lp in live_pos:
        cid      = lp.get("condition_id", "")
        end_date = lp.get("end_date", "")
        if not cid or not end_date or end_date == "—":
            continue
        try:
            end_ts = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            continue
        age_past_end_hours = (now - end_ts) / 3600
        if age_past_end_hours >= 24:
            _L(f"[ZOMBIE] Resolvido: end={end_date} passado {age_past_end_hours:.0f}h → {lp.get('title','')[:45]}")
            execute_exit_trade(cid)


# ── Stop Loss próprio ────────────────────────────────────────────────────────

def _check_stop_loss():
    """Fecha posições do bot que atingiram stop loss ou take profit."""
    with pm_lock:
        live    = list(pm_state["live_positions"])
        bot_ids = set(pm_state["positions"].keys())
        eq_cache = dict(pm_state["wallet_wr_cache"])  # usamos eq_cache para TP

    for pos in live:
        cid       = pos.get("condition_id", "")
        pnl_pct   = pos.get("pnl_pct", 0)
        cur_price = pos.get("cur_price", 0)
        entry     = pos.get("entry", 0)
        if cid not in bot_ids or entry <= 0:
            continue

        # Stop loss próprio
        if pnl_pct <= STOP_LOSS_PCT:
            _L(f"[STOPLOSS] ⛔ {pnl_pct:.1f}% ≤ {STOP_LOSS_PCT:.0f}% → fechando: {pos.get('title','')[:50]}")
            execute_exit_trade(cid)
            continue

        # Take profit: cur_price >= 95% do expected_exit calculado no momento da entrada
        # expected_exit = entry + (1 - entry) × (EQ/100)
        with pm_lock:
            wallet_username = pm_state["positions"].get(cid, {}).get("wallet", "")
            eq = pm_state["wallet_eq_cache"].get(wallet_username, 60.0)
        expected_exit = entry + (1.0 - entry) * (eq / 100.0)
        tp_threshold  = expected_exit * 0.95
        if cur_price >= tp_threshold:
            _L(f"[TAKEPROFIT] ✅ {pnl_pct:.1f}% | price={cur_price:.3f} ≥ target={tp_threshold:.3f} → fechando: {pos.get('title','')[:50]}")
            execute_exit_trade(cid)


# ── Monitor por wallet ────────────────────────────────────────────────────────

def _check_wallet(wallet: dict):
    if not PM_ENABLED:
        return
    positions = fetch_wallet_positions(wallet["address"])
    addr      = wallet["address"]

    current_ids = {
        (pos.get("conditionId") or pos.get("market", ""))
        for pos in positions
        if (pos.get("conditionId") or pos.get("market", ""))
    }

    with pm_lock:
        prev_ids = pm_state["wallet_positions"].get(addr, set())
        pm_state["wallet_positions"][addr] = current_ids

    # Exit mirroring: trader saiu dessas posições
    exited_ids = prev_ids - current_ids
    for cid in exited_ids:
        with pm_lock:
            has_pos = cid in pm_state["positions"]
        if has_pos:
            _L(f"[MIRROR] {wallet['username']} saiu de {cid[:12]}… → fechando cópia")
            execute_exit_trade(cid)

    # Entry mirroring: novas posições do trader
    for pos in positions:
        cid   = pos.get("conditionId") or pos.get("market", "")
        title = pos.get("title") or pos.get("market", "?")
        price = float(pos.get("curPrice") or pos.get("price", 0.5))
        side  = "BUY"  # sempre compra o mesmo token que o trader tem
        # campo real é "asset", não "tokenId"
        token = str(pos.get("asset") or pos.get("tokenId") or pos.get("tokenID", ""))

        if not cid:
            continue
        if not (MIN_PRICE <= price <= MAX_PRICE):
            continue
        if classify_sport(title) in BLOCKED_SPORTS:
            continue
        # Rejeita mercados que resolvem em menos de MIN_HOURS_TO_RESOLVE horas
        end_ts = _get_market_end_date(cid)
        if end_ts > 0 and (end_ts - time.time()) < MIN_HOURS_TO_RESOLVE * 3600:
            continue

        with pm_lock:
            already = cid in pm_state["known_positions"]

        if not already:
            # Fix B: cache da entrada real do trader para cálculo de slippage
            wallet_avg_entry = float(pos.get("avgPrice") or pos.get("price", price))
            with pm_lock:
                pm_state["known_positions"].add(cid)
                pm_state["wallet_entry_cache"][cid] = wallet_avg_entry
            _L(f"[SIGNAL] {wallet['username']} | @{price:.3f} | entry_trader={wallet_avg_entry:.3f} | {title[:50]}")
            _emit_feed(f"📡 {wallet['username']}", title, price, 0)
            execute_copy_trade(cid, token, side, price, title, wallet["username"])


def _wallet_watcher(wallet: dict):
    """Thread dedicada por wallet — polling a cada POLL_INTERVAL segundos."""
    name = wallet["username"]
    _L(f"[WATCH] Thread iniciada: {name}")
    while True:
        try:
            _check_wallet(wallet)
        except Exception as e:
            _log.warning(f"[WATCH] {name} erro: {e}")
        time.sleep(POLL_INTERVAL)


def _poller():
    """Thread principal: inicializa watchers e refresha a lista periodicamente."""
    _L("[POLLER] Iniciando — buscando wallets e posições...")
    fetch_top_wallets()
    _spawn_watchers()
    _refresh_balance()
    _refresh_live_positions()
    _emit_state()

    last_lb_refresh = time.time()

    last_zombie_check = time.time()

    while True:
        try:
            time.sleep(30)
            _init_constants()  # hot-reload de parâmetros do config
            _refresh_balance()
            _refresh_live_positions()
            _check_stop_loss()
            _emit_state()
            with pm_lock:
                n_wallets    = len(pm_state.get("tracked_wallets", []))
                n_pos        = len(pm_state.get("live_positions", []))
                bal          = pm_state.get("usdc_balance", 0)
                bot_pos      = [p for p in pm_state["live_positions"]
                                if p.get("condition_id") in pm_state["positions"]]
                invested_bot = sum(p.get("invested", 0) for p in bot_pos)
            exposure_pct = (invested_bot / bal * 100) if bal > 0 else 0
            _L(f"[POLLER] ✓ {n_wallets} wallets | {n_pos} pos total ({len(bot_pos)} bot) | "
               f"${invested_bot:.0f} investido ({exposure_pct:.0f}%) | "
               f"lb em {max(0, int(LEADERBOARD_REFRESH - (time.time() - last_lb_refresh)))}s")

            if time.time() - last_zombie_check >= 1800:  # a cada 30min
                _cleanup_zombie_positions()
                last_zombie_check = time.time()

            if time.time() - last_lb_refresh >= LEADERBOARD_REFRESH:
                fetch_top_wallets()
                _spawn_watchers()
                last_lb_refresh = time.time()

        except Exception as e:
            _log.warning(f"[POLLER] Erro: {e}")


def _spawn_watchers():
    """Inicia threads de watcher para wallets novas."""
    global _wallet_threads
    with pm_lock:
        wallets = list(pm_state["tracked_wallets"])

    for w in wallets:
        addr = w.get("address", "")
        if not addr or addr in _wallet_threads:
            continue
        t = threading.Thread(target=_wallet_watcher, args=(w,), daemon=True,
                             name=f"pm-watch-{w['username'][:12]}")
        t.start()
        _wallet_threads[addr] = t
        _L(f"[WATCH] Spawn: {w['username']}")


# ── Emissão Socket.IO ─────────────────────────────────────────────────────────

def _emit_feed(action, market, entry=0.0, alloc=0.0):
    if not _sio_ref:
        return
    ts   = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
    item = {"ts": ts, "market": str(market)[:60], "entry": round(float(entry), 3),
            "eq": round(float(alloc), 2), "action": action}
    with pm_lock:
        pm_state.setdefault("feed", deque(maxlen=100)).appendleft(item)
    _sio_ref.emit("pm_feed", item)


def _emit_state():
    if not _sio_ref:
        return
    with pm_lock:
        bal         = pm_state["usdc_balance"]
        live_pos    = list(pm_state["live_positions"])
        wallets     = list(pm_state["tracked_wallets"])
        wins        = pm_state["wins"]
        losses      = pm_state["losses"]
        copies      = pm_state["copies"]
        conn        = pm_state["connected"]
        addr        = pm_state["address"]
        paused      = pm_state["paused"]
        peak        = pm_state["peak_balance"]
        unrealized  = pm_state["unrealized_pnl"]

    total    = wins + losses
    wr       = round(wins / total * 100, 1) if total else 0.0
    dd       = round((peak - bal) / peak * 100, 1) if peak > 0 else 0.0
    equity   = round(bal + sum(p["cur_value"] for p in live_pos), 2)

    _sio_ref.emit("pm_state", {
        "connected":       conn,
        "address":         addr,
        "balance":         round(bal, 2),
        "equity":          equity,
        "unrealized_pnl":  unrealized,
        "live_positions":  live_pos,
        "tracked_wallets": wallets[:10],
        "wins":            wins,
        "losses":          losses,
        "win_rate":        wr,
        "copies":          copies,
        "paused":          paused,
        "drawdown_pct":    dd,
    })


# ── API pública ───────────────────────────────────────────────────────────────

def get_pm_state():
    with pm_lock:
        bal      = pm_state["usdc_balance"]
        live_pos = list(pm_state["live_positions"])
        equity   = round(bal + sum(p["cur_value"] for p in live_pos), 2)
        return {
            "connected":       pm_state["connected"],
            "address":         pm_state["address"],
            "balance":         bal,
            "equity":          equity,
            "unrealized_pnl":  pm_state["unrealized_pnl"],
            "live_positions":  live_pos,
            "tracked_wallets": pm_state["tracked_wallets"],
            "copies":          pm_state["copies"],
            "wins":            pm_state["wins"],
            "losses":          pm_state["losses"],
            "paused":          pm_state["paused"],
        }


def start_pm_live(sio, private_key: str):
    global _sio_ref
    _sio_ref = sio

    if not private_key:
        with pm_lock:
            pm_state["connected"] = False
        threading.Thread(target=_poller, daemon=True, name="pm-poller").start()
        print("  [PM] Modo observação (sem chave privada)")
        return False

    ok = init(sio, private_key)
    threading.Thread(target=_poller, daemon=True, name="pm-poller").start()
    return ok
