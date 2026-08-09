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

import io
import json
import os
import platform
import re
import socket
import string
import subprocess
import sys
import uuid
from datetime import datetime
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

__all__ = [
    'IS_WINDOWS', 'IS_MAC', 'IS_LINUX',
    'collect_system_info', 'build_summary_lines', 'build_summary_sections',
    'generate_pdf_report', 'generate_html_report',
    'get_api_payload', 'send_to_parcinfo', 'upload_report_to_parcinfo',
    'fetch_clients', 'is_elevated',
]

# Platform detection
IS_WINDOWS = sys.platform == 'win32'
IS_MAC = sys.platform == 'darwin'
IS_LINUX = sys.platform == 'linux'

COLLECTOR_VERSION = '3.0'

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
    """Récupère l'adresse MAC réelle de la machine."""
    try:
        mac = uuid.getnode()
        mac_str = ':'.join(['{:02x}'.format((mac >> (i << 3)) & 0xff) for i in range(5, -1, -1)])
        return mac_str.upper()
    except Exception:
        return ""


def get_hostname():
    """Récupère le nom d'hôte exact."""
    try:
        return socket.gethostname()
    except Exception:
        return ""


def get_fqdn():
    """Nom DNS complet de la machine (alimente la colonne nom_dns de ParcInfo)."""
    try:
        fqdn = socket.getfqdn()
        # getfqdn() retombe sur le hostname court quand la résolution échoue :
        # dans ce cas il n'y a pas de vrai nom DNS à remonter
        return fqdn if fqdn and '.' in fqdn else ''
    except Exception:
        return ''


def get_ip_addresses():
    """Récupère toutes les adresses IP locales."""
    ips = []
    try:
        hostname = socket.gethostname()
        ips = socket.gethostbyname_ex(hostname)[2]
    except Exception:
        pass
    return [ip for ip in ips if not ip.startswith('127.')]


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
            mac_ver = platform.mac_ver()
            os_version = mac_ver[0]
            full_os_name = "macOS"
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
        "$tz = $null; try { $tz = (Get-TimeZone -ErrorAction SilentlyContinue).Id } catch {}; "
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
        delta = datetime.utcnow() - datetime(annee, mois, jour)
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
        "| Select-Object MountPoint,VolumeStatus,ProtectionStatus) } catch {}; "
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

    bitlocker = [
        f"{b.get('MountPoint')}: {b.get('VolumeStatus', 'Inconnu')} "
        f"(Protection: {b.get('ProtectionStatus', 'Inconnu')})"
        for b in _as_list(security_data.get('BitLocker')) if b.get('MountPoint')
    ]
    if bitlocker:
        info['bitlocker'] = bitlocker

    tpm = security_data.get('Tpm')
    if tpm:
        info['tpm_present'] = bool(tpm.get('TpmPresent'))
        info['tpm_enabled'] = bool(tpm.get('TpmEnabled'))

    if security_data.get('SecureBoot') is not None:
        info['secure_boot'] = bool(security_data.get('SecureBoot'))

    return info


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

    # Disques physiques : type (SSD/HDD) + état de santé SMART (natif Windows 8+)
    physical_disks_data = _win_powershell_json(
        "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,HealthStatus,Size,OperationalStatus "
        "| ConvertTo-Json -Compress"
    )
    if physical_disks_data:
        physical_list = []
        for d in _as_list(physical_disks_data):
            name = d.get('FriendlyName') or 'Disque'
            media = d.get('MediaType') or 'Inconnu'
            health = d.get('HealthStatus') or 'Inconnu'
            op_status = d.get('OperationalStatus') or ''
            size_gb = round((d.get('Size') or 0) / (1024 ** 3), 1)
            entry = f"{name} — {media} — {size_gb} GB — Santé (SMART): {health}"
            if op_status and op_status != 'OK':
                entry += f" ({op_status})"
            physical_list.append(entry)
        if physical_list:
            info['physical_disks'] = physical_list

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
        "$adapters = @(); try { $adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue "
        "| Where-Object Status -eq 'Up' | ForEach-Object { "
        "$ips = @(Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Select-Object IPAddress,PrefixLength); "
        "[PSCustomObject]@{ Name=$_.Name; InterfaceDescription=$_.InterfaceDescription; "
        "LinkSpeed=$_.LinkSpeed; MacAddress=$_.MacAddress; Virtual=$_.Virtual; "
        "MediaType=$_.MediaType; IPs=$ips } }) } catch {}; "
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
        "@{N='Msg';E={($_.Message -split [char]10)[0].Trim()}}) } catch {} }; "
        "$out | ConvertTo-Json -Compress -Depth 3" % (EVENT_WINDOW_DAYS, specs_ps),
        timeout=90,
    )

    libelles = {(p, i): (lib, lvl) for p, ids, lib, lvl in _EVENT_SPECS for i in ids}
    groupes = {}
    for e in _as_list(data):
        cle = (_clean(e.get('ProviderName')), e.get('Id'), _clean(e.get('Msg')))
        lib, niveau = libelles.get((cle[0], cle[1]), ('Incident système', 'warn'))
        entree = groupes.setdefault(cle, {
            'category': lib, 'level': niveau, 'provider': cle[0], 'event_id': cle[1],
            'message': cle[2], 'count': 0, 'last_seen': '',
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


# Étapes de la collecte Windows, avec le libellé montré à l'utilisateur. La
# collecte dure une bonne minute : sans retour, elle est indiscernable d'un
# blocage.
_WIN_STEPS = [
    ('Matériel de base', lambda: _win_base_hardware()),
    ('Processeur et mémoire', lambda: _win_core()),
    ('Détail matériel', lambda: _win_hardware_detail()),
    ('Écrans, imprimantes et disques', lambda: _win_inventory()),
    ('Licences et correctifs', lambda: _win_licensing()),
    ('Sécurité', lambda: _win_security()),
    ('Comptes utilisateurs', lambda: _win_users()),
    ('Batterie et réseau', lambda: _win_extras()),
    ('Diagnostic (incidents, services, tâches)', lambda: _win_diagnostics()),
    ('Configuration réseau', lambda: _win_network()),
    ('Environnement (WSUS, domaine, temps)', lambda: _win_enterprise()),
    ('Hygiène système', lambda: _win_hygiene()),
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

    suffixe = _clean(data.get('Suffix'))
    if suffixe:
        info['dns_suffixes'] = suffixe

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
        "$rdp=$null; try { $rdp=(Get-ItemProperty "
        "'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' "
        "-ErrorAction SilentlyContinue).fDenyTSConnections } catch {}; "
        "$nla=$null; try { $nla=(Get-ItemProperty "
        "'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server\\WinStations\\RDP-Tcp' "
        "-ErrorAction SilentlyContinue).UserAuthentication } catch {}; "
        "$rp=@(); try { $rp=@(Get-ComputerRestorePoint -ErrorAction SilentlyContinue "
        "| Select-Object -Last 3 -Property Description,"
        "@{N='When';E={$_.ConvertToDateTime($_.CreationTime).ToString('yyyy-MM-dd HH:mm')}}) } catch {}; "
        "$temp=0; try { $temp=[math]::Round((Get-ChildItem $env:TEMP -Recurse -Force "
        "-ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum/1MB,0) } catch {}; "
        "[PSCustomObject]@{ Uac=$uac; Rdp=$rdp; Nla=$nla; Restore=$rp; TempMB=$temp } "
        "| ConvertTo-Json -Compress -Depth 4",
        timeout=90,
    )
    if not data:
        return info

    if data.get('Uac') is not None:
        info['uac_enabled'] = bool(data.get('Uac'))
    if data.get('Rdp') is not None:
        # fDenyTSConnections = 1 signifie que RDP est refusé
        info['rdp_enabled'] = not bool(data.get('Rdp'))
        if info['rdp_enabled'] and data.get('Nla') is not None:
            info['rdp_nla'] = bool(data.get('Nla'))

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

    # ── Écrans ─────────────────────────────────────────────────────────────
    try:
        displays = _mac_profiler_json('SPDisplaysDataType') or []
        monitors = []
        for gpu in displays:
            for screen in gpu.get('spdisplays_ndrvs', []) or []:
                name = screen.get('_name', '')
                if not name:
                    continue
                monitors.append({
                    'manufacturer': '',
                    'model': name,
                    'serial_number': screen.get('_spdisplays_display-serial-number', ''),
                    'year': '',
                })
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

    # ── Uptime ─────────────────────────────────────────────────────────────
    info.update(_unix_uptime())

    return info


# ════════════════════════════════════════════════════════════════════════════
# COLLECTE LINUX
# ════════════════════════════════════════════════════════════════════════════

def _unix_disks():
    """Disques via df (commun macOS / Linux)."""
    info = {}
    try:
        result = subprocess.run(['df', '-h'], capture_output=True, text=True, timeout=10)
        lines = result.stdout.strip().split('\n')[1:]  # Skip header
        disk_list = []
        total_disk = 0.0
        total_free = 0.0
        seen = set()
        for line in lines:
            parts = line.split()
            if len(parts) < 4:
                continue
            device, size_str, used_str, avail_str = parts[0], parts[1], parts[2], parts[3]
            # Les pseudo-systèmes de fichiers gonflent artificiellement le total
            if not device.startswith('/dev/') or device in seen:
                continue
            seen.add(device)

            def to_gb(value):
                value = value.strip()
                try:
                    if value.endswith('T'):
                        return float(value[:-1]) * 1024
                    if value.endswith('G'):
                        return float(value[:-1])
                    if value.endswith('M'):
                        return float(value[:-1]) / 1024
                except ValueError:
                    pass
                return None

            size_gb = to_gb(size_str)
            if size_gb is None:
                continue
            used_gb = to_gb(used_str) or 0
            free_gb = to_gb(avail_str) or 0
            disk_list.append(
                f"{device} — {round(size_gb, 1)} GB total, "
                f"{round(used_gb, 1)} GB utilisés, {round(free_gb, 1)} GB libres"
            )
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
            for d in devices:
                if d.get('name', '').startswith(('loop', 'ram', 'sr')):
                    continue
                media = 'HDD' if d.get('rota') in (True, '1', 1) else 'SSD'
                physical.append(f"{d.get('model') or d.get('name')} — {media} — {d.get('size', '?')}")
            if physical:
                info['physical_disks'] = physical
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

                                    software[name] = {
                                        'name': name,
                                        'version': version,
                                        'publisher': publisher,
                                        'install_date': install_date,
                                    }
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            pass

    elif IS_MAC:
        names = set()
        # /Applications
        try:
            result = subprocess.run(['ls', '/Applications'], capture_output=True, text=True, timeout=10)
            names.update(app.replace('.app', '') for app in result.stdout.split('\n') if app.endswith('.app'))
        except Exception:
            pass

        # /usr/local/opt (Homebrew)
        try:
            result = subprocess.run(['ls', '/usr/local/opt'], capture_output=True, text=True, timeout=10)
            names.update(pkg for pkg in result.stdout.split('\n') if pkg.strip())
        except Exception:
            pass

        # pkgutil pour les packages installés
        try:
            result = subprocess.run(['pkgutil', '--packages'], capture_output=True, text=True, timeout=15)
            names.update(line.strip() for line in result.stdout.split('\n') if line.strip())
        except Exception:
            pass

        for name in names:
            software[name] = {'name': name, 'version': '', 'publisher': '', 'install_date': ''}

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


def collect_system_info(progress=None, test_debit=False, url_debit=None, on_data=None):
    """Collecte toutes les infos système.

    `progress` est un rappel optionnel appelé avec (fraction entre 0 et 1,
    libellé de l'étape en cours). Il permet aux deux collecteurs d'afficher la
    même progression sans dupliquer la liste des étapes.

    `on_data` reçoit une copie de l'état partiel après chaque étape : l'interface
    graphique remplit ainsi ses onglets au fil de l'eau au lieu de tout afficher
    à la fin.
    """
    _report(progress, 0.02, 'Identification de la machine')
    info = {
        'collector_version': COLLECTOR_VERSION,
        'timestamp': datetime.utcnow().isoformat(),
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

    # Logiciels
    _report(progress, 0.82, 'Inventaire logiciel')
    info['installed_software'] = get_installed_software()
    _publier(on_data, info)

    # Périphériques USB connectés. Isolé de la collecte système : une erreur ici
    # (droits, commande absente) ne doit pas priver le rapport du reste.
    _report(progress, 0.88, 'Mesures réseau')
    try:
        info.update(measure_network(info, test_debit=test_debit, url_debit=url_debit))
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
        ('Architecture', info.get('architecture')),
        ('Installé le', info.get('os_install_date')),
        ('Domaine / Groupe', info.get('domain') or info.get('workgroup')),
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
        ('Suffixes DNS', ', '.join(info.get('dns_suffixes') or []) or None),
        ('Proxy', proxy),
    ]
    if dns:
        s['listes'].append({'titre': 'Serveurs DNS', 'elements': dns})
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
    sections.append(s)

    # ── Sécurité ───────────────────────────────────────────────────────────
    s = _sec('securite', 'Sécurité', '🛡')
    s['champs'] += [
        ('Antivirus', info.get('antivirus')),
        ('TPM', ('Présent et activé' if info.get('tpm_enabled')
                 else ('Présent mais désactivé' if info.get('tpm_present') else 'Absent'))
         if info.get('tpm_present') is not None else None),
        ('Secure Boot', ('Activé' if info['secure_boot'] else 'Désactivé')
         if info.get('secure_boot') is not None else None),
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
    if info.get('bitlocker'):
        s['listes'].append({'titre': 'Chiffrement', 'elements': list(info['bitlocker'])})
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
    sections.append(s)

    # ── Diagnostic ─────────────────────────────────────────────────────────
    s = _sec('diagnostic', 'Diagnostic', '🩺')
    if info.get('unexpected_shutdowns') is not None:
        s['champs'].append(('Arrêts inattendus (30 j)', info['unexpected_shutdowns']))
    if info.get('system_incidents'):
        s['listes'].append({'titre': 'Incidents système (30 derniers jours)', 'elements': [
            '%s ×%s%s%s' % (i.get('category', '?'), i.get('count', 1),
                            ' — dernier %s' % i['last_seen'] if i.get('last_seen') else '',
                            ' — %s' % i['disk'] if i.get('disk') else '')
            if isinstance(i, dict) else str(i)
            for i in info['system_incidents']]})
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
    if info.get('smb_shares'):
        s['listes'].append({'titre': 'Partages réseau', 'elements': [
            '%s → %s%s' % (x.get('name', '?'), x.get('path', '?'),
                           ' (administratif)' if x.get('administrative') else '')
            if isinstance(x, dict) else str(x)
            for x in info['smb_shares']]})
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
    sections.append(s)

    # ── Hygiène ────────────────────────────────────────────────────────────
    s = _sec('hygiene', 'Hygiène système', '🧹')
    s['champs'] += [
        ('UAC', ('activé' if info['uac_enabled'] else 'désactivé')
         if info.get('uac_enabled') is not None else None),
        ('Bureau à distance', ('activé%s' % (' (NLA)' if info.get('rdp_nla') else ' — sans NLA')
                               if info['rdp_enabled'] else 'désactivé')
         if info.get('rdp_enabled') is not None else None),
        ('Points de restauration',
         (len(info['restore_points']) or 'aucun') if isinstance(info.get('restore_points'), list)
         else info.get('restore_points')),
        ('Fichiers temporaires', ('%s Mo' % info['temp_files_mb'])
         if info.get('temp_files_mb') is not None else None),
    ]
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
        elements = []
        for soft in software:
            if isinstance(soft, dict):
                libelle = soft.get('name', '')
                if soft.get('version'):
                    libelle += ' (v%s)' % soft['version']
                extras = [x for x in (soft.get('publisher'), soft.get('install_date')) if x]
                if extras:
                    libelle += '  [' + ' · '.join(str(e) for e in extras) + ']'
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
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
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

    bitlocker_off = [v for v in info.get('bitlocker', []) if re.search(r'(non chiffr|not encrypted|off|déciffr)', v, re.I)]
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
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
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

    for profile in info.get('firewall', []):
        level = 'danger' if re.search(r'(désactiv|disabled|off)', profile, re.I) else 'ok'
        items.append(('Pare-feu', profile, level))

    for vol in info.get('bitlocker', []):
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
        else:
            name, version, publisher, install = str(soft), '', '', ''
        rows.append(
            f'<tr><td class="idx">{i}</td><td>{_esc(name)}</td>'
            f'<td class="mono">{_esc(version)}</td><td>{_esc(publisher)}</td>'
            f'<td class="mono">{_esc(install)}</td></tr>')
    return ('<div class="scroll"><table class="tbl"><thead><tr><th>#</th><th>Nom</th>'
            '<th>Version</th><th>Éditeur</th><th>Installé le</th></tr></thead>'
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

    generated = datetime.utcnow().strftime('%d/%m/%Y à %H:%M:%S UTC')
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
        generated = datetime.utcnow().strftime('%d/%m/%Y à %H:%M:%S UTC')
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

        # ── Identification / Système ─────────────────────────────────────────
        bios = info.get('bios_version', '')
        if bios and info.get('bios_release_date'):
            bios = f"{bios} ({info['bios_release_date']})"
        uptime = _num(info.get('uptime_hours'))

        for titre, rows in (
            ('Identification', [
                ('Nom de machine', info.get('hostname')),
                ('Nom DNS', info.get('dns_name')),
                ('Adresse MAC', info.get('mac_address')),
                ('Adresse(s) IP', ', '.join(info.get('ip_addresses', []))),
                ('Marque', info.get('brand')),
                ('Modèle', info.get('model')),
                ('Numéro de série', info.get('serial_number')),
                ('Domaine / Groupe de travail', info.get('domain') or info.get('workgroup')),
            ]),
            ('Système & matériel', [
                ("Système d'exploitation", info.get('os_name')),
                ('Version', info.get('os_version')),
                ('Détail plateforme', info.get('platform')),
                ('BIOS', bios),
                ('Processeur', info.get('cpu')),
                ('Cœurs', info.get('cpu_cores')),
                ('Carte graphique', info.get('gpu')),
                ('Uptime', f'{uptime / 24:.1f} jour(s)' if uptime is not None else None),
            ]),
        ):
            table = _pdf_kv_table(tk, rows, width)
            if table:
                story.append(Paragraph(titre, S['h2']))
                story.append(table)

        # ── Sécurité ─────────────────────────────────────────────────────────
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
        for vol in info.get('bitlocker', []):
            sec_rows.append(('BitLocker', vol,
                             'warn' if re.search(r'(non chiffr|not encrypted|off)', vol, re.I) else 'ok'))
        if info.get('last_windows_update'):
            sec_rows.append(('Dernière mise à jour', info['last_windows_update'], 'info'))
        if info.get('oem_product_key'):
            sec_rows.append(('Clé OEM (firmware)', info['oem_product_key'], 'info'))

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

        # ── Disques logiques ─────────────────────────────────────────────────
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

        # ── Ports en écoute (cartes) ─────────────────────────────────────────
        ports = info.get('listening_ports', [])
        story.append(Paragraph(f'Ports en écoute ({len(ports)})', S['h2']))
        story.extend(_pdf_port_cards(tk, ports, width))

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

        # ── Listes complémentaires ───────────────────────────────────────────
        for titre, items in (
            ('Barrettes mémoire',
             [format_memory_module(m) for m in info.get('memory_modules', [])]),
            ('Disques physiques', info.get('physical_disks', [])),
            ('Fiabilité des disques',
             [format_reliability(r) for r in info.get('disk_reliability', [])]),
            ('Écrans', [format_monitor(m) for m in info.get('monitors', [])]),
            ('Imprimantes', [format_printer(p) for p in info.get('printers', [])]),
            ('Adaptateurs réseau', info.get('network_adapters', [])),
            ('Comptes utilisateurs locaux', info.get('users', [])),
        ):
            if items:
                story.append(Paragraph(f'{titre} ({len(items)})', S['h2']))
                for item in items:
                    story.append(Paragraph(f'• {_pdf_escape(item)}', S['body']))

        # ── Détail matériel ──────────────────────────────────────────────────
        table = _pdf_kv_table(tk, [
            ('Carte mère', info.get('motherboard')),
            ('Châssis', info.get('chassis_type')),
            ('Asset tag', info.get('asset_tag')),
            ('Cœurs physiques / logiques',
             '%s / %s' % (info.get('cpu_physical_cores'), info.get('cpu_logical_cores'))
             if info.get('cpu_physical_cores') else None),
            ('Fréquence maximale',
             '%s MHz' % info['cpu_max_clock_mhz'] if info.get('cpu_max_clock_mhz') else None),
            ('Architecture', info.get('architecture')),
            ('Socket', info.get('cpu_socket')),
            ('Nombre de sockets', info.get('cpu_sockets')),
            ('Cache L3',
             '%s Ko' % info['cpu_l3_cache_kb'] if info.get('cpu_l3_cache_kb') else None),
            ('Virtualisation matérielle',
             ('Activée' if info['cpu_virtualization'] else 'Désactivée')
             if isinstance(info.get('cpu_virtualization'), bool)
             else info.get('cpu_virtualization')),
            ('Hyperviseur détecté', 'Oui' if info.get('hypervisor_present') else None),
            ('Emplacements mémoire',
             '%s occupés sur %s (max %s Go)' % (info.get('memory_slots_used'),
                                                info.get('memory_slots_total'),
                                                info.get('memory_max_gb'))
             if info.get('memory_slots_total') else None),
            ("Date d'installation de Windows", info.get('os_install_date')),
            ('Build', info.get('os_build')),
            ('Fuseau horaire', info.get('timezone')),
            ('Propriétaire enregistré', info.get('registered_owner')),
            ('Session ouverte', info.get('logged_on_user')),
            ('Âge du matériel',
             '%g an(s) (depuis la date du BIOS)' % hardware_age_years(info.get('bios_release_date'))
             if hardware_age_years(info.get('bios_release_date')) is not None else None),
        ], width)
        if table:
            story.append(Paragraph('Détail matériel & système', S['h2']))
            story.append(table)

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

        # ── Configuration réseau ─────────────────────────────────────────────
        proxy = info.get('proxy') or {}
        wifi = info.get('wifi') or {}
        table = _pdf_kv_table(tk, [
            ('Passerelle par défaut', info.get('default_gateway')),
            ('Suffixes DNS', info.get('dns_suffixes')),
            ('Proxy', ('%s (%s)' % (proxy.get('server') or proxy.get('auto_config_url'),
                                    'actif' if proxy.get('enabled') else 'configuré mais inactif'))
             if proxy.get('server') or proxy.get('auto_config_url') else None),
            ('Réseau Wi-Fi', wifi.get('ssid')),
            ('Signal Wi-Fi', wifi.get('signal')),
        ], width)
        if table:
            story.append(Paragraph('Configuration réseau', S['h2']))
            story.append(table)

        profils = info.get('network_profiles') or []
        if profils:
            rows = [[Paragraph(_pdf_escape(p['name']), S['body']),
                     Paragraph(_pdf_escape(p.get('interface')), S['body']),
                     Paragraph(_pdf_escape(p.get('category')), S['body']),
                     Paragraph(_pdf_escape(p.get('connectivity')), S['body'])] for p in profils]
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

        adaptateurs = info.get('network_adapter_details') or []
        if adaptateurs:
            rows = []
            for a in adaptateurs:
                ips = ' · '.join('%s/%s' % (i['address'], i['prefix'])
                                 for i in a.get('ip_addresses') or []) or '—'
                rows.append([
                    Paragraph('<b>%s</b>' % _pdf_escape(a['name']), S['body']),
                    Paragraph('Physique' if a.get('physical') else 'Virtuelle', S['body']),
                    Paragraph(_pdf_escape(ips), S['mono']),
                    Paragraph(_pdf_escape(a.get('link_speed')), S['body']),
                    Paragraph(_pdf_escape(a.get('mac_address')), S['mono']),
                ])
            story.append(Paragraph('Adaptateurs réseau', S['h2']))
            story.append(_pdf_data_table(
                tk, ['Interface', 'Nature', 'Adresse IP / plage', 'Débit', 'MAC'],
                rows, width, [0.26, 0.13, 0.27, 0.14, 0.20]))

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

        # ── Environnement & hygiène ──────────────────────────────────────────
        rdp = None
        if info.get('rdp_enabled') is not None:
            rdp = 'Activé' if info['rdp_enabled'] else 'Désactivé'
            if info.get('rdp_enabled') and info.get('rdp_nla') is not None:
                rdp += ', NLA %s' % ('actif' if info['rdp_nla'] else 'INACTIF')
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
            ('Bureau à distance', rdp),
            ('Fichiers temporaires',
             '%s Mo récupérables' % info['temp_files_mb'] if info.get('temp_files_mb') else None),
            ('Points de restauration',
             ('%d disponible(s), dernier le %s' % (len(info['restore_points']),
                                                   info['restore_points'][-1]['when']))
             if info.get('restore_points') else
             ('Aucun — retour arrière impossible' if 'restore_points' in info else None)),
        ], width)
        if table:
            story.append(Paragraph('Environnement & hygiène système', S['h2']))
            story.append(table)

        # ── Cartes graphiques ────────────────────────────────────────────────
        gpus = info.get('gpu_details') or []
        if gpus:
            rows = [[Paragraph(_pdf_escape(g.get('name')), S['body']),
                     Paragraph(('%s GB' % g['vram_gb']) if g.get('vram_gb') else '—', S['body']),
                     Paragraph(_pdf_escape(g.get('resolution')), S['body']),
                     Paragraph(_pdf_escape(g.get('driver_version')), S['mono'])] for g in gpus]
            story.append(Paragraph('Cartes graphiques', S['h2']))
            story.append(_pdf_data_table(tk, ['Carte', 'VRAM', 'Résolution', 'Pilote'],
                                         rows, width, [0.42, 0.12, 0.20, 0.26]))

        # ── Correctifs installés ─────────────────────────────────────────────
        correctifs = info.get('hotfixes') or []
        if correctifs:
            rows = [[Paragraph(_pdf_escape(h.get('id')), S['mono']),
                     Paragraph(_pdf_escape(h.get('installed_on')), S['body']),
                     Paragraph(_pdf_escape(h.get('description')), S['body'])] for h in correctifs]
            story.append(Paragraph('Correctifs Windows installés (%d)' % len(correctifs), S['h2']))
            story.append(_pdf_data_table(tk, ['Correctif', 'Installé le', 'Type'],
                                         rows, width, [0.30, 0.30, 0.40]))

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

        # ── Démarrage, services, partages, tâches ────────────────────────────
        services = info.get('stopped_auto_services') or []
        if services:
            rows = [[Paragraph(_pdf_escape(s['display_name']), S['body']),
                     Paragraph(_pdf_escape(s['name']), S['mono']),
                     Paragraph(_pdf_escape(s['state']), S['body'])] for s in services]
            story.append(Paragraph('Services automatiques arrêtés (%d)' % len(services), S['h2']))
            story.append(_pdf_data_table(tk, ['Service', 'Nom interne', 'État'],
                                         rows, width, [0.46, 0.36, 0.18]))

        partages = info.get('smb_shares') or []
        if partages:
            rows = [[Paragraph('<b>%s</b>' % _pdf_escape(s['name']), S['body']),
                     Paragraph(_pdf_escape(s.get('path')), S['mono']),
                     Paragraph('Administration' if s.get('administrative') else 'Exposé',
                               S['body'])] for s in partages]
            story.append(Paragraph('Partages réseau (%d)' % len(partages), S['h2']))
            story.append(_pdf_data_table(tk, ['Partage', 'Chemin', 'Nature'],
                                         rows, width, [0.32, 0.46, 0.22]))

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

        demarrage = info.get('startup_programs') or []
        if demarrage:
            rows = [[Paragraph(_pdf_escape(p['name']), S['body']),
                     Paragraph(_pdf_escape(p.get('command')), S['small']),
                     Paragraph(_pdf_escape(p.get('location')), S['mono'])] for p in demarrage]
            story.append(Paragraph('Programmes au démarrage (%d)' % len(demarrage), S['h2']))
            story.append(_pdf_data_table(tk, ['Programme', 'Commande', 'Emplacement'],
                                         rows, width, [0.24, 0.50, 0.26]))

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
                else:
                    name, version, publisher, install = str(soft), '', '', ''
                rows.append([
                    Paragraph(f'<font color="#9ca3af" size="7">{i}</font>', S['body']),
                    Paragraph(_pdf_escape(name), S['body']),
                    Paragraph(f'<font face="Courier" size="7.5">'
                              f'{_pdf_escape(version)}</font>', S['body']),
                    Paragraph(_pdf_escape(publisher), S['body']),
                    Paragraph(f'<font size="7.5">{_pdf_escape(install)}</font>', S['body']),
                ])
            story.append(_pdf_data_table(
                tk, ['#', 'Nom', 'Version', 'Éditeur', 'Installé le'], rows, width,
                [0.06, 0.40, 0.16, 0.24, 0.14]))

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


def upload_report_to_parcinfo(report_content, report_file, server_url, device_id, client_id):
    """Envoie le rapport (PDF ou HTML) à ParcInfo en tant que document joint.

    Args:
        report_content: Contenu du rapport (bytes ou str)
        report_file: Chemin du fichier rapport (détermine le type)
        server_url: URL du serveur ParcInfo
        device_id: ID de l'appareil créé/mis à jour
        client_id: ID du client
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

        request = Request(
            f"{server_url.rstrip('/')}/api/device-info/upload-report",
            data=body.getvalue(),
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
            method='POST'
        )

        with urlopen(request, timeout=30) as response:
            return True, json.loads(response.read().decode('utf-8'))
    except Exception as e:
        return False, str(e)


def fetch_clients(server_url, mac_address=None):
    """Récupère la liste des clients depuis ParcInfo (endpoint public, sans auth).

    Quand une adresse MAC est fournie et que le serveur connaît déjà cette
    machine, il renvoie en plus le client auquel elle est rattachée : le
    collecteur peut alors le présélectionner au lieu de demander à l'utilisateur
    de le retrouver dans une liste qui compte parfois des dizaines d'entrées.

    Retourne (clients, client_suggéré_ou_None).
    """
    try:
        url = "%s/api/clients-public" % server_url.rstrip('/')
        if mac_address:
            url += '?mac=' + quote(mac_address)
        with urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
        # Le serveur répond une liste simple, ou un objet quand il a une
        # suggestion ; les deux formes doivent être acceptées.
        if isinstance(data, dict):
            clients = data.get('clients') or []
            return (clients if isinstance(clients, list) else [], data.get('suggested_client'))
        return (data if isinstance(data, list) else [], None)
    except Exception:
        return ([], None)
