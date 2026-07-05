# Implementation Checklist — Auto-Update System

Step-by-step checklist to integrate the auto-update system.

---

## ✅ Phase 1: Understanding (5 min)

- [ ] Read `AUTO_UPDATE_SUMMARY.md`
- [ ] Read `ARCHITECTURE.md` (understand the system)
- [ ] Review `UPDATE_INTEGRATION_GUIDE.md`

---

## ✅ Phase 2: Code Integration (10 min)

### Edit `app.py`

- [ ] Find: `from flask import Flask`
- [ ] Add after Flask imports:
  ```python
  from app_update_routes import register_update_routes
  ```

- [ ] Find: `app = Flask(__name__)`
- [ ] Add after Flask app creation:
  ```python
  # Register update notification routes
  register_update_routes(app)
  ```

- [ ] (Optional) Add context processor:
  ```python
  @app.context_processor
  def inject_update_status():
      from update_notifier import get_notifier
      try:
          notifier = get_notifier()
          return {
              'update_available': notifier.update_available,
              'update_version': notifier.update_version,
          }
      except Exception:
          return {
              'update_available': False,
              'update_version': None,
          }
  ```

### Edit `templates/base.html`

- [ ] Find: `<body>`
- [ ] Add right after opening `<body>` tag:
  ```html
  <!-- Update Notification Container -->
  <div id="update-notification-container"></div>
  ```

- [ ] Find: `</body>` (closing tag)
- [ ] Add before closing `</body>`:
  ```html
  <!-- Update Notifier Script -->
  <script src="{{ url_for('static', filename='js/update_notifier.js') }}"></script>
  ```

---

## ✅ Phase 3: Verify Files Exist (5 min)

- [ ] `update_checker.py` — exists ✅
- [ ] `update_notifier.py` — exists ✅
- [ ] `app_update_routes.py` — exists ✅
- [ ] `static/js/update_notifier.js` — exists ✅
- [ ] `__version__.py` — exists ✅
- [ ] `launcher.py` — modified ✅

---

## ✅ Phase 4: Test Locally (15 min)

### Build the Application

```bash
python build.py
```

- [ ] Build completes without errors
- [ ] `dist/ParcInfo.exe` created ✅
- [ ] `dist/installer.exe` created ✅

### Test Auto-Install on Startup

```bash
dist/ParcInfo.exe
```

- [ ] App starts
- [ ] Check logs for: `"Checking for updates..."`
- [ ] If no update available: app launches normally ✅
- [ ] Browser opens automatically ✅
- [ ] App is responsive ✅

### Test In-App Notification

While app is running:

- [ ] Open browser console (F12)
- [ ] Run:
  ```javascript
  fetch('/api/updates/status').then(r => r.json()).then(console.log)
  ```
- [ ] Should see response with update status ✅

### Test Notification Banner

- [ ] Look for banner in app (if update available)
- [ ] Banner shows: "📦 ParcInfo X.X.X is available"
- [ ] "Install Update" button visible ✅
- [ ] "Dismiss" button visible ✅

### Test Manual Check (Optional)

- [ ] Add route to app.py:
  ```python
  @app.route('/test-update-check')
  def test_check():
      from update_notifier import get_notifier
      notifier = get_notifier()
      return jsonify(notifier.status)
  ```
- [ ] Visit: `http://localhost:5000/test-update-check`
- [ ] See JSON response with update status ✅

---

## ✅ Phase 5: Create Release (5 min)

### Prepare Release

- [ ] Update `__version__.py`:
  ```python
  __version__ = "2.6.0"
  ```

- [ ] Commit:
  ```bash
  git add __version__.py
  git commit -m "Release v2.6.0"
  git tag v2.6.0
  git push origin v2.6.0
  ```

### Generate Checksums

```bash
python build/generate_version_json.py 2.6.0
```

- [ ] `version.json` created with checksums ✅
- [ ] `downloads` section has URLs ✅
- [ ] `checksums` section has SHA256 hashes ✅

### Create GitHub Release

```bash
gh release create v2.6.0 \
  dist/ParcInfo.exe \
  dist/installer.exe \
  version.json
```

- [ ] Release created on GitHub ✅
- [ ] All 3 files uploaded ✅

---

## ✅ Phase 6: Verify Release (5 min)

### Check version.json on GitHub

```bash
curl https://raw.githubusercontent.com/YOUR_USER/parc_info/master/version.json
```

- [ ] Returns valid JSON ✅
- [ ] Contains downloads URLs ✅
- [ ] Contains checksums ✅
- [ ] Version matches latest tag ✅

### Test Auto-Update from Release

- [ ] Download `installer.exe` from GitHub release
- [ ] Run it
- [ ] Should install app ✅
- [ ] Next startup should find no new updates (already latest) ✅

---

## ✅ Phase 7: Documentation (2 min)

- [ ] Share `AUTO_UPDATE_SUMMARY.md` with team
- [ ] Share `UPDATE_INTEGRATION_GUIDE.md` with developers
- [ ] Share `ARCHITECTURE.md` with architects

---

## 🎯 Summary Checklist

### Code Integration
- [ ] `app.py` imports `register_update_routes`
- [ ] `app.py` calls `register_update_routes(app)`
- [ ] `base.html` has notification container div
- [ ] `base.html` includes `update_notifier.js` script

### Files
- [ ] All update files exist (update_checker.py, etc.)
- [ ] `launcher.py` modified for auto-install
- [ ] `__version__.py` exists

### Testing
- [ ] Local build succeeds
- [ ] App starts without errors
- [ ] API endpoints respond correctly
- [ ] Notification banner appears (if update available)
- [ ] Manual buttons work (Install, Dismiss)

### Release
- [ ] `version.json` created with checksums
- [ ] GitHub release created with all files
- [ ] `version.json` accessible from GitHub raw

---

## ❌ Troubleshooting

### Notification not showing

**Checklist:**
- [ ] Is `<div id="update-notification-container"></div>` in `base.html`?
- [ ] Is `<script src="update_notifier.js"></script>` included?
- [ ] Does `/api/updates/status` return data?
- [ ] Check browser console for JS errors (F12)

**Fix:**
```html
<!-- Add to base.html -->
<div id="update-notification-container"></div>
<script src="{{ url_for('static', filename='js/update_notifier.js') }}"></script>
```

### Auto-install not triggering

**Checklist:**
- [ ] Is `launcher.py` calling `check_and_install_updates()`?
- [ ] Is `version.json` accessible from GitHub?
- [ ] Does `version.json` have valid `downloads` URLs?

**Fix:**
Check `launcher.py` has:
```python
checker.check_and_install_updates(force=True, silent=True)
```

### API endpoints 404

**Checklist:**
- [ ] Is `app_update_routes.py` imported in `app.py`?
- [ ] Is `register_update_routes(app)` called?

**Fix:**
```python
# In app.py
from app_update_routes import register_update_routes
app = Flask(__name__)
register_update_routes(app)
```

---

## 🚀 You're Ready!

**Estimated total time: 30 minutes**

After completing all phases:
- ✅ Auto-install on every startup
- ✅ In-app notifications
- ✅ Background checks every hour
- ✅ One-click install
- ✅ Automatic restart

**Users get seamless updates with zero manual interaction!** 🎉

---

## 📖 Reference Documents

- `AUTO_UPDATE_SUMMARY.md` — High-level overview
- `UPDATE_INTEGRATION_GUIDE.md` — Detailed integration
- `INTEGRATION_EXAMPLE.py` — Code examples
- `ARCHITECTURE.md` — System design
- `IMPLEMENTATION_CHECKLIST.md` — This file

---

**Need help?** See `UPDATE_INTEGRATION_GUIDE.md` for detailed steps.
