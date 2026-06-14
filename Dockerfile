FROM python:3.11-slim

LABEL maintainer="Darkmind64"
LABEL description="ParcInfo - Gestion de parc informatique multi-clients"
LABEL version="2.6.6"

# Variables d'environnement
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_APP=app.py \
    FLASK_ENV=production \
    FLASK_DEBUG=0 \
    RUNNING_IN_DOCKER=1 \
    DATA_DIR=/data

WORKDIR /app

# Copier requirements et installer les dépendances
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir gunicorn>=21.2.0

# Copier le code de l'application
COPY . .

# Copier l'entrypoint et forcer les fins de ligne LF (évite les CRLF Windows)
COPY docker-entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Créer les répertoires de données
RUN mkdir -p /data/uploads /data/backups /app/logs && \
    chmod 755 /data /app/logs

# Volume pour les données persistantes
VOLUME ["/data"]

# Port d'écoute
EXPOSE 3456

# Healthcheck - Test si le serveur répond
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3456/', timeout=5)" || exit 1

# Lancer l'entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]
