"""
Pièce du site et étiquette de câble d'une PRISE MURALE (bandeau RJ) —
entité séparée du port RJ depuis la 2.18.74 (voir test_baie_prises_murales.py
pour sa couverture dédiée), et le chaînage complet qu'elles permettent
(pièce -> prise murale -> port bandeau -> câble -> port switch -> appareil,
avec alerte si l'appareil au bout de la chaîne est hors ligne).
"""
from conftest import login_session


def _poser_bandeau_et_switch(client, cid):
    bandeau = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Bandeau RJ', 'nom_custom': 'Bandeau RJ', 'nb_ports': 24,
    }).get_json()
    switch = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-CORE', 'nb_ports': 8,
    }).get_json()
    return bandeau, switch


def test_piece_persiste_independamment_de_la_cible(client, make_client, make_user, make_appareil):
    """Assigner une pièce à une PRISE MURALE, câbler le port RJ correspondant
    à un switch, puis réassigner la cible de la prise : la pièce ne doit
    JAMAIS disparaître, et le câblage RJ (interconnexion) reste totalement
    indépendant de la prise murale (entité séparée depuis la 2.18.74)."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    bandeau, switch = _poser_bandeau_et_switch(client, cid)

    r = client.put(f"/api/baie/prise-murale/{bandeau['id']}/5", json={'piece': 'Salle 202'})
    prise = r.get_json()
    assert prise['piece'] == 'Salle 202'
    assert prise['appareil_id'] is None  # non touché

    # Câblage du PORT RJ (interconnexion) vers le switch — la pièce de la
    # prise murale doit survivre, et le port RJ ne doit RIEN porter d'autre.
    lien = client.post('/api/baie/lien-port', json={
        'slot1_id': bandeau['id'], 'numero1': 5, 'slot2_id': switch['id'], 'numero2': 1,
    }).get_json()
    assert lien['port1']['lie_slot_id'] == switch['id']
    assert lien['port1']['appareil_id'] is None
    ports = client.get('/api/baie/slots?baie=Baie%20principale').get_json()['slots']
    bandeau_frais = next(s for s in ports if s['id'] == bandeau['id'])
    prise5 = next(p for p in bandeau_frais['ports'] if p['numero'] == 5)['prise_murale']
    assert prise5['piece'] == 'Salle 202'

    # Réassigner explicitement une cible sur la PRISE MURALE ne touche
    # jamais au lien du port RJ (deux entités séparées).
    app_id = make_appareil(cid)
    r2 = client.put(f"/api/baie/prise-murale/{bandeau['id']}/5", json={'appareil_id': app_id})
    prise2 = r2.get_json()
    assert prise2['piece'] == 'Salle 202'
    assert prise2['appareil_id'] == app_id
    port5_apres = client.get('/api/baie/slots?baie=Baie%20principale').get_json()['slots']
    bandeau_apres = next(s for s in port5_apres if s['id'] == bandeau['id'])
    port5 = next(p for p in bandeau_apres['ports'] if p['numero'] == 5)
    assert port5['lie_slot_id'] == switch['id'], "le lien du port RJ ne doit pas être touché par la prise murale"


def test_maj_piece_seule_ne_touche_pas_la_cible_ni_le_lien(client, make_client, make_user):
    """Non-régression : un payload {piece: ...} SEUL sur une prise murale ne
    doit jamais réinitialiser son appareil_id/peripherique_id/usage_libre,
    ni toucher au lien du port RJ correspondant — la route distinguait mal
    "on ne fournit pas ces clés" de "on les vide explicitement"."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    bandeau, switch = _poser_bandeau_et_switch(client, cid)
    client.post('/api/baie/lien-port', json={
        'slot1_id': bandeau['id'], 'numero1': 3, 'slot2_id': switch['id'], 'numero2': 2,
    })
    r = client.put(f"/api/baie/prise-murale/{bandeau['id']}/3", json={'piece': 'Bureau 12'})
    prise = r.get_json()
    assert prise['piece'] == 'Bureau 12'
    ports = client.get('/api/baie/slots?baie=Baie%20principale').get_json()['slots']
    bandeau_frais = next(s for s in ports if s['id'] == bandeau['id'])
    port3 = next(p for p in bandeau_frais['ports'] if p['numero'] == 3)
    assert port3['lie_slot_id'] == switch['id'], "un payload piece-only n'aurait jamais dû détacher le lien du port RJ"


def test_etiquette_cable_persiste_et_editable(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    bandeau, switch = _poser_bandeau_et_switch(client, cid)

    lien = client.post('/api/baie/lien-port', json={
        'slot1_id': bandeau['id'], 'numero1': 1, 'slot2_id': switch['id'], 'numero2': 1,
        'cable_couleur': 'Bleu', 'cable_longueur': '2m',
    }).get_json()
    assert lien['port1']['cable_couleur'] == 'Bleu'
    assert lien['port1']['cable_longueur'] == '2m'
    assert lien['port2']['cable_couleur'] == 'Bleu'  # même étiquette des deux côtés

    # Ré-appeler la même route avec le même couple mais une étiquette
    # différente doit la mettre à jour sans casser le lien (changerCableLabel()
    # rejoue systématiquement le couple courant côté client).
    lien2 = client.post('/api/baie/lien-port', json={
        'slot1_id': bandeau['id'], 'numero1': 1, 'slot2_id': switch['id'], 'numero2': 1,
        'cable_couleur': 'Rouge', 'cable_longueur': '3m',
    }).get_json()
    assert lien2['port1']['cable_couleur'] == 'Rouge'
    assert lien2['port1']['lie_slot_id'] == switch['id']


def test_etiquette_cable_effacee_au_detachement(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    bandeau, switch = _poser_bandeau_et_switch(client, cid)
    client.post('/api/baie/lien-port', json={
        'slot1_id': bandeau['id'], 'numero1': 1, 'slot2_id': switch['id'], 'numero2': 1,
        'cable_couleur': 'Bleu', 'cable_longueur': '2m',
    })
    r = client.put(f"/api/baie/slot/{bandeau['id']}/port/1", json={'usage_libre': 'Réservé'})
    port = r.get_json()
    assert port['cable_couleur'] == ''
    assert port['cable_longueur'] == ''


def test_chaine_complete_cible_finale_et_statut(client, make_client, make_user, make_appareil, conn):
    """Chaîne : port de bandeau câblé à un port de switch, l'ÉLÉMENT switch
    lui-même associé à un appareil (rack-mounted — "Associer à un appareil"
    du panneau, indépendant des cibles individuelles de ses ports, qui elles
    restent mutuellement exclusives avec un lien port-à-port) — la bulle
    doit pouvoir afficher la cible finale ET son statut, sans naviguer
    élément par élément."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='SRV-CORE')
    # dernier_ping renseigné : en_ligne=0 seul est indiscernable d'un
    # appareil jamais pingé (voir _ports_avec_details) — il faut les DEUX
    # pour simuler un échec de ping CONFIRMÉ, pas une absence de ping.
    conn.execute("UPDATE appareils SET en_ligne=0, dernier_ping=? WHERE id=?",
                 ('2026-01-01T00:00:00', app_id))
    conn.commit()

    bandeau, switch = _poser_bandeau_et_switch(client, cid)
    # Associe le SWITCH (l'élément, pas un de ses ports) à l'appareil.
    client.put(f"/api/baie/slot/{switch['id']}", json={
        'position': 2, 'hauteur_u': 1, 'nom_custom': 'SW-CORE', 'type_equipement': 'Switch',
        'appareil_id': app_id, 'nb_ports': 8,
    })
    lien = client.post('/api/baie/lien-port', json={
        'slot1_id': bandeau['id'], 'numero1': 1, 'slot2_id': switch['id'], 'numero2': 1,
    }).get_json()

    port_bandeau = lien['port1']
    assert port_bandeau['cible_finale'] == 'SRV-CORE'
    assert port_bandeau['cible_hors_ligne'] is True

    conn.execute('UPDATE appareils SET en_ligne=1 WHERE id=?', (app_id,))
    conn.commit()
    ports = client.get(f"/api/baie/slots?baie=Baie%20principale").get_json()['slots']
    bandeau_row = next(s for s in ports if s['id'] == bandeau['id'])
    port1 = next(p for p in bandeau_row['ports'] if p['numero'] == 1)
    assert port1['cible_finale'] == 'SRV-CORE'
    assert port1['cible_hors_ligne'] is False
