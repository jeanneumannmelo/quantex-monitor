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
from polymarket_cash import position_size, expected_exit_price, score_wallet

# ── Endpoints ─────────────────────────────────────────────────────────────────
CLOB_API   = "https://clob.polymarket.com"
DATA_API   = "https://data-api.polymarket.com"
GAMMA_API  = "https://gamma-api.polymarket.com"

CHAIN_ID             = 137
POLL_INTERVAL        = 5       # segundos por wallet-thread
LEADERBOARD_REFRESH  = 300     # refresh lista de wallets
MIN_PRICE            = 0.04
MAX_PRICE            = 0.92
TOP_WALLETS_N        = 10
MIN_WIN_RATE         = 62.0
MIN_EXIT_QUALITY     = 55.0
MIN_TRADES_WALLET    = 8
BLOCKED_SPORTS       = {"Tennis"}   # Tennis: jogos resolvem em horas
MIN_HOURS_TO_RESOLVE = 4.0          # ignora mercados que resolvem em < 4h

# Kelly / cash management
KELLY_FRACTION       = 0.50
MIN_ALLOC            = 5.0
MAX_ALLOC_PCT        = 0.10   # teto dinâmico: 10% do saldo atual

# Circuit breakers
CIRCUIT_BREAKER_DD   = 0.20   # pausa se saldo cair 20% do pico
MAX_DAILY_LOSS_PCT   = 0.10   # para o dia após -10%
MAX_CONSECUTIVE_LOSS = 4      # cooldown após 4 perdas seguidas

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
            with pm_lock:
                bal = pm_state["usdc_balance"]
                pm_state["peak_balance"]  = bal
                pm_state["session_start"] = bal
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
        if pm_state["api_key"]:
            data = _signed_get(f"{CLOB_API}/balance-allowance?asset_type=COLLATERAL&signature_type=1")
            bal  = float(data.get("balance", 0)) / 1e6
        else:
            addr = pm_state["address"]
            data = _get(f"{DATA_API}/profile?address={addr}")
            bal  = float(data.get("portfolioValue", data.get("usdcBalance", 0)))
        with pm_lock:
            pm_state["usdc_balance"] = bal
        print(f"  [PM] Saldo USDC: ${bal:.2f}")
    except Exception as e:
        print(f"  [PM] Erro ao buscar saldo: {e}")
        try:
            addr = pm_state["address"]
            data = _get(f"{DATA_API}/profile?address={addr}")
            bal  = float(data.get("portfolioValue", data.get("usdcBalance", 0)))
            with pm_lock:
                pm_state["usdc_balance"] = bal
        except Exception:
            pass


# ── Posições ao vivo com PnL ──────────────────────────────────────────────────

def _refresh_live_positions():
    """Busca posições abertas do proxy wallet e calcula PnL não realizado."""
    try:
        import requests as _req
        with open("/Users/mac/matrix_dashboard/config.json") as f:
            cfg = json.load(f)
        funder = cfg.get("pm_funder", pm_state["address"])

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

            positions.append({
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
            })

        with pm_lock:
            pm_state["live_positions"] = positions
            pm_state["unrealized_pnl"] = round(unrealized, 2)

    except Exception as e:
        print(f"  [PM] Erro ao buscar posições: {e}")


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
        print(f"  [PM] ⛔ Circuit breaker: drawdown {((peak-bal)/peak*100):.1f}% — pausado")
        return False

    if start > 0 and daily / start >= MAX_DAILY_LOSS_PCT:
        with pm_lock:
            pm_state["paused"] = True
        print(f"  [PM] ⛔ Circuit breaker: perda diária {(daily/start*100):.1f}% — pausado")
        return False

    if consec >= MAX_CONSECUTIVE_LOSS:
        print(f"  [PM] ⛔ Circuit breaker: {consec} perdas consecutivas — cooldown 15min")
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


def _analyze_wallets(leaderboard_limit=80, n=TOP_WALLETS_N):
    """
    Busca top wallets do leaderboard, analisa histórico de posições fechadas,
    calcula EQ e WR reais e popula wallet_eq_cache / wallet_wr_cache.
    Só retorna wallets que passam nos filtros MIN_EXIT_QUALITY e MIN_WIN_RATE.
    """
    import requests as _req
    print(f"  [PM] Analisando wallets (buscando top {leaderboard_limit})...")
    try:
        r = _req.get(
            f"{DATA_API}/v1/leaderboard",
            params={"category": "OVERALL", "timePeriod": "MONTH",
                    "orderBy": "PNL", "limit": leaderboard_limit},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        lb = r.json()
        if not isinstance(lb, list):
            return []
    except Exception as e:
        print(f"  [PM] Leaderboard erro: {e}")
        return []

    qualified = []
    for i, w in enumerate(lb, 1):
        addr     = w.get("proxyWallet", "")
        username = w.get("userName") or (addr[:12] if addr else "?")
        pnl      = float(w.get("pnl", 0) or 0)
        if not addr:
            continue

        print(f"  [PM] [{i:02d}/{len(lb)}] {username[:24]:<24}", end="\r")

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

    print(f"  [PM] {' ' * 40}", end="\r")  # limpa linha de progresso

    qualified.sort(key=lambda x: -score_wallet(x))
    top = qualified[:n]

    with pm_lock:
        pm_state["tracked_wallets"] = top

    print(f"  [PM] ✓ {len(top)} wallets qualificados (EQ≥{MIN_EXIT_QUALITY}% WR≥{MIN_WIN_RATE}%)")
    for w in top:
        print(f"       {w['username'][:28]:<28}  EQ={w['avg_eq']:.0f}%  WR={w['win_rate']:.0f}%  trades={w['n_trades']}")

    return top


def fetch_top_wallets(n=TOP_WALLETS_N):
    """Wrapper que usa o cache se disponível, senão chama _analyze_wallets."""
    with pm_lock:
        cached = list(pm_state["tracked_wallets"])
    if cached:
        return cached
    return _analyze_wallets(n=n)


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

    with open("/Users/mac/matrix_dashboard/config.json") as f:
        cfg = json.load(f)

    creds = ApiCreds(
        api_key        = cfg["pm_api_key"],
        api_secret     = cfg["pm_api_secret"],
        api_passphrase = cfg["pm_api_pass"],
    )
    return ClobClient(
        host           = CLOB_API,
        key            = cfg["pm_private_key"],
        chain_id       = CHAIN_ID,
        creds          = creds,
        signature_type = 1,
        funder         = cfg["pm_funder"],
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
            bal = pm_state["usdc_balance"]
            wr  = pm_state["wallet_wr_cache"].get(wallet_username, 0.65)
            eq  = pm_state["wallet_eq_cache"].get(wallet_username, 60.0)

        avg_exit  = expected_exit_price(price, eq)
        max_alloc = bal * MAX_ALLOC_PCT
        alloc = position_size(
            balance        = bal,
            p_win          = wr,
            entry_price    = price,
            avg_exit_price = avg_exit,
            kelly_frac     = KELLY_FRACTION,
            min_size       = MIN_ALLOC,
            max_size       = max_alloc,
        )

        if alloc > bal:
            print("  [PM] Saldo insuficiente para trade")
            return False

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
            print(f"  [PM] ✓ {side} {title[:40]} ${alloc:.2f} (Kelly, WR={wr:.0%}, EQ={eq:.0f}%)")
            return True
        else:
            print(f"  [PM] Ordem rejeitada: {resp}")
            return False

    except Exception as e:
        err = str(e)
        if "does not exist" in err or "FOK" in err or "fully filled" in err:
            pass  # mercado sem orderbook ou sem liquidez — skip silencioso
        else:
            print(f"  [PM] Erro na ordem: {e}")
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
        print(f"  [PM] {'✓' if pnl>=0 else '✗'} EXIT {pos['question'][:40]}  {sign}${pnl:.2f}")

    except Exception as e:
        print(f"  [PM] Erro no exit: {e}")


# ── Zombie cleanup ───────────────────────────────────────────────────────────

ZOMBIE_MAX_DAYS  = 5      # posição aberta há mais de X dias
ZOMBIE_MAX_PRICE = 0.08   # preço atual abaixo de X¢ (indo para 0)

def _cleanup_zombie_positions():
    """
    Fecha posições "zumbi": abertas há mais de ZOMBIE_MAX_DAYS E com preço
    atual < ZOMBIE_MAX_PRICE E o trader copiado já não tem mais essa posição.
    """
    now = time.time()
    with pm_lock:
        open_pos = dict(pm_state["positions"])
        all_trader_ids = set()
        for ids in pm_state["wallet_positions"].values():
            all_trader_ids |= ids

    for cid, pos in open_pos.items():
        age_days = (now - pos.get("ts", now)) / 86400
        if age_days < ZOMBIE_MAX_DAYS:
            continue
        if cid in all_trader_ids:
            continue  # trader ainda segura — não tocar

        # Busca preço atual via live_positions
        cur_price = None
        with pm_lock:
            for lp in pm_state["live_positions"]:
                if lp.get("condition_id") == cid:
                    cur_price = lp.get("cur_price")
                    break

        if cur_price is None or cur_price > ZOMBIE_MAX_PRICE:
            continue

        print(f"  [PM] 🧟 Zombie detectado: {pos['question'][:40]} "
              f"(age={age_days:.1f}d, price={cur_price:.3f}) → fechando")
        execute_exit_trade(cid)


# ── Monitor por wallet ────────────────────────────────────────────────────────

def _check_wallet(wallet: dict):
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
            print(f"  [PM] {wallet['username']} saiu de {cid[:12]}… → fechando cópia")
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
            with pm_lock:
                pm_state["known_positions"].add(cid)
            _emit_feed(f"📡 {wallet['username']}", title, price, 0)
            execute_copy_trade(cid, token, side, price, title, wallet["username"])


def _wallet_watcher(wallet: dict):
    """Thread dedicada por wallet — polling a cada POLL_INTERVAL segundos."""
    name = wallet["username"]
    print(f"  [PM] Watcher iniciado: {name}")
    while True:
        try:
            _check_wallet(wallet)
        except Exception as e:
            print(f"  [PM] Watcher {name} erro: {e}")
        time.sleep(POLL_INTERVAL)


def _poller():
    """Thread principal: inicializa watchers e refresha a lista periodicamente."""
    print("  [PM] Poller iniciado")
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
            _refresh_balance()
            _refresh_live_positions()
            _emit_state()

            if time.time() - last_zombie_check >= 3600:  # a cada 1h
                _cleanup_zombie_positions()
                last_zombie_check = time.time()

            if time.time() - last_lb_refresh >= LEADERBOARD_REFRESH:
                fetch_top_wallets()
                _spawn_watchers()
                last_lb_refresh = time.time()

        except Exception as e:
            print(f"  [PM] Poller erro: {e}")


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
        print(f"  [PM] Thread iniciada: {w['username']}")


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
