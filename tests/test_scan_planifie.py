"""Scan réseau récurrent planifié (Plan 3) — logique de sélection + alerte + ACL."""
import json
from datetime import datetime

from conftest import login_session

import scan_planifie as SP
from config_helpers import cfg_set


# ─── Fonctions pures ─────────────────────────────────────────────────────────

def test_intervalle_secondes():
    assert SP.intervalle_secondes('quotidien') == 86400
    assert SP.intervalle_secondes('hebdo') == 7 * 86400
    assert SP.intervalle_secondes('mensuel') == 30 * 86400
    assert SP.intervalle_secondes('12h') == 12 * 3600
    assert SP.intervalle_secondes('  48 H ') == 48 * 3600
    assert SP.intervalle_secondes('') is None
    assert SP.intervalle_secondes('n_importe_quoi') is None
    assert SP.intervalle_secondes('0h') is None
    assert SP.intervalle_secondes('99999h') is None


def test_dans_fenetre():
    d = lambda h, m=0: datetime(2026, 1, 1, h, m)
    assert SP.dans_fenetre('02:00-05:00', d(3)) is True
    assert SP.dans_fenetre('02:00-05:00', d(6)) is False
    assert SP.dans_fenetre('02:00-05:00', d(2)) is True
    assert SP.dans_fenetre('02:00-05:00', d(5)) is False        # borne haute exclue
    # fenêtre à cheval sur minuit
    assert SP.dans_fenetre('22:00-06:00', d(23)) is True
    assert SP.dans_fenetre('22:00-06:00', d(3)) is True
    assert SP.dans_fenetre('22:00-06:00', d(12)) is False
    # vide / invalide → aucune restriction
    assert SP.dans_fenetre('', d(12)) is True
    assert SP.dans_fenetre('nawak', d(12)) is True


def test_est_du():
    assert SP.est_du(None, 'quotidien', 1_000_000) is True          # jamais scanné
    assert SP.est_du(1_000_000, '', 2_000_000) is False             # pas planifié
    assert SP.est_du(1_000_000, 'quotidien', 1_000_000 + 3600) is False   # trop récent
    assert SP.est_du(1_000_000, 'quotidien', 1_000_000 + 90000) is True   # > 24 h


def _chg(nouveaux=None, disparus=None, **extra):
    d = {'disponible': True, 'nb': len(nouveaux or []) + len(disparus or []),
         'nouveaux': nouveaux or [], 'disparus': disparus or [], 'jours_ecoules': 5}
    d.update(extra)
    return d


def test_resume_alerte_nouveaux_declenche():
    r = SP.resume_alerte(_chg(nouveaux=[{'nom': 'PC-X', 'ip': '10.0.0.9',
                                         'mac': '00:11:22:33:44:55', 'type': 'PC'}]))
    assert r and 'nouveaux' in r['declencheurs']
    assert r['nouveaux'][0]['nom'] == 'PC-X'


def test_resume_alerte_mac_aleatoire_minoree():
    # bit « localement administré » posé (0x02 sur le 1er octet) → smartphone,
    # pas un rogue : ne doit PAS déclencher à lui seul.
    r = SP.resume_alerte(_chg(nouveaux=[{'nom': '?', 'ip': '10.0.0.9',
                                         'mac': '02:11:22:33:44:55', 'type': 'PC'}]))
    assert r is None


def test_resume_alerte_seuil_disparus():
    trois = [{'nom': 'A', 'mac': ''}, {'nom': 'B', 'mac': ''}, {'nom': 'C', 'mac': ''}]
    assert SP.resume_alerte(_chg(disparus=trois[:2]), seuil_disparus=3) is None
    r = SP.resume_alerte(_chg(disparus=trois), seuil_disparus=3)
    assert r and 'disparus' in r['declencheurs']


def test_resume_alerte_rien_ou_indisponible():
    assert SP.resume_alerte(_chg(), seuil_disparus=3) is None
    assert SP.resume_alerte({'disponible': False}) is None
    assert SP.resume_alerte(None) is None


# ─── Lectures + sélection ────────────────────────────────────────────────────

def test_clients_a_scanner(conn, make_client, monkeypatch):
    cid = make_client()
    conn.execute("INSERT INTO parc_general (client_id, plage_ip_locale) VALUES (?,?)",
                 (cid, '192.168.50.0/24'))
    conn.commit()
    cfg_set('scan_auto:%d' % cid, 'quotidien')
    cfg_set('scan_auto_fenetre', '00:00-23:59')
    monkeypatch.setattr('site_terrain.clients_sur_site', lambda c: {cid})

    clients = [{'id': cid, 'nom': 'ACME'}]
    plan = SP.clients_a_scanner(conn, clients, datetime(2026, 1, 1, 3, 0))
    assert [x['client_id'] for x in plan['dus']] == [cid]
    assert plan['dus'][0]['plages'] == ['192.168.50.0/24']

    # hors fenêtre → reporté
    cfg_set('scan_auto_fenetre', '02:00-05:00')
    plan = SP.clients_a_scanner(conn, clients, datetime(2026, 1, 1, 12, 0))
    assert plan['dus'] == []
    assert plan['reportes'] and 'fenêtre' in plan['reportes'][0]['raison']

    # site non joignable → reporté
    cfg_set('scan_auto_fenetre', '00:00-23:59')
    monkeypatch.setattr('site_terrain.clients_sur_site', lambda c: set())
    plan = SP.clients_a_scanner(conn, clients, datetime(2026, 1, 1, 3, 0))
    assert plan['dus'] == []
    assert 'site' in plan['reportes'][0]['raison']

    # déjà scané récemment (instantané scan_auto tout frais) → ni dû ni reporté
    monkeypatch.setattr('site_terrain.clients_sur_site', lambda c: {cid})
    from client_helpers import capturer_instantane
    capturer_instantane(conn, cid, origine='scan_auto'); conn.commit()
    plan = SP.clients_a_scanner(conn, clients, datetime(2026, 1, 1, 3, 0))
    assert plan['dus'] == [] and plan['reportes'] == []


def test_clients_a_scanner_sans_plage_reporte(conn, make_client, monkeypatch):
    cid = make_client()
    cfg_set('scan_auto:%d' % cid, 'hebdo')
    cfg_set('scan_auto_fenetre', '')
    monkeypatch.setattr('site_terrain.clients_sur_site', lambda c: {cid})
    plan = SP.clients_a_scanner(conn, [{'id': cid, 'nom': 'X'}], datetime(2026, 1, 1, 3))
    assert plan['dus'] == []
    assert 'plage' in plan['reportes'][0]['raison']


def test_etat_scan_planifie(conn, make_client):
    cid = make_client()
    cfg_set('scan_auto:%d' % cid, '12h')
    cfg_set('scan_auto_actif', '1')
    etat = SP.etat_scan_planifie(conn, [{'id': cid, 'nom': 'ACME', 'acces': 'proprietaire'}])
    assert etat['actif'] is True
    ligne = next(l for l in etat['clients'] if l['client_id'] == cid)
    assert ligne['cadence'] == '12h' and ligne['planifie'] is True
    assert ligne['peut_ecrire'] is True
    cfg_set('scan_auto_actif', '0')


# ─── Import : origine « scan_auto » sur l'instantané ─────────────────────────

def test_importer_appareils_scan_origine(conn, make_client):
    import app as A
    cid = make_client()
    items = [{'ip': '10.9.9.9', 'ports': [80], 'netbios': 'BOX', 'mac': 'aa:bb:cc:dd:ee:ff',
              'type': 'PC'}]
    r = A._importer_appareils_scan(conn, cid, items, origine='scan_auto',
                                   libelle='Scan planifié (quotidien)')
    conn.commit()
    assert r['importes'] == 1
    row = conn.execute("SELECT origine, libelle FROM client_instantane WHERE id=?",
                       (r['instantane_id'],)).fetchone()
    assert row[0] == 'scan_auto' and 'planifié' in row[1]


# ─── Routes / ACL ───────────────────────────────────────────────────────────

def test_api_get_borne_aux_clients_accessibles(client, conn, make_client, make_user):
    uid, _, _ = make_user(role='user')
    mine = make_client(auth_user_id=uid)
    autre = make_client()                         # pas d'accès
    login_session(client, uid, mine)
    r = client.get('/api/scan/planifie')
    assert r.status_code == 200
    ids = {c['client_id'] for c in r.get_json()['clients']}
    assert mine in ids and autre not in ids


def test_api_post_cadence_exige_ecriture(client, conn, make_client, make_user):
    uid, _, _ = make_user(role='user')
    mine = make_client(auth_user_id=uid)
    autre = make_client()
    login_session(client, uid, mine)

    # cadence invalide → 400
    r = client.post('/api/scan/planifie', json={'client_id': mine, 'cadence': 'toutes_les_lunes'})
    assert r.status_code == 400

    # client sans accès en écriture → 403
    r = client.post('/api/scan/planifie', json={'client_id': autre, 'cadence': 'hebdo'})
    assert r.status_code == 403

    # OK
    r = client.post('/api/scan/planifie', json={'client_id': mine, 'cadence': 'hebdo'})
    assert r.status_code == 200
    from config_helpers import cfg_get, cfg_invalidate
    cfg_invalidate()
    assert cfg_get('scan_auto:%d' % mine) == 'hebdo'


def test_api_executer_sans_plage(client, conn, make_client, make_user):
    uid, _, _ = make_user(role='user')
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    r = client.post('/api/scan/planifie/executer', json={'client_id': cid})
    assert r.status_code == 400 and 'plage' in r.get_json()['error'].lower()
