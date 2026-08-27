"""
Liste de câblage exportable (page imprimable + CSV) et vue mobile
(lecture seule) de la baie de brassage.
"""
from conftest import login_session


def _poser_lien(client, baie='Baie principale'):
    s1 = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': baie, 'nom_custom': 'BANDEAU-X', 'nb_ports': 4,
    }).get_json()
    s2 = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': baie, 'nom_custom': 'SW-Y', 'nb_ports': 4,
    }).get_json()
    client.post('/api/baie/lien-port', json={
        'slot1_id': s1['id'], 'numero1': 3, 'slot2_id': s2['id'], 'numero2': 4,
        'cable_couleur': 'Bleu', 'cable_longueur': '1.5m',
    })
    return s1, s2


def test_page_cablage_liste_le_lien(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    _poser_lien(client)
    r = client.get('/baie/cablage')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'BANDEAU-X' in body
    assert 'SW-Y' in body
    assert 'Port 3' in body
    assert 'Port 4' in body
    assert 'Bleu' in body


def test_page_cablage_ne_double_pas_le_lien(client, make_client, make_user, conn):
    """Le lien est stocké bidirectionnellement (une ligne par port) — la
    liste doit en montrer UN seul, pas deux."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    _poser_lien(client)
    from app import _liste_cablage
    liens = _liste_cablage(conn, cid)
    assert len(liens) == 1


def test_page_cablage_vide_sans_lien(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    r = client.get('/baie/cablage')
    assert r.status_code == 200
    assert 'Aucun lien' in r.get_data(as_text=True)


def test_export_csv_contenu(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    _poser_lien(client)
    r = client.get('/baie/cablage.csv')
    assert r.status_code == 200
    assert 'text/csv' in r.headers['Content-Type']
    assert 'attachment' in r.headers['Content-Disposition']
    body = r.get_data(as_text=True)
    assert 'BANDEAU-X' in body
    assert 'SW-Y' in body
    assert 'Bleu' in body
    assert '1.5m' in body


def test_export_csv_isole_par_client(client, make_client, make_user):
    """Le câblage d'un autre client ne doit jamais apparaître dans cet
    export — même règle d'isolation multi-client que partout ailleurs."""
    uid1, _, _ = make_user()
    cid1 = make_client(auth_user_id=uid1)
    login_session(client, uid1, cid1)
    _poser_lien(client)

    uid2, _, _ = make_user()
    cid2 = make_client(auth_user_id=uid2)
    login_session(client, uid2, cid2)
    r = client.get('/baie/cablage.csv')
    body = r.get_data(as_text=True)
    assert 'BANDEAU-X' not in body


def test_mobile_baie_liste_les_elements(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    _poser_lien(client)
    r = client.get('/m/baie')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'BANDEAU-X' in body
    assert 'SW-Y' in body


def test_mobile_baie_affiche_la_piece(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    bandeau = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Bandeau RJ', 'nom_custom': 'BANDEAU-M', 'nb_ports': 4,
    }).get_json()
    client.put(f"/api/baie/slot/{bandeau['id']}/port/2", json={'piece': 'Salle Réunion'})
    r = client.get('/m/baie')
    assert 'Salle Réunion' in r.get_data(as_text=True)


def test_mobile_baie_vide_sans_planter(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    r = client.get('/m/baie')
    assert r.status_code == 200
