"""
config_helpers.py — Gestion de la configuration applicative et des listes personnalisables.
"""
import threading, logging, time, sqlite3
from datetime import datetime

logger = logging.getLogger('parcinfo')

# ─── PARAMÈTRES PERSONNELS vs GLOBAUX ──────────────────────────────────────────
# Les clés listées ici seront stockées dans user_preferences (par utilisateur)
# Toutes les autres sont dans config (globales/partagées)
PERSONAL_CONFIG_KEYS = {
    'mode',              # dark/light
    'accent_color',      # Couleur accent
    'accent_green',      # Couleur verte
    'accent_red',        # Couleur rouge
    'accent_orange',     # Couleur orange
    'afficher_mac',      # Afficher adresses MAC
    'afficher_dernier_ping',  # Afficher dernier ping
    'lignes_par_page',   # Pagination
    'date_format',       # Format de date
    'nav_mode',          # Menu horizontal/vertical
    'theme',             # Thème prédéfini
    'contrast_level',    # Niveau de contraste (normal/high/max)
}


def _execute_with_retry(func, max_retries=5, retry_delay=0.05):
    """
    Exécute une fonction avec retry exponentiel en cas de verrou BD.
    Gère sqlite3.OperationalError ('locked').
    """
    for attempt in range(max_retries):
        try:
            return func()
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and attempt < max_retries - 1:
                time.sleep(retry_delay * (2 ** attempt))
                continue
            raise
        except Exception:
            raise
    # Fallback (shouldn't happen)
    raise RuntimeError(f'Failed after {max_retries} retries')

# ─── LISTES PAR DÉFAUT ────────────────────────────────────────────────────────

LISTE_DEFAULTS = {
    'types_appareils': [
        'PC', 'PC (Windows)', 'PC/Serveur (Linux)', 'Laptop', 'MacBook',
        'Serveur', 'Imprimante', 'Switch', 'Switch/AP', 'Routeur/Pare-feu',
        'NAS', 'Telephone IP', 'Tablette', 'Camera IP', 'Borne Wi-Fi', 'Autre'
    ],
    'categories_peripheriques': [
        'Ecran', 'Clavier', 'Souris', 'Webcam', 'Casque / Micro', 'Haut-parleurs',
        'Imprimante', 'Scanner', 'Imprimante multifonction',
        'Onduleur / UPS', 'Multiprise parafoudre',
        'Disque dur externe', 'Cle USB', 'Hub USB', 'Lecteur de cartes',
        'Docking station', 'Adaptateur reseau', 'Switch USB',
        'Telephone fixe IP', 'Telephone mobile',
        'Badge / Lecteur de badge', 'Autre'
    ],
    'types_contrats': [
        'Maintenance materiel', 'Support & assistance', 'Infogérance',
        'Abonnement logiciel (SaaS)', 'Licence logicielle', 'Abonnement cloud',
        'Contrat operateur (telecom)', 'Contrat internet / fibre',
        'Abonnement securite / antivirus', 'Contrat de leasing', 'Location',
        'Garantie etendue', 'Contrat energie', 'Autre'
    ],
    'categories_identifiants': [
        'Administration réseau', 'Serveur / NAS', 'Messagerie', 'Logiciel métier',
        'Cloud / SaaS', 'Site web', 'Accès VPN', 'Base de données',
        "Système d'exploitation", 'Firewall / Routeur', 'Fournisseur / Opérateur',
        'Wi-Fi', 'Autre'
    ],
    'marques_antivirus': [
        'Windows Defender', 'Bitdefender', 'ESET', 'Kaspersky', 'Norton / NortonLifeLock',
        'Sophos', 'Trend Micro', 'Malwarebytes', 'Symantec / Broadcom', 'McAfee / Trellix',
        'Avast', 'AVG', 'Panda Security', 'CrowdStrike', 'SentinelOne',
        'Webroot', 'F-Secure', 'G DATA', 'Autre',
    ],
    'noms_antivirus': [
        'Windows Defender / Microsoft Defender',
        'Bitdefender Total Security', 'Bitdefender Endpoint Security Tools', 'Bitdefender GravityZone',
        'ESET NOD32 Antivirus', 'ESET Endpoint Antivirus', 'ESET Endpoint Security', 'ESET PROTECT',
        'Kaspersky Endpoint Security', 'Kaspersky Internet Security', 'Kaspersky Small Office Security',
        'Norton 360', 'Norton Small Business', 'Norton Endpoint Security',
        'Sophos Endpoint', 'Sophos Intercept X', 'Sophos Central',
        'Trend Micro Worry-Free Business Security', 'Trend Micro Apex One',
        'Malwarebytes Premium', 'Malwarebytes Endpoint Protection', 'Malwarebytes ThreatDown',
        'McAfee Endpoint Security', 'McAfee Total Protection',
        'Avast Business Antivirus', 'Avast One',
        'AVG Internet Security Business Edition',
        'CrowdStrike Falcon', 'SentinelOne Singularity',
        'Webroot Business Endpoint Protection', 'F-Secure Elements', 'Autre',
    ],
}

# ─── VALEURS PAR DÉFAUT DE LA CONFIG ─────────────────────────────────────────

CFG_DEFAULTS = {
    'theme': 'dark-blue',
    'accent_color': '#00c9ff',
    'accent_green': '#00ff88',
    'accent_red': '#ff3355',
    'accent_orange': '#ff8c00',
    'ping_interval': '60',
    'ping_workers': '30',
    'ping_timeout': '1.0',
    'scan_workers': '50',
    'scan_ports': '21,22,23,25,53,80,110,135,139,143,443,445,631,3389,5900,8080,8443,9100',
    'port_color_ssh': '#00ff88',
    'port_icon_ssh': '⌨',
    'port_color_http': '#00c9ff',
    'port_icon_http': '🌐',
    'port_color_https': '#00c9ff',
    'port_icon_https': '🔒',
    'port_color_rdp': '#c084fc',
    'port_icon_rdp': '🖥',
    'port_color_ftp': '#ff8c00',
    'port_icon_ftp': '📁',
    'port_color_smb': '#facc15',
    'port_icon_smb': '🗂',
    'port_color_print': '#fb923c',
    'port_icon_print': '🖨',
    'port_color_telnet': '#ff3355',
    'port_icon_telnet': '⚠',
    'port_color_other': '#64748b',
    'port_icon_other': '◈',
    'periph_color_ecran': '#22d3ee',
    'periph_color_clavier': '#a78bfa',
    'periph_color_souris': '#a78bfa',
    'periph_color_webcam': '#fb923c',
    'periph_color_casque': '#c084fc',
    'periph_color_imprimante': '#f97316',
    'periph_color_onduleur': '#facc15',
    'periph_color_stockage': '#4ade80',
    'periph_color_dock': '#60a5fa',
    'periph_color_tel': '#34d399',
    'periph_color_usb': '#94a3b8',
    'periph_color_reseau': '#2dd4bf',
    'periph_color_badge': '#f87171',
    'periph_color_autre': '#94a3b8',
    # ── Couleurs des types d'appareils ──────────────────────────────────────
    'type_color_pc':         '#00c9ff',
    'type_color_linux':      '#4ade80',
    'type_color_laptop':     '#60a5fa',
    'type_color_mac':        '#e2e8f0',
    'type_color_serveur':    '#c084fc',
    'type_color_imprimante': '#f97316',
    'type_color_switch':     '#facc15',
    'type_color_routeur':    '#ff3355',
    'type_color_nas':        '#4ade80',
    'type_color_tel':        '#34d399',
    'type_color_tablette':   '#a78bfa',
    'type_color_camera':     '#fb923c',
    'type_color_wifi':       '#2dd4bf',
    'type_color_autre':      '#94a3b8',
    # ── Labels courts des badges de types d'appareils (≤3 chars) ───────────────
    'type_badge_pc':         'PC',
    'type_badge_linux':      'LNX',
    'type_badge_laptop':     'LAP',
    'type_badge_mac':        'MAC',
    'type_badge_serveur':    'SRV',
    'type_badge_imprimante': 'IMP',
    'type_badge_switch':     'SW',
    'type_badge_routeur':    'RTR',
    'type_badge_nas':        'NAS',
    'type_badge_tel':        'TEL',
    'type_badge_tablette':   'TAB',
    'type_badge_camera':     'CAM',
    'type_badge_wifi':       'WIF',
    'type_badge_autre':      'AUT',
    # ── Configuration personnalisée des ports scannés (noms service ≤8 chars) ─
    'port_21_name':  'FTP',
    'port_22_name':  'SSH',
    'port_23_name':  'TELNET',
    'port_25_name':  'SMTP',
    'port_53_name':  'DNS',
    'port_80_name':  'HTTP',
    'port_110_name': 'POP3',
    'port_135_name': 'RPC',
    'port_139_name': 'NBIOS',
    'port_143_name': 'IMAP',
    'port_443_name': 'HTTPS',
    'port_445_name': 'SMB',
    'port_631_name': 'IPP',
    'port_3389_name':'RDP',
    'port_5900_name':'VNC',
    'port_8080_name':'HTTP8',
    'port_8443_name':'HTTPS8',
    'port_9100_name':'PRINT',
    'garantie_alerte_jours': '90',
    'lignes_par_page': '50',
    'historique_max_lignes': '500',
    'afficher_dernier_ping': '1',
    'afficher_mac': '1',
    'date_format': 'dd/mm/yyyy',
    'entreprise_nom': '',
    'entreprise_logo_url': '',
    'mode': 'dark',
    'nav_mode': 'horizontal',
    'contrast_level': 'normal',
    'db_type': 'local',
    'turso_url': '',
    'turso_token': '',
    'db_sync_interval': '30',
    # ── Navigation preferences ────────────────────────────────────────────────────
    'show_breadcrumb': '1',       # Show breadcrumb navigation (personal preference)
    'show_back_button': '1',      # Show "Back to overview" button (personal preference)
    'confirm_client_switch': '1', # Show confirmation modal before switching clients (personal preference)
    # ── Interventions ────────────────────────────────────────────────────────
    'types_interventions': '["maintenance préventive", "maintenance corrective", "dépannage", "installation", "upgrade", "support", "autre"]',
}

# Cache avec limite de taille (LRU-like) pour éviter memory leak
from collections import OrderedDict
_cfg_cache: OrderedDict = OrderedDict()
_cfg_lock = threading.Lock()
_CFG_CACHE_MAX_SIZE = 500  # Maximum d'entrées en cache

def _cfg_cache_set_unlocked(key: str, value) -> None:
    """Ajoute une clé au cache avec éviction FIFO si dépassement de taille.
    MUST be called within a _cfg_lock context to avoid deadlock."""
    if len(_cfg_cache) >= _CFG_CACHE_MAX_SIZE:
        # Supprimer la clé la plus ancienne (FIFO)
        _cfg_cache.popitem(last=False)
    _cfg_cache[key] = value


def get_liste(nom: str) -> list:
    """Retourne la liste personnalisée depuis la DB.
    Si aucune entrée en base (liste jamais personnalisée), retourne les valeurs par défaut.
    Une fois la liste initialisée en DB, on retourne exactement ce qui est en base —
    sans jamais ré-injecter les defaults manquants, ce qui annulerait les suppressions."""
    from database import get_local_db
    defaults = list(LISTE_DEFAULTS.get(nom, []))
    try:
        conn = get_local_db()  # ← Toujours utiliser le DB local (PRAGMA busy_timeout)
        rows = conn.execute(
            'SELECT valeur FROM config_listes WHERE nom_liste=? ORDER BY ordre, valeur',
            (nom,)).fetchall()
        conn.close()
        if rows:
            # Liste déjà personnalisée : on retourne exactement le contenu de la DB
            return [r[0] for r in rows]
    except Exception:
        logger.exception('Erreur get_liste(%s)', nom)
    # Liste jamais initialisée en DB : on retourne les defaults en mémoire (sans les écrire)
    return defaults


def cfg_get(cle: str, default=None, auth_user_id=None):
    """
    Récupère une configuration.
    Si auth_user_id est fourni, cherche d'abord dans user_preferences, puis fallback config.
    Sinon, cherche uniquement dans config (configurations globales).
    """
    from database import get_local_db

    cache_key = f"{cle}#{auth_user_id}" if auth_user_id else cle
    with _cfg_lock:
        if cache_key in _cfg_cache:
            return _cfg_cache[cache_key]

    try:
        conn = get_local_db()
        val = None

        # Si utilisateur spécifié et c'est une clé personnelle, chercher dans user_preferences
        if auth_user_id and cle in PERSONAL_CONFIG_KEYS:
            row = conn.execute(
                'SELECT valeur FROM user_preferences WHERE auth_user_id=? AND cle=?',
                (auth_user_id, cle)
            ).fetchone()
            if row:
                val = row[0]

        # Fallback vers config globale
        if val is None:
            row = conn.execute('SELECT valeur FROM config WHERE cle=?', (cle,)).fetchone()
            val = row[0] if row else None

        conn.close()
        val = val if val is not None else (default if default is not None else CFG_DEFAULTS.get(cle, ''))
    except Exception:
        logger.exception('Erreur cfg_get(%s, user=%s)', cle, auth_user_id)
        val = default if default is not None else CFG_DEFAULTS.get(cle, '')

    with _cfg_lock:
        _cfg_cache_set_unlocked(cache_key, val)
    return val


def cfg_set(cle: str, valeur, auth_user_id=None):
    """
    Sauvegarde une configuration.
    Si auth_user_id est fourni et c'est une clé personnelle, sauvegarde dans user_preferences.
    Sinon, sauvegarde dans config (configurations globales).
    """
    from database import get_local_db
    now = datetime.now().isoformat()

    def _do_set():
        conn = get_local_db()
        try:
            # Si c'est une clé personnelle et utilisateur spécifié, sauvegarder dans user_preferences
            if auth_user_id and cle in PERSONAL_CONFIG_KEYS:
                conn.execute(
                    'INSERT OR REPLACE INTO user_preferences (auth_user_id,cle,valeur,date_maj) VALUES (?,?,?,?)',
                    (auth_user_id, cle, str(valeur), now)
                )
            else:
                # Sinon, sauvegarder dans config globale
                conn.execute(
                    'INSERT OR REPLACE INTO config (cle,valeur,date_maj) VALUES (?,?,?)',
                    (cle, str(valeur), now)
                )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    _execute_with_retry(_do_set)
    cache_key = f"{cle}#{auth_user_id}" if auth_user_id else cle
    with _cfg_lock:
        _cfg_cache_set_unlocked(cache_key, str(valeur))


def cfg_set_batch(config_dict: dict, auth_user_id=None) -> None:
    """
    Sauvegarde plusieurs configurations en une SEULE transaction.
    Beaucoup plus rapide que cfg_set() appelée N fois.

    Si auth_user_id est fourni, sépare les clés personnelles vs globales.
    Utilisation:
        cfg_set_batch({'mode': 'dark', 'port_22_color': '#00ff88'}, auth_user_id=1)
    """
    from database import get_local_db
    if not config_dict:
        return

    now = datetime.now().isoformat()

    def _do_batch_set():
        conn = get_local_db()
        try:
            # Séparer les clés personnelles et globales
            personal_config = {}
            global_config = {}

            for cle, valeur in config_dict.items():
                if auth_user_id and cle in PERSONAL_CONFIG_KEYS:
                    personal_config[cle] = str(valeur)
                else:
                    global_config[cle] = str(valeur)

            # Insérer dans user_preferences
            for cle, valeur in personal_config.items():
                conn.execute(
                    'INSERT OR REPLACE INTO user_preferences (auth_user_id,cle,valeur,date_maj) VALUES (?,?,?,?)',
                    (auth_user_id, cle, valeur, now)
                )

            # Insérer dans config global
            for cle, valeur in global_config.items():
                conn.execute('INSERT OR REPLACE INTO config (cle,valeur,date_maj) VALUES (?,?,?)',
                           (cle, valeur, now))

            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    _execute_with_retry(_do_batch_set)

    # Mettre à jour le cache
    with _cfg_lock:
        for cle, valeur in config_dict.items():
            cache_key = f"{cle}#{auth_user_id}" if auth_user_id and cle in PERSONAL_CONFIG_KEYS else cle
            _cfg_cache_set_unlocked(cache_key, str(valeur))


def cfg_all(auth_user_id=None) -> dict:
    """
    Retourne TOUTES les configurations.
    Si auth_user_id est fourni, fusionne préférences personnelles + config globale.
    """
    from database import get_local_db

    def _do_fetch():
        conn = get_local_db()
        try:
            result = dict(CFG_DEFAULTS)

            # Charger les configurations globales
            rows = conn.execute('SELECT cle,valeur FROM config').fetchall()
            result.update({r[0]: r[1] for r in rows})

            # Si utilisateur spécifié, charger et fusionner ses préférences personnelles
            if auth_user_id:
                rows = conn.execute(
                    'SELECT cle,valeur FROM user_preferences WHERE auth_user_id=?',
                    (auth_user_id,)
                ).fetchall()
                result.update({r[0]: r[1] for r in rows})  # Les préférences perso écrasent les globales

            return result
        finally:
            try:
                conn.close()
            except Exception:
                pass

    try:
        return _execute_with_retry(_do_fetch)
    except Exception:
        logger.exception('Erreur cfg_all après retries')
        return dict(CFG_DEFAULTS)


def cfg_invalidate():
    with _cfg_lock:
        _cfg_cache.clear()


def get_port_config(port: int) -> dict:
    """
    Retourne la configuration personnalisée pour un port (couleur + nom service + icône).
    Returns: {'color': '#00ff88', 'name': 'SSH', 'icon': '⌨', 'port': 22, 'service_type': 'ssh'}

    Stratégie de lookup:
    1. Clés par numéro de port: port_<num>_color, port_<num>_icon (nouvelles, uniques par port)
    2. Clés par serviceType: port_color_<type>, port_icon_<type> (anciennes, partagées par type)
    3. Defaults CFG_DEFAULTS
    """
    # Mapping défaut port → clé service (pour retrouver le nom de service)
    _DEFAULT_PORT_NAMES = {
        21: 'ftp', 22: 'ssh', 23: 'telnet', 25: 'smtp', 53: 'dns',
        80: 'http', 110: 'pop3', 135: 'rpc', 139: 'nbios', 143: 'imap',
        443: 'https', 445: 'smb', 631: 'ipp', 3389: 'rdp',
        5900: 'vnc', 8080: 'http8080', 8443: 'https8443', 9100: 'print',
    }

    service_type = _DEFAULT_PORT_NAMES.get(port, 'other')

    # Clés par numéro de port (nouvelles, évite les collisions)
    color_key_port = f'port_{port}_color'
    icon_key_port = f'port_{port}_icon'
    name_key = f'port_{port}_name'
    desc_key = f'port_{port}_description'

    # Clés par serviceType (anciennes, fallback)
    color_key_type = f'port_color_{service_type}'
    icon_key_type = f'port_icon_{service_type}'

    # Lookup avec fallback: port-spécifique → type-spécifique → defaults
    color = cfg_get(color_key_port) or cfg_get(color_key_type) or CFG_DEFAULTS.get(color_key_type, '#64748b')
    icon = cfg_get(icon_key_port) or cfg_get(icon_key_type) or CFG_DEFAULTS.get(icon_key_type, '◈')
    name = cfg_get(name_key) or CFG_DEFAULTS.get(name_key, str(port))
    desc = cfg_get(desc_key) or ''

    # Limiter à 8 chars max
    name = str(name)[:8] if name else str(port)
    icon = str(icon)[:2] if icon else '◈'  # Limiter à 2 chars (pour les émojis)
    desc = str(desc)[:64] if desc else ''

    return {
        'port': port,
        'name': name,
        'color': color,
        'icon': icon,
        'description': desc,
        'service_type': service_type
    }


def get_port_icon(port: int) -> str:
    """Retourne uniquement l'icône du port."""
    return get_port_config(port).get('icon', '◈')
