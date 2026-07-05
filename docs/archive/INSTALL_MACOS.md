# ParcInfo Installation for macOS

## 🎯 Choose Your Installation Method

### Method 1: Universal Installer (Recommended) ⭐

**One-liner installation** that compiles directly on your Mac.

```bash
curl -fsSL https://raw.githubusercontent.com/darkmind64/parc-info/master/install.sh | bash
```

**What it does:**
1. ✅ Verifies Python 3.8+ (installs if needed)
2. ✅ Verifies Xcode Command Line Tools (installs if needed)
3. ✅ Downloads source code from GitHub
4. ✅ Creates isolated Python environment
5. ✅ Installs dependencies
6. ✅ Compiles locally with PyInstaller
7. ✅ Installs to `/Applications/ParcInfo.app`
8. ✅ Launches automatically

**Advantages:**
- ✅ No Gatekeeper warnings (compiles locally)
- ✅ Completely transparent (source code visible)
- ✅ No Apple Developer certificate needed
- ✅ Users can audit code before installation
- ✅ Works on any macOS 10.13+

**Disadvantages:**
- ❌ Requires Python 3.8+ (script helps you install)
- ❌ Requires Xcode Command Line Tools (~1GB)
- ❌ Compilation takes 2-3 minutes (one-time)

---

## Manual Installation (If curl fails)

### Step 1: Download the installer script

```bash
# Clone the repository
git clone https://github.com/darkmind64/parc-info.git
cd parc-info

# Or download without git
curl -fsSL https://github.com/darkmind64/parc-info/archive/refs/heads/master.zip -o parc-info.zip
unzip parc-info.zip
cd parc-info-master
```

### Step 2: Run the installer

```bash
chmod +x install.sh
./install.sh
```

---

## Prerequisites

### Python 3.8+ (Optional - script can install)

If Python is not installed, the script will prompt you.

**Manual install options:**

```bash
# Option 1: Homebrew (recommended)
brew install python@3.11

# Option 2: Official installer
# Download from https://www.python.org/downloads/macos/

# Verify installation
python3 --version
```

### Xcode Command Line Tools (Optional - script can install)

The script automatically installs these if needed.

**Manual install:**

```bash
xcode-select --install
```

**Or full Xcode** (much larger):

```bash
# From App Store
# Or install command line tools only (recommended)
```

---

## Installation Process Walkthrough

### Step 1: Download & Compile

```
▶ Checking macOS version...
✓ macOS 14.3.1 (compatible)

▶ Checking Python availability...
✓ Found python3 3.11.8

▶ Checking Xcode Command Line Tools...
✓ Xcode tools found

▶ Checking disk space...
✓ Enough disk space available

▶ Downloading ParcInfo source code...
✓ Source code downloaded

▶ Creating Python virtual environment...
✓ Virtual environment created

▶ Installing dependencies (this may take a few minutes)...
✓ Dependencies installed

▶ Compiling with PyInstaller...
✓ Compilation complete

▶ Installing to /Applications...
✓ Installed to /Applications/ParcInfo.app
```

### Step 2: Launch

```
▶ Launching ParcInfo...
✓ All done! ParcInfo is running.
```

The application opens automatically!

---

## After Installation

### Launch ParcInfo Anytime

```bash
# From Applications folder
/Applications/ParcInfo.app

# Or from command line
open /Applications/ParcInfo.app

# Or via Spotlight search
# Cmd+Space → type "ParcInfo" → Return
```

### Note: System Tray on macOS

The system tray icon is **disabled on macOS** due to compatibility issues with certain macOS versions. The application works perfectly without it — all functionality is available through the web interface. You can still:
- Pin the app to your Dock for easy access
- Use Cmd+Space to launch via Spotlight
- Add the app to your Favorites in Finder

### Add to Dock

1. Open Applications folder
2. Find ParcInfo.app
3. Right-click → Options → Keep in Dock

### Updates

Updates happen automatically from within the app. When a new version is available, you'll see a notification.

### Uninstall

```bash
rm -rf /Applications/ParcInfo.app
```

Or just drag it to Trash from Applications folder.

---

## Troubleshooting

### "Python not found"

```bash
# Install Python via Homebrew (easiest)
brew install python@3.11

# Or download official installer
# https://www.python.org/downloads/macos/
```

### "Xcode Command Line Tools not found"

```bash
# Script will offer to install automatically
# Or manually:
xcode-select --install
```

### "curl: command not found"

Use the manual installation method instead:

```bash
git clone https://github.com/darkmind64/parc-info.git
cd parc-info
chmod +x install.sh
./install.sh
```

### "zsh: permission denied: ./install.sh"

```bash
chmod +x install.sh
./install.sh
```

### Compilation takes too long

This is normal. First installation compiles everything:
- PyInstaller setup: 1-2 minutes
- Dependency compilation: 1-2 minutes
- Application compilation: 30-60 seconds

Subsequent runs (auto-updates) will be faster.

### "Cannot open ParcInfo.app because Apple cannot check it"

This shouldn't happen with the installer script (it compiles locally).

If you get this warning:

```bash
xattr -d com.apple.quarantine /Applications/ParcInfo.app
open /Applications/ParcInfo.app
```

---

## Why This Approach?

### Problems with Pre-Compiled Binaries

**macOS Gatekeeper** (security system):
- Blocks unsigned applications automatically
- Requires Apple Developer certificate ($99/year)
- Requires Apple Notarization (1-7 days wait)
- Still shows warnings to users

### Benefits of Compile-on-Install

✅ **No Gatekeeper Issues**
- Compiled code is trusted locally
- No signature/notarization needed
- Zero security warnings

✅ **Transparency**
- Users see exact source code
- Can audit before installation
- Can modify if desired
- True open-source experience

✅ **Verification**
- Checksum verification of downloads
- Script is readable and auditable
- No "black box" binaries

✅ **Cost-Free**
- No Apple Developer account needed
- No certificate fees
- No notarization waiting

---

## How It Works (Technical)

### The Install Script

1. **Pre-flight checks** (5 seconds)
   - Python version verification
   - Xcode tools availability
   - Disk space validation

2. **Source download** (30 seconds)
   - Clones repository from GitHub
   - Verifies integrity

3. **Environment setup** (30 seconds)
   - Creates isolated `venv`
   - Ensures no dependency conflicts

4. **Dependency installation** (1-2 minutes)
   - Flask, Werkzeug, SQLite
   - PyInstaller, PIL, pystray

5. **Compilation** (1-2 minutes)
   - PyInstaller packages everything
   - Creates macOS .app bundle
   - Bundles Python runtime

6. **Installation** (5 seconds)
   - Moves to /Applications
   - Removes quarantine attribute
   - Makes it Finder-friendly

7. **Launch** (5 seconds)
   - Opens application automatically
   - User can start using immediately

---

## Performance

### Initial Install
- Python: 1-2 minutes (if installing)
- Xcode tools: 15-20 minutes (if installing)
- ParcInfo: 5 minutes total
- **First run: 20-25 minutes** (if starting from scratch)

### Subsequent Runs
- Auto-updates via app: Much faster
- Reinstall from script: ~5 minutes

### Runtime
- App startup: 2-3 seconds
- Auto-update check: <1 second
- Normal operation: Fast, no compilation overhead

---

## Comparison: Installation Methods

| Aspect | Pre-Built .app | Universal Installer (Script) |
|--------|--|--|
| **Gatekeeper warnings** | Yes (needs notarization) | No |
| **Installation time** | 2-3 seconds | 5-10 minutes |
| **Requires Python** | No | Yes (script installs) |
| **Transparency** | Medium (binary) | High (source code) |
| **Customizable** | No | Yes |
| **Cost** | Notarization fees | Free |
| **Trust factor** | Signature-based | Source-based |

---

## Getting Help

### If Installation Fails

1. Check the error message carefully
2. Try running with verbose output:
   ```bash
   bash -x install.sh
   ```
3. Check log file if created

### Report Issues

https://github.com/darkmind64/parc-info/issues

Include:
- macOS version (`sw_vers`)
- Python version (`python3 --version`)
- Error messages from script

---

## Security Notes

### What the Script Does

✅ **Safe**:
- Creates isolated Python environment (`venv`)
- Downloads from official GitHub only
- Uses HTTPS for all downloads
- Verifies downloaded files
- Doesn't require elevated permissions

❌ **Never**:
- Requests passwords
- Modifies system files
- Installs root tools
- Collects personal data

### Verify Script Safety

Read it yourself:
```bash
curl -fsSL https://raw.githubusercontent.com/darkmind64/parc-info/master/install.sh | less
```

The script is just 400 lines of readable bash.

---

## Advanced Options

### Custom Installation Directory

Edit the script before running:

```bash
# Change this line in install.sh
INSTALL_DIR="/Applications"  # → Your custom path
```

### Stay on Specific Version

```bash
# Clone specific branch/tag
git clone --branch v2.5.0 https://github.com/darkmind64/parc-info.git
cd parc-info
chmod +x install.sh
./install.sh
```

### Use Different Python Version

```bash
# Install specific Python version first
brew install python@3.9

# Then run script (it will auto-detect)
./install.sh
```

---

## FAQ

**Q: Do I need to understand the script?**  
A: No, just run it. But you can read it anytime to verify it's safe.

**Q: Can I run the script multiple times?**  
A: Yes, it will reinstall cleanly each time.

**Q: Will auto-updates still work?**  
A: Yes, completely. App checks for updates automatically.

**Q: What if Python version updates?**  
A: Auto-updates will recompile with current Python version.

**Q: Can I modify and recompile?**  
A: Yes! Full source code is available. Edit and run script again.

---

**Installation Method**: Universal Installer (Compile on Target)  
**Compatibility**: macOS 10.13+  
**Updated**: 2026-04-26
