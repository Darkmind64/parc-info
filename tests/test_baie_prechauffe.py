"""Pré-chauffe de la vue d'activité de la baie : un snapshot persisté amorce le
1er cycle pour que les LEDs s'animent DÈS l'ouverture (avant : 2 cycles requis,
et tout oublié 2 min après avoir quitté /baie)."""
import time

import network_diag as N


def _mock_snmp_switch(monkeypatch, ports):
    monkeypatch.setattr(N, '_presence_baie_ok', lambda *a, **k: True)
    monkeypatch.setattr(N, '_noms_interfaces',
                        lambda ip, c: {i: {'nom': f'Gi0/{i}', 'alias': '', 'ethernet': True,
                                           'speed_mbps': ports[i].get('speed_mbps', 0)} for i in ports})
    monkeypatch.setattr(N, '_poll_switch_ports',
                        lambda ip, c, infos=None: (dict(ports), bool(ports), True, None))
    monkeypatch.setattr(N, '_fdb_switch', lambda ip, c: {})


def _isoler_memoire(monkeypatch):
    for nom in ('_activite_prev', '_activite_sut', '_activite_switch_ok',
                '_activite_etat_mappe', '_activite_noms', '_activite_resultat',
                '_activite_detail', '_activite_progres', '_activite_heartbeat',
                '_activite_echecs', '_presence_baie'):
        monkeypatch.setattr(N, nom, {})
    monkeypatch.setattr(N, '_activite_rechauffe', [0])
    monkeypatch.setattr(N, '_ACTIVITE_DOUBLE_ECART', 0.02)   # pas de vraie attente en test
    monkeypatch.setattr(N, '_presence_baie_ok', lambda *a, **k: True)  # pas de vrai SNMP


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


def test_prechauffe_pas_bridee_par_mode_terrain(conn, make_client, monkeypatch):
    """Régression : la pré-chauffe (simple cache de compteurs) ne doit PAS être
    coupée par le mode terrain — sinon elle meurt sur une instance Docker
    (défaut « consultation »)."""
    import config_helpers as C
    _isoler_memoire(monkeypatch)
    monkeypatch.setattr(N, '_cfg', lambda k, d=None: '1' if k in ('diag_snmp_actif', 'diag_baie_prechauffe') else d)
    monkeypatch.setattr(N, '_cfg_int', lambda k, d=0: 0 if k == 'diag_baie_prechauffe_s' else d)
    monkeypatch.setattr(N, '_clients_avec_switch_baie', lambda: [1, 2])
    C.cfg_set('mode_terrain', 'consultation')
    try:
        appels = []
        monkeypatch.setattr(N, '_cycle_activite', lambda cl: appels.append(list(cl)))
        N._prechauffe_last[0] = 0
        N._prechauffe_baie_si_due()
        assert appels == [[1, 2]]
    finally:
        C.cfg_set('mode_terrain', 'auto')


def test_double_releve_a_froid_anime_sans_snapshot(conn, make_client, make_appareil, monkeypatch):
    """Sans aucun snapshot : le 1er cycle à froid fait DEUX relevés rapprochés
    (au lieu d'attendre le cycle suivant) → LED animée dès l'ouverture."""
    _isoler_memoire(monkeypatch)
    cid = make_client()
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.0.0.8')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)", (cid, sw))
    slot_id = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (sw,)).fetchone()[0]
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,1)", (slot_id,))
    conn.commit()

    # 2 relevés successifs : +2 Mo entre les deux → ~... Mbit/s sur l'écart
    etat_appels = {'n': 0}

    def _poll(ip, c, infos=None):
        etat_appels['n'] += 1
        base = 1_000_000 if etat_appels['n'] == 1 else 3_000_000
        return ({1: dict(oper=1, oper_ok=True, speed_mbps=1000, in_oct=base, out_oct=0,
                         in_pkts=base // 100, out_pkts=0, in_err=0, out_err=0)},
                True, True, 1000 + etat_appels['n'] * 5)

    monkeypatch.setattr(N, '_noms_interfaces', lambda ip, c: {1: {'nom': 'Gi0/1', 'alias': '',
                                                                  'ethernet': True, 'speed_mbps': 1000}})
    monkeypatch.setattr(N, '_poll_switch_ports', _poll)
    monkeypatch.setattr(N, '_fdb_switch', lambda ip, c: {})
    N._cycle_activite([cid])
    assert etat_appels['n'] == 2, "le 1er cycle à froid doit poller 2 fois"
    with N._activite_lock:
        res = N._activite_resultat.get(cid)
    etat = next(p['etat'] for p in res['ports'] if p['numero'] == 1)
    assert etat in ('traffic', 'sature'), f"LED animée attendue dès le 1er cycle, obtenu {etat!r}"


def test_activite_baie_reveille_la_boucle(monkeypatch):
    _isoler_memoire(monkeypatch)
    monkeypatch.setattr(N, '_demarrer_activite_thread', lambda: None)
    N._activite_wake.clear()
    N.activite_baie(4242)
    assert N._activite_wake.is_set()


def test_switch_injoignable_ne_bloque_pas_le_cycle(conn, make_client, make_appareil, monkeypatch):
    """Fast-fail : un switch qui ne répond pas en SNMP est écarté par la sonde de
    présence — `_poll_switch_ports` (≈ 40 s de replis GETNEXT sur un mort) n'est
    JAMAIS appelé pour lui, les autres switchs ne sont pas gelés derrière."""
    _isoler_memoire(monkeypatch)
    # présence : le 1er switch répond, le 2e non
    monkeypatch.setattr(N, '_presence_baie_ok',
                        lambda cid, ip, c: ip == '10.0.0.10')
    poll_ips = []

    def _poll(ip, c, infos=None):
        poll_ips.append(ip)
        return ({1: dict(oper=1, oper_ok=True, speed_mbps=1000, in_oct=5_000_000, out_oct=0,
                         in_pkts=4000, out_pkts=0, in_err=0, out_err=0)}, True, True, 1234)

    monkeypatch.setattr(N, '_noms_interfaces', lambda ip, c: {1: {'nom': 'Gi0/1', 'alias': '',
                                                                  'ethernet': True, 'speed_mbps': 1000}})
    monkeypatch.setattr(N, '_poll_switch_ports', _poll)
    monkeypatch.setattr(N, '_fdb_switch', lambda ip, c: {})

    cid = make_client()
    for ip in ('10.0.0.10', '10.0.0.11'):
        a = make_appareil(cid, nom_machine='SW-' + ip[-1], type_appareil='Switch', adresse_ip=ip)
        conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,?,?)",
                     (cid, int(ip[-1]), a))
        sid = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (a,)).fetchone()[0]
        conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,1)", (sid,))
    conn.commit()

    N._cycle_activite([cid])
    assert '10.0.0.11' not in poll_ips, f"le switch muet ne doit pas être pollé : {poll_ips}"
    assert '10.0.0.10' in poll_ips
    with N._activite_lock:
        res = N._activite_resultat.get(cid)
    assert res and res['actif'] is True          # le cycle aboutit malgré le switch mort
