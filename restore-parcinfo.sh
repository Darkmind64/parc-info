#!/bin/bash

################################################################################
# ParcInfo v2.5.0 — Docker Restore Script
################################################################################
#
# USAGE (sur le NAS via SSH):
#   ./restore-parcinfo.sh parcinfo-backup-20260423-150000.tar.gz
#   ./restore-parcinfo.sh /chemin/vers/backup.tar.gz
#
# RESTAURE :
#   Base de données : data/parc_info.db
#   Documents : uploads/
#   Configuration : config/
#
# ⚠️  ATTENTION:
#   - Arrête le conteneur pendant la restauration
#   - Écrase les données existantes (pas de rollback auto)
#   - Conserve backup des données actuelles (.bak)
#
# RECOMMANDATION :
#   - Vérifier que backup.tar.gz valide avant restauration
#   - Faire backup des données actuelles d'abord
#
################################################################################

set -e  # Exit on error

# ============================================================================
# Configuration
# ============================================================================

# Chemin base Docker sur NAS
DOCKER_BASE="/volume1/docker/parcinfo"

# Argument 1 : chemin backup
BACKUP_FILE="${1}"

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

log "🔍 Validation pré-restauration..."

# Argument requis
if [ -z "$BACKUP_FILE" ]; then
    error_exit "Usage: $0 <chemin/vers/backup.tar.gz>"
fi

# Vérifier que backup existe
if [ ! -f "$BACKUP_FILE" ]; then
    error_exit "Backup introuvable: $BACKUP_FILE"
fi

# Vérifier que c'est un fichier tar.gz valide
if ! tar -tzf "$BACKUP_FILE" >/dev/null 2>&1; then
    error_exit "Archive corrompue ou invalide: $BACKUP_FILE"
fi

# Vérifier que dossier Docker existe
if [ ! -d "$DOCKER_BASE" ]; then
    error_exit "Dossier Docker introuvable: $DOCKER_BASE"
fi

log "✅ Validations réussies"
log ""
log "⚠️  ATTENTION:"
log "   - Conteneur sera arrêté"
log "   - Données actuelles seront remplacées"
log "   - Backup des données actuelles créé (.bak)"
log ""

# Demander confirmation (si interactive)
if [ -t 0 ]; then
    read -p "Continuer la restauration? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log "❌ Restauration annulée"
        exit 1
    fi
fi

# ============================================================================
# Arrêter conteneur
# ============================================================================

log "⏹️  Arrêt du conteneur..."
if docker ps | grep -q parcinfo-app; then
    docker stop parcinfo-app || error_exit "Impossible arrêter conteneur"
    sleep 2  # Attendre arrêt propre
else
    log "   (conteneur déjà arrêté)"
fi

# ============================================================================
# Backup des données actuelles
# ============================================================================

log "💾 Backup des données actuelles (.bak)..."
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
CURRENT_BACKUP="${DOCKER_BASE}/current-data-${TIMESTAMP}.bak"

if [ -d "${DOCKER_BASE}/data" ] || [ -d "${DOCKER_BASE}/uploads" ] || [ -d "${DOCKER_BASE}/config" ]; then
    tar -czf "$CURRENT_BACKUP" \
        -C "$DOCKER_BASE" \
        data/ uploads/ config/ \
        2>/dev/null || {
        log "⚠️  Impossible créer backup courant (non critique)"
    }
    log "   Sauvegardé: $CURRENT_BACKUP"
else
    log "   (aucune donnée courante à sauvegarder)"
fi

# ============================================================================
# Restaurer données
# ============================================================================

log "📥 Restauration des données..."

# Extraire backup
tar -xzf "$BACKUP_FILE" -C "$DOCKER_BASE" || error_exit "Échec extraction"

log "✅ Données restaurées"

# ============================================================================
# Vérifier intégrité
# ============================================================================

log "🔍 Vérification intégrité..."

# Vérifier que BD existe et est valide
if [ ! -f "${DOCKER_BASE}/data/parc_info.db" ]; then
    error_exit "Base de données non trouvée après restauration!"
fi

# Vérifier que DB est valide (SQLite pragma)
if ! sqlite3 "${DOCKER_BASE}/data/parc_info.db" "PRAGMA integrity_check;" >/dev/null 2>&1; then
    error_exit "Base de données invalide ou corrompue!"
fi

log "✅ BD intègre"

# ============================================================================
# Redémarrer conteneur
# ============================================================================

log "▶️  Redémarrage du conteneur..."
docker start parcinfo-app || error_exit "Impossible redémarrer conteneur"
sleep 3  # Attendre démarrage

# Vérifier que conteneur est actif
if ! docker ps | grep -q parcinfo-app; then
    error_exit "Conteneur ne démarre pas - vérifier logs: docker logs parcinfo-app"
fi

log "✅ Conteneur redémarré"

# ============================================================================
# Résumé
# ============================================================================

BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
log ""
log "✨ Restauration réussie!"
log ""
log "📊 Résumé:"
log "   Backup restauré: $BACKUP_FILE"
log "   Taille: $BACKUP_SIZE"
log "   Donnée courante sauvegardée: $CURRENT_BACKUP"
log "   Conteneur: actif et prêt"
log ""
log "✅ ParcInfo est maintenant restauré"
log ""
log "📝 Prochaines étapes:"
log "   - Accéder: http://<NAS-IP>:8000"
log "   - Vérifier que toutes données sont présentes"
log "   - Valider formulaires et imports"
log "   - Archiver backup.tar.gz en sécurité"
log ""

# Vérifier logs pour erreurs
log "Vérification des logs du conteneur..."
if docker logs --tail 20 parcinfo-app | grep -i error >/dev/null; then
    log "⚠️  Erreurs détectées - consulter: docker logs parcinfo-app"
else
    log "✅ Pas d'erreur détectée"
fi

exit 0
