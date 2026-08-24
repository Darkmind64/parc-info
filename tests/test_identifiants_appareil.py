"""Lien identifiants <-> appareil/périphérique (audit du 2026-08-24,
proposition #3) : identifiants.appareil_id/peripherique_id (nullable),
section « Identifiants liés » sur la fiche appareil, et détection d'un
conflit quand le login/mot de passe saisi directement sur l'appareil
(user_login/user_password, admin_login/admin_password) diffère de
l'identifiant lié — deux silos de credentials pour la même machine
jusqu'ici sans le moindre rapprochement.
"""
from conftest import get_csrf_token, login_session


def test_identifiant_non_lie_absent_de_la_liste(client, make_user, make_client, make_appareil, make_identifiant):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid)
    make_identifiant(cid, nom='Compte générique')  # pas lié à cet appareil

    login_session(client, uid, cid)
    items = client.get(f'/api/identifiants/appareil/{aid}').get_json()

    assert items == []


def test_identifiant_lie_apparait_sans_conflit(client, make_user, make_client, make_appareil, make_identifiant):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid, user_login='jdupont', user_password='Secret123')
    make_identifiant(cid, nom='Session Windows', login='jdupont', mot_de_passe='Secret123', appareil_id=aid)

    login_session(client, uid, cid)
    items = client.get(f'/api/identifiants/appareil/{aid}').get_json()

    assert len(items) == 1
    assert items[0]['nom'] == 'Session Windows'
    assert items[0]['conflit'] is None
    # Le mot de passe en clair ne part jamais dans cette liste.
    assert 'mot_de_passe' not in items[0] and 'login_rapide' not in items[0]


def test_conflit_detecte_si_mot_de_passe_differe(client, make_user, make_client, make_appareil, make_identifiant):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid, user_login='jdupont', user_password='AncienMDP')
    make_identifiant(cid, nom='Session Windows', login='jdupont', mot_de_passe='NouveauMDP', appareil_id=aid)

    login_session(client, uid, cid)
    items = client.get(f'/api/identifiants/appareil/{aid}').get_json()

    assert items[0]['conflit'] == 'Login utilisateur'


def test_pas_de_conflit_si_login_different(client, make_user, make_client, make_appareil, make_identifiant):
    """Un identifiant lié dont le login ne correspond à aucun champ rapide
    n'est pas un conflit — juste un compte distinct, rien à signaler."""
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid, user_login='jdupont', user_password='Secret123')
    make_identifiant(cid, nom='Compte service', login='svc-backup', mot_de_passe='AutreChose', appareil_id=aid)

    login_session(client, uid, cid)
    items = client.get(f'/api/identifiants/appareil/{aid}').get_json()

    assert items[0]['conflit'] is None


def test_identifiants_isoles_par_client(client, make_user, make_client, make_appareil, make_identifiant):
    uid, _l, _p = make_user(role='admin')
    cid_a = make_client(auth_user_id=uid)
    cid_b = make_client(auth_user_id=uid)
    aid_a = make_appareil(cid_a)
    # Un identifiant du client B ne doit jamais remonter pour un appareil du client A,
    # même si (par accident d'id) il pointait vers le même appareil_id.
    make_identifiant(cid_b, nom='Fuite potentielle', appareil_id=aid_a)

    login_session(client, uid, cid_a)
    items = client.get(f'/api/identifiants/appareil/{aid_a}').get_json()

    assert items == []


def test_sauvegarde_identifiant_avec_appareil_lie(client, make_user, make_client, make_appareil):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid, nom_machine='SRV-01')

    login_session(client, uid, cid)
    token = get_csrf_token(client)
    client.post('/identifiant/nouveau', data={
        'nom': 'Admin routeur', 'categorie': 'Admin réseau',
        'login': 'admin', 'mot_de_passe': 'Motdepasse1',
        'appareil_id': str(aid), 'csrf_token': token,
    })

    import app
    conn = app.get_db()
    row = conn.execute("SELECT appareil_id FROM identifiants WHERE nom='Admin routeur'").fetchone()
    conn.close()
    assert row[0] == aid


def test_page_appareil_liste_les_identifiants_lies(client, make_user, make_client, make_appareil, make_identifiant):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid, nom_machine='SRV-01')
    make_identifiant(cid, nom='Admin routeur', appareil_id=aid)

    login_session(client, uid, cid)
    html = client.get(f'/appareil/{aid}/editer').get_data(as_text=True)

    assert 'Identifiants liés' in html
    assert 'id="identifiants-app-list"' in html
