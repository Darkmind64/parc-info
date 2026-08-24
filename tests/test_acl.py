"""Isolation multi-client (ACL) : le cœur du modèle de sécurité de ParcInfo
(CLAUDE.md, § Contrôle d'Accès Multi-Client). Un utilisateur ne doit jamais
pouvoir lire ou modifier les données d'un client auquel il n'a pas accès,
et un accès 'lecture' ne doit jamais permettre d'écrire.
"""
import app
from conftest import get_csrf_token, login_session


def test_appareil_dun_autre_client_est_invisible(client, make_user, make_client, make_appareil):
    proprietaire_id, _l, _p = make_user(role='admin')
    autre_id, _l2, _p2 = make_user(role='user')
    client_a = make_client(auth_user_id=proprietaire_id)
    client_b = make_client(auth_user_id=autre_id)
    appareil_a = make_appareil(client_a, nom_machine='POSTE-CLIENT-A')

    # Connecté sur son propre client B, tenter d'éditer un appareil du
    # client A par id direct dans l'URL (IDOR classique).
    login_session(client, autre_id, client_b)
    resp = client.get(f'/appareil/{appareil_a}/editer', follow_redirects=True)
    assert resp.status_code == 200
    assert 'Appareil introuvable' in resp.get_data(as_text=True)


def test_acces_lecture_seule_ne_peut_pas_ecrire(client, make_user, make_client, make_appareil):
    proprietaire_id, _l, _p = make_user(role='admin')
    lecteur_id, _l2, _p2 = make_user(role='user')
    cid = make_client(auth_user_id=proprietaire_id)
    aid = make_appareil(cid, nom_machine='AVANT-MODIF')

    conn = app.get_db()
    conn.execute("INSERT INTO client_partages (client_id, auth_user_id, niveau) VALUES (?,?,'lecture')",
                 (cid, lecteur_id))
    conn.commit()
    conn.close()

    login_session(client, lecteur_id, cid)
    token = get_csrf_token(client)
    client.post(f'/appareil/{aid}/editer',
                data={'nom_machine': 'APRES-MODIF', 'csrf_token': token},
                follow_redirects=True)

    conn = app.get_db()
    nom = conn.execute('SELECT nom_machine FROM appareils WHERE id=?', (aid,)).fetchone()[0]
    conn.close()
    assert nom == 'AVANT-MODIF'


def test_acces_ecriture_partage_peut_modifier(client, make_user, make_client, make_appareil):
    proprietaire_id, _l, _p = make_user(role='admin')
    editeur_id, _l2, _p2 = make_user(role='user')
    cid = make_client(auth_user_id=proprietaire_id)
    aid = make_appareil(cid, nom_machine='AVANT-MODIF')

    conn = app.get_db()
    conn.execute("INSERT INTO client_partages (client_id, auth_user_id, niveau) VALUES (?,?,'ecriture')",
                 (cid, editeur_id))
    conn.commit()
    conn.close()

    login_session(client, editeur_id, cid)
    token = get_csrf_token(client)
    client.post(f'/appareil/{aid}/editer',
                data={'nom_machine': 'APRES-MODIF', 'csrf_token': token})

    conn = app.get_db()
    nom = conn.execute('SELECT nom_machine FROM appareils WHERE id=?', (aid,)).fetchone()[0]
    conn.close()
    assert nom == 'APRES-MODIF'


def test_admin_a_acces_a_tous_les_clients(client, make_user, make_client, make_appareil):
    proprietaire_id, _l, _p = make_user(role='user')
    admin_id, _l2, _p2 = make_user(role='admin')
    cid = make_client(auth_user_id=proprietaire_id)
    aid = make_appareil(cid, nom_machine='POSTE-VU-PAR-ADMIN')

    login_session(client, admin_id, cid)
    resp = client.get(f'/appareil/{aid}/editer')
    assert resp.status_code == 200
    assert 'POSTE-VU-PAR-ADMIN' in resp.get_data(as_text=True)
