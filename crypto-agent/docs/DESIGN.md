# Diseño

## Objetivo

Un sistema que investiga el mercado de criptomonedas, encuentra oportunidades y las
ejecuta solo, 24/7. Esta primera fase construye el motor completo en **simulación**:
decide, opera una cartera ficticia, se mide a sí mismo y propone mejoras. No toca dinero
real.

Criterio de éxito de esta fase: que sobreviva (pérdidas acotadas, sin fallos silenciosos)
y que diga con honestidad si hay ventaja o no. No es un criterio de rentabilidad.

## Dos capas que no se mezclan

```
                 ┌────────────────────────────── CÓDIGO ──────────────────────────────┐
 velas, libro    │  estado ──► REFLEX ──► puertas ──► tamaño ──► riesgo ──► bróker    │
 y cuenta   ───► │ (state)    (opina)    (engine)    (sizing)   (risk)    (simulado)  │
                 │     ▲                                                      │       │
                 │     └──────── calibración ◄── señales de seguimiento ◄─────┘       │
                 └────────────────────────────────────────────────────────────────────┘
```

- **REFLEX** (`agent/reflex.py`): juzga una vela y devuelve `ReflexDecision`. Dos
  implementaciones tras la misma interfaz: reglas deterministas y Claude Haiku con salida
  estructurada y esfuerzo bajo. No conoce tamaños, órdenes ni límites.
- **Código**: todo lo demás. Cada umbral, tamaño, orden y efecto secundario.

La capa de planificación (investigación, estrategia, revisión de código) es el trabajo de
desarrollo en sí; no corre dentro del agente.

## Esquema de decisión (`agent/schema.py`)

| Campo | Valores | Uso en código |
|---|---|---|
| `regime` | trending, mean_reverting, high_vol, crisis | `crisis` impide abrir riesgo y cierra la posición |
| `direction` | long, short, neutral | Solo `long` abre posición (spot) |
| `direction_confidence` | 0 a 1 | Puerta: > 0,80. También indexa la calibración |
| `toxic_flow` | true, false, null | `true` impide entrar. `null` si no hay libro de órdenes |
| `setup_quality` | 0 a 3 | Puerta: ≥ 2 |
| `risk_state` | safe, near_limit, reduce | Solo puede endurecer el estado que calcula el código |

Los umbrales son hipótesis de partida, no evidencia de ventaja.

## Motor de estado (`agent/state.py`)

Una instantánea numérica por ticker y vela, de menos de 400 tokens (estimados):
rentabilidades a 1, 4, 24 y 168 velas, ATR, volatilidad y su ratio, RSI, distancia a las
medias de 50 y 200 en unidades de ATR, posición en el rango, ratio de volumen, caída
desde máximos, contexto de BTC, spread y desequilibrio del libro, e inventario,
exposición, caída y resultado del día de la cuenta.

Garantías, cubiertas por tests:

- **Causalidad**: solo velas cerradas hasta el instante de la decisión.
- **Calidad de datos**: huecos, desorden, feed caducado, vela en curso, valores inválidos
  y reloj desfasado se detectan antes de cualquier orden.

## Capa de riesgo (`agent/risk.py`, `agent/config.py`)

| Límite | Valor por defecto | Tope en código |
|---|---|---|
| Riesgo por operación | 2 % de la cartera | 5 % |
| Pérdida diaria | 5 % | 20 % |
| Posiciones abiertas | 3 | 10 |
| Apalancamiento | 2x (en spot, 1x efectivo) | 3x |
| Interruptor de emergencia | 15 % de caída | 50 % |
| Pausa por confianza baja | < 0,60 | — |

- Se comprueba antes de cada orden y solo puede recortar el tamaño.
- El interruptor cierra todo, se guarda en disco y solo se rearma a mano.
- Las salidas siempre están permitidas y se evalúan antes de llamar al REFLEX.
- Una diferencia entre el diario de órdenes y el estado del bróker detiene las compras.

## Tamaño (`agent/sizing.py`, `agent/calibration.py`)

1. Cada opinión de compra del REFLEX abre una **señal de seguimiento**: una operación
   hipotética con el mismo stop, objetivo y duración, se opere o no.
2. Al resolverse, actualiza la tasa de acierto de su tramo de confianza.
3. El tamaño usa Kelly sobre esa tasa, descontando costes, con un máximo de un cuarto de
   Kelly y del 2 % de riesgo.
4. El punto de partida es la probabilidad sin ventaja, 1/(1+b). Para superarlo hacen
   falta 50 señales en el tramo y que la tasa menos dos errores típicos siga por encima.

Consecuencia: un REFLEX nuevo no arriesga nada hasta demostrar algo.

## Backtest (`agent/backtest.py`)

- Mismo `Engine` que en vivo, vela a vela. Decide al cierre, ejecuta a la apertura
  siguiente.
- Comisiones y deslizamiento siempre activos; liquidez limitada al 1 % del volumen de la
  vela.
- La calibración avanza en el tiempo: en cada instante solo conoce señales ya resueltas.
- Se compara siempre con comprar BTC y no tocarlo, con los mismos costes.
- Con un REFLEX basado en modelo solo valen datos posteriores a junio de 2026.

## Mejora continua (`agent/review.py`, `agent/policy.py`)

Cada noche: órdenes y fallos del día, latencia, tabla de calibración, índice de Brier
(confianza declarada, calibrada y tasa media) y, si procede, una política candidata.

- Las candidatas son ficheros versionados en `data/policies/`.
- Activar o revertir una versión exige `--confirm`. Nada se promociona solo.
- Si cambia el REFLEX o la estructura de la operación, la calibración se reinicia.

## Aprobaciones

| Sin aprobación | Con tu visto bueno explícito |
|---|---|
| Leer datos, backtests, operar en simulación, reintentar llamadas | Órdenes reales, mover fondos, claves con permiso de trading, desplegar a producción |

El modo real no está implementado; `mode = "live"` falla a propósito.

## Fuera de alcance en esta fase

Órdenes reales, perpetuos y ventas en corto, investigación fundamental de candidatos,
datos de régimen externos (dominancia, stablecoins, financiación, interés abierto), stops
dentro de la vela y panel web.
