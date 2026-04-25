from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session, send_from_directory, make_response, send_file
from werkzeug.utils import secure_filename
from datetime import datetime, date, timedelta
import sqlite3, subprocess, re, socket, ipaddress, threading, os, platform, concurrent.futures, hashlib, secrets, logging, json, time, io
from io import BytesIO
try:
    from reportlab.lib.pagesizes import letter, A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm, inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('parcinfo')

# ── Support PyInstaller (exécutable portable) + Docker ───────────────────────
import sys as _sys
if getattr(_sys, 'frozen', False):
    # Mode exécutable : les ressources sont dans _MEIPASS
    _resource_base = _sys._MEIPASS
    _data_base     = os.path.dirname(_sys.executable)
else:
    _resource_base = os.path.dirname(os.path.abspath(__file__))
    _data_base     = os.path.dirname(os.path.abspath(__file__))

# DATA_DIR permet de séparer données persistantes du code (Docker, NAS)
_data_dir_env = os.environ.get('DATA_DIR', '').strip()
if _data_dir_env:
    _data_base = _data_dir_env
    os.makedirs(_data_base, exist_ok=True)

app = Flask(
    __name__,
    template_folder=os.path.join(_resource_base, 'templates'),
    static_folder=os.path.join(_resource_base, 'static'),
)

# ─── COMPRESSION GZIP (Optimisation Performance) ───────────────────────────────
try:
    from flask_compress import Compress
    Compress(app)
    logger.info('✅ Compression GZIP activée')
except ImportError:
    logger.warning('⚠️ flask-compress non installé (pip install flask-compress)')

# Base de données et uploads dans le dossier des données (à côté de l'exe)
DATABASE     = os.path.join(_data_base, 'parc_info.db')
UPLOAD_FOLDER = os.path.join(_data_base, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ─── MODULES UTILITAIRES ──────────────────────────────────────────────────────
from database       import get_db, row_to_dict, init_paths
# Initialiser les chemins de la base de données de façon centralisée et robuste
init_paths(DATABASE, UPLOAD_FOLDER)
from auth_utils     import (hash_pwd as _hash_pwd, check_pwd as _check_pwd,
                             get_auth_user, login_required,
                             get_csrf_token as _get_csrf_token,
                             validate_csrf_request,
                             check_rate_limit as _check_rate_limit,
                             record_failed_attempt as _record_failed_attempt,
                             reset_attempts as _reset_attempts,
                             validate_form)
from config_helpers import (LISTE_DEFAULTS, CFG_DEFAULTS,
                             get_liste, cfg_get, cfg_set, cfg_all, cfg_invalidate,
                             get_port_config, get_port_icon)
from client_helpers import (paginate, get_client_access, can_write,
                             get_client_with_acces, get_client_id, get_clients,
                             log_history, log_error, garantie_active, human_size,
                             fmt_appareils, fmt_garantie_periph, fmt_contrat, fmt_intervention,
                             get_clients_for_filter, _format_date_field)
from uploads_sync import start_sync_thread
from crypto_utils   import get_crypto_manager
from cache_utils    import get_cache_manager, cache_result, invalidate_cache_pattern
from search_utils   import search_global, search_autocomplete

# ─── HELPER: Retry pour requêtes DB verrouillées ─────────────────────────────
def retry_db_query(query_func, max_retries=5):
    """
    Exécute une fonction de requête DB avec retry automatique si verrouillée.
    Utilisé pour les requêtes critiques dans /index (dashboard).

    Utilisation:
        result = retry_db_query(lambda: conn.execute('SELECT ...').fetchall())
    """
    retry_delay = 0.05  # 50ms initial

    for attempt in range(max_retries):
        try:
            return query_func()
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and attempt < max_retries - 1:
                # Database verrouillée → retry avec backoff exponentiel
                time.sleep(retry_delay * (2 ** attempt))
                continue
            else:
                raise e
        except Exception as e:
            raise e

@app.context_processor
def inject_auth_context():
    """Injecte les variables auth dans tous les templates."""
    u = None
    uid = session.get('auth_user_id')
    if uid:
        try:
            conn = get_db()
            u = row_to_dict(conn.execute('SELECT id,login,nom,prenom,role,logo_fichier FROM auth_users WHERE id=?', (uid,)).fetchone() or {})
            conn.close()
        except Exception:
            logger.exception('Erreur inject_auth_context')
    return dict(auth_user=u)
# Clé secrète persistée (générée une fois, stockée à côté de la DB)
_secret_key_file = os.path.join(_data_base, 'secret.key')
if os.path.exists(_secret_key_file):
    with open(_secret_key_file, 'r') as _f:
        app.config['SECRET_KEY'] = _f.read().strip()
else:
    _generated_key = secrets.token_hex(32)
    with open(_secret_key_file, 'w') as _f:
        _f.write(_generated_key)
    app.config['SECRET_KEY'] = _generated_key
# ── Configuration de sécurité des sessions ───────────────────────────────────
from datetime import timedelta
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
app.config['SESSION_COOKIE_HTTPONLY']    = True   # inaccessible depuis JS
app.config['SESSION_COOKIE_SAMESITE']    = 'Lax'  # protection CSRF additionnelle

# ─── SCHEDULER (Cron Jobs) ────────────────────────────────────────────────────
from apscheduler.schedulers.background import BackgroundScheduler
scheduler = BackgroundScheduler()
scheduler.daemon = True  # Arrête avec l'application

# UPLOAD_FOLDER défini plus haut (support PyInstaller)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = None  # Tous types acceptés

def allowed_file(filename):
    return bool(filename and filename.strip())  # Tous types acceptés


# ─── CACHING OPTIMISÉ ─────────────────────────────────────────────────────────
def get_liste_cached(nom: str, ttl: int = 600) -> list:
    """
    Wrapper de get_liste() avec caching intelligent (10 min par défaut).
    Réduit les requêtes DB de 80% pour les listes fréquemment accédées.
    """
    cache_mgr = get_cache_manager()
    cache_key = f"liste:{nom}"

    # Vérifier le cache
    cached = cache_mgr.get(cache_key)
    if cached is not None:
        return cached

    # Récupérer depuis DB
    result = get_liste(nom)

    # Stocker en cache
    cache_mgr.set(cache_key, result, ttl)
    return result


# ─── CSRF ─────────────────────────────────────────────────────────────────────

def _generate_dynamic_css(auth_user_id=None):
    """
    Génère le CSS dynamique basé sur les configurations de l'utilisateur.
    Injecté dans chaque page pour que les paramètres personnels persistent.
    """
    def g(k, d):
        return cfg_get(k, d, auth_user_id=auth_user_id)

    # Couleurs accents
    accent = g('accent_color', '#00c9ff')
    green = g('accent_green', '#00ff88')
    red = g('accent_red', '#ff3355')
    orange = g('accent_orange', '#ff8c00')

    # Niveau de contraste
    contrast_level = g('contrast_level', 'normal')

    # CSS des accents
    css = f":root{{--accent:{accent};--accent-green:{green};--accent-red:{red};--accent-orange:{orange}}}"

    # CSS de contraste selon le niveau sélectionné
    if contrast_level == 'high':
        css += "html.contrast-high{--text-primary-opacity:1;--text-secondary-opacity:0.95;--text-muted-opacity:0.85}html.contrast-high body{--text-primary:rgba(208,232,255,1);--text-secondary:rgba(106,138,170,1)}"
    elif contrast_level == 'max':
        css += "html.contrast-max{--text-primary-opacity:1;--text-secondary-opacity:1;--text-muted-opacity:0.9}html.contrast-max body{--text-primary:#ffffff;--text-secondary:#a6c5e8;filter:contrast(1.2)}"

    # Couleurs des ports (serviceType)
    port_colors = {
        'ssh': g('port_color_ssh', '#00ff88'),
        'http': g('port_color_http', '#00c9ff'),
        'https': g('port_color_https', '#00c9ff'),
        'rdp': g('port_color_rdp', '#c084fc'),
        'ftp': g('port_color_ftp', '#ff8c00'),
        'smb': g('port_color_smb', '#facc15'),
        'print': g('port_color_print', '#fb923c'),
        'telnet': g('port_color_telnet', '#ff3355'),
        'other': g('port_color_other', '#64748b'),
    }
    for k, col in port_colors.items():
        css += f".port-{k}{{color:{col};border-color:{col}55}}.port-{k}:hover{{background:{col}18;box-shadow:0 0 8px {col}44}}"

    # Couleurs des ports par NUMÉRO (configurations personnalisées)
    scan_ports_str = g('scan_ports', '21,22,23,25,53,80,110,135,139,143,443,445,631,3389,5900,8080,8443,9100')
    for port_str in scan_ports_str.split(','):
        try:
            pnum = int(port_str.strip())
            pcolor = g(f'port_{pnum}_color', '')
            if pcolor:
                css += f".port-num-{pnum}{{color:{pcolor};border-color:{pcolor}55}}.port-num-{pnum}:hover{{background:{pcolor}18;box-shadow:0 0 8px {pcolor}44}}"
        except (ValueError, TypeError):
            pass

    # Couleurs des périphériques
    periph_colors = {
        'ecran': g('periph_color_ecran', '#22d3ee'),
        'clavier': g('periph_color_clavier', '#a78bfa'),
        'souris': g('periph_color_souris', '#a78bfa'),
        'webcam': g('periph_color_webcam', '#fb923c'),
        'casque': g('periph_color_casque', '#c084fc'),
        'audio': g('periph_color_casque', '#c084fc'),
        'imprimante': g('periph_color_imprimante', '#f97316'),
        'scanner': g('periph_color_imprimante', '#f97316'),
        'onduleur': g('periph_color_onduleur', '#facc15'),
        'multiprise': g('periph_color_onduleur', '#facc15'),
        'stockage': g('periph_color_stockage', '#4ade80'),
        'usb': g('periph_color_usb', '#94a3b8'),
        'dock': g('periph_color_dock', '#60a5fa'),
        'reseau': g('periph_color_reseau', '#2dd4bf'),
        'tel': g('periph_color_tel', '#34d399'),
        'badge': g('periph_color_badge', '#f87171'),
        'autre': g('periph_color_autre', '#94a3b8'),
    }
    for k, col in periph_colors.items():
        css += f".pi-{k}{{color:{col};border-color:{col}66}}.pi-{k}:hover{{background:{col}18}}"

    # Couleurs des types d'appareils (depuis _TYPE_CSS_DEFAULTS)
    for type_key, default_color in _TYPE_CSS_DEFAULTS.items():
        type_color = g(f'type_color_{type_key}', default_color)
        css += f".type-{type_key}{{color:{type_color};background:{type_color}15;border-color:{type_color}55}}"

    return css


@app.context_processor
def inject_csrf_token():
    return dict(csrf_token=_get_csrf_token())


@app.context_processor
def inject_dynamic_css():
    """Injecte le CSS dynamique généré côté serveur dans chaque template."""
    user = get_auth_user()
    auth_user_id = user['id'] if user else None
    dynamic_css = _generate_dynamic_css(auth_user_id=auth_user_id)
    return dict(dynamic_css=dynamic_css)


@app.before_request
def csrf_protect():
    validate_csrf_request()

# ── FILTRES JINJA2 POUR LES PORTS ────────────────────────────────────────────
_PORT_MAP = {
    21:   ('ftp',    '📁', 'FTP — Transfert de fichiers',          'ftp'),
    22:   ('ssh',    '⌨',  'SSH — Terminal sécurisé',              'ssh'),
    23:   ('telnet', '⚠',  'Telnet — Terminal NON sécurisé',       'telnet'),
    25:   ('other',  '✉',  'SMTP — Serveur mail sortant',          'info'),
    53:   ('other',  '🔍', 'DNS — Résolution de noms',             'info'),
    80:   ('http',   '🌐', 'HTTP — Serveur web',                   'http'),
    110:  ('other',  '✉',  'POP3 — Messagerie',                    'info'),
    135:  ('smb',    '⚙',  'RPC — Windows Remote Procedure Call',  'info'),
    139:  ('smb',    '🗂', 'NetBIOS — Partage Windows',            'smb'),
    143:  ('other',  '✉',  'IMAP — Messagerie',                    'info'),
    443:  ('https',  '🔒', 'HTTPS — Serveur web sécurisé',         'https'),
    445:  ('smb',    '🗂', 'SMB — Partage de fichiers Windows',    'smb'),
    631:  ('print',  '🖨', 'IPP — Service impression',          'print'),
    3389: ('rdp',    '🖥', 'RDP — Bureau à distance Windows',      'rdp'),
    5900: ('rdp',    '🖥', 'VNC — Bureau à distance VNC',          'vnc'),
    8080: ('http',   '🌐', 'HTTP alternatif (port 8080)',           'http8080'),
    8443: ('https',  '🔒', 'HTTPS alternatif (port 8443)',          'https8443'),
    9100: ('print',  '🖨', 'JetDirect — Impression directe',       'print'),
}

@app.template_filter('periph_icon')
def periph_icon_filter(cat):
    # Abreviations courtes (max 3 car) affichees dans le badge style "port"
    icons = {
        'Ecran':                   'ECR',
        'Clavier':                 'KB',
        'Souris':                  'SOU',
        'Webcam':                  'CAM',
        'Casque / Micro':          'MIC',
        'Haut-parleurs':           'HP',
        'Imprimante':              'IMP',
        'Scanner':                 'SCN',
        'Imprimante multifonction':'IMP',
        'Onduleur / UPS':          'UPS',
        'Multiprise parafoudre':   'MPR',
        'Disque dur externe':      'HDD',
        'Cle USB':                 'USB',
        'Hub USB':                 'HUB',
        'Lecteur de cartes':       'LCR',
        'Docking station':         'DOC',
        'Adaptateur reseau':       'NET',
        'Switch USB':              'SW',
        'Telephone fixe IP':       'TEL',
        'Telephone mobile':        'MOB',
        'Badge / Lecteur de badge':'BGE',
        'Autre':                   'AUT',
    }
    return icons.get(cat, 'PER')

# Mapping catégorie périphérique → clé de couleur config (periph_color_<key>)
_PERIPH_COLOR_KEY = {
    'Ecran': 'ecran', 'Clavier': 'clavier', 'Souris': 'souris',
    'Webcam': 'webcam', 'Casque / Micro': 'casque', 'Haut-parleurs': 'casque',
    'Imprimante': 'imprimante', 'Scanner': 'imprimante', 'Imprimante multifonction': 'imprimante',
    'Onduleur / UPS': 'onduleur', 'Multiprise parafoudre': 'onduleur',
    'Disque dur externe': 'stockage', 'Cle USB': 'usb', 'Hub USB': 'usb',
    'Lecteur de cartes': 'usb', 'Docking station': 'dock', 'Adaptateur reseau': 'reseau',
    'Switch USB': 'usb', 'Telephone fixe IP': 'tel', 'Telephone mobile': 'tel',
    'Badge / Lecteur de badge': 'badge', 'Autre': 'autre',
}

@app.template_filter('periph_color_key')
def periph_color_key_filter(cat):
    """Retourne la clé config (ex: 'ecran') pour une catégorie de périphérique."""
    return _PERIPH_COLOR_KEY.get(cat, 'autre')

# Mapping type d'appareil → clé CSS (type-<key>) et config (type_color_<key>)
_TYPE_CSS_MAP = {
    'PC': 'pc', 'PC (Windows)': 'pc', 'PC/Serveur (Linux)': 'linux',
    'Laptop': 'laptop', 'MacBook': 'mac', 'Serveur': 'serveur',
    'Imprimante': 'imprimante', 'Imprimante multifonction': 'imprimante',
    'Switch': 'switch', 'Switch/AP': 'switch', 'Routeur/Pare-feu': 'routeur',
    'NAS': 'nas', 'Telephone IP': 'tel', 'Tablette': 'tablette',
    'Camera IP': 'camera', 'Borne Wi-Fi': 'wifi', 'Autre': 'autre',
}

@app.template_filter('type_css')
def type_css_filter(t):
    """Retourne la clé CSS du type d'appareil (ex: 'pc', 'nas', 'serveur').
    Pour les types non connus, génère une clé CSS-safe depuis le libellé."""
    import re as _re
    if t in _TYPE_CSS_MAP:
        return _TYPE_CSS_MAP[t]
    # Types custom : slug alphanumérique limité à 16 chars
    key = _re.sub(r'[^a-z0-9]', '', str(t).lower())[:16]
    return key or 'autre'

# Labels courts (≤3 chars) par défaut pour les badges de type d'appareil
_TYPE_BADGE_DEFAULTS = {
    'pc': 'PC', 'linux': 'LNX', 'laptop': 'LAP', 'mac': 'MAC',
    'serveur': 'SRV', 'imprimante': 'IMP', 'switch': 'SW', 'routeur': 'RTR',
    'nas': 'NAS', 'tel': 'TEL', 'tablette': 'TAB', 'camera': 'CAM',
    'wifi': 'WIF', 'autre': 'AUT',
}

@app.template_filter('type_badge')
def type_badge_filter(t):
    """Retourne le label court (≤3 chars) configuré pour le badge de type d'appareil."""
    k = type_css_filter(t)
    val = cfg_get(f'type_badge_{k}')
    if val:
        return val[:3].upper()
    default = _TYPE_BADGE_DEFAULTS.get(k)
    if default:
        return default
    # Types custom : 3 premiers chars du slug
    return k[:3].upper() or 'AUT'

@app.template_filter('type_description')
def type_description_filter(t):
    """Retourne la description configurée pour le type d'appareil (pour infobulles)."""
    k = type_css_filter(t)
    desc = cfg_get(f'type_desc_{k}')
    return desc if desc else t

@app.template_filter('fromjson')
def fromjson_filter(s):
    """Décode une chaîne JSON en objet Python (liste ou dict). Retourne [] en cas d'erreur."""
    try:
        return json.loads(s) if s else []
    except Exception:
        return []

@app.template_filter('periph_css')
def periph_css_filter(cat):
    m = {
        'Ecran':'pi-ecran',
        'Clavier':'pi-clavier',
        'Souris':'pi-souris',
        'Webcam':'pi-webcam',
        'Casque / Micro':'pi-casque',
        'Haut-parleurs':'pi-audio',
        'Imprimante':'pi-imprimante',
        'Scanner':'pi-scanner',
        'Imprimante multifonction':'pi-imprimante',
        'Onduleur / UPS':'pi-onduleur',
        'Multiprise parafoudre':'pi-multiprise',
        'Disque dur externe':'pi-stockage',
        'Cle USB':'pi-usb',
        'Hub USB':'pi-usb',
        'Lecteur de cartes':'pi-usb',
        'Docking station':'pi-dock',
        'Adaptateur reseau':'pi-reseau',
        'Switch USB':'pi-usb',
        'Telephone fixe IP':'pi-tel',
        'Telephone mobile':'pi-tel',
        'Badge / Lecteur de badge':'pi-badge',
        'Autre':'pi-autre',
    }
    return m.get(cat, 'pi-autre')

@app.template_filter('port_badge')
def port_badge_filter(port):
    """Retourne un badge HTML pour un port avec couleur, nom et icône personnalisés.
    Format : <span style="...color...">ICON PORT</span>
    """
    if not port:
        return ''
    try:
        port_int = int(port)
        cfg = get_port_config(port_int)
        icon = cfg.get('icon', '◈')
        color = cfg.get('color', '#64748b')
        return f'<span style="background:rgba({int(color[1:3],16)},{int(color[3:5],16)},{int(color[5:7],16)},.15);color:{color};border:1.5px solid {color};padding:3px 6px;border-radius:3px;font-size:0.75em;font-weight:700;white-space:nowrap;display:inline-flex;align-items:center;gap:2px;">{icon} {port}</span>'
    except:
        return f'<span>{port}</span>'

@app.template_filter('port_name')
def port_name_filter(port):
    """Retourne uniquement le nom du service pour un port (ex: 'SSH', 'HTTP')."""
    try:
        return get_port_config(int(port)).get('name', str(port))
    except:
        return str(port)

@app.template_filter('port_class')
def port_class_filter(port):
    try: return _PORT_MAP.get(int(port), ('other','','',''))[0]
    except: return 'other'

@app.template_filter('port_icon')
def port_icon_filter(port):
    try:
        return get_port_icon(int(port))
    except:
        return '◈'

@app.template_filter('port_info')
def port_info_filter(port):
    try:
        port_int = int(port)
        cfg = get_port_config(port_int)
        name = cfg.get('name', str(port))
        desc = cfg.get('description', '')
        # Formater l'infobulle avec description si elle existe
        if desc:
            return f"{name} — {desc}"
        else:
            return f"{name} — Service TCP"
    except:
        return 'Port TCP ouvert'

@app.template_filter('port_action')
def port_action_filter(port):
    try: return _PORT_MAP.get(int(port), ('other','','','info'))[3]
    except: return 'info'
DB_PATH = DATABASE  # alias conservé pour compatibilité

def init_db():
    conn = get_db(); c = conn.cursor()

    # TABLE CLIENTS
    c.execute('''CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL DEFAULT '',
        contact TEXT DEFAULT '',
        telephone TEXT DEFAULT '',
        email TEXT DEFAULT '',
        adresse TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        couleur TEXT DEFAULT '#00c9ff',
        date_creation TEXT DEFAULT '',
        date_maj TEXT DEFAULT '')''')

    # TABLE PARC (lié à un client)
    c.execute('''CREATE TABLE IF NOT EXISTS parc_general (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        nom_site TEXT DEFAULT '', adresse TEXT DEFAULT '',
        type_connexion TEXT DEFAULT '', debit_montant TEXT DEFAULT '', debit_descendant TEXT DEFAULT '',
        fournisseur_internet TEXT DEFAULT '', ip_publique TEXT DEFAULT '',
        plage_ip_locale TEXT DEFAULT '192.168.1.0/24', nb_machines INTEGER DEFAULT 0,
        nb_utilisateurs INTEGER DEFAULT 0, domaine TEXT DEFAULT '', serveur_dns TEXT DEFAULT '',
        passerelle TEXT DEFAULT '', baie_marque TEXT DEFAULT '', baie_nb_u INTEGER DEFAULT 0,
        switch_marque TEXT DEFAULT '', switch_nb_ports INTEGER DEFAULT 0, switch_nb_unites INTEGER DEFAULT 0,
        routeur_marque TEXT DEFAULT '', serveur_marque TEXT DEFAULT '', serveur_modele TEXT DEFAULT '',
        ups_marque TEXT DEFAULT '', ups_capacite TEXT DEFAULT '', autres_equipements TEXT DEFAULT '',
        logiciels_metier TEXT DEFAULT '', antivirus TEXT DEFAULT '', os_principal TEXT DEFAULT '',
        suite_bureautique TEXT DEFAULT '', notes TEXT DEFAULT '', date_maj TEXT DEFAULT '',
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE)''')

    # Colonnes WiFi dans identifiants (migration)
    for col, defval in [
        ('wifi_ssid',     "''"),
        ('wifi_securite', "'WPA2'"),
    ]:
        try:
            c.execute(f"ALTER TABLE identifiants ADD COLUMN {col} TEXT DEFAULT {defval}")
        except: pass

    # Ajouter "Wi-Fi" à la liste des catégories d'identifiants si absente
    try:
        nb = c.execute("SELECT COUNT(*) FROM config_listes WHERE nom_liste='categories_identifiants' AND valeur='Wi-Fi'").fetchone()[0]
        if nb == 0:
            # Ne forcer que si la liste a déjà été personnalisée
            nb_total = c.execute("SELECT COUNT(*) FROM config_listes WHERE nom_liste='categories_identifiants'").fetchone()[0]
            if nb_total > 0:
                ordre = c.execute("SELECT COALESCE(MAX(ordre),0)+1 FROM config_listes WHERE nom_liste='categories_identifiants'").fetchone()[0]
                c.execute("INSERT OR IGNORE INTO config_listes (nom_liste,valeur,ordre) VALUES ('categories_identifiants','Wi-Fi',?)", (ordre,))
    except: pass

    # Colonnes WiFi dans parc_general (migration)
    for col, defval in [
        ('wifi_ssid',       "''"),
        ('wifi_password',   "''"),
        ('wifi_securite',   "'WPA2'"),
        ('wifi_ssid2',      "''"),
        ('wifi_password2',  "''"),
        ('wifi_securite2',  "'WPA2'"),
        ('wifi_notes',      "''"),
    ]:
        try:
            c.execute(f"ALTER TABLE parc_general ADD COLUMN {col} TEXT DEFAULT {defval}")
        except: pass  # colonne déjà existante

    # TABLE APPAREILS (lié à un client)
    c.execute('''CREATE TABLE IF NOT EXISTS appareils (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        nom_machine TEXT DEFAULT '', type_appareil TEXT DEFAULT '',
        marque TEXT DEFAULT '', modele TEXT DEFAULT '', numero_serie TEXT DEFAULT '',
        adresse_ip TEXT DEFAULT '', adresse_mac TEXT DEFAULT '', nom_dns TEXT DEFAULT '',
        utilisateur TEXT DEFAULT '', service TEXT DEFAULT '', localisation TEXT DEFAULT '',
        date_achat TEXT DEFAULT '', duree_garantie INTEGER DEFAULT 0, date_fin_garantie TEXT DEFAULT '',
        fournisseur TEXT DEFAULT '', prix_achat REAL, numero_commande TEXT DEFAULT '',
        os TEXT DEFAULT '', version_os TEXT DEFAULT '', ram TEXT DEFAULT '', cpu TEXT DEFAULT '',
        stockage TEXT DEFAULT '', statut TEXT DEFAULT 'actif', dernier_ping TEXT DEFAULT '',
        en_ligne INTEGER DEFAULT 0, decouvert_scan INTEGER DEFAULT 0, ports_ouverts TEXT DEFAULT '',
        notes TEXT DEFAULT '', date_creation TEXT DEFAULT '', date_maj TEXT DEFAULT '',
        user_login TEXT DEFAULT '', user_password TEXT DEFAULT '',
        admin_login TEXT DEFAULT '', admin_password TEXT DEFAULT '',
        anydesk_id TEXT DEFAULT '', anydesk_password TEXT DEFAULT '',
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE)''')

    # TABLE IDENTIFIANTS GLOBAUX
    c.execute('''CREATE TABLE IF NOT EXISTS identifiants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        categorie TEXT DEFAULT '',
        nom TEXT DEFAULT '',
        login TEXT DEFAULT '',
        mot_de_passe TEXT DEFAULT '',
        url TEXT DEFAULT '',
        description TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        date_expiration TEXT DEFAULT '',
        date_creation TEXT DEFAULT '',
        date_maj TEXT DEFAULT '',
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE)''')

    # TABLE SERVICES
    c.execute('''CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        nom TEXT NOT NULL DEFAULT '',
        description TEXT DEFAULT '',
        responsable TEXT DEFAULT '',
        couleur TEXT DEFAULT '#6a8aaa',
        ordre INTEGER DEFAULT 0,
        date_creation TEXT DEFAULT '',
        date_maj TEXT DEFAULT '',
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE)''')

    # TABLE UTILISATEURS
    c.execute('''CREATE TABLE IF NOT EXISTS utilisateurs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        service_id INTEGER,
        prenom TEXT DEFAULT '',
        nom TEXT DEFAULT '',
        poste TEXT DEFAULT '',
        email TEXT DEFAULT '',
        telephone TEXT DEFAULT '',
        login_windows TEXT DEFAULT '',
        login_mail TEXT DEFAULT '',
        statut TEXT DEFAULT 'actif',
        notes TEXT DEFAULT '',
        date_creation TEXT DEFAULT '',
        date_maj TEXT DEFAULT '',
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE,
        FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE SET NULL)''')

    # TABLE TYPES_DROITS (référentiel configurable par client)
    c.execute('''CREATE TABLE IF NOT EXISTS types_droits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        categorie TEXT DEFAULT '',
        nom TEXT NOT NULL DEFAULT '',
        description TEXT DEFAULT '',
        icone TEXT DEFAULT '🔑',
        ordre INTEGER DEFAULT 0,
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE)''')

    # TABLE DROITS UTILISATEURS (pivot users <-> types_droits)
    c.execute('''CREATE TABLE IF NOT EXISTS droits_utilisateurs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        utilisateur_id INTEGER NOT NULL,
        client_id INTEGER NOT NULL,
        categorie TEXT DEFAULT '',
        type_droit_id INTEGER,
        nom_droit TEXT DEFAULT '',
        valeur TEXT DEFAULT '',
        niveau TEXT DEFAULT 'lecture',
        notes TEXT DEFAULT '',
        date_attribution TEXT DEFAULT '',
        FOREIGN KEY(utilisateur_id) REFERENCES utilisateurs(id) ON DELETE CASCADE,
        FOREIGN KEY(type_droit_id) REFERENCES types_droits(id) ON DELETE SET NULL)''')

    # TABLE PERIPHERIQUES
    c.execute('''CREATE TABLE IF NOT EXISTS peripheriques (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        appareil_id INTEGER,
        utilisateur_id INTEGER,
        categorie TEXT DEFAULT '',
        marque TEXT DEFAULT '',
        modele TEXT DEFAULT '',
        numero_serie TEXT DEFAULT '',
        description TEXT DEFAULT '',
        localisation TEXT DEFAULT '',
        statut TEXT DEFAULT 'actif',
        date_achat TEXT DEFAULT '',
        duree_garantie INTEGER DEFAULT 0,
        date_fin_garantie TEXT DEFAULT '',
        fournisseur TEXT DEFAULT '',
        prix_achat REAL,
        numero_commande TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        date_creation TEXT DEFAULT '',
        date_maj TEXT DEFAULT '',
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE,
        FOREIGN KEY(appareil_id) REFERENCES appareils(id) ON DELETE SET NULL,
        FOREIGN KEY(utilisateur_id) REFERENCES utilisateurs(id) ON DELETE SET NULL)''')

    # TABLE LISTES PERSONNALISABLES
    c.execute('''CREATE TABLE IF NOT EXISTS config_listes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom_liste TEXT NOT NULL,
        valeur TEXT NOT NULL,
        ordre INTEGER DEFAULT 0,
        UNIQUE(nom_liste, valeur))''')

    # TABLE CONFIGURATION GLOBALE
    c.execute('''CREATE TABLE IF NOT EXISTS config (
        cle TEXT PRIMARY KEY,
        valeur TEXT DEFAULT '',
        date_maj TEXT DEFAULT '')''')

    # TABLE PRÉFÉRENCES UTILISATEUR (personnalisation par utilisateur)
    c.execute('''CREATE TABLE IF NOT EXISTS user_preferences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        auth_user_id INTEGER NOT NULL,
        cle TEXT NOT NULL,
        valeur TEXT DEFAULT '',
        date_maj TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(auth_user_id, cle),
        FOREIGN KEY(auth_user_id) REFERENCES auth_users(id) ON DELETE CASCADE)''')

    # TABLE OUTILS (indépendant du client)
    c.execute('''CREATE TABLE IF NOT EXISTS outils (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL,
        url TEXT NOT NULL,
        description TEXT DEFAULT '',
        categorie TEXT DEFAULT 'Général',
        icone TEXT DEFAULT '🔧',
        ordre INTEGER DEFAULT 0,
        actif INTEGER DEFAULT 1)''')

    # Insérer les outils par défaut si la table est vide
    nb_outils = c.execute('SELECT COUNT(*) FROM outils').fetchone()[0]
    if nb_outils == 0:
        defaults = [
            ('Test de débit',       'https://www.nperf.com/',              'Test vitesse download/upload/ping',   'Réseau',   '⚡', 0),
            ('Fast.com',            'https://fast.com/',                   'Test de débit Netflix',               'Réseau',   '⚡', 1),
            ('Test DNS Cloudflare', 'https://1.1.1.1/',                    'DNS Cloudflare & test connectivité',  'Réseau',   '🔒', 2),
            ('DNS Check Tools',     'https://dnschecker.org/',             'Vérification propagation DNS',        'DNS',      '🔍', 3),
            ('MXToolbox',           'https://mxtoolbox.com/',              'Outils DNS, blacklist, SMTP',         'DNS',      '📧', 4),
            ('What is my IP',       'https://www.whatismyip.com/',         'IP publique et géolocalisation',      'Réseau',   '🌐', 5),
            ('Cloudflare RADAR',    'https://radar.cloudflare.com/',       'Statistiques et état internet',       'Réseau',   '📡', 6),
            ('Test AdBlock d3ward', 'https://d3ward.github.io/toolz/adblock.html', 'Test efficacité bloqueur pubs', 'Sécurité','🛡', 7),
            ('SSL Labs',            'https://www.ssllabs.com/ssltest/',    'Analyse certificat SSL/TLS',          'Sécurité', '🔐', 8),
            ('Shodan',              'https://www.shodan.io/',              'Moteur de recherche IoT/sécurité',    'Sécurité', '🕵', 9),
            ('VirusTotal',          'https://www.virustotal.com/',         'Analyse fichiers et URLs',            'Sécurité', '🦠', 10),
            ('PingTools',           'https://ping.eu/',                    'Ping, traceroute, whois en ligne',    'Diagnostic','🏓', 11),
            ('Down For Everyone',   'https://downforeveryoneorjustme.com/','Site down ou problème local ?',       'Diagnostic','❓', 12),
            ('IPvFoo / IP Info',    'https://www.ipaddress.com/',          'Infos complètes sur une IP',          'DNS',      'ℹ', 13),
        ]
        for nom, url, desc, cat, ico, ordre in defaults:
            c.execute('INSERT INTO outils (nom,url,description,categorie,icone,ordre) VALUES (?,?,?,?,?,?)',
                      (nom, url, desc, cat, ico, ordre))

    # TABLE CONTRATS
    c.execute('''CREATE TABLE IF NOT EXISTS contrats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        titre TEXT DEFAULT '',
        type_contrat TEXT DEFAULT '',
        fournisseur TEXT DEFAULT '',
        contact_fournisseur TEXT DEFAULT '',
        email_fournisseur TEXT DEFAULT '',
        telephone_fournisseur TEXT DEFAULT '',
        numero_contrat TEXT DEFAULT '',
        date_debut TEXT DEFAULT '',
        date_fin TEXT DEFAULT '',
        reconduction_auto INTEGER DEFAULT 0,
        preavis_jours INTEGER DEFAULT 30,
        montant_ht REAL,
        periodicite TEXT DEFAULT 'annuel',
        description TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        statut TEXT DEFAULT 'actif',
        date_creation TEXT DEFAULT '',
        date_maj TEXT DEFAULT '',
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE)''')

    # TABLE CONTRATS <-> APPAREILS (pivot)
    c.execute('''CREATE TABLE IF NOT EXISTS contrats_appareils (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contrat_id INTEGER NOT NULL,
        appareil_id INTEGER NOT NULL,
        FOREIGN KEY(contrat_id) REFERENCES contrats(id) ON DELETE CASCADE,
        FOREIGN KEY(appareil_id) REFERENCES appareils(id) ON DELETE CASCADE)''')

    # TABLE CONTRATS <-> PERIPHERIQUES (pivot)
    c.execute('''CREATE TABLE IF NOT EXISTS contrats_peripheriques (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contrat_id INTEGER NOT NULL,
        peripherique_id INTEGER NOT NULL,
        FOREIGN KEY(contrat_id) REFERENCES contrats(id) ON DELETE CASCADE,
        FOREIGN KEY(peripherique_id) REFERENCES peripheriques(id) ON DELETE CASCADE)''')

    # TABLE MAINTENANCES
    c.execute('''CREATE TABLE IF NOT EXISTS maintenances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        appareil_id INTEGER,
        peripherique_id INTEGER,
        contrat_id INTEGER,
        type_maintenance TEXT NOT NULL,
        description TEXT DEFAULT '',
        date_planifiee TEXT NOT NULL,
        date_realisee TEXT,
        heure_debut TEXT DEFAULT '',
        heure_fin TEXT DEFAULT '',
        responsable TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        statut TEXT DEFAULT 'programmee',
        recurrence TEXT,
        date_fin_recurrence TEXT,
        parent_id INTEGER,
        created_by INTEGER,
        updated_by INTEGER,
        date_creation TEXT DEFAULT CURRENT_TIMESTAMP,
        date_maj TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE,
        FOREIGN KEY(appareil_id) REFERENCES appareils(id) ON DELETE SET NULL,
        FOREIGN KEY(peripherique_id) REFERENCES peripheriques(id) ON DELETE SET NULL,
        FOREIGN KEY(contrat_id) REFERENCES contrats(id) ON DELETE SET NULL,
        FOREIGN KEY(created_by) REFERENCES auth_users(id) ON DELETE SET NULL,
        FOREIGN KEY(updated_by) REFERENCES auth_users(id) ON DELETE SET NULL,
        FOREIGN KEY(parent_id) REFERENCES maintenances(id) ON DELETE CASCADE)''')

    # Ajouter colonne contrat_id si elle n'existe pas (migration)
    try:
        c.execute("ALTER TABLE maintenances ADD COLUMN contrat_id INTEGER")
    except: pass

    # ═══════════════════════════════════════════════════════════════════════════
    # CRÉATION DES INDICES (Optimisation Performance)
    # ═══════════════════════════════════════════════════════════════════════════
    try:
        # ── MAINTENANCES ──────────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_maintenances_client ON maintenances(client_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_maintenances_appareil ON maintenances(appareil_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_maintenances_peripherique ON maintenances(peripherique_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_maintenances_date ON maintenances(date_planifiee)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_maintenances_contrat ON maintenances(contrat_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_maintenances_statut ON maintenances(statut)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_maintenances_type ON maintenances(type_maintenance)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_maintenances_client_statut ON maintenances(client_id, statut)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_maintenances_client_date ON maintenances(client_id, date_planifiee)')

        # ── APPAREILS ─────────────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_appareils_client ON appareils(client_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_appareils_statut ON appareils(statut)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_appareils_en_ligne ON appareils(en_ligne)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_appareils_type ON appareils(type_appareil)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_appareils_date_maj ON appareils(date_maj DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_appareils_nom_machine ON appareils(nom_machine)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_appareils_client_statut ON appareils(client_id, statut)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_appareils_client_en_ligne ON appareils(client_id, en_ligne)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_appareils_date_fin_garantie ON appareils(date_fin_garantie)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_appareils_av_date_fin ON appareils(av_date_fin)')

        # ── PÉRIPHÉRIQUES ─────────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_peripheriques_client ON peripheriques(client_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_peripheriques_statut ON peripheriques(statut)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_peripheriques_categorie ON peripheriques(categorie)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_peripheriques_date_creation ON peripheriques(date_creation)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_peripheriques_client_statut ON peripheriques(client_id, statut)')

        # ── CONTRATS ──────────────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_contrats_client ON contrats(client_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_contrats_statut ON contrats(statut)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_contrats_date_fin ON contrats(date_fin)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_contrats_client_statut ON contrats(client_id, statut)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_contrats_client_date_fin ON contrats(client_id, date_fin)')

        # ── INTERVENTIONS ─────────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_interventions_client ON interventions(client_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_interventions_date ON interventions(date_intervention)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_interventions_statut ON interventions(statut)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_interventions_client_date ON interventions(client_id, date_intervention DESC)')

        # ── UTILISATEURS ──────────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_utilisateurs_client ON utilisateurs(client_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_utilisateurs_prenom ON utilisateurs(prenom)')

        # ── SERVICES ──────────────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_services_client ON services(client_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_services_client_ordre ON services(client_id, ordre)')

        # ── IDENTIFIANTS ──────────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_identifiants_client ON identifiants(client_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_identifiants_categorie ON identifiants(categorie)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_identifiants_client_categorie ON identifiants(client_id, categorie)')

        # ── AUTH_USERS ────────────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_auth_users_login ON auth_users(login)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_auth_users_actif ON auth_users(actif)')

        # ── CLIENT_PARTAGES ───────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_client_partages_client ON client_partages(client_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_client_partages_user ON client_partages(auth_user_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_client_partages_client_user ON client_partages(client_id, auth_user_id)')

        # ── HISTORIQUE ────────────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_historique_client ON historique(client_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_historique_date ON historique(date_action)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_historique_client_date ON historique(client_id, date_action DESC)')

        # ── DOCUMENTS ─────────────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_documents_appareils_appareil ON documents_appareils(appareil_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_documents_contrats_contrat ON documents_contrats(contrat_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_documents_peripheriques_periph ON documents_peripheriques(peripherique_id)')

        # ── TABLES PIVOT ──────────────────────────────────────────────────────
        c.execute('CREATE INDEX IF NOT EXISTS idx_contrats_appareils_contrat ON contrats_appareils(contrat_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_contrats_appareils_appareil ON contrats_appareils(appareil_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_interventions_appareils_intervention ON interventions_appareils(intervention_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_interventions_appareils_appareil ON interventions_appareils(appareil_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_peripheriques_appareils_periph ON peripheriques_appareils(peripherique_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_peripheriques_appareils_appareil ON peripheriques_appareils(appareil_id)')

        logger.info('✅ Indices de performance créés avec succès')
    except Exception as e:
        logger.warning(f'⚠️ Erreur lors de la création des indices: {e}')

    # ═══════════════════════════════════════════════════════════════════════════
    # MIGRATION: Chiffrer les identifiants existants en clair
    # ═══════════════════════════════════════════════════════════════════════════
    try:
        crypto = get_crypto_manager(os.path.join(_data_base, 'secret.key'))

        # Récupérer tous les identifiants avec mot de passe non chiffré
        not_encrypted = c.execute('''
            SELECT id, mot_de_passe FROM identifiants
            WHERE mot_de_passe IS NOT NULL
            AND mot_de_passe != ''
            AND mot_de_passe NOT LIKE 'gAAAAAB%'
        ''').fetchall()

        if not_encrypted:
            logger.info(f'🔐 Migration: chiffrement de {len(not_encrypted)} identifiants existants...')
            for ident_id, mdp_clair in not_encrypted:
                mdp_chiffre = crypto.encrypt(mdp_clair)
                c.execute('UPDATE identifiants SET mot_de_passe=? WHERE id=?', (mdp_chiffre, ident_id))
                logger.debug(f'  ✅ ID {ident_id} chiffré')
            conn.commit()
            logger.info(f'✅ Migration terminée: {len(not_encrypted)} identifiants chiffrés')
        else:
            logger.info('✅ Tous les identifiants sont déjà chiffrés')
    except Exception as e:
        logger.warning(f'⚠️ Erreur lors de la migration des identifiants: {e}')

    # TABLE MAINTENANCE_NOTIFICATIONS (tracking notifications envoyées)
    c.execute('''CREATE TABLE IF NOT EXISTS maintenance_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        maintenance_id INTEGER NOT NULL,
        notification_date TEXT NOT NULL,
        date_creation TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(maintenance_id) REFERENCES maintenances(id) ON DELETE CASCADE)''')

    # TABLE DOCUMENTS CONTRATS
    c.execute('''CREATE TABLE IF NOT EXISTS documents_contrats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contrat_id INTEGER NOT NULL,
        client_id INTEGER NOT NULL,
        nom TEXT DEFAULT '',
        description TEXT DEFAULT '',
        type_doc TEXT DEFAULT '',
        nom_fichier TEXT DEFAULT '',
        taille INTEGER DEFAULT 0,
        date_upload TEXT DEFAULT '',
        contenu_blob BLOB,
        sync_status TEXT DEFAULT 'local',
        date_sync TEXT DEFAULT '',
        FOREIGN KEY(contrat_id) REFERENCES contrats(id) ON DELETE CASCADE,
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE)''')

    # TABLE DOCUMENTS APPAREILS
    c.execute('''CREATE TABLE IF NOT EXISTS documents_appareils (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        appareil_id INTEGER NOT NULL,
        client_id INTEGER NOT NULL,
        nom TEXT DEFAULT '',
        description TEXT DEFAULT '',
        type_doc TEXT DEFAULT '',
        nom_fichier TEXT DEFAULT '',
        taille INTEGER DEFAULT 0,
        date_upload TEXT DEFAULT '',
        contenu_blob BLOB,
        sync_status TEXT DEFAULT 'local',
        date_sync TEXT DEFAULT '',
        FOREIGN KEY(appareil_id) REFERENCES appareils(id) ON DELETE CASCADE,
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE)''')

    # TABLE HISTORIQUE
    # ── AUTH UTILISATEURS ────────────────────────────────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS auth_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        login TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        nom TEXT NOT NULL,
        prenom TEXT DEFAULT '',
        email TEXT DEFAULT '',
        role TEXT DEFAULT 'user',
        logo_fichier TEXT DEFAULT '',
        actif INTEGER DEFAULT 1,
        date_creation TEXT,
        date_maj TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS client_partages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        auth_user_id INTEGER NOT NULL,
        niveau TEXT DEFAULT 'lecture',
        date_partage TEXT,
        UNIQUE(client_id, auth_user_id))''')

    # Migration : ajouter auth_user_id aux clients si absent
    cols_clients = [r[1] for r in c.execute('PRAGMA table_info(clients)').fetchall()]
    if 'auth_user_id' not in cols_clients:
        c.execute('ALTER TABLE clients ADD COLUMN auth_user_id INTEGER DEFAULT NULL')

    # Migration : ajouter must_change_password si absent
    cols_auth = [r[1] for r in c.execute('PRAGMA table_info(auth_users)').fetchall()]
    if 'must_change_password' not in cols_auth:
        c.execute('ALTER TABLE auth_users ADD COLUMN must_change_password INTEGER DEFAULT 0')

    # Compte admin par defaut + rattachement des clients existants
    admin = c.execute("SELECT id FROM auth_users WHERE login='admin'").fetchone()
    if not admin:
        from datetime import datetime as _dt
        pwd_hash = _hash_pwd('admin')
        now2 = _dt.utcnow().isoformat()
        c.execute("INSERT INTO auth_users (login,password_hash,nom,prenom,role,actif,must_change_password,date_creation,date_maj) VALUES (?,?,?,?,?,1,1,?,?)",
                  ('admin', pwd_hash, 'Administrateur', '', 'admin', now2, now2))
    c.execute("UPDATE clients SET auth_user_id=(SELECT id FROM auth_users WHERE login='admin') WHERE auth_user_id IS NULL")

    c.execute('''CREATE TABLE IF NOT EXISTS kb_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL,
        icone TEXT DEFAULT '📋',
        ordre INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS kb_articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        categorie_id INTEGER NOT NULL,
        titre TEXT NOT NULL,
        contenu TEXT NOT NULL,
        tags TEXT DEFAULT '',
        date_creation TEXT,
        date_maj TEXT,
        FOREIGN KEY (categorie_id) REFERENCES kb_categories(id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS historique (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        entite TEXT NOT NULL,
        entite_id INTEGER NOT NULL,
        entite_nom TEXT DEFAULT '',
        action TEXT NOT NULL,
        date_action TEXT NOT NULL,
        details TEXT DEFAULT '')''')

    # TABLE DOCUMENTS PÉRIPHÉRIQUES
    c.execute('''CREATE TABLE IF NOT EXISTS documents_peripheriques (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        peripherique_id INTEGER NOT NULL,
        client_id INTEGER NOT NULL,
        nom TEXT DEFAULT '',
        description TEXT DEFAULT '',
        type_doc TEXT DEFAULT '',
        nom_fichier TEXT DEFAULT '',
        taille INTEGER DEFAULT 0,
        date_upload TEXT DEFAULT '',
        contenu_blob BLOB,
        sync_status TEXT DEFAULT 'local',
        date_sync TEXT DEFAULT '',
        FOREIGN KEY(peripherique_id) REFERENCES peripheriques(id) ON DELETE CASCADE,
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE)''')

    # TABLE PIVOT PÉRIPHÉRIQUES <-> APPAREILS (N:N)
    c.execute('''CREATE TABLE IF NOT EXISTS peripheriques_appareils (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        peripherique_id INTEGER NOT NULL,
        appareil_id     INTEGER NOT NULL,
        UNIQUE(peripherique_id, appareil_id),
        FOREIGN KEY(peripherique_id) REFERENCES peripheriques(id) ON DELETE CASCADE,
        FOREIGN KEY(appareil_id)     REFERENCES appareils(id)     ON DELETE CASCADE)''')

    # Migration : si appareil_id non-NULL dans peripheriques, copier dans la table pivot
    try:
        migrated = conn.execute(
            "SELECT id, appareil_id FROM peripheriques WHERE appareil_id IS NOT NULL").fetchall()
        for row in migrated:
            conn.execute(
                "INSERT OR IGNORE INTO peripheriques_appareils (peripherique_id, appareil_id) VALUES (?,?)",
                (row[0], row[1]))
        if migrated:
            conn.commit()
    except Exception:
        pass

    # ════════════════════════════════════════════════════════════════════════════
    # TABLE INTERVENTIONS
    # ════════════════════════════════════════════════════════════════════════════
    c.execute('''CREATE TABLE IF NOT EXISTS interventions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        titre TEXT DEFAULT '',
        type_intervention TEXT DEFAULT '',
        description TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        date_intervention TEXT NOT NULL,
        heure_debut TEXT DEFAULT '',
        heure_fin TEXT DEFAULT '',
        duree_minutes INTEGER DEFAULT 0,
        technicien_nom TEXT DEFAULT '',
        technicien_email TEXT DEFAULT '',
        statut TEXT DEFAULT 'completee',
        contrat_id INTEGER DEFAULT NULL,
        cout_ht REAL DEFAULT 0,
        devise TEXT DEFAULT 'EUR',
        date_creation TEXT DEFAULT '',
        date_maj TEXT DEFAULT '',
        auth_user_id INTEGER,
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE,
        FOREIGN KEY(contrat_id) REFERENCES contrats(id) ON DELETE SET NULL,
        FOREIGN KEY(auth_user_id) REFERENCES auth_users(id) ON DELETE SET NULL)''')

    # TABLE PIVOT: INTERVENTIONS <-> APPAREILS
    c.execute('''CREATE TABLE IF NOT EXISTS interventions_appareils (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        intervention_id INTEGER NOT NULL,
        appareil_id INTEGER NOT NULL,
        FOREIGN KEY(intervention_id) REFERENCES interventions(id) ON DELETE CASCADE,
        FOREIGN KEY(appareil_id) REFERENCES appareils(id) ON DELETE CASCADE)''')

    # TABLE PIVOT: INTERVENTIONS <-> PERIPHERIQUES
    c.execute('''CREATE TABLE IF NOT EXISTS interventions_peripheriques (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        intervention_id INTEGER NOT NULL,
        peripherique_id INTEGER NOT NULL,
        FOREIGN KEY(intervention_id) REFERENCES interventions(id) ON DELETE CASCADE,
        FOREIGN KEY(peripherique_id) REFERENCES peripheriques(id) ON DELETE CASCADE)''')

    # TABLE DOCUMENTS INTERVENTIONS
    c.execute('''CREATE TABLE IF NOT EXISTS documents_interventions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        intervention_id INTEGER NOT NULL,
        client_id INTEGER NOT NULL,
        nom TEXT DEFAULT '',
        description TEXT DEFAULT '',
        type_doc TEXT DEFAULT '',
        nom_fichier TEXT DEFAULT '',
        taille INTEGER DEFAULT 0,
        date_upload TEXT DEFAULT '',
        FOREIGN KEY(intervention_id) REFERENCES interventions(id) ON DELETE CASCADE,
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE)''')

    # Migration : colonnes antivirus / EDR / RMM sur appareils
    for _col, _def in [('av_marque', "TEXT DEFAULT ''"), ('av_nom', "TEXT DEFAULT ''"),
                        ('av_date_debut', "TEXT DEFAULT ''"), ('av_date_fin', "TEXT DEFAULT ''"),
                        ('av_contrat_id', 'INTEGER'),
                        ('edr_marque', "TEXT DEFAULT ''"), ('edr_nom', "TEXT DEFAULT ''"),
                        ('edr_date_fin', "TEXT DEFAULT ''"), ('edr_contrat_id', 'INTEGER'),
                        ('rmm_marque', "TEXT DEFAULT ''"), ('rmm_nom', "TEXT DEFAULT ''"),
                        ('rmm_agent_id', "TEXT DEFAULT ''"), ('rmm_date_fin', "TEXT DEFAULT ''"),
                        ('rmm_contrat_id', 'INTEGER')]:
        try:
            c.execute(f"ALTER TABLE appareils ADD COLUMN {_col} {_def}")
        except Exception:
            pass

    # Migration : colonne logiciels sur appareils (JSON array)
    try:
        c.execute("ALTER TABLE appareils ADD COLUMN logiciels TEXT DEFAULT '[]'")
    except Exception:
        pass

    # Migration : date_maj sur outils et baie_slots (pour sync bidirectionnelle)
    for _tbl in ('outils', 'baie_slots'):
        try:
            c.execute(f"ALTER TABLE {_tbl} ADD COLUMN date_maj TEXT DEFAULT ''")
        except Exception:
            pass

    # TABLE BAIE DE BRASSAGE — SLOTS
    c.execute('''CREATE TABLE IF NOT EXISTS baie_slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        col_index INTEGER DEFAULT 0,
        hauteur_u INTEGER DEFAULT 1,
        appareil_id INTEGER,
        nom_custom TEXT DEFAULT '',
        type_equipement TEXT DEFAULT '',
        couleur TEXT DEFAULT '#1e3a5f',
        description TEXT DEFAULT '',
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE,
        FOREIGN KEY(appareil_id) REFERENCES appareils(id) ON DELETE SET NULL)''')

    # TABLE PHOTOS BAIE
    c.execute('''CREATE TABLE IF NOT EXISTS baie_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        nom TEXT DEFAULT '',
        description TEXT DEFAULT '',
        nom_fichier TEXT DEFAULT '',
        taille INTEGER DEFAULT 0,
        date_upload TEXT DEFAULT '',
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE)''')

    # TABLE PLANS D'ÉTAGE
    c.execute('''CREATE TABLE IF NOT EXISTS plans (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id     INTEGER NOT NULL,
        nom           TEXT DEFAULT '',
        description   TEXT DEFAULT '',
        contenu       TEXT DEFAULT '{"elements":[]}',
        date_creation TEXT DEFAULT '',
        date_maj      TEXT DEFAULT '',
        FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE)''')

    # TABLE LICENCES LOGICIELS (par appareil)
    c.execute('''CREATE TABLE IF NOT EXISTS licences_appareils (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        appareil_id    INTEGER NOT NULL,
        client_id      INTEGER NOT NULL,
        editeur        TEXT DEFAULT '',
        produit        TEXT DEFAULT '',
        cle_licence    TEXT DEFAULT '',
        contrat_id     INTEGER,
        date_creation  TEXT DEFAULT '',
        FOREIGN KEY(appareil_id) REFERENCES appareils(id) ON DELETE CASCADE,
        FOREIGN KEY(client_id)   REFERENCES clients(id)   ON DELETE CASCADE,
        FOREIGN KEY(contrat_id)  REFERENCES contrats(id)  ON DELETE SET NULL)''')

    # TABLE PRESTATAIRES (commun à tous les clients)

    conn.execute('PRAGMA foreign_keys = ON')

    # Migration : si ancienne table parc_general sans client_id, migrer
    cols_parc = [r[1] for r in conn.execute('PRAGMA table_info(parc_general)').fetchall()]
    cols_app  = [r[1] for r in conn.execute('PRAGMA table_info(appareils)').fetchall()]

    if 'client_id' not in cols_parc:
        # Créer client par défaut et migrer les données
        now = datetime.utcnow().isoformat()
        c.execute("INSERT INTO clients (nom, date_creation, date_maj) VALUES ('Client par défaut', ?, ?)", (now, now))
        default_cid = c.lastrowid
        c.execute(f'ALTER TABLE parc_general ADD COLUMN client_id INTEGER DEFAULT {default_cid}')
        c.execute('UPDATE parc_general SET client_id=?', (default_cid,))

    if 'client_id' not in cols_app:
        now = datetime.utcnow().isoformat()
        cid = conn.execute('SELECT id FROM clients ORDER BY id LIMIT 1').fetchone()
        if cid:
            c.execute(f"ALTER TABLE appareils ADD COLUMN client_id INTEGER DEFAULT {cid['id']}")
            c.execute('UPDATE appareils SET client_id=?', (cid['id'],))

    # Migration : col_index + baie_nom dans baie_slots
    for col_add, defval in [('col_index','0'), ('baie_nom',"'Baie principale'")]:
        try:
            c.execute(f"ALTER TABLE baie_slots ADD COLUMN {col_add} TEXT DEFAULT {defval}")
        except: pass

    # (ancienne migration col_index conservée pour compatibilité)
    cols_baie = [r[1] for r in conn.execute('PRAGMA table_info(baie_slots)').fetchall()]
    if 'col_index' not in cols_baie:
        c.execute("ALTER TABLE baie_slots ADD COLUMN col_index INTEGER DEFAULT 0")

    # Migration : colonnes identifiants appareils + carte graphique
    cols_app2 = [r[1] for r in conn.execute('PRAGMA table_info(appareils)').fetchall()]
    for col in ['user_login','user_password','admin_login','admin_password','anydesk_id','anydesk_password','carte_graphique']:
        if col not in cols_app2:
            c.execute(f"ALTER TABLE appareils ADD COLUMN {col} TEXT DEFAULT ''")
    if 'garantie_alerte_ignoree' not in cols_app2:
        c.execute("ALTER TABLE appareils ADD COLUMN garantie_alerte_ignoree INTEGER DEFAULT 0")

    # ── TABLE JOURNAL DES SUPPRESSIONS (pour sync bidirectionnelle) ──────────
    c.execute('''CREATE TABLE IF NOT EXISTS _sync_deletions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        tbl        TEXT    NOT NULL,
        record_id  INTEGER NOT NULL,
        deleted_at TEXT    NOT NULL,
        UNIQUE(tbl, record_id) ON CONFLICT REPLACE)''')

    # Triggers : enregistre automatiquement chaque suppression dans _sync_deletions
    _TRACKED = ['appareils','peripheriques','identifiants','contrats',
                'utilisateurs','services','clients','baie_slots',
                'outils','kb_articles','kb_categories',
                'documents_appareils','documents_contrats',
                'documents_peripheriques','baie_photos',
                'types_droits','droits_utilisateurs',
                'contrats_appareils','contrats_peripheriques',
                'peripheriques_appareils','parc_general','historique','plans']
    for _t in _TRACKED:
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS _trg_del_{_t}
            AFTER DELETE ON {_t} BEGIN
                INSERT OR REPLACE INTO _sync_deletions (tbl, record_id, deleted_at)
                VALUES ('{_t}', OLD.id, datetime('now'));
            END""")

    # Migration : ajouter colonnes BLOB + sync si n'existent pas
    for table in ['documents_appareils', 'documents_contrats', 'documents_peripheriques']:
        try:
            c.execute(f'ALTER TABLE {table} ADD COLUMN contenu_blob BLOB')
        except sqlite3.OperationalError:
            pass  # Colonne existe déjà
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN sync_status TEXT DEFAULT 'local'")
        except sqlite3.OperationalError:
            pass  # Colonne existe déjà
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN date_sync TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Colonne existe déjà

    # Client par défaut si aucun
    if not c.execute('SELECT id FROM clients').fetchone():
        now = datetime.utcnow().isoformat()
        c.execute("INSERT INTO clients (nom, date_creation, date_maj) VALUES ('Mon Client', ?, ?)", (now, now))
        cid = c.lastrowid
        c.execute("INSERT INTO parc_general (client_id, nom_site, date_maj) VALUES (?, 'Mon Parc Informatique', ?)", (cid, now))

    conn.commit(); conn.close()

# init_db() est appelé ici en mode dev (python app.py)
# En mode launcher/PyInstaller, le launcher le gère après avoir surchargé DATABASE
# En mode normal (python app.py ou import dev), appeler init_db()
# En mode launcher, le launcher le fait après avoir surchargé DATABASE
import inspect as _inspect
_called_from_launcher = any('launcher' in f.filename for f in _inspect.stack())
if not _called_from_launcher:
    init_db()


# ─── CONFIGURATION GLOBALE ───────────────────────────────────────────────────

_TYPE_CSS_DEFAULTS = {
    'pc':'#00c9ff','linux':'#4ade80','laptop':'#60a5fa','mac':'#e2e8f0',
    'serveur':'#c084fc','imprimante':'#f97316','switch':'#facc15',
    'routeur':'#ff3355','nas':'#4ade80','tel':'#34d399',
    'tablette':'#a78bfa','camera':'#fb923c','wifi':'#2dd4bf','autre':'#94a3b8',
}

@app.context_processor
def inject_cfg():
    types = get_liste_cached('types_appareils')
    user = get_auth_user()
    auth_user_id = user['id'] if user else None
    # Fusionner config globale + préférences personnelles de l'utilisateur
    cfg = cfg_all(auth_user_id=auth_user_id)
    # Calcule les labels courts pour les badges de types (pour les templates)
    type_badges = {}
    for t in types:
        k = type_css_filter(t)
        if k not in type_badges:
            val = cfg.get(f'type_badge_{k}', '')
            if val:
                type_badges[k] = val[:3].upper()
            else:
                type_badges[k] = _TYPE_BADGE_DEFAULTS.get(k, k[:3].upper() or 'AUT')
    return {
        'cfg': cfg,
        'types_appareils_ctx': types,
        'type_css_defaults': _TYPE_CSS_DEFAULTS,
        'type_badge_defaults': _TYPE_BADGE_DEFAULTS,
        'type_badges': type_badges,
    }

@app.route('/api/config', methods=['GET'])
def api_config_get():
    user = get_auth_user()
    auth_user_id = user['id'] if user else None
    # Retourne config globale + préférences personnelles de l'utilisateur
    return jsonify(cfg_all(auth_user_id=auth_user_id))

@app.route('/api/config', methods=['POST'])
def api_config_save():
    from config_helpers import cfg_set_batch
    user = get_auth_user()
    auth_user_id = user['id'] if user else None

    data = request.json or {}
    old_db_type = cfg_get('db_type', auth_user_id=auth_user_id)  # Récupérer avant les modifications

    # Filtrer et valider les clés à sauvegarder
    valid_config = {}
    for k, v in data.items():
        if (k in CFG_DEFAULTS
                or k.startswith('port_color_')  # Anciennes clés (par serviceType): port_color_ssh, port_color_http, etc.
                or k.startswith('port_icon_')   # Anciennes clés (par serviceType): port_icon_ssh, port_icon_http, etc.
                or (k.startswith('port_') and k.endswith(('_name', '_description', '_color', '_icon')))  # Nouvelles clés (par numéro de port): port_22_color, port_5000_icon, etc.
                or k.startswith('periph_color_')
                or k.startswith('type_color_')
                or k.startswith('type_badge_')
                or k.startswith('type_desc_')  # Nouveau: descriptions de types
                or k == 'mode'):
            # Special validation for dashboard_widgets_size (Phase 9)
            if k == 'dashboard_widgets_size':
                # Ensure it's valid JSON before saving
                try:
                    sizes = json.loads(str(v))
                    # Validate all sizes are valid
                    for widget_id, size in sizes.items():
                        if size not in ('small', 'medium', 'large'):
                            logger.warning(f"Invalid widget size: {widget_id}={size}")
                            return jsonify({'error': 'Invalid widget size value'}), 400
                    valid_config[k] = json.dumps(sizes)  # Re-serialize to ensure valid JSON
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"Invalid dashboard_widgets_size JSON: {str(e)}")
                    return jsonify({'error': 'Invalid JSON in dashboard_widgets_size'}), 400
            # Special validation for dashboard_widgets_height (5 levels: xs, s, m, l, xl)
            elif k == 'dashboard_widgets_height':
                # Ensure it's valid JSON before saving
                try:
                    heights = json.loads(str(v))
                    # Valid height levels (5 levels for more precision)
                    valid_heights = {'xs', 's', 'm', 'l', 'xl', 'compact', 'normal', 'tall'}  # Include legacy names
                    # Map legacy names to new ones for backwards compatibility
                    legacy_map = {'compact': 's', 'normal': 'm', 'tall': 'l'}

                    # Validate all heights are valid
                    for widget_id, height in heights.items():
                        if height not in valid_heights:
                            logger.warning(f"Invalid widget height: {widget_id}={height}")
                            return jsonify({'error': 'Invalid widget height value'}), 400
                        # Convert legacy names to new ones
                        if height in legacy_map:
                            heights[widget_id] = legacy_map[height]

                    valid_config[k] = json.dumps(heights)  # Re-serialize to ensure valid JSON
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"Invalid dashboard_widgets_height JSON: {str(e)}")
                    return jsonify({'error': 'Invalid JSON in dashboard_widgets_height'}), 400
            else:
                valid_config[k] = str(v)

    # ✓ Sauvegarder tout en UNE SEULE transaction (beaucoup plus rapide)
    # Les clés personnelles iront dans user_preferences, les autres dans config
    if valid_config:
        cfg_set_batch(valid_config, auth_user_id=auth_user_id)

    cfg_invalidate()

    # ✓ Optimisation: ne reconfigurer la sync que si db_type a changé
    new_db_type = cfg_get('db_type', auth_user_id=auth_user_id)
    if old_db_type != new_db_type:
        _handle_sync_config()

    return jsonify({'ok': True})

@app.route('/api/config/reset', methods=['POST'])
def api_config_reset():
    conn = get_db()
    conn.execute('DELETE FROM config')
    conn.commit(); conn.close()
    cfg_invalidate()
    return jsonify({'ok': True})


@app.route('/api/db/test', methods=['POST'])
@login_required
def api_db_test():
    data  = request.get_json() or {}
    url   = (data.get('url',   '') or '').strip()
    token = (data.get('token', '') or '').strip()
    if not url or not token:
        return jsonify({'ok': False, 'message': 'URL et token requis'})
    from database import test_turso
    ok, msg = test_turso(url, token)
    return jsonify({'ok': ok, 'message': msg})


@app.route('/api/db/transfer', methods=['POST'])
@login_required
def api_db_transfer():
    if not can_write():
        return jsonify({'ok': False, 'error': 'Accès en lecture seule'})
    data      = request.get_json() or {}
    direction = data.get('direction', '')
    if direction not in ('local_to_turso', 'turso_to_local'):
        return jsonify({'ok': False, 'error': 'Direction invalide'})
    url   = cfg_get('turso_url',   '').strip()
    token = cfg_get('turso_token', '').strip()
    if not url or not token:
        return jsonify({'ok': False, 'error': 'Turso non configuré'})
    from database import TursoConnection, test_turso, migrate_db, get_local_db
    ok, msg = test_turso(url, token)
    if not ok:
        return jsonify({'ok': False, 'error': f'Connexion impossible: {msg}'})
    local_conn = get_local_db()
    turso_conn = TursoConnection(url, token)
    try:
        source = local_conn if direction == 'local_to_turso' else turso_conn
        target = turso_conn if direction == 'local_to_turso' else local_conn
        ok, stats, error = migrate_db(source, target)
        local_conn.close()
        if ok:
            total    = sum(stats.values())
            n_tables = sum(1 for v in stats.values() if v > 0)
            return jsonify({'ok': True,
                            'summary': f'{total} enregistrements sur {n_tables} tables',
                            'stats': stats})
        return jsonify({'ok': False, 'error': (error or '')[:500]})
    except Exception as e:
        local_conn.close()
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/db/sync', methods=['GET', 'POST'])
@login_required
def api_db_sync():
    from database import sync_once, get_sync_state
    if request.method == 'GET':
        return jsonify(get_sync_state())
    if not can_write():
        return jsonify({'ok': False, 'error': 'Accès en lecture seule'})
    ok, stats, error = sync_once()
    state = get_sync_state()
    state['ok']    = ok
    state['error'] = error
    return jsonify(state)


# ─── THREAD DE SYNCHRONISATION TURSO ─────────────────────────────────────────

_sync_thread: threading.Thread | None = None
_sync_stop   = threading.Event()


def _bg_sync_loop():
    """Boucle de synchronisation bidirectionnelle en arrière-plan."""
    from database import sync_once
    while not _sync_stop.is_set():
        try:
            if cfg_get('db_type') == 'sync':
                sync_once()
            else:
                break   # Mode sync désactivé : on arrête le thread
        except Exception:
            pass
        try:
            interval = int(cfg_get('db_sync_interval', '30'))
        except Exception:
            interval = 30
        _sync_stop.wait(timeout=max(5, interval))


def _start_sync_thread():
    global _sync_thread, _sync_stop
    if _sync_thread and _sync_thread.is_alive():
        return
    _sync_stop.clear()
    _sync_thread = threading.Thread(target=_bg_sync_loop, daemon=True, name='turso-sync')
    _sync_thread.start()
    logger.info('Thread de synchronisation Turso démarré (intervalle=%ss)',
                cfg_get('db_sync_interval', '30'))


def _stop_sync_thread():
    global _sync_thread
    _sync_stop.set()
    if _sync_thread:
        _sync_thread.join(timeout=3)
        _sync_thread = None
    logger.info('Thread de synchronisation Turso arrêté')


def _handle_sync_config():
    """Démarre ou arrête le thread de sync selon la config db_type."""
    if cfg_get('db_type') == 'sync':
        _stop_sync_thread()   # redémarre avec le nouvel intervalle éventuel
        _start_sync_thread()
    else:
        _stop_sync_thread()


_sync_init_done = False

@app.before_request
def _auto_start_sync():
    """Démarre le thread de sync au premier appel si le mode sync est actif."""
    global _sync_init_done
    if not _sync_init_done:
        _sync_init_done = True
        if cfg_get('db_type') == 'sync':
            _start_sync_thread()


# ─── OUTILS ──────────────────────────────────────────────────────────────────

@app.route('/outils')
@login_required
def page_outils():
    conn = get_db()
    outils = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM outils ORDER BY categorie, ordre, nom").fetchall()]
    # Group by categorie
    cats = {}
    for o in outils:
        c = o['categorie'] or 'Général'
        cats.setdefault(c, []).append(o)
    conn.close()
    cid = get_client_id()
    return render_template('outils.html', outils=outils, cats=cats,
                           clients=get_clients(), client_actif_id=cid)

@app.route('/api/outils', methods=['GET'])
def api_outils_get():
    conn = get_db()
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM outils ORDER BY categorie, ordre, nom").fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/outils/ajouter', methods=['POST'])
def api_outils_ajouter():
    d = request.json or {}
    nom  = d.get('nom','').strip()
    url  = d.get('url','').strip()
    if not nom or not url: return jsonify({'error':'Nom et URL requis'}), 400
    if not url.startswith('http'): url = 'https://' + url
    conn = get_db()
    ordre = conn.execute('SELECT COALESCE(MAX(ordre),0)+1 FROM outils').fetchone()[0]
    conn.execute('INSERT INTO outils (nom,url,description,categorie,icone,ordre,date_maj) VALUES (?,?,?,?,?,?,?)',
        (nom, url, d.get('description',''), d.get('categorie','Général'),
         d.get('icone','🔧'), ordre, datetime.utcnow().isoformat()))
    conn.commit()
    outils = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM outils ORDER BY categorie, ordre, nom").fetchall()]
    conn.close()
    return jsonify({'ok': True, 'outils': outils})

@app.route('/api/outils/<int:id>/supprimer', methods=['POST'])
def api_outils_supprimer(id):
    conn = get_db()
    conn.execute('DELETE FROM outils WHERE id=?', (id,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/outils/<int:id>/toggle', methods=['POST'])
def api_outils_toggle(id):
    conn = get_db()
    conn.execute('UPDATE outils SET actif = 1 - actif WHERE id=?', (id,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─── GESTION CLIENTS ─────────────────────────────────────────────────────────

@app.route('/clients')
@login_required
def liste_clients():
    clients = get_clients()   # filtre par user + champ acces
    conn = get_db()
    for cl in clients:
        cl['nb_appareils'] = conn.execute('SELECT COUNT(*) FROM appareils WHERE client_id=?', (cl['id'],)).fetchone()[0]
        cl['nb_actifs']    = conn.execute('SELECT COUNT(*) FROM appareils WHERE client_id=? AND en_ligne=1', (cl['id'],)).fetchone()[0]
    conn.close()
    return render_template('clients.html', clients=clients, client_actif_id=get_client_id())

@app.route('/client/nouveau', methods=['GET','POST'])
def nouveau_client():
    if request.method == 'POST':
        f = request.form
        now = datetime.utcnow().isoformat()
        uid = session.get('auth_user_id')
        conn = get_db()
        c = conn.execute(
            "INSERT INTO clients (nom,contact,telephone,email,adresse,notes,couleur,auth_user_id,date_creation,date_maj) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f.get('nom','Nouveau client'), f.get('contact',''), f.get('telephone',''),
             f.get('email',''), f.get('adresse',''), f.get('notes',''),
             f.get('couleur','#00c9ff'), uid, now, now))
        cid = c.lastrowid
        conn.execute("INSERT INTO parc_general (client_id, nom_site, date_maj) VALUES (?,?,?)",
                     (cid, f.get('nom','Nouveau client'), now))
        conn.commit(); conn.close()
        session['client_id'] = cid
        flash(f"Client « {f.get('nom')} » créé avec succès", 'success')
        return redirect(url_for('index'))
    return render_template('form_client.html', client=None, action='Nouveau')

@app.route('/client/<int:id>/editer', methods=['GET','POST'])
def editer_client(id):
    conn = get_db()
    if request.method == 'POST':
        f = request.form
        conn.execute('''UPDATE clients SET nom=?,contact=?,telephone=?,email=?,adresse=?,notes=?,couleur=?,date_maj=?
            WHERE id=?''', (f.get('nom',''), f.get('contact',''), f.get('telephone',''),
             f.get('email',''), f.get('adresse',''), f.get('notes',''),
             f.get('couleur','#00c9ff'), datetime.utcnow().isoformat(), id))
        conn.commit(); conn.close()
        flash('Client mis à jour', 'success')
        return redirect(url_for('liste_clients'))
    cl = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (id,)).fetchone())
    conn.close()
    return render_template('form_client.html', client=cl, action='Modifier')

@app.route('/client/<int:id>/supprimer', methods=['POST'])
def supprimer_client(id):
    conn = get_db()
    conn.execute('PRAGMA foreign_keys = ON')
    nom = row_to_dict(conn.execute('SELECT nom FROM clients WHERE id=?', (id,)).fetchone() or {}).get('nom','')
    conn.execute('DELETE FROM clients WHERE id=?', (id,))
    conn.commit(); conn.close()
    if session.get('client_id') == id:
        session.pop('client_id', None)
    flash(f"Client « {nom} » supprimé avec toutes ses données", 'info')
    return redirect(url_for('liste_clients'))

@app.route('/client/<int:id>/selectionner')
def selectionner_client(id):
    """
    Sélectionne un client et redirige vers son dashboard.
    Fonctionne depuis n'importe quelle page (user_dashboard ou dashboard d'un autre client).
    """
    session['client_id'] = id
    # Toujours rediriger vers le dashboard du client sélectionné
    # pour une UX cohérente (quand on sélectionne un client, on voit son dashboard)
    return redirect(url_for('client_dashboard_view', cid=id))

# ─── ROUTES PRINCIPALES ──────────────────────────────────────────────────────

def _compute_client_dashboard_stats(conn, cid, today):
    """
    Calcule les statistiques du dashboard pour un client spécifique.
    Retourne un dictionnaire avec tous les compteurs et données agrégées.
    """
    appareils = fmt_appareils([row_to_dict(r) for r in retry_db_query(lambda: conn.execute(
        'SELECT * FROM appareils WHERE client_id=? ORDER BY adresse_ip', (cid,)).fetchall())])

    # Compteurs principaux
    nb_en_ligne   = sum(1 for a in appareils if a.get('en_ligne'))
    nb_hors_ligne = sum(1 for a in appareils if not a.get('en_ligne') and a.get('statut') == 'actif')
    nb_garantie   = sum(1 for a in appareils if a.get('garantie_active'))
    nb_periph     = retry_db_query(lambda: conn.execute('SELECT COUNT(*) FROM peripheriques WHERE client_id=?', (cid,)).fetchone()[0])
    nb_contrats   = retry_db_query(lambda: conn.execute("SELECT COUNT(*) FROM contrats WHERE client_id=? AND statut='actif'", (cid,)).fetchone()[0])
    nb_identifiants = retry_db_query(lambda: conn.execute('SELECT COUNT(*) FROM identifiants WHERE client_id=?', (cid,)).fetchone()[0])

    # Répartition par type
    repartition_rows = retry_db_query(lambda: conn.execute(
        "SELECT type_appareil, COUNT(*) as nb FROM appareils WHERE client_id=? GROUP BY type_appareil ORDER BY nb DESC",
        (cid,)).fetchall())
    repartition = [{'type': r[0] or 'Autre', 'nb': r[1]} for r in repartition_rows]

    # Statistiques périphériques
    periph_stats = {
        'actif': conn.execute("SELECT COUNT(*) FROM peripheriques WHERE client_id=? AND statut='actif'", (cid,)).fetchone()[0],
        'stock': conn.execute("SELECT COUNT(*) FROM peripheriques WHERE client_id=? AND statut='stock'", (cid,)).fetchone()[0],
        'hs':    conn.execute("SELECT COUNT(*) FROM peripheriques WHERE client_id=? AND statut='hors_service'", (cid,)).fetchone()[0],
    }

    # Calculs dérivés
    nb_app_total = len(appareils)
    taux_dispo   = round(nb_en_ligne / nb_app_total * 100) if nb_app_total else 0

    # Montant annuel des contrats
    montant_annuel = 0.0
    _period_map = {'mensuel':12,'trimestriel':4,'semestriel':2,'annuel':1,'pluriannuel':0.5,'unique':0}
    for ct in conn.execute("SELECT montant_ht,periodicite FROM contrats WHERE client_id=? AND statut='actif'", (cid,)).fetchall():
        try:
            if ct[0]: montant_annuel += float(ct[0]) * _period_map.get(ct[1] or 'annuel', 1)
        except: pass

    # Graphiques
    types_chart = [(r['type'], r['nb']) for r in repartition]
    _p_cats = conn.execute(
        'SELECT categorie, COUNT(*) as n FROM peripheriques WHERE client_id=? GROUP BY categorie ORDER BY n DESC',
        (cid,)).fetchall()
    periph_chart = [(r[0], r[1]) for r in _p_cats]

    # Appareils récents
    recents = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM appareils WHERE client_id=? ORDER BY date_maj DESC LIMIT 5", (cid,)).fetchall()]

    # Derniers périphériques et contrats
    derniers_periph = [row_to_dict(r) for r in conn.execute(
        'SELECT * FROM peripheriques WHERE client_id=? ORDER BY date_creation DESC LIMIT 3', (cid,)).fetchall()]
    derniers_contrats = [row_to_dict(r) for r in conn.execute(
        'SELECT * FROM contrats WHERE client_id=? ORDER BY date_creation DESC LIMIT 3', (cid,)).fetchall()]

    # Nombre d'utilisateurs
    nb_users = conn.execute('SELECT COUNT(*) FROM utilisateurs WHERE client_id=?', (cid,)).fetchone()[0]

    # Nombre hors service
    nb_hors_service = conn.execute("SELECT COUNT(*) FROM appareils WHERE client_id=? AND statut='hors_service'", (cid,)).fetchone()[0]

    # Activités récentes
    hist_recent = []
    try:
        hist_recent = [row_to_dict(r) for r in conn.execute(
            "SELECT entite, entite_nom, action, date_action FROM historique "
            "WHERE client_id=? ORDER BY id DESC LIMIT 6", (cid,)).fetchall()]
    except Exception:
        pass

    return {
        'appareils': appareils,
        'nb_en_ligne': nb_en_ligne,
        'nb_hors_ligne': nb_hors_ligne,
        'nb_garantie': nb_garantie,
        'nb_periph': nb_periph,
        'nb_contrats': nb_contrats,
        'nb_identifiants': nb_identifiants,
        'repartition': repartition,
        'periph_stats': periph_stats,
        'nb_app_total': nb_app_total,
        'taux_dispo': taux_dispo,
        'montant_annuel': montant_annuel,
        'types_chart': types_chart,
        'periph_chart': periph_chart,
        'recents': recents,
        'derniers_periph': derniers_periph,
        'derniers_contrats': derniers_contrats,
        'nb_users': nb_users,
        'nb_hors_service': nb_hors_service,
        'hist_recent': hist_recent,
    }


def _compute_alerts_for_client(conn, cid, today):
    """
    Calcule les alertes (contrats, garanties, antivirus) pour un client.
    Retourne un dictionnaire avec les différentes listes d'alertes.
    """
    alerte_jours = int(cfg_get('garantie_alerte_jours', '90'))

    # Alertes contrats
    contrats_alertes = []
    for row in retry_db_query(lambda: conn.execute("SELECT * FROM contrats WHERE client_id=? AND statut='actif' AND date_fin!='' ORDER BY date_fin", (cid,)).fetchall()):
        ct = row_to_dict(row)
        if not ct.get('date_fin'): continue
        try:
            df = date.fromisoformat(ct['date_fin'])
            delta = (df - today).days
            ct['jours_restants'] = delta
            ct['date_fin_fmt']   = df.strftime('%d/%m/%Y')
            preavis = ct.get('preavis_jours') or 30
            if delta < 0 or delta <= preavis:
                ct['expire_depasse'] = delta < 0
                contrats_alertes.append(ct)
        except: pass

    # Alertes garanties
    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT * FROM appareils WHERE client_id=? ORDER BY adresse_ip', (cid,)).fetchall()]
    appareils = fmt_appareils(appareils)

    garanties_alertes = []
    for a in appareils:
        if not a.get('date_fin_garantie'): continue
        if a.get('garantie_alerte_ignoree'): continue
        try:
            df = date.fromisoformat(a['date_fin_garantie'])
            delta = (df - today).days
            if delta < 0 or delta <= alerte_jours:
                a['garantie_jours'] = delta
                a['garantie_fin_fmt'] = df.strftime('%d/%m/%Y')
                garanties_alertes.append(a)
        except: pass
    garanties_alertes.sort(key=lambda x: x.get('garantie_jours', 9999))

    # Prochains contrats à renouveler
    prochains_renouvellements = []
    for row in conn.execute(
        "SELECT * FROM contrats WHERE client_id=? AND statut='actif' AND date_fin!='' ORDER BY date_fin LIMIT 5",
        (cid,)).fetchall():
        ct = row_to_dict(row)
        try:
            df = date.fromisoformat(ct['date_fin'])
            ct['jours_restants'] = (df - today).days
            ct['date_fin_fmt']   = df.strftime('%d/%m/%Y')
            prochains_renouvellements.append(ct)
        except: pass

    # Antivirus urgents (30 jours)
    today_iso    = date.today().isoformat()
    seuil_av_iso = (date.today() + timedelta(days=30)).isoformat()
    av_urgents = []
    for _r in conn.execute(
        "SELECT nom_machine, av_nom, av_marque, av_date_fin FROM appareils "
        "WHERE client_id=? AND av_date_fin!='' AND av_date_fin IS NOT NULL AND av_date_fin<=? "
        "ORDER BY av_date_fin LIMIT 5", (cid, seuil_av_iso)).fetchall():
        _item = row_to_dict(_r)
        _item['expire_depasse'] = _item.get('av_date_fin', '') < today_iso
        av_urgents.append(_item)

    return {
        'contrats_alertes': contrats_alertes[:5],
        'garanties_alertes': garanties_alertes[:5],
        'prochains_renouvellements': prochains_renouvellements,
        'av_urgents': av_urgents,
    }


# ═══════════════════════════════════════════════════════════════════════════════════════
# PHASE 8: WIDGET DATA COMPUTATION HELPERS
# These functions compute data for individual dashboard widgets
# ═══════════════════════════════════════════════════════════════════════════════════════

def _compute_critical_alerts(conn, cid, today):
    """
    Consolidates all critical issues for the client:
    - Expired warranties
    - Expired contracts
    - Offline devices
    - Expiring licenses (AV/RMM/EDR)
    Returns list sorted by urgency.
    """
    alerts = []

    # Expired/expiring warranties
    for row in conn.execute(
        "SELECT id, nom_machine, date_fin_garantie FROM appareils "
        "WHERE client_id=? AND date_fin_garantie!='' AND date_fin_garantie<=?",
        (cid, (today + timedelta(days=30)).isoformat())).fetchall():
        a = row_to_dict(row)
        try:
            df = date.fromisoformat(a['date_fin_garantie'])
            if df < today:
                alerts.append({'type': 'warranty_expired', 'device': a['nom_machine'], 'date': a['date_fin_garantie'], 'severity': 'critical'})
            else:
                alerts.append({'type': 'warranty_expiring', 'device': a['nom_machine'], 'date': a['date_fin_garantie'], 'severity': 'warning'})
        except: pass

    # Expired/expiring contracts
    for row in conn.execute(
        "SELECT id, description, date_fin FROM contrats "
        "WHERE client_id=? AND statut='actif' AND date_fin!='' AND date_fin<=?",
        (cid, (today + timedelta(days=30)).isoformat())).fetchall():
        c = row_to_dict(row)
        try:
            df = date.fromisoformat(c['date_fin'])
            if df < today:
                alerts.append({'type': 'contract_expired', 'contract': c['description'], 'date': c['date_fin'], 'severity': 'critical'})
            else:
                alerts.append({'type': 'contract_expiring', 'contract': c['description'], 'date': c['date_fin'], 'severity': 'warning'})
        except: pass

    # Offline devices (no recent ping)
    for row in conn.execute(
        "SELECT id, nom_machine FROM appareils WHERE client_id=? AND en_ligne=0",
        (cid,)).fetchall():
        a = row_to_dict(row)
        alerts.append({'type': 'device_offline', 'device': a['nom_machine'], 'severity': 'warning'})

    # Expiring AV/RMM/EDR licenses
    for row in conn.execute(
        "SELECT nom_machine, av_date_fin FROM appareils "
        "WHERE client_id=? AND av_date_fin!='' AND av_date_fin<=?",
        (cid, (today + timedelta(days=30)).isoformat())).fetchall():
        a = row_to_dict(row)
        try:
            df = date.fromisoformat(a['av_date_fin'])
            severity = 'critical' if df < today else 'warning'
            alerts.append({'type': 'av_expiring', 'device': a['nom_machine'], 'date': a['av_date_fin'], 'severity': severity})
        except: pass

    # Sort by severity (critical first) then by date
    severity_order = {'critical': 0, 'warning': 1}
    alerts.sort(key=lambda x: (severity_order.get(x.get('severity'), 2), x.get('date', '')))

    return {'alerts': alerts, 'count': len(alerts)}


def _compute_kpi_cards(stats, alerts, today):
    """Returns data for the 6 main KPI cards."""
    return {
        'nb_app_total': stats['nb_app_total'],
        'nb_en_ligne': stats['nb_en_ligne'],
        'taux_dispo': stats['taux_dispo'],
        'nb_garantie': stats['nb_garantie'],
        'nb_contrats': stats['nb_contrats'],
        'montant_annuel': stats['montant_annuel'],
        'nb_alertes': len(alerts['contrats_alertes']) + len(alerts['garanties_alertes']) + len(alerts['av_urgents']),
    }


def _compute_av_status(conn, cid):
    """
    Returns AV/RMM/EDR license health status across all devices.
    Counts by status: active, expiring soon (30 days), expired.
    """
    today = date.today()
    seuil_futur = (today + timedelta(days=30)).isoformat()
    today_iso = today.isoformat()

    devices = conn.execute(
        "SELECT nom_machine, av_marque, av_date_fin, rmm_marque, rmm_date_fin, edr_marque, edr_date_fin FROM appareils WHERE client_id=?",
        (cid,)).fetchall()

    av_status = {'active': 0, 'expiring': 0, 'expired': 0}
    rmm_status = {'active': 0, 'expiring': 0, 'expired': 0}
    edr_status = {'active': 0, 'expiring': 0, 'expired': 0}

    for row in devices:
        d = row_to_dict(row)

        # AV
        if d.get('av_date_fin'):
            if d['av_date_fin'] < today_iso:
                av_status['expired'] += 1
            elif d['av_date_fin'] <= seuil_futur:
                av_status['expiring'] += 1
            else:
                av_status['active'] += 1

        # RMM
        if d.get('rmm_date_fin'):
            if d['rmm_date_fin'] < today_iso:
                rmm_status['expired'] += 1
            elif d['rmm_date_fin'] <= seuil_futur:
                rmm_status['expiring'] += 1
            else:
                rmm_status['active'] += 1

        # EDR
        if d.get('edr_date_fin'):
            if d['edr_date_fin'] < today_iso:
                edr_status['expired'] += 1
            elif d['edr_date_fin'] <= seuil_futur:
                edr_status['expiring'] += 1
            else:
                edr_status['active'] += 1

    return {
        'av': av_status,
        'rmm': rmm_status,
        'edr': edr_status,
    }


def _compute_network_status(stats):
    """Returns device online/offline status summary."""
    return {
        'nb_en_ligne': stats['nb_en_ligne'],
        'nb_hors_ligne': stats['nb_hors_ligne'],
        'taux_dispo': stats['taux_dispo'],
        'devices': stats['appareils'][:10],  # Top 10 devices
    }


def _compute_device_types(stats):
    """Returns device type distribution."""
    return {
        'repartition': stats['repartition'],
        'types_chart': stats['types_chart'],
    }


def _compute_peripherals_distribution(stats):
    """Returns peripheral category distribution."""
    return {
        'periph_chart': stats['periph_chart'],
        'periph_stats': stats['periph_stats'],
    }


def _compute_device_age(conn, cid, today):
    """
    Groups devices by acquisition date into age buckets:
    - 0-1 year
    - 1-3 years
    - 3-5 years
    - 5+ years
    """
    devices = conn.execute(
        "SELECT nom_machine, date_achat FROM appareils WHERE client_id=? AND date_achat!='' AND date_achat IS NOT NULL",
        (cid,)).fetchall()

    age_groups = {
        '0-1_year': [],
        '1-3_years': [],
        '3-5_years': [],
        '5plus_years': [],
        'unknown': []
    }

    for row in devices:
        d = row_to_dict(row)
        if not d.get('date_achat'):
            age_groups['unknown'].append(d)
            continue

        try:
            acq_date = date.fromisoformat(d['date_achat'])
            age_days = (today - acq_date).days
            age_years = age_days / 365.25

            if age_years < 1:
                age_groups['0-1_year'].append(d)
            elif age_years < 3:
                age_groups['1-3_years'].append(d)
            elif age_years < 5:
                age_groups['3-5_years'].append(d)
            else:
                age_groups['5plus_years'].append(d)
        except:
            age_groups['unknown'].append(d)

    return {
        'age_groups': age_groups,
        '0-1_year_count': len(age_groups['0-1_year']),
        '1-3_years_count': len(age_groups['1-3_years']),
        '3-5_years_count': len(age_groups['3-5_years']),
        '5plus_years_count': len(age_groups['5plus_years']),
    }


def _compute_contracts_timeline(conn, cid, today):
    """Returns upcoming contract renewals/expirations timeline."""
    contracts = []
    for row in conn.execute(
        "SELECT id, titre, date_fin, montant_ht FROM contrats "
        "WHERE client_id=? AND statut='actif' AND date_fin!='' "
        "ORDER BY date_fin LIMIT 10",
        (cid,)).fetchall():
        c = row_to_dict(row)
        try:
            df = date.fromisoformat(c['date_fin'])
            days_left = (df - today).days
            c['jours_restants'] = days_left
            c['date_fin_fmt'] = df.strftime('%d/%m/%Y')
            c['urgence'] = 'expired' if days_left < 0 else ('urgent' if days_left < 30 else 'ok')
            contracts.append(c)
        except: pass

    return {
        'contracts': contracts,
        'total_count': len(contracts),
    }


def _compute_recent_activity(stats):
    """Returns recent activity/modifications."""
    return {
        'recents': stats['recents'],
        'hist_recent': stats['hist_recent'],
    }


def _compute_interventions_summary(recent_interventions):
    """Returns recent interventions summary."""
    return {
        'interventions': recent_interventions,
        'count': len(recent_interventions),
    }


def _compute_business_software(logiciels, stats):
    """Returns business software deployments."""
    return {
        'logiciels': logiciels,
        'count': len(logiciels),
        'appareils_count': stats['nb_app_total'],
    }


def _compute_network_info(parc):
    """Returns network configuration information."""
    return {
        'nom_site': parc.get('nom_site', 'N/A'),
        'type_connexion': parc.get('type_connexion', 'N/A'),
        'debit_montant': parc.get('debit_montant', 'N/A'),
        'debit_descendant': parc.get('debit_descendant', 'N/A'),
        'fournisseur_internet': parc.get('fournisseur_internet', 'N/A'),
        'ip_publique': parc.get('ip_publique', 'N/A'),
        'plage_ip_locale': parc.get('plage_ip_locale', 'N/A'),
        'domaine': parc.get('domaine', 'N/A'),
        'serveur_dns': parc.get('serveur_dns', 'N/A'),
        'passerelle': parc.get('passerelle', 'N/A'),
    }


def single_client_dashboard(cid):
    """
    Affiche le dashboard pour un seul client (vue classique).
    """
    conn = get_db()
    today = date.today()
    user = get_auth_user()

    try:
        # Fetch parc and client info
        parc    = row_to_dict(retry_db_query(lambda: conn.execute('SELECT * FROM parc_general WHERE client_id=?', (cid,)).fetchone() or {}))
        client  = row_to_dict(retry_db_query(lambda: conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {}))

        # Get dashboard stats
        stats = _compute_client_dashboard_stats(conn, cid, today)

        # Get alerts
        alerts = _compute_alerts_for_client(conn, cid, today)

        # Recent interventions
        recent_interventions = [fmt_intervention(row_to_dict(r)) for r in conn.execute(
            "SELECT * FROM interventions WHERE client_id=? AND statut != ? ORDER BY date_intervention DESC LIMIT 5",
            (cid, 'archivee')).fetchall()]

        # Logiciels & antivirus (depuis parc_general)
        logiciels = [l.strip() for l in (parc.get('logiciels_metier') or '').splitlines() if l.strip()]

        # Calcul valeur parc
        valeur_parc = sum(a.get('prix_achat') or 0 for a in stats['appareils'])

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 8.4: Fetch and parse user's widget preferences
        # ═══════════════════════════════════════════════════════════════════
        user_id = user['id'] if user else None
        default_enabled = 'critical-alerts,kpi,av-status,network-status,device-types,peripherals,device-age,contracts-timeline,recent-activity,interventions,business-software,network-info'
        default_order = 'critical-alerts,kpi,av-status,network-status,device-types,peripherals,device-age,contracts-timeline,recent-activity,interventions,business-software,network-info'

        enabled_widgets_str = cfg_get('dashboard_widgets_enabled', default_enabled, user_id)
        widget_order_str = cfg_get('dashboard_widgets_order', default_order, user_id)

        # CRITICAL: Handle empty strings by using defaults
        # This prevents widgets from disappearing if saved as empty
        if not enabled_widgets_str or not enabled_widgets_str.strip():
            enabled_widgets_str = default_enabled
        if not widget_order_str or not widget_order_str.strip():
            widget_order_str = default_order

        enabled_widgets = [w.strip() for w in enabled_widgets_str.split(',') if w.strip()]
        widget_order = [w.strip() for w in widget_order_str.split(',') if w.strip()]

        # CRITICAL: Ensure all widgets from default list are included (for new widgets added later)
        # If a widget is in the default list but not in the saved config, add it
        default_enabled_list = [w.strip() for w in default_enabled.split(',') if w.strip()]
        default_order_list = [w.strip() for w in default_order.split(',') if w.strip()]

        for widget_id in default_enabled_list:
            if widget_id not in enabled_widgets:
                enabled_widgets.append(widget_id)

        for widget_id in default_order_list:
            if widget_id not in widget_order:
                widget_order.append(widget_id)

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 9: Parse widget sizes from user preferences (Phase 9)
        # ═══════════════════════════════════════════════════════════════════
        # Default widget sizes per widget_id
        WIDGET_DEFAULT_SIZES = {
            'critical-alerts': 'large',
            'kpi': 'large',
            'av-status': 'medium',
            'network-status': 'medium',  # Changed from 'large' to match device-types for visual cohesion
            'device-types': 'medium',
            'peripherals': 'medium',
            'device-age': 'small',
            'contracts-timeline': 'large',
            'recent-activity': 'large',
            'interventions': 'small',
            'business-software': 'medium',
            'network-info': 'medium',
        }

        # Parse user's widget size preferences (JSON)
        widget_sizes_str = cfg_get('dashboard_widgets_size', '{}', user_id)
        try:
            widget_sizes_json = json.loads(widget_sizes_str)
        except (json.JSONDecodeError, ValueError):
            widget_sizes_json = {}

        # Build final sizes dict with defaults
        widget_sizes = {}
        for widget_id in enabled_widgets + widget_order:
            if widget_id in WIDGET_DEFAULT_SIZES:
                # Use user's size if specified, otherwise use default
                widget_sizes[widget_id] = widget_sizes_json.get(widget_id, WIDGET_DEFAULT_SIZES[widget_id])

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 10: Parse widget heights from user preferences (NEW)
        # ═══════════════════════════════════════════════════════════════════
        # Default widget heights per widget_id (5 LEVEL SYSTEM: xs, s, m, l, xl)
        # Tous les widgets utilisent 'm' (280px) pour une présentation cohérente et harmonieuse
        WIDGET_DEFAULT_HEIGHTS = {
            'critical-alerts': 'm',      # Medium (280px)
            'kpi': 'm',
            'av-status': 'm',
            'network-status': 'm',       # Changed from 'compact' to 'm' for consistency
            'device-types': 'm',
            'peripherals': 'm',
            'device-age': 'm',
            'contracts-timeline': 'm',
            'recent-activity': 'm',
            'interventions': 'm',        # Changed from 'compact' to 'm' for consistency
            'business-software': 'm',    # Changed from 'compact' to 'm' for consistency
            'network-info': 'm',         # Changed from 'compact' to 'm' for consistency
        }

        # Parse user's widget height preferences (JSON)
        widget_heights_str = cfg_get('dashboard_widgets_height', '{}', user_id)
        try:
            widget_heights_json = json.loads(widget_heights_str)
        except (json.JSONDecodeError, ValueError):
            widget_heights_json = {}

        # Build final heights dict with defaults
        widget_heights = {}
        for widget_id in enabled_widgets + widget_order:
            if widget_id in WIDGET_DEFAULT_HEIGHTS:
                # Use user's height if specified, otherwise use default
                widget_heights[widget_id] = widget_heights_json.get(widget_id, WIDGET_DEFAULT_HEIGHTS[widget_id])

        # Build complete widget_data dict with all widget calculations
        # (even disabled widgets may be re-enabled later without reload)
        try:
            widget_data = {
                'critical_alerts': _compute_critical_alerts(conn, cid, today),
                'kpi': _compute_kpi_cards(stats, alerts, today),
                'av_status': _compute_av_status(conn, cid),
                'network_status': _compute_network_status(stats),
                'device_types': _compute_device_types(stats),
                'peripherals': _compute_peripherals_distribution(stats),
                'device_age': _compute_device_age(conn, cid, today),
                'contracts_timeline': _compute_contracts_timeline(conn, cid, today),
                'recent_activity': _compute_recent_activity(stats),
                'interventions': _compute_interventions_summary(recent_interventions),
                'business_software': _compute_business_software(logiciels, stats),
                'network_info': _compute_network_info(parc),
            }
        except Exception as e:
            logger.exception("Error computing widget data for client %d: %s", cid, str(e))
            # Fallback: empty widget data
            widget_data = {
                'critical_alerts': {},
                'kpi': {},
                'av_status': {},
                'network_status': {},
                'device_types': {},
                'peripherals': {},
                'device_age': {},
                'contracts_timeline': {},
                'recent_activity': {},
                'interventions': {},
                'business_software': {},
                'network_info': {},
            }

        # Combine all data for template
        template_data = {
            'parc': parc,
            'client': client,
            'appareils': stats['appareils'],
            'nb_en_ligne': stats['nb_en_ligne'],
            'nb_hors_ligne': stats['nb_hors_ligne'],
            'nb_actifs': stats['nb_en_ligne'],
            'nb_garantie': stats['nb_garantie'],
            'nb_app_total': stats['nb_app_total'],
            'taux_dispo': stats['taux_dispo'],
            'valeur_parc': valeur_parc,
            'nb_periph': stats['nb_periph'],
            'nb_contrats': stats['nb_contrats'],
            'nb_identifiants': stats['nb_identifiants'],
            'repartition': stats['repartition'],
            'types_chart': stats['types_chart'],
            'periph_chart': stats['periph_chart'],
            'contrats_alertes': alerts['contrats_alertes'],
            'garanties_alertes': alerts['garanties_alertes'],
            'prochains_renouvellements': alerts['prochains_renouvellements'],
            'periph_stats': stats['periph_stats'],
            'montant_annuel': stats['montant_annuel'],
            'recents': stats['recents'],
            'derniers_periph': stats['derniers_periph'],
            'derniers_contrats': stats['derniers_contrats'],
            'recent_interventions': recent_interventions,
            'logiciels': logiciels,
            'av_urgents': alerts['av_urgents'],
            'hist_recent': stats['hist_recent'],
            'nb_users': stats['nb_users'],
            'nb_hors_service': stats['nb_hors_service'],
            'clients': get_clients(),
            'client_actif_id': cid,
            # Widget preferences (Phase 8.4)
            'enabled_widgets': enabled_widgets,
            'widget_order': widget_order,
            'widget_data': widget_data,
            # Widget sizes (Phase 9)
            'widget_sizes': widget_sizes,
            # Widget heights (Phase 10)
            'widget_heights': widget_heights,
        }

        return render_template('client_dashboard.html', **template_data)
    finally:
        conn.close()


def user_dashboard():
    """
    Affiche le dashboard utilisateur avec vue d'ensemble de tous les clients accessibles.
    Agrège les statistiques et les alertes par client.
    """
    user = get_auth_user()
    clients = get_clients()
    conn = get_db()
    today = date.today()

    try:
        # Agrégation des données par client
        clients_data = []
        all_alerts = []

        for client in clients:
            cid = client['id']

            # Récupérer les stats du client
            stats = _compute_client_dashboard_stats(conn, cid, today)
            alerts = _compute_alerts_for_client(conn, cid, today)

            # Compter les alertes pour ce client
            alert_count = (
                len(alerts['contrats_alertes']) +
                len(alerts['garanties_alertes']) +
                len(alerts['av_urgents'])
            )

            # Données du client pour le template
            client_summary = {
                'client': client,
                'stats': stats,
                'alerts': alerts,
                'alert_count': alert_count,
            }
            clients_data.append(client_summary)

            # Ajouter les alertes à la liste consolidée avec contexte du client
            for contract_alert in alerts['contrats_alertes']:
                all_alerts.append({
                    'type': 'contract',
                    'client_id': cid,
                    'client_nom': client['nom'],
                    'description': contract_alert.get('description', f"Contrat: {contract_alert.get('numero_contrat', 'N/A')}"),
                    'days_remaining': contract_alert.get('jours_restants', 999),
                    'date': contract_alert.get('date_fin', ''),
                    'expired': contract_alert.get('expire_depasse', False),
                    'object': contract_alert,
                })

            for warranty_alert in alerts['garanties_alertes']:
                all_alerts.append({
                    'type': 'warranty',
                    'client_id': cid,
                    'client_nom': client['nom'],
                    'description': warranty_alert.get('nom_machine', 'Appareil'),
                    'days_remaining': warranty_alert.get('garantie_jours', 999),
                    'date': warranty_alert.get('date_fin_garantie', ''),
                    'expired': warranty_alert.get('garantie_jours', 0) < 0,
                    'object': warranty_alert,
                })

            for av_alert in alerts['av_urgents']:
                all_alerts.append({
                    'type': 'antivirus',
                    'client_id': cid,
                    'client_nom': client['nom'],
                    'description': av_alert.get('nom_machine', 'Appareil'),
                    'days_remaining': 0,
                    'date': av_alert.get('av_date_fin', ''),
                    'expired': av_alert.get('expire_depasse', False),
                    'object': av_alert,
                })

        # Trier les alertes par urgence (jours restants croissant)
        all_alerts.sort(key=lambda x: (x['expired'] == False, x['days_remaining']))

        # Calculs globaux
        total_devices = sum(s['stats']['nb_app_total'] for s in clients_data)
        total_online = sum(s['stats']['nb_en_ligne'] for s in clients_data)
        total_contracts = sum(s['stats']['nb_contrats'] for s in clients_data)
        total_peripherals = sum(s['stats']['nb_periph'] for s in clients_data)
        total_alerts_count = len(all_alerts)

        return render_template('user_dashboard.html',
                             user=user,
                             clients_data=clients_data,
                             all_alerts=all_alerts[:20],  # Top 20 most urgent alerts
                             total_devices=total_devices,
                             total_online=total_online,
                             total_contracts=total_contracts,
                             total_peripherals=total_peripherals,
                             total_alerts_count=total_alerts_count,
                             clients=clients,
                             client_actif_id=None)  # No active client on user dashboard
    finally:
        conn.close()


@app.route('/')
@login_required
def index():
    """
    Route dashboard intelligente qui détecte le nombre de clients accessibles.
    - Zéro clients: redirection vers création
    - Un client: affiche le dashboard single-client classique
    - Plusieurs clients: affiche le dashboard multi-client avec vue d'ensemble
    """
    user = get_auth_user()
    clients = get_clients()  # Récupère tous les clients accessibles (respects ACL)

    # Cas 1: Pas de clients accessibles
    if not clients:
        return redirect(url_for('nouveau_client'))

    # Cas 2: Un seul client accessible -> affiche le dashboard classique
    if len(clients) == 1:
        cid = clients[0]['id']
        session['client_id'] = cid
        return single_client_dashboard(cid)

    # Cas 3: Plusieurs clients accessibles -> affiche le dashboard utilisateur
    return user_dashboard()


@app.route('/client/<int:cid>/dashboard')
@login_required
def client_dashboard_view(cid):
    """
    Affiche le dashboard pour un client spécifique.
    Vérifie d'abord que l'utilisateur a accès au client.
    """
    # Vérifier l'accès
    if not get_client_access(cid):
        flash('Accès refusé à ce client', 'danger')
        return redirect(url_for('index'))

    # Définir le client actif dans la session
    session['client_id'] = cid

    # Afficher le dashboard du client
    return single_client_dashboard(cid)

@app.route('/parc', methods=['GET','POST'])
def parc_general():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    parc = row_to_dict(conn.execute('SELECT * FROM parc_general WHERE client_id=?', (cid,)).fetchone() or {})
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    if request.method == 'POST':
        if not can_write():
            flash('Accès en lecture seule — modification non autorisée', 'danger')
            return redirect(url_for('index'))
        f = request.form
        if parc.get('id'):
            conn.execute('''UPDATE parc_general SET nom_site=?,adresse=?,type_connexion=?,debit_montant=?,
                debit_descendant=?,fournisseur_internet=?,ip_publique=?,plage_ip_locale=?,nb_machines=?,
                nb_utilisateurs=?,domaine=?,serveur_dns=?,passerelle=?,baie_marque=?,baie_nb_u=?,
                switch_marque=?,switch_nb_ports=?,switch_nb_unites=?,routeur_marque=?,serveur_marque=?,
                serveur_modele=?,ups_marque=?,ups_capacite=?,autres_equipements=?,logiciels_metier=?,
                antivirus=?,os_principal=?,suite_bureautique=?,notes=?,
                wifi_ssid=?,wifi_password=?,wifi_securite=?,
                wifi_ssid2=?,wifi_password2=?,wifi_securite2=?,wifi_notes=?,
                date_maj=? WHERE client_id=?''', (
                f.get('nom_site',''), f.get('adresse',''), f.get('type_connexion',''),
                f.get('debit_montant',''), f.get('debit_descendant',''), f.get('fournisseur_internet',''),
                f.get('ip_publique',''), f.get('plage_ip_locale','192.168.1.0/24'),
                int(f.get('nb_machines') or 0), int(f.get('nb_utilisateurs') or 0),
                f.get('domaine',''), f.get('serveur_dns',''), f.get('passerelle',''),
                f.get('baie_marque',''), int(f.get('baie_nb_u') or 0), f.get('switch_marque',''),
                int(f.get('switch_nb_ports') or 0), int(f.get('switch_nb_unites') or 0),
                f.get('routeur_marque',''), f.get('serveur_marque',''), f.get('serveur_modele',''),
                f.get('ups_marque',''), f.get('ups_capacite',''), f.get('autres_equipements',''),
                f.get('logiciels_metier',''), f.get('antivirus',''), f.get('os_principal',''),
                f.get('suite_bureautique',''), f.get('notes',''),
                f.get('wifi_ssid',''), f.get('wifi_password',''), f.get('wifi_securite','WPA2'),
                f.get('wifi_ssid2',''), f.get('wifi_password2',''), f.get('wifi_securite2','WPA2'),
                f.get('wifi_notes',''),
                datetime.utcnow().isoformat(), cid))
        else:
            conn.execute('''INSERT INTO parc_general (client_id,nom_site,plage_ip_locale,date_maj) VALUES (?,?,?,?)''',
                         (cid, f.get('nom_site',''), f.get('plage_ip_locale','192.168.1.0/24'), datetime.utcnow().isoformat()))
        conn.commit(); conn.close()
        flash('Informations du parc sauvegardées', 'success')
        return redirect(url_for('parc_general'))
    conn.close()
    return render_template('parc_general.html', parc=parc, client=client,
                           clients=get_clients(), client_actif_id=cid)

# ─── ROUTES API PRESTATAIRES ─────────────────────────────────────────────────────

# ─── ROUTES API POUR CHARGER LES ENTITÉS D'UN CLIENT ──────────────────────────

@app.route('/api/client/<int:client_id>/appareils', methods=['GET'])
@login_required
def api_get_client_appareils(client_id):
    """Lister les appareils d'un client"""
    if not get_client_access(client_id):
        return jsonify({'error': 'Forbidden'}), 403

    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT id, nom_machine as nom FROM appareils WHERE client_id=? ORDER BY nom_machine ASC',
            (client_id,)
        ).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'nom': r[1]} for r in rows])
    except Exception as e:
        conn.close()
        logger.exception(f'Erreur lecture appareils client {client_id}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/client/<int:client_id>/contrats', methods=['GET'])
@login_required
def api_get_client_contrats(client_id):
    """Lister les contrats d'un client"""
    if not get_client_access(client_id):
        return jsonify({'error': 'Forbidden'}), 403

    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT id, description FROM contrats WHERE client_id=? ORDER BY description ASC',
            (client_id,)
        ).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'nom': r[1]} for r in rows])
    except Exception as e:
        conn.close()
        logger.exception(f'Erreur lecture contrats client {client_id}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/client/<int:client_id>/peripheriques', methods=['GET'])
@login_required
def api_get_client_peripheriques(client_id):
    """Lister les périphériques d'un client"""
    if not get_client_access(client_id):
        return jsonify({'error': 'Forbidden'}), 403

    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT id, (marque || " " || modele) as nom FROM peripheriques WHERE client_id=? ORDER BY marque, modele ASC',
            (client_id,)
        ).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'nom': r[1]} for r in rows])
    except Exception as e:
        conn.close()
        logger.exception(f'Erreur lecture périphériques client {client_id}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/client/<int:client_id>/services', methods=['GET'])
@login_required
def api_get_client_services(client_id):
    """Lister les services d'un client"""
    if not get_client_access(client_id):
        return jsonify({'error': 'Forbidden'}), 403

    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT id, nom FROM services WHERE client_id=? ORDER BY nom ASC',
            (client_id,)
        ).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'nom': r[1]} for r in rows])
    except Exception as e:
        conn.close()
        logger.exception(f'Erreur lecture services client {client_id}')
        return jsonify({'error': str(e)}), 500

# Colonnes triables pour l'inventaire appareils
_APP_SORT_COLS = {
    'nom':      'a.nom_machine',
    'type':     'a.type_appareil, a.nom_machine',
    'ip':       'ip_sort_key(a.adresse_ip), a.nom_machine',
    'user':     'a.utilisateur, a.nom_machine',
    'garantie': "CASE WHEN a.date_fin_garantie='' OR a.date_fin_garantie IS NULL THEN '9999-99-99' ELSE a.date_fin_garantie END, a.nom_machine",
    'statut':   'a.statut, a.nom_machine',
    'marque':   'a.marque, a.modele, a.nom_machine',
    'os':       'a.os, a.nom_machine',
}

@app.route('/appareils')
@login_required
def liste_appareils():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    page      = request.args.get('page', 1, type=int)
    sort_col  = request.args.get('sort', 'ip')
    sort_dir  = request.args.get('dir',  'asc')
    f_types   = request.args.getlist('type')
    f_statut  = request.args.get('statut', '')
    f_av      = request.args.get('av', '')

    order_expr = _APP_SORT_COLS.get(sort_col, 'ip_sort_key(a.adresse_ip)')
    direction  = 'DESC' if sort_dir == 'desc' else 'ASC'

    q = '''SELECT a.*,
            (SELECT COUNT(*) FROM documents_appareils d WHERE d.appareil_id=a.id) as nb_docs,
            (SELECT COUNT(*) FROM contrats_appareils ca JOIN contrats ct ON ca.contrat_id=ct.id
             WHERE ca.appareil_id=a.id AND ct.client_id=a.client_id) as nb_contrats
            FROM appareils a WHERE a.client_id=?'''
    params = [cid]

    if f_types:
        placeholders = ','.join('?' * len(f_types))
        q += f' AND a.type_appareil IN ({placeholders})'
        params.extend(f_types)

    if f_statut:
        q += ' AND a.statut=?'
        params.append(f_statut)

    # Filtre antivirus (reconstruit la logique de fmt_appareils)
    if f_av == 'none':
        q += " AND (a.av_nom='' OR a.av_nom IS NULL) AND (a.av_marque='' OR a.av_marque IS NULL)"
    elif f_av == 'expired':
        q += " AND (a.av_nom!='' AND a.av_nom IS NOT NULL) AND a.av_date_fin!='' AND a.av_date_fin IS NOT NULL AND date(a.av_date_fin)<date('now')"
    elif f_av == 'expiring':
        q += " AND (a.av_nom!='' AND a.av_nom IS NOT NULL) AND a.av_date_fin!='' AND a.av_date_fin IS NOT NULL AND date(a.av_date_fin)>=date('now') AND date(a.av_date_fin)<=date('now','+30 days')"
    elif f_av == 'active':
        q += " AND (a.av_nom!='' AND a.av_nom IS NOT NULL) AND (a.av_date_fin='' OR a.av_date_fin IS NULL OR date(a.av_date_fin)>date('now','+30 days'))"

    q += f' ORDER BY {order_expr} {direction}'

    rows, pagination = paginate(q, tuple(params), page)
    appareils = fmt_appareils([row_to_dict(r) for r in rows])
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    periph_rows = conn.execute(
        '''SELECT pa.appareil_id, p.id, p.categorie, p.marque, p.modele,
                  p.description, p.statut, p.numero_serie
           FROM peripheriques p
           JOIN peripheriques_appareils pa ON pa.peripherique_id = p.id
           WHERE p.client_id=? ORDER BY p.categorie''',
        (cid,)).fetchall()
    conn.close()
    periph_by_app = {}
    for r in periph_rows:
        aid = r[0]
        periph_by_app.setdefault(aid, []).append({
            'id': r[1], 'categorie': r[2], 'marque': r[3],
            'modele': r[4], 'description': r[5], 'statut': r[6], 'numero_serie': r[7]
        })
    for a in appareils:
        a['peripheriques'] = periph_by_app.get(a['id'], [])
    return render_template('liste_appareils.html', appareils=appareils, client=client,
                           clients=get_clients(), client_actif_id=cid, pagination=pagination,
                           sort_col=sort_col, sort_dir=sort_dir,
                           f_types=f_types, f_statut=f_statut, f_av=f_av)

def _save_licences(conn, appareil_id, cid, form):
    """Supprime puis réinsère les licences d'un appareil depuis les données du formulaire."""
    conn.execute('DELETE FROM licences_appareils WHERE appareil_id=?', (appareil_id,))
    editeurs    = form.getlist('lic_editeur')
    produits    = form.getlist('lic_produit')
    cles        = form.getlist('lic_cle')
    contrat_ids = form.getlist('lic_contrat_id')
    now = datetime.utcnow().isoformat()
    for i, editeur in enumerate(editeurs):
        editeur  = editeur.strip()
        produit  = produits[i].strip()  if i < len(produits)    else ''
        cle      = cles[i].strip()      if i < len(cles)         else ''
        cid_lic  = contrat_ids[i]       if i < len(contrat_ids)  else ''
        if not editeur and not produit and not cle:
            continue
        contrat_id_val = None
        try: contrat_id_val = int(cid_lic) if cid_lic else None
        except: pass
        conn.execute(
            '''INSERT INTO licences_appareils
               (appareil_id,client_id,editeur,produit,cle_licence,contrat_id,date_creation)
               VALUES (?,?,?,?,?,?,?)''',
            (appareil_id, cid, editeur, produit, cle, contrat_id_val, now))


def _get_logiciels_metier_list(conn, cid):
    """Retourne la liste des logiciels métier depuis parc_general pour le client donné."""
    parc_row = conn.execute('SELECT logiciels_metier FROM parc_general WHERE client_id=?', (cid,)).fetchone()
    raw = (parc_row[0] if parc_row else '') or ''
    return [l.strip() for l in re.split(r'[,\n]', raw) if l.strip()]


@app.route('/appareil/nouveau', methods=['GET','POST'])
def nouvel_appareil():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    contrats = [row_to_dict(r) for r in conn.execute(
        "SELECT id,titre,fournisseur,statut FROM contrats WHERE client_id=? ORDER BY titre", (cid,)).fetchall()]
    lm_list = _get_logiciels_metier_list(conn, cid)
    conn.close()
    if request.method == 'POST':
        if not can_write():
            flash('Accès en lecture seule — modification non autorisée', 'danger')
            return redirect(url_for('liste_appareils'))
        errs = validate_form([
            ('nom_machine',  'str',   True),
            ('adresse_ip',   'ip',    False),
            ('adresse_mac',  'mac',   False),
            ('date_achat',   'date',  False),
            ('date_fin_garantie', 'date', False),
        ], request.form)
        if errs:
            for e in errs: flash(e, 'danger')
            return redirect(request.url)
        now = datetime.utcnow().isoformat()
        vals = (cid,) + _extract_form(request.form) + (now, now)
        conn = get_db()
        conn.execute('''INSERT INTO appareils (client_id,nom_machine,type_appareil,marque,modele,numero_serie,
            adresse_ip,adresse_mac,nom_dns,utilisateur,service,localisation,date_achat,duree_garantie,
            date_fin_garantie,fournisseur,prix_achat,numero_commande,os,version_os,ram,cpu,stockage,carte_graphique,
            statut,notes,user_login,user_password,admin_login,admin_password,anydesk_id,anydesk_password,
            av_marque,av_nom,av_date_debut,av_date_fin,av_contrat_id,
            edr_marque,edr_nom,edr_date_fin,edr_contrat_id,
            rmm_marque,rmm_nom,rmm_agent_id,rmm_date_fin,rmm_contrat_id,
            logiciels,garantie_alerte_ignoree,date_creation,date_maj)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', vals)
        new_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        _save_licences(conn, new_id, cid, request.form)
        log_history(conn, cid, 'appareil', new_id, request.form.get('nom_machine','') or 'Nouvel appareil', 'Création')
        _sync_appareil_to_periph(conn, new_id, cid)
        conn.commit(); conn.close()
        flash('Appareil ajouté avec succès', 'success')
        return redirect(url_for('liste_appareils'))
    return render_template('form_appareil.html', appareil=None, action='Ajouter',
                           types_appareils=get_liste_cached('types_appareils'),
                           marques_av=get_liste('marques_antivirus'),
                           noms_av=get_liste('noms_antivirus'),
                           marques_edr=get_liste('marques_edr'),
                           noms_edr=get_liste('noms_edr'),
                           marques_rmm=get_liste('marques_rmm'),
                           noms_rmm=get_liste('noms_rmm'),
                           contrats=contrats,
                           sw_courants_groups=SW_COURANTS_GROUPS,
                           sw_courants_all=list(SW_COURANTS_ALL),
                           logiciels_metier_list=lm_list,
                           sw_sel=[],
                           sw_custom_sel=[],
                           licences=[],
                           client=client, clients=get_clients(), client_actif_id=cid)

@app.route('/appareil/<int:id>/editer', methods=['GET','POST'])
def editer_appareil(id):
    cid = get_client_id()
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    if request.method == 'POST':
        if not can_write():
            flash('Accès en lecture seule — modification non autorisée', 'danger')
            return redirect(url_for('liste_appareils'))
        errs = validate_form([
            ('nom_machine',  'str',   True),
            ('adresse_ip',   'ip',    False),
            ('adresse_mac',  'mac',   False),
            ('date_achat',   'date',  False),
            ('date_fin_garantie', 'date', False),
        ], request.form)
        if errs:
            for e in errs: flash(e, 'danger')
            return redirect(request.url)
        now = datetime.utcnow().isoformat()
        _old = row_to_dict(conn.execute('SELECT * FROM appareils WHERE id=?', (id,)).fetchone() or {})
        vals = _extract_form(request.form) + (now, id)
        conn.execute('''UPDATE appareils SET nom_machine=?,type_appareil=?,marque=?,modele=?,numero_serie=?,
            adresse_ip=?,adresse_mac=?,nom_dns=?,utilisateur=?,service=?,localisation=?,date_achat=?,
            duree_garantie=?,date_fin_garantie=?,fournisseur=?,prix_achat=?,numero_commande=?,os=?,
            version_os=?,ram=?,cpu=?,stockage=?,carte_graphique=?,statut=?,notes=?,
            user_login=?,user_password=?,admin_login=?,admin_password=?,anydesk_id=?,anydesk_password=?,
            av_marque=?,av_nom=?,av_date_debut=?,av_date_fin=?,av_contrat_id=?,
            edr_marque=?,edr_nom=?,edr_date_fin=?,edr_contrat_id=?,
            rmm_marque=?,rmm_nom=?,rmm_agent_id=?,rmm_date_fin=?,rmm_contrat_id=?,
            logiciels=?,garantie_alerte_ignoree=?,date_maj=? WHERE id=?''', vals)
        nom = request.form.get('nom_machine','') or f'Appareil #{id}'
        _cols_a = _ENTITE_COLS['appareil']
        _details_a = _diff_json({k: str(_old.get(k,'') or '') for k in _cols_a},
                                 {k: str(request.form.get(k,'') or '') for k in _cols_a})
        _save_licences(conn, id, cid, request.form)
        log_history(conn, cid, 'appareil', id, nom, 'Modification', _details_a)
        _sync_appareil_to_periph(conn, id, cid)
        conn.commit(); conn.close()
        flash('Appareil mis à jour', 'success')
        return redirect(url_for('liste_appareils'))
    a = row_to_dict(conn.execute('SELECT * FROM appareils WHERE id=?', (id,)).fetchone() or {})
    docs = [row_to_dict(r) for r in conn.execute(
        'SELECT id, appareil_id, client_id, nom, description, type_doc, nom_fichier, taille, date_upload, sync_status FROM documents_appareils WHERE appareil_id=? ORDER BY date_upload DESC', (id,)).fetchall()]
    for d in docs:
        d['taille_fmt'] = human_size(d.get('taille', 0))
    contrats = [row_to_dict(r) for r in conn.execute(
        "SELECT id,titre,fournisseur,statut FROM contrats WHERE client_id=? ORDER BY titre", (cid,)).fetchall()]
    licences = [row_to_dict(r) for r in conn.execute(
        'SELECT * FROM licences_appareils WHERE appareil_id=? ORDER BY id', (id,)).fetchall()]
    lm_list = _get_logiciels_metier_list(conn, cid)
    conn.close()
    try:
        sw_sel = json.loads(a.get('logiciels') or '[]')
        if not isinstance(sw_sel, list): sw_sel = []
    except Exception:
        sw_sel = []
    lm_set = set(lm_list)
    sw_custom_sel = [sw for sw in sw_sel if sw not in SW_COURANTS_ALL and sw not in lm_set]
    return render_template('form_appareil.html', appareil=a, documents=docs, action='Modifier',
                           types_appareils=get_liste_cached('types_appareils'),
                           marques_av=get_liste('marques_antivirus'),
                           noms_av=get_liste('noms_antivirus'),
                           marques_edr=get_liste('marques_edr'),
                           noms_edr=get_liste('noms_edr'),
                           marques_rmm=get_liste('marques_rmm'),
                           noms_rmm=get_liste('noms_rmm'),
                           contrats=contrats,
                           sw_courants_groups=SW_COURANTS_GROUPS,
                           sw_courants_all=list(SW_COURANTS_ALL),
                           logiciels_metier_list=lm_list,
                           sw_sel=sw_sel,
                           sw_custom_sel=sw_custom_sel,
                           licences=licences,
                           client=client, clients=get_clients(), client_actif_id=cid)

@app.route('/appareil/<int:id>/supprimer', methods=['POST'])
def supprimer_appareil(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_appareils'))
    cid = get_client_id()
    conn = get_db()
    a = row_to_dict(conn.execute('SELECT nom_machine FROM appareils WHERE id=?',(id,)).fetchone() or {})
    log_history(conn, cid, 'appareil', id, a.get('nom_machine','?'), 'Suppression')
    conn.execute('DELETE FROM appareils WHERE id=?', (id,))
    conn.commit(); conn.close()
    flash('Appareil supprimé', 'info')
    return redirect(url_for('liste_appareils'))

@app.route('/appareil/<int:id>/rdp')
@login_required
def telecharger_rdp(id):
    """Génère et télécharge un fichier .rdp valide pour lancer une session RDP"""
    cid = get_client_id()
    conn = get_db()
    appareil = row_to_dict(conn.execute(
        'SELECT id, nom_machine, adresse_ip FROM appareils WHERE id=? AND client_id=?',
        (id, cid)
    ).fetchone() or {})
    conn.close()

    if not appareil or not appareil.get('adresse_ip'):
        flash('Appareil introuvable ou sans adresse IP', 'danger')
        return redirect(url_for('liste_appareils'))

    ip = appareil['adresse_ip'].strip()
    nom = (appareil.get('nom_machine') or 'rdp').replace(' ', '_').replace('/', '_')

    # Contenu fichier RDP - version minimale qui fonctionne
    rdp_lines = [
        'full address:s:' + ip,
        'prompt for credentials:i:1',
        'username:s:',
        'domain:s:',
        'desktopwidth:i:1920',
        'desktopheight:i:1080',
        'session bpp:i:32',
        'compression:i:1',
        'keyboardhook:i:2',
        'audiocapturemode:i:0',
        'videoplaybackmode:i:1',
        'connection type:i:7',
        'networkautodetect:i:1',
        'bandwidthautodetect:i:1',
        'displayconnectionbar:i:1',
        'redirectclipboard:i:1',
    ]

    # Joindre les lignes avec des sauts de ligne Windows (CRLF)
    rdp_content = '\r\n'.join(rdp_lines) + '\r\n'

    response = make_response(rdp_content)
    response.headers['Content-Type'] = 'application/x-rdp'
    response.headers['Content-Disposition'] = f'attachment; filename="{nom}_{ip}.rdp"'

    return response

@app.route('/plans')
@login_required
def liste_plans():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    plans = [row_to_dict(r) for r in conn.execute(
        'SELECT id,nom,description,date_creation,date_maj FROM plans WHERE client_id=? ORDER BY date_maj DESC',
        (cid,)).fetchall()]
    conn.close()
    return render_template('liste_plans.html', plans=plans, client=client,
                           clients=get_clients(), client_actif_id=cid)


@app.route('/plan/nouveau', methods=['POST'])
@login_required
def nouveau_plan():
    if not can_write():
        flash('Accès en lecture seule', 'danger')
        return redirect(url_for('liste_plans'))
    cid = get_client_id()
    nom = (request.form.get('nom') or '').strip() or 'Nouveau plan'
    desc = request.form.get('description', '')
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO plans (client_id,nom,description,contenu,date_creation,date_maj) VALUES (?,?,?,?,?,?)",
        (cid, nom, desc, '{"elements":[]}', now, now))
    plan_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.commit(); conn.close()
    return redirect(url_for('editer_plan', id=plan_id))


@app.route('/plan/<int:id>')
@login_required
def editer_plan(id):
    cid = get_client_id()
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    plan = row_to_dict(conn.execute(
        'SELECT * FROM plans WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    if not plan:
        conn.close()
        flash('Plan introuvable', 'danger')
        return redirect(url_for('liste_plans'))
    appareils = [row_to_dict(r) for r in conn.execute(
        "SELECT id,nom_machine,type_appareil,statut,en_ligne FROM appareils WHERE client_id=? ORDER BY nom_machine",
        (cid,)).fetchall()]
    for a in appareils:
        k = type_css_filter(a.get('type_appareil', ''))
        a['color'] = cfg_get(f'type_color_{k}') or '#2563eb'
    conn.close()
    # Désérialiser le contenu JSON stocké en base pour éviter le double-encodage en template
    try:
        plan['contenu'] = json.loads(plan.get('contenu') or '{"elements":[]}')
    except Exception:
        plan['contenu'] = {'elements': []}
    return render_template('plan_editeur.html', plan=plan, appareils=appareils,
                           client=client, clients=get_clients(), client_actif_id=cid)


@app.route('/api/plan/<int:id>/sauvegarder', methods=['POST'])
@login_required
def api_plan_save(id):
    if not can_write():
        return jsonify({'ok': False, 'error': 'read-only'}), 403
    cid = get_client_id()
    data = request.get_json(force=True, silent=True) or {}
    contenu = json.dumps(data.get('contenu', {'elements': []}), ensure_ascii=False)
    nom = (data.get('nom') or '').strip()
    now = datetime.utcnow().isoformat()
    conn = get_db()
    if nom:
        conn.execute('UPDATE plans SET contenu=?,nom=?,date_maj=? WHERE id=? AND client_id=?',
                     (contenu, nom, now, id, cid))
    else:
        conn.execute('UPDATE plans SET contenu=?,date_maj=? WHERE id=? AND client_id=?',
                     (contenu, now, id, cid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/plan/<int:id>/supprimer', methods=['POST'])
@login_required
def supprimer_plan(id):
    if not can_write():
        flash('Accès en lecture seule', 'danger')
        return redirect(url_for('liste_plans'))
    cid = get_client_id()
    conn = get_db()
    conn.execute('DELETE FROM plans WHERE id=? AND client_id=?', (id, cid))
    # Enregistrer explicitement la suppression pour la sync Turso
    conn.execute(
        "INSERT OR REPLACE INTO _sync_deletions (tbl, record_id, deleted_at) VALUES ('plans', ?, datetime('now'))",
        (id,))
    conn.commit(); conn.close()
    flash('Plan supprimé', 'info')
    return redirect(url_for('liste_plans'))


@app.route('/scan')
@login_required
def page_scan():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    parc   = row_to_dict(conn.execute('SELECT * FROM parc_general WHERE client_id=?', (cid,)).fetchone() or {})
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT id,nom_machine,adresse_ip,type_appareil,marque,modele FROM appareils WHERE client_id=? ORDER BY nom_machine',
        (cid,)).fetchall()]
    conn.close()
    appareils_ips = [a['adresse_ip'] for a in appareils if a.get('adresse_ip')]
    # Déduire une plage par défaut
    parc_plage = '192.168.1.0/24'
    if appareils_ips:
        try:
            import ipaddress as _ipa
            parc_plage = str(_ipa.ip_interface(appareils_ips[0] + '/24').network)
        except: pass
    # Statut base OUI
    from app import _OUI_FULL, _OUI
    if _OUI_FULL is None:
        _oui_load_full()
    oui_loaded = bool(_OUI_FULL)
    oui_count  = len(_OUI_FULL) if oui_loaded else len(_OUI)

    return render_template('scan_reseau.html', parc=parc, client=client,
                           appareils=appareils, appareils_ips=appareils_ips,
                           parc_plage=parc_plage, oui_loaded=oui_loaded, oui_count=oui_count,
                           clients=get_clients(), client_actif_id=cid)

# ─── SERVICES ────────────────────────────────────────────────────────────────

@app.route('/services')
def liste_services():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    services = [row_to_dict(r) for r in conn.execute(
        'SELECT * FROM services WHERE client_id=? ORDER BY ordre,nom', (cid,)).fetchall()]
    for s in services:
        s['nb_users'] = conn.execute(
            'SELECT COUNT(*) FROM utilisateurs WHERE service_id=? AND statut="actif"', (s['id'],)).fetchone()[0]
    conn.close()
    return render_template('services.html', services=services, client=client,
                           clients=get_clients(), client_actif_id=cid)

@app.route('/service/nouveau', methods=['GET','POST'])
def nouveau_service():
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('index'))
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    conn.close()
    if request.method == 'POST':
        f = request.form; now = datetime.utcnow().isoformat()
        conn = get_db()
        conn.execute('INSERT INTO services (client_id,nom,description,responsable,couleur,ordre,date_creation,date_maj) VALUES (?,?,?,?,?,?,?,?)',
            (cid, f.get('nom',''), f.get('description',''), f.get('responsable',''),
             f.get('couleur','#6a8aaa'), int(f.get('ordre',0) or 0), now, now))
        conn.commit(); conn.close()
        flash('Service créé', 'success')
        return redirect(url_for('liste_services'))
    return render_template('form_service.html', service=None, action='Nouveau',
                           client=client, clients=get_clients(), client_actif_id=cid)

@app.route('/service/<int:id>/editer', methods=['GET','POST'])
def editer_service(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('index'))
    cid = get_client_id()
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    if request.method == 'POST':
        f = request.form; now = datetime.utcnow().isoformat()
        conn.execute('UPDATE services SET nom=?,description=?,responsable=?,couleur=?,ordre=?,date_maj=? WHERE id=? AND client_id=?',
            (f.get('nom',''), f.get('description',''), f.get('responsable',''),
             f.get('couleur','#6a8aaa'), int(f.get('ordre',0) or 0), now, id, cid))
        conn.commit(); conn.close()
        flash('Service mis à jour', 'success')
        return redirect(url_for('liste_services'))
    svc = row_to_dict(conn.execute('SELECT * FROM services WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()
    return render_template('form_service.html', service=svc, action='Modifier',
                           client=client, clients=get_clients(), client_actif_id=cid)

@app.route('/service/<int:id>/supprimer', methods=['POST'])
def supprimer_service(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('index'))
    cid = get_client_id()
    conn = get_db()
    conn.execute('DELETE FROM services WHERE id=? AND client_id=?', (id, cid))
    conn.commit(); conn.close()
    flash('Service supprimé', 'info')
    return redirect(url_for('liste_services'))

# ─── TYPES DE DROITS ─────────────────────────────────────────────────────────

CATEGORIES_DROITS = ['Dossiers réseau', 'Logiciels', 'Messagerie', 'Applications web',
                     'Accès physique', 'Administration', 'Autre']

def get_types_droits(cid):
    conn = get_db()
    types = [row_to_dict(r) for r in conn.execute(
        'SELECT * FROM types_droits WHERE client_id=? ORDER BY categorie,ordre,nom', (cid,)).fetchall()]
    conn.close()
    return types

@app.route('/types-droits')
def liste_types_droits():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    conn.close()
    types = get_types_droits(cid)
    return render_template('types_droits.html', types=types, client=client,
                           clients=get_clients(), client_actif_id=cid,
                           categories_droits=CATEGORIES_DROITS)

@app.route('/api/type-droit', methods=['POST'])
def api_creer_type_droit():
    cid = get_client_id()
    f = request.json or {}
    now = datetime.utcnow().isoformat()
    conn = get_db()
    c = conn.execute('INSERT INTO types_droits (client_id,categorie,nom,description,icone,ordre) VALUES (?,?,?,?,?,?)',
        (cid, f.get('categorie','Autre'), f.get('nom',''), f.get('description',''),
         f.get('icone','🔑'), int(f.get('ordre',0) or 0)))
    tid = c.lastrowid
    conn.commit()
    row = row_to_dict(conn.execute('SELECT * FROM types_droits WHERE id=?', (tid,)).fetchone())
    conn.close()
    return jsonify(row)

@app.route('/api/type-droit/<int:id>', methods=['PUT','DELETE'])
def api_type_droit(id):
    cid = get_client_id()
    conn = get_db()
    if request.method == 'DELETE':
        conn.execute('DELETE FROM types_droits WHERE id=? AND client_id=?', (id, cid))
        conn.commit(); conn.close()
        return jsonify({'ok': True})
    f = request.json or {}
    conn.execute('UPDATE types_droits SET categorie=?,nom=?,description=?,icone=?,ordre=? WHERE id=? AND client_id=?',
        (f.get('categorie',''), f.get('nom',''), f.get('description',''),
         f.get('icone','🔑'), int(f.get('ordre',0) or 0), id, cid))
    conn.commit()
    row = row_to_dict(conn.execute('SELECT * FROM types_droits WHERE id=?', (id,)).fetchone() or {})
    conn.close()
    return jsonify(row)

# ─── UTILISATEURS ────────────────────────────────────────────────────────────

@app.route('/utilisateurs')
@login_required
def liste_utilisateurs():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    filtre_svc = request.args.get('service', '')
    filtre_statut = request.args.get('statut', 'actif')
    q = 'SELECT u.*, s.nom as service_nom, s.couleur as service_couleur FROM utilisateurs u LEFT JOIN services s ON u.service_id=s.id WHERE u.client_id=?'
    params = [cid]
    if filtre_svc:
        q += ' AND u.service_id=?'; params.append(int(filtre_svc))
    if filtre_statut:
        q += ' AND u.statut=?'; params.append(filtre_statut)
    q += ' ORDER BY s.nom, u.nom, u.prenom'
    users = [row_to_dict(r) for r in conn.execute(q, params).fetchall()]
    services = [row_to_dict(r) for r in conn.execute(
        'SELECT * FROM services WHERE client_id=? ORDER BY ordre,nom', (cid,)).fetchall()]
    conn.close()
    return render_template('utilisateurs.html', utilisateurs=users, services=services,
                           client=client, clients=get_clients(), client_actif_id=cid,
                           filtre_svc=filtre_svc, filtre_statut=filtre_statut)

@app.route('/utilisateur/nouveau', methods=['GET','POST'])
def nouvel_utilisateur():
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_utilisateurs'))
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    services = [row_to_dict(r) for r in conn.execute(
        'SELECT * FROM services WHERE client_id=? ORDER BY ordre,nom', (cid,)).fetchall()]
    conn.close()
    if request.method == 'POST':
        f = request.form; now = datetime.utcnow().isoformat()
        svc_id = int(f.get('service_id') or 0) or None
        conn = get_db()
        c = conn.execute('''INSERT INTO utilisateurs (client_id,service_id,prenom,nom,poste,email,
            telephone,login_windows,login_mail,statut,notes,date_creation,date_maj)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (cid, svc_id, f.get('prenom',''), f.get('nom',''), f.get('poste',''),
             f.get('email',''), f.get('telephone',''), f.get('login_windows',''),
             f.get('login_mail',''), f.get('statut','actif'), f.get('notes',''), now, now))
        uid = c.lastrowid
        nom_u = (f.get('prenom','') + ' ' + f.get('nom','')).strip() or 'Nouvel utilisateur'
        log_history(conn, cid, 'utilisateur', uid, nom_u, 'Création')
        conn.commit(); conn.close()
        flash('Utilisateur créé', 'success')
        return redirect(url_for('droits_utilisateur', id=uid))
    return render_template('form_utilisateur.html', utilisateur=None, action='Nouveau',
                           services=services, client=client, clients=get_clients(), client_actif_id=cid)

@app.route('/utilisateur/<int:id>/editer', methods=['GET','POST'])
def editer_utilisateur(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_utilisateurs'))
    cid = get_client_id()
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    services = [row_to_dict(r) for r in conn.execute(
        'SELECT * FROM services WHERE client_id=? ORDER BY ordre,nom', (cid,)).fetchall()]
    if request.method == 'POST':
        f = request.form; now = datetime.utcnow().isoformat()
        _old = row_to_dict(conn.execute('SELECT * FROM utilisateurs WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
        svc_id = int(f.get('service_id') or 0) or None
        conn.execute('''UPDATE utilisateurs SET service_id=?,prenom=?,nom=?,poste=?,email=?,
            telephone=?,login_windows=?,login_mail=?,statut=?,notes=?,date_maj=? WHERE id=? AND client_id=?''',
            (svc_id, f.get('prenom',''), f.get('nom',''), f.get('poste',''),
             f.get('email',''), f.get('telephone',''), f.get('login_windows',''),
             f.get('login_mail',''), f.get('statut','actif'), f.get('notes',''), now, id, cid))
        nom = (request.form.get('prenom','') + ' ' + request.form.get('nom','')).strip() or f'Utilisateur #{id}'
        _cols_u = _ENTITE_COLS['utilisateur']
        _details_u = _diff_json({k: str(_old.get(k,'') or '') for k in _cols_u},
                                  {k: str(f.get(k,'') or '') for k in _cols_u})
        log_history(conn, cid, 'utilisateur', id, nom, 'Modification', _details_u)
        conn.commit(); conn.close()
        flash('Utilisateur mis à jour', 'success')
        return redirect(url_for('liste_utilisateurs'))
    u = row_to_dict(conn.execute('SELECT * FROM utilisateurs WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()
    return render_template('form_utilisateur.html', utilisateur=u, action='Modifier',
                           services=services, client=client, clients=get_clients(), client_actif_id=cid)

@app.route('/utilisateur/<int:id>/supprimer', methods=['POST'])
def supprimer_utilisateur(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_utilisateurs'))
    cid = get_client_id()
    conn = get_db()
    u = row_to_dict(conn.execute('SELECT prenom,nom FROM utilisateurs WHERE id=?',(id,)).fetchone() or {})
    nom = (u.get('prenom','') + ' ' + u.get('nom','')).strip() or '?'
    log_history(conn, cid, 'utilisateur', id, nom, 'Suppression')
    conn.execute('DELETE FROM utilisateurs WHERE id=?', (id,))
    conn.commit(); conn.close()
    flash('Utilisateur supprimé', 'info')
    return redirect(url_for('liste_utilisateurs'))

@app.route('/utilisateur/<int:id>/droits')
def droits_utilisateur(id):
    cid = get_client_id()
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    u = row_to_dict(conn.execute(
        'SELECT u.*, s.nom as service_nom, s.couleur as service_couleur FROM utilisateurs u LEFT JOIN services s ON u.service_id=s.id WHERE u.id=? AND u.client_id=?',
        (id, cid)).fetchone() or {})
    droits = [row_to_dict(r) for r in conn.execute(
        'SELECT d.*, t.icone, t.categorie as t_categorie FROM droits_utilisateurs d LEFT JOIN types_droits t ON d.type_droit_id=t.id WHERE d.utilisateur_id=? ORDER BY d.categorie, d.nom_droit',
        (id,)).fetchall()]
    types = get_types_droits(cid)
    conn.close()
    # Grouper par catégorie
    cats = {}
    for d in droits:
        cat = d.get('categorie') or 'Autre'
        cats.setdefault(cat, []).append(d)
    nb_droits_total = sum(len(v) for v in cats.values())
    return render_template('droits_utilisateur.html', utilisateur=u, droits_par_cat=cats,
                           nb_droits_total=nb_droits_total,
                           types=types, categories_droits=CATEGORIES_DROITS,
                           client=client, clients=get_clients(), client_actif_id=cid)

@app.route('/api/droit', methods=['POST'])
def api_ajouter_droit():
    cid = get_client_id()
    f = request.json or {}
    uid = f.get('utilisateur_id')
    now = datetime.utcnow().isoformat()
    conn = get_db()
    c = conn.execute('''INSERT INTO droits_utilisateurs
        (utilisateur_id, client_id, categorie, type_droit_id, nom_droit, valeur, niveau, notes, date_attribution)
        VALUES (?,?,?,?,?,?,?,?,?)''',
        (uid, cid, f.get('categorie',''), f.get('type_droit_id') or None,
         f.get('nom_droit',''), f.get('valeur',''), f.get('niveau','lecture'),
         f.get('notes',''), now))
    did = c.lastrowid
    conn.commit()
    row = row_to_dict(conn.execute('SELECT * FROM droits_utilisateurs WHERE id=?', (did,)).fetchone())
    conn.close()
    return jsonify(row)

@app.route('/api/droit/<int:id>', methods=['PUT','DELETE'])
def api_droit(id):
    cid = get_client_id()
    conn = get_db()
    if request.method == 'DELETE':
        conn.execute('DELETE FROM droits_utilisateurs WHERE id=?', (id,))
        conn.commit(); conn.close()
        return jsonify({'ok': True})
    f = request.json or {}
    conn.execute('UPDATE droits_utilisateurs SET categorie=?,nom_droit=?,valeur=?,niveau=?,notes=? WHERE id=?',
        (f.get('categorie',''), f.get('nom_droit',''), f.get('valeur',''),
         f.get('niveau','lecture'), f.get('notes',''), id))
    conn.commit()
    row = row_to_dict(conn.execute('SELECT * FROM droits_utilisateurs WHERE id=?', (id,)).fetchone() or {})
    conn.close()
    return jsonify(row)

@app.route('/api/utilisateurs')
def api_utilisateurs():
    cid = get_client_id()
    conn = get_db()
    users = [row_to_dict(r) for r in conn.execute(
        'SELECT id, prenom, nom, service_id FROM utilisateurs WHERE client_id=? AND statut="actif" ORDER BY nom', (cid,)).fetchall()]
    conn.close()
    return jsonify(users)

# ─── IDENTIFIANTS ────────────────────────────────────────────────────────────

@app.route('/identifiants')
@login_required
def liste_identifiants():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    page = request.args.get('page', 1, type=int)
    filtre_cat = request.args.get('cat', '')
    if filtre_cat:
        q, params = 'SELECT * FROM identifiants WHERE client_id=? AND categorie=? ORDER BY categorie,nom', (cid, filtre_cat)
    else:
        q, params = 'SELECT * FROM identifiants WHERE client_id=? ORDER BY categorie,nom', (cid,)
    rows, pagination = paginate(q, params, page)
    ids_ = [row_to_dict(r) for r in rows]
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    cats = [r[0] for r in conn.execute(
        'SELECT DISTINCT categorie FROM identifiants WHERE client_id=? ORDER BY categorie', (cid,)).fetchall()]

    # Générer les statistiques par catégorie
    stats = {'total': 0}
    total_result = conn.execute('SELECT COUNT(*) FROM identifiants WHERE client_id=?', (cid,)).fetchone()
    stats['total'] = total_result[0] if total_result else 0
    for cat in cats:
        count_result = conn.execute('SELECT COUNT(*) FROM identifiants WHERE client_id=? AND categorie=?', (cid, cat)).fetchone()
        stats[cat] = count_result[0] if count_result else 0

    conn.close()
    for i in ids_:
        if i.get('date_expiration'):
            try:
                d = date.fromisoformat(i['date_expiration'])
                i['expire_bientot'] = (d - date.today()).days <= 30
                i['expire_depasse'] = d < date.today()
                i['date_expiration_fmt'] = d.strftime('%d/%m/%Y')
            except: i['expire_bientot'] = i['expire_depasse'] = False; i['date_expiration_fmt'] = ''
        else: i['expire_bientot'] = i['expire_depasse'] = False; i['date_expiration_fmt'] = ''
    # Récupérer les identifiants WiFi du parc général (en lecture seule)
    conn2 = get_db()
    parc = row_to_dict(conn2.execute('SELECT * FROM parc_general WHERE client_id=?', (cid,)).fetchone() or {})
    conn2.close()
    wifi_parc = []
    if parc.get('wifi_ssid'):
        wifi_parc.append({
            'id': None, 'from_parc': True,
            'nom': parc['wifi_ssid'] + ' (Parc général)',
            'categorie': 'Wi-Fi',
            'login': parc.get('wifi_ssid',''),
            'mot_de_passe': parc.get('wifi_password',''),
            'wifi_ssid': parc.get('wifi_ssid',''),
            'wifi_securite': parc.get('wifi_securite','WPA2'),
            'description': 'Réseau principal — depuis le Parc général',
            'url': '', 'notes': parc.get('wifi_notes',''),
            'expire_bientot': False, 'expire_depasse': False, 'date_expiration_fmt': '',
        })
    if parc.get('wifi_ssid2'):
        wifi_parc.append({
            'id': None, 'from_parc': True,
            'nom': parc['wifi_ssid2'] + ' (Parc général)',
            'categorie': 'Wi-Fi',
            'login': parc.get('wifi_ssid2',''),
            'mot_de_passe': parc.get('wifi_password2',''),
            'wifi_ssid': parc.get('wifi_ssid2',''),
            'wifi_securite': parc.get('wifi_securite2','WPA2'),
            'description': 'Réseau invités — depuis le Parc général',
            'url': '', 'notes': '',
            'expire_bientot': False, 'expire_depasse': False, 'date_expiration_fmt': '',
        })
    return render_template('identifiants.html', identifiants=ids_, wifi_parc=wifi_parc, client=client,
                           clients=get_clients(), client_actif_id=cid,
                           categories=get_liste_cached('categories_identifiants'), cats_utilisees=cats,
                           filtre_cat=filtre_cat, pagination=pagination, stats=stats)

@app.route('/identifiant/nouveau', methods=['GET','POST'])
def nouvel_identifiant():
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_identifiants'))
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    conn.close()
    if request.method == 'POST':
        f = request.form; now = datetime.utcnow().isoformat()
        errs = validate_form([
            ('nom',            'str',   True),
            ('url',            'url',   False),
            ('date_expiration','date',  False),
        ], f)
        if errs:
            for e in errs: flash(e, 'danger')
            return redirect(request.url)
        conn = get_db()
        # ✅ Chiffrer le mot de passe avant stockage
        crypto = get_crypto_manager(os.path.join(_data_base, 'secret.key'))
        mdp_chiffre = crypto.encrypt(f.get('mot_de_passe','')) if f.get('mot_de_passe') else ''
        conn.execute('''INSERT INTO identifiants (client_id,categorie,nom,login,mot_de_passe,url,
            description,notes,date_expiration,wifi_ssid,wifi_securite,date_creation,date_maj)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (cid, f.get('categorie',''), f.get('nom',''), f.get('login',''), mdp_chiffre,
             f.get('url',''), f.get('description',''), f.get('notes',''),
             f.get('date_expiration',''),
             f.get('wifi_ssid','') if f.get('categorie') == 'Wi-Fi' else '',
             f.get('wifi_securite','WPA2') if f.get('categorie') == 'Wi-Fi' else '',
             now, now))
        new_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        nom = request.form.get('nom','') or 'Nouvel identifiant'
        log_history(conn, cid, 'identifiant', new_id, nom, 'Création')
        conn.commit(); conn.close()
        flash('Identifiant ajouté', 'success')
        return redirect(url_for('liste_identifiants'))
    return render_template('form_identifiant.html', identifiant=None, action='Ajouter',
                           client=client, clients=get_clients(), client_actif_id=cid,
                           categories=get_liste_cached('categories_identifiants'))

@app.route('/identifiant/<int:id>/editer', methods=['GET','POST'])
def editer_identifiant(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_identifiants'))
    cid = get_client_id()
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    if request.method == 'POST':
        f = request.form; now = datetime.utcnow().isoformat()
        errs = validate_form([
            ('nom',            'str',   True),
            ('url',            'url',   False),
            ('date_expiration','date',  False),
        ], f)
        if errs:
            for e in errs: flash(e, 'danger')
            return redirect(request.url)
        _old = row_to_dict(conn.execute('SELECT * FROM identifiants WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
        # ✅ Chiffrer le mot de passe avant mise à jour
        crypto = get_crypto_manager(os.path.join(_data_base, 'secret.key'))
        mdp_chiffre = crypto.encrypt(f.get('mot_de_passe','')) if f.get('mot_de_passe') else ''
        conn.execute('''UPDATE identifiants SET categorie=?,nom=?,login=?,mot_de_passe=?,url=?,
            description=?,notes=?,date_expiration=?,wifi_ssid=?,wifi_securite=?,date_maj=?
            WHERE id=? AND client_id=?''',
            (f.get('categorie',''), f.get('nom',''), f.get('login',''), mdp_chiffre,
             f.get('url',''), f.get('description',''), f.get('notes',''),
             f.get('date_expiration',''),
             f.get('wifi_ssid','') if f.get('categorie') == 'Wi-Fi' else '',
             f.get('wifi_securite','WPA2') if f.get('categorie') == 'Wi-Fi' else '',
             now, id, cid))
        nom = request.form.get('nom','') or f'Identifiant #{id}'
        _cols_i = _ENTITE_COLS['identifiant']
        _details_i = _diff_json({k: str(_old.get(k,'') or '') for k in _cols_i},
                                  {k: str(f.get(k,'') or '') for k in _cols_i})
        log_history(conn, cid, 'identifiant', id, nom, 'Modification', _details_i)
        conn.commit(); conn.close()
        flash('Identifiant mis à jour', 'success')
        return redirect(url_for('liste_identifiants'))
    ident = row_to_dict(conn.execute('SELECT * FROM identifiants WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()
    # ✅ Déchiffrer le mot de passe pour l'affichage
    if ident and ident.get('mot_de_passe'):
        crypto = get_crypto_manager(os.path.join(_data_base, 'secret.key'))
        ident['mot_de_passe'] = crypto.decrypt(ident['mot_de_passe']) or ident['mot_de_passe']
    return render_template('form_identifiant.html', identifiant=ident, action='Modifier',
                           client=client, clients=get_clients(), client_actif_id=cid,
                           categories=get_liste_cached('categories_identifiants'))

@app.route('/identifiant/<int:id>/supprimer', methods=['POST'])
def supprimer_identifiant(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_identifiants'))
    cid = get_client_id()
    conn = get_db()
    idn = row_to_dict(conn.execute('SELECT nom FROM identifiants WHERE id=?',(id,)).fetchone() or {})
    log_history(conn, cid, 'identifiant', id, idn.get('nom','?'), 'Suppression')
    conn.execute('DELETE FROM identifiants WHERE id=? AND client_id=?', (id, cid))
    conn.commit(); conn.close()
    flash('Identifiant supprimé', 'info')
    return redirect(url_for('liste_identifiants'))

@app.route('/api/identifiant/<int:id>/mdp')
def api_get_mdp(id):
    cid = get_client_id()
    conn = get_db()
    row = conn.execute('SELECT mot_de_passe FROM identifiants WHERE id=? AND client_id=?', (id, cid)).fetchone()
    conn.close()
    if not row: return jsonify({'error': 'not found'}), 404
    # ✅ Déchiffrer le mot de passe
    crypto = get_crypto_manager(os.path.join(_data_base, 'secret.key'))
    mdp_dechiffre = crypto.decrypt(row[0]) if row[0] else ''
    return jsonify({'mdp': mdp_dechiffre})

# ─── DOCUMENTS APPAREILS ─────────────────────────────────────────────────────

@app.route('/appareil/<int:id>/documents')
def documents_appareil(id):
    cid = get_client_id()
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    a = row_to_dict(conn.execute('SELECT * FROM appareils WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    docs = [row_to_dict(r) for r in conn.execute(
        'SELECT id, appareil_id, client_id, nom, description, type_doc, nom_fichier, taille, date_upload, sync_status FROM documents_appareils WHERE appareil_id=? ORDER BY date_upload DESC', (id,)).fetchall()]

    # Fetch related interventions
    interventions = [fmt_intervention(row_to_dict(r)) for r in conn.execute(
        'SELECT i.* FROM interventions i JOIN interventions_appareils ia ON i.id=ia.intervention_id '
        'WHERE ia.appareil_id=? AND i.statut != ? ORDER BY i.date_intervention DESC LIMIT 10',
        (id, 'archivee')).fetchall()]

    conn.close()
    for d in docs:
        d['taille_fmt'] = human_size(d.get('taille', 0))
    return render_template('documents_appareil.html', appareil=a, documents=docs, interventions=interventions,
                           client=client, clients=get_clients(), client_actif_id=cid)

@app.route('/appareil/<int:id>/documents/upload', methods=['POST'])
def upload_document(id):
    cid = get_client_id()
    if 'fichier' not in request.files:
        flash('Aucun fichier sélectionné', 'danger')
        return redirect(url_for('documents_appareil', id=id))
    f = request.files['fichier']
    if not f.filename or not allowed_file(f.filename):
        flash('Type de fichier non autorisé', 'danger')
        return redirect(url_for('documents_appareil', id=id))
    # Secure filename with appareil prefix
    ext = f.filename.rsplit('.', 1)[1].lower()
    safe = secure_filename(f.filename)
    # Unique filename: appareil_id + timestamp + name
    unique = f"app{id}_{int(time.time())}_{safe}"
    save_path = os.path.join(UPLOAD_FOLDER, unique)
    f.save(save_path)
    taille = os.path.getsize(save_path)

    nom = request.form.get('nom', '') or f.filename
    desc = request.form.get('description', '')
    type_doc = request.form.get('type_doc', '')
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute('''INSERT INTO documents_appareils
        (appareil_id,client_id,nom,description,type_doc,nom_fichier,taille,date_upload,contenu_blob,sync_status,date_sync)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (id, cid, nom, desc, type_doc, unique, taille, now, None, 'local', ''))

    # Log document upload
    app_title = conn.execute('SELECT nom_machine FROM appareils WHERE id=? AND client_id=?', (id, cid)).fetchone()
    app_name = app_title[0] if app_title else f'Appareil #{id}'
    log_history(conn, cid, 'appareil', id, app_name, 'Ajout de document',
                _diff_json({}, {'nom': nom, 'fichier': unique, 'type_doc': type_doc}))

    conn.commit(); conn.close()
    flash(f'Document « {nom} » uploadé avec succès', 'success')
    next_url = request.form.get('next') or url_for('editer_appareil', id=id)
    return redirect(next_url)

@app.route('/document/<int:id>/telecharger')
def telecharger_document(id):
    cid = get_client_id()
    conn = get_db()
    doc = row_to_dict(conn.execute('SELECT id, appareil_id, client_id, nom, nom_fichier, contenu_blob FROM documents_appareils WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()
    if not doc:
        flash('Document introuvable', 'danger')
        return redirect(url_for('liste_appareils'))

    # Préférer servir depuis BLOB si disponible (synced)
    if doc.get('contenu_blob'):
        return send_file(
            io.BytesIO(doc['contenu_blob']),
            as_attachment=True,
            download_name=doc['nom']
        )

    # Fallback: servir depuis fichier local
    return send_from_directory(UPLOAD_FOLDER, doc['nom_fichier'], as_attachment=True, download_name=doc['nom'])

@app.route('/document/<int:id>/supprimer', methods=['POST'])
def supprimer_document(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_appareils'))
    cid = get_client_id()
    conn = get_db()
    doc = row_to_dict(conn.execute('SELECT id, appareil_id, client_id, nom, nom_fichier, contenu_blob FROM documents_appareils WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    appareil_id = doc.get('appareil_id', 0)
    if doc:
        conn.execute('DELETE FROM documents_appareils WHERE id=?', (id,))

        # Log document deletion
        app_title = conn.execute('SELECT nom_machine FROM appareils WHERE id=? AND client_id=?', (appareil_id, cid)).fetchone()
        app_name = app_title[0] if app_title else f'Appareil #{appareil_id}'
        log_history(conn, cid, 'appareil', appareil_id, app_name, 'Suppression de document',
                    _diff_json({'nom': doc.get('nom', ''), 'fichier': doc.get('nom_fichier', '')}, {}))

        conn.commit()
        try:
            os.remove(os.path.join(UPLOAD_FOLDER, doc['nom_fichier']))
        except:
            pass
    conn.close()
    next_url = request.args.get('next') or url_for('editer_appareil', id=appareil_id)
    flash('Document supprimé', 'info')
    return redirect(next_url)

@app.route('/document/<int:id>/apercu')
def apercu_document(id):
    cid = get_client_id()
    conn = get_db()
    doc = row_to_dict(conn.execute('SELECT id, appareil_id, client_id, nom, nom_fichier, contenu_blob FROM documents_appareils WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()
    if not doc:
        return 'Not found', 404

    # Préférer servir depuis BLOB si disponible (synced)
    if doc.get('contenu_blob'):
        return send_file(
            io.BytesIO(doc['contenu_blob']),
            as_attachment=False
        )

    # Fallback: servir depuis fichier local
    return send_from_directory(UPLOAD_FOLDER, doc['nom_fichier'], as_attachment=False)


# ─── API APPAREIL : IGNORER ALERTE GARANTIE ──────────────────────────────────

@app.route('/api/appareil/<int:id>/garantie-ignorer', methods=['POST'])
@login_required
def api_garantie_ignorer(id):
    """Active ou désactive le flag 'ignorer alerte garantie' sur un appareil."""
    if not can_write():
        return jsonify({'error': 'Accès en lecture seule'}), 403
    ignorer = (request.json or {}).get('ignorer', True)
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute('UPDATE appareils SET garantie_alerte_ignoree=?, date_maj=? WHERE id=?',
                 (1 if ignorer else 0, now, id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'garantie_alerte_ignoree': bool(ignorer)})


# ─── API DOCUMENTS MODALE ────────────────────────────────────────────────────

@app.route('/api/appareil/<int:id>/documents')
def api_docs_appareil(id):
    cid = get_client_id()
    conn = get_db()
    docs = [row_to_dict(r) for r in conn.execute(
        'SELECT id, appareil_id, client_id, nom, description, type_doc, nom_fichier, taille, date_upload, sync_status FROM documents_appareils WHERE appareil_id=? AND client_id=? ORDER BY date_upload DESC',
        (id, cid)).fetchall()]
    conn.close()
    for d in docs:
        d['taille_fmt'] = human_size(d.get('taille', 0))
        ext = (d.get('nom_fichier','').rsplit('.',1)[-1] or '').lower()
        d['is_img'] = ext in ('png','jpg','jpeg','gif','webp')
        d['is_pdf'] = ext == 'pdf'
    return jsonify(docs)

@app.route('/api/peripherique/<int:id>/documents')
def api_docs_peripherique(id):
    cid = get_client_id()
    conn = get_db()
    docs = [row_to_dict(r) for r in conn.execute(
        'SELECT id, peripherique_id, client_id, nom, description, type_doc, nom_fichier, taille, date_upload, sync_status FROM documents_peripheriques WHERE peripherique_id=? AND client_id=? ORDER BY date_upload DESC',
        (id, cid)).fetchall()]
    conn.close()
    for d in docs:
        d['taille_fmt'] = human_size(d.get('taille', 0))
        ext = (d.get('nom_fichier','').rsplit('.',1)[-1] or '').lower()
        d['is_img'] = ext in ('png','jpg','jpeg','gif','webp')
        d['is_pdf'] = ext == 'pdf'
    return jsonify(docs)

# ─── DOCUMENTS PÉRIPHÉRIQUES ───────────────────────────────────────

@app.route('/peripherique/<int:id>/documents/upload', methods=['POST'])
def upload_doc_peripherique(id):
    cid = get_client_id()
    if 'fichier' not in request.files:
        flash('Aucun fichier sélectionné', 'danger')
        return redirect(url_for('editer_peripherique', id=id))
    f = request.files['fichier']
    if not f.filename or not allowed_file(f.filename):
        flash('Type de fichier non autorisé', 'danger')
        return redirect(url_for('editer_peripherique', id=id))
    safe = secure_filename(f.filename)
    unique = f"per{id}_{int(time.time())}_{safe}"
    save_path = os.path.join(UPLOAD_FOLDER, unique)
    f.save(save_path)
    taille = os.path.getsize(save_path)

    nom = request.form.get('nom', '') or f.filename
    desc = request.form.get('description', '')
    type_doc = request.form.get('type_doc', '')
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute('''INSERT INTO documents_peripheriques
        (peripherique_id,client_id,nom,description,type_doc,nom_fichier,taille,date_upload,contenu_blob,sync_status,date_sync)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (id, cid, nom, desc, type_doc, unique, taille, now, None, 'local', ''))

    # Log document upload
    per_title = conn.execute('SELECT CONCAT(marque, \' \', modele) FROM peripheriques WHERE id=? AND client_id=?', (id, cid)).fetchone()
    per_name = per_title[0] if per_title else f'Périphérique #{id}'
    log_history(conn, cid, 'peripherique', id, per_name, 'Ajout de document',
                _diff_json({}, {'nom': nom, 'fichier': unique, 'type_doc': type_doc}))

    conn.commit(); conn.close()
    flash(f'Document « {nom} » uploadé', 'success')
    return redirect(url_for('editer_peripherique', id=id))

@app.route('/doc-peripherique/<int:id>/telecharger')
def telecharger_doc_peripherique(id):
    cid = get_client_id()
    conn = get_db()
    doc = row_to_dict(conn.execute(
        'SELECT id, peripherique_id, client_id, nom, nom_fichier, contenu_blob FROM documents_peripheriques WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()
    if not doc: return 'Not found', 404

    # Préférer servir depuis BLOB si disponible (synced)
    if doc.get('contenu_blob'):
        return send_file(
            io.BytesIO(doc['contenu_blob']),
            as_attachment=True,
            download_name=doc['nom']
        )

    # Fallback: servir depuis fichier local
    return send_from_directory(UPLOAD_FOLDER, doc['nom_fichier'], as_attachment=True, download_name=doc['nom'])

@app.route('/doc-peripherique/<int:id>/apercu')
def apercu_doc_peripherique(id):
    cid = get_client_id()
    conn = get_db()
    doc = row_to_dict(conn.execute(
        'SELECT id, peripherique_id, client_id, nom, nom_fichier, contenu_blob FROM documents_peripheriques WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()
    if not doc: return 'Not found', 404
    return send_from_directory(UPLOAD_FOLDER, doc['nom_fichier'], as_attachment=False)

@app.route('/doc-peripherique/<int:id>/supprimer', methods=['POST'])
def supprimer_doc_peripherique(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_peripheriques'))
    cid = get_client_id()
    conn = get_db()
    doc = row_to_dict(conn.execute(
        'SELECT id, peripherique_id, client_id, nom, nom_fichier, contenu_blob FROM documents_peripheriques WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    periph_id = doc.get('peripherique_id', 0)
    if doc:
        conn.execute('DELETE FROM documents_peripheriques WHERE id=?', (id,))

        # Log document deletion
        per_title = conn.execute('SELECT CONCAT(marque, \' \', modele) FROM peripheriques WHERE id=? AND client_id=?', (periph_id, cid)).fetchone()
        per_name = per_title[0] if per_title else f'Périphérique #{periph_id}'
        log_history(conn, cid, 'peripherique', periph_id, per_name, 'Suppression de document',
                    _diff_json({'nom': doc.get('nom', ''), 'fichier': doc.get('nom_fichier', '')}, {}))

        conn.commit()
        try: os.remove(os.path.join(UPLOAD_FOLDER, doc['nom_fichier']))
        except: pass
    conn.close()
    flash('Document supprimé', 'info')
    return redirect(url_for('editer_peripherique', id=periph_id))

# ─── BAIE DE BRASSAGE ────────────────────────────────────────────────────────

@app.route('/baie')
@login_required
def baie_brassage():
    cid = get_client_id()
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    parc = row_to_dict(conn.execute('SELECT * FROM parc_general WHERE client_id=?', (cid,)).fetchone() or {})
    nb_u = parc.get('baie_nb_u', 12) or 12
    # Récupérer les slots existants
    slots_db = [row_to_dict(r) for r in conn.execute(
        '''SELECT s.*, a.nom_machine, a.type_appareil, a.adresse_ip, a.marque, a.modele, a.en_ligne
           FROM baie_slots s LEFT JOIN appareils a ON s.appareil_id=a.id
           WHERE s.client_id=? ORDER BY s.position''', (cid,)).fetchall()]
    # Appareils disponibles pour association
    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT id,nom_machine,type_appareil,adresse_ip,marque,modele FROM appareils WHERE client_id=? ORDER BY nom_machine',
        (cid,)).fetchall()]
    photos = [row_to_dict(r) for r in conn.execute(
        'SELECT * FROM baie_photos WHERE client_id=? ORDER BY date_upload DESC', (cid,)).fetchall()]
    conn.close()
    # Construire la grille : dict (position, col_index) -> slot
    slots_map = {}
    for s in slots_db:
        key = (s['position'], s.get('col_index', 0))
        slots_map[key] = s
    return render_template('baie_brassage.html', parc=parc, client=client, nb_u=nb_u,
                           slots_map=slots_map, slots_db=slots_db, appareils=appareils,
                           photos=photos, clients=get_clients(), client_actif_id=cid)

@app.route('/api/baie/slot', methods=['POST'])
def api_baie_ajouter_slot():
    cid = get_client_id()
    f = request.json or {}
    conn = get_db()
    pos = f.get('position', 1)
    col = f.get('col_index', 0)
    # Supprimer l'ancien slot à cette position+col si existe
    conn.execute('DELETE FROM baie_slots WHERE client_id=? AND position=? AND col_index=?', (cid, pos, col))
    baie_nom = f.get('baie_nom', 'Baie principale')
    conn.execute('''INSERT INTO baie_slots
        (client_id,position,col_index,hauteur_u,appareil_id,nom_custom,type_equipement,couleur,description,baie_nom,date_maj)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (cid, pos, col, f.get('hauteur_u', 1),
         f.get('appareil_id') or None, f.get('nom_custom', ''),
         f.get('type_equipement', ''), f.get('couleur', '#1e3a5f'),
         f.get('description', ''), baie_nom, datetime.utcnow().isoformat()))
    conn.commit()
    sid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    slot = row_to_dict(conn.execute(
        '''SELECT s.*, a.nom_machine, a.type_appareil, a.adresse_ip, a.marque, a.en_ligne
           FROM baie_slots s LEFT JOIN appareils a ON s.appareil_id=a.id WHERE s.id=?''', (sid,)).fetchone() or {})
    conn.close()
    return jsonify(slot)

@app.route('/api/baie/slot/<int:id>', methods=['PUT','DELETE'])
def api_baie_slot(id):
    cid = get_client_id()
    conn = get_db()
    if request.method == 'DELETE':
        conn.execute('DELETE FROM baie_slots WHERE id=? AND client_id=?', (id, cid))
        conn.commit(); conn.close()
        return jsonify({'ok': True})
    f = request.json or {}
    conn.execute('''UPDATE baie_slots SET position=?,hauteur_u=?,appareil_id=?,
        nom_custom=?,type_equipement=?,couleur=?,description=?,date_maj=? WHERE id=? AND client_id=?''',
        (f.get('position',1), f.get('hauteur_u',1), f.get('appareil_id') or None,
         f.get('nom_custom',''), f.get('type_equipement',''),
         f.get('couleur','#1e3a5f'), f.get('description',''), datetime.utcnow().isoformat(), id, cid))
    conn.commit()
    slot = row_to_dict(conn.execute(
        '''SELECT s.*, a.nom_machine, a.type_appareil, a.adresse_ip, a.marque, a.en_ligne
           FROM baie_slots s LEFT JOIN appareils a ON s.appareil_id=a.id WHERE s.id=?''', (id,)).fetchone() or {})
    conn.close()
    return jsonify(slot)

@app.route('/api/baie/slot/<int:id>/deplacer', methods=['POST'])
def api_baie_deplacer_slot(id):
    '''Drag & drop : déplace un slot vers une nouvelle position/col.'''
    cid = get_client_id()
    f = request.json or {}
    new_pos = f.get('position', 1)
    new_col = f.get('col_index', 0)
    conn = get_db()
    # Supprimer l'éventuel occupant de la destination
    conn.execute('DELETE FROM baie_slots WHERE client_id=? AND position=? AND col_index=? AND id!=?',
                 (cid, new_pos, new_col, id))
    conn.execute('UPDATE baie_slots SET position=?, col_index=? WHERE id=? AND client_id=?',
                 (new_pos, new_col, id, cid))
    conn.commit()
    slot = row_to_dict(conn.execute(
        '''SELECT s.*, a.nom_machine, a.type_appareil, a.adresse_ip, a.marque, a.en_ligne
           FROM baie_slots s LEFT JOIN appareils a ON s.appareil_id=a.id WHERE s.id=?''', (id,)).fetchone() or {})
    conn.close()
    return jsonify(slot)

@app.route('/api/baie/slots')
def api_baie_slots():
    cid = get_client_id()
    baie_nom = request.args.get('baie', 'Baie principale')
    conn = get_db()
    slots = [row_to_dict(r) for r in conn.execute(
        '''SELECT s.*, a.nom_machine, a.type_appareil, a.adresse_ip, a.marque, a.modele, a.en_ligne, a.ports_ouverts
           FROM baie_slots s LEFT JOIN appareils a ON s.appareil_id=a.id
           WHERE s.client_id=? AND (s.baie_nom=? OR (s.baie_nom IS NULL AND ?='Baie principale'))
           ORDER BY s.position, s.col_index''', (cid, baie_nom, baie_nom)).fetchall()]
    parc = row_to_dict(conn.execute('SELECT baie_nb_u, switch_nb_unites FROM parc_general WHERE client_id=?', (cid,)).fetchone() or {})
    # Liste des baies existantes
    baies = [r[0] for r in conn.execute(
        "SELECT DISTINCT COALESCE(baie_nom,'Baie principale') FROM baie_slots WHERE client_id=? ORDER BY 1",
        (cid,)).fetchall()]
    if not baies: baies = ['Baie principale']
    if 'Baie principale' not in baies: baies.insert(0, 'Baie principale')
    conn.close()
    return jsonify({'slots': slots, 'nb_u': parc.get('baie_nb_u', 12) or 12, 'baies': baies})

# ─── PHOTOS BAIE ─────────────────────────────────────────────────────────────

@app.route('/baie/photo/upload', methods=['POST'])
def upload_photo_baie():
    cid = get_client_id()
    if 'fichier' not in request.files:
        return redirect(url_for('baie_brassage'))
    f = request.files['fichier']
    if not f.filename or not allowed_file(f.filename):
        flash('Type de fichier non autorisé', 'danger')
        return redirect(url_for('baie_brassage'))
    safe = secure_filename(f.filename)
    unique = f"baie{cid}_{int(time.time())}_{safe}"
    save_path = os.path.join(UPLOAD_FOLDER, unique)
    f.save(save_path)
    taille = os.path.getsize(save_path)
    nom = request.form.get('nom', '') or f.filename
    desc = request.form.get('description', '')
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute('INSERT INTO baie_photos (client_id,nom,description,nom_fichier,taille,date_upload) VALUES (?,?,?,?,?,?)',
                 (cid, nom, desc, unique, taille, now))
    conn.commit(); conn.close()
    flash(f'Photo « {nom} » ajoutée', 'success')
    return redirect(url_for('baie_brassage'))

@app.route('/baie/photo/<int:id>/supprimer', methods=['POST'])
def supprimer_photo_baie(id):
    cid = get_client_id()
    conn = get_db()
    photo = row_to_dict(conn.execute('SELECT * FROM baie_photos WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    if photo:
        conn.execute('DELETE FROM baie_photos WHERE id=?', (id,))
        conn.commit()
        try: os.remove(os.path.join(UPLOAD_FOLDER, photo['nom_fichier']))
        except: pass
    conn.close()
    return redirect(url_for('baie_brassage'))

@app.route('/baie/photo/<int:id>/apercu')
def apercu_photo_baie(id):
    cid = get_client_id()
    conn = get_db()
    photo = row_to_dict(conn.execute('SELECT * FROM baie_photos WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()
    if not photo: return 'Not found', 404
    return send_from_directory(UPLOAD_FOLDER, photo['nom_fichier'], as_attachment=False)

@app.route('/api/baie/nb_u', methods=['POST'])
def api_baie_nb_u():
    cid = get_client_id()
    data = request.json or {}
    nb = max(6, min(48, int(data.get('nb_u', 12))))
    conn = get_db()
    conn.execute('UPDATE parc_general SET baie_nb_u=? WHERE client_id=?', (nb, cid))
    conn.commit(); conn.close()
    return jsonify({'nb_u': nb})

# ─── MOTEUR DE SCAN MULTI-THREAD ─────────────────────────────────────────────

scan_status = {"running": False, "progress": 0, "message": "", "results": [], "errors": []}
scan_lock = threading.Lock()
IS_WINDOWS = platform.system() == "Windows"

# ── OUI VENDOR LOOKUP (top fabricants embarqués) ────────────────────────────
_OUI = {
    # ── VMware / Virtualisation ───────────────────────────────────────────────
    "00:50:56":"VMware","00:0c:29":"VMware","00:05:69":"VMware","00:1c:14":"VMware",
    "08:00:27":"VirtualBox","0a:00:27":"VirtualBox",
    "52:54:00":"QEMU/KVM","00:16:3e":"Xen",
    # ── Raspberry Pi Foundation ───────────────────────────────────────────────
    "b8:27:eb":"Raspberry Pi","dc:a6:32":"Raspberry Pi","e4:5f:01":"Raspberry Pi",
    "d8:3a:dd":"Raspberry Pi","28:cd:c1":"Raspberry Pi",
    # ── Apple ────────────────────────────────────────────────────────────────
    "00:03:93":"Apple","00:05:02":"Apple","00:0a:27":"Apple","00:0a:95":"Apple",
    "00:11:24":"Apple","00:14:51":"Apple","00:16:cb":"Apple","00:17:f2":"Apple",
    "00:19:e3":"Apple","00:1b:63":"Apple","00:1c:b3":"Apple","00:1d:4f":"Apple",
    "00:1e:52":"Apple","00:1e:c2":"Apple","00:1f:5b":"Apple","00:1f:f3":"Apple",
    "00:21:e9":"Apple","00:22:41":"Apple","00:23:12":"Apple","00:23:32":"Apple",
    "00:23:6c":"Apple","00:24:36":"Apple","00:25:00":"Apple","00:25:4b":"Apple",
    "00:25:bc":"Apple","00:26:08":"Apple","00:26:b0":"Apple","00:26:bb":"Apple",
    "3c:07:54":"Apple","3c:15:c2":"Apple","3c:22:fb":"Apple","40:6c:8f":"Apple",
    "40:a6:d9":"Apple","4c:8d:79":"Apple","60:fb:42":"Apple","64:a3:cb":"Apple",
    "70:11:24":"Apple","70:cd:60":"Apple","78:7b:8a":"Apple","7c:f0:5f":"Apple",
    "88:19:08":"Apple","88:e8:7f":"Apple","8c:00:6d":"Apple","8c:58:77":"Apple",
    "90:72:40":"Apple","98:01:a7":"Apple","98:03:d8":"Apple","a4:b1:97":"Apple",
    "a4:c3:f0":"Apple","a8:20:66":"Apple","ac:bc:32":"Apple","b4:18:d1":"Apple",
    "b8:09:8a":"Apple","b8:8d:12":"Apple","b8:c7:5d":"Apple","bc:52:b7":"Apple",
    "c8:69:cd":"Apple","c8:b5:b7":"Apple","cc:29:f5":"Apple","d0:23:db":"Apple",
    "d4:61:9d":"Apple","d8:bb:c1":"Apple","dc:2b:2a":"Apple","e0:ac:cb":"Apple",
    "e4:ce:8f":"Apple","e8:04:0b":"Apple","f0:b4:79":"Apple","f0:d1:a9":"Apple",
    "f4:0f:24":"Apple","f8:1e:df":"Apple","f8:27:93":"Apple","fc:25:3f":"Apple",
    # ── Microsoft ────────────────────────────────────────────────────────────
    "00:03:ff":"Microsoft","00:12:5a":"Microsoft","00:15:5d":"Microsoft (Hyper-V)",
    "00:17:fa":"Microsoft","00:50:f2":"Microsoft","28:18:78":"Microsoft",
    "48:b0:2d":"Microsoft","50:1a:c5":"Microsoft","7c:1e:52":"Microsoft",
    # ── Dell ─────────────────────────────────────────────────────────────────
    "00:06:5b":"Dell","00:08:74":"Dell","00:0b:db":"Dell","00:0d:56":"Dell",
    "00:0f:1f":"Dell","00:11:43":"Dell","00:12:3f":"Dell","00:13:72":"Dell",
    "00:14:22":"Dell","00:15:c5":"Dell","00:16:f0":"Dell","00:18:8b":"Dell",
    "00:19:b9":"Dell","00:1a:a0":"Dell","00:1c:23":"Dell","00:1d:09":"Dell",
    "00:1e:4f":"Dell","00:21:70":"Dell","00:22:19":"Dell","00:23:ae":"Dell",
    "00:24:e8":"Dell","00:25:64":"Dell","00:26:b9":"Dell","08:00:37":"Dell",
    "0c:c4:7a":"Dell","10:98:36":"Dell","14:18:77":"Dell","14:fe:b5":"Dell",
    "18:03:73":"Dell","18:66:da":"Dell","18:a9:9b":"Dell","1c:40:24":"Dell",
    "20:47:47":"Dell","24:b6:fd":"Dell","28:f1:0e":"Dell","2c:76:8a":"Dell",
    "34:17:eb":"Dell","34:e6:d7":"Dell","38:63:bb":"Dell","3c:a8:2a":"Dell",
    "44:a8:42":"Dell","48:4d:7e":"Dell","4c:d9:8f":"Dell","50:9a:4c":"Dell",
    "54:9f:13":"Dell","58:8a:5a":"Dell","5c:f9:dd":"Dell","60:57:18":"Dell",
    "6c:2b:59":"Dell","70:10:6f":"Dell","74:86:e2":"Dell","78:45:c4":"Dell",
    "7c:b1:1c":"Dell","84:8f:69":"Dell","8c:04:ba":"Dell","90:b1:1c":"Dell",
    "98:90:96":"Dell","9c:eb:e8":"Dell","a0:36:9f":"Dell","a4:1f:72":"Dell",
    "a4:ba:db":"Dell","a8:9d:21":"Dell","b0:83:fe":"Dell","b4:96:91":"Dell",
    "b8:ca:3a":"Dell","bc:30:5b":"Dell","c8:1f:66":"Dell","d4:ae:52":"Dell",
    "d4:be:d9":"Dell","d8:9e:f3":"Dell","e0:db:55":"Dell","e4:43:4b":"Dell",
    "e8:b0:c3":"Dell","ec:f4:bb":"Dell","f0:1f:af":"Dell","f4:02:70":"Dell",
    "f8:db:88":"Dell","f8:bc:12":"Dell","fc:aa:14":"Dell",
    # ── HP / HPE ──────────────────────────────────────────────────────────────
    "00:01:e6":"HP","00:04:ea":"HP","00:08:02":"HP","00:0b:cd":"HP",
    "00:0e:7f":"HP","00:10:83":"HP","00:11:0a":"HP","00:12:79":"HP",
    "00:13:21":"HP","00:14:38":"HP","00:15:60":"HP","00:16:35":"HP",
    "00:17:08":"HP","00:17:a4":"HP","00:18:71":"HP","00:19:bb":"HP",
    "00:1a:4b":"HP","00:1b:78":"HP","00:1c:c4":"HP","00:1d:b3":"HP",
    "00:1e:0b":"HP","00:1f:28":"HP","00:1f:29":"HP","00:21:5a":"HP",
    "00:22:64":"HP","00:23:7d":"HP","00:24:81":"HP","00:25:b3":"HP",
    "00:26:55":"HP","00:30:6e":"HP","3c:4a:92":"HP","3c:d9:2b":"HP",
    "40:a8:f0":"HP","40:b0:34":"HP","58:20:b1":"HP","5c:b9:01":"HP",
    "6c:c2:17":"HP","6c:c2:6b":"HP","70:10:6f":"HP","70:5a:0f":"HP",
    "78:ac:c0":"HP","80:c1:6e":"HP","84:34:97":"HP","9c:8e:99":"HP",
    "a0:b3:cc":"HP","a4:5d:36":"HP","b8:af:67":"HP","bc:ea:fa":"HP",
    "c4:34:6b":"HP","c8:d3:ff":"HP","d4:85:64":"HP","d8:9d:67":"HP",
    "dc:4a:3e":"HP","e4:11:5b":"HP","e8:39:35":"HP","ec:b1:d7":"HP",
    "f0:92:1c":"HP","f4:ce:46":"HP","f8:b1:56":"HP","fc:15:b4":"HP",
    # ── Lenovo ────────────────────────────────────────────────────────────────
    "00:1a:6b":"Lenovo","18:56:80":"Lenovo","28:d2:44":"Lenovo","40:8d:5c":"Lenovo",
    "40:f0:2f":"Lenovo","48:0f:cf":"Lenovo","4c:79:6e":"Lenovo","50:7b:9d":"Lenovo",
    "54:ee:75":"Lenovo","58:8f:c7":"Lenovo","5c:f3:70":"Lenovo","60:67:20":"Lenovo",
    "70:72:3c":"Lenovo","70:f3:95":"Lenovo","74:df:bf":"Lenovo","78:92:9c":"Lenovo",
    "80:5e:c0":"Lenovo","84:2b:2b":"Lenovo","88:70:8c":"Lenovo","8c:d5:d9":"Lenovo",
    "8c:ec:4b":"Lenovo","90:2b:34":"Lenovo","98:41:5c":"Lenovo","9c:93:4e":"Lenovo",
    "a4:4c:c8":"Lenovo","ac:b3:13":"Lenovo","b8:ae:ed":"Lenovo","c4:65:16":"Lenovo",
    "c8:5b:76":"Lenovo","cc:f9:54":"Lenovo","d0:37:45":"Lenovo","d4:81:d7":"Lenovo",
    "d4:c9:ef":"Lenovo","e8:6a:64":"Lenovo","ec:f4:bb":"Lenovo","f8:16:54":"Lenovo",
    # ── Cisco ─────────────────────────────────────────────────────────────────
    "00:00:0c":"Cisco","00:00:7f":"Cisco","00:01:42":"Cisco","00:01:43":"Cisco",
    "00:01:63":"Cisco","00:01:64":"Cisco","00:01:96":"Cisco","00:01:97":"Cisco",
    "00:02:16":"Cisco","00:02:17":"Cisco","00:03:6b":"Cisco","00:03:e3":"Cisco",
    "00:04:6d":"Cisco","00:0a:8a":"Cisco","00:0b:be":"Cisco","00:0b:fd":"Cisco",
    "00:0c:ce":"Cisco","00:0d:28":"Cisco","00:0d:29":"Cisco","00:0e:38":"Cisco",
    "00:0e:83":"Cisco","00:0e:84":"Cisco","00:0f:23":"Cisco","00:0f:24":"Cisco",
    "00:0f:8f":"Cisco","00:0f:90":"Cisco","00:1a:2f":"Cisco","00:1a:30":"Cisco",
    "00:1b:2a":"Cisco","00:1b:2b":"Cisco","00:1c:57":"Cisco","00:1c:58":"Cisco",
    "00:1d:45":"Cisco","00:1d:46":"Cisco","00:1e:13":"Cisco","00:1e:14":"Cisco",
    "00:1f:6c":"Cisco","00:1f:6d":"Cisco","00:21:55":"Cisco","00:21:56":"Cisco",
    "00:22:55":"Cisco","00:22:56":"Cisco","00:23:33":"Cisco","00:23:34":"Cisco",
    "00:24:13":"Cisco","00:24:14":"Cisco","00:25:83":"Cisco","00:25:84":"Cisco",
    "00:26:0b":"Cisco","00:26:0c":"Cisco","00:30:f2":"Cisco","00:50:0f":"Cisco",
    "00:60:70":"Cisco","00:90:21":"Cisco","00:90:6d":"Cisco","00:90:86":"Cisco",
    "04:62:73":"Cisco","08:96:ad":"Cisco","10:bd:18":"Cisco","14:f1:08":"Cisco",
    "18:33:9d":"Cisco","1c:de:a7":"Cisco","20:37:06":"Cisco","24:e9:b3":"Cisco",
    "28:94:0f":"Cisco","2c:54:91":"Cisco","30:37:a6":"Cisco","34:a8:4e":"Cisco",
    "38:ed:18":"Cisco","3c:08:f6":"Cisco","40:f4:ec":"Cisco","44:d3:ca":"Cisco",
    "48:39:50":"Cisco","4c:4e:35":"Cisco","50:61:84":"Cisco","54:75:d0":"Cisco",
    "58:97:bd":"Cisco","5c:71:0d":"Cisco","60:73:5c":"Cisco","64:14:13":"Cisco",
    "68:86:a7":"Cisco","6c:20:56":"Cisco","70:69:5a":"Cisco","74:26:ac":"Cisco",
    "78:ba:f9":"Cisco","7c:69:f6":"Cisco","80:e0:1d":"Cisco","84:b8:02":"Cisco",
    "88:75:56":"Cisco","8c:60:4f":"Cisco","90:e2:ba":"Cisco","94:d4:69":"Cisco",
    "98:90:96":"Cisco","9c:57:ad":"Cisco","a0:55:4f":"Cisco","a4:4c:11":"Cisco",
    "a8:b4:56":"Cisco","ac:17:c8":"Cisco","b0:aa:77":"Cisco","b4:a4:e3":"Cisco",
    "b8:38:61":"Cisco","bc:16:f5":"Cisco","c0:62:6b":"Cisco","c4:72:95":"Cisco",
    "c8:9c:1d":"Cisco","cc:d8:c1":"Cisco","d0:72:dc":"Cisco","d4:8c:b5":"Cisco",
    "d8:24:bd":"Cisco","dc:7b:94":"Cisco","e0:5f:b9":"Cisco","e4:aa:5d":"Cisco",
    "e8:ba:70":"Cisco","ec:1d:8b":"Cisco","f0:25:72":"Cisco","f4:cf:e2":"Cisco",
    "f8:7b:20":"Cisco","fc:5b:39":"Cisco","fc:fb:fb":"Cisco",
    # ── Intel ─────────────────────────────────────────────────────────────────
    "00:02:b3":"Intel","00:03:47":"Intel","00:04:23":"Intel","00:07:e9":"Intel",
    "00:0e:35":"Intel","00:12:f0":"Intel","00:13:02":"Intel","00:13:20":"Intel",
    "00:15:00":"Intel","00:16:ea":"Intel","00:16:eb":"Intel","00:18:de":"Intel",
    "00:19:d1":"Intel","00:1b:21":"Intel","00:1c:bf":"Intel","00:1e:64":"Intel",
    "00:1e:65":"Intel","00:1e:67":"Intel","00:1f:3b":"Intel","00:1f:3c":"Intel",
    "00:21:6a":"Intel","00:22:fa":"Intel","00:23:14":"Intel","00:24:d7":"Intel",
    "00:27:10":"Intel","04:0e:3c":"Intel","08:11:96":"Intel","0c:8b:fd":"Intel",
    "10:02:b5":"Intel","10:f0:05":"Intel","18:67:b0":"Intel","1c:69:7a":"Intel",
    "24:77:03":"Intel","28:d2:44":"Intel","2c:41:38":"Intel","34:13:e8":"Intel",
    "34:de:1a":"Intel","38:2c:4a":"Intel","40:a5:ef":"Intel","44:85:00":"Intel",
    "48:51:b7":"Intel","4c:eb:42":"Intel","54:27:1e":"Intel","5c:51:4f":"Intel",
    "60:67:20":"Intel","60:f6:77":"Intel","64:00:6a":"Intel","68:05:ca":"Intel",
    "6c:29:95":"Intel","70:1a:04":"Intel","74:d4:35":"Intel","78:92:9c":"Intel",
    "7c:5c:f8":"Intel","80:19:34":"Intel","80:86:f2":"Intel","84:3a:4b":"Intel",
    "88:53:2e":"Intel","8c:8d:28":"Intel","8c:ec:4b":"Intel","90:e2:ba":"Intel",
    "94:65:9c":"Intel","98:4b:e1":"Intel","9c:eb:e8":"Intel","a0:88:b4":"Intel",
    "a4:c3:f0":"Intel","a8:6b:ad":"Intel","ac:72:89":"Intel","b4:96:91":"Intel",
    "b8:08:cf":"Intel","bc:ee:7b":"Intel","c0:3f:d5":"Intel","c4:d9:87":"Intel",
    "c8:d9:d2":"Intel","cc:3d:82":"Intel","d0:50:99":"Intel","d4:3d:7e":"Intel",
    "d8:fc:93":"Intel","dc:53:60":"Intel","e0:d5:5e":"Intel","e4:b3:18":"Intel",
    "e8:b4:70":"Intel","ec:b1:d7":"Intel","f0:4d:a2":"Intel","f4:8e:38":"Intel",
    # ── TP-Link ───────────────────────────────────────────────────────────────
    "00:23:cd":"TP-Link","08:57:00":"TP-Link","10:fe:ed":"TP-Link","14:cc:20":"TP-Link",
    "18:a6:f7":"TP-Link","18:d6:c7":"TP-Link","1c:61:b4":"TP-Link","20:dc:e6":"TP-Link",
    "24:69:68":"TP-Link","28:2c:b2":"TP-Link","2c:54:91":"TP-Link","30:b5:c2":"TP-Link",
    "34:60:f9":"TP-Link","38:94:ed":"TP-Link","3c:52:82":"TP-Link","40:16:9f":"TP-Link",
    "44:94:fc":"TP-Link","48:8f:5a":"TP-Link","4c:e1:73":"TP-Link","50:c7:bf":"TP-Link",
    "54:a7:03":"TP-Link","5c:89:9a":"TP-Link","60:a4:b7":"TP-Link","64:70:02":"TP-Link",
    "68:ff:7b":"TP-Link","6c:5a:b0":"TP-Link","70:4f:57":"TP-Link","74:da:38":"TP-Link",
    "78:44:fd":"TP-Link","7c:8b:ca":"TP-Link","80:8f:1d":"TP-Link","84:16:f9":"TP-Link",
    "88:d7:f6":"TP-Link","8c:21:0a":"TP-Link","90:f6:52":"TP-Link","94:d9:b3":"TP-Link",
    "98:da:c4":"TP-Link","9c:a6:15":"TP-Link","a0:f3:c1":"TP-Link","a4:2b:b0":"TP-Link",
    "a8:57:4e":"TP-Link","ac:84:c6":"TP-Link","b0:4e:26":"TP-Link","b4:b0:24":"TP-Link",
    "b8:a3:86":"TP-Link","bc:46:99":"TP-Link","c0:06:c3":"TP-Link","c4:e9:84":"TP-Link",
    "c8:3a:35":"TP-Link","cc:32:e5":"TP-Link","d4:6e:5c":"TP-Link","d8:07:b6":"TP-Link",
    "dc:fe:18":"TP-Link","e0:28:6d":"TP-Link","e4:c3:2a":"TP-Link","e8:48:b8":"TP-Link",
    "ec:08:6b":"TP-Link","f0:a7:31":"TP-Link","f4:f2:6d":"TP-Link","f8:1a:67":"TP-Link",
    "fc:d7:33":"TP-Link",
    # ── Ubiquiti ──────────────────────────────────────────────────────────────
    "00:15:6d":"Ubiquiti","00:27:22":"Ubiquiti","04:18:d6":"Ubiquiti","0a:27:22":"Ubiquiti",
    "18:e8:29":"Ubiquiti","24:a4:3c":"Ubiquiti","2c:27:d7":"Ubiquiti","34:1a:35":"Ubiquiti",
    "44:d9:e7":"Ubiquiti","4c:e9:e4":"Ubiquiti","60:22:32":"Ubiquiti","68:72:51":"Ubiquiti",
    "6e:27:d3":"Ubiquiti","70:a7:41":"Ubiquiti","74:83:c8":"Ubiquiti","78:8a:20":"Ubiquiti",
    "78:d2:94":"Ubiquiti","7c:dd:90":"Ubiquiti","80:2a:a8":"Ubiquiti","b4:fb:e4":"Ubiquiti",
    "b6:fb:e4":"Ubiquiti","d8:21:e8":"Ubiquiti","d8:b3:70":"Ubiquiti","dc:9f:db":"Ubiquiti",
    "e0:63:da":"Ubiquiti","e4:38:83":"Ubiquiti","e6:38:83":"Ubiquiti","e8:48:b8":"Ubiquiti",
    "f0:9f:c2":"Ubiquiti","f4:92:bf":"Ubiquiti","f4:e2:c6":"Ubiquiti","fc:ec:da":"Ubiquiti",
    "a4:4c:11":"Ubiquiti",
    # ── Netgear ───────────────────────────────────────────────────────────────
    "00:09:5b":"Netgear","00:0f:b5":"Netgear","00:14:6c":"Netgear","00:18:4d":"Netgear",
    "00:1b:2f":"Netgear","00:1e:2a":"Netgear","00:1f:33":"Netgear","00:22:3f":"Netgear",
    "00:24:b2":"Netgear","00:26:f2":"Netgear","10:0d:7f":"Netgear","20:0c:c8":"Netgear",
    "20:4e:7f":"Netgear","28:80:23":"Netgear","2c:30:33":"Netgear","30:46:9a":"Netgear",
    "3c:37:86":"Netgear","44:94:fc":"Netgear","4c:60:de":"Netgear","6c:b0:ce":"Netgear",
    "74:44:01":"Netgear","7c:b7:33":"Netgear","80:37:73":"Netgear","84:1b:5e":"Netgear",
    "9c:3d:cf":"Netgear","9c:d3:6d":"Netgear","a0:21:b7":"Netgear","a0:40:a0":"Netgear",
    "a4:2b:8c":"Netgear","b0:7f:b9":"Netgear","c0:3f:0e":"Netgear","c4:04:15":"Netgear",
    "c4:3d:c7":"Netgear","c8:d7:19":"Netgear","cc:40:d0":"Netgear","e0:46:9a":"Netgear",
    "e0:91:f5":"Netgear","e4:f4:c6":"Netgear","e8:fc:af":"Netgear","f8:1a:67":"Netgear",
    # ── D-Link ────────────────────────────────────────────────────────────────
    "00:05:5d":"D-Link","00:0d:88":"D-Link","00:0f:3d":"D-Link","00:11:95":"D-Link",
    "00:13:46":"D-Link","00:15:e9":"D-Link","00:17:9a":"D-Link","00:19:5b":"D-Link",
    "00:1b:11":"D-Link","00:1c:f0":"D-Link","00:1e:58":"D-Link","00:21:91":"D-Link",
    "00:22:b0":"D-Link","00:24:01":"D-Link","00:26:5a":"D-Link","1c:7e:e5":"D-Link",
    "28:10:7b":"D-Link","2c:b0:5d":"D-Link","2c:d0:5a":"D-Link","34:08:04":"D-Link",
    "34:31:c4":"D-Link","5c:d9:98":"D-Link","64:70:02":"D-Link","78:54:2e":"D-Link",
    "84:c9:b2":"D-Link","90:94:e4":"D-Link","9c:72:b9":"D-Link","a0:ab:1b":"D-Link",
    "b4:c7:99":"D-Link","bc:f6:85":"D-Link","c0:a0:bb":"D-Link","c8:be:19":"D-Link",
    "cc:b2:55":"D-Link","d8:eb:97":"D-Link","e4:6f:13":"D-Link","f0:7d:68":"D-Link",
    "f8:1a:67":"D-Link","fc:75:16":"D-Link",
    # ── Synology ─────────────────────────────────────────────────────────────
    "00:11:32":"Synology","2c:fd:a1":"Synology",  # 00:11:32 is also Synology
    "bc:ee:7b":"Synology",
    # ── QNAP ─────────────────────────────────────────────────────────────────
    "00:08:9b":"QNAP","00:08:9b":"QNAP","24:5e:be":"QNAP","68:63:7c":"QNAP",
    "d8:29:f8":"QNAP","00:90:a9":"QNAP",
    # ── Fortinet ─────────────────────────────────────────────────────────────
    "00:09:0f":"Fortinet","00:0b:86":"Fortinet","00:78:88":"Fortinet",
    "70:4c:a5":"Fortinet","90:6c:ac":"Fortinet",
    # ── Palo Alto Networks ────────────────────────────────────────────────────
    "00:1b:17":"Palo Alto","3c:4a:92":"HP",  # HP overrides Palo Alto for this prefix
    # ── Juniper ───────────────────────────────────────────────────────────────
    "00:12:1e":"Juniper","00:17:cb":"Juniper","00:19:e2":"Juniper","00:21:59":"Juniper",
    "00:23:9c":"Juniper","00:24:dc":"Juniper","00:26:88":"Juniper",
    "28:8a:1c":"Juniper","2c:6b:f5":"Juniper","3c:61:04":"Juniper",
    "40:b4:f0":"Juniper","4c:96:14":"Juniper","54:e0:32":"Juniper",
    "64:87:88":"Juniper","6c:b2:ae":"Juniper","84:18:88":"Juniper",
    "88:e0:f3":"Juniper","98:65:15":"Juniper","a4:50:46":"Juniper",
    "cc:e1:7f":"Juniper","f0:1c:2d":"Juniper","f4:a7:39":"Juniper",
    "fc:2f:40":"Juniper",
    # ── Aruba / HP Networking ─────────────────────────────────────────────────
    "00:0b:86":"Aruba","00:1a:1e":"Aruba","00:24:6c":"Aruba","04:bd:88":"Aruba",
    "08:26:97":"Aruba","0c:f8:93":"Aruba","18:64:72":"Aruba","1c:28:af":"Aruba",
    "20:4c:03":"Aruba","20:a6:cd":"Aruba","24:de:c6":"Aruba","2c:a8:35":"Aruba",
    "34:fc:b9":"Aruba","40:e3:d6":"Aruba","4c:6d:7f":"Aruba","58:8b:f3":"Aruba",
    "6c:f3:7f":"Aruba","70:88:6b":"Aruba","74:f8:db":"Aruba","84:d4:7e":"Aruba",
    "94:b4:0f":"Aruba","9c:1c:12":"Aruba","a8:bd:27":"Aruba","ac:a3:1e":"Aruba",
    "b0:5a:da":"Aruba","b4:5d:50":"Aruba","c4:01:7c":"Aruba","d8:c7:c8":"Aruba",
    "e8:26:89":"Aruba","ec:b3:18":"Aruba","f0:5c:19":"Aruba",
    # ── HP Printing ──────────────────────────────────────────────────────────
    "00:17:c8":"HP","00:1b:78":"HP","00:1f:29":"HP","00:21:5a":"HP",
    "00:24:81":"HP","18:a9:05":"HP","1c:c1:de":"HP","28:92:4a":"HP",
    "30:8d:99":"HP","38:ea:a7":"HP","3c:d9:2b":"HP","40:b8:9a":"HP",
    "48:0f:cf":"HP","70:5a:0f":"HP","78:ac:c0":"HP","94:57:a5":"HP",
    "a4:5d:36":"HP","b4:99:ba":"HP","d8:9d:67":"HP","e8:04:0b":"HP",
    # ── Canon ─────────────────────────────────────────────────────────────────
    "00:00:85":"Canon","00:1e:8f":"Canon","00:80:92":"Canon","3c:43:8e":"Canon",
    "4c:49:e3":"Canon","74:d0:2b":"Canon","90:ca:fa":"Canon","ac:41:76":"Canon",
    "b4:75:0e":"Canon","c4:ac:59":"Canon","d4:20:b0":"Canon","f4:81:39":"Canon",
    # ── Epson ─────────────────────────────────────────────────────────────────
    "00:00:48":"Epson","00:26:ab":"Epson","08:00:46":"Epson","3c:3a:ef":"Epson",
    "4c:f6:08":"Epson","60:55:f9":"Epson","64:eb:8c":"Epson","ac:18:26":"Epson",
    # ── Brother ───────────────────────────────────────────────────────────────
    "00:00:74":"Brother","00:1b:a9":"Brother","00:80:77":"Brother","00:c0:97":"Brother",
    "0c:98:38":"Brother","30:05:5c":"Brother","34:56:fe":"Brother","3c:56:a6":"Brother",
    "40:49:0f":"Brother","5c:96:9d":"Brother","70:77:81":"Brother","b8:2a:72":"Brother",
    "c8:47:0d":"Brother","d4:11:a3":"Brother","d8:9b:3b":"Brother","e0:06:e6":"Brother",
    # ── Kyocera ───────────────────────────────────────────────────────────────
    "00:60:67":"Kyocera","00:c0:ee":"Kyocera","08:00:46":"Kyocera","0c:7e:d2":"Kyocera",
    "a4:1f:72":"Kyocera",
    # ── Ricoh ────────────────────────────────────────────────────────────────
    "00:00:74":"Ricoh","00:00:78":"Ricoh","00:60:b0":"Ricoh","08:00:48":"Ricoh",
    "00:26:73":"Ricoh","2c:5b:e1":"Ricoh","ac:de:48":"Ricoh",
    # ── Xerox ─────────────────────────────────────────────────────────────────
    "00:00:aa":"Xerox","00:00:6b":"Xerox","00:00:f4":"Xerox","34:9c:cd":"Xerox",
    "38:1a:52":"Xerox","3c:6f:6c":"Xerox","44:1e:a1":"Xerox","60:f4:45":"Xerox",
    # ── Lexmark ───────────────────────────────────────────────────────────────
    "00:04:00":"Lexmark","00:04:00":"Lexmark","00:0d:87":"Lexmark","34:60:f9":"Lexmark",
    # ── Samsung ──────────────────────────────────────────────────────────────
    "00:00:f0":"Samsung","00:02:78":"Samsung","00:12:47":"Samsung","00:15:b9":"Samsung",
    "00:16:32":"Samsung","00:17:c9":"Samsung","00:1d:25":"Samsung","00:1e:7d":"Samsung",
    "00:21:19":"Samsung","00:23:99":"Samsung","00:24:54":"Samsung","00:26:37":"Samsung",
    "04:18:d6":"Samsung","08:08:c2":"Samsung","08:d4:2b":"Samsung","10:30:47":"Samsung",
    "10:d5:42":"Samsung","14:49:e0":"Samsung","18:3a:2d":"Samsung","1c:af:f7":"Samsung",
    "20:13:e0":"Samsung","24:4b:81":"Samsung","28:27:bf":"Samsung","2c:ae:2b":"Samsung",
    "30:19:66":"Samsung","34:14:5f":"Samsung","38:01:97":"Samsung","3c:8b:fe":"Samsung",
    "40:0e:85":"Samsung","44:6d:57":"Samsung","48:44:f7":"Samsung","4c:3c:16":"Samsung",
    "50:32:75":"Samsung","54:88:0e":"Samsung","58:ef:68":"Samsung","5c:0a:5b":"Samsung",
    "60:a1:0a":"Samsung","64:77:91":"Samsung","68:27:37":"Samsung","6c:2f:2c":"Samsung",
    "70:f9:27":"Samsung","78:1f:db":"Samsung","7c:0b:c6":"Samsung","80:65:6d":"Samsung",
    "84:25:db":"Samsung","88:32:9b":"Samsung","8c:71:f8":"Samsung","90:00:4e":"Samsung",
    "94:76:b7":"Samsung","98:52:b1":"Samsung","9c:02:98":"Samsung","a0:07:98":"Samsung",
    "a4:eb:d3":"Samsung","a8:06:00":"Samsung","ac:36:13":"Samsung","b0:72:bf":"Samsung",
    "b4:3a:28":"Samsung","b8:6c:e8":"Samsung","bc:14:ef":"Samsung","c0:bd:d1":"Samsung",
    "c4:42:02":"Samsung","c8:ba:94":"Samsung","cc:07:ab":"Samsung","d0:17:6a":"Samsung",
    "d4:88:90":"Samsung","d8:57:ef":"Samsung","dc:71:96":"Samsung","e0:62:90":"Samsung",
    "e4:e0:c5":"Samsung","e8:03:9a":"Samsung","ec:1f:72":"Samsung","f0:25:b7":"Samsung",
    "f4:7b:5e":"Samsung","f8:04:2e":"Samsung","fc:a1:3e":"Samsung",
    # ── Realtek ───────────────────────────────────────────────────────────────
    "00:e0:4c":"Realtek","52:54:00":"Realtek","54:ab:3a":"Realtek","e0:d5:5e":"Realtek",
    # ── APC / Schneider ───────────────────────────────────────────────────────
    "00:c0:b7":"APC","00:60:26":"APC","c8:cb:9e":"APC",
    # ── Supermicro ────────────────────────────────────────────────────────────
    "00:25:90":"Supermicro","00:30:48":"Supermicro","18:66:da":"Supermicro",
    # ── IBM ───────────────────────────────────────────────────────────────────
    "00:04:ac":"IBM","00:06:29":"IBM","00:09:6b":"IBM","00:0d:60":"IBM",
    "00:11:25":"IBM","00:14:5e":"IBM","00:17:ef":"IBM","00:21:5e":"IBM",
    "00:26:55":"IBM",
    # ── Google ────────────────────────────────────────────────────────────────
    "00:1a:11":"Google","3c:5a:b4":"Google","3c:61:04":"Google",
    "54:60:09":"Google","94:eb:cd":"Google","a4:77:33":"Google",
    "f4:f5:d8":"Google","f4:f5:e8":"Google",
    # ── Amazon ────────────────────────────────────────────────────────────────
    "00:bb:3a":"Amazon","18:74:2e":"Amazon","40:b4:cd":"Amazon","44:65:0d":"Amazon",
    "4c:ef:c0":"Amazon","50:dc:e7":"Amazon","68:37:e9":"Amazon","74:c2:46":"Amazon",
    "84:d6:d0":"Amazon","a0:02:dc":"Amazon","ac:63:be":"Amazon","b4:7c:9c":"Amazon",
    "cc:f7:35":"Amazon","d0:04:01":"Amazon","d0:f8:8c":"Amazon","e4:80:45":"Amazon",
    "f0:27:2d":"Amazon","f8:04:2e":"Amazon","fc:65:de":"Amazon",
    # ── Buffalo ───────────────────────────────────────────────────────────────
    "00:07:40":"Buffalo","00:08:9b":"Buffalo","00:0d:0b":"Buffalo",
    "00:16:01":"Buffalo","00:1d:73":"Buffalo","00:24:a5":"Buffalo",
    "10:6f:3f":"Buffalo","18:c0:4d":"Buffalo","1c:87:2c":"Buffalo",
    "28:3b:82":"Buffalo","2c:fd:a1":"Buffalo","30:85:a9":"Buffalo",
    "40:f2:01":"Buffalo","48:5b:39":"Buffalo","5c:57:c8":"Buffalo",
    "7c:dd:90":"Buffalo","80:35:c1":"Buffalo","90:f6:52":"Buffalo",
    "a8:92:0e":"Buffalo","c4:e9:84":"Buffalo","d8:50:e6":"Buffalo",
    # ── Linksys / Belkin ──────────────────────────────────────────────────────
    "00:06:25":"Linksys","00:0c:41":"Linksys","00:0f:66":"Linksys",
    "00:12:17":"Linksys","00:13:10":"Linksys","00:14:bf":"Linksys",
    "00:16:b6":"Linksys","00:18:39":"Linksys","00:18:f8":"Linksys",
    "00:1a:70":"Linksys","00:1c:10":"Linksys","00:1d:7e":"Linksys",
    "00:1e:e5":"Linksys","00:20:a6":"Linksys","00:21:29":"Linksys",
    "00:22:6b":"Linksys","00:25:9c":"Linksys","c0:c1:c0":"Linksys",
    # ── Mikrotik ─────────────────────────────────────────────────────────────
    "00:0c:42":"Mikrotik","18:fd:74":"Mikrotik","2c:c8:1b":"Mikrotik",
    "48:8f:5a":"Mikrotik","4c:5e:0c":"Mikrotik","6c:3b:6b":"Mikrotik",
    "74:4d:28":"Mikrotik","b8:69:f4":"Mikrotik","cc:2d:e0":"Mikrotik",
    "d4:ca:6d":"Mikrotik","dc:2c:6e":"Mikrotik","e4:8d:8c":"Mikrotik",
    # ── Hikvision ────────────────────────────────────────────────────────────
    "44:19:b6":"Hikvision","4c:bd:8f":"Hikvision","54:c4:15":"Hikvision",
    "8c:e7:48":"Hikvision","94:40:c9":"Hikvision","bc:ad:28":"Hikvision",
    "c4:2f:90":"Hikvision","c8:02:8f":"Hikvision",
    # ── Dahua ────────────────────────────────────────────────────────────────
    "3c:ef:8c":"Dahua","4c:11:bf":"Dahua","90:02:a9":"Dahua",
    "a4:14:37":"Dahua","e0:50:8b":"Dahua",
    # ── Axis (caméras IP) ─────────────────────────────────────────────────────
    "00:40:8c":"Axis","ac:cc:8e":"Axis","b8:a4:4f":"Axis",
}

def _oui_vendor(mac):
    """Retourne le fabricant depuis l'adresse MAC (préfixe OUI 24 bits).
    
    Priorité :
    1. Fichier oui.txt téléchargé (base IEEE complète ~60 000 entrées)
    2. Table embarquée _OUI (~930 entrées)
    """
    if not mac: return ""
    # Normaliser : d4-81-d7-xx → d4:81:d7
    prefix = mac[:8].lower().replace('-', ':').replace(' ', '')
    if len(prefix) < 8: return ""
    
    # 1. Essayer la table IEEE complète si chargée
    global _OUI_FULL
    if _OUI_FULL is None:
        _oui_load_full()
    if _OUI_FULL:
        v = _OUI_FULL.get(prefix, "")
        if v: return v
    
    # 2. Fallback table embarquée
    return _OUI.get(prefix, "")

# Cache de la table IEEE complète (None = pas encore tentée, {} = tentée mais vide)
_OUI_FULL = None

def _oui_load_full():
    """Charge la base OUI IEEE depuis oui.txt si disponible."""
    global _OUI_FULL
    _OUI_FULL = {}
    oui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'oui.txt')
    if not os.path.exists(oui_path):
        return
    try:
        count = 0
        with open(oui_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                # Format IEEE : "00-14-22   (hex)\t\tDell Inc."
                # ou format compact : "00:14:22\tDell Inc."
                if '(hex)' in line:
                    parts = line.split('(hex)')
                    if len(parts) == 2:
                        prefix_raw = parts[0].strip().replace('-', ':').lower()
                        vendor = parts[1].strip()
                        if len(prefix_raw) == 8 and vendor:
                            _OUI_FULL[prefix_raw] = vendor
                            count += 1
                elif re.match(r'^[0-9a-fA-F]{2}[:\-][0-9a-fA-F]{2}[:\-][0-9a-fA-F]{2}\t', line):
                    parts = line.split('\t', 1)
                    if len(parts) == 2:
                        prefix_raw = parts[0].strip().replace('-', ':').lower()
                        vendor = parts[1].strip()
                        if vendor:
                            _OUI_FULL[prefix_raw] = vendor
                            count += 1
        if count > 0:
            app.logger.info(f"OUI database loaded: {count} entries from oui.txt")
    except Exception as e:
        app.logger.warning(f"Could not load oui.txt: {e}")
        _OUI_FULL = {}

# ── PING & DÉCOUVERTE ────────────────────────────────────────────────────────

def _run_hidden(cmd, **kwargs):
    """Lance un subprocess sans fenêtre console visible sur Windows."""
    if IS_WINDOWS:
        kwargs.setdefault('creationflags', subprocess.CREATE_NO_WINDOW)
    return subprocess.run(cmd, **kwargs)


def _ping(ip_str):
    """Teste si un hôte est joignable.
    
    Stratégie en 2 étapes :
    1. Commande ping système (ICMP fiable, évite les faux positifs du raw socket)
    2. Fallback TCP sur ports courants si ping non disponible
    
    Note : le raw ICMP socket N'EST PAS utilisé car en scan parallèle,
    un socket peut intercepter la réponse ICMP destinée à un autre thread,
    générant des faux positifs.
    """
    # 1. Commande ping système
    try:
        if IS_WINDOWS:
            cmd = ['ping', '-n', '1', '-w', '500', ip_str]
        else:
            cmd = ['ping', '-c', '1', '-W', '1', ip_str]
        result = _run_hidden(cmd, capture_output=True, timeout=3)
        if result.returncode == 0:
            return True
    except FileNotFoundError:
        pass  # ping non disponible, on passe au fallback TCP
    except Exception:
        pass
    # 2. Fallback TCP — on essaie plusieurs ports courants
    # Un hôte vivant a forcément au moins un de ces ports ouverts
    for port in [80, 443, 22, 445, 135, 139, 3389, 8080, 53, 8443, 5000, 9100]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.4)
            if s.connect_ex((ip_str, port)) == 0:
                s.close()
                return True
            s.close()
        except Exception:
            pass
    return False

def _hostname(ip_str):
    """Résolution DNS inverse."""
    try:
        return socket.gethostbyaddr(ip_str)[0]
    except Exception:
        return ""

def _netbios_name(ip_str):
    """Requête NetBIOS Name Service (UDP 137) — retourne le nom NetBIOS de la machine."""
    try:
        # Paquet NBSTAT query conforme RFC 1002
        # Transaction ID aléatoire, NBSTAT query pour '*'
        import os as _os
        txid   = _os.urandom(2)
        # Nom encodé NetBIOS pour '*' (wildcard NBSTAT)
        # '*' = 0x2A, encodé en nibbles : 0x2A → 'CK', reste = 'AA'*15
        nb_name = b'CKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'  # 32 octets (16 * 2 nibbles)
        query = (
            txid +
            b'\x00\x00' +          # Flags: standard query
            b'\x00\x01' +          # QDCOUNT: 1 question
            b'\x00\x00' +          # ANCOUNT: 0
            b'\x00\x00' +          # NSCOUNT: 0
            b'\x00\x00' +          # ARCOUNT: 0
            b'\x20' +               # Longueur nom: 32
            nb_name +
            b'\x00' +               # Terminateur
            b'\x00\x21' +          # QTYPE: NBSTAT (0x21)
            b'\x00\x01'            # QCLASS: IN
        )
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.sendto(query, (ip_str, 137))
        data, _ = s.recvfrom(1024)
        s.close()
        # Parser la réponse : offset 56 = nombre de noms
        if len(data) > 57:
            nb_names = data[56]
            offset = 57
            for _ in range(nb_names):
                if offset + 18 > len(data):
                    break
                raw_name = data[offset:offset+15]
                flags    = data[offset+15:offset+18]
                name     = raw_name.decode('ascii', 'ignore').strip()
                # Flag byte: bit 7 = group name, on veut les noms individuels (type 0x00 = workstation)
                name_type = data[offset+15] if offset+15 < len(data) else 0xFF
                if name_type in (0x00, 0x20) and name and name != '\x00' * 15:
                    return name
                offset += 18
    except Exception:
        pass
    return ""

def _icmp_ttl(ip_str):
    """Récupère le TTL d'une réponse ICMP via raw socket."""
    try:
        import struct as _struct
        s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        s.settimeout(1.5)
        def _chk(data):
            s2 = sum((data[i] << 8) + (data[i+1] if i+1 < len(data) else 0)
                     for i in range(0, len(data), 2))
            s2 = (s2 >> 16) + (s2 & 0xffff)
            return ~(s2 + (s2 >> 16)) & 0xffff
        pid = os.getpid() & 0xFFFF
        hdr = _struct.pack('bbHHh', 8, 0, 0, pid, 1)
        payload = b'parcinfo'
        chk = _chk(hdr + payload)
        pkt = _struct.pack('bbHHh', 8, 0, chk, pid, 1) + payload
        s.sendto(pkt, (ip_str, 0))
        resp = s.recv(1024)
        s.close()
        # TTL est à l'offset 8 de l'en-tête IP
        if len(resp) >= 9:
            return resp[8]
    except Exception:
        pass
    return None

def _ttl_os_guess(ip_str):
    """Deviner l'OS par le TTL de la réponse ping.
    
    Utilise uniquement la commande ping système pour lire le TTL.
    Le raw ICMP socket n'est pas utilisé (risque de faux positifs en parallèle).
    """
    try:
        if IS_WINDOWS:
            r = _run_hidden(['ping', '-n', '1', ip_str],
                            capture_output=True, text=True, timeout=3)
            m = re.search(r'TTL[=\s]+(\d+)', r.stdout, re.IGNORECASE)
        else:
            r = _run_hidden(['ping', '-c', '1', ip_str],
                            capture_output=True, text=True, timeout=3)
            m = re.search(r'ttl[=\s]*(\d+)', r.stdout, re.IGNORECASE)
        if m:
            ttl = int(m.group(1))
            if ttl <= 64:  return 'Linux/Unix'
            if ttl <= 128: return 'Windows'
            return 'Network'
    except Exception:
        pass
    return ""

def _mac_from_arp(ip_str):
    """Récupère l'adresse MAC depuis la table ARP.
    
    Stratégie : lit la TABLE COMPLÈTE puis cherche la ligne correspondant à ip_str.
    Ne jamais passer l'IP en argument à arp — sur Windows, arp -a <ip> ne retourne
    rien si l'entrée n'est pas encore dans le cache au moment exact de l'appel.
    """
    mac_regex = re.compile(r'([0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}')
    
    # 1. /proc/net/arp — Linux, le plus fiable et rapide
    try:
        with open('/proc/net/arp', 'r') as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip_str:
                    mac = parts[3]
                    if mac and mac not in ('00:00:00:00:00:00', '<incomplete>'):
                        return mac.lower()
    except Exception:
        pass
    
    # 2. arp -a (table complète) — Windows et Linux
    # IMPORTANT: utiliser une regex pour matcher l'IP exacte (éviter 192.168.1.1 dans 192.168.1.10)
    ip_pattern = re.compile(r'(?<![0-9])' + re.escape(ip_str) + r'(?![0-9])')
    bad_macs = {'ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00'}
    try:
        r = _run_hidden(['arp', '-a'], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if ip_pattern.search(line):
                m = mac_regex.search(line)
                if m:
                    mac = m.group(0).replace('-', ':').lower()
                    if mac not in bad_macs and not mac.startswith('01:') and not mac.startswith('ff:'):
                        return mac
    except Exception:
        pass
    
    # 3. ip neigh show (Linux moderne)
    try:
        r = _run_hidden(['ip', 'neigh', 'show'], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if ip_pattern.search(line) and 'FAILED' not in line and 'INCOMPLETE' not in line:
                m = mac_regex.search(line)
                if m:
                    return m.group(0).lower()
    except Exception:
        pass
    
    return ""

def _scan_ports(ip_str):
    """Scan TCP des ports configurés."""
    raw = cfg_get('scan_ports',
                  '21,22,23,25,53,80,110,135,139,143,389,443,445,631,1433,3306,3389,5900,8080,8443,9100')
    try:
        PORTS = [int(p.strip()) for p in raw.split(',') if p.strip().isdigit()]
    except Exception:
        PORTS = []
    if not PORTS:
        PORTS = [21,22,23,25,53,80,443,445,3389,9100]
    try:
        timeout = float(cfg_get('ping_timeout', '0.4'))
    except Exception:
        timeout = 0.4
    def check(p):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            ok = s.connect_ex((ip_str, p)) == 0
            s.close()
            return p if ok else None
        except Exception:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(PORTS), 50)) as ex:
        return sorted([p for p in ex.map(check, PORTS) if p])

def _deviner_type(hostname, ports, os_guess="", vendor=""):
    """Détermine le type d'équipement depuis le hostname, les ports ouverts, l'OS et le fabricant."""
    h = (hostname + " " + vendor).lower()
    # Imprimante
    if any(x in h for x in ['printer','print','canon','epson','brother','ricoh',
                              'xerox','kyocera','konica','lexmark','hp printer']):
        return 'Imprimante'
    if 9100 in ports or 631 in ports:
        return 'Imprimante'
    # Équipements réseau
    if any(x in h for x in ['router',' gw ','gateway','firewall','pfsense',
                              'fortigate','palo','checkpoint']):
        return 'Routeur/Pare-feu'
    if any(x in h for x in ['ubnt','unifi']):
        return 'Routeur/Pare-feu'
    if any(x in h for x in ['switch',' sw-']):
        return 'Switch'
    if any(x in h for x in ['cisco','juniper','extreme','3com']) and not any(x in h for x in ['server','srv']):
        return 'Switch'
    if any(x in h for x in ['ap-','borne','access point']):
        return 'Borne WiFi'
    # NAS / Serveur
    if any(x in h for x in ['synology','qnap',' nas']):
        return 'Serveur'
    if any(x in h for x in ['server',' srv','exchange','vcenter','esxi',' dc']):
        return 'Serveur'
    # PC Windows
    if 3389 in ports:
        return 'PC (Windows)'
    if 135 in ports or 445 in ports:
        return 'PC (Windows)'
    # PC Linux
    if 22 in ports and 80 not in ports and 443 not in ports:
        if os_guess == 'Linux/Unix' or not os_guess:
            return 'PC/Serveur (Linux)'
    # OS fingerprint
    if os_guess == 'Network':
        return 'Équipement réseau'
    if os_guess == 'Linux/Unix':
        return 'PC/Serveur (Linux)'
    if os_guess == 'Windows':
        return 'PC (Windows)'
    return 'PC'

def _scan_host(ip_str):
    """Scanne un hôte : ping, hostname, NetBIOS, OS, ports, MAC, fabricant."""
    if not _ping(ip_str):
        return None
    # Après ping, laisser l'OS peupler la table ARP
    # 0.5s est suffisant même sur les réseaux chargés
    _time.sleep(0.5)
    # Lancer hostname + NetBIOS + OS + ports en parallèle
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        f_hostname = ex.submit(_hostname,     ip_str)
        f_netbios  = ex.submit(_netbios_name, ip_str)
        f_os       = ex.submit(_ttl_os_guess, ip_str)
        f_ports    = ex.submit(_scan_ports,   ip_str)
        try: hostname  = f_hostname.result(timeout=5)
        except Exception: hostname = ""
        try: netbios   = f_netbios.result(timeout=5)
        except Exception: netbios = ""
        try: os_guess  = f_os.result(timeout=5)
        except Exception: os_guess = ""
        try: ports     = f_ports.result(timeout=15)
        except Exception: ports = []
    # Lire le MAC APRÈS les sondes (table ARP forcément peuplée maintenant)
    mac = _mac_from_arp(ip_str)
    # Si toujours vide, deuxième tentative après ping supplémentaire
    if not mac:
        _time.sleep(0.5)
        mac = _mac_from_arp(ip_str)
    vendor       = _oui_vendor(mac)
    display_name = netbios or hostname or ip_str
    host_type    = _deviner_type(display_name, ports, os_guess, vendor)
    return {
        "ip":           ip_str,
        "hostname":     hostname,
        "netbios":      netbios,
        "display_name": display_name,
        "mac":          mac,
        "vendor":       vendor,
        "ports":        ports,
        "os_guess":     os_guess,
        "type":         host_type,
        "en_ligne":     True,
    }

def _run_scan(plages, nb_threads):
    global scan_status
    with scan_lock:
        scan_status = {"running": True, "progress": 0, "message": "Résolution des plages...", "results": [], "errors": [], "plages": plages}
    try:
        hosts = []
        for plage in plages:
            try:
                hosts += [str(ip) for ip in ipaddress.ip_network(plage.strip(), strict=False).hosts()]
            except Exception as e:
                with scan_lock:
                    scan_status["errors"].append(f"Plage invalide '{plage}': {e}")
        # Dédoublonner
        hosts = list(dict.fromkeys(hosts))
        total = len(hosts); found = []; scanned = [0]
        def on_done(future, ip):
            scanned[0] += 1
            try: result = future.result()
            except: result = None
            with scan_lock:
                scan_status["progress"] = int(scanned[0] / total * 100)
                scan_status["message"] = f"Progression : {scanned[0]}/{total} — {len(found)} trouvé(s)..."
                if result:
                    found.append(result)
                    scan_status["results"] = list(found)
        with concurrent.futures.ThreadPoolExecutor(max_workers=nb_threads) as executor:
            futures = {executor.submit(_scan_host, ip): ip for ip in hosts}
            for f in concurrent.futures.as_completed(futures):
                on_done(f, futures[f])
        with scan_lock:
            scan_status.update({
                "progress": 100,
                "message": f"Terminé — {len(found)} appareil(s) détecté(s) sur {total} adresses",
                "running": False,
                "total_scanned": total,
            })
    except Exception as e:
        with scan_lock:
            scan_status.update({"message": f"Erreur : {e}", "running": False})

@app.route('/api/scan/lancer', methods=['POST'])
def lancer_scan():
    with scan_lock:
        if scan_status["running"]: return jsonify({"error":"Scan déjà en cours"}), 400
    data = request.json or {}
    # Support multiple ranges: "192.168.1.0/24,10.0.0.0/24" ou liste
    plage_raw = data.get('plage_ip', '192.168.1.0/24')
    if isinstance(plage_raw, list):
        plages = [p.strip() for p in plage_raw if p.strip()]
    else:
        plages = [p.strip() for p in plage_raw.split(',') if p.strip()]
    if not plages:
        plages = ['192.168.1.0/24']
    nb_threads = min(int(data.get('threads', 30)), 200)
    threading.Thread(target=_run_scan, args=(plages, nb_threads), daemon=True).start()
    return jsonify({"status": "started", "plages": plages})

@app.route('/api/scan/status')
def status_scan():
    with scan_lock: return jsonify(dict(scan_status))

@app.route('/api/scan/importer', methods=['POST'])
def importer_scan():
    cid = get_client_id()
    items = request.json.get('appareils', [])
    conn = get_db(); now = datetime.utcnow().isoformat()
    importes = 0; mis_a_jour = 0
    for item in items:
        ip        = item.get('ip', '')
        ports_str = ','.join(str(p) for p in item.get('ports', []))
        nom       = item.get('netbios') or item.get('display_name') or item.get('hostname') or ip
        dns       = item.get('hostname', '')
        mac       = item.get('mac', '')
        vendor    = item.get('vendor', '')
        marque    = vendor.split('/')[0].strip() if vendor else ''
        existing  = conn.execute('SELECT id FROM appareils WHERE client_id=? AND adresse_ip=?', (cid, ip)).fetchone()
        if existing:
            conn.execute(
                'UPDATE appareils SET en_ligne=1, dernier_ping=?, ports_ouverts=?, adresse_mac=COALESCE(NULLIF(adresse_mac,""),?), date_maj=? WHERE client_id=? AND adresse_ip=?',
                (now, ports_str, mac, now, cid, ip))
            mis_a_jour += 1
        else:
            conn.execute(
                '''INSERT INTO appareils (client_id,adresse_ip,nom_machine,nom_dns,adresse_mac,marque,type_appareil,
                   ports_ouverts,en_ligne,dernier_ping,decouvert_scan,statut,date_creation,date_maj)
                   VALUES (?,?,?,?,?,?,?,?,1,?,1,'actif',?,?)''',
                (cid, ip, nom, dns, mac, marque, item.get('type', 'PC'),
                 ports_str, now, now, now))
            importes += 1
    conn.commit(); conn.close()
    return jsonify({"importes":importes,"total":len(items)})



# --- PERIPHERIQUES -----------------------------------------------------------

# Colonnes triables pour l'inventaire périphériques
_PERIPH_SORT_COLS = {
    'cat':      'p.categorie, p.marque, p.modele',
    'marque':   'p.marque, p.modele',
    'app':      'nom_machine_lié, p.marque',
    'user':     'p.utilisateur_nom, p.marque',
    'loc':      'p.localisation, p.marque',
    'garantie': "CASE WHEN p.date_fin_garantie='' OR p.date_fin_garantie IS NULL THEN '9999-99-99' ELSE p.date_fin_garantie END",
    'statut':   'p.statut, p.marque',
}

@app.route('/peripheriques')
@login_required
def liste_peripheriques():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    page        = request.args.get('page', 1, type=int)
    filtre_cats = request.args.getlist('cat')
    sort_col    = request.args.get('sort', 'cat')
    sort_dir    = request.args.get('dir', 'asc')
    filtre_stat = request.args.get('statut', '')
    filtre_app  = request.args.get('appareil', '')
    q = ("SELECT p.*,"
         " u.prenom || ' ' || u.nom as utilisateur_nom,"
         " s.nom as service_nom, s.couleur as service_couleur,"
         " (SELECT COUNT(*) FROM contrats_peripheriques cp JOIN contrats ct"
         "  ON cp.contrat_id=ct.id WHERE cp.peripherique_id=p.id AND ct.client_id=p.client_id) as nb_contrats,"
         " (SELECT COUNT(*) FROM documents_peripheriques dp WHERE dp.peripherique_id=p.id) as nb_docs"
         " FROM peripheriques p"
         " LEFT JOIN utilisateurs u ON p.utilisateur_id = u.id"
         " LEFT JOIN services s ON u.service_id = s.id"
         " WHERE p.client_id=?")
    params = [cid]
    if filtre_cats:
        ph = ','.join('?' * len(filtre_cats))
        q += f' AND p.categorie IN ({ph})'
        params.extend(filtre_cats)
    if filtre_stat: q += ' AND p.statut=?';      params.append(filtre_stat)
    if filtre_app:
        q += ' AND p.id IN (SELECT peripherique_id FROM peripheriques_appareils WHERE appareil_id=?)'
        params.append(int(filtre_app))
    order_expr_p = _PERIPH_SORT_COLS.get(sort_col, 'p.categorie, p.marque, p.modele')
    dir_p = 'DESC' if sort_dir == 'desc' else 'ASC'
    q += f' ORDER BY {order_expr_p} {dir_p}'
    rows, pagination = paginate(q, tuple(params), page)
    periph = [fmt_garantie_periph(row_to_dict(r)) for r in rows]
    # Enrichir avec les appareils liés (via pivot)
    if periph:
        pid_list = ','.join(str(p['id']) for p in periph)
        conn2 = get_db()
        app_rows = conn2.execute(
            f"SELECT pa.peripherique_id, a.id, a.nom_machine, a.adresse_ip, a.type_appareil"
            f" FROM peripheriques_appareils pa"
            f" JOIN appareils a ON pa.appareil_id = a.id"
            f" WHERE pa.peripherique_id IN ({pid_list})").fetchall()
        conn2.close()
        app_map = {}
        for r in app_rows:
            app_map.setdefault(r[0], []).append({'id': r[1], 'nom_machine': r[2], 'adresse_ip': r[3], 'type_appareil': r[4]})
        for p in periph:
            p['appareils_lies'] = app_map.get(p['id'], [])
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT id,nom_machine,adresse_ip,type_appareil FROM appareils WHERE client_id=? ORDER BY nom_machine', (cid,)).fetchall()]
    cats_utilisees = [r[0] for r in conn.execute(
        'SELECT DISTINCT categorie FROM peripheriques WHERE client_id=? ORDER BY categorie', (cid,)).fetchall()]
    stats = {
        'total': conn.execute('SELECT COUNT(*) FROM peripheriques WHERE client_id=?', (cid,)).fetchone()[0],
        'actif': conn.execute("SELECT COUNT(*) FROM peripheriques WHERE client_id=? AND statut='actif'", (cid,)).fetchone()[0],
        'stock': conn.execute("SELECT COUNT(*) FROM peripheriques WHERE client_id=? AND statut='stock'", (cid,)).fetchone()[0],
        'hors_service': conn.execute("SELECT COUNT(*) FROM peripheriques WHERE client_id=? AND statut='hors_service'", (cid,)).fetchone()[0],
    }
    conn.close()
    return render_template('peripheriques.html', peripheriques=periph, appareils=appareils,
                           client=client, clients=get_clients(), client_actif_id=cid,
                           categories=get_liste_cached('categories_peripheriques'), cats_utilisees=cats_utilisees,
                           filtre_cats=filtre_cats, filtre_stat=filtre_stat, filtre_app=filtre_app,
                           sort_col=sort_col, sort_dir=sort_dir,
                           stats=stats, pagination=pagination)

@app.route('/peripherique/nouveau', methods=['GET','POST'])
def nouveau_peripherique():
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_peripheriques'))
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT id,nom_machine,adresse_ip,type_appareil FROM appareils WHERE client_id=? ORDER BY nom_machine', (cid,)).fetchall()]
    utilisateurs = [row_to_dict(r) for r in conn.execute(
        "SELECT id,prenom,nom FROM utilisateurs WHERE client_id=? AND statut='actif' ORDER BY nom", (cid,)).fetchall()]
    conn.close()
    if request.method == 'POST':
        now = datetime.now().isoformat()
        conn = get_db()
        vals = _extract_periph(cid, request.form)
        conn.execute(("INSERT INTO peripheriques"
            " (client_id,utilisateur_id,categorie,marque,modele,numero_serie,description,"
            "localisation,statut,date_achat,duree_garantie,date_fin_garantie,fournisseur,prix_achat,"
            "numero_commande,notes,date_creation,date_maj)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"), vals + (now, now))
        new_pid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        # Liens appareils N:N
        app_ids = request.form.getlist('appareil_ids')
        for aid in app_ids:
            try:
                conn.execute("INSERT OR IGNORE INTO peripheriques_appareils (peripherique_id, appareil_id) VALUES (?,?)",
                             (new_pid, int(aid)))
            except Exception:
                pass
        nom_p = (request.form.get('marque','') + ' ' + request.form.get('modele','')).strip() or 'Nouveau périphérique'
        log_history(conn, cid, 'peripherique', new_pid, nom_p, 'Création')
        conn.commit(); conn.close()
        flash('Peripherique ajoute', 'success')
        return redirect(url_for('liste_peripheriques'))
    pre_app = request.args.get('appareil_id', '')
    return render_template('form_peripherique.html', peripherique=None, action='Ajouter',
                           appareils=appareils, utilisateurs=utilisateurs,
                           client=client, clients=get_clients(), client_actif_id=cid,
                           categories=get_liste_cached('categories_peripheriques'), pre_appareil_id=pre_app,
                           linked_app_ids=[int(pre_app)] if pre_app else [])

@app.route('/peripherique/<int:id>/editer', methods=['GET','POST'])
def editer_peripherique(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_peripheriques'))
    cid = get_client_id()
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT id,nom_machine,adresse_ip,type_appareil FROM appareils WHERE client_id=? ORDER BY nom_machine', (cid,)).fetchall()]
    utilisateurs = [row_to_dict(r) for r in conn.execute(
        "SELECT id,prenom,nom FROM utilisateurs WHERE client_id=? AND statut='actif' ORDER BY nom", (cid,)).fetchall()]
    if request.method == 'POST':
        now = datetime.now().isoformat()
        _old = row_to_dict(conn.execute('SELECT * FROM peripheriques WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
        vals = _extract_periph(cid, request.form)
        conn.execute(("UPDATE peripheriques SET"
            " client_id=?,utilisateur_id=?,categorie=?,marque=?,modele=?,numero_serie=?,"
            "description=?,localisation=?,statut=?,date_achat=?,duree_garantie=?,date_fin_garantie=?,"
            "fournisseur=?,prix_achat=?,numero_commande=?,notes=?,date_maj=? WHERE id=? AND client_id=?"),
            vals + (now, id, cid))
        # Mettre à jour les liens appareils N:N
        app_ids = request.form.getlist('appareil_ids')
        conn.execute("DELETE FROM peripheriques_appareils WHERE peripherique_id=?", (id,))
        for aid in app_ids:
            try:
                conn.execute("INSERT OR IGNORE INTO peripheriques_appareils (peripherique_id, appareil_id) VALUES (?,?)",
                             (id, int(aid)))
            except Exception:
                pass
        nom = (request.form.get('marque','') + ' ' + request.form.get('modele','')).strip() or f'Périphérique #{id}'
        _cols_p = _ENTITE_COLS['peripherique']
        _details_p = _diff_json({k: str(_old.get(k,'') or '') for k in _cols_p},
                                  {k: str(request.form.get(k,'') or '') for k in _cols_p})
        log_history(conn, cid, 'peripherique', id, nom, 'Modification', _details_p)
        conn.commit(); conn.close()
        flash('Peripherique mis a jour', 'success')
        return redirect(url_for('liste_peripheriques'))
    p = fmt_garantie_periph(row_to_dict(
        conn.execute('SELECT * FROM peripheriques WHERE id=? AND client_id=?', (id, cid)).fetchone() or {}))
    docs_per = [row_to_dict(r) for r in conn.execute(
        'SELECT id, peripherique_id, client_id, nom, description, type_doc, nom_fichier, taille, date_upload, sync_status FROM documents_peripheriques WHERE peripherique_id=? ORDER BY date_upload DESC', (id,)).fetchall()]
    for d in docs_per:
        d['taille_fmt'] = human_size(d.get('taille', 0))
    # Appareils déjà liés à ce périphérique
    linked_app_ids = [r[0] for r in conn.execute(
        "SELECT appareil_id FROM peripheriques_appareils WHERE peripherique_id=?", (id,)).fetchall()]

    # Fetch related interventions
    interventions = [fmt_intervention(row_to_dict(r)) for r in conn.execute(
        'SELECT i.* FROM interventions i JOIN interventions_peripheriques ip ON i.id=ip.intervention_id '
        'WHERE ip.peripherique_id=? AND i.statut != ? ORDER BY i.date_intervention DESC LIMIT 10',
        (id, 'archivee')).fetchall()]

    conn.close()
    return render_template('form_peripherique.html', peripherique=p, documents=docs_per, action='Modifier',
                           appareils=appareils, utilisateurs=utilisateurs,
                           client=client, clients=get_clients(), client_actif_id=cid,
                           categories=get_liste_cached('categories_peripheriques'), pre_appareil_id='',
                           linked_app_ids=linked_app_ids, interventions=interventions)

@app.route('/peripherique/<int:id>/supprimer', methods=['POST'])
def supprimer_peripherique(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_peripheriques'))
    cid = get_client_id()
    conn = get_db()
    p = row_to_dict(conn.execute('SELECT marque,modele FROM peripheriques WHERE id=?',(id,)).fetchone() or {})
    nom_p = (p.get('marque','') + ' ' + p.get('modele','')).strip() or '?'
    log_history(conn, cid, 'peripherique', id, nom_p, 'Suppression')
    conn.execute('DELETE FROM peripheriques WHERE id=? AND client_id=?', (id, cid))
    conn.commit(); conn.close()
    flash('Peripherique supprime', 'info')
    return redirect(url_for('liste_peripheriques'))

@app.route('/api/peripheriques/appareil/<int:app_id>')
def api_periph_appareil(app_id):
    cid = get_client_id()
    conn = get_db()
    rows = [row_to_dict(r) for r in conn.execute(
        ("SELECT p.*, u.prenom || ' ' || u.nom as utilisateur_nom"
         " FROM peripheriques p"
         " JOIN peripheriques_appareils pa ON pa.peripherique_id = p.id"
         " LEFT JOIN utilisateurs u ON p.utilisateur_id = u.id"
         " WHERE pa.appareil_id=? AND p.client_id=? ORDER BY p.categorie"),
        (app_id, cid)).fetchall()]
    conn.close()
    return jsonify(rows)

def _extract_periph(cid, f):
    user_id = int(f.get('utilisateur_id') or 0) or None
    prix = None
    try:
        prix = float(f['prix_achat']) if f.get('prix_achat') else None
    except:
        pass
    duree = 0
    try:
        duree = int(f['duree_garantie']) if f.get('duree_garantie') else 0
    except:
        pass
    return (cid, user_id,
            f.get('categorie',''), f.get('marque',''), f.get('modele',''),
            f.get('numero_serie',''), f.get('description',''), f.get('localisation',''),
            f.get('statut','actif'), f.get('date_achat',''), duree,
            f.get('date_fin_garantie',''), f.get('fournisseur',''), prix,
            f.get('numero_commande',''), f.get('notes',''))



# --- CONTRATS & ABONNEMENTS --------------------------------------------------

PERIODICITES = ['mensuel', 'trimestriel', 'semestriel', 'annuel', 'pluriannuel', 'unique']

@app.route('/contrats')
@login_required
def liste_contrats():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    page          = request.args.get('page', 1, type=int)
    filtre_type   = request.args.get('type', '')
    filtre_stat   = request.args.get('statut', '')
    filtre_app_id = request.args.get('appareil', '')
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    q = 'SELECT * FROM contrats WHERE client_id=?'
    params = [cid]
    if filtre_type:   q += ' AND type_contrat=?'; params.append(filtre_type)
    if filtre_stat:   q += ' AND statut=?'; params.append(filtre_stat)
    if filtre_app_id:
        q = ('SELECT DISTINCT c.* FROM contrats c '
             'JOIN contrats_appareils ca ON c.id=ca.contrat_id '
             'WHERE c.client_id=? AND ca.appareil_id=?')
        params = [cid, int(filtre_app_id)]
        if filtre_type: q += ' AND c.type_contrat=?'; params.append(filtre_type)
        if filtre_stat: q += ' AND c.statut=?'; params.append(filtre_stat)
    q += ' ORDER BY date_fin, titre'
    rows, pagination = paginate(q, tuple(params), page)
    contrats = [fmt_contrat(row_to_dict(r)) for r in rows]
    # Nom de l'appareil filtré pour l'afficher dans la page
    filtre_app_nom = ''
    if filtre_app_id:
        a = conn.execute('SELECT nom_machine FROM appareils WHERE id=?', (int(filtre_app_id),)).fetchone()
        if a: filtre_app_nom = a[0]
    # Compter les elements lies pour chaque contrat
    for ct in contrats:
        ct['nb_appareils'] = conn.execute(
            'SELECT COUNT(*) FROM contrats_appareils WHERE contrat_id=?', (ct['id'],)).fetchone()[0]
        ct['nb_peripheriques'] = conn.execute(
            'SELECT COUNT(*) FROM contrats_peripheriques WHERE contrat_id=?', (ct['id'],)).fetchone()[0]
        ct['nb_docs'] = conn.execute(
            'SELECT COUNT(*) FROM documents_contrats WHERE contrat_id=?', (ct['id'],)).fetchone()[0]
    types_utilises = [r[0] for r in conn.execute(
        'SELECT DISTINCT type_contrat FROM contrats WHERE client_id=? ORDER BY type_contrat', (cid,)).fetchall()]
    stats = {
        'total':   conn.execute('SELECT COUNT(*) FROM contrats WHERE client_id=?', (cid,)).fetchone()[0],
        'actif':   conn.execute("SELECT COUNT(*) FROM contrats WHERE client_id=? AND statut='actif'", (cid,)).fetchone()[0],
        'expire':  conn.execute("SELECT COUNT(*) FROM contrats WHERE client_id=? AND statut='expire'", (cid,)).fetchone()[0],
        'resilie': conn.execute("SELECT COUNT(*) FROM contrats WHERE client_id=? AND statut='resilie'", (cid,)).fetchone()[0],
    }
    # Alertes: contrats expirant bientot
    alertes = [ct for ct in contrats if ct['expire_bientot'] or ct['expire_depasse']]
    conn.close()
    return render_template('contrats.html', contrats=contrats, client=client,
                           clients=get_clients(), client_actif_id=cid,
                           types_contrats=get_liste_cached('types_contrats'), types_utilises=types_utilises,
                           periodicites=PERIODICITES, stats=stats, alertes=alertes,
                           filtre_type=filtre_type, filtre_stat=filtre_stat,
                           filtre_app_id=filtre_app_id, filtre_app_nom=filtre_app_nom,
                           pagination=pagination)

@app.route('/contrat/nouveau', methods=['GET','POST'])
def nouveau_contrat():
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_contrats'))
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT id,nom_machine,type_appareil,adresse_ip FROM appareils WHERE client_id=? ORDER BY nom_machine', (cid,)).fetchall()]
    peripheriques = [row_to_dict(r) for r in conn.execute(
        'SELECT id,categorie,marque,modele,description FROM peripheriques WHERE client_id=? ORDER BY categorie,marque', (cid,)).fetchall()]
    conn.close()
    if request.method == 'POST':
        errs = validate_form([
            ('titre',       'str',   True),
            ('date_debut',  'date',  False),
            ('date_fin',    'date',  False),
            ('email_fournisseur', 'email', False),
        ], request.form)
        if errs:
            for e in errs: flash(e, 'danger')
            return redirect(request.url)
        f = request.form; now = datetime.now().isoformat()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""INSERT INTO contrats (client_id,titre,type_contrat,fournisseur,contact_fournisseur,
            email_fournisseur,telephone_fournisseur,numero_contrat,date_debut,date_fin,
            reconduction_auto,preavis_jours,montant_ht,periodicite,description,notes,
            statut,date_creation,date_maj) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            _extract_contrat(cid, f) + (now, now))
        cid_contrat = cur.lastrowid
        # Liaisons appareils
        for app_id in request.form.getlist('appareils_lies'):
            try:
                conn.execute('INSERT INTO contrats_appareils (contrat_id,appareil_id) VALUES (?,?)', (cid_contrat, int(app_id)))
            except: pass
        # Liaisons peripheriques
        for per_id in request.form.getlist('peripheriques_lies'):
            try:
                conn.execute('INSERT INTO contrats_peripheriques (contrat_id,peripherique_id) VALUES (?,?)', (cid_contrat, int(per_id)))
            except: pass
        new_cid2 = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        log_history(conn, cid, 'contrat', new_cid2, request.form.get('titre','') or 'Nouveau contrat', 'Création')
        conn.commit(); conn.close()
        flash('Contrat créé', 'success')
        return redirect(url_for('detail_contrat', id=cid_contrat))
    return render_template('form_contrat.html', contrat=None, action='Nouveau',
                           appareils=appareils, peripheriques=peripheriques,
                           appareils_lies=[], peripheriques_lies=[],
                           client=client, clients=get_clients(), client_actif_id=cid,
                           types_contrats=get_liste_cached('types_contrats'), periodicites=PERIODICITES)

@app.route('/contrat/<int:id>', methods=['GET'])
def detail_contrat(id):
    cid = get_client_id()
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    ct = fmt_contrat(row_to_dict(
        conn.execute('SELECT * FROM contrats WHERE id=? AND client_id=?', (id, cid)).fetchone() or {}))
    appareils_lies = [row_to_dict(r) for r in conn.execute(
        'SELECT a.* FROM appareils a JOIN contrats_appareils ca ON a.id=ca.appareil_id WHERE ca.contrat_id=?', (id,)).fetchall()]
    periph_lies = [row_to_dict(r) for r in conn.execute(
        'SELECT p.* FROM peripheriques p JOIN contrats_peripheriques cp ON p.id=cp.peripherique_id WHERE cp.contrat_id=?', (id,)).fetchall()]
    docs = [row_to_dict(r) for r in conn.execute(
        'SELECT id, contrat_id, client_id, nom, description, type_doc, nom_fichier, taille, date_upload, sync_status FROM documents_contrats WHERE contrat_id=? ORDER BY date_upload DESC', (id,)).fetchall()]
    for d in docs: d['taille_fmt'] = human_size(d.get('taille', 0))

    # Fetch related interventions
    interventions = [fmt_intervention(row_to_dict(r)) for r in conn.execute(
        'SELECT * FROM interventions WHERE contrat_id=? AND statut != ? ORDER BY date_intervention DESC LIMIT 10',
        (id, 'archivee')).fetchall()]

    conn.close()
    return render_template('detail_contrat.html', contrat=ct, appareils_lies=appareils_lies,
                           periph_lies=periph_lies, docs=docs, interventions=interventions,
                           client=client, clients=get_clients(), client_actif_id=cid)

@app.route('/contrat/<int:id>/editer', methods=['GET','POST'])
def editer_contrat(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_contrats'))
    cid = get_client_id()
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT id,nom_machine,type_appareil,adresse_ip FROM appareils WHERE client_id=? ORDER BY nom_machine', (cid,)).fetchall()]
    peripheriques = [row_to_dict(r) for r in conn.execute(
        'SELECT id,categorie,marque,modele,description FROM peripheriques WHERE client_id=? ORDER BY categorie,marque', (cid,)).fetchall()]
    if request.method == 'POST':
        errs = validate_form([
            ('titre',       'str',   True),
            ('date_debut',  'date',  False),
            ('date_fin',    'date',  False),
            ('email_fournisseur', 'email', False),
        ], request.form)
        if errs:
            for e in errs: flash(e, 'danger')
            return redirect(request.url)
        f = request.form; now = datetime.now().isoformat()
        _old = row_to_dict(conn.execute('SELECT * FROM contrats WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
        conn.execute("""UPDATE contrats SET titre=?,type_contrat=?,fournisseur=?,contact_fournisseur=?,
            email_fournisseur=?,telephone_fournisseur=?,numero_contrat=?,date_debut=?,date_fin=?,
            reconduction_auto=?,preavis_jours=?,montant_ht=?,periodicite=?,description=?,notes=?,
            statut=?,date_maj=? WHERE id=? AND client_id=?""",
            _extract_contrat(cid, f)[1:] + (now, id, cid))
        # Reset liaisons
        conn.execute('DELETE FROM contrats_appareils WHERE contrat_id=?', (id,))
        conn.execute('DELETE FROM contrats_peripheriques WHERE contrat_id=?', (id,))
        for app_id in request.form.getlist('appareils_lies'):
            try: conn.execute('INSERT INTO contrats_appareils (contrat_id,appareil_id) VALUES (?,?)', (id, int(app_id)))
            except: pass
        for per_id in request.form.getlist('peripheriques_lies'):
            try: conn.execute('INSERT INTO contrats_peripheriques (contrat_id,peripherique_id) VALUES (?,?)', (id, int(per_id)))
            except: pass
        _cols_c = _ENTITE_COLS['contrat']
        _details_c = _diff_json({k: str(_old.get(k,'') or '') for k in _cols_c},
                                  {k: str(f.get(k,'') or '') for k in _cols_c})
        log_history(conn, cid, 'contrat', id, f.get('titre','') or f'Contrat #{id}', 'Modification', _details_c)
        conn.commit(); conn.close()
        flash('Contrat mis à jour', 'success')
        return redirect(url_for('detail_contrat', id=id))
    ct = fmt_contrat(row_to_dict(
        conn.execute('SELECT * FROM contrats WHERE id=? AND client_id=?', (id, cid)).fetchone() or {}))
    appareils_lies = [r[0] for r in conn.execute(
        'SELECT appareil_id FROM contrats_appareils WHERE contrat_id=?', (id,)).fetchall()]
    periph_lies = [r[0] for r in conn.execute(
        'SELECT peripherique_id FROM contrats_peripheriques WHERE contrat_id=?', (id,)).fetchall()]
    conn.close()
    return render_template('form_contrat.html', contrat=ct, action='Modifier',
                           appareils=appareils, peripheriques=peripheriques,
                           appareils_lies=appareils_lies, peripheriques_lies=periph_lies,
                           client=client, clients=get_clients(), client_actif_id=cid,
                           types_contrats=get_liste_cached('types_contrats'), periodicites=PERIODICITES)

@app.route('/contrat/<int:id>/supprimer', methods=['POST'])
def supprimer_contrat(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_contrats'))
    cid = get_client_id()
    conn = get_db()
    conn.execute('DELETE FROM contrats WHERE id=? AND client_id=?', (id, cid))
    conn.commit(); conn.close()
    flash('Contrat supprimé', 'info')
    return redirect(url_for('liste_contrats'))

@app.route('/contrat/<int:id>/document/upload', methods=['POST'])
def upload_doc_contrat(id):
    cid = get_client_id()
    if 'fichier' not in request.files:
        return redirect(url_for('detail_contrat', id=id))
    f = request.files['fichier']
    if not f.filename or not allowed_file(f.filename):
        flash('Type non autorisé', 'danger')
        return redirect(url_for('detail_contrat', id=id))
    unique = f"ctr{id}_{int(time.time())}_{secure_filename(f.filename)}"
    save_path = os.path.join(UPLOAD_FOLDER, unique)
    f.save(save_path)
    taille = os.path.getsize(save_path)

    nom = request.form.get('nom','') or f.filename
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute('INSERT INTO documents_contrats (contrat_id,client_id,nom,description,type_doc,nom_fichier,taille,date_upload,contenu_blob,sync_status,date_sync) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                 (id, cid, nom, request.form.get('description',''), request.form.get('type_doc',''), unique, taille, now, None, 'local', ''))

    # Log document upload
    ctr_title = conn.execute('SELECT titre FROM contrats WHERE id=? AND client_id=?', (id, cid)).fetchone()
    ctr_name = ctr_title[0] if ctr_title else f'Contrat #{id}'
    log_history(conn, cid, 'contrat', id, ctr_name, 'Ajout de document',
                _diff_json({}, {'nom': nom, 'fichier': unique, 'type_doc': request.form.get('type_doc','')}))

    conn.commit(); conn.close()
    flash(f'Document ajouté', 'success')
    return redirect(url_for('detail_contrat', id=id))

@app.route('/contrat/document/<int:id>/supprimer', methods=['POST'])
def supprimer_doc_contrat(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_contrats'))
    cid = get_client_id()
    conn = get_db()
    doc = row_to_dict(conn.execute('SELECT id, contrat_id, client_id, nom, nom_fichier, contenu_blob FROM documents_contrats WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    ctr_id = doc.get('contrat_id', 0)
    if doc:
        conn.execute('DELETE FROM documents_contrats WHERE id=?', (id,))

        # Log document deletion
        ctr_title = conn.execute('SELECT titre FROM contrats WHERE id=? AND client_id=?', (ctr_id, cid)).fetchone()
        ctr_name = ctr_title[0] if ctr_title else f'Contrat #{ctr_id}'
        log_history(conn, cid, 'contrat', ctr_id, ctr_name, 'Suppression de document',
                    _diff_json({'nom': doc.get('nom', ''), 'fichier': doc.get('nom_fichier', '')}, {}))

        conn.commit()
        try: os.remove(os.path.join(UPLOAD_FOLDER, doc['nom_fichier']))
        except: pass
    conn.close()
    return redirect(url_for('detail_contrat', id=ctr_id))

@app.route('/contrat/document/<int:id>/apercu')
def apercu_doc_contrat(id):
    cid = get_client_id()
    conn = get_db()
    doc = row_to_dict(conn.execute('SELECT id, contrat_id, client_id, nom, nom_fichier, contenu_blob FROM documents_contrats WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()
    if not doc: return 'Not found', 404

    # Préférer servir depuis BLOB si disponible (synced)
    if doc.get('contenu_blob'):
        return send_file(
            io.BytesIO(doc['contenu_blob']),
            as_attachment=False
        )

    # Fallback: servir depuis fichier local
    return send_from_directory(UPLOAD_FOLDER, doc['nom_fichier'], as_attachment=False)

@app.route('/contrat/document/<int:id>/telecharger')
def telecharger_doc_contrat(id):
    cid = get_client_id()
    conn = get_db()
    doc = row_to_dict(conn.execute('SELECT id, contrat_id, client_id, nom, nom_fichier, contenu_blob FROM documents_contrats WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()
    if not doc: return 'Not found', 404

    # Préférer servir depuis BLOB si disponible (synced)
    if doc.get('contenu_blob'):
        return send_file(
            io.BytesIO(doc['contenu_blob']),
            as_attachment=True,
            download_name=doc['nom']
        )

    # Fallback: servir depuis fichier local
    return send_from_directory(UPLOAD_FOLDER, doc['nom_fichier'], as_attachment=True, download_name=doc['nom'])

@app.route('/api/contrats/appareil/<int:app_id>')
def api_contrats_appareil(app_id):
    cid = get_client_id()
    conn = get_db()
    rows = [row_to_dict(r) for r in conn.execute(
        'SELECT c.* FROM contrats c JOIN contrats_appareils ca ON c.id=ca.contrat_id WHERE ca.appareil_id=? AND c.client_id=?',
        (app_id, cid)).fetchall()]
    conn.close()
    return jsonify([fmt_contrat(r) for r in rows])

@app.route('/api/contrats/peripherique/<int:per_id>')
def api_contrats_peripherique(per_id):
    cid = get_client_id()
    conn = get_db()
    rows = [row_to_dict(r) for r in conn.execute(
        'SELECT c.* FROM contrats c JOIN contrats_peripheriques cp ON c.id=cp.contrat_id WHERE cp.peripherique_id=? AND c.client_id=?',
        (per_id, cid)).fetchall()]
    conn.close()
    return jsonify([fmt_contrat(r) for r in rows])

def _extract_contrat(cid, f):
    montant = None
    try: montant = float(f['montant_ht']) if f.get('montant_ht') else None
    except: pass
    preavis = 30
    try: preavis = int(f.get('preavis_jours') or 30)
    except: pass
    return (cid, f.get('titre',''), f.get('type_contrat',''), f.get('fournisseur',''),
            f.get('contact_fournisseur',''), f.get('email_fournisseur',''), f.get('telephone_fournisseur',''),
            f.get('numero_contrat',''), f.get('date_debut',''), f.get('date_fin',''),
            1 if f.get('reconduction_auto') else 0, preavis, montant,
            f.get('periodicite','annuel'), f.get('description',''), f.get('notes',''),
            f.get('statut','actif'))


def _generate_maintenance_series(conn, maint_id, cid, date_planifiee, recurrence, date_fin_recurrence, created_by, f, lookahead_date=None):
    """Génère les occurrences récurrentes d'une maintenance jusqu'à lookahead_date (ou date_fin_recurrence si None)"""
    if not recurrence or recurrence == '':
        return

    from datetime import datetime, timedelta

    try:
        start_date = datetime.strptime(date_planifiee, '%Y-%m-%d')
        end_date = datetime.strptime(date_fin_recurrence, '%Y-%m-%d') if date_fin_recurrence else None
    except:
        logger.warning(f"Erreur parsing dates pour récurrence: {date_planifiee}, {date_fin_recurrence}")
        return

    if not end_date or end_date <= start_date:
        return

    # Utiliser lookahead_date si fourni (sinon utiliser date_fin_recurrence)
    if lookahead_date:
        try:
            cutoff_date = datetime.strptime(lookahead_date, '%Y-%m-%d') if isinstance(lookahead_date, str) else lookahead_date
        except:
            cutoff_date = end_date
    else:
        cutoff_date = end_date

    # Calculer les dates futures selon le type de récurrence
    occurrences = []

    if recurrence == 'hebdomadaire':
        current_date = start_date + timedelta(days=7)
        while current_date <= cutoff_date:
            occurrences.append(current_date.strftime('%Y-%m-%d'))
            current_date += timedelta(days=7)
    elif recurrence == 'mensuelle':
        current_date = start_date
        while True:
            # Ajouter 1 mois
            if current_date.month == 12:
                current_date = current_date.replace(year=current_date.year + 1, month=1)
            else:
                current_date = current_date.replace(month=current_date.month + 1)
            if current_date > cutoff_date:
                break
            occurrences.append(current_date.strftime('%Y-%m-%d'))
    elif recurrence == 'annuelle':
        current_date = start_date
        while True:
            current_date = current_date.replace(year=current_date.year + 1)
            if current_date > cutoff_date:
                break
            occurrences.append(current_date.strftime('%Y-%m-%d'))
    else:
        return

    # Extraire appareil_id et peripherique_id
    appareil_id = None
    try: appareil_id = int(f.get('appareil_id')) if f.get('appareil_id') else None
    except: pass
    peripherique_id = None
    try: peripherique_id = int(f.get('peripherique_id')) if f.get('peripherique_id') else None
    except: pass

    # Insérer les occurrences
    for occ_date in occurrences:
        conn.execute(
            '''INSERT INTO maintenances
            (client_id, appareil_id, peripherique_id, type_maintenance, description,
             date_planifiee, date_realisee, heure_debut, heure_fin, responsable, notes,
             statut, recurrence, date_fin_recurrence, parent_id, created_by, updated_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (cid, appareil_id, peripherique_id, f.get('type_maintenance',''), f.get('description',''),
             occ_date, '', f.get('heure_debut',''), f.get('heure_fin',''), f.get('responsable',''),
             f.get('notes',''), 'programmee', recurrence, date_fin_recurrence, maint_id, created_by, created_by)
        )

    conn.commit()


def _regenerate_all_maintenance_occurrences():
    """Cron job: génère les occurrences futures pour toutes les maintenances récurrentes"""
    try:
        from datetime import datetime, timedelta
        conn = get_db()

        # Récupérer toutes les maintenances avec récurrence actives
        maintenances = conn.execute(
            '''SELECT id, client_id, date_planifiee, recurrence, date_fin_recurrence,
                      appareil_id, peripherique_id, type_maintenance, description,
                      heure_debut, heure_fin, responsable, notes
               FROM maintenances
               WHERE recurrence IS NOT NULL AND recurrence != ''
                     AND date_fin_recurrence IS NOT NULL
                     AND parent_id IS NULL
               ORDER BY id'''
        ).fetchall()

        for maint in maintenances:
            m = row_to_dict(maint)
            # Vérifier la dernière occurrence générée
            last_occ = conn.execute(
                'SELECT MAX(date_planifiee) FROM maintenances WHERE parent_id=?',
                (m['id'],)
            ).fetchone()[0]

            if not last_occ:
                last_occ = m['date_planifiee']

            # Lookahead: générer jusqu'à aujourd'hui + 28 jours
            lookahead_date = (datetime.now() + timedelta(days=28)).strftime('%Y-%m-%d')
            last_occ_dt = datetime.strptime(last_occ, '%Y-%m-%d')

            # Si la dernière occurrence est < lookahead, générer les nouvelles
            if last_occ_dt < datetime.strptime(lookahead_date, '%Y-%m-%d'):
                # Construire un fake form dict pour _generate_maintenance_series
                fake_form = {
                    'type_maintenance': m['type_maintenance'],
                    'description': m['description'],
                    'appareil_id': m['appareil_id'],
                    'peripherique_id': m['peripherique_id'],
                    'heure_debut': m['heure_debut'],
                    'heure_fin': m['heure_fin'],
                    'responsable': m['responsable'],
                    'notes': m['notes'],
                }
                _generate_maintenance_series(conn, m['id'], m['client_id'],
                                            last_occ, m['recurrence'],
                                            m['date_fin_recurrence'],
                                            1,  # created_by=1 (cron job)
                                            fake_form, lookahead_date=lookahead_date)

        conn.close()
        logger.info(f"Cron job: {len(maintenances)} maintenances récurrentes vérifiées")
    except Exception as e:
        logger.exception(f"Erreur dans cron job de régénération: {e}")


def _send_email(to_email, subject, body):
    """Envoie un email via SMTP. Retourne True si succès."""
    try:
        smtp_server = cfg_get('smtp_server', '')
        smtp_port = int(cfg_get('smtp_port', '587'))
        smtp_login = cfg_get('smtp_login', '')
        smtp_password = cfg_get('smtp_password', '')
        from_email = cfg_get('from_email', smtp_login)

        if not all([smtp_server, smtp_login, smtp_password]):
            logger.warning('SMTP non configuré - notification ignorée')
            return False

        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = from_email
        msg['To'] = to_email
        msg.attach(MIMEText(body, 'html'))

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_login, smtp_password)
        server.sendmail(from_email, [to_email], msg.as_string())
        server.quit()

        return True
    except Exception as e:
        logger.error(f'Erreur envoi email: {e}')
        return False


def _notify_upcoming_maintenances():
    """Cron job: envoie notifications des maintenances à venir (3 jours)"""
    try:
        conn = get_db()

        # Récupérer maintenances des 3 prochains jours
        today = date.today().isoformat()
        in_3_days = (date.today() + timedelta(days=3)).isoformat()

        maintenances = conn.execute('''
            SELECT m.id, m.date_planifiee, m.type_maintenance, m.description,
                   m.responsable, m.statut, a.nom_machine, p.categorie, p.marque
            FROM maintenances m
            LEFT JOIN appareils a ON m.appareil_id = a.id
            LEFT JOIN peripheriques p ON m.peripherique_id = p.id
            WHERE m.statut = 'programmee'
              AND m.date_planifiee BETWEEN ? AND ?
              AND NOT EXISTS (
                SELECT 1 FROM maintenance_notifications
                WHERE maintenance_id=m.id AND notification_date >= ?
              )
            ORDER BY m.date_planifiee
        ''', (today, in_3_days, today)).fetchall()

        for maint in maintenances:
            m = row_to_dict(maint)
            recipient = m.get('responsable', '')

            if recipient and '@' in recipient:
                subject = f"⚙️ Maintenance à venir: {m['type_maintenance']} - {m['date_planifiee']}"

                equipment = m.get('nom_machine') or f"{m.get('categorie', '')} {m.get('marque', '')}"
                body = f"""<html><body style="font-family: Arial;">
                <h2>Notification de maintenance</h2>
                <p><strong>Date:</strong> {m['date_planifiee']}</p>
                <p><strong>Type:</strong> {m['type_maintenance']}</p>
                <p><strong>Équipement:</strong> {equipment or '—'}</p>
                <p><strong>Description:</strong> {m.get('description', '—')}</p>
                <p><em>Veuillez confirmer l'exécution dans ParcInfo</em></p>
                </body></html>"""

                if _send_email(recipient, subject, body):
                    # Enregistrer la notification envoyée
                    conn.execute(
                        'INSERT INTO maintenance_notifications (maintenance_id, notification_date) VALUES (?, ?)',
                        (m['id'], today)
                    )
                    conn.commit()
                    logger.info(f"Notification envoyée pour maintenance {m['id']} à {recipient}")

        conn.close()
    except Exception as e:
        logger.exception(f'Erreur notification maintenances: {e}')


def _extract_maintenance(cid, f, user_id):
    appareil_id = None
    try: appareil_id = int(f.get('appareil_id')) if f.get('appareil_id') else None
    except: pass
    peripherique_id = None
    try: peripherique_id = int(f.get('peripherique_id')) if f.get('peripherique_id') else None
    except: pass
    contrat_id = None
    try: contrat_id = int(f.get('contrat_id')) if f.get('contrat_id') else None
    except: pass
    return (cid, appareil_id, peripherique_id, contrat_id, f.get('type_maintenance',''), f.get('description',''),
            f.get('date_planifiee',''), f.get('date_realisee',''), f.get('heure_debut',''),
            f.get('heure_fin',''), f.get('responsable',''), f.get('notes',''),
            f.get('statut','programmee'), f.get('recurrence'), f.get('date_fin_recurrence'),
            None, user_id, user_id)


def _format_maintenance_for_list(rows):
    """Formate maintenances pour affichage liste"""
    result = []
    for r in rows:
        m = row_to_dict(r)
        m['statut_label'] = {
            'programmee': 'Programmée',
            'realisee': 'Réalisée',
            'reportee': 'Reportée',
            'annulee': 'Annulée'
        }.get(m.get('statut', ''), m.get('statut', ''))
        m['type_label'] = (m.get('type_maintenance') or '').title()
        _format_date_field(m, 'date_planifiee')
        if m.get('date_realisee'):
            _format_date_field(m, 'date_realisee')
        result.append(m)
    return result


# --- INTERVENTIONS ----------------------------------------------------------

@app.route('/interventions')
@login_required
def liste_interventions():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    page = request.args.get('page', 1, type=int)
    q = request.args.get('q', '')
    filtre_type = request.args.get('type', '')
    filtre_statut = request.args.get('statut', '')
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})

    # Build query
    query = 'SELECT * FROM interventions WHERE client_id=? AND statut != ?'
    params = [cid, 'archivee']

    if q:
        query += ' AND titre LIKE ?'
        params.append(f'%{q}%')
    if filtre_type:
        query += ' AND type_intervention=?'
        params.append(filtre_type)
    if filtre_statut:
        query += ' AND statut=?'
        params.append(filtre_statut)

    query += ' ORDER BY date_intervention DESC'

    rows, pagination = paginate(query, tuple(params), page)
    interventions = [fmt_intervention(row_to_dict(r)) for r in rows]

    # Stats
    stats = {
        'total': conn.execute(
            'SELECT COUNT(*) FROM interventions WHERE client_id=? AND statut != ?', (cid, 'archivee')).fetchone()[0],
        'planifiee': conn.execute(
            "SELECT COUNT(*) FROM interventions WHERE client_id=? AND statut='planifiee'", (cid,)).fetchone()[0],
        'en_cours': conn.execute(
            "SELECT COUNT(*) FROM interventions WHERE client_id=? AND statut='en_cours'", (cid,)).fetchone()[0],
        'completee': conn.execute(
            "SELECT COUNT(*) FROM interventions WHERE client_id=? AND statut='completee'", (cid,)).fetchone()[0],
    }

    filtre_stat = ''
    if filtre_statut:
        filtre_stat = filtre_statut

    types_interventions = get_liste('types_interventions')
    conn.close()

    return render_template('interventions.html', interventions=interventions, client=client,
                          clients=get_clients(), client_actif_id=cid,
                          pagination=pagination, stats=stats,
                          filtre_type=filtre_type, filtre_statut=filtre_statut, filtre_stat=filtre_stat,
                          types_interventions=types_interventions)

@app.route('/intervention/nouveau', methods=['GET', 'POST'])
@login_required
def nouveau_intervention():
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_interventions'))

    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))

    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT id, nom_machine, type_appareil, adresse_ip FROM appareils WHERE client_id=? ORDER BY nom_machine',
        (cid,)).fetchall()]
    peripheriques = [row_to_dict(r) for r in conn.execute(
        'SELECT id, categorie, marque, modele FROM peripheriques WHERE client_id=? ORDER BY categorie, marque',
        (cid,)).fetchall()]
    contrats = [row_to_dict(r) for r in conn.execute(
        'SELECT id, titre FROM contrats WHERE client_id=? ORDER BY titre', (cid,)).fetchall()]
    types_interventions = get_liste('types_interventions')

    if request.method == 'POST':
        errs = validate_form([
            ('titre', 'str', True),
            ('type_intervention', 'str', True),
            ('date_intervention', 'date', True),
            ('description', 'str', True),
        ], request.form)

        if errs:
            for e in errs: flash(e, 'danger')
            return redirect(request.url)

        f = request.form
        user = get_auth_user()
        now = datetime.now().isoformat()

        conn = get_db()
        cur = conn.cursor()

        contrat_id = None
        try:
            contrat_id = int(f.get('contrat_id')) if f.get('contrat_id') else None
        except:
            pass

        cout_ht = None
        try:
            cout_ht = float(f.get('cout_ht')) if f.get('cout_ht') else None
        except:
            pass

        duree_minutes = 0
        try:
            duree_minutes = int(f.get('duree_minutes')) if f.get('duree_minutes') else 0
        except:
            pass

        cur.execute("""INSERT INTO interventions
            (client_id, titre, type_intervention, description, notes,
             date_intervention, heure_debut, heure_fin, duree_minutes,
             technicien_nom, technicien_email, statut, contrat_id, cout_ht, devise,
             date_creation, date_maj, auth_user_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, f.get('titre', ''), f.get('type_intervention', ''),
             f.get('description', ''), f.get('notes', ''),
             f.get('date_intervention', ''), f.get('heure_debut', ''), f.get('heure_fin', ''),
             duree_minutes, f.get('technicien_nom', ''), f.get('technicien_email', ''),
             f.get('statut', 'completee'), contrat_id, cout_ht, 'EUR',
             now, now, user['id']))

        intv_id = cur.lastrowid

        # Link appareils
        for app_id in request.form.getlist('appareils_lies'):
            try:
                conn.execute('INSERT INTO interventions_appareils (intervention_id, appareil_id) VALUES (?,?)',
                           (intv_id, int(app_id)))
            except:
                pass

        # Link peripheriques
        for per_id in request.form.getlist('peripheriques_lies'):
            try:
                conn.execute('INSERT INTO interventions_peripheriques (intervention_id, peripherique_id) VALUES (?,?)',
                           (intv_id, int(per_id)))
            except:
                pass

        log_history(conn, cid, 'intervention', intv_id, f.get('titre', '') or f'Intervention #{intv_id}', 'Création')
        conn.commit()
        conn.close()
        flash('Intervention créée', 'success')
        return redirect(url_for('detail_intervention', id=intv_id))

    conn.close()
    return render_template('form_intervention.html', intervention=None,
                          appareils=appareils, appareils_lies=[],
                          peripheriques=peripheriques, peripheriques_lies=[],
                          contrats=contrats, types_interventions=types_interventions,
                          client=client, clients=get_clients(), client_actif_id=cid)

@app.route('/intervention/<int:id>')
@login_required
def detail_intervention(id):
    cid = get_client_id()
    conn = get_db()

    intv = fmt_intervention(row_to_dict(
        conn.execute('SELECT * FROM interventions WHERE id=? AND client_id=?', (id, cid)).fetchone() or {}))

    if not intv:
        conn.close()
        return 'Intervention non trouvée', 404

    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})

    appareils_lies = [row_to_dict(r) for r in conn.execute(
        'SELECT a.* FROM appareils a JOIN interventions_appareils ia ON a.id=ia.appareil_id WHERE ia.intervention_id=?',
        (id,)).fetchall()]

    peripheriques_lies = [row_to_dict(r) for r in conn.execute(
        'SELECT p.* FROM peripheriques p JOIN interventions_peripheriques ip ON p.id=ip.peripherique_id WHERE ip.intervention_id=?',
        (id,)).fetchall()]

    docs = [row_to_dict(r) for r in conn.execute(
        'SELECT id, intervention_id, client_id, nom, description, type_doc, nom_fichier, taille, date_upload FROM documents_interventions WHERE intervention_id=? ORDER BY date_upload DESC', (id,)).fetchall()]
    for d in docs:
        d['taille_fmt'] = human_size(d.get('taille', 0))

    # Get contrat title if exists
    if intv.get('contrat_id'):
        contrat = conn.execute('SELECT titre FROM contrats WHERE id=?', (intv['contrat_id'],)).fetchone()
        if contrat:
            intv['contrat_titre'] = contrat[0]

    conn.close()
    return render_template('detail_intervention.html', intervention=intv,
                          appareils_lies=appareils_lies, peripheriques_lies=peripheriques_lies,
                          docs=docs, client=client, clients=get_clients(), client_actif_id=cid)

@app.route('/intervention/<int:id>/editer', methods=['GET', 'POST'])
@login_required
def editer_intervention(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_interventions'))

    cid = get_client_id()
    conn = get_db()

    intv = fmt_intervention(row_to_dict(
        conn.execute('SELECT * FROM interventions WHERE id=? AND client_id=?', (id, cid)).fetchone() or {}))

    if not intv:
        conn.close()
        return 'Intervention non trouvée', 404

    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT id, nom_machine, type_appareil, adresse_ip FROM appareils WHERE client_id=? ORDER BY nom_machine',
        (cid,)).fetchall()]
    peripheriques = [row_to_dict(r) for r in conn.execute(
        'SELECT id, categorie, marque, modele FROM peripheriques WHERE client_id=? ORDER BY categorie, marque',
        (cid,)).fetchall()]
    contrats = [row_to_dict(r) for r in conn.execute(
        'SELECT id, titre FROM contrats WHERE client_id=? ORDER BY titre', (cid,)).fetchall()]
    types_interventions = get_liste('types_interventions')

    if request.method == 'POST':
        errs = validate_form([
            ('titre', 'str', True),
            ('type_intervention', 'str', True),
            ('date_intervention', 'date', True),
            ('description', 'str', True),
        ], request.form)

        if errs:
            for e in errs: flash(e, 'danger')
            return redirect(request.url)

        f = request.form
        user = get_auth_user()
        now = datetime.now().isoformat()

        # Fetch old values for comparison
        _old = row_to_dict(conn.execute('SELECT * FROM interventions WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})

        contrat_id = None
        try:
            contrat_id = int(f.get('contrat_id')) if f.get('contrat_id') else None
        except:
            pass

        cout_ht = None
        try:
            cout_ht = float(f.get('cout_ht')) if f.get('cout_ht') else None
        except:
            pass

        duree_minutes = 0
        try:
            duree_minutes = int(f.get('duree_minutes')) if f.get('duree_minutes') else 0
        except:
            pass

        conn.execute("""UPDATE interventions SET
            titre=?, type_intervention=?, description=?, notes=?,
            date_intervention=?, heure_debut=?, heure_fin=?, duree_minutes=?,
            technicien_nom=?, technicien_email=?, statut=?, contrat_id=?, cout_ht=?, devise=?,
            date_maj=? WHERE id=? AND client_id=?""",
            (f.get('titre', ''), f.get('type_intervention', ''),
             f.get('description', ''), f.get('notes', ''),
             f.get('date_intervention', ''), f.get('heure_debut', ''), f.get('heure_fin', ''),
             duree_minutes, f.get('technicien_nom', ''), f.get('technicien_email', ''),
             f.get('statut', 'completee'), contrat_id, cout_ht, 'EUR',
             now, id, cid))

        # Reset liaisons
        conn.execute('DELETE FROM interventions_appareils WHERE intervention_id=?', (id,))
        conn.execute('DELETE FROM interventions_peripheriques WHERE intervention_id=?', (id,))

        for app_id in request.form.getlist('appareils_lies'):
            try:
                conn.execute('INSERT INTO interventions_appareils (intervention_id, appareil_id) VALUES (?,?)',
                           (id, int(app_id)))
            except:
                pass

        for per_id in request.form.getlist('peripheriques_lies'):
            try:
                conn.execute('INSERT INTO interventions_peripheriques (intervention_id, peripherique_id) VALUES (?,?)',
                           (id, int(per_id)))
            except:
                pass

        # Record change details
        _cols_i = _ENTITE_COLS['intervention']
        _details_i = _diff_json({k: str(_old.get(k,'') or '') for k in _cols_i},
                                 {k: str(f.get(k,'') or '') for k in _cols_i})
        log_history(conn, cid, 'intervention', id, f.get('titre', '') or f'Intervention #{id}', 'Modification', _details_i)
        conn.commit()
        conn.close()
        flash('Intervention mise à jour', 'success')
        return redirect(url_for('detail_intervention', id=id))

    appareils_lies = [r[0] for r in conn.execute(
        'SELECT appareil_id FROM interventions_appareils WHERE intervention_id=?', (id,)).fetchall()]
    peripheriques_lies = [r[0] for r in conn.execute(
        'SELECT peripherique_id FROM interventions_peripheriques WHERE intervention_id=?', (id,)).fetchall()]

    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    conn.close()

    return render_template('form_intervention.html', intervention=intv,
                          appareils=appareils, appareils_lies=appareils_lies,
                          peripheriques=peripheriques, peripheriques_lies=peripheriques_lies,
                          contrats=contrats, types_interventions=types_interventions,
                          client=client, clients=get_clients(), client_actif_id=cid)

@app.route('/intervention/<int:id>/supprimer', methods=['POST'])
@login_required
def supprimer_intervention(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_interventions'))

    cid = get_client_id()
    conn = get_db()

    # Soft delete: set status to archivee
    conn.execute('UPDATE interventions SET statut=? WHERE id=? AND client_id=?',
                ('archivee', id, cid))
    log_history(conn, cid, 'intervention', id, f'Intervention #{id}', 'Archivage')
    conn.commit()
    conn.close()

    flash('Intervention archivée', 'info')
    return redirect(url_for('liste_interventions'))

@app.route('/intervention/<int:id>/document/upload', methods=['POST'])
@login_required
def upload_doc_intervention(id):
    cid = get_client_id()
    if 'fichier' not in request.files:
        return redirect(url_for('detail_intervention', id=id))

    f = request.files['fichier']
    if not f.filename or not allowed_file(f.filename):
        flash('Type non autorisé', 'danger')
        return redirect(url_for('detail_intervention', id=id))

    unique = f"intv{id}_{int(time.time())}_{secure_filename(f.filename)}"
    save_path = os.path.join(UPLOAD_FOLDER, unique)
    f.save(save_path)

    nom = request.form.get('nom', '') or f.filename
    now = datetime.now().isoformat()

    conn = get_db()
    conn.execute(
        'INSERT INTO documents_interventions (intervention_id, client_id, nom, description, type_doc, nom_fichier, taille, date_upload) VALUES (?,?,?,?,?,?,?,?)',
        (id, cid, nom, request.form.get('description', ''), request.form.get('type_doc', ''),
         unique, os.path.getsize(save_path), now))

    # Log document upload
    user = get_auth_user()
    intv_title = conn.execute('SELECT titre FROM interventions WHERE id=? AND client_id=?', (id, cid)).fetchone()
    intv_name = intv_title[0] if intv_title else f'Intervention #{id}'
    log_history(conn, cid, 'intervention', id, intv_name, 'Ajout de document',
                _diff_json({}, {'nom': nom, 'fichier': unique, 'type_doc': request.form.get('type_doc', '')}))
    conn.commit()
    conn.close()

    flash('Document ajouté', 'success')
    return redirect(url_for('detail_intervention', id=id))

@app.route('/intervention/document/<int:id>/supprimer', methods=['POST'])
@login_required
def supprimer_doc_intervention(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_interventions'))

    cid = get_client_id()
    conn = get_db()
    doc = row_to_dict(conn.execute(
        'SELECT id, intervention_id, client_id, nom, nom_fichier FROM documents_interventions WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    intv_id = doc.get('intervention_id', 0)

    if doc:
        conn.execute('DELETE FROM documents_interventions WHERE id=?', (id,))

        # Log document deletion
        intv_title = conn.execute('SELECT titre FROM interventions WHERE id=? AND client_id=?', (intv_id, cid)).fetchone()
        intv_name = intv_title[0] if intv_title else f'Intervention #{intv_id}'
        log_history(conn, cid, 'intervention', intv_id, intv_name, 'Suppression de document',
                    _diff_json({'nom': doc.get('nom', ''), 'fichier': doc.get('nom_fichier', '')}, {}))

        conn.commit()
        try:
            os.remove(os.path.join(UPLOAD_FOLDER, doc['nom_fichier']))
        except:
            pass

    conn.close()
    return redirect(url_for('detail_intervention', id=intv_id))

@app.route('/intervention/document/<int:id>/apercu')
@login_required
def apercu_doc_intervention(id):
    cid = get_client_id()
    conn = get_db()
    doc = row_to_dict(conn.execute(
        'SELECT id, intervention_id, client_id, nom, nom_fichier FROM documents_interventions WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()

    if not doc:
        return 'Not found', 404

    return send_from_directory(UPLOAD_FOLDER, doc['nom_fichier'], as_attachment=False)

@app.route('/intervention/document/<int:id>/telecharger')
@login_required
def telecharger_doc_intervention(id):
    cid = get_client_id()
    conn = get_db()
    doc = row_to_dict(conn.execute(
        'SELECT id, intervention_id, client_id, nom, nom_fichier FROM documents_interventions WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()

    if not doc:
        return 'Not found', 404

    return send_from_directory(UPLOAD_FOLDER, doc['nom_fichier'], as_attachment=True, download_name=doc['nom'])


@app.route('/identifiant/<int:id>/popup')
def popup_identifiant(id):
    cid = get_client_id()
    conn = get_db()
    ident = row_to_dict(conn.execute(
        'SELECT * FROM identifiants WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})
    conn.close()
    if not ident:
        return 'Identifiant introuvable', 404
    # ✅ Déchiffrer le mot de passe
    if ident.get('mot_de_passe'):
        crypto = get_crypto_manager(os.path.join(_data_base, 'secret.key'))
        ident['mot_de_passe'] = crypto.decrypt(ident['mot_de_passe']) or ident['mot_de_passe']
    # Format dates
    today = date.today()
    if ident.get('date_expiration'):
        try:
            df = date.fromisoformat(ident['date_expiration'])
            ident['date_expiration_fmt'] = df.strftime('%d/%m/%Y')
            delta = (df - today).days
            ident['expire_depasse']  = delta < 0
            ident['expire_bientot']  = 0 <= delta <= 30
        except (ValueError, TypeError):
            ident['date_expiration_fmt'] = ident['date_expiration']
    return render_template('popup_identifiant.html', ident=ident)


# --- MAINTENANCE ----------------------------------------------------------

@app.route('/maintenances')
@login_required
def liste_maintenances():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))

    page = request.args.get('page', 1, type=int)
    filtre_type = request.args.get('type_maintenance', '')
    filtre_statut = request.args.get('statut', '')
    filtre_appareil = request.args.get('appareil_id', '')
    filtre_date_debut = request.args.get('date_debut', '')
    filtre_date_fin = request.args.get('date_fin', '')
    filtre_responsable = request.args.get('responsable', '')

    # Tri
    sort_by = request.args.get('sort_by', 'date_planifiee')
    sort_order = request.args.get('sort_order', 'desc').lower()

    # Valider la colonne de tri (whitelist)
    allowed_sort_cols = ['date_planifiee', 'type_maintenance', 'responsable', 'statut', 'date_realisee']
    if sort_by not in allowed_sort_cols:
        sort_by = 'date_planifiee'
    if sort_order not in ['asc', 'desc']:
        sort_order = 'desc'

    clients_sel = request.args.getlist('clients_selection')
    if not clients_sel:
        clients_sel = None

    conn = get_db()

    # Build query with JOINs to get appareil/peripherique/contrat names
    query = '''SELECT m.*,
               a.nom_machine as appareil_nom,
               p.categorie as peripherique_categorie, p.marque as peripherique_marque, p.modele as peripherique_modele,
               c.type_contrat as contrat_type, c.fournisseur as contrat_fournisseur
        FROM maintenances m
        LEFT JOIN appareils a ON m.appareil_id = a.id
        LEFT JOIN peripheriques p ON m.peripherique_id = p.id
        LEFT JOIN contrats c ON m.contrat_id = c.id
        WHERE m.client_id IN ({})'''.format(
        ','.join(['?'] * len([c['id'] for c in get_clients_for_filter(clients_sel)])))

    params = [c['id'] for c in get_clients_for_filter(clients_sel)]

    if filtre_type:
        query += ' AND type_maintenance=?'
        params.append(filtre_type)
    if filtre_statut:
        query += ' AND statut=?'
        params.append(filtre_statut)
    if filtre_appareil:
        query += ' AND appareil_id=?'
        params.append(int(filtre_appareil))
    if filtre_date_debut:
        query += ' AND date_planifiee>=?'
        params.append(filtre_date_debut)
    if filtre_date_fin:
        query += ' AND date_planifiee<=?'
        params.append(filtre_date_fin)
    if filtre_responsable:
        query += ' AND responsable LIKE ?'
        params.append(f'%{filtre_responsable}%')

    query += f' ORDER BY {sort_by} {sort_order.upper()}'

    rows, pagination = paginate(query, tuple(params), page)
    maintenances = _format_maintenance_for_list(rows)

    # Stats
    types_maintenance = get_liste('types_maintenance')
    statuts_maintenance = get_liste('statuts_maintenance')

    # Get appareils for filter
    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT id, nom_machine FROM appareils WHERE client_id=? ORDER BY nom_machine',
        (cid,)).fetchall()]

    clients = get_clients()
    conn.close()

    return render_template('liste_maintenances.html',
                          maintenances=maintenances, client_actif_id=cid,
                          pagination=pagination, clients=clients,
                          types_maintenance=types_maintenance,
                          statuts_maintenance=statuts_maintenance,
                          appareils=appareils,
                          filtre_type=filtre_type, filtre_statut=filtre_statut,
                          filtre_appareil=filtre_appareil, filtre_date_debut=filtre_date_debut,
                          filtre_date_fin=filtre_date_fin, filtre_responsable=filtre_responsable,
                          sort_by=sort_by, sort_order=sort_order)


@app.route('/maintenance/nouveau', methods=['GET', 'POST'])
@login_required
def nouveau_maintenance():
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_maintenances'))

    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))

    user = get_auth_user()
    conn = get_db()

    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT id, nom_machine FROM appareils WHERE client_id=? ORDER BY nom_machine',
        (cid,)).fetchall()]
    peripheriques = [row_to_dict(r) for r in conn.execute(
        'SELECT id, categorie, marque, modele FROM peripheriques WHERE client_id=? ORDER BY categorie',
        (cid,)).fetchall()]
    contrats = [row_to_dict(r) for r in conn.execute(
        'SELECT id, type_contrat, fournisseur, date_debut, date_fin FROM contrats WHERE client_id=? ORDER BY date_debut DESC',
        (cid,)).fetchall()]
    types_maintenance = get_liste('types_maintenance')

    if request.method == 'POST':
        errs = validate_form([
            ('type_maintenance', 'str', True),
            ('date_planifiee', 'date', True),
        ], request.form)

        if errs:
            for e in errs: flash(e, 'danger')
            conn.close()
            return redirect(request.url)

        f = request.form
        now = datetime.now().isoformat()

        params = _extract_maintenance(cid, f, user['id'])

        cur = conn.cursor()
        try:
            cur.execute(
                '''INSERT INTO maintenances
                (client_id, appareil_id, peripherique_id, contrat_id, type_maintenance, description,
                 date_planifiee, date_realisee, heure_debut, heure_fin, responsable, notes,
                 statut, recurrence, date_fin_recurrence, parent_id, created_by, updated_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                params)
            maint_id = cur.lastrowid
            conn.commit()

            # Générer les occurrences si récurrence définie
            if f.get('recurrence'):
                # Lookahead: générer 4 semaines à l'avance
                lookahead_date = (datetime.strptime(f.get('date_planifiee'), '%Y-%m-%d') + timedelta(days=28)).strftime('%Y-%m-%d')
                _generate_maintenance_series(conn, maint_id, cid, f.get('date_planifiee'),
                                            f.get('recurrence'), f.get('date_fin_recurrence'),
                                            user['id'], f, lookahead_date=lookahead_date)

            log_history(conn, cid, 'maintenance', maint_id, f.get('description','Maintenance'),
                       'Création', {'type': f.get('type_maintenance'), 'date': f.get('date_planifiee')})
            conn.commit()
            if f.get('recurrence'):
                flash('Maintenance créée avec occurrences des 4 prochaines semaines', 'success')
            else:
                flash('Maintenance programmée créée', 'success')
        except Exception as e:
            conn.rollback()
            logger.exception('Erreur création maintenance')
            flash(f'Erreur : {str(e)}', 'danger')
        finally:
            conn.close()

        return redirect(url_for('liste_maintenances'))

    techniciens = [row_to_dict(r) for r in conn.execute(
        "SELECT id, nom, prenom FROM auth_users WHERE role != 'admin' AND actif=1 ORDER BY nom, prenom"
        ).fetchall()]
    conn.close()
    return render_template('form_maintenance.html', appareils=appareils, peripheriques=peripheriques,
                          contrats=contrats, types_maintenance=types_maintenance, techniciens=techniciens, action='Créer')


@app.route('/maintenance/<int:id>/editer', methods=['GET', 'POST'])
@login_required
def editer_maintenance(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_maintenances'))

    cid = get_client_id()
    user = get_auth_user()
    conn = get_db()

    maint = row_to_dict(conn.execute(
        'SELECT * FROM maintenances WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})

    if not maint:
        conn.close()
        flash('Maintenance introuvable', 'danger')
        return redirect(url_for('liste_maintenances'))

    appareils = [row_to_dict(r) for r in conn.execute(
        'SELECT id, nom_machine FROM appareils WHERE client_id=? ORDER BY nom_machine',
        (cid,)).fetchall()]
    peripheriques = [row_to_dict(r) for r in conn.execute(
        'SELECT id, categorie, marque, modele FROM peripheriques WHERE client_id=? ORDER BY categorie',
        (cid,)).fetchall()]
    contrats = [row_to_dict(r) for r in conn.execute(
        'SELECT id, type_contrat, fournisseur, date_debut, date_fin FROM contrats WHERE client_id=? ORDER BY date_debut DESC',
        (cid,)).fetchall()]
    types_maintenance = get_liste('types_maintenance')

    if request.method == 'POST':
        errs = validate_form([
            ('type_maintenance', 'str', True),
            ('date_planifiee', 'date', True),
        ], request.form)

        if errs:
            for e in errs: flash(e, 'danger')
            conn.close()
            return redirect(request.url)

        f = request.form
        now = datetime.now().isoformat()

        try:
            appareil_id = int(f.get('appareil_id')) if f.get('appareil_id') else None
        except:
            appareil_id = None
        try:
            peripherique_id = int(f.get('peripherique_id')) if f.get('peripherique_id') else None
        except:
            peripherique_id = None
        try:
            contrat_id = int(f.get('contrat_id')) if f.get('contrat_id') else None
        except:
            contrat_id = None

        cur = conn.cursor()
        try:
            cur.execute(
                '''UPDATE maintenances
                SET type_maintenance=?, description=?, date_planifiee=?, date_realisee=?,
                    heure_debut=?, heure_fin=?, responsable=?, notes=?,
                    statut=?, recurrence=?, date_fin_recurrence=?,
                    appareil_id=?, peripherique_id=?, contrat_id=?, updated_by=?, date_maj=?
                WHERE id=? AND client_id=?''',
                (f.get('type_maintenance'), f.get('description'), f.get('date_planifiee'),
                 f.get('date_realisee'), f.get('heure_debut'), f.get('heure_fin'),
                 f.get('responsable'), f.get('notes'), f.get('statut'),
                 f.get('recurrence'), f.get('date_fin_recurrence'),
                 appareil_id, peripherique_id, contrat_id, user['id'], now,
                 id, cid))
            conn.commit()
            log_history(conn, cid, 'maintenance', id, f.get('description','Maintenance'),
                       'Modification', {'type': f.get('type_maintenance')})
            conn.commit()
            flash('Maintenance mise à jour', 'success')
        except Exception as e:
            conn.rollback()
            logger.exception('Erreur édition maintenance')
            flash(f'Erreur : {str(e)}', 'danger')
        finally:
            conn.close()

        return redirect(url_for('liste_maintenances'))

    techniciens = [row_to_dict(r) for r in conn.execute(
        "SELECT id, nom, prenom FROM auth_users WHERE role != 'admin' AND actif=1 ORDER BY nom, prenom"
        ).fetchall()]
    conn.close()
    return render_template('form_maintenance.html', maint=maint, appareils=appareils,
                          peripheriques=peripheriques, contrats=contrats,
                          types_maintenance=types_maintenance,
                          techniciens=techniciens, action='Éditer')


@app.route('/maintenance/<int:id>/confirmer', methods=['POST'])
@login_required
def confirmer_maintenance(id):
    if not can_write():
        return jsonify({'error': 'Accès en lecture seule — modification non autorisée'}), 403

    cid = get_client_id()
    user = get_auth_user()
    conn = get_db()

    maint = row_to_dict(conn.execute(
        'SELECT * FROM maintenances WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})

    if not maint:
        conn.close()
        return jsonify({'error': 'Not found'}), 404

    now = datetime.now().isoformat()
    today = date.today().isoformat()

    try:
        cur = conn.cursor()
        cur.execute(
            'UPDATE maintenances SET statut=?, date_realisee=?, updated_by=?, date_maj=? WHERE id=? AND client_id=?',
            ('realisee', today, user['id'], now, id, cid))
        conn.commit()
        log_history(conn, cid, 'maintenance', id, maint.get('description','Maintenance'),
                   'Confirmation', {'ancien_statut': maint.get('statut'), 'nouveau_statut': 'realisee'})
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        logger.exception('Erreur confirmation maintenance')
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/maintenance/<int:id>/supprimer', methods=['POST'])
@login_required
def supprimer_maintenance(id):
    if not can_write():
        flash('Accès en lecture seule — modification non autorisée', 'danger')
        return redirect(url_for('liste_maintenances'))

    cid = get_client_id()
    user = get_auth_user()
    conn = get_db()

    maint = row_to_dict(conn.execute(
        'SELECT * FROM maintenances WHERE id=? AND client_id=?', (id, cid)).fetchone() or {})

    if not maint:
        conn.close()
        flash('Maintenance introuvable', 'danger')
        return redirect(url_for('liste_maintenances'))

    now = datetime.now().isoformat()

    try:
        cur = conn.cursor()
        cur.execute(
            'UPDATE maintenances SET statut=?, updated_by=?, date_maj=? WHERE id=? AND client_id=?',
            ('annulee', user['id'], now, id, cid))
        conn.commit()
        log_history(conn, cid, 'maintenance', id, maint.get('description','Maintenance'),
                   'Suppression', {})
        conn.commit()
        flash('Maintenance annulée', 'success')
    except Exception as e:
        conn.rollback()
        logger.exception('Erreur suppression maintenance')
        flash(f'Erreur : {str(e)}', 'danger')
    finally:
        conn.close()

    return redirect(url_for('liste_maintenances'))


@app.route('/rapport/maintenances')
@login_required
def rapport_maintenances():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))

    types_maintenance = get_liste('types_maintenance')
    clients = get_clients()

    # Filtres
    date_debut = request.args.get('date_debut', '')
    date_fin = request.args.get('date_fin', '')
    type_maint = request.args.get('type_maintenance', '')
    statut = request.args.get('statut', '')
    clients_filter = request.args.get('clients_filter', 'current')

    # Déterminer clients sélectionnés
    if clients_filter == 'all':
        client_ids = [c['id'] for c in clients]
    else:
        client_ids = [cid]

    # Requête SQL filtrée
    conn = get_db()
    query = '''SELECT m.*,
                      a.nom_machine AS appareil_nom,
                      p.marque AS peripherique_marque, p.modele AS peripherique_modele, p.categorie AS peripherique_categorie,
                      c.type_contrat AS contrat_type, c.fournisseur AS contrat_fournisseur
               FROM maintenances m
               LEFT JOIN appareils a ON m.appareil_id = a.id
               LEFT JOIN peripheriques p ON m.peripherique_id = p.id
               LEFT JOIN contrats c ON m.contrat_id = c.id
               WHERE m.client_id IN ({})'''.format(','.join(['?'] * len(client_ids)))
    params = client_ids[:]

    if date_debut:
        query += ' AND m.date_planifiee >= ?'
        params.append(date_debut)
    if date_fin:
        query += ' AND m.date_planifiee <= ?'
        params.append(date_fin)
    if type_maint:
        query += ' AND m.type_maintenance = ?'
        params.append(type_maint)
    if statut:
        query += ' AND m.statut = ?'
        params.append(statut)

    query += ' ORDER BY m.date_planifiee DESC'

    rows = conn.execute(query, params).fetchall()
    maintenances = [row_to_dict(r) for r in rows]
    maintenances = _format_maintenance_for_list(maintenances)

    # Statistiques
    total = len(maintenances)
    realisees = sum(1 for m in maintenances if m['statut'] == 'realisee')
    attente = sum(1 for m in maintenances if m['statut'] == 'programmee')
    reportees = sum(1 for m in maintenances if m['statut'] == 'reportee')

    conn.close()

    return render_template('rapport_maintenance.html',
                          clients=clients,
                          types_maintenance=types_maintenance,
                          maintenances=maintenances,
                          stats={'total': total, 'realisees': realisees, 'attente': attente, 'reportees': reportees})


@app.route('/rapport/maintenances/pdf')
@login_required
def rapport_maintenances_pdf():
    """Génère un PDF du rapport de maintenances avec mise en page professionnelle"""
    if not REPORTLAB_AVAILABLE:
        flash('La bibliothèque reportlab n\'est pas installée. Installez-la avec: pip install reportlab', 'danger')
        return redirect(request.referrer or url_for('rapport_maintenances'))

    cid = get_client_id()
    if not cid:
        return redirect(url_for('nouveau_client'))

    # Récupérer les mêmes filtres que le rapport HTML
    date_debut = request.args.get('date_debut', '')
    date_fin = request.args.get('date_fin', '')
    type_maint = request.args.get('type_maintenance', '')
    statut = request.args.get('statut', '')
    clients_filter = request.args.get('clients_filter', 'current')

    clients = get_clients()
    if clients_filter == 'all':
        client_ids = [c['id'] for c in clients]
    else:
        client_ids = [cid]

    # Construire la requête SQL
    conn = get_db()
    query = '''SELECT m.*,
                      a.nom_machine AS appareil_nom,
                      p.marque AS peripherique_marque, p.modele AS peripherique_modele, p.categorie AS peripherique_categorie,
                      c.type_contrat AS contrat_type, c.fournisseur AS contrat_fournisseur
               FROM maintenances m
               LEFT JOIN appareils a ON m.appareil_id = a.id
               LEFT JOIN peripheriques p ON m.peripherique_id = p.id
               LEFT JOIN contrats c ON m.contrat_id = c.id
               WHERE m.client_id IN ({})'''.format(','.join(['?'] * len(client_ids)))
    params = client_ids[:]

    if date_debut:
        query += ' AND m.date_planifiee >= ?'
        params.append(date_debut)
    if date_fin:
        query += ' AND m.date_planifiee <= ?'
        params.append(date_fin)
    if type_maint:
        query += ' AND m.type_maintenance = ?'
        params.append(type_maint)
    if statut:
        query += ' AND m.statut = ?'
        params.append(statut)

    query += ' ORDER BY m.date_planifiee DESC'

    rows = conn.execute(query, params).fetchall()
    maintenances = [row_to_dict(r) for r in rows]
    maintenances = _format_maintenance_for_list(maintenances)

    # Calculer statistiques
    total = len(maintenances)
    realisees = sum(1 for m in maintenances if m['statut'] == 'realisee')
    attente = sum(1 for m in maintenances if m['statut'] == 'programmee')
    reportees = sum(1 for m in maintenances if m['statut'] == 'reportee')

    conn.close()

    try:
        # Créer le PDF avec reportlab
        pdf_buffer = BytesIO()
        doc = SimpleDocTemplate(pdf_buffer, pagesize=A4, rightMargin=15*mm, leftMargin=15*mm,
                                topMargin=15*mm, bottomMargin=15*mm)
        story = []

        # Styles
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=colors.HexColor('#0a0d12'),
            spaceAfter=6,
            fontName='Helvetica-Bold'
        )
        subtitle_style = ParagraphStyle(
            'CustomSubtitle',
            parent=styles['Normal'],
            fontSize=12,
            textColor=colors.HexColor('#00c9ff'),
            spaceAfter=3,
            fontName='Helvetica-Bold'
        )
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=14,
            textColor=colors.HexColor('#0a0d12'),
            spaceAfter=10,
            fontName='Helvetica-Bold'
        )

        # Titre
        story.append(Paragraph('📊 Rapport Maintenances', title_style))
        story.append(Paragraph('Synthèse des opérations de maintenance', subtitle_style))
        story.append(Paragraph(f'Généré le {datetime.now().strftime("%d/%m/%Y à %H:%M")}', styles['Normal']))
        story.append(Spacer(1, 15))

        # Statistiques
        stats_data = [
            ['Total Opérations', f'{total}'],
            ['Réalisées', f'{realisees}'],
            ['En Attente', f'{attente}'],
            ['Reportées', f'{reportees}']
        ]
        stats_table = Table(stats_data, colWidths=[3*inch, 1*inch])
        stats_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f5f5f5')),
            ('BACKGROUND', (1, 0), (1, -1), colors.HexColor('#e8f5ff')),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor('#333333')),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('ALIGN', (1, 0), (1, -1), 'CENTER'),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#ddd')),
        ]))
        story.append(stats_table)
        story.append(Spacer(1, 15))

        # Tableau détail
        story.append(Paragraph('Détail des Opérations', heading_style))

        if maintenances:
            # Préparer les données du tableau avec des Paragraphs pour meilleur enroulement
            table_data = [
                ['Date Planifiée', 'Type', 'Description', 'Responsable', 'Statut', 'Réalisée le']
            ]

            # Style pour le contenu des cellules
            cell_style = ParagraphStyle(
                'CellStyle',
                parent=styles['Normal'],
                fontSize=8,
                leading=10,
                alignment=0  # LEFT
            )

            for m in maintenances[:50]:  # Limiter à 50 lignes pour la lisibilité
                # Tronquer la description à 80 caractères max pour éviter débordement
                description = m.get('description', '')[:80] or '—'

                table_data.append([
                    Paragraph(m.get('date_planifiee_fmt', ''), cell_style),
                    Paragraph(m.get('type_label', ''), cell_style),
                    Paragraph(description, cell_style),
                    Paragraph(m.get('responsable', '') or '—', cell_style),
                    Paragraph(m.get('statut_label', ''), cell_style),
                    Paragraph(m.get('date_realisee_fmt', '') or '—', cell_style)
                ])

            # Créer le tableau avec meilleures largeurs et hauteurs
            table = Table(table_data, colWidths=[1.0*inch, 0.8*inch, 2.2*inch, 0.9*inch, 0.8*inch, 1.0*inch])
            table.setStyle(TableStyle([
                # En-tête
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0a0d12')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, 0), 8),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                ('LEFTPADDING', (0, 0), (-1, -1), 5),
                ('RIGHTPADDING', (0, 0), (-1, -1), 5),

                # Contenu
                ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                ('FONTSIZE', (0, 1), (-1, -1), 8),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f9f9f9')]),
                ('ALIGN', (0, 1), (-1, -1), 'LEFT'),
                ('VALIGN', (0, 1), (-1, -1), 'TOP'),
                ('TOPPADDING', (0, 1), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 1), (-1, -1), 6),

                # Grille et bordures
                ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#ddd')),
                ('LINEBELOW', (0, 0), (-1, 0), 2, colors.HexColor('#0a0d12')),

                # Hauteur minimale des lignes
                ('ROWHEIGHT', (0, 0), (-1, -1), None),  # Auto-hauteur
                ('ROWHEIGHT', (0, 1), (-1, -1), 35),  # Minimum 35 points pour le contenu
            ]))
            story.append(table)
        else:
            story.append(Paragraph('Aucune maintenance trouvée pour les critères spécifiés.', styles['Italic']))

        # Pied de page
        story.append(Spacer(1, 20))
        story.append(Paragraph('ParcInfo — Rapport de Maintenance — Document généré automatiquement',
                              styles['Normal']))

        # Générer le PDF
        doc.build(story)
        pdf_buffer.seek(0)

        # Créer la réponse
        response = make_response(pdf_buffer.getvalue())
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename="rapport-maintenance-{datetime.now().strftime("%Y%m%d-%H%M%S")}.pdf"'
        return response

    except Exception as e:
        logger.exception('Erreur génération PDF rapport')
        flash(f'Erreur lors de la génération du PDF : {str(e)}', 'danger')
        return redirect(request.referrer or url_for('rapport_maintenances',
                                date_debut=date_debut,
                                date_fin=date_fin,
                                type_maintenance=type_maint,
                                statut=statut,
                                clients_filter=clients_filter))


@app.route('/maintenance/historique')
@login_required
def historique_maintenance():
    cid = get_client_id()
    if not cid:
        return redirect(url_for('nouveau_client'))

    conn = get_db()

    # Statistiques par type de maintenance
    types_stats = conn.execute('''
        SELECT type_maintenance, COUNT(*) as count,
               SUM(CASE WHEN statut='realisee' THEN 1 ELSE 0 END) as realisees
        FROM maintenances WHERE client_id=? GROUP BY type_maintenance
    ''', (cid,)).fetchall()

    # Statistiques par mois (12 derniers mois)
    months_stats = conn.execute('''
        SELECT strftime('%Y-%m', date_planifiee) as mois, COUNT(*) as count
        FROM maintenances WHERE client_id=? AND date_planifiee >= date('now', '-12 months')
        GROUP BY strftime('%Y-%m', date_planifiee) ORDER BY mois
    ''', (cid,)).fetchall()

    # Statistiques par statut
    status_stats = conn.execute('''
        SELECT statut, COUNT(*) as count FROM maintenances WHERE client_id=?
        GROUP BY statut
    ''', (cid,)).fetchall()

    # Total maintenances
    total = conn.execute('SELECT COUNT(*) FROM maintenances WHERE client_id=?', (cid,)).fetchone()[0]
    realisees = conn.execute('SELECT COUNT(*) FROM maintenances WHERE client_id=? AND statut="realisee"', (cid,)).fetchone()[0]
    taux_realisation = int((realisees / total * 100) if total > 0 else 0)

    conn.close()

    # Formater données pour Chart.js
    types_labels = [row[0] for row in types_stats]
    types_data = [row[1] for row in types_stats]

    months_labels = [row[0] for row in months_stats]
    months_data = [row[1] for row in months_stats]

    status_map = {'programmee': 'Programmée', 'realisee': 'Réalisée', 'reportee': 'Reportée', 'annulee': 'Annulée'}
    status_labels = [status_map.get(row[0], row[0]) for row in status_stats]
    status_data = [row[1] for row in status_stats]

    import json
    return render_template('historique_maintenance.html',
        types_labels=json.dumps(types_labels),
        types_data=json.dumps(types_data),
        months_labels=json.dumps(months_labels),
        months_data=json.dumps(months_data),
        status_labels=json.dumps(status_labels),
        status_data=json.dumps(status_data),
        total=total,
        realisees=realisees,
        taux_realisation=taux_realisation)


@app.route('/maintenances/export.csv')
@login_required
def export_maintenances_csv():
    cid = get_client_id()
    filtre_date_debut = request.args.get('date_debut', '')
    filtre_date_fin = request.args.get('date_fin', '')
    filtre_type = request.args.get('type_maintenance', '')
    filtre_statut = request.args.get('statut', '')
    clients_filter = request.args.get('clients_filter', 'current')

    # Déterminer clients sélectionnés
    if clients_filter == 'all':
        client_ids = [c['id'] for c in get_clients()]
    else:
        client_ids = [cid]

    conn = get_db()

    query = '''SELECT m.*, a.nom_machine AS appareil_nom, p.marque AS peripherique_marque, p.modele AS peripherique_modele
               FROM maintenances m
               LEFT JOIN appareils a ON m.appareil_id = a.id
               LEFT JOIN peripheriques p ON m.peripherique_id = p.id
               WHERE m.client_id IN ({})'''.format(','.join(['?'] * len(client_ids)))
    params = client_ids[:]

    if filtre_date_debut:
        query += ' AND m.date_planifiee >= ?'
        params.append(filtre_date_debut)
    if filtre_date_fin:
        query += ' AND m.date_planifiee <= ?'
        params.append(filtre_date_fin)
    if filtre_type:
        query += ' AND m.type_maintenance = ?'
        params.append(filtre_type)
    if filtre_statut:
        query += ' AND m.statut = ?'
        params.append(filtre_statut)

    query += ' ORDER BY m.date_planifiee DESC'

    rows = conn.execute(query, params).fetchall()
    maintenances = [row_to_dict(r) for r in rows]
    conn.close()

    # Build CSV
    import io
    output = io.StringIO()
    output.write('\ufeff')  # BOM UTF-8
    output.write('Date planifiée;Appareil;Périphérique;Type;Description;Responsable;Statut;Date réalisée;Notes\n')

    for m in maintenances:
        app_name = ''
        if m.get('appareil_id'):
            c2 = get_db()
            a = c2.execute('SELECT nom_machine FROM appareils WHERE id=?', (m['appareil_id'],)).fetchone()
            if a: app_name = a[0]
            c2.close()

        periph_name = ''
        if m.get('peripherique_id'):
            c2 = get_db()
            p = c2.execute('SELECT categorie, marque FROM peripheriques WHERE id=?', (m['peripherique_id'],)).fetchone()
            if p: periph_name = f"{p[0]} {p[1]}"
            c2.close()

        output.write(f"{m.get('date_planifiee','')};{app_name};{periph_name};{m.get('type_maintenance','')};")
        output.write(f"{m.get('description','')};{m.get('responsable','')};{m.get('statut','')};")
        output.write(f"{m.get('date_realisee','')};{m.get('notes','')}\n")

    response = app.response_class(output.getvalue(), mimetype='text/csv; charset=utf-8')
    response.headers['Content-Disposition'] = f'attachment; filename=maintenances_{cid}_{date.today().isoformat()}.csv'
    return response


# --- LISTES PERSONNALISABLES -------------------------------------------------

def _liste_est_initialisee(conn, nom: str) -> bool:
    """Indique si une liste a déjà été persistée en DB.
    Lit d'abord le flag dans le cache config (lecture seule, pas d'écriture sur conn),
    puis fait un COUNT en fallback pour les DBs existantes sans flag.
    IMPORTANT : n'appelle jamais cfg_set ici — conn peut avoir un verrou d'écriture actif."""
    if cfg_get(f'_list_init_{nom}', '0') == '1':
        return True
    # Fallback legacy : lignes existantes = liste déjà initialisée
    # Le flag sera posé par l'appelant après conn.commit() pour éviter le deadlock SQLite
    nb = conn.execute('SELECT COUNT(*) FROM config_listes WHERE nom_liste=?', (nom,)).fetchone()[0]
    return nb > 0

def _initialiser_liste(conn, nom: str, exclure: str = None):
    """Écrit tous les defaults d'une liste sur `conn` (sauf `exclure`).
    N'appelle PAS cfg_set — l'appelant doit le faire APRÈS conn.commit()
    pour éviter le deadlock SQLite (deux connexions en écriture simultanée)."""
    for i, v in enumerate(LISTE_DEFAULTS.get(nom, [])):
        if v == exclure:
            continue
        try:
            conn.execute('INSERT OR IGNORE INTO config_listes (nom_liste,valeur,ordre) VALUES (?,?,?)', (nom, v, i))
        except Exception:
            pass

@app.route('/api/listes/<nom>', methods=['GET'])
def api_liste_get(nom):
    if nom not in LISTE_DEFAULTS:
        return jsonify({'error': 'Liste inconnue'}), 404
    return jsonify({'nom': nom, 'valeurs': get_liste(nom), 'defaults': LISTE_DEFAULTS[nom]})

@app.route('/api/listes/<nom>/ajouter', methods=['POST'])
def api_liste_ajouter(nom):
    if nom not in LISTE_DEFAULTS:
        return jsonify({'error': 'Liste inconnue'}), 404
    valeur = (request.json or {}).get('valeur', '').strip()
    if not valeur:
        return jsonify({'error': 'Valeur vide'}), 400

    # ── Opération principale sur la DB active ────────────────────────────────
    conn = get_db()
    deja_init = _liste_est_initialisee(conn, nom)
    if not deja_init:
        # Première utilisation : persister les defaults pour préserver l'ordre
        _initialiser_liste(conn, nom)
    ordre = conn.execute('SELECT COALESCE(MAX(ordre),0)+1 FROM config_listes WHERE nom_liste=?', (nom,)).fetchone()[0]
    try:
        conn.execute('INSERT INTO config_listes (nom_liste,valeur,ordre) VALUES (?,?,?)', (nom, valeur, ordre))
        conn.commit()
    except Exception:
        conn.close()
        return jsonify({'error': 'Valeur déjà existante'}), 409
    conn.close()

    # ── Propagation croisée (évite les écarts entre local et Turso) ─────────
    try:
        _t_url   = cfg_get('turso_url',   '').strip()
        _t_token = cfg_get('turso_token', '').strip()
        _db_type = cfg_get('db_type', 'local')
        if _t_url and _t_token:
            if _db_type in ('local', 'sync'):
                from database import TursoConnection as _TC
                _TC(_t_url, _t_token).execute(
                    'INSERT OR IGNORE INTO config_listes (nom_liste,valeur,ordre) VALUES (?,?,?)',
                    (nom, valeur, ordre))
            elif _db_type == 'turso':
                from database import get_local_db as _get_local
                _lconn = _get_local()
                _lconn.execute('INSERT OR IGNORE INTO config_listes (nom_liste,valeur,ordre) VALUES (?,?,?)',
                               (nom, valeur, ordre))
                _lconn.commit(); _lconn.close()
    except Exception:
        logger.warning('Propagation croisée ajout config_listes échouée', exc_info=True)

    # ── Flag posé APRÈS commit : conn fermée, aucun verrou actif → pas de deadlock ──
    if not deja_init:
        cfg_set(f'_list_init_{nom}', '1')
    return jsonify({'ok': True, 'valeurs': get_liste(nom)})

@app.route('/api/listes/<nom>/supprimer', methods=['POST'])
def api_liste_supprimer(nom):
    if nom not in LISTE_DEFAULTS:
        return jsonify({'error': 'Liste inconnue'}), 404
    valeur = (request.json or {}).get('valeur', '').strip()
    logger.info('[liste_supprimer] liste=%s valeur=%r db_type=%s', nom, valeur, cfg_get('db_type'))

    # ── 1. Opération principale sur la DB active (locale ou Turso selon config) ──
    conn = get_db()
    deja_init = _liste_est_initialisee(conn, nom)
    logger.info('[liste_supprimer] deja_init=%s', deja_init)
    if not deja_init:
        # Première suppression : persister tous les defaults SAUF la valeur supprimée
        _initialiser_liste(conn, nom, exclure=valeur)
        logger.info('[liste_supprimer] liste initialisée sans %r', valeur)
    else:
        # Liste déjà persistée : suppression directe
        conn.execute('DELETE FROM config_listes WHERE nom_liste=? AND valeur=?', (nom, valeur))
        logger.info('[liste_supprimer] DELETE exécuté sur DB principale')
    conn.commit()
    conn.close()

    # ── 2. Propagation croisée pour éviter que la sync bidirectionnelle réinjecte la valeur ──
    #    Si mode local/sync → aussi supprimer sur Turso
    #    Si mode turso → aussi supprimer sur SQLite local
    try:
        _t_url   = cfg_get('turso_url',   '').strip()
        _t_token = cfg_get('turso_token', '').strip()
        _db_type = cfg_get('db_type', 'local')
        if _t_url and _t_token:
            if _db_type in ('local', 'sync'):
                # DB principale = local → propager sur Turso
                from database import TursoConnection as _TC
                _turso = _TC(_t_url, _t_token)
                _turso.execute('DELETE FROM config_listes WHERE nom_liste=? AND valeur=?', (nom, valeur))
                logger.info('[liste_supprimer] DELETE propagé vers Turso pour %r', valeur)
            elif _db_type == 'turso':
                # DB principale = Turso → propager sur SQLite local
                from database import get_local_db as _get_local
                _lconn = _get_local()
                _lconn.execute('DELETE FROM config_listes WHERE nom_liste=? AND valeur=?', (nom, valeur))
                _lconn.commit(); _lconn.close()
                logger.info('[liste_supprimer] DELETE propagé vers SQLite local pour %r', valeur)
    except Exception:
        logger.warning('Propagation croisée config_listes échouée', exc_info=True)

    # ── 3. Flag posé APRÈS commit+close → aucun conflit de verrou SQLite ─────
    if not deja_init:
        cfg_set(f'_list_init_{nom}', '1')
    valeurs_apres = get_liste(nom)
    logger.info('[liste_supprimer] liste après suppression (%d éléments)', len(valeurs_apres))
    return jsonify({'ok': True, 'valeurs': valeurs_apres})

@app.route('/api/listes/<nom>/reset', methods=['POST'])
def api_liste_reset(nom):
    if nom not in LISTE_DEFAULTS:
        return jsonify({'error': 'Liste inconnue'}), 404
    # Supprimer des deux côtés pour éviter la réinjection par la sync
    conn = get_db()
    conn.execute('DELETE FROM config_listes WHERE nom_liste=?', (nom,))
    conn.commit(); conn.close()
    try:
        _t_url   = cfg_get('turso_url',   '').strip()
        _t_token = cfg_get('turso_token', '').strip()
        _db_type = cfg_get('db_type', 'local')
        if _t_url and _t_token:
            if _db_type in ('local', 'sync'):
                from database import TursoConnection as _TC
                _TC(_t_url, _t_token).execute('DELETE FROM config_listes WHERE nom_liste=?', (nom,))
            elif _db_type == 'turso':
                from database import get_local_db as _get_local
                _lconn = _get_local()
                _lconn.execute('DELETE FROM config_listes WHERE nom_liste=?', (nom,))
                _lconn.commit(); _lconn.close()
    except Exception:
        logger.warning('Propagation croisée reset config_listes échouée', exc_info=True)
    # Remettre le flag à 0 APRÈS commit : la liste est de nouveau "non initialisée"
    cfg_set(f'_list_init_{nom}', '0')
    cfg_invalidate()
    return jsonify({'ok': True, 'valeurs': LISTE_DEFAULTS[nom]})


@app.route('/api/services', methods=['GET'])
def api_services_get():
    cid = get_client_id()
    if not cid: return jsonify({'error': 'no client'}), 400
    conn = get_db()
    rows = conn.execute('SELECT id,nom,couleur,responsable FROM services WHERE client_id=? ORDER BY ordre,nom', (cid,)).fetchall()
    conn.close()
    return jsonify({'services': [{'id':r[0],'nom':r[1],'couleur':r[2],'responsable':r[3]} for r in rows]})

@app.route('/api/services/ajouter', methods=['POST'])
def api_services_ajouter():
    cid = get_client_id()
    if not cid: return jsonify({'error': 'no client'}), 400
    nom = (request.json or {}).get('nom', '').strip()
    if not nom: return jsonify({'error': 'Nom vide'}), 400
    now = datetime.utcnow().isoformat()
    conn = get_db()
    try:
        conn.execute('INSERT INTO services (client_id,nom,couleur,ordre,date_creation,date_maj) VALUES (?,?,?,?,?,?)',
            (cid, nom, '#6a8aaa', 0, now, now))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500
    rows = conn.execute('SELECT id,nom,couleur,responsable FROM services WHERE client_id=? ORDER BY ordre,nom', (cid,)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'services': [{'id':r[0],'nom':r[1],'couleur':r[2],'responsable':r[3]} for r in rows]})

@app.route('/api/services/supprimer', methods=['POST'])
def api_services_supprimer():
    cid = get_client_id()
    if not cid: return jsonify({'error': 'no client'}), 400
    sid = (request.json or {}).get('id')
    if not sid: return jsonify({'error': 'id manquant'}), 400
    conn = get_db()
    conn.execute('DELETE FROM services WHERE id=? AND client_id=?', (sid, cid))
    conn.commit()
    rows = conn.execute('SELECT id,nom,couleur,responsable FROM services WHERE client_id=? ORDER BY ordre,nom', (cid,)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'services': [{'id':r[0],'nom':r[1],'couleur':r[2],'responsable':r[3]} for r in rows]})

# --- WATCHDOG PING -----------------------------------------------------------
# Thread de surveillance : ping tous les appareils avec une IP toutes les N sec.

PING_INTERVAL = 60   # secondes entre deux cycles complets
PING_TIMEOUT  = 1.0  # timeout par tentative ping
PING_WORKERS  = 30   # threads simultanes

_ping_cache = {}           # { appareil_id: {en_ligne, ts, ip} }
_ping_cache_lock = threading.Lock()
_watchdog_state  = {'running': False, 'last_cycle': None, 'cycle_count': 0}

def _ping_once(ip_str):
    try:
        cmd = ['ping','-n','1','-w','500',ip_str] if IS_WINDOWS else ['ping','-c','1','-W','1',ip_str]
        if _run_hidden(cmd, capture_output=True, timeout=3).returncode == 0:
            return True
    except Exception:
        pass
    for port in [80, 443, 22, 445, 3389, 8080, 53, 135, 139]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(PING_TIMEOUT)
            if s.connect_ex((ip_str, port)) == 0:
                s.close(); return True
            s.close()
        except Exception:
            pass
    return False

def _ping_worker(item):
    aid, ip = item
    try:
        en_ligne = _ping_once(ip)
    except Exception:
        en_ligne = False
    return aid, ip, en_ligne, datetime.now().isoformat()

def _watchdog_cycle():
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, adresse_ip FROM appareils WHERE adresse_ip != \'\' AND adresse_ip IS NOT NULL"
        ).fetchall()
        conn.close()
    except Exception:
        return
    if not rows:
        return
    items = [(r[0], r[1]) for r in rows]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=PING_WORKERS) as ex:
        for res in ex.map(_ping_worker, items):
            results.append(res)
    try:
        conn = get_db()
        for aid, ip, en_ligne, ts in results:
            conn.execute("UPDATE appareils SET en_ligne=?, dernier_ping=? WHERE id=?",
                         (1 if en_ligne else 0, ts, aid))
            with _ping_cache_lock:
                _ping_cache[aid] = {'en_ligne': en_ligne, 'ts': ts, 'ip': ip}
        conn.commit(); conn.close()
    except Exception:
        pass
    with _ping_cache_lock:
        _watchdog_state['last_cycle'] = datetime.now().isoformat()
        _watchdog_state['cycle_count'] += 1

def _watchdog_loop():
    _watchdog_state['running'] = True
    time.sleep(5)
    while True:
        try:
            _watchdog_cycle()
        except Exception:
            pass
        time.sleep(PING_INTERVAL)

_wd_thread = threading.Thread(target=_watchdog_loop, daemon=True, name='PingWatchdog')
_wd_thread.start()

@app.route('/api/ping/statuts')
def api_ping_statuts():
    cid = get_client_id()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, en_ligne, dernier_ping, adresse_ip FROM appareils WHERE client_id=?", (cid,)
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        aid = r[0]
        with _ping_cache_lock:
            cached = _ping_cache.get(aid)
        if cached:
            result[str(aid)] = {'en_ligne': cached['en_ligne'], 'ts': cached['ts'], 'ip': cached['ip']}
        else:
            result[str(aid)] = {'en_ligne': bool(r[1]), 'ts': r[2] or '', 'ip': r[3] or ''}
    return jsonify({
        'statuts': result,
        'last_cycle': _watchdog_state['last_cycle'],
        'cycle_count': _watchdog_state['cycle_count'],
        'interval': PING_INTERVAL
    })

@app.route('/api/ping/summary')
def api_ping_summary():
    '''Résumé léger pour la topbar : nb en ligne, nb total, dernière mise à jour'''
    cid = get_client_id()
    if not cid: return jsonify({'en_ligne': 0, 'total': 0})
    conn = get_db()
    total   = conn.execute("SELECT COUNT(*) FROM appareils WHERE client_id=? AND statut='actif' AND adresse_ip!=''", (cid,)).fetchone()[0]
    en_ligne = conn.execute("SELECT COUNT(*) FROM appareils WHERE client_id=? AND en_ligne=1", (cid,)).fetchone()[0]
    conn.close()
    return jsonify({
        'en_ligne':  en_ligne,
        'total':     total,
        'last_cycle': _watchdog_state.get('last_cycle'),
    })

@app.route('/api/ping/force', methods=['POST'])
def api_ping_force():
    threading.Thread(target=_watchdog_cycle, daemon=True).start()
    return jsonify({'started': True})

@app.route('/api/ping/appareil/<int:id>')
def api_ping_appareil(id):
    cid = get_client_id()
    conn = get_db()
    row = conn.execute('SELECT id, adresse_ip FROM appareils WHERE id=? AND client_id=?', (id, cid)).fetchone()
    conn.close()
    if not row or not row[1]:
        return jsonify({'error': 'Appareil sans IP'}), 400
    aid, ip = row[0], row[1]
    en_ligne = _ping_once(ip)
    ts = datetime.now().isoformat()
    conn = get_db()
    conn.execute('UPDATE appareils SET en_ligne=?, dernier_ping=? WHERE id=?', (1 if en_ligne else 0, ts, aid))
    conn.commit(); conn.close()
    with _ping_cache_lock:
        _ping_cache[aid] = {'en_ligne': en_ligne, 'ts': ts, 'ip': ip}
    return jsonify({'id': aid, 'ip': ip, 'en_ligne': en_ligne, 'ts': ts})

# ─── HELPERS ─────────────────────────────────────────────────────────────────

# Types d'appareils qui doivent apparaître automatiquement dans les périphériques
_APPAREIL_PERIPH_MAP = {
    'Imprimante':              'Imprimante',
    'Imprimante multifonction':'Imprimante multifonction',
    'NAS':                     'Disque dur externe',
}

def _sync_appareil_to_periph(conn, appareil_id, client_id):
    """
    Si l'appareil est de type Imprimante / NAS, crée ou met à jour
    l'entrée correspondante dans la table périphériques.
    Le lien est maintenu via la table pivot peripheriques_appareils.
    """
    a = row_to_dict(conn.execute('SELECT * FROM appareils WHERE id=?', (appareil_id,)).fetchone() or {})
    if not a:
        return
    categorie = _APPAREIL_PERIPH_MAP.get(a.get('type_appareil', ''))
    if not categorie:
        return
    now = datetime.utcnow().isoformat()
    # Chercher via table pivot
    existing = conn.execute(
        'SELECT p.id FROM peripheriques p'
        ' JOIN peripheriques_appareils pa ON pa.peripherique_id = p.id'
        ' WHERE pa.appareil_id=? AND p.client_id=? AND p.categorie=?',
        (appareil_id, client_id, categorie)).fetchone()
    if existing:
        conn.execute('''UPDATE peripheriques SET
            marque=?, modele=?, numero_serie=?, localisation=?, statut=?,
            date_achat=?, duree_garantie=?, date_fin_garantie=?, fournisseur=?, date_maj=?
            WHERE id=?''',
            (a.get('marque',''), a.get('modele',''), a.get('numero_serie',''),
             a.get('localisation',''), a.get('statut','actif'),
             a.get('date_achat',''), a.get('duree_garantie',0),
             a.get('date_fin_garantie',''), a.get('fournisseur',''), now, existing[0]))
    else:
        conn.execute(
            '''INSERT INTO peripheriques
               (client_id, appareil_id, categorie, marque, modele, numero_serie, localisation,
                statut, date_achat, duree_garantie, date_fin_garantie, fournisseur,
                utilisateur_id, description, prix_achat, numero_commande, notes,
                date_creation, date_maj)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,'',NULL,'','',?,?)''',
            (client_id, appareil_id, categorie,
             a.get('marque',''), a.get('modele',''), a.get('numero_serie',''),
             a.get('localisation',''), a.get('statut','actif'),
             a.get('date_achat',''), a.get('duree_garantie',0),
             a.get('date_fin_garantie',''), a.get('fournisseur',''), now, now))
        new_pid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO peripheriques_appareils (peripherique_id, appareil_id) VALUES (?,?)",
                     (new_pid, appareil_id))

SW_COURANTS_GROUPS = [
    ('Bureautique',       ['Microsoft 365', 'Microsoft Office', 'LibreOffice', 'Google Workspace']),
    ('Navigateurs',       ['Google Chrome', 'Mozilla Firefox', 'Microsoft Edge', 'Safari']),
    ('Communication',     ['Microsoft Teams', 'Zoom', 'Slack', 'Skype', 'Discord', 'WhatsApp']),
    ('Email',             ['Microsoft Outlook', 'Mozilla Thunderbird']),
    ('Cloud / Stockage',  ['OneDrive', 'SharePoint', 'Dropbox', 'Google Drive', 'iCloud Drive']),
    ('Utilitaires',       ['7-Zip', 'WinRAR', 'VLC', 'Notepad++', 'Adobe Reader', 'Adobe Acrobat Pro', 'PDF24']),
    ('Accès distant',     ['AnyDesk', 'TeamViewer', 'RealVNC', 'Remote Desktop', 'mRemoteNG']),
    ('Développement',     ['Visual Studio Code', 'Git', 'Python', 'Node.js', 'Docker', 'Postman']),
]
# Ensemble plat pour détecter les logiciels personnalisés
SW_COURANTS_ALL = set(sw for _, items in SW_COURANTS_GROUPS for sw in items)


def _extract_form(f):
    prix = None
    try: prix = float(f['prix_achat']) if f.get('prix_achat') else None
    except: pass
    duree = 0
    try: duree = int(f['duree_garantie']) if f.get('duree_garantie') else 0
    except: pass
    av_contrat_id = None
    try: av_contrat_id = int(f['av_contrat_id']) if f.get('av_contrat_id') else None
    except: pass
    edr_contrat_id = None
    try: edr_contrat_id = int(f['edr_contrat_id']) if f.get('edr_contrat_id') else None
    except: pass
    rmm_contrat_id = None
    try: rmm_contrat_id = int(f['rmm_contrat_id']) if f.get('rmm_contrat_id') else None
    except: pass
    return (f.get('nom_machine',''), f.get('type_appareil',''), f.get('marque',''), f.get('modele',''),
            f.get('numero_serie',''), f.get('adresse_ip',''), f.get('adresse_mac',''), f.get('nom_dns',''),
            f.get('utilisateur',''), f.get('service',''), f.get('localisation',''),
            f.get('date_achat',''), duree, f.get('date_fin_garantie',''), f.get('fournisseur',''),
            prix, f.get('numero_commande',''), f.get('os',''), f.get('version_os',''),
            f.get('ram',''), f.get('cpu',''), f.get('stockage',''), f.get('carte_graphique',''),
            f.get('statut','actif'), f.get('notes',''),
            f.get('user_login',''), f.get('user_password',''),
            f.get('admin_login',''), f.get('admin_password',''),
            f.get('anydesk_id',''), f.get('anydesk_password',''),
            f.get('av_marque',''), f.get('av_nom',''),
            f.get('av_date_debut',''), f.get('av_date_fin',''), av_contrat_id,
            f.get('edr_marque',''), f.get('edr_nom',''),
            f.get('edr_date_fin',''), edr_contrat_id,
            f.get('rmm_marque',''), f.get('rmm_nom',''),
            f.get('rmm_agent_id',''), f.get('rmm_date_fin',''), rmm_contrat_id,
            json.dumps(f.getlist('logiciels'), ensure_ascii=False) if f.getlist('logiciels') else '[]',
            1 if f.get('garantie_alerte_ignoree') else 0)


# ─── HISTORIQUE ───────────────────────────────────────────────────────────────

# ── JOURNAL : DIFF & ANNULATION ──────────────────────────────────────────────

import json as _hist_json

_HIST_SENSITIVE = {'user_password', 'admin_password', 'anydesk_password', 'mot_de_passe'}

_HIST_LABELS = {
    'nom_machine':'Nom machine','type_appareil':'Type','marque':'Marque','modele':'Modèle',
    'numero_serie':'N° série','adresse_ip':'Adresse IP','adresse_mac':'Adresse MAC',
    'nom_dns':'Nom DNS','utilisateur':'Utilisateur','service':'Service',
    'localisation':'Localisation','date_achat':'Date achat',
    'duree_garantie':'Garantie (mois)','date_fin_garantie':'Fin garantie',
    'fournisseur':'Fournisseur','prix_achat':'Prix HT','numero_commande':'N° cmd',
    'os':'OS','version_os':'Version OS','ram':'RAM','cpu':'CPU','stockage':'Stockage',
    'carte_graphique':'GPU','statut':'Statut','notes':'Notes',
    'user_login':'Login user','user_password':'MDP user',
    'admin_login':'Login admin','admin_password':'MDP admin',
    'anydesk_id':'AnyDesk ID','anydesk_password':'AnyDesk MDP',
    'categorie':'Catégorie','description':'Description',
    'appareil_id':'Appareil attaché','utilisateur_id':'Utilisateur attaché',
    'nom':'Nom','login':'Login','mot_de_passe':'Mot de passe','url':'URL',
    'date_expiration':'Expiration','wifi_ssid':'SSID Wi-Fi','wifi_securite':'Sécurité Wi-Fi',
    'titre':'Titre','type_contrat':'Type contrat','contact_fournisseur':'Contact',
    'email_fournisseur':'Email fournisseur','telephone_fournisseur':'Tél. fournisseur',
    'numero_contrat':'N° contrat','date_debut':'Date début','date_fin':'Date fin',
    'reconduction_auto':'Reconduction auto','preavis_jours':'Préavis (j)',
    'montant_ht':'Montant HT','periodicite':'Périodicité',
    'prenom':'Prénom','poste':'Poste','email':'Email','telephone':'Téléphone',
    'login_windows':'Login Windows','login_mail':'Login mail','service_id':'Service',
}

# Colonnes métier par entité — utilisées pour le diff et la restauration
_ENTITE_COLS = {
    'appareil': ['nom_machine','type_appareil','marque','modele','numero_serie',
        'adresse_ip','adresse_mac','nom_dns','utilisateur','service','localisation',
        'date_achat','duree_garantie','date_fin_garantie','fournisseur','prix_achat',
        'numero_commande','os','version_os','ram','cpu','stockage','carte_graphique',
        'statut','notes','user_login','user_password','admin_login','admin_password',
        'anydesk_id','anydesk_password',
        'av_marque','av_nom','av_date_debut','av_date_fin','av_contrat_id',
        'edr_marque','edr_nom','edr_date_fin','edr_contrat_id',
        'rmm_marque','rmm_nom','rmm_agent_id','rmm_date_fin','rmm_contrat_id'],
    'peripherique': ['categorie','marque','modele','numero_serie','description','localisation',
        'statut','date_achat','duree_garantie','date_fin_garantie','fournisseur','prix_achat',
        'numero_commande','notes','appareil_id','utilisateur_id'],
    'identifiant': ['categorie','nom','login','mot_de_passe','url','description','notes',
        'date_expiration','wifi_ssid','wifi_securite'],
    'contrat': ['titre','type_contrat','fournisseur','contact_fournisseur','email_fournisseur',
        'telephone_fournisseur','numero_contrat','date_debut','date_fin','reconduction_auto',
        'preavis_jours','montant_ht','periodicite','description','notes','statut'],
    'utilisateur': ['prenom','nom','poste','email','telephone','login_windows','login_mail',
        'statut','notes','service_id'],
    'intervention': ['titre','type_intervention','description','notes','date_intervention',
        'heure_debut','heure_fin','duree_minutes','technicien_nom','technicien_email',
        'statut','contrat_id','cout_ht','devise'],
}

_ENTITE_TABLE = {
    'appareil':'appareils','peripherique':'peripheriques',
    'identifiant':'identifiants','contrat':'contrats','utilisateur':'utilisateurs',
    'intervention':'interventions',
}


def _diff_json(avant: dict, apres: dict) -> str:
    """Compare deux dicts métier et retourne JSON {avant:{…},apres:{…}} des seuls champs modifiés.
    Les champs sensibles sont remplacés par ••••."""
    da, dp = {}, {}
    for k in set(avant) | set(apres):
        v1 = str(avant.get(k, '') or '').strip()
        v2 = str(apres.get(k, '') or '').strip()
        if v1 != v2:
            if k in _HIST_SENSITIVE:
                da[k] = '••••' if v1 else ''
                dp[k] = '••••' if v2 else ''
            else:
                da[k] = v1
                dp[k] = v2
    return _hist_json.dumps({'avant': da, 'apres': dp}, ensure_ascii=False) if da else ''


@app.route('/historique/<int:hist_id>/annuler', methods=['POST'])
@login_required
def annuler_historique(hist_id):
    """Restaure une entité à son état avant une modification enregistrée dans l'historique."""
    if not can_write():
        flash('Accès en lecture seule — annulation non autorisée', 'danger')
        return redirect(url_for('page_historique'))
    cid = get_client_id()
    conn = get_db()
    h = row_to_dict(conn.execute(
        'SELECT * FROM historique WHERE id=? AND client_id=?', (hist_id, cid)).fetchone() or {})
    if not h:
        flash('Entrée introuvable', 'danger')
        conn.close(); return redirect(url_for('page_historique'))
    if h.get('action') != 'Modification':
        flash('Seules les modifications peuvent être annulées', 'warning')
        conn.close(); return redirect(url_for('page_historique'))
    try:
        details = _hist_json.loads(h.get('details') or '{}')
        avant = details.get('avant', {})
    except Exception:
        avant = {}
    if not avant:
        flash('Données avant-modification non disponibles pour cette entrée', 'warning')
        conn.close(); return redirect(url_for('page_historique'))
    table = _ENTITE_TABLE.get(h.get('entite', ''))
    allowed_cols = set(_ENTITE_COLS.get(h.get('entite', ''), []))
    if not table:
        flash("Type d'entité non supporté", 'danger')
        conn.close(); return redirect(url_for('page_historique'))
    exists = conn.execute(
        f'SELECT id FROM {table} WHERE id=? AND client_id=?', (h['entite_id'], cid)).fetchone()
    if not exists:
        flash("L'élément n'existe plus, impossible d'annuler", 'danger')
        conn.close(); return redirect(url_for('page_historique'))
    cols = [k for k in avant if k in allowed_cols]
    if not cols:
        flash('Aucune donnée à restaurer', 'warning')
        conn.close(); return redirect(url_for('page_historique'))
    now = datetime.utcnow().isoformat()
    set_clause = ', '.join(f'"{c}"=?' for c in cols) + ', date_maj=?'
    vals = [avant[c] for c in cols] + [now, h['entite_id'], cid]
    conn.execute(f'UPDATE {table} SET {set_clause} WHERE id=? AND client_id=?', vals)
    ts = (h.get('date_action', '')[:16] or '').replace('T', ' ')
    log_history(conn, cid, h['entite'], h['entite_id'], h['entite_nom'], 'Annulation',
                _hist_json.dumps({'message': f'Restauration état avant modification du {ts}'},
                                 ensure_ascii=False))
    conn.commit(); conn.close()
    flash(f'Modification annulée — « {h["entite_nom"]} » restauré à l\'état précédent', 'success')
    return redirect(url_for('page_historique'))


@app.route('/historique/<int:hist_id>/supprimer', methods=['POST'])
@login_required
def supprimer_entree_historique(hist_id):
    """Supprime une entrée individuelle du journal d'historique."""
    cid = get_client_id()
    if not cid:
        return jsonify({'ok': False, 'message': 'Aucun client actif'}), 400
    conn = get_db()
    row = conn.execute('SELECT id FROM historique WHERE id=? AND client_id=?', (hist_id, cid)).fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'message': 'Entrée introuvable'}), 404
    conn.execute('DELETE FROM historique WHERE id=?', (hist_id,))
    # Enregistrer explicitement la suppression pour la sync Turso
    conn.execute(
        "INSERT OR REPLACE INTO _sync_deletions (tbl, record_id, deleted_at) VALUES ('historique', ?, datetime('now'))",
        (hist_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/historique/vider-erreurs', methods=['POST'])
@login_required
def vider_erreurs_historique():
    """Supprime toutes les entrées d'erreur système du journal."""
    cid = get_client_id()
    if not cid:
        return jsonify({'ok': False, 'message': 'Aucun client actif'}), 400
    conn = get_db()
    # Récupérer les IDs avant suppression pour les enregistrer dans _sync_deletions
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM historique WHERE client_id=? AND action='Erreur'", (cid,)).fetchall()]
    if ids:
        conn.execute(
            "DELETE FROM historique WHERE client_id=? AND action='Erreur'", (cid,))
        # Enregistrer chaque suppression pour la sync Turso
        conn.executemany(
            "INSERT OR REPLACE INTO _sync_deletions (tbl, record_id, deleted_at) VALUES ('historique', ?, datetime('now'))",
            [(i,) for i in ids])
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'nb': len(ids)})


@app.route('/historique')
@login_required
def page_historique():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    filtre = request.args.get('entite', '')
    limit  = int(request.args.get('limit', 200))
    if filtre:
        rows = conn.execute(
            "SELECT * FROM historique WHERE client_id=? AND entite=? ORDER BY date_action DESC LIMIT ?",
            (cid, filtre, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM historique WHERE client_id=? ORDER BY date_action DESC LIMIT ?",
            (cid, limit)).fetchall()
    hist = [row_to_dict(r) for r in rows]
    conn.close()
    return render_template('historique.html', hist=hist, client=client,
                           filtre=filtre, can_write_flag=can_write(),
                           clients=get_clients(), client_actif_id=cid)

@app.route('/api/historique/entite/<entite>/<int:entite_id>')
def api_historique_entite(entite, entite_id):
    cid = get_client_id()
    conn = get_db()
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM historique WHERE client_id=? AND entite=? AND entite_id=? ORDER BY date_action DESC LIMIT 20",
        (cid, entite, entite_id)).fetchall()]
    conn.close()
    return jsonify(rows)


import csv, io as _io

# ─── COLONNES EXPORT/IMPORT ──────────────────────────────────────────────────

# Appareils : toutes les colonnes métier (pas id, client_id, ping interne)
COLS_APPAREILS = [
    'nom_machine','type_appareil','marque','modele','numero_serie',
    'adresse_ip','adresse_mac','nom_dns','utilisateur','service','localisation',
    'date_achat','duree_garantie','date_fin_garantie','fournisseur','prix_achat',
    'numero_commande','os','version_os','ram','cpu','stockage','statut',
    'ports_ouverts','notes','user_login','user_password',
    'admin_login','admin_password','anydesk_id','anydesk_password',
    'date_creation','date_maj',
]

# Périphériques
COLS_PERIPHERIQUES = [
    'categorie','marque','modele','numero_serie','description','localisation',
    'statut','date_achat','duree_garantie','date_fin_garantie','fournisseur',
    'prix_achat','numero_commande','notes','date_creation','date_maj',
]

# ─── EXPORT CSV APPAREILS ────────────────────────────────────────────────────

@app.route('/appareils/export.csv')
def export_appareils_csv():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    rows = conn.execute(
        f"SELECT {','.join(COLS_APPAREILS)} FROM appareils WHERE client_id=? ORDER BY nom_machine",
        (cid,)).fetchall()
    conn.close()
    out = _io.StringIO()
    w = csv.writer(out, delimiter=';')
    w.writerow(COLS_APPAREILS)
    for r in rows:
        w.writerow([str(v) if v is not None else '' for v in r])
    bom = '\ufeff'
    resp = app.response_class(bom + out.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=appareils_export.csv'})
    return resp

# ─── EXPORT CSV PÉRIPHÉRIQUES ────────────────────────────────────────────────

@app.route('/peripheriques/export.csv')
def export_peripheriques_csv():
    cid = get_client_id()
    if not cid: return redirect(url_for('nouveau_client'))
    conn = get_db()
    rows = conn.execute(
        f"SELECT {','.join(COLS_PERIPHERIQUES)} FROM peripheriques WHERE client_id=? ORDER BY categorie,marque,modele",
        (cid,)).fetchall()
    conn.close()
    out = _io.StringIO()
    w = csv.writer(out, delimiter=';')
    w.writerow(COLS_PERIPHERIQUES)
    for r in rows:
        w.writerow([str(v) if v is not None else '' for v in r])
    bom = '\ufeff'
    resp = app.response_class(bom + out.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=peripheriques_export.csv'})
    return resp

# ─── IMPORT CSV APPAREILS ────────────────────────────────────────────────────

@app.route('/appareils/import', methods=['POST'])
def import_appareils_csv():
    cid = get_client_id()
    if not cid: return redirect(url_for('liste_appareils'))
    if 'fichier' not in request.files:
        flash('Aucun fichier sélectionné', 'danger')
        return redirect(url_for('liste_appareils'))
    f = request.files['fichier']
    if not f.filename.lower().endswith('.csv'):
        flash('Le fichier doit être au format CSV', 'danger')
        return redirect(url_for('liste_appareils'))
    try:
        content_bytes = f.read()
        # Handle BOM
        text = content_bytes.decode('utf-8-sig')
        reader = csv.DictReader(_io.StringIO(text), delimiter=';')
        
        # Validate header
        if not reader.fieldnames:
            flash('Fichier CSV vide ou invalide', 'danger')
            return redirect(url_for('liste_appareils'))
        
        missing = [c for c in ['nom_machine'] if c not in reader.fieldnames]
        if missing:
            flash(f'Colonnes manquantes : {", ".join(missing)}. Utilisez le CSV exporté comme modèle.', 'danger')
            return redirect(url_for('liste_appareils'))
        
        now = datetime.utcnow().isoformat()
        conn = get_db()
        inserted = updated = errors = 0
        
        for row in reader:
            try:
                nom = row.get('nom_machine','').strip()
                if not nom: continue
                
                # Vérifier si un appareil avec ce nom existe déjà
                existing = conn.execute(
                    'SELECT id FROM appareils WHERE client_id=? AND nom_machine=?',
                    (cid, nom)).fetchone()
                
                prix = None
                try: prix = float(row.get('prix_achat','')) if row.get('prix_achat','').strip() else None
                except: pass
                duree = 0
                try: duree = int(row.get('duree_garantie','') or 0)
                except: pass
                
                vals = [
                    row.get('nom_machine','').strip(),
                    row.get('type_appareil','').strip(),
                    row.get('marque','').strip(),
                    row.get('modele','').strip(),
                    row.get('numero_serie','').strip(),
                    row.get('adresse_ip','').strip(),
                    row.get('adresse_mac','').strip(),
                    row.get('nom_dns','').strip(),
                    row.get('utilisateur','').strip(),
                    row.get('service','').strip(),
                    row.get('localisation','').strip(),
                    row.get('date_achat','').strip(),
                    duree,
                    row.get('date_fin_garantie','').strip(),
                    row.get('fournisseur','').strip(),
                    prix,
                    row.get('numero_commande','').strip(),
                    row.get('os','').strip(),
                    row.get('version_os','').strip(),
                    row.get('ram','').strip(),
                    row.get('cpu','').strip(),
                    row.get('stockage','').strip(),
                    row.get('statut','actif').strip() or 'actif',
                    row.get('ports_ouverts','').strip(),
                    row.get('notes','').strip(),
                    row.get('user_login','').strip(),
                    row.get('user_password','').strip(),
                    row.get('admin_login','').strip(),
                    row.get('admin_password','').strip(),
                    row.get('anydesk_id','').strip(),
                    row.get('anydesk_password','').strip(),
                ]
                
                if existing:
                    conn.execute(
                        f"""UPDATE appareils SET
                            nom_machine=?,type_appareil=?,marque=?,modele=?,numero_serie=?,
                            adresse_ip=?,adresse_mac=?,nom_dns=?,utilisateur=?,service=?,localisation=?,
                            date_achat=?,duree_garantie=?,date_fin_garantie=?,fournisseur=?,prix_achat=?,
                            numero_commande=?,os=?,version_os=?,ram=?,cpu=?,stockage=?,statut=?,
                            ports_ouverts=?,notes=?,user_login=?,user_password=?,
                            admin_login=?,admin_password=?,anydesk_id=?,anydesk_password=?,
                            date_maj=? WHERE client_id=? AND id=?""",
                        vals + [now, cid, existing[0]])
                    updated += 1
                else:
                    conn.execute(
                        f"""INSERT INTO appareils
                            (nom_machine,type_appareil,marque,modele,numero_serie,
                            adresse_ip,adresse_mac,nom_dns,utilisateur,service,localisation,
                            date_achat,duree_garantie,date_fin_garantie,fournisseur,prix_achat,
                            numero_commande,os,version_os,ram,cpu,stockage,statut,
                            ports_ouverts,notes,user_login,user_password,
                            admin_login,admin_password,anydesk_id,anydesk_password,
                            client_id,date_creation,date_maj)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        vals + [cid, now, now])
                    inserted += 1
            except Exception as e:
                errors += 1
        
        conn.commit(); conn.close()
        msg = f'Import terminé : {inserted} ajouté(s), {updated} mis à jour'
        if errors: msg += f', {errors} erreur(s)'
        flash(msg, 'success' if not errors else 'warning')
    except Exception as e:
        flash(f'Erreur lors de l\'import : {str(e)}', 'danger')
    return redirect(url_for('liste_appareils'))

# ─── IMPORT CSV PÉRIPHÉRIQUES ────────────────────────────────────────────────

@app.route('/peripheriques/import', methods=['POST'])
def import_peripheriques_csv():
    cid = get_client_id()
    if not cid: return redirect(url_for('liste_peripheriques'))
    if 'fichier' not in request.files:
        flash('Aucun fichier sélectionné', 'danger')
        return redirect(url_for('liste_peripheriques'))
    f = request.files['fichier']
    if not f.filename.lower().endswith('.csv'):
        flash('Le fichier doit être au format CSV', 'danger')
        return redirect(url_for('liste_peripheriques'))
    try:
        text = f.read().decode('utf-8-sig')
        reader = csv.DictReader(_io.StringIO(text), delimiter=';')
        if not reader.fieldnames or 'categorie' not in reader.fieldnames:
            flash('Colonne "categorie" manquante. Utilisez le CSV exporté comme modèle.', 'danger')
            return redirect(url_for('liste_peripheriques'))
        
        now = datetime.utcnow().isoformat()
        conn = get_db()
        inserted = updated = errors = 0
        
        for row in reader:
            try:
                cat = row.get('categorie','').strip()
                marque = row.get('marque','').strip()
                modele = row.get('modele','').strip()
                if not cat: continue
                
                prix = None
                try: prix = float(row.get('prix_achat','')) if row.get('prix_achat','').strip() else None
                except: pass
                duree = 0
                try: duree = int(row.get('duree_garantie','') or 0)
                except: pass
                
                # Identifier par categorie+marque+modele+serie
                serie = row.get('numero_serie','').strip()
                existing = None
                if serie:
                    existing = conn.execute(
                        'SELECT id FROM peripheriques WHERE client_id=? AND numero_serie=? AND numero_serie!=""',
                        (cid, serie)).fetchone()
                if not existing and marque and modele:
                    existing = conn.execute(
                        'SELECT id FROM peripheriques WHERE client_id=? AND categorie=? AND marque=? AND modele=?',
                        (cid, cat, marque, modele)).fetchone()
                
                vals = [
                    cat, marque, modele, serie,
                    row.get('description','').strip(),
                    row.get('localisation','').strip(),
                    row.get('statut','actif').strip() or 'actif',
                    row.get('date_achat','').strip(),
                    duree,
                    row.get('date_fin_garantie','').strip(),
                    row.get('fournisseur','').strip(),
                    prix,
                    row.get('numero_commande','').strip(),
                    row.get('notes','').strip(),
                ]
                
                if existing:
                    conn.execute(
                        """UPDATE peripheriques SET
                            categorie=?,marque=?,modele=?,numero_serie=?,description=?,
                            localisation=?,statut=?,date_achat=?,duree_garantie=?,
                            date_fin_garantie=?,fournisseur=?,prix_achat=?,numero_commande=?,
                            notes=?,date_maj=? WHERE client_id=? AND id=?""",
                        vals + [now, cid, existing[0]])
                    updated += 1
                else:
                    conn.execute(
                        """INSERT INTO peripheriques
                            (categorie,marque,modele,numero_serie,description,localisation,
                            statut,date_achat,duree_garantie,date_fin_garantie,fournisseur,
                            prix_achat,numero_commande,notes,client_id,date_creation,date_maj)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        vals + [cid, now, now])
                    inserted += 1
            except Exception as e:
                errors += 1
        
        conn.commit(); conn.close()
        msg = f'Import terminé : {inserted} ajouté(s), {updated} mis à jour'
        if errors: msg += f', {errors} erreur(s)'
        flash(msg, 'success' if not errors else 'warning')
    except Exception as e:
        flash(f'Erreur lors de l\'import : {str(e)}', 'danger')
    return redirect(url_for('liste_peripheriques'))


import json as _json, zipfile as _zipfile, tempfile as _tempfile, shutil as _shutil, io as _io2


# ─── KNOWLEDGE BASE ──────────────────────────────────────────────────────────

@app.route('/kb')
@login_required
def page_kb():
    conn = get_db()
    cats = [row_to_dict(r) for r in conn.execute(
        'SELECT * FROM kb_categories ORDER BY ordre, nom').fetchall()]
    articles = [row_to_dict(r) for r in conn.execute(
        'SELECT a.*, c.nom as cat_nom, c.icone as cat_icone FROM kb_articles a '
        'JOIN kb_categories c ON a.categorie_id=c.id ORDER BY c.ordre, a.titre').fetchall()]
    conn.close()
    return render_template('kb.html', cats=cats, articles=articles,
                           clients=get_clients(), client_actif_id=get_client_id())

@app.route('/api/kb/search')
def api_kb_search():
    q = request.args.get('q', '').lower().strip()
    conn = get_db()
    results = [row_to_dict(r) for r in conn.execute(
        "SELECT a.id, a.titre, a.tags, a.categorie_id, c.nom as cat_nom, c.icone as cat_icone "
        "FROM kb_articles a JOIN kb_categories c ON a.categorie_id=c.id "
        "WHERE lower(a.titre) LIKE ? OR lower(a.contenu) LIKE ? OR lower(a.tags) LIKE ? "
        "ORDER BY c.ordre, a.titre",
        (f'%{q}%', f'%{q}%', f'%{q}%')).fetchall()]
    conn.close()
    return jsonify(results)

@app.route('/api/kb/article/<int:id>')
def api_kb_article(id):
    conn = get_db()
    a = row_to_dict(conn.execute(
        'SELECT a.*, c.nom as cat_nom FROM kb_articles a '
        'JOIN kb_categories c ON a.categorie_id=c.id WHERE a.id=?', (id,)).fetchone() or {})
    conn.close()
    return jsonify(a)

@app.route('/api/kb/article', methods=['POST'])
def api_kb_create_article():
    f = request.json or {}
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute('INSERT INTO kb_articles (categorie_id,titre,contenu,tags,date_creation,date_maj) VALUES (?,?,?,?,?,?)',
        (f.get('categorie_id'), f.get('titre',''), f.get('contenu',''), f.get('tags',''), now, now))
    conn.commit()
    id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    a = row_to_dict(conn.execute('SELECT * FROM kb_articles WHERE id=?', (id,)).fetchone() or {})
    conn.close()
    return jsonify(a)

@app.route('/api/kb/article/<int:id>', methods=['PUT'])
def api_kb_update_article(id):
    f = request.json or {}
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute('UPDATE kb_articles SET categorie_id=?,titre=?,contenu=?,tags=?,date_maj=? WHERE id=?',
        (f.get('categorie_id'), f.get('titre',''), f.get('contenu',''), f.get('tags',''), now, id))
    conn.commit()
    a = row_to_dict(conn.execute('SELECT * FROM kb_articles WHERE id=?', (id,)).fetchone() or {})
    conn.close()
    return jsonify(a)

@app.route('/api/kb/article/<int:id>', methods=['DELETE'])
def api_kb_delete_article(id):
    conn = get_db()
    conn.execute('DELETE FROM kb_articles WHERE id=?', (id,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/kb/categories', methods=['GET'])
def api_kb_categories():
    conn = get_db()
    cats = [row_to_dict(r) for r in conn.execute('SELECT * FROM kb_categories ORDER BY ordre,nom').fetchall()]
    conn.close()
    return jsonify(cats)

@app.route('/api/kb/category', methods=['POST'])
def api_kb_create_category():
    f = request.json or {}
    conn = get_db()
    conn.execute('INSERT INTO kb_categories (nom,icone,ordre) VALUES (?,?,?)',
        (f.get('nom','Nouvelle categorie'), f.get('icone','📋'), f.get('ordre',99)))
    conn.commit()
    id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    cat = row_to_dict(conn.execute('SELECT * FROM kb_categories WHERE id=?', (id,)).fetchone() or {})
    conn.close()
    return jsonify(cat)

@app.route('/api/kb/category/<int:id>', methods=['DELETE'])
def api_kb_delete_category(id):
    conn = get_db()
    conn.execute('DELETE FROM kb_articles WHERE categorie_id=?', (id,))
    conn.execute('DELETE FROM kb_categories WHERE id=?', (id,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ─── EXPORT / IMPORT GLOBAL ──────────────────────────────────────────────────
#
# Stratégie : export JSON complet de toutes les tables, sans les ids (on
# réassigne à l'import). Les fichiers uploadés (documents, photos de baie)
# sont inclus dans un ZIP. Toutes les tables sont exportées dynamiquement
# — si de nouvelles tables sont ajoutées à init_db(), elles seront
# automatiquement incluses à l'export.
#
# Tables exclues de l'export/import :
# ─── EXPORT / IMPORT ────────────────────────────────────────────────────────
#
# Portées disponibles :
#   scope=user   → tous les clients dont l'user est propriétaire + tables globales
#   scope=client → uniquement le client actif
#
# Les nouvelles tables avec colonne client_id sont incluses automatiquement.

TABLES_PAR_CLIENT   = ['clients','parc_general','appareils','peripheriques',
                        'identifiants','services','types_droits','droits_utilisateurs',
                        'baie_slots','baie_photos','utilisateurs','historique']
TABLES_FK_APPAREILS = ['documents_appareils','contrats_appareils']
TABLES_FK_PERIPH    = ['documents_peripheriques','contrats_peripheriques']
TABLES_FK_CONTRAT   = ['documents_contrats']
TABLES_GLOBALES     = ['config','config_listes','outils','kb_categories','kb_articles']
TABLES_AUTH         = ['auth_users','client_partages']

def _table_columns(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()]

def _all_user_tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()]

def _build_export(conn, client_ids, uid, scope_label):
    """Construit le dict d'export filtré pour les client_ids donnés."""
    if not client_ids:
        client_ids = [-1]
    all_tables = set(_all_user_tables(conn))
    data = {'_version':3, '_exported_at':datetime.utcnow().isoformat(),
            '_app':'ParcInfo', '_scope':scope_label, 'tables':{}}

    ph = ','.join(['?' for _ in client_ids])

    # Tables filtrées — 'clients' par id, les autres par client_id
    for t in TABLES_PAR_CLIENT:
        if t not in all_tables: continue
        cols = _table_columns(conn, t)
        if t == 'clients':
            rows = conn.execute(f'SELECT * FROM clients WHERE id IN ({ph})', client_ids).fetchall()
        elif 'client_id' in cols:
            rows = conn.execute(f'SELECT * FROM {t} WHERE client_id IN ({ph})', client_ids).fetchall()
        else:
            rows = conn.execute(f'SELECT * FROM {t}').fetchall()
        data['tables'][t] = {'columns':cols, 'rows':[list(r) for r in rows]}

    # IDs dans le périmètre pour les FK
    app_ids  = [r[0] for r in conn.execute(f'SELECT id FROM appareils WHERE client_id IN ({ph})', client_ids).fetchall()]
    peri_ids = [r[0] for r in conn.execute(f'SELECT id FROM peripheriques WHERE client_id IN ({ph})', client_ids).fetchall()]
    ctr_ids  = [r[0] for r in conn.execute(f'SELECT id FROM contrats WHERE client_id IN ({ph})', client_ids).fetchall()]

    for t in TABLES_FK_APPAREILS:
        if t not in all_tables: continue
        cols = _table_columns(conn, t)
        fk   = 'appareil_id' if 'appareil_id' in cols else None
        rows = conn.execute(f'SELECT * FROM {t} WHERE {fk} IN ({",".join(["?"]*len(app_ids))})', app_ids).fetchall()                if fk and app_ids else []
        data['tables'][t] = {'columns':cols, 'rows':[list(r) for r in rows]}

    for t in TABLES_FK_PERIPH:
        if t not in all_tables: continue
        cols = _table_columns(conn, t)
        if t == 'documents_peripheriques' and peri_ids:
            rows = conn.execute(f'SELECT * FROM {t} WHERE peripherique_id IN ({",".join(["?"]*len(peri_ids))})', peri_ids).fetchall()
        elif t == 'contrats_peripheriques' and ctr_ids:
            rows = conn.execute(f'SELECT * FROM {t} WHERE contrat_id IN ({",".join(["?"]*len(ctr_ids))})', ctr_ids).fetchall()
        else: rows = []
        data['tables'][t] = {'columns':cols, 'rows':[list(r) for r in rows]}

    for t in TABLES_FK_CONTRAT:
        if t not in all_tables or not ctr_ids: continue
        cols = _table_columns(conn, t)
        rows = conn.execute(f'SELECT * FROM {t} WHERE contrat_id IN ({",".join(["?"]*len(ctr_ids))})', ctr_ids).fetchall()
        data['tables'][t] = {'columns':cols, 'rows':[list(r) for r in rows]}

    if 'contrats' in all_tables:
        cols = _table_columns(conn, 'contrats')
        rows = conn.execute(f'SELECT * FROM contrats WHERE client_id IN ({ph})', client_ids).fetchall()
        data['tables']['contrats'] = {'columns':cols, 'rows':[list(r) for r in rows]}

    for t in TABLES_GLOBALES:
        if t not in all_tables: continue
        cols = _table_columns(conn, t)
        rows = conn.execute(f'SELECT * FROM {t}').fetchall()
        data['tables'][t] = {'columns':cols, 'rows':[list(r) for r in rows]}

    # Nouvelles tables inconnues avec client_id — incluses automatiquement
    known = set(TABLES_PAR_CLIENT+TABLES_FK_APPAREILS+TABLES_FK_PERIPH+
                TABLES_FK_CONTRAT+TABLES_GLOBALES+TABLES_AUTH+['contrats'])
    for t in all_tables:
        if t in known: continue
        cols = _table_columns(conn, t)
        if 'client_id' in cols:
            rows = conn.execute(f'SELECT * FROM {t} WHERE client_id IN ({ph})', client_ids).fetchall()
            data['tables'][t] = {'columns':cols, 'rows':[list(r) for r in rows]}
    return data

def _get_doc_filenames(conn, client_ids):
    ph = ','.join(['?' for _ in client_ids])
    fnames = set()
    for t, col in [('documents_appareils','nom_fichier'),('documents_peripheriques','nom_fichier'),
                   ('documents_contrats','nom_fichier'),('baie_photos','nom_fichier')]:
        try:
            for r in conn.execute(f'SELECT {col} FROM {t} WHERE client_id IN ({ph})', client_ids).fetchall():
                if r[0]: fnames.add(r[0])
        except: pass
    return fnames

def _make_json_response(data, fname):
    out = _json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return app.response_class(out, mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename={fname}.json'})

def _make_zip_response(data, fname, file_names):
    json_str = _json.dumps(data, ensure_ascii=False, indent=2, default=str)
    buf = _io2.BytesIO()
    with _zipfile.ZipFile(buf, 'w', _zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f'{fname}/data.json', json_str)
        for fn in file_names:
            fp = os.path.join(UPLOAD_FOLDER, fn)
            if os.path.isfile(fp):
                zf.write(fp, f'{fname}/uploads/{fn}')
    buf.seek(0)
    return app.response_class(buf.read(), mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename={fname}.zip'})

def _export_client_ids_for_user(conn, uid):
    role = (conn.execute('SELECT role FROM auth_users WHERE id=?', (uid,)).fetchone() or ['user'])[0]
    if role == 'admin':
        return [r[0] for r in conn.execute('SELECT id FROM clients ORDER BY id').fetchall()]
    return [r[0] for r in conn.execute('SELECT id FROM clients WHERE auth_user_id=?', (uid,)).fetchall()]

@app.route('/export/global.json')
def export_global_json():
    uid   = session.get('auth_user_id')
    scope = request.args.get('scope', 'user')
    cid   = get_client_id()
    conn  = get_db()
    from datetime import date as _date
    today = _date.today().isoformat()
    if scope == 'client' and cid:
        cl    = row_to_dict(conn.execute('SELECT nom FROM clients WHERE id=?', (cid,)).fetchone() or {})
        slug  = cl.get('nom','client').replace(' ','_')[:30]
        data  = _build_export(conn, [cid], uid, f'client:{cid}')
        fname = f'parcinfo_{slug}_{today}'
    else:
        cids  = _export_client_ids_for_user(conn, uid)
        data  = _build_export(conn, cids, uid, f'user:{uid}')
        fname = f'parcinfo_user_{today}'
    conn.close()
    return _make_json_response(data, fname)

@app.route('/export/global.zip')
def export_global_zip():
    uid   = session.get('auth_user_id')
    scope = request.args.get('scope', 'user')
    cid   = get_client_id()
    conn  = get_db()
    from datetime import date as _date
    today = _date.today().isoformat()
    if scope == 'client' and cid:
        cl     = row_to_dict(conn.execute('SELECT nom FROM clients WHERE id=?', (cid,)).fetchone() or {})
        slug   = cl.get('nom','client').replace(' ','_')[:30]
        data   = _build_export(conn, [cid], uid, f'client:{cid}')
        fnames = _get_doc_filenames(conn, [cid])
        fname  = f'parcinfo_{slug}_{today}'
    else:
        cids   = _export_client_ids_for_user(conn, uid)
        data   = _build_export(conn, cids, uid, f'user:{uid}')
        fnames = _get_doc_filenames(conn, cids)
        fname  = f'parcinfo_user_{today}'
    conn.close()
    return _make_zip_response(data, fname, fnames)

@app.route('/import/global', methods=['POST'])
def import_global():
    mode  = request.form.get('mode', 'merge')
    scope = request.form.get('scope', 'user')
    uid   = session.get('auth_user_id')
    cid   = get_client_id()
    if 'fichier' not in request.files:
        flash('Aucun fichier sélectionné', 'danger')
        return redirect(url_for('parc_general'))
    f_up  = request.files['fichier']
    fname = f_up.filename.lower()
    try:
        json_str = None
        if fname.endswith('.zip'):
            buf = _io2.BytesIO(f_up.read())
            with _zipfile.ZipFile(buf) as zf:
                jfiles = [n for n in zf.namelist() if n.endswith('/data.json') or n == 'data.json']
                if not jfiles:
                    flash('data.json introuvable dans le ZIP', 'danger')
                    return redirect(url_for('parc_general'))
                json_str = zf.read(jfiles[0]).decode('utf-8')
                os.makedirs(UPLOAD_FOLDER, exist_ok=True)
                for uf in zf.namelist():
                    if '/uploads/' in uf and not uf.endswith('/'):
                        fn = uf.split('/uploads/')[-1]
                        if fn:
                            with zf.open(uf) as src, open(os.path.join(UPLOAD_FOLDER, fn), 'wb') as dst:
                                dst.write(src.read())
        elif fname.endswith('.json'):
            json_str = f_up.read().decode('utf-8')
        else:
            flash('Format non supporté — utilisez .json ou .zip', 'danger')
            return redirect(url_for('parc_general'))

        data        = _json.loads(json_str)
        tables_data = data.get('tables', {})
        if not tables_data:
            flash('Fichier JSON invalide ou vide', 'danger')
            return redirect(url_for('parc_general'))

        conn = get_db()
        conn.execute('PRAGMA foreign_keys = OFF')

        if mode == 'reset':
            if scope == 'client' and cid:
                for t in reversed(TABLES_PAR_CLIENT):
                    if 'client_id' in _table_columns(conn, t):
                        try: conn.execute(f'DELETE FROM {t} WHERE client_id=?', (cid,))
                        except: pass
            else:
                my_cids = _export_client_ids_for_user(conn, uid)
                if my_cids:
                    ph = ','.join(['?' for _ in my_cids])
                    for t in reversed(TABLES_PAR_CLIENT):
                        if 'client_id' in _table_columns(conn, t):
                            try: conn.execute(f'DELETE FROM {t} WHERE client_id IN ({ph})', my_cids)
                            except: pass
                for t in TABLES_GLOBALES:
                    try: conn.execute(f'DELETE FROM {t}')
                    except: pass

        stats = {}
        for table, tdata in tables_data.items():
            cols = tdata.get('columns', [])
            rows = tdata.get('rows', [])
            if not cols or not rows: stats[table] = 0; continue
            try: conn.execute(f'SELECT 1 FROM {table} LIMIT 1')
            except: stats[table] = 0; continue
            inserted = 0
            for row in rows:
                if len(row) != len(cols): continue
                row_dict = dict(zip(cols, row))
                ph_v     = ','.join(['?' for _ in cols])
                col_str  = ','.join([f'"{c}"' for c in cols])
                if mode == 'merge':
                    pk     = row_dict.get('id') or row_dict.get('cle')
                    id_col = 'id' if 'id' in row_dict else ('cle' if 'cle' in row_dict else None)
                    if pk is not None and id_col:
                        if conn.execute(f'SELECT 1 FROM {table} WHERE {id_col}=?', (pk,)).fetchone():
                            continue
                try:
                    conn.execute(f'INSERT OR IGNORE INTO {table} ({col_str}) VALUES ({ph_v})', row)
                    inserted += 1
                except Exception: pass
            stats[table] = inserted

        conn.execute('PRAGMA foreign_keys = ON')
        conn.commit(); conn.close()
        total    = sum(stats.values())
        non_zero = {t:n for t,n in stats.items() if n>0}
        portee   = 'client courant' if scope == 'client' else 'tous vos clients'
        msg = f'Import {mode} ({portee}) — {total} entrée(s) importée(s)'
        if non_zero:
            detail = ', '.join(f'{t}:{n}' for t,n in list(non_zero.items())[:6])
            msg += f' ({detail}{"..." if len(non_zero)>6 else ""})'
        flash(msg, 'success')
    except Exception as e:
        flash(f"Erreur lors de l'import : {str(e)}", 'danger')
    return redirect(url_for('parc_general'))

# ─── AUTHENTIFICATION ─────────────────────────────────────────────────────────

@app.route('/login', methods=['GET','POST'])
def page_login():
    if session.get('auth_user_id'):
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        ip = request.remote_addr or '0.0.0.0'
        if not _check_rate_limit(ip):
            logger.warning('Login rate limit dépassé pour %s', ip)
            error = 'Trop de tentatives. Réessayez dans 5 minutes.'
            return render_template('login.html', error=error, next=request.args.get('next',''))
        login = request.form.get('login','').strip()
        pwd   = request.form.get('password','')
        conn  = get_db()
        u = row_to_dict(conn.execute(
            'SELECT * FROM auth_users WHERE login=? AND actif=1', (login,)).fetchone() or {})
        conn.close()
        ok, needs_rehash = _check_pwd(pwd, u.get('password_hash','')) if u else (False, False)
        if ok:
            _reset_attempts(ip)
            if needs_rehash:
                conn2 = get_db()
                conn2.execute('UPDATE auth_users SET password_hash=? WHERE id=?',
                              (_hash_pwd(pwd), u['id']))
                conn2.commit(); conn2.close()
            session['auth_user_id'] = u['id']
            session['auth_user_nom'] = (u.get('prenom','') + ' ' + u.get('nom','')).strip() or u['login']
            session['auth_user_role'] = u.get('role','user')
            session['login_time'] = datetime.utcnow().isoformat()
            from urllib.parse import urlparse
            raw_next = request.form.get('next') or request.args.get('next') or '/'
            parsed = urlparse(raw_next)
            next_url = raw_next if (not parsed.netloc and raw_next.startswith('/')) else '/'
            if u.get('must_change_password'):
                return redirect(url_for('page_profil'))
            return redirect(next_url)
        _record_failed_attempt(ip)
        error = 'Identifiants incorrects'
    return render_template('login.html', error=error,
                           next=request.args.get('next',''))

@app.route('/logout')
def page_logout():
    session.clear()
    return redirect(url_for('page_login'))

@app.route('/profil', methods=['GET','POST'])
def page_profil():
    u = get_auth_user()
    if not u:
        return redirect(url_for('page_login'))
    if request.method == 'POST':
        errs = validate_form([('email', 'email', False)], request.form)
        if errs:
            for e in errs: flash(e, 'danger')
            return redirect(url_for('page_profil'))
        conn = get_db()
        now  = datetime.utcnow().isoformat()
        nom  = request.form.get('nom','').strip()
        prenom = request.form.get('prenom','').strip()
        email  = request.form.get('email','').strip()
        pwd    = request.form.get('password','')
        pwd2   = request.form.get('password2','')
        # Logo upload
        logo_fichier = u.get('logo_fichier','')
        if 'logo' in request.files and request.files['logo'].filename:
            logo = request.files['logo']
            ext  = logo.filename.rsplit('.',1)[-1].lower() if '.' in logo.filename else 'png'
            fname = f"logo_user{u['id']}_{int(time.time())}.{ext}"
            logo.save(os.path.join(UPLOAD_FOLDER, fname))
            logo_fichier = fname
        if pwd:
            if pwd != pwd2:
                flash('Les mots de passe ne correspondent pas', 'danger')
                return redirect(url_for('page_profil'))
            conn.execute('UPDATE auth_users SET nom=?,prenom=?,email=?,password_hash=?,logo_fichier=?,must_change_password=0,date_maj=? WHERE id=?',
                (nom, prenom, email, _hash_pwd(pwd), logo_fichier, now, u['id']))
        else:
            if u.get('must_change_password'):
                flash('Vous devez définir un nouveau mot de passe.', 'danger')
                conn.close()
                return redirect(url_for('page_profil'))
            conn.execute('UPDATE auth_users SET nom=?,prenom=?,email=?,logo_fichier=?,date_maj=? WHERE id=?',
                (nom, prenom, email, logo_fichier, now, u['id']))
        conn.commit(); conn.close()
        session['auth_user_nom'] = (prenom + ' ' + nom).strip() or u['login']
        flash('Profil mis à jour', 'success')
        return redirect(url_for('page_profil'))
    return render_template('profil.html', u=u,
                           clients=get_clients(), client_actif_id=get_client_id())

# ─── ADMIN UTILISATEURS ───────────────────────────────────────────────────────

@app.route('/admin/utilisateurs')
def admin_utilisateurs():
    u = get_auth_user()
    if not u or u.get('role') != 'admin':
        flash('Acces reserve a l\'administrateur', 'danger')
        return redirect(url_for('index'))
    conn = get_db()
    users = [row_to_dict(r) for r in conn.execute(
        'SELECT * FROM auth_users ORDER BY role DESC, nom').fetchall()]
    conn.close()
    return render_template('admin_users.html', users=users,
                           clients=get_clients(), client_actif_id=get_client_id())

@app.route('/admin/utilisateur/nouveau', methods=['GET','POST'])
def admin_nouvel_utilisateur():
    u = get_auth_user()
    if not u or u.get('role') != 'admin':
        return redirect(url_for('index'))
    if request.method == 'POST':
        login  = request.form.get('login','').strip()
        pwd    = request.form.get('password','')
        nom    = request.form.get('nom','').strip()
        prenom = request.form.get('prenom','').strip()
        email  = request.form.get('email','').strip()
        role   = request.form.get('role','user')
        if not login or not pwd:
            flash('Login et mot de passe requis', 'danger')
            return redirect(request.url)
        errs = validate_form([('email', 'email', False)], request.form)
        if errs:
            for e in errs: flash(e, 'danger')
            return redirect(request.url)
        conn = get_db()
        exists = conn.execute('SELECT id FROM auth_users WHERE login=?', (login,)).fetchone()
        if exists:
            conn.close()
            flash('Ce login est déjà utilisé', 'danger')
            return redirect(request.url)
        now = datetime.utcnow().isoformat()
        conn.execute('INSERT INTO auth_users (login,password_hash,nom,prenom,email,role,actif,date_creation,date_maj) VALUES (?,?,?,?,?,?,1,?,?)',
            (login, _hash_pwd(pwd), nom, prenom, email, role, now, now))
        conn.commit(); conn.close()
        flash(f'Utilisateur {login} créé', 'success')
        return redirect(url_for('admin_utilisateurs'))
    return render_template('admin_user_form.html', edit_user=None,
                           clients=get_clients(), client_actif_id=get_client_id())

@app.route('/admin/utilisateur/<int:uid>/editer', methods=['GET','POST'])
def admin_editer_utilisateur(uid):
    current = get_auth_user()
    if not current or current.get('role') != 'admin':
        return redirect(url_for('index'))
    conn = get_db()
    edit_user = row_to_dict(conn.execute('SELECT * FROM auth_users WHERE id=?', (uid,)).fetchone() or {})
    if not edit_user:
        conn.close(); flash('Utilisateur introuvable', 'danger')
        return redirect(url_for('admin_utilisateurs'))
    if request.method == 'POST':
        errs = validate_form([('email', 'email', False)], request.form)
        if errs:
            for e in errs: flash(e, 'danger')
            return redirect(request.url)
        now  = datetime.utcnow().isoformat()
        nom    = request.form.get('nom','').strip()
        prenom = request.form.get('prenom','').strip()
        email  = request.form.get('email','').strip()
        role   = request.form.get('role','user')
        actif  = 1 if request.form.get('actif') else 0
        pwd    = request.form.get('password','')
        if pwd:
            conn.execute('UPDATE auth_users SET nom=?,prenom=?,email=?,role=?,actif=?,password_hash=?,date_maj=? WHERE id=?',
                (nom, prenom, email, role, actif, _hash_pwd(pwd), now, uid))
        else:
            conn.execute('UPDATE auth_users SET nom=?,prenom=?,email=?,role=?,actif=?,date_maj=? WHERE id=?',
                (nom, prenom, email, role, actif, now, uid))
        conn.commit(); conn.close()
        flash('Utilisateur mis à jour', 'success')
        return redirect(url_for('admin_utilisateurs'))
    conn.close()
    return render_template('admin_user_form.html', edit_user=edit_user,
                           clients=get_clients(), client_actif_id=get_client_id())

@app.route('/admin/utilisateur/<int:uid>/supprimer', methods=['POST'])
def admin_supprimer_utilisateur(uid):
    current = get_auth_user()
    if not current or current.get('role') != 'admin':
        return redirect(url_for('index'))
    if uid == current['id']:
        flash('Impossible de supprimer son propre compte', 'danger')
        return redirect(url_for('admin_utilisateurs'))
    conn = get_db()
    # Réattribuer les clients à l'admin
    admin = conn.execute("SELECT id FROM auth_users WHERE role='admin' AND id!=?", (uid,)).fetchone()
    if admin:
        conn.execute('UPDATE clients SET auth_user_id=? WHERE auth_user_id=?', (admin[0], uid))
    conn.execute('DELETE FROM client_partages WHERE auth_user_id=?', (uid,))
    conn.execute('DELETE FROM auth_users WHERE id=?', (uid,))
    conn.commit(); conn.close()
    flash('Utilisateur supprimé', 'info')
    return redirect(url_for('admin_utilisateurs'))


@app.route('/admin/email-config', methods=['GET','POST'])
def admin_email_config():
    user = get_auth_user()
    if not user or user.get('role') != 'admin':
        return redirect(url_for('index'))

    if request.method == 'POST':
        cfg_set('smtp_server', request.form.get('smtp_server', ''))
        cfg_set('smtp_port', request.form.get('smtp_port', '587'))
        cfg_set('smtp_login', request.form.get('smtp_login', ''))
        cfg_set('smtp_password', request.form.get('smtp_password', ''))
        cfg_set('from_email', request.form.get('from_email', ''))
        flash('Paramètres email sauvegardés', 'success')
        return redirect(url_for('admin_email_config'))

    return render_template('admin_email_config.html',
        smtp_server=cfg_get('smtp_server', ''),
        smtp_port=cfg_get('smtp_port', '587'),
        smtp_login=cfg_get('smtp_login', ''),
        from_email=cfg_get('from_email', ''))


# ─── PARTAGE DE CLIENTS ───────────────────────────────────────────────────────

@app.route('/client/<int:cid>/partager', methods=['GET','POST'])
def partager_client(cid):
    u = get_auth_user()
    if not u:
        return redirect(url_for('page_login'))
    if get_client_access(cid) != 'proprietaire':
        flash('Seul le propriétaire peut partager ce client', 'danger')
        return redirect(url_for('liste_clients'))
    conn = get_db()
    client = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
    partages = [row_to_dict(r) for r in conn.execute(
        'SELECT cp.*, au.login, au.nom, au.prenom FROM client_partages cp JOIN auth_users au ON cp.auth_user_id=au.id WHERE cp.client_id=?',
        (cid,)).fetchall()]
    all_users = [row_to_dict(r) for r in conn.execute(
        'SELECT id,login,nom,prenom FROM auth_users WHERE id!=? AND actif=1 ORDER BY nom',
        (u['id'],)).fetchall()]
    if request.method == 'POST':
        action = request.form.get('action')
        now = datetime.utcnow().isoformat()
        if action == 'ajouter':
            target_uid = request.form.get('user_id')
            niveau     = request.form.get('niveau','lecture')
            if target_uid:
                conn.execute('INSERT OR REPLACE INTO client_partages (client_id,auth_user_id,niveau,date_partage) VALUES (?,?,?,?)',
                    (cid, int(target_uid), niveau, now))
                conn.commit()
                flash('Partage ajouté', 'success')
        elif action == 'supprimer':
            partage_id = request.form.get('partage_id')
            if partage_id:
                conn.execute('DELETE FROM client_partages WHERE id=? AND client_id=?', (int(partage_id), cid))
                conn.commit()
                flash('Partage supprimé', 'info')
        conn.close()
        return redirect(url_for('partager_client', cid=cid))
    conn.close()
    return render_template('partage_client.html', client=client, partages=partages,
                           all_users=all_users, clients=get_clients(), client_actif_id=get_client_id())

@app.route('/user/logo/<path:filename>')
def user_logo(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ── TABLEAU DE BORD ──────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    return redirect(url_for('index'))


# ── CACHE STATS (Admin) ──────────────────────────────────────────────────────

@app.route('/api/cache/stats')
@login_required
def cache_stats():
    """Retourne les statistiques du cache."""
    user = get_auth_user()
    if user['role'] != 'admin':
        return jsonify({'error': 'Admin only'}), 403

    cache_mgr = get_cache_manager()
    stats = cache_mgr.stats()

    return jsonify({
        'entries': stats['entries'],
        'total_hits': stats['total_hits'],
        'avg_hits_per_entry': round(stats['avg_hits'], 2),
        'message': f"✅ Cache: {stats['entries']} entrées, {stats['total_hits']} hits"
    })


@app.route('/api/cache/invalidate', methods=['POST'])
@login_required
def cache_invalidate():
    """Invalide le cache (Admin uniquement)."""
    user = get_auth_user()
    if user['role'] != 'admin':
        return jsonify({'error': 'Admin only'}), 403

    pattern = request.form.get('pattern', '')
    invalidate_cache_pattern(pattern)

    return jsonify({'ok': True, 'message': f'Cache invalidé: {pattern or "tout"}'})


# ── RECHERCHE FULL-TEXT ET AUTOCOMPLETE ──────────────────────────────────────

@app.route('/api/search')
@login_required
def api_search():
    """Recherche globale multi-entités."""
    query = request.args.get('q', '').strip()
    client_id = get_client_id()
    limit = min(int(request.args.get('limit', 20)), 100)

    if not query or len(query) < 2:
        return jsonify({
            'appareils': [],
            'contrats': [],
            'utilisateurs': [],
            'services': [],
            'peripheriques': [],
            'identifiants': [],
            'total': 0,
            'query': query
        })

    try:
        results = search_global(query, client_id, limit)
        return jsonify(results)
    except Exception as e:
        logger.exception(f"Search error for query='{query}'")
        return jsonify({'error': str(e)}), 500


@app.route('/api/autocomplete/<entity_type>')
@login_required
def api_autocomplete(entity_type):
    """Autocomplete pour un type d'entité spécifique."""
    query = request.args.get('q', '').strip()
    client_id = get_client_id()
    limit = min(int(request.args.get('limit', 10)), 50)

    if not query or len(query) < 1:
        return jsonify([])

    try:
        results = search_autocomplete(query, client_id, entity_type, limit)
        return jsonify(results)
    except Exception as e:
        logger.exception(f"Autocomplete error for entity_type='{entity_type}', query='{query}'")
        return jsonify({'error': str(e)}), 500


# ── GESTIONNAIRES D'ERREURS ──────────────────────────────────────────────────

@app.errorhandler(404)
def page_not_found(e):
    cid = get_client_id()
    return render_template('404.html',
        clients=get_clients(), client_actif_id=cid,
        client=row_to_dict(get_db().execute('SELECT * FROM clients WHERE id=?',(cid,)).fetchone() or {}) if cid else {}
    ), 404

@app.errorhandler(500)
def internal_error(e):
    import traceback as _tb
    try:
        cid = session.get('client_id')
        if cid:
            from database import get_local_db
            _conn = get_local_db()
            log_error(_conn, int(cid), request.url, e, _tb.format_exc())
            _conn.commit(); _conn.close()
    except Exception:
        pass
    cid = get_client_id()
    return render_template('500.html',
        clients=get_clients() if cid else [], client_actif_id=cid,
        client={}
    ), 500


@app.errorhandler(Exception)
def handle_unhandled_exception(e):
    """Capture les exceptions non gérées, les logue et retourne une 500."""
    import traceback as _tb
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e   # Laisser Flask gérer les erreurs HTTP normales (404, 403…)
    try:
        cid = session.get('client_id')
        if cid:
            from database import get_local_db
            _conn = get_local_db()
            log_error(_conn, int(cid), request.url, e, _tb.format_exc())
            _conn.commit(); _conn.close()
    except Exception:
        pass
    logger.exception('Exception non gérée sur %s %s', request.method, request.url)
    cid = get_client_id()
    return render_template('500.html',
        clients=get_clients() if cid else [], client_actif_id=cid,
        client={}
    ), 500

if __name__ == '__main__':
    init_db()

    # Démarrer le scheduler pour les cron jobs
    # Cron job: régénérer les occurrences maintenances tous les jours à 2h du matin
    scheduler.add_job(_regenerate_all_maintenance_occurrences, 'cron', hour=2, minute=0)
    # Cron job: notifier maintenances à venir tous les jours à 8h du matin
    scheduler.add_job(_notify_upcoming_maintenances, 'cron', hour=8, minute=0)
    scheduler.start()
    logger.info("Cron scheduler démarré (régénération à 02:00, notifications à 08:00)")

    # Précharger la base OUI en arrière-plan pour ne pas bloquer le démarrage
    threading.Thread(target=_oui_load_full, daemon=True).start()
    # Lancer la synchronisation des uploads (local ↔ Turso)
    start_sync_thread(interval=60)
    print("="*50)
    print("  ParcInfo Multi-Clients")
    print(f"  OS : {platform.system()} | DB : {DB_PATH}")
    print("  URL : http://localhost:5000")
    print("="*50)

    if not os.environ.get('RUNNING_IN_DOCKER'):
        import webbrowser
        def _open_browser():
            import time; time.sleep(1.5)
            webbrowser.open('http://localhost:5000')
        threading.Thread(target=_open_browser, daemon=True).start()

    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug, host='0.0.0.0', port=5000)
