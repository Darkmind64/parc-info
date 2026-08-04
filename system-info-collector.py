#!/usr/bin/env python3
"""
ParcInfo System Information Collector

Petit script autonome qui collecte les informations système et les envoie à ParcInfo.
Fonctionne sur Windows, macOS et Linux.

Utilisation :
    python system-info-collector.py [--server http://parcinfo.local:3456] [--token ABC123]

Ou directement dans ParcInfo :
    1. Ouvrir l'interface web
    2. Cliquer sur le lien "Télécharger collecteur" depuis la page Inventaire
    3. Exécuter le script
    4. Les infos système s'ajoutent automatiquement à la machine correspondante
"""

import sys
import platform
import socket
import subprocess
import json
import argparse
import uuid
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError
from datetime import datetime

# Platform detection
IS_WINDOWS = sys.platform == 'win32'
IS_MAC = sys.platform == 'darwin'
IS_LINUX = sys.platform == 'linux'


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


def get_ip_addresses():
    """Récupère toutes les adresses IP locales."""
    ips = []
    try:
        hostname = socket.gethostname()
        ips = socket.gethostbyname_ex(hostname)[2]
    except Exception:
        pass
    # Filtrer les loopback
    return [ip for ip in ips if not ip.startswith('127.')]


def get_os_info():
    """Récupère l'OS et la version exacte avec édition (Pro/Home/Server)."""
    os_name = platform.system()  # 'Windows', 'Darwin', 'Linux'
    os_version = platform.release()
    edition = ""

    # Pour Windows, obtenir la version complète et l'édition
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion') as key:
                display_version = winreg.QueryValueEx(key, 'DisplayVersion')[0]
                os_version = display_version
                # Récupérer l'édition (Pro, Home, Server, etc)
                try:
                    edition_name = winreg.QueryValueEx(key, 'EditionID')[0]
                    # Mapper les éditions Windows
                    edition_map = {
                        'Professional': 'Pro',
                        'Home': 'Home',
                        'Enterprise': 'Enterprise',
                        'Education': 'Education',
                        'ServerStandard': 'Server 2022',
                        'ServerDatacenter': 'Server 2022 Datacenter',
                    }
                    edition = edition_map.get(edition_name, edition_name)
                except Exception:
                    pass
        except Exception:
            pass

    # Pour macOS
    if IS_MAC:
        try:
            mac_ver = platform.mac_ver()
            os_version = mac_ver[0]
        except Exception:
            pass

    # Construire le nom OS complet
    if edition:
        full_os_name = f"Windows {os_version} {edition}"
    else:
        full_os_name = os_name

    return {
        "os_name": full_os_name,
        "os_version": os_version,
        "platform": platform.platform()
    }


def get_system_info_windows():
    """Collecte les infos système via WMI (Windows)."""
    info = {}
    try:
        import wmi
        c = wmi.WMI()

        # Marque et modèle (BIOS/System)
        try:
            system = c.Win32_ComputerSystem()[0]
            info['brand'] = system.Manufacturer.strip()
            info['model'] = system.Model.strip()
        except Exception:
            pass

        # Numéro de série
        try:
            bios = c.Win32_SystemEnclosure()[0]
            info['serial_number'] = bios.SerialNumber.strip()
        except Exception:
            pass

        # RAM
        try:
            mem = c.Win32_PhysicalMemory()
            total_ram_bytes = sum(int(m.Capacity) for m in mem)
            info['ram_gb'] = round(total_ram_bytes / (1024 ** 3), 1)
        except Exception:
            pass

        # CPU
        try:
            cpu = c.Win32_Processor()[0]
            info['cpu'] = cpu.Name.strip()
            info['cpu_cores'] = int(cpu.NumberOfCores) if hasattr(cpu, 'NumberOfCores') else None
        except Exception:
            pass

        # Tous les disques logiques
        try:
            disks = c.Win32_LogicalDisk()
            disk_list = []
            total_disk = 0
            for disk in disks:
                if disk.DriveType == 3:  # Local Disk
                    drive_letter = disk.Name
                    size_gb = round(int(disk.Size) / (1024 ** 3), 1) if disk.Size else 0
                    disk_list.append(f"{drive_letter} ({size_gb} GB)")
                    total_disk += size_gb
            if disk_list:
                info['disk_drives'] = disk_list
                info['disk_total_gb'] = total_disk
        except Exception:
            pass

        # Antivirus
        try:
            av_products = c.Win32_SecurityCenter1()[0]
            av_list = av_products.AntivirusProduct if hasattr(av_products, 'AntivirusProduct') else None
            if av_list:
                # av_list est une liste de strings du type "displayName\\{UUID}"
                avs = [av.split('\\')[0] for av in (av_list if isinstance(av_list, list) else [av_list])]
                info['antivirus'] = ', '.join(avs)
        except Exception:
            pass

    except ImportError:
        # WMI non disponible, utiliser alternatives
        pass

    return info


def get_system_info_mac():
    """Collecte les infos système via system_profiler (macOS)."""
    info = {}
    try:
        result = subprocess.run(['system_profiler', 'SPHardwareDataType'],
                               capture_output=True, text=True, timeout=10)
        lines = result.stdout.split('\n')

        for line in lines:
            if 'Model Identifier:' in line:
                info['model'] = line.split(':', 1)[1].strip()
            elif 'Model Name:' in line:
                info['brand'] = 'Apple'
                model_name = line.split(':', 1)[1].strip()
                if 'model' not in info:
                    info['model'] = model_name
            elif 'Serial Number' in line:
                info['serial_number'] = line.split(':', 1)[1].strip()
            elif 'Processor Cores:' in line:
                info['cpu_cores'] = int(line.split(':', 1)[1].strip())
            elif 'Processor Name:' in line:
                info['cpu'] = line.split(':', 1)[1].strip()
            elif 'Memory:' in line:
                try:
                    mem_str = line.split(':', 1)[1].strip().split()[0]
                    info['ram_gb'] = float(mem_str)
                except Exception:
                    pass
    except Exception:
        pass

    # Tous les disques via df
    try:
        result = subprocess.run(['df', '-h'], capture_output=True, text=True, timeout=5)
        lines = result.stdout.strip().split('\n')[1:]  # Skip header
        disk_list = []
        total_disk = 0
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                device = parts[0]
                size_str = parts[1]
                try:
                    # Convertir la taille en GB
                    if 'T' in size_str:
                        size_gb = float(size_str.replace('T', '')) * 1024
                    elif 'G' in size_str:
                        size_gb = float(size_str.replace('G', ''))
                    else:
                        continue
                    disk_list.append(f"{device} ({round(size_gb, 1)} GB)")
                    total_disk += size_gb
                except Exception:
                    pass
        if disk_list:
            info['disk_drives'] = disk_list
            info['disk_total_gb'] = round(total_disk, 1)
    except Exception:
        pass

    return info


def get_system_info_linux():
    """Collecte les infos système sur Linux."""
    info = {}

    # Marque/Modèle via dmidecode
    try:
        result = subprocess.run(['sudo', 'dmidecode', '-t', 'system'],
                               capture_output=True, text=True, timeout=5)
        lines = result.stdout.split('\n')
        for line in lines:
            if 'Manufacturer:' in line:
                info['brand'] = line.split(':', 1)[1].strip()
            elif 'Product Name:' in line:
                info['model'] = line.split(':', 1)[1].strip()
            elif 'Serial Number:' in line:
                info['serial_number'] = line.split(':', 1)[1].strip()
    except Exception:
        pass

    # CPU info depuis /proc/cpuinfo
    try:
        with open('/proc/cpuinfo', 'r') as f:
            lines = f.readlines()
            for line in lines:
                if line.startswith('model name'):
                    info['cpu'] = line.split(':', 1)[1].strip()
                    break
            # Compter les cores
            info['cpu_cores'] = sum(1 for line in lines if line.startswith('processor'))
    except Exception:
        pass

    # RAM depuis /proc/meminfo
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    kb = int(line.split()[1])
                    info['ram_gb'] = round(kb / (1024 * 1024), 1)
                    break
    except Exception:
        pass

    # Tous les disques
    try:
        result = subprocess.run(['df', '-h'], capture_output=True, text=True, timeout=5)
        lines = result.stdout.strip().split('\n')[1:]  # Skip header
        disk_list = []
        total_disk = 0
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                device = parts[0]
                size_str = parts[1]
                try:
                    # Convertir la taille en GB
                    if 'T' in size_str:
                        size_gb = float(size_str.replace('T', '')) * 1024
                    elif 'G' in size_str:
                        size_gb = float(size_str.replace('G', ''))
                    elif 'M' in size_str:
                        size_gb = float(size_str.replace('M', '')) / 1024
                    else:
                        continue
                    disk_list.append(f"{device} ({round(size_gb, 1)} GB)")
                    total_disk += size_gb
                except Exception:
                    pass
        if disk_list:
            info['disk_drives'] = disk_list
            info['disk_total_gb'] = round(total_disk, 1)
    except Exception:
        pass

    return info


def get_installed_software():
    """Récupère la liste des logiciels installés (max 200)."""
    software = []

    if IS_WINDOWS:
        try:
            import winreg
            hive = winreg.HKEY_LOCAL_MACHINE
            paths = [
                r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
            ]
            for path in paths:
                try:
                    with winreg.OpenKey(hive, path) as key:
                        count = winreg.QueryInfoKey(key)[0]
                        for i in range(count):
                            try:
                                subkey_name = winreg.EnumKey(key, i)
                                with winreg.OpenKey(key, subkey_name) as subkey:
                                    try:
                                        display_name = winreg.QueryValueEx(subkey, 'DisplayName')[0]
                                        if display_name and len(display_name.strip()) > 0:
                                            software.append(display_name.strip())
                                    except WindowsError:
                                        pass
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            pass

    elif IS_MAC:
        # /Applications
        try:
            result = subprocess.run(['ls', '/Applications'], capture_output=True, text=True, timeout=5)
            software.extend([app.replace('.app', '') for app in result.stdout.split('\n') if app.endswith('.app')])
        except Exception:
            pass

        # /usr/local/opt (Homebrew)
        try:
            result = subprocess.run(['ls', '/usr/local/opt'], capture_output=True, text=True, timeout=5)
            software.extend([pkg for pkg in result.stdout.split('\n') if pkg.strip()])
        except Exception:
            pass

        # pkgutil pour les packages installés
        try:
            result = subprocess.run(['pkgutil', '--packages'], capture_output=True, text=True, timeout=10)
            software.extend([line.strip() for line in result.stdout.split('\n') if line.strip()])
        except Exception:
            pass

    elif IS_LINUX:
        # Debian/Ubuntu (dpkg)
        try:
            result = subprocess.run(['dpkg', '--get-selections'], capture_output=True, text=True, timeout=10)
            software.extend([line.split()[0] for line in result.stdout.split('\n') if 'install' in line])
        except Exception:
            pass

        # RedHat/CentOS (rpm)
        if not software:
            try:
                result = subprocess.run(['rpm', '-qa'], capture_output=True, text=True, timeout=10)
                software.extend([line.strip() for line in result.stdout.split('\n') if line.strip()])
            except Exception:
                pass

        # Arch Linux (pacman)
        if not software:
            try:
                result = subprocess.run(['pacman', '-Q'], capture_output=True, text=True, timeout=10)
                software.extend([line.split()[0] for line in result.stdout.split('\n') if line.strip()])
            except Exception:
                pass

    # Limiter à 200 logiciels pour ne pas surcharger
    return sorted(list(set(software)))[:200]


def collect_system_info():
    """Collecte toutes les infos système."""
    info = {
        'timestamp': datetime.utcnow().isoformat(),
        'mac_address': get_mac_address(),
        'hostname': get_hostname(),
        'ip_addresses': get_ip_addresses(),
    }

    # OS
    info.update(get_os_info())

    # Infos spécifiques par OS
    if IS_WINDOWS:
        info.update(get_system_info_windows())
    elif IS_MAC:
        info.update(get_system_info_mac())
    elif IS_LINUX:
        info.update(get_system_info_linux())

    # Logiciels (limité à 200)
    info['installed_software'] = get_installed_software()[:200]

    return info


def generate_html_report(info, client_id=None, client_name=None):
    """Génère un rapport HTML complet avec toutes les infos collectées.

    Retourne le contenu HTML et le chemin du fichier sauvegardé.
    """
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    hostname = info.get('hostname', 'unknown')
    mac = info.get('mac_address', 'unknown')[:8]
    filename = f"system-info-report_{hostname}_{mac}_{timestamp}.html"

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Rapport Collecte Système - {hostname}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; background: #f5f5f5; color: #333; line-height: 1.6; }}
        .container {{ max-width: 1000px; margin: 20px auto; background: white; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #2c3e50 0%, #34495e 100%); color: white; padding: 30px; }}
        .header h1 {{ margin-bottom: 10px; }}
        .header p {{ opacity: 0.9; font-size: 14px; }}
        .content {{ padding: 30px; }}
        .section {{ margin-bottom: 30px; }}
        .section h2 {{ border-bottom: 3px solid #3498db; padding-bottom: 10px; margin-bottom: 15px; color: #2c3e50; font-size: 18px; }}
        .field {{ display: grid; grid-template-columns: 200px 1fr; gap: 15px; margin-bottom: 12px; align-items: start; }}
        .field-label {{ font-weight: 600; color: #555; }}
        .field-value {{ color: #333; word-break: break-word; }}
        .field-value.supported {{ background: #d4edda; padding: 8px 12px; border-left: 4px solid #28a745; border-radius: 3px; }}
        .field-value.unsupported {{ background: #fff3cd; padding: 8px 12px; border-left: 4px solid #ffc107; border-radius: 3px; color: #856404; }}
        .list-item {{ margin-left: 20px; padding: 8px 0; }}
        .disk {{ background: #f8f9fa; padding: 10px; margin: 8px 0; border-radius: 4px; border-left: 3px solid #3498db; }}
        .software-list {{ background: #f8f9fa; padding: 15px; border-radius: 4px; max-height: 400px; overflow-y: auto; }}
        .software-item {{ padding: 4px 0; font-family: monospace; font-size: 12px; }}
        .badge {{ display: inline-block; padding: 3px 8px; border-radius: 3px; font-size: 11px; font-weight: bold; }}
        .badge.supported {{ background: #28a745; color: white; }}
        .badge.partial {{ background: #ffc107; color: white; }}
        .badge.unsupported {{ background: #dc3545; color: white; }}
        .metadata {{ background: #ecf0f1; padding: 15px; border-radius: 4px; font-size: 12px; color: #555; margin-top: 20px; }}
        .metadata p {{ margin: 5px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 Rapport Collecte Informations Système</h1>
            <p>Généré le {datetime.utcnow().strftime("%d/%m/%Y à %H:%M:%S UTC")}</p>
        </div>

        <div class="content">
            <!-- IDENTIFICATION -->
            <div class="section">
                <h2>🔍 Identification</h2>
                <div class="field">
                    <span class="field-label">Hostname</span>
                    <span class="field-value supported">{info.get('hostname', 'N/A')} <span class="badge supported">API ✓</span></span>
                </div>
                <div class="field">
                    <span class="field-label">MAC Address</span>
                    <span class="field-value supported">{info.get('mac_address', 'N/A')} <span class="badge supported">API ✓</span></span>
                </div>
                <div class="field">
                    <span class="field-label">IP Address(es)</span>
                    <span class="field-value supported">{', '.join(info.get('ip_addresses', []))} <span class="badge supported">API ✓</span></span>
                </div>
                <div class="field">
                    <span class="field-label">Marque</span>
                    <span class="field-value supported">{info.get('brand', 'N/A')} <span class="badge supported">API ✓</span></span>
                </div>
                <div class="field">
                    <span class="field-label">Modèle</span>
                    <span class="field-value supported">{info.get('model', 'N/A')} <span class="badge supported">API ✓</span></span>
                </div>
                <div class="field">
                    <span class="field-label">Numéro Série</span>
                    <span class="field-value supported">{info.get('serial_number', 'N/A')} <span class="badge supported">API ✓</span></span>
                </div>
            </div>

            <!-- SYSTÈME D'EXPLOITATION -->
            <div class="section">
                <h2>🖥️ Système d'Exploitation</h2>
                <div class="field">
                    <span class="field-label">OS</span>
                    <span class="field-value supported">{info.get('os_name', 'N/A')} <span class="badge supported">API ✓</span></span>
                </div>
                <div class="field">
                    <span class="field-label">Version</span>
                    <span class="field-value supported">{info.get('os_version', 'N/A')} <span class="badge supported">API ✓</span></span>
                </div>
                <div class="field">
                    <span class="field-label">Platform</span>
                    <span class="field-value unsupported">{info.get('platform', 'N/A')} <span class="badge unsupported">Non stocké</span></span>
                </div>
            </div>

            <!-- MATÉRIEL -->
            <div class="section">
                <h2>⚙️ Matériel</h2>
                <div class="field">
                    <span class="field-label">RAM</span>
                    <span class="field-value supported">{info.get('ram_gb', 'N/A')} GB <span class="badge supported">API ✓</span></span>
                </div>
                <div class="field">
                    <span class="field-label">CPU</span>
                    <span class="field-value supported">{info.get('cpu', 'N/A')} <span class="badge supported">API ✓</span></span>
                </div>
                <div class="field">
                    <span class="field-label">Cores CPU</span>
                    <span class="field-value unsupported">{info.get('cpu_cores', 'N/A')} <span class="badge unsupported">Non stocké</span></span>
                </div>
            </div>

            <!-- DISQUES -->
            <div class="section">
                <h2>💾 Stockage</h2>
                <div class="field">
                    <span class="field-label">Total Stockage</span>
                    <span class="field-value supported">{info.get('disk_total_gb', 'N/A')} GB <span class="badge supported">API ✓</span></span>
                </div>
                <div class="field">
                    <span class="field-label">Disques Détaillés</span>
                    <div class="field-value unsupported" style="border: none; background: none; padding: 0;">
                        <span class="badge unsupported">Non stocké individuellement</span>
                        <div class="software-list">
"""

    if info.get('disk_drives'):
        for drive in info.get('disk_drives', []):
            html += f'                            <div class="disk">{drive}</div>\n'
    else:
        html += '                            <div style="padding: 10px; color: #999;">Aucun disque détecté</div>\n'

    html += f"""                        </div>
                    </div>
                </div>
            </div>

            <!-- SÉCURITÉ -->
            <div class="section">
                <h2>🛡️ Sécurité</h2>
                <div class="field">
                    <span class="field-label">Antivirus</span>
                    <span class="field-value supported">{info.get('antivirus', 'N/A')} <span class="badge supported">API ✓</span></span>
                </div>
            </div>

            <!-- LOGICIELS -->
            <div class="section">
                <h2>📦 Logiciels Installés</h2>
                <div class="field">
                    <span class="field-label">Total Détecté</span>
                    <span class="field-value supported">{len(info.get('installed_software', []))} logiciel(s) <span class="badge supported">API ✓</span> (50 premiers envoyés)</span>
                </div>
                <div class="field">
                    <span class="field-label">Liste</span>
                    <div class="field-value" style="border: none; background: none; padding: 0;">
                        <div class="software-list">
"""

    for i, soft in enumerate(info.get('installed_software', []), 1):
        html += f'                            <div class="software-item">{i}. {soft}</div>\n'

    if not info.get('installed_software'):
        html += '                            <div style="padding: 10px; color: #999;">Aucun logiciel détecté</div>\n'

    html += f"""                        </div>
                    </div>
                </div>
            </div>

            <!-- METADATA -->
            <div class="metadata">
                <p><strong>Client Cible :</strong> {client_name or f'ID {client_id}' or 'Non spécifié'}</p>
                <p><strong>Timestamp Collecte :</strong> {info.get('timestamp', 'N/A')}</p>
                <p><strong>Champs supportés par l'API :</strong> hostname, mac_address, ip_addresses, brand, model, serial_number, os, ram_gb, cpu, disk_total_gb, antivirus, installed_software</p>
                <p><strong>Champs non stockés :</strong> platform, cpu_cores, disk_drives (individuellement)</p>
            </div>
        </div>
    </div>
</body>
</html>"""

    # Sauvegarder le rapport
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(html)
        return html, filename
    except Exception as e:
        return html, None


def get_api_payload(info, client_id=None, client_name=None):
    """Extrait uniquement les champs supportés par l'API ParcInfo."""
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

    payload = {
        'mac_address': info.get('mac_address', ''),
        'ip_addresses': info.get('ip_addresses', []),
        'hostname': info.get('hostname', ''),
        'os_name': info.get('os_name', ''),
        'os_version': info.get('os_version', ''),
        'brand': info.get('brand', ''),
        'model': info.get('model', ''),
        'serial_number': info.get('serial_number', ''),
        'ram_gb': ram_formatted,  # Envoi avec "Go" (ex: "16 Go")
        'cpu': info.get('cpu', ''),
        'disk_total_gb': info.get('disk_total_gb', ''),
        'antivirus': info.get('antivirus', ''),
        'installed_software': info.get('installed_software', [])[:50],  # Max 50 pour l'API
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

        # Filtrer les champs supportés par l'API
        payload_data = get_api_payload(info, client_id, client_name)
        payload = json.dumps(payload_data)

        request = Request(
            f"{server_url.rstrip('/')}/api/device-info",
            data=payload.encode('utf-8'),
            headers=headers,
            method='POST'
        )

        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8'))
            return True, result
    except URLError as e:
        return False, f"Connection error: {e.reason}"
    except Exception as e:
        return False, str(e)


def upload_report_to_parcinfo(html_content, report_file, server_url, device_id, client_id):
    """Envoie le rapport HTML à ParcInfo en tant que document joint.

    Args:
        html_content: Contenu HTML du rapport
        report_file: Chemin du fichier HTML (ou None si généré en mémoire)
        server_url: URL du serveur ParcInfo
        device_id: ID de l'appareil créé/mis à jour
        client_id: ID du client
    """
    try:
        from urllib.request import Request as MultipartRequest
        import io

        # Préparer les données multipart
        boundary = '----FormBoundary' + str(uuid.uuid4()).replace('-', '')
        body = io.BytesIO()

        # Ajouter les champs
        body.write(f'--{boundary}\r\n'.encode())
        body.write(b'Content-Disposition: form-data; name="device_id"\r\n\r\n')
        body.write(f'{device_id}\r\n'.encode())

        body.write(f'--{boundary}\r\n'.encode())
        body.write(b'Content-Disposition: form-data; name="client_id"\r\n\r\n')
        body.write(f'{client_id}\r\n'.encode())

        # Ajouter le fichier
        body.write(f'--{boundary}\r\n'.encode())
        body.write(b'Content-Disposition: form-data; name="report"; filename="report.html"\r\n')
        body.write(b'Content-Type: text/html\r\n\r\n')
        if isinstance(html_content, str):
            body.write(html_content.encode('utf-8'))
        else:
            body.write(html_content)
        body.write(b'\r\n')

        body.write(f'--{boundary}--\r\n'.encode())

        # Envoyer la requête
        headers = {
            'Content-Type': f'multipart/form-data; boundary={boundary}'
        }

        request = Request(
            f"{server_url.rstrip('/')}/api/device-info/upload-report",
            data=body.getvalue(),
            headers=headers,
            method='POST'
        )

        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8'))
            return True, result
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser(
        description='Collecte les infos système et les envoie à ParcInfo'
    )
    parser.add_argument('--server', default='http://parcinfo.local:3456',
                       help='URL du serveur ParcInfo (défaut: http://parcinfo.local:3456)')
    parser.add_argument('--token', default=None,
                       help='Token d\'authentification (optionnel)')
    parser.add_argument('--client-id', type=int, default=None,
                       help='ID du client cible (ex: --client-id 5)')
    parser.add_argument('--client-name', default=None,
                       help='Nom du client cible (ex: --client-name "Mon Entreprise")')
    parser.add_argument('--quiet', action='store_true',
                       help='Mode silencieux (pas d\'affichage)')

    args = parser.parse_args()

    if not args.quiet:
        print("=" * 60)
        print("ParcInfo System Information Collector v1.0")
        print("=" * 60)
        print(f"\n[*] Collecte des informations système...")

    # Collecter les infos
    info = collect_system_info()

    if not args.quiet:
        print(f"    ✓ MAC: {info.get('mac_address', 'N/A')}")
        print(f"    ✓ Hostname: {info.get('hostname', 'N/A')}")
        print(f"    ✓ IP(s): {', '.join(info.get('ip_addresses', []))}")
        print(f"    ✓ OS: {info.get('os_name', 'N/A')} {info.get('os_version', '')}")
        print(f"    ✓ RAM: {info.get('ram_gb', 'N/A')} GB")
        print(f"    ✓ CPU: {info.get('cpu', 'N/A')} ({info.get('cpu_cores', 'N/A')} cores)")

        # Affichage des disques (multiples ou simple)
        disk_drives = info.get('disk_drives', [])
        if disk_drives:
            print(f"    ✓ Disques:")
            for drive in disk_drives:
                print(f"         - {drive}")
            print(f"         Total: {info.get('disk_total_gb', 'N/A')} GB")
        else:
            print(f"    ✓ Disque total: {info.get('disk_total_gb', 'N/A')} GB")

        print(f"    ✓ Logiciels détectés: {len(info.get('installed_software', []))}")

    # Générer rapport HTML complet
    if not args.quiet:
        print(f"\n[*] Génération du rapport HTML...")

    html_content, report_file = generate_html_report(info, args.client_id, args.client_name)
    if report_file and not args.quiet:
        print(f"    ✓ Rapport sauvegardé: {report_file}")

    # Envoyer à ParcInfo (avec champs filtrés)
    if not args.quiet:
        print(f"\n[*] Envoi à {args.server}...")
        if args.client_id:
            print(f"    Client ID: {args.client_id}")
        elif args.client_name:
            print(f"    Client: {args.client_name}")
        else:
            print(f"    ⚠️ No client specified - will use default or prompt")

    success, result = send_to_parcinfo(info, args.server, args.token,
                                      client_id=args.client_id,
                                      client_name=args.client_name)

    if success:
        if not args.quiet:
            print(f"    ✓ Succès!")
            print(f"\n[+] Appareil enregistré:")
            print(f"    ID: {result.get('device_id')}")
            print(f"    Hostname: {result.get('hostname')}")
            print(f"    IP: {result.get('ip_address')}")
            print(f"    MAC: {result.get('mac_address')}")
            if report_file:
                print(f"\n[+] Rapport complet sauvegardé: {report_file}")

        # Envoyer le rapport HTML en tant que document joint
        if report_file and not args.quiet:
            print(f"\n[*] Envoi du rapport vers les documents de l'appareil...")

        device_id = result.get('device_id')
        if report_file and device_id and args.client_id:
            success_report, result_report = upload_report_to_parcinfo(
                html_content, report_file, args.server, device_id, args.client_id
            )

            if success_report and not args.quiet:
                print(f"    ✓ Rapport joint enregistré")
                print(f"    Document ID: {result_report.get('document_id')}")
            elif not args.quiet:
                print(f"    ⚠️ Erreur lors du stockage du rapport: {result_report}")
    else:
        if not args.quiet:
            print(f"    ✗ Erreur: {result}")
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
