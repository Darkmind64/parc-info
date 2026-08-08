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
import socket
import string
import subprocess
import sys
import uuid
from datetime import datetime
from urllib.error import URLError
from urllib.request import Request, urlopen

__all__ = [
    'IS_WINDOWS', 'IS_MAC', 'IS_LINUX',
    'collect_system_info', 'build_summary_lines',
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
    """Exécute une commande PowerShell et parse le JSON retourné."""
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


def _run(cmd, timeout=10):
    """Exécute une commande et retourne stdout (chaîne vide en cas d'échec)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout or ''
    except Exception:
        return ''


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
        "Year = $_.YearOfManufacture } }) } catch {}; "
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
        "[PSCustomObject]@{ Monitors=$mon; Printers=$printers; Reliability=$rel; Ports=$ports } "
        "| ConvertTo-Json -Compress -Depth 4",
        timeout=40
    )
    if not data:
        return info

    # ── Écrans ─────────────────────────────────────────────────────────────
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
        monitors.append({
            'manufacturer': manufacturer,
            'model': model,
            'serial_number': serial,
            'year': m.get('Year') or '',
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


def _win_security():
    """Antivirus, pare-feu, BitLocker, TPM, Secure Boot.

    Modules non garantis selon l'édition Windows - chaque source est protégée
    par un try/catch PowerShell dédié pour ne pas faire échouer les autres.
    """
    info = {}
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
        timeout=25
    )
    if not security_data:
        return info

    avs = [a for a in _as_list(security_data.get('Antivirus')) if a]
    if avs:
        info['antivirus'] = ', '.join(avs)

    profiles = [
        f"{f.get('Name')}: {'Activé' if f.get('Enabled') else 'Désactivé'}"
        for f in _as_list(security_data.get('Firewall')) if f.get('Name')
    ]
    if profiles:
        info['firewall'] = profiles

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


def _win_users():
    """Comptes utilisateurs locaux + appartenance au groupe Administrateurs."""
    info = {}
    users_data = _win_powershell_json(
        "$users = @(Get-CimInstance Win32_UserAccount -Filter \"LocalAccount='True'\" "
        "-ErrorAction SilentlyContinue | Select-Object Name,Disabled,Lockout); "
        "$admins = @(); try { $admins = @(Get-LocalGroupMember -Group Administrators "
        "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name) } catch {}; "
        "[PSCustomObject]@{ Users=$users; Admins=$admins } | ConvertTo-Json -Compress -Depth 4",
        timeout=25
    )
    if not users_data:
        return info

    # Les membres du groupe local Administrateurs sont retournés au format "MACHINE\Nom"
    admin_names = {a.split('\\')[-1].lower() for a in _as_list(users_data.get('Admins')) if a}

    user_list = []
    for u in _as_list(users_data.get('Users')):
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

    return info


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
        "| Where-Object Status -eq 'Up' | Select-Object Name,InterfaceDescription,LinkSpeed,MacAddress) } catch {}; "
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
        })
    if adapters:
        info['network_adapters'] = adapters
        info['network_adapter_details'] = adapter_details

    return info


def get_system_info_windows():
    """Collecte Windows complète (ctypes/winreg/PowerShell, sans dépendance externe)."""
    info = {}
    for collector in (_win_base_hardware, _win_core, _win_hardware_detail,
                      _win_inventory, _win_licensing, _win_security,
                      _win_users, _win_extras):
        try:
            info.update(collector() or {})
        except Exception:
            # Un bloc en échec ne doit jamais empêcher les autres de remonter
            pass
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

def collect_system_info():
    """Collecte toutes les infos système."""
    info = {
        'collector_version': COLLECTOR_VERSION,
        'timestamp': datetime.utcnow().isoformat(),
        'elevated': is_elevated(),
        'mac_address': get_mac_address(),
        'hostname': get_hostname(),
        'dns_name': get_fqdn(),
        'ip_addresses': get_ip_addresses(),
    }

    # OS
    info.update(get_os_info())

    # Infos spécifiques par OS
    try:
        if IS_WINDOWS:
            info.update(get_system_info_windows())
        elif IS_MAC:
            info.update(get_system_info_mac())
        elif IS_LINUX:
            info.update(get_system_info_linux())
    except Exception:
        pass

    # Logiciels
    info['installed_software'] = get_installed_software()

    # Type d'appareil déduit
    info['device_type'] = guess_device_type(info)

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


def build_summary_lines(info):
    """Construit le résumé textuel partagé par la sortie CLI et l'aperçu GUI."""
    lines = []

    def section(title):
        lines.append(f"┌─ {title}")

    def field(label, value):
        lines.append(f"│ {label:<22}: {value}")

    def item(value):
        lines.append(f"│   - {value}")

    def close():
        lines.append("└")
        lines.append("")

    # ── Identification ─────────────────────────────────────────────────────
    section("IDENTIFICATION")
    field("Hostname", info.get('hostname', 'N/A'))
    if info.get('dns_name'):
        field("Nom DNS", info['dns_name'])
    field("Adresse MAC", info.get('mac_address', 'N/A'))
    field("Adresse(s) IP", ', '.join(info.get('ip_addresses', [])) or 'N/A')
    field("Type d'appareil", info.get('device_type', 'N/A'))
    field("Marque", info.get('brand') or 'N/A')
    field("Modèle", info.get('model') or 'N/A')
    field("Numéro de série", info.get('serial_number') or 'N/A')
    if info.get('asset_tag'):
        field("Asset tag", info['asset_tag'])
    if info.get('chassis_type'):
        field("Châssis", info['chassis_type'])
    close()

    # ── Système ────────────────────────────────────────────────────────────
    section("SYSTÈME D'EXPLOITATION")
    field("OS", info.get('os_name', 'N/A'))
    field("Version", info.get('os_version', 'N/A'))
    if info.get('os_build'):
        field("Build", info['os_build'])
    if info.get('architecture'):
        field("Architecture", info['architecture'])
    if info.get('os_install_date'):
        field("Installé le", info['os_install_date'])
    field("Domaine/Groupe", info.get('domain') or info.get('workgroup') or 'N/A')
    if info.get('logged_on_user'):
        field("Session ouverte", info['logged_on_user'])
    uptime = info.get('uptime_hours')
    if uptime is not None:
        field("Uptime", f"{round(uptime / 24, 1)} jour(s)")
    if info.get('bios_version'):
        bios = info['bios_version']
        if info.get('bios_release_date'):
            bios += f" ({info['bios_release_date']})"
        field("BIOS", bios)
    if info.get('hypervisor_present'):
        field("Virtualisation", "Machine virtuelle / hyperviseur détecté")
    close()

    # ── Matériel ───────────────────────────────────────────────────────────
    section("MATÉRIEL")
    field("CPU", info.get('cpu', 'N/A'))
    cores = []
    if info.get('cpu_physical_cores'):
        cores.append(f"{info['cpu_physical_cores']} cœurs physiques")
    if info.get('cpu_logical_cores'):
        cores.append(f"{info['cpu_logical_cores']} logiques")
    if not cores and info.get('cpu_cores'):
        cores.append(f"{info['cpu_cores']} cœurs")
    if cores:
        field("Cœurs", ', '.join(cores))
    if info.get('cpu_max_clock_mhz'):
        field("Fréquence max", f"{info['cpu_max_clock_mhz']} MHz")
    if info.get('motherboard'):
        mb = info['motherboard']
        field("Carte mère", f"{mb.get('manufacturer', '')} {mb.get('model', '')}".strip() or 'N/A')
    field("RAM", f"{info.get('ram_gb', 'N/A')} GB")
    if info.get('memory_slots_total'):
        field("Slots mémoire", f"{info.get('memory_slots_used', 0)}/{info['memory_slots_total']} occupés"
                               + (f" (max {info['memory_max_gb']} GB)" if info.get('memory_max_gb') else ''))
    if info.get('memory_modules'):
        field("Barrettes", '')
        for module in info['memory_modules']:
            item(format_memory_module(module))
    if info.get('gpu_details'):
        field("Carte(s) graphique(s)", '')
        for gpu in info['gpu_details']:
            detail = gpu['name']
            if gpu.get('vram_gb'):
                detail += f" — {gpu['vram_gb']} GB VRAM"
            if gpu.get('resolution'):
                detail += f" — {gpu['resolution']}"
            if gpu.get('driver_version'):
                detail += f" — pilote {gpu['driver_version']}"
            item(detail)
    elif info.get('gpu'):
        field("Carte graphique", info['gpu'])
    close()

    # ── Stockage ───────────────────────────────────────────────────────────
    section("STOCKAGE")
    disk_total = info.get('disk_total_gb', 'N/A')
    if info.get('disk_used_gb') is not None and info.get('disk_free_gb') is not None:
        field("Total", f"{disk_total} GB ({info['disk_used_gb']} GB utilisés, {info['disk_free_gb']} GB libres)")
    else:
        field("Total", f"{disk_total} GB")
    for drive in info.get('disk_drives', []):
        item(drive)
    for drive in info.get('physical_disks', []):
        item(drive)
    for entry in info.get('disk_reliability', []):
        item(format_reliability(entry))
    close()

    # ── Écrans / imprimantes ───────────────────────────────────────────────
    if info.get('monitors') or info.get('printers'):
        section("PÉRIPHÉRIQUES")
        for mon in info.get('monitors', []):
            item(f"Écran : {format_monitor(mon)}")
        for pr in info.get('printers', []):
            item(f"Imprimante : {format_printer(pr)}")
        close()

    # ── Réseau ─────────────────────────────────────────────────────────────
    if info.get('network_adapters') or info.get('listening_ports'):
        section("RÉSEAU")
        for adapter in info.get('network_adapters', []):
            item(adapter)
        ports = info.get('listening_ports', [])
        if ports:
            field("Ports en écoute", f"{len(ports)} port(s) TCP")
            preview = ', '.join(
                f"{p['port']}" + (f" ({p['process']})" if p.get('process') else '')
                for p in ports[:15]
            )
            item(preview + (' …' if len(ports) > 15 else ''))
        close()

    # ── Batterie ───────────────────────────────────────────────────────────
    if info.get('battery') or info.get('battery_health_percent') is not None:
        section("BATTERIE")
        if info.get('battery'):
            field("Charge", info['battery'])
        if info.get('battery_health_percent') is not None:
            field("Santé", f"{info['battery_health_percent']}% "
                           f"(usure {info.get('battery_wear_percent', '?')}%)")
        if info.get('battery_cycles'):
            field("Cycles", info['battery_cycles'])
        if info.get('battery_health_status'):
            field("État", info['battery_health_status'])
        close()

    # ── Sécurité ───────────────────────────────────────────────────────────
    has_security = any(info.get(k) is not None for k in ('antivirus', 'tpm_present', 'secure_boot')) \
        or info.get('firewall') or info.get('bitlocker')
    if has_security:
        section("SÉCURITÉ & CONFORMITÉ")
        if info.get('antivirus'):
            field("Antivirus", info['antivirus'])
        for profile in info.get('firewall', []):
            item(f"Pare-feu {profile}")
        for vol in info.get('bitlocker', []):
            item(f"Chiffrement {vol}")
        if info.get('tpm_present') is not None:
            field("TPM", 'Présent et activé' if info.get('tpm_enabled')
                  else ('Présent mais désactivé' if info['tpm_present'] else 'Absent'))
        if info.get('secure_boot') is not None:
            field("Secure Boot", 'Activé' if info['secure_boot'] else 'Désactivé')
        close()

    # ── Licences ───────────────────────────────────────────────────────────
    if info.get('licenses') or info.get('oem_product_key'):
        section("LICENCES & ACTIVATION")
        if info.get('windows_activated') is not None:
            field("Windows", 'Activé' if info['windows_activated'] else 'NON ACTIVÉ')
        for lic in info.get('licenses', []):
            item(format_license(lic))
        if info.get('oem_product_key'):
            field("Clé OEM (firmware)", info['oem_product_key'])
        close()

    # ── Mises à jour ───────────────────────────────────────────────────────
    hotfixes = info.get('hotfixes', [])
    if hotfixes:
        section("MISES À JOUR")
        field("Correctifs installés", len(hotfixes))
        if info.get('last_windows_update'):
            field("Dernier correctif", info['last_windows_update'])
        for hf in hotfixes[:5]:
            item(f"{hf['id']} — {hf.get('installed_on') or 'date inconnue'}")
        if len(hotfixes) > 5:
            item(f"… et {len(hotfixes) - 5} autre(s)")
        close()

    # ── Comptes ────────────────────────────────────────────────────────────
    users = info.get('users', [])
    if users:
        section("COMPTES UTILISATEURS LOCAUX")
        field("Total", f"{len(users)} compte(s)")
        for u in users:
            item(u)
        close()

    # ── Logiciels ──────────────────────────────────────────────────────────
    software = info.get('installed_software', [])
    if software:
        section("LOGICIELS INSTALLÉS")
        field("Total", f"{len(software)} logiciel(s)")
        for soft in software[:10]:
            label = soft.get('name', '') if isinstance(soft, dict) else str(soft)
            if isinstance(soft, dict) and soft.get('version'):
                label += f" (v{soft['version']})"
            item(label)
        if len(software) > 10:
            item(f"… et {len(software) - 10} autre(s)")
        close()

    if not info.get('elevated'):
        lines.append("⚠ Collecte sans privilèges administrateur : SMART détaillé, TPM,")
        lines.append("  BitLocker et clé OEM peuvent être absents de ce rapport.")
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


def generate_html_report(info, client_id=None, client_name=None):
    """Génère un rapport HTML (repli quand reportlab n'est pas disponible)."""
    import html as _html

    def esc(value):
        return _html.escape(str(value), quote=False)

    filename = _report_filename(info, 'html')
    sections = []

    for line in build_summary_lines(info):
        sections.append(f"<div class='line'>{esc(line)}</div>")

    software_rows = ''.join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            esc(s.get('name', '')), esc(s.get('version', '')),
            esc(s.get('publisher', '')), esc(s.get('install_date', ''))
        )
        for s in info.get('installed_software', []) if isinstance(s, dict)
    )

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rapport système — {esc(info.get('hostname', 'inconnu'))}</title>
<style>
  body {{ font-family: 'Segoe UI', -apple-system, sans-serif; background: #f5f5f5;
         color: #222; line-height: 1.5; margin: 0; padding: 20px; }}
  .container {{ max-width: 1000px; margin: 0 auto; background: #fff; border-radius: 8px;
                box-shadow: 0 2px 10px rgba(0,0,0,.1); overflow: hidden; }}
  .header {{ background: linear-gradient(135deg, #2c3e50, #34495e); color: #fff; padding: 28px; }}
  .content {{ padding: 24px; }}
  .line {{ font-family: Consolas, monospace; font-size: 12.5px; white-space: pre; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 12px; font-size: 12px; }}
  th, td {{ border: 1px solid #ddd; padding: 5px 8px; text-align: left; }}
  th {{ background: #f0f0f0; }}
  h2 {{ border-bottom: 3px solid #3498db; padding-bottom: 8px; margin-top: 28px; font-size: 17px; }}
  .meta {{ background: #ecf0f1; padding: 14px; border-radius: 4px; font-size: 12px; margin-top: 20px; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Rapport système — {esc(info.get('hostname', 'inconnu'))}</h1>
    <p>Généré le {datetime.utcnow().strftime('%d/%m/%Y à %H:%M:%S UTC')}
       — collecteur v{esc(info.get('collector_version', COLLECTOR_VERSION))}</p>
  </div>
  <div class="content">
    {''.join(sections)}
    <h2>Logiciels installés ({len(info.get('installed_software', []))})</h2>
    <table>
      <tr><th>Nom</th><th>Version</th><th>Éditeur</th><th>Installé le</th></tr>
      {software_rows}
    </table>
    <div class="meta">
      <p><strong>Client cible :</strong> {esc(client_name or (f'ID {client_id}' if client_id else 'Non spécifié'))}</p>
      <p><strong>Horodatage :</strong> {esc(info.get('timestamp', 'N/A'))}</p>
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


def generate_pdf_report(info, client_id=None, client_name=None):
    """Génère un rapport PDF structuré façon Belarc (repli HTML si reportlab absent)."""
    try:
        import html as _html
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        def esc(value):
            """Échappe &, <, > pour éviter que reportlab n'interprète le texte comme du XML."""
            return _html.escape(str(value), quote=False)

        filename = _report_filename(info, 'pdf')
        doc = SimpleDocTemplate(filename, pagesize=A4,
                                topMargin=0.6 * inch, bottomMargin=0.6 * inch)
        styles = getSampleStyleSheet()
        story = []

        title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=20,
                                     textColor=colors.HexColor('#2c3e50'), spaceAfter=4)
        heading_style = ParagraphStyle('CustomHeading', parent=styles['Heading2'], fontSize=13,
                                       textColor=colors.HexColor('#2c3e50'), spaceBefore=12, spaceAfter=8)
        small_style = ParagraphStyle('Small', parent=styles['Normal'], fontSize=8.5, leading=11)

        story.append(Paragraph("Rapport système ParcInfo", title_style))
        story.append(Paragraph(
            f"{esc(info.get('hostname', 'inconnu'))} — généré le "
            f"{datetime.utcnow().strftime('%d/%m/%Y à %H:%M:%S UTC')} "
            f"(collecteur v{esc(info.get('collector_version', COLLECTOR_VERSION))})",
            styles['Normal']))
        if not info.get('elevated'):
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(
                "<b>Collecte sans privilèges administrateur</b> — SMART détaillé, TPM, BitLocker "
                "et clé OEM peuvent être absents de ce rapport.",
                ParagraphStyle('Warn', parent=styles['Normal'], fontSize=9,
                               textColor=colors.HexColor('#b45309'))))
        story.append(Spacer(1, 0.25 * inch))

        def add_table(title, data):
            """Ajoute une section clé/valeur, en ignorant les valeurs vides."""
            rows = [(k, v) for k, v in data.items() if v not in ('', None, 'N/A')]
            if not rows:
                return
            story.append(Paragraph(esc(title), heading_style))
            table_data = [[Paragraph(f"<b>{esc(k)}</b>", small_style), Paragraph(esc(v), small_style)]
                          for k, v in rows]
            table = Table(table_data, colWidths=[2.1 * inch, 4.4 * inch])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f0f0f0')),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('FONTSIZE', (0, 0), (-1, -1), 8.5),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 5),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#bbbbbb')),
            ]))
            story.append(table)

        def add_list(title, entries, numbered=False):
            """Ajoute une section en liste à puces (ou numérotée pour les longues listes)."""
            entries = [e for e in entries if e]
            if not entries:
                return
            story.append(Paragraph(esc(title), heading_style))
            for i, entry in enumerate(entries, 1):
                prefix = f"{i}. " if numbered else "• "
                story.append(Paragraph(f"{prefix}{esc(entry)}", small_style))

        # ── Identification ─────────────────────────────────────────────────
        add_table("Identification", {
            "Nom de la machine": info.get('hostname', 'N/A'),
            "Nom DNS": info.get('dns_name', ''),
            "Type d'appareil": info.get('device_type', ''),
            "Adresse MAC": info.get('mac_address', 'N/A'),
            "Adresse(s) IP": ', '.join(info.get('ip_addresses', [])),
            "Marque": info.get('brand', ''),
            "Modèle": info.get('model', ''),
            "Numéro de série": info.get('serial_number', ''),
            "Asset tag": info.get('asset_tag', ''),
            "Châssis": info.get('chassis_type', ''),
            "Date de collecte": info.get('timestamp', ''),
        })

        # ── Système ────────────────────────────────────────────────────────
        uptime_hours = info.get('uptime_hours')
        bios_display = info.get('bios_version', '')
        if bios_display and info.get('bios_release_date'):
            bios_display += f" ({info['bios_release_date']})"
        add_table("Système d'exploitation", {
            "OS": info.get('os_name', 'N/A'),
            "Version": info.get('os_version', ''),
            "Build": info.get('os_build', ''),
            "Architecture": info.get('architecture', ''),
            "Date d'installation": info.get('os_install_date', ''),
            "Propriétaire enregistré": info.get('registered_owner', ''),
            "Organisation": info.get('registered_organization', ''),
            "Fuseau horaire": info.get('timezone', ''),
            "Domaine / Groupe de travail": info.get('domain') or info.get('workgroup', ''),
            "Session ouverte": info.get('logged_on_user', ''),
            "BIOS": bios_display,
            "Fabricant BIOS": info.get('bios_manufacturer', ''),
            "Uptime": f"{round(uptime_hours / 24, 1)} jour(s)" if uptime_hours is not None else '',
            "Hyperviseur détecté": 'Oui' if info.get('hypervisor_present') else '',
        })

        # ── Carte mère & processeur ────────────────────────────────────────
        mb = info.get('motherboard') or {}
        cpu_cores_display = ''
        if info.get('cpu_physical_cores') or info.get('cpu_logical_cores'):
            cpu_cores_display = (f"{info.get('cpu_physical_cores', '?')} physiques / "
                                 f"{info.get('cpu_logical_cores', '?')} logiques")
        elif info.get('cpu_cores'):
            cpu_cores_display = str(info['cpu_cores'])
        add_table("Carte mère & processeur", {
            "Carte mère": f"{mb.get('manufacturer', '')} {mb.get('model', '')}".strip(),
            "Version carte mère": mb.get('version', ''),
            "N° série carte mère": mb.get('serial_number', ''),
            "Processeur": info.get('cpu', ''),
            "Cœurs": cpu_cores_display,
            "Sockets": info.get('cpu_sockets', ''),
            "Socket": info.get('cpu_socket', ''),
            "Fréquence maximale": f"{info['cpu_max_clock_mhz']} MHz" if info.get('cpu_max_clock_mhz') else '',
            "Cache L2": f"{info['cpu_l2_cache_kb']} KB" if info.get('cpu_l2_cache_kb') else '',
            "Cache L3": f"{info['cpu_l3_cache_kb']} KB" if info.get('cpu_l3_cache_kb') else '',
            "Virtualisation matérielle": ('Activée' if info['cpu_virtualization'] else 'Désactivée')
                                          if info.get('cpu_virtualization') is not None else '',
        })

        # ── Mémoire ────────────────────────────────────────────────────────
        slots_display = ''
        if info.get('memory_slots_total'):
            slots_display = f"{info.get('memory_slots_used', 0)} occupé(s) sur {info['memory_slots_total']}"
            if info.get('memory_slots_free'):
                slots_display += f" — {info['memory_slots_free']} libre(s)"
        add_table("Mémoire", {
            "RAM totale": f"{info['ram_gb']} GB" if info.get('ram_gb') else '',
            "RAM disponible": f"{info['ram_free_gb']} GB" if info.get('ram_free_gb') else '',
            "Slots": slots_display,
            "Capacité maximale": f"{info['memory_max_gb']} GB" if info.get('memory_max_gb') else '',
        })
        add_list("Barrettes mémoire installées",
                 [format_memory_module(m) for m in info.get('memory_modules', [])])

        # ── Stockage ───────────────────────────────────────────────────────
        disk_display = f"{info['disk_total_gb']} GB" if info.get('disk_total_gb') else ''
        if disk_display and info.get('disk_used_gb') is not None:
            disk_display += f" ({info['disk_used_gb']} GB utilisés, {info.get('disk_free_gb', '?')} GB libres)"
        add_table("Stockage", {"Capacité totale": disk_display})
        add_list("Volumes logiques", info.get('disk_drives', []))
        add_list("Disques physiques (type & santé SMART)", info.get('physical_disks', []))
        add_list("Usure et fiabilité des disques",
                 [format_reliability(r) for r in info.get('disk_reliability', [])])

        # ── Graphique ──────────────────────────────────────────────────────
        gpu_entries = []
        for gpu in info.get('gpu_details', []):
            entry = gpu['name']
            if gpu.get('vram_gb'):
                entry += f" — {gpu['vram_gb']} GB VRAM"
            if gpu.get('resolution'):
                entry += f" — {gpu['resolution']}"
            if gpu.get('driver_version'):
                entry += f" — pilote {gpu['driver_version']}"
            if gpu.get('driver_date'):
                entry += f" ({gpu['driver_date']})"
            gpu_entries.append(entry)
        if gpu_entries:
            add_list("Cartes graphiques", gpu_entries)
        elif info.get('gpu'):
            add_table("Cartes graphiques", {"Carte(s)": info['gpu']})

        # ── Écrans & imprimantes ───────────────────────────────────────────
        add_list("Écrans", [format_monitor(m) for m in info.get('monitors', [])])
        add_list("Imprimantes", [format_printer(p) for p in info.get('printers', [])])

        # ── Batterie ───────────────────────────────────────────────────────
        battery_health = ''
        if info.get('battery_health_percent') is not None:
            battery_health = (f"{info['battery_health_percent']}% de la capacité d'origine "
                              f"(usure {info.get('battery_wear_percent', '?')}%)")
        add_table("Batterie", {
            "Charge actuelle": info.get('battery', ''),
            "Santé": battery_health,
            "Capacité de conception": f"{info['battery_designed_capacity_mwh']} mWh"
                                       if info.get('battery_designed_capacity_mwh') else '',
            "Capacité réelle": f"{info['battery_full_capacity_mwh']} mWh"
                                if info.get('battery_full_capacity_mwh') else '',
            "Cycles de charge": info.get('battery_cycles', ''),
            "État": info.get('battery_health_status', ''),
        })

        # ── Réseau ─────────────────────────────────────────────────────────
        add_list("Adaptateurs réseau actifs", info.get('network_adapters', []))
        ports = info.get('listening_ports', [])
        if ports:
            add_list(f"Ports TCP en écoute ({len(ports)})", [
                f"{p['port']}" + (f" — {p['process']}" if p.get('process') else '')
                for p in ports
            ])

        # ── Sécurité ───────────────────────────────────────────────────────
        add_table("Sécurité & conformité", {
            "Antivirus": info.get('antivirus', ''),
            "TPM": ('Présent et activé' if info.get('tpm_enabled')
                    else ('Présent mais désactivé' if info.get('tpm_present') else 'Absent'))
                   if info.get('tpm_present') is not None else '',
            "Secure Boot": ('Activé' if info['secure_boot'] else 'Désactivé')
                           if info.get('secure_boot') is not None else '',
        })
        add_list("Pare-feu", info.get('firewall', []))
        add_list("Chiffrement des volumes", info.get('bitlocker', []))

        # ── Licences ───────────────────────────────────────────────────────
        license_entries = [format_license(l) for l in info.get('licenses', [])]
        if info.get('oem_product_key'):
            license_entries.append(f"Clé OEM inscrite dans le firmware : {info['oem_product_key']}")
        add_list("Licences & activation", license_entries)

        # ── Correctifs ─────────────────────────────────────────────────────
        hotfixes = info.get('hotfixes', [])
        if hotfixes:
            add_list(f"Correctifs Windows installés ({len(hotfixes)})", [
                f"{hf['id']} — {hf.get('installed_on') or 'date inconnue'}"
                + (f" — {hf['description']}" if hf.get('description') else '')
                for hf in hotfixes
            ])

        # ── Comptes ────────────────────────────────────────────────────────
        users = info.get('users', [])
        if users:
            add_list(f"Comptes utilisateurs locaux ({len(users)})", users)

        # ── Logiciels ──────────────────────────────────────────────────────
        software_list = info.get('installed_software', [])
        if software_list:
            entries = []
            for soft in software_list:
                if isinstance(soft, dict):
                    parts = [soft.get('name', '')]
                    if soft.get('version'):
                        parts.append(f"v{soft['version']}")
                    if soft.get('publisher'):
                        parts.append(soft['publisher'])
                    if soft.get('install_date'):
                        parts.append(f"installé le {soft['install_date']}")
                    entries.append(' — '.join(p for p in parts if p))
                else:
                    entries.append(str(soft))
            add_list(f"Logiciels installés ({len(entries)})", entries, numbered=True)

        # ── Métadonnées ────────────────────────────────────────────────────
        story.append(Spacer(1, 0.25 * inch))
        story.append(Paragraph(
            f"<i>Rapport généré par system-info-collector v{esc(COLLECTOR_VERSION)} — "
            f"client : {esc(client_name or 'N/A')} (ID : {esc(client_id or 'N/A')})</i>",
            ParagraphStyle('Metadata', parent=styles['Normal'], fontSize=7.5, textColor=colors.grey)))

        doc.build(story)

        with open(filename, 'rb') as f:
            return f.read(), filename

    except ImportError:
        # reportlab absent : le rapport HTML reste exploitable et uploadable
        return generate_html_report(info, client_id, client_name)
    except Exception as e:
        print(f"Erreur génération PDF: {e}")
        return None, None


# ════════════════════════════════════════════════════════════════════════════
# API PARCINFO
# ════════════════════════════════════════════════════════════════════════════

# Champs exclus du snapshot JSON envoyé au serveur : soit redondants avec les
# colonnes dédiées, soit trop volumineux (la liste logicielle a sa propre colonne).
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
        # Périphériques créés automatiquement côté serveur
        'monitors': info.get('monitors', []),
        'printers': info.get('printers', []),
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


def fetch_clients(server_url):
    """Récupère la liste des clients depuis ParcInfo (endpoint public, sans auth)."""
    try:
        url = f"{server_url.rstrip('/')}/api/clients-public"
        with urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data if isinstance(data, list) else []
    except Exception:
        return []
