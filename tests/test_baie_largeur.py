"""
Largeur d'un élément de la baie de brassage — baie_slots.largeur_u.

Couvre : persistance à la création/mise à jour, bornage 1-NB_COLS_BAIE,
valeur par défaut NB_COLS_BAIE (pleine largeur — voir _clamp_largeur_u dans
app.py : chaque emplacement a désormais TOUJOURS une largeur explicite, plus
de partage égal automatique implicite entre éléments d'une même rangée, un
design qui cassait dès qu'un élément à la fois partiel en largeur ET en
hauteur (hauteur_u > 1) partageait sa rangée avec un autre — voir
renderRack() côté client), et la non-régression du bug corrigé au passage :
redimensionner un élément en HAUTEUR (PUT /api/baie/slot/<id>) ne doit jamais
effacer ses ports ni sa largeur déjà choisie, même si le client ne renvoie
que les champs qu'il modifie vraiment.

NB_COLS_BAIE = 1000 (grille horizontale fine, était 10 — voir app.py et
baie_brassage.html : un redimensionnement par crans de 10 % se sentait
"cassé" comparé à la maquette, en continu ; 1000 donne un pas <1px, perçu
comme continu, sans changer le modèle de données (toujours un entier)).
"""
from conftest import login_session


def test_creation_largeur_u_persiste(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-LARGEUR', 'largeur_u': 600,
    }).get_json()
    assert slot['largeur_u'] == 600


def test_largeur_u_defaut_pleine_largeur_si_non_precisee(client, make_client, make_user):
    """Un élément jamais redimensionné en largeur vaut NB_COLS_BAIE/NB_COLS_BAIE
    (pleine largeur) par défaut — plus de sentinelle NULL/"auto" depuis le
    passage à la grille à positions absolues (voir _clamp_largeur_u)."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'nb_ports': 0,
    }).get_json()
    assert slot['largeur_u'] == 1000


def test_largeur_u_bornee_1_1000(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    trop_grand = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale', 'largeur_u': 2500,
    }).get_json()
    assert trop_grand['largeur_u'] == 1000

    trop_petit = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale', 'largeur_u': -3,
    }).get_json()
    assert trop_petit['largeur_u'] == 1

    zero = client.post('/api/baie/slot', json={
        'position': 3, 'col_index': 0, 'baie_nom': 'Baie principale', 'largeur_u': 0,
    }).get_json()
    assert zero['largeur_u'] == 1000


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
    assert slot['largeur_u'] == 1000

    r = client.put(f"/api/baie/slot/{slot['id']}", json={
        'position': 1, 'hauteur_u': 1, 'nom_custom': 'SW-LARGEUR',
        'type_equipement': 'Switch', 'largeur_u': 700,
    })
    updated = r.get_json()
    assert updated['id'] == slot['id']
    assert updated['largeur_u'] == 700


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
        'type_equipement': 'Switch', 'nom_custom': 'SW-H', 'nb_ports': 12, 'largeur_u': 600,
    }).get_json()
    assert len(slot['ports']) == 12
    assert slot['largeur_u'] == 600

    # Redimensionnement en hauteur : le client renvoie nb_ports et largeur_u
    # inchangés (voir redimensionnerSlot() dans baie_brassage.html).
    r = client.put(f"/api/baie/slot/{slot['id']}", json={
        'position': 1, 'hauteur_u': 3, 'nom_custom': 'SW-H', 'type_equipement': 'Switch',
        'nb_ports': slot['nb_ports'], 'largeur_u': slot['largeur_u'],
    })
    updated = r.get_json()
    assert updated['hauteur_u'] == 3
    assert len(updated['ports']) == 12, "les ports n'auraient pas dû disparaître"
    assert updated['largeur_u'] == 600, "la largeur n'aurait pas dû disparaître"

    reste = conn.execute(
        'SELECT COUNT(*) FROM baie_slot_ports WHERE slot_id=?', (slot['id'],)).fetchone()[0]
    assert reste == 12


def test_largeur_u_plafonnee_par_col_index_a_la_creation(client, make_client, make_user):
    """col_index + largeur_u ne doit jamais dépasser NB_COLS_BAIE : au-delà,
    le grid-column CSS déborde de la grille du rack (pistes implicites
    créées par le navigateur, décalant tout le reste de la rangée) —
    signalé en usage réel avec un élément placé en fin de grille."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 999, 'largeur_u': 5, 'baie_nom': 'Baie principale',
    }).get_json()
    assert slot['col_index'] == 999
    assert slot['largeur_u'] == 1, "1000 - col_index(999) = 1, la largeur demandée (5) doit être plafonnée"


def test_largeur_u_plafonnee_par_col_index_a_la_modification(client, make_client, make_user):
    """Même plafond via la route PUT générique (redimensionnement) — celle-ci
    ne reçoit pas col_index (non modifiable par cette route) et doit donc
    relire la valeur ACTUELLE en base pour plafonner correctement."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 997, 'largeur_u': 2, 'baie_nom': 'Baie principale',
    }).get_json()
    assert slot['col_index'] == 997

    r = client.put(f"/api/baie/slot/{slot['id']}", json={
        'position': 1, 'hauteur_u': 1, 'largeur_u': 8,
    })
    updated = r.get_json()
    assert updated['col_index'] == 997, "col_index n'est pas censé changer via cette route"
    assert updated['largeur_u'] == 3, "1000 - col_index(997) = 3, la largeur demandée (8) doit être plafonnée"


def test_deplacer_slot_plafonne_col_index_selon_largeur_existante(client, make_client, make_user):
    """POST /api/baie/slot/<id>/deplacer (drag & drop) ne reçoit ni ne
    modifie largeur_u — mais la nouvelle col_index doit rester compatible
    avec la largeur DÉJÀ choisie du slot, sinon le déplacement ferait
    déborder un élément large vers la fin de la grille (ex : un élément
    déposé tout près du bord droit depuis la grille magnétique)."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'largeur_u': 4, 'baie_nom': 'Baie principale',
    }).get_json()
    assert slot['largeur_u'] == 4

    r = client.post(f"/api/baie/slot/{slot['id']}/deplacer", json={
        'position': 2, 'col_index': 999,
    })
    updated = r.get_json()
    assert updated['position'] == 2
    assert updated['col_index'] == 996, "1000 - largeur_u(4) = 996, la colonne visée (999) doit être plafonnée"
    assert updated['largeur_u'] == 4, "la largeur ne doit pas changer lors d'un simple déplacement"
