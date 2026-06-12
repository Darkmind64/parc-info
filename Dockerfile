FROM python:3.11-slim

LABEL maintainer="Darkmind64"
LABEL description="ParcInfo - Gestion de parc informatique multi-clients"

ENV PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py \
    FLASK_DEBUG=0 \
    RUNNING_IN_DOCKER=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data/uploads /data/backups /app/logs

VOLUME ["/data"]

EXPOSE 3456

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3456', timeout=5)"

CMD ["python", "app.py"]
