"""
ParcInfo — launcher.py
Point d'entrée PyInstaller : port libre, navigateur auto, pas de console.
"""
import sys, os, shutil, threading, time, socket, webbrowser, logging, platform, subprocess

# ── Résolution des chemins ────────────────────────────────────────────────────
def res(relative=''):
    """Ressources embarquées (templates, static, oui.txt…)."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative) if relative else base

def _ancien_dossier_donnees_macos():
    """Emplacement (dangereux) utilisé avant la correction : à côté de l'exe,
    c'est-à-dire DANS le bundle .app — voir data() ci-dessous."""
    return os.path.dirname(sys.executable)

def _dossier_application_support_macos():
    """~/Library/Application Support/ParcInfo — surchargeable par
    PARCINFO_MACOS_DATA_DIR (tests uniquement, pour ne jamais écrire dans le
    vrai dossier utilisateur pendant la suite)."""
    return os.environ.get('PARCINFO_MACOS_DATA_DIR') or os.path.join(
        os.path.expanduser('~'), 'Library', 'Application Support', 'ParcInfo')

def data(relative=''):
    """Données persistantes (DB, uploads, secret.key) — à côté de l'exe sur
    Windows/Linux, mais dans ~/Library/Application Support sur macOS.

    Sur Windows, l'exe est un fichier unique dans son propre dossier : le
    stocker à côté est sûr, une mise à jour ne remplace que ce fichier
    (applique_maj.py). Sur macOS, « à côté de l'exe » veut dire dans
    Contents/MacOS/ — À L'INTÉRIEUR du bundle .app — que
    update_checker.py._install_macos() remplace ENTIÈREMENT à chaque mise à
    jour (ancien bundle déplacé puis supprimé). Y stocker la BD, les uploads
    et la clé de chiffrement les condamnait à disparaître à la première mise
    à jour, sans aucun moyen de les récupérer.
    """
    if getattr(sys, 'frozen', False):
        if platform.system() == 'Darwin':
            base = _dossier_application_support_macos()
            os.makedirs(base, exist_ok=True)
        else:
            base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative) if relative else base

def _migrer_donnees_macos_si_besoin(logger):
    """Rattrape les installations qui ont tourné avant ce correctif : si des
    données existent encore dans l'ancien emplacement (dans le bundle .app)
    et qu'aucune n'existe déjà dans le nouveau, les déplacer plutôt que les
    laisser orphelines dans un bundle promis à disparaître à la prochaine
    mise à jour.
    """
    if not (getattr(sys, 'frozen', False) and platform.system() == 'Darwin'):
        return
    ancien = _ancien_dossier_donnees_macos()
    nouveau = data()
    if ancien == nouveau:
        return
    ancienne_db = os.path.join(ancien, 'parc_info.db')
    nouvelle_db = os.path.join(nouveau, 'parc_info.db')
    if not os.path.exists(ancienne_db) or os.path.exists(nouvelle_db):
        return
    logger.info("Migration des données depuis l'ancien emplacement (%s) vers %s",
                ancien, nouveau)
    for nom in ('parc_info.db', 'secret.key', 'uploads', 'backups'):
        src = os.path.join(ancien, nom)
        dst = os.path.join(nouveau, nom)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                shutil.move(src, dst)
            except OSError as e:
                logger.error("Migration de %s impossible : %s", nom, e)

# ── Certificats CA pour les connexions HTTPS (Turso, vérification de mise à
#    jour) ────────────────────────────────────────────────────────────────────
# Un `pip install` normal trouve ses certificats via le magasin système ou le
# script « Install Certificates.command » du Python.org installé — rien de
# tel n'existe dans un exécutable PyInstaller sur la machine de l'utilisateur.
# Sans ça, tout http.client.HTTPSConnection/urlopen non explicite échoue avec
# CERTIFICATE_VERIFY_FAILED (constaté sur macOS ; Windows/Linux s'en sortent
# via leur propre magasin de certs, mais autant fixer le chemin partout).
# SSL_CERT_FILE est lu par ssl.get_default_verify_paths() pour TOUT usage du
# contexte SSL par défaut du process, donc réglé une fois ici, avant tout
# import réseau — pas besoin de toucher chaque appelant individuellement.
# Affectation directe et non setdefault() : après une mise à jour, le nouveau
# process est lancé avec l'environnement de l'ancien (voir applique_maj.
# environnement_propre — SSL_CERT_FILE n'y est pas un « repère du lanceur »
# à filtrer, donc il passe tel quel). Il pointait alors vers le cacert.pem de
# l'ANCIEN _MEIPASS, un dossier temporaire supprimé dès la sortie de l'ancien
# process — d'où un CERTIFICATE_VERIFY_FAILED (« unable to get local issuer
# certificate ») sur la synchronisation Turso juste après une mise à jour
# macOS, constaté en usage réel. Le chemin recalculé ici, propre à CE
# process, doit toujours l'emporter sur une valeur héritée.
if getattr(sys, 'frozen', False):
    _cacert = res('cacert.pem')
    if os.path.exists(_cacert):
        os.environ['SSL_CERT_FILE'] = _cacert
else:
    try:
        import certifi
        os.environ['SSL_CERT_FILE'] = certifi.where()
    except ImportError:
        pass

# ── Port libre ────────────────────────────────────────────────────────────────
def get_port(preferred=3456):
    """Essaie le port préféré (3456), sinon un port libre.

    PARCINFO_RELANCE_MAJ (posé par la relance après mise à jour, voir
    update_checker._install_macos et applique_maj._relancer) : l'ancienne
    instance garde volontairement le port préféré le temps de sa propre
    vérification avant de s'arrêter (jusqu'à ~12 s, voir _install_macos) —
    sans patience ici, la nouvelle version basculait aussitôt sur un port au
    hasard et y restait bloquée pour le reste de son exécution, l'ancienne
    n'étant plus là ensuite pour le libérer une seconde fois. Signalé en
    usage réel (macOS Intel) ; posé aussi côté Windows depuis que la relance
    de mise à jour ne rouvre plus systématiquement un onglet de navigateur
    (PARCINFO_APRES_MAJ, voir main() plus bas) — sans ce filet, un
    changement de port entre les deux instances laisserait l'onglet resté
    ouvert incapable de jamais retrouver le nouveau serveur. Un lancement
    normal, lui, bascule tout de suite comme avant — utile quand plusieurs
    instances légitimes (Docker/PC/Mac) tournent en parallèle et se
    partagent volontairement des ports distincts.
    """
    tentatives = 30 if os.environ.get('PARCINFO_RELANCE_MAJ') else 1
    for i in range(tentatives):
        try:
            with socket.socket() as s:
                s.bind(('127.0.0.1', preferred))
                s.close()
            return preferred
        except OSError:
            if i < tentatives - 1:
                time.sleep(0.5)
    # Port préféré indisponible (ou toujours occupé après patience) : port libre
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

# ── Arrêt / relance (utilisés par la barre système ET par la page /apropos) ────
def quitter_application(logger=None):
    """Arrête proprement le processus.

    Un léger délai laisse le temps à l'appelant HTTP (route /apropos/quitter)
    de recevoir sa réponse avant que le process ne meure — sinon le navigateur
    voit la connexion coupée sans confirmation. Le menu de la barre système,
    lui, n'a pas ce besoin (pas de requête en cours) mais réutilise la même
    fonction pour rester cohérent.
    """
    if logger:
        logger.info("Arrêt de ParcInfo demandé")
    threading.Timer(0.6, lambda: os._exit(0)).start()

def redemarrer_application(logger=None):
    """Relance un nouvel exemplaire de l'exécutable, puis quitte celui-ci.

    macOS : `open` sur le bundle .app plutôt qu'invoquer le binaire interne
    directement — sans ça, l'app relancée perd son rattachement au Dock et à
    Launch Services (elle tournerait comme un simple process Unix nu).
    """
    def _relancer():
        time.sleep(0.6)
        try:
            if platform.system() == 'Darwin':
                bundle = sys.executable
                for _ in range(4):
                    parent = os.path.dirname(bundle)
                    if parent == bundle:
                        bundle = None
                        break
                    if parent.endswith('.app'):
                        bundle = parent
                        break
                    bundle = parent
                if bundle:
                    subprocess.Popen(['open', bundle])
                else:
                    subprocess.Popen([sys.executable])
            else:
                subprocess.Popen(
                    [sys.executable], cwd=os.path.dirname(sys.executable) or None,
                    close_fds=True, creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0))
            if logger:
                logger.info("ParcInfo relancé")
        except Exception as e:
            if logger:
                logger.error("Relance impossible : %s", e)
        os._exit(0)
    threading.Thread(target=_relancer, daemon=True).start()

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
        try:
            # Le .ico porte un dessin propre à chaque taille ; la barre système
            # affiche petit, où un simple redimensionnement du 256 s'efface.
            with Image.open(res('static/icon.ico')) as ico:
                ico.size = (32, 32)
                img = ico.convert('RGBA')
        except Exception:
            img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse([2, 2, 62, 62], fill='#0a1628')
            d.ellipse([6, 6, 58, 58], fill='#00c9ff')
            d.text((14, 16), 'PI', fill='white')
        Icon('ParcInfo', img, 'ParcInfo', menu=Menu(
            MenuItem('Ouvrir ParcInfo',  lambda i, it: webbrowser.open(url), default=True),
            MenuItem('À propos',         lambda i, it: webbrowser.open(url + '/apropos')),
            MenuItem('Redémarrer',       lambda i, it: (i.stop(), redemarrer_application(logger))),
            MenuItem('Quitter',          lambda i, it: (i.stop(), quitter_application(logger))),
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
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=5, creationflags=no_window
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
    # L'exécutable neuf, téléchargé par la version précédente, est relancé avec
    # ce drapeau pour se recopier sur elle. Ce cas se traite avant tout le
    # reste : rien de l'application n'a à démarrer pour remplacer un fichier.
    import applique_maj
    arguments = applique_maj.mode_mise_a_jour()
    if arguments is not None:
        sys.exit(applique_maj.appliquer(arguments))

    port = get_port(preferred=3456)
    url  = f'http://127.0.0.1:{port}'

    # Configure logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    logger = logging.getLogger('parcinfo')
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    _migrer_donnees_macos_si_besoin(logger)

    # Préparer les chemins de données AVANT d'importer app
    db_path      = data('parc_info.db')
    uploads_path = data('uploads')
    os.makedirs(uploads_path, exist_ok=True)

    # secret.key et BACKUP_DIR (app.py) ne passent pas par db_init_paths()
    # ci-dessous — ils lisent directement DATA_DIR au moment de l'import.
    # Sans ça, sur macOS, ils resteraient dans le bundle .app malgré la
    # correction de data() ci-dessus (voir son docstring).
    if getattr(sys, 'frozen', False) and platform.system() == 'Darwin':
        os.environ.setdefault('DATA_DIR', data())

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

    # Ouvrir le navigateur après démarrage de Flask — sauf s'il s'agit d'une
    # relance de mise à jour (PARCINFO_APRES_MAJ, posé par applique_maj._relancer
    # et update_checker._install_macos) : l'onglet resté ouvert sur la bannière
    # de mise à jour se recharge alors de lui-même une fois ce nouveau serveur
    # prêt (voir static/js/update_notifier.js) — ouvrir un second onglet en plus
    # ne ferait que laisser le premier, périmé, traîner sans être fermé.
    if not os.environ.get('PARCINFO_APRES_MAJ'):
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

    # Synchronisation des documents joints (local ↔ Turso, si configuré)
    # Ces sous-systèmes ne démarrent normalement que dans le bloc __main__ de
    # app.py, qui ne s'exécute jamais quand app est importé comme un module ici
    # (cause du bug "les documents joints ne se synchronisent pas" sur l'exe)
    try:
        from uploads_sync import start_sync_thread
        start_sync_thread(interval=60)
    except Exception as e:
        logger.warning(f"Uploads sync thread failed to start (non-critical): {e}")

    # Préchargement de la base OUI (fabricants réseau, utilisé par le scan réseau)
    threading.Thread(target=flask_app._oui_load_full, daemon=True).start()

    # Scheduler cron (régénération des occurrences de maintenance à 02:00,
    # notifications de maintenance à venir à 08:00)
    try:
        flask_app.scheduler.add_job(flask_app._regenerate_all_maintenance_occurrences, 'cron', hour=2, minute=0)
        flask_app.scheduler.add_job(flask_app._notify_upcoming_maintenances, 'cron', hour=8, minute=0)
        try:
            import network_diag as _nd
            _dc = _nd.parse_rapport_cron(flask_app.cfg_get('diag_rapport_cron', ''))
            if _dc:
                flask_app.scheduler.add_job(_nd.tache_rapport_planifie, 'cron', **_dc)
        except Exception:
            pass
        flask_app.scheduler.start()
        logger.info("Cron scheduler démarré (régénération à 02:00, notifications à 08:00)")
    except Exception as e:
        logger.warning(f"Cron scheduler failed to start (non-critical): {e}")

    # Recherche d'une mise à jour, sans rien installer : l'utilisateur décide
    # depuis la bannière de l'interface. L'ancienne version téléchargeait et
    # remplaçait l'exécutable au démarrage sans le moindre message — d'où des
    # redémarrages inexpliqués, et aucune trace quand ça échouait.
    try:
        from update_notifier import get_notifier
        # La vérification part dans son propre fil : le démarrage n'attend pas
        # le réseau, et le résultat remonte dans la bannière dès qu'il arrive.
        get_notifier(config_dir=str(data()))
        logger.info("Recherche de mise à jour lancée en arrière-plan")
    except Exception as e:
        logger.warning("Recherche de mise à jour impossible (sans conséquence) : %s", e)

    # Démarrer Flask (0.0.0.0 : accessible depuis le réseau local, pas seulement en local)
    flask_app.app.run(host='0.0.0.0', port=port,
                      debug=False, use_reloader=False, threaded=True)

if __name__ == '__main__':
    main()
