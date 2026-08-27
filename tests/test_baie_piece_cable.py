"""
Pièce du site (bandeau RJ) et étiquette de câble — deux métadonnées
indépendantes de la cible d'un port (appareil/périphérique/usage_libre/
lien port-à-port), et le chaînage complet qu'elles permettent (pièce ->
port bandeau -> câble -> port switch -> appareil, avec alerte si l'appareil
au bout de la chaîne est hors ligne).
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
    """Assigner une pièce à un port de bandeau, puis le câbler à un switch,
    puis réassigner sa cible : la pièce ne doit JAMAIS disparaître — c'était
    le défaut de l'ancien design (usage_libre réutilisé, effacé par tout
    changement de cible)."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    bandeau, switch = _poser_bandeau_et_switch(client, cid)

    r = client.put(f"/api/baie/slot/{bandeau['id']}/port/5", json={'piece': 'Salle 202'})
    port = r.get_json()
    assert port['piece'] == 'Salle 202'
    assert port['appareil_id'] is None  # non touché

    # Câblage vers le switch — la pièce doit survivre.
    lien = client.post('/api/baie/lien-port', json={
        'slot1_id': bandeau['id'], 'numero1': 5, 'slot2_id': switch['id'], 'numero2': 1,
    }).get_json()
    assert lien['port1']['piece'] == 'Salle 202'
    assert lien['port1']['lie_slot_id'] == switch['id']

    # Réassigner explicitement une cible (usage libre) détache le lien mais
    # ne touche toujours pas à la pièce.
    app_id = make_appareil(cid)
    r2 = client.put(f"/api/baie/slot/{bandeau['id']}/port/5", json={'appareil_id': app_id})
    port2 = r2.get_json()
    assert port2['piece'] == 'Salle 202'
    assert port2['appareil_id'] == app_id
    assert port2['lie_slot_id'] is None


def test_maj_piece_seule_ne_touche_pas_la_cible_ni_le_lien(client, make_client, make_user):
    """Non-régression du bug trouvé en cours d'implémentation : un payload
    {piece: ...} SEUL ne doit jamais réinitialiser appareil_id/
    peripherique_id/usage_libre/lie_slot_id — la route distinguait mal
    "on ne fournit pas ces clés" de "on les vide explicitement"."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    bandeau, switch = _poser_bandeau_et_switch(client, cid)
    client.post('/api/baie/lien-port', json={
        'slot1_id': bandeau['id'], 'numero1': 3, 'slot2_id': switch['id'], 'numero2': 2,
    })
    r = client.put(f"/api/baie/slot/{bandeau['id']}/port/3", json={'piece': 'Bureau 12'})
    port = r.get_json()
    assert port['piece'] == 'Bureau 12'
    assert port['lie_slot_id'] == switch['id'], "un payload piece-only n'aurait jamais dû détacher le lien"


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
    conn.execute('UPDATE appareils SET en_ligne=0 WHERE id=?', (app_id,))
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
