# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for system-info-collector CLI
Generates standalone executable for Windows, macOS, Linux
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = ['json', 'urllib', 'socket', 'subprocess', 'platform', 'uuid', 'ctypes']
if sys.platform == 'win32':
    hiddenimports += ['winreg']
hiddenimports += collect_submodules('reportlab')

a = Analysis(
    ['system-info-collector.py'],
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
    name='system-info-collector',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# macOS app bundle
if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='system-info-collector.app',
        icon=None,
        bundle_identifier='com.parcinfo.collector-cli',
        info_plist={
            'NSPrincipalClass': 'NSApplication',
            'NSHighResolutionCapable': 'True',
        },
    )
