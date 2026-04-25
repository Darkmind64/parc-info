#!/bin/bash

################################################################################
# ParcInfo v2.5.0 — Docker Backup Script
################################################################################
#
# USAGE (sur le NAS via SSH):
#   ./backup-parcinfo.sh
#   ./backup-parcinfo.sh /chemin/vers/backup  # Custom destination
#
# CRÉÉ :
#   parcinfo-backup-YYYYMMDD-HHMMSS.tar.gz dans /volume1/docker/parcinfo/
#   Contient : parc_info.db, uploads/, config/
#
# TAILLE : ~50MB à 500MB selon données
#
# RECOMMANDATION :
#   Exécuter via cron (Task Scheduler Synology) chaque nuit
#
################################################################################

set -e  # Exit on error

# ============================================================================
# Configuration
# ============================================================================

# Chemin base Docker sur NAS
DOCKER_BASE="/volume1/docker/parcinfo"

# Destination backup (argument 1, ou défaut)
BACKUP_DEST="${1:-$DOCKER_BASE}"

# Timestamp
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_NAME="parcinfo-backup-${TIMESTAMP}.tar.gz"
BACKUP_FILE="${BACKUP_DEST}/${BACKUP_NAME}"

# Dossiers à sauvegarder
DB_PATH="${DOCKER_BASE}/data"
UPLOADS_PATH="${DOCKER_BASE}/uploads"
CONFIG_PATH="${DOCKER_BASE}/config"

# ============================================================================
# Fonctions
# ============================================================================

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

error_exit() {
    log "❌ ERREUR: $1"
    exit 1
}

# ============================================================================
# Validations
# ============================================================================

log "🔍 Validation pré-backup..."

# Vérifier que dossier Docker existe
if [ ! -d "$DOCKER_BASE" ]; then
    error_exit "Dossier Docker introuvable: $DOCKER_BASE"
fi

# Vérifier que BD existe
if [ ! -f "${DB_PATH}/parc_info.db" ]; then
    error_exit "Base de données introuvable: ${DB_PATH}/parc_info.db"
fi

# Vérifier destination accessible
if [ ! -d "$BACKUP_DEST" ]; then
    log "📁 Création dossier destination: $BACKUP_DEST"
    mkdir -p "$BACKUP_DEST" || error_exit "Impossible créer $BACKUP_DEST"
fi

# Vérifier espace disque (minimum 1GB)
AVAILABLE=$(df "$BACKUP_DEST" | awk 'NR==2 {print $4}')
if [ "$AVAILABLE" -lt 1048576 ]; then  # 1GB en KB
    error_exit "Espace disque insuffisant (< 1GB disponible)"
fi

log "✅ Validations réussies"

# ============================================================================
# Arrêter conteneur (optionnel, mais recommandé pour cohérence)
# ============================================================================

log "⏹️  Arrêt temporaire du conteneur..."
if docker ps | grep -q parcinfo-app; then
    docker stop parcinfo-app || error_exit "Impossible arrêter conteneur"
    CONTAINER_WAS_RUNNING=1
else
    CONTAINER_WAS_RUNNING=0
fi

# ============================================================================
# Créer Backup
# ============================================================================

log "📦 Création backup: $BACKUP_NAME"
log "   Data: ${DB_PATH}/parc_info.db"
log "   Uploads: ${UPLOADS_PATH}/"
log "   Config: ${CONFIG_PATH}/"

tar -czf "$BACKUP_FILE" \
    -C "$DOCKER_BASE" \
    data/ \
    uploads/ \
    config/ \
    2>/dev/null || error_exit "Échec création tar.gz"

# ============================================================================
# Redémarrer conteneur
# ============================================================================

if [ "$CONTAINER_WAS_RUNNING" -eq 1 ]; then
    log "▶️  Redémarrage conteneur..."
    docker start parcinfo-app || error_exit "Impossible redémarrer conteneur"
fi

# ============================================================================
# Statistiques
# ============================================================================

BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
BACKUP_SIZE_KB=$(stat -f%z "$BACKUP_FILE" 2>/dev/null || stat -c%s "$BACKUP_FILE")

log "✅ Backup réussi!"
log ""
log "📊 Résumé:"
log "   Fichier: $BACKUP_FILE"
log "   Taille: $BACKUP_SIZE"
log "   Date: $TIMESTAMP"
log ""
log "💾 Conseils:"
log "   - Télécharger sur PC: scp admin@<NAS>:$BACKUP_FILE ~/Backups/"
log "   - Archiver copies mensuelles"
log "   - Tester restauration régulièrement"
log ""
log "✨ Backup terminé!"

# ============================================================================
# Nettoyage anciens backups (optionnel)
# ============================================================================

log "🧹 Gestion anciens backups (7 jours)..."

# Supprimer backups > 7 jours
find "$BACKUP_DEST" -name "parcinfo-backup-*.tar.gz" -mtime +7 -delete

REMAINING=$(find "$BACKUP_DEST" -name "parcinfo-backup-*.tar.gz" | wc -l)
log "   $REMAINING backup(s) conservé(s)"

exit 0
