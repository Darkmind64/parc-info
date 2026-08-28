"""
Ports (RJ45) d'un élément de la baie de brassage — baie_slot_ports.

Couvre : création des N lignes de port à la pose d'un élément, liaison
d'un port à un appareil/périphérique/usage libre, couleur résolue côté
serveur, report des liaisons quand un slot est remplacé à la même position
(POST /api/baie/slot), nettoyage à la suppression (slot entier, appareil,
périphérique), et le lien port-à-port (câblage physique switch<->routeur,
etc.) : création bidirectionnelle, détachement à la réassignation ou à la
suppression, report au remplacement d'un slot à la même position.
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


def test_port_expose_dernier_ping_pour_distinguer_jamais_pingue_de_hors_ligne(
        client, make_client, make_user, make_appareil, conn):
    """dernier_ping ('' si jamais pingé) permet au client de savoir s'il faut
    afficher un voyant du tout — en_ligne vaut 0 par défaut en base, donc à
    lui seul indiscernable d'un appareil réellement hors ligne."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='PC-JAMAIS-PINGUE')

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 2,
    }).get_json()
    port = client.put(f"/api/baie/slot/{slot['id']}/port/1", json={'appareil_id': app_id}).get_json()
    assert port['dernier_ping'] == ''
    assert port['en_ligne'] == 0

    conn.execute("UPDATE appareils SET en_ligne=1, dernier_ping=? WHERE id=?", ('2026-01-01T00:00:00', app_id))
    conn.commit()
    ports = client.get('/api/baie/slots?baie=Baie%20principale').get_json()['slots']
    slot_row = next(s for s in ports if s['id'] == slot['id'])
    port1 = next(p for p in slot_row['ports'] if p['numero'] == 1)
    assert port1['dernier_ping'] == '2026-01-01T00:00:00'
    assert port1['en_ligne'] == 1


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


def test_lien_port_a_port_bidirectionnel(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    sw = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'Switch A', 'nb_ports': 24,
    }).get_json()
    rt = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Routeur', 'nom_custom': 'Routeur B', 'nb_ports': 4,
    }).get_json()

    r = client.post('/api/baie/lien-port', json={
        'slot1_id': sw['id'], 'numero1': 12, 'slot2_id': rt['id'], 'numero2': 1,
    })
    assert r.status_code == 200
    data = r.get_json()
    assert data['port1']['lie_slot_id'] == rt['id']
    assert data['port1']['lie_port_numero'] == 1
    assert data['port1']['nom_cible'] == 'Port 1 — Routeur B'
    assert data['port2']['lie_slot_id'] == sw['id']
    assert data['port2']['lie_port_numero'] == 12
    assert data['port2']['nom_cible'] == 'Port 12 — Switch A'
    # Couleur dédiée au lien port-à-port, distincte du port libre/usage.
    assert data['port1']['couleur'] == data['port2']['couleur'] == '#818cf8'


def test_lien_resout_couleur_ping_via_port_en_face(client, make_client, make_user, make_appareil):
    """Cas standard du brassage structuré : un bandeau RJ dont un port est
    associé DIRECTEMENT à un appareil (prise murale), relié par cordon à un
    port de switch. Signalé en usage réel : le port du switch restait
    indigo générique (couleur "lien") au lieu de la couleur du type de
    l'appareil, et n'était jamais inclus dans "Ping toute la baie" — ni la
    couleur ni le ping ne suivaient la chaîne au-delà de l'élément en face."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    nas_id = make_appareil(cid, nom_machine='NAS-TEST', type_appareil='NAS', adresse_ip='10.0.0.5')

    patch = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Bandeau RJ', 'nom_custom': 'PATCH', 'nb_ports': 4,
    }).get_json()
    sw = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW', 'nb_ports': 4,
    }).get_json()

    # Port 1 du bandeau associé DIRECTEMENT au NAS (câblage réel vers la prise murale).
    client.put(f"/api/baie/slot/{patch['id']}/port/1", json={'appareil_id': nas_id})
    # Cordon de brassage : port 2 du switch <-> port 1 du bandeau.
    r = client.post('/api/baie/lien-port', json={
        'slot1_id': sw['id'], 'numero1': 2, 'slot2_id': patch['id'], 'numero2': 1,
    })
    assert r.status_code == 200
    port_switch = r.get_json()['port1']

    assert port_switch['cible_finale'] == 'NAS-TEST'
    assert port_switch['cible_hors_ligne'] is False
    assert port_switch['lie_appareil_id'] == nas_id
    assert port_switch['couleur'] != '#818cf8', "doit prendre la couleur du NAS, pas l'indigo générique du lien"

    # Le patch panel port 1 garde bien son association directe (le lien ne
    # doit pas l'avoir effacée — c'est le port du SWITCH qui devient un
    # lien, pas celui du bandeau).
    ports_patch = client.get('/api/baie/slots?baie=Baie%20principale').get_json()['slots']
    patch_frais = next(s for s in ports_patch if s['id'] == patch['id'])
    port1_patch = next(p for p in patch_frais['ports'] if p['numero'] == 1)
    assert port1_patch['appareil_id'] == nas_id

    # "Ping toute la baie" doit inclure le NAS via ce port du switch.
    cibles = {p['appareil_id'] or p['lie_appareil_id']
              for s in ports_patch for p in s['ports']
              if p['appareil_id'] or p['lie_appareil_id']}
    assert nas_id in cibles


def test_lien_resout_via_slot_en_face_si_port_libre(client, make_client, make_user, make_appareil):
    """Deuxième cas, plus ancien : l'appareil au bout du câble est
    RACK-MONTÉ (son propre élément de baie), associé au niveau du SLOT
    (comme un serveur) plutôt que sur le port précis relié — le port relié
    lui-même ne porte aucune association directe. Doit continuer à
    fonctionner (mécanisme d'origine, non régressé par le correctif du cas
    ci-dessus)."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    nas_id = make_appareil(cid, nom_machine='NAS-RACKMOUNT', type_appareil='NAS', adresse_ip='10.0.0.6')

    nas_slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'NAS', 'nom_custom': 'NAS-RACKMOUNT',
        'appareil_id': nas_id, 'nb_ports': 1,
    }).get_json()
    sw = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW2', 'nb_ports': 4,
    }).get_json()

    r = client.post('/api/baie/lien-port', json={
        'slot1_id': sw['id'], 'numero1': 1, 'slot2_id': nas_slot['id'], 'numero2': 1,
    })
    assert r.status_code == 200
    port_switch = r.get_json()['port1']

    assert port_switch['cible_finale'] == 'NAS-RACKMOUNT'
    assert port_switch['lie_appareil_id'] == nas_id
    assert port_switch['couleur'] != '#818cf8'


def test_lien_port_refuse_meme_port(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    sw = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 4,
    }).get_json()
    r = client.post('/api/baie/lien-port', json={
        'slot1_id': sw['id'], 'numero1': 1, 'slot2_id': sw['id'], 'numero2': 1,
    })
    assert r.status_code == 400


def test_lien_port_detache_a_la_reassignation(client, make_client, make_user, make_appareil):
    """Réaffecter un port lié (appareil, ou « — Libre — ») doit détacher
    automatiquement le partenaire, sans le laisser pointer dans le vide."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='PC-CABLE')

    sw = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 4,
    }).get_json()
    rt = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 4,
    }).get_json()
    client.post('/api/baie/lien-port', json={
        'slot1_id': sw['id'], 'numero1': 1, 'slot2_id': rt['id'], 'numero2': 1,
    })

    r = client.put(f"/api/baie/slot/{sw['id']}/port/1", json={'appareil_id': app_id})
    port1 = r.get_json()
    assert port1['appareil_id'] == app_id
    assert port1['lie_slot_id'] is None

    r2 = client.put(f"/api/baie/slot/{rt['id']}/port/1", json={})
    port2 = r2.get_json()
    # Le port en face doit avoir été détaché par la réaffectation du premier,
    # PAS par cet appel — mais on vérifie ici l'état final des deux côtés.
    assert port2['lie_slot_id'] is None
    assert port2['appareil_id'] is None


def test_lien_port_detache_a_la_suppression_slot(client, make_client, make_user, conn):
    """Supprimer un des deux éléments liés doit libérer le port restant côté
    survivant (pas de lien à sens unique vers un slot qui n'existe plus)."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    sw = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 24,
    }).get_json()
    rt = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 4,
    }).get_json()
    client.post('/api/baie/lien-port', json={
        'slot1_id': sw['id'], 'numero1': 12, 'slot2_id': rt['id'], 'numero2': 1,
    })

    r = client.delete(f"/api/baie/slot/{rt['id']}")
    assert r.status_code == 200

    row = conn.execute(
        'SELECT lie_slot_id, lie_port_numero FROM baie_slot_ports WHERE slot_id=? AND numero=12',
        (sw['id'],)).fetchone()
    assert row[0] is None and row[1] is None


def test_lien_port_detache_a_la_reduction_nb_ports(client, make_client, make_user, conn):
    """Réduire nb_ports (PUT /api/baie/slot/<id>) en dessous du numéro d'un
    port lié doit détacher le partenaire avant de supprimer la ligne."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    sw = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 24,
    }).get_json()
    rt = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 4,
    }).get_json()
    client.post('/api/baie/lien-port', json={
        'slot1_id': sw['id'], 'numero1': 20, 'slot2_id': rt['id'], 'numero2': 1,
    })

    r = client.put(f"/api/baie/slot/{sw['id']}", json={'nb_ports': 8})
    assert r.status_code == 200

    row = conn.execute(
        'SELECT lie_slot_id, lie_port_numero FROM baie_slot_ports WHERE slot_id=? AND numero=1',
        (rt['id'],)).fetchone()
    assert row[0] is None and row[1] is None


def test_lien_port_reporte_au_remplacement_meme_position(client, make_client, make_user):
    """Comme pour appareil/peripherique/usage_libre : re-poser un slot à la
    même position (édition via le panneau) doit reporter son lien de port
    vers le NOUVEL id, dans les deux sens (slot édité et son partenaire)."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    sw1 = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nb_ports': 24,
    }).get_json()
    rt = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 4,
    }).get_json()
    client.post('/api/baie/lien-port', json={
        'slot1_id': sw1['id'], 'numero1': 12, 'slot2_id': rt['id'], 'numero2': 1,
    })

    # Re-pose à la même position (édition via le panneau, comme placerEquip()).
    sw2 = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-RENOMME', 'nb_ports': 24,
    }).get_json()
    assert sw2['id'] != sw1['id']

    port12 = next(p for p in sw2['ports'] if p['numero'] == 12)
    assert port12['lie_slot_id'] == rt['id']
    assert port12['lie_port_numero'] == 1

    rt_ports = client.get(f"/api/baie/slots?baie=Baie%20principale").get_json()['slots']
    rt_row = next(s for s in rt_ports if s['id'] == rt['id'])
    port1 = next(p for p in rt_row['ports'] if p['numero'] == 1)
    assert port1['lie_slot_id'] == sw2['id'], "le partenaire doit suivre vers le NOUVEL id"
    assert port1['lie_port_numero'] == 12


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
