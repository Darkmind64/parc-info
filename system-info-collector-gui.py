#!/usr/bin/env python3
"""
ParcInfo System Information Collector - GUI Edition

Interface graphique pour collecter et envoyer les informations système à ParcInfo.
Permet de sélectionner le client cible et valider les données avant envoi.

Toute la logique de collecte vit dans `collector_core.py`, partagé avec le
collecteur CLI ; ce script ne contient que l'interface graphique et la
découverte réseau des serveurs ParcInfo.

Utilisation :
    python system-info-collector-gui.py
    python system-info-collector-gui.py --server http://192.168.1.100:3456
"""

import argparse
import json
import logging
import socket
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from urllib.request import urlopen

from collector_core import (
    COLLECTOR_VERSION,
    get_mac_address,
    build_summary_lines,
    collect_system_info,
    fetch_clients,
    generate_pdf_report,
    is_elevated,
    send_to_parcinfo,
    upload_report_to_parcinfo,
)

# La console Windows est en cp1252 : le journal du collecteur contient des
# libellés accentués et des pictogrammes, qui y lèveraient un UnicodeEncodeError.
for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

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


# ════════════════════════════════════════════════════════════════════════════
# DÉCOUVERTE RÉSEAU (spécifique au GUI)
# ════════════════════════════════════════════════════════════════════════════

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

    for i in range(1, 255):
        if progress_callback:
            progress_callback(f"Scan {base}.{i}...")

        ip = f"{base}.{i}"
        if ip == local_ip:
            continue

        try:
            # Test rapide si le port 3456 est ouvert
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((ip, 3456))
            sock.close()

            if result == 0:  # Port ouvert
                logger.debug(f"Port 3456 open on {ip}, checking if ParcInfo...")

                # Vérifier que c'est ParcInfo en appelant l'endpoint public
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


# ════════════════════════════════════════════════════════════════════════════
# INTERFACE
# ════════════════════════════════════════════════════════════════════════════

class CollectorGUI:
    def __init__(self, root, server_url):
        self.root = root
        self.server_url = server_url
        self.system_info = {}
        self.clients = []
        self.selected_client = None
        self.config_file = Path.home() / '.parcinfo-collector-config.json'

        self.root.title(f"ParcInfo System Information Collector v{COLLECTOR_VERSION}")
        self.root.geometry("900x750")
        self.root.resizable(True, True)

        # Couleurs
        self.bg_color = "#f0f0f0"
        self.root.configure(bg=self.bg_color)

        # Charger l'URL sauvegardée si disponible
        self._load_config()

        self._create_widgets()
        # La collecte alimente self.system_info, dont l'adresse MAC sert à
        # reconnaître le client : elle doit donc démarrer en premier. Les deux
        # opérations restent asynchrones, la reconnaissance échoue simplement
        # si la MAC n'est pas encore connue.
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

        # Bandeau de reconnaissance : quand le serveur identifie la machine par
        # son adresse MAC, l'utilisateur doit le voir sans avoir à lire la barre
        # d'état, sinon il refait la sélection à la main.
        self.reconnaissance_var = tk.StringVar(value="")
        self.reconnaissance_label = tk.Label(
            client_frame, textvariable=self.reconnaissance_var,
            font=("Arial", 9, "bold"), fg="#1e8449", bg="#eafaf1",
            anchor=tk.W, padx=8, pady=4)
        # Masqué tant qu'il n'y a rien à annoncer
        self.client_help = tk.Label(
            client_frame,
            text="⚠️ IMPORTANT : Sélectionner le client correct pour éviter le mélange de données",
            font=("Arial", 9), fg="#c0392b")
        self.client_help.pack(padx=10, pady=5)

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

        # Test de débit : décoché par défaut. Il consomme de la bande passante
        # sur le poste de l'utilisateur et sollicite un service tiers, ce qui
        # doit rester un choix explicite et non un comportement systématique.
        self.test_debit_var = tk.BooleanVar(value=False)
        debit_check = ttk.Checkbutton(
            self.root, variable=self.test_debit_var,
            text="Mesurer aussi le débit descendant (télécharge ~10 Mo)")
        debit_check.pack(anchor=tk.W, padx=12, pady=2)

        # Status bar + progression : la collecte dure une bonne minute, une
        # interface figée sans indication passe pour un plantage.
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(self.root, orient=tk.HORIZONTAL,
                                            mode='determinate', maximum=100.0,
                                            variable=self.progress_var)
        self.progress_bar.pack(fill=tk.X)

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

        header = tk.Label(result_window, text=f"🎉 {len(servers)} instance(s) ParcInfo trouvée(s)",
                          font=("Arial", 12, "bold"), bg="#2c3e50", fg="white", pady=10)
        header.pack(fill=tk.X)

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
        self.progress_var.set(0.0)
        self.root.update()

        def avancement(fraction, libelle):
            # La collecte tourne dans un thread : la mise à jour des widgets est
            # renvoyée vers la boucle Tk, seule autorisée à y toucher.
            self.root.after(0, lambda: self._afficher_avancement(fraction, libelle))

        # La valeur de la case est lue ici, dans la boucle Tk : une variable
        # Tkinter ne se lit pas depuis un autre thread.
        test_debit = self.test_debit_var.get()

        def collect():
            try:
                self.system_info = collect_system_info(
                    progress=avancement, test_debit=test_debit)
                self.root.after(0, self._update_summary)
                self.root.after(0, lambda: self.progress_var.set(100.0))
                self.status_var.set("Informations collectées ✓")
            except Exception as e:
                logger.exception("Collecte interrompue")
                self.root.after(0, lambda: self.status_var.set(f"Erreur de collecte : {e}"))

        thread = threading.Thread(target=collect, daemon=True)
        thread.start()

    def _afficher_avancement(self, fraction, libelle):
        """Reporte l'avancement dans la barre et l'étiquette d'état."""
        self.progress_var.set(round(fraction * 100, 1))
        self.status_var.set("%s… (%d %%)" % (libelle, round(fraction * 100)))

    def _fetch_clients(self):
        """Récupère la liste des clients."""
        self.status_var.set("Récupération de la liste des clients...")
        self.root.update()

        def fetch():
            # Ce thread ne fait que du réseau : Tkinter n'est pas sûr en
            # multithread, toute écriture dans un widget est renvoyée à la
            # boucle principale via after().
            try:
                logger.debug(f"Fetching clients from server: {self.server_url}")
                # L'adresse MAC permet au serveur de reconnaître une machine
                # déjà inventoriée et de désigner son client. Elle est lue
                # directement plutôt que prise dans self.system_info : la
                # collecte complète dure une minute et tourne en parallèle,
                # alors que cette lecture est immédiate.
                mac = get_mac_address()
                clients, suggestion = fetch_clients(self.server_url, mac_address=mac)
                logger.debug(f"Got {len(clients)} clients, suggestion={suggestion}")
                self.root.after(0, self._appliquer_clients, clients, suggestion)
            except Exception as e:
                logger.exception(f"ERROR in _fetch_clients: {type(e).__name__}: {str(e)}")
                self.root.after(
                    0, lambda: self.status_var.set("Erreur lors de la récupération des clients"))

        thread = threading.Thread(target=fetch, daemon=True)
        thread.start()

    def _appliquer_clients(self, clients, suggestion):
        """Renseigne la liste des clients — exécuté dans la boucle Tk."""
        self.clients = clients
        if not clients:
            logger.warning("No clients returned - showing default message")
            self.client_combo['values'] = ["Aucun client trouvé"]
            self.status_var.set("Erreur: Aucun client disponible")
            return

        noms = [f"{c.get('id', 'N/A')} - {c.get('nom', 'Inconnu')}" for c in clients]
        self.client_combo['values'] = noms

        index = 0
        message = "Prêt à envoyer ✓"
        if suggestion:
            # Présélectionner le client auquel cette machine est déjà rattachée,
            # sans l'imposer : la liste reste modifiable.
            for i, c in enumerate(clients):
                if c.get('id') == suggestion.get('id'):
                    index = i
                    message = ("Client reconnu d'après l'adresse MAC : %s ✓"
                               % suggestion.get('nom', ''))
                    self._afficher_reconnaissance(suggestion.get('nom', ''))
                    break

        self.client_combo.current(index)
        self._on_client_selected()
        self.status_var.set(message)

    def _afficher_reconnaissance(self, nom_client):
        """Annonce visuellement que le client a été déduit de l'adresse MAC.

        Le bandeau remplace l'avertissement générique : quand la machine est
        déjà inventoriée, le risque de mélange de données ne se pose plus.
        """
        self.reconnaissance_var.set(
            "✓ Machine déjà connue : client « %s » présélectionné d'après son "
            "adresse MAC. Modifiable ci-dessus si nécessaire." % nom_client)
        self.reconnaissance_label.pack(fill=tk.X, padx=10, pady=(0, 6))
        self.client_help.pack_forget()

    def _on_client_selected(self):
        """Appelé quand un client est sélectionné."""
        idx = self.client_combo.current()
        if 0 <= idx < len(self.clients):
            self.selected_client = self.clients[idx]

    def _update_summary(self):
        """Met à jour le résumé des infos collectées."""
        self.summary_text.config(state=tk.NORMAL)
        self.summary_text.delete(1.0, tk.END)

        lines = ["═" * 80, "INFORMATIONS SYSTÈME COLLECTÉES".center(80), "═" * 80, ""]
        lines.extend(build_summary_lines(self.system_info))
        lines.extend(["═" * 80, "Prêt à envoyer vers ParcInfo", "═" * 80])

        self.summary_text.insert(tk.END, "\n".join(lines))
        self.summary_text.config(state=tk.DISABLED)

    def _send_data(self):
        """Envoie les données à ParcInfo."""
        if not self.selected_client:
            messagebox.showerror("Erreur", "Veuillez sélectionner un client")
            return

        client_id = self.selected_client.get('id')
        client_name = self.selected_client.get('nom', 'Inconnu')

        msg = f"Envoyer les informations vers le client :\n\n📍 {client_id} - {client_name}\n\nEtes-vous sûr ?"
        if not messagebox.askyesno("Confirmation", msg):
            return

        self.status_var.set("Envoi en cours...")
        self.root.update()

        def send():
            try:
                # Générer le rapport PDF
                pdf_content, report_file = generate_pdf_report(self.system_info, client_id, client_name)
                if pdf_content:
                    logger.debug(f"PDF report generated: {report_file} ({len(pdf_content)} bytes)")
                else:
                    logger.warning("PDF report generation returned no content")

                success, result = send_to_parcinfo(
                    self.system_info, self.server_url,
                    client_id=client_id, client_name=client_name
                )

                if not success:
                    logger.error(f"API call failed: {result}")
                    messagebox.showerror("Erreur de Connexion", str(result))
                    self.status_var.set("Erreur de connexion")
                    return

                if result.get('status') != 'success':
                    logger.error(f"API returned error: {result}")
                    messagebox.showerror("Erreur", result.get('message', 'Erreur inconnue'))
                    self.status_var.set("Erreur lors de l'envoi")
                    return

                device_id = result.get('device_id')
                msg = "Appareil enregistré avec succès !\n\n"
                msg += f"ID : {device_id}\n"
                msg += f"Hostname : {result.get('hostname')}\n"
                msg += f"IP : {result.get('ip_address')}\n"
                msg += f"MAC : {result.get('mac_address')}"
                if result.get('peripherals_created'):
                    msg += f"\nPériphériques créés : {result.get('peripherals_created')}"

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

    if not is_elevated():
        logger.warning("Collecteur lancé sans privilèges administrateur - "
                       "SMART détaillé, TPM, BitLocker et clé OEM peuvent manquer")

    root = tk.Tk()
    CollectorGUI(root, args.server)
    root.mainloop()


if __name__ == '__main__':
    main()
