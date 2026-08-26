"""
Ports (RJ45) d'un élément de la baie de brassage — baie_slot_ports.

Couvre : création des N lignes de port à la pose d'un élément, liaison
d'un port à un appareil/périphérique/usage libre, couleur résolue côté
serveur, report des liaisons quand un slot est remplacé à la même position
(POST /api/baie/slot), nettoyage à la suppression (slot entier, appareil,
périphérique).
"""
import json

from conftest import login_session, get_csrf_token


def test_pose_switch_cree_les_ports(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    r = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'hauteur_u': 2, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-TEST', 'nb_ports': 8,
    })
    assert r.status_code == 200
    slot = r.get_json()
    assert slot['nb_ports'] == 8
    assert len(slot['ports']) == 8
    assert [p['numero'] for p in slot['ports']] == list(range(1, 9))
    # Port jamais affecté : pas de cible, couleur neutre.
    assert slot['ports'][0]['appareil_id'] is None
    assert slot['ports'][0]['couleur'] == '#334155'


def test_nb_ports_plafonne_a_48(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    r = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nb_ports': 500,
    })
    slot = r.get_json()
    assert slot['nb_ports'] == 48
    assert len(slot['ports']) == 48


def test_lier_port_a_un_appareil_resout_couleur_type(client, make_client, make_user, make_appareil):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='PC-CABLE', type_appareil='Serveur')

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nb_ports': 4,
    }).get_json()

    r = client.put(f"/api/baie/slot/{slot['id']}/port/2", json={'appareil_id': app_id})
    assert r.status_code == 200
    port = r.get_json()
    assert port['appareil_id'] == app_id
    assert port['nom_cible'] == 'PC-CABLE'
    # Couleur = celle configurée pour le type 'Serveur' (config_helpers.CFG_DEFAULTS
    # ou son fallback '#94a3b8') — pas la couleur neutre d'un port libre.
    assert port['couleur'] != '#334155'


def test_lier_port_a_usage_libre(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Patch Panel', 'nb_ports': 2,
    }).get_json()

    r = client.put(f"/api/baie/slot/{slot['id']}/port/1", json={'usage_libre': 'Téléphonie'})
    port = r.get_json()
    assert port['usage_libre'] == 'Téléphonie'
    assert port['appareil_id'] is None
    assert port['peripherique_id'] is None
    assert port['couleur'] == '#f59e0b'


def test_lier_appareil_efface_usage_libre_precedent(client, make_client, make_user, make_appareil):
    """Un port = une seule cible à la fois, comme pour un slot entier."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 1,
    }).get_json()
    client.put(f"/api/baie/slot/{slot['id']}/port/1", json={'usage_libre': 'Alarme'})
    r = client.put(f"/api/baie/slot/{slot['id']}/port/1", json={'appareil_id': app_id})
    port = r.get_json()
    assert port['appareil_id'] == app_id
    assert port['usage_libre'] == ''


def test_remplacement_meme_position_conserve_les_liaisons(client, make_client, make_user, make_appareil, conn):
    """placerEquip() ré-envoie systématiquement un POST, y compris pour
    éditer un slot déjà existant (même position/col) — les liaisons de
    ports ne doivent pas disparaître à chaque modification du panneau."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid)

    slot1 = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nb_ports': 4,
    }).get_json()
    client.put(f"/api/baie/slot/{slot1['id']}/port/3", json={'appareil_id': app_id})

    # Re-pose à la même position (édition via le panneau) avec un nom modifié.
    slot2 = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-RENOMME', 'nb_ports': 4,
    }).get_json()

    assert slot2['id'] != slot1['id']  # POST réinsère toujours (comportement existant)
    port3 = next(p for p in slot2['ports'] if p['numero'] == 3)
    assert port3['appareil_id'] == app_id, "la liaison du port 3 aurait dû être reportée"

    # L'ancien slot (et ses ports) ne doit plus exister en base.
    reste = conn.execute('SELECT COUNT(*) FROM baie_slot_ports WHERE slot_id=?', (slot1['id'],)).fetchone()[0]
    assert reste == 0


def test_suppression_slot_supprime_ses_ports(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 5,
    }).get_json()
    assert conn.execute('SELECT COUNT(*) FROM baie_slot_ports WHERE slot_id=?', (slot['id'],)).fetchone()[0] == 5

    r = client.delete(f"/api/baie/slot/{slot['id']}")
    assert r.status_code == 200
    assert conn.execute('SELECT COUNT(*) FROM baie_slot_ports WHERE slot_id=?', (slot['id'],)).fetchone()[0] == 0


def test_suppression_appareil_libere_le_port_sans_supprimer_le_slot(client, make_client, make_user, make_appareil, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 1,
    }).get_json()
    client.put(f"/api/baie/slot/{slot['id']}/port/1", json={'appareil_id': app_id})

    csrf = get_csrf_token(client)
    r = client.post(f'/appareil/{app_id}/supprimer', data={'csrf_token': csrf})
    assert r.status_code in (302, 200)

    port = conn.execute(
        'SELECT appareil_id FROM baie_slot_ports WHERE slot_id=? AND numero=1', (slot['id'],)).fetchone()
    assert port[0] is None, "le port aurait dû être libéré, pas laissé orphelin"


def test_reverse_lookup_sur_fiche_appareil(client, make_client, make_user, make_appareil):
    """La fiche appareil doit lister les ports de baie qui le référencent."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='SERVEUR-CABLE')

    slot = client.post('/api/baie/slot', json={
        'position': 3, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-A', 'nb_ports': 2,
    }).get_json()
    client.put(f"/api/baie/slot/{slot['id']}/port/1", json={'appareil_id': app_id})

    r = client.get(f'/appareil/{app_id}/editer')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'CÂBLAGE' in body.upper()
    assert 'SW-A' in body
