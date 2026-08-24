"""Lien appareils <-> services/utilisateurs (audit du 2026-08-24,
proposition #4) : appareils.service_id/utilisateur_id (nullable, en plus
des colonnes texte existantes service/utilisateur), résolus automatiquement
par rapprochement de nom à chaque sauvegarde — même principe que
peripheriques_appareils en son temps pour appareil_id. Permet enfin
« quels appareils pour ce service/cet utilisateur » sans matcher du texte.
"""
import app
from conftest import get_csrf_token, login_session


def test_migration_rattrape_les_appareils_deja_saisis(client, make_user, make_client, make_appareil, conn):
    """La migration de app.py:init_db() (pas la sauvegarde d'un formulaire)
    rattrape les appareils déjà saisis avant l'ajout de service_id/
    utilisateur_id — reproduit exactement ce que peripheriques_appareils a
    fait en son temps pour appareil_id."""
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    conn.execute("INSERT INTO services (client_id, nom) VALUES (?,?)", (cid, 'Marketing'))
    conn.commit()
    aid = make_appareil(cid, nom_machine='PC-DEJA-SAISI', service='Marketing')

    app.init_db()  # rejoue la migration, comme au démarrage d'une instance mise à jour

    row = conn.execute("SELECT service_id FROM appareils WHERE id=?", (aid,)).fetchone()
    svc_id = conn.execute("SELECT id FROM services WHERE nom='Marketing'").fetchone()[0]
    assert row[0] == svc_id


def test_creation_resout_service_id_par_nom(client, make_user, make_client, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    conn.execute("INSERT INTO services (client_id, nom) VALUES (?,?)", (cid, 'Comptabilite'))
    conn.commit()

    login_session(client, uid, cid)
    token = get_csrf_token(client)
    client.post('/appareil/nouveau', data={
        'nom_machine': 'PC-COMPTA-01', 'service': 'Comptabilite', 'csrf_token': token,
    })

    row = conn.execute(
        "SELECT a.service_id, s.nom FROM appareils a JOIN services s ON s.id=a.service_id "
        "WHERE a.nom_machine='PC-COMPTA-01'").fetchone()
    assert row is not None
    assert row[1] == 'Comptabilite'


def test_creation_resout_utilisateur_id_par_nom(client, make_user, make_client, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    conn.execute("INSERT INTO utilisateurs (client_id, prenom, nom, statut) VALUES (?,?,?,'actif')",
                 (cid, 'Jean', 'Dupont'))
    conn.commit()

    login_session(client, uid, cid)
    token = get_csrf_token(client)
    client.post('/appareil/nouveau', data={
        'nom_machine': 'PC-JDUPONT', 'utilisateur': 'Jean Dupont', 'csrf_token': token,
    })

    row = conn.execute(
        "SELECT a.utilisateur_id, u.nom FROM appareils a JOIN utilisateurs u ON u.id=a.utilisateur_id "
        "WHERE a.nom_machine='PC-JDUPONT'").fetchone()
    assert row is not None
    assert row[1] == 'Dupont'


def test_service_sans_correspondance_laisse_service_id_null(client, make_user, make_client, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)

    login_session(client, uid, cid)
    token = get_csrf_token(client)
    client.post('/appareil/nouveau', data={
        'nom_machine': 'PC-ORPHELIN', 'service': 'Service qui n existe pas', 'csrf_token': token,
    })

    row = conn.execute("SELECT service_id FROM appareils WHERE nom_machine='PC-ORPHELIN'").fetchone()
    assert row[0] is None


def test_edition_met_a_jour_service_id_quand_le_texte_change(client, make_user, make_client, make_appareil, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    conn.execute("INSERT INTO services (client_id, nom) VALUES (?,?)", (cid, 'RH'))
    conn.commit()
    aid = make_appareil(cid, nom_machine='PC-01', service='')

    login_session(client, uid, cid)
    token = get_csrf_token(client)
    client.post(f'/appareil/{aid}/editer', data={
        'nom_machine': 'PC-01', 'service': 'RH', 'csrf_token': token,
    })

    row = conn.execute("SELECT service_id FROM appareils WHERE id=?", (aid,)).fetchone()
    svc_id = conn.execute("SELECT id FROM services WHERE nom='RH'").fetchone()[0]
    assert row[0] == svc_id


def test_services_liste_compte_les_appareils_lies(client, make_user, make_client, make_appareil, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    svc_id = conn.execute(
        "INSERT INTO services (client_id, nom) VALUES (?,?)", (cid, 'IT')).lastrowid
    conn.commit()
    make_appareil(cid, nom_machine='SRV-A', service_id=svc_id)
    make_appareil(cid, nom_machine='SRV-B', service_id=svc_id)
    make_appareil(cid, nom_machine='SRV-C')  # pas lié

    login_session(client, uid, cid)
    html = client.get('/services').get_data(as_text=True)

    assert '2 appareil(s)' in html


def test_filtre_appareils_par_service(client, make_user, make_client, make_appareil, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    svc_id = conn.execute(
        "INSERT INTO services (client_id, nom) VALUES (?,?)", (cid, 'IT')).lastrowid
    conn.commit()
    make_appareil(cid, nom_machine='DANS-LE-SERVICE', service_id=svc_id)
    make_appareil(cid, nom_machine='HORS-SERVICE')

    login_session(client, uid, cid)
    html = client.get(f'/appareils?service={svc_id}').get_data(as_text=True)

    assert 'DANS-LE-SERVICE' in html
    assert 'HORS-SERVICE' not in html


def test_droits_utilisateur_liste_les_appareils_affectes(client, make_user, make_client, make_appareil, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    util_id = conn.execute(
        "INSERT INTO utilisateurs (client_id, prenom, nom, statut) VALUES (?,?,?,'actif')",
        (cid, 'Marie', 'Curie')).lastrowid
    conn.commit()
    make_appareil(cid, nom_machine='LAPTOP-MCURIE', utilisateur_id=util_id)

    login_session(client, uid, cid)
    html = client.get(f'/utilisateur/{util_id}/droits').get_data(as_text=True)

    assert 'LAPTOP-MCURIE' in html


def test_resolution_isolee_par_client(client, make_user, make_client, conn):
    """Le nom d'un service du client B ne doit jamais résoudre service_id
    pour un appareil créé sous le client A, même si le nom est identique."""
    uid, _l, _p = make_user(role='admin')
    cid_a = make_client(auth_user_id=uid)
    cid_b = make_client(auth_user_id=uid)
    conn.execute("INSERT INTO services (client_id, nom) VALUES (?,?)", (cid_b, 'Comptabilite'))
    conn.commit()

    login_session(client, uid, cid_a)
    token = get_csrf_token(client)
    client.post('/appareil/nouveau', data={
        'nom_machine': 'PC-CLIENT-A', 'service': 'Comptabilite', 'csrf_token': token,
    })

    row = conn.execute("SELECT service_id FROM appareils WHERE nom_machine='PC-CLIENT-A'").fetchone()
    assert row[0] is None
