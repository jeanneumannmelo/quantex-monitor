"""
HYBRID MONITOR — Polymarket Signal → Hyperliquid Execution
Detecta quando top wallets (por Exit Quality) abrem posições em mercados
de cripto na Polymarket e gera sinais de trade para a Hyperliquid.
"""

import requests
import time
import json
import threading
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional

HEADERS  = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
DATA_API = "https://data-api.polymarket.com"
HL_REST  = "https://api.hyperliquid.xyz/info"

POLL_INTERVAL   = 60        # segundos entre polls de cada wallet
MIN_POSITION_USD = 100      # ignora posições menores que $100
MIN_EQ           = 50.0     # exit quality mínimo do wallet

# ── Mapeamento Polymarket → Hyperliquid ──────────────────────────────────────

CRYPTO_KEYWORDS = {
    "BTC":  ["bitcoin", "btc"],
    "ETH":  ["ethereum", "eth", "ether"],
    "SOL":  ["solana", "sol"],
    "HYPE": ["hype", "hyperliquid"],
    "XRP":  ["xrp", "ripple"],
    "BNB":  ["bnb", "binance"],
    "DOGE": ["doge", "dogecoin"],
    "AVAX": ["avax", "avalanche"],
    "LINK": ["chainlink", "link"],
    "SUI":  ["sui"],
}

LONG_KEYWORDS  = ["above", "over", "exceed", "reach", "hit", "higher",
                   "rise", "gain", "bull", "up", "long", "rally", "pump"]
SHORT_KEYWORDS = ["below", "under", "dip", "drop", "fall", "lower",
                  "decline", "bear", "down", "short", "crash", "dump"]


def detect_crypto_signal(title: str, slug: str) -> Optional[dict]:
    """
    Tenta extrair coin + direção de um título de mercado Polymarket.
    Retorna {"coin": "BTC", "side": "LONG"} ou None.
    """
    text = (title + " " + slug).lower()

    coin = None
    for symbol, keywords in CRYPTO_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            coin = symbol
            break

    if not coin:
        return None

    long_score  = sum(1 for kw in LONG_KEYWORDS  if kw in text)
    short_score = sum(1 for kw in SHORT_KEYWORDS if kw in text)

    if long_score == 0 and short_score == 0:
        return None

    side = "LONG" if long_score >= short_score else "SHORT"
    return {"coin": coin, "side": side}


# ── Preço atual na Hyperliquid ────────────────────────────────────────────────

def hl_mid_price(coin: str) -> Optional[float]:
    try:
        payload = json.dumps({"type": "allMids"}).encode()
        req = requests.post(HL_REST, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=8)
        mids = req.json()
        return float(mids.get(coin, 0)) or None
    except Exception:
        return None


# ── Estado compartilhado ──────────────────────────────────────────────────────

hybrid_state = {
    "wallets":        [],        # lista de wallets monitorados
    "known_positions": {},       # wallet -> set of conditionIds abertos
    "signals":        [],        # sinais ativos (posições abertas pelo trader)
    "closed_signals": [],        # sinais fechados (histórico)
    "last_poll":      {},        # wallet -> timestamp último poll
    "hl_prices":      {},        # coin -> preço atual Hyperliquid
    "running":        False,
}
_lock = threading.Lock()


def load_top_wallets():
    """Carrega top wallets do último backtest ou do leaderboard."""
    # Tenta usar resultado do backtest
    try:
        with open("/tmp/polymarket_backtest.json") as f:
            bt = json.load(f)
        wallets_bt = [w for w in bt.get("top_wallets", []) if w["avg_eq"] >= MIN_EQ]
        if wallets_bt:
            # Precisa dos endereços — buscar no leaderboard
            r = requests.get(f"{DATA_API}/v1/leaderboard",
                params={"category": "OVERALL", "timePeriod": "MONTH",
                        "orderBy": "PNL", "limit": 100},
                headers=HEADERS, timeout=15)
            traders = r.json()
            name_to_addr = {t.get("userName", ""): t.get("proxyWallet", "")
                            for t in traders}
            result = []
            for w in wallets_bt:
                addr = name_to_addr.get(w["username"], "")
                if addr:
                    result.append({**w, "address": addr})
            return result
    except Exception:
        pass

    # Fallback: leaderboard direto
    r = requests.get(f"{DATA_API}/v1/leaderboard",
        params={"category": "OVERALL", "timePeriod": "MONTH",
                "orderBy": "PNL", "limit": 50},
        headers=HEADERS, timeout=15)
    return [{"username": t.get("userName", "")[:25],
             "address":  t.get("proxyWallet", ""),
             "avg_eq":   0, "win_rate": 0}
            for t in r.json()[:20]]


def poll_wallet(wallet: dict):
    """Verifica posições abertas de um wallet e detecta sinais novos."""
    addr = wallet.get("address", "")
    name = wallet.get("username", "")
    if not addr:
        return

    try:
        r = requests.get(f"{DATA_API}/positions",
            params={"user": addr, "limit": 50, "sizeThreshold": "10"},
            headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return
        positions = r.json() if isinstance(r.json(), list) else []
    except Exception:
        return

    with _lock:
        known = hybrid_state["known_positions"].get(addr, set())
        current_ids = set()

        for pos in positions:
            cid       = pos.get("conditionId", "")
            title     = pos.get("title", "")
            slug      = pos.get("slug", "")
            avg_price = float(pos.get("avgPrice", 0))
            cur_price = float(pos.get("curPrice", avg_price))
            size_usd  = float(pos.get("initialValue", 0))
            outcome   = pos.get("outcome", "")
            end_date  = pos.get("endDate", "")

            if not cid or size_usd < MIN_POSITION_USD:
                continue

            current_ids.add(cid)

            # Nova posição detectada
            if cid not in known:
                signal = detect_crypto_signal(title, slug)
                ts_now = int(time.time())
                hl_px  = hl_mid_price(signal["coin"]) if signal else None

                entry = {
                    "id":         cid,
                    "wallet":     name,
                    "title":      title[:60],
                    "outcome":    outcome,
                    "poly_entry": round(avg_price, 4),
                    "poly_cur":   round(cur_price, 4),
                    "size_usd":   round(size_usd, 2),
                    "end_date":   end_date[:10] if end_date else "",
                    "signal":     signal,
                    "hl_entry":   round(hl_px, 2) if hl_px else None,
                    "hl_cur":     round(hl_px, 2) if hl_px else None,
                    "hl_pnl":     0.0,
                    "hl_pnl_pct": 0.0,
                    "ts_open":    ts_now,
                    "ts_str":     datetime.fromtimestamp(ts_now, tz=timezone.utc).strftime("%d/%m %H:%M"),
                    "status":     "open",
                }
                hybrid_state["signals"].append(entry)

        # Posições fechadas pelo trader
        closed_ids = known - current_ids
        for cid in closed_ids:
            for sig in hybrid_state["signals"]:
                if sig["id"] == cid and sig["status"] == "open":
                    sig["status"] = "closed"
                    sig["ts_close"] = int(time.time())
                    hybrid_state["closed_signals"].insert(0, sig)

        hybrid_state["signals"] = [s for s in hybrid_state["signals"]
                                    if s["status"] == "open"]
        hybrid_state["known_positions"][addr] = current_ids
        hybrid_state["last_poll"][addr] = int(time.time())


def update_hl_prices():
    """Atualiza preços da Hyperliquid e recalcula PnL dos sinais abertos."""
    coins_needed = set()
    with _lock:
        for sig in hybrid_state["signals"]:
            if sig.get("signal"):
                coins_needed.add(sig["signal"]["coin"])

    if not coins_needed:
        return

    try:
        payload = json.dumps({"type": "allMids"}).encode()
        req = requests.post(HL_REST, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=8)
        mids = req.json()
    except Exception:
        return

    with _lock:
        for coin in coins_needed:
            px = float(mids.get(coin, 0))
            if px:
                hybrid_state["hl_prices"][coin] = round(px, 4)

        # Recalcular PnL de cada sinal
        for sig in hybrid_state["signals"]:
            s = sig.get("signal")
            if not s:
                continue
            coin    = s["coin"]
            side    = s["side"]
            hl_entry = sig.get("hl_entry")
            hl_cur   = hybrid_state["hl_prices"].get(coin)

            if not hl_entry or not hl_cur:
                continue

            sig["hl_cur"] = hl_cur
            if side == "LONG":
                pnl_pct = (hl_cur - hl_entry) / hl_entry * 100
            else:
                pnl_pct = (hl_entry - hl_cur) / hl_entry * 100

            sig["hl_pnl_pct"] = round(pnl_pct, 2)
            sig["hl_pnl"]     = round(80 * pnl_pct / 100, 4)  # base $80


def monitor_loop():
    """Loop principal de monitoramento."""
    hybrid_state["running"] = True
    print("  [HYBRID] Carregando top wallets...")
    wallets = load_top_wallets()
    with _lock:
        hybrid_state["wallets"] = wallets
    print(f"  [HYBRID] {len(wallets)} wallets monitorados")

    while hybrid_state["running"]:
        for wallet in wallets:
            addr = wallet.get("address", "")
            last = hybrid_state["last_poll"].get(addr, 0)
            if time.time() - last >= POLL_INTERVAL:
                poll_wallet(wallet)
                time.sleep(0.5)

        update_hl_prices()
        time.sleep(5)


def start_monitor():
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    return t


def get_state():
    with _lock:
        signals    = list(hybrid_state["signals"])
        closed     = list(hybrid_state["closed_signals"])[:30]
        wallets    = list(hybrid_state["wallets"])
        hl_prices  = dict(hybrid_state["hl_prices"])
        last_poll  = dict(hybrid_state["last_poll"])

    # Enriquecer com tempo desde último poll
    now = time.time()
    wallet_status = []
    for w in wallets:
        addr = w.get("address", "")
        lp   = last_poll.get(addr, 0)
        wallet_status.append({
            **w,
            "last_poll_ago": int(now - lp) if lp else None,
            "open_signals":  sum(1 for s in signals if s["wallet"] == w["username"]),
        })

    # Resumo de PnL
    total_pnl = sum(s.get("hl_pnl", 0) for s in signals + closed
                    if s.get("status") == "open" or True)
    crypto_signals = [s for s in signals if s.get("signal")]

    return {
        "signals":        signals,
        "closed_signals": closed,
        "wallets":        wallet_status,
        "hl_prices":      hl_prices,
        "total_open":     len(signals),
        "total_crypto":   len(crypto_signals),
        "total_pnl":      round(total_pnl, 4),
        "running":        hybrid_state["running"],
        "ts":             int(now),
    }
