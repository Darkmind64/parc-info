"""Pré-chauffe de la vue d'activité de la baie : un snapshot persisté amorce le
1er cycle pour que les LEDs s'animent DÈS l'ouverture (avant : 2 cycles requis,
et tout oublié 2 min après avoir quitté /baie)."""
import time

import network_diag as N


def _mock_snmp_switch(monkeypatch, ports):
    monkeypatch.setattr(N, '_noms_interfaces',
                        lambda ip, c: {i: {'nom': f'Gi0/{i}', 'alias': '', 'ethernet': True,
                                           'speed_mbps': ports[i].get('speed_mbps', 0)} for i in ports})
    monkeypatch.setattr(N, '_poll_switch_ports',
                        lambda ip, c, infos=None: (dict(ports), bool(ports), True, None))
    monkeypatch.setattr(N, '_fdb_switch', lambda ip, c: {})


def _isoler_memoire(monkeypatch):
    for nom in ('_activite_prev', '_activite_sut', '_activite_switch_ok',
                '_activite_etat_mappe', '_activite_noms', '_activite_resultat',
                '_activite_detail', '_activite_heartbeat', '_activite_echecs'):
        monkeypatch.setattr(N, nom, {})
    monkeypatch.setattr(N, '_activite_rechauffe', [0])


def test_snapshot_roundtrip(conn, make_client):
    cid = make_client()
    infos = {1: {'nom': 'Gi0/1', 'alias': 'uplink', 'ethernet': True, 'speed_mbps': 1000}}
    cur = {1: dict(in_oct=1000, out_oct=2000, in_pkts=10, out_pkts=20, in_npkts=1,
                   out_npkts=2, in_err=0, out_err=0, oper=1, oper_ok=True,
                   speed_mbps=1000, cpt_pegge=False)}
    N._snapshot_baie_ecrire(cid, '10.9.0.2', infos, cur, {'sysname': 'SW'}, 123456, time.time())
    d = N._snapshot_baie_charger(cid)
    assert '10.9.0.2' in d
    assert d['10.9.0.2']['sut'] == 123456
    assert d['10.9.0.2']['ports'][1]['in_oct'] == 1000
    assert d['10.9.0.2']['interfaces'][1]['alias'] == 'uplink'


def test_amorcage_seed_active_prev(conn, make_client, monkeypatch):
    _isoler_memoire(monkeypatch)
    cid = make_client()
    cur = {5: dict(in_oct=500, out_oct=0, in_pkts=5, out_pkts=0, in_npkts=0, out_npkts=0,
                   in_err=0, out_err=0, oper=1, oper_ok=True, speed_mbps=1000, cpt_pegge=False)}
    N._snapshot_baie_ecrire(cid, '10.9.0.3', {}, cur, {}, 99, time.time() - 10)
    N._amorcer_activite_depuis_snapshot(cid, [{'ip': '10.9.0.3'}])
    assert (cid, '10.9.0.3', 5) in N._activite_prev
    assert N._activite_prev[(cid, '10.9.0.3', 5)]['in_oct'] == 500
    assert N._activite_sut[(cid, '10.9.0.3')][0] == 99


def test_amorcage_ignore_un_snapshot_trop_vieux(conn, make_client, monkeypatch):
    _isoler_memoire(monkeypatch)
    cid = make_client()
    cur = {5: dict(in_oct=500, out_oct=0, in_pkts=5, out_pkts=0, in_npkts=0, out_npkts=0,
                   in_err=0, out_err=0, oper=1, oper_ok=True, speed_mbps=1000, cpt_pegge=False)}
    N._snapshot_baie_ecrire(cid, '10.9.0.4', {}, cur, {}, 99,
                            time.time() - N._SNAPSHOT_BAIE_MAX_AGE - 60)
    N._amorcer_activite_depuis_snapshot(cid, [{'ip': '10.9.0.4'}])
    assert (cid, '10.9.0.4', 5) not in N._activite_prev


def test_led_animee_des_le_premier_cycle_avec_snapshot(conn, make_client, make_appareil, monkeypatch):
    """Le scénario visé : snapshot frais → 1er cycle calcule un débit, LED
    'traffic', pas 'idle'. Sans snapshot, le 1er cycle serait 'idle'."""
    _isoler_memoire(monkeypatch)
    cid = make_client()
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.7')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,1)", (slot_id,))
    conn.commit()

    # snapshot il y a ~5 s : compteurs bas
    prev = {1: dict(in_oct=0, out_oct=0, in_pkts=0, out_pkts=0, in_npkts=0, out_npkts=0,
                    in_err=0, out_err=0, oper=1, oper_ok=True, speed_mbps=1000, cpt_pegge=False)}
    N._snapshot_baie_ecrire(cid, '10.0.0.7', {}, prev, {}, 0, time.time() - 5)

    # relevé courant : +5 Mo entrants en 5 s ≈ 8 Mbit/s → LED 'traffic'
    _mock_snmp_switch(monkeypatch, {1: dict(oper=1, speed_mbps=1000, in_oct=5_000_000,
                                            out_oct=0, in_pkts=4000, out_pkts=0,
                                            in_err=0, out_err=0)})
    N._cycle_activite([cid])
    with N._activite_lock:
        res = N._activite_resultat.get(cid)
    etat = next(p['etat'] for p in res['ports'] if p['numero'] == 1)
    assert etat == 'traffic', f"attendu 'traffic' dès le 1er cycle, obtenu {etat!r}"


def test_prechauffe_no_op_si_snmp_inactif(conn, monkeypatch):
    monkeypatch.setattr(N, '_cfg', lambda k, d=None: '0' if k == 'diag_snmp_actif' else d)
    appels = []
    monkeypatch.setattr(N, '_cycle_activite', lambda cl: appels.append(cl))
    N._prechauffe_last[0] = 0
    N._prechauffe_baie_si_due()
    assert appels == []
