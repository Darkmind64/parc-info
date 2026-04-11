---
name: Architecture & Development Conventions
description: Code patterns, security practices, ACL, and key utility functions
type: feedback
---

## Architectural Patterns

**Layered architecture**:
1. **HTTP layer** (Flask routes in app.py): handle requests, sessions, CSRF validation
2. **Utilities layer**: database.py, auth_utils.py, config_helpers.py, client_helpers.py
3. **Templates + static**: Jinja2 rendering, vanilla JS (no SPA framework)

**Data access**:
- Use `get_db()` → returns raw `sqlite3.Connection` or Turso proxy
- Convert results with `row_to_dict(row)` for dict output
- No forced ORM; models.py optional, coexists with raw SQL

**No dependency on framework migrations**: Schema created once by `init_db()` in database.py; future migrations add `nullable=True` columns

---

## Security Conventions — **CRITICAL**

### Authentication
- Hash: `auth_utils.hash_pwd(plaintext)` uses PBKDF2
- Verify: `auth_utils.check_pwd(plaintext, hash)` is timing-safe
- Session: 8h lifetime, HttpOnly, SameSite=Lax (hardened in app.py lines 81–83)

### CSRF Protection
- **Every POST form** must include hidden input: `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`
- Server validates: `validate_csrf_request()` before processing
- JS fetch requires: `headers: {'X-CSRF-Token': csrf}` or body param
- **Why**: Prevents cross-origin form submissions.

### Multi-Client ACL
- **Check before every write**: `if not can_write(client_id, user, edit=True): return 403`
- **Check before every read**: `can_read = get_client_access(user_id, client_id) in ['r', 'rw']`
- **Never SELECT without client filter** (info leak risk)
- **Why**: Multi-tenant — strict boundary enforcement.

### Rate-Limiting
- Login attempts: max 5 fails in 1 hour, then blocked
- Function: `check_rate_limit(login, max_attempts=5, window=3600)`
- Stored in `failed_login_attempts` table
- **Why**: Brute-force mitigation.

### Uploads
- **Current**: No extension validation (accepts any file)
- **Risk**: Malicious executables, XXE in XML, etc.
- **Mitigated by**: Stored outside static/ (not executable), served via `send_from_directory()` with attachment headers
- **In production**: Add whitelist (pdf, jpg, png, docx, xlsx) + MIME validation

---

## Logging & Tracing

**Always call after modifications**:
```python
log_history(
    action='CREATE_APPAREIL',      # Action string
    user=user['login'],             # Username
    client_id=client_id,            # Which client
    details={'device_id': 123}      # What changed
)
```

Stored in `histories` table, helps audit trail.

---

## Key Utility Functions

### `database.py`
- `get_db()` — connection (auto-selects SQLite or Turso)
- `row_to_dict(row)` — sqlite3.Row → dict
- `_local_db()` — always SQLite (ignores Turso config)

### `auth_utils.py`
- `hash_pwd(plaintext)` — PBKDF2 hash
- `check_pwd(plaintext, hash)` — timing-safe verify
- `get_auth_user()` — fetch session user from DB
- `login_required(f)` — decorator, redirects if no session
- `validate_csrf_request()` — raises on CSRF fail
- `check_rate_limit(login, ...)` — blocks after N failures
- `get_csrf_token()` — returns session token for templates
- `validate_form(data, rules)` — form validation helper

### `config_helpers.py`
- `cfg_get(key, default)` — read persistent config
- `cfg_set(key, value)` — write persistent config (stored as JSON in DB)
- `cfg_all()` — fetch all config as dict
- `cfg_invalidate()` — clear in-memory cache

### `client_helpers.py`
- `can_write(client_id, user, edit=False)` — ACL check for write/edit
- `get_client_access(user_id, client_id)` — returns 'r', 'rw', or None
- `paginate(page, per_page, items)` — splits list, returns Pagination obj
- `log_history(action, user, client, details)` — audit trail
- `garantie_active(date_fin)` — warranty still valid?
- `human_size(bytes)` — format as B/KB/MB/GB
- `fmt_appareils(rows)` — format device rows for display
- `fmt_garantie_periph(periph)` — format peripheral warranty
- `fmt_contrat(contract)` — format contract dates

---

## Template Conventions

**Base template** (`templates/base.html`):
- Provides nav, footer, auth context
- Child templates extend it: `{% extends "base.html" %}`

**Auth context injected globally**:
```jinja2
{% if auth_user %}
  <span>{{ auth_user.nom }} ({{ auth_user.role }})</span>
{% endif %}
```

**CSRF in every form**:
```jinja2
<form method="POST">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  ...
</form>
```

**Pagination**:
```jinja2
{% set page = request.args.get('page', 1, type=int) %}
{% if pagination.has_prev %}<a href="?page={{ pagination.prev_page }}">Back</a>{% endif %}
```

---

## Development Flow

1. **New route** → add to `app.py` with `@app.route()` and `@login_required`
2. **New business logic** → extract to `client_helpers.py` or new util
3. **New template** → create in `templates/`, extend `base.html`
4. **Database change** → raw SQL `conn.execute()` (no migrations yet)
5. **Testing** → `python app.py`, log in, test in browser
6. **Commit checklist**:
   - ✓ CSRF in all POST forms
   - ✓ ACL checks in all writes
   - ✓ `log_history()` after modifications
   - ✓ No sensitive data in logs
   - ✓ No new untracked dependencies
   - ✓ Tested locally

---

## Database Notes

- **Single-file SQLite**: `parc_info.db` (auto-created if missing)
- **Turso optional**: Set `db_type='turso'` + url/token in config, then `get_db()` routes to cloud
- **Schema**: Defined in `database.py:init_db()` — tables created once, no migration framework yet
- **Querying**: Raw SQL with parameterized queries (sqlite3 module)
  ```python
  conn = get_db()
  rows = conn.execute('SELECT * FROM appareils WHERE client_id=?', (client_id,))
  for row in rows:
      device = row_to_dict(row)
      print(device)
  conn.close()
  ```

---

## PyInstaller / Distribution

- **Spec file**: `parcinfo.spec` (defines what gets bundled)
- **Build command**: `pyinstaller parcinfo.spec` → `dist/ParcInfo.exe`
- **Data isolation**: app.py detects `sys.frozen` state, paths adjust accordingly
- **First-run**: DB auto-initialized, `secret.key` generated
- **Updates**: Can replace .exe without losing `parc_info.db` or `uploads/`

---

## Common Gotchas

1. **Forget ACL check** → info disclosure across clients
   - Always: `if not can_write(client_id, user): return 403`

2. **No CSRF in form** → cross-origin attack
   - Every `<form method="POST">` needs hidden csrf_token input

3. **Skip log_history()** → audit trail gaps
   - After every data modification, call `log_history(...)`

4. **Raw request.form without validation** → injection attacks
   - Use `validate_form(data, rules)` helper

5. **Turso connection secrets in code** → exposure
   - Always use `cfg_get('turso_token')` from config DB
