"""
ParcInfo — launcher.py
Point d'entrée PyInstaller : port libre, navigateur auto, pas de console.
"""
import sys, os, threading, time, socket, webbrowser, logging, platform, subprocess

# ── Résolution des chemins ────────────────────────────────────────────────────
def res(relative=''):
    """Ressources embarquées (templates, static, oui.txt…)."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative) if relative else base

def data(relative=''):
    """Données persistantes (DB, uploads) — à côté de l'exe."""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative) if relative else base

# ── Port libre ────────────────────────────────────────────────────────────────
def get_port(preferred=3456):
    """Essaie le port préféré (3456), sinon un port libre."""
    try:
        with socket.socket() as s:
            s.bind(('127.0.0.1', preferred))
            s.close()
        return preferred
    except OSError:
        # Port préféré occupé, utiliser un port libre
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]

# ── Systray optionnel ─────────────────────────────────────────────────────────
def run_systray(url, logger):
    """Essaie de créer la barre système. Échoue silencieusement si indisponible."""
    # Systray non supporté sur macOS (problèmes de compatibilité AppKit)
    if platform.system() == 'Darwin':
        logger.info("Systray disabled on macOS (AppKit compatibility)")
        return

    try:
        from pystray import Icon, MenuItem, Menu
        from PIL import Image, ImageDraw
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([2, 2, 62, 62], fill='#0a1628')
        d.ellipse([6, 6, 58, 58], fill='#00c9ff')
        d.text((14, 16), 'PI', fill='white')
        Icon('ParcInfo', img, 'ParcInfo', menu=Menu(
            MenuItem('Ouvrir ParcInfo', lambda i, it: webbrowser.open(url), default=True),
            MenuItem('Quitter',         lambda i, it: (i.stop(), os._exit(0))),
        )).run()
    except Exception as e:
        logger.debug(f"Systray unavailable: {e}")

# ── Pare-feu Windows (ouverture automatique du port) ───────────────────────────
def ensure_firewall_rule(port, logger):
    """Crée une règle de pare-feu Windows entrante pour le port ParcInfo.

    La modification du pare-feu nécessite des privilèges administrateur - on
    déclenche donc l'ajout via un processus netsh élevé (une seule invite UAC).
    Si la règle existe déjà pour ce port (cas normal après le 1er lancement),
    ou si l'utilisateur refuse l'élévation, on continue silencieusement sans
    bloquer le démarrage : l'application reste utilisable en local dans tous
    les cas, seul l'accès réseau externe dépend de cette règle.
    """
    if platform.system() != 'Windows':
        return

    no_window = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
    rule_name = f'ParcInfo-{port}'

    try:
        check = subprocess.run(
            ['netsh', 'advfirewall', 'firewall', 'show', 'rule', f'name={rule_name}'],
            capture_output=True, text=True, timeout=5, creationflags=no_window
        )
        if check.returncode == 0 and 'No rules match' not in check.stdout:
            logger.debug(f"Firewall rule already present for port {port}")
            return
    except Exception as e:
        logger.debug(f"Firewall rule check failed (non-critical): {e}")
        return

    try:
        import ctypes
        params = (
            f'advfirewall firewall add rule name="{rule_name}" dir=in action=allow '
            f'protocol=TCP localport={port}'
        )
        # ShellExecuteW + verbe "runas" : une seule invite UAC pour netsh.exe,
        # sans élever le processus ParcInfo lui-même
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh.exe", params, None, 0)
        if ret > 32:
            logger.info(f"Demande d'ouverture du port {port} dans le pare-feu Windows envoyée (confirmation UAC requise)")
        else:
            logger.debug(f"Firewall elevation request returned code {ret}")
    except Exception as e:
        logger.debug(f"Firewall rule setup failed (non-critical): {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    port = get_port(preferred=3456)
    url  = f'http://127.0.0.1:{port}'

    # Configure logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    logger = logging.getLogger('parcinfo')
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    # Préparer les chemins de données AVANT d'importer app
    db_path      = data('parc_info.db')
    uploads_path = data('uploads')
    os.makedirs(uploads_path, exist_ok=True)

    # Ajouter le dossier ressources au path Python
    sys.path.insert(0, res())

    # Patcher les variables de module AVANT import (évite le DB_PATH figé)
    # On injecte directement dans le module via builtins trick
    import builtins as _bi
    _bi._PARCINFO_DB      = db_path
    _bi._PARCINFO_UPLOADS = uploads_path
    _bi._PARCINFO_RES     = res()

    # Importer app — à ce stade DATABASE n'est pas encore défini
    import app as flask_app
    from database import init_paths as db_init_paths

    # Initialiser les chemins de façon centralisée (robuste et non-fragile)
    db_init_paths(db_path, uploads_path)

    # Surcharger les dossiers de l'app Flask si frozen
    if getattr(sys, 'frozen', False):
        flask_app.app.template_folder = res('templates')
        flask_app.app.static_folder   = res('static')

    # Initialiser la DB (utilise maintenant les chemins inicializados)
    flask_app.init_db()

    # Ouvrir le navigateur après démarrage de Flask
    def open_browser():
        time.sleep(1.8)
        webbrowser.open(url)
    threading.Thread(target=open_browser, daemon=True).start()

    # Systray (optionnel — désactivé sur macOS)
    threading.Thread(target=run_systray, args=(url, logger), daemon=True).start()

    # mDNS (accès via http://parcinfo.local:<port> depuis le réseau local)
    threading.Thread(target=lambda: flask_app._register_mdns(port), daemon=True).start()

    # Pare-feu Windows : ouvre automatiquement le port pour l'accès réseau
    # (best-effort, ne bloque jamais le démarrage - voir ensure_firewall_rule())
    threading.Thread(target=ensure_firewall_rule, args=(port, logger), daemon=True).start()

    # Auto-update au démarrage (bloquant, très rapide si pas d'update)
    logger.info("Checking for updates...")
    try:
        from update_checker import UpdateChecker
        checker = UpdateChecker(config_dir=data())

        # Check and install if update available
        if checker.check_and_install_updates(force=True, silent=True):
            # Update was installed, app will be restarted by installer
            logger.info("Update installed, restarting...")
            time.sleep(2)  # Give installer time to close our process
            sys.exit(0)
        else:
            logger.info("No update needed")
    except Exception as e:
        logger.warning(f"Update check failed (non-critical): {e}")
        # Continue anyway, update failure shouldn't block app startup

    # Démarrer Flask (0.0.0.0 : accessible depuis le réseau local, pas seulement en local)
    flask_app.app.run(host='0.0.0.0', port=port,
                      debug=False, use_reloader=False, threaded=True)

if __name__ == '__main__':
    main()
