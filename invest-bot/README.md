# Bot de inversión en ETFs (simulación)

Bot que gestiona una **cartera ficticia de ETFs** siguiendo reglas fijas y la muestra en un
panel web en español. No se conecta a ningún bróker ni mueve dinero real.

> Herramienta educativa. No es asesoramiento financiero. Un buen resultado simulado o
> histórico no garantiza resultados reales, y cualquier estrategia puede perder dinero.

## Qué hace

Tiene tres estrategias; se elige en **Ajustes**.

### Cartera fija (por defecto)

Trata los ETFs como una cartera de fondos: tú eliges los pesos y el bot los mantiene.

1. Invierte el capital según los pesos objetivo. Por defecto, 60 % en acciones de todo el
   mundo (`VT`) y 40 % en bonos (`BND`).
2. Una vez al mes comprueba los pesos. Si alguno se ha desviado más de 5 puntos, vende lo
   que sobra y compra lo que falta. Si no, no toca nada.
3. Si configuras una **aportación mensual**, ese dinero va siempre a lo que está por debajo
   de su peso, sin vender nada.

No usa stops ni señales: su ventaja es operar muy poco.

### Rotación mensual

Cada mes ordena una lista de ETFs por momentum (media de la rentabilidad a 3, 6 y 12
meses) y se queda con los 5 más fuertes que cumplan al menos el 60 % de las 10 condiciones
y coticen sobre su media de 200 días. Entre rotaciones solo actúa un stop-loss del 25 %.

### Lista de condiciones

Compra un ETF cuando cumple al menos el 90 % de las 10 condiciones técnicas de
`bot/rules.py` y lo vende si baja del 30 %, o por stop-loss.

## Qué resultados cabe esperar

Backtest con datos reales de Yahoo Finance, de enero de 2008 a noviembre de 2026 (incluye
las caídas de 2008, 2020 y 2022), con un 0,1 % de comisión por operación:

| Estrategia | Rentabilidad anual | Peor caída | Años en negativo | Ventas |
|---|---|---|---|---|
| Cartera fija 60/40, S&P 500 y bonos del Tesoro (`SPY`, `IEF`) | +8,2 % | −30,6 % | 3 de 19 | 11 |
| Cartera fija 60/40 global (`VT`, `BND`), desde junio de 2008 | +6,5 % | −33,5 % | 5 de 19 | 10 |
| Rotación mensual, 23 ETFs | +7,2 % | −25,2 % | 5 de 19 | 332 |
| Lista de condiciones, 23 ETFs | +2,2 % | −23,7 % | 7 de 19 | 66 |
| Comprar `SPY` y no tocarlo | +11,2 % | −51,9 % | — | 0 |

Con ETFs europeos (UCITS, en euros), donde solo hay historia conjunta desde noviembre de
2017:

| Estrategia | Rentabilidad anual | Peor caída |
|---|---|---|
| Cartera fija 60/40 global (`IUSQ.DE`, `EUNA.DE`) | +7,0 % | −22,9 % |
| Rotación mensual, 14 ETFs UCITS | +8,4 % | −15,8 % |
| Comprar MSCI World (`EUNL.DE`) y no tocarlo | +12,5 % | −33,6 % |

Cómo leerlo:

- **Ninguna estrategia ha batido en rentabilidad a comprar el índice de acciones y
  esperar.** Lo que ofrecen las carteras mixtas es caer bastante menos por el camino.
- **La cartera fija se eligió por defecto por ser la más simple y estable**, no por ser la
  que más gana: rindió parecido a la rotación con treinta veces menos operaciones.
- La 60/40 de EE. UU. sale mejor que la global porque en este periodo la bolsa
  estadounidense fue la que más subió. Eso se sabe ahora; no había forma de saberlo en 2008.
- La lista de condiciones es la peor de las tres con ETFs.
- La simulación es optimista: sin deslizamiento, con participaciones fraccionadas y sin
  impuestos. Las ventas de la rotación tributarían cada año; la cartera fija casi no vende.

Ejemplo con aportaciones: 1.000 $ iniciales más 100 $ al mes en la 60/40 de `SPY` e `IEF`
desde 2008 son 23.600 $ aportados, que en noviembre de 2026 valdrían unos 63.700 $.

Todo esto se puede repetir en la pestaña **Backtest**, que compara las tres estrategias
con el índice, año a año.

## Cómo arrancarlo

Necesitas Python 3.10 o superior.

**Windows (PowerShell)**

```powershell
cd invest-bot
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

**macOS / Linux**

```bash
cd invest-bot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Abre <http://127.0.0.1:5000>. El bot usa datos reales de Yahoo Finance (gratis) y analiza
solo cada 15 minutos mientras el programa esté abierto. Solo lo he ejecutado en Linux.

### ETFs que puedes comprar desde España

Los ETFs estadounidenses (`VT`, `SPY`…) normalmente no se venden a particulares en la UE.
En **Ajustes → Cargar un ejemplo** hay una cartera equivalente con ETFs UCITS de Xetra, en
euros. El bot reconoce la bolsa por el sufijo del ticker (`.DE`, `.L`, `.AS`, `.PA`, `.MI`,
`.MC`…) y solo opera en su horario.

Si mezclas ETFs de EE. UU. y de Europa, el bot solo opera cuando todas las bolsas están
abiertas a la vez (por la tarde, hora española).

### Variables de entorno

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
4. Arranca con las variables `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` definidas.

Recibirás un mensaje con cada compra y venta. El envío real no se ha probado, solo con un
servidor simulado. No guardes el token en el repositorio.

## Detalles de funcionamiento

- **Precios diarios.** Las señales se calculan con velas diarias completas; la vela del
  día en curso solo aporta el precio actual.
- **Horario de bolsa.** Fuera de horario actualiza precios y espera. No conoce los
  festivos.
- **El backtest no ve el futuro:** decide con el cierre de un día y ejecuta a la apertura
  del siguiente. Una cartera fija empieza cuando existe el más joven de sus ETFs.
- **Aportaciones y rentabilidad.** El porcentaje de rentabilidad se calcula por
  participación, así que el dinero aportado no cuenta como ganancia.
- **Moneda.** El ajuste de moneda solo cambia el símbolo que se muestra; no convierte
  divisas. Usa ETFs de una sola moneda en cada cartera.
- **Gastos.** Si indicas tus gastos mensuales, el panel muestra la ganancia neta.

## Tests

```bash
python -m pytest -q
```

## Estructura

```
app.py              Servidor web y API
static/index.html   Panel
bot/config.py       Ajustes por defecto, ejemplos y validación
bot/engine.py       Decisiones de compra y venta (compartidas con el backtest)
bot/backtest.py     Simulación sobre datos históricos
bot/portfolio.py    Cartera ficticia
bot/markets.py      Horarios de cada bolsa
bot/rules.py        Las 10 condiciones técnicas
bot/indicators.py   Medias, RSI, MACD, momentum…
bot/data.py         Datos de Yahoo Finance (y datos de prueba)
bot/notify.py       Alertas de Telegram
bot/store.py        Base de datos SQLite
```
