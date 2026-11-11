"""Avisos al usuario: siempre por consola y, si está configurado, por Telegram.

Variables de entorno (nunca en el repositorio): TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID."""
from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger("agent")


def telegram_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def notify(text: str, opener=urllib.request.urlopen) -> bool:
    """Envía el aviso. Devuelve True si llegó a Telegram. Nunca lanza excepciones."""
    log.warning(text)
    if not telegram_configured():
        return False
    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text[:3900]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener(request, timeout=15) as response:
            return 200 <= response.status < 300
    except Exception as exc:  # un aviso fallido no debe parar el agente (y el token no se registra)
        log.error("No se pudo enviar el aviso de Telegram: %s", type(exc).__name__)
        return False
