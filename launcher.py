"""
ParcInfo — launcher.py
Point d'entrée PyInstaller : port libre, navigateur auto, pas de console.
"""
import sys, os, threading, time, socket, webbrowser, logging

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
def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

# ── Systray optionnel ─────────────────────────────────────────────────────────
def run_systray(url):
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
    except Exception:
        pass

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    port = free_port()
    url  = f'http://127.0.0.1:{port}'

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

    # Systray (optionnel)
    threading.Thread(target=run_systray, args=(url,), daemon=True).start()

    # Démarrer Flask
    flask_app.app.run(host='127.0.0.1', port=port,
                      debug=False, use_reloader=False, threaded=True)

if __name__ == '__main__':
    main()
