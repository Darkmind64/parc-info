# ParcInfo Update System Architecture

Complete system design for automatic updates with in-app notifications.

---

## 🏗️ System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                      USER EXPERIENCE                              │
└──────────────────────────────────────────────────────────────────┘
                                │
                                │
        ┌───────────────────────┼───────────────────────┐
        │                       │                       │
        ↓                       ↓                       ↓
    ┌───────────┐          ┌──────────┐          ┌─────────────┐
    │ Startup   │          │ In App   │          │ Background  │
    │ Auto-     │          │ Banner   │          │ Check       │
    │ Install   │          │ Notification│       │ (hourly)    │
    └─────┬─────┘          └─────┬────┘          └──────┬──────┘
          │                      │                       │
          └──────────┬───────────┴───────────┬───────────┘
                     │                       │
                     ↓                       ↓
          ┌─────────────────────────────────────────┐
          │  Update Checker (update_checker.py)     │
          │  ├─ Check version.json                  │
          │  ├─ Compare versions                    │
          │  ├─ Download installer                  │
          │  ├─ Validate checksum SHA256            │
          │  └─ Install silently                    │
          └──────────────┬──────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          │                             │
          ↓                             ↓
    ┌──────────────┐          ┌───────────────┐
    │ Launcher     │          │ Update API    │
    │ (startup)    │          │ Routes        │
    │              │          │ (Flask)       │
    │ ├ Check      │          │               │
    │ ├ Install    │          │ ├ /status     │
    │ ├ Restart    │          │ ├ /check      │
    │ └ Continue   │          │ ├ /install    │
    │              │          │ └ /dismiss    │
    └──────────────┘          └───────┬───────┘
                                      │
                         ┌────────────┴────────────┐
                         │                         │
                         ↓                         ↓
                  ┌──────────────┐        ┌──────────────┐
                  │ JavaScript   │        │ Notification │
                  │ UI           │        │ Manager      │
                  │              │        │              │
                  │ ├ Show banner│        │ ├ Track      │
                  │ ├ Handle     │        │ ├ Expire     │
                  │ │ buttons    │        │ ├ Dismiss    │
                  │ └ Show status│        │ └ Notify     │
                  └──────────────┘        └──────────────┘
                         │
                         ↓
                  ┌──────────────┐
                  │ User Browser │
                  │              │
                  │ ┌──────────┐ │
                  │ │ Update   │ │
                  │ │ Banner   │ │
                  │ │ Buttons  │ │
                  │ └──────────┘ │
                  └──────────────┘
```

---

## 📊 Data Flow

### Startup (Auto-Install)

```
launcher.py starts
    ↓
read __version__.py (current: 2.5.0)
    ↓
UpdateChecker.check_and_install_updates(force=True, silent=True)
    ↓
fetch version.json from GitHub
    ↓
compare 2.5.0 < 2.6.0 ?
    ├─ YES: proceed to install
    │   ├─ Download installer.exe (from version.json URL)
    │   ├─ Calculate SHA256
    │   ├─ Compare with checksum in version.json
    │   ├─ If match: proceed
    │   └─ If mismatch: fail gracefully
    │       ├─ Run installer.exe (silent)
    │       ├─ Installer copies binary + shortcuts
    │       ├─ Installer exits
    │       └─ launcher.py exits (system restarts app)
    │
    └─ NO: continue
        └─ Flask app.run() normally
```

### In-App (Background Check)

```
Browser loads app
    ↓
JavaScript loads update_notifier.js
    ↓
InitializedUpdateNotifier()
    ├─ start()
    │   └─ setInterval(checkForUpdates, 3600000) // every hour
    │
    └─ Check at: /api/updates/status
        ├─ UpdateNotifier.check_for_updates()
        │   ├─ fetch version.json
        │   ├─ compare versions
        │   └─ set current_notification
        │
        └─ Return to JS:
            {
              "update_available": true,
              "version": "2.6.0",
              "notification": {...}
            }

JS receives response
    ↓
Show banner: "📦 ParcInfo 2.6.0 is available"
    ├─ "Install Update" button
    └─ "Dismiss" button

User click "Install Update"
    ↓
fetch /api/updates/install (POST)
    ├─ UpdateNotifier.install_update()
    │   ├─ Download installer
    │   ├─ Show "Installing..." status
    │   └─ Run installer in background
    │
    └─ Return to JS:
        {
          "status": "installing",
          "notification": {
            "type": "installing",
            "message": "Updating ParcInfo..."
          }
        }

JS updates UI: "Installing... Application will restart."
    ↓
Installer completes
    ├─ Copy binary
    ├─ Create shortcuts
    └─ Exit

App detects restart trigger (or user manually restarts)
    ↓
New version launches
    └─ Notification dismissed automatically
```

---

## 🗂️ Directory Structure

```
parc_info/
│
├── Core Update System
│   ├── update_checker.py ............ Download + install logic
│   ├── update_notifier.py ........... Notification manager
│   ├── app_update_routes.py ......... Flask API routes
│   │
│   ├── launcher.py (modified) ...... Auto-install at startup
│   └── __version__.py .............. Version source of truth
│
├── Frontend (UI Notifications)
│   └── static/js/update_notifier.js . JavaScript notifications
│
├── Flask App
│   ├── app.py (needs update) ....... Add register_update_routes()
│   └── templates/base.html (needs) . Add container + script
│
├── Installation System
│   ├── installer.py ................ Installer logic
│   ├── installer.spec .............. PyInstaller config (installer)
│   └── parcinfo.spec ............... PyInstaller config (app)
│
├── Build System
│   ├── build.py .................... Build script (app + installer)
│   └── build/generate_version_json.py . Metadata generator
│
├── Metadata
│   └── version.json ................ Downloads + checksums
│
└── Documentation
    ├── UPDATE_INTEGRATION_GUIDE.md .. Integration steps
    ├── AUTO_UPDATE_SUMMARY.md ....... Complete summary
    ├── INTEGRATION_EXAMPLE.py ....... Code examples
    ├── ARCHITECTURE.md .............. This file
    └── QUICK_START.md ............... 5-line quick start
```

---

## 🔄 Component Interactions

### Components

```
┌─────────────────────────────────────┐
│     launcher.py                     │
│  (Entry point, auto-install)        │
│                                     │
│  ├─ Detect free port               │
│  ├─ Check for updates               │
│  ├─ Download + install if needed    │
│  └─ Start Flask app                 │
└──────────────────┬──────────────────┘
                   │
        ┌──────────┴──────────┐
        │                     │
        ↓                     ↓
┌──────────────────┐  ┌──────────────────┐
│  update_checker  │  │ Flask App (app.py)│
│  .py             │  │                   │
│                  │  │ ├─ Routes        │
│  ├─ Check latest │  │ ├─ Templates     │
│  ├─ Download     │  │ └─ Context procs │
│  ├─ Validate     │  │                  │
│  └─ Install      │  └────────┬─────────┘
└─────────────────┘           │
         ↑                     │
         │          ┌──────────┴──────────┐
         │          │                     │
         │          ↓                     ↓
         │    ┌─────────────────┐  ┌─────────────────┐
         │    │ update_notifier │  │ app_update_     │
         │    │ .py             │  │ routes.py       │
         │    │                 │  │                 │
         │    │ ├─ Track status │  │ ├─ /api/status  │
         │    │ ├─ Manage notif │  │ ├─ /api/check   │
         │    │ └─ Bg checks    │  │ ├─ /api/install │
         │    │                 │  │ └─ /api/dismiss │
         │    └────┬────────────┘  └────────┬────────┘
         │         │                        │
         │         ↓                        ↓
         │    ┌──────────────────────────────────┐
         │    │ version.json (GitHub)            │
         │    │ - version                        │
         │    │ - downloads URLs                 │
         │    │ - checksums (SHA256)             │
         │    └──────────────────────────────────┘
         │
         └─────── (fetches)
               
Browser:
├─ JavaScript (update_notifier.js)
│  ├─ Load on page
│  ├─ Periodic checks via API
│  ├─ Show notifications
│  └─ Handle user actions
│
└─ HTML (base.html)
   ├─ Container div (#update-notification-container)
   └─ Script tag (load update_notifier.js)
```

---

## 🔐 Security Layer

```
version.json (GitHub)
    ├─ Contains download URLs
    └─ Contains SHA256 checksums
            ↓
    Downloaded by: update_checker.py
            ↓
    Installer downloaded
            ↓
    SHA256 calculated locally
            ↓
    Compare: local hash == version.json hash
    │
    ├─ MATCH: Proceed with installation ✅
    │
    └─ MISMATCH: Abort, don't install ❌
            (Corrupted or tampered file)
```

---

## 🎯 State Machine

```
[IDLE]
  │
  ├─ Startup trigger
  │   └─ → [CHECKING]
  │
  └─ Hourly timer (in background)
      └─ → [CHECKING]


[CHECKING]
  │
  ├─ Update available
  │   └─ → [UPDATE_AVAILABLE]
  │
  └─ No update
      └─ → [IDLE]


[UPDATE_AVAILABLE]
  │
  ├─ User click "Install"
  │   └─ → [DOWNLOADING]
  │
  └─ User click "Dismiss"
      └─ → [IDLE]


[DOWNLOADING]
  │
  ├─ Success
  │   └─ → [INSTALLING]
  │
  └─ Error
      └─ → [UPDATE_AVAILABLE] (retry)


[INSTALLING]
  │
  ├─ Success
  │   └─ → [RESTART] → Quit app → System restarts
  │
  └─ Error
      └─ → [UPDATE_AVAILABLE] (retry)
```

---

## 🧵 Threading Model

```
Main Thread:
├─ launcher.py entry
├─ Auto-install check (blocking, very fast if no update)
├─ Flask app.run()
└─ Handle requests

Background Thread (from launcher):
└─ UpdateChecker.check_for_updates() (if auto-install needed)
   └─ Downloads installer
   └─ Validates
   └─ Installs
   └─ Signals restart

Background Thread (from update_notifier.js):
├─ setInterval every hour
├─ fetch /api/updates/status
├─ Parse response
└─ Update UI if notification available

Background Thread (from app_update_routes.py):
├─ Monitors installation progress
├─ Updates notification status
└─ Handles completion/errors
```

---

## 📈 Update Timeline Example

```
10:00 AM - User launches app (v2.5.0)
    ├─ Checks: version.json (remote: 2.6.0)
    ├─ 2.5.0 < 2.6.0: Update available
    ├─ Download: installer.exe (5 MB, 2 sec)
    ├─ Validate: checksum matches ✅
    ├─ Install: copy binary, create shortcuts (1 sec)
    ├─ Restart: app exits and relaunches
    └─ 10:00:10 - App running (v2.6.0)
       └─ User sees: "Update Complete!"

10:02 AM - User still using app (v2.6.0)
    └─ No action needed ✅

11:00 AM - Background check runs
    ├─ Checks: version.json (still 2.6.0)
    ├─ Already latest: no notification
    └─ Continue running

Next day 12:01 PM - New release (v2.7.0)
    ├─ App startup check
    ├─ 2.6.0 < 2.7.0: Update available
    ├─ Same process as before
    ├─ OR user sees banner during use
    └─ User clicks "Install Update"
       └─ Same download + install process
           └─ App restarts (v2.7.0)
```

---

## ✅ Complete Feature Matrix

| Feature | Implemented | Auto | Manual | Visible |
|---------|-------------|------|--------|---------|
| Check for updates | ✅ | ✅ | ✅ | ❌ (silent) |
| Download | ✅ | ✅ | ✅ | ✅ (status) |
| Validate checksum | ✅ | ✅ | ✅ | ❌ (silent) |
| Install | ✅ | ✅ | ✅ | ✅ (banner) |
| Restart | ✅ | ✅ | ✅ | ✅ (message) |
| Notification | ✅ | ❌ | ✅ | ✅ |
| Periodic check | ✅ | ✅ | ❌ | ❌ |
| Manual trigger | ✅ | ❌ | ✅ | ✅ |
| Error handling | ✅ | ✅ | ✅ | ✅ |
| Rollback | ✅ | ✅ | ✅ | ✅ |

---

**This system provides a complete, secure, and user-friendly automatic update experience.** ✨
