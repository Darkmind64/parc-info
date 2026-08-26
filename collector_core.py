#!/usr/bin/env python3
"""
Noyau de collecte partagé par les deux collecteurs ParcInfo.

`system-info-collector.py` (CLI) et `system-info-collector-gui.py` (GUI) étaient
auparavant deux copies de la même logique de collecte, et elles avaient déjà
divergé (le GUI avait perdu le support pkgutil/pacman). Tout le code de collecte,
de génération de rapport et d'appel API vit désormais ici ; les deux scripts ne
gardent que leur interface (argparse d'un côté, tkinter de l'autre).

Aucune dépendance externe n'est requise pour la collecte (ctypes / winreg /
PowerShell sur Windows, outils système sur macOS et Linux). reportlab n'est
nécessaire que pour le rapport PDF, avec repli automatique sur HTML.
"""

import concurrent.futures
import io
import json
import os
import platform
import plistlib
import re
import shutil
import socket
import string
import struct
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

__all__ = [
    'IS_WINDOWS', 'IS_MAC', 'IS_LINUX',
    'collect_system_info', 'build_summary_lines', 'build_summary_sections',
    'generate_pdf_report', 'generate_html_report',
    'get_api_payload', 'send_to_parcinfo', 'upload_report_to_parcinfo',
    'fetch_clients', 'is_elevated', 'get_mac_address', 'get_all_mac_addresses',
    'discover_parcinfo_mdns', 'scan_network_for_parcinfo', 'get_local_network_range',
    'get_wifi_profiles', 'send_wifi_credentials_to_parcinfo',
    'get_public_ip_info', 'get_dns_check_info', 'get_router_info',
]

# Platform detection
IS_WINDOWS = sys.platform == 'win32'
IS_MAC = sys.platform == 'darwin'
IS_LINUX = sys.platform == 'linux'

COLLECTOR_VERSION = '3.16'


def _utcnow() -> datetime:
    """Équivalent de _utcnow() (dépréciée depuis 3.12), même valeur
    naïve en UTC — voir app.py pour le même helper."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ════════════════════════════════════════════════════════════════════════════
# TABLES DE CORRESPONDANCE (codes SMBIOS / WMI → libellés lisibles)
# ════════════════════════════════════════════════════════════════════════════

# Win32_SystemEnclosure.ChassisTypes
CHASSIS_TYPES = {
    1: 'Autre', 2: 'Inconnu', 3: 'Desktop', 4: 'Desktop faible encombrement',
    5: 'Pizza Box', 6: 'Mini tour', 7: 'Tour', 8: 'Portable', 9: 'Laptop',
    10: 'Notebook', 11: 'Ordinateur de poche', 12: 'Station d\'accueil',
    13: 'Tout-en-un', 14: 'Sub-notebook', 15: 'Compact', 16: 'Lunch Box',
    17: 'Châssis principal', 18: 'Châssis d\'extension', 19: 'Sous-châssis',
    20: 'Châssis d\'extension de bus', 21: 'Châssis de périphériques',
    22: 'Châssis de stockage', 23: 'Châssis rackable', 24: 'PC scellé',
    28: 'Blade', 29: 'Blade Enclosure', 30: 'Tablette', 31: 'Convertible',
    32: 'Détachable', 33: 'IoT Gateway', 34: 'Mini PC', 35: 'Stick PC',
}
# Châssis correspondant à une machine transportable (→ type_appareil "Laptop")
CHASSIS_PORTABLE = {8, 9, 10, 11, 14}
CHASSIS_TABLET = {30, 31, 32}

# Win32_PhysicalMemory.SMBIOSMemoryType
MEMORY_TYPES = {
    20: 'DDR', 21: 'DDR2', 22: 'DDR2 FB-DIMM', 24: 'DDR3', 25: 'FBD2',
    26: 'DDR4', 27: 'LPDDR', 28: 'LPDDR2', 29: 'LPDDR3', 30: 'LPDDR4',
    34: 'DDR5', 35: 'LPDDR5',
}

# Win32_PhysicalMemory.FormFactor
MEMORY_FORM_FACTORS = {8: 'DIMM', 12: 'SODIMM', 13: 'SRIMM', 15: 'RIMM'}

# SoftwareLicensingProduct.LicenseStatus
LICENSE_STATUS = {
    0: 'Non licencié', 1: 'Activé', 2: 'Délai de grâce initial',
    3: 'Délai de grâce supplémentaire', 4: 'Délai de grâce (non authentique)',
    5: 'Notification', 6: 'Délai de grâce étendu',
}

# Version macOS → nom marketing. `sw_vers -productVersion` ne donne que le
# numéro (« 15.5 ») ; Apple n'expose ce nom nulle part de façon
# programmatique et stable — table tenue à la main, mise à jour à chaque
# version majeure. Clé = version majeure seule pour Big Sur (11) et après ;
# (10, mineure) pour les versions 10.x, qui changeaient de nom chaque année
# sous le même numéro majeur. Une version absente de la table (plus récente
# que la dernière mise à jour de ce fichier, ou 10.x très ancienne) retombe
# simplement sur le numéro brut plutôt que d'échouer — voir get_os_info().
_MACOS_CODENAMES = {
    26: 'Tahoe', 15: 'Sequoia', 14: 'Sonoma', 13: 'Ventura', 12: 'Monterey',
    11: 'Big Sur',
    (10, 15): 'Catalina', (10, 14): 'Mojave', (10, 13): 'High Sierra',
}


# ════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _as_list(value):
    """Normalise une valeur PowerShell en liste.

    ConvertTo-Json sérialise un tableau d'un seul élément comme un objet nu :
    sans cette normalisation, un poste à une seule barrette / un seul écran
    ferait échouer les boucles côté Python.
    """
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _clean(value):
    """Nettoie une chaîne WMI (None, espaces, valeurs de remplissage OEM)."""
    if value is None:
        return ''
    text = str(value).strip()
    # Les OEM laissent souvent ces valeurs de gabarit dans le SMBIOS
    placeholders = {
        'to be filled by o.e.m.', 'default string', 'system manufacturer',
        'system product name', 'not specified', 'none', 'not available',
        'o.e.m.', 'unknown', '<bad index>',
    }
    return '' if text.lower() in placeholders else text


# Ports et pilotes des imprimantes virtuelles (PDF, XPS, fax, OneNote, RMM…).
# Sans ce filtre, un poste Windows standard injecte 6 à 8 fausses imprimantes
# dans l'inventaire des périphériques à chaque collecte.
_VIRTUAL_PRINTER_PORTS = {'portprompt:', 'nul:', 'shrfax:', 'ad_port', 'file:'}
_VIRTUAL_PRINTER_HINTS = (
    'xps document writer', 'print to pdf', 'onenote', 'fax', 'pdf converter',
    'send to microsoft', 'anydesk', 'pdfcreator', 'cutepdf', 'foxit reader pdf',
    'microsoft software printer driver', 'remote desktop easy print',
    # macOS : CUPS-PDF (module courant d'impression vers fichier) et son
    # pilote apparaissent sous des noms variables selon l'installation.
    'cups-pdf', 'pdf writer',
)


# Préfixes de port identifiant un raccordement local par câble. ESDPRT est le
# moniteur de port Epson, DOT4 celui de HP : tous deux sont des ports USB.
_USB_PRINTER_PORTS = ('usb', 'esdprt', 'dot4', 'usbprint')
_LOCAL_PRINTER_PORTS = ('lpt', 'com')


def printer_connection(port, network=False):
    """Déduit le raccordement d'une imprimante depuis son port.

    `Win32_Printer.Network` n'est pas fiable : une imprimante WSD parfaitement
    réseau y est rapportée comme non-réseau. Le nom du port, lui, est explicite.
    """
    p = (port or '').lower()
    if p.startswith(_USB_PRINTER_PORTS):
        return 'USB'
    if p.startswith('wsd-') or p.startswith('ip_') or p.startswith('\\\\'):
        return 'Réseau'
    if re.match(r'^\d{1,3}(\.\d{1,3}){3}', p):
        return 'Réseau'
    if p.startswith(_LOCAL_PRINTER_PORTS):
        return 'Local'
    if network:
        return 'Réseau'
    return ''


def is_virtual_printer(name, driver, port):
    """Distingue une imprimante virtuelle d'une imprimante physique."""
    haystack = f"{name} {driver}".lower()
    if any(hint in haystack for hint in _VIRTUAL_PRINTER_HINTS):
        return True

    port_lower = (port or '').lower()
    if port_lower in _VIRTUAL_PRINTER_PORTS:
        return True
    # Ports applicatifs (paquets UWP) et ports fichier
    return port_lower.startswith('microsoft.') or '*.pdf' in port_lower


def is_elevated():
    """Indique si le collecteur tourne avec des privilèges administrateur.

    Plusieurs sources (SMART détaillé, TPM, BitLocker, clé OEM) exigent
    l'élévation : sans cette information, un champ vide serait indiscernable
    d'un champ inaccessible.
    """
    try:
        if IS_WINDOWS:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def _win_powershell_json(cmd, timeout=25):
    """Exécute une commande PowerShell et parse le JSON retourné.

    L'encodage est forcé des deux côtés : PowerShell 5.1 écrit sur la console
    avec la page de code OEM, et `text=True` décoderait avec l'encodage local
    (cp1252 sur un Windows français). Sans cela, tout libellé accentué remonté
    par la collecte est silencieusement corrompu — noms de périphériques,
    comptes utilisateurs, descriptions d'adaptateurs réseau, fabricants.
    """
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        prelude = '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; '
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', prelude + cmd],
            capture_output=True, timeout=timeout, creationflags=creationflags
        )
        if result.returncode == 0:
            out = (result.stdout or b'').decode('utf-8', errors='replace')
            if out.strip():
                return json.loads(out)
    except Exception:
        pass
    return None


def _run(cmd, timeout=10):
    """Exécute une commande et retourne stdout (chaîne vide en cas d'échec)."""
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (result.stdout or b'').decode('utf-8', errors='replace')
    except Exception:
        return ''


# ══════════════════════════════════════════════════════════════════════════════
# PÉRIPHÉRIQUES USB
# ══════════════════════════════════════════════════════════════════════════════

# Classes de périphériques Windows (Get-PnpDevice) → catégories ParcInfo.
# Les catégories correspondent exactement à `categories_peripheriques` de
# config_helpers.py, sinon la liste déroulante de la fiche périphérique
# afficherait une valeur hors liste.
_WIN_CLASS_TO_CATEGORIE = {
    'keyboard': 'Clavier',
    'mouse': 'Souris',
    'printer': 'Imprimante',
    'printqueue': 'Imprimante',
    'image': 'Scanner',
    'camera': 'Webcam',
    'media': 'Casque / Micro',
    'audioendpoint': 'Casque / Micro',
    'diskdrive': 'Disque dur externe',
    'usbdevice': 'Cle USB',
    'wpd': 'Telephone mobile',
    'net': 'Adaptateur reseau',
    'smartcardreader': 'Lecteur de cartes',
    'monitor': 'Ecran',
    'battery': 'Onduleur / UPS',
}

# Repli par mots-clés dans le libellé quand la classe est générique ('USB',
# 'HIDClass'…), ce qui est le cas de la majorité des périphériques réels.
# Ordre significatif : le premier motif trouvé gagne.
_USB_NAME_PATTERNS = [
    (r'\b(webcam|web cam|facetime|caméra|camera)\b', 'Webcam'),
    (r'\b(headset|casque|micro|microphone|headphone)\b', 'Casque / Micro'),
    (r'\b(speaker|haut.?parleur|soundbar)\b', 'Haut-parleurs'),
    (r'\b(keyboard|clavier)\b', 'Clavier'),
    (r'\b(mouse|souris|trackball)\b', 'Souris'),
    (r'\b(scanner|scanjet|perfection)\b', 'Scanner'),
    (r'\b(multifonction|mfp|all.?in.?one)\b', 'Imprimante multifonction'),
    (r'\b(printer|imprimante|laserjet|officejet|deskjet|ecotank)\b', 'Imprimante'),
    (r'\b(dock|docking|thunderbolt dock)\b', 'Docking station'),
    (r'\b(hub)\b', 'Hub USB'),
    (r'\b(card reader|lecteur de carte|cardreader|smart card)\b', 'Lecteur de cartes'),
    (r'\b(ups|onduleur|smart.?ups)\b', 'Onduleur / UPS'),
    (r'\b(ethernet|gigabit|wireless|wi.?fi|wlan|bluetooth|lan adapter)\b', 'Adaptateur reseau'),
    (r'\b(flash disk|flash drive|clé usb|cle usb|usb drive|datatraveler|cruzer)\b', 'Cle USB'),
    (r'\b(disque|disk|ssd|hdd|portable drive|my passport|elements)\b', 'Disque dur externe'),
    (r'\b(monitor|écran|ecran|display)\b', 'Ecran'),
    (r'\b(phone|téléphone|telephone|iphone|android)\b', 'Telephone mobile'),
    (r'\b(badge|proximity reader)\b', 'Badge / Lecteur de badge'),
]

# Libellés de plomberie interne. Windows expose un périphérique physique sous
# plusieurs nœuds PnP : une imprimante multifonction remonte à la fois comme
# « périphérique composite », « stockage de masse » et « prise en charge
# d'impression ». Ces nœuds sont conservés pour le regroupement mais ne servent
# jamais de libellé, et un groupe qui n'en contient que ne devient pas un
# périphérique ParcInfo.
# Les libellés dépendent de la langue de Windows : les accents sont retirés
# avant comparaison, et les variantes FR/EN sont listées côte à côte.
_USB_GENERIC = re.compile(
    r'(root hub|hub usb racine|generic usb hub|concentrateur usb|'
    r'usb composite device|peripherique usb composite|'
    r'usb input device|peripherique d.entree usb|usb printing support|'
    r'prise en charge d.impression usb|usb mass storage device|'
    r'dispositif de stockage de masse usb|hid.compliant|conforme hid|'
    r'unknown usb device|peripherique usb inconnu|generic usb device|'
    r'peripherique usb generique|host controller|controleur|'
    r'usb attached scsi|usb\s*[23]\.\d+\s*hub)',
    re.IGNORECASE,
)

# Ordre de spécificité : quand plusieurs nœuds d'un même périphérique donnent
# des catégories différentes, la plus spécifique gagne.
_CATEGORIE_PRIORITE = [
    # Webcam avant Scanner : une webcam remonte en classe Windows « Image »,
    # comme un scanner. Un vrai multifonction est capté par la règle dédiée
    # ci-dessous avant d'arriver ici.
    'Imprimante multifonction', 'Imprimante', 'Webcam', 'Scanner',
    'Casque / Micro', 'Haut-parleurs', 'Clavier', 'Souris', 'Docking station',
    'Disque dur externe', 'Cle USB', 'Lecteur de cartes', 'Adaptateur reseau',
    'Telephone mobile', 'Ecran', 'Badge / Lecteur de badge', 'Onduleur / UPS',
    'Hub USB', 'Autre',
]


def _strip_accents(text):
    """Compare les libellés sans dépendre des accents (Windows FR vs EN)."""
    import unicodedata
    return ''.join(
        c for c in unicodedata.normalize('NFD', text or '')
        if unicodedata.category(c) != 'Mn'
    )


def _is_generic_label(name):
    return bool(_USB_GENERIC.search(_strip_accents(name or '')))


def _clean_manufacturer(value):
    """Écarte les fabricants génériques attribués par Windows.

    Windows renseigne « (Périphériques système standard) » ou « (Contrôleur
    hôte USB standard) » pour les pilotes intégrés : ce n'est pas un fabricant.
    """
    value = (value or '').strip()
    if not value or value.startswith('('):
        return ''
    if re.search(r'standard|microsoft|generic|g[ée]n[ée]rique', value, re.IGNORECASE):
        return ''
    # « Logitech (x64) » → « Logitech »
    return re.sub(r'\s*\((x64|x86|amd64)\)\s*$', '', value, flags=re.IGNORECASE).strip()


def _usb_date(valeur):
    """Normalise une date PnP (MM/JJ/AAAA hh:mm:ss) en AAAA-MM-JJ.

    Les propriétés PnP sont rendues au format court de la culture du système ;
    on ne conserve que la date, seule information utile en inventaire.
    """
    texte = _clean(valeur)
    if not texte:
        return ''
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})', texte)
    if m:
        mois, jour, annee = m.groups()
        return '%s-%02d-%02d' % (annee, int(mois), int(jour))
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', texte)
    return m.group(0) if m else texte[:10]


def _classify_usb(name, win_class=''):
    """Déduit une catégorie ParcInfo depuis le libellé et la classe Windows."""
    label = _strip_accents(name or '').lower()
    for pattern, categorie in _USB_NAME_PATTERNS:
        if re.search(pattern, label):
            return categorie
    mapped = _WIN_CLASS_TO_CATEGORIE.get((win_class or '').lower())
    return mapped or 'Autre'


def _merge_usb_nodes(nodes):
    """Regroupe les nœuds PnP appartenant au même périphérique physique.

    Windows présente un seul périphérique sous plusieurs nœuds (interface HID,
    nœud composite parent, fonction d'impression…). Sans regroupement, une
    imprimante multifonction créerait quatre périphériques dans ParcInfo.

    Le regroupement se fait sur VID/PID. Si un même VID/PID porte plusieurs
    numéros de série distincts, il s'agit réellement de plusieurs exemplaires
    du même modèle et ils sont séparés.
    """
    by_vidpid = {}
    for node in nodes:
        key = (node['vid'], node['pid']) if node['vid'] else ('name', node['name'].lower())
        by_vidpid.setdefault(key, []).append(node)

    groups = []
    for key, group in by_vidpid.items():
        serials = {n['serial'] for n in group if n['serial']}
        if len(serials) > 1:
            # Plusieurs exemplaires du même modèle : un groupe par série.
            for serial in sorted(serials):
                groups.append([n for n in group if n['serial'] in ('', serial)])
        else:
            groups.append(group)

    devices = []
    for group in groups:
        named = [n for n in group if not _is_generic_label(n['name'])]
        serial = next((n['serial'] for n in group if n['serial']), '')
        # Un groupe entièrement générique (hub racine, contrôleur) reste listé
        # dans le rapport mais n'ira pas dans l'inventaire. Un périphérique qui
        # expose un vrai numéro de série est en revanche un matériel réel même
        # si Windows ne lui donne qu'un libellé générique : c'est le cas des
        # claviers et souris HID, qu'il serait faux d'ignorer.
        inventoriable = bool(named) or bool(serial)

        cats = {n['categorie'] for n in group if n['categorie'] != 'Autre'}
        # Un périphérique exposant à la fois numérisation et impression est un
        # multifonction, pas deux appareils distincts. La fonction impression
        # apparaît sous un libellé générique ("Prise en charge d'impression
        # USB"), d'où la recherche sur l'ensemble des nœuds du groupe.
        has_print_node = any(
            re.search(r'(impression|printing|printer|imprimante)',
                      _strip_accents(n['name']), re.IGNORECASE)
            for n in group
        )
        if 'Scanner' in cats and (has_print_node or 'Imprimante' in cats):
            categorie = 'Imprimante multifonction'
        elif cats:
            categorie = min(cats, key=lambda c: _CATEGORIE_PRIORITE.index(c)
                            if c in _CATEGORIE_PRIORITE else 99)
        else:
            categorie = 'Autre'

        # Libellé : le nœud qui porte la catégorie retenue est le plus
        # représentatif ("HD USB Camera" plutôt que "Périphérique USB
        # composite"). À défaut, n'importe quel nœud nommé.
        preferred = [n for n in named if n['categorie'] == categorie] or named or group
        best = max(preferred, key=lambda n: len(n['name']))

        # Fabricant : celui du nœud retenu d'abord — les autres nœuds d'un même
        # périphérique portent souvent le nom du pilote générique qui les gère
        # (« Dispositif de stockage USB compatible ») plutôt que le fabricant.
        manufacturer = _clean_manufacturer(best['manufacturer']) or next(
            (m for m in (_clean_manufacturer(n['manufacturer']) for n in group) if m), '')

        # `name` reste le libellé réel de Windows — c'est ce que le rapport doit
        # montrer. `inventory_name` est la version présentable utilisée pour
        # créer le périphérique dans ParcInfo, pour éviter d'y inscrire
        # « Périphérique USB composite ».
        inventory_name = best['name']
        if not named:
            inventory_name = (f'{manufacturer} — périphérique USB' if manufacturer
                              else f'Périphérique USB {best["vid"]}:{best["pid"]}')

        # Modèle : le nom déclaré par le périphérique prime sur le libellé du
        # pilote, qui est souvent générique.
        modele = next((n.get('bus_desc') for n in group
                       if n.get('bus_desc') and not _is_generic_label(n['bus_desc'])), '')
        dates_install = sorted(d for d in (n.get('install_date') for n in group) if d)
        pilote = next((n for n in group if n.get('driver_version')), {})

        devices.append({
            'name': best['name'],
            'inventory_name': modele or inventory_name,
            'model': modele,
            'manufacturer': manufacturer,
            'serial': serial,
            'vid': best['vid'],
            'pid': best['pid'],
            'categorie': categorie,
            'inventoriable': inventoriable,
            'nodes': len(group),
            # Première apparition du matériel sur la machine
            'install_date': dates_install[0] if dates_install else '',
            'driver_version': pilote.get('driver_version', ''),
            'driver_date': pilote.get('driver_date', ''),
            'win_class': next((n.get('win_class') for n in group if n.get('win_class')), ''),
        })
    return sorted(devices, key=lambda x: (x['categorie'], x['name']))


def _usb_serial_from_instance_id(instance_id):
    """Extrait le numéro de série d'un InstanceId Windows.

    'USB\\VID_0781&PID_5583\\4C530001120523111593' → numéro de série réel.
    'USB\\VID_046D&PID_C52B\\5&2f4d1b3&0&2'        → chemin de port, pas un
    numéro de série : Windows en génère un quand le périphérique n'en expose
    aucun. Le signe '&' est le marqueur de ce cas.
    """
    if not instance_id:
        return ''
    tail = instance_id.rsplit('\\', 1)[-1]
    if '&' in tail or not tail:
        return ''
    return tail.strip()


def _parse_vid_pid(instance_id):
    m = re.search(r'VID_([0-9A-Fa-f]{4}).*?PID_([0-9A-Fa-f]{4})', instance_id or '')
    return (m.group(1).upper(), m.group(2).upper()) if m else ('', '')


def collect_usb_devices():
    """Liste les périphériques USB connectés.

    Retourne une liste de dicts :
        name, manufacturer, serial, vid, pid, categorie, inventoriable
    """
    if IS_WINDOWS:
        return _collect_usb_windows()
    if IS_MAC:
        return _collect_usb_macos()
    if IS_LINUX:
        return _collect_usb_linux()
    return []


def _collect_usb_windows():
    # BusReportedDeviceDesc est le nom que le périphérique déclare lui-même : il
    # est souvent bien plus parlant que le libellé du pilote ("DCP-195C" plutôt
    # que "Dispositif de stockage de masse USB", "Mouse" plutôt que
    # "Périphérique d'entrée USB"). InstallDate donne la date de premier
    # branchement. Les propriétés sont demandées en un seul appel par nœud.
    data = _win_powershell_json(
        "$keys = @('DEVPKEY_Device_BusReportedDeviceDesc','DEVPKEY_Device_InstallDate',"
        "'DEVPKEY_Device_DriverDate','DEVPKEY_Device_DriverVersion'); "
        "$d = @(); try { $d = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue "
        "| Where-Object { $_.InstanceId -like 'USB\\*' } | ForEach-Object { "
        "$dev = $_; $props = @{}; "
        "try { Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName $keys "
        "-ErrorAction SilentlyContinue | ForEach-Object { "
        "if ($_.Data) { $props[$_.KeyName] = [string]$_.Data } } } catch {}; "
        "[PSCustomObject]@{ FriendlyName=$dev.FriendlyName; Class=$dev.Class; "
        "InstanceId=$dev.InstanceId; Manufacturer=$dev.Manufacturer; Status=$dev.Status; "
        "Props=$props } }) } catch {}; "
        "$d | ConvertTo-Json -Compress -Depth 4",
        timeout=90,
    )
    nodes = []
    for d in _as_list(data):
        name = (d.get('FriendlyName') or '').strip()
        if not name:
            continue
        instance_id = d.get('InstanceId') or ''
        vid, pid = _parse_vid_pid(instance_id)
        props = d.get('Props') or {}
        bus_desc = _clean(props.get('DEVPKEY_Device_BusReportedDeviceDesc'))
        nodes.append({
            'name': name,
            'bus_desc': bus_desc,
            'manufacturer': (d.get('Manufacturer') or '').strip(),
            'serial': _usb_serial_from_instance_id(instance_id),
            'vid': vid,
            'pid': pid,
            'win_class': _clean(d.get('Class')),
            'install_date': _usb_date(props.get('DEVPKEY_Device_InstallDate')),
            'driver_date': _usb_date(props.get('DEVPKEY_Device_DriverDate')),
            'driver_version': _clean(props.get('DEVPKEY_Device_DriverVersion')),
            # Le nom déclaré par le périphérique sert aussi à le classer : c'est
            # lui qui dit « Mouse » là où le pilote ne dit que « périphérique
            # d'entrée USB ».
            'categorie': _classify_usb(f'{name} {bus_desc}', d.get('Class')),
        })
    return _merge_usb_nodes(nodes)


def _collect_usb_macos():
    out = _run(['system_profiler', 'SPUSBDataType', '-json'], timeout=30)
    if not out.strip():
        return []
    try:
        payload = json.loads(out)
    except Exception:
        return []

    nodes = []

    def walk(items):
        for item in items or []:
            name = (item.get('_name') or '').strip()
            # Les contrôleurs de bus n'ont ni vendor_id ni serial : on descend
            # dans leurs enfants sans les inventorier eux-mêmes.
            if name and item.get('vendor_id'):
                vid = re.sub(r'^0x', '', str(item.get('vendor_id', ''))).split()[0].upper()
                pid = re.sub(r'^0x', '', str(item.get('product_id', ''))).split()[0].upper()
                nodes.append({
                    'name': name,
                    'manufacturer': (item.get('manufacturer') or '').strip(),
                    'serial': (item.get('serial_num') or '').strip(),
                    'vid': vid,
                    'pid': pid,
                    'categorie': _classify_usb(name),
                })
            walk(item.get('_items'))

    walk(payload.get('SPUSBDataType'))
    return _merge_usb_nodes(nodes)


def _collect_usb_linux():
    out = _run(['lsusb'], timeout=15)
    nodes = []
    for line in out.splitlines():
        m = re.match(r'Bus \d+ Device \d+: ID ([0-9a-f]{4}):([0-9a-f]{4})\s*(.*)', line.strip())
        if not m:
            continue
        vid, pid, name = m.group(1).upper(), m.group(2).upper(), (m.group(3) or '').strip()
        if not name:
            name = f'Périphérique USB {vid}:{pid}'
        nodes.append({
            'name': name,
            'manufacturer': name.split(',')[0].strip() if ',' in name else '',
            'serial': '',
            'vid': vid,
            'pid': pid,
            'categorie': _classify_usb(name),
        })
    return _merge_usb_nodes(nodes)



# ════════════════════════════════════════════════════════════════════════════
# CLÉS DE LICENCE
# ════════════════════════════════════════════════════════════════════════════
#
# Windows n'expose publiquement que les 5 derniers caractères de la clé en
# service. Récupérer la clé complète demande de balayer plusieurs sources, et
# aucune ne fonctionne dans tous les cas :
#
#   - `BackupProductKeyDefault` : clé complète en clair déposée par Windows à
#     l'activation. Présente sur les installations retail et volume avec clé
#     saisie. C'est la source la plus fiable quand elle existe.
#   - `OA3xOriginalProductKey` : clé OEM gravée dans la table ACPI MSDM des
#     machines préinstallées. Sur une machine réinstallée avec une autre
#     licence, elle subsiste sans correspondre à la licence active.
#   - `DigitalProductId` : blob du registre, décodable sur Windows 7/8 et sur
#     les installations où une clé a été saisie. Sur Windows 10/11 en licence
#     numérique, sa zone de clé est remplie de zéros.
#
# Trois cas ne stockent aucune clé sur la machine, par conception : licence
# numérique Windows, Office Click-to-Run / Microsoft 365, et activation KMS.

# Alphabet d'encodage des clés produit Microsoft : 24 caractères, sans les
# lettres prêtant à confusion (ni O/0, ni I/1, ni voyelles). Le « N » n'en fait
# pas partie mais apparaît bien dans les clés affichées : c'est le caractère que
# l'algorithme Windows 8+ insère à une position variable.
KEY_ALPHABET = 'BCDFGHJKMPQRTVWXY2346789'
_KEY_CHARS = KEY_ALPHABET + 'N'
_KEY_RE = re.compile(r'^[%s]{5}(-[%s]{5}){4}$' % (_KEY_CHARS, _KEY_CHARS))


def _is_valid_key_format(key):
    """Une clé produit Microsoft : 5 groupes de 5 caractères d'un alphabet fixe."""
    return bool(_KEY_RE.match((key or '').strip().upper()))


def _is_plausible_key(key):
    """Écarte les clés décodées depuis un blob vide.

    Sous Windows 10/11 en licence numérique, DigitalProductId existe mais sa
    zone de clé est à zéro : le décodage produit alors « BBBBB-BBBBB-… », soit
    l'index 0 répété. Une vraie clé comporte au moins cinq caractères distincts.
    """
    if not _is_valid_key_format(key):
        return False
    return len(set(key.replace('-', ''))) >= 5


def _decode_digital_product_id(blob):
    """Décode un blob DigitalProductId du registre en clé produit lisible.

    Algorithme Microsoft : base 24 sur les octets 52..66. Les clés Windows 8+
    encodent un « N » à une position variable, signalée par un bit de l'octet 66.
    """
    if not blob or len(blob) < 67:
        return ''
    data = bytearray(blob[52:67])

    is_win8 = (data[14] // 6) & 1
    data[14] = (data[14] & 0xF7) | ((is_win8 & 2) * 4)

    key = ''
    last = 0
    for _ in range(25):
        current = 0
        for j in range(14, -1, -1):
            current = (current * 256) ^ data[j]
            data[j] = current // 24
            current %= 24
        last = current
        key = KEY_ALPHABET[current] + key

    if is_win8:
        key = key[1:last + 1] + 'N' + key[last + 1:]

    formatted = '-'.join(key[i:i + 5] for i in range(0, len(key), 5))
    return formatted if _is_plausible_key(formatted) else ''


def _registry_string(path, value_name):
    """Lit une valeur texte du registre dans la vue 64 bits."""
    if not IS_WINDOWS:
        return ''
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
            return str(value).strip()
    except Exception:
        return ''


def _registry_binary(path, value_name):
    """Lit une valeur binaire du registre dans la vue 64 bits."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            data, _ = winreg.QueryValueEx(key, value_name)
            return bytes(data)
    except Exception:
        return None


def windows_key_candidates(oem_key=''):
    """Toutes les clés produit Windows complètes récupérables sur la machine.

    Retourne une liste de (clé, source), sans doublon, par ordre de fiabilité.
    `oem_key` est la clé firmware déjà relevée par la collecte, passée ici pour
    éviter une seconde interrogation WMI.
    """
    if not IS_WINDOWS:
        return []

    CV = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    candidats = []

    def add(cle, source):
        cle = (cle or '').strip().upper()
        if _is_plausible_key(cle) and not any(c == cle for c, _ in candidats):
            candidats.append((cle, source))

    add(_registry_string(CV + r'\SoftwareProtectionPlatform',
                         'BackupProductKeyDefault'), 'Registre (clé installée)')
    add(oem_key, 'BIOS OEM (table MSDM)')
    add(_decode_digital_product_id(_registry_binary(CV, 'DigitalProductId')),
        'Registre (DigitalProductId)')
    return candidats


def _office_key_candidates():
    """Clés Office issues du registre d'enregistrement (installations MSI).

    Office Click-to-Run et Microsoft 365 ne stockent aucune clé produit : la
    licence est un jeton. Rien à récupérer dans ce cas.
    """
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except Exception:
        return []

    trouvees = []
    for base in (r'SOFTWARE\Microsoft\Office', r'SOFTWARE\Wow6432Node\Microsoft\Office'):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as root:
                versions = [winreg.EnumKey(root, i)
                            for i in range(winreg.QueryInfoKey(root)[0])]
        except OSError:
            continue
        for version in versions:
            reg_path = '%s\\%s\\Registration' % (base, version)
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path, 0,
                                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as regk:
                    guids = [winreg.EnumKey(regk, i)
                             for i in range(winreg.QueryInfoKey(regk)[0])]
            except OSError:
                continue
            for guid in guids:
                chemin = '%s\\%s' % (reg_path, guid)
                cle = _decode_digital_product_id(_registry_binary(chemin, 'DigitalProductId'))
                if cle and not any(c == cle for c, _ in trouvees):
                    trouvees.append((cle, 'Registre Office'))
    return trouvees


def enrich_licenses_with_keys(info):
    """Complète les licences relevées avec leur clé produit entière.

    Le contrôle de correction repose sur `partial_key` : Windows n'expose que
    les 5 derniers caractères de la clé en service, ils servent donc de somme de
    contrôle. Une clé complète dont la fin correspond est certifiée être celle
    qui est installée ; une clé dont la fin ne correspond pas est conservée mais
    signalée comme non appairée, plutôt que présentée comme la licence active.
    """
    licences = info.get('licenses') or []
    candidats = windows_key_candidates(info.get('oem_product_key', ''))
    candidats += _office_key_candidates()
    if not candidats and not licences:
        return

    restants = list(candidats)
    for lic in licences:
        partielle = (lic.get('partial_key') or '').strip().upper()
        lic.setdefault('full_key', '')
        lic.setdefault('key_source', '')
        lic.setdefault('key_verified', False)
        if not partielle:
            continue
        for i, (cle, source) in enumerate(restants):
            if cle.endswith(partielle):
                lic['full_key'] = cle
                lic['key_source'] = source
                lic['key_verified'] = True
                restants.pop(i)
                break

    # Clés complètes retrouvées mais ne correspondant à aucune licence active :
    # les signaler telles quelles plutôt que de les taire.
    for cle, source in restants:
        licences.append({
            'name': 'Windows — clé récupérée, non active',
            'description': '',
            'partial_key': cle[-5:],
            'status': 'Non active',
            'activated': False,
            'channel': '',
            'full_key': cle,
            'key_source': source,
            'key_verified': False,
        })

    if licences:
        info['licenses'] = licences


# ════════════════════════════════════════════════════════════════════════════
# IDENTITÉ
# ════════════════════════════════════════════════════════════════════════════

def get_mac_address():
    """Récupère l'adresse MAC réelle de la machine.

    `uuid.getnode()` n'a aucune notion de « la bonne » carte : sur une machine
    avec plusieurs adaptateurs (VPN, Hyper-V/WSL, VirtualBox, Bluetooth…), il
    peut renvoyer n'importe lequel — pas forcément le physique connecté, ni
    celui effectivement enregistré pour cette machine lors d'une collecte
    complète (voir _meilleure_carte_physique, utilisée là mais pas ici : plus
    lente, elle nécessite l'énumération PowerShell des cartes). Gardé pour
    compatibilité et comme repli ; le pré-remplissage du client dans le
    collecteur GUI utilise get_all_mac_addresses() ci-dessous à la place,
    plus lent d'un appel système mais fiable quelle que soit la carte que
    uuid.getnode() aurait choisie.
    """
    try:
        mac = uuid.getnode()
        mac_str = ':'.join(['{:02x}'.format((mac >> (i << 3)) & 0xff) for i in range(5, -1, -1)])
        return mac_str.upper()
    except Exception:
        return ""


def get_all_mac_addresses():
    """Adresses MAC de TOUTES les cartes réseau visibles localement — pas
    seulement celle que uuid.getnode() choisit arbitrairement (voir sa
    docstring). Sert au pré-remplissage rapide du client dans le collecteur
    GUI, avant même que la collecte complète (qui choisit sa propre
    « meilleure » carte physique connectée) ne s'exécute.

    Constaté sur un poste de développement réel : jusqu'à sept adresses MAC
    différentes (carte physique, VirtualBox, Hyper-V/WSL…) — get_mac_address()
    ne renvoyant qu'une seule d'entre elles, souvent PAS celle effectivement
    enregistrée côté serveur pour cette machine. En envoyer la liste complète
    et laisser le serveur comparer à N'IMPORTE LAQUELLE règle le problème
    sans avoir à deviner laquelle est « la bonne » avant la collecte.
    """
    macs = set()
    brute = get_mac_address()
    if brute:
        macs.add(brute)
    try:
        if IS_WINDOWS:
            sortie = _run(['getmac', '/fo', 'csv', '/nh'], timeout=10)
            for ligne in sortie.splitlines():
                champs = [c.strip('"') for c in ligne.strip().split('","')]
                if champs and re.match(r'^([0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}$', champs[0]):
                    macs.add(champs[0].replace('-', ':').upper())
        elif IS_MAC:
            sortie = _run(['networksetup', '-listallhardwareports'], timeout=10)
            for ligne in sortie.splitlines():
                if ligne.strip().startswith('Ethernet Address:'):
                    valeur = ligne.split(':', 1)[1].strip()
                    if valeur and valeur.lower() != 'n/a':
                        macs.add(valeur.upper())
        elif IS_LINUX:
            racine = '/sys/class/net'
            if os.path.isdir(racine):
                for carte in os.listdir(racine):
                    if carte == 'lo':
                        continue
                    try:
                        with open(os.path.join(racine, carte, 'address')) as f:
                            valeur = f.read().strip()
                        if valeur and valeur != '00:00:00:00:00:00':
                            macs.add(valeur.upper())
                    except OSError:
                        pass
    except Exception:
        pass
    return sorted(macs)


def get_hostname():
    """Récupère le nom d'hôte exact."""
    try:
        return socket.gethostname()
    except Exception:
        return ""


def get_fqdn():
    """Nom DNS complet de la machine (alimente la colonne nom_dns de ParcInfo)."""
    if IS_MAC:
        # socket.getfqdn() est notoirement peu fiable sur macOS : il
        # déclenche gethostbyaddr(gethostname()), et quand cette résolution
        # retombe sur le bouclage IPv6 (::1) sans enregistrement PTR
        # configuré, certains résolveurs renvoient tel quel le nom de la
        # requête plutôt qu'une erreur — donnant un « nom DNS » du genre
        # « 1.0.0.0.[...]0.0.ip6.arpa » (constaté en usage réel, pas
        # supposé). `scutil --get LocalHostName` lit directement le nom
        # Bonjour configuré localement, sans passer par une résolution
        # réseau : rien à confondre avec une adresse de bouclage.
        try:
            nom = _run(['scutil', '--get', 'LocalHostName'], timeout=5).strip()
            if nom:
                return nom if nom.endswith('.local') else nom + '.local'
        except Exception:
            pass
        # Repli : gethostname() inclut déjà généralement le suffixe .local
        # sur macOS — getfqdn() n'y apporte ici aucune valeur fiable de plus.
        hote = get_hostname()
        return hote if hote and '.' in hote else ''
    try:
        fqdn = socket.getfqdn()
        # getfqdn() retombe sur le hostname court quand la résolution échoue :
        # dans ce cas il n'y a pas de vrai nom DNS à remonter
        return fqdn if fqdn and '.' in fqdn else ''
    except Exception:
        return ''


def get_ip_addresses():
    """Récupère toutes les adresses IP locales.

    `socket.gethostbyname_ex(hostname)` est le moyen habituel, mais peu
    fiable sur macOS : le hostname y est souvent un nom mDNS en `.local`
    (Bonjour), que cette résolution classique ne fait pas toujours
    aboutir — la liste ressort alors vide, silencieusement, sans lever
    d'exception. Repli sur la technique du socket UDP « connecté » à une
    adresse externe (aucun paquet réellement envoyé, `connect()` sur UDP ne
    fait qu'interroger la table de routage) : donne l'IP de l'interface que
    le système utiliserait réellement pour sortir, fiable sur les trois OS.
    """
    ips = []
    try:
        hostname = socket.gethostname()
        ips = socket.gethostbyname_ex(hostname)[2]
    except Exception:
        pass
    ips = [ip for ip in ips if not ip.startswith('127.')]
    if ips:
        return ips

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith('127.'):
                return [ip]
        finally:
            s.close()
    except Exception:
        pass
    return ips


def _hosts_file_path():
    """Chemin du fichier hosts, quel que soit l'OS."""
    if IS_WINDOWS:
        return os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                            'System32', 'drivers', 'etc', 'hosts')
    return '/etc/hosts'


# Noms que Windows/macOS/Linux inscrivent eux-mêmes par défaut, sans
# intervention de quiconque — jamais une redirection à auditer.
_HOSTS_NOMS_DEFAUT = {
    'localhost', 'localhost.localdomain',
    'broadcasthost',  # macOS
    'ip6-localhost', 'ip6-loopback', 'ip6-localnet',
    'ip6-mcastprefix', 'ip6-allnodes', 'ip6-allrouters', 'ip6-allhosts',  # Debian/Ubuntu
}


def _parse_hosts_lines(lignes, hostname_machine=None):
    """Extrait les redirections utiles de lignes de fichier hosts déjà lues.

    Fonction pure, séparée de get_hosts_file_entries pour rester testable
    sans dépendre du fichier hosts réel de la machine — même principe que
    _parse_wifi_profile_xml.

    Un fichier hosts réel accumule vite des lignes commentées (le modèle
    livré par Windows n'est qu'exemples désactivés), des doublons — le même
    blocage recopié deux fois par deux outils différents n'est pas rare — et
    la propre entrée `<ip loopback> <hostname de la machine>` que Debian/
    Ubuntu écrivent d'eux-mêmes. Rien de tout ça n'est une redirection
    volontaire ; c'est filtré ici plutôt que remonté tel quel.

    `local` distingue une redirection vers une IP locale/nulle (blocage —
    publicité, licence, télémétrie — ou serveur de dev local) d'une simple
    correspondance nom↔IP réelle sur le réseau local (ex. un poste désigné
    par son nom plutôt que redécouvert par DHCP à chaque fois).
    """
    hote_normalise = (hostname_machine or '').strip().lower()
    vus = set()
    entrees = []
    for ligne in lignes:
        ligne = ligne.split('#', 1)[0].strip()
        if not ligne:
            continue
        morceaux = ligne.split()
        if len(morceaux) < 2:
            continue
        ip = morceaux[0]
        for nom in morceaux[1:]:
            nom_normalise = nom.strip().lower()
            if not nom_normalise or nom_normalise in _HOSTS_NOMS_DEFAUT:
                continue
            if hote_normalise and nom_normalise == hote_normalise and ip in ('127.0.0.1', '127.0.1.1', '::1'):
                continue
            cle = (ip, nom_normalise)
            if cle in vus:
                continue
            vus.add(cle)
            entrees.append({
                'ip': ip, 'hostname': nom,
                'local': ip in ('0.0.0.0', '127.0.0.1', '::1'),
            })
    return entrees


def get_hosts_file_entries(hostname_machine=None):
    """Redirections DNS actives du fichier hosts de cette machine (voir
    _parse_hosts_lines pour le filtrage)."""
    try:
        with open(_hosts_file_path(), encoding='utf-8-sig', errors='replace') as f:
            lignes = f.readlines()
    except Exception:
        return []
    return _parse_hosts_lines(lignes, hostname_machine)


def get_os_info():
    """Récupère l'OS et la version exacte avec édition (Pro/Home/Server).

    os_version = "Display Version" Windows (ex: "22H2") - le build/feature update,
    os_name = nom complet lisible (ex: "Windows 11 Pro", "Windows Server 2022 Standard").

    Note: platform.release() renvoie toujours "10" sur Windows 11 (même noyau NT 10.0) -
    la version majeure (10 vs 11) doit être déduite du numéro de build (>= 22000 = Windows 11).
    """
    os_name = platform.system()  # 'Windows', 'Darwin', 'Linux'
    os_version = platform.release()
    full_os_name = os_name
    extra = {}

    # Pour Windows, obtenir la version complète et l'édition
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion') as key:
                def _reg(name):
                    try:
                        return winreg.QueryValueEx(key, name)[0]
                    except Exception:
                        return ''

                os_version = _reg('DisplayVersion') or os_version
                product_name = _reg('ProductName')
                edition_name = _reg('EditionID')
                ubr = _reg('UBR')

                try:
                    build_number = int(_reg('CurrentBuildNumber') or 0)
                except Exception:
                    build_number = 0

                if build_number:
                    extra['os_build'] = f"{build_number}.{ubr}" if ubr else str(build_number)

                registered_owner = _reg('RegisteredOwner')
                registered_org = _reg('RegisteredOrganization')
                if registered_owner:
                    extra['registered_owner'] = registered_owner
                if registered_org:
                    extra['registered_organization'] = registered_org

                if 'Server' in product_name:
                    # Le ProductName Windows Server contient déjà l'année
                    full_os_name = product_name
                    extra['is_server'] = True
                else:
                    major = 11 if build_number >= 22000 else 10
                    edition_map = {
                        'Professional': 'Pro',
                        'Core': 'Home',
                        'Home': 'Home',
                        'Enterprise': 'Enterprise',
                        'Education': 'Education',
                    }
                    edition = edition_map.get(edition_name, edition_name or 'Pro')
                    full_os_name = f"Windows {major} {edition}"
        except Exception:
            pass

    # Pour macOS
    if IS_MAC:
        try:
            # `sw_vers`, pas platform.mac_ver() : ce dernier a régulièrement
            # traîné derrière une nouvelle version majeure macOS (cas connu -
            # Big Sur longtemps rapporté "10.16" au lieu de "11.x" par
            # certaines versions de Python), alors que sw_vers vient
            # directement du système et est toujours à jour à sa sortie.
            os_version = _run(['sw_vers', '-productVersion'], timeout=5).strip() or platform.mac_ver()[0]
            build = _run(['sw_vers', '-buildVersion'], timeout=5).strip()
            if build:
                extra['os_build'] = build

            full_os_name = "macOS"
            try:
                major, minor = (int(p) for p in (os_version.split('.') + ['0'])[:2])
                nom = _MACOS_CODENAMES.get(major) or _MACOS_CODENAMES.get((major, minor))
                if nom:
                    full_os_name = f"macOS {nom}"
            except (ValueError, TypeError):
                pass
        except Exception:
            pass

    # Pour Linux, lire le nom de distribution dans /etc/os-release
    if IS_LINUX:
        try:
            with open('/etc/os-release', 'r') as f:
                fields = {}
                for line in f:
                    if '=' in line:
                        k, v = line.split('=', 1)
                        fields[k.strip()] = v.strip().strip('"')
            if fields.get('PRETTY_NAME'):
                full_os_name = fields['PRETTY_NAME']
            elif fields.get('NAME'):
                full_os_name = fields['NAME']
            if fields.get('VERSION_ID'):
                os_version = fields['VERSION_ID']
        except Exception:
            pass

    info = {
        "os_name": full_os_name,
        "os_version": os_version,
        "platform": platform.platform(),
        "architecture": platform.machine(),
    }
    info.update(extra)
    return info


# ════════════════════════════════════════════════════════════════════════════
# COLLECTE WINDOWS
# ════════════════════════════════════════════════════════════════════════════

def _win_base_hardware():
    """RAM totale, CPU, disques logiques via ctypes/winreg (sans PowerShell)."""
    info = {}

    # RAM (kernel32 GlobalMemoryStatusEx)
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        info['ram_gb'] = round(stat.ullTotalPhys / (1024 ** 3), 1)
        info['ram_free_gb'] = round(stat.ullAvailPhys / (1024 ** 3), 1)
    except Exception:
        pass

    # CPU nom (registre) + cores (os.cpu_count)
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        cpu_name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        winreg.CloseKey(key)
        info['cpu'] = cpu_name.strip()
    except Exception:
        pass

    try:
        info['cpu_cores'] = os.cpu_count()
    except Exception:
        pass

    # Disques logiques fixes (kernel32) - taille, utilisé, libre
    try:
        import ctypes

        disk_list = []
        total_disk = 0.0
        total_free = 0.0
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if bitmask & (1 << i):
                drive = f"{letter}:\\"
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive)
                if drive_type == 3:  # DRIVE_FIXED
                    total_bytes = ctypes.c_ulonglong(0)
                    free_bytes = ctypes.c_ulonglong(0)
                    ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                        drive, None, ctypes.byref(total_bytes), ctypes.byref(free_bytes)
                    )
                    if ok:
                        size_gb = round(total_bytes.value / (1024 ** 3), 1)
                        free_gb = round(free_bytes.value / (1024 ** 3), 1)
                        used_gb = round(size_gb - free_gb, 1)
                        disk_list.append(f"{letter}: — {size_gb} GB total, {used_gb} GB utilisés, {free_gb} GB libres")
                        total_disk += size_gb
                        total_free += free_gb
        if disk_list:
            info['disk_drives'] = disk_list
            info['disk_total_gb'] = round(total_disk, 1)
            info['disk_free_gb'] = round(total_free, 1)
            info['disk_used_gb'] = round(total_disk - total_free, 1)
    except Exception:
        pass

    return info


def _win_core():
    """Identification matérielle : marque/modèle/domaine/BIOS/uptime/GPU.

    Un seul appel PowerShell groupé - chaque champ est protégé individuellement
    pour qu'une source manquante ne fasse pas échouer les autres.
    """
    info = {}
    core_data = _win_powershell_json(
        "$cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue; "
        "$bios = Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue; "
        "$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue; "
        "$gpuNames = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty Name); "
        "$uptimeHours = $null; "
        "if ($os -and $os.LastBootUpTime) { $uptimeHours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1) }; "
        "$installDate = $null; "
        "if ($os -and $os.InstallDate) { $installDate = $os.InstallDate.ToString('yyyy-MM-dd') }; "
        "$biosDate = $null; "
        "if ($bios -and $bios.ReleaseDate) { $biosDate = $bios.ReleaseDate.ToString('yyyy-MM-dd') }; "
        # .DisplayName ('(UTC+01:00) Bruxelles, Copenhague, Madrid, Paris'), pas .Id
        # ('Romance Standard Time') : l'identifiant interne Windows ne dit rien à
        # personne hors de la table de correspondance des fuseaux du registre.
        "$tz = $null; try { $tz = (Get-TimeZone -ErrorAction SilentlyContinue).DisplayName } catch {}; "
        "[PSCustomObject]@{ "
        "Manufacturer=$cs.Manufacturer; Model=$cs.Model; Domain=$cs.Domain; "
        "PartOfDomain=$cs.PartOfDomain; Workgroup=$cs.Workgroup; LoggedOnUser=$cs.UserName; "
        "SerialNumber=$bios.SerialNumber; BiosVersion=$bios.SMBIOSBIOSVersion; "
        "BiosManufacturer=$bios.Manufacturer; BiosReleaseDate=$biosDate; "
        "UptimeHours=$uptimeHours; OsInstallDate=$installDate; TimeZone=$tz; "
        "GpuNames=$gpuNames } | ConvertTo-Json -Compress -Depth 4",
        timeout=25
    )
    if not core_data:
        return info

    info['brand'] = _clean(core_data.get('Manufacturer'))
    info['model'] = _clean(core_data.get('Model'))
    info['serial_number'] = _clean(core_data.get('SerialNumber'))

    bios_version = core_data.get('BiosVersion')
    if isinstance(bios_version, list):
        bios_version = ', '.join(str(v) for v in bios_version if v)
    if bios_version:
        info['bios_version'] = _clean(bios_version)
    if core_data.get('BiosManufacturer'):
        info['bios_manufacturer'] = _clean(core_data.get('BiosManufacturer'))
    if core_data.get('BiosReleaseDate'):
        info['bios_release_date'] = core_data.get('BiosReleaseDate')

    if core_data.get('PartOfDomain'):
        info['domain'] = core_data.get('Domain') or ''
    elif core_data.get('Workgroup'):
        info['workgroup'] = core_data.get('Workgroup')

    if core_data.get('LoggedOnUser'):
        info['logged_on_user'] = core_data.get('LoggedOnUser')
    if core_data.get('UptimeHours') is not None:
        info['uptime_hours'] = core_data.get('UptimeHours')
    if core_data.get('OsInstallDate'):
        info['os_install_date'] = core_data.get('OsInstallDate')
    if core_data.get('TimeZone'):
        info['timezone'] = core_data.get('TimeZone')

    gpus = [g for g in _as_list(core_data.get('GpuNames')) if g]
    if gpus:
        info['gpu'] = ', '.join(gpus)

    return info


def _win_hardware_detail():
    """Carte mère, châssis, barrettes mémoire, CPU détaillé, GPU détaillé.

    C'est le cœur de la parité Belarc : la mémoire par slot répond à
    "peut-on upgrader cette machine ?" sans démonter le poste.
    """
    info = {}
    data = _win_powershell_json(
        "$bb = Get-CimInstance Win32_BaseBoard -ErrorAction SilentlyContinue | Select-Object -First 1 "
        "Manufacturer,Product,Version,SerialNumber; "
        "$enc = Get-CimInstance Win32_SystemEnclosure -ErrorAction SilentlyContinue | Select-Object -First 1 "
        "ChassisTypes,SMBIOSAssetTag,SerialNumber; "
        "$cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue; "
        "$cpus = @(Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object "
        "Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,L2CacheSize,L3CacheSize,"
        "SocketDesignation,VirtualizationFirmwareEnabled,AddressWidth); "
        "$mem = @(Get-CimInstance Win32_PhysicalMemory -ErrorAction SilentlyContinue | Select-Object "
        "BankLabel,DeviceLocator,Capacity,Speed,ConfiguredClockSpeed,Manufacturer,PartNumber,"
        "SerialNumber,SMBIOSMemoryType,FormFactor); "
        "$memArr = Get-CimInstance Win32_PhysicalMemoryArray -ErrorAction SilentlyContinue "
        "| Select-Object -First 1 MaxCapacityEx,MemoryDevices; "
        "$gpu = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | Select-Object "
        "Name,DriverVersion,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate,"
        "@{N='DriverDate';E={ if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } else { '' } }}); "
        "$gpuVram = @(); try { $gpuVram = @(Get-ItemProperty -Path "
        "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\0*' "
        "-ErrorAction SilentlyContinue | Where-Object { $_.'HardwareInformation.qwMemorySize' } "
        "| Select-Object DriverDesc,@{N='Vram';E={ [uint64]$_.'HardwareInformation.qwMemorySize' }}) } catch {}; "
        "[PSCustomObject]@{ BaseBoard=$bb; Enclosure=$enc; PCSystemType=$cs.PCSystemType; "
        "HypervisorPresent=$cs.HypervisorPresent; Cpus=$cpus; Memory=$mem; MemoryArray=$memArr; "
        "Gpu=$gpu; GpuVram=$gpuVram } | ConvertTo-Json -Compress -Depth 5",
        timeout=35
    )
    if not data:
        return info

    # ── Carte mère ─────────────────────────────────────────────────────────
    bb = data.get('BaseBoard') or {}
    if bb:
        info['motherboard'] = {
            'manufacturer': _clean(bb.get('Manufacturer')),
            'model': _clean(bb.get('Product')),
            'version': _clean(bb.get('Version')),
            'serial_number': _clean(bb.get('SerialNumber')),
        }

    # ── Châssis (→ type d'appareil) ────────────────────────────────────────
    enc = data.get('Enclosure') or {}
    chassis_codes = [c for c in _as_list(enc.get('ChassisTypes')) if isinstance(c, int)]
    if chassis_codes:
        code = chassis_codes[0]
        info['chassis_type'] = CHASSIS_TYPES.get(code, f'Code {code}')
        info['chassis_code'] = code
    if _clean(enc.get('SMBIOSAssetTag')):
        info['asset_tag'] = _clean(enc.get('SMBIOSAssetTag'))

    if data.get('HypervisorPresent'):
        info['hypervisor_present'] = True

    # ── CPU détaillé ───────────────────────────────────────────────────────
    cpus = _as_list(data.get('Cpus'))
    if cpus:
        first = cpus[0]
        info['cpu_sockets'] = len(cpus)
        if first.get('NumberOfCores'):
            info['cpu_physical_cores'] = sum(c.get('NumberOfCores') or 0 for c in cpus)
        if first.get('NumberOfLogicalProcessors'):
            info['cpu_logical_cores'] = sum(c.get('NumberOfLogicalProcessors') or 0 for c in cpus)
        if first.get('MaxClockSpeed'):
            info['cpu_max_clock_mhz'] = first.get('MaxClockSpeed')
        if first.get('L3CacheSize'):
            info['cpu_l3_cache_kb'] = first.get('L3CacheSize')
        if first.get('L2CacheSize'):
            info['cpu_l2_cache_kb'] = first.get('L2CacheSize')
        if first.get('SocketDesignation'):
            info['cpu_socket'] = _clean(first.get('SocketDesignation'))
        if first.get('VirtualizationFirmwareEnabled') is not None:
            info['cpu_virtualization'] = bool(first.get('VirtualizationFirmwareEnabled'))
        if first.get('AddressWidth'):
            info['cpu_address_width'] = first.get('AddressWidth')

    # ── Barrettes mémoire par slot ─────────────────────────────────────────
    modules = []
    for m in _as_list(data.get('Memory')):
        capacity = m.get('Capacity') or 0
        try:
            capacity_gb = round(int(capacity) / (1024 ** 3), 1)
        except (TypeError, ValueError):
            capacity_gb = 0
        mem_type = MEMORY_TYPES.get(m.get('SMBIOSMemoryType'), '')
        modules.append({
            'slot': _clean(m.get('DeviceLocator')) or _clean(m.get('BankLabel')) or 'Slot inconnu',
            'bank': _clean(m.get('BankLabel')),
            'capacity_gb': capacity_gb,
            'type': mem_type,
            'form_factor': MEMORY_FORM_FACTORS.get(m.get('FormFactor'), ''),
            # ConfiguredClockSpeed = fréquence réelle, Speed = fréquence nominale
            'speed_mhz': m.get('ConfiguredClockSpeed') or m.get('Speed') or '',
            'rated_speed_mhz': m.get('Speed') or '',
            'manufacturer': _clean(m.get('Manufacturer')),
            'part_number': _clean(m.get('PartNumber')),
            'serial_number': _clean(m.get('SerialNumber')),
        })
    if modules:
        info['memory_modules'] = modules

    mem_arr = data.get('MemoryArray') or {}
    if mem_arr.get('MemoryDevices'):
        info['memory_slots_total'] = mem_arr.get('MemoryDevices')
        info['memory_slots_used'] = len(modules)
        info['memory_slots_free'] = max(0, mem_arr.get('MemoryDevices') - len(modules))
    if mem_arr.get('MaxCapacityEx'):
        try:
            # MaxCapacityEx est exprimé en kilo-octets
            info['memory_max_gb'] = round(int(mem_arr['MaxCapacityEx']) / (1024 ** 2), 1)
        except (TypeError, ValueError):
            pass

    # ── GPU détaillé ───────────────────────────────────────────────────────
    # AdapterRAM de Win32_VideoController est un int32 signé : il déborde
    # au-delà de 4 Go et renvoie des valeurs aberrantes. La VRAM réelle se lit
    # dans le registre (qwMemorySize), d'où la jointure sur le nom du pilote.
    vram_by_name = {}
    for entry in _as_list(data.get('GpuVram')):
        desc = _clean(entry.get('DriverDesc'))
        if desc and entry.get('Vram'):
            vram_by_name[desc.lower()] = entry['Vram']

    gpu_details = []
    for g in _as_list(data.get('Gpu')):
        name = _clean(g.get('Name'))
        if not name:
            continue
        entry = {
            'name': name,
            'driver_version': _clean(g.get('DriverVersion')),
            'driver_date': _clean(g.get('DriverDate')),
        }
        vram = vram_by_name.get(name.lower())
        if vram:
            entry['vram_gb'] = round(int(vram) / (1024 ** 3), 1)
        h, v = g.get('CurrentHorizontalResolution'), g.get('CurrentVerticalResolution')
        if h and v:
            entry['resolution'] = f"{h}x{v}"
            if g.get('CurrentRefreshRate'):
                entry['resolution'] += f" @ {g.get('CurrentRefreshRate')} Hz"
        gpu_details.append(entry)
    if gpu_details:
        info['gpu_details'] = gpu_details

    return info


def _diagonal_inch(largeur_cm, hauteur_cm):
    """Diagonale d'un écran en pouces à partir de ses dimensions EDID (en cm).

    L'EDID donne les dimensions de la dalle ; la diagonale commerciale s'en
    déduit directement et parle davantage qu'un couple de centimètres.
    """
    try:
        l = float(largeur_cm)
        h = float(hauteur_cm)
    except (TypeError, ValueError):
        return None
    if l <= 0 or h <= 0:
        return None
    return round(((l ** 2 + h ** 2) ** 0.5) / 2.54, 1)


def hardware_age_years(bios_date):
    """Âge approximatif du matériel, déduit de la date du BIOS.

    Ce n'est pas la date d'achat, mais sur un parc c'est le seul repère
    disponible sans interroger les API constructeur : un BIOS de 2016 signale
    une machine à renouveler.
    """
    texte = _clean(bios_date)
    if not texte:
        return None
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', texte)
    if not m:
        return None
    try:
        annee, mois, jour = (int(x) for x in m.groups())
        delta = _utcnow() - datetime(annee, mois, jour)
        return round(delta.days / 365.25, 1)
    except (ValueError, OverflowError):
        return None


def _win_inventory():
    """Écrans, imprimantes, fiabilité des disques, ports en écoute."""
    info = {}
    data = _win_powershell_json(
        # Écrans : WmiMonitorID expose des tableaux d'entiers UTF-16 terminés par
        # des zéros, il faut les reconstituer en chaînes côté PowerShell
        "$mon = @(); try { $mon = @(Get-CimInstance -Namespace root\\wmi -ClassName WmiMonitorID "
        "-ErrorAction SilentlyContinue | ForEach-Object { [PSCustomObject]@{ "
        "Manufacturer = (($_.ManufacturerName | Where-Object { $_ -gt 0 }) | ForEach-Object { [char]$_ }) -join ''; "
        "Model = (($_.UserFriendlyName | Where-Object { $_ -gt 0 }) | ForEach-Object { [char]$_ }) -join ''; "
        "Serial = (($_.SerialNumberID | Where-Object { $_ -gt 0 }) | ForEach-Object { [char]$_ }) -join ''; "
        "Year = $_.YearOfManufacture; Instance = $_.InstanceName } }) } catch {}; "
        # Dimensions physiques (en cm) : permettent de calculer la diagonale
        "$dim = @(); try { $dim = @(Get-CimInstance -Namespace root\\wmi "
        "-ClassName WmiMonitorBasicDisplayParams -ErrorAction SilentlyContinue "
        "| Select-Object InstanceName,MaxHorizontalImageSize,MaxVerticalImageSize) } catch {}; "
        "$printers = @(); try { $printers = @(Get-CimInstance Win32_Printer -ErrorAction SilentlyContinue "
        "| Select-Object Name,DriverName,PortName,Network,Default,Shared) } catch {}; "
        # Fiabilité disque : heures de fonctionnement / usure SSD / température
        "$rel = @(); try { $rel = @(Get-PhysicalDisk -ErrorAction SilentlyContinue | ForEach-Object { "
        "$d = $_; $c = $null; try { $c = $d | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue } catch {}; "
        "if ($c) { [PSCustomObject]@{ Name=$d.FriendlyName; Serial=$d.SerialNumber; "
        "PowerOnHours=$c.PowerOnHours; Wear=$c.Wear; Temperature=$c.Temperature; "
        "ReadErrors=$c.ReadErrorsTotal; WriteErrors=$c.WriteErrorsTotal } } }) } catch {}; "
        "$ports = @(); try { $pmap = @{}; "
        "Get-Process -ErrorAction SilentlyContinue | ForEach-Object { $pmap[[string]$_.Id] = $_.ProcessName }; "
        "$ports = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue "
        "| Group-Object LocalPort | ForEach-Object { $g = $_.Group | Select-Object -First 1; "
        "[PSCustomObject]@{ Port=[int]$_.Name; Process=$pmap[[string]$g.OwningProcess] } } "
        "| Sort-Object Port) } catch {}; "
        "[PSCustomObject]@{ Monitors=$mon; MonitorDims=$dim; Printers=$printers; Reliability=$rel; Ports=$ports } "
        "| ConvertTo-Json -Compress -Depth 4",
        timeout=40
    )
    if not data:
        return info

    # ── Écrans ─────────────────────────────────────────────────────────────
    # Dimensions physiques indexées par instance, pour calculer la diagonale
    dimensions = {}
    for d in _as_list(data.get('MonitorDims')):
        instance = _clean(d.get('InstanceName'))
        if instance:
            dimensions[instance] = (d.get('MaxHorizontalImageSize'),
                                    d.get('MaxVerticalImageSize'))

    monitors = []
    for m in _as_list(data.get('Monitors')):
        model = _clean(m.get('Model'))
        manufacturer = _clean(m.get('Manufacturer'))
        if not model and not manufacturer:
            continue
        # Les écrans sans EDID complet (dummy HDMI, KVM, adaptateurs) renvoient
        # un série "0" : le garder créerait un périphérique dédoublonné sur "0"
        serial = _clean(m.get('Serial'))
        if serial and not serial.strip('0'):
            serial = ''
        largeur, hauteur = dimensions.get(_clean(m.get('Instance')), (None, None))
        monitors.append({
            'manufacturer': manufacturer,
            'model': model,
            'serial_number': serial,
            'year': m.get('Year') or '',
            'diagonal_inch': _diagonal_inch(largeur, hauteur),
        })
    if monitors:
        info['monitors'] = monitors

    # ── Imprimantes ────────────────────────────────────────────────────────
    printers = []
    for p in _as_list(data.get('Printers')):
        name = _clean(p.get('Name'))
        if not name:
            continue
        driver = _clean(p.get('DriverName'))
        port = _clean(p.get('PortName'))
        printers.append({
            'name': name,
            'driver': driver,
            'port': port,
            'network': bool(p.get('Network')),
            'default': bool(p.get('Default')),
            'shared': bool(p.get('Shared')),
            # Les imprimantes virtuelles restent dans le rapport mais ne doivent
            # pas polluer l'inventaire matériel côté serveur
            'virtual': is_virtual_printer(name, driver, port),
            'connection': printer_connection(port, bool(p.get('Network'))),
        })
    if printers:
        info['printers'] = printers

    # ── Fiabilité / usure des disques ──────────────────────────────────────
    reliability = []
    for r in _as_list(data.get('Reliability')):
        name = _clean(r.get('Name'))
        if not name:
            continue
        entry = {'name': name, 'serial_number': _clean(r.get('Serial'))}
        for src, dst in (('PowerOnHours', 'power_on_hours'), ('Wear', 'wear_percent'),
                         ('Temperature', 'temperature_c'), ('ReadErrors', 'read_errors'),
                         ('WriteErrors', 'write_errors')):
            if r.get(src) is not None:
                entry[dst] = r.get(src)
        reliability.append(entry)
    if reliability:
        info['disk_reliability'] = reliability

    # ── Ports TCP en écoute ────────────────────────────────────────────────
    ports = []
    for p in _as_list(data.get('Ports')):
        if p.get('Port') is None:
            continue
        ports.append({'port': p.get('Port'), 'process': _clean(p.get('Process'))})
    if ports:
        info['listening_ports'] = ports

    return info


def _win_licensing():
    """Licences Windows/Office, activation, et liste complète des correctifs."""
    info = {}
    data = _win_powershell_json(
        # Le filtre WQL est indispensable : énumérer SoftwareLicensingProduct
        # sans filtre parcourt plusieurs centaines d'entrées et prend >30 s
        "$lic = @(); try { $lic = @(Get-CimInstance SoftwareLicensingProduct "
        "-Filter 'PartialProductKey IS NOT NULL' -ErrorAction SilentlyContinue "
        "| Select-Object Name,Description,PartialProductKey,LicenseStatus,ProductKeyChannel,GenuineStatus) } catch {}; "
        "$olic = @(); try { $olic = @(Get-CimInstance OfficeSoftwareProtectionProduct "
        "-Filter 'PartialProductKey IS NOT NULL' -ErrorAction SilentlyContinue "
        "| Select-Object Name,Description,PartialProductKey,LicenseStatus,ProductKeyChannel) } catch {}; "
        "$oem = ''; try { $oem = (Get-CimInstance SoftwareLicensingService "
        "-ErrorAction SilentlyContinue).OA3xOriginalProductKey } catch {}; "
        "$hf = @(); try { $hf = @(Get-HotFix -ErrorAction SilentlyContinue "
        "| Select-Object HotFixID,Description,@{N='InstalledOn';E={ if ($_.InstalledOn) "
        "{ $_.InstalledOn.ToString('yyyy-MM-dd') } else { '' } }}) } catch {}; "
        "[PSCustomObject]@{ Licenses=$lic; OfficeLicenses=$olic; OemKey=$oem; HotFixes=$hf } "
        "| ConvertTo-Json -Compress -Depth 4",
        timeout=60
    )
    if not data:
        return info

    licenses = []
    for source in ('Licenses', 'OfficeLicenses'):
        for lic in _as_list(data.get(source)):
            name = _clean(lic.get('Name'))
            if not name:
                continue
            status_code = lic.get('LicenseStatus')
            licenses.append({
                'name': name,
                'description': _clean(lic.get('Description')),
                'partial_key': _clean(lic.get('PartialProductKey')),
                'status': LICENSE_STATUS.get(status_code, f'Code {status_code}'),
                'activated': status_code == 1,
                'channel': _clean(lic.get('ProductKeyChannel')),
            })
    if licenses:
        info['licenses'] = licenses
        # Le produit Windows lui-même sert de statut d'activation global
        windows_lic = next((l for l in licenses if 'windows' in l['name'].lower()), None)
        if windows_lic:
            info['windows_activated'] = windows_lic['activated']
            info['windows_license_channel'] = windows_lic['channel']

    if _clean(data.get('OemKey')):
        info['oem_product_key'] = _clean(data.get('OemKey'))

    # Compléter chaque licence avec sa clé entière quand elle est récupérable,
    # et vérifier qu'il s'agit bien de celle en service.
    try:
        enrich_licenses_with_keys(info)
    except Exception:
        pass

    hotfixes = []
    for hf in _as_list(data.get('HotFixes')):
        hf_id = _clean(hf.get('HotFixID'))
        if not hf_id:
            continue
        hotfixes.append({
            'id': hf_id,
            'description': _clean(hf.get('Description')),
            'installed_on': _clean(hf.get('InstalledOn')),
        })
    if hotfixes:
        # Tri décroissant par date ; les correctifs sans date passent en fin
        hotfixes.sort(key=lambda h: h['installed_on'] or '', reverse=True)
        info['hotfixes'] = hotfixes
        latest = hotfixes[0]
        date_part = f" ({latest['installed_on']})" if latest['installed_on'] else ''
        info['last_windows_update'] = f"{latest['id']}{date_part}"

    return info


def _decode_antivirus_state(nom, product_state):
    """Décode le `productState` du Centre de sécurité Windows.

    C'est un entier dont l'écriture hexadécimale sur 6 chiffres porte trois
    informations : le fournisseur, l'état de la protection temps réel et la
    fraîcheur des signatures. Sans ce décodage, un antivirus installé mais
    désactivé s'affiche exactement comme un antivirus opérationnel.
    """
    actif = a_jour = None
    try:
        brut = '%06x' % int(product_state)
        # Octet du milieu : bit 0x10 = protection temps réel active
        actif = bool(int(brut[2:4], 16) & 0x10)
        # Dernier octet : 0x00 = signatures à jour
        a_jour = int(brut[4:6], 16) == 0
    except (TypeError, ValueError):
        pass

    if actif is None:
        statut = 'État inconnu'
    elif not actif:
        statut = 'Inactif'
    elif a_jour is False:
        statut = 'Actif, signatures obsolètes'
    else:
        statut = 'Actif'
    return {'name': nom, 'enabled': actif, 'up_to_date': a_jour, 'status': statut}


def _win_security():
    """Antivirus, pare-feu, BitLocker, TPM, Secure Boot.

    Modules non garantis selon l'édition Windows - chaque source est protégée
    par un try/catch PowerShell dédié pour ne pas faire échouer les autres.
    """
    info = {}
    security_data = _win_powershell_json(
        "$av = @(); try { $av = @(Get-CimInstance -Namespace root/SecurityCenter2 "
        "-ClassName AntivirusProduct -ErrorAction SilentlyContinue "
        "| Select-Object displayName,productState) } catch {}; "
        "$fw = @(); try { $fw = @(Get-NetFirewallProfile -ErrorAction SilentlyContinue "
        "| Select-Object Name,Enabled) } catch {}; "
        "$bl = @(); try { $bl = @(Get-BitLockerVolume -ErrorAction SilentlyContinue "
        # Les deux états sont des énumérations : sans conversion en chaîne, la
        # sérialisation JSON ne renvoie que des entiers, et la fiche affichait
        # « D:: 0 (Protection: 0) ».
        "| Select-Object MountPoint,@{N='VolumeStatus';E={[string]$_.VolumeStatus}},"
        "@{N='ProtectionStatus';E={[string]$_.ProtectionStatus}},"
        "@{N='EncryptionMethod';E={[string]$_.EncryptionMethod}}) } catch {}; "
        "$tpmObj = $null; try { $tpmObj = Get-Tpm -ErrorAction SilentlyContinue "
        "| Select-Object TpmPresent,TpmReady,TpmEnabled } catch {}; "
        "$secureBoot = $null; try { $secureBoot = Confirm-SecureBootUEFI -ErrorAction SilentlyContinue } catch {}; "
        "[PSCustomObject]@{ Antivirus=$av; Firewall=$fw; BitLocker=$bl; Tpm=$tpmObj; SecureBoot=$secureBoot } "
        "| ConvertTo-Json -Compress -Depth 4",
        timeout=25
    )
    if not security_data:
        return info

    produits = []
    for a in _as_list(security_data.get('Antivirus')):
        if isinstance(a, dict):
            nom = _clean(a.get('displayName'))
            if nom:
                produits.append(_decode_antivirus_state(nom, a.get('productState')))
        elif a:
            # Collecte antérieure : seul le nom était remonté
            produits.append({'name': _clean(a), 'enabled': None, 'up_to_date': None,
                             'status': 'État inconnu'})
    if produits:
        info['antivirus'] = ', '.join(p['name'] for p in produits)
        info['antivirus_products'] = produits

    profiles = [
        f"{f.get('Name')}: {'Activé' if f.get('Enabled') else 'Désactivé'}"
        for f in _as_list(security_data.get('Firewall')) if f.get('Name')
    ]
    if profiles:
        info['firewall'] = profiles
        # Forme structurée : évite d'avoir à réanalyser « Domain: Activé » à
        # l'affichage pour décider de la couleur du badge.
        info['firewall_profiles'] = [
            {'name': _clean(f.get('Name')), 'enabled': bool(f.get('Enabled'))}
            for f in _as_list(security_data.get('Firewall')) if f.get('Name')
        ]

    volumes = []
    for b in _as_list(security_data.get('BitLocker')):
        point = _clean(b.get('MountPoint'))
        if not point:
            continue
        etat = _clean(b.get('VolumeStatus'))
        protection = _clean(b.get('ProtectionStatus'))
        volumes.append({
            'volume': point,
            'etat': _ETATS_VOLUME.get(etat, etat or 'Inconnu'),
            'protection': _PROTECTIONS_VOLUME.get(protection, protection or 'Inconnu'),
            # Les anciennes versions de Windows renvoient des nombres, les
            # récentes des libellés : les deux formes sont reconnues.
            'chiffre': etat in ('1', 'FullyEncrypted'),
            'protege': protection in ('1', 'On'),
            'methode': _clean(b.get('EncryptionMethod')),
        })

    if volumes:
        info['bitlocker_volumes'] = volumes
        # Forme lisible conservée pour les rendus qui affichent une simple liste.
        info['bitlocker'] = ['%s : %s (protection %s)'
                             % (v['volume'], v['etat'], v['protection'].lower())
                             for v in volumes]
        info['bitlocker_actif'] = any(v['protege'] for v in volumes)

    tpm = security_data.get('Tpm')
    if tpm:
        info['tpm_present'] = bool(tpm.get('TpmPresent'))
        info['tpm_enabled'] = bool(tpm.get('TpmEnabled'))

    if security_data.get('SecureBoot') is not None:
        info['secure_boot'] = bool(security_data.get('SecureBoot'))

    return info


def _win_console_output(cmd, timeout=30):
    """Exécute un outil console hérité (netsh, gpresult…) et décode sa sortie
    avec la page de code OEM active.

    Ces outils écrivent dans la page de code OEM du système — 850 en
    français, 437 en anglais US, etc. — jamais en UTF-8. Décoder autrement
    (`_run()`, pensé pour des outils déjà UTF-8) corrompt silencieusement tout
    libellé accentué (« Découverte du réseau », « Activé »…). `GetOEMCP()`
    donne la page de code réellement active plutôt que d'en supposer une,
    ce qui reste valable quelle que soit la langue de Windows.
    """
    try:
        import ctypes
        page_de_code = 'cp%d' % ctypes.windll.kernel32.GetOEMCP()
    except Exception:
        page_de_code = 'cp850'
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        resultat = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=creationflags)
        return (resultat.stdout or b'').decode(page_de_code, errors='replace')
    except Exception:
        return ''


_RE_GUID_FW = re.compile(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}')

# Alias FR/EN par champ : contrairement à `netsh wlan` ou `gpresult`, `netsh
# advfirewall` n'a pas d'export XML. Certains libellés restent d'ailleurs en
# anglais même sur un Windows français (« LocalPort », « Profiles ») — la
# preuve que ce n'est pas uniforme, d'où la couverture des deux langues plutôt
# que le pari sur une seule.
_FW_LABELS = {
    'nom': {'nom de la regle', 'rule name'},
    'actif': {'active', 'enabled'},
    'groupe': {'groupement', 'grouping'},
    'protocole': {'protocole', 'protocol'},
    'port': {'localport', 'local port'},
    'action': {'action'},
    'profils': {'profiles', 'profils'},
}


def _parse_regles_pare_feu(texte):
    """Parse la sortie de `netsh advfirewall firewall show rule name=all`.

    Fonction pure, séparée de _win_firewall_rules pour rester testable sans
    lancer netsh — même principe que _parse_wifi_profile_xml. Chaque règle
    devient un dict {nom, actif, groupe, protocole, port, action, profils} ;
    un champ absent de la sortie pour une règle donnée est simplement absent
    du dict plutôt que forcé à une valeur par défaut trompeuse.
    """
    regles = []
    cur = {}
    for ligne in (texte or '').splitlines():
        if ':' not in ligne:
            continue
        cle_brute, _, valeur = ligne.partition(':')
        cle = _strip_accents(cle_brute).strip().lower()
        valeur = valeur.strip()
        if cle in _FW_LABELS['nom']:
            if cur.get('nom'):
                regles.append(cur)
            cur = {'nom': valeur}
        elif cle in _FW_LABELS['actif']:
            cur['actif'] = _strip_accents(valeur).strip().lower() in ('oui', 'yes')
        elif cle in _FW_LABELS['groupe']:
            cur['groupe'] = valeur
        elif cle in _FW_LABELS['protocole']:
            cur['protocole'] = valeur
        elif cle in _FW_LABELS['port']:
            cur['port'] = valeur
        elif cle in _FW_LABELS['action']:
            cur['action'] = _strip_accents(valeur).strip().lower() in ('autoriser', 'allow')
        elif cle in _FW_LABELS['profils']:
            cur['profils'] = valeur
    if cur.get('nom'):
        regles.append(cur)
    return regles


_FW_DIRECTIONS = (('in', 'Entrant'), ('out', 'Sortant'))


def _win_firewall_rules(limite=150):
    """Règles de pare-feu, entrantes et sortantes, qui ouvrent réellement quelque chose.

    Un poste de travail compte facilement plus d'un millier de règles :
    Windows en embarque des centaines par fonctionnalité (Découverte réseau,
    Partage d'imprimantes…), chacune démultipliée par profil et protocole —
    vérifié à plus de 1500 côté entrant seul sur une machine de développement
    ordinaire. Cette avalanche est délibérément écartée : ces règles groupées
    se pilotent comme un bloc depuis les paramètres réseau, pas une par une,
    et leur état global apparaît déjà dans `firewall_profiles`. Les noms
    comme « HNS Container Networking - <GUID> - 0 », que Docker/Hyper-V
    régénèrent à chaque réseau de conteneur créé, sont écartés pour la même
    raison : churn technique, jamais une configuration à auditer.

    Ce qui reste après filtre — actives, autorisées, sans groupe Windows, sans
    nom généré — ce sont les trous ouverts par des logiciels installés : la
    vraie question d'audit (« qu'est-ce qui peut joindre ce poste, et qu'est-ce
    que ce poste peut joindre »). La direction demandée à `netsh` (`dir=in`/
    `dir=out`) est posée directement sur chaque règle plutôt que reparsée
    depuis la sortie — netsh y répète l'état d'activation, pas « Entrant »/
    « Sortant » en toutes lettres, un champ « Direction » qui dirait le
    contraire de son nom serait trompeur à interpréter. Les entrées de même
    nom ET même direction (plusieurs protocoles, mises à jour successives du
    même logiciel) sont fusionnées en une seule ligne ; entrant et sortant
    d'un même logiciel restent deux lignes distinctes, un programme pouvant
    très bien autoriser l'un sans l'autre.
    """
    toutes = []
    for drapeau, libelle in _FW_DIRECTIONS:
        texte = _win_console_output(
            ['netsh', 'advfirewall', 'firewall', 'show', 'rule', 'name=all', 'dir=' + drapeau],
            timeout=60)
        for r in _parse_regles_pare_feu(texte):
            r['direction'] = libelle
            toutes.append(r)
    return _filtrer_fusionner_regles_pare_feu(toutes, limite)


def _filtrer_fusionner_regles_pare_feu(toutes, limite=150):
    """Filtre et fusionne des règles déjà parsées (voir _win_firewall_rules).

    Séparée de _win_firewall_rules pour rester testable sans lancer netsh —
    même principe que _parse_wifi_profile_xml. Ne garde que les règles
    actives, autorisées, sans groupe Windows et sans nom généré
    automatiquement ; fusionne les entrées de même nom ET même direction
    (protocoles/ports/profils réunis), triées par nom puis par direction.
    """
    if not toutes:
        return {}

    filtrees = [r for r in toutes if r.get('actif') and r.get('action')
                and not (r.get('groupe') or '').strip()
                and not _RE_GUID_FW.search(r.get('nom') or '')]

    fusion = {}
    ordre = []
    for r in filtrees:
        cle = (r.get('nom') or '?', r.get('direction') or 'Entrant')
        if cle not in fusion:
            fusion[cle] = {'protocoles': set(), 'ports': set(), 'profils': set()}
            ordre.append(cle)
        e = fusion[cle]
        if r.get('protocole'):
            e['protocoles'].add(r['protocole'])
        if r.get('port'):
            e['ports'].add(r['port'])
        for p in (r.get('profils') or '').split(','):
            p = p.strip()
            if p:
                e['profils'].add(p)

    if not ordre:
        return {}

    cles_triees = sorted(ordre, key=lambda c: (c[0].lower(), c[1]))
    regles = []
    for cle in cles_triees[:limite]:
        nom, direction = cle
        e = fusion[cle]
        regles.append({
            'name': nom,
            'direction': direction,
            'protocol': '/'.join(sorted(e['protocoles'])),
            'port': ', '.join(sorted(e['ports'])),
            'profiles': ', '.join(sorted(e['profils'])),
        })

    return {'firewall_rules': regles, 'firewall_rules_total': len(cles_triees)}


def _parse_portproxy(texte):
    """Parse la sortie de `netsh interface portproxy show all`.

    Fonction pure, séparée de _win_port_forwards pour rester testable — même
    principe que _parse_regles_pare_feu. Le format est un tableau à largeur
    fixe (adresse/port d'écoute, adresse/port de destination), stable depuis
    Windows XP mais SANS libellés de champs à faire correspondre comme pour
    `netsh advfirewall` : on retient simplement toute ligne à exactement 4
    jetons dont le 2ᵉ et le 4ᵉ sont des nombres (les ports) — ça élimine
    silencieusement l'en-tête, la ligne de séparateurs et les lignes vides
    sans avoir à connaître leur texte exact, y compris dans une langue non
    prévue.
    """
    redirections = []
    for ligne in (texte or '').splitlines():
        morceaux = ligne.split()
        if len(morceaux) != 4:
            continue
        adresse_ecoute, port_ecoute, adresse_dest, port_dest = morceaux
        if not (port_ecoute.isdigit() and port_dest.isdigit()):
            continue
        redirections.append({
            'listen_address': adresse_ecoute, 'listen_port': int(port_ecoute),
            'connect_address': adresse_dest, 'connect_port': int(port_dest),
        })
    return redirections


def _win_port_forwards():
    """Redirections de port locales (`netsh interface portproxy`).

    Un troisième mécanisme de redirection silencieuse, distinct des deux
    autres déjà collectés : le pare-feu décide ce qui peut ENTRER, le
    fichier hosts redirige par NOM, portproxy redirige au niveau du PORT,
    indépendamment des deux premiers — invisible dans l'un comme dans
    l'autre. Généralement vide sur un poste ordinaire (aucun filtre de
    volume nécessaire, contrairement aux règles de pare-feu) ; un service
    métier qui redirige un port pour contourner une restriction en laisse
    la trace ici, et nulle part ailleurs dans cette collecte.
    """
    texte = _win_console_output(['netsh', 'interface', 'portproxy', 'show', 'all'], timeout=20)
    redirections = _parse_portproxy(texte)
    return {'port_forwards': redirections} if redirections else {}


# PrincipalSource de Get-LocalUser → libellé du type de compte.
_ACCOUNT_SOURCES = {
    'local': 'Local',
    'microsoftaccount': 'Microsoft',
    'azuread': 'Microsoft Entra',
    'activedirectory': 'Domaine',
}


def _win_users():
    """Comptes locaux : état, appartenance Administrateurs et type de compte.

    Le groupe Administrateurs est résolu par son SID connu S-1-5-32-544 et non
    par son nom : « Administrators » n'existe pas sur un Windows français, où le
    groupe s'appelle « Administrateurs ». L'interrogation par nom levait donc une
    GroupNotFoundException et aucun compte n'était jamais signalé comme
    administrateur sur un Windows non anglophone.
    """
    info = {}
    users_data = _win_powershell_json(
        "$users = @(Get-LocalUser -ErrorAction SilentlyContinue "
        "| Select-Object Name,Enabled,Description,"
        "@{N='Source';E={[string]$_.PrincipalSource}},@{N='SID';E={$_.SID.Value}},"
        "@{N='NeverExpires';E={$null -eq $_.PasswordExpires}},"
        "@{N='LastLogon';E={ if ($_.LastLogon) { $_.LastLogon.ToString('yyyy-MM-dd') } else { '' } }}); "
        "$admins = @(); try { "
        "$grp = (Get-LocalGroup -ErrorAction SilentlyContinue "
        "| Where-Object { $_.SID.Value -eq 'S-1-5-32-544' }).Name; "
        "if ($grp) { $admins = @(Get-LocalGroupMember -Group $grp -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty Name) } } catch {}; "
        "$locked = @(); try { $locked = @(Get-CimInstance Win32_UserAccount "
        "-Filter \"LocalAccount='True' AND Lockout=True\" -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty Name) } catch {}; "
        "[PSCustomObject]@{ Users=$users; Admins=$admins; Locked=$locked } "
        "| ConvertTo-Json -Compress -Depth 4",
        timeout=30
    )
    if not users_data:
        return info

    # Les membres du groupe sont retournés au format "MACHINE\Nom"
    admin_names = {a.split('\\')[-1].lower() for a in _as_list(users_data.get('Admins')) if a}
    locked_names = {str(n).lower() for n in _as_list(users_data.get('Locked')) if n}

    details = []
    user_list = []
    for u in _as_list(users_data.get('Users')):
        name = _clean(u.get('Name'))
        if not name:
            continue
        if u.get('Enabled') is False:
            statut = 'Désactivé'
        elif name.lower() in locked_names:
            statut = 'Verrouillé'
        else:
            statut = 'Actif'
        est_admin = name.lower() in admin_names
        source = _clean(u.get('Source')).lower()
        details.append({
            'name': name,
            'status': statut,
            'enabled': u.get('Enabled') is not False,
            'admin': est_admin,
            'role': 'Administrateur' if est_admin else 'Utilisateur standard',
            'account_type': _ACCOUNT_SOURCES.get(source, source.capitalize() or 'Local'),
            'description': _clean(u.get('Description')),
            # Hygiène : un compte actif dont le mot de passe n'expire jamais, ou
            # qui n'a jamais servi, mérite d'être revu.
            'password_never_expires': bool(u.get('NeverExpires')),
            'last_logon': _clean(u.get('LastLogon')),
        })
        # Forme textuelle conservée : le résumé et les rapports s'en servent
        libelle = statut + (', Administrateur' if est_admin else '')
        user_list.append(f"{name} ({libelle})")

    if user_list:
        info['users'] = sorted(user_list)
        info['users_details'] = sorted(details, key=lambda d: d['name'].lower())

    return info


def _ip_entries(raw):
    """Normalise les adresses IPv4 d'une carte et calcule leur plage.

    Une adresse seule ne dit pas sur quel réseau la machine est branchée :
    192.168.1.101/24 appartient au réseau 192.168.1.0/24. La plage est calculée
    ici plutôt qu'à l'affichage, pour que la page et le rapport concordent.
    """
    entries = []
    for item in _as_list(raw):
        if not isinstance(item, dict):
            continue
        adresse = _clean(item.get('IPAddress'))
        if not adresse:
            continue
        prefixe = item.get('PrefixLength')
        reseau = ''
        try:
            import ipaddress
            reseau = str(ipaddress.ip_network('%s/%d' % (adresse, int(prefixe)), strict=False))
        except (ImportError, ValueError, TypeError):
            reseau = ''
        entries.append({
            'address': adresse,
            'prefix': prefixe if isinstance(prefixe, int) else None,
            'network': reseau,
        })
    return entries


def _win_extras():
    """Batterie (dont usure réelle), adaptateurs réseau actifs, disques physiques."""
    info = {}

    # Disques physiques : type (SSD/HDD), état de santé SMART (natif Windows 8+),
    # et leurs partitions (lettre, taille, libre) pour la vue « un disque, ses
    # partitions dedans » de la fiche système. Une seule requête pour les deux :
    # Get-Partition/Get-Volume par disque n'est pas gratuit, pas la peine de le
    # payer deux fois.
    #
    # Get-PhysicalDisk n'a PAS de paramètre -DeviceId (constaté sur un poste
    # réel : erreur de liaison de paramètre, avalée par le try/catch — Health
    # et MediaType retombaient silencieusement à 'Inconnu' pour tous les
    # disques). La table de correspondance DeviceId → disque est donc
    # construite une fois via Where-Object plutôt qu'interrogée par disque.
    # Get-Disk remonte aussi des périphériques sans rapport (lecteur de carte
    # d'imprimante USB vu en test, 0 octet) : exclus par la taille.
    disks_data = _win_powershell_json(
        "$pdMap=@{}; try { Get-PhysicalDisk -ErrorAction SilentlyContinue "
        "| ForEach-Object { $pdMap[[int]$_.DeviceId] = $_ } } catch {}; "
        "@(Get-Disk -ErrorAction SilentlyContinue | Where-Object { $_.Size -gt 0 } | ForEach-Object { "
        "$d=$_; $pd=$pdMap[[int]$d.Number]; "
        # Toutes les partitions, pas seulement celles avec une lettre de
        # lecteur : la réservée système (MSR), l'EFI et les partitions de
        # récupération n'en ont jamais et n'en restent pas moins des
        # partitions réelles du disque, avec leur propre taille.
        "$parts=@(); try { Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue "
        "| ForEach-Object { "
        "$p=$_; $vol=$null; if ($p.DriveLetter) { try { $vol = Get-Volume -DriveLetter $p.DriveLetter "
        "-ErrorAction SilentlyContinue } catch {} }; "
        "$parts += [PSCustomObject]@{ "
        "Letter=$(if ($p.DriveLetter) { [string]$p.DriveLetter } else { $null }); "
        "Type=[string]$p.Type; SizeGB=[math]::Round($p.Size/1GB,2); "
        "FreeGB=$(if ($vol) { [math]::Round($vol.SizeRemaining/1GB,1) } else { $null }) } } } catch {}; "
        "[PSCustomObject]@{ Number=$d.Number; Model=$d.FriendlyName; Bus=$d.BusType; "
        "SizeGB=[math]::Round($d.Size/1GB,1); "
        "MediaType=$(if ($pd) { [string]$pd.MediaType } else { 'Inconnu' }); "
        "Health=$(if ($pd) { [string]$pd.HealthStatus } else { 'Inconnu' }); "
        "OpStatus=$(if ($pd) { [string]$pd.OperationalStatus } else { '' }); "
        "Partitions=$parts } }) | ConvertTo-Json -Compress -Depth 5",
        timeout=45,
    )
    if disks_data:
        physical_list = []
        layout = []
        for d in _as_list(disks_data):
            name = d.get('Model') or 'Disque'
            media = d.get('MediaType') or 'Inconnu'
            health = d.get('Health') or 'Inconnu'
            op_status = d.get('OpStatus') or ''
            size_gb = round(_num(d.get('SizeGB')) or 0, 1)
            entry = f"{name} — {media} — {size_gb} GB — Santé (SMART): {health}"
            if op_status and op_status != 'OK':
                entry += f" ({op_status})"
            physical_list.append(entry)

            partitions = []
            for p in _as_list(d.get('Partitions')):
                total = _num(p.get('SizeGB'))
                if total is None:
                    continue
                letter = _clean(p.get('Letter')) or None
                free = _num(p.get('FreeGB'))
                used = round(total - free, 2) if free is not None else None
                pct = round(used / total * 100) if used is not None and total else None
                partitions.append({
                    'letter': letter, 'type': _clean(p.get('Type')), 'total': total,
                    'free': free, 'used': used, 'pct': pct,
                })
            layout.append({
                'number': d.get('Number'), 'model': name, 'bus': _clean(d.get('Bus')),
                'media_type': media, 'health': health, 'op_status': op_status,
                'size_gb': size_gb, 'partitions': partitions,
            })
        if physical_list:
            info['physical_disks'] = physical_list
        if layout:
            info['disk_layout'] = layout

    extras_data = _win_powershell_json(
        "$battery = @(); try { $battery = @(Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue "
        "| Select-Object EstimatedChargeRemaining,BatteryStatus) } catch {}; "
        # Win32_Battery.DesignCapacity est presque toujours vide : l'usure réelle
        # ne s'obtient que via les classes root\wmi du pilote ACPI
        "$wear = $null; try { "
        "$st = Get-CimInstance -Namespace root\\wmi -ClassName BatteryStaticData -ErrorAction SilentlyContinue "
        "| Select-Object -First 1; "
        "$fu = Get-CimInstance -Namespace root\\wmi -ClassName BatteryFullChargedCapacity -ErrorAction SilentlyContinue "
        "| Select-Object -First 1; "
        "$cy = Get-CimInstance -Namespace root\\wmi -ClassName BatteryCycleCount -ErrorAction SilentlyContinue "
        "| Select-Object -First 1; "
        "if ($st -or $fu) { $wear = [PSCustomObject]@{ Designed=$st.DesignedCapacity; "
        "Full=$fu.FullChargedCapacity; Cycles=$cy.CycleCount } } } catch {}; "
        # Toutes les cartes, pas seulement celles « Up » : une carte désactivée
        # ou débranchée reste une information d'inventaire (« ce poste a un
        # second port Ethernet, inutilisé ») — Status permet de la distinguer
        # à l'affichage plutôt que de la faire disparaître à la collecte.
        "$adapters = @(); try { $adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue "
        "| ForEach-Object { "
        "$ips = @(Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Select-Object IPAddress,PrefixLength); "
        "[PSCustomObject]@{ Name=$_.Name; InterfaceDescription=$_.InterfaceDescription; "
        "LinkSpeed=$_.LinkSpeed; MacAddress=$_.MacAddress; Virtual=$_.Virtual; "
        "Status=[string]$_.Status; MediaType=$_.MediaType; IPs=$ips } }) } catch {}; "
        "[PSCustomObject]@{ Battery=$battery; BatteryWear=$wear; Adapters=$adapters } "
        "| ConvertTo-Json -Compress -Depth 4",
        timeout=25
    )
    if not extras_data:
        return info

    battery_list = _as_list(extras_data.get('Battery'))
    b = battery_list[0] if battery_list else None
    if b and b.get('EstimatedChargeRemaining') is not None:
        info['battery'] = f"{b.get('EstimatedChargeRemaining')}% (statut: {b.get('BatteryStatus', 'Inconnu')})"
        info['battery_charge_percent'] = b.get('EstimatedChargeRemaining')

    wear = extras_data.get('BatteryWear')
    if wear and wear.get('Designed') and wear.get('Full'):
        try:
            designed = float(wear['Designed'])
            full = float(wear['Full'])
            if designed > 0:
                health = round(full / designed * 100, 1)
                info['battery_health_percent'] = health
                info['battery_wear_percent'] = round(100 - health, 1)
                info['battery_designed_capacity_mwh'] = int(designed)
                info['battery_full_capacity_mwh'] = int(full)
        except (TypeError, ValueError):
            pass
    if wear and wear.get('Cycles'):
        info['battery_cycles'] = wear['Cycles']

    adapters = []
    adapter_details = []
    for a in _as_list(extras_data.get('Adapters')):
        if not a.get('Name'):
            continue
        adapters.append(f"{a.get('Name')} — {a.get('InterfaceDescription', '')} — {a.get('LinkSpeed', 'N/A')}")
        adapter_details.append({
            'name': a.get('Name'),
            'description': a.get('InterfaceDescription', ''),
            'link_speed': a.get('LinkSpeed', ''),
            'mac_address': a.get('MacAddress', ''),
            # Distinguer le matériel réel des interfaces créées par Hyper-V,
            # WSL, Docker ou un VPN : sur un poste de développement les
            # secondes sont largement majoritaires et noient les premières.
            'physical': a.get('Virtual') is False,
            'media_type': _clean(a.get('MediaType')),
            'ip_addresses': _ip_entries(a.get('IPs')),
            'connected': _clean(a.get('Status')).lower() == 'up',
        })
    if adapters:
        info['network_adapters'] = adapters
        info['network_adapter_details'] = adapter_details

    return info


# Paires (fournisseur, identifiants) réellement significatives dans le journal
# Système. Un identifiant d'événement n'a de sens que rapporté à son
# fournisseur : l'ID 7 vaut « bloc défectueux » chez `disk` et tout autre chose
# chez Hyper-V. Filtrer sur l'ID seul ramène surtout du bruit.
_EVENT_SPECS = [
    ('EventLog', [6008], 'Arrêt inattendu', 'danger'),
    ('Microsoft-Windows-Kernel-Power', [41], 'Arrêt inattendu', 'danger'),
    ('Microsoft-Windows-WER-SystemErrorReporting', [1001], 'Écran bleu', 'danger'),
    ('disk', [7, 11, 51, 52], 'Erreur disque', 'danger'),
    ('Ntfs', [55], 'Corruption de système de fichiers', 'danger'),
    ('volmgr', [46], 'Erreur de volume', 'warn'),
]

EVENT_WINDOW_DAYS = 30


def _disk_map():
    """Associe chaque numéro de disque physique à son modèle et ses lettres.

    Le journal Système désigne les disques par `\\Device\\HarddiskN\\DRxx`, ce qui
    ne dit rien à personne. N est le numéro de disque physique, qui permet de
    remonter au modèle et aux lettres de lecteur.
    """
    data = _win_powershell_json(
        "@(Get-Disk -ErrorAction SilentlyContinue | ForEach-Object { "
        "$d=$_; $letters=@(); try { $letters=@(Get-Partition -DiskNumber $d.Number "
        "-ErrorAction SilentlyContinue | Where-Object { $_.DriveLetter } "
        "| ForEach-Object { [string]$_.DriveLetter }) } catch {}; "
        "[PSCustomObject]@{ Number=$d.Number; Model=$d.FriendlyName; "
        "Bus=$d.BusType; Letters=$letters } }) | ConvertTo-Json -Compress -Depth 3",
        timeout=45,
    )
    carte = {}
    for d in _as_list(data):
        numero = d.get('Number')
        if numero is None:
            continue
        lettres = [str(l).rstrip(':') for l in _as_list(d.get('Letters')) if l]
        carte[int(numero)] = {
            'model': _clean(d.get('Model')),
            'bus': _clean(d.get('Bus')),
            'letters': lettres,
        }
    return carte


def describe_disk_device(chemin, carte):
    """Traduit `\\Device\\Harddisk7\\DR22` en disque identifiable.

    Un disque amovible débranché depuis l'incident ne figure plus dans la carte :
    le dire explicitement vaut mieux que de laisser un chemin brut, et mieux que
    de l'attribuer au hasard à un disque encore présent.
    """
    m = re.search(r'Harddisk(\d+)', chemin or '', re.IGNORECASE)
    if not m:
        return ''
    numero = int(m.group(1))
    disque = carte.get(numero)
    if not disque:
        return 'disque n°%d (absent — support amovible débranché depuis ?)' % numero
    parties = []
    if disque['letters']:
        parties.append(', '.join('%s:' % l for l in disque['letters']))
    if disque['model']:
        parties.append(disque['model'])
    if disque['bus']:
        parties.append(disque['bus'])
    return ' — '.join(parties) or 'disque n°%d' % numero


# Codes STOP (bugcheck) les plus courants — Microsoft en documente plusieurs
# centaines, mais la grande majorité des écrans bleus réels retombe sur cette
# poignée. Un code non répertorié est affiché brut plutôt que masqué.
_BUGCHECK_CODES = {
    '0x0000000a': 'IRQL_NOT_LESS_OR_EQUAL',
    '0x00000019': 'BAD_POOL_HEADER',
    '0x0000001a': 'MEMORY_MANAGEMENT',
    '0x0000001e': 'KMODE_EXCEPTION_NOT_HANDLED',
    '0x00000024': 'NTFS_FILE_SYSTEM',
    '0x00000027': 'RDR_FILE_SYSTEM',
    '0x0000003b': 'SYSTEM_SERVICE_EXCEPTION',
    '0x00000050': 'PAGE_FAULT_IN_NONPAGED_AREA',
    '0x0000007b': 'INACCESSIBLE_BOOT_DEVICE',
    '0x0000007e': 'SYSTEM_THREAD_EXCEPTION_NOT_HANDLED',
    '0x0000007f': 'UNEXPECTED_KERNEL_MODE_TRAP',
    '0x0000009f': 'DRIVER_POWER_STATE_FAILURE',
    '0x000000a5': 'ACPI BIOS non conforme (ACPI_BIOS_ERROR)',
    '0x000000c2': 'BAD_POOL_CALLER',
    '0x000000c4': 'DRIVER_VERIFIER_DETECTED_VIOLATION',
    '0x000000d1': 'DRIVER_IRQL_NOT_LESS_OR_EQUAL',
    '0x000000ef': 'CRITICAL_PROCESS_DIED',
    '0x000000f4': 'CRITICAL_OBJECT_TERMINATION',
    '0x00000109': 'CRITICAL_STRUCTURE_CORRUPTION',
    '0x00000116': 'VIDEO_TDR_FAILURE (pilote graphique)',
    '0x00000124': 'WHEA_UNCORRECTABLE_ERROR (défaillance matérielle probable)',
    '0x00000133': 'DPC_WATCHDOG_VIOLATION',
    '0x00000139': 'KERNEL_SECURITY_CHECK_FAILURE',
}

_RE_BUGCHECK_HEX = re.compile(r'0x[0-9a-fA-F]{8}')


def _code_arret_depuis_param1(param1):
    """Code STOP et libellé depuis le param1 brut d'un événement 1001
    (Microsoft-Windows-WER-SystemErrorReporting).

    Le format documenté est « 0xXXXXXXXX (param, param, param, param) » — les
    quatre paramètres entre parenthèses sont spécifiques au pilote/à
    l'adresse mémoire en cause, illisibles sans les symboles de débogage :
    seul le code lui-même est exploitable ici.

    Retourne (code, libellé_ou_None) ; (None, None) si rien d'exploitable.
    """
    m = _RE_BUGCHECK_HEX.search(param1 or '')
    if not m:
        return None, None
    code = m.group(0).lower()
    return code, _BUGCHECK_CODES.get(code)


def _win_diagnostics():
    """Signaux de diagnostic : incidents, correctifs en attente, démarrage.

    Ces informations ne décrivent pas la configuration de la machine mais son
    comportement : c'est ce qui permet de répondre à « le poste rame » ou
    « il redémarre tout seul » sans se déplacer.
    """
    info = {}

    specs_ps = '; '.join(
        "@{P='%s';I=@(%s)}" % (prov, ','.join(str(i) for i in ids))
        for prov, ids, _lib, _lvl in _EVENT_SPECS
    )
    data = _win_powershell_json(
        "$since=(Get-Date).AddDays(-%d); $out=@(); "
        "$specs=@(%s); "
        "foreach($s in $specs){ try { $out += @(Get-WinEvent -FilterHashtable @{LogName='System'; "
        "ProviderName=$s.P; ID=$s.I; StartTime=$since} -MaxEvents 60 -ErrorAction SilentlyContinue "
        "| Select-Object Id,ProviderName,"
        "@{N='When';E={$_.TimeCreated.ToString('yyyy-MM-dd HH:mm')}},"
        "@{N='Msg';E={($_.Message -split [char]10)[0].Trim()}},"
        "@{N='Code';E={ if ($s.P -eq 'Microsoft-Windows-WER-SystemErrorReporting') { try { "
        "$d=([xml]$_.ToXml()).Event.EventData.Data; "
        "$nomme=@($d | Where-Object {$_.Name -eq 'param1'}); "
        "if ($nomme.Count -gt 0) { $nomme[0].'#text' } "
        "elseif ($d.Count -gt 0) { $d[0].'#text' } else { '' } "
        "} catch { '' } } else { '' } }}) } catch {} }; "
        "$out | ConvertTo-Json -Compress -Depth 3" % (EVENT_WINDOW_DAYS, specs_ps),
        timeout=90,
    )

    libelles = {(p, i): (lib, lvl) for p, ids, lib, lvl in _EVENT_SPECS for i in ids}
    groupes = {}
    for e in _as_list(data):
        code, code_label = (None, None)
        if _clean(e.get('ProviderName')) == 'Microsoft-Windows-WER-SystemErrorReporting':
            code, code_label = _code_arret_depuis_param1(_clean(e.get('Code')))
        # Le code STOP entre dans la clé de regroupement : deux écrans bleus
        # de causes différentes ne doivent pas être comptés comme un seul
        # incident répété.
        cle = (_clean(e.get('ProviderName')), e.get('Id'), _clean(e.get('Msg')), code)
        lib, niveau = libelles.get((cle[0], cle[1]), ('Incident système', 'warn'))
        if code:
            lib = '%s — %s' % (lib, code_label or code)
        entree = groupes.setdefault(cle, {
            'category': lib, 'level': niveau, 'provider': cle[0], 'event_id': cle[1],
            'message': cle[2], 'count': 0, 'last_seen': '', 'code': code, 'code_label': code_label,
        })
        entree['count'] += 1
        quand = _clean(e.get('When'))
        # Les incidents répétitifs (un bloc défectueux relu six fois) sont
        # regroupés : c'est le nombre et la dernière occurrence qui informent.
        if quand > entree['last_seen']:
            entree['last_seen'] = quand

    if groupes:
        # Rapprocher les chemins de périphérique des disques réels : la carte
        # n'est construite que si un incident disque a effectivement été relevé.
        carte = _disk_map() if any(g['category'].startswith(('Erreur disque', 'Corruption'))
                                   for g in groupes.values()) else {}
        for g in groupes.values():
            g['disk'] = describe_disk_device(g['message'], carte) if carte else ''

        incidents = sorted(groupes.values(), key=lambda g: g['last_seen'], reverse=True)
        info['system_incidents'] = incidents
        info['unexpected_shutdowns'] = sum(
            g['count'] for g in incidents if g['category'] == 'Arrêt inattendu')
        info['disk_error_events'] = sum(
            g['count'] for g in incidents
            if g['category'] in ('Erreur disque', 'Corruption de système de fichiers'))

    # ── Mises à jour disponibles ───────────────────────────────────────────
    # La recherche en ligne interroge Microsoft Update (ou le WSUS configuré) et
    # voit donc les correctifs *applicables*, pas seulement ceux que Windows a
    # déjà décidé d'installer. La différence est réelle : sur la machine de
    # référence le cache local annonçait zéro alors qu'une mise à jour de 1,5 Go
    # était disponible. Repli sur le cache si la recherche échoue ou expire.
    requete = ("$r=$sr.Search('IsInstalled=0 and IsHidden=0'); "
               "@($r.Updates | Select-Object -First 60 | ForEach-Object { [PSCustomObject]@{ "
               "Title=$_.Title; Severity=$_.MsrcSeverity; KB=($_.KBArticleIDs -join ','); "
               "SizeMB=[math]::Round($_.MaxDownloadSize/1MB,1); "
               "Security=[bool]($_.Categories | Where-Object { $_.Name -match 'Security|Sécurité' }) } }) "
               "| ConvertTo-Json -Compress -Depth 3")

    source = 'en ligne'
    maj = _win_powershell_json(
        "try { $s=New-Object -ComObject Microsoft.Update.Session; "
        "$sr=$s.CreateUpdateSearcher(); $sr.Online=$true; " + requete + " } catch {}",
        timeout=240,
    )
    if maj is None:
        source = 'cache local'
        maj = _win_powershell_json(
            "try { $s=New-Object -ComObject Microsoft.Update.Session; "
            "$sr=$s.CreateUpdateSearcher(); $sr.Online=$false; " + requete + " } catch {}",
            timeout=60,
        )

    attente = []
    for u in _as_list(maj):
        titre = _clean(u.get('Title'))
        if not titre:
            continue
        try:
            taille = float(u.get('SizeMB') or 0)
        except (TypeError, ValueError):
            taille = 0
        attente.append({
            'title': titre,
            'severity': _clean(u.get('Severity')),
            'kb': _clean(u.get('KB')),
            'size_mb': round(taille) if taille else None,
            'security': bool(u.get('Security')),
        })
    info['pending_updates'] = attente
    info['pending_updates_source'] = source if maj is not None else 'indisponible'
    info['pending_updates_security'] = sum(
        1 for u in attente if u['security'] or u['severity'] in ('Critical', 'Important'))

    # ── Démarrage et services ──────────────────────────────────────────────
    demarrage = _win_powershell_json(
        "$startup=@(); try { $startup=@(Get-CimInstance Win32_StartupCommand -ErrorAction SilentlyContinue "
        "| Select-Object Name,Command,Location,User) } catch {}; "
        "$svc=@(); try { $svc=@(Get-CimInstance Win32_Service -ErrorAction SilentlyContinue "
        "| Where-Object { $_.StartMode -eq 'Auto' -and $_.State -ne 'Running' } "
        "| Select-Object Name,DisplayName,State,StartMode) } catch {}; "
        "$shares=@(); try { $shares=@(Get-SmbShare -ErrorAction SilentlyContinue "
        "| Select-Object Name,Path,Description,@{N='Special';E={$_.Special}}) } catch {}; "
        "[PSCustomObject]@{ Startup=$startup; Services=$svc; Shares=$shares } "
        "| ConvertTo-Json -Compress -Depth 4",
        timeout=60,
    )
    if demarrage:
        progs = []
        for s in _as_list(demarrage.get('Startup')):
            nom = _clean(s.get('Name'))
            if nom:
                progs.append({
                    'name': nom,
                    'command': _clean(s.get('Command')),
                    'location': _clean(s.get('Location')),
                    'user': _clean(s.get('User')),
                })
        if progs:
            info['startup_programs'] = sorted(progs, key=lambda p: p['name'].lower())

        services = []
        for s in _as_list(demarrage.get('Services')):
            nom = _clean(s.get('Name'))
            if nom:
                services.append({
                    'name': nom,
                    'display_name': _clean(s.get('DisplayName')) or nom,
                    'state': _clean(s.get('State')),
                })
        if services:
            info['stopped_auto_services'] = sorted(
                services, key=lambda s: s['display_name'].lower())

        partages = []
        for s in _as_list(demarrage.get('Shares')):
            nom = _clean(s.get('Name'))
            if not nom:
                continue
            # Les partages d'administration (C$, ADMIN$, IPC$) existent partout
            # et n'apprennent rien ; ce sont les partages créés à la main qui
            # méritent d'être vus.
            partages.append({
                'name': nom,
                'path': _clean(s.get('Path')),
                'description': _clean(s.get('Description')),
                'administrative': bool(s.get('Special')) or nom.endswith('$'),
            })
        if partages:
            info['smb_shares'] = partages

    # ── Tâches planifiées non-Microsoft ────────────────────────────────────
    # Les tâches du dossier \Microsoft\ sont celles de Windows lui-même : des
    # centaines d'entrées sans valeur d'inventaire. Ce qui informe, ce sont les
    # tâches ajoutées par des logiciels tiers ou à la main.
    taches = _win_powershell_json(
        "@(Get-ScheduledTask -ErrorAction SilentlyContinue "
        "| Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' } | ForEach-Object { "
        "$t=$_; $i=$null; try { $i=Get-ScheduledTaskInfo -TaskName $t.TaskName "
        "-TaskPath $t.TaskPath -ErrorAction SilentlyContinue } catch {}; "
        "[PSCustomObject]@{ Name=$t.TaskName; Path=$t.TaskPath; State=[string]$t.State; "
        "Author=$t.Author; "
        "Action=($t.Actions | ForEach-Object { $_.Execute } | Select-Object -First 1); "
        "LastRun=$(if($i.LastRunTime){$i.LastRunTime.ToString('yyyy-MM-dd HH:mm')}else{''}); "
        "LastResult=$i.LastTaskResult } }) | ConvertTo-Json -Compress -Depth 3",
        timeout=90,
    )
    planifiees = []
    for t in _as_list(taches):
        nom = _clean(t.get('Name'))
        if not nom:
            continue
        resultat = t.get('LastResult')
        planifiees.append({
            'name': nom,
            'path': _clean(t.get('Path')),
            'state': _clean(t.get('State')),
            'author': _clean(t.get('Author')),
            'action': _clean(t.get('Action')),
            'last_run': _clean(t.get('LastRun')),
            # 0 = succès, 267011 = jamais exécutée ; tout le reste est un échec
            'last_result': resultat,
            'failed': resultat not in (None, 0, 267011),
        })
    if planifiees:
        info['scheduled_tasks'] = sorted(planifiees, key=lambda t: t['name'].lower())

    return info


def _win_top_processes(echantillon_ms=600, limite=10):
    """Dix processus les plus gourmands en CPU, dix en RAM, au moment de la collecte.

    Le CPU de `Get-Process` est un temps CUMULÉ depuis le lancement du
    processus, pas une charge instantanée : un navigateur ouvert depuis trois
    jours dominerait ce classement même parfaitement inactif là, maintenant.
    Deux relevés espacés de `echantillon_ms` et leur delta donnent, eux, un
    vrai pourcentage instantané — normalisé par le nombre de cœurs.

    Snapshot, pas une moyenne : utile pour « le poste rame maintenant », pas
    pour un historique de charge. Le processus PowerShell qui exécute cette
    mesure apparaît lui-même dans le classement — c'est une donnée honnête,
    pas un artefact à masquer (un vrai autre processus PowerShell qui
    consommerait, lui, mériterait d'être vu).
    """
    data = _win_powershell_json(
        "$avant=Get-Process | Select-Object Id,@{N='C';E={$_.CPU}}; "
        "Start-Sleep -Milliseconds %d; "
        "$n=[Environment]::ProcessorCount; "
        "$liste=@(Get-Process | ForEach-Object { $p=$_; "
        "$old=$avant | Where-Object {$_.Id -eq $p.Id}; $cpu=0; "
        "if ($old -and $p.CPU -and $old.C) { $d=$p.CPU-$old.C; "
        "if ($d -gt 0) { $cpu=[math]::Round($d/(%d/1000.0)*100/$n,1) } }; "
        "[PSCustomObject]@{ Name=$p.Name; Cpu=$cpu; RamMb=[math]::Round($p.WorkingSet/1MB,1) } }); "
        "[PSCustomObject]@{ "
        "ByCpu=@($liste | Sort-Object Cpu -Descending | Select-Object -First %d); "
        "ByRam=@($liste | Sort-Object RamMb -Descending | Select-Object -First %d) "
        "} | ConvertTo-Json -Compress -Depth 3" % (echantillon_ms, echantillon_ms, limite, limite),
        timeout=15 + echantillon_ms // 1000,
    )
    if not data:
        return {}

    def _liste(cle):
        sortie = []
        for p in _as_list(data.get(cle)):
            nom = _clean(p.get('Name'))
            if not nom:
                continue
            sortie.append({'name': nom, 'cpu_pct': p.get('Cpu') or 0, 'ram_mb': p.get('RamMb') or 0})
        return sortie

    info = {}
    par_cpu, par_ram = _liste('ByCpu'), _liste('ByRam')
    if par_cpu:
        info['top_processes_cpu'] = par_cpu
    if par_ram:
        info['top_processes_ram'] = par_ram
    return info


def _win_unsigned_drivers(limite=40):
    """Pilotes installés sans signature numérique.

    `DriverDate` n'est volontairement pas utilisé pour signaler des pilotes
    « obsolètes » : de nombreux pilotes Windows intégrés portent une date
    ancienne héritée de leur toute première publication sans que ce soit un
    signal de problème — un faux positif systématique sur la moitié du parc.
    L'absence de signature, elle, est un fait vérifiable sans ambiguïté
    (`IsSigned`, exposé directement par `Win32_PNPSignedDriver`).
    """
    data = _win_powershell_json(
        "@(Get-CimInstance Win32_PNPSignedDriver -ErrorAction SilentlyContinue "
        "| Where-Object { $_.IsSigned -eq $false -and $_.DeviceName } "
        "| Select-Object DeviceName,DriverVersion,DriverProviderName "
        "| Select-Object -First %d) | ConvertTo-Json -Compress -Depth 3" % limite,
        timeout=45,
    )
    pilotes = []
    for p in _as_list(data):
        nom = _clean(p.get('DeviceName'))
        if not nom:
            continue
        pilotes.append({
            'device': nom,
            'version': _clean(p.get('DriverVersion')),
            'provider': _clean(p.get('DriverProviderName')),
        })
    return {'unsigned_drivers': pilotes} if pilotes else {}


def _win_group_policy():
    """Stratégies de groupe (GPO) réellement appliquées à cet utilisateur/poste.

    Passe par `gpresult /X` (export XML), pas par `gpresult /r` (texte) :
    même raison que pour les profils Wi-Fi (`_win_wifi_profiles`) — le texte
    change de libellés selon la langue de Windows, le schéma XML est fixe.

    Le périmètre ORDINATEUR (`ComputerResults`) n'apparaît dans ce rapport que
    si la collecte tourne élevée ; le périmètre UTILISATEUR (`UserResults`),
    lui, ne demande aucun privilège particulier et est donc toujours présent.
    """
    fichier = tempfile.mktemp(suffix='.xml', prefix='parcinfo_gpo_')
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        subprocess.run(['gpresult', '/X', fichier], capture_output=True, timeout=30,
                       creationflags=creationflags)
        if not os.path.exists(fichier):
            return {}
        return _lire_rapport_gpo(fichier)
    except Exception:
        return {}
    finally:
        try:
            os.remove(fichier)
        except Exception:
            pass


_GPO_XML_NS = {'r': 'http://www.microsoft.com/GroupPolicy/Rsop'}


def _lire_rapport_gpo(chemin):
    """Extrait les GPO appliquées d'un rapport `gpresult /X`.

    Séparée de _win_group_policy pour rester testable sans lancer gpresult
    (indisponible hors Windows) — même principe que _parse_wifi_profile_xml.
    """
    racine = ET.parse(chemin).getroot()
    ns = _GPO_XML_NS
    gpos = []
    for perimetre, cle in (('r:UserResults', 'Utilisateur'), ('r:ComputerResults', 'Ordinateur')):
        section = racine.find(perimetre, ns)
        if section is None:
            continue
        for gpo in section.findall('r:GPO', ns):
            nom = (gpo.findtext('r:Name', namespaces=ns) or '').strip()
            if not nom:
                continue
            refuse = (gpo.findtext('r:AccessDenied', namespaces=ns) or '').strip().lower() == 'true'
            actif = (gpo.findtext('r:Enabled', namespaces=ns) or '').strip().lower() == 'true'
            gpos.append({'name': nom, 'scope': cle, 'enabled': actif, 'denied': refuse})
    return {'group_policies': gpos} if gpos else {}


# Étapes de la collecte Windows, avec le libellé montré à l'utilisateur. La
# collecte dure une bonne minute : sans retour, elle est indiscernable d'un
# blocage.
_WIN_STEPS = [
    ('Matériel de base', lambda: _win_base_hardware()),
    ('Processeur et mémoire', lambda: _win_core()),
    ('Détail matériel', lambda: _win_hardware_detail()),
    ('Écrans, imprimantes et disques', lambda: _win_inventory()),
    ('Style de partition et démarrage', lambda: _win_boot_disk()),
    ('Licences et correctifs', lambda: _win_licensing()),
    ('Sécurité', lambda: _win_security()),
    ('Règles de pare-feu', lambda: _win_firewall_rules()),
    ('Redirections de port', lambda: _win_port_forwards()),
    ('Pilotes non signés', lambda: _win_unsigned_drivers()),
    ('Détections antivirus (historique)', lambda: _win_malware_detections()),
    ('Comptes utilisateurs', lambda: _win_users()),
    ('Batterie et réseau', lambda: _win_extras()),
    ('Diagnostic (incidents, services, tâches)', lambda: _win_diagnostics()),
    ('Processus les plus gourmands', lambda: _win_top_processes()),
    ('Erreurs système (historique)', lambda: _win_system_errors()),
    ('Erreurs applicatives (historique)', lambda: _win_application_errors()),
    ('Configuration réseau', lambda: _win_network()),
    ('Environnement (WSUS, domaine, temps)', lambda: _win_enterprise()),
    ('Stratégies de groupe appliquées', lambda: _win_group_policy()),
    ('Hygiène système', lambda: _win_hygiene()),
    ('Maintenance et performance', lambda: _win_maintenance()),
    ('Accès distant et exposition', lambda: _win_remote_access()),
    ('Agents de télémaintenance', lambda: _win_managed_agents()),
    ('Politique de mot de passe et accès', lambda: _win_access_policy()),
    ('Comptes de messagerie', lambda: _win_mail_accounts()),
    ('Applications par défaut et lecteurs réseau', lambda: _win_workstation_extras()),
    ('Périphériques en erreur', lambda: _win_problem_devices()),
    ('Temps de démarrage', lambda: _win_boot_performance()),
    ('Historique des arrêts et redémarrages', lambda: _win_shutdown_history()),
    ('Journal de sécurité', lambda: _win_security_events()),
    ('Connexions Bureau à distance entrantes', lambda: _win_rdp_logon_history()),
    ('Certificats', lambda: _win_certificates()),
    ('Clés de récupération BitLocker', lambda: _win_bitlocker_keys()),
    ('Profils utilisateurs', lambda: _win_user_profiles()),
]


def _report(progress, fraction, libelle):
    """Notifie l'avancement, sans jamais faire échouer la collecte.

    Le rappel vient de l'interface appelante (barre texte ou widget) : une
    erreur d'affichage ne doit pas interrompre une collecte d'une minute.
    """
    if not progress:
        return
    try:
        progress(max(0.0, min(1.0, float(fraction))), libelle)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════
# RÉSEAU, ENVIRONNEMENT ET HYGIÈNE
# ════════════════════════════════════════════════════════════════════════════

# Catégories de réseau Windows : elles déterminent quel profil de pare-feu
# s'applique, ce qui explique bien des « ça marche chez moi, pas chez lui ».
_NETWORK_CATEGORIES = {0: 'Public', 1: 'Privé', 2: 'Domaine'}
# Connectivité IPv4 constatée par Windows lui-même.
_IPV4_CONNECTIVITY = {0: 'Aucune', 1: 'Locale', 2: 'Locale (sous-réseau)',
                      3: 'Locale (site)', 4: 'Internet'}


def _win_network():
    """Paramétrage réseau effectif : DNS, passerelle, DHCP, profil, proxy, Wi-Fi.

    Ce sont les réglages qui répondent à « il n'a plus Internet » : un DNS
    injoignable, une passerelle absente ou un proxy résiduel produisent tous le
    même symptôme et ne se distinguent qu'ici.
    """
    info = {}
    data = _win_powershell_json(
        "$gw=@(); try { $gw=@(Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
        "-ErrorAction SilentlyContinue | Select-Object NextHop,InterfaceAlias,RouteMetric) } catch {}; "
        "$dns=@(); try { $dns=@(Get-DnsClientServerAddress -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Where-Object { $_.ServerAddresses } "
        "| Select-Object InterfaceAlias,ServerAddresses) } catch {}; "
        "$prof=@(); try { $prof=@(Get-NetConnectionProfile -ErrorAction SilentlyContinue "
        "| Select-Object Name,InterfaceAlias,NetworkCategory,IPv4Connectivity) } catch {}; "
        "$dhcp=@(); try { $dhcp=@(Get-NetIPInterface -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Where-Object { $_.ConnectionState -eq 'Connected' } "
        "| Select-Object InterfaceAlias,Dhcp) } catch {}; "
        "$px=$null; try { $px=Get-ItemProperty "
        "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' "
        "-ErrorAction SilentlyContinue | Select-Object ProxyEnable,ProxyServer,AutoConfigURL } catch {}; "
        "$suffix=''; try { $suffix=(Get-DnsClientGlobalSetting -ErrorAction SilentlyContinue).SuffixSearchList -join ', ' } catch {}; "
        "[PSCustomObject]@{ Gateways=$gw; Dns=$dns; Profiles=$prof; Dhcp=$dhcp; "
        "Proxy=$px; Suffix=$suffix } | ConvertTo-Json -Compress -Depth 4",
        timeout=60,
    )
    if not data:
        return info

    dhcp_par_carte = {_clean(d.get('InterfaceAlias')): d.get('Dhcp')
                      for d in _as_list(data.get('Dhcp'))}

    passerelles = []
    for g in _as_list(data.get('Gateways')):
        adresse = _clean(g.get('NextHop'))
        if adresse and adresse != '0.0.0.0':
            passerelles.append({
                'address': adresse,
                'interface': _clean(g.get('InterfaceAlias')),
                'metric': g.get('RouteMetric'),
            })
    if passerelles:
        # La passerelle de plus faible métrique est celle réellement empruntée.
        passerelles.sort(key=lambda p: p['metric'] if p['metric'] is not None else 9999)
        info['gateways'] = passerelles
        info['default_gateway'] = passerelles[0]['address']

    serveurs_dns = []
    for d in _as_list(data.get('Dns')):
        adresses = [_clean(a) for a in _as_list(d.get('ServerAddresses')) if _clean(a)]
        if adresses:
            carte = _clean(d.get('InterfaceAlias'))
            serveurs_dns.append({
                'interface': carte,
                'servers': adresses,
                # 1 = DHCP, 2 = adressage manuel
                'dhcp': dhcp_par_carte.get(carte) == 1,
            })
    if serveurs_dns:
        info['dns_servers'] = serveurs_dns

    profils = []
    for p in _as_list(data.get('Profiles')):
        nom = _clean(p.get('Name'))
        if not nom:
            continue
        profils.append({
            'name': nom,
            'interface': _clean(p.get('InterfaceAlias')),
            'category': _NETWORK_CATEGORIES.get(p.get('NetworkCategory'), 'Inconnue'),
            'connectivity': _IPV4_CONNECTIVITY.get(p.get('IPv4Connectivity'), 'Inconnue'),
        })
    if profils:
        info['network_profiles'] = profils

    proxy = data.get('Proxy') or {}
    serveur_proxy = _clean(proxy.get('ProxyServer'))
    auto_config = _clean(proxy.get('AutoConfigURL'))
    if serveur_proxy or auto_config:
        info['proxy'] = {
            'enabled': bool(proxy.get('ProxyEnable')),
            'server': serveur_proxy,
            'auto_config_url': auto_config,
        }

    # Bug de longue date corrigé en 3.10 : PowerShell renvoyait déjà une
    # chaîne jointe (`-join ', '`), mais `dns_suffixes` est documenté et
    # consommé partout ailleurs (fiche, PDF, macOS depuis 3.10) comme une
    # LISTE — `build_summary_sections()` faisait `', '.join(dns_suffixes)`
    # dessus, ce qui rejoignait la chaîne CARACTÈRE PAR CARACTÈRE au lieu de
    # suffixe par suffixe. Reconverti ici en liste réelle, symétrique du
    # côté macOS.
    suffixe = _clean(data.get('Suffix'))
    if suffixe:
        info['dns_suffixes'] = [s.strip() for s in suffixe.split(',') if s.strip()]

    # ── Wi-Fi ──────────────────────────────────────────────────────────────
    sortie = _run(['netsh', 'wlan', 'show', 'interfaces'], timeout=20)
    if sortie and 'SSID' in sortie:
        champs = {}
        for ligne in sortie.splitlines():
            if ':' in ligne:
                cle, _, valeur = ligne.partition(':')
                champs[_strip_accents(cle).strip().lower()] = valeur.strip()
        ssid = champs.get('ssid', '')
        if ssid:
            info['wifi'] = {
                'ssid': ssid,
                'signal': champs.get('signal', ''),
                'radio': champs.get('type de radio') or champs.get('radio type', ''),
                'band': champs.get('bande') or champs.get('band', ''),
                'channel': champs.get('canal') or champs.get('channel', ''),
            }

    return info


# Table de correspondance netsh (authentification WLAN) → libellé identifiants.
# `authentification` prend une poignée de valeurs documentées par Microsoft ;
# tout ce qui n'y figure pas est renvoyé tel quel plutôt que forcé à WPA2.
_WIFI_AUTH_LABELS = {
    'open': 'Ouvert', 'none': 'Ouvert',
    'shared': 'WEP', 'wep': 'WEP',
    'wpapsk': 'WPA', 'wpa': 'WPA', 'wpaenterprise': 'WPA',
    'wpa2psk': 'WPA2', 'wpa2': 'WPA2', 'wpa2enterprise': 'WPA2',
    'wpa3sae': 'WPA3', 'wpa3ent': 'WPA3', 'wpa3ent192': 'WPA3', 'wpa3': 'WPA3',
}

_WIFI_PROFILE_XML_NS = {'w': 'http://www.microsoft.com/networking/WLAN/profile/v1'}


def _parse_wifi_profile_xml(chemin, inclure_mdp=False):
    """Extrait SSID/sécurité (et le mot de passe si demandé) d'un XML exporté
    par `netsh wlan export profile`.

    Fonction pure, séparée de _win_wifi_profiles pour rester testable sans
    lancer netsh (indisponible hors Windows, et hors CI faute d'adaptateur
    Wi-Fi) — même principe que _champs_erreur_application pour les journaux
    d'événements. Retourne None si le fichier n'a pas de SSID exploitable.
    """
    racine = ET.parse(chemin).getroot()
    ns = _WIFI_PROFILE_XML_NS
    ssid = (racine.findtext('.//w:SSIDConfig/w:SSID/w:name', namespaces=ns) or '').strip()
    if not ssid:
        return None
    auth_brute = (racine.findtext(
        './/w:MSM/w:security/w:authEncryption/w:authentication', namespaces=ns) or '').strip()
    cle = auth_brute.lower().replace('-', '').replace('_', '')
    profil = {
        'ssid': ssid,
        'authentification': _WIFI_AUTH_LABELS.get(cle, auth_brute or 'WPA2'),
        'chiffrement': (racine.findtext(
            './/w:MSM/w:security/w:authEncryption/w:encryption', namespaces=ns) or '').strip(),
    }
    if inclure_mdp:
        mdp = (racine.findtext(
            './/w:MSM/w:security/w:sharedKey/w:keyMaterial', namespaces=ns) or '').strip()
        if mdp:
            profil['password'] = mdp
    return profil


def _win_wifi_profiles(inclure_mdp=False):
    """Réseaux Wi-Fi enregistrés sur ce poste (SSID, sécurité, et le mot de
    passe uniquement si `inclure_mdp` est vrai).

    Passe par `netsh wlan export profile`, qui écrit un XML par réseau, plutôt
    que par « netsh wlan show profile … key=clear » : le texte de cette
    dernière commande est localisé (les libellés changent selon la langue de
    Windows), alors que le schéma XML est fixe quelle que soit la langue.

    Le dossier d'export est temporaire et supprimé — y compris en cas
    d'erreur — dès la lecture terminée : avec `inclure_mdp`, il contient les
    mots de passe en clair, qui ne doivent pas traîner sur le disque plus
    longtemps que nécessaire pour les lire. Le nettoyage lui-même ne doit
    jamais faire échouer la fonction : un verrou transitoire (antivirus qui
    scanne le fichier qu'on vient d'écrire) ne doit pas maquiller en échec une
    collecte par ailleurs réussie — d'où `shutil.rmtree(..., ignore_errors=True)`
    plutôt que le context manager de TemporaryDirectory, qui propagerait une
    telle erreur de nettoyage.

    Volontairement tenu à l'écart de `_WIN_STEPS` : tout ce qui y entre finit
    dans `system_report` (fiche système, PDF), en clair. Un mot de passe
    Wi-Fi ne doit avoir qu'une seule destination, chiffrée — voir
    `send_wifi_credentials_to_parcinfo`.
    """
    if not IS_WINDOWS:
        return []
    profils = []
    dossier = tempfile.mkdtemp(prefix='parcinfo_wifi_')
    try:
        try:
            cmd = ['netsh', 'wlan', 'export', 'profile', 'folder=' + dossier]
            if inclure_mdp:
                cmd.append('key=clear')
            creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            subprocess.run(cmd, capture_output=True, timeout=20, creationflags=creationflags)
        except Exception:
            return []

        try:
            fichiers = [f for f in os.listdir(dossier) if f.lower().endswith('.xml')]
        except Exception:
            fichiers = []

        for nom_fichier in fichiers:
            try:
                profil = _parse_wifi_profile_xml(os.path.join(dossier, nom_fichier), inclure_mdp)
                if profil:
                    profils.append(profil)
            except Exception:
                continue
    finally:
        shutil.rmtree(dossier, ignore_errors=True)

    return profils


def _mac_wifi_profiles():
    """Réseaux Wi-Fi enregistrés (SSID seul) — pendant macOS partiel de
    `_win_wifi_profiles()`.

    Le mot de passe n'est **jamais** collecté ici, contrairement à Windows :
    sur macOS il vit dans le Trousseau, protégé par une invite interactive
    (Touch ID/mot de passe) déclenchée par `security find-generic-password`
    — obtenir chaque mot de passe demanderait une confirmation utilisateur
    PAR RÉSEAU, incompatible avec une collecte automatisée. `authentification`
    et `chiffrement` restent vides pour la même raison : `networksetup` ne
    les expose pas sans lire le Trousseau.
    """
    interface = None
    ports = _run(['networksetup', '-listallhardwareports'], timeout=10)
    bloc = ports.split('Hardware Port: ')
    for section in bloc:
        if section.startswith('Wi-Fi') or section.startswith('AirPort'):
            m = re.search(r'Device:\s*(\S+)', section)
            if m:
                interface = m.group(1)
                break
    if not interface:
        return []

    out = _run(['networksetup', '-listpreferredwirelessnetworks', interface], timeout=10)
    lignes = out.splitlines()[1:]  # 1ʳᵉ ligne : "Preferred networks on <if>:"
    return [{'ssid': l.strip(), 'authentification': '', 'chiffrement': ''}
            for l in lignes if l.strip()]


def get_wifi_profiles(inclure_mdp=False):
    """Point d'entrée public, indépendant de `_WIN_STEPS` (voir _win_wifi_profiles).

    macOS : liste de SSID uniquement, jamais le mot de passe — voir
    `_mac_wifi_profiles()`. `inclure_mdp` y est donc sans effet. Linux : pas
    d'équivalent implémenté.
    """
    if IS_WINDOWS:
        return _win_wifi_profiles(inclure_mdp)
    if IS_MAC:
        try:
            return _mac_wifi_profiles()
        except Exception:
            return []
    return []


def _win_enterprise():
    """Rattachement à une infrastructure : WSUS, domaine, temps.

    Sur un parc géré, ces trois réglages expliquent la majorité des écarts entre
    un poste et ses voisins — un WSUS injoignable bloque les mises à jour, un
    décalage d'horloge casse l'authentification Kerberos.
    """
    info = {}
    data = _win_powershell_json(
        "$wu=$null; try { $wu=Get-ItemProperty "
        "'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate' "
        "-ErrorAction SilentlyContinue | Select-Object WUServer,TargetGroup } catch {}; "
        "$cs=$null; try { $cs=Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue "
        "| Select-Object PartOfDomain,Domain,Workgroup } catch {}; "
        "$dc=''; try { $dc=(nltest /dsgetdc:) 2>$null | Select-String 'DC:' "
        "| ForEach-Object { $_.ToString().Trim() } | Select-Object -First 1 } catch {}; "
        "$ntp=''; try { $ntp=(w32tm /query /source) 2>$null | Select-Object -First 1 } catch {}; "
        "$offset=''; try { $offset=((w32tm /query /status) 2>$null | Select-String 'Phase Offset|Décalage de phase' "
        "| Select-Object -First 1).ToString().Trim() } catch {}; "
        "[PSCustomObject]@{ WU=$wu; Cs=$cs; Dc=$dc; Ntp=$ntp; Offset=$offset } "
        "| ConvertTo-Json -Compress -Depth 4",
        timeout=60,
    )
    if not data:
        return info

    wu = data.get('WU') or {}
    serveur = _clean(wu.get('WUServer'))
    if serveur:
        info['wsus_server'] = serveur
        groupe = _clean(wu.get('TargetGroup'))
        if groupe:
            info['wsus_group'] = groupe

    cs = data.get('Cs') or {}
    if cs:
        info['domain_joined'] = bool(cs.get('PartOfDomain'))
        info['domain_name'] = _clean(cs.get('Domain')) or _clean(cs.get('Workgroup'))

    # Hors domaine, nltest ne renvoie rien et PowerShell sérialise un objet
    # vide : « {} » ne doit pas se retrouver affiché comme un nom de serveur.
    dc = _clean(data.get('Dc'))
    if dc and dc not in ('{}', '[]', 'None') and any(ch.isalnum() for ch in dc):
        info['domain_controller'] = dc

    ntp = _clean(data.get('Ntp'))
    if (ntp and ntp not in ('{}', '[]', 'None')
            and 'erreur' not in ntp.lower() and 'error' not in ntp.lower()):
        info['time_source'] = ntp
    offset = _clean(data.get('Offset'))
    if offset:
        info['time_offset'] = offset

    return info


#: Codes d'erreur du Gestionnaire de périphériques (ConfigManagerErrorCode).
#: Seuls ceux qu'on rencontre en dépannage sont nommés ; les autres sont
#: rapportés avec leur numéro plutôt que passés sous silence.
_CODES_PERIPHERIQUE = {
    1:  "Périphérique mal configuré",
    3:  "Pilote endommagé ou mémoire insuffisante",
    10: "Impossible de démarrer le périphérique",
    12: "Ressources insuffisantes (conflit)",
    14: "Redémarrage nécessaire",
    16: "Ressources non toutes identifiées",
    18: "Réinstallation des pilotes nécessaire",
    19: "Informations de registre incomplètes ou endommagées",
    21: "Suppression en cours",
    22: "Périphérique désactivé",
    24: "Absent, mal installé ou déconnecté",
    28: "Pilotes non installés",
    31: "Ne fonctionne pas correctement (pilote défaillant)",
    32: "Pilote de démarrage désactivé",
    37: "Échec d'initialisation du pilote",
    39: "Pilote endommagé ou manquant",
    43: "Arrêté à la suite d'un incident signalé par le matériel",
    45: "Actuellement déconnecté",
    52: "Signature du pilote non vérifiable",
}


#: États de chiffrement d'un volume. Windows renvoie un entier sur les versions
#: anciennes et un libellé sur les récentes : les deux formes sont traduites.
_ETATS_VOLUME = {
    '0': 'Non chiffré', 'FullyDecrypted': 'Non chiffré',
    '1': 'Chiffré', 'FullyEncrypted': 'Chiffré',
    '2': 'Chiffrement en cours', 'EncryptionInProgress': 'Chiffrement en cours',
    '3': 'Déchiffrement en cours', 'DecryptionInProgress': 'Déchiffrement en cours',
    '4': 'Chiffrement suspendu', 'EncryptionSuspended': 'Chiffrement suspendu',
    '5': 'Déchiffrement suspendu', 'DecryptionSuspended': 'Déchiffrement suspendu',
}
_PROTECTIONS_VOLUME = {
    '0': 'Désactivée', 'Off': 'Désactivée',
    '1': 'Activée', 'On': 'Activée',
    '2': 'Inconnue', 'Unknown': 'Inconnue',
}


def _win_security_events():
    """Échecs d'ouverture de session et verrouillages de comptes.

    Le journal Sécurité est un canal distinct de « System » et n'est lisible
    qu'avec des droits administrateur : sans élévation, la requête ne renvoie
    rien plutôt que d'échouer.

    Un compte verrouillé en boucle trahit le plus souvent un service qui tourne
    encore avec un mot de passe changé ; une rafale d'échecs sur un compte
    depuis une même source, une tentative d'intrusion.
    """
    data = _win_powershell_json(
        "$since=(Get-Date).AddDays(-%d); "
        "$e=@(); try { $e=@(Get-WinEvent -FilterHashtable @{LogName='Security'; "
        "ID=@(4625,4740); StartTime=$since} -MaxEvents 400 -ErrorAction SilentlyContinue) } catch {}; "
        "$e | ForEach-Object { $x=[xml]$_.ToXml(); "
        "$n=($x.Event.EventData.Data | Where-Object {$_.Name -eq 'TargetUserName'}).'#text'; "
        "$src=($x.Event.EventData.Data | Where-Object {$_.Name -in @('IpAddress','WorkstationName')} "
        "| Where-Object {$_.'#text' -and $_.'#text' -ne '-'} | Select-Object -First 1).'#text'; "
        "[PSCustomObject]@{ Id=$_.Id; Compte=$n; Source=$src; "
        "When=$_.TimeCreated.ToString('yyyy-MM-dd HH:mm') } } "
        "| ConvertTo-Json -Compress -Depth 3" % EVENT_WINDOW_DAYS,
        timeout=90,
    )

    groupes = {}
    for e in _as_list(data):
        try:
            identifiant = int(e.get('Id') or 0)
        except (TypeError, ValueError):
            continue
        compte = _clean(e.get('Compte'))
        # Les comptes machine (suffixés $) génèrent un bruit permanent sans
        # rapport avec une personne : ils n'apprennent rien ici.
        if not compte or compte.endswith('$'):
            continue
        cle = (identifiant, compte)
        entree = groupes.setdefault(cle, {
            'type': 'Verrouillage de compte' if identifiant == 4740
                    else "Échec d'ouverture de session",
            'event_id': identifiant, 'compte': compte,
            'count': 0, 'last_seen': '', 'sources': set(),
        })
        entree['count'] += 1
        quand = _clean(e.get('When'))
        if quand > entree['last_seen']:
            entree['last_seen'] = quand
        source = _clean(e.get('Source'))
        if source:
            entree['sources'].add(source)

    evenements = []
    for entree in sorted(groupes.values(), key=lambda g: (-g['count'], g['compte'])):
        entree['sources'] = sorted(entree['sources'])[:5]
        evenements.append(entree)

    resultat = {'security_events': evenements}
    if evenements:
        resultat['failed_logons'] = sum(e['count'] for e in evenements if e['event_id'] == 4625)
        resultat['account_lockouts'] = sum(e['count'] for e in evenements if e['event_id'] == 4740)
    return resultat


def _win_boot_performance():
    """Durée des derniers démarrages, telle que Windows la mesure lui-même.

    « Le poste est long à démarrer » est la plainte la plus courante et la plus
    difficile à objectiver. Windows chronomètre chaque démarrage dans son
    journal de diagnostic : autant lire sa mesure plutôt que de deviner.
    """
    data = _win_powershell_json(
        "$e=@(); try { $e=@(Get-WinEvent -FilterHashtable @{"
        "LogName='Microsoft-Windows-Diagnostics-Performance/Operational'; ID=100} "
        "-MaxEvents 10 -ErrorAction SilentlyContinue) } catch {}; "
        # Les champs sont des <Data Name="…">valeur</Data> : $d.BootTime ne
        # renvoie rien, il faut filtrer sur le nom. Vérifié sur un événement
        # réel — l'accès direct rendait la mesure invisible en permanence.
        "$e | ForEach-Object { $x=[xml]$_.ToXml(); $d=$x.Event.EventData.Data; "
        "$val={ param($n) ($d | Where-Object {$_.Name -eq $n}).'#text' }; "
        "[PSCustomObject]@{ "
        "When=$_.TimeCreated.ToString('yyyy-MM-dd HH:mm'); "
        "Total=(& $val 'BootTime'); Noyau=(& $val 'MainPathBootTime'); "
        "Bureau=(& $val 'BootPostBootTime'); "
        "Degradation=(& $val 'BootDegradationTime') } } "
        "| ConvertTo-Json -Compress -Depth 3",
        timeout=60,
    )

    demarrages = []
    for e in _as_list(data):
        def _ms(valeur):
            try:
                return int(valeur)
            except (TypeError, ValueError):
                return None
        total = _ms(e.get('Total'))
        if not total:
            continue
        demarrages.append({
            'when': _clean(e.get('When')),
            'secondes': round(total / 1000.0, 1),
            'noyau_s': round((_ms(e.get('Noyau')) or 0) / 1000.0, 1),
            'bureau_s': round((_ms(e.get('Bureau')) or 0) / 1000.0, 1),
        })

    if not demarrages:
        return {}
    durees = [d['secondes'] for d in demarrages]
    return {
        'boot_times': demarrages[:5],
        'boot_last_seconds': durees[0],
        'boot_average_seconds': round(sum(durees) / len(durees), 1),
    }


#: Sévérité Windows Defender (`MSFT_MpThreat.SeverityID`), table publiée par
#: Microsoft — 3 n'existe pas dans l'énumération.
_MP_SEVERITY = {0: 'Inconnue', 1: 'Faible', 2: 'Modérée', 4: 'Élevée', 5: 'Sévère'}
_MP_SEVERITY_LEVEL = {0: 'muted', 1: 'muted', 2: 'warn', 4: 'warn', 5: 'danger'}
#: `file:_C:\…`, `webfile:_D:\…|https://…|pid:1234,…` → chemin lisible seul.
_RE_MP_RESSOURCE = re.compile(r'^\w+:_')


def _chemin_ressource_defender(brut):
    """Chemin lisible depuis une ressource Defender, sans le jeton de session."""
    sans_prefixe = _RE_MP_RESSOURCE.sub('', brut or '', count=1)
    return sans_prefixe.split('|', 1)[0].strip()


def _categoriser_menace(nom_menace):
    """Catégorie et criticité d'une menace, depuis le préfixe de son nom.

    Le préfixe avant `:` (`Trojan:…`, `PUA:…`, `Ransom:…`) est la catégorie
    Microsoft, documentée et stable — contrairement à l'énumération numérique
    complète de `CategoryID`, dont seules quelques valeurs sont confirmées.
    Une menace sans préfixe reconnu reste « Autre » plutôt que mal étiquetée.
    """
    categorie = nom_menace.split(':', 1)[0] if ':' in nom_menace else ''
    niveau = 'danger' if categorie in ('Trojan', 'Virus', 'Ransom', 'Backdoor', 'Worm') else \
             'warn' if categorie == 'PUA' else 'muted'
    return categorie or 'Autre', niveau


def _win_malware_detections(jours=365, limite=100):
    """Historique des détections de menaces par Windows Defender.

    `Get-MpThreatDetection` donne les événements (fichier, processus, date,
    action) ; `Get-MpThreat` donne le catalogue des menaces elles-mêmes (nom,
    sévérité) — les deux se joignent sur `ThreatID`, Defender ne les expose pas
    déjà assemblés. La catégorie se lit dans le préfixe du nom (`Trojan:…`,
    `PUA:…`) plutôt que dans `CategoryID` : ce préfixe est documenté et stable,
    l'énumération numérique complète ne l'est pas.

    Une fenêtre d'un an plutôt que les 30 jours habituels : une détection est
    un événement rare, contrairement à un incident système — la borner à un
    mois masquerait le plus souvent une machine parfaitement saine.

    Ne couvre que Defender. Un antivirus tiers stocke sa quarantaine dans un
    format propre à l'éditeur, illisible génériquement.
    """
    data = _win_powershell_json(
        "$d=@(); try { $d=@(Get-MpThreatDetection -EA SilentlyContinue "
        "| Select-Object ThreatID,ProcessName,Resources,"
        "@{N='When';E={ if ($_.InitialDetectionTime) { "
        # `else { '' }` explicite : sans lui, une propriété calculée sans
        # branche de repli ressort de ConvertTo-Json comme `{}` plutôt que
        # `null` — une chaîne truthy qui aurait fait passer l'entrée telle
        # quelle. Constaté sur cette machine, pas supposé.
        "$_.InitialDetectionTime.ToString('yyyy-MM-dd HH:mm') } else { '' } }},"
        "ActionSuccess) } catch {}; "
        "$t=@(); try { $t=@(Get-MpThreat -EA SilentlyContinue "
        "| Select-Object ThreatID,ThreatName) } catch {}; "
        "[PSCustomObject]@{ D=$d; T=$t } | ConvertTo-Json -Compress -Depth 4",
        timeout=45,
    )
    if not data:
        return {}

    catalogue = {t.get('ThreatID'): _clean(t.get('ThreatName'))
                for t in _as_list(data.get('T')) if t.get('ThreatID') is not None}
    limite_date = (_utcnow() - timedelta(days=jours)).strftime('%Y-%m-%d %H:%M')

    detections = []
    for d in _as_list(data.get('D')):
        quand = _clean(d.get('When'))
        # Une propriété calculée PowerShell sans branche de repli peut
        # ressortir de ConvertTo-Json comme `{}` plutôt que `null` — une
        # chaîne non vide qui échapperait au `not quand` seul.
        if not re.match(r'^\d{4}-\d{2}-\d{2}', quand) or quand < limite_date:
            continue
        nom_menace = catalogue.get(d.get('ThreatID')) or 'Menace inconnue'
        categorie, gravite = _categoriser_menace(nom_menace)
        ressources = [_chemin_ressource_defender(r) for r in _as_list(d.get('Resources'))]
        ressources = [r for r in ressources if r]
        detections.append({
            'threat': nom_menace,
            'category': categorie,
            'level': gravite,
            'process': _clean(d.get('ProcessName')) or None,
            'resource': ressources[0] if ressources else None,
            'when': quand,
            'cleaned': bool(d.get('ActionSuccess')),
        })

    if not detections:
        return {}
    detections.sort(key=lambda x: x['when'], reverse=True)
    return {'malware_detections': detections[:limite],
            'malware_detections_total': len(detections)}


def _win_system_errors():
    """Erreurs du journal Système au sens large, groupées par source.

    Distinct de `system_incidents` (voir `_win_diagnostics`), qui ne retient
    volontairement qu'une liste restreinte de signaux critiques — arrêts
    inattendus, écrans bleus, disque. Ici, le reste : services qui échouent à
    démarrer, erreurs DCOM, pilotes en échec… Les (fournisseur, ID) déjà
    couverts par `_EVENT_SPECS` sont exclus pour ne pas doubler la même
    information sous un autre nom.
    """
    exclus = {(prov, i) for prov, ids, _lib, _lvl in _EVENT_SPECS for i in ids}
    data = _win_powershell_json(
        "$since=(Get-Date).AddDays(-%d); $e=@(); "
        "try { $e=@(Get-WinEvent -FilterHashtable @{LogName='System'; Level=@(1,2); "
        "StartTime=$since} -MaxEvents 500 -EA SilentlyContinue "
        "| Select-Object Id,ProviderName,"
        "@{N='When';E={$_.TimeCreated.ToString('yyyy-MM-dd HH:mm')}},"
        "@{N='Msg';E={($_.Message -split [char]10)[0].Trim()}}) } catch {}; "
        "$e | ConvertTo-Json -Compress -Depth 3" % EVENT_WINDOW_DAYS,
        timeout=60,
    )

    groupes = {}
    for e in _as_list(data):
        fournisseur = _clean(e.get('ProviderName'))
        identifiant = e.get('Id')
        if (fournisseur, identifiant) in exclus:
            continue
        cle = (fournisseur, identifiant, _clean(e.get('Msg')))
        entree = groupes.setdefault(cle, {
            'provider': fournisseur, 'event_id': identifiant, 'message': cle[2],
            'count': 0, 'last_seen': '',
        })
        entree['count'] += 1
        quand = _clean(e.get('When'))
        if quand > entree['last_seen']:
            entree['last_seen'] = quand

    if not groupes:
        return {}
    erreurs = sorted(groupes.values(), key=lambda g: (-g['count'], g['last_seen']))
    return {'system_errors': erreurs[:40]}


#: Codes d'exception NTSTATUS les plus courants dans les plantages
#: applicatifs — traduits pour ne pas laisser un technicien chercher
#: « c0000005 » dans un moteur de recherche.
_EXCEPTION_CODES = {
    'c0000005': 'Violation d\'accès mémoire',
    'c0000409': 'Protection contre le dépassement de pile (stack overrun)',
    'c00000fd': 'Débordement de pile (stack overflow)',
    'c0000135': 'DLL introuvable',
    'c0000142': "Échec de l'initialisation d'une DLL",
    '80000003': 'Point d\'arrêt (débogueur absent)',
    'c0000374': 'Corruption du tas (heap corruption)',
}


def _champs_erreur_application(event_id, module_brut, exception_brut, chemin_brut):
    """Type et champs valides d'un événement de plantage applicatif.

    Les indices 3/6/10 du XML ne désignent module/exception/chemin que pour un
    1000 (plantage) ; un 1002 (ne répond plus) suit un schéma à 10 champs sans
    équivalent aux mêmes positions — les y lire renvoyait un horodatage ou un
    GUID travesti en « module » ou en « exception ». Fonction pure, séparée de
    l'appel PowerShell pour rester testable sans lui.
    """
    est_plantage = event_id == 1000
    type_label = 'Plantage' if est_plantage else 'Ne répond plus'
    if not est_plantage:
        return type_label, None, None, None
    module = _clean(module_brut)
    module = module if module and module.lower() != 'unknown' else None
    code = _clean(exception_brut).lower()
    exception = _EXCEPTION_CODES.get(code, code) if code else None
    return type_label, module, exception, (_clean(chemin_brut) or None)


def _win_application_errors():
    """Plantages et blocages applicatifs (journal Application).

    Événements 1000 (l'application a cessé de fonctionner) et 1002 (ne répond
    plus). Leurs champs ne sont pas nommés dans le XML — contrairement à la
    plupart des événements déjà lus ailleurs dans ce fichier — Microsoft les
    documente par position : nom, version, horodatage, module fautif, version
    du module, horodatage du module, code d'exception, décalage, PID,
    horodatage de création du processus, chemin de l'exécutable. Un événement
    1002 n'en fournit qu'une poignée ; le reste vaut alors `None`, pas une
    erreur (voir `_champs_erreur_application`).
    """
    data = _win_powershell_json(
        "$since=(Get-Date).AddDays(-%d); $e=@(); "
        "try { $e=@(Get-WinEvent -FilterHashtable @{LogName='Application'; "
        "ID=@(1000,1002); StartTime=$since} -MaxEvents 300 -EA SilentlyContinue) } catch {}; "
        "$e | ForEach-Object { $x=[xml]$_.ToXml(); $d=@($x.Event.EventData.Data); "
        "[PSCustomObject]@{ Id=$_.Id; "
        "When=$_.TimeCreated.ToString('yyyy-MM-dd HH:mm'); "
        "App=$d[0]; Version=$d[1]; Module=$d[3]; Exception=$d[6]; Path=$d[10] } } "
        "| ConvertTo-Json -Compress -Depth 3" % EVENT_WINDOW_DAYS,
        timeout=60,
    )

    groupes = {}
    for e in _as_list(data):
        app = _clean(e.get('App'))
        if not app:
            continue
        type_label, module, exception, chemin = _champs_erreur_application(
            e.get('Id'), e.get('Module'), e.get('Exception'), e.get('Path'))
        cle = (app, type_label, module or '')
        entree = groupes.setdefault(cle, {
            'application': app, 'type': type_label, 'module': module,
            'exception': exception, 'path': chemin, 'count': 0, 'last_seen': '',
        })
        entree['count'] += 1
        if exception and not entree['exception']:
            entree['exception'] = exception
        quand = _clean(e.get('When'))
        if quand > entree['last_seen']:
            entree['last_seen'] = quand

    if not groupes:
        return {}
    erreurs = sorted(groupes.values(), key=lambda g: (-g['count'], g['last_seen']))
    return {'application_errors': erreurs[:40]}


def _win_shutdown_history(jours=EVENT_WINDOW_DAYS, limite=25):
    """Historique des arrêts/redémarrages, avec la raison indiquée par Windows.

    Événement 1074 : distingue un arrêt planifié (mise à jour, maintenance
    programmée) d'un arrêt réellement non planifié — complète
    `unexpected_shutdowns`, déduit d'un signal plus pauvre (simple absence de
    message d'arrêt propre, sans savoir si l'arrêt suivant était volontaire).
    """
    data = _win_powershell_json(
        "$since=(Get-Date).AddDays(-%d); $e=@(); "
        "try { $e=@(Get-WinEvent -FilterHashtable @{LogName='System'; ID=1074; "
        "StartTime=$since} -MaxEvents %d -EA SilentlyContinue) } catch {}; "
        "$e | ForEach-Object { $x=[xml]$_.ToXml(); $d=$x.Event.EventData.Data; "
        "$val={ param($n) ($d | Where-Object {$_.Name -eq $n}).'#text' }; "
        "[PSCustomObject]@{ "
        "When=$_.TimeCreated.ToString('yyyy-MM-dd HH:mm'); "
        "Action=(& $val 'param5'); Reason=(& $val 'param3'); "
        "User=(& $val 'param7') } } "
        "| ConvertTo-Json -Compress -Depth 3" % (jours, limite),
        timeout=45,
    )

    historique = []
    for e in _as_list(data):
        quand = _clean(e.get('When'))
        if not quand:
            continue
        raison = _clean(e.get('Reason'))
        # Windows retourne ce libellé précis pour un arrêt dont il n'a pas pu
        # déterminer la cause — c'est justement ce qui distingue un arrêt
        # volontaire d'un arrêt qui ne l'était pas.
        planifie = bool(raison) and not re.search(
            r'non planifi|aucun titre', raison, re.IGNORECASE)
        action = _clean(e.get('Action'))
        historique.append({
            'when': quand,
            'action': 'Redémarrage' if re.search('red', action, re.IGNORECASE) else 'Arrêt',
            'reason': raison or 'Non renseignée',
            'planned': planifie,
            'user': _clean(e.get('User')) or None,
        })

    return {'shutdown_history': historique} if historique else {}


def _win_certificates(jours_alerte=90):
    """Certificats machine proches de l'expiration.

    Un certificat expiré est une panne qui tombe un matin sans prévenir : VPN,
    bureau à distance ou 802.1X cessent de fonctionner sans qu'aucune
    modification n'ait été faite la veille.
    """
    data = _win_powershell_json(
        "Get-ChildItem Cert:\\LocalMachine\\My -ErrorAction SilentlyContinue "
        "| Select-Object @{N='Sujet';E={$_.Subject}}, @{N='Emetteur';E={$_.Issuer}}, "
        "@{N='Expire';E={$_.NotAfter.ToString('yyyy-MM-dd')}}, "
        "@{N='Jours';E={[int]($_.NotAfter - (Get-Date)).TotalDays}}, "
        "@{N='Empreinte';E={$_.Thumbprint}} "
        "| ConvertTo-Json -Compress -Depth 3",
        timeout=45,
    )

    certificats = []
    for c in _as_list(data):
        try:
            jours = int(c.get('Jours'))
        except (TypeError, ValueError):
            continue
        if jours > jours_alerte:
            continue
        sujet = _clean(c.get('Sujet'))
        certificats.append({
            'sujet': sujet.replace('CN=', '', 1) if sujet.startswith('CN=') else sujet,
            'emetteur': _clean(c.get('Emetteur')),
            'expire_le': _clean(c.get('Expire')),
            'jours_restants': jours,
            'expire': jours < 0,
        })
    certificats.sort(key=lambda c: c['jours_restants'])
    return {'certificates_expiring': certificats} if certificats else {}


def _win_bitlocker_keys():
    """Clés de récupération BitLocker des volumes chiffrés.

    Nécessite des droits administrateur. Ces clés déverrouillent les disques :
    ParcInfo les stocke chiffrées, comme les mots de passe des identifiants, et
    ne les affiche qu'à la demande.
    """
    data = _win_powershell_json(
        "$v=@(); try { $v=@(Get-BitLockerVolume -ErrorAction SilentlyContinue) } catch {}; "
        "$v | ForEach-Object { $vol=$_; "
        "$vol.KeyProtector | Where-Object { $_.KeyProtectorType -eq 'RecoveryPassword' } "
        "| ForEach-Object { [PSCustomObject]@{ Volume=$vol.MountPoint; "
        "Etat=[string]$vol.ProtectionStatus; Chiffrement=[string]$vol.EncryptionMethod; "
        "Identifiant=$_.KeyProtectorId; Cle=$_.RecoveryPassword } } } "
        "| ConvertTo-Json -Compress -Depth 3",
        timeout=60,
    )

    cles = []
    for k in _as_list(data):
        valeur = _clean(k.get('Cle'))
        if not valeur:
            continue
        identifiant = _clean(k.get('Identifiant')).strip('{}')
        cles.append({
            'volume': _clean(k.get('Volume')),
            'protection': _clean(k.get('Etat')),
            'chiffrement': _clean(k.get('Chiffrement')),
            'identifiant': identifiant,
            'cle': valeur,
        })
    return {'bitlocker_keys': cles} if cles else {}


#: Fin de support par build de Windows. À TENIR À JOUR : une date périmée ici
#: raconterait une histoire fausse, ce qui est pire que de ne rien dire. Un
#: build absent de la table ne produit aucune conclusion.
_FIN_DE_SUPPORT = {
    '19044': ('Windows 10 21H2', '2024-06-11'),
    '19045': ('Windows 10 22H2', '2025-10-14'),
    '22000': ('Windows 11 21H2', '2023-10-10'),
    '22621': ('Windows 11 22H2', '2024-10-08'),
    '22631': ('Windows 11 23H2', '2025-11-11'),
    '26100': ('Windows 11 24H2', '2026-10-13'),
    '14393': ('Windows Server 2016', '2027-01-12'),
    '17763': ('Windows Server 2019', '2029-01-09'),
    '20348': ('Windows Server 2022', '2031-10-14'),
}


def support_windows(build, aujourdhui=None):
    """Échéance de support de la version de Windows installée.

    Retourne None quand le build n'est pas connu de la table : mieux vaut ne
    rien annoncer qu'annoncer une date inventée.
    """
    if not build:
        return None
    numero = str(build).split('.')[0].strip()
    entree = _FIN_DE_SUPPORT.get(numero)
    if not entree:
        return None
    libelle, fin = entree
    try:
        echeance = datetime.strptime(fin, '%Y-%m-%d')
    except ValueError:
        return None
    reference = aujourdhui or _utcnow()
    jours = (echeance - reference).days
    return {
        'version': libelle,
        'fin_de_support': fin,
        'jours_restants': jours,
        'termine': jours < 0,
    }


def _win_user_profiles(budget_secondes=30):
    """Taille des profils utilisateurs locaux.

    Savoir qu'un disque se remplit sans savoir de quoi n'aide qu'à moitié, et
    les profils sont le premier suspect. Mesurer une taille impose de parcourir
    l'arborescence : l'opération est donc bornée dans le temps, et préfère ne
    rien renvoyer plutôt qu'un total partiel qui passerait pour exact.
    """
    # Le parcours doit rester interruptible, ce qui exclut deux écritures
    # naturelles : `foreach (… in Get-ChildItem -Recurse)` matérialise toute
    # l'arborescence avant la première itération — impossible d'arrêter à
    # temps —, et `break` dans un ForEach-Object sans boucle englobante
    # interrompt le script entier au lieu du seul parcours. D'où le pipeline
    # enveloppé dans une boucle étiquetée.
    data = _win_powershell_json(
        "$limite=(Get-Date).AddSeconds(%d); $res=@(); "
        "$profils=@(Get-CimInstance Win32_UserProfile -ErrorAction SilentlyContinue "
        "| Where-Object { -not $_.Special -and $_.LocalPath }); "
        "foreach($p in $profils){ "
        "  if((Get-Date) -gt $limite){ break } "
        "  $somme=[long]0; $complet=$true; "
        "  :mesure while($true) { "
        "    Get-ChildItem $p.LocalPath -Recurse -Force -File -ErrorAction SilentlyContinue "
        "    | ForEach-Object { $somme += $_.Length; "
        "        if((Get-Date) -gt $limite){ $complet=$false; break mesure } }; "
        "    break mesure } "
        "  $res += [PSCustomObject]@{ Chemin=$p.LocalPath; Octets=$somme; Complet=$complet; "
        "    Derniere=$(if($p.LastUseTime){$p.LastUseTime.ToString('yyyy-MM-dd')}else{''}) } }; "
        "[PSCustomObject]@{ Profils=@($res); Tous=($res.Count -eq $profils.Count) } "
        "| ConvertTo-Json -Compress -Depth 4" % budget_secondes,
        timeout=budget_secondes + 30,
    )
    if not data:
        return {}

    profils = []
    for p in _as_list(data.get('Profils')):
        try:
            octets = int(p.get('Octets') or 0)
        except (TypeError, ValueError):
            continue
        if not octets:
            continue
        chemin = _clean(p.get('Chemin'))
        profils.append({
            'chemin': chemin,
            'nom': chemin.rsplit('\\', 1)[-1] if chemin else '',
            'taille_go': round(octets / (1024 ** 3), 2),
            # Faux quand la mesure a été interrompue : la taille est alors un
            # minimum, jamais le total. Le marquer par profil et non une seule
            # fois pour l'ensemble évite de présenter un chiffre partiel comme
            # exact.
            'mesure_complete': bool(p.get('Complet')),
            'derniere_utilisation': _clean(p.get('Derniere')),
        })
    if not profils:
        return {}
    profils.sort(key=lambda p: p['taille_go'], reverse=True)
    return {
        'user_profiles': profils,
        # « tous vus » et non « tous mesurés » : un profil peut avoir été
        # parcouru sans que sa mesure ait pu aller au bout.
        'user_profiles_tous_vus': bool(data.get('Tous')),
    }


def _win_problem_devices():
    """Périphériques signalés en erreur par le Gestionnaire de périphériques.

    Un pilote manquant ou un matériel arrêté se voit ici, et nulle part ailleurs
    dans l'inventaire : le reste de la collecte décrit ce qui est présent, pas
    ce qui fonctionne mal.
    """
    data = _win_powershell_json(
        "Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue "
        "| Where-Object { $_.ConfigManagerErrorCode -ne 0 -and $_.ConfigManagerErrorCode -ne $null } "
        "| Select-Object Name, PNPClass, ConfigManagerErrorCode, DeviceID, Manufacturer "
        "| ConvertTo-Json -Compress -Depth 3",
        timeout=60,
    )
    peripheriques = []
    for d in _as_list(data):
        try:
            code = int(d.get('ConfigManagerErrorCode') or 0)
        except (TypeError, ValueError):
            continue
        if not code:
            continue
        peripheriques.append({
            'name': _clean(d.get('Name')) or 'Périphérique inconnu',
            'classe': _clean(d.get('PNPClass')),
            'fabricant': _clean(d.get('Manufacturer')),
            'code': code,
            'libelle': _CODES_PERIPHERIQUE.get(code, "Code d'erreur %d" % code),
            'instance': _clean(d.get('DeviceID')),
        })
    # Les codes 22 (désactivé) et 45 (déconnecté) décrivent des états voulus la
    # plupart du temps : ils restent listés, mais ne comptent pas comme pannes.
    en_panne = [p for p in peripheriques if p['code'] not in (22, 45)]
    return {'problem_devices': sorted(peripheriques, key=lambda p: (-p['code'], p['name'])),
            'problem_devices_count': len(en_panne)}


def _win_hygiene():
    """Réglages de sécurité et espace récupérable.

    Regroupe ce qui se vérifie en dépannage sans être de l'inventaire : peut-on
    revenir en arrière, l'UAC est-il actif, deux antivirus se gênent-ils, et
    combien d'espace un simple nettoyage rendrait-il.
    """
    info = {}
    data = _win_powershell_json(
        "$uac=$null; try { $uac=(Get-ItemProperty "
        "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System' "
        "-ErrorAction SilentlyContinue).EnableLUA } catch {}; "
        # RDP et l'ouverture automatique de session sont désormais relevés par
        # _win_remote_access, avec les autres voies d'accès distant.
        "$rp=@(); try { $rp=@(Get-ComputerRestorePoint -ErrorAction SilentlyContinue "
        "| Select-Object -Last 3 -Property Description,"
        "@{N='When';E={$_.ConvertToDateTime($_.CreationTime).ToString('yyyy-MM-dd HH:mm')}}) } catch {}; "
        "$temp=0; try { $temp=[math]::Round((Get-ChildItem $env:TEMP -Recurse -Force "
        "-ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum/1MB,0) } catch {}; "
        "[PSCustomObject]@{ Uac=$uac; Restore=$rp; TempMB=$temp } "
        "| ConvertTo-Json -Compress -Depth 4",
        timeout=90,
    )
    if not data:
        return info

    if data.get('Uac') is not None:
        info['uac_enabled'] = bool(data.get('Uac'))

    points = []
    for r in _as_list(data.get('Restore')):
        quand = _clean(r.get('When'))
        if quand:
            points.append({'description': _clean(r.get('Description')), 'when': quand})
    info['restore_points'] = points

    try:
        temp_mb = float(data.get('TempMB') or 0)
        if temp_mb > 0:
            info['temp_files_mb'] = round(temp_mb)
    except (TypeError, ValueError):
        pass

    return info


#: Mécanismes d'accès distant relevés, dans l'ordre d'affichage. Chacun devient
#: une ligne de la section « Accès distant & exposition ».
def _acces(cle, label, actif, detail='', securise=None, sensible=False):
    """Une entrée d'accès distant, avec sa criticité.

    `sensible` marque un accès à n'ouvrir qu'à bon escient (Telnet en clair,
    ouverture automatique de session) : actif, il passe en rouge plutôt qu'en
    simple information.
    """
    if actif is None:
        niveau = 'muted'
    elif not actif:
        niveau = 'ok' if sensible else 'muted'
    elif securise is False or sensible:
        niveau = 'danger'
    elif securise is True:
        niveau = 'ok'
    else:
        niveau = 'warn'
    return {'key': cle, 'label': label, 'enabled': actif,
            'secure': securise, 'detail': detail, 'level': niveau}


def _win_remote_access():
    """Voies d'accès distant et d'administration, actives ou non.

    Un port qui écoute ne dit pas si le service est *configuré* pour accepter
    des connexions ; on lit donc l'état réel de chaque mécanisme (registre et
    services), pas seulement la présence d'un port. L'ouverture automatique de
    session est incluse : c'est un contournement d'authentification, et le mot
    de passe traîne parfois en clair dans le registre — on en signale la
    présence, jamais la valeur.
    """
    info = {}
    data = _win_powershell_json(
        "function svc($n){ $s=Get-Service -Name $n -ErrorAction SilentlyContinue; "
        "if($s){ [PSCustomObject]@{Etat=[string]$s.Status; Demarrage=[string]$s.StartType} } else { $null } }; "
        "$rdp=$null; try { $rdp=(Get-ItemProperty "
        "'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' -EA SilentlyContinue).fDenyTSConnections } catch {}; "
        "$nla=$null; try { $nla=(Get-ItemProperty "
        "'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server\\WinStations\\RDP-Tcp' -EA SilentlyContinue).UserAuthentication } catch {}; "
        "$ra=$null; try { $ra=(Get-ItemProperty "
        "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Remote Assistance' -EA SilentlyContinue).fAllowToGetHelp } catch {}; "
        "$telcli=Test-Path \"$env:SystemRoot\\System32\\telnet.exe\"; "
        "$alo=$null; $alu=$null; $alp=$false; try { "
        "$w=Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon' -EA SilentlyContinue; "
        "$alo=$w.AutoAdminLogon; $alu=$w.DefaultUserName; $alp=[bool]$w.DefaultPassword } catch {}; "
        "[PSCustomObject]@{ Rdp=$rdp; Nla=$nla; WinRM=(svc 'WinRM'); Sshd=(svc 'sshd'); "
        "Telnet=(svc 'tlntsvr'); TelnetClient=$telcli; RemoteReg=(svc 'RemoteRegistry'); "
        "RA=$ra; AutoLogon=$alo; AutoUser=$alu; AutoPwd=$alp } | ConvertTo-Json -Compress -Depth 4",
        timeout=40,
    )
    if not data:
        return info

    def actif_service(bloc):
        """Un service compte comme actif s'il tourne ou démarre automatiquement."""
        if not isinstance(bloc, dict):
            return None
        etat = (bloc.get('Etat') or '').lower()
        demarrage = (bloc.get('Demarrage') or '').lower()
        if etat == 'running':
            return True
        if 'auto' in demarrage:
            return True
        return False

    def detail_service(bloc):
        if not isinstance(bloc, dict):
            return 'service absent'
        return 'service %s, démarrage %s' % (bloc.get('Etat') or '?',
                                             bloc.get('Demarrage') or '?')

    acces = []

    # RDP : fDenyTSConnections = 1 signifie que les connexions sont refusées.
    rdp_actif = None
    if data.get('Rdp') is not None:
        rdp_actif = not bool(data.get('Rdp'))
        nla = bool(data['Nla']) if data.get('Nla') is not None else None
        info['rdp_enabled'] = rdp_actif          # conservé pour les alertes et les vignettes
        if rdp_actif and nla is not None:
            info['rdp_nla'] = nla
        detail = ('NLA actif' if nla else 'sans NLA — authentification faible') if rdp_actif else ''
        acces.append(_acces('rdp', 'Bureau à distance (RDP)', rdp_actif, detail,
                            securise=nla if rdp_actif else None))

    winrm = data.get('WinRM')
    acces.append(_acces('winrm', 'WinRM / PowerShell Remoting', actif_service(winrm),
                        detail_service(winrm)))
    sshd = data.get('Sshd')
    acces.append(_acces('ssh', 'OpenSSH Server', actif_service(sshd), detail_service(sshd)))

    telnet_srv = data.get('Telnet')
    acces.append(_acces('telnet_server', 'Serveur Telnet', actif_service(telnet_srv),
                        detail_service(telnet_srv), sensible=True))
    acces.append(_acces('telnet_client', 'Client Telnet installé',
                        bool(data.get('TelnetClient')),
                        'commande telnet.exe présente' if data.get('TelnetClient') else '',
                        sensible=False))

    if data.get('RA') is not None:
        ra_actif = bool(data.get('RA'))
        acces.append(_acces('remote_assistance', 'Assistance à distance', ra_actif,
                            'invitations autorisées' if ra_actif else '', sensible=ra_actif))

    rreg = data.get('RemoteReg')
    acces.append(_acces('remote_registry', 'Registre distant', actif_service(rreg),
                        detail_service(rreg), sensible=actif_service(rreg)))

    info['remote_access'] = acces

    # Ouverture automatique de session : traitée à part, avec le compte concerné.
    if str(data.get('AutoLogon') or '0') == '1':
        info['autologon'] = {
            'enabled': True,
            'user': _clean(data.get('AutoUser')) or None,
            'password_stored': bool(data.get('AutoPwd')),
        }
    else:
        info['autologon'] = {'enabled': False, 'user': None, 'password_stored': False}

    return info


def _mac_remote_access():
    """Connexion à distance (SSH) et Partage d'écran (VNC/ARD) — pendant
    macOS des accès distants Windows (`_win_remote_access()`), même
    structure de sortie via `_acces()`.

    Les deux sources exigent les privilèges administrateur pour donner une
    réponse fiable (`systemsetup` refuse même la lecture sans root ;
    `launchctl print` sur le domaine système est incomplet sans root) : sans
    élévation, ces blocs échouent silencieusement et le champ reste absent
    plutôt que faussement "désactivé" — comme SMART/TPM/BitLocker côté
    Windows non élevé.
    """
    info = {}
    acces = []

    ssh_actif = None
    sortie = _run(['systemsetup', '-getremotelogin'], timeout=10)
    if sortie.strip():
        ssh_actif = 'on' in sortie.lower()
        acces.append(_acces('ssh', 'Connexion à distance (SSH)', ssh_actif,
                            sortie.strip()))

    partage_ecran = None
    sortie = _run(['launchctl', 'print', 'system/com.apple.screensharing'], timeout=10)
    if sortie.strip():
        partage_ecran = 'state = running' in sortie.lower()
        acces.append(_acces('screen_sharing', "Partage d'écran (VNC/ARD)",
                            partage_ecran,
                            'service chargé et actif' if partage_ecran
                            else 'service non actif'))

    if acces:
        info['remote_access'] = acces
    return info


def _win_mail_accounts():
    """Comptes de messagerie configurés — paramètres serveur, jamais les mots
    de passe.

    Les mots de passe d'Outlook (DPAPI) et de Thunderbird (NSS) sont
    déchiffrables sous le compte de l'utilisateur ; les extraire ferait de ce
    collecteur un outil de vol d'identifiants, et ces rapports se répliquent
    d'une instance à l'autre. On se limite donc aux paramètres, et à un simple
    drapeau « mot de passe enregistré ».
    """
    info = {}
    comptes = []
    comptes.extend(_mail_outlook_classique())
    comptes.extend(_mail_thunderbird())

    nouveau = _mail_new_outlook()
    if comptes:
        info['mail_accounts'] = comptes
    if nouveau:
        info['mail_new_outlook'] = nouveau
    return info


#: Reconnaît une adresse mail, pour récupérer celle des comptes Exchange dont
#: seul le nom affiché porte l'adresse.
_RE_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _mail_decode(valeur):
    """Décode une valeur de compte Outlook, souvent stockée en UTF-16."""
    if isinstance(valeur, bytes):
        try:
            return valeur.decode('utf-16-le').rstrip('\x00').strip()
        except Exception:
            return ''
    return _clean(valeur)


def _mail_outlook_classique():
    """Comptes Outlook « classique » lus dans le registre du profil.

    Outlook range chaque compte sous un sous-profil ; les valeurs utiles
    (adresse, serveurs, ports) sont mêlées à beaucoup de binaire. On ne retient
    que ce qui se décode en texte, et on ignore le reste sans bruit.
    """
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except Exception:
        return []

    comptes = []
    racine = r'Software\Microsoft\Office'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, racine) as cle:
            versions = []
            i = 0
            while True:
                try:
                    versions.append(winreg.EnumKey(cle, i)); i += 1
                except OSError:
                    break
    except OSError:
        return []

    # 9375CFF0413111d3B88A00104B2A6676 est le conteneur des comptes de messagerie.
    GUID = '9375CFF0413111d3B88A00104B2A6676'
    for ver in versions:
        base = r'%s\%s\Outlook\Profiles' % (racine, ver)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as cprof:
                profils = []
                i = 0
                while True:
                    try:
                        profils.append(winreg.EnumKey(cprof, i)); i += 1
                    except OSError:
                        break
        except OSError:
            continue

        for profil in profils:
            chemin = r'%s\%s\%s' % (base, profil, GUID)
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, chemin) as ccpt:
                    idx = 0
                    while True:
                        try:
                            sous = winreg.EnumKey(ccpt, idx); idx += 1
                        except OSError:
                            break
                        compte = _mail_lire_compte_outlook(
                            winreg, r'%s\%s' % (chemin, sous), profil)
                        if compte:
                            comptes.append(compte)
            except OSError:
                continue
    return comptes


def _mail_lire_compte_outlook(winreg, chemin, profil):
    """Extrait les paramètres lisibles d'un sous-compte Outlook."""
    valeurs = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, chemin) as cle:
            i = 0
            while True:
                try:
                    nom, donnee, _ = winreg.EnumValue(cle, i); i += 1
                except OSError:
                    break
                valeurs[nom] = donnee
    except OSError:
        return None

    email = _mail_decode(valeurs.get('Email')) or _mail_decode(valeurs.get('SMTP Email Address'))
    nom_affiche = _mail_decode(valeurs.get('Display Name')) or _mail_decode(valeurs.get('Account Name'))
    imap = _mail_decode(valeurs.get('IMAP Server'))
    pop = _mail_decode(valeurs.get('POP3 Server'))
    smtp = _mail_decode(valeurs.get('SMTP Server'))
    entrant = imap or pop

    # Le profil contient aussi des entrées qui ne sont pas des comptes : carnet
    # d'adresses, fichier de données PST. Elles n'ont ni adresse ni serveur.
    if not email and _RE_EMAIL.match(nom_affiche or ''):
        # Compte Exchange/Hotmail : l'adresse n'est que dans le nom affiché.
        email = nom_affiche
    if not (email or entrant or smtp):
        return None

    protocole = ('IMAP' if imap else 'POP3' if pop
                 else 'Exchange/Autre' if email else '')

    def port(nom):
        v = valeurs.get(nom)
        try:
            return int(v) if v not in (None, '') else None
        except (TypeError, ValueError):
            return None

    return {
        'client': 'Outlook',
        'email': email,
        'display_name': nom_affiche,
        'protocol': protocole,
        'incoming_server': entrant,
        'incoming_port': port('IMAP Port') or port('POP3 Port'),
        'outgoing_server': smtp,
        'outgoing_port': port('SMTP Port'),
        'password_stored': None,   # blob DPAPI présent ou non : non déterminé ici
        'profile': _clean(profil),
    }


def _mail_thunderbird():
    """Comptes Thunderbird, lus dans le prefs.js de chaque profil."""
    base = os.path.join(os.environ.get('APPDATA', ''), 'Thunderbird', 'Profiles')
    if not os.path.isdir(base):
        return []

    comptes = []
    for profil in os.listdir(base):
        prefs = os.path.join(base, profil, 'prefs.js')
        if not os.path.isfile(prefs):
            continue
        try:
            with open(prefs, 'r', encoding='utf-8', errors='replace') as f:
                contenu = f.read()
        except OSError:
            continue
        comptes.extend(_parse_thunderbird_prefs(contenu, profil))
    return comptes


def _parse_thunderbird_prefs(contenu, profil):
    """Reconstruit les comptes à partir des `user_pref(...)` de Thunderbird.

    Thunderbird éclate chaque compte entre un `mail.server.serverN` (réception),
    un `mail.smtpserver.smtpM` (envoi) et une `mail.identity.idK` (adresse) ; on
    rassemble ces morceaux par leurs identifiants.
    """
    prefs = {}
    for m in re.finditer(r'user_pref\("([^"]+)",\s*(".*?"|true|false|-?\d+)\);', contenu):
        cle, brut = m.group(1), m.group(2)
        if brut.startswith('"'):
            valeur = brut[1:-1]
        elif brut in ('true', 'false'):
            valeur = brut == 'true'
        else:
            valeur = int(brut)
        prefs[cle] = valeur

    def sous(prefixe):
        ids = set()
        for cle in prefs:
            if cle.startswith(prefixe):
                ids.add(cle[len(prefixe):].split('.', 1)[0])
        return ids

    identites = {}
    for i in sous('mail.identity.'):
        identites[i] = {
            'email': prefs.get('mail.identity.%s.useremail' % i, ''),
            'name': prefs.get('mail.identity.%s.fullName' % i, ''),
            'smtp': prefs.get('mail.identity.%s.smtpServer' % i, ''),
        }

    smtps = {}
    for s in sous('mail.smtpserver.'):
        smtps[s] = {
            'server': prefs.get('mail.smtpserver.%s.hostname' % s, ''),
            'port': prefs.get('mail.smtpserver.%s.port' % s, None),
        }

    # Relie chaque serveur de réception à son identité via mail.account.*
    id_par_serveur = {}
    for a in sous('mail.account.'):
        serveur = prefs.get('mail.account.%s.server' % a)
        identity = prefs.get('mail.account.%s.identities' % a, '')
        if serveur:
            id_par_serveur[serveur] = (identity or '').split(',')[0].strip()

    comptes = []
    for s in sous('mail.server.'):
        hote = prefs.get('mail.server.%s.hostname' % s)
        if not hote:
            continue
        ident = identites.get(id_par_serveur.get(s, ''), {})
        smtp = smtps.get((ident.get('smtp') or '').replace('smtp://', ''), {})
        # L'identité peut désigner son smtp par clé (smtp1) ou par hôte.
        if not smtp and ident.get('smtp'):
            smtp = smtps.get(ident['smtp'], {})
        comptes.append({
            'client': 'Thunderbird',
            'email': _clean(ident.get('email')),
            'display_name': _clean(ident.get('name')),
            'protocol': (prefs.get('mail.server.%s.type' % s, '') or '').upper(),
            'incoming_server': _clean(hote),
            'incoming_port': prefs.get('mail.server.%s.port' % s) or None,
            'outgoing_server': _clean(smtp.get('server')),
            'outgoing_port': smtp.get('port') or None,
            # logins.json + key4.db : présence d'un secret enregistré, sans le lire
            'password_stored': _thunderbird_a_un_secret(profil),
            'profile': _clean(profil),
        })
    return comptes


def _thunderbird_a_un_secret(profil):
    """Vrai si le profil Thunderbird stocke des identifiants (sans les lire)."""
    base = os.path.join(os.environ.get('APPDATA', ''), 'Thunderbird', 'Profiles', profil)
    fichier = os.path.join(base, 'logins.json')
    try:
        if os.path.isfile(fichier) and os.path.getsize(fichier) > 2:
            return True
    except OSError:
        pass
    return None


def _mail_new_outlook():
    """Présence et comptes du « nouvel Outlook », dans la mesure du lisible.

    Le nouvel Outlook range ses comptes dans le magasin de l'application
    Courrier, hors du registre classique. On détecte sa présence et on tente d'y
    lire les adresses ; ce qui n'est pas énumérable est signalé comme tel plutôt
    que passé sous silence.
    """
    if not IS_WINDOWS:
        return None
    base = os.path.join(os.environ.get('LOCALAPPDATA', ''),
                        'Packages')
    if not os.path.isdir(base):
        return None
    paquet = None
    try:
        for nom in os.listdir(base):
            if nom.lower().startswith('microsoft.outlookforwindows'):
                paquet = nom
                break
    except OSError:
        return None
    if not paquet:
        return None

    adresses = []
    # Les comptes du nouvel Outlook ne sont pas exposés de façon stable ; on
    # remonte la seule présence, avec les adresses si un fichier lisible les
    # contient. Ne pas inventer ce qu'on ne peut pas lire de façon fiable.
    return {'installed': True, 'accounts': adresses,
            'note': 'Comptes non énumérables de façon fiable — présence détectée'}


#: ProgId de navigateur → nom lisible, pour l'association par défaut.
_PROGID_NAVIGATEURS = {
    'ChromeHTML': 'Google Chrome', 'FirefoxURL': 'Mozilla Firefox',
    'MSEdgeHTM': 'Microsoft Edge', 'IE.HTTP': 'Internet Explorer',
    'BraveHTML': 'Brave', 'OperaStable': 'Opera',
    'AppXq0fevzme2pys62n3e0fbqa7peapykr8v': 'Microsoft Edge',
}


def _nom_client_mail(progid):
    """Nom lisible d'un client mail à partir de son ProgId mailto."""
    p = progid.lower()
    if 'outlook' in p:
        return 'Microsoft Outlook'
    if 'thunderbird' in p:
        return 'Mozilla Thunderbird'
    if 'onenote' in p:
        return 'OneNote'
    return progid


#: Extensions de fichiers courantes dont on veut connaître le programme par
#: défaut, en plus du navigateur/client mail — celles citées explicitement
#: par les utilisateurs (PDF, TXT, LOG, JPG) plus quelques bureautiques
#: fréquentes en dépannage.
_EXTENSIONS_SUIVIES = ('.pdf', '.txt', '.log', '.jpg', '.png', '.docx', '.xlsx', '.csv')


def _win_workstation_extras():
    """Réglages du poste utiles au dépannage : applications par défaut,
    navigateurs installés, associations de fichiers, lecteurs réseau,
    redémarrage en attente.

    Chaque application identifiée (navigateur/mail par défaut, association de
    fichier, navigateur installé) porte aussi sa vraie icône — extraite du
    fichier exécutable via `ExtractIconEx` (Win32), pas une icône générique
    déduite du nom. `DefaultIcon` (clé de registre associée à chaque ProgId)
    donne le chemin et l'index à extraire ; mise en cache par valeur brute
    dans le script PowerShell pour ne jamais extraire deux fois la même icône
    (plusieurs associations pointent souvent vers le même programme).
    """
    info = {}
    liste_ext_ps = "@(%s)" % ','.join("'%s'" % e for e in _EXTENSIONS_SUIVIES)
    script = (
        r'''
Add-Type -AssemblyName System.Drawing
try {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class ParcInfoIconExtractor {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    public static extern int ExtractIconEx(string file, int index, out IntPtr large, out IntPtr small, int count);
}
'@ -ErrorAction SilentlyContinue
} catch {}

$iconCache = @{}
function Resolve-DefaultIconSpec($progId) {
    if (-not $progId) { return $null }
    try { return (Get-ItemProperty "Registry::HKEY_CLASSES_ROOT\$progId\DefaultIcon" -EA SilentlyContinue).'(default)' } catch { return $null }
}
function Get-IconBase64($spec) {
    if (-not $spec) { return $null }
    if ($iconCache.ContainsKey($spec)) { return $iconCache[$spec] }
    $resultat = $null
    try {
        $path = $spec; $idx = 0
        if ($spec -match '^"?(.+?)"?,(-?\d+)$') { $path = $matches[1]; $idx = [int]$matches[2] }
        $path = [Environment]::ExpandEnvironmentVariables($path.Trim('"'))
        if (Test-Path -LiteralPath $path) {
            # Un index négatif dans DefaultIcon désigne un identifiant de
            # ressource (pas une position) — ExtractIconEx le sait déjà
            # nativement, il ne faut surtout pas le rendre positif.
            $large=[IntPtr]::Zero; $small=[IntPtr]::Zero
            [void][ParcInfoIconExtractor]::ExtractIconEx($path, $idx, [ref]$large, [ref]$small, 1)
            $handle = if ($large -ne [IntPtr]::Zero) { $large } elseif ($small -ne [IntPtr]::Zero) { $small } else { [IntPtr]::Zero }
            if ($handle -ne [IntPtr]::Zero) {
                $icon = [System.Drawing.Icon]::FromHandle($handle)
                # Canevas fixe 32x32 : une icône source plus grande ou plus
                # petite est mise à l'échelle, la taille du rapport reste
                # prévisible quelle que soit l'application.
                $bmp = New-Object System.Drawing.Bitmap 32,32
                $g = [System.Drawing.Graphics]::FromImage($bmp)
                $g.DrawIcon($icon, 0, 0)
                $g.Dispose()
                $ms = New-Object System.IO.MemoryStream
                $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
                $resultat = [Convert]::ToBase64String($ms.ToArray())
            }
        }
    } catch {}
    $iconCache[$spec] = $resultat
    return $resultat
}

# Association par défaut https et mailto (choix de l'utilisateur)
$nav=$null; try { $nav=(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice' -EA SilentlyContinue).ProgId } catch {}
$courriel=$null; try { $courriel=(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\mailto\UserChoice' -EA SilentlyContinue).ProgId } catch {}

# Navigateurs installés : déclarés sous StartMenuInternet, avec leur exe
$navs=@()
try {
    foreach ($ruche in @('HKLM:\SOFTWARE\Clients\StartMenuInternet','HKCU:\SOFTWARE\Clients\StartMenuInternet')) {
        if (Test-Path $ruche) {
            foreach ($c in Get-ChildItem $ruche -EA SilentlyContinue) {
                $exe=(Get-ItemProperty "$($c.PSPath)\shell\open\command" -EA SilentlyContinue).'(default)'
                $exe=($exe -replace '"','').Trim()
                $ver=''
                if ($exe -and (Test-Path $exe)) { try { $ver=(Get-Item $exe).VersionInfo.ProductVersion } catch {} }
                $icone = if ($exe -and (Test-Path $exe)) { Get-IconBase64 "$exe,0" } else { $null }
                $navs += [PSCustomObject]@{ Nom=(Get-ItemProperty $c.PSPath -EA SilentlyContinue).'(default)'; Version=$ver; Exe=$exe; Icone=$icone }
            }
        }
    }
} catch {}

# Association par défaut pour une poignée d'extensions courantes : choix
# utilisateur (FileExts\UserChoice) sinon valeur globale HKCR, puis nom
# lisible du ProgId (description enregistrée, sinon le ProgId lui-même).
$assocs=@()
foreach ($ext in ''' + liste_ext_ps + r''') {
    try {
        $progId=$null; $cle="HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$ext\UserChoice"
        if (Test-Path $cle) { $progId=(Get-ItemProperty $cle -EA SilentlyContinue).ProgId }
        if (-not $progId) { $progId=(Get-ItemProperty "Registry::HKEY_CLASSES_ROOT\$ext" -EA SilentlyContinue).'(default)' }
        $nomProg=$null
        if ($progId) { $nomProg=(Get-ItemProperty "Registry::HKEY_CLASSES_ROOT\$progId" -EA SilentlyContinue).'(default)' }
        if ($progId -or $nomProg) {
            $assocs += [PSCustomObject]@{ Ext=$ext; ProgId=$progId; Nom=$nomProg; Icone=(Get-IconBase64 (Resolve-DefaultIconSpec $progId)) }
        }
    } catch {}
}

# Lecteurs réseau mappés
$lecteurs=@(); try { $lecteurs=@(Get-CimInstance Win32_MappedLogicalDisk -EA SilentlyContinue | Select-Object DeviceID,ProviderName) } catch {}

# Redémarrage en attente : plusieurs indices possibles
$rb=@()
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $rb+='Servicing (mise à jour de composants)' }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') { $rb+='Windows Update' }
try { if ((Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -EA SilentlyContinue).PendingFileRenameOperations) { $rb+='Fichiers en attente de renommage' } } catch {}

$navIcon = Get-IconBase64 (Resolve-DefaultIconSpec $nav)
$courrielIcon = Get-IconBase64 (Resolve-DefaultIconSpec $courriel)

[PSCustomObject]@{ Navigateur=$nav; NavigateurIcone=$navIcon; Courriel=$courriel; CourrielIcone=$courrielIcon;
    Navigateurs=$navs; Associations=$assocs; Lecteurs=$lecteurs; Reboot=$rb } | ConvertTo-Json -Compress -Depth 4
'''
    )
    data = _win_powershell_json(script, timeout=50)
    if not data:
        return info

    prog_nav = _clean(data.get('Navigateur'))
    if prog_nav:
        info['default_browser'] = _PROGID_NAVIGATEURS.get(prog_nav, prog_nav)
        icone = _clean(data.get('NavigateurIcone'))
        if icone:
            info['default_browser_icon'] = icone
    prog_mail = _clean(data.get('Courriel'))
    if prog_mail:
        info['default_mail'] = _nom_client_mail(prog_mail)
        icone = _clean(data.get('CourrielIcone'))
        if icone:
            info['default_mail_icon'] = icone

    associations = []
    for a in _as_list(data.get('Associations')):
        ext = _clean(a.get('Ext'))
        if not ext:
            continue
        nom = _clean(a.get('Nom')) or _clean(a.get('ProgId'))
        if not nom:
            continue
        associations.append({'extension': ext, 'name': nom, 'icon': _clean(a.get('Icone')) or None})
    if associations:
        info['file_type_defaults'] = associations

    navigateurs = []
    for n in _as_list(data.get('Navigateurs')):
        nom = _clean(n.get('Nom'))
        if not nom:
            continue
        navigateurs.append({
            'name': nom, 'version': _clean(n.get('Version')),
            'icon': _clean(n.get('Icone')) or None,
        })
    if navigateurs:
        # Dédoublonnage : une même install peut figurer sous HKLM et HKCU.
        vus, uniques = set(), []
        for n in navigateurs:
            if n['name'].lower() in vus:
                continue
            vus.add(n['name'].lower()); uniques.append(n)
        info['installed_browsers'] = sorted(uniques, key=lambda n: n['name'].lower())

    lecteurs = []
    for d in _as_list(data.get('Lecteurs')):
        lettre = _clean(d.get('DeviceID'))
        chemin = _clean(d.get('ProviderName'))
        if lettre or chemin:
            lecteurs.append({'letter': lettre, 'path': chemin})
    if lecteurs:
        info['mapped_drives'] = sorted(lecteurs, key=lambda d: d['letter'])

    raisons = [r for r in _as_list(data.get('Reboot')) if _clean(r)]
    info['reboot_pending'] = bool(raisons)
    if raisons:
        info['reboot_reasons'] = raisons

    return info


#: Agents de télémaintenance/RMM reconnus par sous-chaîne de leur nom affiché.
#: Les noms de service internes varient trop d'une version à l'autre pour
#: servir de clé fiable ; le nom affiché, lui, reste stable. Recherche au
#: mieux : ni exhaustive, ni une preuve — elle vise à préremplir la fiche
#: appareil, que le technicien corrige si besoin.
_AGENTS_RMM = [
    ('NinjaRMM', 'NinjaOne', 'NinjaRMM'),
    ('ScreenConnect', 'ConnectWise', 'ScreenConnect'),
    ('ConnectWise Automate', 'ConnectWise', 'ConnectWise Automate'),
    ('LabTech', 'ConnectWise', 'ConnectWise Automate'),
    ('CentraStage', 'Datto', 'Datto RMM'),
    ('Datto RMM', 'Datto', 'Datto RMM'),
    ('N-central', 'N-able', 'N-able N-central'),
    ('Advanced Monitoring Agent', 'N-able', 'N-able N-sight'),
    ('AteraAgent', 'Atera', 'Atera Agent'),
    ('Atera Agent', 'Atera', 'Atera Agent'),
    ('Kaseya', 'Kaseya', 'Kaseya VSA'),
    ('Syncro', 'Syncro', 'Syncro'),
    ('Pulseway', 'Pulseway', 'Pulseway'),
    ('TeamViewer', 'TeamViewer', 'TeamViewer Remote Management'),
    ('AnyDesk', 'AnyDesk', 'AnyDesk'),
    ('Splashtop', 'Splashtop', 'Splashtop'),
    ('LogMeIn', 'LogMeIn', 'LogMeIn'),
]
#: Agents EDR reconnus par sous-chaîne — distincts de `antivirus_products`
#: (issu de SecurityCenter2), qui ne référence pas toujours les EDR.
_AGENTS_EDR = [
    ('CrowdStrike', 'CrowdStrike', 'CrowdStrike Falcon'),
    ('SentinelOne', 'SentinelOne', 'SentinelOne Singularity'),
    ('Sentinel Agent', 'SentinelOne', 'SentinelOne Singularity'),
    ('Cortex XDR', 'Palo Alto Networks', 'Palo Alto Cortex XDR'),
    ('Carbon Black', 'VMware Carbon Black', 'VMware Carbon Black Cloud'),
    ('Cb Defense', 'VMware Carbon Black', 'VMware Carbon Black Cloud'),
    ('Cybereason', 'Cybereason', 'Cybereason Defense Platform'),
    ('Sophos Intercept X', 'Sophos', 'Sophos Intercept X'),
    ('Elastic Endpoint', 'Elastic', 'Elastic Security'),
    ('Defender Advanced Threat Protection', 'Microsoft', 'Microsoft Defender for Endpoint'),
]
#: Format d'un identifiant AnyDesk : une suite de chiffres, parfois groupée
#: par trois avec des espaces. Toute autre sortie (aide affichée, erreur) est
#: écartée plutôt que remontée telle quelle.
_RE_ANYDESK_ID = re.compile(r'^\d[\d ]{5,}$')


def chercher_agents(services, catalogue):
    """Services correspondant au catalogue (RMM ou EDR), par sous-chaîne du
    nom affiché — au mieux, pas une preuve. Fonction pure, testée à part de
    la collecte PowerShell qui lui fournit ses données.
    """
    trouves, vus = [], set()
    for s in services:
        affiche = _clean(s.get('DisplayName'))
        if not affiche:
            continue
        for motif, marque, produit in catalogue:
            if produit in vus or motif.lower() not in affiche.lower():
                continue
            trouves.append({
                'marque': marque, 'nom': produit, 'service': affiche,
                'actif': (s.get('State') or '').lower() == 'running',
            })
            vus.add(produit)
    return trouves


def _win_managed_agents():
    """Agents de télémaintenance, RMM et EDR détectés parmi les services.

    Alimente le préremplissage de av_nom/edr_nom/rmm_nom/anydesk_id sur la
    fiche appareil (ces champs existaient déjà, saisis à la main jusqu'ici).
    L'identifiant AnyDesk se lit directement via `anydesk.exe --get-id`,
    documenté par l'éditeur — pas de fichier de configuration à interpréter.
    """
    info = {}
    data = _win_powershell_json(
        "$svcs = @(Get-CimInstance Win32_Service -EA SilentlyContinue "
        "| Select-Object Name,DisplayName,State); "
        "$id=$null; try { "
        "$exe = (Get-Command anydesk.exe -EA SilentlyContinue).Source; "
        "if (-not $exe) { foreach ($c in @(\"${env:ProgramFiles(x86)}\\AnyDesk\\AnyDesk.exe\", "
        "\"$env:ProgramFiles\\AnyDesk\\AnyDesk.exe\", \"$env:APPDATA\\AnyDesk\\AnyDesk.exe\")) { "
        "if (Test-Path $c) { $exe = $c; break } } }; "
        "if ($exe) { $id = (& $exe --get-id 2>$null | Select-Object -First 1); $id = ($id -as [string]).Trim() } "
        "} catch {}; "
        "[PSCustomObject]@{ Services=$svcs; AnyDeskId=$id } | ConvertTo-Json -Compress -Depth 3",
        timeout=30,
    )
    if not data:
        return info

    services = _as_list(data.get('Services'))
    rmm = chercher_agents(services, _AGENTS_RMM)
    if rmm:
        info['remote_support_agents'] = rmm
    edr = chercher_agents(services, _AGENTS_EDR)
    if edr:
        info['edr_agents'] = edr

    anydesk_id = _clean(data.get('AnyDeskId'))
    if anydesk_id and _RE_ANYDESK_ID.match(anydesk_id):
        info['anydesk_id'] = anydesk_id.replace(' ', '')

    return info


#: Supplément macOS à `_AGENTS_RMM` : MDM/RMM courants en parc Mac
#: professionnel, absents des services Windows donc jamais rencontrés côté
#: `_win_managed_agents()`. Même format (motif, marque, produit), même
#: recherche par sous-chaîne — au mieux, pas une preuve.
_AGENTS_RMM_MAC = [
    ('JamfDaemon', 'Jamf', 'Jamf Pro'),
    ('JamfAgent', 'Jamf', 'Jamf Pro'),
    ('jamf', 'Jamf', 'Jamf Pro'),
    ('Kandji', 'Kandji', 'Kandji'),
    ('Addigy', 'Addigy', 'Addigy'),
    ('Mosyle', 'Mosyle', 'Mosyle'),
]
#: Antivirus grand public macOS reconnus par nom de process/app — sert à
#: remplacer la valeur de base `XProtect (intégré macOS)` quand un produit
#: tiers est réellement présent.
_AGENTS_AV_MAC = [
    ('Malwarebytes', 'Malwarebytes', 'Malwarebytes'),
    ('Sophos', 'Sophos', 'Sophos Endpoint'),
    ('Norton', 'Gen Digital', 'Norton'),
    ('avast', 'Gen Digital', 'Avast'),
    ('bitdefender', 'Bitdefender', 'Bitdefender'),
]
#: Supplément EDR macOS : contrairement au `DisplayName` d'un service
#: Windows, le nom de *process* d'un agent EDR sur macOS ne contient
#: généralement pas le nom commercial (SentinelOne tourne sous
#: `SentinelAgent`, CrowdStrike Falcon sous `falconctl`/`falcond`) — ces
#: motifs ciblent le process directement plutôt que la marque, en
#: complément de `_AGENTS_EDR` (qui, lui, matche surtout via le nom du
#: bundle applicatif installé, cherché séparément ci-dessous).
_AGENTS_EDR_MAC = [
    ('SentinelAgent', 'SentinelOne', 'SentinelOne Singularity'),
    ('falconctl', 'CrowdStrike', 'CrowdStrike Falcon'),
    ('falcond', 'CrowdStrike', 'CrowdStrike Falcon'),
    ('cbdefense', 'VMware Carbon Black', 'VMware Carbon Black Cloud'),
]


def _mac_managed_agents():
    """Agents de télémaintenance, RMM, EDR et antivirus tiers détectés —
    pendant macOS de `_win_managed_agents()`, réutilise le même
    `chercher_agents()` pur.

    Deux sources combinées, chacune avec sa limite propre :
    - process en cours (`ps -axo comm=`) : le nom y est fiable pour
      l'état 'actif', mais c'est le nom du binaire, pas forcément le nom
      commercial (voir `_AGENTS_EDR_MAC` ci-dessus) ;
    - applications installées (`/Applications/*.app`) : le nom porte
      généralement la marque (utile pour matcher les catalogues pensés
      pour les `DisplayName` Windows), mais rien ne dit que l'agent tourne
      réellement — ces entrées sont marquées `actif=False` plutôt qu'une
      valeur inventée.

    Recherche par sous-chaîne dans les deux cas, comme côté Windows : au
    mieux, jamais une preuve.
    """
    info = {}
    services, vus = [], set()

    sortie = _run(['ps', '-axo', 'comm='], timeout=15)
    for ligne in sortie.splitlines():
        nom = os.path.basename(ligne.strip())
        if nom and nom not in vus:
            vus.add(nom)
            services.append({'DisplayName': nom, 'State': 'running'})

    try:
        result = subprocess.run(['ls', '/Applications'], capture_output=True, text=True, timeout=10)
        for app in result.stdout.splitlines():
            if app.endswith('.app'):
                nom = app[:-4]
                if nom and nom not in vus:
                    vus.add(nom)
                    services.append({'DisplayName': nom, 'State': 'installed'})
    except Exception:
        pass

    if not services:
        return info

    rmm = chercher_agents(services, _AGENTS_RMM + _AGENTS_RMM_MAC)
    if rmm:
        info['remote_support_agents'] = rmm
    edr = chercher_agents(services, _AGENTS_EDR + _AGENTS_EDR_MAC)
    if edr:
        info['edr_agents'] = edr
    av = chercher_agents(services, _AGENTS_AV_MAC)
    if av:
        info['antivirus'] = av[0]['nom']

    # Identifiant AnyDesk : lu dans son fichier de configuration, comme
    # anydesk.exe --get-id sur Windows lit l'ID via l'exécutable. Pas de
    # commande --get-id équivalente documentée sur macOS.
    try:
        conf = os.path.expanduser('~/Library/Application Support/AnyDesk/system.conf')
        if os.path.isfile(conf):
            with open(conf, 'r', encoding='utf-8', errors='replace') as f:
                for ligne in f:
                    if ligne.startswith('ad.anynet.id='):
                        valeur = ligne.split('=', 1)[1].strip()
                        if _RE_ANYDESK_ID.match(valeur):
                            info['anydesk_id'] = valeur.replace(' ', '')
                        break
    except Exception:
        pass

    return info


def _win_access_policy():
    """Politique de mot de passe local, accès Bureau à distance, identifiants
    Windows enregistrés pour une session distante.

    La politique de mot de passe se lit par `secedit /export`, dont les clés
    (MinimumPasswordLength, etc.) restent en anglais quelle que soit la langue
    de Windows — contrairement à `net accounts`, dont la sortie est localisée
    et aurait rendu l'analyse fragile sur un Windows francophone. Le groupe
    Bureau à distance est résolu par son SID S-1-5-32-555, pour la même raison
    qui a déjà servi pour le groupe Administrateurs : son nom change avec la
    langue. Les identifiants enregistrés ne sont comptés que pour les cibles
    « TERMSRV/… » — cette chaîne-là n'est, elle, jamais traduite.
    """
    info = {}
    data = _win_powershell_json(
        "$pol=@{}; try { "
        "$tmp=[System.IO.Path]::GetTempFileName(); "
        "secedit /export /cfg $tmp /areas SECURITYPOLICY | Out-Null; "
        "$c = Get-Content $tmp -EA SilentlyContinue; "
        "Remove-Item $tmp -EA SilentlyContinue; "
        "foreach ($l in $c) { if ($l -match "
        "'^(MinimumPasswordLength|PasswordComplexity|PasswordHistorySize|"
        "MaximumPasswordAge|MinimumPasswordAge|LockoutBadCount|LockoutDuration|"
        "ResetLockoutCount)\\s*=\\s*(.+)$') { $pol[$matches[1]] = $matches[2].Trim() } } "
        "} catch {}; "
        "$rdp=@(); try { "
        "$grp=(Get-LocalGroup -EA SilentlyContinue | Where-Object { $_.SID.Value -eq 'S-1-5-32-555' }).Name; "
        "if ($grp) { $rdp=@(Get-LocalGroupMember -Group $grp -EA SilentlyContinue "
        "| Select-Object -ExpandProperty Name) } } catch {}; "
        "$creds=@(); try { "
        "$out = cmdkey /list 2>$null; "
        "foreach ($l in $out) { if ($l -match 'TERMSRV[/\\\\]([^\\s]+)') { $creds += $matches[1] } } "
        "} catch {}; "
        "[PSCustomObject]@{ Pol=$pol; Rdp=$rdp; Creds=$creds } | ConvertTo-Json -Compress -Depth 3",
        timeout=40,
    )
    if not data:
        return info

    pol = data.get('Pol') or {}

    def entier(cle):
        try:
            return int(str(pol.get(cle)).strip())
        except (TypeError, ValueError):
            return None

    politique = {}
    lg = entier('MinimumPasswordLength')
    if lg is not None:
        politique['min_length'] = lg
    hist = entier('PasswordHistorySize')
    if hist is not None:
        politique['history'] = hist
    if str(pol.get('PasswordComplexity', '')).strip() in ('0', '1'):
        politique['complexity'] = pol['PasswordComplexity'].strip() == '1'
    age_max = entier('MaximumPasswordAge')
    if age_max is not None:
        politique['max_age_days'] = age_max
    seuil = entier('LockoutBadCount')
    if seuil is not None:
        politique['lockout_threshold'] = seuil
    duree = entier('LockoutDuration')
    if duree is not None:
        politique['lockout_duration_min'] = duree
    if politique:
        info['local_password_policy'] = politique

    rdp_users = [_clean(u).split('\\')[-1] for u in _as_list(data.get('Rdp')) if _clean(u)]
    if rdp_users:
        info['rdp_allowed_users'] = sorted(set(rdp_users), key=str.lower)

    creds = sorted(set(_clean(c) for c in _as_list(data.get('Creds')) if _clean(c)))
    if creds:
        info['saved_rdp_credentials'] = creds

    return info


#: Table des versions .NET Framework 4.x, par numéro de build (`Release`) —
#: celui-ci identifie la version installée de façon plus fiable que le
#: numéro affiché, lui-même parfois trompeur entre mises à jour mineures.
#: Table publiée par Microsoft ; on retient le plus grand seuil atteint.
_DOTNET_RELEASES = sorted([
    (533320, '4.8.1'), (528040, '4.8'), (461808, '4.7.2'), (461308, '4.7.1'),
    (460798, '4.7'), (394802, '4.6.2'), (394254, '4.6.1'), (393295, '4.6'),
    (379893, '4.5.2'), (378675, '4.5.1'), (378389, '4.5'),
], reverse=True)


def _dotnet_version_from_release(release):
    for seuil, version in _DOTNET_RELEASES:
        if release >= seuil:
            return version
    return None


def _win_maintenance():
    """Plan d'alimentation, démarrage rapide, dernière analyse antivirus
    complète, versions du framework .NET installées.

    Le framework .NET 4.x s'identifie par le numéro de build de la clé
    `Release`, pas par le numéro de version affiché (qui ne distingue pas
    toujours deux mises à jour mineures) ; la table de correspondance est
    celle publiée par Microsoft.
    """
    info = {}
    data = _win_powershell_json(
        "$plan=$null; try { $o = powercfg /getactivescheme 2>$null; "
        "if ($o -match '\\(([^)]+)\\)') { $plan = $matches[1] } } catch {}; "
        "$fast=$null; try { $fast = (Get-ItemProperty "
        "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power' "
        "-EA SilentlyContinue).HiberbootEnabled } catch {}; "
        "$def=$null; try { $def = Get-MpComputerStatus -EA SilentlyContinue "
        "| Select-Object AntivirusEnabled,"
        # Converties en texte ici : un DateTime imbriqué dans un objet
        # personnalisé ressort de ConvertTo-Json comme une structure
        # {value=/Date(...)/; DateTime=...} plutôt qu'une chaîne simple.
        "@{N='Full';E={ if ($_.FullScanEndTime) { $_.FullScanEndTime.ToString('yyyy-MM-dd HH:mm') } }},"
        "@{N='Quick';E={ if ($_.QuickScanEndTime) { $_.QuickScanEndTime.ToString('yyyy-MM-dd HH:mm') } }} "
        "} catch {}; "
        "$rel=$null; try { $rel = (Get-ItemProperty "
        "'HKLM:\\SOFTWARE\\Microsoft\\NET Framework Setup\\NDP\\v4\\Full' "
        "-EA SilentlyContinue).Release } catch {}; "
        "$v35=$false; try { $v35 = [bool](Get-ItemProperty "
        "'HKLM:\\SOFTWARE\\Microsoft\\NET Framework Setup\\NDP\\v3.5' "
        "-EA SilentlyContinue).Install } catch {}; "
        "$core=@(); try { $dn = (Get-Command dotnet.exe -EA SilentlyContinue).Source; "
        "if ($dn) { $core = @(& $dn --list-runtimes 2>$null) } } catch {}; "
        "[PSCustomObject]@{ Plan=$plan; Fast=$fast; Defender=$def; "
        "Release=$rel; V35=$v35; Core=$core } | ConvertTo-Json -Compress -Depth 3",
        timeout=30,
    )
    if not data:
        return info

    if data.get('Plan'):
        info['power_plan'] = _clean(data['Plan'])
    if data.get('Fast') is not None:
        info['fast_startup'] = bool(data.get('Fast'))

    defender = data.get('Defender') or {}
    if defender.get('Full'):
        info['defender_last_full_scan'] = _clean(defender['Full'])
    if defender.get('Quick'):
        info['defender_last_quick_scan'] = _clean(defender['Quick'])

    dotnet = []
    if data.get('V35'):
        dotnet.append('.NET Framework 3.5')
    try:
        release = int(data.get('Release') or 0)
    except (TypeError, ValueError):
        release = 0
    if release:
        version = _dotnet_version_from_release(release)
        dotnet.append('.NET Framework %s' % (version or ('(build %d)' % release)))
    for ligne in _as_list(data.get('Core')):
        ligne = _clean(ligne)
        if ligne:
            dotnet.append(ligne)
    if dotnet:
        info['dotnet_versions'] = dotnet

    return info


def _win_boot_disk():
    """Style de partition (GPT/MBR) et mode de démarrage (UEFI/Legacy).

    Le mode de démarrage se lisait d'abord via `bcdedit /enum` — qui s'est
    révélé exiger les droits administrateur pour la simple lecture, y compris
    sur un magasin de démarrage sans rien d'inhabituel (constaté, pas supposé).
    `Get-ComputerInfo -Property BiosFirmwareType` donne la même réponse sans
    élévation.
    """
    info = {}
    data = _win_powershell_json(
        "$disks=@(); try { $disks = @(Get-Disk -EA SilentlyContinue "
        "| Select-Object Number,PartitionStyle,@{N='Boot';E={[bool]$_.IsBoot}}) } catch {}; "
        "$firmware=$null; try { $firmware = (Get-ComputerInfo -Property BiosFirmwareType "
        "-EA SilentlyContinue).BiosFirmwareType } catch {}; "
        "[PSCustomObject]@{ Disks=$disks; Firmware=[string]$firmware } "
        "| ConvertTo-Json -Compress -Depth 3",
        timeout=25,
    )
    if not data:
        return info

    disques = []
    for d in _as_list(data.get('Disks')):
        style = _clean(d.get('PartitionStyle'))
        if style and style.upper() != 'RAW':
            disques.append({'number': d.get('Number'), 'style': style, 'boot': bool(d.get('Boot'))})
    if disques:
        info['disk_partition_styles'] = disques
        boot_style = next((d['style'] for d in disques if d['boot']), None)
        if boot_style:
            info['boot_disk_style'] = boot_style

    firmware = _clean(data.get('Firmware'))
    if firmware:
        info['boot_mode'] = 'UEFI' if firmware.lower() == 'uefi' else 'Legacy (BIOS)'

    return info


def _win_rdp_logon_history(jours=EVENT_WINDOW_DAYS, limite=25):
    """Connexions Bureau à distance entrantes récentes (qui, depuis où, quand).

    Complète le journal de sécurité (échecs, verrouillages) par les
    connexions qui ont, elles, réussi — utile pour confirmer qu'un accès
    distant a bien eu lieu, ou en repérer un qui ne devrait pas.
    """
    data = _win_powershell_json(
        "$since=(Get-Date).AddDays(-%d).ToUniversalTime().ToString('o'); "
        "$xpath=\"*[System[(EventID=4624) and TimeCreated[@SystemTime&gt;='$since']] "
        "and EventData[Data[@Name='LogonType']='10']]\"; "
        "$e=@(); try { $e=@(Get-WinEvent -LogName Security -FilterXPath $xpath "
        "-MaxEvents %d -EA SilentlyContinue) } catch {}; "
        "$e | ForEach-Object { $x=[xml]$_.ToXml(); "
        "$d=$x.Event.EventData.Data; "
        "$u=($d | Where-Object {$_.Name -eq 'TargetUserName'}).'#text'; "
        "$ip=($d | Where-Object {$_.Name -eq 'IpAddress'}).'#text'; "
        "[PSCustomObject]@{ User=$u; Ip=$ip; When=$_.TimeCreated.ToString('yyyy-MM-dd HH:mm') } } "
        "| ConvertTo-Json -Compress -Depth 3" % (jours, limite),
        timeout=60,
    )
    connexions = []
    for e in _as_list(data):
        utilisateur = _clean(e.get('User'))
        if not utilisateur or utilisateur.endswith('$'):
            continue
        connexions.append({
            'user': utilisateur,
            'ip': _clean(e.get('Ip')) or None,
            'when': _clean(e.get('When')),
        })
    return {'rdp_logon_history': connexions} if connexions else {}


# Cible publique par défaut pour la mesure Internet. Un simple ping : aucune
# donnée n'est transmise, et la cible est modifiable.
INTERNET_PROBE = '1.1.1.1'
# Point de mesure de débit — sollicité uniquement sur demande explicite.
SPEEDTEST_URL = 'https://speed.cloudflare.com/__down?bytes=10000000'


def _ping_stats(cible, essais=4, timeout=6):
    """Latence moyenne et perte de paquets vers une cible.

    Utilise Test-Connection, dont la sortie est structurée, plutôt que d'analyser
    le texte localisé de `ping`.
    """
    if not cible:
        return None
    data = _win_powershell_json(
        "try { $r=@(Test-Connection -ComputerName '%s' -Count %d -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty ResponseTime); "
        "[PSCustomObject]@{ Sent=%d; Received=$r.Count; "
        "Avg=$(if($r.Count){[math]::Round(($r | Measure-Object -Average).Average,1)}else{$null}); "
        "Max=$(if($r.Count){($r | Measure-Object -Maximum).Maximum}else{$null}) } "
        "| ConvertTo-Json -Compress } catch {}" % (cible, essais, essais),
        timeout=timeout + essais * 2,
    )
    if not data:
        return {'target': cible, 'sent': essais, 'received': 0,
                'loss_pct': 100, 'avg_ms': None, 'max_ms': None}
    recus = data.get('Received') or 0
    return {
        'target': cible,
        'sent': essais,
        'received': recus,
        'loss_pct': round((essais - recus) / essais * 100) if essais else 0,
        'avg_ms': data.get('Avg'),
        'max_ms': data.get('Max'),
    }


def measure_network(info, test_debit=False, url_debit=None):
    """Latence vers la passerelle, le DNS et Internet ; débit si demandé.

    La latence distingue ce que le débit seul ne montre pas : un poste à
    500 Mb/s avec 200 ms vers sa propre passerelle a un problème de lien local,
    pas de connexion Internet. Le test de débit reste facultatif — il consomme
    de la bande passante sur un poste de production et sollicite un tiers.
    """
    if not IS_WINDOWS:
        return {}

    mesures = []
    passerelle = info.get('default_gateway')
    if passerelle:
        stat = _ping_stats(passerelle)
        if stat:
            stat['role'] = 'Passerelle'
            mesures.append(stat)

    serveurs = info.get('dns_servers') or []
    premier_dns = serveurs[0]['servers'][0] if serveurs and serveurs[0].get('servers') else None
    if premier_dns and premier_dns != passerelle:
        stat = _ping_stats(premier_dns)
        if stat:
            stat['role'] = 'Serveur DNS'
            mesures.append(stat)

    stat = _ping_stats(INTERNET_PROBE)
    if stat:
        stat['role'] = 'Internet'
        mesures.append(stat)

    resultat = {'latency': mesures} if mesures else {}

    if test_debit:
        debit = _mesurer_debit(url_debit or SPEEDTEST_URL)
        if debit:
            resultat['bandwidth'] = debit

    return resultat


def _mesurer_debit(url):
    """Débit descendant approximatif, mesuré sur un téléchargement unique."""
    import time as _time
    try:
        debut = _time.time()
        octets = 0
        requete = Request(url, headers={'User-Agent': 'ParcInfo-Collector'})
        with urlopen(requete, timeout=30) as reponse:
            while True:
                bloc = reponse.read(65536)
                if not bloc:
                    break
                octets += len(bloc)
                # Garde-fou : ni plus de 15 s, ni plus de 50 Mo téléchargés
                if _time.time() - debut > 15 or octets > 50 * 1024 * 1024:
                    break
        duree = max(_time.time() - debut, 0.001)
        if octets < 100 * 1024:
            return None
        return {
            'downloaded_mb': round(octets / (1024 * 1024), 1),
            'seconds': round(duree, 1),
            'mbps': round(octets * 8 / duree / 1_000_000, 1),
            'source': url,
        }
    except Exception:
        return None


# Service consulté pour l'IP publique + opérateur. Un seul fournisseur,
# best-effort, comme SPEEDTEST_URL ci-dessus — pas de repli si indisponible :
# une panne ou un réseau filtrant ne doit jamais faire échouer la collecte.
PUBLIC_IP_API_URL = 'https://ipapi.co/json/'


def get_public_ip_info():
    """IP publique et opérateur (FAI) constatés depuis CE poste, à l'instant
    de la collecte.

    Distinct de parc_general.ip_publique (un par SITE, saisi à la main) :
    ce champ-ci vient de chaque appareil individuellement, ce qui a un sens
    pour un poste itinérant (laptop) dont l'IP publique change selon le lieu
    de connexion. Cross-plateforme (un simple appel HTTPS), donc pas rattaché
    à measure_network() ci-dessus, qui est réservée à Windows (mesures de
    latence via `ping`).
    """
    try:
        requete = Request(PUBLIC_IP_API_URL, headers={'User-Agent': 'ParcInfo-Collector'})
        with urlopen(requete, timeout=6) as reponse:
            data = json.loads(reponse.read().decode('utf-8', errors='replace'))
        if not isinstance(data, dict) or data.get('error'):
            return {}
        resultat = {}
        ip = _clean(data.get('ip'))
        if ip:
            resultat['public_ip'] = ip
        # 'org' renvoie typiquement le nom du FAI/opérateur (ex: "Orange S.A.",
        # "Free SAS") ou, pour un hébergeur, celui-ci — pas toujours un nom
        # d'opérateur grand public au sens strict, mais la meilleure
        # approximation disponible sans service payant.
        operateur = _clean(data.get('org'))
        if operateur:
            resultat['public_ip_isp'] = operateur
        return resultat
    except Exception:
        return {}


# ── Vérification DNS (dnscheck.tools), désactivée par défaut ─────────────────
# Service pensé pour un navigateur (requêtes DNS spéciales interprétées côté
# client) : pas d'API JSON documentée. dnscheck.tools/help ne documente en
# clair que la requête de base — `dig txt test.dnscheck.tools` — les variantes
# ECS/DNSSEC ne sont pas assez précisément décrites pour en fabriquer un
# verdict OK/KO fiable ici. Le résultat brut est donc affiché tel quel, à
# charge du technicien de l'interpréter — mieux vaut ça qu'un faux "validé"
# sur un point de sécurité DNS.
DNS_CHECK_HOSTNAME = 'test.dnscheck.tools'


def _dns_encoder_nom(nom):
    """Encode un nom de domaine au format DNS (labels préfixés par leur
    longueur, terminés par un octet nul)."""
    encodage = b''
    for label in nom.strip('.').split('.'):
        brut = label.encode('ascii', errors='ignore')[:63]
        encodage += bytes([len(brut)]) + brut
    return encodage + b'\x00'


def _dns_construire_requete_txt(nom):
    """Construit une requête DNS minimale (question unique, type TXT)."""
    txn_id = struct.unpack('>H', os.urandom(2))[0]
    flags = 0x0100  # requête standard, récursion demandée
    entete = struct.pack('>HHHHHH', txn_id, flags, 1, 0, 0, 0)
    question = _dns_encoder_nom(nom) + struct.pack('>HH', 16, 1)  # TXT, IN
    return entete + question, txn_id


def _dns_sauter_nom(data, offset):
    """Avance `offset` au-delà d'un nom DNS, compression (pointeur 0xC0)
    comprise. Ne résout pas le nom pointé : sert seulement à retrouver les
    octets qui suivent (type/classe/TTL/longueur/données)."""
    while offset < len(data):
        longueur = data[offset]
        if longueur == 0:
            return offset + 1
        if (longueur & 0xC0) == 0xC0:
            return offset + 2
        offset += 1 + longueur
    return offset


def _dns_parser_txt(data, txn_id_attendu):
    """Extrait les chaînes TXT d'une réponse DNS brute. Best-effort : une
    réponse tronquée ou inattendue renvoie simplement une liste vide plutôt
    que de lever une exception."""
    if len(data) < 12:
        return []
    id_recu, flags, qdcount, ancount = struct.unpack('>HHHH', data[:8])
    if id_recu != txn_id_attendu or not (flags & 0x8000):  # bit QR : réponse
        return []
    offset = 12
    for _ in range(qdcount):
        offset = _dns_sauter_nom(data, offset) + 4
    resultats = []
    for _ in range(ancount):
        offset = _dns_sauter_nom(data, offset)
        if offset + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlength = struct.unpack('>HHIH', data[offset:offset + 10])
        offset += 10
        rdata = data[offset:offset + rdlength]
        if rtype == 16:  # TXT
            chaines, pos = [], 0
            while pos < len(rdata):
                l = rdata[pos]
                pos += 1
                chaines.append(rdata[pos:pos + l].decode('utf-8', errors='replace'))
                pos += l
            resultats.append(''.join(chaines))
        offset += rdlength
    return resultats


def _resolveur_dns_systeme(info):
    """Premier serveur DNS configuré sur ce poste — c'est LUI que
    dnscheck.tools observerait depuis un navigateur, pas un résolveur public
    arbitraire. Déjà collecté sur Windows (_win_network → dns_servers) ;
    /etc/resolv.conf reste la seule source universelle sans dépendance
    supplémentaire sur macOS/Linux."""
    for entree in (info.get('dns_servers') or []):
        serveurs = entree.get('servers') if isinstance(entree, dict) else None
        if serveurs:
            return serveurs[0]
    if not IS_WINDOWS:
        try:
            with open('/etc/resolv.conf', 'r', encoding='utf-8', errors='ignore') as f:
                for ligne in f:
                    ligne = ligne.strip()
                    if ligne.startswith('nameserver'):
                        parties = ligne.split()
                        if len(parties) >= 2:
                            return parties[1]
        except OSError:
            pass
    return None


def get_dns_check_info(info):
    """Requête DNS brute vers test.dnscheck.tools, via le résolveur configuré
    sur ce poste. Option désactivée par défaut (--dns-check / case à cocher) :
    sollicite un service tiers, comme --test-debit."""
    serveur = _resolveur_dns_systeme(info)
    if not serveur:
        return {}
    try:
        requete, txn_id = _dns_construire_requete_txt(DNS_CHECK_HOSTNAME)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(5)
            s.sendto(requete, (serveur, 53))
            data, _adresse = s.recvfrom(4096)
        chaines = _dns_parser_txt(data, txn_id)
        if not chaines:
            return {}
        return {
            'dns_check_resolveur': serveur,
            'dns_check_reponse': ' / '.join(chaines),
        }
    except Exception:
        return {}


# ── Infos de la box internet (UPnP/IGD), désactivée par défaut ───────────────
# Best-effort : beaucoup de box grand public désactivent UPnP, ou ne
# répondent pas dans le délai imparti — un échec à n'importe quelle étape
# rend simplement {}, jamais d'exception qui remonterait à la collecte.
_UPNP_MULTICAST_ADDR = '239.255.255.250'
_UPNP_MULTICAST_PORT = 1900
_UPNP_DEVICE_NS = 'urn:schemas-upnp-org:device-1-0'


def _upnp_decouvrir_passerelle(timeout=3):
    """Découverte SSDP : renvoie l'URL de description XML de la passerelle
    Internet (IGD), ou None si rien ne répond."""
    import time as _time
    message = (
        'M-SEARCH * HTTP/1.1\r\n'
        'HOST: 239.255.255.250:1900\r\n'
        'MAN: "ssdp:discover"\r\n'
        'MX: 2\r\n'
        'ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n'
        '\r\n'
    ).encode('utf-8')
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(message, (_UPNP_MULTICAST_ADDR, _UPNP_MULTICAST_PORT))
            fin = _time.time() + timeout
            while _time.time() < fin:
                try:
                    data, _adresse = s.recvfrom(4096)
                except socket.timeout:
                    break
                texte = data.decode('utf-8', errors='ignore')
                for ligne in texte.split('\r\n'):
                    if ligne.upper().startswith('LOCATION:'):
                        return ligne.split(':', 1)[1].strip()
    except Exception:
        pass
    return None


def _upnp_description(location_url):
    """Description XML de la passerelle : fabricant/modèle, et l'URL de
    contrôle du service WAN (IP externe) si présent."""
    try:
        requete = Request(location_url, headers={'User-Agent': 'ParcInfo-Collector'})
        with urlopen(requete, timeout=5) as reponse:
            xml_brut = reponse.read()
        racine = ET.fromstring(xml_brut)
    except Exception:
        return {}

    ns = {'u': _UPNP_DEVICE_NS}

    def texte(chemin):
        el = racine.find('.//u:' + chemin, ns)
        return el.text.strip() if el is not None and el.text else None

    resultat = {
        'manufacturer': texte('manufacturer'),
        'model_name': texte('modelName'),
        'friendly_name': texte('friendlyName'),
    }

    # Service WAN (IP externe) : cherché parmi tous les services décrits, sous
    # n'importe quel device imbriqué (WANDevice > WANConnectionDevice).
    for service in racine.iter('{%s}service' % _UPNP_DEVICE_NS):
        type_service = service.find('u:serviceType', ns)
        if type_service is not None and type_service.text and (
                'WANIPConnection' in type_service.text or 'WANPPPConnection' in type_service.text):
            control_url = service.find('u:controlURL', ns)
            if control_url is not None and control_url.text:
                resultat['_service_type'] = type_service.text.strip()
                resultat['_control_url'] = urljoin(location_url, control_url.text.strip())
            break

    return {k: v for k, v in resultat.items() if v}


def _upnp_ip_externe(control_url, service_type):
    """Appel SOAP GetExternalIPAddress sur le service WAN identifié."""
    corps = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body><u:GetExternalIPAddress xmlns:u="%s"/></s:Body></s:Envelope>'
    ) % service_type
    entetes = {
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPAction': '"%s#GetExternalIPAddress"' % service_type,
        'User-Agent': 'ParcInfo-Collector',
    }
    try:
        requete = Request(control_url, data=corps.encode('utf-8'), headers=entetes, method='POST')
        with urlopen(requete, timeout=5) as reponse:
            xml_brut = reponse.read()
        racine = ET.fromstring(xml_brut)
        for el in racine.iter():
            if el.tag.endswith('NewExternalIPAddress') and el.text:
                return el.text.strip()
    except Exception:
        pass
    return None


def get_router_info():
    """Infos de la box internet via UPnP (IGD) : fabricant, modèle, IP WAN.
    Option désactivée par défaut (--router-info / case à cocher) : sonde le
    réseau local, mieux vaut un choix explicite qu'un comportement
    systématique."""
    location = _upnp_decouvrir_passerelle()
    if not location:
        return {}
    description = _upnp_description(location)
    if not description:
        return {}
    resultat = {}
    if description.get('manufacturer'):
        resultat['router_manufacturer'] = description['manufacturer']
    if description.get('model_name'):
        resultat['router_model'] = description['model_name']
    if description.get('friendly_name'):
        resultat['router_name'] = description['friendly_name']
    control_url = description.get('_control_url')
    service_type = description.get('_service_type')
    if control_url and service_type:
        ip_externe = _upnp_ip_externe(control_url, service_type)
        if ip_externe:
            resultat['router_wan_ip'] = ip_externe
    return resultat


def _publier(on_data, info):
    """Livre l'état partiel de la collecte, sans jamais l'interrompre.

    Comme `_report`, ce rappel vient de l'interface : une erreur d'affichage ne
    doit pas faire échouer une collecte d'une minute.
    """
    if not on_data:
        return
    try:
        on_data(dict(info))
    except Exception:
        pass


def get_system_info_windows(progress=None, on_data=None):
    """Collecte Windows complète (ctypes/winreg/PowerShell, sans dépendance externe).

    `progress` est appelé avec (fraction, libellé) avant chaque étape.
    `on_data` reçoit l'état partiel après chaque étape, ce qui permet à
    l'interface d'afficher les données au fur et à mesure plutôt que de laisser
    l'utilisateur devant un écran vide une minute durant.
    """
    info = {}
    total = len(_WIN_STEPS)
    for index, (libelle, collector) in enumerate(_WIN_STEPS):
        _report(progress, 0.10 + 0.70 * index / total, libelle)
        try:
            info.update(collector() or {})
        except Exception:
            # Un bloc en échec ne doit jamais empêcher les autres de remonter
            pass
        _publier(on_data, info)
    return info


# ════════════════════════════════════════════════════════════════════════════
# COLLECTE macOS
# ════════════════════════════════════════════════════════════════════════════

def _mac_profiler_json(datatype, timeout=25):
    """Interroge system_profiler en JSON (plus fiable que le parsing texte)."""
    out = _run(['system_profiler', '-json', datatype], timeout=timeout)
    if not out.strip():
        return None
    try:
        return json.loads(out).get(datatype)
    except Exception:
        return None


def _mac_printers():
    """Imprimantes CUPS — pendant macOS de `_win_inventory()` côté imprimantes.

    `lpstat` ne donne ni pilote ni partage de façon fiable (contrairement à
    `Win32_Printer` sur Windows) : ces deux champs restent vides/`False`,
    comme pour l'inventaire Linux existant (`get_system_info_linux`).
    """
    info = {}
    liste = _run(['lpstat', '-p'], timeout=10)
    if not liste.strip():
        return info

    default = ''
    m = re.search(r'system default destination:\s*(\S+)', _run(['lpstat', '-d'], timeout=10))
    if m:
        default = m.group(1)

    printers = []
    for ligne in liste.splitlines():
        m = re.match(r'^printer\s+(\S+)\s', ligne)
        if not m:
            continue
        name = m.group(1)
        port = ''
        info_uri = _run(['lpstat', '-v', name], timeout=10)
        m_uri = re.search(r'device for %s:\s*(\S+)' % re.escape(name), info_uri)
        if m_uri:
            port = m_uri.group(1)
        network = bool(re.match(r'^(ipp|ipps|https?|dnssd|socket|lpd|smb)://', port, re.IGNORECASE))
        printers.append({
            'name': name,
            'driver': '',
            'port': port,
            'network': network,
            'default': name == default,
            'shared': False,
            'virtual': is_virtual_printer(name, '', port),
            'connection': printer_connection(port, network),
        })
    if printers:
        info['printers'] = printers
    return info


def _mac_listening_ports():
    """Ports TCP en écoute via `lsof` — pendant macOS de `listening_ports`
    (Windows : `Get-NetTCPConnection` ; Linux : `ss`)."""
    info = {}
    out = _run(['lsof', '-iTCP', '-sTCP:LISTEN', '-n', '-P'], timeout=15)
    lignes = out.splitlines()
    if len(lignes) < 2:
        return info

    ports = {}
    for ligne in lignes[1:]:
        colonnes = ligne.split()
        # COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME — NAME (ex.
        # "*:445" ou "127.0.0.1:5000") est le 9ᵉ jeton.
        if len(colonnes) < 9:
            continue
        process, adresse = colonnes[0], colonnes[8]
        if ':' not in adresse:
            continue
        try:
            port = int(adresse.rsplit(':', 1)[1])
        except ValueError:
            continue
        ports.setdefault(port, process)
    if ports:
        info['listening_ports'] = [{'port': p, 'process': proc} for p, proc in sorted(ports.items())]
    return info


def _mac_pending_updates():
    """Mises à jour macOS/Apple disponibles via `softwareupdate -l` —
    pendant macOS de `pending_updates` (Windows : Microsoft Update).

    Distinct des mises à jour Homebrew par logiciel (`update_status`,
    `_brew_outdated()`) : ceci couvre le système et les apps Apple, jamais
    couvertes par `brew`. `softwareupdate -l` ne distingue pas de façon
    fiable une mise à jour de sécurité d'une mise à jour de confort —
    `security` reste toujours `False` plutôt qu'un verdict inventé (même
    principe que `dns_check_reponse`, jamais interprété côté collecteur).
    Peut prendre du temps (interroge les serveurs Apple) : timeout généreux.
    """
    info = {}
    out = _run(['softwareupdate', '-l'], timeout=90)
    if 'Title:' not in out:
        return info

    attente = []
    for ligne in out.splitlines():
        if 'Title:' not in ligne:
            continue
        m = re.search(
            r'Title:\s*([^,]+),\s*Version:\s*([^,]+),\s*Size:\s*([\d.]+)\s*([KMG]?)i?B?',
            ligne)
        if not m:
            continue
        titre, version, taille_brute, unite = m.groups()
        try:
            taille = float(taille_brute)
        except ValueError:
            taille = 0
        taille_mo = {'G': taille * 1024, 'M': taille}.get((unite or 'K').upper(), taille / 1024)
        attente.append({
            'title': ('%s %s' % (titre.strip(), version.strip())).strip(),
            'severity': 'Recommandée' if 'recommended: yes' in ligne.lower() else '',
            'kb': '',
            'size_mb': round(taille_mo) if taille_mo else None,
            'security': False,
        })
    if attente:
        info['pending_updates'] = attente
        info['pending_updates_source'] = 'softwareupdate'
    return info


#: Marques d'écran reconnues en préfixe du nom EDID (`_name` de
#: SPDisplaysDataType, ex. « DELL U2720Q ») — jamais depuis l'ID vendeur EDID
#: brut : son encodage exact (chaîne déjà résolue vs identifiant à décoder
#: selon la version macOS) est trop incertain sans matériel réel pour
#: vérifier, et une marque devinée à tort serait pire qu'un champ vide.
_MONITOR_BRANDS = (
    'Apple', 'Dell', 'LG', 'Samsung', 'BenQ', 'Acer', 'ASUS', 'HP',
    'Lenovo', 'Philips', 'ViewSonic', 'AOC', 'Sony', 'Sceptre', 'MSI',
    'Gigabyte', 'Eizo', 'NEC', 'Iiyama', 'Huawei', 'Xiaomi', 'Alienware',
)


def _deviner_marque_ecran(nom):
    """Devine la marque d'un écran depuis son nom EDID (`_name`), qui la
    porte souvent déjà en préfixe. Écran interne : toujours Apple."""
    bas = nom.lower()
    if 'built-in' in bas or 'intégré' in bas or 'liquid retina' in bas:
        return 'Apple'
    premier_mot = nom.split()[0] if nom.split() else ''
    for marque in _MONITOR_BRANDS:
        if premier_mot.lower() == marque.lower():
            return marque
    return ''


def _vram_texte_vers_gb(texte):
    """Convertit une VRAM textuelle `system_profiler` (« 1536 MB », « 8 GB »)
    en Go. Retourne None si non reconnu plutôt qu'une valeur inventée."""
    if not texte:
        return None
    m = re.match(r'^([\d.]+)\s*(MB|GB)$', str(texte).strip(), re.IGNORECASE)
    if not m:
        return None
    valeur = float(m.group(1))
    return round(valeur / 1024, 1) if m.group(2).upper() == 'MB' else round(valeur, 1)


def _mac_network():
    """Passerelle par défaut, DNS (serveurs + suffixes de recherche), proxy
    — pendant macOS de `_win_network()`. Ce sont les réglages qui répondent
    à « il n'a plus Internet » : un DNS injoignable, une passerelle absente
    ou un proxy résiduel produisent tous le même symptôme et ne se
    distinguent qu'ici — même principe que côté Windows.
    """
    info = {}

    # ── Passerelle par défaut ────────────────────────────────────────────
    route = _run(['route', '-n', 'get', 'default'], timeout=10)
    gw, iface = '', ''
    for ligne in route.splitlines():
        ligne = ligne.strip()
        if ligne.startswith('gateway:'):
            gw = ligne.split(':', 1)[1].strip()
        elif ligne.startswith('interface:'):
            iface = ligne.split(':', 1)[1].strip()
    if gw:
        info['default_gateway'] = gw
        info['gateways'] = [{'address': gw, 'interface': iface}]

    # ── DNS : serveurs + suffixes de recherche ──────────────────────────
    # `scutil --dns` liste un ou plusieurs blocs "resolver #N" (VPN et
    # split-DNS en ajoutent, chacun restreint à un domaine particulier) :
    # plutôt que de ne garder QUE le premier (au risque de rater le bon
    # bloc selon la configuration), tous les serveurs/domaines de recherche
    # rencontrés sont repris, dédupliqués en conservant l'ordre —
    # l'interface retenue est celle du tout premier `if_index` rencontré,
    # généralement le résolveur principal.
    dns_out = _run(['scutil', '--dns'], timeout=10)
    serveurs, suffixes = [], []
    vus_serveurs, vus_suffixes = set(), set()
    interface_dns = ''
    for ligne in dns_out.splitlines():
        ligne = ligne.strip()
        m = re.match(r'nameserver\[\d+\]\s*:\s*(\S+)', ligne)
        if m:
            if m.group(1) not in vus_serveurs:
                vus_serveurs.add(m.group(1))
                serveurs.append(m.group(1))
            continue
        m = re.match(r'search domain\[\d+\]\s*:\s*(\S+)', ligne)
        if m:
            if m.group(1) not in vus_suffixes:
                vus_suffixes.add(m.group(1))
                suffixes.append(m.group(1))
            continue
        if not interface_dns:
            m = re.match(r'if_index\s*:\s*\d+\s*\(([^)]+)\)', ligne)
            if m:
                interface_dns = m.group(1)
    if serveurs:
        info['dns_servers'] = [{'interface': interface_dns, 'servers': serveurs}]
    if suffixes:
        info['dns_suffixes'] = suffixes

    # ── Proxy ────────────────────────────────────────────────────────────
    proxy_out = _run(['scutil', '--proxy'], timeout=10)
    champs = {}
    for ligne in proxy_out.splitlines():
        if ':' in ligne:
            cle, _, valeur = ligne.partition(':')
            champs[cle.strip()] = valeur.strip()
    serveur = ''
    if champs.get('HTTPEnable') == '1' and champs.get('HTTPProxy'):
        serveur = '%s:%s' % (champs['HTTPProxy'], champs.get('HTTPPort', '') or '')
    auto_config = (champs.get('ProxyAutoConfigURLString', '')
                   if champs.get('ProxyAutoConfigEnable') == '1' else '')
    if serveur or auto_config:
        info['proxy'] = {
            'enabled': bool(serveur or auto_config),
            'server': serveur,
            'auto_config_url': auto_config,
        }

    return info


def _mac_security_posture():
    """Protection de l'intégrité système (SIP) et Gatekeeper — posture de
    sécurité macOS sans équivalent Windows direct, mais du même ordre
    d'utilité diagnostique que TPM/Secure Boot côté Windows : un technicien
    qui désactive SIP pour installer un kext tiers, ou Gatekeeper pour
    exécuter du logiciel non notarié, laisse une trace ici plutôt que de
    devoir le redécouvrir en console. Rendus comme deux lignes de plus dans
    la table « Sécurité & conformité » déjà existante — pas de nouvelle
    section.
    """
    info = {}
    sip = _run(['csrutil', 'status'], timeout=5)
    if sip.strip():
        info['sip_status'] = 'Activée' if 'enabled' in sip.lower() else 'Désactivée'

    gk = _run(['spctl', '--status'], timeout=5)
    if gk.strip():
        info['gatekeeper_status'] = 'Activé' if 'enabled' in gk.lower() else 'Désactivé'

    return info


def _mac_mdm_status():
    """Inscription MDM (Jamf/Kandji/Mosyle/Apple Business Manager…) —
    pertinent croisé avec `_mac_managed_agents()` : un agent RMM détecté sans
    inscription MDM correspondante, ou l'inverse, vaut la peine d'être
    remarqué sur un parc censé être entièrement géré.
    """
    info = {}
    out = _run(['profiles', 'status', '-type', 'enrollment'], timeout=10)
    if not out.strip():
        return info
    m = re.search(r'MDM enrollment:\s*(.+)', out)
    if m:
        detail = m.group(1).strip()
        info['mdm_enrolled'] = detail.lower().startswith('yes')
        info['mdm_detail'] = detail
    return info


def _mac_time_machine():
    """Sauvegardes Time Machine — pendant macOS des points de restauration
    Windows (`restore_points`, System Restore) : même champ générique
    réutilisé, même question de fond (« ce poste est-il sauvegardé, et
    depuis quand pas »), pertinente pour la gestion de parc au même titre
    que la garantie ou les contrats.
    """
    info = {}
    dest = _run(['tmutil', 'destinationinfo'], timeout=10)
    m_nom = re.search(r'Name\s*:\s*(.+)', dest)
    destination = m_nom.group(1).strip() if m_nom else ''

    dernieres = _run(['tmutil', 'latestbackup'], timeout=15)
    dernieres = dernieres.strip()
    if dernieres and not dernieres.lower().startswith('no backup'):
        # tmutil latestbackup renvoie un chemin du type
        # /Volumes/Backup/Backups.backupdb/MBP/2026-08-20-093000 — la date
        # est le dernier segment, plus lisible que le chemin complet.
        horodatage = os.path.basename(dernieres.rstrip('/'))
        description = ('Sauvegarde Time Machine (%s)' % destination) if destination \
            else 'Sauvegarde Time Machine'
        info['restore_points'] = [{'description': description, 'when': horodatage}]
    return info


def _mac_top_processes(limite=10):
    """Processus les plus gourmands CPU/RAM — pendant macOS de
    `_win_top_processes()`. Contrairement à Windows, `ps` expose déjà un
    `%cpu` instantané normalisé par le noyau (moyenné sur son propre
    intervalle d'échantillonnage interne) : pas besoin du double relevé à
    600 ms que fait la version Windows pour obtenir un chiffre honnête.

    `rss` (Ko) plutôt que `%mem` : le champ `ram_mb` est déjà documenté et
    affiché comme une quantité absolue (voir la version Windows,
    `WorkingSet64`) — y mettre un pourcentage l'aurait rendu silencieusement
    incohérent d'un poste à l'autre (4,2 signifierait 4,2 % sur un Mac et
    4,2 Mo sur un PC).
    """
    info = {}
    out = _run(['ps', '-Ao', 'comm,%cpu,rss', '-r'], timeout=10)
    lignes = [l for l in out.splitlines()[1:] if l.strip()]
    if not lignes:
        return info

    def parser(ligne):
        parts = ligne.split()
        if len(parts) < 3:
            return None
        try:
            cpu, rss_ko = float(parts[-2]), float(parts[-1])
        except ValueError:
            return None
        nom = os.path.basename(' '.join(parts[:-2]))
        return {'name': nom, 'cpu_pct': cpu, 'ram_mb': round(rss_ko / 1024, 1)}

    entrees = [e for e in (parser(l) for l in lignes) if e]
    if entrees:
        info['top_processes_cpu'] = entrees[:limite]
        info['top_processes_ram'] = sorted(
            entrees, key=lambda e: e['ram_mb'], reverse=True)[:limite]
    return info


def _mac_crash_diagnostics(jours=30):
    """Rapports de plantage applicatifs et paniques noyau — pendant macOS
    des incidents système Windows (`system_incidents` : écrans bleus,
    erreurs disque). Comptage par pure lecture de répertoire (mtime), sans
    dépendre du format interne des fichiers .crash/.ips/.panic — robuste aux
    changements de format entre versions macOS, contrairement à un parsing
    de leur contenu.
    """
    info = {}
    limite = datetime.now().timestamp() - jours * 86400
    incidents = []

    def compter(dossier, motifs, categorie, niveau):
        # `motifs` : plusieurs extensions pour un même compteur — .crash et
        # .ips sont tous deux des plantages applicatifs (le premier avant
        # macOS Monterey, le second depuis), jamais les deux en même temps
        # sur un poste donné ; les compter à part aurait affiché deux lignes
        # « Plantage application » redondantes plutôt qu'un total unique.
        chemin = os.path.expanduser(dossier)
        if not os.path.isdir(chemin):
            return
        fichiers = []
        try:
            for nom in os.listdir(chemin):
                if not nom.endswith(motifs):
                    continue
                chemin_complet = os.path.join(chemin, nom)
                try:
                    mtime = os.path.getmtime(chemin_complet)
                except OSError:
                    continue
                if mtime >= limite:
                    fichiers.append((nom, mtime))
        except OSError:
            return
        if fichiers:
            dernier = max(fichiers, key=lambda f: f[1])
            incidents.append({
                'category': categorie,
                'count': len(fichiers),
                'last_seen': datetime.fromtimestamp(dernier[1]).strftime('%Y-%m-%d %H:%M'),
                'message': dernier[0],
                'level': niveau,
            })

    compter('~/Library/Logs/DiagnosticReports', ('.ips', '.crash'), 'Plantage application', 'warn')
    compter('/Library/Logs/DiagnosticReports', ('.panic',), 'Panique noyau', 'danger')

    if incidents:
        info['system_incidents'] = incidents
    return info


def _mac_startup_items():
    """Agents et démons au démarrage — pendant macOS des programmes de
    démarrage Windows (`startup_programs`). Lu directement via `plistlib`
    (bibliothèque standard) plutôt qu'en scrapant `launchctl list` : un
    fichier .plist a un format stable et documenté, alors que le texte de
    `launchctl list` change de colonnes selon la version macOS. Les jobs
    Apple (`/System/Library/Launch*`) sont exclus — même principe que
    l'exclusion du dossier `\\Microsoft\\` côté tâches planifiées Windows :
    plusieurs centaines d'entrées internes au système, aucune valeur de
    diagnostic pour un parc.
    """
    info = {}
    emplacements = [
        (os.path.expanduser('~/Library/LaunchAgents'), 'Utilisateur (LaunchAgent)'),
        ('/Library/LaunchAgents', 'Système (LaunchAgent)'),
        ('/Library/LaunchDaemons', 'Système (LaunchDaemon)'),
    ]
    items = []
    for dossier, emplacement in emplacements:
        if not os.path.isdir(dossier):
            continue
        try:
            noms = os.listdir(dossier)
        except OSError:
            continue
        for nom in noms:
            if not nom.endswith('.plist'):
                continue
            chemin = os.path.join(dossier, nom)
            try:
                with open(chemin, 'rb') as f:
                    plist = plistlib.load(f)
            except Exception:
                continue
            label = plist.get('Label') or nom[:-6]
            commande = plist.get('Program')
            if not commande:
                args = plist.get('ProgramArguments')
                commande = args[0] if isinstance(args, list) and args else ''
            items.append({
                'name': label, 'command': commande or '',
                'location': chemin, 'user': emplacement,
            })
    if items:
        info['startup_programs'] = sorted(items, key=lambda i: i['name'].lower())
    return info


def get_system_info_mac():
    """Collecte les infos système via system_profiler (macOS)."""
    info = {}

    # ── Matériel de base ───────────────────────────────────────────────────
    try:
        result = subprocess.run(['system_profiler', 'SPHardwareDataType'],
                                capture_output=True, text=True, timeout=20)
        for line in result.stdout.split('\n'):
            if 'Model Identifier:' in line:
                info['model'] = line.split(':', 1)[1].strip()
            elif 'Model Name:' in line:
                info['brand'] = 'Apple'
                model_name = line.split(':', 1)[1].strip()
                info.setdefault('model', model_name)
                info['model_name'] = model_name
            elif 'Serial Number' in line:
                info['serial_number'] = line.split(':', 1)[1].strip()
            elif 'Total Number of Cores:' in line:
                try:
                    info['cpu_physical_cores'] = int(line.split(':', 1)[1].strip().split()[0])
                except Exception:
                    pass
            elif 'Processor Cores:' in line:
                try:
                    info['cpu_cores'] = int(line.split(':', 1)[1].strip())
                except Exception:
                    pass
            elif 'Chip:' in line or 'Processor Name:' in line:
                info['cpu'] = line.split(':', 1)[1].strip()
            elif 'Boot ROM Version:' in line or 'System Firmware Version:' in line:
                # Le libellé a changé plusieurs fois selon les générations
                # d'Intel Mac (EFI classique vs firmware T2) — les deux sont
                # tentés, le premier trouvé gagne. Rendu via `bios_version`,
                # champ déjà générique (Windows/Linux), pas de nouveau champ.
                info.setdefault('bios_version', line.split(':', 1)[1].strip())
                info.setdefault('bios_manufacturer', 'Apple')
            elif 'Memory:' in line:
                try:
                    info['ram_gb'] = float(line.split(':', 1)[1].strip().split()[0])
                except Exception:
                    pass
    except Exception:
        pass

    info.setdefault('cpu_cores', os.cpu_count())

    # ── Barrettes mémoire ──────────────────────────────────────────────────
    try:
        mem_data = _mac_profiler_json('SPMemoryDataType') or []
        modules = []
        for bank in mem_data:
            for key, value in bank.items():
                if not isinstance(value, dict):
                    continue
                size = str(value.get('dimm_size', ''))
                if not size or size.lower() in ('empty', 'vide'):
                    continue
                try:
                    capacity_gb = float(size.upper().replace('GB', '').strip())
                except ValueError:
                    capacity_gb = 0
                modules.append({
                    'slot': key,
                    'capacity_gb': capacity_gb,
                    'type': value.get('dimm_type', ''),
                    'speed_mhz': value.get('dimm_speed', ''),
                    'manufacturer': value.get('dimm_manufacturer', ''),
                    'part_number': value.get('dimm_part_number', ''),
                    'serial_number': value.get('dimm_serial_number', ''),
                })
        if modules:
            info['memory_modules'] = modules
            info['memory_slots_used'] = len(modules)
    except Exception:
        pass

    # ── GPU & écrans (un seul appel system_profiler pour les deux) ──────────
    # Jusqu'à 3.11, aucune collecte GPU n'existait côté macOS — `gpu`/
    # `gpu_details` restaient toujours absents, contrairement à Windows
    # (`_win_core()`/`_win_hardware_detail()`).
    try:
        displays = _mac_profiler_json('SPDisplaysDataType') or []
        gpus, gpu_details, monitors = [], [], []
        for carte in displays:
            nom_gpu = carte.get('_name') or carte.get('sppci_model') or ''
            if nom_gpu:
                gpus.append(nom_gpu)
                # Le libellé de la clé VRAM varie selon le type de GPU
                # (intégré vs dédié) et la version macOS — les variantes
                # rencontrées dans la nature sont toutes tentées.
                vram_brute = (carte.get('spdisplays_vram') or carte.get('spdisplays_vram_shared')
                              or carte.get('sppci_vram') or '')
                entree = {'name': nom_gpu, 'driver_version': '', 'driver_date': ''}
                vram_gb = _vram_texte_vers_gb(vram_brute)
                if vram_gb is not None:
                    entree['vram_gb'] = vram_gb
                gpu_details.append(entree)

            for screen in carte.get('spdisplays_ndrvs', []) or []:
                name = screen.get('_name', '')
                if not name:
                    continue
                annee = screen.get('_spdisplays_display-year')
                monitors.append({
                    'manufacturer': _deviner_marque_ecran(name),
                    'model': name,
                    'serial_number': screen.get('_spdisplays_display-serial-number', ''),
                    'year': str(annee) if annee else '',
                })
        if gpus:
            info['gpu'] = ', '.join(gpus)
        if gpu_details:
            info['gpu_details'] = gpu_details
        if monitors:
            info['monitors'] = monitors
    except Exception:
        pass

    # ── Batterie (usure réelle) ────────────────────────────────────────────
    try:
        power = _mac_profiler_json('SPPowerDataType') or []
        for section in power:
            health = section.get('sppower_battery_health_info') or {}
            charge = section.get('sppower_battery_charge_info') or {}
            if health.get('sppower_battery_cycle_count') is not None:
                info['battery_cycles'] = health['sppower_battery_cycle_count']
            if health.get('sppower_battery_health'):
                info['battery_health_status'] = health['sppower_battery_health']
            if charge.get('sppower_battery_state_of_charge') is not None:
                pct = charge['sppower_battery_state_of_charge']
                info['battery_charge_percent'] = pct
                info['battery'] = f"{pct}%"
    except Exception:
        pass

    # ── Disques ────────────────────────────────────────────────────────────
    info.update(_unix_disks())

    # ── Pare-feu applicatif macOS ──────────────────────────────────────────
    try:
        fw = _run(['/usr/libexec/ApplicationFirewall/socketfilterfw', '--getglobalstate'], timeout=5)
        if fw.strip():
            info['firewall'] = [f"macOS: {'Activé' if 'enabled' in fw.lower() else 'Désactivé'}"]
    except Exception:
        pass

    # ── FileVault (équivalent BitLocker) ───────────────────────────────────
    try:
        fv = _run(['fdesetup', 'status'], timeout=10)
        if fv.strip():
            info['bitlocker'] = [f"FileVault: {'Activé' if 'On' in fv else 'Désactivé'}"]
    except Exception:
        pass

    # ── Comptes locaux ─────────────────────────────────────────────────────
    try:
        out = _run(['dscl', '.', '-list', '/Users'], timeout=10)
        users = [u for u in out.split('\n') if u.strip() and not u.startswith('_')]
        admins = set()
        admin_out = _run(['dscl', '.', '-read', '/Groups/admin', 'GroupMembership'], timeout=10)
        if admin_out:
            admins = set(admin_out.replace('GroupMembership:', '').split())
        if users:
            info['users'] = sorted(
                f"{u} (Actif{', Administrateur' if u in admins else ''})"
                for u in users if u not in ('daemon', 'nobody', 'root')
            )
    except Exception:
        pass

    # ── Passerelle, DNS, proxy ───────────────────────────────────────────────
    try:
        info.update(_mac_network())
    except Exception:
        pass

    # ── Imprimantes ────────────────────────────────────────────────────────
    try:
        info.update(_mac_printers())
    except Exception:
        pass

    # ── Ports TCP en écoute ────────────────────────────────────────────────
    try:
        info.update(_mac_listening_ports())
    except Exception:
        pass

    # ── Accès distant (SSH, Partage d'écran) ───────────────────────────────
    try:
        info.update(_mac_remote_access())
    except Exception:
        pass

    # ── Agents de télémaintenance, RMM, EDR, antivirus tiers ───────────────
    try:
        info.update(_mac_managed_agents())
    except Exception:
        pass
    # XProtect est intégré à tout macOS moderne : valeur de base tant qu'un
    # antivirus tiers n'a pas été détecté ci-dessus parmi les process actifs.
    info.setdefault('antivirus', 'XProtect (intégré macOS)')

    # ── Mises à jour macOS/Apple disponibles ────────────────────────────────
    try:
        info.update(_mac_pending_updates())
    except Exception:
        pass

    # ── Posture de sécurité (SIP, Gatekeeper) ───────────────────────────────
    try:
        info.update(_mac_security_posture())
    except Exception:
        pass

    # ── Inscription MDM ──────────────────────────────────────────────────────
    try:
        info.update(_mac_mdm_status())
    except Exception:
        pass

    # ── Sauvegardes Time Machine ─────────────────────────────────────────────
    try:
        info.update(_mac_time_machine())
    except Exception:
        pass

    # ── Processus les plus gourmands CPU/RAM ────────────────────────────────
    try:
        info.update(_mac_top_processes())
    except Exception:
        pass

    # ── Rapports de plantage & paniques noyau ───────────────────────────────
    try:
        info.update(_mac_crash_diagnostics())
    except Exception:
        pass

    # ── Agents et démons au démarrage ────────────────────────────────────────
    try:
        info.update(_mac_startup_items())
    except Exception:
        pass

    # ── Uptime ─────────────────────────────────────────────────────────────
    info.update(_unix_uptime())

    return info


# ════════════════════════════════════════════════════════════════════════════
# COLLECTE LINUX
# ════════════════════════════════════════════════════════════════════════════

def _unix_disk_parent(device):
    """Nom du disque physique parent d'une partition Linux/macOS telle que
    `df` la nomme : /dev/sda1 -> sda, /dev/nvme0n1p1 -> nvme0n1,
    /dev/mmcblk0p1 -> mmcblk0, /dev/disk3s1 -> disk3 (macOS/APFS)."""
    nom = re.sub(r'^/dev/', '', device or '')
    for motif in (r'^(nvme\d+n\d+)p\d+$', r'^(mmcblk\d+)p\d+$', r'^(disk\d+)s\d+'):
        m = re.match(motif, nom)
        if m:
            return m.group(1)
    m = re.match(r'^([a-z]+)\d+$', nom)
    return m.group(1) if m else nom


#: Points de montage des volumes de service APFS présents sur tout Mac
#: moderne (depuis Catalina), qu'aucun technicien ne voudrait voir listés
#: comme des « disques » à part entière dans un inventaire — chacun
#: rapporte à `df` la capacité totale du conteneur partagé, comme si
#: c'était son propre disque physique. `/` (Système) et
#: `/System/Volumes/Data` restent affichés : ce sont les deux volumes où
#: vit réellement le contenu de la machine.
_MACOS_VOLUMES_INTERNES = {
    '/System/Volumes/VM', '/System/Volumes/Preboot', '/System/Volumes/Update',
    '/System/Volumes/xarts', '/System/Volumes/iSCPreboot',
    '/System/Volumes/Hardware', '/System/Volumes/Recovery',
}


def _unix_disks():
    """Disques via df (commun macOS / Linux).

    En plus du détail par volume (`disk_drives`), regroupe les partitions par
    disque physique parent (`disk_layout`) pour la vue « un disque, ses
    partitions dedans » de la fiche système. Sans modèle ni type ici — Linux
    les ajoute séparément via lsblk juste après ; macOS n'a pas de source pour
    ces deux champs et le groupe reste identifié par son seul nom d'appareil.
    """
    info = {}
    try:
        result = subprocess.run(['df', '-h'], capture_output=True, text=True, timeout=10)
        lines = result.stdout.strip().split('\n')[1:]  # Skip header
        disk_list = []
        total_used = 0.0
        seen = set()
        groupes = {}
        for line in lines:
            parts = line.split()
            if len(parts) < 4:
                continue
            device, size_str, used_str, avail_str = parts[0], parts[1], parts[2], parts[3]
            mount = parts[-1] if len(parts) > 4 else ''
            # Les pseudo-systèmes de fichiers gonflent artificiellement le total
            if not device.startswith('/dev/') or device in seen:
                continue
            # Volumes internes macOS/APFS (constaté, pas supposé) : System,
            # Preboot, VM, Update, xarts, iSCPreboot, Hardware, Recovery
            # existent sur TOUT Mac moderne, chacun rapportant à `df` la
            # capacité totale du conteneur partagé comme si c'était son
            # propre disque — sur la fiche système, cela donnait l'illusion
            # de 6 à 8 « disques » de taille quasi identique là où il n'y en
            # a physiquement qu'un. Ni de vrais volumes utilisateur, ni des
            # partitions système au sens où un technicien voudrait les
            # inventorier (contrairement à `/` et `/System/Volumes/Data`,
            # conservés) : exclus de l'affichage ET du total, comme les
            # pseudo-systèmes de fichiers ci-dessus.
            if mount in _MACOS_VOLUMES_INTERNES:
                continue
            seen.add(device)

            def to_gb(value):
                # Bug corrigé en 3.12 : `df -h` BSD (macOS) suffixe les
                # unités binaires d'un « i » (« 494Gi », « 11Mi »), que
                # GNU df (Linux) n'ajoute jamais (« 494G », « 11M ») bien
                # qu'utilisant la même base 1024 — cette fonction ne
                # reconnaissait que la forme Linux. Sur macOS, AUCUNE ligne
                # ne matchait jamais : la section stockage entière restait
                # vide, sans qu'aucune exception ne le signale (une valeur
                # simplement toujours `None`, silencieusement filtrée par
                # le `continue` juste en dessous).
                value = value.strip()
                m = re.match(r'^([\d.]+)\s*([KMGT])i?B?$', value, re.IGNORECASE)
                if not m:
                    return None
                try:
                    nombre = float(m.group(1))
                except ValueError:
                    return None
                facteur = {'T': 1024, 'G': 1, 'M': 1 / 1024, 'K': 1 / 1024 / 1024}
                return nombre * facteur[m.group(2).upper()]

            size_gb = to_gb(size_str)
            if size_gb is None:
                continue
            used_gb = to_gb(used_str) or 0
            free_gb = to_gb(avail_str) or 0
            disk_list.append(
                f"{device} — {round(size_gb, 1)} GB total, "
                f"{round(used_gb, 1)} GB utilisés, {round(free_gb, 1)} GB libres"
            )
            total_used += used_gb

            parent = _unix_disk_parent(device)
            groupe = groupes.setdefault(parent, {
                'number': parent, 'model': '', 'bus': '', 'media_type': '',
                'health': '', 'op_status': '', 'size_gb': 0.0, 'partitions': [],
            })
            groupe['partitions'].append({
                'letter': device.replace('/dev/', ''), 'type': '', 'total': round(size_gb, 1),
                'used': round(used_gb, 1), 'free': round(free_gb, 1),
                'pct': round(used_gb / size_gb * 100) if size_gb else None,
            })

        # Capacité totale/libre : une seule fois par couple (taille, libre)
        # identique au sein d'un même disque physique, pas la somme brute de
        # toutes les partitions. Sur macOS/APFS, plusieurs volumes d'un même
        # conteneur (Système, Données, VM, Preboot...) rapportent TOUS la
        # même capacité totale et le même espace libre partagé — les
        # additionner comme des disques distincts gonflait le total d'un
        # facteur égal au nombre de volumes du conteneur (constaté : ~500 Go
        # réels comptés jusqu'à 8 fois). `used`, lui, reste sommé sans
        # dédoublonnage : c'est l'usage propre de CHAQUE volume, une donnée
        # distincte à chaque ligne, jamais partagée. Sur Linux, où chaque
        # partition a sa propre taille distincte, le dédoublonnage ne change
        # rien au comportement existant (chaque partition garde sa taille).
        total_disk = 0.0
        total_free = 0.0
        for groupe in groupes.values():
            paires = {(p['total'], p['free']) for p in groupe['partitions']}
            groupe['size_gb'] = round(sum(taille for taille, _ in paires), 1)
            total_disk += groupe['size_gb']
            total_free += sum(libre for _, libre in paires)

        if disk_list:
            info['disk_drives'] = disk_list
            info['disk_total_gb'] = round(total_disk, 1)
            info['disk_free_gb'] = round(total_free, 1)
            info['disk_used_gb'] = round(total_used, 1)
        if groupes:
            info['disk_layout'] = list(groupes.values())
    except Exception:
        pass
    return info


def _unix_uptime():
    """Uptime en heures (commun macOS / Linux)."""
    info = {}
    try:
        if IS_LINUX:
            with open('/proc/uptime', 'r') as f:
                info['uptime_hours'] = round(float(f.read().split()[0]) / 3600, 1)
        elif IS_MAC:
            out = _run(['sysctl', '-n', 'kern.boottime'], timeout=5)
            # Format : { sec = 1699999999, usec = 0 } ...
            if 'sec =' in out:
                boot = int(out.split('sec =')[1].split(',')[0].strip())
                delta = datetime.now().timestamp() - boot
                info['uptime_hours'] = round(delta / 3600, 1)
    except Exception:
        pass
    return info


def get_system_info_linux():
    """Collecte les infos système sur Linux."""
    info = {}

    # ── Marque / modèle / série via DMI (lisible sans sudo depuis /sys) ────
    dmi_map = {
        'sys_vendor': 'brand',
        'product_name': 'model',
        'product_serial': 'serial_number',
        'chassis_type': 'chassis_code',
        'board_vendor': '_board_vendor',
        'board_name': '_board_name',
        'board_version': '_board_version',
        'board_serial': '_board_serial',
        'bios_version': 'bios_version',
        'bios_vendor': 'bios_manufacturer',
        'bios_date': 'bios_release_date',
    }
    dmi = {}
    for filename, key in dmi_map.items():
        try:
            with open(f'/sys/class/dmi/id/{filename}', 'r') as f:
                value = _clean(f.read())
                if value:
                    dmi[key] = value
        except Exception:
            pass

    for key, value in dmi.items():
        if not key.startswith('_'):
            info[key] = value

    if dmi.get('chassis_code'):
        try:
            code = int(dmi['chassis_code'])
            info['chassis_code'] = code
            info['chassis_type'] = CHASSIS_TYPES.get(code, f'Code {code}')
        except ValueError:
            info.pop('chassis_code', None)

    if dmi.get('_board_name') or dmi.get('_board_vendor'):
        info['motherboard'] = {
            'manufacturer': dmi.get('_board_vendor', ''),
            'model': dmi.get('_board_name', ''),
            'version': dmi.get('_board_version', ''),
            'serial_number': dmi.get('_board_serial', ''),
        }

    # ── CPU depuis /proc/cpuinfo ───────────────────────────────────────────
    try:
        with open('/proc/cpuinfo', 'r') as f:
            lines = f.readlines()
        for line in lines:
            if line.startswith('model name'):
                info['cpu'] = line.split(':', 1)[1].strip()
                break
        info['cpu_cores'] = sum(1 for line in lines if line.startswith('processor'))
        info['cpu_logical_cores'] = info['cpu_cores']
        cores_per_socket = {line.split(':', 1)[1].strip()
                            for line in lines if line.startswith('cpu cores')}
        if cores_per_socket:
            try:
                info['cpu_physical_cores'] = int(next(iter(cores_per_socket)))
            except ValueError:
                pass
    except Exception:
        pass

    # ── RAM depuis /proc/meminfo ───────────────────────────────────────────
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    info['ram_gb'] = round(int(line.split()[1]) / (1024 * 1024), 1)
                elif line.startswith('MemAvailable:'):
                    info['ram_free_gb'] = round(int(line.split()[1]) / (1024 * 1024), 1)
    except Exception:
        pass

    # ── Barrettes mémoire (dmidecode, nécessite root) ──────────────────────
    if is_elevated():
        try:
            out = _run(['dmidecode', '-t', 'memory'], timeout=15)
            modules = []
            current = {}
            for line in out.split('\n'):
                stripped = line.strip()
                if stripped.startswith('Memory Device'):
                    if current.get('capacity_gb'):
                        modules.append(current)
                    current = {}
                elif ':' in stripped and current is not None:
                    key, value = [p.strip() for p in stripped.split(':', 1)]
                    if key == 'Size' and 'No Module' not in value:
                        try:
                            if 'GB' in value:
                                current['capacity_gb'] = float(value.replace('GB', '').strip())
                            elif 'MB' in value:
                                current['capacity_gb'] = round(float(value.replace('MB', '').strip()) / 1024, 1)
                        except ValueError:
                            pass
                    elif key == 'Locator':
                        current['slot'] = value
                    elif key == 'Type':
                        current['type'] = value
                    elif key == 'Configured Memory Speed':
                        current['speed_mhz'] = value
                    elif key == 'Manufacturer':
                        current['manufacturer'] = value
                    elif key == 'Part Number':
                        current['part_number'] = value
                    elif key == 'Serial Number':
                        current['serial_number'] = value
            if current.get('capacity_gb'):
                modules.append(current)
            if modules:
                info['memory_modules'] = modules
                info['memory_slots_used'] = len(modules)
        except Exception:
            pass

    # ── Disques ────────────────────────────────────────────────────────────
    info.update(_unix_disks())

    # ── Type et santé des disques physiques (lsblk + smartctl) ─────────────
    try:
        out = _run(['lsblk', '-d', '-o', 'NAME,SIZE,ROTA,MODEL', '-J'], timeout=10)
        if out.strip():
            devices = json.loads(out).get('blockdevices', [])
            physical = []
            par_nom = {}
            for d in devices:
                if d.get('name', '').startswith(('loop', 'ram', 'sr')):
                    continue
                media = 'HDD' if d.get('rota') in (True, '1', 1) else 'SSD'
                physical.append(f"{d.get('model') or d.get('name')} — {media} — {d.get('size', '?')}")
                par_nom[d.get('name')] = {'model': d.get('model') or '', 'media_type': media}
            if physical:
                info['physical_disks'] = physical
            # Rattache modèle et type au groupe déjà construit par _unix_disks() :
            # lsblk connaît le disque physique, df n'en a que le nom d'appareil.
            for groupe in info.get('disk_layout') or []:
                enrichi = par_nom.get(groupe.get('number'))
                if enrichi:
                    groupe['model'] = enrichi['model']
                    groupe['media_type'] = enrichi['media_type']
    except Exception:
        pass

    # ── Écrans (EDID exposé par le noyau, sans dépendance X) ───────────────
    try:
        monitors = []
        drm_root = '/sys/class/drm'
        if os.path.isdir(drm_root):
            for entry in sorted(os.listdir(drm_root)):
                status_path = os.path.join(drm_root, entry, 'status')
                if not os.path.exists(status_path):
                    continue
                with open(status_path) as f:
                    if f.read().strip() != 'connected':
                        continue
                monitors.append({
                    'manufacturer': '',
                    'model': entry.replace('card0-', '').replace('card1-', ''),
                    'serial_number': '',
                    'year': '',
                })
        if monitors:
            info['monitors'] = monitors
    except Exception:
        pass

    # ── Imprimantes (CUPS) ─────────────────────────────────────────────────
    try:
        out = _run(['lpstat', '-p'], timeout=10)
        printers = [
            {'name': line.split()[1], 'driver': '', 'port': '', 'network': False,
             'default': False, 'shared': False}
            for line in out.split('\n') if line.startswith('printer ') and len(line.split()) > 1
        ]
        if printers:
            info['printers'] = printers
    except Exception:
        pass

    # ── Comptes locaux ─────────────────────────────────────────────────────
    try:
        users = []
        admins = set()
        try:
            with open('/etc/group') as f:
                for line in f:
                    parts = line.split(':')
                    if len(parts) >= 4 and parts[0] in ('sudo', 'wheel', 'admin'):
                        admins.update(u for u in parts[3].strip().split(',') if u)
        except Exception:
            pass
        with open('/etc/passwd') as f:
            for line in f:
                parts = line.split(':')
                if len(parts) < 7:
                    continue
                name, uid, shell = parts[0], parts[2], parts[6].strip()
                # Les comptes de service ont un UID < 1000 ou un shell nologin
                if not uid.isdigit() or int(uid) < 1000 or 'nologin' in shell or 'false' in shell:
                    continue
                status = 'Actif'
                if name in admins:
                    status += ', Administrateur'
                users.append(f"{name} ({status})")
        if users:
            info['users'] = sorted(users)
    except Exception:
        pass

    # ── Ports TCP en écoute ────────────────────────────────────────────────
    try:
        out = _run(['ss', '-tlnp'], timeout=10)
        ports = {}
        for line in out.split('\n')[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            local = parts[3]
            if ':' not in local:
                continue
            try:
                port = int(local.rsplit(':', 1)[1])
            except ValueError:
                continue
            process = ''
            if 'users:' in line:
                try:
                    process = line.split('users:')[1].split('"')[1]
                except IndexError:
                    pass
            ports.setdefault(port, process)
        if ports:
            info['listening_ports'] = [
                {'port': p, 'process': proc} for p, proc in sorted(ports.items())
            ]
    except Exception:
        pass

    # ── Uptime ─────────────────────────────────────────────────────────────
    info.update(_unix_uptime())

    # ── Domaine / groupe de travail ────────────────────────────────────────
    try:
        domain = socket.getfqdn()
        if domain and '.' in domain:
            info['domain'] = domain.split('.', 1)[1]
    except Exception:
        pass

    return info


# ════════════════════════════════════════════════════════════════════════════
# LOGICIELS INSTALLÉS
# ════════════════════════════════════════════════════════════════════════════

def get_installed_software():
    """Récupère la liste complète des logiciels installés (machine + par utilisateur).

    Retourne une liste de dicts {name, version, publisher, install_date} - version/
    publisher/install_date restent vides quand la source ne les fournit pas
    (cas Mac/Linux, dont les gestionnaires de paquets énumérés ici ne donnent
    que le nom sans requête individuelle coûteuse par paquet).
    """
    software = {}  # clé = nom (dédup), valeur = dict métadonnées

    if IS_WINDOWS:
        try:
            import winreg
            # (hive, chemin) - HKLM couvre les installs machine (64 et 32 bits sur WOW6432Node),
            # HKCU couvre les installs propres à l'utilisateur courant (souvent absentes sinon)
            hives = [
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'),
                (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
            ]
            for hive, path in hives:
                try:
                    with winreg.OpenKey(hive, path) as key:
                        count = winreg.QueryInfoKey(key)[0]
                        for i in range(count):
                            try:
                                subkey_name = winreg.EnumKey(key, i)
                                with winreg.OpenKey(key, subkey_name) as subkey:
                                    try:
                                        display_name = winreg.QueryValueEx(subkey, 'DisplayName')[0]
                                    except OSError:
                                        continue
                                    if not display_name or not display_name.strip():
                                        continue
                                    name = display_name.strip()

                                    def _read(value_name):
                                        try:
                                            return winreg.QueryValueEx(subkey, value_name)[0]
                                        except OSError:
                                            return ''

                                    version = str(_read('DisplayVersion') or '')
                                    publisher = str(_read('Publisher') or '')
                                    install_date_raw = str(_read('InstallDate') or '')
                                    # Format registre habituel : "AAAAMMJJ" -> "AAAA-MM-JJ"
                                    if len(install_date_raw) == 8 and install_date_raw.isdigit():
                                        install_date = f"{install_date_raw[:4]}-{install_date_raw[4:6]}-{install_date_raw[6:8]}"
                                    else:
                                        install_date = install_date_raw

                                    # Taille déclarée par l'installeur, en Ko :
                                    # elle est déjà dans cette clé de registre et
                                    # répond à « qu'est-ce qui remplit le disque »
                                    # sans aucun parcours de fichiers.
                                    taille_ko = _read('EstimatedSize')
                                    try:
                                        taille_mo = round(int(taille_ko) / 1024) if taille_ko else None
                                    except (TypeError, ValueError):
                                        taille_mo = None

                                    software[name] = {
                                        'name': name,
                                        'version': version,
                                        'publisher': publisher,
                                        'install_date': install_date,
                                        'size_mb': taille_mo,
                                    }
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            pass

    elif IS_MAC:
        # SPApplicationsDataType donne nom/version/éditeur/date pour toute
        # application dans /Applications, ~/Applications et les emplacements
        # système — contrairement à un simple `ls /Applications`, jusqu'ici
        # la seule source (nom seul, sans version). Réputé lent (vérifie la
        # signature de chaque app) : timeout généreux, best-effort comme
        # tout le reste de ce fichier.
        try:
            apps = _mac_profiler_json('SPApplicationsDataType', timeout=60) or []
            for app in apps:
                name = (app.get('_name') or '').strip()
                if not name:
                    continue
                obtenu = app.get('obtained_from') or ''
                editeur = {
                    'apple': 'Apple', 'mac_app_store': 'App Store',
                    'identified_developer': app.get('signed_by') or 'Développeur identifié',
                    'unidentified_developer': '',
                }.get(obtenu, '')
                date_install = (app.get('lastModified') or app.get('last_modified') or '')
                software[name] = {
                    'name': name,
                    'version': str(app.get('version') or ''),
                    'publisher': editeur,
                    'install_date': date_install[:10] if date_install else '',
                }
        except Exception:
            pass

        # Homebrew (formules + casks) : les paquets en ligne de commande
        # n'apparaissent jamais dans SPApplicationsDataType (pas de bundle
        # .app). Intel installe sous /usr/local, Apple Silicon sous
        # /opt/homebrew — les deux chemins sont tentés, un seul existera.
        for prefixe in ('/usr/local/opt', '/opt/homebrew/opt'):
            try:
                result = subprocess.run(['ls', prefixe], capture_output=True, text=True, timeout=10)
                for pkg in result.stdout.split('\n'):
                    pkg = pkg.strip()
                    if pkg and pkg not in software:
                        software[pkg] = {'name': pkg, 'version': '', 'publisher': '', 'install_date': ''}
            except Exception:
                pass

        # pkgutil : installeurs .pkg qui ne créent pas de bundle .app
        # (souvent des composants système ou des CLI) — nom seul, comme
        # avant, en complément des deux sources ci-dessus.
        try:
            result = subprocess.run(['pkgutil', '--packages'], capture_output=True, text=True, timeout=15)
            for pkg in result.stdout.split('\n'):
                pkg = pkg.strip()
                if pkg and pkg not in software:
                    software[pkg] = {'name': pkg, 'version': '', 'publisher': '', 'install_date': ''}
        except Exception:
            pass

    elif IS_LINUX:
        # Debian/Ubuntu (dpkg) - le format query donne aussi version et éditeur
        try:
            result = subprocess.run(
                ['dpkg-query', '-W', '-f=${Package}\t${Version}\t${Maintainer}\n'],
                capture_output=True, text=True, timeout=20
            )
            for line in result.stdout.split('\n'):
                parts = line.split('\t')
                if parts and parts[0].strip():
                    software[parts[0]] = {
                        'name': parts[0],
                        'version': parts[1] if len(parts) > 1 else '',
                        'publisher': parts[2] if len(parts) > 2 else '',
                        'install_date': '',
                    }
        except Exception:
            pass

        # RedHat/CentOS (rpm)
        if not software:
            try:
                result = subprocess.run(
                    ['rpm', '-qa', '--queryformat', '%{NAME}\t%{VERSION}-%{RELEASE}\t%{VENDOR}\n'],
                    capture_output=True, text=True, timeout=20
                )
                for line in result.stdout.split('\n'):
                    parts = line.split('\t')
                    if parts and parts[0].strip():
                        software[parts[0]] = {
                            'name': parts[0],
                            'version': parts[1] if len(parts) > 1 else '',
                            'publisher': parts[2] if len(parts) > 2 else '',
                            'install_date': '',
                        }
            except Exception:
                pass

        # Arch Linux (pacman)
        if not software:
            try:
                result = subprocess.run(['pacman', '-Q'], capture_output=True, text=True, timeout=20)
                for line in result.stdout.split('\n'):
                    parts = line.split()
                    if parts:
                        software[parts[0]] = {
                            'name': parts[0],
                            'version': parts[1] if len(parts) > 1 else '',
                            'publisher': '',
                            'install_date': '',
                        }
            except Exception:
                pass

    return sorted(software.values(), key=lambda s: s['name'].lower())


# ════════════════════════════════════════════════════════════════════════════
# MISES À JOUR LOGICIELLES DISPONIBLES
# ════════════════════════════════════════════════════════════════════════════

def _run_hidden(cmd, timeout=10):
    """Comme _run(), mais masque la fenêtre console qui apparaîtrait sinon par-
    dessus le collecteur GUI (sans console attachée) — utilisé pour winget."""
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=creationflags)
        return (result.stdout or b'').decode('utf-8', errors='replace')
    except Exception:
        return ''


def _normalize_software_name(name):
    """Réduit un nom de logiciel à une forme comparable entre deux sources
    (registre Windows vs gestionnaire de paquets) : casse, ponctuation et
    mentions d'architecture entre parenthèses ignorées."""
    n = re.sub(r'\([^)]*\)', ' ', str(name or '').lower())
    n = re.sub(r'[^a-z0-9]+', ' ', n)
    return n.strip()


def _parse_winget_table(sortie):
    """Parse un tableau texte produit par winget (`upgrade` ou `list`), par
    POSITION de colonne, jamais par intitulé d'en-tête — sur un Windows non
    anglophone, l'en-tête est traduit (« Name »/« Nom », « Available »/
    « Disponible »…), seul l'ordre des colonnes (nom, ID, version,
    disponible, source) est stable d'une langue à l'autre (constaté sur un
    Windows en français : la version par intitulé anglais ne trouvait jamais
    l'en-tête et retournait silencieusement un résultat vide).

    La sortie peut contenir plusieurs tableaux (`upgrade` en affiche un
    second pour les paquets nécessitant un ciblage explicite) — chaque bloc
    « en-tête + séparateur + lignes » rencontré est parsé, ligne de résumé
    finale comprise : elle ne produit pas de ligne exploitable, trop courte
    pour remplir les dernières colonnes.

    Retourne une liste de lignes, chacune une liste de champs alignés sur les
    colonnes de SON propre en-tête (pas forcément le même nombre de colonnes
    d'un tableau à l'autre dans une même sortie).
    """
    lignes = sortie.splitlines()
    resultat = []
    i = 1
    while i < len(lignes):
        if not re.match(r'^-{5,}\s*$', lignes[i].strip()):
            i += 1
            continue
        colonnes = [m.start() for m in re.finditer(r'\S+', lignes[i - 1])]
        i += 1
        if len(colonnes) < 2:
            continue
        while i < len(lignes) and not re.match(r'^-{5,}\s*$', lignes[i].strip()):
            ligne = lignes[i]
            if ligne.strip():
                champs = []
                for k, debut in enumerate(colonnes):
                    fin = colonnes[k + 1] if k + 1 < len(colonnes) else len(ligne)
                    champs.append(ligne[debut:fin].strip())
                resultat.append(champs)
            i += 1
    return resultat


#: Position des colonnes dans un tableau winget (upgrade ou list) — ordre
#: fixe, contrairement aux intitulés d'en-tête (voir _parse_winget_table).
_WINGET_COL_NOM, _WINGET_COL_DISPONIBLE, _WINGET_COL_SOURCE = 0, 3, 4


def _winget_upgradable():
    """Liste les paquets winget ayant une mise à jour disponible.

    Retourne {nom_normalise: version_disponible}, vide si winget est absent,
    hors ligne ou si le format de sortie change.
    """
    resultat = {}
    if not shutil.which('winget'):
        return resultat
    sortie = _run_hidden(
        ['winget', 'upgrade', '--include-unknown', '--disable-interactivity',
         '--accept-source-agreements'],
        timeout=90)
    for champs in _parse_winget_table(sortie):
        if len(champs) > _WINGET_COL_DISPONIBLE and champs[_WINGET_COL_NOM] and champs[_WINGET_COL_DISPONIBLE]:
            resultat[_normalize_software_name(champs[_WINGET_COL_NOM])] = champs[_WINGET_COL_DISPONIBLE]
    return resultat


def _winget_installed():
    """Ensemble des logiciels (noms normalisés) que winget confirme suivre
    via une vraie source de paquets (colonne Source non vide) — `winget
    list` remonte aussi les entrées qu'il a simplement lues dans le Panneau
    de configuration sans pouvoir les rapprocher d'aucune source, ce qui ne
    permet de rien affirmer sur leur statut.

    Sert à distinguer, dans `check_software_updates()`, un logiciel confirmé
    à jour (suivi par winget, absent de la liste des mises à jour) d'un
    logiciel simplement non vérifiable (absent des deux — winget ne le
    connaît pas du tout). Sans cette distinction, les deux cas se
    ressemblaient à l'affichage : silence total.
    """
    resultat = set()
    if not shutil.which('winget'):
        return resultat
    sortie = _run_hidden(
        ['winget', 'list', '--disable-interactivity', '--accept-source-agreements'],
        timeout=90)
    for champs in _parse_winget_table(sortie):
        if (len(champs) > _WINGET_COL_SOURCE and champs[_WINGET_COL_NOM]
                and champs[_WINGET_COL_SOURCE]):
            resultat.add(_normalize_software_name(champs[_WINGET_COL_NOM]))
    return resultat


def _brew_outdated():
    """Consulte `brew outdated` (Homebrew, formules + casks) pour les logiciels
    ayant une mise à jour disponible. Retourne {nom_normalise: version_dispo}."""
    resultat = {}
    if not shutil.which('brew'):
        return resultat
    sortie = _run(['brew', 'outdated', '--json=v2'], timeout=45)
    try:
        data = json.loads(sortie) if sortie.strip() else {}
    except (ValueError, TypeError):
        return resultat
    for cle in ('formulae', 'casks'):
        for item in (data.get(cle) or []):
            try:
                nom = item.get('name')
                if isinstance(nom, list):
                    nom = nom[0] if nom else None
                version = item.get('current_version')
                if nom and version:
                    resultat[_normalize_software_name(nom)] = str(version)
            except Exception:
                continue
    return resultat


def _apt_upgradable():
    """Paquets Debian/Ubuntu ayant une mise à jour dans le cache apt local —
    pas de `apt update` déclenché ici : réseau et droits non garantis."""
    resultat = {}
    if not shutil.which('apt'):
        return resultat
    sortie = _run(['apt', 'list', '--upgradable'], timeout=30)
    for ligne in sortie.splitlines():
        m = re.match(r'^(\S+)/\S+\s+(\S+)\s', ligne.strip())
        if m:
            resultat[_normalize_software_name(m.group(1))] = m.group(2)
    return resultat


def _dnf_upgradable():
    """Paquets RedHat/CentOS/Fedora ayant une mise à jour (`dnf`/`yum check-update`)."""
    resultat = {}
    exe = 'dnf' if shutil.which('dnf') else ('yum' if shutil.which('yum') else None)
    if not exe:
        return resultat
    sortie = _run([exe, 'check-update'], timeout=45)
    for ligne in sortie.splitlines():
        m = re.match(r'^(\S+)\.\S+\s+(\S+)\s+\S+\s*$', ligne.strip())
        if m:
            resultat[_normalize_software_name(m.group(1))] = m.group(2)
    return resultat


def _pacman_upgradable():
    """Paquets Arch Linux ayant une mise à jour (`pacman -Qu`)."""
    resultat = {}
    if not shutil.which('pacman'):
        return resultat
    sortie = _run(['pacman', '-Qu'], timeout=30)
    for ligne in sortie.splitlines():
        parts = ligne.split()
        if len(parts) >= 4 and parts[2] == '->':
            resultat[_normalize_software_name(parts[0])] = parts[3]
    return resultat


def check_software_updates(software):
    """Annote chaque entrée de `software` avec `update_status`
    ('obsolete' / 'a_jour' / 'inconnu') et, si une mise à jour est repérée,
    `latest_version` / `update_source`. Modifie et retourne la même liste.

    'a_jour' n'est posé QUE quand le gestionnaire de paquets confirme
    explicitement connaître ce logiciel (winget : présent dans `winget list`
    avec une source réelle ; Linux : `installed_software` vient déjà du même
    gestionnaire que celui interrogé ici, apt/dpkg ou dnf/rpm ou pacman
    partagent la même base de paquets). Un simple silence ne suffit jamais :
    un gestionnaire n'indexe qu'une partie de ce que remonte le registre ou
    les listes système (bien des installations manuelles lui échappent), et
    son silence sur un logiciel qu'il ne connaît pas du tout ('inconnu')
    n'a rien à voir avec un logiciel qu'il connaît et confirme à jour.

    macOS reste volontairement sans 'a_jour' : `installed_software` y mélange
    /Applications, pkgutil et Homebrew sans distinguer leur origine, donc
    rien ne garantit qu'un logiciel donné soit dans le champ de `brew`.
    """
    resultat, source = {}, ''
    connus = None  # None : aucune confirmation possible ; set : noms confirmés (winget) ; True : tout installed_software vient de la même base que `resultat` (Linux)
    try:
        if IS_WINDOWS:
            resultat, source = _winget_upgradable(), 'winget'
            connus = _winget_installed()
        elif IS_MAC:
            resultat, source = _brew_outdated(), 'brew'
        elif IS_LINUX:
            if shutil.which('apt'):
                resultat, source, connus = _apt_upgradable(), 'apt', True
            elif shutil.which('dnf') or shutil.which('yum'):
                resultat, source, connus = _dnf_upgradable(), 'dnf/yum', True
            elif shutil.which('pacman'):
                resultat, source, connus = _pacman_upgradable(), 'pacman', True
    except Exception:
        resultat, source, connus = {}, '', None

    for soft in software:
        if not isinstance(soft, dict):
            continue
        cle = _normalize_software_name(soft.get('name'))
        dispo = resultat.get(cle) if resultat else None
        if dispo:
            soft['update_status'] = 'obsolete'
            soft['latest_version'] = dispo
            soft['update_source'] = source
        elif connus is True or (isinstance(connus, set) and cle in connus):
            soft['update_status'] = 'a_jour'
            soft['latest_version'] = ''
            soft['update_source'] = source
        else:
            soft['update_status'] = 'inconnu'
            soft['latest_version'] = ''
    return software


# ════════════════════════════════════════════════════════════════════════════
# TYPE D'APPAREIL
# ════════════════════════════════════════════════════════════════════════════

def guess_device_type(info):
    """Déduit le type ParcInfo (liste `types_appareils`) à partir du châssis.

    Sans cette déduction, l'API crée tous les postes en "PC", y compris les
    portables et les serveurs.
    """
    if info.get('is_server'):
        return 'Serveur'

    chassis_code = info.get('chassis_code')
    if chassis_code in CHASSIS_PORTABLE:
        return 'MacBook' if IS_MAC else 'Laptop'
    if chassis_code in CHASSIS_TABLET:
        return 'Tablette'

    # macOS : le nom de modèle distingue portable et fixe quand le châssis manque
    if IS_MAC:
        model_name = (info.get('model_name') or info.get('model') or '').lower()
        return 'MacBook' if 'book' in model_name else 'PC'

    if IS_LINUX:
        return 'PC/Serveur (Linux)'

    if IS_WINDOWS:
        return 'PC (Windows)'

    return 'PC'


# ════════════════════════════════════════════════════════════════════════════
# COLLECTE GLOBALE
# ════════════════════════════════════════════════════════════════════════════

def console_progress(largeur=32, flux=None):
    """Fabrique un rappel de progression qui dessine une barre en console.

    La barre est réécrite sur la même ligne tant que le flux est un terminal ;
    redirigé vers un fichier ou un journal, on retombe sur une ligne par étape
    pour ne pas produire un fichier illisible de retours chariot.
    """
    flux = flux or sys.stdout
    interactif = hasattr(flux, 'isatty') and flux.isatty()
    etat = {'dernier': None}

    def rappel(fraction, libelle):
        if not interactif:
            if libelle != etat['dernier']:
                etat['dernier'] = libelle
                flux.write('  [%3d%%] %s\n' % (round(fraction * 100), libelle))
                flux.flush()
            return
        remplies = int(round(largeur * fraction))
        barre = '#' * remplies + '-' * (largeur - remplies)
        ligne = '  [%s] %3d%%  %s' % (barre, round(fraction * 100), libelle)
        # Compléter par des espaces : un libellé plus court laisserait sinon la
        # fin du précédent à l'écran.
        flux.write('\r' + ligne.ljust(78)[:78])
        flux.flush()
        if fraction >= 1.0:
            flux.write('\n')
            flux.flush()

    return rappel


def _meilleure_carte_physique(adapters):
    """Choisit la carte physique la plus pertinente pour l'identité de la machine.

    `get_mac_address()`/`get_ip_addresses()` (utilisés au tout début de la
    collecte, avant que les cartes détaillées soient connues) retombent
    respectivement sur `uuid.getnode()` et la résolution DNS du hostname —
    ni l'un ni l'autre ne garantit de tomber sur la carte physique réellement
    utilisée : un VPN, Docker ou Hyper-V s'intercalent facilement devant, et
    l'un ou l'autre devient alors la MAC/IP « officielle » de l'appareil.

    Une fois les cartes détaillées disponibles (Windows), la meilleure
    candidate les remplace : physique, connectée, avec au moins une adresse
    IP — à défaut la meilleure approximation possible, jamais pire que ce
    qu'on avait déjà.

    Retourne le dict de la carte choisie, ou None si aucune carte physique
    n'est disponible (macOS/Linux aujourd'hui, faute de collecte équivalente,
    ou aucune carte physique détectée).
    """
    candidats = [a for a in (adapters or []) if isinstance(a, dict) and a.get('physical')]
    if not candidats:
        return None
    candidats.sort(key=lambda a: (a.get('connected') is True, bool(a.get('ip_addresses'))), reverse=True)
    return candidats[0]


def collect_system_info(progress=None, test_debit=False, url_debit=None, on_data=None,
                        verifier_dns=False, info_box=False):
    """Collecte toutes les infos système.

    `progress` est un rappel optionnel appelé avec (fraction entre 0 et 1,
    libellé de l'étape en cours). Il permet aux deux collecteurs d'afficher la
    même progression sans dupliquer la liste des étapes.

    `on_data` reçoit une copie de l'état partiel après chaque étape : l'interface
    graphique remplit ainsi ses onglets au fil de l'eau au lieu de tout afficher
    à la fin.

    `verifier_dns` et `info_box` sont désactivées par défaut : la première
    sollicite un service tiers (dnscheck.tools), la seconde sonde le réseau
    local (UPnP) — même principe que `test_debit`, un choix explicite plutôt
    qu'un comportement systématique.
    """
    _report(progress, 0.02, 'Identification de la machine')
    info = {
        'collector_version': COLLECTOR_VERSION,
        'timestamp': _utcnow().isoformat(),
        'elevated': is_elevated(),
        'mac_address': get_mac_address(),
        'hostname': get_hostname(),
        'dns_name': get_fqdn(),
        'ip_addresses': get_ip_addresses(),
    }
    _publier(on_data, info)

    # OS
    _report(progress, 0.06, "Système d'exploitation")
    info.update(get_os_info())
    _publier(on_data, info)

    # Infos spécifiques par OS
    try:
        if IS_WINDOWS:
            info.update(get_system_info_windows(progress, on_data=lambda partiel: (
                _publier(on_data, dict(info, **partiel)))))
        elif IS_MAC:
            _report(progress, 0.30, 'Collecte macOS')
            info.update(get_system_info_mac())
        elif IS_LINUX:
            _report(progress, 0.30, 'Collecte Linux')
            info.update(get_system_info_linux())
    except Exception:
        pass
    _publier(on_data, info)

    # Le MAC/IP « canoniques » posés au tout début (uuid.getnode() / résolution
    # DNS du hostname) tombent souvent sur une interface virtuelle plutôt que
    # la carte physique réellement utilisée — corrigés ici si une meilleure
    # source existe (voir _meilleure_carte_physique), pour que la fiche
    # appareil et le rapport système restent cohérents avec « Réseau ».
    try:
        meilleure = _meilleure_carte_physique(info.get('network_adapter_details'))
        if meilleure:
            if meilleure.get('mac_address'):
                info['mac_address'] = meilleure['mac_address']
            adresses = [ip['address'] for ip in (meilleure.get('ip_addresses') or []) if ip.get('address')]
            if adresses:
                info['ip_addresses'] = adresses
    except Exception:
        pass
    _publier(on_data, info)

    # Fichier hosts : redirections DNS actives, hors bruit par défaut.
    # Cross-plateforme (simple lecture de fichier), donc appelé ici plutôt
    # que dans un bloc spécifique à un OS.
    try:
        redirections = get_hosts_file_entries(info.get('hostname'))
        if redirections:
            info['hosts_entries'] = redirections
    except Exception:
        pass
    _publier(on_data, info)

    # Logiciels
    _report(progress, 0.82, 'Inventaire logiciel')
    info['installed_software'] = get_installed_software()
    _publier(on_data, info)

    # Mises à jour disponibles : dépend d'un gestionnaire de paquets (winget/
    # brew/apt/dnf/pacman) et peut interroger le réseau — isolé pour qu'un
    # échec ou une lenteur ici ne prive pas le rapport de l'inventaire déjà
    # récupéré ci-dessus.
    _report(progress, 0.85, 'Vérification des mises à jour logicielles')
    try:
        check_software_updates(info['installed_software'])
    except Exception:
        pass
    _publier(on_data, info)

    # Périphériques USB connectés. Isolé de la collecte système : une erreur ici
    # (droits, commande absente) ne doit pas priver le rapport du reste.
    _report(progress, 0.88, 'Mesures réseau')
    try:
        info.update(measure_network(info, test_debit=test_debit, url_debit=url_debit))
    except Exception:
        pass
    _publier(on_data, info)

    # IP publique et opérateur : cross-plateforme (simple appel HTTPS), donc
    # appelé ici plutôt que dans measure_network() ci-dessus, réservée à
    # Windows.
    _report(progress, 0.90, 'IP publique')
    try:
        info.update(get_public_ip_info())
    except Exception:
        pass
    _publier(on_data, info)

    # Options désactivées par défaut : sollicitent un service tiers / sondent
    # le réseau local, un choix explicite plutôt qu'un comportement
    # systématique — même principe que test_debit.
    if verifier_dns:
        _report(progress, 0.905, 'Vérification DNS (dnscheck.tools)')
        try:
            info.update(get_dns_check_info(info))
        except Exception:
            pass
        _publier(on_data, info)

    if info_box:
        _report(progress, 0.91, 'Infos box internet (UPnP)')
        try:
            info.update(get_router_info())
        except Exception:
            pass
        _publier(on_data, info)

    _report(progress, 0.93, 'Périphériques USB')
    try:
        info['usb_devices'] = collect_usb_devices()
    except Exception:
        info['usb_devices'] = []

    # Type d'appareil déduit
    info['device_type'] = guess_device_type(info)

    # Échéance de support, déduite du build : aucune commande à lancer.
    echeance = support_windows(info.get('os_build'))
    if echeance:
        info['windows_support'] = echeance

    _report(progress, 1.0, 'Collecte terminée')
    _publier(on_data, info)
    return info


# ════════════════════════════════════════════════════════════════════════════
# FORMATAGE / RÉSUMÉ
# ════════════════════════════════════════════════════════════════════════════

def format_memory_module(module):
    """Formate une barrette pour affichage : 'Slot — 16 GB DDR4 3200 MHz — Kingston (KF432)'."""
    parts = [f"{module.get('capacity_gb', '?')} GB"]
    if module.get('type'):
        parts.append(module['type'])
    if module.get('speed_mhz'):
        parts.append(f"{module['speed_mhz']} MHz")
    detail = ' '.join(parts)

    suffix = []
    if module.get('manufacturer'):
        suffix.append(module['manufacturer'])
    if module.get('part_number'):
        suffix.append(module['part_number'])

    line = f"{module.get('slot', 'Slot inconnu')} — {detail}"
    if suffix:
        line += f" — {' / '.join(suffix)}"
    return line


def format_reliability(entry):
    """Formate l'usure d'un disque : heures de fonctionnement, usure, température."""
    bits = []
    if entry.get('power_on_hours') is not None:
        hours = entry['power_on_hours']
        bits.append(f"{hours} h ({round(hours / 8760, 1)} an(s))")
    if entry.get('wear_percent') is not None:
        bits.append(f"usure {entry['wear_percent']}%")
    if entry.get('temperature_c'):
        bits.append(f"{entry['temperature_c']} °C")
    errors = (entry.get('read_errors') or 0) + (entry.get('write_errors') or 0)
    if errors:
        bits.append(f"{errors} erreur(s)")
    return f"{entry.get('name', 'Disque')} — {', '.join(bits)}" if bits else entry.get('name', 'Disque')


def format_license(lic):
    """Formate une licence : 'Windows 11 Pro — Activé (OEM:DM) — clé ...XXXXX'."""
    line = lic.get('name', '')
    if lic.get('status'):
        line += f" — {lic['status']}"
    if lic.get('channel'):
        line += f" ({lic['channel']})"
    if lic.get('partial_key'):
        line += f" — clé se terminant par {lic['partial_key']}"
    return line


def format_monitor(mon):
    """Formate un écran : 'Dell U2419H — S/N ABC123 (2021)'."""
    parts = [p for p in (mon.get('manufacturer'), mon.get('model')) if p]
    line = ' '.join(parts) or 'Écran'
    if mon.get('serial_number'):
        line += f" — S/N {mon['serial_number']}"
    if mon.get('year'):
        line += f" ({mon['year']})"
    return line


def format_printer(pr):
    """Formate une imprimante : 'HP LaserJet (par défaut, réseau) — port IP_192.168.1.50'."""
    flags = []
    if pr.get('virtual'):
        flags.append('virtuelle')
    if pr.get('default'):
        flags.append('par défaut')
    if pr.get('network'):
        flags.append('réseau')
    if pr.get('shared'):
        flags.append('partagée')
    line = pr.get('name', 'Imprimante')
    if flags:
        line += f" ({', '.join(flags)})"
    if pr.get('port'):
        line += f" — port {pr['port']}"
    if pr.get('driver'):
        line += f" — {pr['driver']}"
    return line


def _sec(cle, titre, icone):
    """Squelette d'une rubrique du résumé."""
    return {'cle': cle, 'titre': titre, 'icone': icone, 'champs': [], 'listes': [],
            'notes': []}


def _usb_ligne(d):
    """Une ligne lisible pour un périphérique USB."""
    parts = [d.get('name') or 'Périphérique']
    if d.get('manufacturer'):
        parts.append(d['manufacturer'])
    if d.get('model') and d.get('model') != d.get('name'):
        parts.append(d['model'])
    detail = ' — '.join(parts)
    suffixes = []
    if d.get('categorie'):
        suffixes.append(d['categorie'])
    if d.get('vid'):
        suffixes.append('%s:%s' % (d['vid'], d['pid']))
    if d.get('serial'):
        suffixes.append('S/N %s' % d['serial'])
    if d.get('install_date'):
        suffixes.append('vu le %s' % d['install_date'])
    if suffixes:
        detail += '  [' + ' · '.join(suffixes) + ']'
    return detail


def build_summary_sections(info):
    """Découpe les données collectées en rubriques affichables.

    Source unique du résumé : la sortie texte du collecteur CLI en découle
    (build_summary_lines), et l'interface graphique en fait ses onglets. Deux
    constructions séparées auraient dérivé l'une de l'autre au fil des ajouts.

    Retourne une liste de rubriques :
        {'cle', 'titre', 'icone', 'champs': [(libellé, valeur)],
         'listes': [{'titre', 'elements': [...]}], 'notes': [...]}
    Les rubriques sans aucune donnée sont omises.
    """
    sections = []

    # ── Alertes ────────────────────────────────────────────────────────────
    try:
        alertes = build_alerts(info) or []
    except Exception:
        alertes = []
    if alertes:
        s = _sec('alertes', 'Points de vigilance', '⚠')
        s['listes'].append({'titre': '', 'elements': [
            ('%s%s' % (a.get('titre', ''), ' — %s' % a['detail'] if a.get('detail') else ''))
            if isinstance(a, dict) else str(a)
            for a in alertes]})
        sections.append(s)

    # ── Identification ─────────────────────────────────────────────────────
    s = _sec('identification', 'Identification', '🖥')
    s['champs'] += [
        ('Hostname', info.get('hostname')),
        ('Nom DNS', info.get('dns_name')),
        ('Adresse MAC', info.get('mac_address')),
        ("Adresse(s) IP", ', '.join(info.get('ip_addresses') or []) or None),
        ("Type d'appareil", info.get('device_type')),
        ('Marque', info.get('brand')),
        ('Modèle', info.get('model')),
        ('Numéro de série', info.get('serial_number')),
        ('Asset tag', info.get('asset_tag')),
        ('Châssis', info.get('chassis_type')),
    ]
    age = hardware_age_years(info.get('bios_release_date'))
    if age is not None:
        s['champs'].append(('Âge du matériel', '%s an(s)' % age))
    sections.append(s)

    # ── Système ────────────────────────────────────────────────────────────
    s = _sec('systeme', "Système d'exploitation", '⚙')
    bios = info.get('bios_version') or ''
    if bios and info.get('bios_release_date'):
        bios += ' (%s)' % info['bios_release_date']
    uptime = info.get('uptime_hours')
    s['champs'] += [
        ('OS', info.get('os_name')),
        ('Version', info.get('os_version')),
        ('Build', info.get('os_build')),
        ('Fin de support', ('%s — %s (%s)' % (
            (info.get('windows_support') or {}).get('fin_de_support'),
            (info.get('windows_support') or {}).get('version'),
            'dépassée' if (info.get('windows_support') or {}).get('termine')
            else 'dans %d jours' % (info.get('windows_support') or {}).get('jours_restants', 0)))
         if info.get('windows_support') else None),
        ('Architecture', info.get('architecture')),
        ('Installé le', info.get('os_install_date')),
        # Le rattachement domaine/groupe de travail est repris, plus complet
        # (avec contrôleur de domaine), dans la rubrique Environnement.
        ('Session ouverte', info.get('logged_on_user')),
        ('Propriétaire déclaré', info.get('registered_owner')),
        ('Uptime', ('%s jour(s)' % round(uptime / 24, 1)) if uptime is not None else None),
        ('BIOS', bios or None),
        ('Fuseau horaire', info.get('timezone')),
        ('Virtualisation', 'Machine virtuelle / hyperviseur détecté'
         if info.get('hypervisor_present') else None),
    ]
    sections.append(s)

    # ── Matériel ───────────────────────────────────────────────────────────
    s = _sec('materiel', 'Matériel', '🔧')
    cores = []
    if info.get('cpu_physical_cores'):
        cores.append('%s cœurs physiques' % info['cpu_physical_cores'])
    if info.get('cpu_logical_cores'):
        cores.append('%s logiques' % info['cpu_logical_cores'])
    if not cores and info.get('cpu_cores'):
        cores.append('%s cœurs' % info['cpu_cores'])
    mb = info.get('motherboard') or {}
    slots = None
    if info.get('memory_slots_total'):
        slots = '%s/%s occupés' % (info.get('memory_slots_used', 0), info['memory_slots_total'])
        if info.get('memory_max_gb'):
            slots += ' (max %s GB)' % info['memory_max_gb']
    s['champs'] += [
        ('CPU', info.get('cpu')),
        ('Cœurs', ', '.join(cores) or None),
        ('Fréquence max', ('%s MHz' % info['cpu_max_clock_mhz'])
         if info.get('cpu_max_clock_mhz') else None),
        ('Socket', info.get('cpu_socket')),
        ('Virtualisation CPU', info.get('cpu_virtualization')),
        ('Carte mère', ('%s %s' % (mb.get('manufacturer', ''), mb.get('model', ''))).strip() or None),
        ('RAM', ('%s GB' % info['ram_gb']) if info.get('ram_gb') else None),
        ('RAM libre', ('%s GB' % info['ram_free_gb']) if info.get('ram_free_gb') else None),
        ('Slots mémoire', slots),
    ]
    if info.get('memory_modules'):
        s['listes'].append({'titre': 'Barrettes installées',
                            'elements': [format_memory_module(m) for m in info['memory_modules']]})
    if info.get('gpu_details'):
        elements = []
        for gpu in info['gpu_details']:
            detail = gpu.get('name', '')
            if gpu.get('vram_gb'):
                detail += ' — %s GB VRAM' % gpu['vram_gb']
            if gpu.get('resolution'):
                detail += ' — %s' % gpu['resolution']
            if gpu.get('driver_version'):
                detail += ' — pilote %s' % gpu['driver_version']
            elements.append(detail)
        s['listes'].append({'titre': 'Carte(s) graphique(s)', 'elements': elements})
    elif info.get('gpu'):
        s['champs'].append(('Carte graphique', info['gpu']))
    sections.append(s)

    # ── Stockage ───────────────────────────────────────────────────────────
    s = _sec('stockage', 'Stockage', '💾')
    if info.get('disk_total_gb') is not None:
        total = '%s GB' % info['disk_total_gb']
        if info.get('disk_used_gb') is not None and info.get('disk_free_gb') is not None:
            total += ' (%s GB utilisés, %s GB libres)' % (info['disk_used_gb'], info['disk_free_gb'])
        s['champs'].append(('Total', total))
    if info.get('disk_drives'):
        s['listes'].append({'titre': 'Volumes', 'elements': list(info['disk_drives'])})
    if info.get('physical_disks'):
        s['listes'].append({'titre': 'Disques physiques', 'elements': list(info['physical_disks'])})
    if info.get('disk_reliability'):
        s['listes'].append({'titre': 'Santé (SMART)',
                            'elements': [format_reliability(e) for e in info['disk_reliability']]})
    if info.get('disk_error_events'):
        s['champs'].append(('Erreurs disque (30 j)', info['disk_error_events']))
    if info.get('boot_mode'):
        s['champs'].append(('Mode de démarrage', info['boot_mode']))
    if info.get('disk_partition_styles'):
        s['listes'].append({'titre': 'Style de partition', 'elements': [
            'Disque %s — %s%s' % (d.get('number'), d.get('style'),
                                  ' (démarrage)' if d.get('boot') else '')
            for d in info['disk_partition_styles']]})
    sections.append(s)

    # ── Écrans et imprimantes ──────────────────────────────────────────────
    s = _sec('peripheriques', 'Écrans et imprimantes', '🖨')
    if info.get('monitors'):
        s['listes'].append({'titre': 'Écrans',
                            'elements': [format_monitor(m) for m in info['monitors']]})
    if info.get('printers'):
        s['listes'].append({'titre': 'Imprimantes',
                            'elements': [format_printer(p) for p in info['printers']]})
    sections.append(s)

    # ── USB ────────────────────────────────────────────────────────────────
    s = _sec('usb', 'Périphériques USB', '🔌')
    usb = info.get('usb_devices') or []
    if usb:
        inventories = [d for d in usb if d.get('inventoriable')]
        internes = [d for d in usb if not d.get('inventoriable')]
        s['champs'].append(('Détectés', '%d, dont %d repris dans l\'inventaire'
                            % (len(usb), len(inventories))))
        if inventories:
            s['listes'].append({'titre': 'Repris dans l\'inventaire',
                                'elements': [_usb_ligne(d) for d in inventories]})
        if internes:
            s['listes'].append({'titre': 'Contrôleurs et concentrateurs internes',
                                'elements': [_usb_ligne(d) for d in internes]})
    sections.append(s)

    # ── Réseau ─────────────────────────────────────────────────────────────
    s = _sec('reseau', 'Réseau', '🌐')
    passerelles = ', '.join(
        '%s (%s)' % (g.get('address', '?'), g.get('interface', '?')) if isinstance(g, dict) else str(g)
        for g in (info.get('gateways') or [])) or info.get('default_gateway')
    dns = []
    for entree in (info.get('dns_servers') or []):
        if isinstance(entree, dict):
            serveurs = ', '.join(entree.get('servers') or [])
            if serveurs:
                dns.append('%s : %s%s' % (entree.get('interface', '?'), serveurs,
                                          ' (DHCP)' if entree.get('dhcp') else ''))
        else:
            dns.append(str(entree))
    proxy = info.get('proxy')
    if isinstance(proxy, dict):
        proxy = ('%s%s' % (proxy.get('server') or 'configuration automatique',
                           '' if proxy.get('enabled') else ' — inactif')) or None
    s['champs'] += [
        ('Passerelle', passerelles),
        ('IP publique', info.get('public_ip')),
        ('Opérateur (FAI)', info.get('public_ip_isp')),
        ('Suffixes DNS', ', '.join(info.get('dns_suffixes') or []) or None),
        ('Proxy', proxy),
        ('Résolveur DNS interrogé', info.get('dns_check_resolveur')),
        ('Vérification DNS (dnscheck.tools)', info.get('dns_check_reponse')),
        ('Box internet — fabricant', info.get('router_manufacturer')),
        ('Box internet — modèle', info.get('router_model')),
        ('Box internet — nom', info.get('router_name')),
        ('Box internet — IP WAN', info.get('router_wan_ip')),
    ]
    if dns:
        s['listes'].append({'titre': 'Serveurs DNS', 'elements': dns})
    if info.get('hosts_entries'):
        s['listes'].append({'titre': 'Redirections du fichier hosts', 'elements': [
            '%s → %s%s' % (h.get('hostname', '?'), h.get('ip', '?'),
                           ' (local)' if h.get('local') else '')
            if isinstance(h, dict) else str(h)
            for h in info['hosts_entries']]})
    if info.get('port_forwards'):
        s['listes'].append({'titre': 'Redirections de port', 'elements': [
            '%s:%s → %s:%s' % (p.get('listen_address', '?'), p.get('listen_port', '?'),
                               p.get('connect_address', '?'), p.get('connect_port', '?'))
            if isinstance(p, dict) else str(p)
            for p in info['port_forwards']]})
    # La latence est mesurée vers plusieurs cibles (passerelle, Internet) :
    # chacune a son intérêt — une passerelle lente n'a pas la même cause qu'un
    # Internet lent.
    for mesure in (info.get('latency') or []):
        if not isinstance(mesure, dict):
            continue
        s['champs'].append((
            'Latence %s' % (mesure.get('role') or mesure.get('target', '?')),
            '%s ms (max %s ms, perte %s %%) — %s'
            % (mesure.get('avg_ms', '?'), mesure.get('max_ms', '?'),
               mesure.get('loss_pct', '?'), mesure.get('target', '?'))))
    bp = info.get('bandwidth') or {}
    if bp:
        s['champs'].append(('Débit descendant', '%s Mb/s (%s Mo en %s s)'
                            % (bp.get('mbps', '?'), bp.get('downloaded_mb', '?'),
                               bp.get('seconds', '?'))))
    elif info.get('latency') is not None:
        # « Non mesuré » n'a de sens qu'une fois l'étape réseau passée : l'écrire
        # avant ferait apparaître une rubrique Réseau dès la première seconde,
        # avec cette seule ligne pour tout contenu.
        s['champs'].append(('Débit descendant', 'Non mesuré'))
    if info.get('wifi'):
        s['champs'].append(('Wi-Fi', info['wifi'] if isinstance(info['wifi'], str)
                            else json.dumps(info['wifi'], ensure_ascii=False)))
    cartes = []
    for a in (info.get('network_adapter_details') or []):
        if not isinstance(a, dict):
            cartes.append(str(a))
            continue
        libelle = a.get('name') or a.get('description') or '?'
        details = []
        # Chaque adresse porte son masque : l'IP seule ne dit pas dans quelle
        # plage la carte se trouve, information utile en diagnostic.
        adresses = ['%s/%s' % (ip.get('address', '?'), ip.get('prefix'))
                    if isinstance(ip, dict) else str(ip)
                    for ip in (a.get('ip_addresses') or [])]
        if adresses:
            details.append(', '.join(adresses))
        if a.get('mac_address'):
            details.append(a['mac_address'])
        if a.get('link_speed'):
            details.append(str(a['link_speed']))
        details.append('physique' if a.get('physical') else 'virtuelle')
        if a.get('connected') is not None:
            details.append('connectée' if a['connected'] else 'déconnectée')
        cartes.append('%s  [%s]' % (libelle, ' · '.join(details)))
    if not cartes and info.get('network_adapters'):
        cartes = list(info['network_adapters'])
    if cartes:
        s['listes'].append({'titre': 'Cartes réseau', 'elements': cartes})
    if info.get('network_profiles'):
        s['listes'].append({'titre': 'Profils réseau', 'elements': [
            '%s — %s (%s, %s)' % (p.get('name', '?'), p.get('interface', '?'),
                                  p.get('category', '?'), p.get('connectivity', '?'))
            if isinstance(p, dict) else str(p)
            for p in info['network_profiles']]})
    ports = info.get('listening_ports') or []
    if ports:
        s['listes'].append({'titre': 'Ports TCP en écoute (%d)' % len(ports),
                            'elements': ['%s%s' % (p.get('port'),
                                                   ' — %s' % p['process'] if p.get('process') else '')
                                         for p in ports]})
    # Deux faces de la même ressource : ce que la machine expose au réseau,
    # et ce qu'elle y a mappé depuis d'autres machines.
    if info.get('smb_shares'):
        s['listes'].append({'titre': 'Partages réseau exposés', 'elements': [
            '%s → %s%s' % (x.get('name', '?'), x.get('path', '?'),
                           ' (administratif)' if x.get('administrative') else '')
            if isinstance(x, dict) else str(x)
            for x in info['smb_shares']]})
    if info.get('mapped_drives'):
        s['listes'].append({'titre': 'Lecteurs réseau mappés', 'elements': [
            '%s → %s' % (d.get('letter'), d.get('path') or '?') for d in info['mapped_drives']]})
    sections.append(s)

    # ── Sécurité ───────────────────────────────────────────────────────────
    s = _sec('securite', 'Sécurité', '🛡')
    pol = info.get('local_password_policy') or {}
    s['champs'] += [
        ('Antivirus', info.get('antivirus')),
        ('TPM', ('Présent et activé' if info.get('tpm_enabled')
                 else ('Présent mais désactivé' if info.get('tpm_present') else 'Absent'))
         if info.get('tpm_present') is not None else None),
        ('Secure Boot', ('Activé' if info['secure_boot'] else 'Désactivé')
         if info.get('secure_boot') is not None else None),
        ("Protection de l'intégrité système (SIP)", info.get('sip_status')),
        ('Gatekeeper', info.get('gatekeeper_status')),
        ('Inscription MDM', info.get('mdm_detail') or
         ('Oui' if info.get('mdm_enrolled') else 'Non')
         if info.get('mdm_enrolled') is not None else None),
        ('Mot de passe — longueur mini',
         '%s caractère(s)' % pol['min_length'] if pol.get('min_length') is not None else None),
        ('Mot de passe — complexité',
         ('exigée' if pol['complexity'] else 'non exigée')
         if pol.get('complexity') is not None else None),
        ('Verrouillage de compte',
         ('après %s essai(s)' % pol['lockout_threshold'] if pol['lockout_threshold'] else 'aucun')
         if pol.get('lockout_threshold') is not None else None),
    ]
    if info.get('antivirus_products'):
        s['listes'].append({'titre': 'Antivirus détectés', 'elements': [
            '%s — %s%s' % (a.get('name', '?'),
                           a.get('status') or ('actif' if a.get('enabled') else 'inactif'),
                           '' if a.get('up_to_date') else ', signatures à jour : non')
            if isinstance(a, dict) else str(a)
            for a in info['antivirus_products']]})
    if info.get('firewall_profiles'):
        s['listes'].append({'titre': 'Pare-feu',
                            'elements': ['%s : %s' % (p.get('name', '?'),
                                                      'activé' if p.get('enabled') else 'désactivé')
                                         if isinstance(p, dict) else str(p)
                                         for p in info['firewall_profiles']]})
    elif info.get('firewall'):
        s['listes'].append({'titre': 'Pare-feu', 'elements': list(info['firewall'])})
    if info.get('firewall_rules'):
        total = info.get('firewall_rules_total') or len(info['firewall_rules'])
        titre = 'Règles de pare-feu autorisées (%d)' % total
        if len(info['firewall_rules']) < total:
            titre += ' — %d affichées' % len(info['firewall_rules'])
        s['listes'].append({'titre': titre, 'elements': [
            '%s [%s] — %s%s%s' % (r.get('name', '?'), r.get('direction') or '?',
                                  r.get('protocol') or '?',
                                  ':%s' % r['port'] if r.get('port') else '',
                                  ' (%s)' % r['profiles'] if r.get('profiles') else '')
            if isinstance(r, dict) else str(r)
            for r in info['firewall_rules']]})
    if info.get('bitlocker'):
        s['listes'].append({'titre': 'Chiffrement', 'elements': list(info['bitlocker'])})
    if info.get('failed_logons'):
        s['champs'].append(("Échecs d'ouverture de session (%d j)" % EVENT_WINDOW_DAYS,
                            info['failed_logons']))
    if info.get('account_lockouts'):
        s['champs'].append(('Verrouillages de compte (%d j)' % EVENT_WINDOW_DAYS,
                            info['account_lockouts']))
    if info.get('security_events'):
        s['listes'].append({'titre': 'Journal de sécurité', 'elements': [
            '%s — %s ×%s%s%s' % (e.get('compte', '?'), e.get('type', '?'), e.get('count', 1),
                                 ' — depuis %s' % ', '.join(e['sources']) if e.get('sources') else '',
                                 ' — dernier %s' % e['last_seen'] if e.get('last_seen') else '')
            if isinstance(e, dict) else str(e)
            for e in info['security_events']]})
    if info.get('certificates_expiring'):
        s['listes'].append({'titre': 'Certificats à renouveler', 'elements': [
            '%s — %s le %s (%s)' % (c.get('sujet', '?'),
                                    'expiré' if c.get('expire') else 'expire',
                                    c.get('expire_le', '?'),
                                    'il y a %d j' % abs(c.get('jours_restants', 0))
                                    if c.get('expire') else
                                    'dans %d j' % c.get('jours_restants', 0))
            if isinstance(c, dict) else str(c)
            for c in info['certificates_expiring']]})
    if info.get('rdp_logon_history'):
        s['listes'].append({'titre': 'Connexions Bureau à distance entrantes', 'elements': [
            '%s — depuis %s — %s' % (c.get('user', '?'), c.get('ip') or '?', c.get('when', '?'))
            for c in info['rdp_logon_history']]})
    if info.get('malware_detections'):
        total = info.get('malware_detections_total') or len(info['malware_detections'])
        s['champs'].append(('Détections antivirus (1 an)', total))
        s['listes'].append({'titre': 'Détections antivirus (Windows Defender)', 'elements': [
            '%s (%s) — %s%s' % (d.get('threat', '?'), d.get('category', '?'), d.get('when', '?'),
                                '' if d.get('cleaned') else ' — non traitée')
            for d in info['malware_detections']]})
    if info.get('unsigned_drivers'):
        s['champs'].append(('Pilotes non signés', len(info['unsigned_drivers'])))
        s['listes'].append({'titre': 'Pilotes non signés', 'elements': [
            '%s — %s%s' % (p.get('device', '?'), p.get('version') or 'version inconnue',
                           ' (%s)' % p['provider'] if p.get('provider') else '')
            if isinstance(p, dict) else str(p)
            for p in info['unsigned_drivers']]})
    sections.append(s)

    # ── Licences ───────────────────────────────────────────────────────────
    s = _sec('licences', 'Licences et activation', '🔑')
    if info.get('windows_activated') is not None:
        s['champs'].append(('Windows', 'Activé' if info['windows_activated'] else 'NON ACTIVÉ'))
    s['champs'] += [
        ('Canal de licence', info.get('windows_license_channel')),
        ('Clé OEM (firmware)', info.get('oem_product_key')),
    ]
    if info.get('licenses'):
        s['listes'].append({'titre': 'Produits',
                            'elements': [format_license(l) for l in info['licenses']]})
    sections.append(s)

    # ── Mises à jour ───────────────────────────────────────────────────────
    s = _sec('maj', 'Mises à jour', '📦')
    hotfixes = info.get('hotfixes') or []
    if hotfixes:
        s['champs'].append(('Correctifs installés', len(hotfixes)))
    s['champs'].append(('Dernier correctif', info.get('last_windows_update')))
    pending = info.get('pending_updates') or []
    if pending:
        libelle = '%d disponible(s)' % len(pending)
        if info.get('pending_updates_security'):
            libelle += ', dont %s de sécurité' % info['pending_updates_security']
        if info.get('pending_updates_source'):
            libelle += ' — recherche %s' % info['pending_updates_source']
        s['champs'].append(('Non installées', libelle))
        s['listes'].append({'titre': 'Mises à jour disponibles', 'elements': [
            '%s%s%s' % (u.get('title', '?'),
                        ' [%s]' % u['kb'] if u.get('kb') else '',
                        ' — %s Mo' % u['size_mb'] if u.get('size_mb') else '')
            if isinstance(u, dict) else str(u)
            for u in pending]})
    if hotfixes:
        s['listes'].append({'titre': 'Correctifs',
                            'elements': ['%s — %s%s' % (hf.get('id', '?'),
                                                        hf.get('installed_on') or 'date inconnue',
                                                        ' (%s)' % hf['description']
                                                        if hf.get('description') else '')
                                         for hf in hotfixes]})
    sections.append(s)

    # ── Comptes ────────────────────────────────────────────────────────────
    s = _sec('comptes', 'Comptes utilisateurs', '👤')
    details = info.get('users_details') or []
    if details:
        s['champs'].append(('Total', '%d compte(s)' % len(details)))
        elements = []
        for u in details:
            libelle = u.get('name', '?')
            marques = []
            if u.get('role'):
                marques.append(u['role'])
            elif u.get('admin'):
                marques.append('Administrateur')
            if u.get('account_type'):
                marques.append('compte %s' % u['account_type'])
            marques.append(u.get('status') or ('actif' if u.get('enabled') else 'désactivé'))
            if u.get('password_never_expires'):
                marques.append('mot de passe sans expiration')
            if u.get('last_logon'):
                marques.append('dernière ouverture %s' % u['last_logon'])
            if marques:
                libelle += '  [' + ' · '.join(marques) + ']'
            elements.append(libelle)
        s['listes'].append({'titre': 'Comptes locaux', 'elements': elements})
    elif info.get('users'):
        s['champs'].append(('Total', '%d compte(s)' % len(info['users'])))
        s['listes'].append({'titre': 'Comptes locaux', 'elements': list(info['users'])})
    if info.get('user_profiles'):
        s['listes'].append({'titre': 'Occupation disque par profil', 'elements': [
            '%s — %s Go%s%s' % (p.get('nom', '?'), p.get('taille_go', '?'),
                                '' if p.get('mesure_complete') else ' (au moins — mesure interrompue)',
                                ' — dernière utilisation %s' % p['derniere_utilisation']
                                if p.get('derniere_utilisation') else '')
            if isinstance(p, dict) else str(p)
            for p in info['user_profiles']]})
    sections.append(s)

    # ── Diagnostic ─────────────────────────────────────────────────────────
    s = _sec('diagnostic', 'Diagnostic', '🩺')
    if info.get('unexpected_shutdowns') is not None:
        s['champs'].append(('Arrêts inattendus (30 j)', info['unexpected_shutdowns']))
    if info.get('top_processes_cpu'):
        s['listes'].append({'titre': 'Processus les plus gourmands (CPU, instantané)', 'elements': [
            '%s — %s %%' % (p.get('name', '?'), p.get('cpu_pct', 0))
            if isinstance(p, dict) else str(p)
            for p in info['top_processes_cpu']]})
    if info.get('top_processes_ram'):
        s['listes'].append({'titre': 'Processus les plus gourmands (RAM, instantané)', 'elements': [
            '%s — %s Mo' % (p.get('name', '?'), p.get('ram_mb', 0))
            if isinstance(p, dict) else str(p)
            for p in info['top_processes_ram']]})
    if info.get('system_incidents'):
        s['listes'].append({'titre': 'Incidents système (30 derniers jours)', 'elements': [
            '%s ×%s%s%s' % (i.get('category', '?'), i.get('count', 1),
                            ' — dernier %s' % i['last_seen'] if i.get('last_seen') else '',
                            ' — %s' % i['disk'] if i.get('disk') else '')
            if isinstance(i, dict) else str(i)
            for i in info['system_incidents']]})
    if info.get('system_errors'):
        s['listes'].append({'titre': 'Erreurs système (hors incidents ci-dessus)', 'elements': [
            '%s/%s ×%s — dernière %s%s' % (e.get('provider', '?'), e.get('event_id', '?'),
                                           e.get('count', 1), e.get('last_seen', '?'),
                                           ' — %s' % e['message'] if e.get('message') else '')
            for e in info['system_errors']]})
    if info.get('application_errors'):
        s['listes'].append({'titre': 'Erreurs applicatives', 'elements': [
            '%s — %s ×%s — dernière %s%s' % (a.get('application', '?'), a.get('type', '?'),
                                             a.get('count', 1), a.get('last_seen', '?'),
                                             ' — %s' % a['exception'] if a.get('exception') else '')
            for a in info['application_errors']]})
    if info.get('shutdown_history'):
        s['listes'].append({'titre': 'Arrêts & redémarrages', 'elements': [
            '%s %s — %s%s' % (h.get('action', '?'), h.get('when', '?'), h.get('reason', '?'),
                              '' if h.get('planned') else ' (non planifié)')
            for h in info['shutdown_history']]})
    if info.get('stopped_auto_services'):
        s['listes'].append({'titre': "Services automatiques à l'arrêt", 'elements': [
            x.get('display_name') or x.get('name', '?') if isinstance(x, dict) else str(x)
            for x in info['stopped_auto_services']]})
    if info.get('startup_programs'):
        s['listes'].append({'titre': 'Programmes au démarrage', 'elements': [
            '%s%s%s' % (x.get('name', '?'),
                        ' — %s' % x['command'] if x.get('command') else '',
                        ' (%s)' % x['user'] if x.get('user') else '')
            if isinstance(x, dict) else str(x)
            for x in info['startup_programs']]})
    if info.get('scheduled_tasks'):
        s['listes'].append({'titre': 'Tâches planifiées (hors Microsoft)', 'elements': [
            '%s%s — %s%s%s' % (x.get('path', ''), x.get('name', '?'), x.get('state', '?'),
                               ' — dernière exécution %s' % x['last_run'] if x.get('last_run') else '',
                               '  ⚠ en échec' if x.get('failed') else '')
            if isinstance(x, dict) else str(x)
            for x in info['scheduled_tasks']]})
    if info.get('boot_last_seconds') is not None:
        s['champs'].append(('Dernier démarrage', '%s s (moyenne %s s)'
                            % (info['boot_last_seconds'],
                               info.get('boot_average_seconds', '?'))))
    # Échecs d'ouverture de session, journal de sécurité et certificats sont
    # dans la rubrique Sécurité ; profils utilisateurs dans Comptes ; partages
    # réseau dans Réseau, à côté des lecteurs mappés.
    if info.get('problem_devices'):
        s['listes'].append({'titre': 'Périphériques en erreur', 'elements': [
            '%s — %s%s' % (p.get('name', '?'), p.get('libelle', '?'),
                           ' (%s)' % p['classe'] if p.get('classe') else '')
            if isinstance(p, dict) else str(p)
            for p in info['problem_devices']]})
    sections.append(s)

    # ── Environnement ──────────────────────────────────────────────────────
    s = _sec('environnement', 'Environnement', '🏢')
    s['champs'] += [
        ('Domaine', info.get('domain_name') or info.get('domain')),
        ('Contrôleur de domaine', info.get('domain_controller')),
        ('Intégré au domaine', ('oui' if info['domain_joined'] else 'non')
         if info.get('domain_joined') is not None else None),
        ('Serveur WSUS', info.get('wsus_server')),
        ('Groupe WSUS', info.get('wsus_group')),
        ('Source de temps', info.get('time_source')),
        ('Écart d\'horloge', info.get('time_offset')),
        ('Rôle serveur', ('oui' if info['is_server'] else 'non')
         if info.get('is_server') is not None else None),
    ]
    if info.get('group_policies'):
        s['listes'].append({'titre': 'Stratégies de groupe appliquées', 'elements': [
            '%s (%s)%s' % (g.get('name', '?'), g.get('scope', '?'),
                           ' — refusée' if g.get('denied') else
                           ('' if g.get('enabled') else ' — désactivée'))
            if isinstance(g, dict) else str(g)
            for g in info['group_policies']]})
    sections.append(s)

    # ── Hygiène ────────────────────────────────────────────────────────────
    # Le bureau à distance et les autres voies d'accès sont dans « Accès
    # distant » ci-dessous.
    s = _sec('hygiene', 'Hygiène système', '🧹')
    s['champs'] += [
        ('UAC', ('activé' if info['uac_enabled'] else 'désactivé')
         if info.get('uac_enabled') is not None else None),
        ('Points de restauration',
         (len(info['restore_points']) or 'aucun') if isinstance(info.get('restore_points'), list)
         else info.get('restore_points')),
        ('Fichiers temporaires', ('%s Mo' % info['temp_files_mb'])
         if info.get('temp_files_mb') is not None else None),
        ('Redémarrage en attente',
         ('oui — %s' % ', '.join(info.get('reboot_reasons') or []))
         if info.get('reboot_pending') else
         ('non' if info.get('reboot_pending') is not None else None)),
        ("Plan d'alimentation", info.get('power_plan')),
        ('Démarrage rapide', ('activé' if info.get('fast_startup') else 'désactivé')
         if info.get('fast_startup') is not None else None),
        ('Dernière analyse antivirus',
         '%s (complète)' % info['defender_last_full_scan']
         if info.get('defender_last_full_scan') else
         ('%s (rapide)' % info['defender_last_quick_scan']
          if info.get('defender_last_quick_scan') else None)),
    ]
    if info.get('dotnet_versions'):
        s['listes'].append({'titre': '.NET installé', 'elements': list(info['dotnet_versions'])})
    sections.append(s)

    # ── Accès distant ──────────────────────────────────────────────────────
    s = _sec('acces', 'Accès distant', '🔓')
    for a in info.get('remote_access') or []:
        if a.get('enabled') is None:
            etat = 'non installé'
        else:
            etat = 'ACTIF' if a['enabled'] else 'inactif'
        detail = ' — %s' % a['detail'] if a.get('detail') else ''
        s['champs'].append((a.get('label'), '%s%s' % (etat, detail)))
    auto = info.get('autologon')
    if auto is not None:
        if auto.get('enabled'):
            s['champs'].append(('Ouverture auto de session',
                                'ACTIVÉE — %s%s' % (auto.get('user') or '?',
                                ' (mot de passe en clair)' if auto.get('password_stored') else '')))
        else:
            s['champs'].append(('Ouverture auto de session', 'désactivée'))
    if info.get('rdp_allowed_users'):
        s['listes'].append({'titre': 'Membres autorisés (Bureau à distance)',
                            'elements': list(info['rdp_allowed_users'])})
    if info.get('saved_rdp_credentials'):
        s['listes'].append({'titre': 'Identifiants Bureau à distance enregistrés',
                            'elements': list(info['saved_rdp_credentials'])})
    if info.get('remote_support_agents'):
        s['listes'].append({'titre': 'Agents de télémaintenance', 'elements': [
            '%s (%s) — %s' % (a.get('nom', '?'), a.get('marque', '?'),
                              'actif' if a.get('actif') else 'inactif')
            for a in info['remote_support_agents']]})
    if info.get('edr_agents'):
        s['listes'].append({'titre': 'Agents EDR', 'elements': [
            '%s (%s) — %s' % (a.get('nom', '?'), a.get('marque', '?'),
                              'actif' if a.get('actif') else 'inactif')
            for a in info['edr_agents']]})
    sections.append(s)

    # ── Messagerie ─────────────────────────────────────────────────────────
    s = _sec('messagerie', 'Comptes de messagerie', '📧')
    for m in info.get('mail_accounts') or []:
        entrant = m.get('incoming_server') or ''
        if entrant and m.get('incoming_port'):
            entrant += ':%s' % m['incoming_port']
        valeur = '%s%s%s' % (m.get('protocol') or 'compte',
                             ' — %s' % entrant if entrant else '',
                             ' (%s)' % m['client'])
        s['champs'].append((m.get('email') or m.get('display_name') or '?', valeur))
    nouveau = info.get('mail_new_outlook')
    if nouveau and nouveau.get('installed'):
        s['champs'].append(('Nouvel Outlook', 'installé — %s' % nouveau.get('note', '')))
    if s['champs']:
        s['notes'].append('Les mots de passe ne sont jamais collectés.')
    sections.append(s)

    # ── Applications par défaut ──────────────────────────────────────────────
    # Les lecteurs réseau mappés sont dans Réseau, avec les partages exposés ;
    # le redémarrage en attente est dans Hygiène, avec les autres signaux de
    # maintenance.
    s = _sec('poste', 'Applications par défaut', '🧭')
    s['champs'] += [
        ('Navigateur par défaut', info.get('default_browser')),
        ('Client mail par défaut', info.get('default_mail')),
    ]
    if info.get('installed_browsers'):
        s['listes'].append({'titre': 'Navigateurs installés', 'elements': [
            '%s %s' % (b.get('name'), b.get('version') or '') for b in info['installed_browsers']]})
    if info.get('file_type_defaults'):
        s['listes'].append({'titre': 'Types de fichiers', 'elements': [
            '%s → %s' % (a.get('extension'), a.get('name')) for a in info['file_type_defaults']]})
    sections.append(s)

    # ── Batterie ───────────────────────────────────────────────────────────
    s = _sec('batterie', 'Batterie', '🔋')
    s['champs'] += [
        ('Charge', info.get('battery')),
        ('Santé', ('%s %% (usure %s %%)' % (info['battery_health_percent'],
                                            info.get('battery_wear_percent', '?')))
         if info.get('battery_health_percent') is not None else None),
        ('Cycles', info.get('battery_cycles')),
        ('État', info.get('battery_health_status')),
    ]
    sections.append(s)

    # ── Logiciels ──────────────────────────────────────────────────────────
    s = _sec('logiciels', 'Logiciels installés', '📚')
    software = info.get('installed_software') or []
    if software:
        s['champs'].append(('Total', '%d logiciel(s)' % len(software)))
        obsoletes = [x for x in software if isinstance(x, dict) and x.get('update_status') == 'obsolete']
        a_jour = [x for x in software if isinstance(x, dict) and x.get('update_status') == 'a_jour']
        if obsoletes:
            s['champs'].append(('Mises à jour disponibles', '%d logiciel(s)' % len(obsoletes)))
        if a_jour:
            s['champs'].append(('Confirmés à jour', '%d logiciel(s)' % len(a_jour)))
        elements = []
        for soft in software:
            if isinstance(soft, dict):
                libelle = soft.get('name', '')
                if soft.get('version'):
                    libelle += ' (v%s)' % soft['version']
                extras = [x for x in (soft.get('publisher'), soft.get('install_date')) if x]
                if extras:
                    libelle += '  [' + ' · '.join(str(e) for e in extras) + ']'
                if soft.get('update_status') == 'obsolete':
                    libelle += '  ⚠ maj disponible (v%s)' % soft.get('latest_version', '?')
                elif soft.get('update_status') == 'a_jour':
                    libelle += '  ✓ à jour'
            else:
                libelle = str(soft)
            elements.append(libelle)
        s['listes'].append({'titre': 'Inventaire', 'elements': elements})
    sections.append(s)

    # Rubriques vides écartées : un onglet sans contenu ne se distingue pas
    # d'un onglet dont la collecte a échoué.
    sections = [s for s in sections
                if [c for c in s['champs'] if c[1] not in (None, '', [])] or s['listes']]
    for s in sections:
        s['champs'] = [(lib, val) for lib, val in s['champs'] if val not in (None, '', [])]

    if not info.get('elevated'):
        for s in sections:
            if s['cle'] == 'identification':
                s['notes'].append(
                    "Collecte sans privilèges administrateur : SMART détaillé, TPM, "
                    "BitLocker et clé OEM peuvent manquer.")
    return sections


def build_summary_lines(info):
    """Résumé textuel du collecteur CLI, dérivé des mêmes rubriques que l'aperçu graphique."""
    lines = []
    for section in build_summary_sections(info):
        lines.append("┌─ %s" % section['titre'].upper())
        for libelle, valeur in section['champs']:
            lines.append("│ %-22s: %s" % (libelle, valeur))
        for bloc in section['listes']:
            if bloc['titre']:
                lines.append("│ %-22s:" % bloc['titre'])
            # Le texte de la console reste un résumé : les listes complètes
            # sont dans le PDF et dans les onglets de l'interface graphique.
            for element in bloc['elements'][:15]:
                lines.append("│   - %s" % element)
            reste = len(bloc['elements']) - 15
            if reste > 0:
                lines.append("│   … et %d autre(s)" % reste)
        for note in section['notes']:
            lines.append("│ ⚠ %s" % note)
        lines.append("└")
        lines.append("")
    return lines


# ════════════════════════════════════════════════════════════════════════════
# RAPPORTS
# ════════════════════════════════════════════════════════════════════════════

def _report_filename(info, extension):
    """Nom de fichier normalisé : system-info-report_HOST_MAC_HORODATAGE.ext."""
    timestamp = _utcnow().strftime("%Y%m%d_%H%M%S")
    hostname = info.get('hostname', 'unknown')
    mac = (info.get('mac_address') or 'unknown').replace(':', '').replace('/', '')[:8]
    return f"system-info-report_{hostname}_{mac}_{timestamp}.{extension}"


PORT_CATALOG = {
    20:   ('FTP-DATA', 'FTP — canal de données', 'danger'),
    21:   ('FTP', 'FTP — transfert de fichiers en clair', 'danger'),
    22:   ('SSH', 'SSH — terminal sécurisé', 'ok'),
    23:   ('TELNET', 'Telnet — terminal NON chiffré', 'danger'),
    25:   ('SMTP', 'SMTP — serveur mail sortant', 'info'),
    53:   ('DNS', 'DNS — résolution de noms', 'info'),
    80:   ('HTTP', 'HTTP — serveur web non chiffré', 'warn'),
    110:  ('POP3', 'POP3 — messagerie en clair', 'warn'),
    135:  ('RPC', 'RPC — Windows Remote Procedure Call', 'warn'),
    137:  ('NETBIOS', 'NetBIOS — service de noms Windows', 'warn'),
    139:  ('NETBIOS', 'NetBIOS — partage de fichiers Windows', 'warn'),
    143:  ('IMAP', 'IMAP — messagerie en clair', 'warn'),
    443:  ('HTTPS', 'HTTPS — serveur web sécurisé', 'ok'),
    445:  ('SMB', 'SMB — partage de fichiers Windows', 'warn'),
    465:  ('SMTPS', 'SMTP sur TLS', 'ok'),
    587:  ('SUBMISSION', 'SMTP soumission (authentifié)', 'ok'),
    631:  ('IPP', 'IPP — service d\'impression', 'info'),
    993:  ('IMAPS', 'IMAP sur TLS', 'ok'),
    995:  ('POP3S', 'POP3 sur TLS', 'ok'),
    1433: ('MSSQL', 'Microsoft SQL Server', 'warn'),
    1521: ('ORACLE', 'Oracle Database', 'warn'),
    3306: ('MYSQL', 'MySQL / MariaDB', 'warn'),
    3389: ('RDP', 'RDP — bureau à distance Windows', 'danger'),
    5432: ('POSTGRES', 'PostgreSQL', 'warn'),
    5900: ('VNC', 'VNC — bureau à distance', 'danger'),
    5985: ('WINRM', 'WinRM — administration distante HTTP', 'warn'),
    5986: ('WINRM-S', 'WinRM — administration distante HTTPS', 'ok'),
    6379: ('REDIS', 'Redis', 'warn'),
    8080: ('HTTP-ALT', 'HTTP alternatif', 'warn'),
    8443: ('HTTPS-ALT', 'HTTPS alternatif', 'ok'),
    9100: ('JETDIRECT', 'Impression directe (JetDirect)', 'info'),
    27017: ('MONGODB', 'MongoDB', 'warn'),
}


# Plage dynamique IANA : ports attribués à la volée (endpoints RPC, sockets
# clientes). Ils changent à chaque redémarrage et n'ont aucune valeur
# d'inventaire — ils sont comptés mais pas détaillés dans les rapports.
EPHEMERAL_PORT_START = 49152


def describe_listening_port(entry):
    """Enrichit un port relevé ({port, process}) de son service et sa criticité.

    La collecte ne renvoie que le numéro et le processus propriétaire ; le nom
    du service, sa description et son niveau de sensibilité sont déduits ici,
    au moment du rendu.
    """
    port = entry.get('port')
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 0
    nom, description, niveau = describe_port(port)
    enrichi = dict(entry)
    enrichi.update({
        'port': port,
        'name': nom,
        'description': description,
        'level': niveau,
        'ephemeral': port >= EPHEMERAL_PORT_START,
    })
    return enrichi


def notable_ports(ports):
    """Ports méritant une carte : tout sauf la plage dynamique."""
    return [p for p in (ports or []) if not p.get('ephemeral')]


def describe_port(port):
    """Retourne (nom, description, niveau) pour un port, avec repli générique.

    Le nom est vide pour un port non répertorié : afficher le numéro une
    seconde fois en guise d'étiquette n'apporte rien.
    """
    if port in PORT_CATALOG:
        return PORT_CATALOG[port]
    if port >= EPHEMERAL_PORT_START:
        return ('', 'Port dynamique / éphémère', 'info')
    return ('', 'Service non répertorié', 'info')


# ANALYSE : SEUILS, ALERTES, MISE EN ÉVIDENCE
# ══════════════════════════════════════════════════════════════════════════════

# Seuils d'occupation disque au-delà desquels la barre change de couleur.
DISK_WARN_PCT = 75
DISK_DANGER_PCT = 90
BATTERY_WARN_PCT = 40
BATTERY_DANGER_PCT = 20

_LEVEL_COLORS = {
    'ok':     ('#0e9f6e', '#def7ec'),
    'info':   ('#3f83f8', '#e1effe'),
    'warn':   ('#c27803', '#fdf6b2'),
    'danger': ('#e02424', '#fde8e8'),
    'muted':  ('#6b7280', '#f3f4f6'),
}


def _num(value):
    """Convertit en float ce qui peut l'être, sinon None."""
    if value in ('', None):
        return None
    try:
        return float(str(value).replace(',', '.').split()[0])
    except (ValueError, TypeError, IndexError):
        return None


def _parse_drive(text):
    """Extrait (libellé, total, utilisé, libre) d'une ligne de disque logique.

    Le collecteur formate déjà ces lignes différemment selon l'OS ; en cas de
    format non reconnu, on retourne None et l'appelant affiche la ligne brute
    plutôt que d'inventer des chiffres.
    """
    m = re.match(
        r'^(.+?)\s*[—-]\s*([\d.,]+)\s*GB total,\s*([\d.,]+)\s*GB utilis[ée]s?,\s*([\d.,]+)\s*GB libres?',
        text or '', re.IGNORECASE)
    if m:
        return (m.group(1).strip(), _num(m.group(2)), _num(m.group(3)), _num(m.group(4)))
    m = re.match(r'^(.+?)\s*\(([\d.,]+)\s*GB\)', text or '')
    if m:
        return (m.group(1).strip(), _num(m.group(2)), None, None)
    return None


#: État SMART renvoyé par Get-PhysicalDisk, et la criticité correspondante.
#: « Warning » signale un disque qui fonctionne encore mais dont le firmware
#: a relevé des secteurs douteux : c'est l'avertissement à ne pas manquer.
_SANTE_DISQUE = {
    'healthy': ('Sain', 'ok'),
    'warning': ('À surveiller', 'warn'),
    'unhealthy': ('Défaillant', 'danger'),
    'unknown': ('Inconnu', 'muted'),
}

#: Type renvoyé par `Get-Partition.Type` (Windows) → libellé lisible. Une
#: partition système (Reserved/System/Recovery) n'a jamais de lettre de
#: lecteur : c'est ce type qui l'identifie à l'affichage, faute de lettre.
#: 'IFS' est le libellé legacy MBR pour une partition de données NTFS/exFAT
#: (équivalent GPT de 'Basic') — constaté sur un disque MBR réel.
_TYPE_PARTITION = {
    'basic': 'Données',
    'ifs': 'Données',
    'system': 'Système EFI',
    'reserved': 'Réservé (MSR)',
    'recovery': 'Récupération',
    'unknown': 'Inconnu',
}


def parse_physical_disk(text):
    """Décompose une ligne de disque physique produite par `_win_extras`.

    Format attendu : « nom — type — 931.5 GB — Santé (SMART): Healthy », avec
    un éventuel « (état opérationnel) » en fin. La ligne est reconstituée à
    partir de champs distincts ; l'afficher telle quelle obligeait à la lire en
    entier pour retrouver la capacité ou l'état.

    Retourne None si le format n'est pas reconnu — l'appelant restitue alors la
    ligne brute plutôt que d'inventer des valeurs.
    """
    m = re.match(
        r'^(?P<nom>.+?)\s*—\s*(?P<type>[^—]+?)\s*—\s*(?P<taille>[\d.,]+)\s*GB\s*—\s*'
        r'Sant[ée]\s*\(SMART\)\s*:\s*(?P<sante>[^(]+?)\s*(?:\((?P<etat>[^)]+)\))?\s*$',
        (text or '').strip())
    if not m:
        return None
    brut = m.group('sante').strip()
    libelle, niveau = _SANTE_DISQUE.get(brut.lower(), (brut, 'muted'))
    return {
        'nom': m.group('nom').strip(),
        'type': m.group('type').strip(),
        'taille_go': _num(m.group('taille')),
        'sante': libelle,
        'sante_niveau': niveau,
        'etat': (m.group('etat') or '').strip() or None,
        'brut': None,
    }


def _battery_pct(info):
    """Niveau de charge. collector_core expose un champ numérique dédié ;
    l'ancien format texte « 42% (statut: …) » reste accepté en repli."""
    if isinstance(info, dict):
        pct = info.get('battery_charge_percent')
        if pct is not None:
            try:
                return int(float(pct))
            except (TypeError, ValueError):
                return None
        info = info.get('battery')
    m = re.match(r'^(\d+)\s*%', str(info or ''))
    return int(m.group(1)) if m else None


def build_alerts(info):
    """Construit la liste des points d'attention mis en évidence en tête.

    Chaque alerte est un dict {level, titre, detail}. La liste est vide quand
    tout va bien — auquel cas le rapport affiche un encadré vert.
    """
    alerts = []

    def add(level, titre, detail=''):
        alerts.append({'level': level, 'titre': titre, 'detail': detail})

    total = _num(info.get('disk_total_gb'))
    used = _num(info.get('disk_used_gb'))
    if total and used is not None and total > 0:
        pct = round(used / total * 100)
        if pct >= DISK_DANGER_PCT:
            add('danger', f'Disque saturé — {pct} % occupés',
                f'{round(total - used, 1)} GB restants sur {total} GB')
        elif pct >= DISK_WARN_PCT:
            add('warn', f'Disque bien rempli — {pct} % occupés',
                f'{round(total - used, 1)} GB restants sur {total} GB')

    antivirus = (info.get('antivirus') or '').strip()
    if not antivirus or antivirus.lower() in ('n/a', 'aucun', 'none'):
        add('danger', 'Aucun antivirus détecté')

    if info.get('tpm_present') is False:
        add('warn', 'Aucune puce TPM', 'Bloque la mise à niveau vers Windows 11')
    elif info.get('tpm_present') and not info.get('tpm_enabled'):
        add('warn', 'TPM présent mais désactivé')

    if info.get('secure_boot') is False:
        add('warn', 'Secure Boot désactivé')

    firewall_off = [p for p in info.get('firewall', []) if re.search(r'(désactiv|disabled|off)', p, re.I)]
    if firewall_off:
        add('danger', 'Pare-feu désactivé', ' · '.join(firewall_off[:3]))

    # L'état vient désormais de la forme structurée : chercher « non chiffré »
    # dans une phrase revenait à interpréter un texte qu'on produit soi-même,
    # et laissait passer les libellés renvoyés en anglais par Windows.
    if info.get('bitlocker_volumes'):
        non_proteges = [v['volume'] for v in info['bitlocker_volumes'] if not v.get('protege')]
        if non_proteges and not info.get('bitlocker_actif'):
            add('warn', 'Aucun volume chiffré',
                'BitLocker désactivé sur %s — les données du disque sont lisibles '
                'si la machine est volée' % ', '.join(non_proteges[:6]))
        elif non_proteges:
            add('warn', 'Volume non chiffré', ' · '.join(non_proteges[:6]))
    else:
        bitlocker_off = [v for v in info.get('bitlocker', [])
                         if re.search(r'(non chiffr|not encrypted|off|déciffr)', v, re.I)]
        if bitlocker_off:
            add('warn', 'Volume non chiffré', ' · '.join(bitlocker_off[:3]))

    battery = _battery_pct(info)
    if battery is not None and battery <= BATTERY_DANGER_PCT:
        add('danger', f'Batterie très faible — {battery} %')

    usure = _num(info.get('battery_wear_percent'))
    if usure is not None and usure >= 30:
        add('warn', 'Batterie usée — %g %% de capacité perdue' % usure,
            info.get('battery_health_status') or '')

    # Ports exposés jugés sensibles : c'est la mise en évidence la plus utile
    # d'un rapport de parc.
    risky = [p for p in info.get('listening_ports', []) if p.get('level') == 'danger']
    if risky:
        add('danger', f'{len(risky)} port(s) sensible(s) en écoute',
            ' · '.join(f"{p['port']} ({p['name']})" for p in risky[:5]))

    for lic in info.get('licenses', []):
        # « Non active » désigne une clé récupérée hors licence en service, pas
        # un défaut d'activation : ce n'est pas un point d'attention.
        if lic.get('status') == 'Non active':
            continue
        if lic.get('activated') is False:
            add('warn', "Licence non activée — %s" % lic.get('name', ''),
                lic.get('status') or '')

    uptime = _num(info.get('uptime_hours'))
    if uptime and uptime > 24 * 30:
        add('info', f'Machine non redémarrée depuis {round(uptime / 24)} jours')

    # ── Signaux de comportement, pas de configuration ──────────────────────
    arrets = info.get('unexpected_shutdowns') or 0
    if arrets:
        add('danger', '%d arrêt(s) inattendu(s) sur %d jours' % (arrets, EVENT_WINDOW_DAYS),
            'Coupure secteur, surchauffe ou plantage — à corréler avec les écrans bleus')

    support = info.get('windows_support') or {}
    if support.get('termine'):
        add('danger', 'Version de Windows sans support depuis le %s'
            % support.get('fin_de_support'),
            '%s ne reçoit plus de correctifs de sécurité' % support.get('version', ''))
    elif support.get('jours_restants') is not None and support['jours_restants'] < 180:
        add('warn', 'Fin de support de Windows le %s' % support.get('fin_de_support'),
            '%s — %d jours pour planifier la montée de version'
            % (support.get('version', ''), support['jours_restants']))

    for certificat in (info.get('certificates_expiring') or [])[:3]:
        if certificat.get('expire'):
            add('danger', 'Certificat expiré : %s' % certificat.get('sujet', '?'),
                'Depuis le %s — VPN, bureau à distance ou 802.1X peuvent être hors service'
                % certificat.get('expire_le', '?'))
        elif certificat.get('jours_restants', 999) <= 30:
            add('warn', 'Certificat à renouveler : %s' % certificat.get('sujet', '?'),
                'Expire le %s, dans %d jours'
                % (certificat.get('expire_le', '?'), certificat.get('jours_restants', 0)))

    verrouillages = info.get('account_lockouts') or 0
    if verrouillages:
        comptes = ', '.join(sorted({e.get('compte', '?') for e in info.get('security_events') or []
                                    if e.get('event_id') == 4740}))
        add('warn', '%d verrouillage(s) de compte sur %d jours'
            % (verrouillages, EVENT_WINDOW_DAYS),
            'Souvent un service resté sur un ancien mot de passe — %s' % comptes[:100])

    echecs = info.get('failed_logons') or 0
    if echecs >= 20:
        add('warn', "%d échecs d'ouverture de session sur %d jours"
            % (echecs, EVENT_WINDOW_DAYS),
            'À rapprocher des sources listées dans le journal de sécurité')

    lent = info.get('boot_last_seconds')
    if lent and lent >= 120:
        add('warn', 'Démarrage long : %s secondes' % lent,
            'Moyenne %s s — voir les programmes lancés au démarrage'
            % info.get('boot_average_seconds', '?'))

    # Un pilote manquant ou un matériel arrêté ne se voit nulle part ailleurs :
    # le reste du rapport décrit ce qui est présent, pas ce qui fonctionne mal.
    en_panne = info.get('problem_devices_count') or 0
    if en_panne:
        exemples = ' · '.join(
            p.get('name', '?') for p in (info.get('problem_devices') or [])
            if isinstance(p, dict) and p.get('code') not in (22, 45))
        add('warn', '%d périphérique(s) en erreur' % en_panne, exemples[:140])

    erreurs_disque = info.get('disk_error_events') or 0
    if erreurs_disque:
        detail = next((i['message'] for i in info.get('system_incidents', [])
                       if i.get('category') == 'Erreur disque'), '')
        add('danger', '%d erreur(s) disque signalée(s) par Windows' % erreurs_disque,
            detail[:110])

    maj_secu = info.get('pending_updates_security') or 0
    if maj_secu:
        add('warn', '%d mise(s) à jour de sécurité en attente' % maj_secu)
    elif info.get('pending_updates'):
        add('info', '%d mise(s) à jour en attente' % len(info['pending_updates']))

    obsoletes = [s for s in (info.get('installed_software') or [])
                 if isinstance(s, dict) and s.get('update_status') == 'obsolete']
    if obsoletes:
        add('warn', '%d logiciel(s) avec une mise à jour disponible' % len(obsoletes),
            ' · '.join('%s → v%s' % (s.get('name', '?'), s.get('latest_version', '?'))
                       for s in obsoletes[:5]))

    age = hardware_age_years(info.get('bios_release_date'))
    if age is not None and age >= 6:
        add('info', 'Matériel ancien — BIOS daté de %g an(s)' % age,
            "Repère de renouvellement, pas une date d'achat")

    # Un compte administrateur dont le mot de passe n'expire jamais est le
    # défaut d'hygiène qui compte vraiment ; le signaler compte par compte
    # noierait la liste.
    admins_sans_expiration = [u['name'] for u in info.get('users_details', [])
                              if u.get('admin') and u.get('enabled')
                              and u.get('password_never_expires')]
    if admins_sans_expiration:
        add('warn', '%d compte(s) administrateur à mot de passe sans expiration'
            % len(admins_sans_expiration), ' · '.join(admins_sans_expiration[:5]))

    # ── Réseau et hygiène ──────────────────────────────────────────────────
    for mesure in info.get('latency', []):
        if mesure.get('loss_pct', 0) >= 50:
            add('danger', 'Perte de paquets vers %s' % mesure['role'].lower(),
                '%d %% de perte vers %s' % (mesure['loss_pct'], mesure['target']))
        elif mesure['role'] == 'Passerelle' and (mesure.get('avg_ms') or 0) > 20:
            # Une passerelle locale répond en 1 à 2 ms ; au-delà, le lien est en
            # cause (Wi-Fi faible, câble abîmé, switch saturé) et non la
            # connexion Internet.
            add('warn', 'Latence anormale vers la passerelle',
                '%g ms de moyenne — lien local dégradé' % mesure['avg_ms'])

    proxy = info.get('proxy') or {}
    if proxy.get('server') and not proxy.get('enabled'):
        add('info', 'Proxy configuré mais désactivé', proxy['server'])

    if info.get('rdp_enabled') and info.get('rdp_nla') is False:
        add('warn', 'Bureau à distance activé sans NLA',
            'Sans authentification au niveau réseau, le service est exposé '
            'avant toute authentification')

    if info.get('uac_enabled') is False:
        add('warn', "Contrôle de compte d'utilisateur (UAC) désactivé")

    if 'restore_points' in info and not info['restore_points']:
        add('info', 'Aucun point de restauration',
            'Impossible de revenir en arrière après une mise à jour ratée')

    temp_mb = _num(info.get('temp_files_mb'))
    if temp_mb and temp_mb >= 1024:
        add('info', 'Fichiers temporaires — %.1f Go récupérables' % (temp_mb / 1024))

    taches_ko = [t['name'] for t in info.get('scheduled_tasks', []) if t.get('failed')]
    if taches_ko:
        add('warn', '%d tâche(s) planifiée(s) en échec' % len(taches_ko),
            ' · '.join(taches_ko[:4]))

    partages = [s['name'] for s in info.get('smb_shares', []) if not s.get('administrative')]
    if partages:
        add('info', '%d partage(s) réseau exposé(s)' % len(partages),
            ' · '.join(partages[:6]))

    return alerts


# ══════════════════════════════════════════════════════════════════════════════
# RAPPORT HTML (« fiche système »)
# ══════════════════════════════════════════════════════════════════════════════

def _esc(value):
    """Échappe le HTML — les libellés viennent du système, pas de nous."""
    import html as _html
    return _html.escape('' if value is None else str(value), quote=True)


def _report_filename(info, extension):
    timestamp = _utcnow().strftime('%Y%m%d_%H%M%S')
    hostname = re.sub(r'[^A-Za-z0-9_.-]', '_', info.get('hostname') or 'unknown')
    mac = re.sub(r'[^0-9A-Fa-f]', '', info.get('mac_address') or '')[:8] or 'nomac'
    return f'system-info-report_{hostname}_{mac}_{timestamp}.{extension}'


def _bar_html(pct, level='info'):
    """Barre de progression : un div coloré dont la largeur porte l'information."""
    pct = max(0, min(100, int(round(pct))))
    color = _LEVEL_COLORS.get(level, _LEVEL_COLORS['info'])[0]
    return (f'<div class="bar"><div class="bar-fill" '
            f'style="width:{pct}%;background:{color}"></div></div>')


def _pill_html(text, level='info'):
    fg, bg = _LEVEL_COLORS.get(level, _LEVEL_COLORS['info'])
    return f'<span class="pill" style="color:{fg};background:{bg}">{_esc(text)}</span>'


def _disk_level(pct):
    if pct >= DISK_DANGER_PCT:
        return 'danger'
    if pct >= DISK_WARN_PCT:
        return 'warn'
    return 'ok'


def _kpi_cards_html(info):
    """Bandeau de vignettes chiffrées avec barres — le coup d'œil d'ouverture."""
    cards = []

    total = _num(info.get('disk_total_gb'))
    used = _num(info.get('disk_used_gb'))
    if total and used is not None and total > 0:
        pct = round(used / total * 100)
        cards.append(
            '<div class="kpi">'
            '<div class="kpi-head"><span class="kpi-icon">HDD</span> Stockage</div>'
            f'<div class="kpi-value">{pct}<span class="kpi-unit">%</span></div>'
            f'{_bar_html(pct, _disk_level(pct))}'
            f'<div class="kpi-sub">{used:g} GB utilisés sur {total:g} GB</div>'
            '</div>')

    ram = _num(info.get('ram_gb'))
    if ram:
        libre = _num(info.get('ram_free_gb'))
        if libre is not None and ram > 0:
            pct = round((ram - libre) / ram * 100)
            niveau = 'danger' if pct >= 90 else 'warn' if pct >= 75 else 'ok'
            sous_titre = '%.1f GB libres sur %g GB' % (libre, ram)
        else:
            # Pas de mesure d'occupation : situer la machine sur une échelle
            # 0–64 Go plutôt qu'afficher un pourcentage qui n'existe pas.
            pct = min(ram / 64 * 100, 100)
            niveau = 'ok' if ram >= 8 else 'warn'
            sous_titre = 'Confortable' if ram >= 16 else 'Correct' if ram >= 8 else 'Juste'
        cards.append(
            '<div class="kpi">'
            '<div class="kpi-head"><span class="kpi-icon">RAM</span> Mémoire vive</div>'
            f'<div class="kpi-value">{ram:g}<span class="kpi-unit">GB</span></div>'
            f'{_bar_html(pct, niveau)}'
            f'<div class="kpi-sub">{sous_titre}</div>'
            '</div>')

    battery = _battery_pct(info)
    if battery is not None:
        level = ('danger' if battery <= BATTERY_DANGER_PCT
                 else 'warn' if battery <= BATTERY_WARN_PCT else 'ok')
        cards.append(
            '<div class="kpi">'
            '<div class="kpi-head"><span class="kpi-icon">BAT</span> Batterie</div>'
            f'<div class="kpi-value">{battery}<span class="kpi-unit">%</span></div>'
            f'{_bar_html(battery, level)}'
            f'<div class="kpi-sub">{_esc(info.get("battery"))}</div>'
            '</div>')

    uptime = _num(info.get('uptime_hours'))
    if uptime is not None:
        days = uptime / 24
        cards.append(
            '<div class="kpi">'
            '<div class="kpi-head"><span class="kpi-icon">UP</span> Sans redémarrage</div>'
            f'<div class="kpi-value">{days:.0f}<span class="kpi-unit">j</span></div>'
            f'{_bar_html(min(days / 30 * 100, 100), "warn" if days > 30 else "ok")}'
            f'<div class="kpi-sub">{uptime:.0f} heures d\'uptime</div>'
            '</div>')

    return f'<div class="kpi-row">{"".join(cards)}</div>' if cards else ''


def _alerts_html(alerts):
    if not alerts:
        fg, bg = _LEVEL_COLORS['ok']
        return (f'<div class="alert" style="border-left-color:{fg};background:{bg}">'
                '<div><strong>Aucun point d\'attention détecté</strong>'
                '<div class="alert-detail">Sécurité, stockage et licences dans les '
                'seuils attendus.</div></div></div>')
    rows = []
    for a in alerts:
        fg, bg = _LEVEL_COLORS.get(a['level'], _LEVEL_COLORS['info'])
        detail = f'<div class="alert-detail">{_esc(a["detail"])}</div>' if a.get('detail') else ''
        rows.append(
            f'<div class="alert" style="border-left-color:{fg};background:{bg}">'
            f'<div><strong>{_esc(a["titre"])}</strong>{detail}</div></div>')
    return ''.join(rows)


def _ports_cards_html(ports):
    """Ports à l'écoute affichés en cartes plutôt qu'en simple liste."""
    if not ports:
        return '<p class="empty">Aucun port TCP en écoute détecté.</p>'
    ports = [describe_listening_port(p) for p in ports]
    shown = notable_ports(ports)
    hidden = len(ports) - len(shown)
    if not shown:
        return (f'<p class="empty">Aucun port de service en écoute — '
                f'{hidden} port(s) dynamique(s) uniquement.</p>')
    cards = []
    for p in shown:
        fg, bg = _LEVEL_COLORS.get(p['level'], _LEVEL_COLORS['info'])
        process = f'<div class="port-proc">{_esc(p["process"])}</div>' if p.get('process') else ''
        # Étiquette omise pour un port non répertorié : le numéro suffit.
        badge = (f'<div class="port-name" style="background:{bg};color:{fg}">'
                 f'{_esc(p["name"])}</div>') if p.get('name') else '<div class="port-gap"></div>'
        cards.append(
            f'<div class="port-card" style="border-top-color:{fg}">'
            f'<div class="port-num" style="color:{fg}">{p["port"]}</div>'
            f'{badge}'
            f'<div class="port-desc">{_esc(p["description"])}</div>'
            f'{process}</div>')
    note = (f'<p class="hint">{hidden} port(s) de la plage dynamique '
            f'({EPHEMERAL_PORT_START}+) ne sont pas détaillés : attribués à la volée, '
            'ils changent à chaque redémarrage.</p>') if hidden else ''
    return f'{note}<div class="port-grid">{"".join(cards)}</div>'


def _usb_html(devices):
    if not devices:
        return '<p class="empty">Aucun périphérique USB détecté.</p>'
    rows = []
    for d in devices:
        badge = (_pill_html('Inventorié', 'ok') if d.get('inventoriable')
                 else _pill_html('Interne', 'muted'))
        details = []
        if d.get('serial'):
            details.append(f'N° série {_esc(d["serial"])}')
        if d.get('vid'):
            details.append(f'{_esc(d["vid"])}:{_esc(d["pid"])}')
        if d.get('manufacturer'):
            details.append(_esc(d['manufacturer']))
        rows.append(
            '<tr>'
            f'<td><strong>{_esc(d["name"])}</strong>'
            f'<div class="meta">{" · ".join(details) or "—"}</div></td>'
            f'<td>{_esc(d["categorie"])}</td>'
            f'<td>{badge}</td></tr>')
    inventories = sum(1 for d in devices if d.get('inventoriable'))
    return (
        f'<p class="hint">{len(devices)} périphérique(s) détecté(s), dont {inventories} '
        'repris dans l\'inventaire ParcInfo. Les éléments « Interne » '
        '(concentrateurs, contrôleurs, nœuds composites) sont listés pour '
        'information mais ne sont pas créés comme périphériques.</p>'
        '<table class="tbl"><thead><tr><th>Périphérique</th><th>Catégorie</th>'
        f'<th>Inventaire</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')


def _licenses_html(licences):
    if not licences:
        return '<p class="empty">Aucune licence détectée.</p>'
    rows = []
    for lic in licences:
        ok = lic.get('activated')
        if lic.get('full_key'):
            # Clé affichée en entier, sans troncature ni masquage.
            cle_html = f'<code class="licence-key">{_esc(lic["full_key"])}</code>'
            if lic.get('key_verified'):
                cle_html += ('<div class="licence-check">Vérifiée : les 5 derniers '
                             'caractères correspondent à la licence active.</div>')
            else:
                cle_html += ('<div class="licence-warn">Non appairée à une licence '
                             'active de cette machine.</div>')
        elif lic.get('partial_key'):
            cle_html = ('<span class="licence-partial">Clé complète absente de la machine '
                        '(licence numérique, Click-to-Run ou KMS) — se termine par '
                        f'<code>{_esc(lic["partial_key"])}</code></span>')
        else:
            cle_html = '<span class="licence-partial">Aucune clé exposée</span>'
        # Expression sortie de la f-string : les guillemets imbriqués de même
        # type n'y sont légaux qu'à partir de Python 3.12, or le projet cible
        # 3.8+ et l'image Docker tourne en 3.11.
        source = lic.get('key_source') or lic.get('channel') or 'Windows Licensing'
        rows.append(
            f'<tr><td><strong>{_esc(lic.get("name"))}</strong>'
            f'<div class="meta">Microsoft · {_esc(source)}</div></td>'
            f'<td>{cle_html}</td>'
            f'<td>{_pill_html(lic.get("status") or "Inconnu", "ok" if ok else "warn")}</td></tr>')
    return ('<table class="tbl"><thead><tr><th>Produit</th><th>Clé de licence</th>'
            f'<th>État</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')


def _disks_html(info):
    drives = info.get('disk_drives', [])
    if not drives:
        return ''
    blocks = []
    for raw in drives:
        parsed = _parse_drive(raw)
        if parsed and parsed[1] and parsed[2] is not None:
            label, total, used, _free = parsed
            pct = round(used / total * 100) if total else 0
            blocks.append(
                '<div class="disk-row">'
                f'<div class="disk-head"><strong>{_esc(label)}</strong>'
                f'<span>{used:g} / {total:g} GB — {pct} %</span></div>'
                f'{_bar_html(pct, _disk_level(pct))}</div>')
        else:
            # Format non reconnu (macOS/Linux) : afficher la ligne telle quelle
            # plutôt que de fabriquer un pourcentage.
            blocks.append(f'<div class="disk-row"><strong>{_esc(raw)}</strong></div>')
    return ''.join(blocks)


def _security_html(info):
    items = []
    antivirus = (info.get('antivirus') or '').strip()
    items.append(('Antivirus', antivirus or 'Aucun détecté',
                  'ok' if antivirus and antivirus.lower() not in ('n/a', 'aucun') else 'danger'))

    if info.get('tpm_present') is not None:
        if info.get('tpm_enabled'):
            items.append(('TPM', 'Présent et activé', 'ok'))
        elif info.get('tpm_present'):
            items.append(('TPM', 'Présent mais désactivé', 'warn'))
        else:
            items.append(('TPM', 'Absent', 'warn'))

    if info.get('secure_boot') is not None:
        items.append(('Secure Boot', 'Activé' if info.get('secure_boot') else 'Désactivé',
                      'ok' if info.get('secure_boot') else 'warn'))

    if info.get('sip_status'):
        items.append(("Protection de l'intégrité système (SIP)", info['sip_status'],
                      'ok' if info['sip_status'] == 'Activée' else 'warn'))
    if info.get('gatekeeper_status'):
        items.append(('Gatekeeper', info['gatekeeper_status'],
                      'ok' if info['gatekeeper_status'] == 'Activé' else 'warn'))
    if info.get('mdm_enrolled') is not None:
        items.append(('Inscription MDM', info.get('mdm_detail') or
                      ('Oui' if info['mdm_enrolled'] else 'Non'), 'ok'))

    for profile in info.get('firewall', []):
        level = 'danger' if re.search(r'(désactiv|disabled|off)', profile, re.I) else 'ok'
        items.append(('Pare-feu', profile, level))

    if info.get('bitlocker_volumes'):
        items.append(('BitLocker', 'Activé' if info.get('bitlocker_actif') else 'Désactivé',
                      'ok' if info.get('bitlocker_actif') else 'warn'))
    else:
        for vol in (info.get('bitlocker') or []):
            level = 'warn' if re.search(r'(non chiffr|not encrypted|off)', vol, re.I) else 'ok'
            items.append(('BitLocker', vol, level))

    if info.get('last_windows_update'):
        items.append(('Dernière mise à jour', info['last_windows_update'], 'info'))

    cells = ''.join(
        f'<div class="sec-item"><div class="sec-label">{_esc(label)}</div>'
        f'<div>{_pill_html(value, level)}</div></div>'
        for label, value, level in items)
    return f'<div class="sec-grid">{cells}</div>' if items else ''


def _kv_table_html(rows):
    body = ''.join(f'<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>'
                   for k, v in rows if v not in ('', None))
    return f'<table class="tbl kv"><tbody>{body}</tbody></table>' if body else ''


def _software_html(software):
    if not software:
        return '<p class="empty">Aucun logiciel détecté.</p>'
    rows = []
    for i, soft in enumerate(software, 1):
        if isinstance(soft, dict):
            name = soft.get('name', '')
            version = soft.get('version', '')
            publisher = soft.get('publisher', '')
            install = soft.get('install_date', '')
            if soft.get('update_status') == 'obsolete':
                maj = _pill_html('v%s disponible' % (soft.get('latest_version') or '?'), 'warn')
            elif soft.get('update_status') == 'a_jour':
                maj = _pill_html('À jour', 'ok')
            else:
                maj = '<span class="empty">—</span>'
        else:
            name, version, publisher, install = str(soft), '', '', ''
            maj = '<span class="empty">—</span>'
        rows.append(
            f'<tr><td class="idx">{i}</td><td>{_esc(name)}</td>'
            f'<td class="mono">{_esc(version)}</td><td>{_esc(publisher)}</td>'
            f'<td class="mono">{_esc(install)}</td><td>{maj}</td></tr>')
    return ('<div class="scroll"><table class="tbl"><thead><tr><th>#</th><th>Nom</th>'
            '<th>Version</th><th>Éditeur</th><th>Installé le</th><th>Mise à jour</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _list_section_html(title, items):
    """Section simple en liste — omise entièrement si la donnée est absente."""
    if not items:
        return ''
    lis = ''.join(f'<li>{_esc(i)}</li>' for i in items)
    return (f'<div class="section"><h2>{_esc(title)}'
            f'<span class="count">{len(items)}</span></h2>'
            f'<ul class="plain">{lis}</ul></div>')


_REPORT_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
 background:#eef1f5;color:#1f2937;line-height:1.5;padding:24px 16px}
.wrap{max-width:1100px;margin:0 auto}
.card{background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);
 overflow:hidden;margin-bottom:20px}
.hero{background:linear-gradient(135deg,#1e3a5f 0%,#2c5282 55%,#2b6cb0 100%);
 color:#fff;padding:26px 28px}
.hero h1{font-size:1.55rem;font-weight:700;letter-spacing:-.01em;margin-bottom:4px}
.hero .sub{opacity:.85;font-size:.86rem}
.hero-meta{display:flex;flex-wrap:wrap;gap:22px;margin-top:16px;
 padding-top:16px;border-top:1px solid rgba(255,255,255,.18)}
.hero-meta div{font-size:.8rem}
.hero-meta .lbl{opacity:.7;text-transform:uppercase;letter-spacing:.06em;font-size:.66rem}
.hero-meta .val{font-weight:600;font-size:.92rem;margin-top:2px}
.section{padding:22px 28px;border-top:1px solid #eef1f5}
.section:first-child{border-top:none}
h2{font-size:1rem;font-weight:700;color:#1e3a5f;margin-bottom:14px;
 display:flex;align-items:center;gap:8px}
h2 .count{background:#eef1f5;color:#6b7280;border-radius:20px;padding:1px 9px;
 font-size:.72rem;font-weight:600}
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
.kpi{background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px}
.kpi-head{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;
 color:#6b7280;font-weight:700;display:flex;align-items:center;gap:6px}
.kpi-icon{background:#1e3a5f;color:#fff;border-radius:4px;padding:1px 5px;
 font-size:.6rem;letter-spacing:.04em}
.kpi-value{font-size:2rem;font-weight:700;color:#1f2937;line-height:1.1;margin:6px 0 2px}
.kpi-unit{font-size:.9rem;font-weight:600;color:#6b7280;margin-left:3px}
.kpi-sub{font-size:.75rem;color:#6b7280;margin-top:6px}
.bar{height:7px;background:#e5e7eb;border-radius:4px;overflow:hidden;margin-top:4px}
.bar-fill{height:100%;border-radius:4px}
.alert{display:flex;gap:12px;padding:11px 15px;border-left:4px solid;
 border-radius:6px;margin-bottom:8px;font-size:.86rem}
.alert-detail{color:#4b5563;font-size:.79rem;margin-top:2px}
.pill{display:inline-block;padding:2px 10px;border-radius:20px;
 font-size:.75rem;font-weight:600}
.port-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:12px}
.port-card{background:#fff;border:1px solid #e5e7eb;border-top:3px solid;
 border-radius:8px;padding:12px 13px}
.port-num{font-size:1.4rem;font-weight:700;font-family:ui-monospace,'Consolas',monospace;
 line-height:1.1}
.port-name{display:inline-block;padding:1px 7px;border-radius:4px;font-size:.68rem;
 font-weight:700;letter-spacing:.03em;margin:5px 0 6px}
.port-gap{height:9px}
.port-desc{font-size:.74rem;color:#4b5563;line-height:1.35}
.port-proc{font-size:.7rem;color:#6b7280;margin-top:6px;padding-top:5px;
 border-top:1px dashed #e5e7eb;font-family:ui-monospace,'Consolas',monospace}
.sec-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:12px}
.sec-item{background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:10px 13px}
.sec-label{font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;
 color:#6b7280;font-weight:700;margin-bottom:5px}
.tbl{width:100%;border-collapse:collapse;font-size:.84rem}
.tbl th,.tbl td{text-align:left;padding:8px 11px;border-bottom:1px solid #eef1f5;
 vertical-align:top}
.tbl thead th{background:#f9fafb;font-size:.71rem;text-transform:uppercase;
 letter-spacing:.05em;color:#6b7280;font-weight:700}
.tbl.kv th{width:230px;color:#6b7280;font-weight:600;background:#f9fafb}
.tbl tbody tr:last-child td{border-bottom:none}
.meta{font-size:.73rem;color:#6b7280;margin-top:2px}
.idx{color:#9ca3af;font-size:.75rem;width:44px}
.mono,.licence-key{font-family:ui-monospace,'Consolas',monospace}
.licence-key{background:#1e3a5f;color:#fff;padding:4px 10px;border-radius:5px;
 font-size:.86rem;letter-spacing:.06em;display:inline-block;font-weight:600}
.licence-partial{font-size:.78rem;color:#6b7280}
.licence-check{font-size:.71rem;color:#0e9f6e;margin-top:4px;font-weight:600}
.licence-warn{font-size:.71rem;color:#c27803;margin-top:4px;font-weight:600}
.licence-partial code{background:#f3f4f6;padding:1px 5px;border-radius:3px;
 font-weight:600;color:#1f2937}
.disk-row{margin-bottom:13px}
.disk-head{display:flex;justify-content:space-between;font-size:.82rem;margin-bottom:4px}
.disk-head span{color:#6b7280;font-size:.78rem}
ul.plain{list-style:none;display:grid;
 grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:5px}
ul.plain li{background:#f9fafb;border-radius:6px;padding:7px 11px;font-size:.81rem}
.scroll{max-height:460px;overflow-y:auto;border:1px solid #eef1f5;border-radius:8px}
.empty,.hint{color:#6b7280;font-size:.82rem;font-style:italic}
.hint{margin-bottom:11px;font-style:normal}
.foot{padding:16px 28px;background:#f9fafb;color:#6b7280;font-size:.75rem}
@media print{
 body{background:#fff;padding:0}
 .card{box-shadow:none;border:1px solid #e5e7eb}
 .scroll{max-height:none;overflow:visible}
 .port-card,.kpi,.sec-item{break-inside:avoid}
}
"""


def generate_html_report(info, client_id=None, client_name=None):
    """Génère la fiche système HTML.

    Retourne (contenu, chemin_fichier). Le fichier est écrit à côté de
    l'exécutable ; en cas d'échec d'écriture le contenu est tout de même
    retourné pour que l'envoi vers ParcInfo reste possible.
    """
    filename = _report_filename(info, 'html')
    alerts = build_alerts(info)

    uptime = _num(info.get('uptime_hours'))
    bios = info.get('bios_version', '')
    if bios and info.get('bios_release_date'):
        bios = f"{bios} ({info['bios_release_date']})"

    identification = _kv_table_html([
        ('Nom de machine', info.get('hostname')),
        ('Adresse MAC', info.get('mac_address')),
        ('Adresse(s) IP', ', '.join(info.get('ip_addresses', []))),
        ('Marque', info.get('brand')),
        ('Modèle', info.get('model')),
        ('Numéro de série', info.get('serial_number')),
        ('Domaine / Groupe de travail', info.get('domain') or info.get('workgroup')),
    ])

    systeme = _kv_table_html([
        ("Système d'exploitation", info.get('os_name')),
        ('Version', info.get('os_version')),
        ('Détail plateforme', info.get('platform')),
        ('BIOS', bios),
        ('Processeur', info.get('cpu')),
        ('Cœurs', info.get('cpu_cores')),
        ('Carte graphique', info.get('gpu')),
        ('Uptime', f"{uptime / 24:.1f} jour(s)" if uptime is not None else None),
    ])

    ports = info.get('listening_ports', [])
    usb = info.get('usb_devices', [])
    licences = info.get('licenses', [])
    software = info.get('installed_software', [])

    sections = [
        f'<div class="section"><h2>Points d\'attention</h2>{_alerts_html(alerts)}</div>',
        f'<div class="section"><h2>Vue d\'ensemble</h2>{_kpi_cards_html(info)}</div>',
        f'<div class="section"><h2>Identification</h2>{identification}</div>',
        f'<div class="section"><h2>Système &amp; matériel</h2>{systeme}</div>',
    ]

    security = _security_html(info)
    if security:
        sections.append(f'<div class="section"><h2>Sécurité &amp; conformité</h2>{security}</div>')

    disks = _disks_html(info)
    if disks:
        sections.append(f'<div class="section"><h2>Disques logiques</h2>{disks}</div>')

    sections.append(
        '<div class="section"><h2>Ports en écoute'
        f'<span class="count">{len(ports)}</span></h2>{_ports_cards_html(ports)}</div>')
    sections.append(
        '<div class="section"><h2>Périphériques USB'
        f'<span class="count">{len(usb)}</span></h2>{_usb_html(usb)}</div>')
    sections.append(
        '<div class="section"><h2>Licences'
        f'<span class="count">{len(licences)}</span></h2>{_licenses_html(licences)}</div>')

    sections.append(_list_section_html(
        'Barrettes mémoire',
        [format_memory_module(m) for m in info.get('memory_modules', [])]))
    sections.append(_list_section_html('Disques physiques', info.get('physical_disks', [])))
    sections.append(_list_section_html(
        'Fiabilité des disques',
        [format_reliability(r) for r in info.get('disk_reliability', [])]))
    sections.append(_list_section_html(
        'Écrans', [format_monitor(m) for m in info.get('monitors', [])]))
    sections.append(_list_section_html(
        'Imprimantes', [format_printer(p) for p in info.get('printers', [])]))
    sections.append(_list_section_html('Adaptateurs réseau', info.get('network_adapters', [])))
    sections.append(_list_section_html('Comptes utilisateurs locaux', info.get('users', [])))

    sections.append(
        '<div class="section"><h2>Logiciels installés'
        f'<span class="count">{len(software)}</span></h2>{_software_html(software)}</div>')

    generated = _utcnow().strftime('%d/%m/%Y à %H:%M:%S UTC')
    cible = client_name or (f'ID {client_id}' if client_id else 'non spécifié')

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fiche système — {_esc(info.get('hostname') or 'machine')}</title>
<style>{_REPORT_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="hero">
      <h1>{_esc(info.get('hostname') or 'Machine inconnue')}</h1>
      <div class="sub">{_esc(info.get('brand'))} {_esc(info.get('model'))} — fiche générée le {generated}</div>
      <div class="hero-meta">
        <div><div class="lbl">Client</div><div class="val">{_esc(cible)}</div></div>
        <div><div class="lbl">Système</div><div class="val">{_esc(info.get('os_name') or '—')}</div></div>
        <div><div class="lbl">Adresse IP</div><div class="val">{_esc((info.get('ip_addresses') or ['—'])[0])}</div></div>
        <div><div class="lbl">N° de série</div><div class="val">{_esc(info.get('serial_number') or '—')}</div></div>
      </div>
    </div>
    {''.join(sections)}
    <div class="foot">
      Rapport produit par system-info-collector · collecte du
      {_esc(info.get('timestamp') or 'N/A')} · {len(software)} logiciel(s),
      {len(usb)} périphérique(s) USB, {len(ports)} port(s) en écoute.
    </div>
  </div>
</div>
</body>
</html>"""

    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(html)
        return html, filename
    except Exception:
        return html, None


# ══════════════════════════════════════════════════════════════════════════════
# RAPPORT PDF
# ══════════════════════════════════════════════════════════════════════════════
#
# Les polices standard de reportlab (Helvetica) ne savent pas rendre les emoji :
# les pictogrammes du rapport précédent sortaient en carrés vides. Les éléments
# graphiques sont donc dessinés en vectoriel (barres, pastilles, cartes) plutôt
# que posés en caractères Unicode.

def _pdf_escape(value):
    """Échappe les entités XML interprétées par les Paragraph de reportlab."""
    import html as _html
    return _html.escape('' if value is None else str(value), quote=False)


def _build_pdf_toolkit():
    """Importe reportlab et construit styles et flowables personnalisés.

    Retourne None si reportlab est absent — l'appelant bascule alors sur le
    rapport HTML.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            CondPageBreak, Flowable, KeepTogether, PageBreak, Paragraph,
            SimpleDocTemplate, Spacer, Table, TableStyle,
        )
        from reportlab.platypus.tableofcontents import TableOfContents
    except ImportError:
        return None

    class ProgressBar(Flowable):
        """Barre de progression vectorielle (largeur = valeur)."""

        def __init__(self, pct, color, width=150, height=6):
            Flowable.__init__(self)
            self.pct = max(0, min(100, float(pct)))
            self.color = color
            self.width = width
            self.height = height

        def wrap(self, availWidth, availHeight):
            self.width = min(self.width, availWidth)
            return (self.width, self.height)

        def draw(self):
            c = self.canv
            c.setFillColor(colors.HexColor('#e5e7eb'))
            c.roundRect(0, 0, self.width, self.height, self.height / 2, stroke=0, fill=1)
            filled = self.width * self.pct / 100.0
            if filled > 0:
                c.setFillColor(colors.HexColor(self.color))
                # Un rayon supérieur à la moitié de la largeur fait planter
                # roundRect : on borne pour les valeurs très faibles.
                radius = min(self.height / 2, filled / 2)
                c.roundRect(0, 0, filled, self.height, radius, stroke=0, fill=1)

    styles = getSampleStyleSheet()
    S = {
        'title': ParagraphStyle('T', parent=styles['Heading1'], fontSize=19,
                                textColor=colors.white, spaceAfter=2, leading=23),
        'subtitle': ParagraphStyle('ST', parent=styles['Normal'], fontSize=8.5,
                                   textColor=colors.HexColor('#c3dafe'), leading=12),
        'h2': ParagraphStyle('H2', parent=styles['Heading2'], fontSize=11.5,
                             textColor=colors.HexColor('#1e3a5f'), spaceBefore=13,
                             spaceAfter=7, leading=14),
        'body': ParagraphStyle('B', parent=styles['Normal'], fontSize=8.5, leading=11.5),
        'small': ParagraphStyle('S', parent=styles['Normal'], fontSize=7.3,
                                textColor=colors.HexColor('#6b7280'), leading=9.5),
        'kpi_num': ParagraphStyle('KN', parent=styles['Normal'], fontSize=17,
                                  leading=19, alignment=TA_LEFT),
        'kpi_lbl': ParagraphStyle('KL', parent=styles['Normal'], fontSize=6.6,
                                  textColor=colors.HexColor('#6b7280'), leading=9),
        'port_num': ParagraphStyle('PN', parent=styles['Normal'], fontSize=13,
                                   fontName='Helvetica-Bold', leading=15),
        'mono': ParagraphStyle('M', parent=styles['Normal'], fontName='Courier',
                               fontSize=8.5, leading=11),
        'key': ParagraphStyle('K', parent=styles['Normal'], fontName='Courier-Bold',
                              fontSize=9.5, textColor=colors.white, leading=13),
        'alert': ParagraphStyle('A', parent=styles['Normal'], fontSize=8.5, leading=11),
    }
    class DocumentRapport(SimpleDocTemplate):
        """Document qui alimente son sommaire au fil de la mise en page.

        Sur un rapport de plusieurs dizaines de pages, un sommaire paginé évite
        de faire défiler au jugé. reportlab ne peut le remplir qu'en deux
        passes : la première mesure les positions, la seconde les écrit.
        """

        def afterFlowable(self, flowable):
            niveau = getattr(flowable, '_niveau_sommaire', None)
            if niveau is not None:
                self.notify('TOCEntry', (niveau, flowable.getPlainText(), self.page))

    S['toc1'] = ParagraphStyle('TOC1', parent=styles['Normal'], fontSize=9,
                               leading=15, leftIndent=8,
                               textColor=colors.HexColor('#1e3a5f'))

    return {
        'colors': colors, 'A4': A4, 'mm': mm, 'Paragraph': Paragraph,
        'SimpleDocTemplate': SimpleDocTemplate, 'DocumentRapport': DocumentRapport,
        'Spacer': Spacer, 'Table': Table, 'TableStyle': TableStyle,
        'KeepTogether': KeepTogether, 'PageBreak': PageBreak,
        'CondPageBreak': CondPageBreak, 'TableOfContents': TableOfContents,
        'ProgressBar': ProgressBar, 'S': S,
    }


def _preparer_mise_en_page(tk, story):
    """Empêche les titres de rubrique de rester orphelins en bas de page.

    Plutôt que d'altérer les vingt-sept endroits qui produisent un titre, le
    récit complet est parcouru une fois juste avant le rendu : chaque titre de
    rubrique est précédé d'un saut conditionnel qui réserve sa hauteur et celle
    des premières lignes qui suivent, et se voit marqué pour le sommaire.
    """
    prepare = []
    for element in story:
        est_titre = (getattr(getattr(element, 'style', None), 'name', '') == 'H2'
                     and not getattr(element, '_sans_sommaire', False))
        if est_titre:
            # Si moins de 70 points restent sur la page, la rubrique commence
            # sur la suivante au lieu de laisser son intitulé seul en pied.
            prepare.append(tk['CondPageBreak'](70))
            element._niveau_sommaire = 0
        prepare.append(element)
    return prepare


def _pdf_kv_table(tk, rows, width):
    """Tableau clé/valeur avec en-tête de ligne grisé."""
    data = [[tk['Paragraph'](f'<b>{_pdf_escape(k)}</b>', tk['S']['body']),
             tk['Paragraph'](_pdf_escape(v), tk['S']['body'])]
            for k, v in rows if v not in ('', None)]
    if not data:
        return None
    t = tk['Table'](data, colWidths=[width * 0.34, width * 0.66])
    t.setStyle(tk['TableStyle']([
        ('BACKGROUND', (0, 0), (0, -1), tk['colors'].HexColor('#f9fafb')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('LINEBELOW', (0, 0), (-1, -2), 0.4, tk['colors'].HexColor('#eef1f5')),
        ('BOX', (0, 0), (-1, -1), 0.5, tk['colors'].HexColor('#e5e7eb')),
    ]))
    return t


def _pdf_alerts(tk, alerts, width):
    """Encadrés colorés : c'est la mise en évidence principale du rapport."""
    flows = []
    if not alerts:
        fg, bg = _LEVEL_COLORS['ok']
        t = tk['Table']([[tk['Paragraph'](
            '<b>Aucun point d\'attention détecté</b><br/>'
            '<font size="7.5" color="#4b5563">Sécurité, stockage et licences dans '
            'les seuils attendus.</font>', tk['S']['alert'])]], colWidths=[width])
        t.setStyle(tk['TableStyle']([
            ('BACKGROUND', (0, 0), (-1, -1), tk['colors'].HexColor(bg)),
            ('LINEBEFORE', (0, 0), (0, -1), 3, tk['colors'].HexColor(fg)),
            ('LEFTPADDING', (0, 0), (-1, -1), 9),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        return [t]

    for a in alerts:
        fg, bg = _LEVEL_COLORS.get(a['level'], _LEVEL_COLORS['info'])
        text = f'<b>{_pdf_escape(a["titre"])}</b>'
        if a.get('detail'):
            text += f'<br/><font size="7.5" color="#4b5563">{_pdf_escape(a["detail"])}</font>'
        t = tk['Table']([[tk['Paragraph'](text, tk['S']['alert'])]], colWidths=[width])
        t.setStyle(tk['TableStyle']([
            ('BACKGROUND', (0, 0), (-1, -1), tk['colors'].HexColor(bg)),
            ('LINEBEFORE', (0, 0), (0, -1), 3, tk['colors'].HexColor(fg)),
            ('LEFTPADDING', (0, 0), (-1, -1), 9),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        flows.append(t)
        flows.append(tk['Spacer'](1, 3))
    return flows


def _pdf_kpis(tk, info, width):
    """Vignettes chiffrées avec barre, sur une ligne."""
    cells = []

    total = _num(info.get('disk_total_gb'))
    used = _num(info.get('disk_used_gb'))
    if total and used is not None and total > 0:
        pct = round(used / total * 100)
        cells.append(('STOCKAGE', f'{pct} %', pct, _LEVEL_COLORS[_disk_level(pct)][0],
                      f'{used:g} / {total:g} GB'))

    ram = _num(info.get('ram_gb'))
    if ram:
        libre = _num(info.get('ram_free_gb'))
        if libre is not None and ram > 0:
            pct = round((ram - libre) / ram * 100)
            niveau = 'danger' if pct >= 90 else 'warn' if pct >= 75 else 'ok'
            sous = '%.1f GB libres' % libre
        else:
            pct = min(ram / 64 * 100, 100)
            niveau = 'ok' if ram >= 8 else 'warn'
            sous = 'Confortable' if ram >= 16 else 'Correct' if ram >= 8 else 'Juste'
        cells.append(('MÉMOIRE VIVE', f'{ram:g} GB', pct, _LEVEL_COLORS[niveau][0], sous))

    battery = _battery_pct(info)
    if battery is not None:
        lvl = ('danger' if battery <= BATTERY_DANGER_PCT
               else 'warn' if battery <= BATTERY_WARN_PCT else 'ok')
        cells.append(('BATTERIE', f'{battery} %', battery, _LEVEL_COLORS[lvl][0], 'Charge restante'))

    uptime = _num(info.get('uptime_hours'))
    if uptime is not None:
        days = uptime / 24
        cells.append(('SANS REDÉMARRAGE', f'{days:.0f} j', min(days / 30 * 100, 100),
                      _LEVEL_COLORS['warn' if days > 30 else 'ok'][0], f'{uptime:.0f} heures'))

    if not cells:
        return None

    col_w = width / len(cells)
    row = []
    for label, value, pct, color, sub in cells:
        inner = tk['Table']([
            [tk['Paragraph'](label, tk['S']['kpi_lbl'])],
            [tk['Paragraph'](f'<b>{_pdf_escape(value)}</b>', tk['S']['kpi_num'])],
            [tk['ProgressBar'](pct, color, width=col_w - 18)],
            [tk['Paragraph'](_pdf_escape(sub), tk['S']['kpi_lbl'])],
        ], colWidths=[col_w - 12])
        inner.setStyle(tk['TableStyle']([
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 1),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ]))
        row.append(inner)

    t = tk['Table']([row], colWidths=[col_w] * len(cells))
    t.setStyle(tk['TableStyle']([
        ('BACKGROUND', (0, 0), (-1, -1), tk['colors'].HexColor('#f9fafb')),
        ('BOX', (0, 0), (-1, -1), 0.5, tk['colors'].HexColor('#e5e7eb')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, tk['colors'].HexColor('#e5e7eb')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LEFTPADDING', (0, 0), (-1, -1), 7),
    ]))
    return t


def _pdf_port_cards(tk, ports, width, per_row=4):
    """Ports en écoute sous forme de cartes, réparties en grille."""
    if not ports:
        return [tk['Paragraph']('Aucun port TCP en écoute détecté.', tk['S']['small'])]

    ports = [describe_listening_port(p) for p in ports]
    ports = [describe_listening_port(p) for p in ports]
    shown = notable_ports(ports)
    hidden = len(ports) - len(shown)
    flows = []
    if hidden:
        flows.append(tk['Paragraph'](
            f'{hidden} port(s) de la plage dynamique ({EPHEMERAL_PORT_START}+) ne sont '
            'pas détaillés : attribués à la volée, ils changent à chaque redémarrage.',
            tk['S']['small']))
        flows.append(tk['Spacer'](1, 5))
    if not shown:
        flows.append(tk['Paragraph']('Aucun port de service en écoute.', tk['S']['small']))
        return flows

    col_w = width / per_row
    for start in range(0, len(shown), per_row):
        chunk = shown[start:start + per_row]
        row = []
        for p in chunk:
            fg, bg = _LEVEL_COLORS.get(p['level'], _LEVEL_COLORS['info'])
            body = [
                [tk['Paragraph'](
                    f'<font color="{fg}">{p["port"]}</font>', tk['S']['port_num'])],
                # Étiquette omise pour un port non répertorié : le numéro suffit.
                [tk['Paragraph'](
                    f'<font color="{fg}" size="6.5"><b>{_pdf_escape(p["name"])}</b></font>'
                    if p.get('name') else '&nbsp;', tk['S']['kpi_lbl'])],
                [tk['Paragraph'](_pdf_escape(p['description']), tk['S']['small'])],
            ]
            if p.get('process'):
                body.append([tk['Paragraph'](
                    f'<font face="Courier" size="6.5">{_pdf_escape(p["process"])}</font>',
                    tk['S']['small'])])
            inner = tk['Table'](body, colWidths=[col_w - 14])
            inner.setStyle(tk['TableStyle']([
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 1),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
            ]))
            row.append(inner)
        # Compléter la dernière rangée pour garder des colonnes régulières.
        while len(row) < per_row:
            row.append('')

        t = tk['Table']([row], colWidths=[col_w] * per_row)
        style = [
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 7),
            ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ]
        for i, p in enumerate(chunk):
            fg, _bg = _LEVEL_COLORS.get(p['level'], _LEVEL_COLORS['info'])
            style.append(('BOX', (i, 0), (i, 0), 0.5, tk['colors'].HexColor('#e5e7eb')))
            style.append(('LINEABOVE', (i, 0), (i, 0), 2, tk['colors'].HexColor(fg)))
        t.setStyle(tk['TableStyle'](style))
        flows.append(t)
        flows.append(tk['Spacer'](1, 5))
    return flows


def _pdf_data_table(tk, header, rows, width, col_ratios, styles_extra=None):
    """Tableau générique à en-tête coloré."""
    data = [[tk['Paragraph'](f'<b>{_pdf_escape(h)}</b>', tk['S']['kpi_lbl']) for h in header]]
    data.extend(rows)
    t = tk['Table'](data, colWidths=[width * r for r in col_ratios], repeatRows=1)
    style = [
        ('BACKGROUND', (0, 0), (-1, 0), tk['colors'].HexColor('#f9fafb')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 0), (-1, -1), 0.4, tk['colors'].HexColor('#eef1f5')),
        ('BOX', (0, 0), (-1, -1), 0.5, tk['colors'].HexColor('#e5e7eb')),
    ]
    style.extend(styles_extra or [])
    t.setStyle(tk['TableStyle'](style))
    return t


def generate_pdf_report(info, client_id=None, client_name=None):
    """Génère le rapport PDF. Bascule sur le HTML si reportlab est absent."""
    tk = _build_pdf_toolkit()
    if tk is None:
        return generate_html_report(info, client_id, client_name)

    try:
        filename = _report_filename(info, 'pdf')
        colors, S = tk['colors'], tk['S']
        Paragraph, Spacer, Table, TableStyle = (
            tk['Paragraph'], tk['Spacer'], tk['Table'], tk['TableStyle'])

        doc = tk['DocumentRapport'](
            filename, pagesize=tk['A4'],
            leftMargin=15 * tk['mm'], rightMargin=15 * tk['mm'],
            topMargin=13 * tk['mm'], bottomMargin=16 * tk['mm'],
            title=f"Fiche système — {info.get('hostname', '')}",
        )
        width = doc.width
        story = []

        # ── Bandeau de titre ─────────────────────────────────────────────────
        generated = _utcnow().strftime('%d/%m/%Y à %H:%M:%S UTC')
        cible = client_name or (f'ID {client_id}' if client_id else 'non spécifié')
        header_txt = (
            f"{_pdf_escape(info.get('brand'))} {_pdf_escape(info.get('model'))} — "
            f"fiche générée le {generated}<br/>"
            f"Client : {_pdf_escape(cible)} · OS : {_pdf_escape(info.get('os_name') or '—')} · "
            f"IP : {_pdf_escape((info.get('ip_addresses') or ['—'])[0])} · "
            f"N° série : {_pdf_escape(info.get('serial_number') or '—')}"
        )
        hero = Table([[Paragraph(
            f"<b>{_pdf_escape(info.get('hostname') or 'Machine inconnue')}</b>", S['title'])],
            [Paragraph(header_txt, S['subtitle'])]], colWidths=[width])
        hero.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#1e3a5f')),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
            ('TOPPADDING', (0, 0), (0, 0), 10),
            ('BOTTOMPADDING', (0, 1), (0, 1), 10),
        ]))
        story.append(hero)
        story.append(Spacer(1, 11))

        # Sommaire : le rapport dépasse la quarantaine de pages.
        sommaire = tk['TableOfContents']()
        sommaire.levelStyles = [S['toc1']]
        titre_sommaire = Paragraph('Sommaire', S['h2'])
        # Ce titre-ci ne doit pas figurer dans le sommaire qu'il introduit.
        titre_sommaire._sans_sommaire = True
        story.append(titre_sommaire)
        story.append(sommaire)
        story.append(Spacer(1, 10))

        # ── Points d'attention ───────────────────────────────────────────────
        story.append(Paragraph("Points d'attention", S['h2']))
        story.extend(_pdf_alerts(tk, build_alerts(info), width))

        # ── Vue d'ensemble ───────────────────────────────────────────────────
        kpis = _pdf_kpis(tk, info, width)
        if kpis:
            story.append(Paragraph("Vue d'ensemble", S['h2']))
            story.append(kpis)

        # ── Identification & système ─────────────────────────────────────────
        # Regroupée en un seul bloc : elle était jusqu'ici coupée en deux,
        # avec Sécurité / Disques / Ports / USB / Licences intercalés entre les
        # deux moitiés — la même machine se décrivait à deux endroits distants
        # du rapport.
        bios = info.get('bios_version', '')
        if bios and info.get('bios_release_date'):
            bios = f"{bios} ({info['bios_release_date']})"
        uptime = _num(info.get('uptime_hours'))
        age_materiel = hardware_age_years(info.get('bios_release_date'))

        table = _pdf_kv_table(tk, [
            ('Nom de machine', info.get('hostname')),
            ('Nom DNS', info.get('dns_name')),
            ("Type d'appareil", info.get('device_type')),
            ('Châssis', info.get('chassis_type')),
            ('Marque', info.get('brand')),
            ('Modèle', info.get('model')),
            ('Numéro de série', info.get('serial_number')),
            ('Asset tag', info.get('asset_tag')),
            ('Adresse MAC', info.get('mac_address')),
            ('Adresse(s) IP', ', '.join(info.get('ip_addresses', []))),
            # Le rattachement domaine/groupe de travail est repris, plus complet
            # (avec contrôleur de domaine), dans « Environnement & hygiène ».
            ("Système d'exploitation", info.get('os_name')),
            ('Version', info.get('os_version')),
            ('Build', info.get('os_build')),
            ('Fin de support', ('%s — %s (%s)' % (
                (info.get('windows_support') or {}).get('fin_de_support'),
                (info.get('windows_support') or {}).get('version'),
                'dépassée' if (info.get('windows_support') or {}).get('termine')
                else 'dans %d jours' % (info.get('windows_support') or {}).get('jours_restants', 0)))
             if info.get('windows_support') else None),
            ('Architecture', info.get('architecture')),
            ("Date d'installation de Windows", info.get('os_install_date')),
            ('Propriétaire enregistré', info.get('registered_owner')),
            ('Session ouverte', info.get('logged_on_user')),
            ('Fuseau horaire', info.get('timezone')),
            ('Uptime', f'{uptime / 24:.1f} jour(s)' if uptime is not None else None),
            ('Hyperviseur détecté', 'Machine virtuelle / hyperviseur détecté'
             if info.get('hypervisor_present') else None),
            ('BIOS', bios or None),
            ('Détail plateforme', info.get('platform')),
            ('Dernière mise à jour Windows', info.get('last_windows_update')),
            ('Horodatage de collecte', info.get('timestamp')),
            ('Âge du matériel',
             '%g an(s) (depuis la date du BIOS)' % age_materiel if age_materiel is not None else None),
        ], width)
        if table:
            story.append(Paragraph('Identification & système', S['h2']))
            story.append(table)

        # ── Processeur & carte mère ───────────────────────────────────────────
        mb = info.get('motherboard') or {}
        # `motherboard` est un dict ; le passer tel quel à une cellule
        # produisait sa représentation Python brute dans le PDF.
        carte_mere = ('%s %s' % (mb.get('manufacturer', ''), mb.get('model', ''))).strip() or None
        table = _pdf_kv_table(tk, [
            ('Processeur', info.get('cpu')),
            ('Cœurs physiques / logiques',
             '%s / %s' % (info.get('cpu_physical_cores'), info.get('cpu_logical_cores'))
             if info.get('cpu_physical_cores') else None),
            ('Cœurs', info.get('cpu_cores') if not info.get('cpu_physical_cores') else None),
            ('Fréquence maximale',
             '%s MHz' % info['cpu_max_clock_mhz'] if info.get('cpu_max_clock_mhz') else None),
            ('Socket', info.get('cpu_socket')),
            ('Nombre de sockets', info.get('cpu_sockets')),
            ('Cache L3',
             '%s Ko' % info['cpu_l3_cache_kb'] if info.get('cpu_l3_cache_kb') else None),
            ('Virtualisation matérielle',
             ('Activée' if info['cpu_virtualization'] else 'Désactivée')
             if isinstance(info.get('cpu_virtualization'), bool)
             else info.get('cpu_virtualization')),
            ('Carte mère', carte_mere),
            ('Version carte mère', mb.get('version')),
            ('N° série carte mère', mb.get('serial_number')),
        ], width)
        if table:
            story.append(Paragraph('Processeur & carte mère', S['h2']))
            story.append(table)

        # ── Mémoire ───────────────────────────────────────────────────────────
        # N'existait pas comme rubrique propre : la RAM totale/disponible
        # n'apparaissait nulle part, les emplacements étaient noyés dans
        # « Détail matériel » et les barrettes dans une liste anonyme en fin
        # de rapport.
        table = _pdf_kv_table(tk, [
            ('RAM totale', '%s GB' % info['ram_gb'] if info.get('ram_gb') else None),
            ('RAM disponible', '%s GB' % info['ram_free_gb'] if info.get('ram_free_gb') else None),
            ('Emplacements mémoire',
             '%s occupés sur %s (max %s Go)' % (info.get('memory_slots_used'),
                                                info.get('memory_slots_total'),
                                                info.get('memory_max_gb'))
             if info.get('memory_slots_total') else None),
        ], width)
        if table:
            story.append(Paragraph('Mémoire', S['h2']))
            story.append(table)
        modules = info.get('memory_modules') or []
        if modules:
            for m in modules:
                story.append(Paragraph(f'• {_pdf_escape(format_memory_module(m))}', S['body']))

        # ── Stockage ─────────────────────────────────────────────────────────
        drives = info.get('disk_drives', [])
        if drives:
            story.append(Paragraph('Disques logiques', S['h2']))
            for raw in drives:
                parsed = _parse_drive(raw)
                if parsed and parsed[1] and parsed[2] is not None:
                    label, total, used, _free = parsed
                    pct = round(used / total * 100) if total else 0
                    story.append(Paragraph(
                        f'<b>{_pdf_escape(label)}</b> — {used:g} / {total:g} GB ({pct} %)',
                        S['body']))
                    story.append(tk['ProgressBar'](pct, _LEVEL_COLORS[_disk_level(pct)][0],
                                                   width=width))
                else:
                    story.append(Paragraph(_pdf_escape(raw), S['body']))
                story.append(Spacer(1, 5))

        # Même lecture qu'à la fiche système : l'état SMART est reconstitué en
        # badge plutôt que laissé en phrase brute — c'était le seul endroit où
        # ce travail (2.9.3) n'avait pas été répercuté.
        disques_physiques = [parse_physical_disk(str(d)) or {'nom': None, 'brut': str(d)}
                             for d in (info.get('physical_disks') or [])]
        if disques_physiques:
            rows, extra = [], []
            for i, d in enumerate(disques_physiques, start=1):
                if d.get('brut'):
                    rows.append([Paragraph(_pdf_escape(d['brut']), S['mono']), '', ''])
                    continue
                fg, bg = _LEVEL_COLORS.get(d.get('sante_niveau'), _LEVEL_COLORS['muted'])
                etat = d.get('sante') or '—'
                if d.get('etat'):
                    etat += ' (%s)' % d['etat']
                rows.append([
                    Paragraph(f"<b>{_pdf_escape(d.get('nom'))}</b> "
                              f"<font size='7' color='#6b7280'>{_pdf_escape(d.get('type'))}</font>",
                              S['body']),
                    Paragraph(f"{_pdf_escape(d.get('taille_go'))} GB", S['mono']),
                    Paragraph(f'<font color="{fg}"><b>{_pdf_escape(etat)}</b></font>', S['body']),
                ])
                extra.append(('BACKGROUND', (2, i), (2, i), colors.HexColor(bg)))
            story.append(Paragraph('Disques physiques (%d)' % len(disques_physiques), S['h2']))
            story.append(_pdf_data_table(tk, ['Disque', 'Capacité', 'État SMART'],
                                         rows, width, [0.52, 0.18, 0.30], extra))

        fiabilite = info.get('disk_reliability') or []
        if fiabilite:
            story.append(Paragraph('Fiabilité des disques', S['h2']))
            for r in fiabilite:
                story.append(Paragraph(f'• {_pdf_escape(format_reliability(r))}', S['body']))

        styles_disque = info.get('disk_partition_styles') or []
        if info.get('boot_mode') or styles_disque:
            story.append(Paragraph(
                'Démarrage — utile avant une réinstallation ou un remplacement de disque',
                S['h2']))
            if info.get('boot_mode'):
                story.append(Paragraph('<b>Mode de démarrage :</b> %s'
                                       % _pdf_escape(info['boot_mode']), S['body']))
            if styles_disque:
                rows = [[Paragraph('Disque %s' % d.get('number'), S['mono']),
                         Paragraph(_pdf_escape(d.get('style')), S['body']),
                         Paragraph('Oui' if d.get('boot') else '—', S['body'])] for d in styles_disque]
                story.append(_pdf_data_table(tk, ['Disque', 'Style', 'Démarrage'],
                                             rows, width, [0.34, 0.33, 0.33]))

        # ── Affichage & impression ────────────────────────────────────────────
        # GPU, écrans et imprimantes vivaient à trois endroits distincts du
        # rapport (l'un d'eux tout en bas, après Applications par défaut) :
        # même sujet, un seul endroit désormais.
        gpus = info.get('gpu_details') or []
        if gpus:
            rows = [[Paragraph(_pdf_escape(g.get('name')), S['body']),
                     Paragraph(('%s GB' % g['vram_gb']) if g.get('vram_gb') else '—', S['body']),
                     Paragraph(_pdf_escape(g.get('resolution')), S['body']),
                     Paragraph(_pdf_escape(g.get('driver_version')), S['mono'])] for g in gpus]
            story.append(Paragraph('Affichage & impression', S['h2']))
            story.append(_pdf_data_table(tk, ['Carte graphique', 'VRAM', 'Résolution', 'Pilote'],
                                         rows, width, [0.42, 0.12, 0.20, 0.26]))
        ecrans = info.get('monitors') or []
        if ecrans:
            for m in ecrans:
                story.append(Paragraph(f'• {_pdf_escape(format_monitor(m))}', S['body']))
        imprimantes = info.get('printers') or []
        if imprimantes:
            for p in imprimantes:
                story.append(Paragraph(f'• {_pdf_escape(format_printer(p))}', S['body']))

        # ── Batterie ─────────────────────────────────────────────────────────
        table = _pdf_kv_table(tk, [
            ('Charge actuelle', info.get('battery')),
            ('Santé',
             '%s %% de la capacité d\'origine' % info['battery_health_percent']
             if info.get('battery_health_percent') is not None else None),
            ('Usure',
             '%s %%' % info['battery_wear_percent']
             if info.get('battery_wear_percent') is not None else None),
            ('Cycles de charge', info.get('battery_cycles')),
            ("Capacité d'origine",
             '%s mWh' % info['battery_designed_capacity_mwh']
             if info.get('battery_designed_capacity_mwh') else None),
            ('Capacité réelle',
             '%s mWh' % info['battery_full_capacity_mwh']
             if info.get('battery_full_capacity_mwh') else None),
            ('État', info.get('battery_health_status')),
        ], width)
        if table:
            story.append(Paragraph('Batterie', S['h2']))
            story.append(table)

        # ── Sécurité & conformité ────────────────────────────────────────────
        sec_rows = []
        if info.get('antivirus_products'):
            for av in info['antivirus_products']:
                niveau = ('ok' if av.get('enabled')
                          else 'danger' if av.get('enabled') is False else 'info')
                sec_rows.append(('Antivirus',
                                 '%s — %s' % (av['name'], av.get('status', '')), niveau))
        else:
            antivirus = (info.get('antivirus') or '').strip()
            sec_rows.append(('Antivirus', antivirus or 'Aucun détecté',
                             'ok' if antivirus and antivirus.lower() not in ('n/a', 'aucun') else 'danger'))
        if info.get('tpm_present') is not None:
            if info.get('tpm_enabled'):
                sec_rows.append(('TPM', 'Présent et activé', 'ok'))
            elif info.get('tpm_present'):
                sec_rows.append(('TPM', 'Présent mais désactivé', 'warn'))
            else:
                sec_rows.append(('TPM', 'Absent', 'warn'))
        if info.get('secure_boot') is not None:
            sec_rows.append(('Secure Boot', 'Activé' if info.get('secure_boot') else 'Désactivé',
                             'ok' if info.get('secure_boot') else 'warn'))
        if info.get('sip_status'):
            sec_rows.append(("Protection de l'intégrité système (SIP)", info['sip_status'],
                             'ok' if info['sip_status'] == 'Activée' else 'warn'))
        if info.get('gatekeeper_status'):
            sec_rows.append(('Gatekeeper', info['gatekeeper_status'],
                             'ok' if info['gatekeeper_status'] == 'Activé' else 'warn'))
        if info.get('mdm_enrolled') is not None:
            sec_rows.append(('Inscription MDM', info.get('mdm_detail') or
                             ('Oui' if info['mdm_enrolled'] else 'Non'), 'ok'))
        if info.get('firewall_profiles'):
            for prof in info['firewall_profiles']:
                sec_rows.append(('Pare-feu',
                                 '%s : %s' % (prof['name'],
                                              'Activé' if prof['enabled'] else 'Désactivé'),
                                 'ok' if prof['enabled'] else 'danger'))
        else:
            for profile in info.get('firewall', []):
                sec_rows.append(('Pare-feu', profile,
                                 'danger' if re.search(r'(désactiv|disabled|off)', profile, re.I) else 'ok'))
        if info.get('bitlocker_volumes'):
            sec_rows.append(('BitLocker',
                             'Activé' if info.get('bitlocker_actif') else 'Désactivé',
                             'ok' if info.get('bitlocker_actif') else 'danger'))
            for v in info['bitlocker_volumes']:
                sec_rows.append(('Volume %s' % v.get('volume', '?'),
                                 '%s — protection %s' % (v.get('etat', '?'),
                                                         (v.get('protection') or '?').lower()),
                                 'ok' if v.get('protege') else 'warn'))
        else:
            for vol in (info.get('bitlocker') or []):
                sec_rows.append(('BitLocker', vol,
                                 'warn' if re.search(r'(non chiffr|not encrypted|off)', vol, re.I) else 'ok'))
        if info.get('last_windows_update'):
            sec_rows.append(('Dernière mise à jour', info['last_windows_update'], 'info'))
        if info.get('oem_product_key'):
            sec_rows.append(('Clé OEM (firmware)', info['oem_product_key'], 'info'))
        pol = info.get('local_password_policy') or {}
        if pol.get('min_length') is not None:
            sec_rows.append(('Mot de passe — longueur mini', '%s caractère(s)' % pol['min_length'],
                             'danger' if pol['min_length'] == 0 else 'warn' if pol['min_length'] < 8 else 'ok'))
        if pol.get('complexity') is not None:
            sec_rows.append(('Mot de passe — complexité',
                             'Exigée' if pol['complexity'] else 'Non exigée',
                             'ok' if pol['complexity'] else 'warn'))
        if pol.get('lockout_threshold') is not None:
            sec_rows.append(('Verrouillage de compte',
                             'après %s essai(s)' % pol['lockout_threshold'] if pol['lockout_threshold']
                             else 'Aucun',
                             'ok' if pol['lockout_threshold'] else 'danger'))

        if sec_rows:
            story.append(Paragraph('Sécurité & conformité', S['h2']))
            data, extra = [], []
            for i, (label, value, level) in enumerate(sec_rows):
                fg, bg = _LEVEL_COLORS.get(level, _LEVEL_COLORS['info'])
                data.append([
                    Paragraph(f'<b>{_pdf_escape(label)}</b>', S['body']),
                    Paragraph(f'<font color="{fg}"><b>{_pdf_escape(value)}</b></font>', S['body']),
                ])
                extra.append(('BACKGROUND', (1, i), (1, i), colors.HexColor(bg)))
            t = Table(data, colWidths=[width * 0.34, width * 0.66])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f9fafb')),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 7),
                ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#e5e7eb')),
                ('LINEBELOW', (0, 0), (-1, -2), 0.4, colors.HexColor('#eef1f5')),
            ] + extra))
            story.append(t)

        # ── Détections antivirus ──────────────────────────────────────────────
        # Historique Windows Defender sur un an — un antivirus tiers stocke sa
        # quarantaine dans un format propre à l'éditeur, illisible ici.
        detections = info.get('malware_detections') or []
        if detections:
            rows, extra = [], []
            for i, d in enumerate(detections, start=1):
                fg, bg = _LEVEL_COLORS.get(d.get('level'), _LEVEL_COLORS['muted'])
                rows.append([
                    Paragraph(f'<b>{_pdf_escape(d.get("threat"))}</b>', S['body']),
                    Paragraph(f'<font color="{fg}"><b>{_pdf_escape(d.get("category"))}</b></font>', S['body']),
                    Paragraph(_pdf_escape(d.get('resource')), S['small']),
                    Paragraph('Oui' if d.get('cleaned') else 'Non', S['body']),
                    Paragraph(_pdf_escape(d.get('when')), S['mono']),
                ])
                extra.append(('BACKGROUND', (1, i), (1, i), colors.HexColor(bg)))
            story.append(Paragraph('Détections antivirus (%d)'
                                   % (info.get('malware_detections_total') or len(detections)), S['h2']))
            story.append(_pdf_data_table(tk, ['Menace', 'Catégorie', 'Fichier', 'Traitée', 'Date'],
                                         rows, width, [0.22, 0.14, 0.38, 0.10, 0.16], extra))

        pilotes_ns = info.get('unsigned_drivers') or []
        if pilotes_ns:
            rows = [[Paragraph(_pdf_escape(p.get('device')), S['body']),
                     Paragraph(_pdf_escape(p.get('version')), S['mono']),
                     Paragraph(_pdf_escape(p.get('provider')), S['small'])] for p in pilotes_ns]
            story.append(Paragraph('Pilotes non signés (%d)' % len(pilotes_ns), S['h2']))
            story.append(_pdf_data_table(tk, ['Périphérique', 'Version', 'Éditeur'],
                                         rows, width, [0.40, 0.20, 0.40]))

        regles_fw = info.get('firewall_rules') or []
        if regles_fw:
            total_fw = info.get('firewall_rules_total') or len(regles_fw)
            rows = [[Paragraph(_pdf_escape(r.get('name')), S['body']),
                     Paragraph(_pdf_escape(r.get('direction')), S['body']),
                     Paragraph(_pdf_escape(r.get('protocol')), S['mono']),
                     Paragraph(_pdf_escape(r.get('port')), S['mono']),
                     Paragraph(_pdf_escape(r.get('profiles')), S['small'])] for r in regles_fw]
            titre_fw = 'Règles de pare-feu autorisées (%d)' % total_fw
            if len(regles_fw) < total_fw:
                titre_fw += ' — %d affichées' % len(regles_fw)
            story.append(Paragraph(titre_fw, S['h2']))
            story.append(_pdf_data_table(tk, ['Règle', 'Direction', 'Protocole', 'Port', 'Profils'],
                                         rows, width, [0.28, 0.12, 0.14, 0.14, 0.32]))

        # ── Accès distant & exposition ───────────────────────────────────────
        acces = info.get('remote_access') or []
        if acces:
            rows = []
            for a in acces:
                if a.get('enabled') is None:
                    etat = 'Non installé'
                else:
                    etat = 'Actif' if a['enabled'] else 'Inactif'
                rows.append([Paragraph(_pdf_escape(a.get('label')), S['body']),
                             Paragraph(etat, S['body']),
                             Paragraph(_pdf_escape(a.get('detail')), S['small'])])
            story.append(Paragraph('Accès distant & exposition', S['h2']))
            story.append(_pdf_data_table(tk, ['Voie d\'accès', 'État', 'Détail'],
                                         rows, width, [0.34, 0.16, 0.50]))
        autologon = info.get('autologon')
        if autologon is not None:
            if autologon.get('enabled'):
                txt = 'ACTIVÉE — compte %s%s' % (
                    autologon.get('user') or '?',
                    ' (mot de passe en clair dans le registre)' if autologon.get('password_stored') else '')
            else:
                txt = 'Désactivée'
            story.append(Paragraph('<b>Ouverture automatique de session :</b> %s'
                                   % _pdf_escape(txt), S['body']))
        rdp_users = info.get('rdp_allowed_users') or []
        if rdp_users:
            story.append(Paragraph('<b>Membres autorisés (Bureau à distance) :</b> %s'
                                   % _pdf_escape(', '.join(rdp_users)), S['body']))
        rdp_creds = info.get('saved_rdp_credentials') or []
        if rdp_creds:
            story.append(Paragraph(
                '<b>Identifiants Bureau à distance enregistrés :</b> %s — un serveur qui '
                "n'existe plus dans cette liste est une piste de nettoyage."
                % _pdf_escape(', '.join(rdp_creds)), S['small']))

        # ── Agents de télémaintenance & EDR ───────────────────────────────────
        # Recherchés par sous-chaîne du nom affiché des services — au mieux, à
        # corriger sur la fiche appareil si besoin.
        rmm = info.get('remote_support_agents') or []
        edr = info.get('edr_agents') or []
        if rmm or edr:
            story.append(Paragraph('Agents de télémaintenance & EDR', S['h2']))
        if rmm:
            rows = [[Paragraph('<b>%s</b>' % _pdf_escape(a.get('nom')), S['body']),
                     Paragraph(_pdf_escape(a.get('marque')), S['body']),
                     Paragraph(_pdf_escape(a.get('service')), S['small']),
                     Paragraph('Actif' if a.get('actif') else 'Inactif', S['body'])] for a in rmm]
            story.append(_pdf_data_table(tk, ['Agent', 'Marque', 'Service', 'État'],
                                         rows, width, [0.28, 0.20, 0.36, 0.16]))
        if edr:
            rows = [[Paragraph('<b>%s</b>' % _pdf_escape(a.get('nom')), S['body']),
                     Paragraph(_pdf_escape(a.get('marque')), S['body']),
                     Paragraph(_pdf_escape(a.get('service')), S['small']),
                     Paragraph('Actif' if a.get('actif') else 'Inactif', S['body'])] for a in edr]
            story.append(_pdf_data_table(tk, ['EDR', 'Marque', 'Service', 'État'],
                                         rows, width, [0.28, 0.20, 0.36, 0.16]))

        # ── Journal de sécurité ───────────────────────────────────────────────
        journal = info.get('security_events') or []
        if journal:
            rows = [[Paragraph('<b>%s</b>' % _pdf_escape(e.get('compte')), S['body']),
                     Paragraph(_pdf_escape(e.get('type')), S['body']),
                     Paragraph(str(e.get('count', '')), S['mono']),
                     Paragraph(_pdf_escape(', '.join(e.get('sources') or [])), S['small']),
                     Paragraph(_pdf_escape(e.get('last_seen')), S['mono'])] for e in journal]
            story.append(Paragraph(
                "Journal de sécurité — %s échec(s) d'ouverture, %s verrouillage(s)"
                % (info.get('failed_logons', 0), info.get('account_lockouts', 0)), S['h2']))
            story.append(_pdf_data_table(
                tk, ['Compte', 'Type', 'Nb', 'Origine', 'Dernier'],
                rows, width, [0.22, 0.24, 0.08, 0.26, 0.20]))
        rdp_hist = info.get('rdp_logon_history') or []
        if rdp_hist:
            rows = [[Paragraph('<b>%s</b>' % _pdf_escape(c.get('user')), S['body']),
                     Paragraph(_pdf_escape(c.get('ip')), S['mono']),
                     Paragraph(_pdf_escape(c.get('when')), S['mono'])] for c in rdp_hist]
            story.append(Paragraph(
                'Connexions Bureau à distance entrantes (%d) — celles qui ont réussi'
                % len(rdp_hist), S['h2']))
            story.append(_pdf_data_table(tk, ['Compte', 'Depuis', 'Quand'],
                                         rows, width, [0.34, 0.33, 0.33]))

        # ── Certificats à renouveler ──────────────────────────────────────────
        certificats = info.get('certificates_expiring') or []
        if certificats:
            rows = [[Paragraph('<b>%s</b>' % _pdf_escape(c.get('sujet')), S['body']),
                     Paragraph(_pdf_escape(c.get('emetteur')), S['small']),
                     Paragraph(_pdf_escape(c.get('expire_le')), S['mono']),
                     Paragraph(('expiré depuis %d j' % -c.get('jours_restants', 0))
                               if c.get('expire') else ('%d j' % c.get('jours_restants', 0)),
                               S['body'])] for c in certificats]
            story.append(Paragraph('Certificats à renouveler (%d)' % len(certificats), S['h2']))
            story.append(_pdf_data_table(
                tk, ['Sujet', 'Émetteur', 'Expiration', 'Reste'],
                rows, width, [0.30, 0.34, 0.18, 0.18]))

        # ── Licences ─────────────────────────────────────────────────────────
        licences = info.get('licenses', [])
        story.append(Paragraph(f'Licences ({len(licences)})', S['h2']))
        if licences:
            rows, extra = [], []
            for i, lic in enumerate(licences, start=1):
                if lic.get('full_key'):
                    # Clé complète, en monospace sur fond sombre pour être
                    # relisible sans ambiguïté (0/O, 1/I) depuis un tirage papier.
                    # Teintes claires : la cellule est sur fond sombre.
                    if lic.get('key_verified'):
                        mention = ('<br/><font size="6.3" color="#7bdcb5">Vérifiée : les 5 '
                                   'derniers caractères correspondent à la licence active.</font>')
                    else:
                        mention = ('<br/><font size="6.3" color="#fbd38d">Non appairée à une '
                                   'licence active de cette machine.</font>')
                    cle_cell = Paragraph(
                        '<font face="Courier-Bold" size="9.5" color="#ffffff">'
                        f'{_pdf_escape(lic["full_key"])}</font>{mention}', S['body'])
                    extra.append(('BACKGROUND', (1, i), (1, i), colors.HexColor('#1e3a5f')))
                elif lic.get('partial_key'):
                    cle_cell = Paragraph(
                        'Clé complète absente de la machine (licence numérique, '
                        'Click-to-Run ou KMS) — se termine par '
                        f'<font face="Courier-Bold">{_pdf_escape(lic["partial_key"])}</font>',
                        S['small'])
                else:
                    cle_cell = Paragraph('Aucune clé exposée', S['small'])
                ok = lic.get('activated')
                fg, bg = _LEVEL_COLORS['ok' if ok else 'warn']
                source = lic.get('key_source') or lic.get('channel') or 'Windows Licensing'
                rows.append([
                    Paragraph(f'<b>{_pdf_escape(lic.get("name"))}</b><br/>'
                              f'<font size="6.8" color="#6b7280">'
                              f'Microsoft · {_pdf_escape(source)}</font>', S['body']),
                    cle_cell,
                    Paragraph(f'<font color="{fg}"><b>'
                              f'{_pdf_escape(lic.get("status") or "Inconnu")}</b></font>', S['body']),
                ])
                extra.append(('BACKGROUND', (2, i), (2, i), colors.HexColor(bg)))
            story.append(_pdf_data_table(
                tk, ['Produit', 'Clé de licence', 'État'], rows, width,
                [0.34, 0.48, 0.18], extra))
        else:
            story.append(Paragraph('Aucune licence détectée.', S['small']))

        # ── Périphériques USB ────────────────────────────────────────────────
        usb = info.get('usb_devices', [])
        story.append(Paragraph(f'Périphériques USB ({len(usb)})', S['h2']))
        if usb:
            inv = sum(1 for d in usb if d.get('inventoriable'))
            story.append(Paragraph(
                f'{len(usb)} périphérique(s) détecté(s), dont {inv} repris dans '
                "l'inventaire ParcInfo. Les éléments « Interne » (concentrateurs, "
                'contrôleurs, nœuds composites) sont listés pour information.',
                S['small']))
            story.append(Spacer(1, 4))
            rows, extra = [], []
            for i, d in enumerate(usb, start=1):
                meta = []
                if d.get('serial'):
                    meta.append(f'N° série {d["serial"]}')
                if d.get('vid'):
                    meta.append(f'{d["vid"]}:{d["pid"]}')
                if d.get('manufacturer'):
                    meta.append(d['manufacturer'])
                etat, level = (('Inventorié', 'ok') if d.get('inventoriable')
                               else ('Interne', 'muted'))
                fg, bg = _LEVEL_COLORS[level]
                rows.append([
                    Paragraph(f'<b>{_pdf_escape(d["name"])}</b><br/>'
                              f'<font size="6.8" color="#6b7280">'
                              f'{_pdf_escape(" · ".join(meta) or "—")}</font>', S['body']),
                    Paragraph(_pdf_escape(d['categorie']), S['body']),
                    Paragraph(f'<font color="{fg}"><b>{etat}</b></font>', S['body']),
                ])
                extra.append(('BACKGROUND', (2, i), (2, i), colors.HexColor(bg)))
            story.append(_pdf_data_table(
                tk, ['Périphérique', 'Catégorie', 'Inventaire'], rows, width,
                [0.55, 0.26, 0.19], extra))
        else:
            story.append(Paragraph('Aucun périphérique USB détecté.', S['small']))

        # ── Périphériques en erreur ───────────────────────────────────────────
        en_erreur = info.get('problem_devices') or []
        if en_erreur:
            rows = [[Paragraph('<b>%s</b>' % _pdf_escape(p.get('name')), S['body']),
                     Paragraph(_pdf_escape(p.get('classe')), S['body']),
                     Paragraph(_pdf_escape(p.get('fabricant')), S['body']),
                     Paragraph('%s (code %s)' % (_pdf_escape(p.get('libelle')), p.get('code')),
                               S['small'])] for p in en_erreur]
            story.append(Paragraph('Périphériques en erreur (%d)' % len(en_erreur), S['h2']))
            story.append(_pdf_data_table(
                tk, ['Périphérique', 'Classe', 'Fabricant', 'Diagnostic'],
                rows, width, [0.32, 0.14, 0.18, 0.36]))

        # ── Réseau ───────────────────────────────────────────────────────────
        # Adaptateurs et ports en écoute vivaient à trois endroits séparés du
        # rapport (dont un doublon : les mêmes cartes réseau ressortaient en
        # liste brute plus loin) ; ils sont désormais ensemble.
        adaptateurs = info.get('network_adapter_details') or []
        if adaptateurs:
            # Physiques d'abord, puis connectées : même ordre de lecture que la
            # fiche système.
            adaptateurs = sorted(adaptateurs, key=lambda a: (bool(a.get('physical')),
                                                              a.get('connected') is True),
                                 reverse=True)
            rows = []
            for a in adaptateurs:
                ips = ' · '.join('%s/%s' % (i['address'], i['prefix'])
                                 for i in a.get('ip_addresses') or []) or '—'
                nature = 'Physique' if a.get('physical') else 'Virtuelle'
                if a.get('connected') is not None:
                    nature += ' · %s' % ('connectée' if a['connected'] else 'déconnectée')
                rows.append([
                    Paragraph('<b>%s</b>' % _pdf_escape(a['name']), S['body']),
                    Paragraph(nature, S['body']),
                    Paragraph(_pdf_escape(ips), S['mono']),
                    Paragraph(_pdf_escape(a.get('link_speed')), S['body']),
                    Paragraph(_pdf_escape(a.get('mac_address')), S['mono']),
                ])
            story.append(Paragraph('Réseau', S['h2']))
            story.append(_pdf_data_table(
                tk, ['Interface', 'Nature', 'Adresse IP / plage', 'Débit', 'MAC'],
                rows, width, [0.26, 0.13, 0.27, 0.14, 0.20]))
        elif info.get('network_adapters'):
            # Repli pour une collecte antérieure, sans détail structuré.
            story.append(Paragraph('Réseau', S['h2']))
            for a in info['network_adapters']:
                story.append(Paragraph(f'• {_pdf_escape(a)}', S['body']))

        ports = info.get('listening_ports', [])
        story.append(Paragraph(f'Ports en écoute ({len(ports)})', S['h2']))
        story.extend(_pdf_port_cards(tk, ports, width))

        # ── Configuration réseau ─────────────────────────────────────────────
        proxy = info.get('proxy') or {}
        wifi = info.get('wifi') or {}
        table = _pdf_kv_table(tk, [
            ('Passerelle par défaut', info.get('default_gateway')),
            ('IP publique', info.get('public_ip')),
            ('Opérateur (FAI)', info.get('public_ip_isp')),
            ('Suffixes DNS', ', '.join(info.get('dns_suffixes') or []) or None),
            ('Proxy', ('%s (%s)' % (proxy.get('server') or proxy.get('auto_config_url'),
                                    'actif' if proxy.get('enabled') else 'configuré mais inactif'))
             if proxy.get('server') or proxy.get('auto_config_url') else None),
            ('Réseau Wi-Fi', wifi.get('ssid')),
            ('Signal Wi-Fi', wifi.get('signal')),
            ('Résolveur DNS interrogé', info.get('dns_check_resolveur')),
            ('Vérification DNS (dnscheck.tools)', info.get('dns_check_reponse')),
            ('Box internet — fabricant', info.get('router_manufacturer')),
            ('Box internet — modèle', info.get('router_model')),
            ('Box internet — nom', info.get('router_name')),
            ('Box internet — IP WAN', info.get('router_wan_ip')),
        ], width)
        if table:
            story.append(Paragraph('Configuration réseau', S['h2']))
            story.append(table)

        profils_reseau = info.get('network_profiles') or []
        if profils_reseau:
            rows = [[Paragraph(_pdf_escape(p['name']), S['body']),
                     Paragraph(_pdf_escape(p.get('interface')), S['body']),
                     Paragraph(_pdf_escape(p.get('category')), S['body']),
                     Paragraph(_pdf_escape(p.get('connectivity')), S['body'])] for p in profils_reseau]
            story.append(Paragraph('Environnement réseau détecté', S['h2']))
            story.append(_pdf_data_table(
                tk, ['Réseau', 'Interface', 'Catégorie', 'Connectivité'], rows, width,
                [0.28, 0.30, 0.20, 0.22]))

        dns = info.get('dns_servers') or []
        if dns:
            rows = [[Paragraph(_pdf_escape(d['interface']), S['body']),
                     Paragraph(_pdf_escape(', '.join(d['servers'])), S['mono']),
                     Paragraph('DHCP' if d.get('dhcp') else 'Manuelle', S['body'])] for d in dns]
            story.append(Paragraph('Serveurs DNS', S['h2']))
            story.append(_pdf_data_table(tk, ['Interface', 'Serveurs', 'Attribution'],
                                         rows, width, [0.42, 0.38, 0.20]))

        hosts_entrees = info.get('hosts_entries') or []
        if hosts_entrees:
            rows = [[Paragraph(_pdf_escape(h.get('hostname')), S['body']),
                     Paragraph(_pdf_escape(h.get('ip')), S['mono']),
                     Paragraph('Local' if h.get('local') else 'Réseau', S['body'])] for h in hosts_entrees]
            story.append(Paragraph('Redirections du fichier hosts (%d)' % len(hosts_entrees), S['h2']))
            story.append(_pdf_data_table(tk, ['Nom', 'Adresse', 'Type'],
                                         rows, width, [0.46, 0.30, 0.24]))

        redirections_port = info.get('port_forwards') or []
        if redirections_port:
            rows = [[Paragraph('%s:%s' % (_pdf_escape(p.get('listen_address')), p.get('listen_port')), S['mono']),
                     Paragraph('%s:%s' % (_pdf_escape(p.get('connect_address')), p.get('connect_port')), S['mono'])]
                    for p in redirections_port]
            story.append(Paragraph('Redirections de port (%d)' % len(redirections_port), S['h2']))
            story.append(_pdf_data_table(tk, ['Écoute', 'Destination'], rows, width, [0.5, 0.5]))

        # ── Qualité du lien ──────────────────────────────────────────────────
        latence = info.get('latency') or []
        if latence:
            rows = []
            for l in latence:
                moyenne = ('%s ms' % l['avg_ms']) if l.get('avg_ms') is not None else 'injoignable'
                rows.append([
                    Paragraph('<b>%s</b>' % _pdf_escape(l['role']), S['body']),
                    Paragraph(_pdf_escape(l['target']), S['mono']),
                    Paragraph(moyenne, S['body']),
                    Paragraph(('%s ms' % l['max_ms']) if l.get('max_ms') is not None else '—', S['body']),
                    Paragraph('%s %%' % l.get('loss_pct', 0), S['body']),
                ])
            story.append(Paragraph('Qualité du lien réseau', S['h2']))
            story.append(_pdf_data_table(
                tk, ['Cible', 'Adresse', 'Latence moyenne', 'Pic', 'Perte'],
                rows, width, [0.22, 0.24, 0.20, 0.14, 0.20]))

        debit = info.get('bandwidth')
        if debit:
            story.append(Paragraph(
                '<b>Débit descendant :</b> %s Mb/s — %s Mo en %s s'
                % (debit['mbps'], debit['downloaded_mb'], debit['seconds']), S['body']))
        else:
            story.append(Paragraph(
                'Débit descendant : non mesuré — relancer avec --test-debit '
                '(ou cocher la case dans le collecteur graphique).', S['small']))

        # ── Partages & lecteurs réseau ────────────────────────────────────────
        # Deux faces de la même ressource : ce que la machine expose au réseau,
        # et ce qu'elle y a mappé depuis d'autres machines. Elles vivaient
        # jusqu'ici dans « Démarrage » et « Applications par défaut ».
        partages = info.get('smb_shares') or []
        if partages:
            rows = [[Paragraph('<b>%s</b>' % _pdf_escape(s['name']), S['body']),
                     Paragraph(_pdf_escape(s.get('path')), S['mono']),
                     Paragraph('Administration' if s.get('administrative') else 'Exposé',
                               S['body'])] for s in partages]
            story.append(Paragraph('Partages réseau exposés (%d)' % len(partages), S['h2']))
            story.append(_pdf_data_table(tk, ['Partage', 'Chemin', 'Nature'],
                                         rows, width, [0.32, 0.46, 0.22]))
        lecteurs = info.get('mapped_drives') or []
        if lecteurs:
            rows = [[Paragraph(_pdf_escape(d.get('letter')), S['mono']),
                     Paragraph(_pdf_escape(d.get('path')), S['mono'])] for d in lecteurs]
            story.append(Paragraph('Lecteurs réseau mappés (%d)' % len(lecteurs), S['h2']))
            story.append(_pdf_data_table(tk, ['Lettre', 'Cible'], rows, width, [0.15, 0.85]))

        # ── Environnement & hygiène système ───────────────────────────────────
        table = _pdf_kv_table(tk, [
            ('Rattachement',
             '%s (%s)' % (info.get('domain_name') or '—',
                          'domaine' if info.get('domain_joined') else 'groupe de travail')
             if info.get('domain_joined') is not None else None),
            ('Contrôleur de domaine', info.get('domain_controller')),
            ('Serveur WSUS', info.get('wsus_server')),
            ('Groupe WSUS', info.get('wsus_group')),
            ('Source de temps', info.get('time_source')),
            ("Décalage d'horloge", info.get('time_offset')),
            ('UAC', ('Activé' if info.get('uac_enabled') else 'Désactivé')
             if info.get('uac_enabled') is not None else None),
            ('Fichiers temporaires',
             '%s Mo récupérables' % info['temp_files_mb'] if info.get('temp_files_mb') else None),
            ('Points de restauration',
             ('%d disponible(s), dernier le %s' % (len(info['restore_points']),
                                                   info['restore_points'][-1]['when']))
             if info.get('restore_points') else
             ('Aucun — retour arrière impossible' if 'restore_points' in info else None)),
            ('Redémarrage en attente',
             ('Oui — %s' % ', '.join(info.get('reboot_reasons') or []))
             if info.get('reboot_pending') else
             ('Non' if info.get('reboot_pending') is not None else None)),
            ("Plan d'alimentation", info.get('power_plan')),
            ('Démarrage rapide', ('Activé' if info.get('fast_startup') else 'Désactivé')
             if info.get('fast_startup') is not None else None),
            ('Dernière analyse antivirus',
             '%s (complète)' % info['defender_last_full_scan']
             if info.get('defender_last_full_scan') else
             ('%s (rapide)' % info['defender_last_quick_scan']
              if info.get('defender_last_quick_scan') else None)),
            ('Framework .NET installé', ', '.join(info.get('dotnet_versions') or []) or None),
        ], width)
        if table:
            story.append(Paragraph('Environnement & hygiène système', S['h2']))
            story.append(table)

        gpos = info.get('group_policies') or []
        if gpos:
            rows = [[Paragraph(_pdf_escape(g.get('name')), S['body']),
                     Paragraph(_pdf_escape(g.get('scope')), S['body']),
                     Paragraph('Refusée' if g.get('denied') else
                               ('Activée' if g.get('enabled') else 'Désactivée'), S['body'])]
                    for g in gpos]
            story.append(Paragraph('Stratégies de groupe appliquées (%d)' % len(gpos), S['h2']))
            story.append(_pdf_data_table(tk, ['Stratégie', 'Périmètre', 'État'],
                                         rows, width, [0.50, 0.25, 0.25]))

        # ── Comptes de messagerie ────────────────────────────────────────────
        mails = info.get('mail_accounts') or []
        if mails:
            rows = []
            for m in mails:
                entrant = m.get('incoming_server') or ''
                if entrant and m.get('incoming_port'):
                    entrant += ':%s' % m['incoming_port']
                sortant = m.get('outgoing_server') or ''
                if sortant and m.get('outgoing_port'):
                    sortant += ':%s' % m['outgoing_port']
                mdp = ('enregistré' if m.get('password_stored')
                       else 'non' if m.get('password_stored') is False else '—')
                rows.append([Paragraph(_pdf_escape(m.get('client')), S['body']),
                             Paragraph(_pdf_escape(m.get('email') or m.get('display_name')), S['mono']),
                             Paragraph(_pdf_escape(m.get('protocol') or '—'), S['body']),
                             Paragraph(_pdf_escape(entrant or '—'), S['mono']),
                             Paragraph(_pdf_escape(sortant or '—'), S['mono']),
                             Paragraph(mdp, S['small'])])
            story.append(Paragraph('Comptes de messagerie (%d) — sans les mots de passe'
                                   % len(mails), S['h2']))
            story.append(_pdf_data_table(
                tk, ['Client', 'Adresse', 'Protocole', 'Entrant', 'Sortant', 'Mot de passe'],
                rows, width, [0.12, 0.28, 0.12, 0.21, 0.21, 0.06]))
        nouveau = info.get('mail_new_outlook')
        if nouveau and nouveau.get('installed'):
            story.append(Paragraph('Nouvel Outlook : installé — %s'
                                   % _pdf_escape(nouveau.get('note')), S['small']))

        # ── Applications par défaut ───────────────────────────────────────────
        # Lecteurs réseau mappés et redémarrage en attente ont rejoint,
        # respectivement, Partages & lecteurs réseau et Environnement & hygiène.
        table = _pdf_kv_table(tk, [
            ('Navigateur par défaut', info.get('default_browser')),
            ('Client mail par défaut', info.get('default_mail')),
            ('Navigateurs installés',
             ', '.join('%s %s' % (b.get('name'), b.get('version') or '')
                       for b in info['installed_browsers']).strip()
             if info.get('installed_browsers') else None),
            ('Types de fichiers',
             ', '.join('%s → %s' % (a.get('extension'), a.get('name'))
                       for a in info['file_type_defaults'])
             if info.get('file_type_defaults') else None),
        ], width)
        if table:
            story.append(Paragraph('Applications par défaut', S['h2']))
            story.append(table)

        # ── Comptes utilisateurs ─────────────────────────────────────────────
        comptes = info.get('users_details') or []
        if comptes:
            rows = [[Paragraph('<b>%s</b>' % _pdf_escape(u['name']), S['body']),
                     Paragraph(_pdf_escape(u['status']), S['body']),
                     Paragraph(_pdf_escape(u['role']), S['body']),
                     Paragraph(_pdf_escape(u['account_type']), S['body']),
                     Paragraph("N'expire jamais" if u.get('password_never_expires') else 'Expire',
                               S['body']),
                     Paragraph(_pdf_escape(u.get('last_logon') or 'jamais'), S['mono'])]
                    for u in comptes]
            story.append(Paragraph('Comptes utilisateurs locaux (%d)' % len(comptes), S['h2']))
            story.append(_pdf_data_table(
                tk, ['Compte', 'État', 'Type', 'Compte', 'Mot de passe', 'Connexion'],
                rows, width, [0.22, 0.13, 0.19, 0.12, 0.18, 0.16]))
        elif info.get('users'):
            # Collecte antérieure : uniquement la forme textuelle. La table
            # ci-dessus la couvre déjà quand `users_details` existe — l'écrire
            # aussi ici doublonnerait les mêmes comptes.
            story.append(Paragraph('Comptes utilisateurs locaux (%d)' % len(info['users']), S['h2']))
            for u in info['users']:
                story.append(Paragraph(f'• {_pdf_escape(u)}', S['body']))

        # ── Profils utilisateurs ──────────────────────────────────────────────
        profils_utilisateurs = info.get('user_profiles') or []
        if profils_utilisateurs:
            rows = [[Paragraph('<b>%s</b>' % _pdf_escape(p.get('nom')), S['body']),
                     Paragraph(_pdf_escape(p.get('chemin')), S['small']),
                     Paragraph('%s Go' % p.get('taille_go'), S['mono']),
                     Paragraph(_pdf_escape(p.get('derniere_utilisation')), S['mono']),
                     Paragraph('complète' if p.get('mesure_complete') else 'interrompue',
                               S['small'])] for p in profils_utilisateurs]
            story.append(Paragraph('Profils utilisateurs (%d)' % len(profils_utilisateurs), S['h2']))
            story.append(_pdf_data_table(
                tk, ['Profil', 'Emplacement', 'Taille', 'Dernière utilisation', 'Mesure'],
                rows, width, [0.18, 0.34, 0.14, 0.20, 0.14]))

        # ── Processus les plus gourmands ────────────────────────────────────────
        top_cpu = info.get('top_processes_cpu') or []
        top_ram = info.get('top_processes_ram') or []
        if top_cpu or top_ram:
            story.append(Paragraph('Processus les plus gourmands (instantané)', S['h2']))
        if top_cpu:
            rows = [[Paragraph(_pdf_escape(p.get('name')), S['body']),
                     Paragraph('%s %%' % p.get('cpu_pct', 0), S['mono'])] for p in top_cpu]
            story.append(_pdf_data_table(tk, ['Processus', 'CPU'], rows, width, [0.70, 0.30]))
        if top_ram:
            rows = [[Paragraph(_pdf_escape(p.get('name')), S['body']),
                     Paragraph('%s Mo' % p.get('ram_mb', 0), S['mono'])] for p in top_ram]
            story.append(_pdf_data_table(tk, ['Processus', 'RAM'], rows, width, [0.70, 0.30]))

        # ── Incidents système ────────────────────────────────────────────────
        incidents = info.get('system_incidents') or []
        if incidents:
            rows = []
            for i in incidents:
                rows.append([
                    Paragraph('<b>%s</b>' % _pdf_escape(i['category']), S['body']),
                    Paragraph(str(i['count']), S['body']),
                    Paragraph(_pdf_escape(i['last_seen']), S['mono']),
                    Paragraph(_pdf_escape(i.get('disk')), S['small']),
                    Paragraph(_pdf_escape(i['message']), S['small']),
                ])
            story.append(Paragraph('Incidents système (%d jours)' % EVENT_WINDOW_DAYS, S['h2']))
            story.append(_pdf_data_table(
                tk, ['Type', 'Occurrences', 'Dernière', 'Disque', 'Message'],
                rows, width, [0.17, 0.10, 0.15, 0.22, 0.36]))

        # ── Erreurs système ───────────────────────────────────────────────────
        # Journal Système hors incidents déjà listés ci-dessus.
        erreurs_sys = info.get('system_errors') or []
        if erreurs_sys:
            rows = [[Paragraph(_pdf_escape('%s/%s' % (e.get('provider'), e.get('event_id'))), S['mono']),
                     Paragraph(str(e.get('count', '')), S['body']),
                     Paragraph(_pdf_escape(e.get('last_seen')), S['mono']),
                     Paragraph(_pdf_escape(e.get('message')), S['small'])] for e in erreurs_sys]
            story.append(Paragraph('Erreurs système (%d)' % len(erreurs_sys), S['h2']))
            story.append(_pdf_data_table(tk, ['Source', 'Occurrences', 'Dernière', 'Message'],
                                         rows, width, [0.24, 0.12, 0.16, 0.48]))

        # ── Erreurs applicatives ──────────────────────────────────────────────
        erreurs_app = info.get('application_errors') or []
        if erreurs_app:
            rows = []
            for a in erreurs_app:
                cause = a.get('exception') or '—'
                if a.get('module'):
                    cause += ' — %s' % a['module']
                rows.append([
                    Paragraph('<b>%s</b>' % _pdf_escape(a.get('application')), S['body']),
                    Paragraph(a.get('type'), S['body']),
                    Paragraph(str(a.get('count', '')), S['body']),
                    Paragraph(_pdf_escape(a.get('last_seen')), S['mono']),
                    Paragraph(_pdf_escape(cause), S['small']),
                ])
            story.append(Paragraph('Erreurs applicatives (%d)' % len(erreurs_app), S['h2']))
            story.append(_pdf_data_table(tk, ['Application', 'Type', 'Occurrences', 'Dernière', 'Cause'],
                                         rows, width, [0.24, 0.14, 0.10, 0.14, 0.38]))

        # ── Mises à jour disponibles ─────────────────────────────────────────
        maj = info.get('pending_updates') or []
        if maj:
            rows = [[Paragraph(_pdf_escape(u['title']), S['body']),
                     Paragraph(('KB%s' % u['kb']) if u.get('kb') else '—', S['mono']),
                     Paragraph(('%s Mo' % u['size_mb']) if u.get('size_mb') else '—', S['body']),
                     Paragraph('Sécurité' if u.get('security') else (u.get('severity') or '—'),
                               S['body'])] for u in maj]
            story.append(Paragraph('Mises à jour disponibles (%d) — recherche %s'
                                   % (len(maj), info.get('pending_updates_source', '')), S['h2']))
            story.append(_pdf_data_table(tk, ['Mise à jour', 'KB', 'Taille', 'Nature'],
                                         rows, width, [0.54, 0.14, 0.12, 0.20]))

        # ── Correctifs installés ─────────────────────────────────────────────
        # À côté des mises à jour disponibles : même sujet (le cycle de
        # correctifs Windows), plutôt que séparés par une dizaine de rubriques.
        correctifs = info.get('hotfixes') or []
        if correctifs:
            rows = [[Paragraph(_pdf_escape(h.get('id')), S['mono']),
                     Paragraph(_pdf_escape(h.get('installed_on')), S['body']),
                     Paragraph(_pdf_escape(h.get('description')), S['body'])] for h in correctifs]
            story.append(Paragraph('Correctifs Windows installés (%d)' % len(correctifs), S['h2']))
            story.append(_pdf_data_table(tk, ['Correctif', 'Installé le', 'Type'],
                                         rows, width, [0.30, 0.30, 0.40]))

        # ── Démarrage & services ──────────────────────────────────────────────
        services = info.get('stopped_auto_services') or []
        if services:
            rows = [[Paragraph(_pdf_escape(s['display_name']), S['body']),
                     Paragraph(_pdf_escape(s['name']), S['mono']),
                     Paragraph(_pdf_escape(s['state']), S['body'])] for s in services]
            story.append(Paragraph('Services automatiques arrêtés (%d)' % len(services), S['h2']))
            story.append(_pdf_data_table(tk, ['Service', 'Nom interne', 'État'],
                                         rows, width, [0.46, 0.36, 0.18]))

        demarrage = info.get('startup_programs') or []
        if demarrage:
            rows = [[Paragraph(_pdf_escape(p['name']), S['body']),
                     Paragraph(_pdf_escape(p.get('command')), S['small']),
                     Paragraph(_pdf_escape(p.get('location')), S['mono'])] for p in demarrage]
            story.append(Paragraph('Programmes au démarrage (%d)' % len(demarrage), S['h2']))
            story.append(_pdf_data_table(tk, ['Programme', 'Commande', 'Emplacement'],
                                         rows, width, [0.24, 0.50, 0.26]))

        demarrages = info.get('boot_times') or []
        if demarrages:
            rows = [[Paragraph(_pdf_escape(b.get('when')), S['mono']),
                     Paragraph('%s s' % b.get('secondes'), S['body']),
                     Paragraph('%s s' % b.get('noyau_s'), S['mono']),
                     Paragraph('%s s' % b.get('bureau_s'), S['mono'])] for b in demarrages]
            story.append(Paragraph(
                'Temps de démarrage — dernier %s s, moyenne %s s'
                % (info.get('boot_last_seconds', '?'),
                   info.get('boot_average_seconds', '?')), S['h2']))
            story.append(_pdf_data_table(
                tk, ['Démarrage', 'Total', 'Noyau', 'Ouverture de session'],
                rows, width, [0.34, 0.22, 0.22, 0.22]))

        # ── Arrêts & redémarrages ─────────────────────────────────────────────
        arrets = info.get('shutdown_history') or []
        if arrets:
            rows = [[Paragraph(_pdf_escape(h.get('when')), S['mono']),
                     Paragraph(h.get('action'), S['body']),
                     Paragraph('Oui' if h.get('planned') else 'Non', S['body']),
                     Paragraph(_pdf_escape(h.get('reason')), S['small']),
                     Paragraph(_pdf_escape(h.get('user')), S['small'])] for h in arrets]
            story.append(Paragraph('Arrêts & redémarrages (%d)' % len(arrets), S['h2']))
            story.append(_pdf_data_table(tk, ['Quand', 'Action', 'Planifié', 'Raison', 'Compte'],
                                         rows, width, [0.18, 0.12, 0.10, 0.38, 0.22]))

        taches = info.get('scheduled_tasks') or []
        if taches:
            rows = [[Paragraph('<b>%s</b>' % _pdf_escape(t['name']), S['body']),
                     Paragraph(_pdf_escape(t.get('state')), S['body']),
                     Paragraph(_pdf_escape(t.get('last_run') or 'jamais'), S['mono']),
                     Paragraph('Échec' if t.get('failed') else 'OK', S['body']),
                     Paragraph(_pdf_escape(t.get('action')), S['small'])] for t in taches]
            story.append(Paragraph('Tâches planifiées (%d)' % len(taches), S['h2']))
            story.append(_pdf_data_table(
                tk, ['Tâche', 'État', 'Dernière exécution', 'Résultat', 'Exécutable'],
                rows, width, [0.26, 0.12, 0.18, 0.12, 0.32]))

        # ── Logiciels ────────────────────────────────────────────────────────
        software = info.get('installed_software', [])
        if software:
            story.append(tk['PageBreak']())
            story.append(Paragraph(f'Logiciels installés ({len(software)})', S['h2']))
            rows = []
            for i, soft in enumerate(software, 1):
                if isinstance(soft, dict):
                    name, version = soft.get('name', ''), soft.get('version', '')
                    publisher, install = soft.get('publisher', ''), soft.get('install_date', '')
                    if soft.get('update_status') == 'obsolete':
                        maj = (f'<font color="#c27803" size="7.5">v'
                               f'{_pdf_escape(soft.get("latest_version") or "?")} disponible</font>')
                    elif soft.get('update_status') == 'a_jour':
                        maj = '<font color="#0e9f6e" size="7.5">À jour</font>'
                    else:
                        maj = '<font color="#9ca3af" size="7.5">—</font>'
                else:
                    name, version, publisher, install = str(soft), '', '', ''
                    maj = '<font color="#9ca3af" size="7.5">—</font>'
                rows.append([
                    Paragraph(f'<font color="#9ca3af" size="7">{i}</font>', S['body']),
                    Paragraph(_pdf_escape(name), S['body']),
                    Paragraph(f'<font face="Courier" size="7.5">'
                              f'{_pdf_escape(version)}</font>', S['body']),
                    Paragraph(_pdf_escape(publisher), S['body']),
                    Paragraph(f'<font size="7.5">{_pdf_escape(install)}</font>', S['body']),
                    Paragraph(maj, S['body']),
                ])
            story.append(_pdf_data_table(
                tk, ['#', 'Nom', 'Version', 'Éditeur', 'Installé le', 'Mise à jour'], rows, width,
                [0.05, 0.32, 0.13, 0.19, 0.12, 0.19]))

        story.append(Spacer(1, 10))
        story.append(Paragraph(
            f"Rapport produit par system-info-collector · collecte du "
            f"{_pdf_escape(info.get('timestamp') or 'N/A')} · {len(software)} logiciel(s), "
            f"{len(usb)} périphérique(s) USB, {len(ports)} port(s) en écoute.", S['small']))

        def _pied_de_page(canvas, document):
            """Repère de navigation : une page isolée reste identifiable."""
            canvas.saveState()
            canvas.setFont('Helvetica', 7)
            canvas.setFillColor(colors.HexColor('#9ca3af'))
            gauche = '%s — %s' % (info.get('hostname', ''), cible)
            canvas.drawString(doc.leftMargin, 8 * tk['mm'], gauche[:90])
            canvas.drawRightString(doc.leftMargin + doc.width, 8 * tk['mm'],
                                   'page %d' % document.page)
            canvas.setStrokeColor(colors.HexColor('#e5e7eb'))
            canvas.line(doc.leftMargin, 11 * tk['mm'],
                        doc.leftMargin + doc.width, 11 * tk['mm'])
            canvas.restoreState()

        # multiBuild : deux passes, indispensables pour paginer le sommaire.
        doc.multiBuild(_preparer_mise_en_page(tk, story),
                       onFirstPage=_pied_de_page, onLaterPages=_pied_de_page)
        with open(filename, 'rb') as f:
            return f.read(), filename

    except Exception as exc:
        # Un PDF qui échoue ne doit pas priver l'utilisateur de rapport.
        try:
            print(f'Erreur génération PDF ({exc}) — repli sur le rapport HTML')
        except Exception:
            pass
        return generate_html_report(info, client_id, client_name)


_SNAPSHOT_EXCLUDE = {'installed_software'}


def get_api_payload(info, client_id=None, client_name=None):
    """Construit le payload envoyé à /api/device-info.

    Les colonnes historiques restent alimentées à l'identique ; le snapshot
    complet part en plus dans `system_report` pour que le serveur puisse tout
    afficher sans avoir à parser le PDF.
    """
    # Formater la RAM pour correspondre aux options (ex: "16" → "16 Go")
    ram_value = info.get('ram_gb', '')
    if ram_value:
        try:
            ram_num = float(ram_value)
            ram_formatted = f"{int(ram_num)} Go" if ram_num == int(ram_num) else f"{ram_num} Go"
        except (ValueError, TypeError):
            ram_formatted = str(ram_value)
    else:
        ram_formatted = ''

    # GPU : préférer la version détaillée (avec VRAM) quand elle est disponible
    gpu_display = info.get('gpu', '')
    gpu_details = info.get('gpu_details', [])
    if gpu_details:
        gpu_display = ', '.join(
            g['name'] + (f" ({g['vram_gb']} GB)" if g.get('vram_gb') else '')
            for g in gpu_details
        )

    payload = {
        'mac_address': info.get('mac_address', ''),
        'ip_addresses': info.get('ip_addresses', []),
        'hostname': info.get('hostname', ''),
        'dns_name': info.get('dns_name', ''),
        'device_type': info.get('device_type', ''),
        'os_name': info.get('os_name', ''),
        'os_version': info.get('os_version', ''),
        'brand': info.get('brand', ''),
        'model': info.get('model', ''),
        'serial_number': info.get('serial_number', ''),
        'ram_gb': ram_formatted,  # Envoi avec "Go" (ex: "16 Go")
        'cpu': info.get('cpu', ''),
        'disk_total_gb': info.get('disk_total_gb', ''),
        'antivirus': info.get('antivirus', ''),
        'gpu': gpu_display,
        # IP publique + opérateur constatés depuis ce poste (get_public_ip_info) —
        # distincts de ip_addresses (réseau local).
        'public_ip': info.get('public_ip', ''),
        'public_ip_isp': info.get('public_ip_isp', ''),
        'installed_software': info.get('installed_software', []),
        # Ports TCP en écoute → colonne ports_ouverts
        'open_ports': [p['port'] for p in info.get('listening_ports', [])],
        # Périphériques créés automatiquement côté serveur. Seuls les
        # matériels USB réels sont transmis : hubs racine, contrôleurs et nœuds
        # composites restent dans le rapport mais n'ont pas leur place dans un
        # inventaire client.
        'monitors': info.get('monitors', []),
        'printers': info.get('printers', []),
        'usb_devices': [d for d in info.get('usb_devices', []) if d.get('inventoriable')],
        # Licences dont la clé complète a été récupérée : elles alimentent la
        # section « Licences logiciels » de la fiche appareil. Une licence sans
        # clé exploitable (licence numérique, Click-to-Run, KMS) n'aurait rien
        # à y inscrire.
        'licenses': [l for l in info.get('licenses', []) if l.get('full_key')],
        # Snapshot complet pour la fiche système
        'system_report': {k: v for k, v in info.items() if k not in _SNAPSHOT_EXCLUDE},
    }

    # Ajouter client targeting
    if client_id:
        payload['client_id'] = client_id
    if client_name:
        payload['client_name'] = client_name

    return payload


def send_to_parcinfo(info, server_url, token=None, client_id=None, client_name=None):
    """Envoie les infos à ParcInfo via l'API.

    Args:
        info: Dict des informations système
        server_url: URL du serveur ParcInfo
        token: Token d'authentification (optionnel)
        client_id: ID du client cible (optionnel)
        client_name: Nom du client cible (optionnel - sera résolu en ID)
    """
    try:
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'

        payload = json.dumps(get_api_payload(info, client_id, client_name))

        request = Request(
            f"{server_url.rstrip('/')}/api/device-info",
            data=payload.encode('utf-8'),
            headers=headers,
            method='POST'
        )

        # Le payload complet (logiciels + snapshot) est nettement plus lourd que
        # l'ancien : 30 s laissent le serveur écrire sans couper la connexion
        with urlopen(request, timeout=30) as response:
            return True, json.loads(response.read().decode('utf-8'))
    except URLError as e:
        return False, f"Connection error: {e.reason}"
    except Exception as e:
        return False, str(e)


def upload_report_to_parcinfo(report_content, report_file, server_url, device_id,
                              client_id, token=None):
    """Envoie le rapport (PDF ou HTML) à ParcInfo en tant que document joint.

    Args:
        report_content: Contenu du rapport (bytes ou str)
        report_file: Chemin du fichier rapport (détermine le type)
        server_url: URL du serveur ParcInfo
        device_id: ID de l'appareil créé/mis à jour
        client_id: ID du client
        token: jeton du collecteur, si le serveur en exige un
    """
    try:
        if report_file and report_file.endswith('.pdf'):
            content_type = 'application/pdf'
            filename = report_file
        else:
            content_type = 'text/html'
            filename = report_file or 'report.html'

        boundary = '----FormBoundary' + str(uuid.uuid4()).replace('-', '')
        body = io.BytesIO()

        body.write(f'--{boundary}\r\n'.encode())
        body.write(b'Content-Disposition: form-data; name="device_id"\r\n\r\n')
        body.write(f'{device_id}\r\n'.encode())

        body.write(f'--{boundary}\r\n'.encode())
        body.write(b'Content-Disposition: form-data; name="client_id"\r\n\r\n')
        body.write(f'{client_id}\r\n'.encode())

        body.write(f'--{boundary}\r\n'.encode())
        body.write(f'Content-Disposition: form-data; name="report"; filename="{filename}"\r\n'.encode())
        body.write(f'Content-Type: {content_type}\r\n\r\n'.encode())
        if isinstance(report_content, str):
            body.write(report_content.encode('utf-8'))
        else:
            body.write(report_content)
        body.write(b'\r\n')

        body.write(f'--{boundary}--\r\n'.encode())

        headers = {'Content-Type': f'multipart/form-data; boundary={boundary}'}
        if token:
            headers['Authorization'] = f'Bearer {token}'

        request = Request(
            f"{server_url.rstrip('/')}/api/device-info/upload-report",
            data=body.getvalue(),
            headers=headers,
            method='POST'
        )

        with urlopen(request, timeout=30) as response:
            return True, json.loads(response.read().decode('utf-8'))
    except Exception as e:
        return False, str(e)


def send_wifi_credentials_to_parcinfo(profiles, server_url, device_id, client_id, token=None):
    """Envoie les réseaux Wi-Fi détectés (voir get_wifi_profiles) à ParcInfo.

    Volontairement un appel séparé de send_to_parcinfo plutôt qu'un champ du
    payload principal : les réseaux (et leur mot de passe, si collecté)
    n'ont pas leur place dans le snapshot système envoyé à /api/device-info,
    qui atterrit tel quel dans la fiche appareil et le PDF — le serveur range
    ceux-ci dans la table des identifiants, chiffrés.
    """
    if not profiles:
        return True, {'status': 'success', 'created': 0, 'updated': 0}
    try:
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'

        payload = json.dumps({
            'device_id': device_id,
            'client_id': client_id,
            'profiles': profiles,
        })

        request = Request(
            f"{server_url.rstrip('/')}/api/device-info/wifi-credentials",
            data=payload.encode('utf-8'),
            headers=headers,
            method='POST'
        )

        with urlopen(request, timeout=30) as response:
            return True, json.loads(response.read().decode('utf-8'))
    except URLError as e:
        return False, f"Connection error: {e.reason}"
    except Exception as e:
        return False, str(e)


def fetch_clients(server_url, mac_address=None, token=None):
    """Récupère la liste des clients depuis ParcInfo.

    Quand une (ou plusieurs) adresse(s) MAC sont fournies et que le serveur
    connaît déjà cette machine par l'une d'elles, il renvoie en plus le
    client auquel elle est rattachée : le collecteur peut alors le
    présélectionner au lieu de demander à l'utilisateur de le retrouver dans
    une liste qui compte parfois des dizaines d'entrées.

    `mac_address` accepte une chaîne (une seule adresse, compatibilité) ou
    une liste — voir get_all_mac_addresses() : une machine avec plusieurs
    cartes (VPN, Hyper-V/WSL, VirtualBox…) n'a pas de « bonne » adresse
    évidente à envoyer seule, le serveur compare donc à toutes à la fois.

    Retourne (clients, client_suggéré_ou_None).
    """
    try:
        url = "%s/api/clients-public" % server_url.rstrip('/')
        macs = [mac_address] if isinstance(mac_address, str) else list(mac_address or [])
        macs = [m for m in macs if m]
        if macs:
            url += '?' + '&'.join('mac=' + quote(m) for m in macs)
        entete = {'Authorization': 'Bearer %s' % token} if token else {}
        with urlopen(Request(url, headers=entete), timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
        # Le serveur répond une liste simple, ou un objet quand il a une
        # suggestion ; les deux formes doivent être acceptées.
        if isinstance(data, dict):
            clients = data.get('clients') or []
            return (clients if isinstance(clients, list) else [], data.get('suggested_client'))
        return (data if isinstance(data, list) else [], None)
    except Exception:
        return ([], None)


def discover_parcinfo_mdns(timeout=3):
    """Découvre les instances ParcInfo sur le réseau local par mDNS.

    Chaque instance s'annonce elle-même (app.py::_register_mdns, un nom
    unique par poste depuis la 2.18.2) — bien plus rapide et fiable qu'un
    balayage de sous-réseau : le port réel est connu directement, pas
    seulement le 3456 par défaut (utile pour une instance Docker republiée
    sur un autre port, par exemple), et rien n'est à deviner.

    Limite connue : un conteneur Docker en réseau « bridge » (le mode par
    défaut) ne relaie généralement pas le trafic multicast mDNS vers le
    réseau local — une telle instance restera probablement invisible ici
    tant qu'elle n'est pas passée en réseau « host ». Le scan de sous-réseau
    (scan_network_for_parcinfo, côté collecteur GUI) reste le repli pour ce
    cas.

    Retourne une liste de {'url', 'ip', 'port', 'nom', 'version', 'docker'},
    vide si zeroconf est absent ou si rien n'a répondu dans le délai.
    """
    try:
        from zeroconf import Zeroconf, ServiceBrowser
        import time
    except ImportError:
        return []

    trouves = {}

    class _Ecouteur:
        def add_service(self, zc, type_service, nom):
            if not nom.startswith('ParcInfo'):
                return
            try:
                info = zc.get_service_info(type_service, nom, timeout=1500)
            except Exception:
                info = None
            if not info:
                return
            try:
                adresses = info.parsed_addresses()
            except Exception:
                adresses = []
            if not adresses:
                return
            proprietes = {}
            for cle, valeur in (info.properties or {}).items():
                try:
                    cle_txt = cle.decode('utf-8', 'replace') if isinstance(cle, (bytes, bytearray)) else str(cle)
                    val_txt = valeur.decode('utf-8', 'replace') if isinstance(valeur, (bytes, bytearray)) else str(valeur)
                    proprietes[cle_txt] = val_txt
                except Exception:
                    continue
            url = 'http://%s:%s' % (adresses[0], info.port)
            trouves[url] = {
                'url': url, 'ip': adresses[0], 'port': info.port,
                'nom': proprietes.get('hostname') or nom.split('.')[0],
                'version': proprietes.get('version') or '',
                'docker': proprietes.get('docker') == '1',
            }

        def update_service(self, *a, **k):
            pass

        def remove_service(self, *a, **k):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_http._tcp.local.", _Ecouteur())
        time.sleep(timeout)
    except Exception:
        pass
    finally:
        try:
            zc.close()
        except Exception:
            pass
    return list(trouves.values())


#: Ports ParcInfo sondés lors d'un balayage de sous-réseau. 3456 est le port
#: natif par défaut ; 5010 est le port hôte publié par docker-compose.yml
#: pour une instance conteneurisée (docker-compose.yml: "5010:3456") — le
#: seul moyen de la détecter, puisque le réseau "bridge" de Docker (mode par
#: défaut) ne relaie généralement pas le multicast mDNS que discover_
#: parcinfo_mdns() utilise.
SCAN_PORTS = (3456, 5010)


def get_local_network_range():
    """Détermine la plage de réseau local (ex: 192.168.1.0/24)."""
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)

        octets = local_ip.split('.')
        if len(octets) == 4:
            return '.'.join(octets[:3]), local_ip
    except Exception:
        pass
    return None, None


def _compter_clients(server_url, timeout):
    """Nombre de clients visibles sur une instance — confirme au passage que
    c'est bien du ParcInfo (l'endpoint existe et répond en JSON), pas un
    service HTTP quelconque tombé sur le même port. None si injoignable ou
    protégé par un jeton collecteur (pas encore saisi à ce stade du flux)."""
    try:
        with urlopen('%s/api/clients-public' % server_url, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                return len((data.get('clients') or []))
            return 0
    except Exception:
        return None


def _instance_info(server_url, timeout):
    """Interroge /api/instance-info (hostname/version/docker) pour étiqueter
    une instance trouvée par balayage direct plutôt que par mDNS — seul ce
    dernier expose ces informations sans passer par une requête HTTP dédiée
    (TXT record). None si injoignable, pas du JSON, ou serveur antérieur à
    l'ajout de cette route (endpoint absent, 404) : le hostname reste alors
    inconnu et l'appelant retombe sur l'IP comme libellé."""
    try:
        with urlopen('%s/api/instance-info' % server_url, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
            if isinstance(data, dict) and 'hostname' in data:
                return data
    except Exception:
        pass
    return None


def scan_network_for_parcinfo(timeout=2, progress_callback=None):
    """
    Découvre les instances ParcInfo sur le réseau local — d'abord par mDNS
    (rapide, donne le port réel, pas seulement le 3456 par défaut, et le
    hostname directement via le TXT record), puis par balayage du
    sous-réseau en repli pour ce que le mDNS ne trouve pas (typiquement une
    instance Docker en réseau « bridge », voir SCAN_PORTS ci-dessus).

    Le balayage de repli sonde chaque hôte du /24 sur SCAN_PORTS en
    parallèle (ThreadPoolExecutor) — indispensable : à un hôte par seconde en
    séquentiel, un /24 sur deux ports prendrait plusieurs minutes. Un port
    ouvert confirmé ParcInfo (_compter_clients) est ensuite interrogé via
    /api/instance-info pour en obtenir le hostname, comme pour une instance
    trouvée par mDNS — sans quoi seule l'IP nue l'identifierait.

    Retourne une liste de serveurs trouvés, dédupliqués par URL :
    [{"url", "ip", "clients", "nom", "version", "docker"}, ...]
    """
    servers = {}

    if progress_callback:
        progress_callback("Recherche via mDNS...")
    try:
        for trouve in discover_parcinfo_mdns(timeout=3):
            entree = dict(trouve)
            compte = _compter_clients(entree['url'], timeout)
            entree['clients'] = compte if compte is not None else 0
            servers[entree['url']] = entree
    except Exception:
        pass

    base, local_ip = get_local_network_range()
    if not base:
        return list(servers.values())

    candidats = [ip for ip in
                 ('%s.%d' % (base, i) for i in range(1, 255))
                 if ip != local_ip]

    def sonder(ip):
        trouvailles = []
        for port in SCAN_PORTS:
            server_url = 'http://%s:%d' % (ip, port)
            if server_url in servers:
                continue  # déjà trouvé par mDNS, inutile de le sonder deux fois
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                ouvert = sock.connect_ex((ip, port)) == 0
                sock.close()
            except Exception:
                ouvert = False
            if not ouvert:
                continue
            clients_count = _compter_clients(server_url, timeout)
            if clients_count is None:
                continue  # port ouvert mais pas un ParcInfo (ou jeton requis)
            info = _instance_info(server_url, timeout) or {}
            trouvailles.append({
                'url': server_url, 'ip': ip, 'clients': clients_count,
                'nom': info.get('hostname') or ip,
                'version': info.get('version') or '',
                'docker': bool(info.get('docker')),
            })
        return trouvailles

    if progress_callback:
        progress_callback('Balayage de %s.0/24 (ports %s)...'
                          % (base, '/'.join(str(p) for p in SCAN_PORTS)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
        futures = {executor.submit(sonder, ip): ip for ip in candidats}
        termines = 0
        for future in concurrent.futures.as_completed(futures):
            termines += 1
            if progress_callback and termines % 25 == 0:
                progress_callback('Balayage… %d/%d hôtes vérifiés' % (termines, len(candidats)))
            try:
                for entree in future.result():
                    servers.setdefault(entree['url'], entree)
            except Exception:
                continue

    return list(servers.values())
