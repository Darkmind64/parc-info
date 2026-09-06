"""`_mac_octets` tolère un préfixe parasite, et `diagnostiquer_fdb_brute`
montre la forme exacte des MAC renvoyées par l'agent (retour terrain : MAC
préfixées 00:01 / chaînes de 8 octets)."""
import network_diag as N


def test_mac_octets_prefixe_parasite():
    assert N._mac_octets(bytes.fromhex('aabbccddeeff')) == 'aa:bb:cc:dd:ee:ff'
    # 8 octets : 2 octets parasites en tête (00:01) + la vraie MAC entière
    assert N._mac_octets(bytes.fromhex('0001aabbccddeeff')) == 'aa:bb:cc:dd:ee:ff'
    # 10 octets (double encapsulation BER)
    assert N._mac_octets(bytes.fromhex('04060001aabbccddeeff')) == 'aa:bb:cc:dd:ee:ff'
    # STP : 2 octets de priorité du pont
    assert N._mac_octets(bytes.fromhex('8000aabbccddeeff')) == 'aa:bb:cc:dd:ee:ff'


def test_mac_octets_rejette_ce_qui_nest_pas_une_mac():
    assert N._mac_octets(bytes.fromhex('aabbcc')) == ''          # < 6 octets
    assert N._mac_octets(b'GigabitEther0/1') == ''               # > 12 octets
    assert N._mac_octets(None) == ''
    assert N._mac_octets(b'') == ''


def test_diagnostiquer_fdb_brute(conn, make_client, make_appareil, monkeypatch):
    cid = make_client()
    sw = make_appareil(cid, nom_machine='SW-PROC', type_appareil='Switch',
                       adresse_ip='10.7.0.2', adresse_mac='aa:bb:cc:00:00:01')
    pc = make_appareil(cid, nom_machine='PC1', adresse_ip='10.7.0.20',
                       adresse_mac='de:ad:be:ef:00:11')
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,1,?)",
                 (cid, sw))
    conn.commit()

    monkeypatch.setattr(N, '_communautes_snmp', lambda: ['public'])
    import app as _app
    monkeypatch.setattr(_app, '_snmp_presence', lambda ip, comm, **k: (True, True, ''))

    def _fake_walk(oid, ip, comm, **k):
        if oid == N._OID_FDB_DOT1Q_PORT:
            # index dot1q buggé : <fdbId=1>.<0>.<1>.<4 octets réels> -> "00:01:..." après [-6:]
            return {'1.0.1.222.173.190.239': 5,
                    '1.0.1.170.187.204.0': 7}
        return {}
    monkeypatch.setattr(N, '_snmp_walk', _fake_walk)
    monkeypatch.setattr(N, '_snmp_walk_octets',
                        lambda oid, ip, comm, **k: {'3.10.7.0.20': bytes.fromhex('0001deadbeef0011')}
                        if oid == N._OID_ARP_PHYS else {})

    d = N.diagnostiquer_fdb_brute(cid)
    e = d['equipements'][0]
    assert e['nom'] == 'SW-PROC' and e['dialecte'] == 'dot1q'
    assert e['fdb']['nb'] == 2
    # les 6 derniers sous-identifiants -> "00:01:de:ad:be:ef" (préfixe parasite visible)
    assert e['fdb']['exemples'][0]['mac_6_derniers'] == '00:01:de:ad:be:ef'
    assert e['fdb']['exemples'][0]['dans_inventaire'] == ''    # ne matche rien tel quel
    # ARP : 8 octets reçus, les 6 derniers = la vraie MAC (récupérable) -> reconnue
    a0 = e['arp']['exemples'][0]
    assert a0['nb_octets'] == 8 and a0['mac_6_derniers'] == 'de:ad:be:ef:00:11'
    assert a0['dans_inventaire'] == 'PC1'


# ── correction d'une FDB tronquée avec la table ARP d'un routeur comme référence ──

def test_fdb_corriger_prefixe2_sans_reference_ne_detecte_rien():
    # switch ProCurve : 4 premiers octets de la vraie MAC, préfixés 00:01
    raw = {3: {'00:01:00:23:24:53', '00:01:20:7b:d2:a3'}, 5: {'00:01:00:09:4c:c5'}}
    inv = {'de:ad:be:ef:00:11': (1, 'x', 'PC')}   # inventaire pauvre
    fdb, meta = N._fdb_corriger(raw, inv, '')
    assert meta['transform'] == 'exact'          # rien détecté -> les 00:01 passent
    assert fdb[3] == raw[3]


def test_fdb_corriger_prefixe2_avec_reference_arp_recupere_la_mac_entiere():
    raw = {3: {'00:01:00:23:24:53', '00:01:20:7b:d2:a3'}, 5: {'00:01:00:09:4c:c5'}}
    inv = {'de:ad:be:ef:00:11': (1, 'x', 'PC')}
    # la table ARP d'un routeur du parc porte les MAC ENTIÈRES
    ref = {'00:23:24:53:87:3d', '20:7b:d2:a3:1f:b7', '00:09:4c:c5:11:22'}
    fdb, meta = N._fdb_corriger(raw, inv, '', reference=ref)
    assert meta['transform'] == 'prefixe2' and meta['reconnues'] == 3
    assert fdb[3] == {'00:23:24:53:87:3d', '20:7b:d2:a3:1f:b7'}
    assert fdb[5] == {'00:09:4c:c5:11:22'}       # récupérée même si absente de l'inventaire


def test_fdb_switch_expose_arp_macs(monkeypatch):
    # `_fdb_switch` écrit dans des dicts globaux (cache) : on les isole pour ne
    # pas polluer les autres tests.
    monkeypatch.setattr(N, '_activite_fdb', {})
    monkeypatch.setattr(N, '_activite_fdb_dialecte', {})
    monkeypatch.setattr(N, '_activite_fdb_echec', {})
    monkeypatch.setattr(N, '_activite_fdb_baseport', {})
    monkeypatch.setattr(N, '_snmp_walk', lambda *a, **k: {})         # pas de bridge FDB
    monkeypatch.setattr(N, '_vlans_actifs', lambda *a, **k: ([], {}))
    monkeypatch.setattr(N, '_fdb_par_vlan', lambda *a, **k: ({}, {}))
    monkeypatch.setattr(N, '_snmp_walk_octets',
                        lambda oid, ip, comm, **k: {'2.192.168.1.20': bytes.fromhex('f48c504eb574')}
                        if oid == N._OID_ARP_PHYS else {})
    par_if, info = N._fdb_switch('10.0.0.1', ['public'])
    assert info['arp_macs'] == {'f4:8c:50:4e:b5:74'}
