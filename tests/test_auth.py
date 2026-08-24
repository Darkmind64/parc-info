"""Authentification : login, timeout de session, rate-limiting, CSRF.

Ces mécanismes sont documentés comme critiques dans CLAUDE.md (§ Sécurité) —
ce sont eux qui protègent tout le reste de l'app contre le brute-force et le
détournement de session/formulaire.
"""
from conftest import get_csrf_token, login_session


def test_route_protegee_redirige_vers_login_si_non_authentifie(client):
    resp = client.get('/appareils')
    assert resp.status_code in (301, 302)
    assert '/login' in resp.headers['Location']


def test_login_reussi_ouvre_une_session(client, make_user):
    _uid, login, password = make_user()
    resp = client.post('/login', data={'login': login, 'password': password},
                        follow_redirects=False)
    assert resp.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get('auth_user_id')


def test_login_mauvais_mot_de_passe_echoue(client, make_user):
    _uid, login, _password = make_user()
    resp = client.post('/login', data={'login': login, 'password': 'mauvais-mot-de-passe'})
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert not sess.get('auth_user_id')
    assert 'Identifiants incorrects' in resp.get_data(as_text=True)


def test_rate_limiting_bloque_apres_10_echecs(client, make_user):
    _uid, login, _password = make_user()
    for _ in range(10):
        client.post('/login', data={'login': login, 'password': 'faux'})
    resp = client.post('/login', data={'login': login, 'password': 'faux'})
    assert 'Trop de tentatives' in resp.get_data(as_text=True)


def test_rate_limiting_se_reinitialise_apres_succes(client, make_user):
    _uid, login, password = make_user()
    for _ in range(5):
        client.post('/login', data={'login': login, 'password': 'faux'})
    ok = client.post('/login', data={'login': login, 'password': password})
    assert ok.status_code in (301, 302)
    # Après un succès, un nouvel échec ne doit pas être immédiatement bloqué.
    resp = client.post('/login', data={'login': login, 'password': 'faux'})
    assert 'Trop de tentatives' not in resp.get_data(as_text=True)


def test_post_sans_token_csrf_est_rejete(client, make_user, make_client, make_appareil):
    uid, _login, _password = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid)
    login_session(client, uid, cid)
    resp = client.post(f'/appareil/{aid}/editer', data={'nom_machine': 'Nouveau nom'})
    assert resp.status_code == 403


def test_post_avec_token_csrf_valide_est_accepte(client, make_user, make_client, make_appareil):
    uid, _login, _password = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid)
    login_session(client, uid, cid)
    token = get_csrf_token(client)
    resp = client.post(f'/appareil/{aid}/editer',
                        data={'nom_machine': 'Nouveau nom', 'csrf_token': token})
    assert resp.status_code in (301, 302)


def test_api_est_exempte_de_csrf(client, make_user, make_client):
    """Les endpoints /api/* servent des collecteurs externes sans session
    Flask : ils ne peuvent pas fournir de token CSRF (voir CLAUDE.md,
    § Pourquoi /api/* est exempté)."""
    uid, _login, _password = make_user(role='admin')
    make_client(auth_user_id=uid)
    resp = client.post('/api/device-info', json={'hostname': 'X'})
    # Peu importe le verdict métier (client_id manquant, etc.) — ce qui
    # compte ici est qu'il ne s'agit PAS d'un 403 CSRF.
    assert resp.status_code != 403
