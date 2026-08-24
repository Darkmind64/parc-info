"""Suggestions dans Parc Général (Switch, Routeur, Serveur, UPS) tirées de
l'inventaire réel (appareils/périphériques déjà saisis pour le client),
sans jamais empêcher la saisie manuelle — voir _marque_modele_combos()
dans app.py."""
from conftest import login_session


def test_suggestions_tirees_de_linventaire_du_client(client, make_user, make_client, make_appareil, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    make_appareil(cid, nom_machine='SW-01', type_appareil='Switch', marque='HP', modele='ProCurve 2530')
    make_appareil(cid, nom_machine='FW-01', type_appareil='Routeur/Pare-feu', marque='pfSense', modele='SG-3100')
    make_appareil(cid, nom_machine='SRV-01', type_appareil='Serveur', marque='Dell', modele='PowerEdge R740')
    conn.execute("INSERT INTO peripheriques (client_id, categorie, marque, modele) VALUES (?,?,?,?)",
                 (cid, 'Onduleur / UPS', 'APC', 'Smart-UPS 1500'))
    conn.commit()

    login_session(client, uid, cid)
    html = client.get('/parc').get_data(as_text=True)

    assert '<option value="HP ProCurve 2530">' in html
    assert '<option value="pfSense SG-3100">' in html
    assert '<option value="APC Smart-UPS 1500">' in html
    assert '<option value="Dell">' in html
    assert '<option value="PowerEdge R740">' in html
    # Les champs restent de simples <input> texte : la saisie manuelle marche toujours.
    assert 'id="id-switch_marque"' in html and 'type="text"' in html


def test_suggestions_isolees_par_client(client, make_user, make_client, make_appareil):
    uid, _l, _p = make_user(role='admin')
    cid_a = make_client(auth_user_id=uid)
    cid_b = make_client(auth_user_id=uid)
    make_appareil(cid_a, nom_machine='SW-A', type_appareil='Switch', marque='Cisco', modele='2960')
    make_appareil(cid_b, nom_machine='SW-B', type_appareil='Switch', marque='Netgear', modele='GS724')

    login_session(client, uid, cid_a)
    html = client.get('/parc').get_data(as_text=True)

    assert '<option value="Cisco 2960">' in html
    assert 'Netgear' not in html


def test_type_appareil_non_switch_nest_pas_suggere(client, make_user, make_client, make_appareil):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    make_appareil(cid, nom_machine='PC-01', type_appareil='PC', marque='Lenovo', modele='M720')

    login_session(client, uid, cid)
    html = client.get('/parc').get_data(as_text=True)

    assert 'Lenovo' not in html
