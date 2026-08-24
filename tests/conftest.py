"""Fixtures pytest partagées.

Reprend le schéma déjà validé par test_remplissage_fiche.py : DB SQLite
temporaire (DATA_DIR), app importée une seule fois avec cette config, et
authentification de test via client.session_transaction() plutôt que via
le formulaire /login (sauf dans test_auth.py, qui teste justement ce
formulaire).
"""
import os
import sys
import tempfile

# Doit s'exécuter avant tout `import app` — le sien ou celui d'un module de
# test — car app.py lit DATA_DIR au moment de l'import pour choisir où
# créer parc_info.db. conftest.py est toujours importé par pytest avant les
# fichiers test_*.py du même dossier, donc cet ordre est garanti.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='parcinfo_tests_'))
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import pytest

import app as flask_app_module
from auth_utils import hash_pwd, reset_attempts

flask_app_module.app.config.update(TESTING=True)


@pytest.fixture(scope='session', autouse=True)
def _init_database():
    flask_app_module.init_db()
    yield


@pytest.fixture(autouse=True)
def _clean_rate_limit():
    """Le rate-limiting est un dict en mémoire, partagé par tout le process :
    sans ce nettoyage, un test de brute-force ferait échouer le login des
    tests suivants qui utilisent la même IP de test (127.0.0.1)."""
    reset_attempts('127.0.0.1')
    yield
    reset_attempts('127.0.0.1')


@pytest.fixture
def client():
    return flask_app_module.app.test_client()


@pytest.fixture
def conn():
    c = flask_app_module.get_db()
    yield c
    c.close()


_seq = {'n': 0}


def _unique(prefix):
    _seq['n'] += 1
    return f'{prefix}{_seq["n"]}'


@pytest.fixture
def make_client(conn):
    """Factory : crée un client (au sens ParcInfo, l'entité gérée) et
    retourne son id."""
    def _make(nom=None, auth_user_id=None):
        nom = nom or _unique('Client de test ')
        cur = conn.execute(
            'INSERT INTO clients (nom, auth_user_id, date_creation) VALUES (?,?,?)',
            (nom, auth_user_id, '2026-01-01T00:00:00'))
        conn.commit()
        return cur.lastrowid
    return _make


@pytest.fixture
def make_user(conn):
    """Factory : crée un auth_user avec un mot de passe connu en clair et
    retourne (id, login, password)."""
    def _make(password='CorrectHorse123!', role='user', login=None):
        login = login or _unique('user')
        conn.execute(
            'INSERT INTO auth_users (login, password_hash, nom, role, actif, date_creation) '
            'VALUES (?,?,?,?,1,?)',
            (login, hash_pwd(password), 'Utilisateur de test', role, '2026-01-01T00:00:00'))
        conn.commit()
        uid = conn.execute('SELECT id FROM auth_users WHERE login=?', (login,)).fetchone()[0]
        return uid, login, password
    return _make


@pytest.fixture
def make_appareil(conn):
    """Factory : crée un appareil pour un client donné et retourne son id."""
    def _make(client_id, nom_machine=None, **extra):
        nom_machine = nom_machine or _unique('POSTE-')
        cols = ['client_id', 'nom_machine'] + list(extra.keys())
        vals = [client_id, nom_machine] + list(extra.values())
        placeholders = ','.join('?' * len(cols))
        cur = conn.execute(
            f"INSERT INTO appareils ({','.join(cols)}) VALUES ({placeholders})", vals)
        conn.commit()
        return cur.lastrowid
    return _make


def login_session(client, auth_user_id, client_id=None):
    """Authentifie le test_client par manipulation directe de session,
    comme test_remplissage_fiche.py — équivalent à un /login réussi mais
    sans dépendre du HTML du formulaire."""
    with client.session_transaction() as sess:
        sess['auth_user_id'] = auth_user_id
        if client_id is not None:
            sess['client_id'] = client_id


def get_csrf_token(client):
    """Récupère un token CSRF valide pour la session du test_client, en
    faisant rendre un template (le context processor inject_csrf_token()
    l'écrit dans la session à ce moment-là), puis en le relisant.

    Suppose une session déjà authentifiée (login_session appelé avant) —
    /login redirige sans rendu dès qu'une session existe, donc ne
    déclencherait pas le context processor dans ce cas."""
    client.get('/appareils')
    with client.session_transaction() as sess:
        return sess['csrf_token']
