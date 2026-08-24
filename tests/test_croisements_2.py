"""Suite de l'audit croisements de données (2026-08-24) :
  1. identifiants.utilisateur_id — un identifiant peut désormais être
     rattaché à une personne (boîte mail perso, VPN nominatif), pas
     seulement à un appareil/périphérique.
  2. baie_slots.peripherique_id — un emplacement de baie peut désormais
     référencer un vrai périphérique (onduleur, panneau de brassage) au
     lieu d'une étiquette texte libre.
  3. Nettoyage à la suppression pour toutes les colonnes ajoutées via
     ALTER TABLE (sans FK déclarée donc sans nettoyage automatique) sur
     ces deux chantiers et le précédent (service_id/utilisateur_id sur
     appareils).
"""
from conftest import get_csrf_token, login_session


# ─── 1. identifiants.utilisateur_id ────────────────────────────────────────

def test_identifiant_lie_a_un_utilisateur(client, make_user, make_client, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    util_id = conn.execute(
        "INSERT INTO utilisateurs (client_id, prenom, nom, statut) VALUES (?,?,?,'actif')",
        (cid, 'Marie', 'Curie')).lastrowid
    conn.commit()

    login_session(client, uid, cid)
    token = get_csrf_token(client)
    client.post('/identifiant/nouveau', data={
        'nom': 'Boite mail perso', 'utilisateur_id': str(util_id), 'csrf_token': token,
    })

    row = conn.execute(
        "SELECT utilisateur_id FROM identifiants WHERE nom='Boite mail perso'").fetchone()
    assert row[0] == util_id


def test_droits_utilisateur_liste_les_identifiants_lies(client, make_user, make_client, make_identifiant, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    util_id = conn.execute(
        "INSERT INTO utilisateurs (client_id, prenom, nom, statut) VALUES (?,?,?,'actif')",
        (cid, 'Marie', 'Curie')).lastrowid
    conn.commit()
    make_identifiant(cid, nom='VPN nominatif', login='mcurie', utilisateur_id=util_id)

    login_session(client, uid, cid)
    html = client.get(f'/utilisateur/{util_id}/droits').get_data(as_text=True)

    assert 'VPN nominatif' in html
    assert 'Identifiants liés' in html


def test_identifiants_isoles_par_client_utilisateur(client, make_user, make_client, make_identifiant, conn):
    uid, _l, _p = make_user(role='admin')
    cid_a = make_client(auth_user_id=uid)
    cid_b = make_client(auth_user_id=uid)
    util_b = conn.execute(
        "INSERT INTO utilisateurs (client_id, prenom, nom, statut) VALUES (?,?,?,'actif')",
        (cid_b, 'Jean', 'Dupont')).lastrowid
    conn.commit()
    make_identifiant(cid_b, nom='Compte client B', utilisateur_id=util_b)

    login_session(client, uid, cid_a)
    html = client.get(f'/utilisateur/{util_b}/droits').get_data(as_text=True)

    # Un utilisateur d'un autre client n'a pas de fiche accessible ici.
    assert 'Compte client B' not in html


# ─── 2. baie_slots.peripherique_id ──────────────────────────────────────────

def test_page_baie_propose_les_peripheriques(client, make_user, make_client, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    conn.execute("INSERT INTO peripheriques (client_id, categorie, marque, modele) VALUES (?,?,?,?)",
                 (cid, 'Onduleur / UPS', 'APC', 'Smart-UPS 1500'))
    conn.commit()

    login_session(client, uid, cid)
    html = client.get('/baie').get_data(as_text=True)

    assert 'id="cfg-periph"' in html
    assert 'APC' in html and 'Smart-UPS 1500' in html


def test_ajout_slot_avec_peripherique_lie(client, make_user, make_client, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    per_id = conn.execute(
        "INSERT INTO peripheriques (client_id, categorie, marque, modele) VALUES (?,?,?,?)",
        (cid, 'Onduleur / UPS', 'APC', 'Smart-UPS 1500')).lastrowid
    conn.commit()

    login_session(client, uid, cid)
    token = get_csrf_token(client)
    resp = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'hauteur_u': 1,
        'peripherique_id': per_id,
    }, headers={'X-CSRF-Token': token})

    data = resp.get_json()
    assert data['peripherique_id'] == per_id
    assert data['p_marque'] == 'APC'
    assert data['p_modele'] == 'Smart-UPS 1500'


def test_slot_peripherique_visible_dans_api_slots(client, make_user, make_client, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    per_id = conn.execute(
        "INSERT INTO peripheriques (client_id, categorie, marque, modele) VALUES (?,?,?,?)",
        (cid, 'Onduleur / UPS', 'APC', 'Smart-UPS 750')).lastrowid
    conn.execute(
        "INSERT INTO baie_slots (client_id, position, col_index, hauteur_u, peripherique_id, baie_nom) "
        "VALUES (?,?,?,?,?,?)", (cid, 3, 0, 1, per_id, 'Baie principale'))
    conn.commit()

    login_session(client, uid, cid)
    data = client.get('/api/baie/slots').get_json()

    slot = next(s for s in data['slots'] if s['position'] == 3)
    assert slot['p_marque'] == 'APC'
    assert slot['p_modele'] == 'Smart-UPS 750'


def test_slot_peripherique_isole_par_client(client, make_user, make_client, conn):
    uid, _l, _p = make_user(role='admin')
    cid_a = make_client(auth_user_id=uid)
    cid_b = make_client(auth_user_id=uid)
    per_b = conn.execute(
        "INSERT INTO peripheriques (client_id, categorie, marque, modele) VALUES (?,?,?,?)",
        (cid_b, 'Onduleur / UPS', 'Eaton', '9SX')).lastrowid
    conn.execute(
        "INSERT INTO baie_slots (client_id, position, col_index, hauteur_u, peripherique_id, baie_nom) "
        "VALUES (?,?,?,?,?,?)", (cid_b, 1, 0, 1, per_b, 'Baie principale'))
    conn.commit()

    login_session(client, uid, cid_a)
    data = client.get('/api/baie/slots').get_json()

    assert data['slots'] == []


# ─── 3. Nettoyage à la suppression (colonnes ALTER TABLE sans FK) ──────────

def test_suppression_appareil_nettoie_identifiants_lies(client, make_user, make_client, make_appareil, make_identifiant, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid)
    make_identifiant(cid, nom='Session Windows', appareil_id=aid)

    login_session(client, uid, cid)
    token = get_csrf_token(client)
    client.post(f'/appareil/{aid}/supprimer', data={'csrf_token': token})

    row = conn.execute("SELECT appareil_id FROM identifiants WHERE nom='Session Windows'").fetchone()
    assert row[0] is None


def test_suppression_peripherique_nettoie_identifiants_et_baie(client, make_user, make_client, make_identifiant, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    per_id = conn.execute(
        "INSERT INTO peripheriques (client_id, categorie, marque, modele) VALUES (?,?,?,?)",
        (cid, 'Onduleur / UPS', 'APC', 'Smart-UPS 1500')).lastrowid
    conn.execute(
        "INSERT INTO baie_slots (client_id, position, col_index, hauteur_u, peripherique_id, baie_nom) "
        "VALUES (?,?,?,?,?,?)", (cid, 5, 0, 1, per_id, 'Baie principale'))
    conn.commit()
    make_identifiant(cid, nom='Console UPS', peripherique_id=per_id)

    login_session(client, uid, cid)
    token = get_csrf_token(client)
    client.post(f'/peripherique/{per_id}/supprimer', data={'csrf_token': token})

    assert conn.execute("SELECT peripherique_id FROM identifiants WHERE nom='Console UPS'").fetchone()[0] is None
    assert conn.execute("SELECT peripherique_id FROM baie_slots WHERE position=5").fetchone()[0] is None


def test_suppression_utilisateur_nettoie_appareils_et_identifiants(client, make_user, make_client, make_appareil, make_identifiant, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    util_id = conn.execute(
        "INSERT INTO utilisateurs (client_id, prenom, nom, statut) VALUES (?,?,?,'actif')",
        (cid, 'Marie', 'Curie')).lastrowid
    conn.commit()
    aid = make_appareil(cid, utilisateur_id=util_id)
    make_identifiant(cid, nom='Compte perso', utilisateur_id=util_id)

    login_session(client, uid, cid)
    token = get_csrf_token(client)
    client.post(f'/utilisateur/{util_id}/supprimer', data={'csrf_token': token})

    assert conn.execute("SELECT utilisateur_id FROM appareils WHERE id=?", (aid,)).fetchone()[0] is None
    assert conn.execute("SELECT utilisateur_id FROM identifiants WHERE nom='Compte perso'").fetchone()[0] is None


def test_suppression_service_nettoie_appareils_lies(client, make_user, make_client, make_appareil, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    svc_id = conn.execute("INSERT INTO services (client_id, nom) VALUES (?,?)", (cid, 'IT')).lastrowid
    conn.commit()
    aid = make_appareil(cid, service_id=svc_id)

    login_session(client, uid, cid)
    token = get_csrf_token(client)
    client.post(f'/service/{svc_id}/supprimer', data={'csrf_token': token})

    assert conn.execute("SELECT service_id FROM appareils WHERE id=?", (aid,)).fetchone()[0] is None
