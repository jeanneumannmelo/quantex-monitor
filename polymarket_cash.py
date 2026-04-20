#!/usr/bin/env python3
"""
Módulo de gestão de caixa matemática — Kelly Criterion + scoring de wallets.
Sem dependências dos outros módulos do projeto.
"""

import math


# ── Kelly Criterion ───────────────────────────────────────────────────────────

def kelly_fraction(p_win: float, b_net_odds: float, kelly_frac: float = 0.25) -> float:
    """
    Calcula a fração do caixa a alocar via Kelly Criterion.

    f* = (b·p - q) / b  × kelly_frac

    p_win:       probabilidade de ganho estimada (win rate histórico do wallet)
    b_net_odds:  (avg_exit - entry) / entry — retorno líquido esperado
    kelly_frac:  fração do Kelly completo a usar (0.25 = quarter-Kelly, padrão seguro)
    """
    if b_net_odds <= 0 or p_win <= 0 or p_win >= 1:
        return 0.0
    q = 1.0 - p_win
    f_full = (b_net_odds * p_win - q) / b_net_odds
    return max(0.0, f_full * kelly_frac)


def position_size(
    balance:          float,
    p_win:            float,
    entry_price:      float,
    avg_exit_price:   float,
    kelly_frac:       float = 0.25,
    min_size:         float = 5.0,
    max_size:         float = 30.0,
    skip_negative_ev: bool  = True,
) -> float:
    """
    Retorna o valor em USDC a alocar em um trade.

    Exemplos para saldo $179.36, WR=81.8%:
      entry=0.35, exit=0.80  →  b=1.286 → f*=67.6% → 25%K=16.9% → $30.31 → cap $30
      entry=0.55, exit=0.85  →  b=0.545 → f*=53.6% → 25%K=13.4% → $24.02
      entry=0.70, exit=0.90  →  b=0.286 → f*=18.2% → 25%K=4.6%  → $8.19

    Retorna 0.0 quando EV é negativo (skip_negative_ev=True) ou Kelly é zero.
    """
    if entry_price <= 0 or balance <= 0:
        return 0.0
    if avg_exit_price <= entry_price:
        return 0.0 if skip_negative_ev else min_size
    b_net = (avg_exit_price - entry_price) / entry_price
    frac  = kelly_fraction(p_win, b_net, kelly_frac)
    if frac <= 0:
        return 0.0
    raw = balance * frac
    return round(max(min_size, min(max_size, raw)), 2)


# ── Scoring de wallets ────────────────────────────────────────────────────────

def score_wallet(w: dict) -> float:
    """
    Score composto para ranquear wallets a copiar.

    Score = 0.50 × (avg_eq/100) + 0.30 × (win_rate/100) + 0.20 × log(trades)/log(100)

    Prioriza exit quality (50%), win rate (30%) e volume de dados (20%).
    """
    eq     = w.get("avg_eq", 0.0) / 100.0
    wr     = w.get("win_rate", 0.0) / 100.0
    n      = max(1, w.get("n_trades", w.get("trades", 1)))
    volume = min(1.0, math.log(n) / math.log(100))
    return round(0.50 * eq + 0.30 * wr + 0.20 * volume, 4)


# ── Utilidades ────────────────────────────────────────────────────────────────

def expected_exit_price(entry_price: float, exit_quality: float) -> float:
    """
    Estima o preço de saída esperado com base no exit quality histórico do wallet.

    eq=100% → saiu no preço 1.0 (resolução completa)
    eq=60%  → saiu quando capturou 60% do movimento até 1.0
    """
    eq = max(0.0, min(100.0, exit_quality)) / 100.0
    return round(entry_price + (1.0 - entry_price) * eq, 4)
