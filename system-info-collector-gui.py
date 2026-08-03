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
    """Récupère l'OS et la version exacte."""
    os_name = platform.system()
    os_version = platform.release()

    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion') as key:
                display_version = winreg.QueryValueEx(key, 'DisplayVersion')[0]
                os_version = f"{os_version} ({display_version})"
        except Exception:
            pass

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

        try:
            system = c.Win32_ComputerSystem()[0]
            info['brand'] = system.Manufacturer.strip()
            info['model'] = system.Model.strip()
        except Exception:
            pass

        try:
            bios = c.Win32_SystemEnclosure()[0]
            info['serial_number'] = bios.SerialNumber.strip()
        except Exception:
            pass

        try:
            mem = c.Win32_PhysicalMemory()
            total_ram_bytes = sum(int(m.Capacity) for m in mem)
            info['ram_gb'] = round(total_ram_bytes / (1024 ** 3), 1)
        except Exception:
            pass

        try:
            cpu = c.Win32_Processor()[0]
            info['cpu'] = cpu.Name.strip()
            info['cpu_cores'] = int(cpu.NumberOfCores) if hasattr(cpu, 'NumberOfCores') else None
        except Exception:
            pass

        try:
            disk = c.Win32_LogicalDisk(Name='C:')[0]
            info['disk_total_gb'] = round(int(disk.Size) / (1024 ** 3), 1)
        except Exception:
            pass

        try:
            av_products = c.Win32_SecurityCenter1()[0]
            av_list = av_products.AntivirusProduct if hasattr(av_products, 'AntivirusProduct') else None
            if av_list:
                avs = [av.split('\\')[0] for av in (av_list if isinstance(av_list, list) else [av_list])]
                info['antivirus'] = ', '.join(avs)
        except Exception:
            pass

    except ImportError:
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

    try:
        result = subprocess.run(['df', '/'], capture_output=True, text=True, timeout=5)
        lines = result.stdout.strip().split('\n')
        if len(lines) > 1:
            parts = lines[1].split()
            if len(parts) >= 2:
                info['disk_total_gb'] = round(int(parts[1]) / (1024 * 1024), 1)
    except Exception:
        pass

    return info


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

    return info


def fetch_clients(server_url):
    """Récupère la liste des clients depuis ParcInfo (endpoint public, pas d'auth requise)."""
    try:
        url = f"{server_url.rstrip('/')}/api/clients-public"
        with urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data if isinstance(data, list) else []
    except Exception as e:
        return []


class CollectorGUI:
    def __init__(self, root, server_url):
        self.root = root
        self.server_url = server_url
        self.system_info = {}
        self.clients = []
        self.selected_client = None

        self.root.title("ParcInfo System Information Collector")
        self.root.geometry("900x700")
        self.root.resizable(True, True)

        # Couleurs
        self.bg_color = "#f0f0f0"
        self.root.configure(bg=self.bg_color)

        self._create_widgets()
        self._collect_info()
        self._fetch_clients()

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
            self.clients = fetch_clients(self.server_url)
            client_names = [f"{c.get('id', 'N/A')} - {c.get('nom', 'Inconnu')}" for c in self.clients]
            self.client_combo['values'] = client_names
            if client_names:
                self.client_combo.current(0)
                self._on_client_selected()
            self.status_var.set("Prêt à envoyer ✓")

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
                headers = {'Content-Type': 'application/json'}
                payload = json.dumps({**self.system_info, 'client_id': client_id})
                request = Request(
                    f"{self.server_url.rstrip('/')}/api/device-info",
                    data=payload.encode('utf-8'),
                    headers=headers,
                    method='POST'
                )

                with urlopen(request, timeout=10) as response:
                    result = json.loads(response.read().decode('utf-8'))
                    if result.get('status') == 'success':
                        messagebox.showinfo("Succès ✓",
                            f"Appareil enregistré avec succès !\n\n"
                            f"ID : {result.get('device_id')}\n"
                            f"Hostname : {result.get('hostname')}\n"
                            f"IP : {result.get('ip_address')}\n"
                            f"MAC : {result.get('mac_address')}")
                        self.status_var.set("Envoi réussi ✓")
                    else:
                        messagebox.showerror("Erreur", result.get('message', 'Erreur inconnue'))
                        self.status_var.set("Erreur lors de l'envoi")
            except Exception as e:
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
