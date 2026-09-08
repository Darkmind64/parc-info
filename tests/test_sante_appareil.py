"""Score de santé synthétique par appareil (`sante.py`).

`sante_appareil(appareil, ctx)` est pur : dict d'une ligne `appareils` +
contexte pré-chargé → `{niveau, score, raisons[]}`. On teste chaque règle
isolément, le cumul, le niveau, le tri, puis `charger_contexte_sante` sur une
vraie base.
"""
import json
from datetime import date, timedelta

import sante
from sante import sante_appareil
from conftest import login_session


def _il_y_a(jours):
    return (date.today() - timedelta(days=jours)).isoformat()


def _ctx(**kw):
    base = {'reglages': {**sante._DEFAUTS, 'desactivees': set()},
            'appareils_sous_contrat': set(), 'equip_muet_ip': set(),
            'equip_par_appareil': {}, 'ports_erreur_par_appareil': {},
            'series_doublon': set()}
    base.update(kw)
    return base


def test_appareil_sain():
    s = sante_appareil({'id': 1, 'type_appareil': 'PC fixe', 'statut': 'actif',
                        'date_maj': _il_y_a(2), 'date_creation': _il_y_a(2),
                        'rapport_systeme_json': '{}'}, _ctx())
    assert s['niveau'] == 'ok' and s['score'] == 0 and s['raisons'] == []


def test_garantie_expiree_sans_contrat():
    ap = {'id': 1, 'type_appareil': 'PC', 'statut': 'actif',
          'date_fin_garantie': _il_y_a(30), 'date_maj': _il_y_a(1)}
    s = sante_appareil(ap, _ctx())
    codes = {r['code'] for r in s['raisons']}
    assert 'garantie_expiree_sans_contrat' in codes and s['niveau'] == 'attention'
    # avec un contrat qui couvre l'appareil -> plus de raison garantie
    s2 = sante_appareil(ap, _ctx(appareils_sous_contrat={1}))
    assert 'garantie_expiree_sans_contrat' not in {r['code'] for r in s2['raisons']}


def test_materiel_ancien_selon_famille():
    vieux = {'id': 1, 'statut': 'actif', 'date_achat': _il_y_a(int(7 * 365.25)),
             'date_maj': _il_y_a(1)}
    # PC : seuil 6 ans -> signalé
    assert 'materiel_ancien' in {r['code'] for r in
                                 sante_appareil({**vieux, 'type_appareil': 'PC'}, _ctx())['raisons']}
    # Switch : seuil 10 ans -> pas signalé à 7 ans
    assert 'materiel_ancien' not in {r['code'] for r in
                                     sante_appareil({**vieux, 'type_appareil': 'Switch'}, _ctx())['raisons']}


def test_jamais_collecte():
    base = {'id': 1, 'statut': 'actif', 'date_maj': _il_y_a(1), 'date_creation': _il_y_a(90)}
    s = sante_appareil({**base, 'type_appareil': 'PC portable'}, _ctx())
    jc = next((r for r in s['raisons'] if r['code'] == 'jamais_collecte'), None)
    assert jc and jc['gravite'] == 'info' and s['niveau'] == 'ok'   # nudge, pas défaut
    # une imprimante n'est pas censée remonter une collecte système
    assert 'jamais_collecte' not in {r['code'] for r in
                                     sante_appareil({**base, 'type_appareil': 'Imprimante'}, _ctx())['raisons']}
    # appareil récent : grâce, pas de nudge
    assert 'jamais_collecte' not in {r['code'] for r in sante_appareil(
        {**base, 'type_appareil': 'PC', 'date_creation': _il_y_a(5)}, _ctx())['raisons']}


def test_collecte_ancienne_est_info():
    ap = {'id': 1, 'type_appareil': 'PC', 'statut': 'actif',
          'rapport_systeme_json': '{}', 'derniere_synchro': _il_y_a(60)}
    s = sante_appareil(ap, _ctx())
    r = next(r for r in s['raisons'] if r['code'] == 'collecte_ancienne')
    assert r['gravite'] == 'info' and s['niveau'] == 'ok'   # info ne dégrade pas la pastille


def test_alerte_collecteur_disque_sature_est_critique():
    rapport = json.dumps({'disk_total_gb': 500, 'disk_used_gb': 480, 'antivirus': 'Defender'})
    s = sante_appareil({'id': 1, 'type_appareil': 'PC', 'statut': 'actif',
                        'rapport_systeme_json': rapport, 'date_maj': _il_y_a(1)}, _ctx())
    assert s['niveau'] == 'critique'
    assert any(r['code'].startswith('collecteur:') and r['gravite'] == 'critique'
               for r in s['raisons'])


def test_reseau_equipement_muet():
    s = sante_appareil({'id': 7, 'type_appareil': 'Switch', 'statut': 'actif',
                        'adresse_ip': '10.0.0.9', 'date_maj': _il_y_a(1)},
                       _ctx(equip_par_appareil={7: {'snmp_ok': False, 'nb_ports_erreur': 0,
                                                    'motif': 'timeout'}}))
    assert 'reseau:equipement_muet' in {r['code'] for r in s['raisons']}
    assert s['niveau'] == 'attention'


def test_reseau_port_erreur_critique():
    s = sante_appareil({'id': 3, 'type_appareil': 'Serveur', 'statut': 'actif',
                        'date_maj': _il_y_a(1)},
                       _ctx(ports_erreur_par_appareil={3: [
                           {'classe': 'physique', 'libelle': 'CRC / couche physique',
                            'gravite': 'critique'}]}))
    assert s['niveau'] == 'critique'
    assert any(r['code'] == 'reseau:port_erreur:physique' for r in s['raisons'])


def test_doublon_serie():
    s = sante_appareil({'id': 1, 'type_appareil': 'PC', 'statut': 'actif',
                        'numero_serie': 'ABC123', 'date_maj': _il_y_a(1)},
                       _ctx(series_doublon={'ABC123'}))
    assert 'doublon_serie' in {r['code'] for r in s['raisons']}


def test_regle_desactivee():
    ap = {'id': 1, 'type_appareil': 'PC', 'statut': 'actif',
          'date_achat': _il_y_a(int(9 * 365.25)), 'date_maj': _il_y_a(1)}
    ctx = _ctx()
    ctx['reglages']['desactivees'] = {'materiel_ancien'}
    assert 'materiel_ancien' not in {r['code'] for r in sante_appareil(ap, ctx)['raisons']}


def test_statut_retire_ignore_cycle_de_vie_et_reseau():
    ap = {'id': 1, 'type_appareil': 'Switch', 'statut': 'retire',
          'date_fin_garantie': _il_y_a(400), 'adresse_ip': '10.0.0.9'}
    s = sante_appareil(ap, _ctx(equip_muet_ip={'10.0.0.9'}))
    assert s['niveau'] == 'ok' and s['raisons'] == []


def test_cumul_niveau_et_score_plafonne():
    rapport = json.dumps({'disk_total_gb': 500, 'disk_used_gb': 490,   # danger
                          'firewall': ['Profil domaine: désactivé'],   # danger
                          'tpm_present': False})                       # warn
    ap = {'id': 1, 'type_appareil': 'PC', 'statut': 'actif',
          'rapport_systeme_json': rapport, 'date_fin_garantie': _il_y_a(10),
          'date_maj': _il_y_a(1)}
    s = sante_appareil(ap, _ctx())
    assert s['niveau'] == 'critique'
    assert s['score'] == 100   # plafonné


def test_resume_client_trie_pire_dabord():
    a = ({'id': 1, 'nom_machine': 'A'}, {'niveau': 'attention', 'score': 12,
                                         'raisons': [{'texte': 'garantie'}]})
    b = ({'id': 2, 'nom_machine': 'B'}, {'niveau': 'critique', 'score': 60,
                                         'raisons': [{'texte': 'disque'}]})
    c = ({'id': 3, 'nom_machine': 'C'}, {'niveau': 'ok', 'score': 0, 'raisons': []})
    r = sante.resume_client([a, b, c])
    assert r['compte'] == {'ok': 1, 'attention': 1, 'critique': 1}
    assert [x['id'] for x in r['a_traiter']] == [2, 1]   # critique avant attention


def test_charger_contexte_sante(conn, make_client, make_appareil):
    cid = make_client()
    a1 = make_appareil(cid, numero_serie='DUP', type_appareil='PC')
    a2 = make_appareil(cid, numero_serie='DUP', type_appareil='PC')
    sw = make_appareil(cid, type_appareil='Switch', adresse_ip='10.0.0.9')
    conn.execute("INSERT INTO contrats (client_id, titre, date_fin, statut) "
                 "VALUES (?, 'Maint', ?, 'actif')", (cid, _il_y_a(-400)))
    ct = conn.execute("SELECT id FROM contrats WHERE client_id=?", (cid,)).fetchone()[0]
    conn.execute("INSERT INTO contrats_appareils (contrat_id, appareil_id) VALUES (?,?)", (ct, a1))
    conn.execute("INSERT INTO diag_etat_equipement (client_id, equipement_ip, appareil_id, "
                 "snmp_ok, nb_ports_erreur) VALUES (?, '10.0.0.9', ?, 0, 0)", (cid, sw))
    conn.execute("INSERT INTO diag_etat_port (client_id, equipement_ip, port_index, "
                 "appareil_vu_id, classe_erreur, classe_libelle, gravite) "
                 "VALUES (?, '10.0.0.9', 5, ?, 'duplex', 'Duplex mismatch', 'attention')", (cid, a1))
    conn.commit()

    ctx = sante.charger_contexte_sante(conn, cid)
    assert a1 in ctx['appareils_sous_contrat'] and a2 not in ctx['appareils_sous_contrat']
    assert '10.0.0.9' in ctx['equip_muet_ip']
    assert ctx['equip_par_appareil'][sw]['snmp_ok'] is False
    assert ctx['ports_erreur_par_appareil'][a1][0]['classe'] == 'duplex'
    assert 'DUP' in ctx['series_doublon']


def test_recalculer_ne_reecrit_que_le_change(conn, make_client, make_appareil):
    cid = make_client()
    aid = make_appareil(cid, type_appareil='PC', date_fin_garantie=_il_y_a(30),
                        date_creation=_il_y_a(2))
    n1 = sante.recalculer(conn, cid)
    assert n1 >= 1
    row = conn.execute("SELECT sante_niveau, sante_raisons FROM appareils WHERE id=?", (aid,)).fetchone()
    assert row[0] == 'attention' and 'garantie' in row[1]
    # rien n'a changé -> aucune réécriture
    assert sante.recalculer(conn, cid) == 0
    # la garantie repasse dans les clous -> la pastille redevient ok
    conn.execute("UPDATE appareils SET date_fin_garantie=? WHERE id=?", (_il_y_a(-400), aid))
    conn.commit()
    assert sante.recalculer(conn, cid) == 1
    assert conn.execute("SELECT sante_niveau FROM appareils WHERE id=?", (aid,)).fetchone()[0] == 'ok'


def test_liste_appareils_pastille_et_filtre(client, conn, make_user, make_client):
    import json as _j
    uid, _l, _p = make_user()
    cid = make_client(auth_user_id=uid)
    bad = make_appareil_row(conn, cid, 'PC-KO', type_appareil='PC', date_creation=_il_y_a(2),
                            rapport_systeme_json=_j.dumps({'disk_total_gb': 500,
                                                           'disk_used_gb': 490}))
    ok = make_appareil_row(conn, cid, 'PC-OK', type_appareil='PC', date_creation=_il_y_a(2),
                           rapport_systeme_json='{}')
    sante.recalculer(conn, cid)
    login_session(client, uid, cid)

    html = client.get('/appareils').get_data(as_text=True)
    assert 'sante-critique' in html and 'PC-KO' in html and 'PC-OK' in html

    filtre = client.get('/appareils?sante=probleme').get_data(as_text=True)
    assert 'PC-KO' in filtre and 'PC-OK' not in filtre

    assert client.get('/appareils?sort=sante&dir=asc').status_code == 200


def make_appareil_row(conn, client_id, nom, **extra):
    cols = ['client_id', 'nom_machine'] + list(extra)
    cur = conn.execute(
        "INSERT INTO appareils (%s) VALUES (%s)" % (','.join(cols), ','.join('?' * len(cols))),
        [client_id, nom] + list(extra.values()))
    conn.commit()
    return cur.lastrowid


def test_resume_cache(conn, make_client):
    cid = make_client()
    for niv, sc, n in (('critique', 60, 2), ('attention', 12, 3), ('ok', 0, 5)):
        for i in range(n):
            make_appareil_row(conn, cid, f'{niv}-{i}', type_appareil='PC', statut='actif',
                              sante_niveau=niv, sante_score=sc,
                              sante_raisons='[{"texte":"x","gravite":"attention"}]')
    make_appareil_row(conn, cid, 'retire-ko', statut='retire', sante_niveau='critique')
    r = sante.resume_cache(conn, cid)
    assert r['compte'] == {'ok': 5, 'attention': 3, 'critique': 2}   # 'retire' exclu
    assert r['total'] == 10
    assert r['a_traiter'][0]['niveau'] == 'critique'          # critique en tête
    assert all(x['niveau'] != 'ok' for x in r['a_traiter'])


def test_journalise_les_bascules_du_seuil(conn, make_client, make_appareil):
    cid = make_client()
    aid = make_appareil(cid, type_appareil='PC', date_creation=_il_y_a(2))
    sante.recalculer(conn, cid)          # 1er calcul : ok, pas de bascule journalisée
    _nb = lambda act: conn.execute(
        "SELECT COUNT(*) FROM historique WHERE client_id=? AND entite_id=? AND action=?",
        (cid, aid, act)).fetchone()[0]
    assert _nb('Santé dégradée') == 0
    # ok -> critique
    conn.execute("UPDATE appareils SET rapport_systeme_json=? WHERE id=?",
                 ('{"disk_total_gb":500,"disk_used_gb":495}', aid))
    conn.commit()
    sante.recalculer(conn, cid)
    assert _nb('Santé dégradée') == 1
    # critique -> ok
    conn.execute("UPDATE appareils SET rapport_systeme_json='{}' WHERE id=?", (aid,))
    conn.commit()
    sante.recalculer(conn, cid)
    assert _nb('Santé rétablie') == 1


def test_fiche_systeme_encart_sante(client, conn, make_user, make_client):
    import json as _j
    uid, _l, _p = make_user()
    cid = make_client(auth_user_id=uid)
    aid = make_appareil_row(conn, cid, 'FICHE-KO', type_appareil='PC', date_creation=_il_y_a(2),
                            rapport_systeme_json=_j.dumps({'disk_total_gb': 500,
                                                           'disk_used_gb': 490}))
    login_session(client, uid, cid)
    html = client.get(f'/appareil/{aid}/fiche-systeme').get_data(as_text=True)
    assert 'Santé —' in html and 'à traiter en priorité' in html


def test_mobile_appareil_bandeau_sante(client, conn, make_user, make_client):
    uid, _l, _p = make_user()
    cid = make_client(auth_user_id=uid)
    aid = make_appareil_row(conn, cid, 'MOB-KO', type_appareil='PC', statut='actif',
                            sante_niveau='critique', sante_score=60,
                            sante_raisons='[{"texte":"Disque saturé","gravite":"critique"}]')
    login_session(client, uid, cid)
    html = client.get(f'/m/appareil/{aid}').get_data(as_text=True)
    assert 'À traiter en priorité' in html and 'Disque saturé' in html
