# ParcInfo — Compilation Exécutable Portable 📦

Créez un exécutable autonome (Windows/macOS) sans installer Python.

**Durée** : ~5-10 min | **Taille finale** : 25-40 MB

---

## 🎯 Vue d'ensemble

```
Source (parc_info/)
    ↓
pyinstaller parcinfo.spec
    ↓
dist/ParcInfo.exe (Windows) ou dist/ParcInfo.app (macOS)
    ↓
Utilisateur final : double-clic → navigateur s'ouvre auto
```

**Donnees** : stockées à côté de l'exécutable, **pas dedans**
```
C:\Program Files\ParcInfo\
├── ParcInfo.exe          ← executable (25-40 MB)
├── parc_info.db          ← données (creées 1er run)
├── uploads/              ← documents joints
└── oui.txt               ← fabricants réseau (optionnel)
```

---

## 📋 Pré-requis (Une Seule Fois)

### 1. Installer PyInstaller et Dépendances

```bash
# Naviguer au projet
cd parc_info

# Installer build tools
pip install pyinstaller pillow pystray

# (Ou manuellement)
pip install pyinstaller>=5.0
pip install pillow>=9.0      # Image pour tray icon
pip install pystray>=0.15    # System tray (optionnel)
```

### 2. Vérifier Configuration

```bash
# Vérifier parcinfo.spec existe
ls parcinfo.spec

# Voir contenu spec
cat parcinfo.spec
```

### Important : Compilation Cross-Plateforme

⚠️ **PyInstaller DOIT être lancé sur la plateforme cible** :
- Pour produire `.exe` (Windows) → compiler sur Windows
- Pour produire `.app` (macOS) → compiler sur macOS

Raison : respect des APIs système, code loader.

---

## 🪟 Windows → ParcInfo.exe

### Step 1 : Préparation

Ouvrir **Command Prompt** (ou PowerShell) **EN ADMINISTRATEUR** :

```cmd
# Naviguer au projet
cd C:\Chemin\vers\parc_info

# Vérifier venv activé (optionnel mais recommandé)
venv\Scripts\activate
```

### Step 2 : Compilation

```cmd
# Compiler avec spec
pyinstaller parcinfo.spec

# Vous verrez :
# 101 INFO: PyInstaller: 5.x.x
# ...
# 1234 INFO: UPX is not available.
# ...
# 5678 INFO: Appending archive to EXE
```

⏱️ **Durée** : 1-3 minutes (première fois peut être plus lent)

### Step 3 : Résultat

```
dist\
├── ParcInfo.exe          ← L'executable (25-40 MB)
├── ParcInfo/             ← Dossier support (temporaire)
└── ...

build\                    ← Artifacts (ignoré)
```

### Step 4 : Tester

Double-clic sur `dist\ParcInfo.exe` :
- ✅ Pas de console Windows (lancé en arrière-plan)
- ✅ Navigateur s'ouvre auto → http://127.0.0.1:PORT
- ✅ BD `parc_info.db` créée à côté de l'exe
- ✅ Attendre 3-5s première utilisation (init DB)

### Icône (déjà active)

`parcinfo.spec` référence déjà `icon='static/icon.ico'` (256×256px) — rien à
décommenter. Pour changer l'icône, remplacer le fichier `static/icon.ico`
puis recompiler :
```cmd
pyinstaller parcinfo.spec
```

---

## 🍎 macOS → ParcInfo.app

### Step 1 : Préparation

Ouvrir **Terminal** :

```bash
# Naviguer
cd /Chemin/vers/parc_info

# Venv (si utilisé)
source venv/bin/activate
```

### Step 2 : Compilation

```bash
# Compiler
pyinstaller parcinfo.spec

# Voir progress...
```

⏱️ **Durée** : 1-3 minutes

### Step 3 : Résultat

```
dist/
├── ParcInfo.app          ← L'application macOS
│   └── Contents/
│       ├── MacOS/
│       │   └── ParcInfo  ← Executable
│       ├── Resources/
│       │   ├── templates/
│       │   └── static/
│       └── Info.plist    ← Metadata
└── ...
```

### Step 4 : Installation

**Option A : Depuis Finder**
```bash
# Glisser ParcInfo.app dans /Applications
cp -r dist/ParcInfo.app /Applications/
```

**Option B : Depuis Terminal**
```bash
cp -r dist/ParcInfo.app /Applications/
```

### Step 5 : Lancer

1. Ouvrir **Applications** (Cmd+Shift+A)
2. Double-clic sur **ParcInfo.app**

**Si macOS bloque** ("ParcInfo.app cannot be opened...") :

**Méthode 1** : Clic droit → Ouvrir
```
Clic droit sur ParcInfo.app
→ "Ouvrir" (dans contexte)
→ "Ouvrir quand même"
```

**Méthode 2** : Terminal (supprimer quarantaine)
```bash
xattr -cr /Applications/ParcInfo.app
open /Applications/ParcInfo.app
```

**Méthode 3** : Signer ad hoc, seulement si la méthode 2 ne suffit pas.
Constaté en usage réel : sur certaines versions de macOS, une signature ad
hoc est REFUSÉE alors qu'un bundle non signé (quarantaine levée) est
accepté — signer peut donc *durcir* le blocage au lieu de le lever. Ne
jamais utiliser `--deep` (resigne récursivement Python.framework et les
bibliothèques embarquées par PyInstaller — cas connu de signature invalide
sur un bundle complexe comme celui-ci) :
```bash
codesign --force --sign - dist/ParcInfo.app
```
Voir `update_checker.py::_debloquer_gatekeeper_macos` pour le mécanisme
automatisé équivalent (méthode 2, puis 3 seulement si nécessaire, avec
vérification `spctl` à chaque étape).

### Icône macOS (optionnelle, désactivée par défaut)

`parcinfo.spec` porte `icon=None,  # 'static/icon.icns' pour custom icon` —
contrairement à Windows, aucune icône macOS custom n'est active par défaut.
Pour en ajouter une :
1. Créer icône : `static/icon.icns` (1024×1024, format ICNS)
2. Remplacer `icon=None` par `icon='static/icon.icns'` dans `parcinfo.spec`
   (bloc `BUNDLE`, section macOS)
3. Recompiler

---

## 💾 Gestion Données Persistantes

### Où Sont Stockées les Données ?

**À côté du binaire**, pas embarquées :

```
Windows:
  C:\Users\User\Downloads\ParcInfo.exe
  C:\Users\User\Downloads\parc_info.db       ← BD
  C:\Users\User\Downloads\uploads/           ← Documents
  C:\Users\User\Downloads\oui.txt            ← Fabricants

macOS:
  /Applications/ParcInfo.app
  ~/Downloads/parc_info.db                   ← BD (ou ~/.parc_info/)
  ~/Downloads/uploads/
  ~/Downloads/oui.txt

Linux (si compilé):
  ./ParcInfo
  ./parc_info.db
  ./uploads/
```

### Créées Au Premier Lancement

```
run 1 : ParcInfo.exe → Init DB (auto)
                    → Génère secret.key
                    → Crée uploads/
                    → Ouvre http://127.0.0.1:5000

Données persiste après → ParcInfo.exe mis à jour
```

### Sauvegarde

**Avant mise à jour** :
```bash
# Windows
copy parc_info.db parc_info.db.backup
xcopy uploads\* uploads.backup\ /E

# macOS/Linux
cp parc_info.db parc_info.db.backup
cp -r uploads uploads.backup
```

**Restauration** :
```bash
# Windows
copy parc_info.db.backup parc_info.db

# macOS/Linux
cp parc_info.db.backup parc_info.db
```

---

## 🔧 Configuration PyInstaller (parcinfo.spec)

Fichier clé : `parcinfo.spec`

### Ce Qui Est Embarqué

```python
datas = [
    ('templates', 'templates'),     # Templates Jinja2
    ('static',    'static'),        # JS, CSS, images
]
if os.path.exists('oui.txt'):
    datas.append(('oui.txt', '.'))  # Fabricants (optionnel)
```

### Ce Qui Est Caché

```python
hiddenimports = [
    'flask', 'jinja2', 'werkzeug',  # Framework
    'sqlite3', 'hashlib', 'secrets', # Libs critiques
    'threading', 'socket', 'subprocess',
    # ... (30+ modules)
]

excludes = [
    'tkinter', 'test', 'setuptools', # Libs non-nécessaires
    'doctest', 'pdb', 'unittest',
    # ...
]
```

### Console

```python
console=False,          # ← Pas de fenêtre console (Windows)
disable_windowed_traceback=False,  # ← Logs en fichier
```

### Optimisations

```python
upx=False,              # ← Compression désactivée : réduit les faux positifs
                        #   antivirus (signalés en usage réel, voir CHANGELOG)
noarchive=False,        # ← Archive zip interne
```

**Pour modifier** : éditer `parcinfo.spec`, puis recompiler. Ne pas réactiver
UPX sans en avoir mesuré l'effet sur les faux positifs antivirus — c'est
précisément pour ça qu'il a été désactivé.

---

## 🧭 Fabricants Réseau (OUI Database)

### Pourquoi ?

Scan réseau détecte MAC addresses. Pour identifier fabricant :
```
MAC: 00:50:F2:xx:xx:xx → Microsoft
MAC: 08:00:27:xx:xx:xx → Oracle VirtualBox
```

### Setup

```bash
# Télécharger (60k fabricants, ~5 MB)
python download_oui.py
# → Crée oui.txt

# Copier à côté du binaire

# Windows
move oui.txt C:\Program Files\ParcInfo\

# macOS
cp oui.txt ~/Downloads/  (si ParcInfo.exe là)
# ou
cp oui.txt /Applications/ParcInfo.app/Contents/MacOS/
```

### Vérification

Dans app Web :
- Scan réseau → Découvrir appareils
- Devrait voir fabricants (ex. "Apple Inc.", "Intel Corp.")
- Si absent → oui.txt pas trouvé

---

## 🚨 Dépannage Compilation

| Problème | Cause | Solution |
|----------|-------|----------|
| **"No such file: parcinfo.spec"** | File non trouvé | `cd parc_info/`, vérifier chemin |
| **"PermissionError: ... UPX"** | UPX pas dispo | Ignorer (compression optionnelle) |
| **"ModuleNotFoundError: flask"** | Dépendances absentes | `pip install -r requirements.txt` |
| **Build lent** | Antivirus scanne binaires | Exclure dossier `dist/`, `build/` |
| **Exe lance mais crash** | Fichiers manquants | Vérifier templates/, static/ existent |
| **Antivirus bloque exe** | Faux positif PyInstaller | Ajouter exception antivirus |
| **macOS : "command not found: codesign"** | Xcode absent | `xcode-select --install` |

---

## 🎁 Distribution

### Paquetage Utilisateur Final

Le pipeline de release (`.github/workflows/build-release.yml` +
`build-macos-intel.yml`) publie l'exécutable Windows nu et **deux archives
macOS séparées** — pas de binaire universel :

```
ParcInfo-Windows.exe                # exe unique, pas de zip

ParcInfo-macOS-ARM.zip              # Apple Silicon (M1+), natif
└── ParcInfo.app

ParcInfo-macOS-Intel.zip            # Intel, croisé sous Rosetta en CI
└── ParcInfo.app
```

### Hébergement

- **Releases GitHub** : https://github.com/Darkmind64/parc-info/releases
  (publication automatique par CI sur chaque tag `vX.Y.Z`, `SHA256SUMS.txt`
  généré et vérifié par la mise à jour in-app avant tout remplacement)
- **Docker Hub** : `docker pull darkmind64/parcinfo` (voir DOCKERHUB_SETUP.md)

### Mise à Jour

En développement, un simple tag suffit à déclencher la publication :
```bash
git tag -a vX.Y.Z -m "..."
git push origin vX.Y.Z
```
Côté utilisateur final, voir README.md § Mises à Jour — la bannière in-app
gère le téléchargement, la vérification d'empreinte et le remplacement
(BD/uploads/clé de chiffrement toujours conservés).

---

## 📊 Spécifications Finales

### Taille & Performance

| Métrique | Valeur |
|----------|--------|
| Taille exe | 25-40 MB |
| Temps démarrage | 2-5s |
| Mémoire RAM (idle) | 100-150 MB |
| Mémoire RAM (scan) | 200-300 MB |
| BD init (vide) | < 1 MB |

### Compatibilité

**Windows**
- Windows 10+
- x64 (64-bit)
- Pas admin requis (sauf scan réseau)

**macOS**
- macOS 10.13+ (High Sierra)
- Intel x64 et Apple Silicon, en deux builds natifs séparés (pas un binaire universel — voir § Distribution)
- Authentification requise pour ARP

**Linux** (si compilé)
- Ubuntu 18.04+, Debian 10+, Fedora 30+
- x64
- Root requis pour ARP

---

## ✅ Pre-Release Checklist

- [ ] Compiler sur Windows → `.exe` fonctionne
- [ ] Compiler sur macOS → `.app` fonctionne
- [ ] Login marche (user par défaut)
- [ ] Créer appareil → BD sauvegarde
- [ ] Upload fichier → `uploads/` créé
- [ ] Scan réseau → ports détectés
- [ ] Upgrade version → BD persiste
- [ ] Antivirus test → pas faux positif
- [ ] README inclus dans ZIP/DMG
- [ ] Version taggée Git

---

**ParcInfo Build Guide** — mis à jour 2026-08-19 (v2.18.15)
