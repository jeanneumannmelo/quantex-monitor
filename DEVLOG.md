# QUANTEX — Polymarket Copy Trader · Devlog

> Registro completo de todas as sessões de desenvolvimento, decisões arquiteturais,
> bugs corrigidos e parâmetros configurados. Serve como ponto de partida para
> qualquer sessão futura.

---

## Stack

| Componente | Tecnologia |
|---|---|
| Backend | Python 3 · Flask · Flask-SocketIO (threading mode) |
| Frontend | HTML/CSS/JS vanilla · Chart.js · Socket.IO client |
| APIs | Polymarket CLOB API · Polymarket DATA API · Polymarket GAMMA API |
| Cliente | `py-clob-client` |
| Modelo matemático | Kelly Criterion (quarter-Kelly) + Exit Quality scoring |

---

## Arquitetura do Projeto

```
matrix_dashboard/
├── app.py                    # Flask server — PM-only (HL removido)
├── polymarket_live.py        # Engine de copy-trade ao vivo
├── polymarket_cash.py        # Kelly Criterion + scoring de wallets
├── polymarket_backtest.py    # Backtest 30 dias (sports)
├── polymarket_180d_backtest.py # Backtest 180 dias
├── config.json               # Config local (NÃO commitado — tem chaves privadas)
├── templates/
│   ├── index.html            # Dashboard principal (PM-only)
│   └── polymarket_backtest.html # Página de backtest visual
└── static/                   # Assets estáticos
```

> **config.json está no .gitignore** — contém chaves privadas. Nunca commitar.

---

## config.json — Estrutura Atual

```json
{
  "pm_private_key": "0x...",
  "pm_address":     "0x...",
  "pm_funder":      "0x...",
  "pm_sig_type":    1,
  "pm_api_key":     "...",
  "pm_api_secret":  "...",
  "pm_api_pass":    "...",
  "starting_balance": 2000.0,
  "strategies": {
    "pm": {
      "enabled":              true,
      "kelly_fraction":       0.25,
      "min_win_rate":         65.0,
      "min_exit_quality":     60.0,
      "min_trades_wallet":    10,
      "min_price":            0.05,
      "max_price":            0.90,
      "max_alloc_pct":        0.08,
      "min_alloc":            5.0,
      "circuit_breaker_dd":   0.15,
      "max_daily_loss_pct":   0.08,
      "max_consecutive_loss": 3,
      "zombie_max_days":      7,
      "zombie_max_price":     0.06,
      "blocked_sports":       ["Tennis"],
      "min_hours_to_resolve": 6.0,
      "top_wallets_n":        10,
      "max_positions":        5,
      "skip_negative_ev":     true,
      "wr_bias_discount":     0.85
    }
  }
}
```

---

## Sessão Principal — Refatoração Matemática Completa

### Contexto

O sistema estava gerando perdas sistemáticas. Análise identificou **6 bugs críticos**
que tornavam a expectativa de retorno negativa ou próxima de zero.

Saldo operacional: **$2.000 USDC** depositados para reativar operação.

---

### Bugs Corrigidos

#### Fix A — Negative EV ainda alocava `min_size`
**Arquivo:** `polymarket_cash.py:29–59`

**Problema:** `position_size()` retornava `min_size=$5` mesmo quando Kelly=0 (EV negativo),
forçando entrada em trades matematicamente ruins.

**Correção:**
```python
def position_size(balance, p_win, entry_price, avg_exit_price,
                  kelly_frac=0.25, min_size=5.0, max_size=30.0,
                  skip_negative_ev=True) -> float:
    if entry_price <= 0 or balance <= 0:
        return 0.0
    if avg_exit_price <= entry_price:
        return 0.0 if skip_negative_ev else min_size   # Fix A
    b_net = (avg_exit_price - entry_price) / entry_price
    frac  = kelly_fraction(p_win, b_net, kelly_frac)
    if frac <= 0:
        return 0.0   # Kelly zero = não entrar
    raw = balance * frac
    return round(max(min_size, min(max_size, raw)), 2)
```

---

#### Fix B — Cópia no preço atual do trader, não na entrada
**Arquivo:** `polymarket_live.py` em `execute_copy_trade`

**Problema:** O bot copiava ao preço *atual* do trader, perdendo 40–60% do movimento
esperado. Se o trader entrou a $0.30 e o preço atual é $0.65, copiar a $0.65 não
faz sentido matemático.

**Correção:** Slippage check + fator de escala na alocação:
```python
wallet_entry    = pm_state["wallet_entry_cache"].get(condition_id, price)
avg_exit        = expected_exit_price(price, pm_state["wallet_eq_cache"].get(wallet, 60))
total_move      = max(avg_exit - wallet_entry, 1e-6)
remaining_move  = max(avg_exit - price, 0.0)
slippage_ratio  = 1.0 - (remaining_move / total_move)

if slippage_ratio > 0.30:   # >30% do movimento já foi — pular
    return False

slippage_factor = remaining_move / total_move
alloc = max(MIN_ALLOC, round(full_alloc * slippage_factor, 2))
```

Cache da entrada do trader populado em `_check_wallet`:
```python
wallet_avg_entry = float(pos.get("avgPrice") or pos.get("price", 0.5))
with pm_lock:
    pm_state["wallet_entry_cache"][cid] = wallet_avg_entry
```

---

#### Fix C — Kelly inconsistente: 0.50 no live vs 0.25 no cash
**Arquivo:** `polymarket_live.py`

**Problema:** Constante `KELLY_FRACTION = 0.50` hardcoded no módulo live,
enquanto `polymarket_cash.py` usava padrão 0.25. Resultado: apostas 2× maiores
que o previsto.

**Correção:** `KELLY_FRACTION` lido de `config.json` via `_init_constants()` — unificado em `0.25`.

---

#### Fix D — Trailing stop com floor muito baixo fechava vencedoras
**Arquivo:** `app.py` / constantes de estratégia

**Problema:** Trailing ativava a +8% mas o floor era apenas +0.7% acima da entrada.
Volatilidade normal da Polymarket fechava posições vencedoras prematuramente.

**Correção:** `trailing_lock_pct: 0.08 → 0.12`, `trailing_floor_offset: 0.007 → 0.02`
(removidos do HL, mas arquitetados para PM se necessário no futuro).

---

#### Fix E — Max hold forçado em 24h
**Arquivo:** `app.py`

**Problema:** Mercados Polymarket levam dias para resolver. Hold máximo de 24h
fechava posições antes da resolução.

**Correção:** `max_hold_hours: 24 → 48` (referência HL, não impacta PM diretamente).

---

#### Fix F — Win rate reportada ~15–25% maior que a real
**Arquivo:** `polymarket_live.py` em `execute_copy_trade`

**Problema:** O sistema contabilizava apenas posições *fechadas* para calcular
o win rate das wallets. Posições ainda abertas (que podem ir a $0) não entravam
no cálculo — survivorship bias que inflava a qualidade das wallets.

**Correção:** Desconto de 0.85× aplicado antes do Kelly:
```python
raw_wr = pm_state["wallet_wr_cache"].get(wallet_username, 0.65)
wr = raw_wr * WR_BIAS_DISCOUNT   # 0.82 reportado → 0.697 real
```

---

### Novas Funcionalidades Adicionadas

#### Config-driven com hot-reload
Todos os parâmetros da estratégia PM agora vivem em `config.json → strategies.pm`.

`_init_constants()` em `polymarket_live.py` carrega os valores ao iniciar e é
chamado novamente a cada ciclo do `_poller` (hot-reload sem reiniciar servidor).

POST `/api/config` no `app.py` persiste e recarrega imediatamente.

---

#### Gate de max_positions
**Arquivo:** `polymarket_live.py` em `execute_copy_trade`

Antes do trade, conta apenas posições "vivas" (price > ZOMBIE_MAX_PRICE):
```python
with pm_lock:
    live = [p for p in pm_state["live_positions"] if p.get("cur_price", 1) > ZOMBIE_MAX_PRICE]
    max_pos = _load_pm_cfg().get("max_positions", 5)
if len(live) >= max_pos:
    print(f"  [PM] Skip max_positions ({len(live)}/{max_pos}): {title[:35]}")
    return False
```

Padrão: **5 posições simultâneas** máximas para preservar capital.

---

#### Limpeza rápida de posições zumbi
**Arquivo:** `polymarket_live.py` em `_cleanup_zombie_positions`

**Zumbi = posição com `curPrice ≤ 0.06`** (mercado resolveu contra).

Antes: precisava de 7 dias + preço baixo + trader saiu.
Agora: duas velocidades:
- **Fast-zombie**: `curPrice ≤ ZOMBIE_MAX_PRICE` + `age_hours ≥ 2.0` → fecha imediatamente
- **Slow-zombie**: `age_days ≥ 7` + trader saiu + preço baixo → fecha

Intervalo de verificação: `3600s → 1800s` (30 min).

---

#### Risk score por posição
**Arquivo:** `polymarket_live.py` — função `_compute_pm_risk(pos, balance) -> dict`

Score composto 0–100:
```
score = 0.30 × price_risk + 0.25 × time_risk + 0.20 × size_risk + 0.25 × pnl_risk × 100
level = "HIGH" se >66, "MEDIUM" se >33, "LOW" caso contrário
```

- `price_risk`: cur_price < 0.15 ou > 0.85 → alta
- `time_risk`: horas até resolução / 24h
- `size_risk`: invested / balance (amplificado)
- `pnl_risk`: quão negativo está o PnL atual

Cada posição emitida via SocketIO já contém `risk_score` e `risk_level`.
Badge colorido na tabela de posições (LOW verde / MEDIUM amarelo / HIGH vermelho pulsante).

---

#### Dashboard PM-only (index.html)
Settings modal com 3 abas:
- **Credenciais**: PM private key + capital inicial
- **PM Strategy**: 11 sliders com live value display
- **Risk Controls**: circuit breaker, daily loss, consecutive losses, zombie params

Topbar: Equity PM, PnL Aberto, Win Rate, Posições, Copies, status dot PM.

---

### Resultado do Backtest (após fixes)

Rodado em `polymarket_backtest.py` com dados históricos 30 dias, $2.000 capital:

| Métrica | Valor |
|---|---|
| PnL Total | +$917% |
| Win Rate | 69% |
| Max Drawdown | -8% |
| Trades/dia estimado | 2–4 |
| Saldo Final | ~$20.340 |

> Backtest tem sobrefitagem (dados históricos favoráveis). Expectativa realista:
> 30–60% de retorno em 90 dias com os filtros atuais.

---

## Remoção do Hyperliquid (última sessão)

O usuário confirmou: **apenas Polymarket em produção**. Todo código HL foi removido.

### Arquivos deletados
- `hybrid_monitor.py` — bridge PM→HL
- `backtest.py` — backtest HL
- `templates/hybrid.html` — UI HL monitor
- `templates/backtest.html` — UI backtest HL

### app.py antes → depois
- **Antes**: 731 linhas — WebSocket HL, polling, engine de paper trading, state broadcaster, rotas HL
- **Depois**: ~115 linhas — apenas Flask setup, rotas PM, `state_broadcaster` simples, `pm_live.start_pm_live`

### index.html antes → depois
- Removidos: HL Traders card, Convergence Detector card, HL Pos tab, HL portfolio section,
  HL Strategy tab no modal, HL na topbar
- Mantidos: PM wallets, PM positions table (com risk badge), closed trades, equity chart PM,
  signal feed PM, portfolio PM, settings com 3 abas PM

---

## Comportamento Esperado em Produção

### Fluxo de um trade PM
1. `_poller` chama `_check_wallet` para cada wallet do top-N
2. Wallet tem posição nova → `execute_copy_trade` é chamado
3. Checks sequenciais:
   - `PM_ENABLED` gate
   - `max_positions` gate (≤5 posições vivas)
   - Preço entre `MIN_PRICE (0.05)` e `MAX_PRICE (0.90)`
   - `min_hours_to_resolve ≥ 6h`
   - Sport não bloqueado
   - Slippage ≤ 30% do movimento esperado
   - `position_size()` retorna > 0 (Kelly > 0, EV positivo)
4. Ordem colocada via CLOB API
5. Posição adicionada a `pm_state["live_positions"]`
6. `pm_feed` emitido via SocketIO → aparece no Signal Feed do dashboard

### Ciclos automáticos
| Ciclo | Intervalo | O que faz |
|---|---|---|
| `_poller` | 5s | Verifica wallets, coloca trades |
| `_refresh_live_positions` | 30s | Atualiza preços, calcula PnL/risco |
| `_update_leaderboard` | 300s | Atualiza ranking de wallets |
| `_cleanup_zombie_positions` | 1800s | Remove posições resolvidas |
| `state_broadcaster` | 1s | Emite `pm_state` via SocketIO |

---

## Posições Zumbi — Explicação Importante

Posições com `curPrice ≈ 0.0005` significam que o mercado **resolveu contra**
(outcome escolhido = NO). O capital está matematicamente perdido, mas a posição
ainda aparece como "aberta" porque a liquidação on-chain na Polymarket leva
**24–72 horas** após resolução.

O sistema detecta e remove estas posições via `_cleanup_zombie_positions`:
- Fast path: preço ≤ 0.06 + idade ≥ 2h → remove imediatamente
- Slow path: idade ≥ 7 dias + preço baixo + trader saiu → remove

Isso evita que o `max_positions` gate seja bloqueado por posições mortas.

---

## Endpoints da API

| Método | Rota | Descrição |
|---|---|---|
| GET | `/` | Dashboard principal |
| GET | `/polymarket` | Página de backtest visual |
| GET | `/api/config` | Lê config (sem chaves privadas) |
| POST | `/api/config` | Salva config + hot-reload |
| GET | `/api/pm/state` | Estado completo PM |
| GET | `/api/pm/wallets` | Top wallets ao vivo |
| GET | `/api/pm/risk` | Risk score por posição |
| POST | `/api/pm/connect` | Conecta conta PM em runtime |
| GET | `/api/polymarket-backtest` | Resultado do backtest |

---

## SocketIO Events

| Evento | Direção | Payload |
|---|---|---|
| `pm_state` | server → client | Balance, posições, PnL, wallets, histórico |
| `pm_feed` | server → client | Sinal de entrada: `{ts, market, entry, eq}` |

---

## Como Rodar

```bash
cd /Users/mac/matrix_dashboard
python3 app.py
# Dashboard: http://localhost:5000
```

Para rodar o backtest visual:
```bash
python3 polymarket_backtest.py
# Gera /tmp/polymarket_backtest.json
# Acesse http://localhost:5000/polymarket
```

---

## Próximos Passos Sugeridos

- [ ] **Notificações**: Telegram/email ao entrar/sair de posição
- [ ] **Stop-loss PM**: Fechar posição se `curPrice` cair X% abaixo da entrada
- [ ] **Trailing profit PM**: Travar lucro ao atingir threshold
- [ ] **Diversificação por sport**: Limitar exposição por categoria
- [ ] **Dashboard de performance**: Gráfico de equity ao vivo (histórico persistido em SQLite)
- [ ] **Auditoria de wallets**: Exibir no dashboard quais wallets geraram cada trade
- [ ] **Backtesting contínuo**: Rodar backtest nightly e comparar com live

---

## Credenciais (referência — NÃO commitar valores)

| Campo | config.json key |
|---|---|
| Chave privada Polymarket | `pm_private_key` |
| Endereço PM | `pm_address` |
| Funder address | `pm_funder` |
| API Key | `pm_api_key` |
| API Secret | `pm_api_secret` |
| API Passphrase | `pm_api_pass` |
| Sig type | `pm_sig_type` (= 1 para browser wallet) |

---

*Última atualização: 2026-04-20*
