# Update Integration Guide

Complete setup for automatic update installation + in-app notifications.

---

## 🎯 What's New

### Auto-Install on Startup
- ✅ Check for updates **every time app starts**
- ✅ Download + install automatically (silently, no user interaction)
- ✅ Restart app after update complete

### In-App Notifications
- ✅ Banner notification when update is available
- ✅ "Install Now" button for immediate update
- ✅ "Dismiss" to hide notification
- ✅ "Installing..." status during update
- ✅ Periodic background checks (every hour)

---

## 📦 Files Modified/Created

### Modified
- `launcher.py` — Auto-install at startup
- `update_checker.py` — Added `check_and_install_updates()` method

### Created
- `update_notifier.py` — Notification manager for Flask
- `app_update_routes.py` — Flask routes for update API
- `static/js/update_notifier.js` — UI notifications (JavaScript)

---

## 🔧 Integration Steps

### Step 1: Import Update Routes in `app.py`

Add at the **top** of `app.py` (after Flask imports):

```python
from app_update_routes import register_update_routes
```

Then register routes after creating the Flask app:

```python
app = Flask(__name__)
# ... other app config ...

# Register update notification routes
register_update_routes(app)
```

### Step 2: Add HTML Container in Base Template

Add this to `templates/base.html` (in `<body>`, near the top):

```html
<!-- Update Notification Container -->
<div id="update-notification-container"></div>
```

### Step 3: Include JavaScript in Base Template

Add this to `templates/base.html` (before `</body>`):

```html
<!-- Update Notifier Script -->
<script src="{{ url_for('static', filename='js/update_notifier.js') }}"></script>
```

That's it! ✅

---

## 🚀 Behavior

### On Application Start

```
launcher.py starts
  ↓
Checks for updates (auto-install mode)
  ↓
If update available:
  ├─ Downloads silently
  ├─ Validates checksum
  ├─ Installs silently
  └─ Restarts app
     
If no update:
  └─ Continues to Flask app
```

### While Using Application

```
Browser loads app
  ↓
JavaScript loads update_notifier.js
  ↓
Periodic checks (every hour) for updates
  ↓
If update found:
  ├─ Shows banner: "📦 ParcInfo 2.6.0 is available"
  ├─ User sees "Install Update" button
  └─ User can:
      ├─ Click "Install Update" → downloads + installs + restarts
      └─ Click "Dismiss" → hides notification
```

---

## 📡 API Endpoints

### `GET /api/updates/status`
Get current update status.

**Response:**
```json
{
  "checking": false,
  "update_available": true,
  "version": "2.6.0",
  "current_version": "2.5.0",
  "notification": {
    "type": "update_available",
    "message": "ParcInfo 2.6.0 is available",
    "version": "2.6.0"
  }
}
```

### `POST /api/updates/check`
Check for updates immediately.

**Response:**
```json
{
  "status": "check_complete",
  "update_available": true,
  "version": "2.6.0",
  "notification": {...}
}
```

### `POST /api/updates/install`
Install update immediately.

**Response:**
```json
{
  "status": "installing",
  "message": "Update installation started",
  "version": "2.6.0",
  "notification": {
    "type": "installing",
    "message": "Updating ParcInfo... Application will restart."
  }
}
```

### `POST /api/updates/dismiss`
Dismiss notification.

**Response:**
```json
{
  "status": "dismissed",
  "notification": null
}
```

---

## 🎨 Customization

### Change Auto-Check Interval

Edit `update_notifier.py`:

```python
class UpdateNotifier:
    CHECK_INTERVAL_SECONDS = 3600  # Change to desired value
    # 3600 = 1 hour
    # 1800 = 30 minutes
    # 300 = 5 minutes
```

### Change Notification Appearance

Edit `static/js/update_notifier.js` → `defaultTemplate()` method.

Example: Change banner color

```javascript
bgColor = '#d1ecf1';  // Light blue
borderColor = '#17a2b8';  // Dark blue
```

### Disable Auto-Install on Startup

Edit `launcher.py`, comment out:

```python
# if checker.check_and_install_updates(force=True, silent=True):
#     ...
```

---

## 🔐 Security

✅ SHA256 checksum validation
✅ HTTPS for downloads
✅ Silent installation (no user interaction needed)
✅ Automatic restart on completion

---

## 🐛 Troubleshooting

### Auto-update not happening on startup

**Check:** Does `version.json` exist on GitHub?
```bash
curl https://raw.githubusercontent.com/YOUR_REPO/master/version.json
```

### Notification not showing in app

**Check:**
1. Is `update_notifier.js` loaded? (Browser DevTools → Console)
2. Is `<div id="update-notification-container"></div>` in `base.html`?
3. Are update routes registered? (See Step 1)

### Update downloads but won't install

**Check:**
1. Is installer available? (Check `version.json` downloads URLs)
2. Is checksum correct? (Compare `version.json` vs actual file)

---

## 📝 Testing

### Test Auto-Install on Startup

```bash
# 1. Build current version
python build.py

# 2. Update version.json with newer version (manually)
# Edit version.json: change "version": "2.5.0" → "2.6.0"

# 3. Create dummy installer at version URL location
# (or change URL to point to a real newer installer)

# 4. Run app
dist/ParcInfo.exe

# 5. App should check, find update, download, and restart
```

### Test In-App Notification

```bash
# 1. Manually call API
curl -X POST http://localhost:5000/api/updates/check

# 2. Check response
# Should show notification in browser UI
```

### Test User Actions

1. Click "Install Update" → app restarts
2. Click "Dismiss" → notification disappears
3. After 1 hour → auto-check runs and shows notification again

---

## 📖 Summary

### Before Integration
- Manual checks only
- No in-app notifications
- User must manually download + install

### After Integration
- ✅ Auto-install on every startup
- ✅ In-app banner notifications
- ✅ Periodic background checks
- ✅ One-click install from banner
- ✅ Automatic restart after update

---

## 🎯 Complete Workflow Example

```python
# launcher.py (startup)
→ check_and_install_updates(force=True, silent=True)
  → Downloads installer for v2.6.0
  → Validates SHA256
  → Installs (replaces executable)
  → Restarts app
    → New version loads
      → Browser shows banner: "Update installed!"

# During use
→ Every hour: check for updates
  → If found: show banner with "Install Update"
  → User clicks "Install Update"
    → Same process as above
    → App restarts
```

---

**Everything is automatic and transparent to the user!** ✅

Next step: Test the integration locally, then release v2.6.0 🚀
