#!/usr/bin/env python3
"""
POLYMARKET 180-DAY BACKTEST
Estratégia: Momentum em mercados de longa duração
- Entra quando o preço rompe 0.60 com momentum de 3 dias
- Sai quando captura 80% do movimento restante ou preço reverte 10%
- Breakdown mensal do PnL
"""

import requests, json, time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

HEADERS     = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"

STARTING_BAL       = 1000.0
ALLOC_PER_TRADE    = 80.0        # $80 por trade (fixo)
MAX_OPEN           = 5           # máx 5 posições simultâneas
ENTRY_THRESHOLD    = 0.58        # entra quando preço cruza acima deste nível
MOMENTUM_DAYS      = 3           # mínimo de dias subindo para confirmar sinal
EXIT_CAPTURE_PCT   = 0.80        # sai quando captura 80% do movimento restante
STOP_LOSS_PCT      = 0.10        # stop se preço cai 10% desde a entrada
MIN_MARKET_DAYS    = 90          # só mercados com 90+ dias de duração
MIN_VOLUME         = 50_000      # mínimo $50k de volume
BACKTEST_DAYS      = 180


def get_long_markets():
    """Coleta mercados fechados com 90+ dias de duração e volume significativo."""
    all_markets = []
    for tag_id in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
        r = requests.get(f"{GAMMA_API}/markets",
            params={"limit": 100, "order": "volume", "ascending": "false",
                    "closed": "true", "tag_id": str(tag_id)},
            headers=HEADERS, timeout=15)
        if r.status_code != 200:
            continue
        for m in r.json() if isinstance(r.json(), list) else []:
            start = m.get("startDate", "")
            end   = m.get("endDate", "")
            vol   = float(m.get("volume", 0))
            cid   = m.get("conditionId", "")
            if not start or not end or not cid or vol < MIN_VOLUME:
                continue
            try:
                s = datetime.fromisoformat(start.replace("Z", "+00:00"))
                e = datetime.fromisoformat(end.replace("Z", "+00:00"))
                dur = (e - s).days
                if dur >= MIN_MARKET_DAYS:
                    if not any(x["conditionId"] == cid for x in all_markets):
                        all_markets.append({
                            "conditionId": cid,
                            "question": m["question"][:70],
                            "dur": dur, "vol": vol,
                            "start": start, "end": end,
                        })
            except Exception:
                pass
        time.sleep(0.2)
    all_markets.sort(key=lambda x: -x["vol"])
    return all_markets


def get_price_history(condition_id):
    """Retorna histórico de preços de ambos os tokens de um mercado."""
    r = requests.get(f"{CLOB_API}/markets/{condition_id}",
        headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return []
    tokens = r.json().get("tokens", [])
    histories = []
    for tok in tokens:
        tid = tok.get("token_id", "")
        if not tid:
            continue
        r2 = requests.get(f"{CLOB_API}/prices-history",
            params={"market": tid, "interval": "all", "fidelity": 1},
            headers=HEADERS, timeout=10)
        hist = r2.json().get("history", [])
        if hist:
            histories.append({
                "token_id": tid,
                "outcome": tok.get("outcome", ""),
                "winner": tok.get("winner", False),
                "history": sorted(hist, key=lambda x: x["t"]),
            })
        time.sleep(0.1)
    return histories


def simulate_market(token_data, backtest_start_ts, backtest_end_ts):
    """
    Simula a estratégia de momentum num único token/mercado.
    Retorna lista de trades simulados.
    """
    hist   = token_data["history"]
    winner = token_data["winner"]
    outcome = token_data["outcome"]

    # Filtrar janela de backtest
    hist = [p for p in hist
            if backtest_start_ts <= p["t"] <= backtest_end_ts]
    if len(hist) < MOMENTUM_DAYS + 2:
        return []

    trades = []
    in_position = False
    entry_price = 0.0
    entry_ts    = 0

    for i in range(MOMENTUM_DAYS, len(hist)):
        cur_p  = float(hist[i]["p"])
        prev_p = float(hist[i - MOMENTUM_DAYS]["p"])
        ts     = hist[i]["t"]

        if not in_position:
            # Sinal de entrada: preço cruzou ENTRY_THRESHOLD com momentum positivo
            if cur_p >= ENTRY_THRESHOLD and cur_p > prev_p:
                in_position = True
                entry_price = cur_p
                entry_ts    = ts

        else:
            move_done    = cur_p - entry_price
            total_move   = 1.0 - entry_price
            capture_pct  = move_done / total_move if total_move > 0 else 0
            loss_pct     = (entry_price - cur_p) / entry_price if entry_price > 0 else 0

            # Saída: capturou 80% do movimento restante ou stop-loss
            exit_reason = None
            if capture_pct >= EXIT_CAPTURE_PCT:
                exit_reason = "capture"
            elif loss_pct >= STOP_LOSS_PCT:
                exit_reason = "stop"
            elif i == len(hist) - 1:
                exit_reason = "end"

            if exit_reason:
                pnl_pct = (cur_p - entry_price) / entry_price * 100
                pnl_usd = ALLOC_PER_TRADE * (cur_p - entry_price) / entry_price

                trades.append({
                    "ts_entry":  entry_ts,
                    "ts_exit":   ts,
                    "entry_px":  round(entry_price, 4),
                    "exit_px":   round(cur_p, 4),
                    "pnl_pct":   round(pnl_pct, 2),
                    "pnl_usd":   round(pnl_usd, 4),
                    "reason":    exit_reason,
                    "outcome":   outcome,
                    "winner":    winner,
                    "capture":   round(capture_pct * 100, 1),
                })
                in_position = False

    return trades


# ── MAIN ──────────────────────────────────────────────────────────────────────

now_ts          = int(time.time())
backtest_end    = now_ts
backtest_start  = now_ts - (BACKTEST_DAYS * 86400)

print(f"\n  POLYMARKET 180-DAY BACKTEST")
print(f"  Período: {datetime.fromtimestamp(backtest_start, tz=timezone.utc).strftime('%d/%m/%Y')}"
      f" → {datetime.fromtimestamp(backtest_end, tz=timezone.utc).strftime('%d/%m/%Y')}\n")

print("  Coletando mercados de longa duração...")
markets = get_long_markets()
print(f"  {len(markets)} mercados encontrados (90+ dias, vol >= $50k)\n")

print("  Coletando históricos de preço...")
all_token_data = []
for i, m in enumerate(markets, 1):
    print(f"  [{i:02d}/{len(markets)}] {m['question'][:55]}", end="\r")
    try:
        token_histories = get_price_history(m["conditionId"])
        for td in token_histories:
            td["question"] = m["question"]
            td["vol"] = m["vol"]
        all_token_data.extend(token_histories)
    except Exception:
        pass
    time.sleep(0.3)

print(f"\n  {len(all_token_data)} tokens com histórico de preço\n")

# ── Simulação ─────────────────────────────────────────────────────────────────
print("  Simulando estratégia de momentum...\n")

all_trades  = []
open_slots  = 0

for td in all_token_data:
    if not td["history"]:
        continue
    trades = simulate_market(td, backtest_start, backtest_end)
    for t in trades:
        t["question"] = td["question"]
    all_trades.extend(trades)

# Simular ordem cronológica com controle de posições abertas
all_trades.sort(key=lambda x: x["ts_entry"])

balance      = STARTING_BAL
sim_trades   = []
equity_curve = [{"ts": backtest_start * 1000, "bal": balance}]
open_count   = 0

for t in all_trades:
    if open_count >= MAX_OPEN:
        continue
    if balance < ALLOC_PER_TRADE:
        continue

    alloc    = ALLOC_PER_TRADE
    pnl      = alloc * t["pnl_pct"] / 100
    balance += pnl

    dt_entry = datetime.fromtimestamp(t["ts_entry"], tz=timezone.utc).strftime("%d/%m/%Y")
    dt_exit  = datetime.fromtimestamp(t["ts_exit"], tz=timezone.utc).strftime("%d/%m/%Y")

    sim_trades.append({
        **t,
        "date_entry":  dt_entry,
        "date_exit":   dt_exit,
        "alloc":       alloc,
        "pnl":         round(pnl, 4),
        "balance":     round(balance, 4),
    })
    equity_curve.append({"ts": t["ts_exit"] * 1000, "bal": round(balance, 4)})

equity_curve.append({"ts": backtest_end * 1000, "bal": round(balance, 4)})

# ── PnL MENSAL ────────────────────────────────────────────────────────────────
monthly_pnl  = defaultdict(float)
monthly_wins = defaultdict(int)
monthly_loss = defaultdict(int)

for t in sim_trades:
    month_key = datetime.fromtimestamp(t["ts_exit"], tz=timezone.utc).strftime("%Y-%m")
    monthly_pnl[month_key]  += t["pnl"]
    if t["pnl"] > 0:
        monthly_wins[month_key] += 1
    else:
        monthly_loss[month_key] += 1

# Preencher meses sem trades
cur = datetime.fromtimestamp(backtest_start, tz=timezone.utc).replace(day=1)
end_dt = datetime.fromtimestamp(backtest_end, tz=timezone.utc).replace(day=1)
monthly_data = {}
while cur <= end_dt:
    k = cur.strftime("%Y-%m")
    monthly_data[k] = {
        "label":  cur.strftime("%b/%Y"),
        "pnl":    round(monthly_pnl.get(k, 0), 4),
        "wins":   monthly_wins.get(k, 0),
        "losses": monthly_loss.get(k, 0),
        "trades": monthly_wins.get(k, 0) + monthly_loss.get(k, 0),
    }
    # próximo mês
    if cur.month == 12:
        cur = cur.replace(year=cur.year + 1, month=1)
    else:
        cur = cur.replace(month=cur.month + 1)

# ── Estatísticas ──────────────────────────────────────────────────────────────
wins   = sum(1 for t in sim_trades if t["pnl"] > 0)
losses = sum(1 for t in sim_trades if t["pnl"] <= 0)
total_pnl     = balance - STARTING_BAL
total_pnl_pct = total_pnl / STARTING_BAL * 100

peak   = STARTING_BAL
max_dd = 0.0
for e in equity_curve:
    if e["bal"] > peak:
        peak = e["bal"]
    dd = (peak - e["bal"]) / peak * 100 if peak else 0
    if dd > max_dd:
        max_dd = dd

best  = max(sim_trades, key=lambda t: t["pnl"]) if sim_trades else None
worst = min(sim_trades, key=lambda t: t["pnl"]) if sim_trades else None

# ── Output ────────────────────────────────────────────────────────────────────
result = {
    "strategy":       "Polymarket 180-Day Momentum Backtest",
    "period_start":   datetime.fromtimestamp(backtest_start, tz=timezone.utc).strftime("%d/%m/%Y"),
    "period_end":     datetime.fromtimestamp(backtest_end, tz=timezone.utc).strftime("%d/%m/%Y"),
    "starting_bal":   STARTING_BAL,
    "final_bal":      round(balance, 4),
    "total_pnl":      round(total_pnl, 4),
    "total_pnl_pct":  round(total_pnl_pct, 2),
    "wins":           wins,
    "losses":         losses,
    "win_rate":       round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0,
    "max_drawdown":   round(max_dd, 2),
    "max_bal":        max((e["bal"] for e in equity_curve), default=STARTING_BAL),
    "min_bal":        min((e["bal"] for e in equity_curve), default=STARTING_BAL),
    "trades":         sim_trades,
    "equity_curve":   equity_curve,
    "monthly":        monthly_data,
    "best_trade":     best,
    "worst_trade":    worst,
    "markets_used":   len(all_token_data),
    "config": {
        "entry_threshold":   ENTRY_THRESHOLD,
        "momentum_days":     MOMENTUM_DAYS,
        "exit_capture_pct":  EXIT_CAPTURE_PCT * 100,
        "stop_loss_pct":     STOP_LOSS_PCT * 100,
        "alloc_per_trade":   ALLOC_PER_TRADE,
        "max_open":          MAX_OPEN,
    },
}

with open("/tmp/polymarket_180d.json", "w") as f:
    json.dump(result, f, indent=2)

sign = "+" if total_pnl >= 0 else ""
print(f"\n{'═'*65}")
print(f"  POLYMARKET — BACKTEST 180 DIAS")
print(f"  {result['period_start']} → {result['period_end']}")
print(f"{'═'*65}")
print(f"  Saldo inicial:  ${STARTING_BAL:,.2f}")
print(f"  Saldo final:    ${balance:,.2f}")
print(f"  PnL total:      {sign}${total_pnl:,.4f}  ({sign}{total_pnl_pct:.2f}%)")
print(f"  Win rate:       {result['win_rate']}%  ({wins}W / {losses}L)")
print(f"  Trades:         {wins + losses}")
print(f"  Max drawdown:   -{max_dd:.2f}%")
print(f"  Mercados:       {len(all_token_data)} tokens")
print(f"\n  PnL POR MÊS:")
print(f"  {'Mês':<12} {'PnL':>10} {'W':>4} {'L':>4} {'Trades':>7}")
print(f"  {'─'*40}")
for k in sorted(monthly_data.keys()):
    m  = monthly_data[k]
    s  = "+" if m["pnl"] >= 0 else ""
    cl = "▲" if m["pnl"] >= 0 else "▼"
    print(f"  {m['label']:<12} {s}${m['pnl']:>8.2f}  {m['wins']:>3}W  {m['losses']:>3}L  {cl}")
print(f"{'═'*65}")
print(f"\n  Salvo em /tmp/polymarket_180d.json")
