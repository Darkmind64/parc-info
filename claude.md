# ParcInfo — Guide de Développement Claude 🚀

**Dernière mise à jour** : 2026-04-07

## 📋 Aperçu Exécutif

**ParcInfo** est une **application Flask de gestion de parc informatique**, entièrement fonctionnelle et prête pour la production. Elle gère inventaire d'appareils, contrats, périphériques, utilisateurs finaux, et identifiants dans un contexte **multi-client avec ACL granulaire**.

### Caractéristiques Clés
- ✅ **Multi-client** : isolation stricte entre clients, partage d'accès configurable
- ✅ **Authentification** : PBKDF2, sessions 8h, rate-limiting
- ✅ **Scan réseau** : découverte automatique (ping/arp/ports TCP)
- ✅ **CRUD complet** : appareils, contrats, utilisateurs, identifiants, périphériques
- ✅ **Portabilité** : exécutable autonome (.exe/.app) avec BD locale
- ✅ **Audit trail** : historique complet de chaque modification
- ✅ **Configuration persistée** : thèmes, couleurs, listes personnalisées

### Stack Technique
- **Backend** : Python 3.8+, Flask 3.0+, Werkzeug 3.0+
- **Database** : SQLite (local) + option Turso serverless
- **Frontend** : Jinja2 templates, Vanilla JS, HTML/CSS
- **Distribution** : PyInstaller (25-40 MB executable)

---

## 🏗️ Architecture Détaillée

### Couches (Layered Architecture)

```
╔════════════════════════════════════════════════════════════════╗
║ LAUNCHER PYTHON (launcher.py)                                  ║
║ • Point d'entrée PyInstaller                                   ║
║ • Détection port libre + ouverture navigateur auto             ║
║ • Optionnel : tray icon (pystray + PIL)                        ║
╚════════════════════════════════════════════════════════════════╝
                              ↓
╔════════════════════════════════════════════════════════════════╗
║ FLASK APP (app.py) — ~1000+ lignes                             ║
║ • Routeurs @app.route() (login, CRUD appareils, scan, etc)    ║
║ • Middlewares : CSRF, auth, error handlers                    ║
║ • Filtres Jinja2 : formatage ports, périphériques, types      ║
║ • Support PyInstaller : _MEIPASS / _data_base                 ║
╚════════════════════════════════════════════════════════════════╝
                              ↓
╔════════════════════════════════════════════════════════════════╗
║ UTILITAIRES (modules Python)                                   ║
├─ auth_utils.py      : hash PBKDF2, CSRF, rate-limit (10/5min) ║
├─ database.py        : SQLite + Turso, row_to_dict(), init_db()║
├─ config_helpers.py  : cfg_get/cfg_set, listes personnalisées  ║
├─ client_helpers.py  : ACL (can_write), audit, pagination      ║
├─ models.py          : SQLAlchemy ORM (optionnel, coexiste)    ║
└─ [autres] : launcher.py, convert_discovered_devices.py, etc   ║
╚════════════════════════════════════════════════════════════════╝
                              ↓
╔════════════════════════════════════════════════════════════════╗
║ PRÉSENTATION                                                   ║
├─ templates/ (25+ fichiers .html Jinja2)                       ║
│  ├─ base.html → layout principal (nav, auth context)          ║
│  ├─ index.html, parc_general.html, dashboard.html             ║
│  ├─ form_*.html → formulaires CRUD (appareil, contrat, etc)   ║
│  ├─ liste_*.html → listes paginées avec tri/filtrage          ║
│  ├─ detail_*.html → fiche individuelle                        ║
│  ├─ admin_*.html → pages d'administration                     ║
│  └─ login.html, 404.html, 500.html                            ║
└─ static/js/ (vanilla JS — tri, formulaires, AJAX)             ║
╚════════════════════════════════════════════════════════════════╝
                              ↓
╔════════════════════════════════════════════════════════════════╗
║ BASE DE DONNÉES                                                ║
├─ parc_info.db (SQLite local, créée auto au 1er run)           ║
├─ Tables principales : clients, auth_users, appareils,         ║
│  contrats, peripheriques, utilisateurs, identifiants,         ║
│  services, documents, histories, configurations               ║
└─ Stockage : uploads/ (documents joints, images)               ║
╚════════════════════════════════════════════════════════════════╝
```

### Flux Principal

1. **Démarrage**
   - launcher.py → détecte port libre → charge app.py → init_db()
   - Ouvre navigateur auto → http://127.0.0.1:PORT

2. **Authentification**
   - POST /login → check_rate_limit(ip) → validate pwd → session['auth_user_id']
   - Timeout : 8h (validé à chaque @login_required)
   - Rate-limiting : 10 tentatives / 5 min → bloqué temporairement

3. **ACL & Multi-client**
   - get_client_access(client_id) → 'proprietaire' | 'ecriture' | 'lecture' | None
   - Session['client_id'] stocke le client actif
   - can_write(client_id) → booléen (vérifie avant chaque update)

4. **Modifications & Audit**
   - Chaque POST/PUT validé : CSRF, auth, ACL
   - Exécution DB en transaction
   - log_history() → enregistre dans table `histories`

5. **Scan Réseau (async)**
   - Thread séparé : ping → arp → port scan
   - Résultats en temps réel : WebSocket ou AJAX polling
   - Import optionnel dans la BD

---

## 📁 Structure Détaillée du Projet

```
parc_info/                           # Répertoire racine
│
├─ 📘 Documentation
│  ├─ claude.md                      # Ce fichier — guide complet pour développement
│  ├─ README.md                      # Guide utilisateur + installation
│  ├─ BUILD.md                       # Compilation PyInstaller (.exe/.app)
│  └─ .claude/
│     ├─ launch.json                 # Configuration serveurs Claude Code (dev)
│     └─ settings.local.json         # Paramètres locaux Claude Code
│
├─ 🐍 Code Python
│  ├─ app.py (1000+ lignes)          # Routeurs Flask, middlewares CSRF/auth
│  ├─ launcher.py (96 lignes)        # Point entrée PyInstaller → port libre + browser
│  ├─ database.py (300+ lignes)      # SQLite/Turso, row_to_dict(), init_db()
│  ├─ auth_utils.py (200+ lignes)    # PBKDF2, CSRF, rate-limiting (10/5min)
│  ├─ config_helpers.py (200+ lignes)# cfg_get/cfg_set, listes perso
│  ├─ client_helpers.py (300+ lignes)# ACL, audit, pagination, formatage
│  ├─ models.py (200+ lignes)        # SQLAlchemy ORM (optionnel)
│  ├─ convert_discovered_devices.py  # Utilitaire import réseau
│  ├─ download_oui.py                # Télécharge base IEEE OUI (fabricants)
│  └─ requirements.txt                # flask, werkzeug, flask-sqlalchemy
│
├─ 📄 Configuration Build
│  ├─ parcinfo.spec                  # PyInstaller spec (exe/app)
│  └─ __pycache__/                   # Bytecode compilé (ignoré)
│
├─ 🌐 Présentation (Templates Jinja2)
│  └─ templates/ (25+ fichiers)
│     ├─ base.html                   # Layout parent (nav, footer, auth)
│     ├─ index.html                  # Accueil / tableau de bord
│     ├─ login.html                  # Formulaire connexion
│     │
│     ├─ Parc & Configuration
│     │  ├─ parc_general.html        # Vue synthétique du parc
│     │  ├─ dashboard.html           # Statistiques/métriques
│     │  ├─ baie_brassage.html       # Baie de brassage
│     │  └─ plan_editeur.html        # Plans de disposition (éditeur)
│     │
│     ├─ Appareils
│     │  ├─ liste_appareils.html     # Liste paginée appareils
│     │  ├─ form_appareil.html       # Formulaire CRUD
│     │  └─ detail_*.html            # Fiche détail
│     │
│     ├─ Périphériques
│     │  ├─ peripheriques.html       # Liste périphériques
│     │  └─ form_peripherique.html   # Form CRUD
│     │
│     ├─ Contrats & Documents
│     │  ├─ contrats.html            # Liste contrats
│     │  ├─ form_contrat.html        # Form CRUD
│     │  ├─ detail_contrat.html      # Fiche contrat
│     │  └─ documents_appareil.html  # Uploads associés
│     │
│     ├─ Services & Identifiants
│     │  ├─ services.html            # Services/logiciels métier
│     │  ├─ form_service.html        # Form CRUD
│     │  ├─ identifiants.html        # Credentials stockées
│     │  ├─ form_identifiant.html    # Form CRUD
│     │  ├─ types_droits.html        # Types droits d'accès
│     │  └─ droits_utilisateur.html  # Matrice droits
│     │
│     ├─ Utilisateurs Finaux
│     │  ├─ utilisateurs.html        # End-users du client
│     │  ├─ form_utilisateur.html    # Form CRUD
│     │  └─ partage_client.html      # Partage accès clients
│     │
│     ├─ Admin
│     │  ├─ admin_users.html         # Gestion utilisateurs
│     │  ├─ admin_user_form.html     # Form user admin
│     │  ├─ outils.html              # Outils administration
│     │  └─ kb.html                  # Knowledge base/help
│     │
│     ├─ Scan Réseau
│     │  └─ scan_reseau.html         # Découverte auto (ping/arp/ports)
│     │
│     ├─ Historique & Listes
│     │  ├─ historique.html          # Audit trail
│     │  └─ liste_plans.html         # Plans disponibles
│     │
│     ├─ Erreurs & Popups
│     │  ├─ 404.html                 # Page non trouvée
│     │  ├─ 500.html                 # Erreur serveur
│     │  ├─ login.html               # Popup identifiant
│     │  └─ popup_*.html             # Popups modales
│     │
│     ├─ Profil Utilisateur
│     │  └─ profil.html              # Profil connecté + prefs
│     │
│     └─ Clients & Configuration
│        ├─ clients.html             # Gestion clients
│        └─ form_client.html         # Form CRUD
│
├─ 🎨 Ressources Statiques
│  └─ static/
│     ├─ js/
│     │  ├─ form_tools.js            # Utilitaires formulaires (CSRF, validation)
│     │  ├─ liste_tools.js           # Filtrage/tri listes, pagination AJAX
│     │  └─ tri.js                   # Tri colonne (header cliquables)
│     │
│     ├─ [css/ — optionnel]          # Styles CSS personnalisés
│     ├─ [images/ — optionnel]       # Images/icônes personnalisées
│     └─ [icon.ico — optionnel]      # Icône Windows PyInstaller
│
├─ 💾 Données Persistantes
│  ├─ parc_info.db                   # Base de données SQLite (auto-créée)
│  ├─ secret.key                     # Clé secrète Flask 32-hex (générée une fois)
│  ├─ uploads/                       # Documents joints (PDF, images, etc)
│  │  └─ app<id>_<timestamp>_<filename>
│  ├─ oui.txt (optionnel)            # Base IEEE OUI (fabricants réseau)
│  └─ __pycache__/                   # Bytecode Python compilé (ignoré)
```

### Hiérarchie Fichiers Clés

| Fichier | Lignes | Responsabilité |
|---------|--------|-----------------|
| app.py | 1000+ | Routeurs Flask, middlewares, filtres Jinja2, init_db() |
| launcher.py | 96 | Point entrée PyInstaller, port libre, browser auto |
| database.py | 300+ | Connexion SQLite/Turso, helpers SQL |
| auth_utils.py | 200+ | Auth PBKDF2, CSRF, rate-limit, validation |
| config_helpers.py | 200+ | Config persistée, listes perso |
| client_helpers.py | 300+ | ACL, audit, pagination, formatage |
| models.py | 200+ | Modèles SQLAlchemy (optionnel) |

---

## 🗄️ Schéma de Base de Données (SQLite)

Créée automatiquement par `app.py:init_db()` au 1er lancement.

### Groupe 1 : Authentification & Multi-client

```sql
-- Utilisateurs système (admins + users)
CREATE TABLE auth_users (
    id INTEGER PRIMARY KEY,
    login TEXT UNIQUE NOT NULL,
    password_hash TEXT,           -- PBKDF2 (ou SHA256 legacy)
    nom TEXT, prenom TEXT,
    email TEXT,
    role TEXT,                    -- 'admin' | 'user'
    logo_fichier TEXT,            -- Avatar upload
    actif INTEGER DEFAULT 1,      -- Softdelete (0=suspendu)
    must_change_password INTEGER, -- Forcer change au login
    date_creation TEXT, date_maj TEXT
);

-- Clients (entités à gérer)
CREATE TABLE clients (
    id INTEGER PRIMARY KEY,
    nom TEXT NOT NULL,
    contact TEXT, telephone TEXT, email TEXT, adresse TEXT,
    notes TEXT,
    couleur TEXT DEFAULT '#00c9ff',    -- Badge couleur personnalisé
    auth_user_id INTEGER,              -- Owner (propriétaire)
    date_creation TEXT, date_maj TEXT
);

-- Matrice d'accès (partage multi-client)
CREATE TABLE client_partages (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL,
    auth_user_id INTEGER NOT NULL,
    niveau TEXT,                  -- 'proprietaire' | 'ecriture' | 'lecture'
    UNIQUE(client_id, auth_user_id)
);

-- Tentatives échouées login (rate-limiting)
CREATE TABLE failed_login_attempts (
    id INTEGER PRIMARY KEY,
    ip_address TEXT,
    username TEXT,
    attempted_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

### Groupe 2 : Parc Informatique

```sql
-- Configuration du parc par client (site, réseau, équipements)
CREATE TABLE parc_general (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL,
    nom_site TEXT,
    adresse TEXT,
    type_connexion TEXT,           -- 'ADSL' | 'Fibre' | 'Leasing' etc
    debit_montant TEXT, debit_descendant TEXT,
    fournisseur_internet TEXT,
    ip_publique TEXT,
    plage_ip_locale TEXT,          -- '192.168.1.0/24'
    nb_machines INTEGER,
    nb_utilisateurs INTEGER,
    domaine TEXT,
    serveur_dns TEXT,
    passerelle TEXT,
    -- Baie de brassage
    baie_marque TEXT, baie_nb_u INTEGER,
    -- Switch
    switch_marque TEXT, switch_nb_ports INTEGER, switch_nb_unites INTEGER,
    -- Serveur
    routeur_marque TEXT,
    serveur_marque TEXT, serveur_modele TEXT,
    -- Autre
    ups_marque TEXT, ups_capacite TEXT,
    autres_equipements TEXT,
    -- Logiciels
    logiciels_metier TEXT,         -- JSON array
    antivirus TEXT, os_principal TEXT,
    suite_bureautique TEXT,
    notes TEXT,
    date_maj TEXT
);

-- Appareils (PC, Laptop, Serveur, etc)
CREATE TABLE appareils (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL,
    type TEXT,                     -- 'PC' | 'Laptop' | 'Serveur' | etc
    nom TEXT NOT NULL,
    numero_serie TEXT UNIQUE,
    adresse_ip TEXT,
    adresse_mac TEXT,
    marque TEXT,
    modele TEXT,
    date_acquisition TEXT,
    date_fin_support TEXT,
    prix_acquisition TEXT,
    date_fin_garantie TEXT,
    localisation TEXT,
    notes TEXT,
    utilisateur_affecte TEXT,
    --- Métadonnées ajoutées
    documents JSON,                -- Liste IDs documents
    services JSON,                 -- Services installés
    port_scan_json TEXT,           -- Résultats scan ports
    date_maj TEXT
);

-- Périphériques (écrans, claviers, imprimantes, etc)
CREATE TABLE peripheriques (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL,
    categorie TEXT,                -- 'Écran' | 'Clavier' | 'Imprimante' etc
    numero_serie TEXT,
    marque TEXT, modele TEXT,
    date_acquisition TEXT,
    prix TEXT,
    localisation TEXT,
    notes TEXT,
    appareil_lie_id INTEGER,       -- FK appareils (optionnel)
    date_maj TEXT
);
```

### Groupe 3 : Services & Configuration Métier

```sql
-- Services métier / logiciels
CREATE TABLE services (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL,
    nom TEXT NOT NULL,
    type TEXT,                     -- 'ERP' | 'Comptabilité' | 'GRH' etc
    version TEXT,
    licence TEXT,
    description TEXT,
    notes TEXT,
    date_maj TEXT
);

-- Utilisateurs finaux du client
CREATE TABLE utilisateurs (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL,
    nom TEXT, prenom TEXT,
    email TEXT,
    fonction TEXT,
    telephone TEXT,
    date_arrivee TEXT,
    date_depart TEXT,
    notes TEXT,
    date_maj TEXT
);
```

### Groupe 4 : Contrats & Documents

```sql
-- Contrats (maintenance, support, licences, etc)
CREATE TABLE contrats (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL,
    type TEXT,                     -- 'Maintenance' | 'Support' | 'SaaS' etc
    description TEXT,
    fournisseur TEXT,
    date_debut TEXT,
    date_fin TEXT,
    montant TEXT,
    devise TEXT DEFAULT 'EUR',
    numero_contrat TEXT,
    notes TEXT,
    documents JSON,                -- Liste IDs documents joints
    date_maj TEXT
);

-- Stockage identifiants / credentials
CREATE TABLE identifiants (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL,
    categorie TEXT,                -- 'Admin réseau' | 'Serveur' | 'Cloud' etc
    description TEXT,
    login TEXT,
    password_encrypted TEXT,       -- CHIFFRÉ en prod
    url_acces TEXT,
    notes TEXT,
    date_maj TEXT
);

-- Documents joints (PDF, images)
CREATE TABLE documents_appareil (
    id INTEGER PRIMARY KEY,
    appareil_id INTEGER,
    fichier_hash TEXT UNIQUE,
    nom_original TEXT,
    type_fichier TEXT,
    taille INTEGER,
    date_upload TEXT,
    uploader_id INTEGER
);
```

### Groupe 5 : Audit & Configuration

```sql
-- Historique (audit trail)
CREATE TABLE histories (
    id INTEGER PRIMARY KEY,
    action TEXT,                   -- 'CREATE_APPAREIL' | 'UPDATE_*' | 'DELETE_*'
    user_id INTEGER,
    client_id INTEGER,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    details JSON,                  -- {'field': old_val, 'field': new_val}
    ip_address TEXT
);

-- Configuration persistée (clé/valeur JSON)
CREATE TABLE configurations (
    clé TEXT PRIMARY KEY,
    valeur TEXT,                   -- JSON-encodé
    date_maj TEXT
);
```

### Relations Principales

```
auth_users (1) ──→ (N) clients              (propriétaire)
                  (N) client_partages       (accès partagé)

clients (1) ─────→ (N) appareils
                  (N) peripheriques
                  (N) services
                  (N) utilisateurs
                  (N) contrats
                  (N) identifiants
                  (N) parc_general

appareils (1) ───→ (N) documents_appareil
                  (N) contrats (implicite via JSON)

contrats (1) ────→ (N) documents_appareil
```

---

## 🔐 Sécurité & Conventions (CRITIQUE)

### 1. Authentification Multi-couches

```python
# ✅ Hash sécurisé (PBKDF2 via werkzeug)
from auth_utils import hash_pwd, check_pwd
pwd_hash = hash_pwd(plaintext)  # PBKDF2+SHA256+salt
ok, needs_rehash = check_pwd(plaintext, stored_hash)  # Timing-safe

# ✅ Sessions durcie
app.config['SESSION_COOKIE_HTTPONLY'] = True   # ← Inaccessible JS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # ← Défense CSRF
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)  # ← Timeout

# ✅ Contrôle durée session
@login_required  # ← Vérifie auth + timeout 8h
def protected_route():
    user = get_auth_user()  # ← Reload depuis DB (vérif actif=1)
    ...
```

### 2. Rate-Limiting (Brute-Force Mitigation)

```python
# ✅ Implémentation en mémoire (par IP)
def check_rate_limit(ip: str) -> bool:
    """Retourne False si > 10 tentatives en 5 min"""
    # Stockage : {ip: [t1, t2, ...], ...}
    # Nettoyage : garde que les tentatives < 5 min

record_failed_attempt(ip)  # Après /login échoué
reset_attempts(ip)         # Après /login réussi
```

**Configuration**
- Max 10 tentatives par IP
- Fenêtre glissante : 5 minutes
- Persiste en mémoire (pas en DB)

### 3. CSRF Protection (Obligatoire)

```html
<!-- CHAQUE formulaire POST/PUT/DELETE doit avoir: -->
<form method="POST">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <input type="text" name="champ">
  <button type="submit">Valider</button>
</form>
```

```python
# Middleware automatique : app.py:99-101
@app.before_request
def csrf_protect():
    validate_csrf_request()  # Lève 403 si CSRF manquant/invalide
```

**Implémentation**
- Token généré : `secrets.token_hex(32)` (256 bits)
- Stocké : `session['csrf_token']`
- Validé : compare timing-safe (`secrets.compare_digest()`)
- Exceptions : GET, /static/*, /login, /logout

**JS AJAX**
```javascript
// Option 1 : Form data
const formData = new FormData();
formData.append('csrf_token', '{{ csrf_token }}');
formData.append('data', value);

// Option 2 : Headers
fetch('/api/endpoint', {
  method: 'POST',
  headers: {
    'X-CSRF-Token': '{{ csrf_token }}',
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({...})
});
```

### 4. Contrôle d'Accès (ACL) Multi-Client ⚠️ CRITIQUE

```python
# ✅ Vérifier AVANT chaque write
from client_helpers import can_write, get_client_access

@app.route('/api/appareil/<id>', methods=['PUT'])
@login_required
def update_appareil(id):
    client_id = get_client_id()  # Client actif de l'utilisateur

    # VÉRIFIER ACL
    if not can_write(client_id):  # ← NON FACULTATIF
        return jsonify({'error': 'Forbidden'}), 403

    # Vérifier que l'appareil appartient bien au client
    conn = get_db()
    appareil = conn.execute(
        'SELECT * FROM appareils WHERE id=? AND client_id=?',
        (id, client_id)
    ).fetchone()

    if not appareil:
        conn.close()
        return jsonify({'error': 'Not Found'}), 404

    # Update sécurisé
    conn.execute(
        'UPDATE appareils SET nom=? WHERE id=? AND client_id=?',
        (request.form['nom'], id, client_id)  # ← Paramètres
    )
    conn.commit()
    log_history('UPDATE_APPAREIL', user['login'], client_id, {...})
    conn.close()
    return jsonify({'ok': True})
```

**Niveaux d'accès**
- `proprietaire` : propriétaire du client (all ops)
- `ecriture` : lecture + création/modification
- `lecture` : consultation uniquement
- `None` : pas d'accès (403 Forbidden)

**Pattern ACL**
```python
# get_client_access() retourne :
# - 'proprietaire' si admin OU propriétaire
# - 'ecriture' / 'lecture' si partagé
# - None si pas d'accès

# can_write() simplifie :
# - True si proprietaire OU ecriture
# - False si lecture OU None
```

### 5. Validation Formulaires

```python
from auth_utils import validate_form

rules = [
    ('nom', 'str', True),           # Obligatoire
    ('ip', 'ip', False),            # Optionnel, si fourni → valide IP
    ('mac', 'mac', False),          # Format MAC
    ('email', 'email', False),      # Format email
    ('url', 'url', False),          # Format URL
    ('date', 'date', False),        # Format date
]

errors = validate_form(rules, request.form)
if errors:
    return jsonify({'errors': errors}), 400
```

### 6. Injection SQL (Protection)

```python
# ✅ TOUJOURS paramètres (? placeholders)
conn.execute(
    'SELECT * FROM appareils WHERE client_id=? AND nom LIKE ?',
    (client_id, f'%{search}%')  # ← Paramètres séparés
)

# ❌ JAMAIS f-string ou concaténation
conn.execute(
    f"SELECT * FROM appareils WHERE client_id={client_id}"  # ← DANGER
)
```

### 7. XSS Protection (Jinja2)

```html
<!-- ✅ Auto-échappement (défaut) -->
<h1>{{ appareil.nom }}</h1>  <!-- &lt; &gt; &amp; échappés -->

<!-- ❌ Raw (DANGER) -->
<div>{{ description | safe }}</div>  <!-- Accepte HTML/JS -->

<!-- ✅ url_for() (safe) -->
<a href="{{ url_for('appareil_detail', id=appareil.id) }}">Vue</a>

<!-- ❌ Format string (DANGER) -->
<a href="/appareil?name={{ user_input }}">  <!-- XSS possible -->
```

### 8. Uploads (Points d'Amélioration)

**État actuel**
- ✅ Stockés hors webroot (uploads/)
- ✅ Servis via `send_from_directory()` avec headers `attachment`
- ❌ Aucune validation d'extension
- ❌ Aucune validation MIME
- ❌ Pas de limite de taille fichier
- ❌ Pas de scan antivirus

**Recommandations prod**
```python
# Whitelist extensions
ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png', 'docx', 'xlsx', 'txt'}

# Whitelist MIME types
ALLOWED_MIMES = {
    'application/pdf', 'image/jpeg', 'image/png',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    ...
}

# Limiter taille
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# Vérifier extension + MIME
def secure_upload(file):
    if '.' not in file.filename:
        return False, 'Extension requise'

    ext = file.filename.rsplit('.', 1)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, f'Extension {ext} non autorisée'

    mime = file.content_type  # ou file.mimetype
    if mime not in ALLOWED_MIMES:
        return False, f'Type {mime} non autorisé'

    if file.content_length > MAX_FILE_SIZE:
        return False, 'Fichier trop volumineux'

    return True, None
```

### Checklist Sécurité

- [ ] CSRF token dans chaque formulaire POST
- [ ] ACL vérifiée avant chaque write (`can_write()`)
- [ ] SQL paramètré (? placeholders)
- [ ] Input validé (`validate_form()`)
- [ ] Output échappé (Jinja2 auto)
- [ ] Rate-limiting actif (/login)
- [ ] Sessions HttpOnly + SameSite
- [ ] Audit trail complet (`log_history()`)
- [ ] Pas de secrets en logs
- [ ] Uploads whitelistés (extension + MIME)

---

## 📝 Conventions de Code

### Import Principal (app.py)

```python
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import sqlite3, subprocess, re, socket, ipaddress, threading, os, platform, logging

# Modules locaux
from database import get_db, row_to_dict
from auth_utils import (
    hash_pwd, check_pwd,
    get_auth_user, login_required,
    get_csrf_token, validate_csrf_request,
    check_rate_limit, record_failed_attempt, reset_attempts,
    validate_form
)
from config_helpers import (
    LISTE_DEFAULTS, CFG_DEFAULTS,
    get_liste, cfg_get, cfg_set, cfg_all, cfg_invalidate
)
from client_helpers import (
    paginate, get_client_access, can_write,
    get_client_with_acces, get_client_id, get_clients,
    log_history, log_error, garantie_active, human_size,
    fmt_appareils, fmt_garantie_periph, fmt_contrat
)
```

### API Utilitaires Complète

#### `database.py`

| Fonction | Signature | Usage |
|----------|-----------|-------|
| `get_db()` | `() → Connection` | SQLite ou Turso selon config |
| `get_local_db()` | `() → sqlite3.Connection` | Force SQLite (ignore Turso) |
| `row_to_dict(row)` | `(Row\|None) → dict` | sqlite3.Row → Python dict |
| `init_db()` | `()` | Crée schéma si absent (appelé par app) |

#### `auth_utils.py`

| Fonction | Signature | Usage |
|----------|-----------|-------|
| `hash_pwd(pwd)` | `(str) → str` | PBKDF2 hash |
| `check_pwd(pwd, hash)` | `(str, str) → (bool, bool)` | Vérif + migration SHA256 |
| `get_auth_user()` | `() → dict\|None` | User connecté depuis session |
| `login_required(f)` | `@decorator` | Redirect /login si pas auth |
| `get_csrf_token()` | `() → str` | Récupère/génère token CSRF |
| `validate_csrf_request()` | `()` | Valide ou 403 (avant_request) |
| `check_rate_limit(ip)` | `(str) → bool` | <10 tentatives/5min ? |
| `record_failed_attempt(ip)` | `(str)` | Enregistre tentative échouée |
| `reset_attempts(ip)` | `(str)` | Réinitialise après succès |
| `validate_form(rules, form)` | `(list, dict) → list[err]` | Valide formulaire |

#### `config_helpers.py`

| Fonction | Signature | Usage |
|----------|-----------|-------|
| `cfg_get(key, default)` | `(str, any) → str` | Lire config (cached) |
| `cfg_set(key, value)` | `(str, str)` | Écrire config persistent |
| `cfg_all()` | `() → dict` | Tous les configs |
| `cfg_invalidate()` | `()` | Vider cache mémoire |
| `get_liste(key)` | `(str) → list` | Lister perso (types_appareils, etc) |

#### `client_helpers.py`

| Fonction | Signature | Usage |
|----------|-----------|-------|
| `get_client_access(cid)` | `(int) → 'proprietaire'\|'ecriture'\|'lecture'\|None` | Niveau accès |
| `can_write(client_id)` | `(int) → bool` | Peut modifier ? |
| `get_client_id()` | `() → int\|None` | Client actif session |
| `get_client_with_acces(cid)` | `(int) → dict` | Client + son accès |
| `get_clients()` | `() → list[dict]` | Tous clients accessibles user |
| `log_history(action, user, cid, details)` | `(str, str, int, dict)` | Audit trail |
| `log_error(action, user, cid, error)` | `(str, str, int, str)` | Erreur audit |
| `paginate(query, params, page)` | `(str, tuple, int) → (rows, dict)` | Pagination |
| `garantie_active(date_fin)` | `(str) → bool` | Garantie valide ? |
| `human_size(bytes)` | `(int) → str` | '1.5 MB' au lieu de 1572864 |
| `fmt_appareils(rows)` | `(list) → list` | Format affichage appareils |
| `fmt_garantie_periph(periph)` | `(dict) → str` | Statut garantie périph |
| `fmt_contrat(contract)` | `(dict) → dict` | Format affichage contrat |

### Patterns Routeurs

```python
# ✅ Pattern standard
@app.route('/api/appareil/<int:id>', methods=['GET', 'POST'])
@login_required
def appareil_handler(id):
    user = get_auth_user()
    client_id = get_client_id()

    # GET — lecture
    if request.method == 'GET':
        if not get_client_access(client_id):
            return jsonify({'error': 'Forbidden'}), 403

        conn = get_db()
        row = conn.execute(
            'SELECT * FROM appareils WHERE id=? AND client_id=?',
            (id, client_id)
        ).fetchone()
        conn.close()

        if not row:
            return jsonify({'error': 'Not Found'}), 404

        return jsonify(row_to_dict(row))

    # POST — écriture
    if not can_write(client_id):
        return jsonify({'error': 'Forbidden'}), 403

    errors = validate_form([
        ('nom', 'str', True),
        ('ip', 'ip', False),
        ('mac', 'mac', False),
    ], request.form)

    if errors:
        return jsonify({'errors': errors}), 400

    conn = get_db()
    try:
        conn.execute(
            'UPDATE appareils SET nom=?, ip=? WHERE id=? AND client_id=?',
            (request.form['nom'], request.form.get('ip'), id, client_id)
        )
        conn.commit()
        log_history('UPDATE_APPAREIL', user['login'], client_id,
                    {'id': id, 'nom': request.form['nom']})
    except Exception as e:
        conn.rollback()
        log_error('UPDATE_APPAREIL', user['login'], client_id, str(e))
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

    return jsonify({'ok': True})
```

### Patterns Templates Jinja2

```html
<!-- Layout héritage -->
{% extends "base.html" %}

{% block title %}Appareil {{ appareil.nom }}{% endblock %}

{% block content %}
<div class="container">
    <!-- User auth context (injecté auto) -->
    {% if auth_user %}
        <p>Connecté : {{ auth_user.nom }} ({{ auth_user.role }})</p>
    {% endif %}

    <!-- Formulaire avec CSRF (obligatoire) -->
    <form method="POST" action="{{ url_for('appareil_update') }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <input type="text" name="nom" value="{{ appareil.nom }}" required>
        <button type="submit">Valider</button>
    </form>

    <!-- Pagination -->
    {% if pagination %}
        {% if pagination.page > 1 %}
            <a href="?page=1">Première</a>
            <a href="?page={{ pagination.page - 1 }}">← Précédent</a>
        {% endif %}

        <span>Page {{ pagination.page }} / {{ pagination.pages }}</span>

        {% if pagination.page < pagination.pages %}
            <a href="?page={{ pagination.page + 1 }}">Suivant →</a>
            <a href="?page={{ pagination.pages }}">Dernière</a>
        {% endif %}
    {% endif %}

    <!-- Filtre JSON (from template) -->
    {% for periph in appareil.peripheriques | fromjson %}
        <li>{{ periph }}</li>
    {% endfor %}
</div>
{% endblock %}
```

### Logging Standard

```python
import logging
logger = logging.getLogger('parcinfo')

# ✅ Informations utiles
logger.info(f"User {user_id} ({user['login']}) created device {device_id}")
logger.warning(f"Rate limit exceeded for IP {ip_address}")
logger.exception("Failed to execute scan")  # ← Stack trace auto

# ❌ Mauvaises pratiques
logger.info(f"Password: {pwd}")  # PII
logger.debug("test")  # Pas assez précis
print("debug")       # Utiliser logger.debug()
```

### Gestion Erreurs

```python
# ✅ Traiter les erreurs extérieures (DB, réseau, files)
try:
    conn = get_db()
    result = conn.execute("SELECT COUNT(*) FROM appareils").fetchone()[0]
except Exception as e:
    logger.exception("DB error")
    return jsonify({'error': 'Database error'}), 500
finally:
    conn.close()

# ✅ Logique métier — trust framework
from config_helpers import cfg_get
per_page = int(cfg_get('lignes_par_page', '50'))  # Trust on app startup
```

---

## 🚀 Installation, Développement, Build

### Installation Développement

```bash
# 1. Cloner/naviguer
cd parc_info

# 2. Créer venv (recommandé)
python -m venv venv
source venv/bin/activate  # Linux/macOS
venv\Scripts\activate     # Windows

# 3. Installer dépendances
pip install -r requirements.txt
# flask>=3.0.0
# werkzeug>=3.0.0
# flask-sqlalchemy>=3.1.0

# 4. Lancer
python app.py
# → http://localhost:5000
# Vous verrez : [INFO] Running on http://127.0.0.1:5000
```

### Développement Local

```bash
# Lancer dev server (rechargement auto)
python app.py

# Lancer en mode admin (scan réseau complet)
# Linux/macOS
sudo python app.py

# Windows : lancer terminal en mode admin, puis
python app.py
```

**Premier Lancement**
- BD créée automatiquement : `parc_info.db`
- Clé secrète générée : `secret.key` (256 bits)
- Utilisateur par défaut : voir code app.py:init_db() pour login/pwd initial

### Build Exécutable (PyInstaller)

#### Pré-requis

```bash
# Installer dépendances build (une seule fois)
pip install pyinstaller pillow pystray

# Installer dépendances Flask/Werkzeug
pip install -r requirements.txt
```

#### Compilation Windows → ParcInfo.exe

```bash
cd parc_info
pyinstaller parcinfo.spec
# ↓
# dist/ParcInfo.exe (25-40 MB)

# Test
./dist/ParcInfo.exe
# → Navigateur s'ouvre auto
# → http://127.0.0.1:<PORT_LIBRE>
```

#### Compilation macOS → ParcInfo.app

```bash
cd parc_info
pyinstaller parcinfo.spec
# ↓
# dist/ParcInfo.app

# Si macOS bloque (non signé) :
xattr -cr dist/ParcInfo.app  # Supprimer quarantaine
# OU clic droit → Ouvrir quand même

# Optionnel : signer
codesign --deep --force --sign - dist/ParcInfo.app
```

#### Données Persistantes

Après build, structure du répertoire installation :
```
Dossier contenant ParcInfo.exe/
├── ParcInfo.exe                    (l'executable)
├── parc_info.db                    (BD créée 1er run)
├── secret.key                      (clé secrète 1er run)
├── uploads/                        (documents joints)
└── oui.txt (optionnel)             (base IEEE OUI)
```

**Important** : BD et uploads survivent aux mises à jour de l'exe.

### Télécharger Base IEEE OUI (Fabricants)

```bash
# Depuis racine projet
python download_oui.py
# → Télécharge depuis https://standards.ieee.org/oui.txt
# → Sauvegarde : ./oui.txt (5 MB, ~60k fabricants)

# Puis, copier oui.txt dans :
# - Dev : dossier racine parc_info/
# - Build : à côté de ParcInfo.exe après build
```

Utilisation dans l'app :
- Scan réseau détecte MAC → cherche fabricant dans oui.txt
- Affichage amélioré en liste appareils ("Apple Inc.", "Microsoft Corp.", etc)

---

## 🔧 Workflows Développement

### Ajouter une Entité (ex. Type Équipement)

**Étape 1 : Schéma DB** (app.py:init_db())
```python
c.execute('''CREATE TABLE IF NOT EXISTS type_equipements (
    id INTEGER PRIMARY KEY,
    nom TEXT NOT NULL UNIQUE,
    categorie TEXT,
    date_creation TEXT DEFAULT CURRENT_TIMESTAMP
)''')
```

**Étape 2 : Lister perso** (config_helpers.py)
```python
LISTE_DEFAULTS = {
    'categories_equipements': [
        'Actif réseau', 'Serveur', 'Stockage', ...
    ],
    ...
}
```

**Étape 3 : Routes CRUD** (app.py)
```python
@app.route('/api/type-equipement', methods=['POST'])
@login_required
def create_type_equipement():
    if not (user := get_auth_user()) or user['role'] != 'admin':
        return jsonify({'error': 'Admin only'}), 403

    nom = request.form.get('nom', '').strip()
    if not nom:
        return jsonify({'errors': ['Nom requis']}), 400

    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO type_equipements (nom, categorie) VALUES (?, ?)',
            (nom, request.form.get('categorie'))
        )
        conn.commit()
        log_history('CREATE_TYPE_EQUIPEMENT', user['login'], None, {'nom': nom})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Nom déjà existant'}), 409
    except Exception as e:
        logger.exception("Create type_equipement")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

    return jsonify({'ok': True})
```

**Étape 4 : Template** (templates/type_equipement_form.html)
```html
{% extends "base.html" %}
{% block content %}
<form method="POST" action="{{ url_for('create_type_equipement') }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <input type="text" name="nom" placeholder="Nom type" required>
  <select name="categorie">
    <option value="">--</option>
    {% for cat in config.categories_equipements %}
      <option>{{ cat }}</option>
    {% endfor %}
  </select>
  <button type="submit">Ajouter</button>
</form>
{% endblock %}
```

**Étape 5 : Tester**
```bash
python app.py
# http://localhost:5000
# POST /api/type-equipement
```

### Ajouter un Champ à Appareil

**Approche 1 : Colonne nouvelle** (migration manuelle)
```python
# Dans app.py:init_db() → après CREATE TABLE appareils:
c.execute('ALTER TABLE appareils ADD COLUMN nouveau_champ TEXT DEFAULT ""')
```

**Approche 2 : JSON (flexible)**
```python
# Stocker dans colonne existante 'metadata' (JSON)
conn.execute(
    'UPDATE appareils SET metadata=? WHERE id=?',
    (json.dumps({...}), id)
)
```

### Ajouter une Validation Personnalisée

```python
# auth_utils.py
_RE_SERIALNUMBER = re.compile(r'^[A-Z0-9\-]{5,20}$')

def validate_form(rules, form):
    errors = []
    for field, ftype, required in rules:
        val = form.get(field, '').strip()
        if required and not val:
            errors.append(f'{field} requis')
            continue
        if not val:
            continue

        # ✅ Types standards
        if ftype == 'ip' and not _RE_IP.match(val):
            errors.append(f'IP invalide: {val}')
        # ✅ Types custom
        elif ftype == 'sn' and not _RE_SERIALNUMBER.match(val):
            errors.append(f'S/N format invalide')

    return errors
```

### Filtres Jinja2 Personnalisés

```python
# app.py
@app.template_filter('prix_format')
def prix_format_filter(montant):
    """Formatte montant en EUR"""
    try:
        return f"{float(montant):,.2f} €".replace(',', '.')
    except:
        return montant

# Template
{{ appareil.prix_acquisition | prix_format }}  → "1.250,00 €"
```

---

## 🐛 Dépannage & Débogage

### Logs Serveur

```bash
# Format : [TIMESTAMP] [LEVEL] logger_name: message
# Exemple
[2026-04-07 14:32:15] [INFO] parcinfo: User 1 logged in
[2026-04-07 14:33:22] [WARNING] parcinfo: CSRF check failed
[2026-04-07 14:34:01] [ERROR] parcinfo: DB error: ...

# Pour debug détaillé
python -c "
import logging
logging.getLogger('parcinfo').setLevel(logging.DEBUG)
" && python app.py
```

### Inspecter Base de Données

```bash
# Ouvrir DB
sqlite3 parc_info.db

# Commandes utiles
> .tables                           # Lister tables
> .schema appareils                 # Voir structure
> SELECT COUNT(*) FROM appareils;  # Compter
> SELECT * FROM auth_users LIMIT 1; # Premier user
> SELECT * FROM histories WHERE action LIKE 'CREATE%' LIMIT 5; # Audit
> .quit
```

### Déboguer Requêtes API

```bash
# 1. Ouvrir DevTools (F12)
# 2. Onglet Network
# 3. Cliquer action dans UI
# 4. Voir requête : URL, headers, body, réponse

# Exemple avec curl
curl -X POST http://localhost:5000/api/appareil \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "X-CSRF-Token: <TOKEN>" \
  -d "nom=Test&ip=192.168.1.100"
```

### Problèmes Courants

| Symptôme | Cause Probable | Solution |
|----------|----------------|----------|
| "Port already in use" | Port 5000 occupé | Changer FLASK_PORT ou redémarrer |
| "No such table" | DB corruptée/non initialisée | Supprimer parc_info.db (sera recréée) |
| "CSRF token missing" | Form sans csrf_token input | Ajouter `<input name="csrf_token">` |
| "Forbidden (403)" | ACL check échoue | Vérifier can_write(), client_id |
| Slow performance | Requête SQL mal optimisée | Ajouter index sur client_id, user_id |
| Session expires | Timeout 8h dépassé | Se reconnecter (normal) |
| Rate limit exceeded | >10 login échoués en 5min | Attendre 5 min ou réinitialiser |

### Debug Mode (Développement)

```python
# app.py : autoriser pour dev UNIQUEMENT
# app.run(debug=True, ...)  # ← JAMAIS en prod

# Console Python interactive
python
>>> from app import app, get_db
>>> with app.app_context():
...     db = get_db()
...     user = db.execute('SELECT * FROM auth_users LIMIT 1').fetchone()
...     print(user)
```

### Performance Profiling

```bash
# Profiler requête lente
python -m cProfile -s cumtime app.py
# Voir "ncalls" et "cumtime" pour bottlenecks

# Vérifier requêtes SQL lentes
# Active SQL logger : database.py:conn.set_trace()
```

---

## ✅ Checklist Avant Commit

### Sécurité (CRITIQUE)
- [ ] **CSRF** dans CHAQUE formulaire POST (`<input name="csrf_token">`)
- [ ] **ACL vérifiée** avant chaque write (`can_write(client_id)`)
- [ ] **SQL paramètré** (? placeholders, jamais f-string)
- [ ] **Input validé** (`validate_form()`)
- [ ] **Pas de PII en logs** (pwd, email, IP, tokens)

### Code Quality
- [ ] Pas d'erreurs `logger.exception()` au terminal
- [ ] `log_history()` après chaque modification
- [ ] Imports propres (pas de wildcard)
- [ ] Fonctions < 50 lignes ou commentées
- [ ] Pas de variables globales mutables
- [ ] Pas de TODO/FIXME sans ticket

### Dépendances
- [ ] `pip freeze > requirements.txt` si changement
- [ ] Version Python ≥ 3.8 testée
- [ ] Pas de `pip install` direct en prod

### Testing
- [ ] Testée en mode dev (`python app.py`)
- [ ] Testée au niveau ACL (user, user2, admin)
- [ ] Cas limite testés (empty string, None, dates invalides)
- [ ] Responsive design (mobile/desktop si UI)

### Documentation
- [ ] Docstring sur fonctions non-triviales
- [ ] Commentaires pour logique métier
- [ ] README mis à jour si interface change

---

## 🎯 Architecture Décisions

### Pourquoi Pas d'ORM Systématique ?

SQLite brut + `sqlite3` pour :
- ✅ Flexibilité → requêtes ad-hoc faciles
- ✅ Performance → pas de N+1 queries magic
- ✅ Transparence → SQL visible
- ✅ Poids → pas de dépendance supplémentaire

SQLAlchemy (`models.py`) optionnel pour :
- Projets futurs avec Turso/PostgreSQL
- Coexistence peaceful avec SQL brut

### Pourquoi Multi-Client Strict ?

**Isolation par défaut** → plus facile d'accorder que de révoquer.

Pattern :
```python
# ✅ TOUJOURS filtrer par client
conn.execute('SELECT * FROM appareils WHERE client_id=?', (client_id,))

# ❌ JAMAIS fetch all puis filter client-side
conn.execute('SELECT * FROM appareils')  # ← Risque fuite donnée
```

### Pourquoi Rate-Limiting en Mémoire ?

- Léger (pas de DB hit extra)
- Efficace (simple dict en-mémoire)
- Limites : reset au redémarrage, shared threading local

Alternative : `configurations` table + garbage collection.

### Pourquoi Pas Migrations (alembic/yoyo) ?

**État actuel** : schema versionning manuel

Raison :
- Petite équipe → migrations adhoc acceptables
- SQLite local → backups faciles
- Prod = un seul déploiement (exécutable)

Si besoin : implémenter Alembic avec:
```python
# app.py hook
from alembic.config import Config as AlembicConfig
from alembic import command
alembic_cfg = AlembicConfig('alembic.ini')
command.upgrade(alembic_cfg, 'head')
```

---

## 🔗 Ressources

- **Flask Docs** : https://flask.palletsprojects.com/
- **SQLite Docs** : https://www.sqlite.org/docs.html
- **Jinja2 Docs** : https://jinja.palletsprojects.com/
- **Werkzeug** : https://werkzeug.palletsprojects.com/
- **PyInstaller Docs** : https://pyinstaller.org/
- **OWASP Top 10** : https://owasp.org/www-project-top-ten/

---

## ⚡ Quick Reference

### Session & Auth
```python
user = get_auth_user()                    # Dict or None
session['client_id'] = cid                # Store client
session['auth_user_id'] = uid             # Store user
```

### Database
```python
from database import get_db, row_to_dict
conn = get_db()
rows = conn.execute('SELECT * FROM appareils WHERE client_id=?', (cid,)).fetchall()
for row in rows:
    d = row_to_dict(row)  # sqlite3.Row → dict
conn.close()
```

### ACL
```python
from client_helpers import can_write, get_client_access
if not can_write(client_id):
    return 403
acces = get_client_access(client_id)  # 'proprietaire' | 'ecriture' | 'lecture' | None
```

### Audit & Logging
```python
from client_helpers import log_history
log_history('CREATE_APPAREIL', user['login'], client_id, {'id': 123})

import logging
logger = logging.getLogger('parcinfo')
logger.info(f"Device {id} created")
```

### Config Persistée
```python
from config_helpers import cfg_get, cfg_set
theme = cfg_get('theme', 'dark-blue')
cfg_set('theme', 'light')
```

### Validation
```python
from auth_utils import validate_form
errors = validate_form([
    ('nom', 'str', True),
    ('ip', 'ip', False),
    ('email', 'email', False),
], request.form)
```

### Templates
```html
{{ csrf_token }}              {# CSRF token form #}
{{ auth_user.nom }}           {# User connecté #}
{{ appareil.nom | escape }}   {# XSS-safe #}
{{ appareil.documents | fromjson }}  {# Parse JSON #}
```

---

## 📋 Fichiers Clés Quick Lookup

| Besoin | Fichier | Ligne |
|--------|---------|-------|
| Ajouter route | app.py | ~500+ |
| Ajouter DB field | app.py:init_db() | ~270+ |
| Ajouter validation | auth_utils.py | ~130+ |
| Ajouter config | config_helpers.py | ~60+ |
| Ajouter ACL | client_helpers.py | ~40+ |
| Ajouter template | templates/ | - |
| Debug auth | auth_utils.py:get_auth_user() | ~36 |
| Debug SQL | database.py | ~50+ |

---

## 🚀 Déploiement Production (Checklist)

- [ ] `SECRET_KEY` unique + stocké sécurisé
- [ ] HTTPS activé (reverse proxy nginx/apache)
- [ ] Upload whitelist strict (extension + MIME)
- [ ] Rate-limiting renforcé (DB-backed)
- [ ] Logs centralisés (syslog/ELK)
- [ ] Backup automatique BD (cron daily)
- [ ] Monitoring (uptime, erreurs, performance)
- [ ] Admin panel sécurisé (IP whitelist)

---

**Dernier update** : 2026-04-07
**Mainteneur** : ParcInfo Team
**License** : Voir LICENSE.md
