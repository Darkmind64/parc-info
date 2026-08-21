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
import os
import shlex
import socket
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from urllib.request import urlopen

from collector_core import (
    COLLECTOR_VERSION,
    discover_parcinfo_mdns,
    get_all_mac_addresses,
    build_summary_sections,
    collect_system_info,
    fetch_clients,
    generate_pdf_report,
    get_wifi_profiles,
    is_elevated,
    send_to_parcinfo,
    send_wifi_credentials_to_parcinfo,
    upload_report_to_parcinfo,
)

# La console Windows est en cp1252 : le journal du collecteur contient des
# libellés accentués et des pictogrammes, qui y lèveraient un UnicodeEncodeError.
for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

# Même cause que le correctif du journal ci-dessous, mais pour TOUT le reste
# du module : generate_pdf_report()/generate_html_report() (collector_core.py)
# écrivent le rapport sous un nom de fichier RELATIF (_report_filename()),
# résolu contre le répertoire courant. Sous macOS (Finder/LaunchServices), ce
# répertoire est "/" — non inscriptible par un utilisateur normal. L'écriture
# échoue alors silencieusement côté collector_core (try/except déjà en
# place, qui renvoie `None` comme chemin de fichier), mais `Path(None)` plus
# loin dans ce module levait ensuite un TypeError bien moins parlant que le
# problème réel. Se repositionner une bonne fois sur le dossier personnel de
# l'utilisateur, dès le lancement, rend inscriptible tout chemin relatif
# écrit par la suite — pas seulement celui déjà identifié.
try:
    os.chdir(str(Path.home()))
except Exception:
    pass

# Configure logging to file for debugging
#
# Chemin ABSOLU, jamais relatif au répertoire courant : celui-ci dépend de
# comment l'exécutable a été lancé. Double-clic Windows → dossier de l'exe
# (écriture OK) ; double-clic macOS (Finder/LaunchServices) → "/" (racine),
# où un utilisateur normal ne peut pas écrire. Avec un chemin relatif, la
# construction de FileHandler levait un PermissionError non rattrapé au tout
# premier import du module — avant même la fenêtre Tk — et l'app (build
# windowed, sans console pour afficher la trace) se fermait silencieusement
# sans aucune icône dans le dock ni message d'erreur. Reproduit uniquement
# sur macOS ; lancer le binaire depuis un terminal (CWD = son propre
# dossier) masquait le bug.
_log_handlers = [logging.StreamHandler(sys.stdout)]
try:
    _log_handlers.append(
        logging.FileHandler(str(Path.home() / '.parcinfo-collector-gui.log'), mode='a'))
except Exception:
    pass  # Console seule : un home dir inaccessible ne doit jamais empêcher le lancement

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - [%(levelname)s] %(message)s',
    handlers=_log_handlers,
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


def _compter_clients(server_url, timeout):
    """Nombre de clients visibles sur une instance — confirme au passage que
    c'est bien du ParcInfo (l'endpoint existe et répond en JSON), pas un
    service HTTP quelconque tombé sur le même port. None si injoignable ou
    protégé par un jeton collecteur (pas encore saisi à ce stade du flux)."""
    try:
        with urlopen(f"{server_url}/api/clients-public", timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                return len((data.get('clients') or []))
            return 0
    except Exception:
        return None


def scan_network_for_parcinfo(timeout=2, progress_callback=None):
    """
    Découvre les instances ParcInfo sur le réseau local — d'abord par mDNS
    (rapide, donne le port réel, pas seulement le 3456 par défaut), puis par
    balayage du sous-réseau en repli pour ce que le mDNS ne trouve pas.

    Le repli reste nécessaire : une instance Docker en réseau « bridge » (le
    mode par défaut) ne relaie généralement pas le trafic multicast mDNS
    vers le réseau local et n'apparaît donc probablement que via le balayage
    — voir discover_parcinfo_mdns() dans collector_core.py.

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
    except Exception as e:
        logger.debug(f"mDNS discovery failed: {e}")

    base, local_ip = get_local_network_range()
    if base:
        logger.debug(f"Scanning network range {base}.x for ParcInfo instances...")
        for i in range(1, 255):
            if progress_callback:
                progress_callback(f"Scan {base}.{i}...")

            ip = f"{base}.{i}"
            if ip == local_ip:
                continue
            server_url = f"http://{ip}:3456"
            if server_url in servers:
                continue  # déjà trouvé par mDNS, inutile de le sonder deux fois

            try:
                # Test rapide si le port 3456 est ouvert
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                result = sock.connect_ex((ip, 3456))
                sock.close()

                if result == 0:  # Port ouvert
                    logger.debug(f"Port 3456 open on {ip}, checking if ParcInfo...")
                    clients_count = _compter_clients(server_url, timeout)
                    if clients_count is not None:
                        servers[server_url] = {
                            'url': server_url, 'ip': ip, 'clients': clients_count,
                            'nom': ip, 'version': '', 'docker': False,
                        }
                        logger.debug(f"Found ParcInfo at {server_url} with {clients_count} clients")
            except Exception as e:
                logger.debug(f"Error scanning {ip}: {e}")

    return list(servers.values())


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
        self.attente_label = None
        self.dernier_rapport = None
        self.token = ''

        self.root.title(f"ParcInfo System Information Collector v{COLLECTOR_VERSION}")
        self.root.geometry("980x900")
        self.root.resizable(True, True)
        self._appliquer_icone()

        # Couleurs
        self.bg_color = "#f0f0f0"
        self.root.configure(bg=self.bg_color)

        # Charger l'URL sauvegardée si disponible
        self._load_config()

        self._create_widgets()
        # La collecte ne démarre plus toute seule : elle lirait les cases
        # (débit, mots de passe Wi-Fi) dans leur état par défaut avant que
        # l'utilisateur ait pu les cocher, l'obligeant à cocher puis
        # rafraîchir. Le bouton « Rafraîchir les infos » la déclenche
        # explicitement une fois les options choisies.
        # La reconnaissance du client, elle, ne dépend pas de la collecte :
        # _fetch_clients() lit l'adresse MAC directement (immédiat), pas
        # depuis self.system_info (qui ne serait rempli qu'une minute plus tard).
        self._fetch_clients()

    def _appliquer_icone(self):
        """Pose l'icône ParcInfo sur la fenêtre, si elle est disponible.

        Embarquée par PyInstaller sous `static/` ; en exécution depuis les
        sources, elle est à la même place dans le dépôt. Sans elle, Tk laisse
        sa plume par défaut — sans conséquence, d'où l'échec silencieux.
        """
        base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
        icone = base / 'static' / 'icon.ico'
        try:
            if icone.exists():
                self.root.iconbitmap(str(icone))
        except Exception:
            pass

    def _load_config(self):
        """Charge la configuration sauvegardée."""
        try:
            if self.config_file.exists():
                with open(self.config_file, encoding="utf-8") as f:
                    config = json.load(f)
                    saved_url = config.get('server_url')
                    if saved_url:
                        self.server_url = saved_url
                        logger.debug(f"Loaded server URL from config: {saved_url}")
                    self.token = config.get('token', '') or ''
        except Exception as e:
            logger.debug(f"Could not load config: {e}")

    def _save_config(self):
        """Sauvegarde la configuration."""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump({'server_url': self.server_url, 'token': self.token}, f,
                          ensure_ascii=False)
                logger.debug(f"Saved server URL to config: {self.server_url}")
        except Exception as e:
            logger.warning(f"Could not save config: {e}")

    def _create_widgets(self):
        """Crée les widgets de l'interface."""
        # Header
        header = tk.Frame(self.root, bg="#2c3e50", height=46)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        title_label = tk.Label(header, text="ParcInfo - Collecteur d'Informations Système",
                               font=("Arial", 14, "bold"), bg="#2c3e50", fg="white")
        title_label.pack(pady=9)

        # Main content — placé dans la fenêtre en toute fin de méthode, une fois
        # les barres du bas installées : un conteneur extensible posé en premier
        # prend toute la place restante et rejette hors de l'écran ce qui est
        # ajouté après (les boutons se retrouvaient coupés).
        main_frame = ttk.Frame(self.root)

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

        # Jeton : à ne renseigner que si le serveur en exige un. Il est
        # conservé dans la configuration locale pour ne pas le ressaisir à
        # chaque collecte.
        jeton_frame = tk.Frame(server_frame)
        jeton_frame.pack(fill=tk.X, padx=10, pady=(0, 6))
        tk.Label(jeton_frame, text="Jeton (facultatif) :").pack(side=tk.LEFT, padx=5)
        self.token_var = tk.StringVar(value=self.token or '')
        tk.Entry(jeton_frame, textvariable=self.token_var, width=28,
                 show='•').pack(side=tk.LEFT, padx=5)
        tk.Label(jeton_frame, text="requis seulement si le serveur en impose un",
                 font=("Arial", 8), fg="#666").pack(side=tk.LEFT, padx=5)

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

        # Section 2 : Résumé des infos collectées, une rubrique par onglet.
        # Les onglets apparaissent au fur et à mesure de la collecte : elle dure
        # une bonne minute, et un panneau vide pendant tout ce temps donne
        # l'impression que rien ne se passe.
        summary_frame = ttk.LabelFrame(main_frame, text="2. Informations Collectées")
        summary_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        # Onglets sur le côté et non en haut : les rubriques sont nombreuses
        # (une quinzaine), et une barre horizontale les rogne jusqu'à
        # « Points c », « Ident », « Sé »… — illisible. Empilés à gauche, leurs
        # noms tiennent en entier quel qu'en soit le nombre.
        style = ttk.Style()
        try:
            style.configure('Rubriques.TNotebook', tabposition='wn')
            style.configure('Rubriques.TNotebook.Tab', padding=(8, 2), width=22)
            notebook_style = 'Rubriques.TNotebook'
        except tk.TclError:
            notebook_style = 'TNotebook'

        self.notebook = ttk.Notebook(summary_frame, style=notebook_style)
        self.notebook.pack(padx=8, pady=8, fill=tk.BOTH, expand=True)

        self.onglets = {}        # clé de rubrique → {'cadre', 'texte'}
        self.ordre_onglets = []  # ordre canonique, pour insérer au bon endroit

        self.attente_label = tk.Label(
            self.notebook, text="Collecte en cours…",
            font=("Segoe UI", 10), fg="#7f8c8d", bg="white", pady=30)
        self.notebook.add(self.attente_label, text="  …  ")

        # Section 3 : Boutons d'action
        action_frame = tk.Frame(self.root, bg=self.bg_color)

        refresh_btn = ttk.Button(action_frame, text="🔄 Rafraîchir les Infos",
                                 command=self._collect_info)
        refresh_btn.pack(side=tk.LEFT, padx=5)

        self.pdf_btn = ttk.Button(action_frame, text="📄 Ouvrir le rapport PDF",
                                  command=self._ouvrir_pdf, state=tk.DISABLED)
        self.pdf_btn.pack(side=tk.LEFT, padx=5)

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

        # Réseaux Wi-Fi enregistrés : décoché par défaut. Le SSID et le type de
        # sécurité sont envoyés dans tous les cas (comme le reste de la
        # collecte) ; c'est le mot de passe en clair, lui, qui doit rester un
        # choix explicite — même principe que le test de débit ci-dessus, pour
        # un secret plutôt qu'une bande passante.
        self.wifi_passwords_var = tk.BooleanVar(value=False)
        wifi_check = ttk.Checkbutton(
            self.root, variable=self.wifi_passwords_var,
            text="Inclure les mots de passe Wi-Fi enregistrés (stockés chiffrés)")

        # Vérification DNS (dnscheck.tools) : décochée par défaut. Sollicite
        # un service tiers — même principe que le test de débit ci-dessus.
        self.dns_check_var = tk.BooleanVar(value=False)
        dns_check_box = ttk.Checkbutton(
            self.root, variable=self.dns_check_var,
            text="Vérifier la configuration DNS (dnscheck.tools)")

        # Infos box internet (UPnP) : décochée par défaut. Sonde le réseau
        # local plutôt que ce poste — un choix explicite, comme les autres
        # options ci-dessus.
        self.router_info_var = tk.BooleanVar(value=False)
        router_info_box = ttk.Checkbutton(
            self.root, variable=self.router_info_var,
            text="Récupérer les infos de la box internet (UPnP)")

        # Status bar + progression : la collecte dure une bonne minute, une
        # interface figée sans indication passe pour un plantage.
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(self.root, orient=tk.HORIZONTAL,
                                            mode='determinate', maximum=100.0,
                                            variable=self.progress_var)

        self.status_var = tk.StringVar(
            value="Cochez vos options ci-dessous puis cliquez sur « Rafraîchir les infos »")
        status_bar = tk.Label(self.root, textvariable=self.status_var,
                              bg="#ecf0f1", fg="#2c3e50", anchor=tk.W)

        # Les barres du bas réservent leur place avant le conteneur extensible,
        # en partant du bas : elles restent visibles quelle que soit la hauteur
        # de la fenêtre.
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.progress_bar.pack(side=tk.BOTTOM, fill=tk.X)
        router_info_box.pack(side=tk.BOTTOM, anchor=tk.W, padx=12, pady=2)
        dns_check_box.pack(side=tk.BOTTOM, anchor=tk.W, padx=12, pady=2)
        wifi_check.pack(side=tk.BOTTOM, anchor=tk.W, padx=12, pady=2)
        debit_check.pack(side=tk.BOTTOM, anchor=tk.W, padx=12, pady=2)
        action_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=8)
        main_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)

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
            # 'nom'/'version'/'docker' ne viennent que de la découverte mDNS
            # (voir discover_parcinfo_mdns) — absents pour une instance
            # trouvée seulement par balayage de sous-réseau (repli).
            nom = server.get('nom')
            libelle_poste = f"{nom} — " if nom and nom != server.get('ip') else ""
            version = server.get('version')
            libelle_version = f"  v{version}" if version else ""
            libelle_docker = "  🐳 Docker" if server.get('docker') else ""
            btn_text = (f"📍 {libelle_poste}{server['url']}   "
                       f"({server['clients']} clients){libelle_version}{libelle_docker}")
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
        self._lire_jeton()
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
        # Les données changent : le rapport déjà produit ne les décrit plus.
        self.dernier_rapport = None
        self.pdf_btn.config(state=tk.DISABLED)
        self.root.update()

        def avancement(fraction, libelle):
            # La collecte tourne dans un thread : la mise à jour des widgets est
            # renvoyée vers la boucle Tk, seule autorisée à y toucher.
            self.root.after(0, lambda: self._afficher_avancement(fraction, libelle))

        # La valeur de la case est lue ici, dans la boucle Tk : une variable
        # Tkinter ne se lit pas depuis un autre thread.
        test_debit = self.test_debit_var.get()
        dns_check = self.dns_check_var.get()
        router_info = self.router_info_var.get()

        def partiel(donnees):
            # Même règle que pour l'avancement : la collecte tourne dans un
            # thread, seule la boucle Tk a le droit de toucher aux widgets.
            self.root.after(0, lambda d=donnees: self._update_summary(d))

        def collect():
            try:
                self.system_info = collect_system_info(
                    progress=avancement, test_debit=test_debit, on_data=partiel,
                    verifier_dns=dns_check, info_box=router_info)
                self.root.after(0, self._update_summary)
                self.root.after(0, lambda: self.progress_var.set(100.0))
                self.root.after(0, lambda: self.pdf_btn.config(state=tk.NORMAL))
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

    def _lire_jeton(self):
        """Relit le jeton saisi — à appeler depuis la boucle Tk uniquement."""
        try:
            self.token = (self.token_var.get() or '').strip()
        except Exception:
            pass
        return self.token

    def _fetch_clients(self):
        """Récupère la liste des clients."""
        self._lire_jeton()
        self.status_var.set("Récupération de la liste des clients...")
        self.root.update()

        def fetch():
            # Ce thread ne fait que du réseau : Tkinter n'est pas sûr en
            # multithread, toute écriture dans un widget est renvoyée à la
            # boucle principale via after().
            try:
                logger.debug(f"Fetching clients from server: {self.server_url}")
                # L'adresse MAC permet au serveur de reconnaître une machine
                # déjà inventoriée et de désigner son client. Lue directement
                # plutôt que prise dans self.system_info : la collecte
                # complète dure une minute et tourne en parallèle, alors que
                # cette lecture est immédiate. Toutes les cartes sont
                # envoyées, pas une seule choisie au hasard par
                # get_mac_address() : sur une machine à plusieurs adaptateurs
                # (VPN, Hyper-V/WSL, VirtualBox…), rien ne dit laquelle est
                # celle effectivement enregistrée côté serveur.
                macs = get_all_mac_addresses()
                clients, suggestion = fetch_clients(self.server_url, mac_address=macs,
                                                    token=self.token)
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

    # ── Aperçu en onglets ───────────────────────────────────────────────────

    def _creer_onglet(self, section, position):
        """Crée l'onglet d'une rubrique, à sa place dans l'ordre canonique."""
        cadre = tk.Frame(self.notebook, bg="white")
        texte = scrolledtext.ScrolledText(
            cadre, wrap=tk.WORD, font=("Segoe UI", 9), bg="white",
            relief=tk.FLAT, padx=10, pady=8, cursor="arrow")
        texte.pack(fill=tk.BOTH, expand=True)

        # Mise en forme : des étiquettes plutôt qu'une colonne de texte brut.
        texte.tag_configure('bloc', font=("Segoe UI", 9, "bold"), foreground="#2c3e50",
                            spacing1=8, spacing3=3)
        texte.tag_configure('libelle', font=("Segoe UI", 9), foreground="#7f8c8d")
        texte.tag_configure('valeur', font=("Segoe UI", 9, "bold"), foreground="#1a252f")
        texte.tag_configure('element', font=("Segoe UI", 9), foreground="#34495e",
                            lmargin1=14, lmargin2=26, spacing1=1)
        texte.tag_configure('alerte', font=("Segoe UI", 9), foreground="#c0392b",
                            lmargin1=14, lmargin2=26, spacing1=2)
        texte.tag_configure('note', font=("Segoe UI", 8, "italic"), foreground="#b9770e",
                            spacing1=10)
        texte.config(state=tk.DISABLED)

        libelle = "%s %s" % (section['icone'], section['titre'])
        # insert() refuse une position au-delà des onglets existants (« Slave
        # index out of bounds ») : au bout, c'est add() qu'il faut appeler.
        if position >= len(self.notebook.tabs()):
            self.notebook.add(cadre, text=libelle)
        else:
            self.notebook.insert(position, cadre, text=libelle)
        self.onglets[section['cle']] = {'cadre': cadre, 'texte': texte}
        return texte

    def _remplir_onglet(self, texte, section):
        """Réécrit le contenu d'un onglet."""
        # La position de défilement est conservée : sans cela, chaque
        # rafraîchissement pendant la collecte renverrait l'utilisateur en haut
        # de la rubrique qu'il est en train de lire.
        position = texte.yview()
        texte.config(state=tk.NORMAL)
        texte.delete('1.0', tk.END)

        largeur = max([len(l) for l, _ in section['champs']] or [0])
        for libelle, valeur in section['champs']:
            texte.insert(tk.END, '%s ' % libelle.ljust(largeur), 'libelle')
            texte.insert(tk.END, '%s\n' % valeur, 'valeur')

        alerte = section['cle'] == 'alertes'
        for bloc in section['listes']:
            if bloc['titre']:
                texte.insert(tk.END, '\n%s (%d)\n' % (bloc['titre'], len(bloc['elements'])), 'bloc')
            for element in bloc['elements']:
                texte.insert(tk.END, '• %s\n' % element, 'alerte' if alerte else 'element')

        for note in section['notes']:
            texte.insert(tk.END, '\n⚠ %s\n' % note, 'note')

        texte.config(state=tk.DISABLED)
        try:
            texte.yview_moveto(position[0])
        except Exception:
            pass

    def _update_summary(self, partiel=None):
        """Met à jour les onglets — appelé pendant la collecte puis à la fin."""
        info = partiel if partiel is not None else self.system_info
        if not info:
            return
        try:
            sections = build_summary_sections(info)
        except Exception:
            logger.exception("Construction de l'aperçu")
            return

        if sections and self.attente_label is not None:
            self.notebook.forget(self.attente_label)
            self.attente_label = None

        for section in sections:
            if section['cle'] not in self.ordre_onglets:
                self.ordre_onglets.append(section['cle'])
            position = self.ordre_onglets.index(section['cle'])
            entree = self.onglets.get(section['cle'])
            texte = entree['texte'] if entree else self._creer_onglet(section, position)
            self._remplir_onglet(texte, section)

    def _ouvrir_pdf(self):
        """Génère le rapport PDF et l'ouvre avec la visionneuse du système."""
        if not self.system_info:
            messagebox.showinfo("Rapport", "La collecte n'est pas encore terminée.")
            return

        # Le nom du rapport porte un horodatage : régénérer à chaque clic
        # sèmerait des fichiers quasi identiques dans le dossier de travail.
        if self.dernier_rapport and Path(self.dernier_rapport).exists():
            self._afficher_fichier(self.dernier_rapport)
            return

        client_id = self.selected_client.get('id') if self.selected_client else None
        client_name = self.selected_client.get('nom') if self.selected_client else None
        self.status_var.set("Génération du rapport PDF...")

        def travail():
            try:
                _, chemin = generate_pdf_report(self.system_info, client_id, client_name)
                if not chemin:
                    raise RuntimeError("Aucun fichier produit")
                chemin_absolu = str(Path(chemin).resolve())
                self.root.after(0, lambda: self._afficher_fichier(chemin_absolu))
            except Exception as e:
                logger.exception("Génération du rapport")
                self.root.after(0, lambda: messagebox.showerror(
                    "Rapport", "Impossible de générer le rapport :\n%s" % e))
                self.root.after(0, lambda: self.status_var.set("Erreur de génération du rapport"))

        threading.Thread(target=travail, daemon=True).start()

    def _afficher_fichier(self, chemin):
        """Ouvre un fichier avec l'application par défaut du système."""
        self.dernier_rapport = chemin
        try:
            if sys.platform == 'win32':
                os.startfile(chemin)            # noqa: S606 - ouverture par le shell Windows
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', chemin])
            else:
                subprocess.Popen(['xdg-open', chemin])
            self.status_var.set("Rapport ouvert : %s" % chemin)
        except Exception as e:
            # L'ouverture peut échouer (aucune visionneuse associée) : le
            # fichier existe malgré tout, autant en donner le chemin.
            logger.warning("Ouverture du rapport impossible : %s", e)
            messagebox.showinfo("Rapport généré",
                                "Le rapport a été enregistré :\n\n%s" % chemin)
            self.status_var.set("Rapport enregistré : %s" % chemin)

    def _send_data(self):
        """Envoie les données à ParcInfo."""
        if not self.system_info:
            messagebox.showerror("Erreur", "Cliquez d'abord sur « Rafraîchir les infos » "
                                           "pour lancer la collecte.")
            return
        if not self.selected_client:
            messagebox.showerror("Erreur", "Veuillez sélectionner un client")
            return

        self._lire_jeton()
        self._save_config()
        client_id = self.selected_client.get('id')
        client_name = self.selected_client.get('nom', 'Inconnu')
        # Lu ici, dans la boucle Tk : une variable Tkinter ne se lit pas
        # depuis le thread d'envoi (même règle que test_debit dans _collect_info).
        wifi_passwords = self.wifi_passwords_var.get()

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
                    if report_file:
                        # Le bouton « Ouvrir le rapport PDF » rouvrira celui-ci
                        # plutôt que d'en produire un second, identique.
                        self.dernier_rapport = str(Path(report_file).resolve())
                        self.root.after(0, lambda: self.pdf_btn.config(state=tk.NORMAL))
                    else:
                        # Contenu généré mais écriture sur disque impossible
                        # (répertoire non inscriptible) : l'envoi au serveur
                        # reste possible, le contenu est déjà en mémoire —
                        # seul le bouton « Ouvrir » local n'a rien à afficher.
                        logger.warning("PDF report content generated but no file was written")
                else:
                    logger.warning("PDF report generation returned no content")

                success, result = send_to_parcinfo(
                    self.system_info, self.server_url, self.token or None,
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
                        pdf_content, report_file, self.server_url, device_id, client_id,
                        token=self.token or None
                    )
                    if success_report:
                        logger.debug(f"Report uploaded: {result_report}")
                        msg += f"\n✓ Rapport joint enregistré (Doc ID: {result_report.get('document_id')})"
                    else:
                        logger.error(f"Report upload failed: {result_report}")
                        msg += f"\n⚠️ Erreur lors du stockage du rapport: {result_report}"

                # Réseaux Wi-Fi enregistrés : SSID + sécurité systématiquement, le
                # mot de passe seulement si la case est cochée. Appel séparé de
                # l'envoi principal — voir send_wifi_credentials_to_parcinfo.
                if device_id and client_id:
                    wifi_profiles = get_wifi_profiles(inclure_mdp=wifi_passwords)
                    if wifi_profiles:
                        success_wifi, result_wifi = send_wifi_credentials_to_parcinfo(
                            wifi_profiles, self.server_url, device_id, client_id,
                            token=self.token or None
                        )
                        if success_wifi:
                            logger.debug(f"Wifi credentials synced: {result_wifi}")
                            msg += (f"\n✓ Réseaux Wi-Fi : {result_wifi.get('created', 0)} créé(s), "
                                    f"{result_wifi.get('updated', 0)} mis à jour")
                        else:
                            logger.error(f"Wifi credentials sync failed: {result_wifi}")
                            msg += f"\n⚠️ Erreur lors de l'envoi des réseaux Wi-Fi: {result_wifi}"

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


def _commande_relance():
    """Commande (liste d'arguments) pour relancer ce même collecteur.

    En exécutable PyInstaller, `sys.executable` EST déjà le binaire lancé
    (`sys.frozen` vrai) : pas besoin d'y ajouter le script. En `python
    system-info-collector-gui.py`, `sys.executable` est l'interpréteur —
    il faut alors explicitement rappeler le script (`sys.argv[0]`).
    """
    if getattr(sys, 'frozen', False):
        return [sys.executable] + sys.argv[1:]
    return [sys.executable, sys.argv[0]] + sys.argv[1:]


def _relancer_macos_eleve(cmd):
    """Relance ce collecteur avec les droits administrateur via l'invite
    d'authentification macOS standard (la même que Réglages Système ou une
    installation de logiciel) — jamais de ligne de commande à taper.

    Fire-and-forget, volontairement : contrairement à Windows
    (ShellExecuteW, dont le code de retour se lit de façon synchrone et
    fiable), rien ne permet ici de savoir si l'authentification a réussi
    sans risquer d'attendre — `do shell script ... with administrator
    privileges` reste bloqué tant que LE COLLECTEUR ÉLEVÉ tourne, pas
    seulement le temps de l'authentification. Deviner à partir d'un délai
    d'attente aurait pu fermer cette fenêtre-ci sur un faux positif (un
    technicien qui met plus de X secondes à taper son mot de passe, puis
    annule). Cette fenêtre reste donc toujours ouverte : au pire, une fois
    authentifié, le technicien se retrouve avec deux fenêtres au lieu
    d'une — jamais avec aucune.
    """
    commande = ' '.join(shlex.quote(a) for a in cmd)
    script = ('do shell script "%s" with administrator privileges'
              % commande.replace('\\', '\\\\').replace('"', '\\"'))
    subprocess.Popen(['osascript', '-e', script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _proposer_elevation():
    """Si le collecteur tourne sans droits administrateur, demande au
    technicien s'il veut relancer avec — jamais de relance automatique
    sans confirmation explicite.

    Retourne True si CE process-ci doit se terminer (une relance élevée a
    été déclenchée avec succès sous Windows), False s'il doit continuer
    normalement (déjà élevé, refus, ou relance impossible/annulée).
    """
    if is_elevated():
        return False

    manque = ("Certaines informations resteront incomplètes sans les droits "
              "administrateur : usure SMART des disques, TPM, BitLocker, clé "
              "OEM (Windows) ; connexion à distance/SIP (macOS).")

    if sys.platform == 'win32':
        relancer = messagebox.askyesno(
            "Droits administrateur",
            "Cette collecte tourne sans droits administrateur.\n\n%s\n\n"
            "Relancer avec les droits administrateur ?" % manque)
        if not relancer:
            logger.warning("Collecte lancée sans droits administrateur (choix du technicien)")
            return False
        try:
            import ctypes
            cmd = _commande_relance()
            # ShellExecuteW attend l'exécutable et SES arguments séparément
            # (pas une seule ligne de commande comme subprocess) — la mise
            # en forme Windows correcte (guillemets autour des arguments
            # contenant des espaces) est celle de list2cmdline().
            parametres = subprocess.list2cmdline(cmd[1:])
            resultat = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", cmd[0], parametres, None, 1)
            # ShellExecuteW renvoie un entier <= 32 en cas d'échec (UAC
            # annulé par l'utilisateur, ou autre erreur) — jamais
            # d'exception dans ce cas, il faut lire la valeur de retour.
            if resultat > 32:
                return True
            logger.warning("Relance élevée annulée ou impossible (code %s)", resultat)
        except Exception as e:
            logger.warning("Relance élevée impossible : %s", e)
        return False

    # macOS/Linux : pas d'équivalent fiable au "runas" Windows pour relancer
    # un processus GUI entier avec élévation sans risquer de laisser le
    # technicien sans aucune fenêtre ouverte si l'authentification échoue
    # ou est annulée (contrairement à ShellExecuteW, dont le code de retour
    # se lit de façon synchrone et fiable). On affiche donc la commande à
    # lancer soi-même dans un terminal, puis on continue sans élévation —
    # le technicien garde toujours une collecte utilisable, complète ou non.
    relancer = messagebox.askyesno(
        "Droits administrateur",
        "Cette collecte tourne sans droits administrateur.\n\n%s\n\n"
        "Relancer avec les droits administrateur ?" % manque)
    if relancer:
        cmd = _commande_relance()
        if '/AppTranslocation/' in cmd[0]:
            # Gatekeeper « App Translocation » : l'app vient d'être
            # téléchargée et n'a jamais été ouverte/déplacée depuis — macOS
            # l'exécute alors depuis une copie temporaire en lecture seule à
            # un chemin aléatoire (/private/var/folders/.../AppTranslocation/…)
            # au lieu de son vrai emplacement. sys.executable/argv[0]
            # reflète CE chemin temporaire côté process, propre à cette
            # session de lancement : inexploitable pour une relance élevée,
            # que ce soit via une commande sudo ou via osascript. La sortie
            # de translocation ne demande PAS de Terminal, contrairement à
            # ce qu'on pourrait croire : déplacer l'app avec le Finder
            # suffit — macOS ne la relance plus jamais depuis un
            # emplacement temporaire dès qu'elle a été bougée une fois hors
            # de son dossier de téléchargement d'origine.
            messagebox.showinfo(
                "Droits administrateur",
                "macOS a lancé cette copie depuis un emplacement temporaire "
                "en lecture seule (protection Gatekeeper « App "
                "Translocation »), qui s'applique tant que l'app n'a jamais "
                "été déplacée depuis son téléchargement.\n\n"
                "Pas besoin de Terminal pour y remédier :\n"
                "1. Quittez cette application.\n"
                "2. Dans le Finder, faites glisser ParcInfo-Collector.app "
                "vers un autre dossier (par ex. Applications, ou même "
                "juste le Bureau) — le simple fait de le déplacer suffit, "
                "aucune commande à taper.\n"
                "3. Relancez l'application depuis son nouvel emplacement : "
                "la proposition d'élévation fonctionnera normalement.\n\n"
                "(Pour qui préfère malgré tout le Terminal : `xattr -cr "
                "<chemin de l'app>` a le même effet.)\n\n"
                "La collecte actuelle continue sans élévation en attendant.")
        else:
            try:
                _relancer_macos_eleve(cmd)
                messagebox.showinfo(
                    "Droits administrateur",
                    "Une invite d'authentification macOS va s'ouvrir (la même "
                    "que pour Réglages Système ou l'installation d'un "
                    "logiciel) — pas de ligne de commande à taper.\n\n"
                    "Une fois authentifié·e, une nouvelle fenêtre du "
                    "collecteur démarre avec les droits administrateur.\n\n"
                    "Cette fenêtre-ci continue sans élévation en attendant : "
                    "fermez-la une fois la nouvelle ouverte, ou gardez les "
                    "deux.")
            except Exception as e:
                logger.warning("Relance élevée macOS impossible : %s", e)
                commande = ' '.join(shlex.quote(a) for a in cmd)
                messagebox.showinfo(
                    "Droits administrateur",
                    "Impossible de proposer l'authentification automatique "
                    "(%s).\n\n"
                    "Pour une collecte complète, ouvrez un Terminal et "
                    "lancez :\n\n"
                    "sudo %s\n\n"
                    "La collecte actuelle continue sans élévation en "
                    "attendant." % (e, commande))
    else:
        logger.warning("Collecte lancée sans droits administrateur (choix du technicien)")
    return False


def main():
    parser = argparse.ArgumentParser(
        description='Collecteur d\'informations système avec interface graphique'
    )
    parser.add_argument('--server', default='http://parcinfo.local:3456',
                        help='URL du serveur ParcInfo (défaut: http://parcinfo.local:3456)')

    args = parser.parse_args()

    # La proposition d'élévation a besoin d'une racine Tk pour ses boîtes de
    # dialogue, mais pas encore de la fenêtre principale — cachée le temps
    # de la décision, pour ne pas afficher un collecteur à moitié construit
    # derrière la boîte de dialogue.
    root = tk.Tk()
    root.withdraw()
    if _proposer_elevation():
        root.destroy()
        return
    root.deiconify()

    CollectorGUI(root, args.server)
    root.mainloop()


if __name__ == '__main__':
    main()
