#!/bin/bash
#
# merge_uploads_sync.sh — Automatise la fusion des changements uploads_sync vers master
#
# Usage:
#   chmod +x merge_uploads_sync.sh
#   ./merge_uploads_sync.sh
#
# Cela va:
# 1. Vérifier que nous sommes en environnement git
# 2. Sortir du worktree si nécessaire
# 3. Checkout master
# 4. Merger la branche uploads_sync
# 5. Afficher un résumé
#

set -e  # Exit on error

# Couleurs pour l'output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  Merge Automation: uploads_sync + widget system${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
echo ""

# Vérifier qu'on est dans un repo git
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo -e "${RED}✗ Erreur: Pas dans un repo git${NC}"
    exit 1
fi

REPO_ROOT=$(git rev-parse --show-toplevel)
echo -e "${GREEN}✓${NC} Repo git détecté: ${REPO_ROOT}"
echo ""

# Vérifier les branches disponibles
echo -e "${YELLOW}📋 Branches disponibles:${NC}"
git branch | sed 's/^/  /'
echo ""

# Déterminer la branche source (uploads_sync)
SOURCE_BRANCH=$(git branch | grep "uploads_sync\|vigorous-ellis" | head -1 | sed 's/^[* ]*//')

if [ -z "$SOURCE_BRANCH" ]; then
    echo -e "${RED}✗ Erreur: Branche uploads_sync non trouvée${NC}"
    echo "   Branches disponibles:"
    git branch
    exit 1
fi

echo -e "${YELLOW}📌 Branche source:${NC} ${BLUE}${SOURCE_BRANCH}${NC}"

# Vérifier que master existe
if ! git rev-parse --verify master >/dev/null 2>&1; then
    echo -e "${RED}✗ Erreur: Branche master non trouvée${NC}"
    exit 1
fi

echo -e "${YELLOW}📌 Branche destination:${NC} ${BLUE}master${NC}"
echo ""

# Vérifier les changements non-committés
if ! git diff-index --quiet HEAD --; then
    echo -e "${RED}⚠ Attention: Changements non-committés détectés${NC}"
    echo ""
    git status
    echo ""
    read -p "Continuer quand même? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}Opération annulée${NC}"
        exit 0
    fi
fi

echo ""
echo -e "${YELLOW}📊 Commits à merger:${NC}"
git log --oneline master..${SOURCE_BRANCH} | sed 's/^/  /'
echo ""

# Demander confirmation
echo -e "${YELLOW}❓ Confirmer le merge?${NC}"
read -p "Continuer? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}Opération annulée${NC}"
    exit 0
fi

echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  Début du merge...${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
echo ""

# Vérifier si nous sommes dans un worktree
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" != "master" ]; then
    echo -e "${YELLOW}⚠ Actuellement sur la branche:${NC} ${BLUE}${CURRENT_BRANCH}${NC}"
    echo ""

    # Chercher si master est utilisé par un autre worktree
    if git worktree list | grep -q master; then
        echo -e "${YELLOW}📂 master est utilisé par un worktree${NC}"
        echo "   Worktrees disponibles:"
        git worktree list | sed 's/^/   /'
        echo ""
        echo -e "${YELLOW}Action requise:${NC}"
        echo "   1. cd dans le répertoire principal (non-worktree) avec master"
        echo "   2. Relancer ce script"
        exit 1
    fi

    # Essayer de checkout master
    echo -e "${BLUE}→${NC} Checkout de master..."
    if ! git checkout master; then
        echo -e "${RED}✗ Erreur lors du checkout de master${NC}"
        echo "   (Probablement parce que master est utilisé par un autre worktree)"
        exit 1
    fi
fi

# Faire le merge
echo -e "${BLUE}→${NC} Merge de ${SOURCE_BRANCH} vers master..."
MERGE_MSG="feat: Add uploads synchronization and dashboard widget system

- Implement bi-directional uploads sync (local ↔ Turso)
- Store file content as BLOB for centralized management
- Automatic sync thread every 60 seconds
- Dashboard widget system with dynamic rendering
- New migration script for existing uploads
- All routes support BLOB fallback to local files

Test results:
✓ Application starts without errors
✓ Sync thread launches automatically
✓ Migration script processes existing uploads
✓ Database columns properly added
✓ All download/preview routes work

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"

if git merge --no-ff --message="$MERGE_MSG" ${SOURCE_BRANCH}; then
    echo -e "${GREEN}✓ Merge réussi${NC}"
else
    echo -e "${RED}✗ Erreur lors du merge${NC}"
    echo ""
    echo -e "${YELLOW}État actuel:${NC}"
    git status
    echo ""
    echo -e "${YELLOW}Action requise:${NC}"
    echo "   1. Résoudre les conflits si nécessaire"
    echo "   2. git add ."
    echo "   3. git commit"
    exit 1
fi

echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  ✓ Merge terminé avec succès!${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
echo ""

# Afficher un résumé
echo -e "${YELLOW}📊 Résumé:${NC}"
echo -e "  ${GREEN}✓${NC} Branche source: ${BLUE}${SOURCE_BRANCH}${NC}"
echo -e "  ${GREEN}✓${NC} Branche destination: ${BLUE}master${NC}"
echo -e "  ${GREEN}✓${NC} Changements mergés avec succès"
echo ""

# Afficher les derniers commits
echo -e "${YELLOW}📝 Derniers commits:${NC}"
git log --oneline -5 | sed 's/^/  /'
echo ""

# Afficher les next steps
echo -e "${YELLOW}📋 Prochaines étapes:${NC}"
echo -e "  1. Vérifier les changements: ${BLUE}git log -p master..origin/master${NC} (si remote configuré)"
echo -e "  2. Push vers le remote: ${BLUE}git push origin master${NC}"
echo -e "  3. Lancer la migration: ${BLUE}python migrate_uploads.py${NC}"
echo -e "  4. Tester l'application: ${BLUE}python app.py${NC}"
echo ""

echo -e "${GREEN}✓ Script terminé avec succès!${NC}"
exit 0
