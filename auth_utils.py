"""
auth_utils.py — Authentification, CSRF, rate-limiting, validation serveur.
"""
import re, hashlib, secrets, threading, logging, time
from functools import wraps
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from flask import session, redirect, url_for, request, abort

logger = logging.getLogger('parcinfo')

# ─── HACHAGE ──────────────────────────────────────────────────────────────────

def hash_pwd(pwd: str) -> str:
    """Génère un hash sécurisé PBKDF2+SHA256+sel."""
    return generate_password_hash(pwd)


def check_pwd(pwd: str, stored_hash: str):
    """
    Vérifie un mot de passe.
    Supporte la migration transparente des anciens hashes SHA256 bruts.
    Retourne (ok: bool, needs_rehash: bool).
    """
    if len(stored_hash) == 64 and all(c in '0123456789abcdef' for c in stored_hash):
        ok = hashlib.sha256(pwd.encode()).hexdigest() == stored_hash
        return ok, ok
    return check_password_hash(stored_hash, pwd), False


# ─── SESSION ──────────────────────────────────────────────────────────────────

SESSION_TIMEOUT_HOURS = 8


def get_auth_user():
    """Retourne l'utilisateur authentifié depuis la session, ou None."""
    from database import get_db, row_to_dict
    uid = session.get('auth_user_id')
    if not uid:
        return None
    conn = get_db()
    u = conn.execute('SELECT * FROM auth_users WHERE id=? AND actif=1', (uid,)).fetchone()
    conn.close()
    return row_to_dict(u) if u else None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('auth_user_id'):
            return redirect(url_for('page_login', next=request.path))
        # Vérification du timeout (8h)
        login_time = session.get('login_time')
        if login_time:
            try:
                elapsed = datetime.utcnow() - datetime.fromisoformat(login_time)
                if elapsed > timedelta(hours=SESSION_TIMEOUT_HOURS):
                    session.clear()
                    from flask import flash
                    flash('Votre session a expiré. Veuillez vous reconnecter.', 'info')
                    return redirect(url_for('page_login', next=request.path))
            except Exception:
                pass
        return f(*args, **kwargs)
    return decorated


# ─── CSRF ─────────────────────────────────────────────────────────────────────

def get_csrf_token() -> str:
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']


def validate_csrf_request():
    """Lève une 403 si le token CSRF est absent ou invalide."""
    if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
        return
    if request.path.startswith('/static/'):
        return
    if request.path in ('/login', '/logout'):
        return
    expected = session.get('csrf_token')
    received = (request.form.get('csrf_token')
                or request.headers.get('X-CSRF-Token'))
    if not expected or not received or not secrets.compare_digest(expected, received):
        logger.warning('CSRF check failed: %s %s (ip=%s)',
                       request.method, request.path, request.remote_addr)
        abort(403)


# ─── RATE LIMITING (login) ────────────────────────────────────────────────────

_login_attempts: dict = {}
_LOGIN_MAX    = 10
_LOGIN_WINDOW = 300   # secondes
_LOGIN_LOCK   = threading.Lock()


def check_rate_limit(ip: str) -> bool:
    now = time.time()
    with _LOGIN_LOCK:
        attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW]
        _login_attempts[ip] = attempts
        return len(attempts) < _LOGIN_MAX


def record_failed_attempt(ip: str):
    with _LOGIN_LOCK:
        _login_attempts.setdefault(ip, []).append(time.time())


def reset_attempts(ip: str):
    with _LOGIN_LOCK:
        _login_attempts.pop(ip, None)


# ─── VALIDATION SERVEUR ───────────────────────────────────────────────────────

_RE_IP    = re.compile(r'^(\d{1,3}\.){3}\d{1,3}(\/\d{1,2})?$')
_RE_MAC   = re.compile(r'^([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}$')
_RE_EMAIL = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
_RE_URL   = re.compile(r'^https?://.+')


def validate_form(rules, form) -> list:
    """
    Valide un formulaire selon des règles déclaratives.
    rules = [('champ', 'type', required), ...]
      type : 'str' | 'ip' | 'mac' | 'email' | 'url' | 'date'
    Retourne une liste de messages d'erreur (vide = OK).
    """
    errors = []
    for field, ftype, required in rules:
        val = form.get(field, '').strip()
        if required and not val:
            errors.append(f'Le champ « {field} » est obligatoire.')
            continue
        if not val:
            continue
        if ftype == 'ip' and not _RE_IP.match(val):
            errors.append(f'Adresse IP invalide : {val}')
        elif ftype == 'mac' and not _RE_MAC.match(val):
            errors.append(f'Adresse MAC invalide : {val}')
        elif ftype == 'email' and not _RE_EMAIL.match(val):
            errors.append(f'Adresse e-mail invalide : {val}')
        elif ftype == 'url' and not _RE_URL.match(val):
            errors.append(f'URL invalide (doit commencer par http:// ou https://) : {val}')
        elif ftype == 'date':
            try:
                datetime.fromisoformat(val)
            except ValueError:
                errors.append(f'Date invalide : {val}')
    return errors
