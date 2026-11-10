"""Alertas por Telegram cuando el bot compra o vende.

Se activan definiendo dos variables de entorno (nunca se guardan en el repositorio):
    TELEGRAM_BOT_TOKEN   token que da @BotFather al crear tu bot
    TELEGRAM_CHAT_ID     identificador de tu chat con ese bot
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)


def telegram_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def format_cycle(summary: dict) -> str | None:
    """Texto de la alerta, o None si en el ciclo no hubo operaciones."""
    if not summary["buys"] and not summary["sells"]:
        return None
    lines = ["Bot de inversión (simulación)"]
    for t in summary["sells"]:
        lines.append(f"VENTA {t['symbol']}: {t['pnl']:+.2f} $ ({t['pnl_pct']:+.1f} %). {t['reason']}")
    for t in summary["buys"]:
        lines.append(f"COMPRA {t['symbol']}: {t['amount']:.2f} $ a {t['price']:.2f}. {t['reason']}")
    lines.append(f"Valor total de la cartera: {summary['equity']:.2f} $")
    return "\n".join(lines)


def notify_cycle(summary: dict, opener=urllib.request.urlopen) -> bool:
    """Envía la alerta si hay operaciones y Telegram está configurado. Nunca lanza excepciones."""
    text = format_cycle(summary)
    if text is None or not telegram_configured():
        return False
    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener(request, timeout=15) as response:
            return 200 <= response.status < 300
    except Exception as exc:  # una alerta fallida no debe parar el bot
        log.warning("No se pudo enviar la alerta de Telegram: %s", type(exc).__name__)
        return False
