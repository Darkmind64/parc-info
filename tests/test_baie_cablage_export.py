"""
Fiche de brassage exportable (page imprimable + CSV, en deux parties —
prises murales puis interconnexions) et vue mobile (lecture seule) de la
baie de brassage.
"""
from conftest import login_session


def _poser_lien(client, baie='Baie principale'):
    """Deux éléments SANS type_equipement 'Bandeau RJ' (nom_custom
    'BANDEAU-X' trompeur, volontairement conservé tel quel — les tests
    historiques de ce fichier vérifient le comportement générique des
    interconnexions, pas le cas bandeau réel, couvert séparément
    ci-dessous)."""
    s1 = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': baie, 'nom_custom': 'BANDEAU-X', 'nb_ports': 4,
    }).get_json()
    s2 = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': baie, 'nom_custom': 'SW-Y', 'nb_ports': 4,
    }).get_json()
    client.post('/api/baie/lien-port', json={
        'slot1_id': s1['id'], 'numero1': 3, 'slot2_id': s2['id'], 'numero2': 4,
        'cable_couleur': 'Bleu', 'cable_longueur': '1.5m',
    })
    return s1, s2


def test_page_cablage_liste_le_lien(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    _poser_lien(client)
    r = client.get('/baie/cablage')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'BANDEAU-X' in body
    assert 'SW-Y' in body
    assert 'Port 3' in body
    assert 'Port 4' in body
    assert 'Bleu' in body


def test_page_cablage_ne_double_pas_le_lien(client, make_client, make_user, conn):
    """Le lien est stocké bidirectionnellement (une ligne par port) — la
    liste doit en montrer UN seul, pas deux."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    _poser_lien(client)
    from app import _liste_cablage
    liens = _liste_cablage(conn, cid)
    assert len(liens) == 1


def test_page_cablage_vide_sans_lien(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    r = client.get('/baie/cablage')
    assert r.status_code == 200
    assert 'Aucun lien' in r.get_data(as_text=True)


def test_export_csv_contenu(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    _poser_lien(client)
    r = client.get('/baie/cablage.csv')
    assert r.status_code == 200
    assert 'text/csv' in r.headers['Content-Type']
    assert 'attachment' in r.headers['Content-Disposition']
    body = r.get_data(as_text=True)
    assert 'BANDEAU-X' in body
    assert 'SW-Y' in body
    assert 'Bleu' in body
    assert '1.5m' in body


def test_export_csv_isole_par_client(client, make_client, make_user):
    """Le câblage d'un autre client ne doit jamais apparaître dans cet
    export — même règle d'isolation multi-client que partout ailleurs."""
    uid1, _, _ = make_user()
    cid1 = make_client(auth_user_id=uid1)
    login_session(client, uid1, cid1)
    _poser_lien(client)

    uid2, _, _ = make_user()
    cid2 = make_client(auth_user_id=uid2)
    login_session(client, uid2, cid2)
    r = client.get('/baie/cablage.csv')
    body = r.get_data(as_text=True)
    assert 'BANDEAU-X' not in body


def test_mobile_baie_liste_les_elements(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    _poser_lien(client)
    r = client.get('/m/baie')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'BANDEAU-X' in body
    assert 'SW-Y' in body


def test_mobile_baie_affiche_la_piece(client, make_client, make_user):
    """La pièce vit désormais sur la PRISE MURALE (voir
    test_baie_prises_murales.py), pas le port RJ — la vue mobile doit
    continuer à l'afficher."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    bandeau = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Bandeau RJ', 'nom_custom': 'BANDEAU-M', 'nb_ports': 4,
    }).get_json()
    client.put(f"/api/baie/prise-murale/{bandeau['id']}/2", json={'piece': 'Salle Réunion'})
    r = client.get('/m/baie')
    assert 'Salle Réunion' in r.get_data(as_text=True)


def test_mobile_baie_vide_sans_planter(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    r = client.get('/m/baie')
    assert r.status_code == 200


# ── FICHE DE BRASSAGE : SECTION "PRISES MURALES" ────────────────────────────

def test_fiche_liste_toutes_les_prises_d_un_bandeau_meme_libres(client, make_client, make_user, conn):
    """Une fiche de brassage documente tout le panneau, pas seulement les
    prises déjà câblées — les 4 ports d'un bandeau de 4 doivent tous
    apparaître, marqués "Libre" tant que rien n'y est branché."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Bandeau RJ', 'nom_custom': 'BANDEAU-FICHE', 'nb_ports': 4,
    })
    from app import _fiche_prises_murales
    prises = _fiche_prises_murales(conn, cid)
    assert len(prises) == 4
    assert [p['numero'] for p in prises] == [1, 2, 3, 4]
    assert all(p['prise_cible'] == '' for p in prises)

    r = client.get('/baie/cablage')
    body = r.get_data(as_text=True)
    assert 'BANDEAU-FICHE' in body
    assert body.count('Libre') >= 4


def test_fiche_prise_murale_chaine_complete(client, make_client, make_user, make_appareil, conn):
    """La ligne d'une prise murale câblée doit montrer toute la chaîne :
    pièce, identification, appareil branché, et — le port RJ étant relié à
    un switch — jusqu'à la cible finale."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    pc_id = make_appareil(cid, nom_machine='PC-FICHE', type_appareil='PC')

    bandeau = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Bandeau RJ', 'nom_custom': 'BANDEAU-CHAINE', 'nb_ports': 4,
    }).get_json()
    sw = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-CHAINE', 'nb_ports': 4,
    }).get_json()
    client.put(f"/api/baie/prise-murale/{bandeau['id']}/1", json={
        'piece': 'Bureau 7', 'identification': 'RJ 1.01', 'appareil_id': pc_id,
    })
    client.post('/api/baie/lien-port', json={
        'slot1_id': bandeau['id'], 'numero1': 1, 'slot2_id': sw['id'], 'numero2': 2,
    })

    from app import _fiche_prises_murales
    prises = _fiche_prises_murales(conn, cid)
    ligne = next(p for p in prises if p['bandeau'] == 'BANDEAU-CHAINE' and p['numero'] == 1)
    assert ligne['piece'] == 'Bureau 7'
    assert ligne['identification'] == 'RJ 1.01'
    assert ligne['prise_cible'] == 'PC-FICHE'
    assert 'SW-CHAINE' in ligne['lien_cible']
    # cible_finale (voir _ports_avec_details) reflète ce qui est résolu DE
    # L'AUTRE CÔTÉ du cordon, pas l'appareil de la prise murale elle-même
    # (déjà dans prise_cible ci-dessus) — ici le port 2 du switch ne porte
    # lui-même rien de plus, donc vide, à raison (voir
    # test_chaine_prise_murale_port_rj_switch dans
    # test_baie_prises_murales.py pour le sens inverse : depuis le PORT DU
    # SWITCH, cible_finale résout bien jusqu'à PC-FICHE).
    assert ligne['cible_finale'] == ''

    r = client.get('/baie/cablage')
    body = r.get_data(as_text=True)
    assert 'Bureau 7' in body
    assert 'RJ 1.01' in body
    assert 'PC-FICHE' in body


def test_fiche_distingue_cable_mural_et_cordon(client, make_client, make_user, make_appareil, conn):
    """Deux câbles physiquement distincts sur la même ligne (voir
    baie_prises_murales dans init_db()) : le câble mural fixe (mur ->
    bandeau, sur la prise murale) et le cordon de brassage (bandeau ->
    élément relié, sur le port RJ) ne doivent jamais se confondre — bug
    trouvé en vérifiant en direct dans le navigateur (la colonne "Câble
    mural" affichait en réalité le cordon)."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='PC-CABLES')
    bandeau = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Bandeau RJ', 'nom_custom': 'BANDEAU-CABLES', 'nb_ports': 2,
    }).get_json()
    sw = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-CABLES', 'nb_ports': 2,
    }).get_json()
    client.put(f"/api/baie/prise-murale/{bandeau['id']}/1", json={
        'appareil_id': app_id, 'cable_couleur': 'Blanc', 'cable_longueur': '8m',
    })
    client.post('/api/baie/lien-port', json={
        'slot1_id': bandeau['id'], 'numero1': 1, 'slot2_id': sw['id'], 'numero2': 1,
        'cable_couleur': 'Noir', 'cable_longueur': '0.3m',
    })

    from app import _fiche_prises_murales
    ligne = next(p for p in _fiche_prises_murales(conn, cid) if p['numero'] == 1)
    assert (ligne['cable_mural_couleur'], ligne['cable_mural_longueur']) == ('Blanc', '8m')
    assert (ligne['cordon_couleur'], ligne['cordon_longueur']) == ('Noir', '0.3m')

    r = client.get('/baie/cablage')
    body = r.get_data(as_text=True)
    assert 'Blanc' in body and '8m' in body
    assert 'Noir' in body and '0.3m' in body

    r2 = client.get('/baie/cablage.csv')
    body2 = r2.get_data(as_text=True)
    assert 'Blanc · 8m' in body2
    assert 'Noir · 0.3m' in body2


def test_fiche_lien_bandeau_absent_des_interconnexions(client, make_client, make_user, conn):
    """Un lien partant d'un VRAI bandeau RJ ne doit apparaître qu'une seule
    fois — dans la section prises murales, pas dans les interconnexions
    (sinon le même câble physique serait documenté deux fois)."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    bandeau = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Bandeau RJ', 'nom_custom': 'BANDEAU-UNIQUE', 'nb_ports': 4,
    }).get_json()
    sw = client.post('/api/baie/slot', json={
        'position': 2, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-UNIQUE', 'nb_ports': 4,
    }).get_json()
    client.post('/api/baie/lien-port', json={
        'slot1_id': bandeau['id'], 'numero1': 1, 'slot2_id': sw['id'], 'numero2': 1,
    })

    from app import _liste_cablage
    liens = _liste_cablage(conn, cid)
    assert len(liens) == 0, "le lien d'un vrai bandeau RJ ne doit pas dupliquer la fiche de brassage"


def test_export_csv_deux_sections(client, make_client, make_user, make_appareil):
    """Le CSV doit contenir les deux sections, chacune avec son propre
    en-tête de colonnes."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    pc_id = make_appareil(cid, nom_machine='PC-CSV')
    # position 3 : _poser_lien() pose ses deux propres éléments en position
    # 1 et 2 — même position+colonne que celui-ci le remplacerait purement
    # et simplement (voir api_baie_ajouter_slot, édition en place), effaçant
    # ses prises murales au passage.
    bandeau = client.post('/api/baie/slot', json={
        'position': 3, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Bandeau RJ', 'nom_custom': 'BANDEAU-CSV', 'nb_ports': 2,
    }).get_json()
    client.put(f"/api/baie/prise-murale/{bandeau['id']}/1", json={'piece': 'Local Technique', 'appareil_id': pc_id})
    _poser_lien(client, baie='Baie principale')

    r = client.get('/baie/cablage.csv')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'PRISES MURALES' in body
    assert 'INTERCONNEXIONS' in body
    assert 'Local Technique' in body
    assert 'PC-CSV' in body
    assert 'BANDEAU-X' in body  # section interconnexions, voir _poser_lien
    assert 'SW-Y' in body
