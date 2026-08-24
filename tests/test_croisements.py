"""Croisements de données appareil <-> contrats/services (audit du
2026-08-24) :
  1. /api/contrats/appareil/<id> n'affichait que les contrats liés via le
     pivot contrats_appareils — les contrats spécifiques à l'antivirus/EDR/
     RMM (av_contrat_id, edr_contrat_id, rmm_contrat_id) étaient invisibles
     dans « Contrats liés » tant qu'ils n'étaient pas *aussi* ajoutés
     manuellement au pivot. Voir _contrats_appareil() dans app.py.
  2. Le champ Service de la fiche appareil est du texte libre sans le
     moindre datalist, contrairement au champ Utilisateur juste à côté.
"""
from conftest import login_session


def test_contrat_lie_via_pivot_seul(client, make_user, make_client, make_appareil, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid)
    ctr = conn.execute("INSERT INTO contrats (client_id, titre) VALUES (?,?)", (cid, 'Maintenance générale'))
    conn.execute("INSERT INTO contrats_appareils (contrat_id, appareil_id) VALUES (?,?)",
                 (ctr.lastrowid, aid))
    conn.commit()

    login_session(client, uid, cid)
    items = client.get(f'/api/contrats/appareil/{aid}').get_json()

    assert len(items) == 1
    assert items[0]['titre'] == 'Maintenance générale'
    assert items[0]['roles'] == ['Général']


def test_contrat_lie_via_av_contrat_id_seul_est_visible(client, make_user, make_client, make_appareil, conn):
    """Le bug corrigé : avant, un contrat lié uniquement via av_contrat_id
    n'apparaissait jamais ici."""
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    ctr = conn.execute("INSERT INTO contrats (client_id, titre) VALUES (?,?)", (cid, 'Licence antivirus'))
    aid = make_appareil(cid, av_contrat_id=ctr.lastrowid)

    login_session(client, uid, cid)
    items = client.get(f'/api/contrats/appareil/{aid}').get_json()

    assert len(items) == 1
    assert items[0]['titre'] == 'Licence antivirus'
    assert items[0]['roles'] == ['Antivirus']


def test_contrat_lie_par_les_deux_mecanismes_cumule_les_roles(client, make_user, make_client, make_appareil, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    ctr = conn.execute("INSERT INTO contrats (client_id, titre) VALUES (?,?)", (cid, 'Contrat global'))
    ctr_id = ctr.lastrowid
    aid = make_appareil(cid, av_contrat_id=ctr_id, edr_contrat_id=ctr_id)
    conn.execute("INSERT INTO contrats_appareils (contrat_id, appareil_id) VALUES (?,?)", (ctr_id, aid))
    conn.commit()

    login_session(client, uid, cid)
    items = client.get(f'/api/contrats/appareil/{aid}').get_json()

    assert len(items) == 1
    assert set(items[0]['roles']) == {'Général', 'Antivirus', 'EDR'}


def test_contrats_isoles_par_client(client, make_user, make_client, make_appareil, conn):
    uid, _l, _p = make_user(role='admin')
    cid_a = make_client(auth_user_id=uid)
    cid_b = make_client(auth_user_id=uid)
    ctr_b = conn.execute("INSERT INTO contrats (client_id, titre) VALUES (?,?)", (cid_b, 'Contrat du client B'))
    aid_a = make_appareil(cid_a, av_contrat_id=ctr_b.lastrowid)  # id d'un contrat d'un AUTRE client

    login_session(client, uid, cid_a)
    items = client.get(f'/api/contrats/appareil/{aid_a}').get_json()

    assert items == []


def test_datalist_services_alimente_depuis_la_table_services(client, make_user, make_client, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    conn.execute("INSERT INTO services (client_id, nom) VALUES (?,?)", (cid, 'Comptabilité'))
    conn.commit()

    login_session(client, uid, cid)
    html = client.get('/appareil/nouveau').get_data(as_text=True)

    assert 'id="datalist-services"' in html
    assert '<option value="Comptabilité">' in html
    # Le champ reste un texte libre : la saisie manuelle marche toujours.
    assert 'list="datalist-services"' in html
