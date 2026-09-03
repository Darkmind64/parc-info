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
    # 3 paquets/s : doit clignoter (correctif v2.19.8 — avant, seul le % de bande
    # passante comptait, un port bureautique normal restait "idle").
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

    # compteur d'octets pégé : pas de débit (bps), mais le pps PLAUSIBLE est gardé
    pegge = dict(oper=1, speed_mbps=1000, in_oct=0, out_oct=0, in_pkts=0, out_pkts=0,
                 in_err=0, out_err=0, cpt_pegge=True)
    lp = network_diag._etat_led(prev, pegge, 14.0, seuils)
    assert lp['pps'] <= 12.0 and lp['bps'] <= 4_000.0

    # compteur d'octets pégé ET pps délirant (les compteurs de paquets mentent
    # aussi) -> on n'y croit pas
    pegge_faux = dict(oper=1, speed_mbps=1000, in_oct=0, out_oct=0,
                      in_pkts=10_000_000, out_pkts=0, in_npkts=0, out_npkts=0,
                      in_err=0, out_err=0, cpt_pegge=True)
    lpf = network_diag._etat_led(dict(prev, bps_ema=0.0, pps_ema=0.0), pegge_faux, 14.0, seuils)
    assert lpf['pps'] == 0.0

    # lien inconnu : plafond absolu
    sans_vitesse = dict(oper=1, speed_mbps=0, in_oct=10**12, out_oct=0,
                        in_pkts=5_000_000_000, out_pkts=0, in_err=0, out_err=0)
    ls = network_diag._etat_led(dict(prev, bps_ema=0.0, pps_ema=0.0), sans_vitesse, 14.0, seuils)
    assert ls['pps'] <= network_diag._ACTIVITE_PPS_MAX_PORT


def test_etat_led_bouclage_32bits():
    """Un compteur d'octets 32 bits qui boucle (prev proche de 2^32) doit donner
    le VRAI delta, pas être pris pour un reboot (sous-estimation du débit)."""
    seuils = {'err': 20, 'sat_pct': 90, 'pps_mini': 15}
    # prev = 2^32 - 1 Go, cur = 2 Go, dt 10 s : 3 Go passés (1 avant bouclage, 2 après)
    prev = dict(in_oct=2**32 - 1_000_000_000, out_oct=0, in_pkts=0, out_pkts=0,
                in_err=0, out_err=0, ts=0.0, bps_ema=0.0, pps_ema=0.0)
    cur = dict(oper=1, speed_mbps=10000, in_oct=2_000_000_000, out_oct=0,
               in_pkts=0, out_pkts=0, in_err=0, out_err=0, hc=False)   # ifTable = Counter32
    led = network_diag._etat_led(prev, cur, 10.0, seuils)
    # 3 Go / 10 s * 8 = 2,4 Gb/s instantané ; EMA (1er échantillon) -> ~1,2 Gb/s.
    # Si le bouclage était pris pour un reset (delta = cur = 2 Go), on aurait ~0,8 Gb/s.
    assert led['bps'] > 1_000_000_000 and led['reset'] is False

    # vrai redémarrage : prev petit, cur repart de ~0
    prev2 = dict(prev, in_oct=5_000_000)
    led2 = network_diag._etat_led(prev2, dict(cur, in_oct=1_000), 10.0, seuils)
    assert led2['reset'] is True and led2['bps'] >= 0

    # switch redémarré (sysUpTime a reculé) : reboot=True -> aucun débit
    led3 = network_diag._etat_led(prev, dict(cur, in_oct=99), 10.0, seuils, reboot=True)
    assert led3['bps'] == 0 and led3['reset'] is True


def test_etat_led_antirebond_asymetrique():
    """On PASSE en « traffic » dès le 1er relevé (_ACTIVITE_DEBOUNCE_ON = 1,
    comme une vraie LED) mais on en SORT après _ACTIVITE_DEBOUNCE relevés calmes
    (persistance visuelle, pas de scintillement)."""
    assert network_diag._ACTIVITE_DEBOUNCE == 2 and network_diag._ACTIVITE_DEBOUNCE_ON == 1
    seuils = {'err': 20, 'sat_pct': 90, 'pps_mini': 15, 'bps_mini': 500}
    p0 = dict(in_oct=0, out_oct=0, in_pkts=0, out_pkts=0, in_npkts=0, out_npkts=0,
              in_err=0, out_err=0, ts=0.0, etat='idle', bps_ema=0.0, pps_ema=0.0)
    traf = dict(oper=1, speed_mbps=1000, in_oct=0, out_oct=0,
                in_pkts=100, out_pkts=100, in_npkts=0, out_npkts=0, in_err=0, out_err=0)

    # 1er cycle avec du trafic : « traffic » tout de suite
    l1 = network_diag._etat_led(p0, traf, 1.0, seuils)
    assert l1['etat'] == 'traffic'

    # trafic qui s'arrête : un seul cycle calme ne coupe pas la LED
    p2 = dict(p0, etat='traffic', bps_ema=l1['bps_ema'], pps_ema=0.0,
              in_pkts=200, out_pkts=200)
    calme = dict(oper=1, speed_mbps=1000, in_oct=0, out_oct=0,
                 in_pkts=200, out_pkts=200, in_npkts=0, out_npkts=0, in_err=0, out_err=0)
    l2 = network_diag._etat_led(p2, calme, 1.0, seuils)
    assert l2['etat'] == 'traffic' and l2['etat_pending'] == 'idle'
    p3 = dict(p2, etat=l2['etat'], etat_pending='idle', etat_pending_n=l2['etat_pending_n'],
              pps_ema=l2['pps_ema'])
    l3 = network_diag._etat_led(p3, calme, 1.0, seuils)
    assert l3['etat'] == 'idle'

    # port de VLAN calme : QUE du broadcast/multicast -> compte quand même
    bm = dict(oper=1, speed_mbps=1000, in_oct=0, out_oct=0, in_pkts=0, out_pkts=0,
              in_npkts=40, out_npkts=0, in_err=0, out_err=0)          # 40 non-unicast/s
    lbm = network_diag._etat_led(dict(p0, etat='idle'), bm, 1.0, seuils)
    assert lbm['etat'] == 'traffic'

    # un défaut (err/sature/down) reste prioritaire
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

    m, src, cal, _div = network_diag._mapping_baie_ifindex(conn, cid, slot_id, sw, infos)
    assert m == {12: 37, 7: 5} and cal is True           # via le NOM d'interface
    assert src[12] == 'nom_port' and src[7] == 'nom_port'

    # topologie : PC vu sur ifIndex 99 -> l'emporte sur le nom
    conn.execute("INSERT INTO diag_topologie (client_id, equipement_ip, equipement_appareil_id, "
                 "port_index, appareil_vu_id, horodatage) VALUES (?,?,?,?,?,'x')",
                 (cid, '10.0.0.2', sw, 99, pc))
    conn.commit()
    m2, src2, _, _ = network_diag._mapping_baie_ifindex(conn, cid, slot_id, sw, infos)
    assert m2[12] == 99 and src2[12] == 'topologie'

    # calibration manuelle : l'emporte sur tout
    conn.execute("UPDATE baie_slot_ports SET if_index=42 WHERE slot_id=? AND numero=12", (slot_id,))
    conn.commit()
    m3, src3, _, _ = network_diag._mapping_baie_ifindex(conn, cid, slot_id, sw, infos)
    assert m3[12] == 42 and src3[12] == 'manuel'

    # repli naïf désactivé par défaut : un port sans topo/nom/manuel n'est PAS mappé
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,3)", (slot_id,))
    conn.commit()
    m4, _, _, _ = network_diag._mapping_baie_ifindex(
        conn, cid, slot_id, sw, {3: {'nom': 'bizarre', 'alias': '', 'ethernet': True}})
    assert 3 not in m4


def test_mapping_sfp_pas_de_nom_port(conn, deux_clients, make_appareil):
    """Un port de baie SFP (numéro 1001+) ne doit PAS être mappé par nom
    d'interface : son numéro logique (1) collisionnerait avec le port RJ 1."""
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.2')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,1)", (slot_id,))      # RJ 1
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,1001)", (slot_id,))   # SFP 1
    conn.commit()
    infos = {1:  {'nom': 'Gi1/0/1',  'alias': '', 'ethernet': True},
             49: {'nom': 'Te1/1/1',  'alias': '', 'ethernet': True}}
    m, src, _, _ = network_diag._mapping_baie_ifindex(conn, cid, slot_id, sw, infos)
    assert m.get(1) == 1 and src.get(1) == 'nom_port'
    assert 1001 not in m                              # SFP non mappé par nom


def test_mapping_divergence(conn, deux_clients, make_appareil):
    """topologie et nom d'interface désaccordés sur un port -> signalé."""
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.2')
    pc = make_appareil(cid, nom_machine='PC', adresse_ip='10.0.0.50')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero, appareil_id) VALUES (?,3,?)", (slot_id, pc))
    conn.execute("INSERT INTO diag_topologie (client_id, equipement_ip, equipement_appareil_id, "
                 "port_index, appareil_vu_id, horodatage) VALUES (?,?,?,?,?,'x')",
                 (cid, '10.0.0.2', sw, 99, pc))       # topo dit ifIndex 99
    conn.commit()
    infos = {5: {'nom': 'Gi1/0/3', 'alias': '', 'ethernet': True}}   # nom dit ifIndex 5
    m, src, _, div = network_diag._mapping_baie_ifindex(conn, cid, slot_id, sw, infos)
    assert m[3] == 99 and src[3] == 'topologie' and div == [3]


def test_calibrer_decalage_baie(conn, deux_clients, make_appareil):
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.2')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    for n in (1, 2, 3, 1001):
        conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,?)", (slot_id, n))
    conn.commit()
    assert network_diag.calibrer_decalage_baie(cid, slot_id, 10) == 3      # RJ seulement
    from database import get_db
    c = get_db()
    rows = dict(c.execute("SELECT numero, if_index FROM baie_slot_ports WHERE slot_id=?", (slot_id,)).fetchall())
    c.close()
    assert rows[1] == 11 and rows[2] == 12 and rows[3] == 13 and rows[1001] is None
    assert network_diag.calibrer_decalage_baie(deux_clients['cid_b'], slot_id, 5) == 0   # autre client


def test_mapping_repli_naif_optin(conn, deux_clients, make_appareil, monkeypatch):
    monkeypatch.setattr(network_diag, '_cfg',
                        lambda k, d=None: '1' if k == 'diag_baie_activite_repli_naif' else d)
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.9')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,5)", (slot_id,))
    conn.commit()
    m, src, cal, _ = network_diag._mapping_baie_ifindex(
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

    _TABLE[network_diag._OID_SYS_UPTIME] = {'0': 123456}
    monkeypatch.setattr(A, '_snmp_bulk_cols',
                        lambda ip, bases, comm, **k: {b: dict(_TABLE.get(b, {})) for b in bases})
    res, ok, hc, sut = network_diag._poll_switch_ports('10.0.0.2', ['public'], _infos)
    assert ok is True and hc is True and sut == 123456    # sysUpTime lu dans le même GETBULK
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
    res2, ok2, hc2, _sut2 = network_diag._poll_switch_ports('10.0.0.5', ['public'], {})   # 2e -> 32 bits
    assert res2[1]['in_oct'] == 0 and res2[1]['out_oct'] == 12345 and hc2 is False
    assert res2[1]['cpt_pegge'] is True

    for d in (network_diag._activite_hc, network_diag._activite_capa_neg,
              network_diag._activite_capa_reprobe):
        d.pop('10.0.0.6', None)
    monkeypatch.setattr(A, '_snmp_bulk_cols', lambda *a, **k: {})
    res3, ok3, _, _ = network_diag._poll_switch_ports('10.0.0.6', ['public'], {})
    assert ok3 is False and res3 == {}

    # réponse partielle : les octets répondent mais PAS ifOperStatus pour if 2
    # -> on ne fabrique pas un « up » (oper=None, oper_ok=False)
    for d in (network_diag._activite_hc, network_diag._activite_capa_neg):
        d['10.0.0.7'] = d.get('10.0.0.7')
    network_diag._activite_hc['10.0.0.7'] = True
    _T3 = {network_diag._OID_IF_OPER: {'1': 1},          # if 2 absent
           network_diag._OID_IF_HCIN: {'1': 100, '2': 200},
           network_diag._OID_IF_HCOUT: {'1': 0, '2': 0}}
    monkeypatch.setattr(A, '_snmp_bulk_cols',
                        lambda ip, bases, comm, **k: {b: dict(_T3.get(b, {})) for b in bases})
    r4, _, _, _ = network_diag._poll_switch_ports('10.0.0.7', ['public'], {})
    assert r4[1]['oper'] == 1 and r4[1]['oper_ok'] is True
    assert r4[2]['oper'] is None and r4[2]['oper_ok'] is False


def test_api_baie_activite_route_sans_snmp_synchrone(client, conn, deux_clients, make_appareil, monkeypatch):
    cid = deux_clients['cid_a']
    monkeypatch.setattr(network_diag, '_poll_switch_ports',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('SNMP synchrone dans le handler')))
    login_session(client, deux_clients['lecteur'], cid)          # lecture seule suffit
    r = client.get('/api/baie/activite')
    assert r.status_code == 200 and 'actif' in r.get_json()


def _mock_snmp_switch(monkeypatch, ports, fdb=None):
    """ports = {ifindex: dict(oper,speed_mbps,in_oct,out_oct,in_pkts,out_pkts,in_err,out_err)}
    fdb   = {ifindex: set(mac)} appris (FDB live) — vide par défaut."""
    monkeypatch.setattr(network_diag, '_noms_interfaces',
                        lambda ip, c: {i: {'nom': f'Gi0/{i}', 'alias': '', 'ethernet': True,
                                           'speed_mbps': ports[i].get('speed_mbps', 0)} for i in ports})
    monkeypatch.setattr(network_diag, '_poll_switch_ports',
                        lambda ip, c, infos=None: (dict(ports), bool(ports), True, None))
    monkeypatch.setattr(network_diag, '_fdb_switch', lambda ip, c: dict(fdb or {}))


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


def test_prises_murales_activite(conn, deux_clients, make_appareil):
    """LED d'une prise murale via le port de switch du cordon de brassage, +
    contrôle de câblage : la MAC déclarée sur la prise doit être apprise (FDB
    live) sur le port ; sinon repli sur la topologie L2."""
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.2')
    pc = make_appareil(cid, nom_machine='PC-COMPTA', adresse_mac='AA:BB:CC:00:00:01')
    autre = make_appareil(cid, nom_machine='PC-ACCUEIL', adresse_mac='AA:BB:CC:00:00:02')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    sw_slot = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slots (client_id, position, type_equipement) VALUES (?,2,'Bandeau RJ')", (cid,))
    b_slot = conn.execute("SELECT id FROM baie_slots WHERE type_equipement='Bandeau RJ' AND client_id=?",
                          (cid,)).fetchone()[0]
    conn.execute("INSERT INTO baie_prises_murales (slot_id, numero, appareil_id) VALUES (?,5,?)", (b_slot, pc))
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero, lie_slot_id, lie_port_numero) "
                 "VALUES (?,5,?,8)", (b_slot, sw_slot))
    conn.commit()

    etats_par_ip = {'10.0.0.2': {42: {'etat': 'traffic', 'blink_ms': 300, 'bps': 1e6, 'pps': 50.0}}}
    ip_par_slot = {sw_slot: '10.0.0.2'}
    mapping_par_slot = {sw_slot: {8: 42}}          # port de switch 8 -> ifIndex 42
    noms_par_ip = {'10.0.0.2': {42: {'nom': 'Gi1/0/8'}}}
    inv_mac = {'aa:bb:cc:00:00:01': (pc, 'PC-COMPTA'), 'aa:bb:cc:00:00:02': (autre, 'PC-ACCUEIL')}
    prec = {}
    _f = network_diag._prises_murales_activite

    # FDB : la bonne MAC est apprise sur l'ifIndex 42 -> câblage confirmé
    pu, jo = _f(conn, cid, ip_par_slot, etats_par_ip, mapping_par_slot, noms_par_ip,
                {}, {'10.0.0.2': {42: {'aa:bb:cc:00:00:01'}}}, inv_mac, lambda s: prec)
    assert len(pu) == 1 and pu[0]['prise_murale'] is True and pu[0]['numero'] == 5
    assert pu[0]['etat'] == 'traffic' and pu[0]['cable'] == 'ok' and pu[0]['cible'] == 'PC-COMPTA'
    assert jo == []

    # FDB : le port apprend une AUTRE MAC -> incohérent, appareil vu nommé dans le journal
    prec.clear()
    pu2, jo2 = _f(conn, cid, ip_par_slot, etats_par_ip, mapping_par_slot, noms_par_ip,
                  {}, {'10.0.0.2': {42: {'aa:bb:cc:00:00:02'}}}, inv_mac, lambda s: prec)
    assert pu2[0]['cable'] == 'incoherent' and pu2[0]['voisins'] == ['PC-ACCUEIL']
    assert any('incohérent' in m[0] and 'PC-ACCUEIL' in m[0] for m in jo2)

    # pas de FDB : repli sur la topologie L2 (appareil_id)
    prec.clear()
    pu3, jo3 = _f(conn, cid, ip_par_slot, etats_par_ip, mapping_par_slot, noms_par_ip,
                  {'10.0.0.2': {42: {autre}}}, {}, inv_mac, lambda s: prec)
    assert pu3[0]['cable'] == 'incoherent'

    # ni FDB ni topologie -> inconnu, pas d'alerte
    prec.clear()
    pu4, jo4 = _f(conn, cid, ip_par_slot, etats_par_ip, mapping_par_slot, noms_par_ip,
                  {}, {}, inv_mac, lambda s: prec)
    assert pu4[0]['cable'] == 'inconnu' and jo4 == []


def test_fdb_switch_et_voisins(monkeypatch):
    """_fdb_switch agrège la FDB bridge-MIB en {ifIndex: set(mac)} ; _voisins_port
    résout les MAC en noms d'inventaire / fabricant."""
    network_diag._activite_fdb.clear()
    walks = {
        network_diag._OID_FDB_BASEPORT_IF: {'1': '10', '2': '20'},   # bridge port -> ifIndex
        network_diag._OID_FDB_DOT1Q_PORT: {'1.170.187.204.0.0.1': '1',
                                           '1.170.187.204.0.0.2': '1',
                                           '1.170.187.204.0.0.9': '2'},
        network_diag._OID_FDB_DOT1D_PORT: {},
    }
    monkeypatch.setattr(network_diag, '_snmp_walk', lambda oid, ip, c: walks.get(oid, {}))
    monkeypatch.setattr(network_diag, '_snmp_walk_octets', lambda *a, **k: {})   # table ARP : vide
    fdb = network_diag._fdb_switch('10.0.0.2', ['public'])
    assert fdb[10] == {'aa:bb:cc:00:00:01', 'aa:bb:cc:00:00:02'}
    assert fdb[20] == {'aa:bb:cc:00:00:09'}

    inv = {'aa:bb:cc:00:00:01': (7, 'PC-A')}
    v = network_diag._voisins_port(fdb[10], inv)
    assert 'PC-A' in v['noms'] and v['n'] == 2


def test_fdb_corriger():
    """Détection multi-hypothèses de la déformation d'une table MAC :
    - ProCurve : `00:01` + 4 premiers octets → hypothèse 'prefixe2', réparée
    - FDB normale : hypothèse 'exact', inchangée
    - collision de préfixe → écartée ; réglage manuel respecté."""
    _f = network_diag._fdb_corriger
    inv = {'1c:1b:0d:95:99:21': (1, 'PC-A', ''), '00:11:32:43:97:9d': (2, 'NAS', ''),
           '0c:8f:ff:59:db:3b': (3, 'AP', ''), '20:7b:d2:a3:1f:b7': (4, 'MAC', '')}
    tronq = {10: {'00:01:1c:1b:0d:95'}, 11: {'00:01:00:11:32:43', '00:01:0c:8f:ff:59'},
             12: {'00:01:20:7b:d2:a3'}, 13: {'00:01:de:ad:be:ef'}}   # dernière hors inventaire
    rep, meta = _f(tronq, inv)
    assert meta['tronquee'] is True and meta['transform'] == 'prefixe2'
    assert meta['reconnues'] == 4
    assert rep == {10: {'1c:1b:0d:95:99:21'}, 12: {'20:7b:d2:a3:1f:b7'},
                   11: {'00:11:32:43:97:9d', '0c:8f:ff:59:db:3b'}}   # port 13 écarté

    # FDB normale : hypothèse exact, inchangée
    ok = {10: {'1c:1b:0d:95:99:21'}, 11: {'00:11:32:43:97:9d'}, 12: {'aa:bb:cc:dd:ee:ff'},
          13: {'11:22:33:44:55:66'}}
    rep2, meta2 = _f(ok, inv)
    assert meta2['transform'] == 'exact' and meta2['tronquee'] is False and rep2 == ok

    # réglage manuel : 'standard' force la lecture directe même sur une FDB déformée
    rep3, meta3 = _f(tronq, inv, mode='standard')
    assert meta3['transform'] == 'exact' and rep3 == tronq
    # 'ignorer' → FDB vide
    rep4, meta4 = _f(tronq, inv, mode='ignorer')
    assert rep4 == {} and meta4['transform'] == 'ignore'

    # collision de préfixe (2 appareils, mêmes 4 premiers octets) → écartée
    inv2 = {**inv, '00:11:32:43:97:9e': (5, 'NAS-2', '')}
    rep5, meta5 = _f({10: {'00:01:00:11:32:43'}, 11: {'00:01:00:11:32:43'},
                      12: {'00:01:1c:1b:0d:95'}, 13: {'00:01:0c:8f:ff:59'},
                      14: {'00:01:20:7b:d2:a3'}}, inv2)
    assert meta5['tronquee'] is True
    assert rep5 == {12: {'1c:1b:0d:95:99:21'}, 13: {'0c:8f:ff:59:db:3b'},
                    14: {'20:7b:d2:a3:1f:b7'}}   # ports 10/11 ambigus, écartés
    # #19 : les MAC ambiguës ne sont plus jetées en silence, meta['ambigus']
    # liste les ports candidats.
    assert meta5['ambigus'] == {'00:11:32:43:97:9d': [10, 11]}


def test_bits_capacites_lldp_cdp():
    """Décodage des capacités système : BITS LLDP (bit 0 = MSB octet 0) et
    bitmap CDP (bit 0 = router, LSB)."""
    _b = network_diag._bits_actifs
    L = network_diag._LLDP_CAP_BITS
    # bridge(2) + wlan(3) : octet 0 = 0b00110000 = 0x30
    assert _b(b'\x30', L) == {'bridge', 'wlan'}
    # router(4) : 0b00001000 = 0x08
    assert _b(b'\x08', L) == {'router'}
    assert _b(b'', L) == set() and _b(None, L) == set()
    # CDP : router(bit0) + switch(bit3) -> 0b1001 = 9
    assert network_diag._cdp_bits((9).to_bytes(4, 'big')) == {'router', 'bridge'}
    assert network_diag._cdp_bits((0x80).to_bytes(4, 'big')) == {'phone'}


def test_classer_cascade():
    _f = network_diag._classer_cascade
    # 2 MAC filaires classiques, aucune aléatoire → switch non géré
    assert _f({'00:11:22:33:44:55', '00:aa:bb:cc:dd:ee'}, {})['type'] == 'switch'
    # une MAC localement administrée (aléatoire) → client Wi-Fi
    assert _f({'02:11:22:33:44:55', '00:aa:bb:cc:dd:ee'}, {})['type'] == 'wifi'
    # un appareil inventorié « Borne WiFi » vu sur le port → Wi-Fi certain
    r = _f({'00:11:22:33:44:55', '00:aa:bb:cc:dd:ee'},
           {'00:11:22:33:44:55': (9, 'AP-1', 'Borne WiFi')})
    assert r['type'] == 'wifi' and r['n_macs'] == 2
    # capacité LLDP du voisin : 'wlan' -> Wi-Fi certain ; 'bridge' -> switch
    assert _f({'11:11:11:11:11:11', '22:22:22:22:22:22'}, {}, caps='wlan')['type'] == 'wifi'
    assert _f({'11:11:11:11:11:11', '22:22:22:22:22:22'}, {}, caps='bridge')['type'] == 'switch'


def test_analyser_brassage_baie(conn, deux_clients, make_appareil, monkeypatch):
    """Carte réseau proposée à partir de toutes les MAC vues sur les switchs :
    machine sur une prise brassée, machine directe sur un port non brassé,
    cordon déduit d'une machine de prise, cascade, hors inventaire."""
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.9')
    srv = make_appareil(cid, nom_machine='SRV-1', adresse_mac='AA:00:00:00:00:01')   # direct sur port
    pcP = make_appareil(cid, nom_machine='PC-PRISE', adresse_mac='AA:00:00:00:00:02')  # au bout d'un cordon
    pcC = make_appareil(cid, nom_machine='PC-CORDON', adresse_mac='AA:00:00:00:00:03')  # machine de prise, cordon à créer
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    sw_slot = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    for n in (2, 3, 4, 5, 6):
        conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,?)", (sw_slot, n))
    conn.execute("INSERT INTO baie_slots (client_id, position, type_equipement) VALUES (?,2,'Bandeau RJ')", (cid,))
    b_slot = conn.execute("SELECT id FROM baie_slots WHERE type_equipement='Bandeau RJ' AND client_id=?",
                          (cid,)).fetchone()[0]
    for n in (1, 2, 3):
        conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,?)", (b_slot, n))
        conn.execute("INSERT INTO baie_prises_murales (slot_id, numero) VALUES (?,?)", (b_slot, n))
    # prise 1 brassée -> switch port 3 (on y verra PC-PRISE) ; prise 2 déclare PC-CORDON (cordon à créer)
    conn.execute("UPDATE baie_slot_ports SET lie_slot_id=?, lie_port_numero=3 WHERE slot_id=? AND numero=1", (sw_slot, b_slot))
    conn.execute("UPDATE baie_slot_ports SET lie_slot_id=?, lie_port_numero=1 WHERE slot_id=? AND numero=3", (b_slot, sw_slot))
    conn.execute("UPDATE baie_prises_murales SET appareil_id=? WHERE slot_id=? AND numero=2", (pcC, b_slot))
    conn.commit()

    monkeypatch.setattr(network_diag, '_cfg', lambda k, d=None: '1' if k == 'diag_snmp_actif' else d)
    monkeypatch.setattr(network_diag, '_communautes_snmp', lambda: ['public'])
    monkeypatch.setattr(network_diag, '_macs_infra_switch', lambda ip, c: set())
    monkeypatch.setattr(network_diag, '_noms_interfaces',
        lambda ip, c: {i * 11: {'nom': f'Gi0/{i}', 'alias': '', 'ethernet': True} for i in (2, 3, 4, 5, 6)})
    monkeypatch.setattr(network_diag, '_fdb_switch', lambda ip, c: {
        22: {'aa:00:00:00:00:01'},                                  # port 2 : SRV-1 direct
        33: {'aa:00:00:00:00:02'},                                  # port 3 : PC-PRISE (au bout du cordon de la prise 1)
        44: {'aa:00:00:00:00:03'},                                  # port 4 : PC-CORDON -> cordon à créer vers prise 2
        55: {'de:ad:be:ef:00:01'},                                  # port 5 : un appareil hors inventaire
        66: {'00:11:22:33:44:55', '02:99:88:77:66:55'},             # port 6 : cascade (une MAC aléatoire)
    })

    d = network_diag.analyser_brassage_baie(cid)
    assert d['ok'] is True
    assert [(p['prise_numero'], p['machine_nom']) for p in d['prises_appareils']] == [(1, 'PC-PRISE')]
    assert [(p['switch_port_numero'], p['machine_nom']) for p in d['ports_appareils']] == [(2, 'SRV-1')]
    assert [(p['prise_numero'], p['switch_port_numero']) for p in d['cordons']] == [(2, 4)]
    assert [c['switch_port_numero'] for c in d['cascades']] == [6] and d['cascades'][0]['type'] == 'wifi'
    assert [x['switch_port_numero'] for x in d['hors_inventaire']] == [5]
    assert d['retypage'] == []


def test_analyser_brassage_mac_secondaire(conn, deux_clients, make_appareil, monkeypatch):
    """Un appareil est reconnu sur un port de switch via une MAC déclarée dans
    appareil_macs (2e carte), pas seulement via appareils.adresse_mac."""
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.9')
    srv = make_appareil(cid, nom_machine='SRV-BI-NIC', adresse_mac='AA:00:00:00:00:01')
    conn.execute("INSERT INTO appareil_macs (appareil_id, client_id, adresse_mac, source, date_maj) "
                 "VALUES (?,?,?, 'collecteur', '')", (srv, cid, 'bb:00:00:00:00:02'))
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    sw_slot = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    for n in (2, 3):
        conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,?)", (sw_slot, n))
    conn.commit()

    monkeypatch.setattr(network_diag, '_cfg', lambda k, d=None: '1' if k == 'diag_snmp_actif' else d)
    monkeypatch.setattr(network_diag, '_communautes_snmp', lambda: ['public'])
    monkeypatch.setattr(network_diag, '_macs_infra_switch', lambda ip, c: set())
    monkeypatch.setattr(network_diag, '_noms_interfaces',
        lambda ip, c: {i * 11: {'nom': f'Gi0/{i}', 'alias': '', 'ethernet': True} for i in (2, 3)})
    # la 2e carte (bb:...) est vue sur le port 3, pas la MAC principale
    monkeypatch.setattr(network_diag, '_fdb_switch', lambda ip, c: {33: {'bb:00:00:00:00:02'}})

    d = network_diag.analyser_brassage_baie(cid)
    assert d['ok'] is True
    assert [(p['switch_port_numero'], p['machine_nom']) for p in d['ports_appareils']] == [(3, 'SRV-BI-NIC')]
    assert d['hors_inventaire'] == []


def test_analyser_brassage_element_baie_sur_port_switch(conn, deux_clients, make_appareil, monkeypatch):
    """Un élément de baie SANS FDB (NAS, serveur…) positionné dans le rack et
    vu sur un port de switch était perdu : la branche « lien switch ⇄ élément »
    ne trouvait pas le port du voisin et s'arrêtait là. Il doit maintenant
    être proposé (affectation directe au port si plusieurs ports, lien si un
    seul port)."""
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.9')
    nas = make_appareil(cid, nom_machine='DS415', type_appareil='NAS', adresse_mac='00:11:32:43:97:9d')
    imp = make_appareil(cid, nom_machine='IMPRIMANTE', type_appareil='Imprimante',
                        adresse_mac='00:21:b7:39:4e:07')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    sw_slot = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    for n in (2, 3, 8, 9):
        conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,?)", (sw_slot, n))
    # le NAS est un élément de baie avec 2 ports -> affectation directe au port
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,2,?)", (cid, nas))
    nas_slot = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (nas,)).fetchone()[0]
    for n in (1, 2):
        conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,?)", (nas_slot, n))
    # l'imprimante est un élément de baie avec 1 seul port -> lien
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,3,?)", (cid, imp))
    imp_slot = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (imp,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,1)", (imp_slot,))
    conn.commit()

    monkeypatch.setattr(network_diag, '_cfg', lambda k, d=None: '1' if k == 'diag_snmp_actif' else d)
    monkeypatch.setattr(network_diag, '_communautes_snmp', lambda: ['public'])
    monkeypatch.setattr(network_diag, '_macs_infra_switch', lambda ip, c: set())
    monkeypatch.setattr(network_diag, '_noms_interfaces',
        lambda ip, c: {i * 11: {'nom': str(i), 'alias': '', 'ethernet': True} for i in (2, 3, 8, 9)})
    monkeypatch.setattr(network_diag, '_fdb_switch', lambda ip, c: {
        88: {'00:11:32:43:97:9d'},   # port 8 : DS415 (élément de baie, 2 ports)
        99: {'00:21:b7:39:4e:07'},   # port 9 : imprimante (élément de baie, 1 port)
    })

    d = network_diag.analyser_brassage_baie(cid)
    assert d['ok'] is True
    assert [(p['switch_port_numero'], p['machine_nom']) for p in d['ports_appareils']] == [(8, 'DS415')]
    assert [(l['a_port'], l['b_nom'], l['b_port'], l['via']) for l in d['liens_baie']] \
        == [(9, 'IMPRIMANTE', 1, 'port_unique')]
    assert d['hors_inventaire'] == []


def test_analyser_brassage_retypage_lldp(conn, deux_clients, make_appareil, monkeypatch):
    """Le voisin LLDP se déclare 'wlan' mais l'appareil correspondant est
    typé 'Switch' en inventaire -> proposition de retypage (indicative)."""
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.9')
    ap = make_appareil(cid, nom_machine='AP-1', type_appareil='Switch',
                       adresse_mac='AA:00:00:00:00:AA')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    sw_slot = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    for n in (2, 3):
        conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,?)", (sw_slot, n))
    conn.execute("INSERT INTO diag_topologie (client_id, equipement_appareil_id, port_index, "
                 "voisin_nom, voisin_mac, voisin_caps, voisin_port, voisin_port_subtype) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (cid, sw, 22, 'AP-1', 'aa:00:00:00:00:aa', 'bridge,wlan', '1', 'local'))
    conn.commit()

    monkeypatch.setattr(network_diag, '_cfg', lambda k, d=None: '1' if k == 'diag_snmp_actif' else d)
    monkeypatch.setattr(network_diag, '_communautes_snmp', lambda: ['public'])
    monkeypatch.setattr(network_diag, '_macs_infra_switch', lambda ip, c: set())
    monkeypatch.setattr(network_diag, '_noms_interfaces',
        lambda ip, c: {i * 11: {'nom': f'Gi0/{i}', 'alias': '', 'ethernet': True} for i in (2, 3)})
    monkeypatch.setattr(network_diag, '_fdb_switch', lambda ip, c: {22: {'aa:00:00:00:00:aa'}})

    d = network_diag.analyser_brassage_baie(cid)
    assert d['ok'] is True
    assert len(d['retypage']) == 1
    r = d['retypage'][0]
    assert r['machine_nom'] == 'AP-1' and r['type_actuel'] == 'Switch'
    assert r['type_propose'] == 'Borne Wi-Fi'


def test_route_brassage_proposer_poll(client, deux_clients, monkeypatch):
    """La route /api/baie/brassage/proposer répond tout de suite (en_cours),
    puis rend le résultat quand la tâche de fond a fini."""
    import time
    monkeypatch.setattr(network_diag, 'analyser_brassage_baie',
                        lambda cid, _progress=None, budget_s=0: {'ok': True, 'prises_appareils': []})
    with network_diag._brassage_lock:
        network_diag._brassage_status.update(
            {'running': False, 'progress': 0, 'resultat': None, 'client_id': None, 'ts': 0.0})
    login_session(client, deux_clients['proprio'], deux_clients['cid_a'])
    r = client.get('/api/baie/brassage/proposer')
    assert r.status_code == 200 and r.get_json().get('en_cours') is True
    for _ in range(60):
        body = client.get('/api/baie/brassage/proposer').get_json()
        if not body.get('en_cours'):
            break
        time.sleep(0.05)
    assert body.get('ok') is True


def test_analyse_brassage_tache_de_fond(monkeypatch):
    """lancer/statut_analyse_brassage : relevé en thread, résultat rendu une
    fois prêt et seulement au bon client, pas de 2e relevé concurrent."""
    import time
    calls = []

    def _fake(cid, _progress=None, budget_s=0):
        if _progress:
            _progress(50, 'moitié')
        calls.append(cid)
        return {'ok': True, 'x': cid}

    monkeypatch.setattr(network_diag, 'analyser_brassage_baie', _fake)
    with network_diag._brassage_lock:
        network_diag._brassage_status.update(
            {'running': False, 'progress': 0, 'resultat': None, 'client_id': None, 'ts': 0.0})

    assert network_diag.lancer_analyse_brassage(42) is True
    st = None
    for _ in range(60):
        st = network_diag.statut_analyse_brassage(42)
        if st['resultat'] is not None:
            break
        time.sleep(0.05)
    assert st['resultat'] == {'ok': True, 'x': 42}
    assert network_diag.statut_analyse_brassage(99)['resultat'] is None   # pas de fuite
    assert calls == [42]


def test_analyser_brassage_budget(conn, deux_clients, make_appareil, monkeypatch):
    """budget_s dépassé -> relevé partiel + switchs_non_releves."""
    import time
    cid = deux_clients['cid_a']
    for i, ip in enumerate(('10.0.0.1', '10.0.0.2'), start=1):
        sw = make_appareil(cid, nom_machine=f'SW{i}', type_appareil='Switch', adresse_ip=ip)
        conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,?,?)", (cid, i, sw))
    conn.commit()
    monkeypatch.setattr(network_diag, '_cfg', lambda k, d=None: '1' if k == 'diag_snmp_actif' else d)
    monkeypatch.setattr(network_diag, '_communautes_snmp', lambda: ['public'])
    monkeypatch.setattr(network_diag, '_macs_infra_switch', lambda ip, c: set())

    def _lent(ip, c):
        time.sleep(0.15)
        return {11: {'nom': 'Gi0/1', 'alias': '', 'ethernet': True}}

    monkeypatch.setattr(network_diag, '_noms_interfaces', _lent)
    monkeypatch.setattr(network_diag, '_fdb_switch', lambda ip, c: {11: {'aa:bb:cc:dd:ee:ff'}})
    import app as _app
    monkeypatch.setattr(_app, '_snmp_presence', lambda *a, **k: (False, False, ''))

    d = network_diag.analyser_brassage_baie(cid, budget_s=0.05)
    assert d['ok'] is True
    assert len(d['switchs_non_releves']) == 1   # 1 relevé, 1 sauté


def test_analyser_brassage_snmp_refuse(conn, deux_clients, make_appareil, monkeypatch):
    """Un switch qui ne répond à rien mais dont _snmp_presence dit « agent
    présent, non exploitable » -> switchs_snmp_refuse (#7)."""
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='FW-EDGE', type_appareil='Routeur/Pare-feu', adresse_ip='10.0.0.254')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    conn.commit()
    monkeypatch.setattr(network_diag, '_cfg', lambda k, d=None: '1' if k == 'diag_snmp_actif' else d)
    monkeypatch.setattr(network_diag, '_communautes_snmp', lambda: ['public'])
    monkeypatch.setattr(network_diag, '_macs_infra_switch', lambda ip, c: set())
    monkeypatch.setattr(network_diag, '_noms_interfaces', lambda ip, c: {})
    monkeypatch.setattr(network_diag, '_fdb_switch', lambda ip, c: {})
    import app as _app
    monkeypatch.setattr(_app, '_snmp_presence',
                        lambda ip, comm, **k: (True, False, 'SNMPv3 : authentification refusée'))

    d = network_diag.analyser_brassage_baie(cid)
    assert [s['nom'] for s in d['switchs_snmp_refuse']] == ['FW-EDGE']
    assert 'refus' in d['switchs_snmp_refuse'][0]['detail']


def test_analyser_brassage_capture(conn, deux_clients, make_appareil, monkeypatch):
    """Une MAC active vue par la dernière capture passive, absente de
    l'inventaire et d'aucune FDB -> hors_inventaire (via='capture')."""
    cid = deux_clients['cid_a']
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.9')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    sw_slot = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,2)", (sw_slot,))
    conn.commit()
    monkeypatch.setattr(network_diag, '_cfg', lambda k, d=None: '1' if k == 'diag_snmp_actif' else d)
    monkeypatch.setattr(network_diag, '_communautes_snmp', lambda: ['public'])
    monkeypatch.setattr(network_diag, '_macs_infra_switch', lambda ip, c: set())
    monkeypatch.setattr(network_diag, '_noms_interfaces',
                        lambda ip, c: {22: {'nom': 'Gi0/2', 'alias': '', 'ethernet': True}})
    monkeypatch.setattr(network_diag, '_fdb_switch', lambda ip, c: {})
    monkeypatch.setattr(network_diag, 'statut_capture_baie', lambda: {
        'client_id': cid, 'resultat': {'disponible': True, 'talkers': [
            {'mac': 'd4:d4:d4:d4:d4:d4', 'vendor': 'Acme', 'appareil_id': None},
            {'mac': '02:aa:bb:cc:dd:ee', 'vendor': '', 'appareil_id': None},  # locale -> ignorée
        ]}})

    d = network_diag.analyser_brassage_baie(cid)
    assert d['ok'] is True
    hi = [x for x in d['hors_inventaire'] if x.get('via') == 'capture']
    assert [x['mac'] for x in hi] == ['d4:d4:d4:d4:d4:d4']


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

    monkeypatch.setattr(network_diag, '_poll_switch_ports', lambda ip, c, infos=None: ({}, False, False, None))
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
