FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
# Normalmente basta con `pip install`. El "secret" opcional solo hace falta detrás de un proxy
# que inspecciona HTTPS:  docker build --secret id=ca,src=/ruta/ca.crt .
RUN --mount=type=secret,id=ca,required=false \
    if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install --no-cache-dir -r requirements.txt
COPY agent ./agent
COPY tests ./tests
# Docker reinicia el contenedor si el agente deja de dar señales de vida.
HEALTHCHECK --interval=60s --timeout=15s --start-period=180s --retries=3 CMD ["python", "-m", "agent", "health"]
CMD ["python", "-m", "agent", "run"]
