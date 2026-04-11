---
name: Development Checklist & Best Practices
description: Security, testing, and code quality requirements before commit
type: feedback
---

## Pre-Commit Checklist

- [ ] **No new runtime errors** — check logs for `logger.exception()`
- [ ] **CSRF in all POST forms** — `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`
- [ ] **ACL verified** — `can_write(client_id, user)` before updates
- [ ] **History logged** — `log_history(action, user, client_id, details)` after changes
- [ ] **No hardcoded secrets** — use `cfg_get('key')` for config
- [ ] **No sensitive data in logs** — no passwords, tokens, emails (unless DEBUG)
- [ ] **Tested locally** — `python app.py`, tested in browser
- [ ] **No new dependencies** — if added, run `pip freeze > requirements.txt`
- [ ] **Parameterized SQL** — use `?` placeholders, never f-strings for user input
- [ ] **No console logs left** — remove `print()` statements

---

## Security Review Checklist

### Inputs
- [ ] User input validated (email format, string length, numeric ranges)
- [ ] File uploads validated (extension, MIME type, size)
- [ ] No path traversal (`../` in filenames)

### Outputs
- [ ] HTML escaped in templates (Jinja2 auto-escapes, but check raw filters)
- [ ] JSON responses use `jsonify()`, not `json.dumps()` + raw response

### Database
- [ ] All SQL uses parameterized queries (`:?` placeholders)
- [ ] No `SELECT *` without client_id filter (info leak risk)
- [ ] Foreign key constraints enforced (ON DELETE CASCADE as needed)

### Auth & Sessions
- [ ] Login locked after N failures (rate-limiting enabled)
- [ ] Passwords hashed (PBKDF2 via `hash_pwd()`)
- [ ] Session timeout enforced (8h default)
- [ ] HttpOnly cookies used (no JS access to tokens)

### Multi-Client
- [ ] ACL checked before every read/write
- [ ] User cannot escalate own role
- [ ] Shared clients properly scoped (client_partages table enforced)

---

## Code Quality Checklist

### Style
- [ ] PEP 8 (4-space indents, snake_case functions)
- [ ] Type hints optional but appreciated in complex functions
- [ ] Docstrings for non-obvious functions
- [ ] Comments only for "why", not "what" (code is self-documenting)

### Functions
- [ ] Single responsibility (one thing per function)
- [ ] Parameters < 4 (or use dict)
- [ ] Error handling for external resources (DB, network, files)
- [ ] Return early to reduce nesting

### Tests
- [ ] Happy path tested manually in UI
- [ ] Error cases tested (401/403/404, DB disconnects)
- [ ] Edge cases (empty lists, null values, boundary conditions)

---

## Logging Best Practices

**Good**:
```python
logger.info(f"User {user_id} created device {device_id}")
logger.warning(f"Scan timeout for network {network_ip}")
logger.exception("Failed to parse scan results")  # Logs stack trace
```

**Bad**:
```python
logger.info(f"Password: {pwd}")  # ‼️ Never log secrets
logger.info(f"Email: {user_email}")  # PII
logger.info("Done")  # Too vague
print(f"Debug: {value}")  # Use logger.debug() instead
```

---

## Template Best Practices

**Safe rendering**:
```jinja2
{# Jinja2 auto-escapes by default #}
<h1>{{ device.nom }}</h1>  {# HTML-safe #}

{# If you need raw HTML, use safe filter carefully #}
<div>{{ description | safe }}</div>  {# Only for trusted content #}
```

**Avoid**:
```jinja2
{# Never put user input in URLs without escaping #}
<a href="/devices?name={{ search_term }}">  {# ✗ XSS risk #}

{# Always use url_for() #}
<a href="{{ url_for('device_detail', id=device_id) }}">  {# ✓ Safe #}
```

---

## Testing Workflow

```bash
# 1. Run dev server
python app.py
# → http://localhost:5000

# 2. Test scenarios
# - Login (valid/invalid credentials)
# - Multi-client isolation (switch clients, verify no leaks)
# - CRUD operations (create/read/update/delete)
# - Upload files (various formats)
# - Network scan (if root/admin on system)
# - Rate-limiting (5 failed logins, then blocked)

# 3. Check logs
# Open terminal where python app.py runs → see [INFO] / [ERROR] messages

# 4. Inspect DB
sqlite3 parc_info.db
> SELECT * FROM auth_users LIMIT 1;
> SELECT * FROM histories WHERE user_id=1 ORDER BY date DESC LIMIT 5;
```

---

## Common Issues & Fixes

| Issue | Check |
|-------|-------|
| "CSRF token missing" | Form has hidden input with csrf_token? |
| "Forbidden" (403) | User has access to this client? (client_partages) |
| "No such table" | DB initialized? (`init_db()` called?) |
| "Port already in use" | Change `FLASK_PORT` env var or restart Flask |
| Slow queries | Check for missing indexes (id, client_id, user_id) |
| File not uploading | Check UPLOAD_FOLDER permissions, file size limit |
| Can't connect to Turso | URL/token correct? Network accessible? |

---

## Build & Distribution

```bash
# 1. Install PyInstaller
pip install pyinstaller pillow pystray

# 2. Build executable
cd parc_info
pyinstaller parcinfo.spec

# 3. Output
dist/ParcInfo.exe              # ~25-40 MB
dist/ParcInfo.app (macOS)      # Drag to /Applications

# 4. Test executable
./dist/ParcInfo.exe            # Should auto-open browser
                               # Data persists in parc_info.db next to exe
```

---

## Notes for Claude (Future Sessions)

- This is a mature, functional project — focus on **security & ACL**, not refactoring
- Multi-client isolation is **critical** — always verify ACL before reads/writes
- CSRF protection is **mandatory** — every form must have token
- History logging is **audit-trail** — don't skip after modifications
- PyInstaller distribution is **critical use case** — test as .exe, not just `python app.py`
