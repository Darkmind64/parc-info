#!/usr/bin/env python3
"""
ParcInfo System Information Collector - GUI Edition

Interface graphique pour collecter et envoyer les informations système à ParcInfo.
Permet de sélectionner le client cible et valider les données avant envoi.

Utilisation :
    python system-info-collector-gui.py
    python system-info-collector-gui.py --server http://192.168.1.100:3456
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
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import logging

# Configure logging to file for debugging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('collector-gui.log', mode='a'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('collector-gui')

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
    return [ip for ip in ips if not ip.startswith('127.')]


def get_os_info():
    """Récupère l'OS et la version exacte avec édition (Pro/Home/Server)."""
    os_name = platform.system()
    os_version = platform.release()
    edition = ""

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


def _win_powershell_json(cmd, timeout=15):
    """Exécute une commande PowerShell et parse le JSON retourné (aucune dépendance externe)."""
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', cmd],
            capture_output=True, text=True, timeout=timeout, creationflags=creationflags
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception as e:
        logger.debug(f"PowerShell command failed: {e}")
    return None


def get_system_info_windows():
    """Collecte les infos système sur Windows via ctypes/winreg/PowerShell (aucune dépendance externe requise)."""
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
    except Exception as e:
        logger.debug(f"RAM detection failed: {e}")

    # CPU nom (registre) + cores (os.cpu_count)
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        cpu_name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        winreg.CloseKey(key)
        info['cpu'] = cpu_name.strip()
    except Exception as e:
        logger.debug(f"CPU name detection failed: {e}")

    try:
        import os as _os
        info['cpu_cores'] = _os.cpu_count()
    except Exception as e:
        logger.debug(f"CPU cores detection failed: {e}")

    # Disques logiques fixes (kernel32)
    try:
        import ctypes
        import string

        disk_list = []
        total_disk = 0.0
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
                        disk_list.append(f"{letter}: ({size_gb} GB)")
                        total_disk += size_gb
        if disk_list:
            info['disk_drives'] = disk_list
            info['disk_total_gb'] = round(total_disk, 1)
    except Exception as e:
        logger.debug(f"Disk detection failed: {e}")

    # Marque / modèle (PowerShell CIM - natif Windows, aucune dépendance)
    system_data = _win_powershell_json(
        "Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model | ConvertTo-Json -Compress"
    )
    if system_data:
        info['brand'] = (system_data.get('Manufacturer') or '').strip()
        info['model'] = (system_data.get('Model') or '').strip()

    # Numéro de série (BIOS)
    bios_data = _win_powershell_json(
        "Get-CimInstance Win32_BIOS | Select-Object SerialNumber | ConvertTo-Json -Compress"
    )
    if bios_data:
        info['serial_number'] = (bios_data.get('SerialNumber') or '').strip()

    # Antivirus (SecurityCenter2)
    av_data = _win_powershell_json(
        "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntivirusProduct "
        "-ErrorAction SilentlyContinue | Select-Object displayName | ConvertTo-Json -Compress"
    )
    if av_data:
        if isinstance(av_data, list):
            avs = [d.get('displayName', '') for d in av_data if d.get('displayName')]
        else:
            name = av_data.get('displayName', '')
            avs = [name] if name else []
        if avs:
            info['antivirus'] = ', '.join(avs)

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
        lines = result.stdout.strip().split('\n')[1:]
        disk_list = []
        total_disk = 0
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                device = parts[0]
                size_str = parts[1]
                try:
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

    try:
        with open('/proc/cpuinfo', 'r') as f:
            lines = f.readlines()
            for line in lines:
                if line.startswith('model name'):
                    info['cpu'] = line.split(':', 1)[1].strip()
                    break
            info['cpu_cores'] = sum(1 for line in lines if line.startswith('processor'))
    except Exception:
        pass

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
        lines = result.stdout.strip().split('\n')[1:]
        disk_list = []
        total_disk = 0
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                device = parts[0]
                size_str = parts[1]
                try:
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


def get_local_network_range():
    """Détermine la plage de réseau local (ex: 192.168.1.0/24)."""
    try:
        # Récupère toutes les interfaces réseau
        import socket
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)

        # Détermine la plage de sous-réseau
        octets = local_ip.split('.')
        if len(octets) == 4:
            base = '.'.join(octets[:3])
            return base, local_ip
    except Exception:
        pass
    return None, None


def scan_network_for_parcinfo(timeout=2, progress_callback=None):
    """
    Scan le réseau local pour chercher des instances ParcInfo.
    Retourne une liste de serveurs trouvés: [{"url": "...", "clients": N}, ...]
    """
    servers = []
    base, local_ip = get_local_network_range()

    if not base:
        return servers

    logger.debug(f"Scanning network range {base}.x for ParcInfo instances...")

    # Scan les adresses 1-254 du sous-réseau
    for i in range(1, 255):
        if progress_callback:
            progress_callback(f"Scan {base}.{i}...")

        ip = f"{base}.{i}"
        if ip == local_ip:
            continue  # Skip local IP

        try:
            # Test rapide si le port 3456 est ouvert
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((ip, 3456))
            sock.close()

            if result == 0:  # Port ouvert
                logger.debug(f"Port 3456 open on {ip}, checking if ParcInfo...")

                # Vérifier que c'est ParcInfo en appelant l'endpoint
                try:
                    test_url = f"http://{ip}:3456/api/clients-public"
                    with urlopen(test_url, timeout=timeout) as response:
                        data = json.loads(response.read().decode('utf-8'))
                        clients_count = len(data) if isinstance(data, list) else 0
                        server_url = f"http://{ip}:3456"
                        servers.append({
                            'url': server_url,
                            'ip': ip,
                            'clients': clients_count
                        })
                        logger.debug(f"Found ParcInfo at {server_url} with {clients_count} clients")
                except Exception as e:
                    logger.debug(f"Not ParcInfo at {ip}: {e}")
        except Exception as e:
            logger.debug(f"Error scanning {ip}: {e}")

    return servers


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
        try:
            result = subprocess.run(['ls', '/Applications'], capture_output=True, text=True, timeout=5)
            software.extend([app.replace('.app', '') for app in result.stdout.split('\n') if app.endswith('.app')])
        except Exception:
            pass
        try:
            result = subprocess.run(['ls', '/usr/local/opt'], capture_output=True, text=True, timeout=5)
            software.extend([pkg for pkg in result.stdout.split('\n') if pkg.strip()])
        except Exception:
            pass

    elif IS_LINUX:
        try:
            result = subprocess.run(['dpkg', '--get-selections'], capture_output=True, text=True, timeout=10)
            software.extend([line.split()[0] for line in result.stdout.split('\n') if 'install' in line])
        except Exception:
            pass
        if not software:
            try:
                result = subprocess.run(['rpm', '-qa'], capture_output=True, text=True, timeout=10)
                software.extend([line.strip() for line in result.stdout.split('\n') if line.strip()])
            except Exception:
                pass

    return sorted(list(set(software)))[:200]


def collect_system_info():
    """Collecte toutes les infos système."""
    info = {
        'timestamp': datetime.utcnow().isoformat(),
        'mac_address': get_mac_address(),
        'hostname': get_hostname(),
        'ip_addresses': get_ip_addresses(),
    }

    info.update(get_os_info())

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
    """Génère un rapport HTML complet avec toutes les infos collectées."""
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
        .disk {{ background: #f8f9fa; padding: 10px; margin: 8px 0; border-radius: 4px; border-left: 3px solid #3498db; }}
        .software-list {{ background: #f8f9fa; padding: 15px; border-radius: 4px; max-height: 400px; overflow-y: auto; }}
        .software-item {{ padding: 4px 0; font-family: monospace; font-size: 12px; }}
        .badge {{ display: inline-block; padding: 3px 8px; border-radius: 3px; font-size: 11px; font-weight: bold; }}
        .badge.supported {{ background: #28a745; color: white; }}
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


def generate_pdf_report(info, client_id=None, client_name=None):
    """Génère un rapport PDF complet avec toutes les infos collectées."""
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    hostname = info.get('hostname', 'unknown')
    mac = info.get('mac_address', 'unknown')[:8]
    filename = f"system-info-report_{hostname}_{mac}_{timestamp}.pdf"

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib import colors

        doc = SimpleDocTemplate(filename, pagesize=A4)
        styles = getSampleStyleSheet()
        story = []

        # Titre
        title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=24, textColor=colors.HexColor('#2c3e50'), spaceAfter=6)
        story.append(Paragraph("📊 Rapport Collecte Système", title_style))
        story.append(Paragraph(f"Généré le {datetime.utcnow().strftime('%d/%m/%Y à %H:%M:%S UTC')}", styles['Normal']))
        story.append(Spacer(1, 0.3*inch))

        # Fonction helper pour ajouter une section
        def add_section(title, data):
            heading_style = ParagraphStyle('CustomHeading', parent=styles['Heading2'], fontSize=14, textColor=colors.HexColor('#2c3e50'), spaceAfter=10)
            story.append(Paragraph(title, heading_style))

            table_data = [[Paragraph(f"<b>{k}</b>", styles['Normal']), Paragraph(str(v), styles['Normal'])] for k, v in data.items()]
            if table_data:
                table = Table(table_data, colWidths=[2*inch, 3.5*inch])
                table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f0f0f0')),
                    ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                    ('GRID', (0, 0), (-1, -1), 1, colors.black)
                ]))
                story.append(table)
            story.append(Spacer(1, 0.3*inch))

        # Identification
        add_section("🔍 Identification", {
            "Nom machine": info.get('hostname', 'N/A'),
            "Système": info.get('os_name', 'N/A'),
            "Version OS": info.get('os_version', 'N/A'),
            "Adresse MAC": info.get('mac_address', 'N/A'),
        })

        # Matériel
        ram_val = info.get('ram_gb', '')
        ram_display = f"{ram_val} GB" if ram_val not in ('', None) else 'N/A'
        disk_val = info.get('disk_total_gb', '')
        disk_display = f"{disk_val} GB" if disk_val not in ('', None) else 'N/A'
        hardware_data = {
            "CPU": info.get('cpu', 'N/A'),
            "Cores": info.get('cpu_cores', 'N/A'),
            "RAM": ram_display,
            "Disque total": disk_display,
        }
        add_section("💻 Matériel", hardware_data)

        # Réseau
        add_section("🌐 Réseau", {
            "Adresse(s) IP": ', '.join(info.get('ip_addresses', [])) or 'N/A',
        })

        # Logiciels (limité à 20)
        software_list = info.get('installed_software', [])[:20]
        if software_list:
            story.append(Paragraph("<b>📦 Logiciels Installés (top 20)</b>", styles['Heading2']))
            for i, soft in enumerate(software_list, 1):
                story.append(Paragraph(f"{i}. {soft}", styles['Normal']))
            story.append(Spacer(1, 0.2*inch))

        # Métadonnées
        story.append(Spacer(1, 0.2*inch))
        metadata_style = ParagraphStyle('Metadata', parent=styles['Normal'], fontSize=8, textColor=colors.grey)
        story.append(Paragraph(f"<i>Rapport généré par system-info-collector | Client: {client_name or 'N/A'} (ID: {client_id or 'N/A'})</i>", metadata_style))

        # Générer le PDF
        doc.build(story)

        # Lire le contenu du fichier PDF
        with open(filename, 'rb') as f:
            pdf_content = f.read()
        return pdf_content, filename

    except Exception as e:
        logger.exception(f"Erreur génération PDF: {e}")
        return None, None


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
        'installed_software': info.get('installed_software', [])[:50],
    }

    if client_id:
        payload['client_id'] = client_id
    if client_name:
        payload['client_name'] = client_name

    return payload


def upload_report_to_parcinfo(report_content, report_file, server_url, device_id, client_id):
    """Envoie le rapport (PDF ou HTML) à ParcInfo en tant que document joint."""
    try:
        import io
        from urllib.request import Request

        # Déterminer le type MIME basé sur l'extension du fichier
        if report_file and report_file.endswith('.pdf'):
            content_type = 'application/pdf'
            filename = report_file
        else:
            content_type = 'text/html'
            filename = 'report.html'

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
        body.write(f'Content-Disposition: form-data; name="report"; filename="{filename}"\r\n'.encode())
        body.write(f'Content-Type: {content_type}\r\n\r\n'.encode())
        if isinstance(report_content, str):
            body.write(report_content.encode('utf-8'))
        else:
            body.write(report_content)
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


def fetch_clients(server_url):
    """Récupère la liste des clients depuis ParcInfo (endpoint public, pas d'auth requise)."""
    try:
        url = f"{server_url.rstrip('/')}/api/clients-public"
        logger.debug(f"Fetching clients from: {url}")
        with urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            logger.debug(f"Response: {data}")
            result = data if isinstance(data, list) else []
            logger.debug(f"Returning {len(result)} clients")
            return result
    except Exception as e:
        logger.error(f"ERROR fetching clients: {type(e).__name__}: {str(e)}", exc_info=True)
        return []


class CollectorGUI:
    def __init__(self, root, server_url):
        self.root = root
        self.server_url = server_url
        self.system_info = {}
        self.clients = []
        self.selected_client = None
        self.config_file = Path.home() / '.parcinfo-collector-config.json'

        self.root.title("ParcInfo System Information Collector")
        self.root.geometry("900x750")
        self.root.resizable(True, True)

        # Couleurs
        self.bg_color = "#f0f0f0"
        self.root.configure(bg=self.bg_color)

        # Charger l'URL sauvegardée si disponible
        self._load_config()

        self._create_widgets()
        self._collect_info()
        self._fetch_clients()

    def _load_config(self):
        """Charge la configuration sauvegardée."""
        try:
            if self.config_file.exists():
                with open(self.config_file) as f:
                    config = json.load(f)
                    saved_url = config.get('server_url')
                    if saved_url:
                        self.server_url = saved_url
                        logger.debug(f"Loaded server URL from config: {saved_url}")
        except Exception as e:
            logger.debug(f"Could not load config: {e}")

    def _save_config(self):
        """Sauvegarde la configuration."""
        try:
            with open(self.config_file, 'w') as f:
                json.dump({'server_url': self.server_url}, f)
                logger.debug(f"Saved server URL to config: {self.server_url}")
        except Exception as e:
            logger.warning(f"Could not save config: {e}")

    def _create_widgets(self):
        """Crée les widgets de l'interface."""
        # Header
        header = tk.Frame(self.root, bg="#2c3e50", height=60)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        title_label = tk.Label(header, text="ParcInfo - Collecteur d'Informations Système",
                              font=("Arial", 16, "bold"), bg="#2c3e50", fg="white")
        title_label.pack(pady=10)

        # Main content
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Section 0 : Configuration du serveur
        server_frame = ttk.LabelFrame(main_frame, text="0. Configuration du Serveur ParcInfo")
        server_frame.pack(fill=tk.X, pady=5)

        server_input_frame = tk.Frame(server_frame)
        server_input_frame.pack(fill=tk.X, padx=10, pady=10)

        tk.Label(server_input_frame, text="URL ParcInfo:").pack(side=tk.LEFT, padx=5)
        self.server_url_var = tk.StringVar(value=self.server_url)
        self.server_entry = tk.Entry(server_input_frame, textvariable=self.server_url_var, width=40)
        self.server_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        test_btn = ttk.Button(server_input_frame, text="🔗 Tester", command=self._test_connection)
        test_btn.pack(side=tk.LEFT, padx=3)

        scan_btn = ttk.Button(server_input_frame, text="🔍 Scan Réseau", command=self._scan_network)
        scan_btn.pack(side=tk.LEFT, padx=3)

        server_help = tk.Label(server_frame, text="Exemples: http://localhost:3456, http://192.168.1.100:3456, http://parcinfo.local:3456",
                              font=("Arial", 8), fg="#666")
        server_help.pack(padx=10, pady=2)

        # Section 1 : Sélection du client
        client_frame = ttk.LabelFrame(main_frame, text="1. Sélectionner le Client Cible")
        client_frame.pack(fill=tk.X, pady=5)

        self.client_var = tk.StringVar()
        self.client_combo = ttk.Combobox(client_frame, textvariable=self.client_var,
                                         state="readonly", width=50)
        self.client_combo.pack(padx=10, pady=10)
        self.client_combo.bind('<<ComboboxSelected>>', lambda e: self._on_client_selected())

        client_help = tk.Label(client_frame, text="⚠️ IMPORTANT : Sélectionner le client correct pour éviter le mélange de données",
                              font=("Arial", 9), fg="#c0392b")
        client_help.pack(padx=10, pady=5)

        # Section 2 : Résumé des infos collectées
        summary_frame = ttk.LabelFrame(main_frame, text="2. Informations Collectées")
        summary_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.summary_text = scrolledtext.ScrolledText(summary_frame, height=15, width=80,
                                                      font=("Courier", 9), bg="white")
        self.summary_text.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)
        self.summary_text.config(state=tk.DISABLED)

        # Section 3 : Boutons d'action
        action_frame = tk.Frame(self.root, bg=self.bg_color)
        action_frame.pack(fill=tk.X, padx=10, pady=10)

        refresh_btn = ttk.Button(action_frame, text="🔄 Rafraîchir les Infos",
                                command=self._collect_info)
        refresh_btn.pack(side=tk.LEFT, padx=5)

        send_btn = ttk.Button(action_frame, text="✓ Envoyer à ParcInfo",
                             command=self._send_data)
        send_btn.pack(side=tk.LEFT, padx=5)

        cancel_btn = ttk.Button(action_frame, text="✕ Annuler",
                               command=self.root.quit)
        cancel_btn.pack(side=tk.LEFT, padx=5)

        # Status bar
        self.status_var = tk.StringVar(value="En attente...")
        status_bar = tk.Label(self.root, textvariable=self.status_var,
                             bg="#ecf0f1", fg="#2c3e50", anchor=tk.W)
        status_bar.pack(fill=tk.X)

    def _scan_network(self):
        """Lance un scan du réseau pour chercher des instances ParcInfo."""
        self.status_var.set("Scan du réseau en cours...")
        self.root.update()

        def scan():
            try:
                logger.debug("Starting network scan for ParcInfo instances...")
                servers = scan_network_for_parcinfo(
                    timeout=1,
                    progress_callback=lambda msg: self.status_var.set(msg)
                )

                if not servers:
                    messagebox.showinfo("Scan Réseau",
                                      "Aucune instance ParcInfo trouvée sur le réseau local.\n\n"
                                      "Vérifiez que:\n"
                                      "1. ParcInfo est lancé\n"
                                      "2. C'est sur le même réseau local\n"
                                      "3. Le port 3456 est accessible")
                    self.status_var.set("Aucun serveur trouvé")
                    return

                # Affiche les résultats dans une fenêtre popup
                self._show_scan_results(servers)
                self.status_var.set("Scan terminé ✓")
            except Exception as e:
                logger.exception("Network scan failed")
                messagebox.showerror("Erreur de Scan", f"Erreur lors du scan: {e}")
                self.status_var.set("Erreur de scan")

        thread = threading.Thread(target=scan, daemon=True)
        thread.start()

    def _show_scan_results(self, servers):
        """Affiche les résultats du scan dans une fenêtre."""
        result_window = tk.Toplevel(self.root)
        result_window.title("Résultats du Scan Réseau")
        result_window.geometry("600x300")

        # Header
        header = tk.Label(result_window, text=f"🎉 {len(servers)} instance(s) ParcInfo trouvée(s)",
                         font=("Arial", 12, "bold"), bg="#2c3e50", fg="white", pady=10)
        header.pack(fill=tk.X)

        # Liste des serveurs
        frame = tk.Frame(result_window)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        for server in servers:
            btn_text = f"📍 {server['url']}   ({server['clients']} clients)"
            btn = tk.Button(
                frame,
                text=btn_text,
                bg="#ecf0f1",
                fg="#2c3e50",
                font=("Courier", 10),
                justify=tk.LEFT,
                command=lambda url=server['url']: self._select_found_server(url, result_window)
            )
            btn.pack(fill=tk.X, pady=5)

        # Bouton Annuler
        ttk.Button(frame, text="✕ Annuler", command=result_window.destroy).pack(pady=10)

    def _select_found_server(self, url, window):
        """Sélectionne un serveur trouvé par le scan."""
        self.server_url_var.set(url)
        self.server_url = url
        self._save_config()
        window.destroy()
        self._test_connection()

    def _test_connection(self):
        """Teste la connexion au serveur."""
        new_url = self.server_url_var.get().strip()
        if not new_url:
            messagebox.showerror("Erreur", "L'URL du serveur ne peut pas être vide")
            return

        self.status_var.set("Test de connexion...")
        self.root.update()

        def test():
            try:
                test_url = f"{new_url.rstrip('/')}/api/clients-public"
                logger.debug(f"Testing connection to: {test_url}")
                with urlopen(test_url, timeout=5) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    clients_count = len(data) if isinstance(data, list) else 0
                    self.server_url = new_url
                    self._save_config()
                    messagebox.showinfo("Connexion ✓",
                                      f"Connecté avec succès!\n\n"
                                      f"URL: {new_url}\n"
                                      f"Clients disponibles: {clients_count}\n\n"
                                      f"Récupération de la liste des clients...")
                    self._fetch_clients()
                    self.status_var.set("Connecté ✓")
            except Exception as e:
                logger.error(f"Connection test failed: {e}")
                messagebox.showerror("Erreur de Connexion",
                                   f"Impossible de se connecter à {new_url}\n\n"
                                   f"Erreur: {str(e)}\n\n"
                                   f"Vérifiez l'URL et essayez à nouveau.")
                self.status_var.set("Erreur de connexion")

        thread = threading.Thread(target=test, daemon=True)
        thread.start()

    def _collect_info(self):
        """Collecte les informations système."""
        self.status_var.set("Collecte des informations système...")
        self.root.update()

        def collect():
            self.system_info = collect_system_info()
            self._update_summary()
            self.status_var.set("Informations collectées ✓")

        thread = threading.Thread(target=collect, daemon=True)
        thread.start()

    def _fetch_clients(self):
        """Récupère la liste des clients."""
        self.status_var.set("Récupération de la liste des clients...")
        self.root.update()

        def fetch():
            try:
                logger.debug(f"Fetching clients from server: {self.server_url}")
                self.clients = fetch_clients(self.server_url)
                logger.debug(f"Got {len(self.clients)} clients: {self.clients}")

                if not self.clients:
                    logger.warning("No clients returned - showing default message")
                    self.client_combo['values'] = ["Aucun client trouvé"]
                    self.status_var.set("Erreur: Aucun client disponible")
                    return

                client_names = [f"{c.get('id', 'N/A')} - {c.get('nom', 'Inconnu')}" for c in self.clients]
                logger.debug(f"Client names: {client_names}")
                self.client_combo['values'] = client_names
                if client_names:
                    self.client_combo.current(0)
                    self._on_client_selected()
                self.status_var.set("Prêt à envoyer ✓")
            except Exception as e:
                logger.exception(f"ERROR in _fetch_clients: {type(e).__name__}: {str(e)}")
                self.status_var.set("Erreur lors de la récupération des clients")

        thread = threading.Thread(target=fetch, daemon=True)
        thread.start()

    def _on_client_selected(self):
        """Appelé quand un client est sélectionné."""
        idx = self.client_combo.current()
        if 0 <= idx < len(self.clients):
            self.selected_client = self.clients[idx]

    def _update_summary(self):
        """Met à jour le résumé des infos collectées."""
        self.summary_text.config(state=tk.NORMAL)
        self.summary_text.delete(1.0, tk.END)

        summary = []
        summary.append("═" * 80)
        summary.append("INFORMATIONS SYSTÈME COLLECTÉES".center(80))
        summary.append("═" * 80)
        summary.append("")

        # Section Identification
        summary.append("┌─ IDENTIFICATION")
        summary.append(f"│ Hostname           : {self.system_info.get('hostname', 'N/A')}")
        summary.append(f"│ MAC Address        : {self.system_info.get('mac_address', 'N/A')}")
        summary.append(f"│ IP Address(es)     : {', '.join(self.system_info.get('ip_addresses', []))}")
        summary.append(f"│ Marque             : {self.system_info.get('brand', 'N/A')}")
        summary.append(f"│ Modèle             : {self.system_info.get('model', 'N/A')}")
        summary.append(f"│ Numéro Série       : {self.system_info.get('serial_number', 'N/A')}")
        summary.append("└")
        summary.append("")

        # Section OS
        summary.append("┌─ SYSTÈME D'EXPLOITATION")
        summary.append(f"│ OS                 : {self.system_info.get('os_name', 'N/A')}")
        summary.append(f"│ Version            : {self.system_info.get('os_version', 'N/A')}")
        summary.append("└")
        summary.append("")

        # Section Matériel
        summary.append("┌─ MATÉRIEL")
        summary.append(f"│ RAM                : {self.system_info.get('ram_gb', 'N/A')} GB")
        summary.append(f"│ CPU                : {self.system_info.get('cpu', 'N/A')}")
        summary.append(f"│ CPU Cores          : {self.system_info.get('cpu_cores', 'N/A')}")

        # Affichage des disques (multiples ou simple)
        disk_drives = self.system_info.get('disk_drives', [])
        if disk_drives:
            summary.append(f"│ Disques :")
            for drive in disk_drives:
                summary.append(f"│   - {drive}")
            summary.append(f"│ Total Stockage     : {self.system_info.get('disk_total_gb', 'N/A')} GB")
        else:
            summary.append(f"│ Stockage           : {self.system_info.get('disk_total_gb', 'N/A')} GB")

        summary.append("└")
        summary.append("")

        # Section Sécurité
        av = self.system_info.get('antivirus')
        if av:
            summary.append("┌─ SÉCURITÉ")
            summary.append(f"│ Antivirus          : {av}")
            summary.append("└")
            summary.append("")

        # Section Logiciels
        software_list = self.system_info.get('installed_software', [])
        if software_list:
            summary.append("┌─ LOGICIELS INSTALLÉS")
            summary.append(f"│ Total détecté      : {len(software_list)} logiciel(s)")
            summary.append("│ Premiers 10 :")
            for i, soft in enumerate(software_list[:10], 1):
                summary.append(f"│   {i}. {soft}")
            if len(software_list) > 10:
                summary.append(f"│   ... et {len(software_list) - 10} autres")
            summary.append("└")
            summary.append("")

        summary.append("═" * 80)
        summary.append("Prêt à envoyer vers ParcInfo")
        summary.append("═" * 80)

        self.summary_text.insert(tk.END, "\n".join(summary))
        self.summary_text.config(state=tk.DISABLED)

    def _send_data(self):
        """Envoie les données à ParcInfo."""
        if not self.selected_client:
            messagebox.showerror("Erreur", "Veuillez sélectionner un client")
            return

        client_id = self.selected_client.get('id')
        client_name = self.selected_client.get('nom', 'Inconnu')

        # Confirmation
        msg = f"Envoyer les informations vers le client :\n\n📍 {client_id} - {client_name}\n\nEtes-vous sûr ?"
        if not messagebox.askyesno("Confirmation", msg):
            return

        self.status_var.set("Envoi en cours...")
        self.root.update()

        def send():
            try:
                logger.debug(f"System info collected: {self.system_info}")

                # Générer le rapport PDF
                pdf_content, report_file = generate_pdf_report(self.system_info, client_id, client_name)
                if pdf_content:
                    logger.debug(f"PDF report generated: {report_file} ({len(pdf_content)} bytes)")
                else:
                    logger.warning("PDF report generation returned no content")

                # Filtrer les champs pour l'API
                payload_data = get_api_payload(self.system_info, client_id, client_name)
                logger.debug(f"API payload: {payload_data}")
                payload = json.dumps(payload_data)

                headers = {'Content-Type': 'application/json'}
                request = Request(
                    f"{self.server_url.rstrip('/')}/api/device-info",
                    data=payload.encode('utf-8'),
                    headers=headers,
                    method='POST'
                )

                with urlopen(request, timeout=10) as response:
                    result = json.loads(response.read().decode('utf-8'))
                    logger.debug(f"API response: {result}")
                    if result.get('status') == 'success':
                        device_id = result.get('device_id')
                        msg = f"Appareil enregistré avec succès !\n\n"
                        msg += f"ID : {device_id}\n"
                        msg += f"Hostname : {result.get('hostname')}\n"
                        msg += f"IP : {result.get('ip_address')}\n"
                        msg += f"MAC : {result.get('mac_address')}"

                        # Uploader le rapport PDF
                        if pdf_content and device_id and client_id:
                            success_report, result_report = upload_report_to_parcinfo(
                                pdf_content, report_file, self.server_url, device_id, client_id
                            )
                            if success_report:
                                logger.debug(f"Report uploaded: {result_report}")
                                msg += f"\n✓ Rapport joint enregistré (Doc ID: {result_report.get('document_id')})"
                            else:
                                logger.error(f"Report upload failed: {result_report}")
                                msg += f"\n⚠️ Erreur lors du stockage du rapport: {result_report}"

                        if report_file:
                            msg += f"\n\nRapport complet sauvegardé :\n{report_file}"

                        messagebox.showinfo("Succès ✓", msg)
                        self.status_var.set("Envoi réussi ✓")
                    else:
                        logger.error(f"API returned error: {result}")
                        messagebox.showerror("Erreur", result.get('message', 'Erreur inconnue'))
                        self.status_var.set("Erreur lors de l'envoi")
            except Exception as e:
                logger.exception(f"ERROR in send(): {type(e).__name__}: {str(e)}")
                messagebox.showerror("Erreur de Connexion", str(e))
                self.status_var.set("Erreur de connexion")

        thread = threading.Thread(target=send, daemon=True)
        thread.start()


def main():
    parser = argparse.ArgumentParser(
        description='Collecteur d\'informations système avec interface graphique'
    )
    parser.add_argument('--server', default='http://parcinfo.local:3456',
                       help='URL du serveur ParcInfo (défaut: http://parcinfo.local:3456)')

    args = parser.parse_args()

    root = tk.Tk()
    app = CollectorGUI(root, args.server)
    root.mainloop()


if __name__ == '__main__':
    main()
