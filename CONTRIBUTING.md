# Contributing to ParcInfo

Thank you for your interest in contributing to ParcInfo! This document provides guidelines for reporting bugs, suggesting features, and submitting pull requests.

## Code of Conduct

By participating in this project, you agree to be respectful and constructive in all interactions.

## How to Report a Bug

### Before Submitting
- Check existing [Issues](../../issues) to avoid duplicates
- Test with the latest version from `main` branch
- Gather information:
  - Python version (`python --version`)
  - OS (Windows/macOS/Linux)
  - How to reproduce the issue step-by-step
  - Expected vs actual behavior
  - Error messages or logs

### Submitting a Bug Report
1. Click **New Issue** → Select **Bug Report** template
2. Provide clear title and description
3. Use the template fields (reproduction steps, screenshots, logs)
4. Reference any related issues with `#123`

**Example:**
```markdown
## Description
Dashboard widgets disappear after login with old browser cookies.

## Steps to Reproduce
1. Login to ParcInfo
2. Clear browser cache partially
3. Refresh dashboard
4. Widgets are missing

## Expected Behavior
All widgets should display correctly.

## Environment
- Python: 3.10
- OS: Windows 11
- Browser: Chrome 120
```

## How to Suggest a Feature

### Before Submitting
- Check existing [Discussions](../../discussions) and [Issues](../../issues)
- Ensure the feature aligns with project scope (IT asset management)

### Submitting a Feature Request
1. Click **New Issue** → Select **Feature Request** template
2. Describe the problem/use case you're solving
3. Propose a solution or implementation approach
4. List any potential alternatives

**Example:**
```markdown
## Problem
Users can't easily export device inventory to Excel for reports.

## Proposed Solution
Add export button on device list to generate XLSX file with selected columns.

## Alternatives
- CSV export (simpler but less formatting)
- PDF report (more complex but printable)
```

## Development Setup

### Prerequisites
- Python 3.8+ (`python --version`)
- Git

### Local Installation

```bash
# 1. Clone the repository
git clone https://github.com/YOUR-USERNAME/parc-info.git
cd parc-info

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/macOS
# or
venv\Scripts\activate     # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) Install dev dependencies
pip install pyinstaller pillow pystray

# 5. Run the application
python app.py
# → Visit http://127.0.0.1:5000 in your browser
```

### First Run
- Database will be created automatically: `parc_info.db`
- Default login credentials are in `app.py:init_db()` (view source for initial setup)

## Code Style & Conventions

### Python

**General Rules**
- Follow [PEP 8](https://pep8.org/) style guide
- Use clear, descriptive variable names
- Keep functions under 50 lines or comment non-obvious logic
- No wildcard imports (`from module import *`)

**Key Patterns** (see [CLAUDE.md](CLAUDE.md) for details)

```python
# ✅ Parameterized SQL (ALWAYS)
conn.execute('SELECT * FROM appareils WHERE client_id=? AND nom=?', 
             (client_id, search_term))

# ❌ String interpolation (NEVER)
conn.execute(f'SELECT * FROM appareils WHERE client_id={client_id}')

# ✅ ACL check before write
if not can_write(client_id):
    return jsonify({'error': 'Forbidden'}), 403

# ✅ Audit trail after modification
log_history('UPDATE_APPAREIL', user['login'], client_id, 
            {'field': new_value})

# ✅ Form validation
errors = validate_form([
    ('nom', 'str', True),
    ('ip', 'ip', False),
], request.form)
if errors:
    return jsonify({'errors': errors}), 400
```

**Security Checklist for Code**
- [ ] CSRF token in every POST/PUT/DELETE form
- [ ] SQL queries use `?` parameters (never f-strings)
- [ ] ACL verified with `can_write(client_id)` before writes
- [ ] Form input validated with `validate_form()`
- [ ] Audit trail logged with `log_history()`
- [ ] No PII (passwords, tokens) in logs
- [ ] No hardcoded secrets (use environment variables)

### Templates (Jinja2)

```html
<!-- ✅ Auto-escaped (safe) -->
<h1>{{ device.name }}</h1>

<!-- ✅ Safe URL generation -->
<a href="{{ url_for('device_detail', id=device.id) }}">View</a>

<!-- ✅ CSRF token in forms (REQUIRED) -->
<form method="POST">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <input type="text" name="nom" required>
  <button type="submit">Save</button>
</form>

<!-- ❌ Avoid raw HTML (unless sanitized) -->
<div>{{ description | safe }}</div>
```

## Making a Pull Request

### Before Submitting
1. **Create a feature branch** off `main`
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** and commit regularly
   ```bash
   git add .
   git commit -m "Brief description of changes"
   ```

3. **Test your changes**
   ```bash
   python app.py
   # Test the feature manually in browser
   ```

4. **Check for security issues**
   - Verify CSRF tokens in forms
   - Check ACL validation before writes
   - Confirm SQL uses parameterized queries
   - No hardcoded secrets

5. **Keep commits clean**
   - Use descriptive commit messages
   - Reference issues: `Fixes #123`
   - One feature per PR

### Submitting a PR

1. Push your branch
   ```bash
   git push origin feature/your-feature-name
   ```

2. Click **New Pull Request** and use the template
3. Link related issues with `Fixes #123` or `Relates to #456`
4. Describe what changed and why
5. Request review from maintainers

**PR Title Example:** `feat: Add device export to Excel`

**PR Description Example:**
```markdown
## Description
Adds ability to export device list to Excel file with selected columns.

## Changes
- Added `/api/devices/export` endpoint
- New `export_to_excel()` helper in client_helpers.py
- Added export button to device list UI
- Includes filtering by device type and status

## Testing
- [x] Manual test with 100 devices
- [x] Tested with different filters
- [x] Verified exported file format

## Checklist
- [x] Code follows style guide
- [x] CSRF tokens in forms
- [x] ACL validated before operations
- [x] Audit trail logged
- [x] No hardcoded secrets
- [x] Tested in browser
```

## Running Tests

Currently, ParcInfo has manual testing via browser. Automated test contributions are welcome!

```bash
# Manual testing workflow
python app.py

# Test these scenarios:
# 1. Login/logout functionality
# 2. Device CRUD operations
# 3. Contract management
# 4. Multi-client ACL (different users/roles)
# 5. Document upload/download
# 6. Network scan
```

## Documentation

- **User Guide:** See [README.md](README.md)
- **Architecture & API:** See [CLAUDE.md](CLAUDE.md)
- **Code Examples:** Look for docstrings and comments in source files

When adding features, update relevant sections in documentation.

## Reporting Security Issues

**Do NOT open a public issue for security vulnerabilities.**

Email security concerns directly to maintainers with:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Proposed fix (optional)

We'll work with you to fix and credit the discovery.

## Branch Strategy

- **main** — Stable releases, protected branch
- **feature/** — New features
- **bugfix/** — Bug fixes
- **docs/** — Documentation updates

## Questions?

- Check [CLAUDE.md](CLAUDE.md) for architecture decisions
- Search existing [Issues](../../issues) and [Discussions](../../discussions)
- Open a Discussion for questions (not bugs)

---

**Thank you for contributing to ParcInfo! 🚀**
