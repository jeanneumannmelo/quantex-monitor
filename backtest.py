#!/usr/bin/env python3
"""
BACKTEST — Simula copy trader rodando desde o início do mês
Fills reais, ordem cronológica, matching preciso por conditionId/posição
"""

import json
import time
import urllib.request
from datetime import datetime, timezone
from collections import defaultdict

STARTING_BAL   = 1000.0
MAX_POSITIONS  = 10
MIN_COPY_WR    = 100
ALLOC_PCT      = 0.06   # 6% fixo por trade — até 10 posições simultâneas

# ── Filtros ───────────────────────────────────────────────────────────────────
# F1: bloqueia futures de commodities (preços inconsistentes no fechamento simulado)
BLOCKED_PREFIXES   = ("xyz:", "flx:")
MIN_HOUR_UTC       = 0                   # F2: sem restrição de horário
MAX_HOUR_UTC       = 24
MIN_CONVERGENCE    = 1                   # F3: mínimo 1 trader abrindo
CONVERGENCE_WIN_MS = 14_400_000
STOP_LOSS_PCT      = -0.20              # F5: stop largo -20% (deixar posições respirar)
MAX_HOLD_HOURS     = 168                # F5: max 7 dias aberto

TOP_TRADERS = [
    {"addr": "0xa5b0edf6b55128e0ddae8e51ac538c3188401d41", "label": "ETH-KING",   "wr": 100},
    {"addr": "0x6c8512516ce5669d35113a11ca8b8de322fd84f6", "label": "ETH-BULL",   "wr": 100},
    {"addr": "0x61ceef212ff4a86933c69fb6aca2fe35d8f2a62b", "label": "MULTI-X",    "wr":  69},
    {"addr": "0xa31441e058492bc7cfffda9aa7623c407ae83a81", "label": "OIL-SHORT",  "wr": 100},
    {"addr": "0xeadc152ac1014ace57c6b353f89adf5faffe9d55", "label": "JUP-TRADER", "wr": 100},
    {"addr": "0x5b5d51203a0f9079f8aeb098a6523a13f298c060", "label": "BTC-HUNTER", "wr": 100},
    {"addr": "0x469e9a7f624b04c24f0e64edf8d8a277e6bf58a5", "label": "BTC-LONG",   "wr": 100},
    {"addr": "0xfc667adba8d4837586078f4fdcdc29804337ca06", "label": "OIL-SCALPEL","wr":  47},
    {"addr": "0x985f02b19dbc062e565c981aac5614baf2cf501f", "label": "OIL-BEAST",  "wr": 100},
    {"addr": "0x939f95036d2e7b4d3e80f2b2d3ec1b82b4ca7b74", "label": "HYPE-RIDER", "wr": 100},
]

REST_URL = "https://api.hyperliquid.xyz/info"

def fetch_fills(addr, retries=3):
    for attempt in range(retries):
        try:
            payload = json.dumps({"type": "userFills", "user": addr}).encode()
            req = urllib.request.Request(REST_URL, data=payload,
                  headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 5 * (attempt + 1)
                print(f"    rate limit, aguardando {wait}s...")
                time.sleep(wait)
            else:
                return []
        except Exception:
            return []
    return []

# ── Coleta fills ──────────────────────────────────────────────────────────────
print("\n  Coletando fills históricos dos top traders (WR >= 85%)...\n")
all_fills = []
for t in TOP_TRADERS:
    if t["wr"] < MIN_COPY_WR:
        print(f"  ⊘ SKIP  {t['label']:<12}  WR {t['wr']}% < {MIN_COPY_WR}%")
        continue
    fills = fetch_fills(t["addr"])
    for f in fills:
        f["_trader"] = t["label"]
        f["_addr"]   = t["addr"]
    all_fills.extend(fills)
    print(f"  ✓ {t['label']:<12}  {len(fills)} fills coletados")
    time.sleep(0.5)

all_fills.sort(key=lambda f: f["time"])

now_ms    = time.time() * 1000
month_ms  = 30 * 24 * 3600 * 1000
month_fills = [f for f in all_fills if now_ms - f["time"] <= month_ms]
print(f"\n  Total fills no mês: {len(month_fills)}")

# ── Estado da simulação ────────────────────────────────────────────────────────
trader_positions = defaultdict(dict)
recent_opens     = defaultdict(list)

def close_position(pos_key, exit_px, exit_ts, reason, pos_map, bal, trd_list, w, l, eq):
    pos      = pos_map.pop(pos_key)
    size_usd = pos["size_usd"]
    entry_px = pos["entry_px"]
    side     = pos["side"]

    pnl = size_usd * (exit_px - entry_px) / entry_px if side == "LONG" else \
          size_usd * (entry_px - exit_px) / entry_px
    bal += size_usd + pnl
    pnl_pct = pnl / size_usd * 100

    dt_str = datetime.fromtimestamp(exit_ts / 1000, tz=timezone.utc).strftime("%d/%m %H:%M")
    trd_list.append({
        "ts":       dt_str,
        "coin":     pos["coin"],
        "side":     side,
        "trader":   pos["trader"],
        "entry":    round(entry_px, 4),
        "exit":     round(exit_px, 4),
        "size":     round(size_usd, 2),
        "pnl":      round(pnl, 4),
        "pnl_pct":  round(pnl_pct, 2),
        "hold_h":   round((exit_ts - pos["ts"]) / 3_600_000, 1),
        "balance":  round(bal, 4),
        "reason":   reason,
        "blocked_by": None,
    })
    eq.append({"ts": exit_ts, "bal": round(bal, 4)})
    if pnl > 0:
        w += 1
    else:
        l += 1
    return bal, w, l

# ── Simulação ─────────────────────────────────────────────────────────────────
balance      = STARTING_BAL
positions    = {}
trades       = []
equity_curve = []
wins = losses = copies = 0
last_prices  = {}

blocked = {"F2_session": 0, "F3_convergence": 0, "F5_stoploss": 0, "F5_hold": 0}

if month_fills:
    equity_curve.append({"ts": month_fills[0]["time"], "bal": balance})

for fill in month_fills:
    trader = fill["_trader"]
    coin   = fill["coin"]
    px     = float(fill["px"])
    direct = fill.get("dir", "")
    ts     = fill["time"]

    last_prices[coin] = px

    # F5 contínuo: checar stop-loss e hold em posições abertas
    for pk in list(positions.keys()):
        if pk not in positions:
            continue
        pos     = positions[pk]
        cur_px  = last_prices.get(pos["coin"], pos["entry_px"])
        hold_h  = (ts - pos["ts"]) / 3_600_000
        pnl_pct = (cur_px - pos["entry_px"]) / pos["entry_px"] * 100 if pos["side"] == "LONG" \
                  else (pos["entry_px"] - cur_px) / pos["entry_px"] * 100

        if pnl_pct <= STOP_LOSS_PCT * 100:
            balance, wins, losses = close_position(
                pk, cur_px, ts, "stop-loss", positions, balance, trades, wins, losses, equity_curve)
            blocked["F5_stoploss"] += 1
        elif hold_h >= MAX_HOLD_HOURS:
            balance, wins, losses = close_position(
                pk, cur_px, ts, "max-hold", positions, balance, trades, wins, losses, equity_curve)
            blocked["F5_hold"] += 1

    # ── ABERTURA ──────────────────────────────────────────────────────────────
    if direct.startswith("Open"):
        side    = "LONG" if "Long" in direct else "SHORT"
        our_key = f"{coin}:{side}:{trader}"

        trader_positions[trader][f"{coin}:{side}"] = True

        # F1 — Bloqueia commodities futures
        if any(coin.startswith(p) for p in BLOCKED_PREFIXES):
            continue

        # F2 — Sessão de mercado
        hour_utc = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).hour
        if not (MIN_HOUR_UTC <= hour_utc < MAX_HOUR_UTC):
            blocked["F2_session"] += 1
            continue

        # F3 — Convergência
        recent_opens[our_key] = [e for e in recent_opens[our_key] if ts - e["ts"] < CONVERGENCE_WIN_MS]
        if not any(e["trader"] == trader for e in recent_opens[our_key]):
            recent_opens[our_key].append({"trader": trader, "ts": ts})
        if len(recent_opens[our_key]) < MIN_CONVERGENCE:
            blocked["F3_convergence"] += 1
            continue

        if our_key in positions or len(positions) >= MAX_POSITIONS:
            continue

        alloc = round(balance * ALLOC_PCT, 2)
        if alloc < 1.0:
            continue

        balance -= alloc
        positions[our_key] = {
            "coin": coin, "side": side,
            "entry_px": px, "size_usd": alloc,
            "trader": trader, "ts": ts,
        }
        copies += 1
        equity_curve.append({"ts": ts, "bal": round(balance, 4)})

    # ── FECHAMENTO pelo trader ────────────────────────────────────────────────
    elif direct.startswith("Close"):
        side    = "LONG" if "Long" in direct else "SHORT"
        our_key = f"{coin}:{side}:{trader}"
        trader_positions[trader][f"{coin}:{side}"] = False

        if our_key not in positions:
            continue

        balance, wins, losses = close_position(
            our_key, px, ts, "trader-close", positions, balance, trades, wins, losses, equity_curve)

# ── Fechar posições órfãs no último preço conhecido ──────────────────────────
for pk in list(positions.keys()):
    pos    = positions[pk]
    cur_px = last_prices.get(pos["coin"], pos["entry_px"])
    balance, wins, losses = close_position(
        pk, cur_px, now_ms, "end-of-sim", positions, balance, trades, wins, losses, equity_curve)

equity_curve.append({"ts": now_ms, "bal": round(balance, 4)})

# ── Estatísticas ──────────────────────────────────────────────────────────────
total_pnl     = balance - STARTING_BAL
total_pnl_pct = total_pnl / STARTING_BAL * 100

peak   = STARTING_BAL
max_dd = 0.0
for e in equity_curve:
    if e["bal"] > peak:
        peak = e["bal"]
    dd = (peak - e["bal"]) / peak * 100 if peak > 0 else 0
    if dd > max_dd:
        max_dd = dd

real_trades = [t for t in trades if t.get("reason") != "blocked"]
best  = max(real_trades, key=lambda t: t["pnl"]) if real_trades else None
worst = min(real_trades, key=lambda t: t["pnl"]) if real_trades else None

pnl_by_trader   = defaultdict(float)
count_by_trader = defaultdict(int)
for t in real_trades:
    pnl_by_trader[t["trader"]]   += t["pnl"]
    count_by_trader[t["trader"]] += 1

result = {
    "starting_bal":    STARTING_BAL,
    "final_bal":       round(balance, 4),
    "total_pnl":       round(total_pnl, 4),
    "total_pnl_pct":   round(total_pnl_pct, 2),
    "copies":          copies,
    "closed":          wins + losses,
    "wins":            wins,
    "losses":          losses,
    "win_rate":        round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0,
    "open_positions":  0,
    "max_drawdown":    round(max_dd, 2),
    "max_bal":         max((e["bal"] for e in equity_curve), default=STARTING_BAL),
    "min_bal":         min((e["bal"] for e in equity_curve), default=STARTING_BAL),
    "trades":          real_trades,
    "blocked_trades":  [],
    "equity_curve":    equity_curve,
    "pnl_by_trader":   {k: round(v, 4) for k, v in pnl_by_trader.items()},
    "count_by_trader": dict(count_by_trader),
    "best_trade":      best,
    "worst_trade":     worst,
    "blocked_stats":   blocked,
    "filters": {
        "allowed_coins":   "todas (F1 removido)",
        "session_utc":     f"{MIN_HOUR_UTC}h–{MAX_HOUR_UTC}h",
        "min_convergence": MIN_CONVERGENCE,
        "stop_loss_pct":   STOP_LOSS_PCT * 100,
        "max_hold_hours":  MAX_HOLD_HOURS,
        "alloc_pct":       ALLOC_PCT * 100,
    },
    "traders_used":    [t["label"] for t in TOP_TRADERS if t["wr"] >= MIN_COPY_WR],
    "traders_blocked": [t["label"] for t in TOP_TRADERS if t["wr"] < MIN_COPY_WR],
    "open_pos_detail": [],
}

with open("/tmp/backtest_result.json", "w") as f:
    json.dump(result, f, indent=2)

sign = "+" if total_pnl >= 0 else ""
print(f"\n{'═'*60}")
print(f"  BACKTEST — ESTRATÉGIA CORRIGIDA")
print(f"{'═'*60}")
print(f"  Saldo inicial:     ${STARTING_BAL:,.2f}")
print(f"  Saldo final:       ${balance:,.2f}")
print(f"  PnL total:         {sign}${total_pnl:,.4f}  ({sign}{total_pnl_pct:.2f}%)")
print(f"  Win rate:          {result['win_rate']}%  ({wins}W / {losses}L)")
print(f"  Trades executados: {wins + losses}")
print(f"  Max drawdown:      -{max_dd:.2f}%")
if best:
    print(f"  Melhor trade:      {best['coin']} {best['side']} {'+' if best['pnl']>=0 else ''}{best['pnl']:.4f} ({best['pnl_pct']:+.2f}%)")
if worst:
    print(f"  Pior trade:        {worst['coin']} {worst['side']} {'+' if worst['pnl']>=0 else ''}{worst['pnl']:.4f} ({worst['pnl_pct']:+.2f}%)")
print(f"\n  Filtros bloqueados:")
for k, v in blocked.items():
    print(f"    {k}: {v}")
if pnl_by_trader:
    print(f"\n  PnL por trader:")
    for trd, pnl in sorted(pnl_by_trader.items(), key=lambda x: -x[1]):
        ct = count_by_trader[trd]
        s  = "+" if pnl >= 0 else ""
        print(f"    {trd:<14}  {s}${pnl:.4f}  ({ct} trades)")
print(f"{'═'*60}")
print(f"\n  Resultado salvo em /tmp/backtest_result.json")
