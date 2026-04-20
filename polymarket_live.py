#!/usr/bin/env python3
"""
POLYMARKET LIVE — Autenticação L1/L2 via REST direto (sem py-clob-client)
Requer: eth_account (já instalado), requests
"""

import json
import time
import threading
import hashlib
import hmac
import base64
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from collections import deque

from eth_account import Account

# ── Endpoints ─────────────────────────────────────────────────────────────────
CLOB_API   = "https://clob.polymarket.com"
DATA_API   = "https://data-api.polymarket.com"
GAMMA_API  = "https://gamma-api.polymarket.com"

CHAIN_ID        = 137   # Polygon
POLL_INTERVAL   = 30    # segundos entre checagens
ALLOC_PCT       = 0.06
MAX_ALLOC       = 500.0
MIN_PRICE       = 0.04
MAX_PRICE       = 0.92
TOP_WALLETS_N   = 12
BLOCKED_SPORTS  = {"NHL", "MLB", "NFL"}

# ── Estado global ─────────────────────────────────────────────────────────────
pm_state = {
    "connected":       False,
    "address":         "",
    "api_key":         "",
    "api_secret":      "",
    "api_passphrase":  "",
    "usdc_balance":    0.0,
    "positions":       {},
    "closed_trades":   deque(maxlen=50),
    "pnl_history":     deque(maxlen=120),
    "tracked_wallets": [],
    "wins":  0,
    "losses": 0,
    "copies": 0,
    "known_positions": set(),
}
pm_lock  = threading.Lock()
_sio_ref = None
_account = None   # eth_account.Account local


# ── Helpers REST ──────────────────────────────────────────────────────────────

def _get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url, body: dict, headers: dict = None, timeout=15):
    import requests  # já disponível no env
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
    # Sign only the path portion (before ?), not query params
    full_path = url.replace(CLOB_API, "")
    path = full_path.split("?")[0]
    ts   = str(int(time.time()))
    sig  = _hmac_sig("GET", path, "", ts)
    h = {
        "POLY_ADDRESS":        pm_state["address"],
        "POLY_SIGNATURE":      sig,
        "POLY_TIMESTAMP":      ts,
        "POLY_API_KEY":        pm_state["api_key"],
        "POLY_PASSPHRASE":     pm_state["api_passphrase"],
        "Content-Type":        "application/json",
        "User-Agent":          "Mozilla/5.0",
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
    """Assina EIP-712 ClobAuth — formato exato do Polymarket CLOB."""
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
    """Deriva credenciais L2 via EIP-712 → /auth/api-key."""
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
    """Tenta carregar credenciais salvas em config.json."""
    try:
        with open("/Users/mac/matrix_dashboard/config.json") as f:
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

        print(f"  [PM] Endereço derivado: {addr}")

        # Try saved credentials first to avoid re-deriving (avoid nonce conflicts)
        api_key, secret, passphrase = _load_saved_creds()

        if not api_key:
            print("  [PM] Sem creds salvas — derivando via EIP-712…")
            api_key, secret, passphrase = _derive_api_creds()

        with pm_lock:
            pm_state["api_key"]        = api_key
            pm_state["api_secret"]     = secret
            pm_state["api_passphrase"] = passphrase
            pm_state["connected"]      = bool(api_key)

        if api_key:
            print(f"  [PM] Autenticado — API key: {api_key[:8]}…")
            _refresh_balance()
            return True
        else:
            print(f"  [PM] Auth retornou sem api_key — modo observação")
            return False

    except Exception as e:
        print(f"  [PM] Erro de auth: {e}")
        return False


# ── Saldo USDC ────────────────────────────────────────────────────────────────

def _refresh_balance():
    try:
        # Try authenticated CLOB balance endpoint first
        if pm_state["api_key"]:
            data = _signed_get(f"{CLOB_API}/balance-allowance?asset_type=COLLATERAL")
            bal  = float(data.get("balance", 0))
        else:
            addr = pm_state["address"]
            data = _get(f"{DATA_API}/profile?address={addr}")
            bal  = float(data.get("portfolioValue", data.get("usdcBalance", 0)))
        with pm_lock:
            pm_state["usdc_balance"] = bal
        print(f"  [PM] Saldo USDC: ${bal:.2f}")
    except Exception as e:
        print(f"  [PM] Erro ao buscar saldo: {e}")
        # Fallback to public profile API
        try:
            addr = pm_state["address"]
            data = _get(f"{DATA_API}/profile?address={addr}")
            bal  = float(data.get("portfolioValue", data.get("usdcBalance", 0)))
            with pm_lock:
                pm_state["usdc_balance"] = bal
        except Exception:
            pass


# ── Leaderboard ───────────────────────────────────────────────────────────────

def fetch_top_wallets(n=TOP_WALLETS_N):
    try:
        data = _get(f"{DATA_API}/leaderboard?limit={n*3}&offset=0")
        wallets = []
        for w in data:
            if len(wallets) >= n:
                break
            trades = w.get("tradesCount", 0)
            if trades < 5:
                continue
            wins = w.get("profitablePositionsCount", 0)
            wr   = wins / trades * 100 if trades else 0
            if wr < 60:
                continue
            wallets.append({
                "username": w.get("name") or w.get("pseudonym") or w.get("proxyWalletAddress","?")[:10],
                "address":  w.get("proxyWalletAddress", ""),
                "pnl":      float(w.get("profit", 0)),
                "trades":   trades,
                "win_rate": round(wr, 1),
            })
        with pm_lock:
            pm_state["tracked_wallets"] = wallets
        return wallets
    except Exception as e:
        print(f"  [PM] Leaderboard erro: {e}")
        return []


def fetch_wallet_positions(address: str):
    try:
        data = _get(f"{DATA_API}/positions?user={address}&sizeThreshold=.1&limit=50")
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ── Classificação de esporte ──────────────────────────────────────────────────

SPORT_PATTERNS = {
    "NHL": ["nhl","hockey","stanley cup"],
    "MLB": ["mlb","baseball","world series"],
    "NFL": ["nfl","super bowl","american football"],
}

def classify_sport(title: str):
    t = title.lower()
    for sport, kws in SPORT_PATTERNS.items():
        if any(k in t for k in kws):
            return sport
    return None


# ── Execução de ordem ─────────────────────────────────────────────────────────

def execute_copy_trade(condition_id: str, token_id: str, side: str, price: float, title: str):
    """
    Compra YES ou NO num mercado Polymarket via market order (FOK).
    Requer L2 autenticado.
    """
    if not pm_state["connected"] or not pm_state["api_key"]:
        _emit_feed(f"[sim] {side}", title, price, 0)
        return False

    try:
        import requests

        with pm_lock:
            bal   = pm_state["usdc_balance"]
            alloc = min(bal * ALLOC_PCT, MAX_ALLOC)

        if alloc < 5.0:
            print("  [PM] Saldo insuficiente para trade")
            return False

        ts   = str(int(time.time()))
        body = {
            "order": {
                "tokenID":   token_id,
                "side":      "BUY",
                "type":      "MARKET",
                "amount":    str(round(alloc, 2)),
                "makerAmount": str(round(alloc, 2)),
            }
        }
        body_str = json.dumps(body, separators=(",",":"))
        sig  = _hmac_sig("POST", "/order", body_str, ts)

        headers = {
            "POLY_ADDRESS":    pm_state["address"],
            "POLY_SIGNATURE":  sig,
            "POLY_TIMESTAMP":  ts,
            "POLY_API_KEY":    pm_state["api_key"],
            "POLY_PASSPHRASE": pm_state["api_passphrase"],
            "Content-Type":    "application/json",
            "User-Agent":      "Mozilla/5.0",
        }

        r = requests.post(f"{CLOB_API}/order", data=body_str, headers=headers, timeout=15)
        resp = r.json()

        if resp.get("success") or resp.get("orderID"):
            with pm_lock:
                pm_state["usdc_balance"] -= alloc
                pm_state["copies"]       += 1
                pm_state["positions"][condition_id] = {
                    "condition_id": condition_id,
                    "question":     title,
                    "side":         side,
                    "entry_price":  price,
                    "size_usd":     alloc,
                    "ts":           time.time(),
                }
            _emit_feed(f"COPY {side}", title, price, alloc)
            print(f"  [PM] ✓ Ordem executada: {side} {title[:40]} ${alloc:.2f}")
            return True
        else:
            print(f"  [PM] Ordem rejeitada: {resp}")
            return False

    except Exception as e:
        print(f"  [PM] Erro na ordem: {e}")
        return False


# ── Monitor de posições ───────────────────────────────────────────────────────

def _check_wallet(wallet: dict):
    positions = fetch_wallet_positions(wallet["address"])
    for pos in positions:
        cid   = pos.get("conditionId") or pos.get("market","")
        title = pos.get("title") or pos.get("market","?")
        price = float(pos.get("curPrice") or pos.get("price", 0.5))
        side  = "YES" if str(pos.get("outcomeIndex","0")) == "0" else "NO"
        token = str(pos.get("tokenId") or pos.get("tokenID",""))

        if not cid:
            continue
        if not (MIN_PRICE <= price <= MAX_PRICE):
            continue
        if classify_sport(title) in BLOCKED_SPORTS:
            continue

        with pm_lock:
            already = cid in pm_state["known_positions"]

        if not already:
            with pm_lock:
                pm_state["known_positions"].add(cid)
            _emit_feed(f"📡 {wallet['username']}", title, price, 0)
            execute_copy_trade(cid, token, side, price, title)


def _poller():
    print("  [PM] Poller iniciado")
    fetch_top_wallets()

    while True:
        try:
            with pm_lock:
                wallets = list(pm_state["tracked_wallets"])

            for w in wallets:
                if w.get("address"):
                    _check_wallet(w)
                    time.sleep(1)

            _refresh_balance()
            _emit_state()

        except Exception as e:
            print(f"  [PM] Poller erro: {e}")

        time.sleep(POLL_INTERVAL)


# ── Emissão Socket.IO ─────────────────────────────────────────────────────────

def _emit_feed(action, market, entry=0.0, alloc=0.0):
    if not _sio_ref:
        return
    ts   = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
    item = {"ts": ts, "market": str(market)[:60], "entry": round(float(entry),3), "eq": round(float(alloc),2), "action": action}
    with pm_lock:
        pm_state.setdefault("feed", deque(maxlen=100)).appendleft(item)
    _sio_ref.emit("pm_feed", item)


def _emit_state():
    if not _sio_ref:
        return
    with pm_lock:
        bal      = pm_state["usdc_balance"]
        positions = list(pm_state["positions"].values())
        wallets  = list(pm_state["tracked_wallets"])
        wins     = pm_state["wins"]
        losses   = pm_state["losses"]
        copies   = pm_state["copies"]
        conn     = pm_state["connected"]
        addr     = pm_state["address"]

    total = wins + losses
    wr    = round(wins / total * 100, 1) if total else 0.0

    _sio_ref.emit("pm_state", {
        "connected":       conn,
        "address":         addr,
        "balance":         round(bal, 2),
        "positions":       positions,
        "tracked_wallets": wallets[:10],
        "wins":   wins,
        "losses": losses,
        "win_rate": wr,
        "copies":  copies,
    })


# ── API pública ───────────────────────────────────────────────────────────────

def get_pm_state():
    with pm_lock:
        return {
            "connected":       pm_state["connected"],
            "address":         pm_state["address"],
            "usdc_balance":    pm_state["usdc_balance"],
            "positions":       list(pm_state["positions"].values()),
            "tracked_wallets": pm_state["tracked_wallets"],
            "copies":          pm_state["copies"],
            "wins":            pm_state["wins"],
            "losses":          pm_state["losses"],
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
