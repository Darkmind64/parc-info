"""Historique des modifications par appareil : /api/historique/entite/... et
l'annulation associée (déjà branchés dans form_appareil.html — voir la
discussion en conversation, ce n'était pas manquant, seulement pas testé).
"""
from conftest import get_csrf_token, login_session


def test_modification_appareil_cree_une_entree_historique(client, make_user, make_client, make_appareil):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid, nom_machine='AVANT', marque='HP')
    login_session(client, uid, cid)
    token = get_csrf_token(client)

    client.post(f'/appareil/{aid}/editer',
                data={'nom_machine': 'APRES', 'marque': 'HP', 'csrf_token': token})

    resp = client.get(f'/api/historique/entite/appareil/{aid}')
    assert resp.status_code == 200
    items = resp.get_json()
    assert any(h['action'] == 'Modification' for h in items)


def test_historique_isole_par_client(client, make_user, make_client, make_appareil):
    uid, _l, _p = make_user(role='admin')
    cid_a = make_client(auth_user_id=uid)
    cid_b = make_client(auth_user_id=uid)
    aid_a = make_appareil(cid_a, nom_machine='POSTE-A')
    login_session(client, uid, cid_a)
    token = get_csrf_token(client)
    client.post(f'/appareil/{aid_a}/editer', data={'nom_machine': 'POSTE-A-MODIFIE', 'csrf_token': token})

    # Le journal du client A ne doit pas fuiter vers une session sur le client B.
    login_session(client, uid, cid_b)
    resp = client.get(f'/api/historique/entite/appareil/{aid_a}')
    assert resp.get_json() == []


def test_annulation_restaure_la_valeur_precedente(client, make_user, make_client, make_appareil):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid, nom_machine='NOM-ORIGINAL')
    login_session(client, uid, cid)
    token = get_csrf_token(client)

    client.post(f'/appareil/{aid}/editer', data={'nom_machine': 'NOM-MODIFIE', 'csrf_token': token})
    hist = client.get(f'/api/historique/entite/appareil/{aid}').get_json()
    entree_modif = next(h for h in hist if h['action'] == 'Modification')

    resp = client.post(f'/historique/{entree_modif["id"]}/annuler',
                        data={'csrf_token': token}, follow_redirects=True)
    assert resp.status_code == 200
    assert 'restauré' in resp.get_data(as_text=True)

    import app
    conn = app.get_db()
    nom = conn.execute('SELECT nom_machine FROM appareils WHERE id=?', (aid,)).fetchone()[0]
    conn.close()
    assert nom == 'NOM-ORIGINAL'


def test_lecteur_ne_peut_pas_annuler(client, make_user, make_client, make_appareil, conn):
    proprietaire_id, _l, _p = make_user(role='admin')
    lecteur_id, _l2, _p2 = make_user(role='user')
    cid = make_client(auth_user_id=proprietaire_id)
    aid = make_appareil(cid, nom_machine='NOM-ORIGINAL')

    login_session(client, proprietaire_id, cid)
    token = get_csrf_token(client)
    client.post(f'/appareil/{aid}/editer', data={'nom_machine': 'NOM-MODIFIE', 'csrf_token': token})
    hist = client.get(f'/api/historique/entite/appareil/{aid}').get_json()
    entree_modif = next(h for h in hist if h['action'] == 'Modification')

    conn.execute("INSERT INTO client_partages (client_id, auth_user_id, niveau) VALUES (?,?,'lecture')",
                 (cid, lecteur_id))
    conn.commit()

    login_session(client, lecteur_id, cid)
    token2 = get_csrf_token(client)
    client.post(f'/historique/{entree_modif["id"]}/annuler', data={'csrf_token': token2})

    import app
    conn2 = app.get_db()
    nom = conn2.execute('SELECT nom_machine FROM appareils WHERE id=?', (aid,)).fetchone()[0]
    conn2.close()
    assert nom == 'NOM-MODIFIE'
