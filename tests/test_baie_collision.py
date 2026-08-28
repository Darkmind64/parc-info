"""
Détection de chevauchement entre éléments de la baie de brassage.

Signalé en usage réel, à plusieurs reprises : placer, redimensionner ou
déplacer un élément à la fois MOINS LARGE que la baie ET HAUT DE PLUSIEURS U
restait "toujours très compliqué", malgré plusieurs correctifs successifs
sur la précision du positionnement (grille à 10 colonnes, grille magnétique,
curseur précis — voir CLAUDE.md 2.18.67-2.18.69). La cause réelle : AUCUNE
route ne vérifiait quoi que ce soit au-delà de la case d'origine exacte
(position, col_index) — un élément haut de plusieurs U et étroit pouvait
donc silencieusement chevaucher un autre élément déjà en place (rangées
et/ou colonnes qui se recoupent), sans le moindre avertissement.

Couvre : rejet (409, sans rien modifier en base) d'une création, d'un
redimensionnement (PUT) ou d'un déplacement (/deplacer) qui chevaucherait
un AUTRE élément ; acceptation d'un chevauchement avec SOI-MÊME (édition en
place, redimensionnement, déplacement horizontal) ; et le déplacement
horizontal désormais possible via PUT /api/baie/slot/<id> (col_index), qui
corrige le bug jumeau : éditer la position d'un élément déjà placé via le
panneau "+ Placer" (au lieu du glisser-déposer, imprécis sur un gros pavé)
créait un DOUBLON à la nouvelle case sans jamais toucher l'ancien, tant que
le formulaire ne postait qu'une création.
"""
from conftest import login_session


def _placer(client, **kw):
    payload = {'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale'}
    payload.update(kw)
    r = client.post('/api/baie/slot', json=payload)
    return r, r.get_json()


def test_creation_rejetee_si_chevauchement_avec_element_different(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    # SW-A : U3-U4 (2U), colonnes 0-4.
    _placer(client, position=3, col_index=0, hauteur_u=2, largeur_u=5, nom_custom='SW-A')

    # SW-B : U4 (chevauche la 2e rangée de SW-A), colonnes 2-6 (chevauche
    # aussi en largeur) — origine DIFFÉRENTE de SW-A, jamais détecté avant.
    r, data = _placer(client, position=4, col_index=2, hauteur_u=1, largeur_u=5, nom_custom='SW-B')
    assert r.status_code == 409
    assert 'SW-A' in data['error']

    # Rien créé : un seul slot en base.
    assert len(client.get('/api/baie/slots?baie=Baie%20principale').get_json()['slots']) == 1


def test_creation_acceptee_si_aucun_chevauchement(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    _placer(client, position=3, col_index=0, hauteur_u=2, largeur_u=5, nom_custom='SW-A')
    # SW-C : mêmes rangées (U3-U4) mais colonnes 5-9, aucun recoupement.
    r, data = _placer(client, position=3, col_index=5, hauteur_u=2, largeur_u=5, nom_custom='SW-C')
    assert r.status_code == 200
    assert data['nom_custom'] == 'SW-C'


def test_edition_a_la_meme_case_ne_se_bloque_pas_elle_meme(client, make_client, make_user):
    """Re-poser un élément à SA PROPRE case d'origine (édition via le
    panneau "+ Placer", même position/colonne) ne doit jamais se heurter à
    lui-même — c'est le mécanisme de remplacement en place existant."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    _placer(client, position=5, col_index=2, hauteur_u=3, largeur_u=4, nom_custom='SW-EDIT')
    r, data = _placer(client, position=5, col_index=2, hauteur_u=3, largeur_u=4,
                       nom_custom='SW-EDIT-RENOMME', type_equipement='Switch')
    assert r.status_code == 200
    assert data['nom_custom'] == 'SW-EDIT-RENOMME'


def test_redimensionnement_hauteur_rejete_si_chevauchement(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    _r, sw_a = _placer(client, position=3, col_index=0, hauteur_u=1, largeur_u=5, nom_custom='SW-A')
    _placer(client, position=4, col_index=0, hauteur_u=1, largeur_u=5, nom_custom='SW-B')

    # Agrandir SW-A à 2U le ferait déborder sur la rangée de SW-B (mêmes
    # colonnes) — doit être refusé, hauteur_u inchangée en base.
    r = client.put(f"/api/baie/slot/{sw_a['id']}", json={
        'position': 3, 'hauteur_u': 2, 'largeur_u': 5,
        'nom_custom': 'SW-A', 'type_equipement': 'Switch',
    })
    assert r.status_code == 409
    assert 'SW-B' in r.get_json()['error']

    encore = client.get('/api/baie/slots?baie=Baie%20principale').get_json()['slots']
    sw_a_frais = next(s for s in encore if s['id'] == sw_a['id'])
    assert sw_a_frais['hauteur_u'] == 1, "le redimensionnement refusé ne doit rien changer en base"


def test_redimensionnement_accepte_sans_chevauchement(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    _r, sw_a = _placer(client, position=3, col_index=0, hauteur_u=1, largeur_u=5, nom_custom='SW-A')

    r = client.put(f"/api/baie/slot/{sw_a['id']}", json={
        'position': 3, 'hauteur_u': 3, 'largeur_u': 5,
        'nom_custom': 'SW-A', 'type_equipement': 'Switch',
    })
    assert r.status_code == 200
    assert r.get_json()['hauteur_u'] == 3


def test_put_deplace_horizontalement_via_col_index(client, make_client, make_user):
    """Le bug jumeau du chevauchement : avant ce correctif, col_index
    n'était PAS modifiable par cette route (réservé à /deplacer) — le
    formulaire "+ Placer" ne pouvant que POSTer une création, changer la
    colonne d'un élément déjà sélectionné créait un doublon au lieu de le
    déplacer. Vérifie ici que la route accepte désormais col_index et
    déplace réellement le MÊME id, sans rien laisser d'autre en base."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    _r, sw_a = _placer(client, position=3, col_index=0, hauteur_u=2, largeur_u=4, nom_custom='SW-A')

    r = client.put(f"/api/baie/slot/{sw_a['id']}", json={
        'position': 3, 'col_index': 5, 'hauteur_u': 2, 'largeur_u': 4,
        'nom_custom': 'SW-A', 'type_equipement': 'Switch',
    })
    assert r.status_code == 200
    updated = r.get_json()
    assert updated['id'] == sw_a['id'], "même id : un déplacement, pas une recréation"
    assert updated['col_index'] == 5

    tous = client.get('/api/baie/slots?baie=Baie%20principale').get_json()['slots']
    assert len(tous) == 1, "aucun doublon laissé à l'ancienne colonne"
    assert tous[0]['col_index'] == 5


def test_put_col_index_absent_garde_la_colonne_actuelle(client, make_client, make_user):
    """Un redimensionnement (glisser la poignée de hauteur/largeur) ne
    renvoie jamais col_index — doit continuer à ne rien déplacer
    horizontalement, comme avant ce correctif."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    _r, sw_a = _placer(client, position=3, col_index=4, hauteur_u=1, largeur_u=3, nom_custom='SW-A')

    r = client.put(f"/api/baie/slot/{sw_a['id']}", json={
        'position': 3, 'hauteur_u': 2, 'largeur_u': 3,
        'nom_custom': 'SW-A', 'type_equipement': 'Switch',
    })
    assert r.status_code == 200
    assert r.get_json()['col_index'] == 4


def test_deplacer_rejete_si_chevauchement(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    _r, sw_a = _placer(client, position=1, col_index=0, hauteur_u=1, largeur_u=5, nom_custom='SW-A')
    _placer(client, position=5, col_index=0, hauteur_u=3, largeur_u=5, nom_custom='SW-B')

    # Glisser-déposer SW-A vers U6 (dans la plage U5-U7 de SW-B), mêmes
    # colonnes qui se recoupent.
    r = client.post(f"/api/baie/slot/{sw_a['id']}/deplacer", json={'position': 6, 'col_index': 1})
    assert r.status_code == 409
    assert 'SW-B' in r.get_json()['error']

    encore = client.get('/api/baie/slots?baie=Baie%20principale').get_json()['slots']
    sw_a_frais = next(s for s in encore if s['id'] == sw_a['id'])
    assert sw_a_frais['position'] == 1, "le déplacement refusé ne doit rien changer en base"


def test_deplacer_accepte_sans_chevauchement(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    _r, sw_a = _placer(client, position=1, col_index=0, hauteur_u=1, largeur_u=5, nom_custom='SW-A')

    r = client.post(f"/api/baie/slot/{sw_a['id']}/deplacer", json={'position': 8, 'col_index': 2})
    assert r.status_code == 200
    updated = r.get_json()
    assert updated['position'] == 8
    assert updated['col_index'] == 2
