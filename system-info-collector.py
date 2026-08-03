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
    """Récupère l'OS et la version exacte."""
    os_name = platform.system()  # 'Windows', 'Darwin', 'Linux'
    os_version = platform.release()

    # Pour Windows, obtenir la version complète
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion') as key:
                display_version = winreg.QueryValueEx(key, 'DisplayVersion')[0]
                os_version = f"{os_version} ({display_version})"
        except Exception:
            pass

    # Pour macOS
    if IS_MAC:
        try:
            mac_ver = platform.mac_ver()
            os_version = mac_ver[0]
        except Exception:
            pass

    return {
        "os_name": os_name,
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

        # Ajouter client_id ou client_name
        if client_id:
            info['client_id'] = client_id
        if client_name:
            info['client_name'] = client_name

        payload = json.dumps(info)
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

    # Envoyer à ParcInfo
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
            print(f"\n[+] Résultat : {json.dumps(result, indent=2)}")
            print("\nLe système a été enregistré dans ParcInfo.")
    else:
        if not args.quiet:
            print(f"    ✗ Erreur: {result}")
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
