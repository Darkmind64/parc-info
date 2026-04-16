# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-04-13

### Added
- **Multi-Client IT Asset Management System** — Core application for managing device inventory, contracts, peripherals, and users in a multi-client environment with strict ACL isolation
- **Network Scanning** — Automatic device discovery via ping, ARP, and TCP port scanning with real-time results
- **Role-Based Access Control (RBAC)** — Three-tier access system (proprietaire/ecriture/lecture) with granular client-level permissions
- **Audit Trail** — Complete history of all modifications with user, timestamp, and change details
- **Authentication System** — PBKDF2-based password hashing with 8-hour session timeout and rate-limiting (10 attempts/5 minutes)
- **Security Features** — CSRF protection, SQL injection prevention via parameterized queries, XSS protection via Jinja2 auto-escaping
- **Dashboard & Reporting** — Customizable widget-based dashboard with metrics, device counts, and service status
- **Document Management** — Support for PDF and image uploads linked to devices and contracts
- **Contract Tracking** — Manage maintenance, support, and SaaS contracts with expiration tracking
- **Device Management** — Complete CRUD for computers, laptops, servers, and peripherals with detailed specifications
- **Configuration Management** — Persistent configuration storage with customizable lists (device types, categories, etc.)
- **PyInstaller Support** — Standalone executable distribution (.exe/.app) with auto-detected free port and browser launch
- **Docker Support** — Dockerfile and docker-compose.yml for containerized deployment
- **Database Options** — SQLite for local deployment, optional Turso serverless database support
- **Comprehensive Documentation** — Detailed CLAUDE.md development guide, user README, and security checklist

### Bug Fixes
- Fixed widget rendering to ensure all default widgets appear correctly even with legacy configurations
- Fixed missing widgets and visibility issues on dashboard initialization
- Resolved widget state management and spacing issues
- Applied consistent flex card structure to all widgets for unified layout
- Fixed KPI width display and widget height resizing functionality

### Known Limitations
- File upload validation (extension/MIME whitelist) is basic — recommended hardening for production
- Rate-limiting is memory-based (resets on server restart)
- Password encryption for stored credentials requires implementation for production deployment
- Scan network feature requires elevated privileges (Linux/macOS sudo, Windows admin mode) for ARP discovery

### Security Considerations
- ✅ PBKDF2+SHA256 password hashing (Werkzeug)
- ✅ Timing-safe CSRF validation
- ✅ Parameterized SQL queries (SQLite3 ? placeholders)
- ✅ HttpOnly + SameSite session cookies
- ✅ Multi-client ACL enforced before all write operations
- ✅ Audit trail for compliance and forensics
- ⚠️ Uploads require whitelisting extension + MIME types in production
- ⚠️ Credential storage recommended to use database encryption (AES-256) in production

### Installation & Setup

#### Development
```bash
python -m venv venv
source venv/bin/activate  # Linux/macOS or venv\Scripts\activate on Windows
pip install -r requirements.txt
python app.py
# → http://127.0.0.1:5000
```

#### Docker
```bash
docker-compose up
# → http://localhost:5000
```

#### Standalone Executable
```bash
pip install pyinstaller pillow pystray
pyinstaller parcinfo.spec
./dist/ParcInfo.exe  # Windows
open dist/ParcInfo.app  # macOS
```

### Upgrade Instructions

**From Previous Versions:**
1. Backup your database: `cp parc_info.db parc_info.db.bak`
2. Update application files (replace Python source)
3. Database schema migrations are applied automatically on startup via `init_db()`
4. No manual schema migration required for SQLite deployments

### API Reference

See [CLAUDE.md](CLAUDE.md) for:
- Complete API function signatures
- Database schema definition
- Security checklist
- Development patterns and examples
- Architecture decisions

### Contributors

Initial release by ParcInfo Team

---

## Roadmap

### v1.1.0 (Planned)
- [ ] Database encryption at rest
- [ ] Advanced search and filtering
- [ ] Export to PDF/Excel reports
- [ ] Mobile-responsive improvements
- [ ] Two-factor authentication (2FA)
- [ ] API documentation (OpenAPI/Swagger)

### v2.0.0 (Future)
- [ ] PostgreSQL/MySQL backend support
- [ ] Real-time WebSocket notifications
- [ ] Slack/Teams integration
- [ ] Inventory forecasting
- [ ] Hardware lifecycle tracking
