# ParcInfo Installation & Build Guide

Professional installer system for ParcInfo with auto-update, system dependency checking, and native integration.

**Table of Contents**
- [For End Users](#for-end-users)
- [For Developers](#for-developers)
- [Technical Details](#technical-details)

---

## For End Users

### Windows Installation

1. **Download** installer from GitHub Releases: `ParcInfo-Setup-2.5.0.exe`

2. **Run** the installer
   - Right-click → "Run as Administrator" (optional, for all-users installation)
   - Follow the wizard

3. **System Requirements Check**
   - Installer verifies Python 3.8+
   - Visual C++ Runtime (optional)
   - 100+ MB free disk space

4. **After Installation**
   - Shortcut created on Desktop
   - Start Menu folder: `ParcInfo → ParcInfo`
   - Application launches automatically
   - Database created on first run (`parc_info.db`)

5. **Uninstall**
   - Control Panel → Programs → Programs and Features
   - Find "ParcInfo" → Uninstall
   - OR: Right-click Start Menu shortcut → Uninstall

### macOS Installation

1. **Download** installer from GitHub Releases: `ParcInfo-2.5.0.dmg` (Intel or ARM64)

2. **Mount** the DMG
   - Double-click `ParcInfo-2.5.0.dmg`
   - Drag `ParcInfo.app` to `Applications` folder

3. **Launch**
   - Open Applications folder
   - Double-click `ParcInfo`
   - If blocked: Right-click → Open → "Open anyway"
   - OR: Terminal → `xattr -cr /Applications/ParcInfo.app && open /Applications/ParcInfo.app`

4. **System Requirements**
   - macOS 10.13+
   - Python 3.8+ (bundled with app)
   - 200+ MB free disk space
   - Xcode CLI tools (optional, for advanced features)

5. **Uninstall**
   - Drag `ParcInfo.app` to Trash
   - Empty Trash
   - Data stored in `~/.parcinfo/` (optional: manually delete)

### Auto-Update

**Every month**, ParcInfo checks for updates:

1. **Notification** appears if new version available
2. **Click "Update"** to download and install
3. **Auto-restart** (application will restart after installation)
4. **Data preserved** (database and uploads untouched)

To **disable** auto-update:
- In menu: Settings → Advanced → Auto-update → Disable

To **check manually**:
- In menu: Help → Check for Updates

---

## For Developers

### Prerequisites

**Windows:**
```bash
# Install build tools
pip install pyinstaller pillow

# Install NSIS (free)
# https://nsis.sourceforge.io/Download
# Add to PATH: C:\Program Files (x86)\NSIS
```

**macOS:**
```bash
# Install build tools
pip install pyinstaller pillow create-dmg

# Ensure Xcode CLI tools
xcode-select --install
```

**Linux:**
```bash
# Install build tools
pip install pyinstaller pillow
```

### Building Installers

#### Quick Start (Current Platform)

```bash
# Build for your current OS
python build/build_installers.py --version 2.5.0

# Build Windows only
python build/build_installers.py --version 2.5.0 --windows-only

# Build macOS only
python build/build_installers.py --version 2.5.0 --macos-only
```

#### Windows (from Windows Command Prompt, Admin)

**Using PowerShell:**
```powershell
cd installers/windows
.\build_installer.ps1 -Version "2.5.0" -Clean -Verbose
```

**Using Python:**
```bash
cd build
python build_installers.py --version 2.5.0 --windows-only
```

Output: `dist/ParcInfo-Setup-2.5.0.exe`

#### macOS (from Terminal)

```bash
python build/build_installers.py --version 2.5.0 --macos-only
```

Output: `dist/ParcInfo-2.5.0.dmg`

#### Creating Assets (Images, Icons)

```bash
cd installers/windows
python create_assets.py  # Creates banner.bmp, icon.ico, background.png
```

**Custom branding:**
- Replace `installers/windows/files/banner.bmp` (150×57, NSIS wizard)
- Replace `installers/windows/files/icon.ico` (256×256, app icon)
- Replace `installers/macos/resources/background.png` (604×316, DMG)

### Version Management

**Automatic sync via `__version__.py`:**

```python
from __version__ import __version__, APP_NAME, GITHUB_REPO
print(f"{APP_NAME} v{__version__}")  # "ParcInfo v2.5.0"
```

**Update workflow:**

1. **Edit `CHANGELOG.md`** — add release notes
2. **Update `__version__.py`** — change `__version__ = "X.Y.Z"`
3. **Commit & Push**
   ```bash
   git add __version__.py CHANGELOG.md
   git commit -m "Release v2.5.0"
   ```
4. **Create Git tag**
   ```bash
   git tag v2.5.0
   git push origin v2.5.0
   ```
5. **GitHub Actions** automatically:
   - Compiles PyInstaller executables
   - Builds installers (NSIS + DMG)
   - Generates `version.json` with checksums
   - Creates GitHub Release with all assets

### Manual Testing

**Test Windows installer:**
```bash
# Build
python build/build_installers.py --version 2.5.0 --windows-only

# Install
dist/ParcInfo-Setup-2.5.0.exe

# Verify
# - App launches
# - Database created: C:\Program Files\ParcInfo\parc_info.db
# - Shortcuts exist: Start Menu, Desktop
# - Registry entries present: HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\Uninstall\ParcInfo
```

**Test macOS installer:**
```bash
# Build
python build/build_installers.py --version 2.5.0 --macos-only

# Mount
open dist/ParcInfo-2.5.0.dmg

# Install
# Drag ParcInfo.app to /Applications

# Verify
# - App launches
# - Data dir created: ~/.parcinfo/
# - Launchpad shows ParcInfo
```

**Test auto-update:**
```bash
# 1. Modify version.json to trigger update
# 2. Launch app
# 3. Go to Help → Check for Updates
# 4. Should show notification
# 5. Click "Update" → downloads and installs
```

---

## Technical Details

### Architecture

```
Installer System
├── Windows NSIS
│   ├── Checks: Python 3.8+, Visual C++ Runtime
│   ├── Installs: C:\Program Files\ParcInfo\
│   ├── Creates: Start Menu, Desktop shortcuts
│   └── Registry: Add/Remove Programs entry
│
├── macOS DMG
│   ├── Checks: Python 3.8+, Xcode CLI (optional)
│   ├── Installs: /Applications/ParcInfo.app
│   ├── Creates: Launchpad entry, .app bundle
│   └── Scripts: pre/post install hooks
│
└── Auto-Update
    ├── Fetches: version.json from GitHub
    ├── Compares: semantic versioning
    ├── Downloads: platform-specific installer
    ├── Validates: SHA256 checksums
    └── Installs: silent mode with rollback
```

### Files & Directories

```
parc_info/
├── __version__.py                    # Version centralized
├── system_checker.py                 # Dependency validation
├── update_checker.py                 # Auto-update logic
│
├── installers/
│   ├── windows/
│   │   ├── parcinfo.nsi             # NSIS script
│   │   ├── build_installer.ps1      # PowerShell builder
│   │   ├── create_assets.py         # Image generation
│   │   └── files/
│   │       ├── banner.bmp           # NSIS banner
│   │       ├── icon.ico             # App icon
│   │       └── LICENSE.txt
│   │
│   └── macos/
│       ├── create_dmg.sh            # DMG builder
│       ├── create_pkg.sh            # PKG builder (MDM)
│       ├── resources/
│       │   ├── background.png       # DMG background
│       │   └── icon.icns            # App icon
│       └── scripts/
│           ├── preinstall           # Pre-install checks
│           ├── postinstall          # Post-install setup
│           └── uninstall            # Cleanup
│
├── build/
│   ├── build_installers.py          # Build orchestrator
│   ├── generate_version_json.py     # Metadata generator
│   └── sign_binaries.py             # Code signing (optional)
│
└── version.json                      # Version metadata + downloads
```

### Version Metadata (`version.json`)

```json
{
  "version": "2.5.0",
  "release_date": "2026-04-23",
  "min_python": "3.8",
  "downloads": {
    "windows": "https://github.com/darkmind64/parc_info/releases/...",
    "macos_intel": "https://github.com/darkmind64/parc_info/releases/...",
    "macos_arm": "https://github.com/darkmind64/parc_info/releases/..."
  },
  "checksums": {
    "windows": "sha256:...",
    "macos_intel": "sha256:...",
    "macos_arm": "sha256:..."
  }
}
```

### Logging & Debugging

**Application logs:**
```bash
# Windows
type %APPDATA%\ParcInfo\parc_info.log

# macOS
tail -f ~/.parcinfo/parc_info.log

# Linux
tail -f ~/.parcinfo/parc_info.log
```

**Build logs:**
```bash
# Verbose build output
python build/build_installers.py --version 2.5.0 --verbose

# NSIS build details
installers\windows\build_installer.ps1 -Version "2.5.0" -Verbose
```

**Update checker logs:**
```python
import logging
logging.getLogger("parcinfo.updater").setLevel(logging.DEBUG)

from update_checker import UpdateChecker
checker = UpdateChecker()
checker.check_for_updates(force=True)
```

### Security Considerations

- ✅ **Checksums**: SHA256 validation of downloaded installers
- ✅ **HTTPS**: All downloads over encrypted connection
- ✅ **Registry**: Limited registry access (Windows)
- ✅ **Permissions**: Pre-check system requirements before install
- ⚠️ **Code signing**: Optional (not implemented, can be added)
- ⚠️ **Auto-update**: Monthly check (configurable)

### Platform Support

| Feature | Windows | macOS | Linux |
|---------|---------|-------|-------|
| Installer | NSIS | DMG/PKG | — |
| Auto-update | ✅ | ✅ | — |
| System checks | ✅ | ✅ | ✅ |
| Registry | ✅ | — | — |
| Shortcuts | ✅ | ✅ | — |
| Background install | ✅ | — | — |

---

## Troubleshooting

### Installation Issues

**"Python 3.8+ required"**
- Install Python from https://python.org
- Ensure it's in PATH: `python --version`

**"NSIS not found" (Windows)**
- Install NSIS: https://nsis.sourceforge.io/Download
- Add to PATH: `C:\Program Files (x86)\NSIS`

**"Access Denied" (Windows)**
- Run installer as Administrator
- OR: UAC prompt will appear automatically

### macOS Issues

**"Cannot open because it is from an unidentified developer"**
- Right-click → "Open" → "Open anyway"
- OR: `xattr -d com.apple.quarantine /Applications/ParcInfo.app`

**"Xcode CLI Tools not found"**
- Install: `xcode-select --install`
- Network features may be limited without it

### Update Issues

**Update downloads but doesn't install**
- Installer may be blocked by antivirus
- Check Windows Defender, McAfee, Norton settings
- Add exception for ParcInfo installer

**Auto-update disabled after crash**
- Restart application
- Check: Settings → Advanced → Auto-update

---

## Support

- **GitHub**: https://github.com/darkmind64/parc_info
- **Issues**: https://github.com/darkmind64/parc_info/issues
- **Discussions**: https://github.com/darkmind64/parc_info/discussions

---

**Last Updated**: 2026-04-26  
**Version**: 2.5.0
