# ============================================================================
# ParcInfo v2.5.0 — PowerShell Backup Script (Windows)
# ============================================================================
#
# USAGE:
#   .\backup-parcinfo.ps1 -NasIP "192.168.1.100" -NasUser "admin"
#   .\backup-parcinfo.ps1 -NasIP "192.168.1.100" -NasUser "admin" -DestPath "C:\Backups"
#
# PARAMÈTRES:
#   -NasIP        : Adresse IP du NAS Synology
#   -NasUser      : Utilisateur admin du NAS (default: admin)
#   -DestPath     : Chemin destination sur PC (default: C:\ParcInfo-Backups)
#   -Compress     : Compresser backup supplémentaire (default: $true)
#
# CRÉÉ:
#   C:\ParcInfo-Backups\parcinfo-backup-YYYYMMDD-HHMMSS.tar.gz
#   C:\ParcInfo-Backups\parcinfo-backup-YYYYMMDD-HHMMSS.zip (si -Compress)
#
# RECOMMANDATION:
#   Planifier exécution via Task Scheduler Windows (quotidienne)
#
# ============================================================================

[CmdletBinding()]
Param(
    [Parameter(Mandatory=$true)]
    [string]$NasIP,

    [Parameter(Mandatory=$false)]
    [string]$NasUser = "admin",

    [Parameter(Mandatory=$false)]
    [string]$DestPath = "C:\ParcInfo-Backups",

    [Parameter(Mandatory=$false)]
    [bool]$Compress = $true
)

# ============================================================================
# Configuration
# ============================================================================

$ErrorActionPreference = "Stop"
$WarningPreference = "Continue"

# Timestamps
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dateStr = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Chemins
$NasBasePath = "/volume1/docker/parcinfo"
$BackupName = "parcinfo-backup-${timestamp}.tar.gz"
$BackupPath = "$DestPath\$BackupName"
$CompressedPath = "$DestPath\parcinfo-backup-${timestamp}.zip"

# ============================================================================
# Fonctions
# ============================================================================

function Write-Log {
    [CmdletBinding()]
    param(
        [string]$Message,
        [ValidateSet('Info','Warning','Error','Success')]
        [string]$Level = 'Info'
    )

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $color = @{
        'Info'    = 'Cyan'
        'Warning' = 'Yellow'
        'Error'   = 'Red'
        'Success' = 'Green'
    }

    Write-Host "[$timestamp] $Message" -ForegroundColor $color[$Level]
}

function Test-SSHConnection {
    [CmdletBinding()]
    param([string]$NasIP, [string]$NasUser)

    Write-Log "🔍 Test connexion SSH..." -Level Info

    try {
        $result = ssh -o "ConnectTimeout=5" -o "StrictHostKeyChecking=no" `
                       "$NasUser@$NasIP" "echo OK" 2>$null

        if ($result -eq "OK") {
            Write-Log "✅ Connexion SSH établie" -Level Success
            return $true
        }
    }
    catch {}

    Write-Log "❌ Impossible se connecter via SSH à $NasIP" -Level Error
    Write-Log "   Vérifier:" -Level Info
    Write-Log "   - IP NAS correcte" -Level Info
    Write-Log "   - SSH activé sur NAS" -Level Info
    Write-Log "   - Firewall NAS autorise SSH" -Level Info
    Write-Log "   - Clé SSH configurée (ssh-keygen, ssh-copy-id)" -Level Info
    return $false
}

function Get-NasBackup {
    [CmdletBinding()]
    param(
        [string]$NasIP,
        [string]$NasUser,
        [string]$RemotePath,
        [string]$LocalPath
    )

    Write-Log "📥 Téléchargement backup depuis NAS..." -Level Info
    Write-Log "   Source: $NasUser@$NasIP:$RemotePath" -Level Info
    Write-Log "   Destination: $LocalPath" -Level Info

    try {
        scp -P 22 -o "ConnectTimeout=10" `
            "$NasUser@$NasIP`:$RemotePath" `
            "$LocalPath" 2>$null

        if (Test-Path $LocalPath) {
            $size = (Get-Item $LocalPath).Length / 1MB
            Write-Log "✅ Téléchargement réussi ($([math]::Round($size, 2)) MB)" -Level Success
            return $true
        }
    }
    catch {}

    Write-Log "❌ Échec téléchargement" -Level Error
    return $false
}

# ============================================================================
# Validations
# ============================================================================

Write-Log "🔍 Validation pré-backup..." -Level Info

# Vérifier que destination existe
if (-not (Test-Path $DestPath)) {
    Write-Log "📁 Création dossier destination: $DestPath" -Level Info
    New-Item -ItemType Directory -Path $DestPath -Force | Out-Null
}

# Vérifier espace disque (minimum 2GB)
$drive = Split-Path -Path $DestPath -Qualifier
$driveInfo = Get-Volume -DriveLetter ($drive -replace ':','')
$freespace = $driveInfo.SizeRemaining / 1GB

if ($freespace -lt 2) {
    Write-Log "❌ Espace disque insuffisant (< 2GB disponible)" -Level Error
    exit 1
}

Write-Log "✅ Validations réussies" -Level Success

# ============================================================================
# Test connexion SSH
# ============================================================================

if (-not (Test-SSHConnection -NasIP $NasIP -NasUser $NasUser)) {
    Write-Log ""
    Write-Log "⚠️  Alternative sans SSH:" -Level Warning
    Write-Log "   Utiliser File Station pour copier manuellement:" -Level Info
    Write-Log "   /volume1/docker/parcinfo/*.tar.gz → PC" -Level Info
    exit 1
}

# ============================================================================
# Créer backup sur NAS
# ============================================================================

Write-Log ""
Write-Log "📦 Création backup sur NAS..." -Level Info

$remoteCmd = @"
#!/bin/bash
set -e
DOCKER_BASE="/volume1/docker/parcinfo"
BACKUP_NAME="$BackupName"
BACKUP_FILE="`$DOCKER_BASE/`$BACKUP_NAME"

# Arrêter conteneur
docker stop parcinfo-app 2>/dev/null || true

# Créer backup
tar -czf "`$BACKUP_FILE" \
    -C "`$DOCKER_BASE" \
    data/ uploads/ config/ 2>/dev/null

# Redémarrer
docker start parcinfo-app 2>/dev/null || true

echo "`$BACKUP_FILE"
"@

try {
    $result = ssh -o "ConnectTimeout=10" "$NasUser@$NasIP" $remoteCmd 2>$null
    $remoteBackupPath = $result.Trim()

    if ([string]::IsNullOrEmpty($remoteBackupPath)) {
        throw "Aucun chemin retourné"
    }

    Write-Log "✅ Backup créé sur NAS: $remoteBackupPath" -Level Success
}
catch {
    Write-Log "❌ Échec création backup: $_" -Level Error
    exit 1
}

# ============================================================================
# Télécharger backup
# ============================================================================

Write-Log ""
Write-Log "📥 Téléchargement backup sur PC..." -Level Info

if (-not (Get-NasBackup -NasIP $NasIP -NasUser $NasUser `
                        -RemotePath $remoteBackupPath -LocalPath $BackupPath)) {
    Write-Log "❌ Impossible télécharger le backup" -Level Error
    exit 1
}

# ============================================================================
# Compression supplémentaire (optionnel)
# ============================================================================

if ($Compress) {
    Write-Log ""
    Write-Log "🗜️  Compression supplémentaire..." -Level Info

    try {
        Compress-Archive -Path $BackupPath -DestinationPath $CompressedPath -Force
        $origSize = (Get-Item $BackupPath).Length / 1MB
        $compSize = (Get-Item $CompressedPath).Length / 1MB
        $ratio = [math]::Round(($compSize / $origSize) * 100, 1)
        Write-Log "✅ Archive ZIP créée: $ratio% de la taille d'origine" -Level Success
    }
    catch {
        Write-Log "⚠️  Compression échouée (non critique): $_" -Level Warning
    }
}

# ============================================================================
# Résumé
# ============================================================================

$backupSize = (Get-Item $BackupPath).Length / 1MB

Write-Log ""
Write-Log "✨ Backup réussi!" -Level Success
Write-Log ""
Write-Log "📊 Résumé:" -Level Info
Write-Log "   Fichier: $BackupPath" -Level Info
Write-Log "   Taille: $([math]::Round($backupSize, 2)) MB" -Level Info
Write-Log "   Date: $dateStr" -Level Info
if ($Compress) {
    $compSize = (Get-Item $CompressedPath).Length / 1MB
    Write-Log "   Archive ZIP: $CompressedPath" -Level Info
    Write-Log "   Taille ZIP: $([math]::Round($compSize, 2)) MB" -Level Info
}

Write-Log ""
Write-Log "💾 Conseils:" -Level Info
Write-Log "   - Archiver copies mensuelles en sécurité" -Level Info
Write-Log "   - Tester restauration régulièrement" -Level Info
Write-Log "   - Dupliquer backup sur disque externe" -Level Info

Write-Log ""
Write-Log "✅ Processus terminé!" -Level Success

exit 0
