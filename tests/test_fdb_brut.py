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
