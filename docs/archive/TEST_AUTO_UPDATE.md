# Testing Auto-Update System (Phase 6)

**Objective**: Verify that the auto-update system works correctly

---

## Test 1: Verify GitHub Release

### Steps
1. Visit: https://github.com/Darkmind64/parc-info/releases/tag/v2.5.0
2. Verify all 3 files are present:
   - [ ] ParcInfo.exe (38 MB)
   - [ ] installer.exe (13.5 MB)
   - [ ] version.json

3. Check version.json is accessible from:
   ```
   https://raw.githubusercontent.com/Darkmind64/parc-info/master/version.json
   ```

4. Verify checksums match:
   - [ ] ParcInfo.exe SHA256: a98e4cfc7c73f1d816c8e6afc4a223628c9564eadaf3983990fd403131d511d7
   - [ ] installer.exe SHA256: fdba169fffda72419148050f50caf6a01495034e1a96bd4face84f7b07ec9dbf

---

## Test 2: Installation Test

### Prerequisites
- Windows 10/11 with admin privileges
- Fresh system (or VM recommended for clean test)

### Steps
1. Download `installer.exe` from GitHub release
2. Run installer.exe
3. Allow installation to Program Files
4. Verify shortcuts created:
   - [ ] Start Menu: ParcInfo entry
   - [ ] Desktop: ParcInfo shortcut

5. Verify registry (Windows):
   - [ ] Open: Settings → Apps → Apps & features
   - [ ] Should show "ParcInfo 2.5.0"

### Result
Installation should complete without errors, app ready to launch.

---

## Test 3: Startup Auto-Update Check

### Prerequisites
- ParcInfo v2.5.0 installed from installer.exe
- Internet connection active

### Steps
1. Launch ParcInfo.exe
2. Observe launcher logs (should see):
   ```
   [INFO] Checking for updates...
   ```

3. If NO newer version available:
   - [ ] App launches normally
   - [ ] No update message shown
   - [ ] Browser opens to dashboard

4. If newer version IS available (simulate by updating version.json):
   - [ ] App checks and downloads
   - [ ] Installer runs silently
   - [ ] App restarts automatically
   - [ ] New version running (verify in About dialog)

### Expected Behavior
✅ Startup check completes silently
✅ No user interaction required
✅ App continues or restarts as needed

---

## Test 4: In-App Notification

### Prerequisites
- ParcInfo v2.5.0 running
- Dashboard visible in browser

### Steps
1. Open browser console (F12)
2. Navigate to Application tab
3. Trigger update check:
   ```javascript
   fetch('/api/updates/check', {method: 'POST'})
     .then(r => r.json())
     .then(d => console.log(d))
   ```

4. Observe in console:
   - [ ] Response contains `update_available` field
   - [ ] Response contains version number
   - [ ] Response contains notification object

### Expected Behavior
✅ API endpoint responds correctly
✅ Notification data is present

### Test 5: Notification Banner

### Prerequisites
- ParcInfo running with update available (simulated)

### Steps
1. If /api/updates/check returns update_available: true
2. Notification banner should appear:
   - [ ] Location: Top of page
   - [ ] Color: Cyan accent color
   - [ ] Text: "📦 ParcInfo X.X.X is available"
   - [ ] Buttons: "Install Update" + "Dismiss"

3. Click "Install Update":
   - [ ] Status changes to "Installing..."
   - [ ] Download progress shown
   - [ ] Installation message displayed
   - [ ] "Restarting..." message shown
   - [ ] App restarts automatically

4. Click "Dismiss":
   - [ ] Banner disappears
   - [ ] Notification hidden for 24 hours

### Expected Behavior
✅ Banner appears with correct styling
✅ Buttons respond to clicks
✅ Install process completes
✅ App restarts automatically

---

## Test 6: Background Update Checks

### Prerequisites
- ParcInfo running for extended period
- No manual checks triggered

### Steps
1. Let app run for 1+ hour
2. Wait for hourly background check (JavaScript scheduler)
3. If update available during this time:
   - [ ] Notification appears without user action
   - [ ] Notification shows version number
   - [ ] User can click "Install Update"

### Expected Behavior
✅ Notification appears every hour if update available
✅ No user interaction required
✅ Seamless background operation

---

## Test 7: Checksum Validation

### Prerequisites
- Version.json with correct checksums
- Corrupted or fake installer file

### Steps
1. Manually edit version.json:
   - Change SHA256 to incorrect value
   
2. Trigger update check
3. App should:
   - [ ] Download installer
   - [ ] Calculate checksum
   - [ ] Detect mismatch
   - [ ] Show error message
   - [ ] NOT run the installer
   - [ ] Gracefully fallback

### Expected Behavior
✅ Checksum mismatch detected
✅ Corrupted file rejected
✅ Graceful error handling
✅ App continues normally

---

## Test 8: Next Release (v2.6.0)

### Steps
1. Update __version__.py:
   ```python
   __version__ = "2.6.0"
   ```

2. Build:
   ```bash
   python build.py
   ```

3. Generate metadata:
   ```bash
   python build/generate_version_json.py 2.6.0
   ```

4. Create release:
   ```bash
   git tag v2.6.0
   git push origin v2.6.0
   gh release create v2.6.0 dist/ParcInfo.exe dist/installer.exe version.json
   ```

5. Test with v2.5.0 user:
   - [ ] Notification appears within 1 hour
   - [ ] Shows version 2.6.0 available
   - [ ] User can click "Install Update"
   - [ ] Installation completes
   - [ ] App restarts with v2.6.0

### Expected Behavior
✅ Users see update notification automatically
✅ No manual download required
✅ Installation happens silently
✅ App updates to new version

---

## Success Criteria

All tests pass if:

✅ **Test 1**: Files present on GitHub with correct checksums
✅ **Test 2**: Installation completes without errors
✅ **Test 3**: Startup check runs silently
✅ **Test 4**: API endpoint returns correct data
✅ **Test 5**: Notification banner appears and responds
✅ **Test 6**: Background checks run hourly
✅ **Test 7**: Checksum validation prevents corruption
✅ **Test 8**: Next release updates automatically

---

## Troubleshooting

### Notification doesn't appear
- [ ] Check /api/updates/status endpoint
- [ ] Open browser console (F12) for JavaScript errors
- [ ] Verify update_notifier.js is loaded
- [ ] Check that version.json is accessible

### Auto-update doesn't install
- [ ] Verify version.json is on GitHub master branch
- [ ] Check that checksums are correct
- [ ] Verify download URLs point to actual files
- [ ] Check launcher.py logs for errors

### Installer doesn't run
- [ ] Verify installer.exe exists in dist/
- [ ] Check Windows admin privileges
- [ ] Review installer logs in %TEMP%

### App doesn't restart
- [ ] Check that launcher.py calls sys.exit(0)
- [ ] Verify shell integration working
- [ ] Check Windows task scheduler (if auto-start enabled)

---

## Documentation References

For more details, see:
- ARCHITECTURE.md - System design
- UPDATE_INTEGRATION_GUIDE.md - Integration steps
- RELEASE_VERIFICATION.md - Release info

---

**Test Status**: Ready to execute Phase 6

Good luck! The auto-update system is designed to be bulletproof. 🚀
