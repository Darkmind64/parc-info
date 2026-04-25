# Guide de Fusion: uploads_sync + Widget System

Ce guide explique comment fusionner les changements uploads_sync vers `master` en utilisant les scripts d'automatisation.

## 📋 Contenu

- 3 scripts de fusion automatisés
- Instructions pour différents systèmes d'exploitation
- Troubleshooting et solutions

---

## 🚀 Utilisation Rapide

### Windows

**Option 1 : Double-clic (le plus simple)**
```
Double-cliquer sur: merge_uploads_sync.cmd
```

**Option 2 : Terminal PowerShell**
```powershell
# Autoriser l'exécution des scripts (une seule fois)
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# Lancer le script
.\merge_uploads_sync.ps1
```

**Option 3 : Terminal CMD (Invite de commandes)**
```cmd
merge_uploads_sync.cmd
```

### Linux / macOS

```bash
# Rendre le script exécutable (une seule fois)
chmod +x merge_uploads_sync.sh

# Lancer le script
./merge_uploads_sync.sh
```

---

## 📝 Ce que le script fait

1. ✅ **Vérifie** que vous êtes dans un repo git
2. ✅ **Détecte** les branches disponibles
3. ✅ **Identifie** la branche source (uploads_sync)
4. ✅ **Liste** les commits à merger
5. ✅ **Demande confirmation** avant de continuer
6. ✅ **Checkout** la branche master
7. ✅ **Merge** les changements avec un message détaillé
8. ✅ **Affiche** un résumé et les prochaines étapes

---

## 🔍 Prérequis

- Git configuré et en PATH
- Terminal/PowerShell avec permissions suffisantes
- Pas de changements non-committés (ou confirmation demandée)
- La branche `master` accessible

---

## 📊 Commits à merger

```
e73021f feat: Add uploads synchronization between local and Turso
a682d33 Fix widget rendering: ensure all default widgets...
6d189c1 Ensure newly added widgets are always rendered...
... et 15+ autres commits du widget system
```

**Total:** ~20+ commits intégrant:
- ✨ Dashboard widget system avec dynamic rendering
- ✨ Uploads synchronization (local ↔ Turso)
- 🔧 Migration script pour uploads existants

---

## ⚠️ Situations Spéciales

### Situation 1: Master utilisé par un worktree

**Symptôme:**
```
fatal: 'master' is already used by worktree
```

**Solution:**
1. Sortir du worktree:
```bash
cd "E:\Claude Code\parc_info - docker"  # Répertoire principal
```
2. Relancer le script

### Situation 2: Conflits de merge

**Symptôme:**
```
CONFLICT (content): Merge conflict in app.py
```

**Solution:**
1. Ouvrir le fichier en conflit
2. Résoudre les conflits (sections `<<<<< ... ===== ... >>>>`)
3. Continuer le merge:
```bash
git add .
git commit
```

### Situation 3: Changements non-committés

**Symptôme:**
```
⚠ Changements non-committés détectés
```

**Solution (Option A):** Continuer quand même (le script vous demandera confirmation)
**Solution (Option B):** Stasher les changements d'abord:
```bash
git stash
# Relancer le script
./merge_uploads_sync.sh
git stash pop
```

---

## ✅ Vérification Post-Merge

Après la fusion, vérifier:

```bash
# 1. Vérifier qu'on est sur master
git status
# → "On branch master"

# 2. Vérifier les derniers commits
git log --oneline -5
# → e73021f feat: Add uploads synchronization...

# 3. Vérifier les fichiers nouveaux
git log --name-status e73021f -1
# → A  uploads_sync.py
# → A  migrate_uploads.py
# → M  app.py
```

---

## 🔄 Prochaines étapes après merge

### Étape 1: Migrer les uploads existants
```bash
python migrate_uploads.py
```
Cela va remplir les colonnes BLOB pour tous les uploads existants.

**Output attendu:**
```
✓ documents_appareils: 8 documents migrés, 0 erreurs
✓ documents_contrats: 0 documents
✓ documents_peripheriques: 0 documents
Migration complète: 8 documents migrés
```

### Étape 2: Tester l'application
```bash
python app.py
```

**Vérifier dans les logs:**
```
[INFO] Starting uploads sync thread (interval=60s)
[INFO] Uploads sync thread started
✓ Running on http://127.0.0.1:5000
```

### Étape 3: Uploader un fichier de test

1. Accéder à http://localhost:5000
2. Créer/modifier un appareil
3. Uploader un fichier
4. Vérifier dans la DB:
```bash
sqlite3 parc_info.db "SELECT id, nom, sync_status FROM documents_appareils ORDER BY date_upload DESC LIMIT 1;"
```

**Output attendu:**
```
1|test.pdf|local
```

### Étape 4: Vérifier la synchronisation

Attendre 60 secondes et vérifier dans les logs que le sync s'est fait:
```
[INFO] Pushed X documents to Turso (documents_appareils)
```

### Étape 5: Configurer Turso (optionnel)

Si vous voulez synchroniser vers Turso serverless:

1. Voir CLAUDE.md section "Turso Configuration"
2. Configurer les variables:
   - `db_type = 'turso'`
   - `turso_url = 'libsql://...'`
   - `turso_token = '...'`

---

## 🐛 Troubleshooting

### Le script refuse de fusionner

```
✗ Erreur lors du merge
```

**Vérifications:**
1. Vérifier qu'il n'y a pas de conflits:
```bash
git status
```

2. Si conflits, les résoudre:
```bash
# Ouvrir les fichiers en conflit
# Chercher les marqueurs: <<<<< ===== >>>>>
# Choisir la version correcte
# git add .
# git commit
```

3. Si trop complexe, annuler et recommencer:
```bash
git merge --abort
```

### PowerShell refuse d'exécuter le script

```
File cannot be loaded because running scripts is disabled
```

**Solution:**
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Puis relancer le script.

### Git ne trouve pas la branche

```
fatal: 'master' is not a valid ref
```

**Solution:**
1. Vérifier les branches:
```bash
git branch -a
```

2. Si `master` n'existe pas, créer depuis la branche courante:
```bash
git branch -m master
```

---

## 📞 Support

Si vous rencontrez d'autres problèmes:

1. **Logs détaillés:**
```bash
# Lancer le merge en mode verbose
git merge --no-ff -v claude/vigorous-ellis-c95bbe
```

2. **Vérifier l'état du repo:**
```bash
git status
git log --oneline -10
git branch -a
```

3. **Consulter CLAUDE.md** pour le contexte complet du projet

---

## 📄 Scripts disponibles

| Script | Système | Usage |
|--------|---------|-------|
| `merge_uploads_sync.cmd` | Windows (CMD) | Double-clic ou `merge_uploads_sync.cmd` |
| `merge_uploads_sync.ps1` | Windows (PowerShell) | `.\merge_uploads_sync.ps1` |
| `merge_uploads_sync.sh` | Linux/macOS | `./merge_uploads_sync.sh` |

---

**Créé avec 🤖 Claude Code**
