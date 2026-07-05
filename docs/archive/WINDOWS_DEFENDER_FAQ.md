# Windows Defender FAQ — ParcInfo Installer

## ❓ Why does Windows Defender flag the installer?

PyInstaller executables are sometimes flagged by antivirus software as a precaution because:
- They're dynamically compiled (not statically linked)
- The packer/bundler can resemble malware techniques
- The executable may be new/unknown to Windows reputation system

**This is a false positive.** ParcInfo is legitimate open-source software.

---

## ✅ How do I safely bypass the warning?

### Option 1: Click "Run Anyway" (Easiest)

**Windows SmartScreen appears:**
```
⚠️ Windows protected your PC
Windows SmartScreen can't find info about this app. It may not be safe.
```

**Solution:**
1. Click "More info"
2. Click "Run anyway"
3. Installation proceeds normally

---

### Option 2: Unblock the File (One-Time)

**Before running installer.exe:**

1. Right-click `installer.exe`
2. Select "Properties"
3. Check the box: "☐ Unblock" (if present)
4. Click "Apply" then "OK"
5. Run normally

This tells Windows you trust the file source.

---

### Option 3: Exclude from Real-Time Scanning (Advanced)

**Windows Defender Settings → Virus & threat protection → Manage settings:**

```
Add exclusions:
  📁 Downloads folder (or wherever you have installer.exe)
  📁 C:\Program Files\ParcInfo\ (after installation)
```

---

## 🛡️ Is the installer actually safe?

**Yes.** Here's why:

✅ **Open Source**
- Full source code publicly available on GitHub
- Anyone can audit it
- No hidden malicious code

✅ **No Admin Required**
- Installer runs as normal user
- Manifest declares `asInvoker` (no elevated privileges)
- Can't make system-wide changes

✅ **Standard Installation**
- Uses Windows Installer API
- Creates standard registry entries
- Normal application behavior

✅ **Regular Releases**
- Signed commits on GitHub
- Version history visible
- Community can track changes

---

## 🚀 What's Happening Behind the Scenes?

When you click "Run anyway":

1. **Windows allows execution** (you've authorized it)
2. **PyInstaller extracts bundle** to temp directory
3. **installer.exe** reads the Windows manifest
4. **Manifest identifies** the application as legitimate
5. **Installation proceeds** to `C:\Program Files\ParcInfo\`
6. **Database initialized** with your settings
7. **Application launches** automatically

No malicious activity occurs at any step.

---

## 📊 Why Disable UPX Compression?

**What was causing the flag:**
- Old builds used UPX compression
- Antivirus engines detect UPX as a packing technique
- Malware sometimes uses UPX to hide code

**What we changed:**
- ✅ UPX compression **disabled** in latest builds
- ✅ Windows manifest **added** (legitimate application indicator)
- ✅ Application icon **enabled** (professional appearance)

**Result:**
- New builds have fewer antivirus false positives
- v2.5.0+ includes these optimizations

---

## 🔄 How Long Until Windows Trusts It?

Windows uses multiple methods to build reputation:

| Method | Timeline | Status |
|--------|----------|--------|
| Code Signing | Immediate (if purchased) | Not yet implemented |
| SmartScreen Whitelist | 1-7 days | Can submit (see below) |
| Download Reputation | 30-90 days | Builds over time |
| User Feedback | Ongoing | Helps Windows learn |

**Current Status:** Using optimization techniques (manifest, no UPX) which significantly reduce false positives.

---

## 🔗 Submit to Microsoft for Analysis

Want to help build reputation faster?

1. Go to: https://www.microsoft.com/en-us/wdsi/filesubmission
2. Upload `installer.exe`
3. Microsoft analyzes it (1-7 days)
4. Once approved, Windows fully trusts it

This is entirely optional but recommended for long-term trust.

---

## ❌ What About Malware Concerns?

### Real Risks with Other Software:
- Downloads hidden toolbars
- Modifies homepage
- Collects browsing data
- Installs cryptocurrency miners

### ParcInfo:
- ✅ No tracking/telemetry
- ✅ No data collection
- ✅ No system modifications beyond install directory
- ✅ No registry persistence (except Uninstall key)
- ✅ No background services

ParcInfo is a simple IT asset management tool. It manages your local database and that's it.

---

## 🆘 Still Getting Warnings?

### If Windows still blocks after these steps:

**1. Check your antivirus settings:**
   - Some third-party antivirus (Norton, McAfee, etc.) may have stricter policies
   - Check their settings for ParcInfo or installer.exe
   - Add to whitelist if available

**2. Temporary workaround:**
   - Disable real-time scanning temporarily
   - Run installer
   - Re-enable scanning
   - (Not recommended, but works)

**3. Code signing certificate:**
   - Most reliable long-term solution
   - Requires purchasing EV certificate (~$150/year)
   - Eliminates all warnings immediately
   - Recommended for production/commercial use

---

## 💬 Questions or Concerns?

### How to verify the installer is safe:

1. **Check source:**
   - Downloaded from official GitHub? ✅
   - From https://github.com/darkmind64/parc-info/releases ✅

2. **Verify checksum:**
   ```bash
   # Windows PowerShell
   Get-FileHash installer.exe -Algorithm SHA256
   
   # Compare with: https://github.com/darkmind64/parc-info/releases/tag/v2.5.0
   ```

3. **Audit source code:**
   - Full source available on GitHub
   - Read installer.py, launcher.py, etc.
   - No obfuscation or hidden code

4. **Report false positive:**
   - Found a real issue? Report on GitHub Issues
   - Think it's a false positive? Help Microsoft learn:
     https://www.microsoft.com/en-us/wdsi/filesubmission

---

## 📝 Summary

| Question | Answer |
|----------|--------|
| **Is it safe?** | Yes, completely safe. Open-source, auditable code. |
| **Why the warning?** | PyInstaller false positive. Legitimate but flagged as precaution. |
| **How do I fix it?** | Click "Run anyway" or unblock the file properties. |
| **Will it always warn?** | No, Windows learns over time. Latest builds (v2.5.0+) have optimizations. |
| **Do I have to bypass?** | Yes, to use it. Windows has no reason to distrust it beyond precaution. |
| **Best long-term solution?** | Submit to Microsoft SmartScreen for free whitelist (1-7 days). |

---

## 🎯 Next Steps

1. **Download** installer.exe from GitHub releases
2. **Run it** (click "Run anyway" if warned)
3. **Install** to default location or choose custom path
4. **Launch** application automatically
5. **Use** for managing your IT assets
6. **Updates** happen automatically when available

---

**v2.5.0 includes optimizations to minimize these warnings.**

If you're still seeing them, you're likely on an older version. Download the latest from GitHub.

---

**Questions?** Open an issue on GitHub: https://github.com/darkmind64/parc-info/issues
