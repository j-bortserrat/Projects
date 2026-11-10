# Bot de inversión (simulación)

Bot que vigila una lista de acciones y ETFs y gestiona una **cartera ficticia** siguiendo
reglas fijas. No se conecta a ningún bróker ni mueve dinero real. Incluye un panel web en
español con la cartera, el ranking de señales, el historial de operaciones, un backtest
comparativo y los ajustes.

> Herramienta educativa. No es asesoramiento financiero. Un buen resultado simulado o
> histórico no garantiza resultados reales, y cualquier estrategia puede perder dinero.

## Qué hace

Tiene dos estrategias; se elige en **Ajustes**.

### Rotación mensual (por defecto)

1. La primera sesión de cada mes ordena los tickers por **momentum**: la media de su
   rentabilidad a 3, 6 y 12 meses.
2. Se queda con los **5 más fuertes** que además cumplan al menos el **60 % de las 10
   condiciones** y coticen sobre su media de 200 días. Si hay menos de 5, el resto queda
   en efectivo.
3. Vende los que han salido de ese grupo y compra los nuevos, un 20 % de la cartera cada
   uno (las ganancias se reinvierten).
4. Entre rotaciones solo actúa el stop-loss (−25 % por posición).

### Lista de condiciones (la versión original)

Compra un ticker cuando cumple al menos el **90 %** de las condiciones y lo vende si baja
del 30 %, o por stop-loss.

### Las 10 condiciones (`bot/rules.py`)

1. Precio por encima de la media de 200 días
2. Media de 50 días por encima de la de 200
3. Precio por encima de la media de 50 días
4. RSI entre 45 y 68
5. MACD por encima de su señal
6. Impulso acelerando (histograma MACD mayor que hace 3 días)
7. Volumen sostenido (media de 20 días ≥ 90 % de la de 60)
8. A menos de un 10 % del máximo de 52 semanas
9. Rentabilidad positiva en 3 meses
10. Volatilidad anualizada por debajo del 50 %

## Qué resultados cabe esperar

Backtest con datos reales de Yahoo Finance, noviembre de 2016 a noviembre de 2026, con
los ajustes por defecto y un 0,1 % de comisión por operación:

| Lista de tickers | Estrategia | Rentabilidad anual | Peor caída |
|---|---|---|---|
| Solo ETFs (20) | Rotación mensual | +11,0 % | −19,5 % |
| Solo ETFs (20) | Lista de condiciones | +6,0 % | −28,8 % |
| La lista por defecto (25, con acciones) | Rotación mensual | +30,4 % | −21,7 % |
| La lista por defecto (25, con acciones) | Lista de condiciones | +13,6 % | −25,9 % |
| — | Comprar SPY y no tocarlo | +15,0 % | −33,7 % |

Cómo leerlo:

- **La cifra creíble es la de «solo ETFs».** La lista por defecto incluye acciones (NVDA,
  TSLA, META…) elegidas sabiendo que subieron mucho; su +30 % está inflado por ese sesgo
  y no es una expectativa realista. Solo en 2020 sumó un +156 %.
- **Sin ese sesgo, la rotación no ha batido al índice en rentabilidad** (+11 % frente a
  +15 % anual). Lo que sí ha hecho es caer bastante menos en los malos momentos.
- En 2025 y 2026 la rotación ha ido por detrás del índice también con la lista por defecto.
- Los ajustes por defecto se eligieron comparando variantes sobre estos mismos datos. Se
  eligió lo más estable, no lo que más ganó, pero siguen estando ajustados al pasado.
- La simulación es optimista: sin deslizamiento, con acciones fraccionadas y sin impuestos.

Puedes repetir estas pruebas en la pestaña **Backtest**, que compara tu estrategia, la
otra y comprar el índice, año a año.

## Cómo arrancarlo

Necesitas Python 3.10 o superior.

```bash
cd invest-bot
python -m venv .venv
source .venv/bin/activate        # en Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Abre <http://127.0.0.1:5000>. El bot usa datos reales de Yahoo Finance (gratis) y analiza
solo cada 15 minutos mientras el programa esté abierto.

| Variable | Por defecto | Qué hace |
|---|---|---|
| `AUTO_CYCLE_MINUTES` | `15` | Cada cuántos minutos analiza solo (`0` = solo manual) |
| `DATA_PROVIDER` | `yahoo` | `synthetic` usa precios inventados, para probar sin Internet |
| `BOT_DB` | `data/bot.db` | Dónde se guarda la cartera ficticia |
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Dónde escucha el panel |
| `PANEL_PASSWORD` | — | Contraseña del panel |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | Alertas de compras y ventas |

### Verlo desde el móvil (servidor)

En un servidor, arráncalo con contraseña. Sin ella el programa se niega a abrirse fuera
del propio equipo:

```bash
HOST=0.0.0.0 PANEL_PASSWORD='una-contraseña-larga' python app.py
```

El navegador pedirá usuario y contraseña; el usuario puede ser cualquiera. La contraseña
viaja sin cifrar salvo que pongas HTTPS delante (por ejemplo con Caddy o un túnel).

### Alertas por Telegram

1. En Telegram, habla con **@BotFather**, envía `/newbot` y guarda el token que te da.
2. Escribe cualquier mensaje a tu bot nuevo.
3. Abre `https://api.telegram.org/bot<TOKEN>/getUpdates` y copia el número de `chat.id`.
4. Arranca con `TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python app.py`.

Recibirás un mensaje con cada compra y venta. No guardes el token en el repositorio.

## Detalles de funcionamiento

- **Precios diarios.** Las condiciones se calculan con velas diarias completas; la vela
  del día en curso solo aporta el precio actual. Consultar más a menudo hace que el
  stop-loss reaccione antes, pero no cambia qué se compra.
- **Solo opera con la bolsa de EE. UU. abierta** (lunes a viernes, 9:30–16:00 de Nueva
  York; no conoce los festivos). Fuera de horario actualiza las señales y espera.
- **El backtest no ve el futuro:** decide con el cierre de un día y ejecuta a la apertura
  del siguiente.
- **Gastos:** si indicas tus gastos mensuales en Ajustes, el panel muestra la ganancia
  neta después de descontarlos.
- Los importes se muestran en dólares; no se convierte divisa.
- Si resides en la UE, muchos ETFs estadounidenses (SPY, QQQ…) no se pueden comprar como
  minorista. Para simular con equivalentes europeos añade en Ajustes tickers UCITS con el
  sufijo de Yahoo, por ejemplo `VWCE.DE` o `CSPX.L`.

## Tests

```bash
python -m pytest -q
```

## Estructura

```
app.py              Servidor web y API
static/index.html   Panel
bot/rules.py        Las 10 condiciones
bot/engine.py       Decisiones de compra y venta (compartidas con el backtest)
bot/backtest.py     Simulación sobre datos históricos
bot/portfolio.py    Cartera ficticia
bot/data.py         Datos de Yahoo Finance (y datos de prueba)
bot/indicators.py   Medias, RSI, MACD, momentum…
bot/notify.py       Alertas de Telegram
bot/store.py        Base de datos SQLite
bot/config.py       Ajustes por defecto y su validación
```
