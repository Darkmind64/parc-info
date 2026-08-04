# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for system-info-collector GUI
Generates standalone executable for Windows, macOS, Linux
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = [
    'tkinter',
    'json',
    'urllib',
    'socket',
    'subprocess',
    'platform',
    'uuid',
    'threading',
    'ctypes',
    'logging',
    'logging.handlers',
]
if sys.platform == 'win32':
    hiddenimports += ['winreg']
hiddenimports += collect_submodules('reportlab')

a = Analysis(
    ['system-info-collector-gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludedimports=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='system-info-collector-gui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI app - no console
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# macOS app bundle
if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='ParcInfo-Collector.app',
        icon=None,
        bundle_identifier='com.parcinfo.collector-gui',
        info_plist={
            'NSPrincipalClass': 'NSApplication',
            'NSHighResolutionCapable': 'True',
            'CFBundleShortVersionString': '2.6.24',
            'CFBundleVersion': '2.6.24',
        },
    )
