# -*- mode: python ; coding: utf-8 -*-
"""
ParcInfo Installer Spec
Compiles installer.py into standalone installer.exe

Usage:
  pyinstaller installer.spec

Output:
  dist/installer.exe (~15 MB)
"""

import sys
import os

block_cipher = None

a = Analysis(
    ['installer.py'],
    pathex=['.'],
    binaries=[],
    datas=[('dist/ParcInfo.exe', '.')],  # Embed ParcInfo.exe inside installer
    hiddenimports=[
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'win32com.client',  # Optional: Windows COM
        'winreg',           # Windows registry
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'numpy',
        'pandas',
        'django',
        'flask',
    ],
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
    name='installer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='static/icon.ico',  # Use app icon
)
