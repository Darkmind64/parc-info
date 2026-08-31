"""Module de diagnostic réseau (network_diag.py) — routes, ACL, persistance.

Contrôle :
  - la page /diag-reseau et les endpoints /api/diag-reseau/* répondent
  - l'ACL : un utilisateur en lecture seule ne peut ni lancer un snapshot
    ni basculer la surveillance (403)
  - l'upsert par signature : une même signature n'ajoute pas de doublon
    mais incrémente nb_occurrences ; « résoudre » puis re-détecter rouvre
  - l'isolation multi-client : les évènements d'un client ne fuient pas
"""
import json

import pytest

import network_diag
from conftest import login_session


@pytest.fixture
def deux_clients(conn, make_client, make_user):
    proprio_id, _, _ = make_user(role='user')
    lecteur_id, _, _ = make_user(role='user')
    cid_a = make_client(auth_user_id=proprio_id)
    cid_b = make_client(auth_user_id=proprio_id)
    # lecteur : accès lecture seule sur cid_a
    conn.execute(
        "INSERT INTO client_partages (client_id, auth_user_id, niveau) VALUES (?,?,'lecture')",
        (cid_a, lecteur_id))
    conn.commit()
    return dict(proprio=proprio_id, lecteur=lecteur_id, cid_a=cid_a, cid_b=cid_b)


def test_page_diag_reseau_rend(client, deux_clients):
    login_session(client, deux_clients['proprio'], deux_clients['cid_a'])
    r = client.get('/diag-reseau')
    assert r.status_code == 200
    assert b'Diagnostic' in r.data


def test_etat_capture_json(client, deux_clients):
    login_session(client, deux_clients['proprio'], deux_clients['cid_a'])
    r = client.get('/api/diag-reseau/etat-capture')
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) == {'disponible', 'motif'}
    assert isinstance(body['disponible'], bool)


def test_snapshot_lecture_seule_interdit(client, deux_clients):
    login_session(client, deux_clients['lecteur'], deux_clients['cid_a'])
    r = client.post('/api/diag-reseau/snapshot', json={'avec_capture': False})
    assert r.status_code == 403


def test_surveillance_lecture_seule_interdit(client, deux_clients):
    login_session(client, deux_clients['lecteur'], deux_clients['cid_a'])
    r = client.post('/api/diag-reseau/surveillance', json={'actif': True})
    assert r.status_code == 403


def test_surveillance_bascule(client, deux_clients):
    login_session(client, deux_clients['proprio'], deux_clients['cid_a'])
    r = client.post('/api/diag-reseau/surveillance', json={'actif': True})
    assert r.status_code == 200 and r.get_json()['actif'] is True
    r = client.post('/api/diag-reseau/surveillance', json={'actif': False})
    assert r.get_json()['actif'] is False


def _f(ip='192.168.1.42'):
    return network_diag._finding('conflit_ip', f"Conflit sur {ip}", {'ip': ip}, ip)


def test_upsert_signature_pas_de_doublon(deux_clients):
    cid = deux_clients['cid_a']
    n1 = network_diag._enregistrer_evenements(cid, [_f()], 'actif')
    n2 = network_diag._enregistrer_evenements(cid, [_f()], 'actif')
    assert n1 == 1 and n2 == 0  # 2e passage : aucun nouvel évènement

    from database import get_db
    c = get_db()
    row = c.execute(
        "SELECT nb_occurrences FROM diag_reseau_evenements WHERE client_id=? AND categorie='conflit_ip'",
        (cid,)).fetchone()
    c.close()
    assert row[0] == 2


def test_resolution_puis_redetection_rouvre(client, deux_clients):
    cid = deux_clients['cid_a']
    network_diag._enregistrer_evenements(cid, [_f('10.0.0.9')], 'actif')
    login_session(client, deux_clients['proprio'], cid)
    evts = client.get('/api/diag-reseau/evenements?resolu=0').get_json()['evenements']
    eid = next(e['id'] for e in evts if e['details'].get('ip') == '10.0.0.9')

    assert client.post(f'/api/diag-reseau/evenement/{eid}/resoudre', json={}).status_code == 200
    assert not any(e['id'] == eid for e in
                   client.get('/api/diag-reseau/evenements?resolu=0').get_json()['evenements'])

    # re-détection de la même signature : l'évènement se rouvre
    network_diag._enregistrer_evenements(cid, [_f('10.0.0.9')], 'actif')
    reouvert = client.get('/api/diag-reseau/evenements?resolu=0').get_json()['evenements']
    assert any(e['id'] == eid for e in reouvert)


def test_purge_par_age(conn, deux_clients):
    """Un évènement résolu et ancien est purgé ; un ancien NON résolu survit."""
    cid = deux_clients['cid_a']
    conn.execute(
        "INSERT INTO diag_reseau_evenements (client_id,horodatage,gravite,categorie,titre,"
        "details_json,source,signature,resolu,date_resolu,premiere_occurrence,derniere_occurrence,nb_occurrences) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
        (cid, '2000-01-01T00:00:00Z', 'info', 'conflit_nom', 'vieux résolu', '{}', 'actif',
         'sig-vieux-resolu', 1, '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z'))
    conn.execute(
        "INSERT INTO diag_reseau_evenements (client_id,horodatage,gravite,categorie,titre,"
        "details_json,source,signature,resolu,date_resolu,premiere_occurrence,derniere_occurrence,nb_occurrences) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
        (cid, '2000-01-01T00:00:00Z', 'critique', 'conflit_ip', 'vieux actif', '{}', 'actif',
         'sig-vieux-actif', 0, None, '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z'))
    conn.commit()

    network_diag._purger_anciens(conn, cid)
    conn.commit()
    restants = {r[0] for r in conn.execute(
        "SELECT signature FROM diag_reseau_evenements WHERE client_id=?", (cid,)).fetchall()}
    assert 'sig-vieux-resolu' not in restants
    assert 'sig-vieux-actif' in restants


def test_isolation_multi_client(client, deux_clients):
    network_diag._enregistrer_evenements(deux_clients['cid_b'], [_f('172.16.0.5')], 'actif')
    login_session(client, deux_clients['proprio'], deux_clients['cid_a'])
    evts = client.get('/api/diag-reseau/evenements').get_json()['evenements']
    assert all(e['details'].get('ip') != '172.16.0.5' for e in evts)


def test_evenement_rattache_a_appareil(client, conn, deux_clients, make_appareil):
    cid = deux_clients['cid_a']
    aid = make_appareil(cid, nom_machine='POSTE-DIAG', adresse_ip='192.168.1.77')
    network_diag._enregistrer_evenements(
        cid, [network_diag._finding('conflit_ip', 'x', {'ip': '192.168.1.77'}, '192.168.1.77')], 'actif')
    login_session(client, deux_clients['proprio'], cid)
    evt = client.get('/api/diag-reseau/evenements').get_json()['evenements'][0]
    assert evt['appareil_id'] == aid
    assert evt['appareil_nom'] == 'POSTE-DIAG'


def test_evenements_dans_alertes_critiques(conn, deux_clients):
    from datetime import date
    import app as A
    cid = deux_clients['cid_a']
    network_diag._enregistrer_evenements(
        cid, [network_diag._finding('dhcp_pirate', 'DHCP pirate détecté', {'serveur': '10.0.0.9'}, '10.0.0.9')],
        'actif')
    crit = A._compute_critical_alerts(conn, cid, date.today())
    assert any(a['type'] == 'diag_reseau' and a['severity'] == 'critical' for a in crit['alerts'])


def test_page_mobile_diag(client, deux_clients):
    login_session(client, deux_clients['proprio'], deux_clients['cid_a'])
    r = client.get('/m/diag-reseau')
    assert r.status_code == 200
    assert b'Diagnostic' in r.data


def test_api_snmp_acl_et_forme(client, deux_clients):
    login_session(client, deux_clients['lecteur'], deux_clients['cid_a'])
    r = client.get('/api/diag-reseau/snmp')
    assert r.status_code == 200  # lecture seule autorisée
    body = r.get_json()
    assert set(body) >= {'actif', 'equipements'}
    assert isinstance(body['equipements'], list)


def test_snmp_releve_et_findings_rattaches(client, conn, deux_clients, make_appareil):
    cid = deux_clients['cid_a']
    aid = make_appareil(cid, nom_machine='SW-CORE', type_appareil='Switch', adresse_ip='10.0.0.2')

    def equip(ts, **o):
        p = dict(index=1, nom='Gi1/0/1', alias='', oper=1, admin=1, speed_mbps=1000,
                 in_oct=0, out_oct=0, in_err=0, out_err=0, in_disc=0, out_disc=0,
                 align_err=0, fcs_err=0, late_coll=0, exc_coll=0, duplex=3)
        p.update(o)
        return {'sysname': 'sw', 'ts': ts, 'ports': [p]}

    assert network_diag._analyser_snmp(cid, '10.0.0.2', aid, equip(1000.0)) == []
    findings = network_diag._analyser_snmp(cid, '10.0.0.2', aid, equip(1030.0, duplex=2))
    assert [f['categorie'] for f in findings] == ['duplex_mismatch']
    network_diag._enregistrer_evenements(cid, findings, 'snmp')

    login_session(client, deux_clients['proprio'], cid)
    snmp = client.get('/api/diag-reseau/snmp').get_json()
    eq = next(e for e in snmp['equipements'] if e['ip'] == '10.0.0.2')
    assert eq['appareil_id'] == aid
    port = eq['ports'][0]
    assert any(f['categorie'] == 'duplex_mismatch' for f in port['findings'])

    evt = next(e for e in client.get('/api/diag-reseau/evenements').get_json()['evenements']
               if e['categorie'] == 'duplex_mismatch')
    assert evt['appareil_id'] == aid  # rattaché au switch


def test_api_topologie_et_metriques_acl(client, deux_clients):
    login_session(client, deux_clients['lecteur'], deux_clients['cid_a'])
    assert client.get('/api/diag-reseau/topologie').status_code == 200
    assert client.get('/api/diag-reseau/metriques').status_code == 200
    # lecture seule : appliquer-baie interdit
    assert client.post('/api/diag-reseau/topologie/appliquer-baie', json={}).status_code == 403


def test_evenements_incluent_remediation(client, conn, deux_clients):
    cid = deux_clients['cid_a']
    network_diag._enregistrer_evenements(
        cid, [network_diag._finding('duplex_mismatch', 'x', {'equipement': '10.0.0.1', 'port_index': 1},
                                    '10.0.0.1', 1)], 'snmp')
    login_session(client, deux_clients['proprio'], cid)
    evt = client.get('/api/diag-reseau/evenements').get_json()['evenements'][0]
    assert evt['remediation'] and 'corriger' in evt['remediation']


def test_rapport_pdf_route(client, deux_clients):
    login_session(client, deux_clients['proprio'], deux_clients['cid_a'])
    r = client.get('/diag-reseau/rapport.pdf')
    assert r.status_code == 200
    assert r.headers['Content-Type'].startswith(('application/pdf', 'text/html'))
    rh = client.get('/diag-reseau/rapport.html')
    assert rh.status_code == 200 and b'<html' in rh.data.lower()


def test_metriques_serie(client, conn, deux_clients):
    import time
    cid = deux_clients['cid_a']
    ep = time.time()
    for i in range(12):
        conn.execute("INSERT INTO diag_metriques (client_id,categorie,cible,horodatage,epoch,valeur) "
                     "VALUES (?,?,?,?,?,?)", (cid, 'liaison_latence', '8.8.8.8', 'x', ep - 60 * i, 5.0 + i))
    conn.commit()
    login_session(client, deux_clients['proprio'], cid)
    d = client.get('/api/diag-reseau/metriques?categorie=liaison_latence&cible=8.8.8.8').get_json()
    assert d['points'] and d['mediane'] is not None


def test_alerte_email_echappe_le_titre(monkeypatch, deux_clients):
    """Le titre d'un évènement (données LAN non fiables) est échappé avant
    insertion dans le corps HTML de l'e-mail d'alerte."""
    envois = []
    monkeypatch.setattr('config_helpers.cfg_get', lambda k, d=None, **kw: {
        'diag_alerte_email': '1', 'diag_alerte_destinataire': 'admin@example.com'}.get(k, d or ''))
    import app as A
    monkeypatch.setattr(A, '_send_email', lambda to, sujet, corps: envois.append((to, sujet, corps)) or True)

    f = network_diag._finding('conflit_ip', 'Machine <script>alert(1)</script>', {'ip': '1.2.3.4'}, '1.2.3.4')
    f['gravite'] = 'critique'
    network_diag._alerter_email(deux_clients['cid_a'], [f])

    assert envois, "un e-mail aurait dû être envoyé"
    corps = envois[0][2]
    assert '<script>' not in corps
    assert '&lt;script&gt;' in corps
