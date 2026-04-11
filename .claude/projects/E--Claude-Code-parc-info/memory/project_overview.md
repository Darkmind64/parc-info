---
name: ParcInfo Project Overview
description: Flask IT inventory management system with multi-client support, network scanning, and PyInstaller distribution
type: project
---

## Core Purpose
ParcInfo manages IT assets for organizations:
- Device inventory (hardware, peripherals, services)
- Contract tracking, warranties, network scanning
- Multi-client support with role-based ACL
- Distributable as portable executable (.exe/.app)

## Tech Stack
- **Backend**: Python 3.8+, Flask 3.0, SQLite (+ optional Turso serverless)
- **Frontend**: Jinja2 templates, vanilla JavaScript
- **Distribution**: PyInstaller (produces .exe/.app)
- **Database**: SQLite locally; can switch to Turso via config

## Key Files
- `app.py` — Flask routing, sessions, CSRF, auth middleware
- `database.py` — SQLite/Turso connection, utilities
- `models.py` — SQLAlchemy ORM (optional, coexists with raw SQL)
- `auth_utils.py` — Password hashing, CSRF tokens, rate-limiting
- `config_helpers.py` — Persistent configuration (JSON dict)
- `client_helpers.py` — Business logic, ACL checks, formatting
- `templates/` — Jinja2 HTML (login, dashboard, devices, contracts, admin, scan)
- `static/js/` — Vanilla JS (form validation, list filtering, sorting)

## Core Features
1. **Authentication**: Login → PBKDF2 hash → 8h session, HttpOnly/SameSite
2. **Multi-client ACL**: Each user has role (admin/user) + per-client access
3. **Device Management**: CRUD devices with IP, MAC, warranty, contracts
4. **Network Scan**: Async discovery via ping/arp/port scan
5. **Uploads**: Documents (PDF, images) attached to devices/contracts
6. **History**: Every action logged (user, client, timestamp, details)

## Deployment
- **Dev**: `python app.py` → http://localhost:5000
- **Production**: `pyinstaller parcinfo.spec` → dist/ParcInfo.exe (25-40MB)
  - Database & uploads stored **next to executable** (persistent across updates)
  - Optional tray launcher with auto-browser open

**Key distinction**: Single-file portable app but data lives outside in `parc_info.db` + `uploads/`
