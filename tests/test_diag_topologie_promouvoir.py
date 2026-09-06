"""Proposition #2 : POST /api/diag-reseau/topologie/promouvoir crée dans
l'inventaire un équipement vu en LLDP mais absent (jamais sans ce clic)."""
from conftest import login_session


def _cid_owner(conn, make_user, make_client):
    uid, _login, _pwd = make_user(role='user')
    cid = make_client(auth_user_id=uid)
    return cid, uid


def test_promouvoir_cree_le_switch(client, conn, make_user, make_client):
    cid, uid = _cid_owner(conn, make_user, make_client)
    login_session(client, uid, cid)
    r = client.post('/api/diag-reseau/topologie/promouvoir',
                    json={'ip': '10.20.0.9', 'nom': 'SW-SALLE-B', 'modele': 'Cisco C9200'})
    assert r.status_code == 200
    aid = r.get_json()['id']
    row = conn.execute("SELECT nom_machine, adresse_ip, type_appareil, marque FROM appareils "
                       "WHERE id=?", (aid,)).fetchone()
    assert tuple(row) == ('SW-SALLE-B', '10.20.0.9', 'Switch', 'Cisco')
    h = conn.execute("SELECT action FROM historique WHERE entite='appareil' AND entite_id=?",
                     (aid,)).fetchone()
    assert h and 'topologie' in h[0].lower()


def test_promouvoir_ip_deja_presente(client, conn, make_user, make_client, make_appareil):
    cid, uid = _cid_owner(conn, make_user, make_client)
    make_appareil(cid, nom_machine='DEJA', adresse_ip='10.20.0.10')
    login_session(client, uid, cid)
    r = client.post('/api/diag-reseau/topologie/promouvoir', json={'ip': '10.20.0.10'})
    assert r.status_code == 409


def test_promouvoir_ip_invalide(client, conn, make_user, make_client):
    cid, uid = _cid_owner(conn, make_user, make_client)
    login_session(client, uid, cid)
    r = client.post('/api/diag-reseau/topologie/promouvoir', json={'ip': 'pas-une-ip'})
    assert r.status_code == 400


def test_promouvoir_lecture_seule_refuse(client, conn, make_user, make_client):
    """Un utilisateur sans droit d'écriture sur le client ne peut pas créer."""
    owner_uid, _l, _p = make_user(role='user')
    cid = make_client(auth_user_id=owner_uid)
    autre_uid, _l2, _p2 = make_user(role='user')
    login_session(client, autre_uid, cid)
    r = client.post('/api/diag-reseau/topologie/promouvoir', json={'ip': '10.20.0.11'})
    assert r.status_code == 403
