# Preguntas y filtros — Mesa paper-trading Solana

Carriles: **memecoins** y **líquido (SOL / cbBTC vía Jupiter)**.
Flujo: colector (GeckoTerminal, DexScreener, RPC pública Solana, cuenta X del proyecto) → filtro duro en código → Jev (TypeSafe AI) → sizing/riesgo en código → fills simulados con quotes reales de Jupiter.

## 1. Filtro duro (código, sin IA) — carril memecoins
Rechazar si falla CUALQUIERA (se registra el rechazo con motivo):

| Regla | Umbral |
|---|---|
| Liquidez del pool | ≥ $12,000 |
| Volumen 24h | ≥ $48,000 |
| Top holder (excl. pool/LP y burn) | ≤ 5% |
| Top 10 holders | ≤ 60% |
| Mint authority | revocada (null) |
| Freeze authority | revocada (null) |
| Programa | no Token-2022 con extensión transfer hook (ni fee/permanent delegate) |
| Copycat | nombre/símbolo no duplica token existente más antiguo; contrato distinto del original |
| LP | quemado o bloqueado (verificado on-chain) |

Datos faltantes en un campo obligatorio = rechazo.

## 2. Preguntas Jev
Reglas generales:
- Todo número del `state` se calcula en código antes de preguntar; Jev no calcula ni busca datos.
- SDK: `AsyncTypeSafeClient` con `Noul`, `Choice`, `Score`.
- Abstención, error o timeout de Jev = **NO TRADE** (se registra).
- **La confianza/probabilidad no es precisión.** Los umbrales son iniciales y se calibran en paper (curvas de calibración por pregunta, Brier score).

### Q1 — Ballena distribuyendo (Noul)
- **State:** `top_holder_pct`, `top10_pct`, `top10_net_flow_6h_pct` (cambio % de saldo), `n_wallets_selling_top20_6h`, `largest_sell_usd_6h`, `sell_buy_ratio_h1`, `sell_buy_ratio_h6`, `price_change_h6`.
- **Pregunta:** "Según estos datos, ¿una o más wallets grandes (top 20) están distribuyendo (vendiendo de forma sostenida) este token? Responde sí/no con probabilidad."
- **Decisión:** P(sí) ≥ 0.60 → rechazar.

### Q2 — Momentum agotado (Noul)
- **State:** `price_change_m5/h1/h6/h24`, `volume_h1`, `volume_h6`, `volume_h24`, `vol_ratio = volume.h6/(volume.h24/4)`, `buys_h1`, `sells_h1`, `ath_drawdown_pct`, `token_age_h`.
- **Pregunta:** "¿El impulso alcista de este token está agotado (volumen y compras decayendo tras el pico) de modo que es improbable otra subida ≥15% en 24h? Sí/no con probabilidad."
- **Decisión:** P(sí) ≥ 0.55 → rechazar.

### Q3 — Cuenta X reciclada/falsa (Noul)
- **State:** `x_account_age_days`, `x_handle_changes`, `x_followers`, `x_following`, `x_posts_total`, `x_posts_before_token`, `x_bio_mentions_contract` (bool), `contract_matches` (bool), `engagement_rate`, `pct_followers_suspected_bot`.
- **Pregunta:** "¿La cuenta X del proyecto parece reciclada (renombrada/comprada) o falsa respecto a este contrato? Sí/no con probabilidad."
- **Decisión:** P(sí) ≥ 0.50 → rechazar. Sin cuenta X → no se pregunta; factor de tamaño 0.6 (ver risk_rules).

### Q4 — Fuerza de narrativa (Score 1–10, con confianza)
- **State:** `narrative_tag` (texto corto del colector), `n_similar_tokens_24h`, `mentions_x_24h`, `mention_growth_pct`, `volume_h24`, `holders_count`, `holder_growth_h24_pct`.
- **Rúbrica:** 1–2 sin narrativa / ruido; 3–4 tema débil o saturado; 5–6 tema activo sin tracción propia; 7–8 tema en alza con tracción medible; 9–10 narrativa dominante del día con crecimiento fuerte.
- **Decisión:** score ≥ 6 y confianza ≥ 0.5 para pasar a Q5.

### Q5 — Selección final entre sobrevivientes (Choice)
- **State:** lista de candidatos (máx. 254) con `symbol`, `mint`, `liquidity`, `volume_h24`, `vol_ratio`, `narrative_score`, `P_whale`, `P_exhausted`, `edge_est` (de código).
- **Pregunta:** "Elige como máximo UN token para abrir posición ahora, o 'none' si ninguno ofrece ventaja clara."
- **Opciones:** `[<symbol_mint_1>, …, <symbol_mint_n>, "none"]` (≤ 255).
- **Decisión:** se ejecuta la elección solo si pasa sizing; "none" es una salida válida y se registra.

### Q6 — Carril líquido SOL / cbBTC (Choice)
- **State (por activo):** `price`, `ret_1h/4h/24h/7d`, `rsi_14_4h`, `ema20_vs_ema50_4h_pct`, `atr_pct_24h`, `volume_24h_vs_7d_avg`, `funding_proxy` (si existe), `position_open` (bool), `unrealized_pnl_pct`.
- **Pregunta:** "Para {SOL|cbBTC} en el horizonte de 24h, ¿cuál es la acción?"
- **Opciones:** `["BUY", "SELL", "HOLD"]` (SELL = cerrar/reducir; sin cortos en paper).
- **Decisión:** actuar solo si confianza ≥ 0.60; si no, HOLD.
- **La confianza de Q6 ≠ probabilidad de ganar.** Debe calibrarse por separado (buckets de fiabilidad) antes de usarse como umbral; hasta entonces el umbral 0.60 es provisional.

## 3. Calibración
Cada pregunta guarda probabilidad/score antes del resultado. Revisión semanal: ajustar umbrales por bins de probabilidad vs. tasa real; nunca ajustar mirando operaciones individuales. Ajustes solo en semana 1; en semana 2 (evaluación) los umbrales quedan congelados.
