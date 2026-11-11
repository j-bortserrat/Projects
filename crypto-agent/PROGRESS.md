# Progreso

Léelo al empezar cada sesión de trabajo. Última actualización: 2026-11-11.

## Respuestas del usuario (2026-11-10)

Binance · spot · 1.000 $ ficticios · sin memecoins ni monedas poco líquidas · sin clave
de Jev (se usa Claude Haiku) · velas de 1 hora · se ejecuta en su PC.

## Hecho y probado

- Motor de estado, REFLEX de reglas, capa de riesgo, tamaño por Kelly calibrado, bróker
  simulado, motor común, backtest, diario SQLite, bucle 24/7, revisión nocturna, política
  versionada, comandos. 57 tests.
- Probado con datos reales de Binance: `doctor`, `once`, `run` (en Docker), `backtest`,
  `review`, `status`, `policy`, `health`.
- Docker Compose: el contenedor queda «healthy», procesa velas reales, guarda el estado y
  se reinicia tras una caída del proceso.

## Hecho pero solo con simulación

- **Adaptador de Claude Haiku**: probado con un cliente falso. Nunca ha llamado a la API
  real; falta una clave.
- **Alertas de Telegram**: probadas con un servidor falso.

## No hecho

- Órdenes reales (testnet y producción).
- Capa de investigación: 5-10 candidatos con tesis, catalizadores y escenario bajista.
- Régimen de mercado con datos externos.
- Backtest con Haiku sobre datos posteriores a junio de 2026.

## Decisiones y su motivo

| Decisión | Motivo |
|---|---|
| Solo simulación; `mode = "live"` falla | Dinero real exige aprobación explícita y claves |
| Un único `Engine` para vivo y backtest | Evita que el backtest pruebe otro código |
| El `risk_state` del modelo solo endurece | Ningún límite duro delegado en un modelo |
| Salidas antes de llamar al REFLEX | Un modelo lento no puede retrasar un stop |
| Partida en probabilidad sin ventaja | Sin evidencia, el tamaño debe ser cero |
| 50 señales y dos errores típicos | Con 30 y uno, el backtest abrió operaciones sobre ruido |
| Equilibrio descontando costes | A 1 hora los costes son ~25 % del riesgo de cada operación |
| Sin reentrada en la vela de la salida | Un test destapó compras justo tras saltar el stop |
| Se mantiene 1 hora | En 4 h y 1 día tampoco hubo ventaja; elegir por resultado sería sobreajuste |
| Datos vía `data-api.binance.vision` aquí | `api.binance.com` devuelve 451 desde el entorno de desarrollo |
| ccxt respeta el proxy del sistema | Por defecto lo ignora y falla tras un proxy |

## Hallazgos

- Backtest 2022-10-11 → 2026-11-10, 1 h, 6 tickers, reglas: agente −7,4 % (caída −7,5 %),
  BTC +296,1 % (caída −53,7 %). 54 operaciones, 26 % con ganancia.
- La confianza de las reglas no tiene poder predictivo: Brier calibrado 0,2347 frente a
  0,2335 de la tasa media. Acierto ~37 % en todos los tramos; equilibrio tras costes 40 %.
- Las operaciones se concentraron entre 2023-T4 y 2024-T3: la tasa de acierto subió
  durante el tramo alcista, superó el filtro estadístico y luego revirtió. La calibración
  acumulada es sensible a cambios de régimen.
- Fallos corregidos gracias a los tests: RSI de 100 con precio plano; reentrada inmediata
  tras un stop.

## Siguiente

1. Dejar el agente en simulación en el PC del usuario y revisar la calibración en 2-4
   semanas.
2. Con clave de Anthropic: activar Haiku y comparar su calibración con la de las reglas.
3. Decidir si merece la pena la capa de investigación antes de saber si el REFLEX aporta.
4. Calibración con ventana móvil, para no arrastrar regímenes pasados.
