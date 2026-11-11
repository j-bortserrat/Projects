# Agente de trading de criptomonedas (simulación)

Agente que vigila el mercado 24/7 y opera una **cartera ficticia** en Binance spot. No
envía órdenes reales ni necesita claves del exchange: solo lee datos públicos.

> Herramienta experimental. No es asesoramiento financiero. Hoy **no hay evidencia de que
> gane dinero** (ver «Qué resultados da»). Está construido para sobrevivir y medir, no
> para parecer rentable.

## Cómo funciona, en dos capas

- **El REFLEX opina.** En cada vela cerrada recibe un resumen numérico del mercado y
  devuelve un juicio tipado: régimen, dirección, confianza, calidad y estado de riesgo.
  Puede ser Claude Haiku (con clave de Anthropic) o una referencia de reglas en código.
- **El código decide.** Umbrales, tamaño de cada operación, órdenes y límites de riesgo
  están en código. El modelo no puede cambiarlos ni saltárselos.

Una compra solo ocurre si se cumplen todas estas condiciones:

1. Datos fiables: sin huecos, sin retraso y con el reloj sincronizado.
2. El REFLEX responde a tiempo. Si tarda o falla, esa vela se salta.
3. Dirección de compra, calidad ≥ 2 y confianza > 0,80.
4. Estado de riesgo «seguro», tanto para el código como para el REFLEX.
5. **Evidencia medida**: el tamaño sale de la tasa de acierto real de señales pasadas con
   esa misma confianza. Sin al menos 50 señales que demuestren ventaja tras comisiones,
   el tamaño es cero.
6. Límites duros: 2 % de la cartera en riesgo por operación, 5 % de pérdida diaria,
   3 posiciones abiertas y un interruptor de emergencia al 15 % de caída.

Las salidas (stop, objetivo, tiempo máximo) las ejecuta el código antes de consultar al
REFLEX, así que funcionan aunque el modelo esté caído.

## Qué resultados da

Backtest con velas reales de 1 hora de Binance, del 11-10-2022 al 10-11-2026, seis
tickers, con comisiones (0,10 %) y deslizamiento (0,05 %), usando la referencia de reglas:

| | Resultado total | Peor caída |
|---|---|---|
| Agente (reglas) | **−7,4 %** | −7,5 % |
| Comprar BTC y no tocarlo | **+296,1 %** | −53,7 % |

Lectura honesta:

- **No supera a mantener BTC**, ni de lejos. Hizo 54 operaciones en 4 años, con el 0,2 %
  del capital invertido de media, y las que hizo perdieron.
- **La confianza de las reglas no predice nada.** Acierta en torno al 37 % diga 0,6 o
  0,9, cuando haría falta un 40 % para cubrir costes. Medido con el índice de Brier, no
  mejora a suponer siempre la tasa media.
- **Lo que sí funcionó fue la contención**: sin evidencia de ventaja, el agente casi no
  arriesgó, y la pérdida quedó acotada.
- Con velas de 4 horas y de 1 día la conclusión fue la misma.
- **Con Claude Haiku no hay resultado todavía.** Un modelo de lenguaje solo se puede
  evaluar con datos posteriores a su entrenamiento (junio de 2026) y con una clave de
  Anthropic. La única forma de saber si aporta algo es dejarlo en simulación y mirar su
  calibración semanas después.

## Arrancarlo en tu PC

Necesitas Python 3.11 o superior y conexión a Internet. No hace falta cuenta en Binance.

**Windows (PowerShell)**

```powershell
git clone https://github.com/j-bortserrat/Projects.git
cd Projects
git checkout claude/app-idea-life-money-6b9t1s
cd crypto-agent
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m agent doctor
```

**macOS / Linux**

```bash
git clone https://github.com/j-bortserrat/Projects.git
cd Projects && git checkout claude/app-idea-life-money-6b9t1s && cd crypto-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m agent doctor
```

`doctor` comprueba la conexión, el reloj y qué REFLEX se va a usar. Si dice «Todo listo»:

```bash
python -m agent backtest --days 365   # repite la prueba histórica (tarda 1-2 minutos)
python -m agent run                   # bucle 24/7 en simulación; Ctrl+C para parar
```

El agente procesa una vela por hora mientras el programa esté abierto. Todo el estado se
guarda en `data/agent.db`: si cierras el PC, al volver a ejecutar `run` continúa donde
estaba y revisa los stops de las velas que se perdió.

| Comando | Qué hace |
|---|---|
| `python -m agent status` | Cartera, posiciones y últimas órdenes |
| `python -m agent once` | Procesa una sola vela y muestra cada decisión con su motivo |
| `python -m agent review` | Revisión nocturna bajo demanda (informe en `data/reports/`) |
| `python -m agent policy list` | Versiones de la política de umbrales |
| `python -m agent reset-kill --confirm` | Rearma el interruptor de emergencia |

Para cambiar tickers, saldo ficticio o límites, copia `config.example.toml` como
`config.toml` y edítalo. Si `doctor` no puede conectar con Binance, descomenta en ese
fichero la línea `public_api_url`.

### Usar Claude Haiku como REFLEX (opcional)

1. Crea una clave en la consola de Anthropic.
2. Defínela como variable de entorno antes de arrancar:
   - PowerShell: `$env:ANTHROPIC_API_KEY = "tu-clave"`
   - macOS/Linux: `export ANTHROPIC_API_KEY="tu-clave"`
3. `python -m agent doctor` mostrará `REFLEX: haiku:claude-haiku-5-5` y una estimación
   del coste.

Con 6 tickers y velas de 1 hora son unas 4.300 llamadas al mes. A los precios de Haiku 5.5
de noviembre de 2026, la estimación es de **alrededor de 1,5 $ al mes**; es una
estimación sin medir, así que comprueba el consumo real en tu cuenta.

Al cambiar de REFLEX la calibración empieza de cero: los aciertos de las reglas no valen
para el modelo, así que durante las primeras semanas medirá sin arriesgar.

### Alertas por Telegram (opcional)

Define `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` como variables de entorno. Recibirás cada
compra, venta, incidencia y la revisión nocturna. El envío real no se ha probado, solo
con un servidor simulado.

### Con Docker (opcional, para dejarlo siempre encendido)

```bash
cp config.example.toml config.toml
cp .env.example .env        # y rellena las claves que vayas a usar
docker compose up -d --build
docker compose logs -f
```

El contenedor se reinicia solo tras un fallo o un reinicio del equipo, y Docker lo marca
como no saludable si deja de procesar velas.

## Revisión nocturna y cambios de política

Cada noche (UTC) el agente escribe un informe con las órdenes del día, las velas
perdidas, la latencia del REFLEX y la calibración. Si la evidencia lo justifica, guarda
una **política candidata** como versión nueva. Nunca la activa solo:

```bash
python -m agent policy list
python -m agent policy promote 2 --confirm    # activarla (requiere reiniciar el agente)
python -m agent policy rollback --confirm     # volver a la anterior
```

## Qué no hace todavía

- **Dinero real.** No hay conexión de órdenes con ningún exchange. Arrancar con
  `mode = "live"` da un error a propósito.
- **Investigación de candidatos.** Opera una lista de monedas grandes y líquidas sin
  tesis propia. La búsqueda de 5-10 oportunidades con fundamentales, catalizadores y
  escenario bajista no está hecha.
- **Régimen de mercado con datos externos** (dominancia, liquidez de stablecoins,
  financiación, interés abierto, macro). Hoy solo usa precio y volumen del exchange.
- **Ventas en corto y perpetuos.** Solo spot: compra o está fuera.
- **Stops dentro de la vela.** Se revisan una vez por vela con su máximo y mínimo; un
  exchange real usaría órdenes stop permanentes.
- **Festivos y mantenimiento del exchange**: se tratan como datos caducados y no se opera.

## Tests

```bash
python -m pytest -q
```

Diseño y decisiones: `docs/DESIGN.md`. Estado del trabajo: `PROGRESS.md`.
