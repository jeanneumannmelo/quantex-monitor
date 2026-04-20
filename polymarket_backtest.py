#!/usr/bin/env python3
"""
POLYMARKET BACKTEST — Copy-trade dos melhores wallets por Exit Quality
Estratégia: copiar posições de traders que capturam o maior % do movimento
antes da resolução final (exit quality), não apenas win rate.

Exit Quality (posição vencedora):
  eq = (exit_price - entry_price) / (1.0 - entry_price) * 100
  → 100% = segurou até a resolução
  → 80%  = saiu quando o mercado estava em 0.92 e entry era 0.60 — ótimo

Exit Quality (posição perdedora):
  eq = (entry_price - exit_price) / entry_price * 100
  → 100% = cortou perda cedo (saiu antes de chegar a 0)
  → 0%   = segurou até o zero — péssimo
"""

import json
import time
import requests
from datetime import datetime, timezone
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
STARTING_BAL       = 1000.0
ALLOC_FIXED        = 80.0          # $80 fixo por trade (sem compounding)
MAX_POSITIONS      = 5
MIN_TRADES_WALLET  = 5             # mínimo de posições fechadas para ranquear
TOP_WALLETS        = 15            # quantos wallets copiar
MIN_EXIT_QUALITY   = 50.0          # só copiar se exit quality histórica >= 50%
MIN_WIN_RATE       = 55.0          # só copiar wallets com WR >= 55%
LEADERBOARD_SIZE   = 100           # top N do leaderboard para analisar
BACKTEST_DAYS      = 90            # janela do backtest em dias

HEADERS    = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
DATA_API   = "https://data-api.polymarket.com"


# ── Coleta de dados ───────────────────────────────────────────────────────────

def get_leaderboard(limit=50):
    r = requests.get(
        f"{DATA_API}/v1/leaderboard",
        params={"category": "OVERALL", "timePeriod": "MONTH",
                "orderBy": "PNL", "limit": limit},
        headers=HEADERS, timeout=15,
    )
    r.raise_for_status()
    return r.json()


def get_closed_positions(address, max_pages=5):
    """Pagina até max_pages * 100 posições para cobrir janela maior."""
    all_pos = []
    for page in range(max_pages):
        r = requests.get(
            f"{DATA_API}/closed-positions",
            params={"user": address, "limit": 100, "offset": page * 100,
                    "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
            headers=HEADERS, timeout=15,
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
    return all_pos


# ── Exit Quality ──────────────────────────────────────────────────────────────

def calc_exit_quality(pos):
    """
    Retorna exit quality (0-100) e categoriza a posição.
    Usa: avgPrice (entry), totalBought, realizedPnl, curPrice (preço atual/final)
    """
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

    exit_price = entry + (pnl / tokens)
    exit_price = max(0.0, min(1.0, exit_price))

    # Posição vencedora: curPrice → 1 (o outcome deles ganhou)
    if cur >= 0.95:
        denom = 1.0 - entry
        if denom < 0.01:
            return None, "win"
        eq = (exit_price - entry) / denom * 100
        return round(max(0, min(100, eq)), 1), "win"

    # Posição perdedora: curPrice → 0 (o outcome deles perdeu)
    if cur <= 0.05:
        if entry < 0.01:
            return None, "loss"
        eq = (entry - exit_price) / entry * 100
        return round(max(0, min(100, eq)), 1), "loss"

    # Ainda aberta / não resolvida — ignora
    return None, "open"


def calc_exit_price(pos):
    try:
        entry    = float(pos.get("avgPrice", 0))
        invested = float(pos.get("totalBought", 0))
        pnl      = float(pos.get("realizedPnl", 0))
        tokens   = invested / entry if entry > 0 else 0
        if tokens <= 0:
            return entry
        return max(0.0, min(1.0, entry + pnl / tokens))
    except Exception:
        return float(pos.get("avgPrice", 0))


# ── Pipeline ──────────────────────────────────────────────────────────────────

import time as _time
BACKTEST_CUTOFF = int(_time.time()) - (BACKTEST_DAYS * 86400)

print(f"\n  Polymarket Backtest — Exit Quality Strategy ({BACKTEST_DAYS} dias)")
print(f"  {datetime.fromtimestamp(BACKTEST_CUTOFF, tz=timezone.utc).strftime('%d/%m/%Y')} → {datetime.now().strftime('%d/%m/%Y')}\n")
print(f"  Buscando top {LEADERBOARD_SIZE} traders do mês...")

traders_raw = get_leaderboard(limit=LEADERBOARD_SIZE)
if not traders_raw:
    print("  Sem dados do leaderboard.")
    exit(1)

print(f"  Coletando posições fechadas...\n")

wallet_stats = []

for i, t in enumerate(traders_raw, 1):
    address  = t.get("proxyWallet", "")
    username = t.get("userName") or (address[:12] + "...") if address else "?"
    monthly_pnl = t.get("pnl", 0)

    print(f"  [{i:02d}/{LEADERBOARD_SIZE}] {username[:28]:<28}", end="\r")

    try:
        positions = get_closed_positions(address)
    except Exception:
        positions = []

    time.sleep(0.3)

    if not positions:
        continue

    eq_wins   = []
    eq_losses = []
    all_eq    = []

    for pos in positions:
        eq, kind = calc_exit_quality(pos)
        if eq is None:
            continue
        all_eq.append(eq)
        if kind == "win":
            eq_wins.append(eq)
        elif kind == "loss":
            eq_losses.append(eq)

    if len(all_eq) < MIN_TRADES_WALLET:
        continue

    avg_eq      = sum(all_eq) / len(all_eq)
    avg_eq_win  = sum(eq_wins) / len(eq_wins) if eq_wins else 0
    avg_eq_loss = sum(eq_losses) / len(eq_losses) if eq_losses else 0
    win_rate    = len(eq_wins) / len(all_eq) * 100 if all_eq else 0

    if win_rate < MIN_WIN_RATE:
        continue

    wallet_stats.append({
        "address":    address,
        "username":   username[:28],
        "monthly_pnl": monthly_pnl,
        "avg_eq":     round(avg_eq, 1),
        "avg_eq_win": round(avg_eq_win, 1),
        "avg_eq_loss": round(avg_eq_loss, 1),
        "win_rate":   round(win_rate, 1),
        "n_trades":   len(all_eq),
        "positions":  positions,
    })

print(" " * 60)
print(f"  {len(wallet_stats)} wallets com dados suficientes.\n")

# Ordenar por exit quality média (descending)
wallet_stats.sort(key=lambda x: -x["avg_eq"])

# Top wallets acima do threshold
top_wallets = [w for w in wallet_stats if w["avg_eq"] >= MIN_EXIT_QUALITY][:TOP_WALLETS]

print(f"  Top {len(top_wallets)} wallets por Exit Quality (mín {MIN_EXIT_QUALITY}%):\n")
for rank, w in enumerate(top_wallets, 1):
    print(f"  #{rank:02d}  EQ={w['avg_eq']:5.1f}%  WR={w['win_rate']:5.1f}%  "
          f"Trades={w['n_trades']:3d}  PnL=${w['monthly_pnl']:,.0f}  {w['username']}")


# ── Simulação de copy trade ───────────────────────────────────────────────────
print("\n  Simulando copy trade...\n")

# Coletar todas as posições dos top wallets com timestamp
all_ops = []
for w in top_wallets:
    for pos in w["positions"]:
        ts = pos.get("timestamp", 0)
        if not ts or ts < BACKTEST_CUTOFF:
            continue
        eq, kind = calc_exit_quality(pos)
        if eq is None or kind not in ("win", "loss"):
            continue
        entry      = float(pos.get("avgPrice", 0))
        exit_price = calc_exit_price(pos)
        pnl_pct    = (exit_price - entry) / entry if entry > 0 else 0
        all_ops.append({
            "ts":         ts,
            "wallet":     w["username"],
            "title":      pos.get("title", "")[:50],
            "outcome":    pos.get("outcome", ""),
            "entry":      round(entry, 4),
            "exit":       round(exit_price, 4),
            "pnl_pct":    round(pnl_pct * 100, 2),
            "exit_quality": eq,
            "kind":       kind,
            "slug":       pos.get("slug", ""),
        })

# Ordenar cronologicamente
all_ops.sort(key=lambda x: x["ts"])

# Simular
balance      = STARTING_BAL
open_pos     = {}
trades       = []
equity_curve = []
wins = losses = 0

if all_ops:
    equity_curve.append({"ts": all_ops[0]["ts"] * 1000, "bal": balance})

for op in all_ops:
    key = f"{op['slug']}:{op['wallet']}"

    if key in open_pos:
        continue  # já temos posição aberta neste mercado/wallet

    if len(open_pos) >= MAX_POSITIONS:
        continue

    alloc = ALLOC_FIXED
    if balance < alloc:
        continue

    balance -= alloc
    open_pos[key] = {**op, "alloc": alloc}
    equity_curve.append({"ts": op["ts"] * 1000, "bal": round(balance, 4)})

    # Fechar imediatamente ao exit price do trader (simulação histórica)
    pnl     = alloc * op["pnl_pct"] / 100
    balance += alloc + pnl
    del open_pos[key]

    dt = datetime.fromtimestamp(op["ts"], tz=timezone.utc).strftime("%d/%m %H:%M")
    trades.append({
        "ts":           dt,
        "ts_unix":      op["ts"],
        "wallet":       op["wallet"],
        "title":        op["title"],
        "outcome":      op["outcome"],
        "entry":        op["entry"],
        "exit":         op["exit"],
        "pnl":          round(pnl, 4),
        "pnl_pct":      op["pnl_pct"],
        "exit_quality": op["exit_quality"],
        "kind":         op["kind"],
        "alloc":        round(alloc, 2),
        "balance":      round(balance, 4),
    })

    equity_curve.append({"ts": op["ts"] * 1000, "bal": round(balance, 4)})

    if pnl > 0:
        wins += 1
    else:
        losses += 1

equity_curve.append({"ts": int(time.time() * 1000), "bal": round(balance, 4)})

# ── Estatísticas ──────────────────────────────────────────────────────────────
total_pnl     = balance - STARTING_BAL
total_pnl_pct = total_pnl / STARTING_BAL * 100

peak = STARTING_BAL
max_dd = 0.0
for e in equity_curve:
    if e["bal"] > peak:
        peak = e["bal"]
    dd = (peak - e["bal"]) / peak * 100 if peak > 0 else 0
    if dd > max_dd:
        max_dd = dd

pnl_by_wallet = defaultdict(float)
count_by_wallet = defaultdict(int)
for t in trades:
    pnl_by_wallet[t["wallet"]]   += t["pnl"]
    count_by_wallet[t["wallet"]] += 1

best  = max(trades, key=lambda t: t["pnl"]) if trades else None
worst = min(trades, key=lambda t: t["pnl"]) if trades else None

# ── Breakdown mensal ──────────────────────────────────────────────────────────
from collections import defaultdict as _dd
monthly_pnl   = _dd(float)
monthly_wins  = _dd(int)
monthly_losses= _dd(int)
for t in trades:
    mk = datetime.fromtimestamp(t["ts_unix"], tz=timezone.utc).strftime("%Y-%m")
    monthly_pnl[mk]    += t["pnl"]
    if t["pnl"] > 0:
        monthly_wins[mk]   += 1
    else:
        monthly_losses[mk] += 1

# Preencher todos os meses na janela
monthly_data = {}
cur = datetime.fromtimestamp(BACKTEST_CUTOFF, tz=timezone.utc).replace(day=1)
end_dt = datetime.now(tz=timezone.utc).replace(day=1)
while cur <= end_dt:
    k = cur.strftime("%Y-%m")
    monthly_data[k] = {
        "label":  cur.strftime("%b/%Y"),
        "pnl":    round(monthly_pnl.get(k, 0.0), 4),
        "wins":   monthly_wins.get(k, 0),
        "losses": monthly_losses.get(k, 0),
        "trades": monthly_wins.get(k, 0) + monthly_losses.get(k, 0),
    }
    cur = cur.replace(month=cur.month % 12 + 1, year=cur.year + (1 if cur.month == 12 else 0))

result = {
    "strategy":        "Polymarket Exit Quality Copy Trade",
    "starting_bal":    STARTING_BAL,
    "final_bal":       round(balance, 4),
    "total_pnl":       round(total_pnl, 4),
    "total_pnl_pct":   round(total_pnl_pct, 2),
    "wins":            wins,
    "losses":          losses,
    "win_rate":        round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0,
    "max_drawdown":    round(max_dd, 2),
    "max_bal":         max((e["bal"] for e in equity_curve), default=STARTING_BAL),
    "min_bal":         min((e["bal"] for e in equity_curve), default=STARTING_BAL),
    "trades":          trades,
    "equity_curve":    equity_curve,
    "pnl_by_wallet":   {k: round(v, 4) for k, v in pnl_by_wallet.items()},
    "count_by_wallet": dict(count_by_wallet),
    "best_trade":      best,
    "worst_trade":     worst,
    "top_wallets": [
        {
            "username":   w["username"],
            "avg_eq":     w["avg_eq"],
            "avg_eq_win": w["avg_eq_win"],
            "avg_eq_loss": w["avg_eq_loss"],
            "win_rate":   w["win_rate"],
            "n_trades":   w["n_trades"],
            "monthly_pnl": round(w["monthly_pnl"], 2),
        }
        for w in top_wallets
    ],
    "config": {
        "leaderboard_size":  LEADERBOARD_SIZE,
        "top_wallets":       TOP_WALLETS,
        "min_trades_wallet": MIN_TRADES_WALLET,
        "min_exit_quality":  MIN_EXIT_QUALITY,
        "alloc_fixed":       ALLOC_FIXED,
        "max_positions":     MAX_POSITIONS,
    },
    "monthly":       monthly_data,
    "backtest_days": BACKTEST_DAYS,
    "period_start":  datetime.fromtimestamp(BACKTEST_CUTOFF, tz=timezone.utc).strftime("%d/%m/%Y"),
    "period_end":    datetime.now(tz=timezone.utc).strftime("%d/%m/%Y"),
}

with open("/tmp/polymarket_backtest.json", "w") as f:
    json.dump(result, f, indent=2)

sign = "+" if total_pnl >= 0 else ""
print(f"\n{'═'*65}")
print(f"  RESULTADO — POLYMARKET EXIT QUALITY STRATEGY")
print(f"{'═'*65}")
print(f"  Saldo inicial:   ${STARTING_BAL:,.2f}")
print(f"  Saldo final:     ${balance:,.2f}")
print(f"  PnL total:       {sign}${total_pnl:,.4f}  ({sign}{total_pnl_pct:.2f}%)")
print(f"  Win rate:        {result['win_rate']}%  ({wins}W / {losses}L)")
print(f"  Trades:          {wins + losses}")
print(f"  Max drawdown:    -{max_dd:.2f}%")
if best:
    print(f"  Melhor trade:    {best['title'][:35]}  {'+' if best['pnl']>=0 else ''}{best['pnl']:.4f}")
if worst:
    print(f"  Pior trade:      {worst['title'][:35]}  {'+' if worst['pnl']>=0 else ''}{worst['pnl']:.4f}")
print(f"\n  PnL por wallet:")
for wlt, pnl in sorted(pnl_by_wallet.items(), key=lambda x: -x[1]):
    s = "+" if pnl >= 0 else ""
    print(f"    {wlt:<30}  {s}${pnl:.4f}  ({count_by_wallet[wlt]} trades)")
print(f"{'═'*65}")
print(f"\n  Salvo em /tmp/polymarket_backtest.json")
