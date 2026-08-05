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
    """Récupère l'OS et la version exacte avec édition (Pro/Home/Server).

    os_version = "Display Version" Windows (ex: "22H2") - le build/feature update,
    os_name = nom complet lisible (ex: "Windows 11 Pro", "Windows Server 2022 Standard").

    Note: platform.release() renvoie toujours "10" sur Windows 11 (même noyau NT 10.0) -
    la version majeure (10 vs 11) doit être déduite du numéro de build (>= 22000 = Windows 11).
    """
    os_name = platform.system()  # 'Windows', 'Darwin', 'Linux'
    os_version = platform.release()
    full_os_name = os_name

    # Pour Windows, obtenir la version complète et l'édition
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion') as key:
                try:
                    os_version = winreg.QueryValueEx(key, 'DisplayVersion')[0]
                except Exception:
                    pass

                product_name = ''
                try:
                    product_name = winreg.QueryValueEx(key, 'ProductName')[0]
                except Exception:
                    pass

                edition_name = ''
                try:
                    edition_name = winreg.QueryValueEx(key, 'EditionID')[0]
                except Exception:
                    pass

                build_number = 0
                try:
                    build_number = int(winreg.QueryValueEx(key, 'CurrentBuildNumber')[0])
                except Exception:
                    pass

                if 'Server' in product_name:
                    # Le ProductName Windows Server contient déjà l'année (ex: "Windows Server 2022 Standard")
                    full_os_name = product_name
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
    except Exception:
        pass
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
        import os as _os
        info['cpu_cores'] = _os.cpu_count()
    except Exception:
        pass

    # Disques logiques fixes (kernel32) - taille, utilisé, libre
    try:
        import ctypes
        import string

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

    # Disques physiques : type (SSD/HDD) + état de santé SMART (Get-PhysicalDisk - natif Windows 8+)
    physical_disks_data = _win_powershell_json(
        "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,HealthStatus,Size,OperationalStatus "
        "| ConvertTo-Json -Compress"
    )
    if physical_disks_data:
        disks = physical_disks_data if isinstance(physical_disks_data, list) else [physical_disks_data]
        physical_list = []
        for d in disks:
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

    # Identification matérielle étendue : marque/modèle/domaine/BIOS/uptime/GPU
    # (1 seul appel PowerShell groupé - chaque champ est protégé individuellement
    # pour qu'une source manquante ne fasse pas échouer les autres)
    core_data = _win_powershell_json(
        "$cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue; "
        "$bios = Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue; "
        "$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue; "
        "$gpuNames = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty Name); "
        "$uptimeHours = $null; "
        "if ($os -and $os.LastBootUpTime) { $uptimeHours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1) }; "
        "$biosDate = $null; "
        "if ($bios -and $bios.ReleaseDate) { $biosDate = $bios.ReleaseDate.ToString('yyyy-MM-dd') }; "
        "[PSCustomObject]@{ "
        "Manufacturer=$cs.Manufacturer; Model=$cs.Model; Domain=$cs.Domain; "
        "PartOfDomain=$cs.PartOfDomain; Workgroup=$cs.Workgroup; "
        "SerialNumber=$bios.SerialNumber; BiosVersion=$bios.SMBIOSBIOSVersion; "
        "BiosReleaseDate=$biosDate; UptimeHours=$uptimeHours; GpuNames=$gpuNames "
        "} | ConvertTo-Json -Compress -Depth 4",
        timeout=20
    )
    if core_data:
        info['brand'] = (core_data.get('Manufacturer') or '').strip()
        info['model'] = (core_data.get('Model') or '').strip()
        info['serial_number'] = (core_data.get('SerialNumber') or '').strip()

        bios_version = core_data.get('BiosVersion')
        if isinstance(bios_version, list):
            bios_version = ', '.join(str(v) for v in bios_version if v)
        if bios_version:
            info['bios_version'] = str(bios_version).strip()

        if core_data.get('BiosReleaseDate'):
            info['bios_release_date'] = core_data.get('BiosReleaseDate')

        if core_data.get('PartOfDomain'):
            info['domain'] = core_data.get('Domain') or ''
        elif core_data.get('Workgroup'):
            info['workgroup'] = core_data.get('Workgroup')

        if core_data.get('UptimeHours') is not None:
            info['uptime_hours'] = core_data.get('UptimeHours')

        gpu_names = core_data.get('GpuNames')
        if gpu_names:
            gpus = gpu_names if isinstance(gpu_names, list) else [gpu_names]
            gpus = [g for g in gpus if g]
            if gpus:
                info['gpu'] = ', '.join(gpus)

    # Sécurité & conformité : antivirus, pare-feu, BitLocker, TPM, Secure Boot
    # (modules non garantis selon l'édition Windows - chaque source est protégée
    # par un try/catch PowerShell dédié pour ne pas faire échouer les autres)
    security_data = _win_powershell_json(
        "$av = @(); try { $av = @(Get-CimInstance -Namespace root/SecurityCenter2 "
        "-ClassName AntivirusProduct -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty displayName) } catch {}; "
        "$fw = @(); try { $fw = @(Get-NetFirewallProfile -ErrorAction SilentlyContinue "
        "| Select-Object Name,Enabled) } catch {}; "
        "$bl = @(); try { $bl = @(Get-BitLockerVolume -ErrorAction SilentlyContinue "
        "| Select-Object MountPoint,VolumeStatus,ProtectionStatus) } catch {}; "
        "$tpmObj = $null; try { $tpmObj = Get-Tpm -ErrorAction SilentlyContinue "
        "| Select-Object TpmPresent,TpmReady,TpmEnabled } catch {}; "
        "$secureBoot = $null; try { $secureBoot = Confirm-SecureBootUEFI -ErrorAction SilentlyContinue } catch {}; "
        "[PSCustomObject]@{ Antivirus=$av; Firewall=$fw; BitLocker=$bl; Tpm=$tpmObj; SecureBoot=$secureBoot } "
        "| ConvertTo-Json -Compress -Depth 4",
        timeout=20
    )
    if security_data:
        av_names = security_data.get('Antivirus')
        if av_names:
            avs = av_names if isinstance(av_names, list) else [av_names]
            avs = [a for a in avs if a]
            if avs:
                info['antivirus'] = ', '.join(avs)

        fw_raw = security_data.get('Firewall')
        if fw_raw:
            fw_list = fw_raw if isinstance(fw_raw, list) else [fw_raw]
            profiles = [f"{f.get('Name')}: {'Activé' if f.get('Enabled') else 'Désactivé'}" for f in fw_list if f.get('Name')]
            if profiles:
                info['firewall'] = profiles

        bl_raw = security_data.get('BitLocker')
        if bl_raw:
            bl_list = bl_raw if isinstance(bl_raw, list) else [bl_raw]
            bitlocker = [
                f"{b.get('MountPoint')}: {b.get('VolumeStatus', 'Inconnu')} "
                f"(Protection: {b.get('ProtectionStatus', 'Inconnu')})"
                for b in bl_list if b.get('MountPoint')
            ]
            if bitlocker:
                info['bitlocker'] = bitlocker

        tpm = security_data.get('Tpm')
        if tpm:
            info['tpm_present'] = bool(tpm.get('TpmPresent'))
            info['tpm_enabled'] = bool(tpm.get('TpmEnabled'))

        if security_data.get('SecureBoot') is not None:
            info['secure_boot'] = bool(security_data.get('SecureBoot'))

    # Comptes utilisateurs locaux + appartenance au groupe Administrateurs
    users_data = _win_powershell_json(
        "$users = @(Get-CimInstance Win32_UserAccount -Filter \"LocalAccount='True'\" "
        "-ErrorAction SilentlyContinue | Select-Object Name,Disabled,Lockout); "
        "$admins = @(); try { $admins = @(Get-LocalGroupMember -Group Administrators "
        "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name) } catch {}; "
        "[PSCustomObject]@{ Users=$users; Admins=$admins } | ConvertTo-Json -Compress -Depth 4",
        timeout=20
    )
    if users_data:
        admins_raw = users_data.get('Admins') or []
        admins_raw = admins_raw if isinstance(admins_raw, list) else [admins_raw]
        # Les membres du groupe local Administrateurs sont retournés au format "MACHINE\Nom"
        admin_names = {a.split('\\')[-1].lower() for a in admins_raw if a}

        users_raw = users_data.get('Users')
        if users_raw:
            users_list = users_raw if isinstance(users_raw, list) else [users_raw]
            user_list = []
            for u in users_list:
                name = u.get('Name', '')
                if not name:
                    continue
                if u.get('Disabled'):
                    status = 'Désactivé'
                elif u.get('Lockout'):
                    status = 'Verrouillé'
                else:
                    status = 'Actif'
                if name.lower() in admin_names:
                    status += ', Administrateur'
                user_list.append(f"{name} ({status})")
            if user_list:
                info['users'] = sorted(user_list)

    # Batterie (portables), adaptateurs réseau actifs, dernière mise à jour Windows
    extras_data = _win_powershell_json(
        "$battery = @(); try { $battery = @(Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue "
        "| Select-Object EstimatedChargeRemaining,BatteryStatus) } catch {}; "
        "$adapters = @(); try { $adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue "
        "| Where-Object Status -eq 'Up' | Select-Object Name,InterfaceDescription,LinkSpeed) } catch {}; "
        "$hotfix = $null; try { "
        "$hf = Get-HotFix -ErrorAction SilentlyContinue | Sort-Object InstalledOn -Descending | Select-Object -First 1; "
        "if ($hf) { $hfDate = $null; if ($hf.InstalledOn) { $hfDate = $hf.InstalledOn.ToString('yyyy-MM-dd') }; "
        "$hotfix = [PSCustomObject]@{ HotFixID=$hf.HotFixID; InstalledOn=$hfDate } } "
        "} catch {}; "
        "[PSCustomObject]@{ Battery=$battery; Adapters=$adapters; LastUpdate=$hotfix } "
        "| ConvertTo-Json -Compress -Depth 4",
        timeout=20
    )
    if extras_data:
        battery_raw = extras_data.get('Battery')
        if battery_raw:
            battery_list = battery_raw if isinstance(battery_raw, list) else [battery_raw]
            b = battery_list[0] if battery_list else None
            if b and b.get('EstimatedChargeRemaining') is not None:
                info['battery'] = f"{b.get('EstimatedChargeRemaining')}% (statut: {b.get('BatteryStatus', 'Inconnu')})"

        adapters_raw = extras_data.get('Adapters')
        if adapters_raw:
            adapters_list = adapters_raw if isinstance(adapters_raw, list) else [adapters_raw]
            adapters = [
                f"{a.get('Name')} — {a.get('InterfaceDescription', '')} — {a.get('LinkSpeed', 'N/A')}"
                for a in adapters_list if a.get('Name')
            ]
            if adapters:
                info['network_adapters'] = adapters

        hotfix = extras_data.get('LastUpdate')
        if hotfix and hotfix.get('HotFixID'):
            date_part = f" ({hotfix.get('InstalledOn')})" if hotfix.get('InstalledOn') else ''
            info['last_windows_update'] = f"{hotfix.get('HotFixID')}{date_part}"

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
                                    except WindowsError:
                                        continue
                                    if not display_name or not display_name.strip():
                                        continue
                                    name = display_name.strip()

                                    def _read(value_name):
                                        try:
                                            return winreg.QueryValueEx(subkey, value_name)[0]
                                        except WindowsError:
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
            result = subprocess.run(['ls', '/Applications'], capture_output=True, text=True, timeout=5)
            names.update(app.replace('.app', '') for app in result.stdout.split('\n') if app.endswith('.app'))
        except Exception:
            pass

        # /usr/local/opt (Homebrew)
        try:
            result = subprocess.run(['ls', '/usr/local/opt'], capture_output=True, text=True, timeout=5)
            names.update(pkg for pkg in result.stdout.split('\n') if pkg.strip())
        except Exception:
            pass

        # pkgutil pour les packages installés
        try:
            result = subprocess.run(['pkgutil', '--packages'], capture_output=True, text=True, timeout=10)
            names.update(line.strip() for line in result.stdout.split('\n') if line.strip())
        except Exception:
            pass

        for name in names:
            software[name] = {'name': name, 'version': '', 'publisher': '', 'install_date': ''}

    elif IS_LINUX:
        names = set()
        # Debian/Ubuntu (dpkg)
        try:
            result = subprocess.run(['dpkg', '--get-selections'], capture_output=True, text=True, timeout=10)
            names.update(line.split()[0] for line in result.stdout.split('\n') if 'install' in line)
        except Exception:
            pass

        # RedHat/CentOS (rpm)
        if not names:
            try:
                result = subprocess.run(['rpm', '-qa'], capture_output=True, text=True, timeout=10)
                names.update(line.strip() for line in result.stdout.split('\n') if line.strip())
            except Exception:
                pass

        # Arch Linux (pacman)
        if not names:
            try:
                result = subprocess.run(['pacman', '-Q'], capture_output=True, text=True, timeout=10)
                names.update(line.split()[0] for line in result.stdout.split('\n') if line.strip())
            except Exception:
                pass

        for name in names:
            software[name] = {'name': name, 'version': '', 'publisher': '', 'install_date': ''}

    return sorted(software.values(), key=lambda s: s['name'].lower())


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
    info['installed_software'] = get_installed_software()

    return info


def generate_html_report(info, client_id=None, client_name=None):
    """Génère un rapport HTML complet avec toutes les infos collectées.

    Retourne le contenu HTML et le chemin du fichier sauvegardé.
    """
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    hostname = info.get('hostname', 'unknown')
    mac = info.get('mac_address', 'unknown').replace(':', '').replace('/', '')[:8]
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
        if isinstance(soft, dict):
            label = soft.get('name', '')
            if soft.get('version'):
                label += f" (v{soft['version']})"
        else:
            label = str(soft)
        html += f'                            <div class="software-item">{i}. {label}</div>\n'

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
    try:
        import html as _html
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib import colors

        def esc(value):
            """Échappe &, <, > pour éviter que reportlab n'interprète le texte comme du XML."""
            return _html.escape(str(value), quote=False)

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        hostname = info.get('hostname', 'unknown')
        mac = info.get('mac_address', 'unknown').replace(':', '').replace('/', '')[:8]
        filename = f"system-info-report_{hostname}_{mac}_{timestamp}.pdf"

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
            story.append(Paragraph(esc(title), heading_style))

            table_data = [[Paragraph(f"<b>{esc(k)}</b>", styles['Normal']), Paragraph(esc(v), styles['Normal'])] for k, v in data.items()]
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

        # Sections
        add_section("🔍 Identification", {
            "Hostname": info.get('hostname', 'N/A'),
            "MAC": info.get('mac_address', 'N/A'),
            "IP(s)": ', '.join(info.get('ip_addresses', [])) or 'N/A',
            "Marque": info.get('brand', 'N/A'),
            "Modèle": info.get('model', 'N/A'),
            "Numéro de série": info.get('serial_number', 'N/A'),
            "Date de collecte": info.get('timestamp', 'N/A'),
        })

        uptime_hours = info.get('uptime_hours')
        uptime_display = f"{round(uptime_hours / 24, 1)} jour(s)" if uptime_hours is not None else 'N/A'
        domain_display = info.get('domain') or info.get('workgroup') or 'N/A'
        bios_display = info.get('bios_version', 'N/A')
        if info.get('bios_release_date'):
            bios_display += f" ({info.get('bios_release_date')})"
        add_section("🖥️ Système", {
            "OS": info.get('os_name', 'N/A'),
            "Version": info.get('os_version', 'N/A'),
            "Détail plateforme": info.get('platform', 'N/A'),
            "Domaine / Groupe de travail": domain_display,
            "BIOS": bios_display,
            "Uptime (depuis dernier redémarrage)": uptime_display,
            "Dernière mise à jour Windows": info.get('last_windows_update', 'N/A'),
        })

        # Sécurité & conformité
        security_data_display = {"Antivirus": info.get('antivirus', 'N/A')}
        if info.get('tpm_present') is not None:
            tpm_status = 'Présent et activé' if info.get('tpm_enabled') else ('Présent mais désactivé' if info.get('tpm_present') else 'Absent')
            security_data_display["TPM"] = tpm_status
        if info.get('secure_boot') is not None:
            security_data_display["Secure Boot"] = 'Activé' if info.get('secure_boot') else 'Désactivé'
        add_section("🛡️ Sécurité & Conformité", security_data_display)

        firewall = info.get('firewall', [])
        if firewall:
            story.append(Paragraph("<b>🔥 Pare-feu Windows</b>", styles['Heading2']))
            for profile in firewall:
                story.append(Paragraph(f"• {esc(profile)}", styles['Normal']))
            story.append(Spacer(1, 0.2*inch))

        bitlocker = info.get('bitlocker', [])
        if bitlocker:
            story.append(Paragraph("<b>🔒 BitLocker</b>", styles['Heading2']))
            for vol in bitlocker:
                story.append(Paragraph(f"• {esc(vol)}", styles['Normal']))
            story.append(Spacer(1, 0.2*inch))

        ram_val = info.get('ram_gb', '')
        ram_display = f"{ram_val} GB" if ram_val not in ('', None) else 'N/A'
        disk_val = info.get('disk_total_gb', '')
        disk_display = f"{disk_val} GB" if disk_val not in ('', None) else 'N/A'
        used_val = info.get('disk_used_gb', '')
        free_val = info.get('disk_free_gb', '')
        if used_val not in ('', None) and free_val not in ('', None):
            disk_display += f" ({used_val} GB utilisés, {free_val} GB libres)"
        add_section("⚙️ Matériel", {
            "CPU": info.get('cpu', 'N/A'),
            "Cores": info.get('cpu_cores', 'N/A'),
            "RAM": ram_display,
            "Carte(s) graphique(s)": info.get('gpu', 'N/A'),
            "Disque total": disk_display,
        })

        # Disques logiques (lettres, espace utilisé/libre)
        disk_drives = info.get('disk_drives', [])
        if disk_drives:
            story.append(Paragraph("<b>💾 Disques Logiques</b>", styles['Heading2']))
            for drive in disk_drives:
                story.append(Paragraph(f"• {esc(drive)}", styles['Normal']))
            story.append(Spacer(1, 0.2*inch))

        # Disques physiques (type SSD/HDD + santé SMART)
        physical_disks = info.get('physical_disks', [])
        if physical_disks:
            story.append(Paragraph("<b>🔩 Disques Physiques (Type &amp; Santé SMART)</b>", styles['Heading2']))
            for drive in physical_disks:
                story.append(Paragraph(f"• {esc(drive)}", styles['Normal']))
            story.append(Spacer(1, 0.2*inch))

        # Batterie (portables uniquement)
        if info.get('battery'):
            story.append(Paragraph("<b>🔋 Batterie</b>", styles['Heading2']))
            story.append(Paragraph(f"• {esc(info.get('battery'))}", styles['Normal']))
            story.append(Spacer(1, 0.2*inch))

        # Adaptateurs réseau actifs
        network_adapters = info.get('network_adapters', [])
        if network_adapters:
            story.append(Paragraph("<b>🌐 Adaptateurs Réseau Actifs</b>", styles['Heading2']))
            for adapter in network_adapters:
                story.append(Paragraph(f"• {esc(adapter)}", styles['Normal']))
            story.append(Spacer(1, 0.2*inch))

        # Comptes utilisateurs locaux
        users_list = info.get('users', [])
        if users_list:
            story.append(Paragraph(f"<b>👥 Comptes Utilisateurs Locaux ({len(users_list)})</b>", styles['Heading2']))
            for u in users_list:
                story.append(Paragraph(f"• {esc(u)}", styles['Normal']))
            story.append(Spacer(1, 0.2*inch))

        # Logiciels (liste complète : nom, version, éditeur, date d'installation)
        software_list = info.get('installed_software', [])
        if software_list:
            story.append(Paragraph(f"<b>📦 Logiciels Installés ({len(software_list)})</b>", styles['Heading2']))
            for i, soft in enumerate(software_list, 1):
                if isinstance(soft, dict):
                    parts = [soft.get('name', '')]
                    if soft.get('version'):
                        parts.append(f"v{soft['version']}")
                    if soft.get('publisher'):
                        parts.append(soft['publisher'])
                    if soft.get('install_date'):
                        parts.append(f"installé le {soft['install_date']}")
                    display = ' — '.join(p for p in parts if p)
                else:
                    display = str(soft)
                story.append(Paragraph(f"{i}. {esc(display)}", styles['Normal']))
            story.append(Spacer(1, 0.2*inch))

        # Métadonnées
        story.append(Spacer(1, 0.2*inch))
        metadata_style = ParagraphStyle('Metadata', parent=styles['Normal'], fontSize=8, textColor=colors.grey)
        story.append(Paragraph(f"<i>Rapport généré par system-info-collector | Client: {esc(client_name or 'N/A')} (ID: {client_id or 'N/A'})</i>", metadata_style))

        # Générer le PDF
        doc.build(story)

        # Lire le contenu du fichier PDF
        with open(filename, 'rb') as f:
            pdf_content = f.read()
        return pdf_content, filename

    except ImportError:
        print("⚠️  reportlab non disponible, utilisation du format HTML")
        return generate_html_report(info, client_id, client_name)
    except Exception as e:
        print(f"Erreur génération PDF: {e}")
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
        'gpu': info.get('gpu', ''),
        'installed_software': info.get('installed_software', []),
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
        from urllib.request import Request as MultipartRequest
        import io

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
        if info.get('domain'):
            print(f"    ✓ Domaine: {info.get('domain')}")
        elif info.get('workgroup'):
            print(f"    ✓ Groupe de travail: {info.get('workgroup')}")
        if info.get('uptime_hours') is not None:
            print(f"    ✓ Uptime: {round(info.get('uptime_hours') / 24, 1)} jour(s)")
        print(f"    ✓ RAM: {info.get('ram_gb', 'N/A')} GB")
        print(f"    ✓ CPU: {info.get('cpu', 'N/A')} ({info.get('cpu_cores', 'N/A')} cores)")
        if info.get('gpu'):
            print(f"    ✓ GPU: {info.get('gpu')}")

        # Affichage des disques (multiples ou simple)
        disk_drives = info.get('disk_drives', [])
        if disk_drives:
            print(f"    ✓ Disques logiques:")
            for drive in disk_drives:
                print(f"         - {drive}")
            print(f"         Total: {info.get('disk_total_gb', 'N/A')} GB")
        else:
            print(f"    ✓ Disque total: {info.get('disk_total_gb', 'N/A')} GB")

        physical_disks = info.get('physical_disks', [])
        if physical_disks:
            print(f"    ✓ Disques physiques (Type/SMART):")
            for drive in physical_disks:
                print(f"         - {drive}")

        if info.get('battery'):
            print(f"    ✓ Batterie: {info.get('battery')}")

        network_adapters = info.get('network_adapters', [])
        if network_adapters:
            print(f"    ✓ Adaptateurs réseau actifs:")
            for adapter in network_adapters:
                print(f"         - {adapter}")

        if info.get('antivirus'):
            print(f"    ✓ Antivirus: {info.get('antivirus')}")
        if info.get('firewall'):
            print(f"    ✓ Pare-feu: {', '.join(info.get('firewall'))}")
        if info.get('bitlocker'):
            print(f"    ✓ BitLocker: {', '.join(info.get('bitlocker'))}")
        if info.get('tpm_present') is not None:
            tpm_status = 'Présent et activé' if info.get('tpm_enabled') else ('Présent mais désactivé' if info.get('tpm_present') else 'Absent')
            print(f"    ✓ TPM: {tpm_status}")
        if info.get('secure_boot') is not None:
            print(f"    ✓ Secure Boot: {'Activé' if info.get('secure_boot') else 'Désactivé'}")
        if info.get('last_windows_update'):
            print(f"    ✓ Dernière mise à jour Windows: {info.get('last_windows_update')}")

        users_list = info.get('users', [])
        if users_list:
            print(f"    ✓ Comptes utilisateurs locaux: {len(users_list)}")
            for u in users_list:
                print(f"         - {u}")

        print(f"    ✓ Logiciels détectés: {len(info.get('installed_software', []))}")

    # Générer rapport PDF
    if not args.quiet:
        print(f"\n[*] Génération du rapport PDF...")

    pdf_content, report_file = generate_pdf_report(info, args.client_id, args.client_name)
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

        # Envoyer le rapport PDF en tant que document joint
        if report_file and not args.quiet:
            print(f"\n[*] Envoi du rapport vers les documents de l'appareil...")

        device_id = result.get('device_id')
        if report_file and device_id and args.client_id:
            success_report, result_report = upload_report_to_parcinfo(
                pdf_content, report_file, args.server, device_id, args.client_id
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
