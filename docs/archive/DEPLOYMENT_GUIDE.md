# ParcInfo Deployment Guide

Simple deployment system: **compile → install → auto-update**

---

## 📦 For End Users

### Installation

**Windows:**
1. Download `installer.exe` from releases
2. Run it (double-click)
3. Click "Install"
4. Application launches automatically

**macOS:**
1. Download `installer.exe` equivalent or `ParcInfo.app`
2. Run installer or drag app to Applications

### Auto-Update

**Automatic:**
- Application checks for updates every 30 days
- Notification appears if new version available
- Click "Update" → downloads and installs silently
- App restarts automatically

**Manual:**
- In application menu: `Help → Check for Updates`

---

## 🔨 For Developers

### Prerequisites

```bash
pip install pyinstaller
# macOS: brew install create-dmg
```

### Build Process

**1. Build application and installer:**
```bash
python build.py
```

Output:
- `dist/ParcInfo.exe` (or `.app` on macOS) — main application
- `dist/installer.exe` — installer executable

**2. Test locally:**
```bash
# Install to default location
dist/installer.exe

# Or silent install
dist/installer.exe --silent --install-dir "C:\Program Files\ParcInfo"
```

**3. Verify:**
- Check app launches
- Check shortcuts created
- Check auto-update works: `Help → Check for Updates`

### Release Process

**1. Update version:**
```python
# Edit __version__.py
__version__ = "2.6.0"
```

**2. Commit and tag:**
```bash
git add __version__.py
git commit -m "Release v2.6.0"
git tag v2.6.0
git push origin v2.6.0
```

**3. Create release manually (or use CI):**
```bash
# Create GitHub release
gh release create v2.6.0 \
  dist/ParcInfo.exe \
  dist/installer.exe \
  --title "ParcInfo 2.6.0" \
  --notes-file CHANGELOG.md
```

**4. Update version.json:**
```bash
# Manually update or via script
python build/generate_version_json.py 2.6.0
```

---

## 🏗️ Architecture

### Components

```
ParcInfo Deployment System
│
├─ Application (parcinfo.spec)
│  └─ Compiled with PyInstaller → dist/ParcInfo.exe
│
├─ Installer (installer.spec)
│  └─ Compiled with PyInstaller → dist/installer.exe
│  └─ Copies binaries + creates shortcuts
│
├─ Auto-Update (update_checker.py)
│  └─ Checks version.json monthly
│  └─ Downloads + installs silently
│
└─ Version Metadata (version.json)
   └─ Current version + download URLs + checksums
```

### Files

```
parc_info/
├── __version__.py ...................... Version number
├── launcher.py ......................... Auto-update integration
├── installer.py ........................ Installer logic
├── update_checker.py ................... Auto-update checker
│
├── parcinfo.spec ....................... PyInstaller config (app)
├── installer.spec ...................... PyInstaller config (installer)
├── build.py ............................ Build script
│
├── version.json ........................ Version metadata
└── DEPLOYMENT_GUIDE.md ................. This file
```

---

## 🔄 Auto-Update Flow

```
Application Start
  ↓
launcher.py checks for updates (background)
  ↓
update_checker.py fetches version.json
  ↓
If newer version available:
  ├─ Download installer
  ├─ Validate checksum
  ├─ User notification
  └─ User clicks "Update"
       ├─ Run installer silently
       ├─ Replace executable
       └─ Restart app
  
No update needed:
  └─ Continue normally
```

---

## 📝 Configuration

### version.json

```json
{
  "version": "2.6.0",
  "min_python": "3.8",
  "downloads": {
    "windows": "https://github.com/.../releases/download/v2.6.0/ParcInfo.exe",
    "macos": "https://github.com/.../releases/download/v2.6.0/ParcInfo.app"
  },
  "checksums": {
    "windows": "sha256:abc123...",
    "macos": "sha256:def456..."
  }
}
```

### Update Check Interval

Default: **30 days**

To change, edit `update_checker.py`:
```python
CHECK_INTERVAL_DAYS = 30  # Change to desired interval
```

To disable auto-update:
```python
# In launcher.py, comment out:
# threading.Thread(target=check_updates, daemon=True).start()
```

---

## 🛠️ Troubleshooting

### Installer fails

**Check prerequisites:**
```bash
python --version  # Must be 3.8+
pyinstaller --version
```

**Check source executable exists:**
```bash
ls dist/ParcInfo*
```

### Auto-update not working

**Check version.json:**
```bash
curl https://raw.githubusercontent.com/.../master/version.json
```

**Check logs:**
- Windows: `%APPDATA%\ParcInfo\parc_info.log`
- macOS: `~/.parcinfo/parc_info.log`

**Manually check:**
```python
from update_checker import UpdateChecker
checker = UpdateChecker()
checker.check_for_updates(force=True)
```

### Installation location issues

**Windows custom location:**
```bash
dist/installer.exe --install-dir "C:\MyApps\ParcInfo"
```

**macOS custom location:**
```bash
# Edit installer.py before compiling
self.install_dir = Path("/Users/shared/Applications") / f"{APP_NAME}.app"
```

---

## 📊 Checklist Before Release

- [ ] Version updated in `__version__.py`
- [ ] `version.json` updated with download URLs
- [ ] App builds successfully: `python build.py`
- [ ] Installer builds successfully
- [ ] Test installation locally
- [ ] Test auto-update: `Help → Check for Updates`
- [ ] GitHub release created with assets
- [ ] Checksums match in `version.json`

---

## 🔐 Security

✅ **Implemented:**
- SHA256 checksum validation
- HTTPS for downloads
- User permissions check (Windows)

**Optional enhancements:**
- Code signing (Windows Authenticode)
- App notarization (macOS)
- Release signing (GPG)

---

## 📞 Support

- Issues: https://github.com/darkmind64/parc_info/issues
- Email: support@parcinfo.local

---

**Last Updated:** 2026-04-26  
**Version:** 2.5.0
