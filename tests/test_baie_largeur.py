"""
Largeur d'un élément de la baie de brassage — baie_slots.largeur_u.

Couvre : persistance à la création/mise à jour, bornage 1-10, valeur par
défaut NULL ("jamais redimensionné, partage égal automatique" — voir le
commentaire de migration dans app.py:init_db()), et la non-régression du
bug corrigé au passage : redimensionner un élément en HAUTEUR (PUT
/api/baie/slot/<id>) ne doit jamais effacer ses ports ni sa largeur déjà
choisie, même si le client ne renvoie que les champs qu'il modifie vraiment.
"""
from conftest import login_session


def test_creation_largeur_u_persiste(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-LARGEUR', 'largeur_u': 6,
    }).get_json()
    assert slot['largeur_u'] == 6


def test_largeur_u_defaut_null_si_non_precisee(client, make_client, make_user):
    """Un élément jamais redimensionné en largeur doit rester NULL (pas 10) —
    c'est ce qui déclenche le partage égal automatique côté rendu client,
    indispensable pour ne pas casser l'affichage historique de plusieurs
    éléments déjà placés côte à côte via col_index."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 0,
    }).get_json()
    assert slot['largeur_u'] is None


def test_largeur_u_bornee_1_10(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    trop_grand = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'largeur_u': 25,
    }).get_json()
    assert trop_grand['largeur_u'] == 10

    trop_petit = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale', 'largeur_u': -3,
    }).get_json()
    assert trop_petit['largeur_u'] == 1

    zero = client.post('/api/baie/slot', json={
        'position': 3, 'col_index': 0, 'baie_nom': 'Baie principale', 'largeur_u': 0,
    }).get_json()
    assert zero['largeur_u'] is None


def test_put_largeur_u_modifie_le_slot_existant(client, make_client, make_user):
    """redimensionnerLargeurSlot() : PUT sur le slot existant (même id),
    largeur_u seule change."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-LARGEUR',
    }).get_json()
    assert slot['largeur_u'] is None

    r = client.put(f"/api/baie/slot/{slot['id']}", json={
        'position': 1, 'hauteur_u': 1, 'nom_custom': 'SW-LARGEUR',
        'type_equipement': 'Switch', 'largeur_u': 7,
    })
    updated = r.get_json()
    assert updated['id'] == slot['id']
    assert updated['largeur_u'] == 7


def test_redimensionnement_hauteur_ne_wipe_pas_ports_ni_largeur(client, make_client, make_user, conn):
    """Non-régression : le PUT générique (position/hauteur/etc écrasent TOUTE
    la ligne) doit recevoir nb_ports et largeur_u tels quels pour ne pas les
    remettre à 0/NULL à chaque redimensionnement en hauteur — reproduit ici
    ce que redimensionnerSlot() envoie côté client depuis le correctif."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-H', 'nb_ports': 12, 'largeur_u': 6,
    }).get_json()
    assert len(slot['ports']) == 12
    assert slot['largeur_u'] == 6

    # Redimensionnement en hauteur : le client renvoie nb_ports et largeur_u
    # inchangés (voir redimensionnerSlot() dans baie_brassage.html).
    r = client.put(f"/api/baie/slot/{slot['id']}", json={
        'position': 1, 'hauteur_u': 3, 'nom_custom': 'SW-H', 'type_equipement': 'Switch',
        'nb_ports': slot['nb_ports'], 'largeur_u': slot['largeur_u'],
    })
    updated = r.get_json()
    assert updated['hauteur_u'] == 3
    assert len(updated['ports']) == 12, "les ports n'auraient pas dû disparaître"
    assert updated['largeur_u'] == 6, "la largeur n'aurait pas dû disparaître"

    reste = conn.execute(
        'SELECT COUNT(*) FROM baie_slot_ports WHERE slot_id=?', (slot['id'],)).fetchone()[0]
    assert reste == 12
