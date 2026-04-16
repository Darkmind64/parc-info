# ─────────────────────────────────────────────────────────────────────────────
# ParcInfo — Dockerfile
# Compatible : Synology NAS (Container Manager / DSM 7.2+)
# Architectures : amd64, arm64
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Métadonnées
LABEL maintainer="ParcInfo"
LABEL description="ParcInfo — Gestion de parc informatique"
LABEL version="1.0"

# Variables d'environnement de build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Installer les dépendances système (scan réseau : ping, arp)
RUN apt-get update && apt-get install -y --no-install-recommends \
        iputils-ping \
        net-tools \
        arp-scan \
    && rm -rf /var/lib/apt/lists/*

# Installer les dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copier le code source (hors données persistantes — voir .dockerignore)
COPY . .

# Créer les dossiers de données persistantes dans le volume
RUN mkdir -p /data/uploads

# Port exposé (configurable via variable PORT)
EXPOSE 5000

# Healthcheck — vérifie que Flask répond
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/login')" \
    || exit 1

# Démarrage
CMD ["python", "app.py"]
