"""
Audit trail de la baie de brassage — log_history() n'était appelé nulle part
dans /api/baie/* jusqu'ici (contrairement à tout le reste de l'app) :
placer, déplacer, redimensionner, câbler ou supprimer un élément ne
laissait aucune trace. Couvre chaque route.

Les actions utilisent volontairement des libellés DIFFÉRENTS du littéral
exact 'Modification' (ex: 'Modification (baie)') : historique.html n'active
le bouton "Annuler" (restauration via diff avant/après) que pour ce
libellé précis — ces entrées n'ont pas de diff structuré, l'afficher
casserait silencieusement ce bouton.
"""
from conftest import login_session


def _dernieres_entrees(conn, cid, n=1):
    return [dict(zip(
        ('id', 'client_id', 'entite', 'entite_id', 'entite_nom', 'action', 'date_action', 'details'), r))
        for r in conn.execute(
            'SELECT * FROM historique WHERE client_id=? ORDER BY id DESC LIMIT ?', (cid, n)).fetchall()]


def test_placement_log_history(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-AUDIT', 'nb_ports': 4,
    }).get_json()

    h = _dernieres_entrees(conn, cid)[0]
    assert h['entite'] == 'baie_slot'
    assert h['entite_id'] == slot['id']
    assert h['entite_nom'] == 'SW-AUDIT'
    assert h['action'] == 'Placement'
    assert h['action'] != 'Modification'


def test_remplacement_meme_position_log_modification(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nom_custom': 'SW-1',
    })
    client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nom_custom': 'SW-1-RENOMME',
    })
    h = _dernieres_entrees(conn, cid)[0]
    assert h['action'] == 'Modification (baie)'
    assert h['action'] != 'Modification'


def test_redimensionnement_log_history(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nom_custom': 'SW-R',
    }).get_json()
    client.put(f"/api/baie/slot/{slot['id']}", json={
        'position': 1, 'hauteur_u': 3, 'nom_custom': 'SW-R', 'largeur_u': 6,
    })
    h = _dernieres_entrees(conn, cid)[0]
    assert h['entite_id'] == slot['id']
    assert h['action'] == 'Modification (baie)'


def test_deplacement_log_history(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nom_custom': 'SW-D',
    }).get_json()
    client.post(f"/api/baie/slot/{slot['id']}/deplacer", json={'position': 4, 'col_index': 0})
    h = _dernieres_entrees(conn, cid)[0]
    assert h['action'] == 'Déplacement'
    assert 'U4' in h['details']


def test_retrait_log_history(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nom_custom': 'SW-DEL',
    }).get_json()
    client.delete(f"/api/baie/slot/{slot['id']}")
    h = _dernieres_entrees(conn, cid)[0]
    assert h['entite_nom'] == 'SW-DEL'
    assert h['action'] == 'Retrait'


def test_modification_port_log_history(client, make_client, make_user, make_appareil, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='PC-TEST')
    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nom_custom': 'SW-P', 'nb_ports': 4,
    }).get_json()
    client.put(f"/api/baie/slot/{slot['id']}/port/1", json={'appareil_id': app_id})
    h = _dernieres_entrees(conn, cid)[0]
    assert h['entite'] == 'baie_slot'
    assert h['entite_id'] == slot['id']
    assert h['action'] == 'Modification (port baie)'
    assert 'PC-TEST' in h['details']


def test_cablage_log_history(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    s1 = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nom_custom': 'BANDEAU-A', 'nb_ports': 4,
    }).get_json()
    s2 = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale', 'nom_custom': 'SW-B', 'nb_ports': 4,
    }).get_json()
    client.post('/api/baie/lien-port', json={
        'slot1_id': s1['id'], 'numero1': 1, 'slot2_id': s2['id'], 'numero2': 2,
    })
    h = _dernieres_entrees(conn, cid)[0]
    assert h['action'] == 'Câblage (baie)'
    assert 'SW-B' in h['details']
    assert 'port 2' in h['details']


def test_suppression_baie_log_history(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nom_custom': 'X',
    })
    client.delete('/api/baie?baie=Baie%20principale')
    h = _dernieres_entrees(conn, cid)[0]
    assert h['entite'] == 'baie'
    assert h['action'] == 'Suppression (baie)'
