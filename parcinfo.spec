# -*- mode: python ; coding: utf-8 -*-
"""
ParcInfo PyInstaller Spec
Génère exécutable portable (Windows .exe / macOS .app)

Usage:
  pyinstaller parcinfo.spec

Sortie:
  dist/ParcInfo.exe        (Windows, ~25-40 MB)
  dist/ParcInfo.app        (macOS, ~25-40 MB)

Données:
  Embarquées : templates/, static/, oui.txt (optionnel)
  Externes : parc_info.db, uploads/, secret.key (crées au 1er run)

Entry point: launcher.py (détecte port libre, ouvre navigateur)
"""
import sys, os

block_cipher = None

# Permet de surcharger l'architecture cible via variable d'environnement.
# Ex. : TARGET_ARCH=universal2 pyinstaller parcinfo.spec
_target_arch = os.environ.get('TARGET_ARCH') or None

# ── Ressources à embarquer (pas la BD) ────────────────────────────────────────

datas = [
    ('templates', 'templates'),    # Templates Jinja2 (25+ fichiers)
    ('static',    'static'),       # JS, CSS, images
]

# Optionnel : base fabricants réseau (60k MACs)
if os.path.exists('oui.txt'):
    datas.append(('oui.txt', '.'))

# ── Imports cachés (modules non auto-détectés par PyInstaller) ────────────────

hiddenimports = [
    # Flask + Jinja2
    'flask', 'flask.templating', 'flask.json', 'flask.helpers',
    'jinja2', 'jinja2.ext', 'jinja2.utils',

    # Werkzeug (HTTP, sécurité)
    'werkzeug', 'werkzeug.serving', 'werkzeug.routing', 'werkzeug.security',
    'werkzeug.middleware.shared_data', 'werkzeug.middleware.proxy_fix',

    # Dépendances Flask
    'click', 'itsdangerous', 'markupsafe',

    # Base de données
    'sqlite3', '_sqlite3',

    # Cryptographie
    'hashlib', 'secrets',

    # Réseau
    'ipaddress', 'socket', 'subprocess',

    # Threading + async
    'concurrent.futures', 'threading',

    # Utilitaires
    'webbrowser', 'json', 'zipfile', 'io', 'struct', 'time', 'logging',

    # Optional : pystray (system tray icon)
    'pystray', 'PIL', 'PIL.Image', 'PIL.ImageDraw',
]

# ── Analyse dépendances ──────────────────────────────────────────────────────

a = Analysis(
    ['launcher.py'],                    # Entry point
    pathex=['.'],
    binaries=[],                        # Libs C (sqlite3 inclus)
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Développement / tests
        'tkinter', 'unittest', 'doctest', 'pdb',
        'lib2to3', 'setuptools', 'pip', 'wheel',

        # Réseau / FTP obsolètes
        'xmlrpc', 'ftplib', 'telnetlib', 'imaplib', 'poplib', 'smtpd',

        # Turtle graphics, terminal UI
        'turtle', 'curses', 'test',
    ],
    cipher=block_cipher,
    noarchive=False,
)

# ── Création archive Python ──────────────────────────────────────────────────

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ── Exécutable unique ─────────────────────────────────────────────────────────

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ParcInfo',                        # Nom de l'exécutable
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                              # Désactiver UPX (réduit faux positifs antivirus)
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                          # ← IMPORTANT : pas de console Windows
    disable_windowed_traceback=False,       # Logs en fichier
    argv_emulation=False,                   # macOS : ne pas émuler argv
    target_arch=_target_arch,
    codesign_identity=None,
    entitlements_file=None,
    manifest='app.manifest',                # Manifest de confiance Windows
    icon='static/icon.ico',                 # Icône de l'application
)

# ── Bundle macOS .app ─────────────────────────────────────────────────────────
# Crée structure macOS native (ParcInfo.app)

if sys.platform == 'darwin':
    BUNDLE(
        exe,
        name='ParcInfo.app',
        icon=None,                          # 'static/icon.icns' pour custom icon
        bundle_identifier='fr.parcinfo.app',
        info_plist={
            'CFBundleName':                 'ParcInfo',
            'CFBundleDisplayName':          'ParcInfo',
            'CFBundleVersion':              '1.0.0',
            'CFBundleShortVersionString':   '1.0.0',
            'NSHighResolutionCapable':      True,      # Retina support
            'LSBackgroundOnly':             False,     # App visible
            'NSRequiresAquaSystemAppearance': False,   # Dark mode support
            'NSPrincipalClass':             'NSApplication',
        },
    )
