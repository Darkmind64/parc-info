#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Collecte étendue (USB, ports à l'écoute, clés de licence) et génération des
rapports système — module partagé par les deux collecteurs.

`system-info-collector.py` (CLI) et `system-info-collector-gui.py` étaient des
quasi-duplicatas : 15 fonctions communes ne différant que par des commentaires
et le logging. Tout ce qui est ajouté ou réécrit ici est donc factorisé plutôt
qu'écrit deux fois. PyInstaller embarque automatiquement ce module dans les deux
exécutables via l'analyse des imports (il est importé au premier niveau).

Aucune dépendance externe obligatoire : reportlab n'est requis que pour le PDF,
et son absence fait basculer sur le rapport HTML.
"""

import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime

IS_WINDOWS = platform.system() == 'Windows'
IS_MACOS = platform.system() == 'Darwin'
IS_LINUX = platform.system() == 'Linux'


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS D'EXÉCUTION
# ══════════════════════════════════════════════════════════════════════════════

def _no_window_flags():
    """Empêche l'ouverture d'une console Windows depuis le collecteur GUI."""
    return subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0


def _run(cmd, timeout=15):
    """Exécute une commande et retourne stdout, ou '' en cas d'échec.

    Le décodage est forcé en UTF-8 : `text=True` seul utilise l'encodage local
    (cp1252 sur un Windows français), ce qui corrompt silencieusement tout
    libellé accentué renvoyé par PowerShell — noms de périphériques, comptes
    utilisateurs, descriptions d'adaptateurs réseau.
    """
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=_no_window_flags(),
        )
        if result.returncode == 0:
            return (result.stdout or b'').decode('utf-8', errors='replace')
    except Exception:
        pass
    return ''


def _ps_json(cmd, timeout=20):
    """Exécute du PowerShell et parse le JSON retourné (None si échec).

    PowerShell 5.1 écrit sur la console avec la page de code OEM et non en
    UTF-8 ; forcer OutputEncoding est le seul moyen d'obtenir des accents
    intacts côté Python.
    """
    if not IS_WINDOWS:
        return None
    prelude = '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; '
    out = _run(['powershell', '-NoProfile', '-NonInteractive', '-Command', prelude + cmd],
               timeout=timeout)
    if not out.strip():
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def _as_list(value):
    """PowerShell sérialise un résultat unique en objet et non en tableau."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


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

        devices.append({
            'name': best['name'],
            'inventory_name': inventory_name,
            'manufacturer': manufacturer,
            'serial': serial,
            'vid': best['vid'],
            'pid': best['pid'],
            'categorie': categorie,
            'inventoriable': inventoriable,
            'nodes': len(group),
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
    if IS_MACOS:
        return _collect_usb_macos()
    if IS_LINUX:
        return _collect_usb_linux()
    return []


def _collect_usb_windows():
    data = _ps_json(
        "$d = @(); try { $d = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue "
        "| Where-Object { $_.InstanceId -like 'USB\\*' } "
        "| Select-Object FriendlyName,Class,InstanceId,Manufacturer,Status) } catch {}; "
        "$d | ConvertTo-Json -Compress -Depth 3",
        timeout=25,
    )
    nodes = []
    for d in _as_list(data):
        name = (d.get('FriendlyName') or '').strip()
        if not name:
            continue
        instance_id = d.get('InstanceId') or ''
        vid, pid = _parse_vid_pid(instance_id)
        nodes.append({
            'name': name,
            'manufacturer': (d.get('Manufacturer') or '').strip(),
            'serial': _usb_serial_from_instance_id(instance_id),
            'vid': vid,
            'pid': pid,
            'categorie': _classify_usb(name, d.get('Class')),
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


# ══════════════════════════════════════════════════════════════════════════════
# PORTS À L'ÉCOUTE
# ══════════════════════════════════════════════════════════════════════════════

# (nom court, description, niveau) — niveau pilote la couleur de la carte.
# 'danger' = protocole en clair ou exposition sensible, 'warn' = à surveiller,
# 'ok' = service courant chiffré/légitime, 'info' = neutre.
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


def collect_listening_ports():
    """Liste les ports TCP en écoute sur la machine.

    Retourne une liste de dicts triée par numéro de port :
        port, process, name, description, level, local_address
    """
    raw = _listening_windows() if IS_WINDOWS else _listening_unix()

    merged = {}
    for port, proc, addr in raw:
        # Un service qui écoute à la fois sur 0.0.0.0 et [::] apparaît deux
        # fois : on ne garde qu'une carte par port, en privilégiant l'entrée
        # qui porte un nom de processus.
        prev = merged.get(port)
        if prev and prev['process'] and not proc:
            continue
        name, desc, level = describe_port(port)
        merged[port] = {
            'port': port,
            'process': proc,
            'name': name,
            'description': desc,
            'level': level,
            'local_address': addr,
            'ephemeral': port >= EPHEMERAL_PORT_START,
        }
    return [merged[p] for p in sorted(merged)]


def notable_ports(ports):
    """Ports méritant une carte : tout sauf la plage dynamique."""
    return [p for p in (ports or []) if not p.get('ephemeral')]


def _listening_windows():
    data = _ps_json(
        "$c = @(); try { $c = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object LocalPort,LocalAddress,OwningProcess) } catch {}; "
        "$procs = @{}; try { Get-Process -ErrorAction SilentlyContinue "
        "| ForEach-Object { $procs[[string]$_.Id] = $_.ProcessName } } catch {}; "
        "$c | ForEach-Object { [PSCustomObject]@{ Port=$_.LocalPort; Address=$_.LocalAddress; "
        "Process=$procs[[string]$_.OwningProcess] } } | ConvertTo-Json -Compress -Depth 3",
        timeout=25,
    )
    out = []
    for c in _as_list(data):
        try:
            port = int(c.get('Port'))
        except (TypeError, ValueError):
            continue
        out.append((port, (c.get('Process') or '').strip(), (c.get('Address') or '').strip()))
    return out


def _listening_unix():
    text = _run(['ss', '-tlnp'], timeout=15) or _run(['netstat', '-tlnp'], timeout=15)
    if not text:
        # macOS n'a ni ss ni netstat -p : lsof est le seul recours.
        text = _run(['lsof', '-nP', '-iTCP', '-sTCP:LISTEN'], timeout=20)
        out = []
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 9:
                continue
            m = re.search(r':(\d+)$', parts[8])
            if m:
                out.append((int(m.group(1)), parts[0], parts[8]))
        return out

    out = []
    for line in text.splitlines():
        m = re.search(r'[\s:](\d+)\s+[\d\.\*:\[\]]+\s', line)
        addr_m = re.search(r'((?:\d{1,3}\.){3}\d{1,3}|\[::\]|\*|0\.0\.0\.0):(\d+)', line)
        if not addr_m:
            continue
        proc_m = re.search(r'users:\(\("([^"]+)"|(\d+)/(\S+)', line)
        proc = ''
        if proc_m:
            proc = proc_m.group(1) or proc_m.group(3) or ''
        out.append((int(addr_m.group(2)), proc, addr_m.group(1)))
    return out


# ══════════════════════════════════════════════════════════════════════════════
# CLÉS DE LICENCE
# ══════════════════════════════════════════════════════════════════════════════

def _decode_digital_product_id(blob):
    """Décode un blob DigitalProductId du registre en clé produit lisible.

    Algorithme Microsoft (base 24 sur les octets 52..66). Les clés Windows 8+
    encodent un 'N' à une position variable, signalée par un bit de l'octet 66.
    """
    if not blob or len(blob) < 67:
        return ''
    chars = 'BCDFGHJKMPQRTVWXY2346789'
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
        key = chars[current] + key

    if is_win8:
        key = key[1:last + 1] + 'N' + key[last + 1:]

    formatted = '-'.join(key[i:i + 5] for i in range(0, len(key), 5))
    return formatted if _is_plausible_key(formatted) else ''


def _is_plausible_key(key):
    """Écarte les clés décodées depuis un blob vide.

    Sous Windows 10/11 avec licence numérique, DigitalProductId existe mais sa
    zone de clé est à zéro : le décodage produit alors « BBBBB-BBBBB-… », soit
    l'index 0 répété. Une vraie clé comporte au moins cinq caractères
    distincts.
    """
    letters = set((key or '').replace('-', ''))
    return len(letters) >= 5


def _read_registry_binary(root, path, value_name):
    """Lit une valeur binaire du registre dans la vue 64 bits."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            data, _ = winreg.QueryValueEx(key, value_name)
            return bytes(data)
    except Exception:
        return None


def collect_licenses():
    """Collecte les licences avec leur clé produit complète.

    Retourne une liste de dicts : editeur, produit, cle, statut, source.
    La clé est renvoyée en entier (jamais tronquée) — c'est le but recherché.
    """
    if not IS_WINDOWS:
        return []

    licences = []
    seen_keys = set()

    def add(editeur, produit, cle, statut, source, complete=True, partielle=''):
        cle = (cle or '').strip()
        if not produit:
            return
        # Une même clé peut remonter par deux voies (BIOS + registre) : on ne
        # la liste qu'une fois, en gardant la première source rencontrée.
        if cle and cle in seen_keys:
            return
        if cle:
            seen_keys.add(cle)
        licences.append({
            'editeur': editeur,
            'produit': produit,
            'cle': cle,
            'cle_complete': bool(cle) and complete,
            'cle_partielle': partielle,
            'statut': statut,
            'source': source,
        })

    # 1. Clé OEM gravée dans le BIOS (machines préinstallées) — clé complète.
    oem = _ps_json(
        "try { Get-CimInstance -ClassName SoftwareLicensingService -ErrorAction Stop "
        "| Select-Object OA3xOriginalProductKey | ConvertTo-Json -Compress } catch {}",
        timeout=20,
    )
    for row in _as_list(oem):
        key = (row.get('OA3xOriginalProductKey') or '').strip()
        if key:
            add('Microsoft', 'Windows (clé OEM BIOS)', key, 'Préinstallée', 'BIOS OA3')

    # 2. Clé installée, décodée depuis le registre — complète elle aussi.
    try:
        import winreg
        blob = _read_registry_binary(
            winreg.HKEY_LOCAL_MACHINE,
            r'SOFTWARE\Microsoft\Windows NT\CurrentVersion',
            'DigitalProductId',
        )
    except Exception:
        blob = None
    decoded = _decode_digital_product_id(blob) if blob else ''

    # 3. État d'activation + édition (la clé partielle sert de recoupement).
    products = _ps_json(
        "$p = @(); try { $p = @(Get-CimInstance -ClassName SoftwareLicensingProduct "
        "-Filter 'PartialProductKey IS NOT NULL' -ErrorAction SilentlyContinue "
        "| Select-Object Name,Description,LicenseStatus,PartialProductKey) } catch {}; "
        "$p | ConvertTo-Json -Compress -Depth 3",
        timeout=25,
    )
    status_labels = {
        0: 'Non licencié', 1: 'Activé', 2: 'Période de grâce', 3: 'Grâce OEM',
        4: 'Grâce hors tolérance', 5: 'Non autorisé', 6: 'Notification',
    }
    for row in _as_list(products):
        name = (row.get('Name') or '').strip()
        if not name:
            continue
        statut = status_labels.get(row.get('LicenseStatus'), 'Inconnu')
        partial = (row.get('PartialProductKey') or '').strip()
        # Le décodage registre ne concerne que Windows : on ne l'attache jamais
        # à une licence Office.
        cle = decoded if ('windows' in name.lower() and decoded) else ''
        if cle:
            add('Microsoft', name, cle, statut, 'Registre Windows',
                complete=True, partielle=partial)
        else:
            # Licence numérique ou MAK : la clé complète n'existe nulle part sur
            # la machine, seuls les 5 derniers caractères sont exposés. Mieux
            # vaut l'annoncer que d'afficher « XXXXX-…-ABCDE » qui aurait l'air
            # d'une vraie clé tronquée.
            add('Microsoft', name, '', statut, 'Windows Licensing',
                complete=False, partielle=partial)

    # 4. Licences Office : DigitalProductId sous chaque GUID d'enregistrement.
    licences.extend(_collect_office_licenses(seen_keys))

    return licences


def _collect_office_licenses(seen_keys):
    """Parcourt les clés de registre d'enregistrement Office."""
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except Exception:
        return []

    found = []
    for base in (r'SOFTWARE\Microsoft\Office', r'SOFTWARE\Wow6432Node\Microsoft\Office'):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    version = winreg.EnumKey(root, i)
                    reg_path = f'{base}\\{version}\\Registration'
                    try:
                        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path, 0,
                                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as regk:
                            for j in range(winreg.QueryInfoKey(regk)[0]):
                                guid = winreg.EnumKey(regk, j)
                                blob = _read_registry_binary(
                                    winreg.HKEY_LOCAL_MACHINE, f'{reg_path}\\{guid}',
                                    'DigitalProductId')
                                if not blob:
                                    continue
                                cle = _decode_digital_product_id(blob)
                                if not cle or cle in seen_keys:
                                    continue
                                seen_keys.add(cle)
                                produit = _registry_string(
                                    winreg.HKEY_LOCAL_MACHINE, f'{reg_path}\\{guid}',
                                    'ProductName') or f'Microsoft Office {version}'
                                found.append({
                                    'editeur': 'Microsoft',
                                    'produit': produit,
                                    'cle': cle,
                                    'statut': 'Installée',
                                    'source': 'Registre Office',
                                })
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _registry_string(root, path, value_name):
    try:
        import winreg
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
            return str(value).strip()
    except Exception:
        return ''


def collect_extended_info():
    """Collecte l'ensemble des nouvelles données en une passe.

    Renvoie un dict à fusionner dans `info`. Chaque bloc est isolé : une
    collecte qui échoue (droits insuffisants, commande absente) laisse les
    autres intactes plutôt que de faire échouer tout le rapport.
    """
    extended = {}
    for key, func in (
        ('usb_devices', collect_usb_devices),
        ('listening_ports', collect_listening_ports),
        ('licenses', collect_licenses),
    ):
        try:
            extended[key] = func() or []
        except Exception:
            extended[key] = []
    return extended


# ══════════════════════════════════════════════════════════════════════════════
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


def _battery_pct(text):
    m = re.match(r'^(\d+)\s*%', str(text or ''))
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

    battery = _battery_pct(info.get('battery'))
    if battery is not None and battery <= BATTERY_DANGER_PCT:
        add('danger', f'Batterie très faible — {battery} %')

    # Ports exposés jugés sensibles : c'est la mise en évidence la plus utile
    # d'un rapport de parc.
    risky = [p for p in info.get('listening_ports', []) if p.get('level') == 'danger']
    if risky:
        add('danger', f'{len(risky)} port(s) sensible(s) en écoute',
            ' · '.join(f"{p['port']} ({p['name']})" for p in risky[:5]))

    for lic in info.get('licenses', []):
        if lic.get('statut') and lic['statut'] not in ('Activé', 'Préinstallée', 'Installée'):
            add('warn', f"Licence non activée — {lic.get('produit', '')}", lic['statut'])

    uptime = _num(info.get('uptime_hours'))
    if uptime and uptime > 24 * 30:
        add('info', f'Machine non redémarrée depuis {round(uptime / 24)} jours')

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
        # La collecte ne mesure pas l'occupation mémoire à l'instant T : la
        # barre situe la machine sur une échelle 0–64 Go plutôt que d'afficher
        # un pourcentage d'utilisation qui n'existe pas.
        qualif = 'Confortable' if ram >= 16 else 'Correct' if ram >= 8 else 'Juste'
        cards.append(
            '<div class="kpi">'
            '<div class="kpi-head"><span class="kpi-icon">RAM</span> Mémoire vive</div>'
            f'<div class="kpi-value">{ram:g}<span class="kpi-unit">GB</span></div>'
            f'{_bar_html(min(ram / 64 * 100, 100), "ok" if ram >= 8 else "warn")}'
            f'<div class="kpi-sub">{qualif}</div>'
            '</div>')

    battery = _battery_pct(info.get('battery'))
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
        ok = lic.get('statut') in ('Activé', 'Préinstallée', 'Installée')
        if lic.get('cle'):
            # Clé affichée en entier, sans troncature ni masquage.
            cle_html = f'<code class="licence-key">{_esc(lic["cle"])}</code>'
        elif lic.get('cle_partielle'):
            cle_html = ('<span class="licence-partial">Clé complète non exposée par '
                        'Windows (licence numérique ou MAK) — se termine par '
                        f'<code>{_esc(lic["cle_partielle"])}</code></span>')
        else:
            cle_html = '<span class="licence-partial">Aucune clé exposée</span>'
        rows.append(
            f'<tr><td><strong>{_esc(lic.get("produit"))}</strong>'
            f'<div class="meta">{_esc(lic.get("editeur"))} · {_esc(lic.get("source"))}</div></td>'
            f'<td>{cle_html}</td>'
            f'<td>{_pill_html(lic.get("statut") or "Inconnu", "ok" if ok else "warn")}</td></tr>')
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

    sections.append(_list_section_html('Disques physiques', info.get('physical_disks', [])))
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
            Flowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
            Spacer, Table, TableStyle,
        )
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
    return {
        'colors': colors, 'A4': A4, 'mm': mm, 'Paragraph': Paragraph,
        'SimpleDocTemplate': SimpleDocTemplate, 'Spacer': Spacer, 'Table': Table,
        'TableStyle': TableStyle, 'KeepTogether': KeepTogether, 'PageBreak': PageBreak,
        'ProgressBar': ProgressBar, 'S': S,
    }


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
        cells.append(('MÉMOIRE VIVE', f'{ram:g} GB', min(ram / 64 * 100, 100),
                      _LEVEL_COLORS['ok' if ram >= 8 else 'warn'][0],
                      'Confortable' if ram >= 16 else 'Correct' if ram >= 8 else 'Juste'))

    battery = _battery_pct(info.get('battery'))
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

        doc = tk['SimpleDocTemplate'](
            filename, pagesize=tk['A4'],
            leftMargin=15 * tk['mm'], rightMargin=15 * tk['mm'],
            topMargin=13 * tk['mm'], bottomMargin=13 * tk['mm'],
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
        for profile in info.get('firewall', []):
            sec_rows.append(('Pare-feu', profile,
                             'danger' if re.search(r'(désactiv|disabled|off)', profile, re.I) else 'ok'))
        for vol in info.get('bitlocker', []):
            sec_rows.append(('BitLocker', vol,
                             'warn' if re.search(r'(non chiffr|not encrypted|off)', vol, re.I) else 'ok'))
        if info.get('last_windows_update'):
            sec_rows.append(('Dernière mise à jour', info['last_windows_update'], 'info'))

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
                if lic.get('cle'):
                    # Clé complète, en monospace sur fond sombre pour être
                    # relisible sans ambiguïté (0/O, 1/I) depuis un tirage papier.
                    cle_cell = Paragraph(_pdf_escape(lic['cle']), S['key'])
                    extra.append(('BACKGROUND', (1, i), (1, i), colors.HexColor('#1e3a5f')))
                elif lic.get('cle_partielle'):
                    cle_cell = Paragraph(
                        'Clé complète non exposée par Windows (licence numérique ou '
                        f'MAK) — se termine par <font face="Courier-Bold">'
                        f'{_pdf_escape(lic["cle_partielle"])}</font>', S['small'])
                else:
                    cle_cell = Paragraph('Aucune clé exposée', S['small'])
                ok = lic.get('statut') in ('Activé', 'Préinstallée', 'Installée')
                fg, bg = _LEVEL_COLORS['ok' if ok else 'warn']
                rows.append([
                    Paragraph(f'<b>{_pdf_escape(lic.get("produit"))}</b><br/>'
                              f'<font size="6.8" color="#6b7280">'
                              f'{_pdf_escape(lic.get("editeur"))} · '
                              f'{_pdf_escape(lic.get("source"))}</font>', S['body']),
                    cle_cell,
                    Paragraph(f'<font color="{fg}"><b>'
                              f'{_pdf_escape(lic.get("statut") or "Inconnu")}</b></font>', S['body']),
                ])
                extra.append(('BACKGROUND', (2, i), (2, i), colors.HexColor(bg)))
            story.append(_pdf_data_table(
                tk, ['Produit', 'Clé de licence', 'État'], rows, width,
                [0.34, 0.48, 0.18], extra))
        else:
            story.append(Paragraph('Aucune licence détectée.', S['small']))

        # ── Listes complémentaires ───────────────────────────────────────────
        for titre, items in (
            ('Disques physiques', info.get('physical_disks', [])),
            ('Adaptateurs réseau', info.get('network_adapters', [])),
            ('Comptes utilisateurs locaux', info.get('users', [])),
        ):
            if items:
                story.append(Paragraph(f'{titre} ({len(items)})', S['h2']))
                for item in items:
                    story.append(Paragraph(f'• {_pdf_escape(item)}', S['body']))

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

        doc.build(story)
        with open(filename, 'rb') as f:
            return f.read(), filename

    except Exception as exc:
        # Un PDF qui échoue ne doit pas priver l'utilisateur de rapport.
        try:
            print(f'Erreur génération PDF ({exc}) — repli sur le rapport HTML')
        except Exception:
            pass
        return generate_html_report(info, client_id, client_name)
