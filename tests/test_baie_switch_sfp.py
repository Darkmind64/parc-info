"""
Switch : disposition des ports RJ (1 ou 2 lignes) + ports SFP (fibre).

Demandé : pouvoir afficher les ports RJ d'un switch sur une seule ligne
(comportement historique) ou sur deux lignes façon vrai switch/patch panel
(impairs en haut, pairs en dessous — 1 3 5 7.../2 4 6 8...), centrées dans
les 80% gauche de l'élément, et ajouter des ports SFP centrés dans les 20%
restants.

Les ports SFP vivent dans un espace de numérotation SÉPARÉ des ports RJ
(SFP_NUMERO_OFFSET = 1000, voir app.py) plutôt qu'une colonne type_port
supplémentaire sur baie_slot_ports — les deux plages ne se chevauchent
jamais (RJ 1-48, SFP 1001-1008), pas besoin de toucher à la contrainte
UNIQUE(slot_id, numero). Le rendu (80/20, 1/2 lignes) est couvert côté
navigateur, pas ici — ce fichier couvre uniquement l'API/la persistance.
"""
from conftest import login_session


def test_pose_switch_avec_sfp_cree_les_deux_plages(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    r = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Switch', 'nom_custom': 'SW-SFP',
        'nb_ports': 8, 'nb_ports_sfp': 4, 'ports_disposition': 'deux_lignes',
    })
    assert r.status_code == 200
    slot = r.get_json()
    assert slot['nb_ports'] == 8
    assert slot['nb_ports_sfp'] == 4
    assert slot['ports_disposition'] == 'deux_lignes'
    numeros = [p['numero'] for p in slot['ports']]
    assert numeros == [1, 2, 3, 4, 5, 6, 7, 8, 1001, 1002, 1003, 1004]


def test_nb_ports_sfp_plafonne_a_8(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    r = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Switch', 'nb_ports_sfp': 500,
    })
    slot = r.get_json()
    assert slot['nb_ports_sfp'] == 8
    assert len([p for p in slot['ports'] if p['numero'] > 1000]) == 8


def test_disposition_par_defaut_est_ligne(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    r = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Switch', 'nb_ports': 4,
    })
    assert r.get_json()['ports_disposition'] == 'ligne'


def test_disposition_invalide_retombe_sur_ligne(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    r = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Switch', 'nb_ports': 4,
        'ports_disposition': "n'importe quoi",
    })
    assert r.get_json()['ports_disposition'] == 'ligne'


def test_lier_port_sfp_a_un_appareil_resout_couleur_type(client, make_client, make_user, make_appareil):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='NAS-FIBRE', type_appareil='NAS')

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Switch', 'nb_ports_sfp': 2,
    }).get_json()

    r = client.put(f"/api/baie/slot/{slot['id']}/port/1001", json={'appareil_id': app_id})
    assert r.status_code == 200
    port = r.get_json()
    assert port['appareil_id'] == app_id
    assert port['nom_cible'] == 'NAS-FIBRE'
    assert port['couleur'] != '#334155'


def test_reduire_nb_ports_sfp_detache_sans_toucher_aux_ports_rj(client, make_client, make_user, make_appareil):
    """Bug potentiel corrigé au passage : le calcul des numéros "en trop" (à
    détacher/supprimer au remplacement d'un slot à la même position, voir
    api_baie_ajouter_slot) comparait initialement TOUS les numéros — y
    compris les numéros SFP (1001+) — au seul plafond RJ demandé, ce qui
    aurait détaché à tort tout port SFP existant dès que nb_ports (RJ) était
    inférieur à 1001 (systématiquement)."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='PC-RJ')

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Switch',
        'nb_ports': 4, 'nb_ports_sfp': 4,
    }).get_json()
    client.put(f"/api/baie/slot/{slot['id']}/port/2", json={'appareil_id': app_id})

    # Remplacement au même emplacement (POST, comme placerEquip()) avec
    # nb_ports_sfp réduit à 1 — le port RJ #2 (bien en-deçà de nb_ports=4)
    # ne doit PAS être perdu, seuls les SFP 2/3/4 doivent disparaître.
    r = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Switch',
        'nb_ports': 4, 'nb_ports_sfp': 1,
    })
    slot2 = r.get_json()
    numeros = [p['numero'] for p in slot2['ports']]
    assert numeros == [1, 2, 3, 4, 1001]
    port2 = next(p for p in slot2['ports'] if p['numero'] == 2)
    assert port2['appareil_id'] == app_id, "le port RJ #2 ne doit pas être affecté par la réduction des ports SFP"


def test_redimensionnement_conserve_sfp_et_disposition(client, make_client, make_user):
    """Les routes de redimensionnement (hauteur/largeur, PUT sur l'id du
    slot) doivent renvoyer TOUS les champs pour ne rien effacer — même
    précaution déjà en place pour nb_ports/largeur_u (voir
    redimensionnerSlot() côté client), étendue à nb_ports_sfp/
    ports_disposition."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'type_equipement': 'Switch', 'nom_custom': 'SW-R',
        'nb_ports': 8, 'nb_ports_sfp': 2, 'ports_disposition': 'deux_lignes',
    }).get_json()

    # PUT rejouant tous les champs SAUF nb_ports_sfp/ports_disposition (cas
    # d'un ancien client qui ne les enverrait pas) : le serveur retombe sur
    # ses valeurs par défaut (0/'ligne') plutôt que de planter — mais un
    # client à jour (voir redimensionnerSlot()) doit toujours les inclure,
    # ce que ce test vérifie explicitement.
    r = client.put(f"/api/baie/slot/{slot['id']}", json={
        'position': 1, 'hauteur_u': 2, 'nom_custom': 'SW-R', 'type_equipement': 'Switch',
        'nb_ports': 8, 'nb_ports_sfp': 2, 'ports_disposition': 'deux_lignes',
    })
    slot2 = r.get_json()
    assert slot2['nb_ports_sfp'] == 2
    assert slot2['ports_disposition'] == 'deux_lignes'
    assert len([p for p in slot2['ports'] if p['numero'] > 1000]) == 2
