# Reglas de riesgo — Mesa paper-trading Solana

Todo en este archivo se aplica **en código, fuera de la IA**. Jev no puede anular ninguna regla.

## 1. TAMAÑO (SIZE)
1. **Fijo 1% del capital por trade** hasta que la calibración demuestre edge.
   Solo tras calibración **verificada por el auditor**: `ticket = kelly(edge) * bank`, con tope **6% del bank**.
2. Factores multiplicativos: **×0.4** datos oscuros (fuente faltante/inconsistente), **×0.6** sin cuenta X.
3. `ticket = min(ticket, 2% de la liquidez del pool)`.
4. Piso de comisiones: si comisiones ida+vuelta (swap + priority fee + slippage esperado) > edge → **no operar** (se registra).

## 2. EJECUCIÓN (FILLS)
- Precio = quote real de Jupiter en el momento (fill simulado sobre esa quote).
- Slippage máximo configurado (memecoins 1.5%, líquido 0.3%); si la quote lo excede → no fill.
- Priority fee incluida en costo.
- Sin ruta de Jupiter o sin quote de venta → **NO TRADE** (se registra).
- Jev se abstiene, error o timeout → **NO TRADE** (se registra).
- Cada trade tiene **id único**.
- Si falla: reintentar **una vez a los 30 s**; si vuelve a fallar → cancelar y registrar.
- **2 RPCs** (primaria + respaldo); discrepancia o dato viejo → no operar.
- **No operar con datos stale** (umbral p.ej. > 60 s de antigüedad).

## 3. SALIDAS (EXITS)
- 25% a **+15%**, 25% a **+30%**, resto con **trailing stop**.
- Stop loss **−15%**. Trailing stop **10% desde el pico**.
- Stops y trailing se llenan a la **quote real de venta de Jupiter en el momento en que el código detecta el disparo**, incluido slippage.
- Posiciones revisadas **cada 60 s**.
- Regla de volumen: si `volume.h6 / (volume.h24/4) < 0.20` → **cerrar todo**.
- Máx. **1 posición por token**, máx. **3 concurrentes**.

## 4. LÍMITES DUROS
- Pérdida diaria y total **incluyen PnL no realizado** (mark-to-market con quote de venta de Jupiter).
- Pérdida diaria **5%** → halt hasta el día siguiente.
- Drawdown **25%** desde el pico → parada total (requiere reinicio manual).
- **Kill switch** manual que cierra todo y bloquea nuevas entradas.
- Apalancamiento: **ninguno en paper**. Más adelante: solo BTC, máx. **2x**.

## 5. BASELINES (comparación)
1. Hold SOL. 2. Hold BTC.
3. Regla simple: filtro duro + stop −15% + salida por tiempo a 24h, **sin Jev**.
La regla simple corre sobre **los mismos candidatos, mismos timestamps, mismos costos y mismo modelo de fill** que la estrategia Jev.
Todo comparado neto de comisiones.

## 6. PUERTA A REAL (GO-LIVE)
Protocolo: **semana 1 = calibración** (se pueden ajustar umbrales); **semana 2 = evaluación** con umbrales **congelados**. La evaluación solo cuenta trades de la semana 2.

Todos obligatorios:
- ≥ **2 semanas** **Y** ≥ **65 trades simulados** (pendiente confirmación de 069).
- Supera a **hold BTC**, **hold SOL** y la **regla simple** después de comisiones.
- **Auditor** aprobado.
- **Aprobación explícita de 069**.

## 7. REGISTRO INMUTABLE
Cada decisión (incluidos rechazos y "none") en log append-only con hash encadenado: timestamp, token/mint, precio visto, state completo enviado a Jev, probabilidades de Jev, probabilidad/score/confianza **antes del resultado**, regla que decidió, tamaño, quote, fill o motivo de rechazo. Resultado se anexa después, nunca se edita.

## edge_est (elección final)
- p = 0.5*p_jev + 0.5*base_rate  (p_jev = 1 − P(momentum agotado) de Q2; NO es prob. de ganar, se calibra en semana 1 contra ganadores reales; base_rate = % de ganadores de la regla simple en paper, 0.35 hasta tener 20 trades)
- avg_win = 0.20, avg_loss = 0.15 (iniciales; se reemplazan por promedios reales del paper tras 20 trades, congelados en semana 2)
- costs = slippage + fees ida y vuelta según cotización Jupiter (compra y venta) para el tamaño real
- edge_est = p*avg_win - (1-p)*avg_loss - costs
- Entrar solo si edge_est >= 0.02. Con varios candidatos, el de mayor edge_est. Se registra en la bitácora con sus insumos.

## Gate actualizado (069, 29 oct)
- Mínimo 2 semanas. Al cumplirse, revisión de progreso con 069; con ≥30 trades puede bastar (65 sigue siendo la meta ideal).
- Siguen obligatorios: superar a BTC, SOL y la regla simple después de fees, pase del auditor y aprobación explícita de 069.
- Nota: con avg_win 0.20, avg_loss 0.15 y costos ~1%, se necesita p ≥ 0.51, o sea p_jev ≥ 0.68 (intencional).
- Los ≥30 trades cuentan solo en la semana de evaluación (umbrales congelados). Si son menos de 30, el veredicto es "insuficiente".
- Relajar filtros solo en semana 1, con motivo registrado y aplicado igual a la regla simple. Semana 2 congelada.
- Carriles separados: memecoins y líquido (SOL/cbBTC) con su propio conteo y comparaciones; no se suman.
- Controles de 069: reducir riesgo se aplica al momento; subirlo requiere auditor. Toda pausa aplica a Jev y regla simple del carril a la vez, con hora y motivo en bitácora; el reporte muestra tiempo pausado.
