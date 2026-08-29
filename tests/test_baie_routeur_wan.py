"""
Routeur/Pare-feu : groupes de connecteurs WAN + LAN + Fibre (phase 2 de la
généralisation démarrée avec le switch — voir test_baie_switch_sfp.py).

Trois plages de numérotation générique, jamais partagées : "de base"
(LAN, 1-48, colonne nb_ports — réutilisée telle quelle, un routeur n'a
jamais besoin des deux à la fois avec un switch), "haute" (Fibre, même
plage que le SFP d'un switch, SFP_NUMERO_OFFSET+1..+8), et "WAN"
(WAN_NUMERO_OFFSET+1..+4, la plus haute des trois — seul un routeur/
pare-feu a besoin des TROIS groupes en même temps).
"""
from conftest import login_session


def test_pose_routeur_avec_wan_lan_fibre(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    r = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Routeur/Pare-feu', 'nom_custom': 'RTR-WAN',
        'nb_ports': 8, 'nb_ports_sfp': 2, 'nb_ports_wan': 2,
    })
    assert r.status_code == 200
    slot = r.get_json()
    assert slot['nb_ports'] == 8
    assert slot['nb_ports_sfp'] == 2
    assert slot['nb_ports_wan'] == 2
    numeros = [p['numero'] for p in slot['ports']]
    assert numeros == [1, 2, 3, 4, 5, 6, 7, 8, 1001, 1002, 2001, 2002]


def test_nb_ports_wan_plafonne_a_4(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    r = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Routeur/Pare-feu', 'nb_ports_wan': 500,
    })
    slot = r.get_json()
    assert slot['nb_ports_wan'] == 4
    assert len([p for p in slot['ports'] if p['numero'] > 2000]) == 4


def test_lier_port_wan_a_un_appareil_resout_couleur_type(client, make_client, make_user, make_appareil):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='MODEM-FAI', type_appareil='Routeur/Pare-feu')

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Routeur/Pare-feu', 'nb_ports_wan': 2,
    }).get_json()

    r = client.put(f"/api/baie/slot/{slot['id']}/port/2001", json={'appareil_id': app_id})
    assert r.status_code == 200
    port = r.get_json()
    assert port['appareil_id'] == app_id
    assert port['nom_cible'] == 'MODEM-FAI'
    assert port['couleur'] != '#334155'


def test_reduire_sfp_ne_touche_pas_au_wan(client, make_client, make_user, make_appareil):
    """Bug potentiel corrigé au passage : réduire nb_ports_sfp appelait
    _reconcilier_ports_sfp, dont la suppression 'numero > plafond_numero'
    n'était PAS bornée en haut — sans la borne < WAN_NUMERO_OFFSET, un
    routeur ayant à la fois Fibre ET WAN configurés aurait vu tous ses
    ports WAN silencieusement détachés/supprimés dès que le nombre de
    ports Fibre était réduit (2001 > plafond_numero de la plage SFP, quel
    qu'il soit)."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='WAN-DEVICE')

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Routeur/Pare-feu',
        'nb_ports': 4, 'nb_ports_sfp': 4, 'nb_ports_wan': 2,
    }).get_json()
    client.put(f"/api/baie/slot/{slot['id']}/port/2001", json={'appareil_id': app_id})

    r = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Routeur/Pare-feu',
        'nb_ports': 4, 'nb_ports_sfp': 1, 'nb_ports_wan': 2,
    })
    slot2 = r.get_json()
    numeros = [p['numero'] for p in slot2['ports']]
    assert numeros == [1, 2, 3, 4, 1001, 2001, 2002]
    port_wan1 = next(p for p in slot2['ports'] if p['numero'] == 2001)
    assert port_wan1['appareil_id'] == app_id, "le port WAN 1 ne doit pas être affecté par la réduction des ports Fibre"


def test_reduire_wan_ne_touche_pas_au_lan_ni_a_la_fibre(client, make_client, make_user, make_appareil):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='LAN-DEVICE')

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Routeur/Pare-feu',
        'nb_ports': 4, 'nb_ports_sfp': 2, 'nb_ports_wan': 4,
    }).get_json()
    client.put(f"/api/baie/slot/{slot['id']}/port/2", json={'appareil_id': app_id})

    r = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Routeur/Pare-feu',
        'nb_ports': 4, 'nb_ports_sfp': 2, 'nb_ports_wan': 1,
    })
    slot2 = r.get_json()
    numeros = [p['numero'] for p in slot2['ports']]
    assert numeros == [1, 2, 3, 4, 1001, 1002, 2001]
    port2 = next(p for p in slot2['ports'] if p['numero'] == 2)
    assert port2['appareil_id'] == app_id, "le port LAN 2 ne doit pas être affecté par la réduction des ports WAN"


def test_redimensionnement_conserve_wan(client, make_client, make_user):
    """Même précaution déjà en place pour nb_ports/nb_ports_sfp (voir
    redimensionnerSlot() côté client) — étendue à nb_ports_wan."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Routeur/Pare-feu', 'nom_custom': 'RTR-R',
        'nb_ports': 8, 'nb_ports_sfp': 2, 'nb_ports_wan': 2,
    }).get_json()

    r = client.put(f"/api/baie/slot/{slot['id']}", json={
        'position': 1, 'hauteur_u': 2, 'nom_custom': 'RTR-R', 'type_equipement': 'Routeur/Pare-feu',
        'nb_ports': 8, 'nb_ports_sfp': 2, 'nb_ports_wan': 2,
    })
    slot2 = r.get_json()
    assert slot2['nb_ports_wan'] == 2
    assert len([p for p in slot2['ports'] if p['numero'] > 2000]) == 2
