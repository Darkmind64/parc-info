# ParcInfo — Gestion de Parc Informatique 🖥️

Application web **Python/Flask** pour la gestion d'inventaire informatique avec **support multi-client**, **authentification sécurisée**, **scan réseau automatisé**, et **exécutable portable** (Windows/macOS).

**Dernière mise à jour** : 2026-04-07

---

## ✨ Fonctionnalités Principales

### Gestion d'Inventaire
- ✅ **Appareils** : PC, laptops, serveurs, imprimantes, switches, NAS, etc.
- ✅ **Périphériques** : écrans, claviers, souris, webcams, imprimantes, dongles, etc.
- ✅ **Contrats** : maintenance, support, SaaS, licences avec dates/montants
- ✅ **Services** : logiciels métier, antivirus, suites bureautiques, etc.
- ✅ **Utilisateurs finaux** : end-users avec affectation appareils
- ✅ **Identifiants** : credentials stockés chiffrés

### Parc & Configuration
- ✅ **Parc général** : site, réseau, baie de brassage, switches, serveurs, UPS
- ✅ **Scan réseau** : découverte auto via ping → ARP → scan ports TCP
- ✅ **Détection fabricants** : identification MAC avec base IEEE OUI
- ✅ **Plans de disposition** : éditeur visuel (optionnel)
- ✅ **Tableau de bord** : statistiques, warranties, contrats actifs

### Sécurité & Accès
- ✅ **Multi-client** : isolation stricte, partage d'accès granulaire
- ✅ **Authentification** : PBKDF2 + sessions 8h HttpOnly
- ✅ **Rate-limiting** : protection brute-force (10/5min)
- ✅ **CSRF** : tokens sur chaque modification
- ✅ **Audit trail** : historique complet des modifications
- ✅ **Rôles** : admin (tous) + user (client affecté)

### Uploads & Documents
- ✅ **Documents joints** : PDF, images, guides utilisateur
- ✅ **Rapports** : scans, certifications, garanties
- ✅ **Accès sécurisé** : stockage hors webroot

### Distribution
- ✅ **Exécutable portable** : .exe (Windows) / .app (macOS)
- ✅ **Zéro installation** : double-clic → navigateur s'ouvre
- ✅ **Données persistantes** : BD locale à côté du binaire
- ✅ **Mise à jour facile** : remplace l'exe, conserve les données

---

## 🚀 Démarrage Rapide

### Option 1 : Développement Local

```bash
# 1. Clone & setup
git clone <repo>
cd parc_info

# 2. Installer dépendances
pip install -r requirements.txt

# 3. Lancer
python app.py
# → http://localhost:5000
```

### Option 2 : Exécutable Portable

**Windows/macOS** : voir [BUILD.md](BUILD.md)
```bash
pyinstaller parcinfo.spec
# → dist/ParcInfo.exe (ou .app)
```

Double-clic → navigateur s'ouvre auto. BD créée première utilisation.

---

## 📋 Pré-requis

| Component | Version | Notes |
|-----------|---------|-------|
| **Python** | 3.8+ | 3.10+ recommandé |
| **pip** | N/A | Gestionnaire paquets |
| **Flask** | 3.0+ | Automatique via requirements.txt |
| **Werkzeug** | 3.0+ | Automatique |
| **SQLite** | 3.x | Inclus Python |
| **ping, arp** | Système | Optionnel (scan réseau) |

### Optionnel (pour distribution exécutable)
```bash
pip install pyinstaller pillow pystray
```

---

## 📥 Installation Détaillée

### Linux / macOS

```bash
# 1. Cloner
git clone https://github.com/your-org/parc_info.git
cd parc_info

# 2. Environnement virtuel (recommandé)
python3 -m venv venv
source venv/bin/activate

# 3. Dépendances
pip install -r requirements.txt

# 4. Lancer
python app.py
```

### Windows (Command Prompt Admin)

```cmd
# 1. Cloner
git clone https://github.com/your-org/parc_info.git
cd parc_info

# 2. Environnement virtuel
python -m venv venv
venv\Scripts\activate

# 3. Dépendances
pip install -r requirements.txt

# 4. Lancer
python app.py
```

### Accès Application

Ouvrir navigateur : **http://localhost:5000**

- Utilisateur par défaut : voir logs ou `app.py:init_db()`
- Clé secrète générée auto : `secret.key`
- BD créée auto : `parc_info.db`

---

## 🔍 Scan Réseau (Optionnel)

### Activer Détection MAC Complète

```bash
# Linux / macOS (root requis)
sudo python app.py

# Windows (terminal admin)
# Lancer dans "Command Prompt" admin, puis :
python app.py
```

### Télécharger Base IEEE OUI

Fabricants MAC (60k entrées) :

```bash
python download_oui.py
# → oui.txt (5 MB)
```

Copiez dans :
- **Dev** : racine `parc_info/oui.txt`
- **Build** : à côté `ParcInfo.exe`

### Ports Scannés par Défaut

```
21 (FTP), 22 (SSH), 23 (Telnet), 25 (SMTP), 53 (DNS),
80 (HTTP), 110 (POP3), 135 (RPC), 139 (NetBIOS),
143 (IMAP), 443 (HTTPS), 445 (SMB), 631 (IPP),
3389 (RDP), 5900 (VNC), 8080 (HTTP-alt), 8443 (HTTPS-alt),
9100 (JetDirect)
```

Personnalisable : Configuration → Ports scan

---

## 📁 Structure du Projet

```
parc_info/
├── 📘 Documentation
│   ├── README.md          ← Vous êtes ici
│   ├── BUILD.md           ← Compilation exécutable
│   ├── claude.md          ← Guide développement
│   └── .claude/           ← Config Claude Code
│
├── 🐍 Code Python
│   ├── app.py             ← Routeurs Flask + DB init
│   ├── launcher.py        ← PyInstaller entry point
│   ├── database.py        ← SQLite/Turso
│   ├── auth_utils.py      ← PBKDF2, CSRF, rate-limit
│   ├── config_helpers.py  ← Config persistée
│   ├── client_helpers.py  ← ACL, audit, pagination
│   ├── models.py          ← SQLAlchemy (optionnel)
│   ├── requirements.txt   ← Dépendances
│   └── parcinfo.spec      ← PyInstaller config
│
├── 🌐 Frontend
│   ├── templates/         ← 25+ templates Jinja2
│   │   ├── base.html
│   │   ├── index.html (dashboard)
│   │   ├── login.html
│   │   ├── liste_appareils.html
│   │   ├── form_appareil.html
│   │   ├── scan_reseau.html
│   │   ├── contrats.html
│   │   └── ...
│   └── static/
│       └── js/            ← Vanilla JS
│           ├── form_tools.js
│           ├── liste_tools.js
│           └── tri.js
│
└── 💾 Données (créées au 1er run)
    ├── parc_info.db       ← SQLite
    ├── secret.key         ← Session key
    ├── uploads/           ← Documents joints
    └── oui.txt (optionnel)
```

---

## ⚙️ Configuration

Paramètres sauvegardés dans BD (`configurations` table) :

| Clé | Défaut | Description |
|-----|--------|-------------|
| `theme` | dark-blue | Thème UI |
| `accent_color` | #00c9ff | Couleur accent principal |
| `ping_interval` | 60 | Intervalle ping (sec) |
| `ping_workers` | 30 | Threads parallèles ping |
| `scan_workers` | 50 | Threads parallèles scan ports |
| `scan_ports` | 21,22,... | Ports TCP scanner |
| `lignes_par_page` | 50 | Pagination listes |

Modifiables via admin → Configuration.

---

## 🔐 Sécurité

### Authentification
- **Hashing** : PBKDF2-SHA256 (via werkzeug)
- **Sessions** : 8h timeout, HttpOnly, SameSite=Lax
- **Rate-limiting** : 10 tentatives / 5 min par IP

### Accès Données
- **Multi-client** : isolation stricte par `client_id`
- **ACL** : proprietaire | ecriture | lecture
- **Audit** : historique complet (user, action, timestamp, delta)

### Protection Attaques
- **CSRF** : tokens stateless `secrets.token_hex(32)`
- **SQL Injection** : requêtes paramétrées (?)
- **XSS** : Jinja2 auto-escape
- **HTTPS** : à configurer reverse proxy (nginx/apache)

**Recommandations Prod** : voir [claude.md](claude.md#-sécurité--conventions-critique)

---

## 🐛 Dépannage

| Problème | Solution |
|----------|----------|
| **"Port already in use"** | Changer port : `FLASK_PORT=5001 python app.py` |
| **"No such table" (DB error)** | Supprimer `parc_info.db` (sera recréée) |
| **Slow queries** | Ajouter index : `CREATE INDEX idx_client ON appareils(client_id)` |
| **Scan réseau absent** | Lancer `sudo python app.py` (root requis pour ARP) |
| **Antivirus bloque exe** | Faux positif PyInstaller (ajouter exception) |
| **macOS "app not found"** | `xattr -cr dist/ParcInfo.app` |

Pour plus de détails : voir [claude.md — Dépannage](claude.md#-dépannage--débogage)

---

## 📖 Documentation Complète

- **[claude.md](claude.md)** ← Guide développement complet
- **[BUILD.md](BUILD.md)** ← Compilation exécutable
- **Code** : consulter docstrings dans `app.py`, `auth_utils.py`, etc.

---

## 🛠️ Développement

### Ajouter Feature

1. **Backend** → route `@app.route()` dans `app.py`
2. **DB** → table/colonne si besoin dans `app.py:init_db()`
3. **Frontend** → template `templates/` + JS si AJAX
4. **Sécurité** → CSRF, ACL, audit trail
5. **Test** → `python app.py` → tester dans navigateur

Voir [claude.md — Workflows](claude.md#-workflows-développement) pour exemples.

### Testing

```bash
# Dev server
python app.py

# Test ACL
# 1. Login user A → client 1
# 2. Login user B → client 2
# 3. Vérifier isolation donnée
```

### Pre-commit

- [ ] CSRF tokens dans tous les forms POST
- [ ] ACL vérifiée (`can_write()`)
- [ ] `log_history()` après modification
- [ ] Pas d'erreurs logs
- [ ] Testée en navigateur

---

## 📜 Licences

- **ParcInfo** : [MIT](LICENSE) (ou votre licence)
- **Flask** : BSD 3-Clause
- **Werkzeug** : BSD 3-Clause
- **SQLite** : Public Domain

---

## 🤝 Contribution

Pour contribuer :

1. Fork le repo
2. Créer branche feature (`git checkout -b feature/xxx`)
3. Commit avec messages clairs
4. Push et créer Pull Request

Voir [CONTRIBUTING.md](CONTRIBUTING.md) pour conventions.

---

## 📞 Support

- **Issues** : https://github.com/your-org/parc_info/issues
- **Discussions** : https://github.com/your-org/parc_info/discussions
- **Email** : dev@your-org.fr

---

**ParcInfo** — Gérez votre parc informatique efficacement. 🚀
