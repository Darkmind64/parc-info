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


# ─── Audit 2026-09 : faille cross-tenant sur 7 endpoints ────────────────────
#
# Constat : plusieurs endpoints vérifiaient can_write() sur le CLIENT ACTIF
# de la session, puis modifiaient/supprimaient une ligne identifiée par un
# simple id d'URL, sans jamais vérifier que cette ligne appartenait bien à
# ce client actif (IDOR). Un utilisateur avec un accès 'ecriture' sur son
# propre client A pouvait ainsi tamponner les données d'un client B en
# devinant/énumérant des id. Chaque test ci-dessous établit un attaquant
# actif sur le client A et une victime sur le client B, sans aucun partage
# entre les deux, puis vérifie que l'action cross-tenant est refusée ET que
# la donnée de la victime n'a pas bougé.

def _attaquant_sur_client_a(make_user, make_client):
    """Un utilisateur avec accès 'ecriture' sur son PROPRE client A — jamais
    partagé avec le client B de la victime."""
    proprietaire_a, _l, _p = make_user(role='admin')
    attaquant_id, _l2, _p2 = make_user(role='user')
    client_a = make_client(auth_user_id=proprietaire_a)
    conn = app.get_db()
    conn.execute(
        "INSERT INTO client_partages (client_id, auth_user_id, niveau) VALUES (?,?,'ecriture')",
        (client_a, attaquant_id))
    conn.commit(); conn.close()
    return attaquant_id, client_a


def test_droit_dun_autre_client_non_modifiable(client, make_user, make_client):
    attaquant_id, client_a = _attaquant_sur_client_a(make_user, make_client)
    proprietaire_b, _l, _p = make_user(role='admin')
    client_b = make_client(auth_user_id=proprietaire_b)

    conn = app.get_db()
    conn.execute("INSERT INTO utilisateurs (id, client_id, prenom, nom) VALUES (900, ?, 'Marie', 'Martin')", (client_b,))
    conn.execute("INSERT INTO droits_utilisateurs (id, utilisateur_id, client_id, categorie, nom_droit, valeur) "
                 "VALUES (900, 900, ?, 'Test', 'DroitB', 'secret')", (client_b,))
    conn.commit(); conn.close()

    login_session(client, attaquant_id, client_a)
    token = get_csrf_token(client)
    client.put('/api/droit/900', json={'categorie': 'HACKED'}, headers={'X-CSRF-Token': token})
    client.delete('/api/droit/900', headers={'X-CSRF-Token': token})

    conn = app.get_db()
    row = conn.execute('SELECT categorie FROM droits_utilisateurs WHERE id=900').fetchone()
    conn.close()
    assert row is not None and row[0] == 'Test'


def test_type_droit_ecriture_requise(client, make_user, make_client):
    """api_type_droit / api_creer_type_droit n'avaient aucune vérification
    can_write() — un accès 'lecture' pouvait créer/modifier un type de
    droit sur son propre client."""
    proprietaire_id, _l, _p = make_user(role='admin')
    lecteur_id, _l2, _p2 = make_user(role='user')
    cid = make_client(auth_user_id=proprietaire_id)
    conn = app.get_db()
    conn.execute("INSERT INTO client_partages (client_id, auth_user_id, niveau) VALUES (?,?,'lecture')", (cid, lecteur_id))
    conn.commit(); conn.close()

    login_session(client, lecteur_id, cid)
    token = get_csrf_token(client)
    resp = client.post('/api/type-droit', json={'nom': 'HACKED'}, headers={'X-CSRF-Token': token})
    assert resp.status_code == 403

    conn = app.get_db()
    row = conn.execute("SELECT 1 FROM types_droits WHERE client_id=? AND nom='HACKED'", (cid,)).fetchone()
    conn.close()
    assert row is None


def test_garantie_ignoree_dun_autre_client_non_modifiable(client, make_user, make_client, make_appareil):
    attaquant_id, client_a = _attaquant_sur_client_a(make_user, make_client)
    proprietaire_b, _l, _p = make_user(role='admin')
    client_b = make_client(auth_user_id=proprietaire_b)
    appareil_b = make_appareil(client_b, nom_machine='POSTE-B')

    login_session(client, attaquant_id, client_a)
    token = get_csrf_token(client)
    client.post(f'/api/appareil/{appareil_b}/garantie-ignorer', json={'ignorer': True},
                headers={'X-CSRF-Token': token})

    conn = app.get_db()
    row = conn.execute('SELECT garantie_alerte_ignoree FROM appareils WHERE id=?', (appareil_b,)).fetchone()
    conn.close()
    assert not row[0]


def test_utilisateur_dun_autre_client_non_supprimable(client, make_user, make_client):
    attaquant_id, client_a = _attaquant_sur_client_a(make_user, make_client)
    proprietaire_b, _l, _p = make_user(role='admin')
    client_b = make_client(auth_user_id=proprietaire_b)
    conn = app.get_db()
    conn.execute("INSERT INTO utilisateurs (id, client_id, prenom, nom) VALUES (901, ?, 'Marie', 'Martin')", (client_b,))
    conn.commit(); conn.close()

    login_session(client, attaquant_id, client_a)
    token = get_csrf_token(client)
    client.post('/utilisateur/901/supprimer', data={'csrf_token': token})

    conn = app.get_db()
    row = conn.execute('SELECT 1 FROM utilisateurs WHERE id=901').fetchone()
    conn.close()
    assert row is not None


def test_peripherique_dun_autre_client_liens_intacts(client, make_user, make_client, make_appareil):
    """La fiche périphérique elle-même était déjà protégée (WHERE id=? AND
    client_id=?) ; mais le nettoyage de la table pivot
    peripheriques_appareils s'exécutait AVANT toute vérification —
    supprimant les liens d'un autre client sans pouvoir modifier sa fiche."""
    attaquant_id, client_a = _attaquant_sur_client_a(make_user, make_client)
    proprietaire_b, _l, _p = make_user(role='admin')
    client_b = make_client(auth_user_id=proprietaire_b)
    appareil_b = make_appareil(client_b, nom_machine='POSTE-B')
    conn = app.get_db()
    conn.execute("INSERT INTO peripheriques (id, client_id, categorie, marque, modele) "
                 "VALUES (900, ?, 'Ecran', 'Dell', 'X')", (client_b,))
    conn.execute("INSERT INTO peripheriques_appareils (peripherique_id, appareil_id) VALUES (900, ?)", (appareil_b,))
    conn.commit(); conn.close()

    login_session(client, attaquant_id, client_a)
    token = get_csrf_token(client)
    client.post('/peripherique/900/editer', data={'csrf_token': token, 'marque': 'HACKED'})

    conn = app.get_db()
    lien = conn.execute('SELECT 1 FROM peripheriques_appareils WHERE peripherique_id=900').fetchone()
    marque = conn.execute('SELECT marque FROM peripheriques WHERE id=900').fetchone()[0]
    conn.close()
    assert lien is not None
    assert marque == 'Dell'


def test_contrat_dun_autre_client_liens_intacts(client, make_user, make_client, make_appareil):
    attaquant_id, client_a = _attaquant_sur_client_a(make_user, make_client)
    proprietaire_b, _l, _p = make_user(role='admin')
    client_b = make_client(auth_user_id=proprietaire_b)
    appareil_b = make_appareil(client_b, nom_machine='POSTE-B')
    conn = app.get_db()
    conn.execute("INSERT INTO contrats (id, client_id, titre) VALUES (900, ?, 'Contrat B')", (client_b,))
    conn.execute("INSERT INTO contrats_appareils (contrat_id, appareil_id) VALUES (900, ?)", (appareil_b,))
    conn.commit(); conn.close()

    login_session(client, attaquant_id, client_a)
    token = get_csrf_token(client)
    client.post('/contrat/900/editer', data={'csrf_token': token, 'titre': 'HACKED'})

    conn = app.get_db()
    lien = conn.execute('SELECT 1 FROM contrats_appareils WHERE contrat_id=900').fetchone()
    titre = conn.execute('SELECT titre FROM contrats WHERE id=900').fetchone()[0]
    conn.close()
    assert lien is not None
    assert titre == 'Contrat B'


def test_intervention_dun_autre_client_liens_intacts(client, make_user, make_client, make_appareil):
    attaquant_id, client_a = _attaquant_sur_client_a(make_user, make_client)
    proprietaire_b, _l, _p = make_user(role='admin')
    client_b = make_client(auth_user_id=proprietaire_b)
    appareil_b = make_appareil(client_b, nom_machine='POSTE-B')
    conn = app.get_db()
    conn.execute("INSERT INTO interventions (id, client_id, titre, date_intervention) "
                 "VALUES (900, ?, 'Interv B', '2026-01-01')", (client_b,))
    conn.execute("INSERT INTO interventions_appareils (intervention_id, appareil_id) VALUES (900, ?)", (appareil_b,))
    conn.commit(); conn.close()

    login_session(client, attaquant_id, client_a)
    token = get_csrf_token(client)
    client.post('/intervention/900/editer', data={'csrf_token': token, 'titre': 'HACKED'})

    conn = app.get_db()
    lien = conn.execute('SELECT 1 FROM interventions_appareils WHERE intervention_id=900').fetchone()
    titre = conn.execute('SELECT titre FROM interventions WHERE id=900').fetchone()[0]
    conn.close()
    assert lien is not None
    assert titre == 'Interv B'


def test_baie_slot_dun_autre_client_non_supprimable(client, make_user, make_client):
    attaquant_id, client_a = _attaquant_sur_client_a(make_user, make_client)
    proprietaire_b, _l, _p = make_user(role='admin')
    client_b = make_client(auth_user_id=proprietaire_b)
    conn = app.get_db()
    conn.execute("INSERT INTO baie_slots (id, client_id, baie_nom, position, col_index, hauteur_u, nb_ports) "
                 "VALUES (900, ?, 'Baie principale', 1, 0, 1, 0)", (client_b,))
    conn.commit(); conn.close()

    login_session(client, attaquant_id, client_a)
    token = get_csrf_token(client)
    resp = client.delete('/api/baie/slot/900', headers={'X-CSRF-Token': token})

    assert resp.status_code == 404
    conn = app.get_db()
    row = conn.execute('SELECT 1 FROM baie_slots WHERE id=900').fetchone()
    conn.close()
    assert row is not None


# ─── Trouvé par /ultrareview sur la PR ci-dessus : failles voisines, même
# schéma, ratées par le premier passage ──────────────────────────────────────

def test_suppression_contrat_dun_autre_client_appareils_intacts(client, make_user, make_client, make_appareil):
    """supprimer_contrat filtrait bien SELECT/DELETE par client_id, mais les
    3 UPDATE appareils SET av/edr/rmm_contrat_id=NULL n'étaient pas scopés :
    ils s'appliquaient à n'importe quel appareil référençant ce contrat,
    même chez un autre client."""
    attaquant_id, client_a = _attaquant_sur_client_a(make_user, make_client)
    proprietaire_b, _l, _p = make_user(role='admin')
    client_b = make_client(auth_user_id=proprietaire_b)
    appareil_b = make_appareil(client_b, nom_machine='POSTE-B', av_contrat_id=910)
    conn = app.get_db()
    conn.execute("INSERT INTO contrats (id, client_id, titre) VALUES (910, ?, 'Contrat B')", (client_b,))
    conn.commit(); conn.close()

    login_session(client, attaquant_id, client_a)
    token = get_csrf_token(client)
    client.post('/contrat/910/supprimer', data={'csrf_token': token})

    conn = app.get_db()
    ctr = conn.execute('SELECT 1 FROM contrats WHERE id=910').fetchone()
    av = conn.execute('SELECT av_contrat_id FROM appareils WHERE id=?', (appareil_b,)).fetchone()[0]
    conn.close()
    assert ctr is not None
    assert av == 910


def test_type_droit_put_ne_fuit_pas_un_autre_client(client, make_user, make_client):
    """api_type_droit (PUT) scopait bien l'UPDATE par client_id, mais la
    SELECT de retour utilisée pour la réponse JSON ne l'était pas : un id
    d'un autre client renvoyait quand même ses categorie/nom/description."""
    attaquant_id, client_a = _attaquant_sur_client_a(make_user, make_client)
    proprietaire_b, _l, _p = make_user(role='admin')
    client_b = make_client(auth_user_id=proprietaire_b)
    conn = app.get_db()
    conn.execute("INSERT INTO types_droits (id, client_id, nom) VALUES (911, ?, 'Secret-B')", (client_b,))
    conn.commit(); conn.close()

    login_session(client, attaquant_id, client_a)
    token = get_csrf_token(client)
    resp = client.put('/api/type-droit/911', json={'nom': 'HACKED'}, headers={'X-CSRF-Token': token})

    assert resp.get_json() == {}
    conn = app.get_db()
    nom = conn.execute('SELECT nom FROM types_droits WHERE id=911').fetchone()[0]
    conn.close()
    assert nom == 'Secret-B'


def test_ajout_droit_refuse_pour_un_utilisateur_dun_autre_client(client, make_user, make_client):
    """api_ajouter_droit vérifiait can_write() sur le client actif mais
    acceptait n'importe quel utilisateur_id du payload sans vérifier qu'il
    appartenait à ce client — un droit pouvait être attribué à un
    utilisateur d'un AUTRE client (visible ensuite sur sa fiche)."""
    attaquant_id, client_a = _attaquant_sur_client_a(make_user, make_client)
    proprietaire_b, _l, _p = make_user(role='admin')
    client_b = make_client(auth_user_id=proprietaire_b)
    conn = app.get_db()
    conn.execute("INSERT INTO utilisateurs (id, client_id, prenom, nom) VALUES (912, ?, 'Marie', 'Martin')", (client_b,))
    conn.commit(); conn.close()

    login_session(client, attaquant_id, client_a)
    token = get_csrf_token(client)
    resp = client.post('/api/droit', json={'utilisateur_id': 912, 'nom_droit': 'HACKED'},
                        headers={'X-CSRF-Token': token})

    assert resp.status_code == 404
    conn = app.get_db()
    row = conn.execute('SELECT 1 FROM droits_utilisateurs WHERE utilisateur_id=912').fetchone()
    conn.close()
    assert row is None


def test_suppression_peripherique_dun_autre_client_pas_de_faux_journal(client, make_user, make_client):
    """supprimer_peripherique lisait marque/modele sans filtre client_id et
    les journalisait quand même comme 'Suppression' dans l'historique de
    l'attaquant, alors que rien n'avait été supprimé (DELETE déjà scopé)."""
    attaquant_id, client_a = _attaquant_sur_client_a(make_user, make_client)
    proprietaire_b, _l, _p = make_user(role='admin')
    client_b = make_client(auth_user_id=proprietaire_b)
    conn = app.get_db()
    conn.execute("INSERT INTO peripheriques (id, client_id, categorie, marque, modele) "
                 "VALUES (913, ?, 'Ecran', 'Dell', 'Secret-B')", (client_b,))
    conn.commit(); conn.close()

    login_session(client, attaquant_id, client_a)
    token = get_csrf_token(client)
    client.post('/peripherique/913/supprimer', data={'csrf_token': token})

    conn = app.get_db()
    row = conn.execute('SELECT 1 FROM peripheriques WHERE id=913').fetchone()
    faux_journal = conn.execute(
        "SELECT 1 FROM historique WHERE client_id=? AND entite='peripherique' AND entite_nom LIKE '%Secret-B%'",
        (client_a,)).fetchone()
    conn.close()
    assert row is not None
    assert faux_journal is None


def test_suppression_utilisateur_dun_autre_client_pas_de_faux_journal(client, make_user, make_client):
    """Même défaut que ci-dessus sur supprimer_utilisateur : une entrée
    'Suppression' était journalisée côté attaquant même quand la ligne
    visée n'appartenait pas à son client (donc jamais réellement supprimée)."""
    attaquant_id, client_a = _attaquant_sur_client_a(make_user, make_client)
    proprietaire_b, _l, _p = make_user(role='admin')
    client_b = make_client(auth_user_id=proprietaire_b)
    conn = app.get_db()
    conn.execute("INSERT INTO utilisateurs (id, client_id, prenom, nom) VALUES (914, ?, 'Marie', 'Martin')", (client_b,))
    conn.commit(); conn.close()

    login_session(client, attaquant_id, client_a)
    token = get_csrf_token(client)
    client.post('/utilisateur/914/supprimer', data={'csrf_token': token})

    conn = app.get_db()
    row = conn.execute('SELECT 1 FROM utilisateurs WHERE id=914').fetchone()
    faux_journal = conn.execute(
        "SELECT 1 FROM historique WHERE client_id=? AND entite='utilisateur' AND entite_id=914",
        (client_a,)).fetchone()
    conn.close()
    assert row is not None
    assert faux_journal is None
