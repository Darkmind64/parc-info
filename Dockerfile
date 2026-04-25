# Dockerfile - ParcInfo v2.5.0
# Optimisé pour Synology NAS

FROM python:3.11-slim

# Metadata
LABEL maintainer="Claude AI"
LABEL version="2.5.0"
LABEL description="ParcInfo - Gestion de parc informatique"

# Variables d'environnement
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=app.py
ENV FLASK_ENV=production

# Créer répertoire de travail
WORKDIR /app

# Copier requirements
COPY requirements.txt .

# Installer dépendances
RUN pip install --no-cache-dir -r requirements.txt

# Copier l'application
COPY . .

# Créer répertoires pour données persistantes
RUN mkdir -p /data/uploads /data/backups /app/logs

# Permissions
RUN chmod -R 755 /app

# Volume pour données persistantes
VOLUME ["/data"]

# Port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5000', timeout=5)"

# Commande de démarrage
CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "5000"]
