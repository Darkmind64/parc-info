#!/bin/bash
# ParcInfo Docker Entrypoint
# Optimisé pour Synology DS1522+ et autres NAS

set -e

echo "=========================================="
echo "  🐳 ParcInfo Docker Entrypoint"
echo "=========================================="

# Variables d'environnement par défaut
export FLASK_APP=${FLASK_APP:-app.py}
export FLASK_DEBUG=${FLASK_DEBUG:-0}
export RUNNING_IN_DOCKER=1
export DISABLE_TURSO_SYNC=${DISABLE_TURSO_SYNC:-1}

# Afficher la configuration
echo "📋 Configuration:"
echo "   Flask App: $FLASK_APP"
echo "   Debug: $FLASK_DEBUG"
echo "   Python: $(python --version 2>&1)"

# Créer les répertoires nécessaires
mkdir -p /data/uploads /data/backups /app/logs

# S'assurer que les permissions sont correctes
chmod -R 755 /data /app/logs 2>/dev/null || true

# Initialiser la base de données si elle n'existe pas
if [ ! -f "/data/parc_info.db" ]; then
    echo "📦 Initialisation de la base de données..."
    python -c "from app import init_db; init_db()" || true
fi

# Vérifier Gunicorn
if python -c "import gunicorn" 2>/dev/null; then
    echo "✅ Gunicorn disponible (recommandé pour Synology)"
else
    echo "⚠️  Gunicorn non trouvé - installation en cours..."
    pip install --no-cache-dir gunicorn>=21.2.0 || echo "⚠️  Installation Gunicorn échouée"
fi

echo "=========================================="
echo "  🚀 Démarrage du serveur ParcInfo"
echo "=========================================="

# Lancer l'application
exec python app.py
