"""« Changements depuis la dernière visite » — instantané d'inventaire par scan
+ diff (nouveaux / disparus / IP changée) + corrélation MAC au scan (bouche le
trou : un appareil qui a changé d'IP n'est plus vu comme un nouvel appareil)."""
from conftest import login_session

import client_helpers as CH


def test_diff_nouveaux_disparus_ip(conn, make_client, make_appareil):
    cid = make_client()
    a = make_appareil(cid, nom_machine='PC-A', adresse_ip='10.0.0.10',
                      adresse_mac='aa:bb:cc:00:00:01', type_appareil='PC', ports_ouverts='22,80')
    make_appareil(cid, nom_machine='NAS-1', adresse_ip='10.0.0.20',
                  adresse_mac='aa:bb:cc:00:00:02', type_appareil='NAS')
    CH.capturer_instantane(conn, cid, 'scan')
    conn.commit()

    conn.execute("UPDATE appareils SET adresse_ip='10.0.0.55' WHERE id=?", (a,))
    conn.execute("DELETE FROM appareils WHERE client_id=? AND nom_machine='NAS-1'", (cid,))
    make_appareil(cid, nom_machine='PC-B', adresse_ip='10.0.0.30',
                  adresse_mac='aa:bb:cc:00:00:03', type_appareil='PC')
    conn.commit()
    CH.capturer_instantane(conn, cid, 'scan')
    conn.commit()

    d = CH.changements_client(conn, cid)
    assert d['disponible'] and d['nb'] == 3
    assert [x['nom'] for x in d['nouveaux']] == ['PC-B']
    assert [x['nom'] for x in d['disparus']] == ['NAS-1']
    assert d['ip_changees'] == [{'id': a, 'nom': 'PC-A', 'avant': '10.0.0.10', 'apres': '10.0.0.55'}]


def test_fiche_recreee_meme_mac_nest_pas_un_nouveau(conn, make_client, make_appareil):
    cid = make_client()
    make_appareil(cid, nom_machine='SW', adresse_ip='10.0.0.2', adresse_mac='de:ad:be:ef:00:11',
                  type_appareil='Switch')
    CH.capturer_instantane(conn, cid, 'scan'); conn.commit()
    conn.execute("DELETE FROM appareils WHERE client_id=?", (cid,))
    # meme MAC, IP differente, nouvel id
    nid = make_appareil(cid, nom_machine='SW', adresse_ip='10.0.0.9',
                        adresse_mac='de:ad:be:ef:00:11', type_appareil='Switch')
    CH.capturer_instantane(conn, cid, 'scan'); conn.commit()
    d = CH.changements_client(conn, cid)
    assert d['nouveaux'] == [] and d['disparus'] == []
    assert d['ip_changees'] and d['ip_changees'][0]['apres'] == '10.0.0.9'


def test_un_seul_instantane_pas_de_diff(conn, make_client, make_appareil):
    cid = make_client()
    make_appareil(cid, nom_machine='X', adresse_ip='10.0.0.1')
    CH.capturer_instantane(conn, cid, 'scan'); conn.commit()
    d = CH.changements_client(conn, cid)
    assert d['disponible'] is False


def test_reference_epinglee_sert_de_base(conn, make_client, make_appareil):
    cid = make_client()
    make_appareil(cid, nom_machine='A', adresse_ip='10.0.0.1', adresse_mac='aa:aa:aa:aa:aa:01')
    ref = CH.capturer_instantane(conn, cid, 'manuel', reference=True); conn.commit()
    for ip in ('10.0.0.2', '10.0.0.3'):
        make_appareil(cid, adresse_ip=ip, adresse_mac='aa:aa:aa:aa:aa:' + ip[-2:])
        CH.capturer_instantane(conn, cid, 'scan'); conn.commit()
    d = CH.changements_client(conn, cid)
    # compare la REFERENCE (1 appareil) au dernier (3 appareils) -> 2 nouveaux
    assert d['avant']['id'] == ref and len(d['nouveaux']) == 2


def test_scan_correlation_mac_detecte_changement_ip(client, conn, make_user, make_client, make_appareil):
    uid, _l, _p = make_user()
    cid = make_client(auth_user_id=uid)
    a = make_appareil(cid, nom_machine='POSTE-1', adresse_ip='192.168.1.10',
                      adresse_mac='00:1a:2b:3c:4d:5e', type_appareil='PC')
    login_session(client, uid, cid)
    r = client.post('/api/scan/importer', json={'appareils': [
        {'ip': '192.168.1.77', 'mac': '00:1A:2B:3C:4D:5E', 'hostname': 'poste-1', 'ports': [3389]},
    ]})
    assert r.status_code == 200
    row = conn.execute("SELECT adresse_ip FROM appareils WHERE id=?", (a,)).fetchone()
    assert row[0] == '192.168.1.77'          # l'IP a été mise à jour, pas de doublon
    assert conn.execute("SELECT COUNT(*) FROM appareils WHERE client_id=?", (cid,)).fetchone()[0] == 1
    h = conn.execute("SELECT action FROM historique WHERE client_id=? AND entite_id=? "
                     "AND action LIKE '%IP%'", (cid, a)).fetchone()
    assert h and "IP" in h[0]
