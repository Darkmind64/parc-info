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


def test_api_test_snmp(client, conn, deux_clients, make_appareil, monkeypatch):
    cid = deux_clients['cid_a']
    make_appareil(cid, nom_machine='SW-T', type_appareil='Switch', adresse_ip='10.0.0.5')
    import app as A
    monkeypatch.setattr(A, '_snmp_get', lambda ip, oids, comm='public', timeout=1.5: {
        A._OID_SYS_NAME: 'SW-CORE', A._OID_SYS_DESCR: 'Test switch'} if comm == 'public' else {})
    login_session(client, deux_clients['proprio'], cid)
    d = client.post('/api/diag-reseau/test-snmp', json={}).get_json()
    assert d['ok'] is True and d['sysname'] == 'SW-CORE' and d['communaute'] == 'public'
    # lecture seule -> 403
    login_session(client, deux_clients['lecteur'], cid)
    assert client.post('/api/diag-reseau/test-snmp', json={}).status_code == 403


def test_api_test_snmp_sans_equipement(client, deux_clients, monkeypatch):
    login_session(client, deux_clients['proprio'], deux_clients['cid_b'])
    d = client.post('/api/diag-reseau/test-snmp', json={}).get_json()
    assert d['ok'] is False and 'motif' in d


def test_api_wifi_et_ups(client, deux_clients, monkeypatch):
    monkeypatch.setattr(network_diag, 'etat_wifi',
                        lambda: {'connecte': False, 'motif': 'aucun adaptateur Wi-Fi'})
    login_session(client, deux_clients['lecteur'], deux_clients['cid_a'])
    w = client.get('/api/diag-reseau/wifi').get_json()
    assert 'actif' in w and w['connecte'] is False
    u = client.get('/api/diag-reseau/ups').get_json()
    assert 'actif' in u and isinstance(u['onduleurs'], list)


def test_ups_finding_rattache_appareil(client, conn, deux_clients, make_appareil):
    cid = deux_clients['cid_a']
    aid = make_appareil(cid, nom_machine='UPS-1', type_appareil='Onduleur / UPS', adresse_ip='10.0.0.9')
    ups = {'modele': 'APC', 'source': 5, 'source_txt': 'batterie', 'charge_pct': 40,
           'autonomie_min': 3, 'batterie_pct': 50, 'batterie_statut': 3,
           'batterie_statut_txt': 'faible', 'sur_batterie_s': 30, 'temp_c': 25,
           'tension_entree': 230, 'remplacer_batterie': False, 'alarmes': 0, 'ts': 1000.0}
    findings = network_diag._analyser_ups(cid, '10.0.0.9', aid, ups)
    cats = {f['categorie'] for f in findings}
    assert 'ups_sur_batterie' in cats and 'ups_batterie_faible' in cats
    assert all(f['appareil_id'] == aid for f in findings)
    network_diag._enregistrer_evenements(cid, findings, 'snmp')
    login_session(client, deux_clients['proprio'], cid)
    evt = next(e for e in client.get('/api/diag-reseau/evenements').get_json()['evenements']
               if e['categorie'] == 'ups_sur_batterie')
    assert evt['appareil_id'] == aid and evt['remediation']


def test_interroger_ups_utilise_snmp_get_typed(deux_clients, monkeypatch):
    """Régression v2.19.5 : les scalaires INTEGER de l'UPS-MIB doivent être lus
    (interroger_ups() utilisait _snmp_get qui ne renvoie que les OCTET STRING)."""
    import app as A
    appele = {'typed': False}

    def _typed(ip, oids, comm='public', timeout=1.5, port=161):
        appele['typed'] = True
        return {A._OID_UPS_MODEL if hasattr(A, '_OID_UPS_MODEL') else '': ''} or {}

    # network_diag importe app._snmp_get_typed paresseusement
    monkeypatch.setattr(A, '_snmp_get_typed',
                        lambda ip, oids, comm='public', timeout=1.5, port=161: (
                            appele.__setitem__('typed', True) or
                            {network_diag._OID_UPS_OUT_SOURCE: 5,
                             network_diag._OID_UPS_MODEL: 'X'}))
    monkeypatch.setattr(network_diag, '_snmp_walk', lambda oid, ip, c: {})
    ups = network_diag.interroger_ups('10.0.0.9', ['public'])
    assert appele['typed'] and ups and ups['source'] == 5


def test_wifi_cache(monkeypatch):
    """etat_wifi() met son résultat en cache (le scan est lent/intrusif)."""
    network_diag._wifi_cache.update(ts=0.0, val=None)
    appels = []
    monkeypatch.setattr(network_diag, 'IS_WINDOWS', True)
    monkeypatch.setattr(network_diag, '_wifi_windows',
                        lambda: appels.append(1) or {'connecte': False, 'motif': 'x'})
    network_diag.etat_wifi()
    network_diag.etat_wifi()
    assert len(appels) == 1  # 2e appel servi par le cache
    assert len(network_diag.etat_wifi(forcer=True)) or True
    assert len(appels) == 2  # forcer=True re-scanne


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


# ── Vue d'activité de la baie (LEDs live SNMP, v2.19.6-.8) ────────────────────

def test_etat_led_transitions():
    seuils = {'err': 20, 'sat_pct': 90, 'pps_mini': 1}
    prev = dict(in_oct=0, out_oct=0, in_pkts=0, out_pkts=0, in_err=0, out_err=0, ts=0.0)
    # débit faible (800 bit/s, sous _ACTIVITE_BPS_MINI) mais 3 paquets/s : doit
    # quand même clignoter — c'est le correctif v2.19.8 (avant : seul le % de
    # bande passante comptait, un port bureautique normal restait "idle").
    cur = dict(oper=1, speed_mbps=1000, in_oct=100, out_oct=0, in_pkts=3, out_pkts=0,
               in_err=0, out_err=0)

    led = network_diag._etat_led(prev, cur, 1.0, seuils)
    assert led['etat'] == 'traffic' and 120 <= led['blink_ms'] <= 1200

    sat = dict(cur, in_oct=120_000_000, in_pkts=3000)              # ~96 % -> saturé
    assert network_diag._etat_led(prev, sat, 1.0, seuils)['etat'] == 'sature'

    err = dict(cur, in_err=30)                                     # Δerr 30 > 20 -> err (prioritaire)
    assert network_diag._etat_led(prev, err, 1.0, seuils)['etat'] == 'err'

    assert network_diag._etat_led(prev, dict(cur, oper=2), 1.0, seuils)['etat'] == 'down'

    calme = dict(cur, in_oct=0, in_pkts=0)
    assert network_diag._etat_led(prev, calme, 1.0, seuils)['etat'] == 'idle'

    reboot = network_diag._etat_led(
        dict(in_oct=10**9, out_oct=0, in_pkts=0, out_pkts=0, in_err=0, out_err=0, ts=0.0),
        cur, 1.0, seuils)
    assert reboot['bps'] >= 0 and reboot['reset'] is True         # compteur qui recule : jamais négatif


def test_etat_led_plafond_compteur_aberrant():
    """Un compteur qui dépègue / boucle / bascule 64<->32 bits injecte un delta
    gigantesque : le débit et le pps ne doivent jamais dépasser la capacité du
    lien (régression du moniteur qui affichait des millions de pkt/s)."""
    seuils = {'err': 20, 'sat_pct': 90, 'pps_mini': 1}
    prev = dict(in_oct=0, out_oct=0, in_pkts=0, out_pkts=0, in_err=0, out_err=0,
                ts=0.0, bps_ema=4_000.0, pps_ema=12.0)
    # +1,4 milliard de paquets en 14 s sur un lien 1 Gb/s : impossible -> on garde
    # la dernière valeur lissée connue, pas le pic
    aberrant = dict(oper=1, speed_mbps=1000, in_oct=10**12, out_oct=0,
                    in_pkts=1_400_000_000, out_pkts=0, in_err=0, out_err=0)
    led = network_diag._etat_led(prev, aberrant, 14.0, seuils)
    assert led['pps'] <= 1000 * 1500 and led['bps'] <= 1000 * 1e6 * 1.05
    assert abs(led['pps_ema'] - 12.0) < 1.0        # revenu vers la valeur connue

    # compteur explicitement pégé -> aucun débit inventé
    pegge = dict(oper=1, speed_mbps=1000, in_oct=0, out_oct=0, in_pkts=0, out_pkts=0,
                 in_err=0, out_err=0, cpt_pegge=True)
    lp = network_diag._etat_led(prev, pegge, 14.0, seuils)
    assert lp['pps'] <= 12.0 and lp['bps'] <= 4_000.0

    # lien inconnu : plafond absolu
    sans_vitesse = dict(oper=1, speed_mbps=0, in_oct=10**12, out_oct=0,
                        in_pkts=5_000_000_000, out_pkts=0, in_err=0, out_err=0)
    ls = network_diag._etat_led(dict(prev, bps_ema=0.0, pps_ema=0.0), sans_vitesse, 14.0, seuils)
    assert ls['pps'] <= network_diag._ACTIVITE_PPS_MAX_PORT


def test_etat_led_antirebond_idle_traffic():
    """Le passage idle<->traffic n'est retenu qu'après _ACTIVITE_DEBOUNCE cycles
    consécutifs — un port marginal ne fait plus scintiller la LED ni varier le
    compte de « ports actifs » d'une instance à l'autre."""
    assert network_diag._ACTIVITE_DEBOUNCE == 2
    seuils = {'err': 20, 'sat_pct': 90, 'pps_mini': 15}
    p0 = dict(in_oct=0, out_oct=0, in_pkts=0, out_pkts=0, in_err=0, out_err=0,
              ts=0.0, etat='idle', bps_ema=0.0, pps_ema=0.0)
    traf = dict(oper=1, speed_mbps=1000, in_oct=0, out_oct=0,
                in_pkts=100, out_pkts=100, in_err=0, out_err=0)   # 200 pkt/s

    # 1er cycle avec du trafic : encore « idle », mais transition en attente
    l1 = network_diag._etat_led(p0, traf, 1.0, seuils)
    assert l1['etat'] == 'idle'
    assert l1['etat_pending'] == 'traffic' and l1['etat_pending_n'] == 1

    # 2e cycle consécutif : bascule confirmée
    p1 = dict(p0, etat=l1['etat'], etat_pending=l1['etat_pending'],
              etat_pending_n=l1['etat_pending_n'],
              bps_ema=l1['bps_ema'], pps_ema=l1['pps_ema'])
    l2 = network_diag._etat_led(p1, dict(traf, in_pkts=200, out_pkts=200), 1.0, seuils)
    assert l2['etat'] == 'traffic' and l2['etat_pending'] is None

    # trafic qui s'arrête : un seul cycle calme ne coupe pas la LED tout de suite
    p2 = dict(p0, etat='traffic', bps_ema=l2['bps_ema'], pps_ema=0.0,
              in_pkts=200, out_pkts=200)
    calme = dict(oper=1, speed_mbps=1000, in_oct=0, out_oct=0,
                 in_pkts=200, out_pkts=200, in_err=0, out_err=0)   # 0 pkt/s
    l3 = network_diag._etat_led(p2, calme, 1.0, seuils)
    assert l3['etat'] == 'traffic' and l3['etat_pending'] == 'idle'
    p3 = dict(p2, etat=l3['etat'], etat_pending='idle', etat_pending_n=l3['etat_pending_n'],
              pps_ema=l3['pps_ema'])
    l4 = network_diag._etat_led(p3, calme, 1.0, seuils)
    assert l4['etat'] == 'idle'

    # un défaut (err/sature/down) ignore l'anti-rebond
    down = network_diag._etat_led(dict(p0, etat='traffic'), dict(traf, oper=2), 1.0, seuils)
    assert down['etat'] == 'down'


def test_port_physique_depuis_nom():
    f = network_diag._port_physique_depuis_nom
    assert f('GigabitEthernet1/0/12') == 12
    assert f('Gi1/0/12') == 12 and f('Te1/1/1') == 1
    assert f('ethernet1/12') == 12 and f('Ethernet1/0/48') == 48
    assert f('xe-0/0/12.0') == 12 and f('ge-0/0/5') == 5
    assert f('Port 12') == 12 and f('swp12') == 12 and f('eth7') == 7
    assert f('') is None and f(None) is None


def test_mapping_baie_ifindex(conn, deux_clients, make_appareil):
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.2')
    pc = make_appareil(cid, nom_machine='PC', adresse_ip='10.0.0.50')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero, appareil_id) VALUES (?,12,?)", (slot_id, pc))
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,7)", (slot_id,))
    conn.commit()
    # l'ifIndex n'est PAS le numéro de port : port 12 = Gi1/0/12 = ifIndex 37
    infos = {37: {'nom': 'Gi1/0/12', 'alias': '', 'ethernet': True},
             5:  {'nom': 'Gi1/0/7',  'alias': '', 'ethernet': True}}

    m, src, cal = network_diag._mapping_baie_ifindex(conn, cid, slot_id, sw, infos)
    assert m == {12: 37, 7: 5} and cal is True           # via le NOM d'interface
    assert src[12] == 'nom_port' and src[7] == 'nom_port'

    # topologie : PC vu sur ifIndex 99 -> l'emporte sur le nom
    conn.execute("INSERT INTO diag_topologie (client_id, equipement_ip, equipement_appareil_id, "
                 "port_index, appareil_vu_id, horodatage) VALUES (?,?,?,?,?,'x')",
                 (cid, '10.0.0.2', sw, 99, pc))
    conn.commit()
    m2, src2, _ = network_diag._mapping_baie_ifindex(conn, cid, slot_id, sw, infos)
    assert m2[12] == 99 and src2[12] == 'topologie'

    # calibration manuelle : l'emporte sur tout
    conn.execute("UPDATE baie_slot_ports SET if_index=42 WHERE slot_id=? AND numero=12", (slot_id,))
    conn.commit()
    m3, src3, _ = network_diag._mapping_baie_ifindex(conn, cid, slot_id, sw, infos)
    assert m3[12] == 42 and src3[12] == 'manuel'

    # repli naïf désactivé par défaut : un port sans topo/nom/manuel n'est PAS mappé
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,3)", (slot_id,))
    conn.commit()
    m4, _, _ = network_diag._mapping_baie_ifindex(
        conn, cid, slot_id, sw, {3: {'nom': 'bizarre', 'alias': '', 'ethernet': True}})
    assert 3 not in m4


def test_mapping_repli_naif_optin(conn, deux_clients, make_appareil, monkeypatch):
    monkeypatch.setattr(network_diag, '_cfg',
                        lambda k, d=None: '1' if k == 'diag_baie_activite_repli_naif' else d)
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.9')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,5)", (slot_id,))
    conn.commit()
    m, src, cal = network_diag._mapping_baie_ifindex(
        conn, cid, slot_id, sw, {5: {'nom': 'bizarre', 'alias': '', 'ethernet': True}})
    assert m == {5: 5} and src[5] == 'repli' and cal is False


def test_noms_interfaces_cache(monkeypatch):
    import app as A
    appels = {'n': 0}

    def fake_bulk(ip, bases, comm, **kw):
        appels['n'] += 1
        return {network_diag._OID_IF_NAME:  {'1': 'Gi0/1', '2': 'Gi0/2'},
                network_diag._OID_IF_ALIAS: {'1': 'poste bureau'},
                network_diag._OID_IF_TYPE:  {'1': 6, '2': 6, '3': 24},
                network_diag._OID_IF_DESCR: {'1': 'GigabitEthernet0/1', '2': 'GigabitEthernet0/2', '3': 'Vlan1'}}
    monkeypatch.setattr(A, '_snmp_bulk_cols', fake_bulk)
    network_diag._activite_noms.pop('10.9.9.9', None)

    infos = network_diag._noms_interfaces('10.9.9.9', ['public'])
    assert infos[1]['nom'] == 'Gi0/1' and infos[1]['alias'] == 'poste bureau'
    assert infos[1]['ethernet'] is True and infos[3]['ethernet'] is False   # Vlan1 = ifType 24
    n1 = appels['n']
    network_diag._noms_interfaces('10.9.9.9', ['public'])                    # servi par le cache
    assert appels['n'] == n1

    # un cache VIDE n'est pas conservé (on retentera au cycle suivant)
    network_diag._activite_noms.pop('10.9.9.9', None)
    monkeypatch.setattr(A, '_snmp_bulk_cols', lambda *a, **k: {})
    assert network_diag._noms_interfaces('10.9.9.9', ['public']) == {}
    assert '10.9.9.9' not in network_diag._activite_noms


def test_poll_switch_ports(monkeypatch):
    import app as A

    network_diag._activite_hc.pop('10.0.0.2', None)
    _infos = {1: {'nom': 'Gi0/1', 'alias': '', 'ethernet': True, 'speed_mbps': 1000},
              2: {'nom': 'Gi0/2', 'alias': '', 'ethernet': True, 'speed_mbps': 1000}}
    _TABLE = {network_diag._OID_IF_OPER:        {'1': 1, '2': 2},
              network_diag._OID_IF_HCIN:        {'1': 5000, '2': 0},
              network_diag._OID_IF_HCOUT:       {'1': 9000, '2': 0},
              network_diag._OID_IF_HCIN_UCAST:  {'1': 10, '2': 0},
              network_diag._OID_IF_HCOUT_UCAST: {'1': 20, '2': 0}}

    monkeypatch.setattr(A, '_snmp_bulk_cols',
                        lambda ip, bases, comm, **k: {b: dict(_TABLE.get(b, {})) for b in bases})
    res, ok, hc = network_diag._poll_switch_ports('10.0.0.2', ['public'], _infos)
    assert ok is True and hc is True
    assert res[1]['oper'] == 1 and res[1]['speed_mbps'] == 1000 and res[1]['out_oct'] == 9000
    assert res[1]['in_pkts'] == 10 and res[2]['oper'] == 2

    # valeur sentinelle 32 bits (« compteur indisponible » de certains agents) -> 0.
    # Il faut _ACTIVITE_NEG_CONFIRME relevés sans ifXTable avant de basculer en
    # 32 bits (un paquet HC perdu ne doit pas condamner le mode 64 bits).
    for d in (network_diag._activite_hc, network_diag._activite_capa_neg,
              network_diag._activite_capa_reprobe):
        d.pop('10.0.0.5', None)
    _T2 = {network_diag._OID_IF_OPER: {'1': 1},
           network_diag._OID_IF_IN_OCTETS: {'1': 2**31 - 1},
           network_diag._OID_IF_OUT_OCTETS: {'1': 12345}}
    monkeypatch.setattr(A, '_snmp_bulk_cols',
                        lambda ip, bases, comm, **k: {b: dict(_T2.get(b, {})) for b in bases})
    network_diag._poll_switch_ports('10.0.0.5', ['public'], {})          # 1er négatif
    assert network_diag._activite_hc.get('10.0.0.5') is None             # pas encore basculé
    res2, ok2, hc2 = network_diag._poll_switch_ports('10.0.0.5', ['public'], {})   # 2e -> 32 bits
    assert res2[1]['in_oct'] == 0 and res2[1]['out_oct'] == 12345 and hc2 is False
    assert res2[1]['cpt_pegge'] is True

    for d in (network_diag._activite_hc, network_diag._activite_capa_neg,
              network_diag._activite_capa_reprobe):
        d.pop('10.0.0.6', None)
    monkeypatch.setattr(A, '_snmp_bulk_cols', lambda *a, **k: {})
    res3, ok3, _ = network_diag._poll_switch_ports('10.0.0.6', ['public'], {})
    assert ok3 is False and res3 == {}


def test_api_baie_activite_route_sans_snmp_synchrone(client, conn, deux_clients, make_appareil, monkeypatch):
    cid = deux_clients['cid_a']
    monkeypatch.setattr(network_diag, '_poll_switch_ports',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('SNMP synchrone dans le handler')))
    login_session(client, deux_clients['lecteur'], cid)          # lecture seule suffit
    r = client.get('/api/baie/activite')
    assert r.status_code == 200 and 'actif' in r.get_json()


def _mock_snmp_switch(monkeypatch, ports):
    """ports = {ifindex: dict(oper,speed_mbps,in_oct,out_oct,in_pkts,out_pkts,in_err,out_err)}"""
    monkeypatch.setattr(network_diag, '_noms_interfaces',
                        lambda ip, c: {i: {'nom': f'Gi0/{i}', 'alias': '', 'ethernet': True,
                                           'speed_mbps': ports[i].get('speed_mbps', 0)} for i in ports})
    monkeypatch.setattr(network_diag, '_poll_switch_ports',
                        lambda ip, c, infos=None: (dict(ports), bool(ports), True))


def test_cycle_activite_peuple_le_resultat(conn, deux_clients, make_appareil, monkeypatch):
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.2')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,1)", (slot_id,))
    conn.commit()
    # port baie 1 → nom 'Gi0/1' → ifIndex 1 (mapping par nom, aucune topologie)
    _mock_snmp_switch(monkeypatch, {1: dict(oper=1, speed_mbps=1000, in_oct=0, out_oct=0,
                                            in_pkts=0, out_pkts=0, in_err=0, out_err=0)})
    network_diag._cycle_activite([cid])
    with network_diag._activite_lock:
        res = network_diag._activite_resultat.get(cid)
        detail = network_diag._activite_detail.get(cid)
    assert res and res['actif'] is True
    assert any(p['numero'] == 1 for p in res['ports'])
    assert res['equipements'][0]['ip'] == '10.0.0.2'
    assert detail['switchs'][0]['compteurs_64bits'] is True
    assert detail['ports'][0]['numero'] == 1 and detail['ports'][0]['source_mapping'] == 'nom_port'
    assert any(i['ifindex'] == 1 for i in detail['interfaces'])   # liste complète des interfaces


def test_cycle_activite_stale_apres_echecs(conn, deux_clients, make_appareil, monkeypatch):
    """Un relevé manqué garde le dernier état connu (jamais 'down' juste parce
    que le SNMP a raté) ; après 3 échecs consécutifs, le port passe 'stale'."""
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.3')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,1)", (slot_id,))
    conn.commit()
    _mock_snmp_switch(monkeypatch, {1: dict(oper=1, speed_mbps=1000, in_oct=1000, out_oct=0,
                                            in_pkts=1, out_pkts=0, in_err=0, out_err=0)})
    network_diag._cycle_activite([cid])

    monkeypatch.setattr(network_diag, '_poll_switch_ports', lambda ip, c, infos=None: ({}, False, False))
    for _ in range(2):
        network_diag._cycle_activite([cid])
        with network_diag._activite_lock:
            etat = next(p['etat'] for p in network_diag._activite_resultat[cid]['ports'] if p['numero'] == 1)
        assert etat != 'down'

    network_diag._cycle_activite([cid])
    with network_diag._activite_lock:
        etat = next(p['etat'] for p in network_diag._activite_resultat[cid]['ports'] if p['numero'] == 1)
    assert etat == 'stale'


def test_calibrer_port_baie(conn, deux_clients, make_appareil):
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.2')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,5)", (slot_id,))
    conn.commit()

    assert network_diag.calibrer_port_baie(cid, slot_id, 5, 37) is True
    from database import get_db
    c = get_db()
    assert c.execute("SELECT if_index FROM baie_slot_ports WHERE slot_id=? AND numero=5",
                     (slot_id,)).fetchone()[0] == 37
    c.close()
    # effacer
    network_diag.calibrer_port_baie(cid, slot_id, 5, None)
    c = get_db()
    assert c.execute("SELECT if_index FROM baie_slot_ports WHERE slot_id=? AND numero=5",
                     (slot_id,)).fetchone()[0] is None
    c.close()
    # slot d'un autre client -> refusé
    assert network_diag.calibrer_port_baie(deux_clients['cid_b'], slot_id, 5, 12) is False
    # numéro qui n'est pas un port réel du slot -> refusé, aucune ligne créée
    assert network_diag.calibrer_port_baie(cid, slot_id, 999, 12) is False
    c = get_db()
    assert c.execute("SELECT COUNT(*) FROM baie_slot_ports WHERE slot_id=? AND numero=999",
                     (slot_id,)).fetchone()[0] == 0
    c.close()


def test_poll_capa_reprobe(monkeypatch):
    """Une capacité (PoE) déclarée absente est re-testée après
    _ACTIVITE_REPROBE_CYCLES — un paquet perdu ne la condamne pas pour toujours."""
    import app as A
    for d in (network_diag._activite_poe, network_diag._activite_capa_neg,
              network_diag._activite_capa_reprobe):
        d.pop('10.9.9.9', None)
    monkeypatch.setattr(A, '_snmp_bulk_cols', lambda *a, **k: {})
    network_diag._poll_poe('10.9.9.9', ['public'])
    network_diag._poll_poe('10.9.9.9', ['public'])
    assert network_diag._activite_poe['10.9.9.9'] is False
    # avant l'échéance de re-test : on ne sollicite plus le switch
    appels = []
    monkeypatch.setattr(A, '_snmp_bulk_cols', lambda ip, bases, comm, **k: appels.append(1) or {})
    network_diag._poll_poe('10.9.9.9', ['public'])
    assert appels == []
    # échéance atteinte -> nouveau relevé
    network_diag._activite_capa_reprobe['10.9.9.9']['poe'] = network_diag._activite_cyc[0] - 1
    network_diag._poll_poe('10.9.9.9', ['public'])
    assert appels == [1]


def test_route_calibrer_acl(client, conn, deux_clients, make_appareil):
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.2')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,5)", (slot_id,))
    conn.commit()
    login_session(client, deux_clients['lecteur'], cid)
    assert client.post('/api/baie/activite/calibrer',
                       json={'slot_id': slot_id, 'numero': 5, 'if_index': 12}).status_code == 403
    login_session(client, deux_clients['proprio'], cid)
    assert client.post('/api/baie/activite/calibrer',
                       json={'slot_id': slot_id, 'numero': 5, 'if_index': 12}).get_json()['ok'] is True


def test_interroger_equipement_getbulk(monkeypatch):
    """interroger_equipement lit par GETBULK ; ifType 117 (gigabitEthernet,
    déprécié — HP ProCurve) est bien reconnu comme port ethernet."""
    import app as A
    table = {
        network_diag._OID_IF_DESCR:     {'1': 'Gi1/0/1', '2': 'Gi1/0/2', '3': 'Vlan1'},
        network_diag._OID_IF_TYPE:      {'1': 117, '2': 6, '3': 53},   # 117 & 6 -> ethernet ; 53 -> non
        network_diag._OID_IF_OPER:      {'1': 1, '2': 2, '3': 1},
        network_diag._OID_IF_ADMIN:     {'1': 1, '2': 1, '3': 1},
        network_diag._OID_IF_HIGHSPEED: {'1': 1000, '2': 1000},
    }
    monkeypatch.setattr(A, '_snmp_bulk_cols',
                        lambda ip, bases, comm, **k: {b: dict(table.get(b, {})) for b in bases})
    monkeypatch.setattr(A, '_snmp_get', lambda *a, **k: {A._OID_SYS_NAME: 'SW-CORE'})
    eq = network_diag.interroger_equipement('10.0.0.2', ['public'])
    assert eq and eq['sysname'] == 'SW-CORE'
    idxs = {p['index'] for p in eq['ports']}
    assert idxs == {1, 2}          # Vlan1 (ifType 53) exclu
    assert next(p for p in eq['ports'] if p['index'] == 1)['speed_mbps'] == 1000


def test_poll_poe(monkeypatch):
    import app as A
    table = {
        network_diag._OID_POE_DETECT: {'1.5': 3, '1.6': 4, '1.7': 1},   # port 5 alimenté, 6 défaut, 7 off
        network_diag._OID_POE_CLASS:  {'1.5': 4, '1.6': 2},
        network_diag._OID_POE_MAIN_W: {'1': 370},
        network_diag._OID_POE_CONS_W: {'1': 62},
    }
    monkeypatch.setattr(A, '_snmp_bulk_cols',
                        lambda ip, bases, comm, **k: {b: dict(table.get(b, {})) for b in bases})
    network_diag._activite_poe.pop('10.0.0.2', None)
    poe = network_diag._poll_poe('10.0.0.2', ['public'])
    assert poe['budget_w'] == 370 and poe['total_w'] == 62
    assert poe['ports'][5]['statut_txt'] == 'alimenté' and poe['ports'][5]['classe'] == 3
    assert poe['ports'][5]['watts_max'] == 15.4
    assert poe['ports'][6]['statut'] == 4          # défaut
    assert poe['ports'][7]['watts_max'] is None

    # switch sans PoE -> {} ; il faut _ACTIVITE_NEG_CONFIRME relevés vides avant
    # d'arrêter de le solliciter (un paquet perdu ne condamne pas le PoE)
    monkeypatch.setattr(A, '_snmp_bulk_cols', lambda *a, **k: {})
    for d in (network_diag._activite_poe, network_diag._activite_capa_neg,
              network_diag._activite_capa_reprobe):
        d.pop('10.0.0.9', None)
    assert network_diag._poll_poe('10.0.0.9', ['public']) == {}
    assert network_diag._activite_poe.get('10.0.0.9') is None       # pas encore confirmé
    assert network_diag._poll_poe('10.0.0.9', ['public']) == {}
    assert network_diag._activite_poe['10.0.0.9'] is False          # confirmé après 2


def _calib_cycle(cid, sid, ports):
    network_diag._activite_cyc[0] += 1
    return network_diag._maj_assistant_calibration(cid, sid, '10.0.0.2', ports)


def test_assistant_calibration_detecte_transition():
    """L'interface débranchée puis rebranchée pendant la fenêtre est celle du
    port ; la décision attend un cycle sans nouveau mouvement."""
    network_diag._activite_calib.clear()
    cid, sid, num = 900, 7, 12
    network_diag.assistant_calibration(cid, sid, num, 'start')

    _calib_cycle(cid, sid, {5: {'oper': 1}, 6: {'oper': 1}, 7: {'oper': 1}})   # référence
    _calib_cycle(cid, sid, {5: {'oper': 1}, 6: {'oper': 2}, 7: {'oper': 1}})   # if6 débranché
    _calib_cycle(cid, sid, {5: {'oper': 1}, 6: {'oper': 1}, 7: {'oper': 1}})   # if6 rebranché (2 transitions)
    assert network_diag._activite_calib[(cid, sid)]['trouve'] is None          # réseau pas encore calme
    r = _calib_cycle(cid, sid, {5: {'oper': 1}, 6: {'oper': 1}, 7: {'oper': 1}})   # cycle calme -> décision
    assert network_diag._activite_calib[(cid, sid)]['trouve'] == 6
    assert r == (sid, num, 6)
    network_diag._activite_calib.clear()


def test_assistant_calibration_multi_flap():
    """Si un voisin flappe avant la fin du geste, on retient l'interface
    débranchée/rebranchée en DERNIER (celle sur laquelle on agit vraiment)."""
    network_diag._activite_calib.clear()
    cid, sid, num = 901, 8, 4
    network_diag.assistant_calibration(cid, sid, num, 'start')
    up = lambda extra=None: {**{3: {'oper': 1}, 4: {'oper': 1}, 9: {'oper': 1}}, **(extra or {})}

    _calib_cycle(cid, sid, up())                       # référence
    _calib_cycle(cid, sid, up({9: {'oper': 2}}))       # if9 (voisin) flappe
    _calib_cycle(cid, sid, up())                       # if9 rebranché (geste complet, mauvais port)
    _calib_cycle(cid, sid, up({4: {'oper': 2}}))       # if4 (le vrai) débranché, plus tard
    _calib_cycle(cid, sid, up())                       # if4 rebranché
    r = _calib_cycle(cid, sid, up())                   # cycle calme -> décision
    assert network_diag._activite_calib[(cid, sid)]['trouve'] == 4
    assert r == (sid, num, 4)
    network_diag._activite_calib.clear()


def test_route_assistant_acl(client, conn, deux_clients, make_appareil):
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.2')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.commit()
    login_session(client, deux_clients['lecteur'], cid)
    assert client.post('/api/baie/activite/calibrer/assistant',
                       json={'slot_id': slot_id, 'numero': 5}).status_code == 403
    login_session(client, deux_clients['proprio'], cid)
    r = client.post('/api/baie/activite/calibrer/assistant', json={'slot_id': slot_id, 'numero': 5})
    assert r.get_json()['etat'] == 'attente'
    network_diag._activite_calib.clear()


def test_journal_dedoublonne():
    network_diag._activite_journal.clear()
    network_diag._journal('même message', 'info', 'x')
    network_diag._journal('même message', 'info', 'x')
    network_diag._journal('autre message', 'info', 'x')
    assert len(network_diag._activite_journal) == 2
    assert network_diag._activite_journal[1]['n'] == 2   # le plus ancien (dédoublonné) a été incrémenté


def test_moniteur_baie_forme(conn, deux_clients):
    d = network_diag.moniteur_baie(deux_clients['cid_a'])
    assert set(d) >= {'switchs', 'ports', 'interfaces', 'ports_baie', 'journal', 'capture', 'snmp_actif'}
    assert isinstance(d['journal'], list) and isinstance(d['interfaces'], list)


def test_route_moniteur_forme_et_acl(client, deux_clients):
    cid = deux_clients['cid_a']
    login_session(client, deux_clients['lecteur'], cid)          # lecture seule suffit
    r = client.get('/api/baie/activite/moniteur')
    assert r.status_code == 200
    assert set(r.get_json()) >= {'switchs', 'ports', 'interfaces', 'ports_baie', 'journal', 'capture', 'snmp_actif'}


def test_capturer_trafic_indisponible(monkeypatch):
    monkeypatch.setattr(network_diag, 'etat_capture', lambda: {'disponible': False, 'motif': 'scapy_absent'})
    assert network_diag.capturer_trafic(5) == {'disponible': False, 'motif': 'scapy_absent'}


def test_lancer_capture_baie_async(monkeypatch):
    """Thread détaché : lancer_capture_baie() ne bloque pas l'appelant."""
    monkeypatch.setattr(network_diag, 'capturer_trafic',
                        lambda duree, client_id=None: {'disponible': True, 'motif': 'ok',
                                                        'duree_s': duree, 'total': 42,
                                                        'broadcast': 1, 'multicast': 0, 'unicast': 41,
                                                        'protos': {}, 'talkers': [], 'anomalies': []})
    with network_diag._capture_baie_lock:
        network_diag._capture_baie_status.update(running=False, resultat=None, client_id=None)
    assert network_diag.lancer_capture_baie(99, 1) is True
    assert network_diag.lancer_capture_baie(99, 1) is False      # déjà en cours

    import time as _t
    for _ in range(50):
        if not network_diag.statut_capture_baie()['running']:
            break
        _t.sleep(0.05)
    st = network_diag.statut_capture_baie()
    assert st['running'] is False and st['resultat']['total'] == 42


def test_route_capture_acl_et_indisponible(client, deux_clients, monkeypatch):
    cid = deux_clients['cid_a']
    monkeypatch.setattr(network_diag, 'etat_capture', lambda: {'disponible': False, 'motif': 'scapy_absent'})
    login_session(client, deux_clients['proprio'], cid)
    r = client.post('/api/baie/activite/capture')
    assert r.status_code == 409 and r.get_json()['motif'] == 'scapy_absent'


def test_route_capture_ecriture_requise(client, deux_clients, monkeypatch):
    """Lancer une capture consomme le réseau/système hôte -> can_write requis,
    comme /api/diag-reseau/snapshot (pas juste un accès en lecture)."""
    cid = deux_clients['cid_a']
    monkeypatch.setattr(network_diag, 'etat_capture', lambda: {'disponible': True, 'motif': 'ok'})
    login_session(client, deux_clients['lecteur'], cid)
    assert client.post('/api/baie/activite/capture').status_code == 403


def test_switchs_baie_repli_type_equipement(conn, deux_clients, make_appareil):
    """Un slot étiqueté « Switch » dans la baie, lié à un appareil qui a une IP
    mais n'est PAS typé Switch, doit quand même être interrogé (le seul vrai
    prérequis SNMP est l'adresse IP)."""
    cid = deux_clients['cid_a']
    aid = make_appareil(cid, nom_machine='SW-X', type_appareil='Autre', adresse_ip='10.0.0.7')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id, type_equipement) "
                 "VALUES (?,1,?,'Switch')", (cid, aid))
    conn.commit()
    ips = [s['ip'] for s in network_diag._switchs_baie(conn, cid)]
    assert '10.0.0.7' in ips


def test_cycle_activite_motif_sans_switch(conn, deux_clients):
    """Aucun switch de baie -> motif 'aucun_switch', calibre False, pas d'erreur."""
    cid = deux_clients['cid_b']
    network_diag._cycle_activite([cid])
    with network_diag._activite_lock:
        res = network_diag._activite_resultat.get(cid)
    assert res['actif'] is True and res['motif'] == 'aucun_switch'
    assert res['nb_switchs'] == 0 and res['calibre'] is False
