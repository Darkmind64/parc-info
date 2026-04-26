# ParcInfo Build & Deploy — Quick Start

## 🚀 One-Liner Build

```bash
python build.py
```

Generates:
- `dist/ParcInfo.exe` — Application
- `dist/installer.exe` — Installer (copies app + shortcuts)

---

## 📦 Release Workflow

### 1. Build locally
```bash
python build.py
```

### 2. Test installer
```bash
dist/installer.exe

# Or silent
dist/installer.exe --silent
```

### 3. Create GitHub Release
```bash
# Update version
vim __version__.py  # Change __version__ = "2.6.0"

# Commit
git add __version__.py && git commit -m "Release v2.6.0" && git tag v2.6.0

# Generate checksums
python build/generate_version_json.py 2.6.0

# Create release
gh release create v2.6.0 \
  dist/ParcInfo.exe \
  dist/installer.exe \
  version.json
```

### 4. Done ✓
Users:
1. Download `installer.exe`
2. Run it
3. App installs & auto-updates itself

---

## 🤖 Auto-Update (Already Built-In)

**User Experience:**
- App checks for updates monthly (background)
- Notification: "New version available"
- Click "Update" → downloads & installs silently
- App restarts automatically

**Nothing to configure!** ✅

---

## 📂 Key Files

| File | Purpose |
|------|---------|
| `__version__.py` | Version number (source of truth) |
| `installer.py` | Installer logic (copies app + shortcuts) |
| `launcher.py` | App launcher (auto-update check) |
| `update_checker.py` | Auto-update logic |
| `parcinfo.spec` | PyInstaller config (app) |
| `installer.spec` | PyInstaller config (installer) |
| `build.py` | Build script |
| `version.json` | Download URLs + checksums |

---

## 💡 That's It!

```
Your Code → build.py → {ParcInfo.exe + installer.exe} → Release → Users Auto-Update
```

No complex installers. No scripts. Just:
- ✅ Compile app (PyInstaller)
- ✅ Compile installer (PyInstaller)
- ✅ Upload to GitHub
- ✅ Users get auto-updates automatically

---

## 🔧 Customization

**Change install location:**
```python
# Edit installer.py before building
if self.system == "Windows":
    self.install_dir = Path("D:/MyApp")  # Custom location
```

**Change update check frequency:**
```python
# Edit update_checker.py
CHECK_INTERVAL_DAYS = 7  # Check weekly instead of monthly
```

**Disable auto-update:**
```python
# Comment out in launcher.py
# threading.Thread(target=check_updates, daemon=True).start()
```

---

## ✅ Checklist Before Release

- [ ] Version updated in `__version__.py`
- [ ] `python build.py` succeeds
- [ ] `dist/installer.exe` works
- [ ] `python build/generate_version_json.py <version>` creates `version.json`
- [ ] GitHub release created with all 3 files

---

**That's all you need!** 🎉
