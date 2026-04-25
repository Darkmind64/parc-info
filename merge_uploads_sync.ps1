#
# merge_uploads_sync.ps1 — Automatise la fusion des changements uploads_sync vers master
#
# Usage:
#   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
#   .\merge_uploads_sync.ps1
#
# Cela va:
# 1. Vérifier que nous sommes en environnement git
# 2. Sortir du worktree si nécessaire
# 3. Checkout master
# 4. Merger la branche uploads_sync
# 5. Afficher un résumé
#

$ErrorActionPreference = "Stop"

# Couleurs pour l'output
function Write-Success {
    param([string]$Message)
    Write-Host "✓ $Message" -ForegroundColor Green
}

function Write-Error-Custom {
    param([string]$Message)
    Write-Host "✗ $Message" -ForegroundColor Red
}

function Write-Warning-Custom {
    param([string]$Message)
    Write-Host "⚠ $Message" -ForegroundColor Yellow
}

function Write-Info {
    param([string]$Message)
    Write-Host "→ $Message" -ForegroundColor Cyan
}

function Write-Header {
    param([string]$Message)
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Blue
    Write-Host "  $Message" -ForegroundColor Blue
    Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Blue
    Write-Host ""
}

# --- DÉBUT DU SCRIPT ---

Write-Header "Merge Automation: uploads_sync + widget system"

# Vérifier qu'on est dans un repo git
try {
    $null = git rev-parse --git-dir 2>$null
} catch {
    Write-Error-Custom "Pas dans un repo git"
    exit 1
}

$repoRoot = (git rev-parse --show-toplevel)
Write-Success "Repo git détecté: $repoRoot"
Write-Host ""

# Vérifier les branches disponibles
Write-Host "📋 Branches disponibles:" -ForegroundColor Yellow
git branch | ForEach-Object { Write-Host "  $_" }
Write-Host ""

# Déterminer la branche source
$branches = git branch | ForEach-Object { $_.Trim().Substring(2) }
$sourceBranch = $branches | Where-Object { $_ -like "*uploads_sync*" -or $_ -like "*vigorous-ellis*" } | Select-Object -First 1

if (-not $sourceBranch) {
    Write-Error-Custom "Branche uploads_sync non trouvée"
    Write-Host "Branches disponibles:"
    git branch
    exit 1
}

Write-Host "📌 Branche source: " -ForegroundColor Yellow -NoNewline
Write-Host "$sourceBranch" -ForegroundColor Cyan
Write-Host ""

# Vérifier que master existe
$masterExists = git rev-parse --verify master 2>$null
if (-not $masterExists) {
    Write-Error-Custom "Branche master non trouvée"
    exit 1
}

Write-Host "📌 Branche destination: " -ForegroundColor Yellow -NoNewline
Write-Host "master" -ForegroundColor Cyan
Write-Host ""

# Vérifier les changements non-committés
$hasChanges = git diff-index --quiet HEAD -- 2>$null; $hasChanges = -not $?
if ($hasChanges) {
    Write-Warning-Custom "Changements non-committés détectés"
    Write-Host ""
    git status
    Write-Host ""
    $response = Read-Host "Continuer quand même? (y/n)"
    if ($response -ne "y" -and $response -ne "Y") {
        Write-Host "Opération annulée" -ForegroundColor Yellow
        exit 0
    }
}

Write-Host ""
Write-Host "📊 Commits à merger:" -ForegroundColor Yellow
git log --oneline master..$sourceBranch | ForEach-Object { Write-Host "  $_" }
Write-Host ""

# Demander confirmation
Write-Host "❓ Confirmer le merge?" -ForegroundColor Yellow
$response = Read-Host "Continuer? (y/n)"
if ($response -ne "y" -and $response -ne "Y") {
    Write-Host "Opération annulée" -ForegroundColor Yellow
    exit 0
}

Write-Header "Début du merge..."

# Vérifier la branche actuelle
$currentBranch = git rev-parse --abbrev-ref HEAD

if ($currentBranch -ne "master") {
    Write-Host "⚠ Actuellement sur la branche: " -ForegroundColor Yellow -NoNewline
    Write-Host "$currentBranch" -ForegroundColor Cyan
    Write-Host ""

    # Chercher si master est utilisé par un autre worktree
    $worktrees = git worktree list
    if ($worktrees -like "*master*") {
        Write-Warning-Custom "master est utilisé par un worktree"
        Write-Host "Worktrees disponibles:"
        git worktree list | ForEach-Object { Write-Host "   $_" }
        Write-Host ""
        Write-Host "Action requise:" -ForegroundColor Yellow
        Write-Host "  1. cd dans le répertoire principal (non-worktree) avec master"
        Write-Host "  2. Relancer ce script"
        exit 1
    }

    # Essayer de checkout master
    Write-Info "Checkout de master..."
    try {
        git checkout master 2>&1 | Out-Null
    } catch {
        Write-Error-Custom "Erreur lors du checkout de master"
        Write-Host "(Probablement parce que master est utilisé par un autre worktree)"
        exit 1
    }
}

# Faire le merge
Write-Info "Merge de $sourceBranch vers master..."

$mergeMsg = @"
feat: Add uploads synchronization and dashboard widget system

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

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>
"@

try {
    git merge --no-ff --message=$mergeMsg $sourceBranch 2>&1 | Out-Null
    Write-Success "Merge réussi"
} catch {
    Write-Error-Custom "Erreur lors du merge"
    Write-Host ""
    Write-Host "État actuel:" -ForegroundColor Yellow
    git status
    Write-Host ""
    Write-Host "Action requise:" -ForegroundColor Yellow
    Write-Host "  1. Résoudre les conflits si nécessaire"
    Write-Host "  2. git add ."
    Write-Host "  3. git commit"
    exit 1
}

Write-Header "✓ Merge terminé avec succès!"

# Afficher un résumé
Write-Host "📊 Résumé:" -ForegroundColor Yellow
Write-Host "  ✓ Branche source: " -NoNewline
Write-Host "$sourceBranch" -ForegroundColor Cyan
Write-Host "  ✓ Branche destination: " -NoNewline
Write-Host "master" -ForegroundColor Cyan
Write-Host "  ✓ Changements mergés avec succès"
Write-Host ""

# Afficher les derniers commits
Write-Host "📝 Derniers commits:" -ForegroundColor Yellow
git log --oneline -5 | ForEach-Object { Write-Host "  $_" }
Write-Host ""

# Afficher les next steps
Write-Host "📋 Prochaines étapes:" -ForegroundColor Yellow
Write-Host "  1. Vérifier les changements: " -NoNewline
Write-Host "git log -p master..origin/master" -ForegroundColor Cyan
Write-Host "  2. Push vers le remote: " -NoNewline
Write-Host "git push origin master" -ForegroundColor Cyan
Write-Host "  3. Lancer la migration: " -NoNewline
Write-Host "python migrate_uploads.py" -ForegroundColor Cyan
Write-Host "  4. Tester l'application: " -NoNewline
Write-Host "python app.py" -ForegroundColor Cyan
Write-Host ""

Write-Success "Script terminé avec succès!"
exit 0
