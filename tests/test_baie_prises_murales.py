"""
Prises murales d'un bandeau RJ (baie_prises_murales) — entité séparée du
port RJ depuis la 2.18.74 : la prise murale porte désormais la pièce du
site et l'appareil/périphérique/usage branché dans le bureau, le port RJ
de même numéro ne servant plus qu'à interconnecter avec un autre élément
de la baie (câblage physique bandeau<->switch, comme dans un vrai système
de brassage structuré).

Couvre : création automatique en vis-à-vis de chaque port à la pose d'un
bandeau, plafond de 24 ports pour ce type d'élément (contre 48 pour les
autres), assignation d'une prise murale sans toucher au port RJ, résolution
couleur/ping/cible_finale d'un port RJ câblé au switch à travers sa prise
murale, nettoyage à la réduction du nombre de ports / suppression du slot /
suppression de l'appareil ou du périphérique référencé, et le report des
prises murales quand un bandeau est remplacé à la même position (POST
/api/baie/slot, comme pour les ports).
"""
from conftest import login_session


def _poser_bandeau(client, position=1, col=0, nb_ports=24, nom='BANDEAU'):
    return client.post('/api/baie/slot', json={
        'position': position, 'col_index': col, 'baie_nom': 'Baie principale',
        'type_equipement': 'Bandeau RJ', 'nom_custom': nom, 'nb_ports': nb_ports,
    }).get_json()


def test_pose_bandeau_cree_les_prises_murales_en_vis_a_vis(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    bandeau = _poser_bandeau(client, nb_ports=8)
    assert bandeau['nb_ports'] == 8
    assert len(bandeau['ports']) == 8
    for p in bandeau['ports']:
        assert p['prise_murale'] is not None
        assert p['prise_murale']['numero'] == p['numero']
        assert p['prise_murale']['appareil_id'] is None


def test_element_non_bandeau_n_a_aucune_prise_murale(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    sw = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nb_ports': 8,
    }).get_json()
    for p in sw['ports']:
        assert p['prise_murale'] is None


def test_nb_ports_bandeau_plafonne_a_24(client, make_client, make_user):
    """Demandé explicitement : un bandeau RJ reste plafonné à 24 ports (pas
    48 comme les autres éléments) — garantit la place d'empiler la rangée
    des prises murales au-dessus de celle des ports RJ sans agrandir la
    hauteur du bandeau."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    bandeau = _poser_bandeau(client, nb_ports=48)
    assert bandeau['nb_ports'] == 24
    assert len(bandeau['ports']) == 24
    assert len([p for p in bandeau['ports'] if p['prise_murale'] is not None]) == 24

    # Un autre type d'élément garde son plafond à 48, inchangé.
    sw = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nb_ports': 48,
    }).get_json()
    assert sw['nb_ports'] == 48

    # Même plafond en PUT (redimensionnement d'un bandeau déjà posé).
    r = client.put(f"/api/baie/slot/{bandeau['id']}", json={
        'position': 1, 'hauteur_u': 1, 'type_equipement': 'Bandeau RJ', 'nb_ports': 48,
    })
    assert r.get_json()['nb_ports'] == 24


def test_assigner_prise_murale_ne_touche_pas_le_port_rj(client, make_client, make_user, make_appareil):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='PC-BUREAU', type_appareil='PC')
    bandeau = _poser_bandeau(client)

    r = client.put(f"/api/baie/prise-murale/{bandeau['id']}/3", json={
        'appareil_id': app_id, 'piece': 'Bureau 5',
    })
    assert r.status_code == 200
    prise = r.get_json()
    assert prise['appareil_id'] == app_id
    assert prise['nom_cible'] == 'PC-BUREAU'
    assert prise['piece'] == 'Bureau 5'
    assert prise['couleur'] != '#334155'

    # Le port RJ de même numéro reste totalement vierge — pas de cible,
    # aucun lien.
    ports = client.get('/api/baie/slots?baie=Baie%20principale').get_json()['slots']
    bandeau_frais = next(s for s in ports if s['id'] == bandeau['id'])
    port3 = next(p for p in bandeau_frais['ports'] if p['numero'] == 3)
    assert port3['appareil_id'] is None
    assert port3['peripherique_id'] is None
    assert port3['usage_libre'] == ''
    assert port3['lie_slot_id'] is None
    assert port3['prise_murale']['appareil_id'] == app_id


def test_identification_persiste_independamment_de_la_piece_et_de_la_cible(client, make_client, make_user, make_appareil):
    """Identification (repère physique de la prise, ex. "RJ 3.12") — champ
    demandé séparément de la pièce (emplacement) : les deux, et la cible,
    doivent pouvoir être modifiés indépendamment sans s'effacer l'un
    l'autre, comme piece/appareil_id (voir test_baie_piece_cable.py)."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='PC-IDENT')
    bandeau = _poser_bandeau(client)

    r = client.put(f"/api/baie/prise-murale/{bandeau['id']}/4", json={'identification': 'RJ 3.12'})
    prise = r.get_json()
    assert prise['identification'] == 'RJ 3.12'
    assert prise['piece'] == ''
    assert prise['appareil_id'] is None

    r2 = client.put(f"/api/baie/prise-murale/{bandeau['id']}/4", json={'piece': 'Bureau 5', 'appareil_id': app_id})
    prise2 = r2.get_json()
    assert prise2['identification'] == 'RJ 3.12', "l'identification ne doit pas disparaître à un autre changement"
    assert prise2['piece'] == 'Bureau 5'
    assert prise2['appareil_id'] == app_id


def test_prise_murale_usage_libre_exclusif_de_la_cible(client, make_client, make_user, make_appareil):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid)
    bandeau = _poser_bandeau(client)

    client.put(f"/api/baie/prise-murale/{bandeau['id']}/1", json={'usage_libre': 'Alarme'})
    r = client.put(f"/api/baie/prise-murale/{bandeau['id']}/1", json={'appareil_id': app_id})
    prise = r.get_json()
    assert prise['appareil_id'] == app_id
    assert prise['usage_libre'] == ''


def test_prise_murale_404_sur_element_non_bandeau(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    sw = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nb_ports': 4,
    }).get_json()
    r = client.put(f"/api/baie/prise-murale/{sw['id']}/1", json={'piece': 'X'})
    assert r.status_code == 404


def test_chaine_prise_murale_port_rj_switch(client, make_client, make_user, make_appareil):
    """Le scénario central de ce chantier : un appareil branché en bureau,
    câblé en fixe vers une prise murale, elle-même face au port RJ N du
    bandeau — reliée par cordon de brassage au port du switch. La couleur/
    le statut/le ciblage du PORT DU SWITCH doivent suivre toute la chaîne."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    pc_id = make_appareil(cid, nom_machine='PC-CHAINE', type_appareil='PC', adresse_ip='10.0.0.9')

    bandeau = _poser_bandeau(client)
    sw = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-CHAINE', 'nb_ports': 8,
    }).get_json()

    client.put(f"/api/baie/prise-murale/{bandeau['id']}/7", json={'appareil_id': pc_id, 'piece': 'Bureau 3'})
    lien = client.post('/api/baie/lien-port', json={
        'slot1_id': sw['id'], 'numero1': 4, 'slot2_id': bandeau['id'], 'numero2': 7,
    }).get_json()
    port_switch = lien['port1']

    assert port_switch['cible_finale'] == 'PC-CHAINE'
    assert port_switch['cible_hors_ligne'] is False
    assert port_switch['lie_appareil_id'] == pc_id
    assert port_switch['couleur'] != '#818cf8', "doit prendre la couleur du PC, pas l'indigo générique du lien"

    # "Ping toute la baie" (calcul serveur, via /api/baie/slots) doit
    # inclure le PC via ce port du switch.
    slots = client.get('/api/baie/slots?baie=Baie%20principale').get_json()['slots']
    cibles = {p['appareil_id'] or p['lie_appareil_id']
              for s in slots for p in s['ports']
              if p['appareil_id'] or p['lie_appareil_id']}
    assert pc_id in cibles

    # La prise murale, elle, garde bien son association (le lien du port RJ
    # ne l'a pas effacée — deux entités séparées).
    bandeau_frais = next(s for s in slots if s['id'] == bandeau['id'])
    port7 = next(p for p in bandeau_frais['ports'] if p['numero'] == 7)
    assert port7['prise_murale']['appareil_id'] == pc_id
    assert port7['prise_murale']['piece'] == 'Bureau 3'


def test_reduction_nb_ports_supprime_les_prises_au_dela(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    bandeau = _poser_bandeau(client, nb_ports=10)
    assert conn.execute('SELECT COUNT(*) FROM baie_prises_murales WHERE slot_id=?', (bandeau['id'],)).fetchone()[0] == 10

    r = client.put(f"/api/baie/slot/{bandeau['id']}", json={
        'position': 1, 'hauteur_u': 1, 'type_equipement': 'Bandeau RJ', 'nb_ports': 4,
    })
    assert r.status_code == 200
    assert conn.execute('SELECT COUNT(*) FROM baie_prises_murales WHERE slot_id=?', (bandeau['id'],)).fetchone()[0] == 4


def test_suppression_slot_supprime_ses_prises_murales(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    bandeau = _poser_bandeau(client, nb_ports=6)
    assert conn.execute('SELECT COUNT(*) FROM baie_prises_murales WHERE slot_id=?', (bandeau['id'],)).fetchone()[0] == 6

    r = client.delete(f"/api/baie/slot/{bandeau['id']}")
    assert r.status_code == 200
    assert conn.execute('SELECT COUNT(*) FROM baie_prises_murales WHERE slot_id=?', (bandeau['id'],)).fetchone()[0] == 0


def test_changement_de_type_supprime_les_prises_murales(client, make_client, make_user, conn):
    """Un bandeau reconfiguré en autre chose (Switch, par ex.) perd son
    système de prise murale — _reconcilier_prises_murales doit nettoyer,
    pas laisser des lignes orphelines pour un type qui n'en a plus besoin."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    bandeau = _poser_bandeau(client, nb_ports=6)

    r = client.put(f"/api/baie/slot/{bandeau['id']}", json={
        'position': 1, 'hauteur_u': 1, 'type_equipement': 'Switch', 'nb_ports': 8,
    })
    assert r.status_code == 200
    assert conn.execute('SELECT COUNT(*) FROM baie_prises_murales WHERE slot_id=?', (bandeau['id'],)).fetchone()[0] == 0


def test_suppression_appareil_libere_la_prise_murale(client, make_client, make_user, make_appareil, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid)
    bandeau = _poser_bandeau(client)
    client.put(f"/api/baie/prise-murale/{bandeau['id']}/1", json={'appareil_id': app_id})

    from conftest import get_csrf_token
    csrf = get_csrf_token(client)
    r = client.post(f'/appareil/{app_id}/supprimer', data={'csrf_token': csrf})
    assert r.status_code in (302, 200)

    row = conn.execute(
        'SELECT appareil_id FROM baie_prises_murales WHERE slot_id=? AND numero=1', (bandeau['id'],)).fetchone()
    assert row[0] is None, "la prise murale aurait dû être libérée, pas laissée orpheline"


def test_reverse_lookup_sur_fiche_appareil_via_prise_murale(client, make_client, make_user, make_appareil):
    """La fiche appareil doit lister aussi les PRISES MURALES qui le
    référencent, pas seulement les ports directs (voir le même test dans
    test_baie_ports.py pour le cas port direct) — sans quoi un appareil
    câblé via le système de prise murale disparaissait silencieusement de
    cette section depuis la migration prises murales."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='SERVEUR-MURALE')
    bandeau = _poser_bandeau(client, nom='BANDEAU-REVERSE')
    client.put(f"/api/baie/prise-murale/{bandeau['id']}/9", json={'appareil_id': app_id})

    r = client.get(f'/appareil/{app_id}/editer')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'CÂBLAGE' in body.upper()
    assert 'BANDEAU-REVERSE' in body
    assert 'Prise murale 9' in body


def test_remplacement_meme_position_conserve_les_prises_murales(client, make_client, make_user, make_appareil, conn):
    """Comme pour les ports (voir test_baie_ports.py) : ré-éditer un bandeau
    via le panneau "Placer l'équipement" ré-envoie systématiquement un POST
    — les prises murales déjà affectées ne doivent pas disparaître."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='PC-REPORT')
    bandeau1 = _poser_bandeau(client, nb_ports=4)
    client.put(f"/api/baie/prise-murale/{bandeau1['id']}/2", json={
        'appareil_id': app_id, 'piece': 'Salle A', 'identification': 'RJ 2.02',
    })

    bandeau2 = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Bandeau RJ', 'nom_custom': 'BANDEAU-RENOMME', 'nb_ports': 4,
    }).get_json()
    assert bandeau2['id'] != bandeau1['id']

    prise2 = next(p['prise_murale'] for p in bandeau2['ports'] if p['numero'] == 2)
    assert prise2['appareil_id'] == app_id, "la prise murale du port 2 aurait dû être reportée"
    assert prise2['piece'] == 'Salle A'
    assert prise2['identification'] == 'RJ 2.02'

    reste = conn.execute('SELECT COUNT(*) FROM baie_prises_murales WHERE slot_id=?', (bandeau1['id'],)).fetchone()[0]
    assert reste == 0
