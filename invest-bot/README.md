# Bot de inversión (simulación)

Bot que vigila una lista de acciones y ETFs, comprueba **10 condiciones** en cada uno y
**solo compra cuando se cumple al menos el 90 %** (9 de 10). Vende cuando salta alguna
condición de salida. Todo con **dinero ficticio**: no se conecta a ningún bróker ni
mueve dinero real.

Incluye un panel web (en español) con la cartera, las señales, el historial de
operaciones, un backtest y los ajustes.

> Herramienta educativa. No es asesoramiento financiero. Un buen resultado simulado
> o histórico no garantiza resultados reales.

## Cómo arrancarlo

Necesitas Python 3.10 o superior.

```bash
cd invest-bot
python -m venv .venv
source .venv/bin/activate        # en Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Abre <http://127.0.0.1:5000> en el navegador.

- Con `python app.py` usa **datos reales de Yahoo Finance** y analiza solo cada 30 minutos
  mientras el programa esté abierto.
- `DATA_PROVIDER=synthetic python app.py` usa precios inventados (solo para probar sin
  Internet; el panel lo avisa con un cartel amarillo).

| Variable | Por defecto | Qué hace |
|---|---|---|
| `DATA_PROVIDER` | `yahoo` | `yahoo` o `synthetic` |
| `AUTO_CYCLE_MINUTES` | `30` | Cada cuántos minutos analiza solo (`0` = solo manual) |
| `BOT_DB` | `data/bot.db` | Dónde se guarda la cartera ficticia |
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Dónde escucha el panel |

El panel no tiene contraseña: déjalo en `127.0.0.1` salvo que lo protejas tú.

## Cómo decide

**Compra** (importe fijo, 100 $ ficticios por defecto) si el ticker cumple al menos el
90 % de estas condiciones, definidas en `bot/rules.py`:

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

**Vende** toda la posición cuando ocurre lo primero de:

- Stop-loss: pierde un 8 % desde el precio de compra
- Trailing stop: cae un 12 % desde el máximo alcanzado tras la compra
- Las condiciones se deterioran: se cumple menos del 50 %
- Objetivo de beneficio (desactivado por defecto)

Un ticker vendido no se vuelve a comprar hasta el día siguiente. Todos los porcentajes,
el importe por compra, el capital inicial y la lista de tickers se cambian en la pestaña
**Ajustes**.

## Limitaciones que conviene conocer

- **Los precios son diarios.** El bot usa velas diarias de Yahoo; durante la sesión, la
  vela de hoy está incompleta. No sirve para operar en minutos.
- **La simulación es optimista:** compra y vende al último precio sin deslizamiento, con
  acciones fraccionadas y una comisión fija del 0,1 %.
- **El backtest tiene sesgo de supervivencia** (usa tickers que existen hoy) y las reglas
  se eligieron mirando el pasado.
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
bot/rules.py        Las 10 condiciones de compra
bot/engine.py       Decisiones de compra y venta
bot/portfolio.py    Cartera ficticia
bot/backtest.py     Simulación sobre datos históricos
bot/data.py         Datos de Yahoo Finance (y datos de prueba)
bot/indicators.py   Medias, RSI, MACD…
bot/store.py        Base de datos SQLite
```
