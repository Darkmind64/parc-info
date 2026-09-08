"""Baux DHCP relevés à la source (Plan 1) — parseurs purs, relevé SNMP, base,
recoupements, routes."""
from conftest import login_session

from netdiag import dhcp as D


# ─── Normalisation ──────────────────────────────────────────────────────────

def test_norm_mac():
    assert D._norm_mac('AA-BB-CC-DD-EE-FF') == 'aa:bb:cc:dd:ee:ff'
    assert D._norm_mac('aabb.ccdd.eeff') == 'aa:bb:cc:dd:ee:ff'
    assert D._norm_mac('aa bb cc dd ee ff') == 'aa:bb:cc:dd:ee:ff'
    assert D._norm_mac('00:11:22:33:44:55') == '00:11:22:33:44:55'
    assert D._norm_mac('nope') == ''
    assert D._norm_mac('') == ''


# ─── Parseur ISC (pfSense / OPNsense / ISC dhcpd) ───────────────────────────

_ISC = """
lease 192.168.1.10 {
  starts 3 2026/01/10 12:00:00;
  ends 3 2026/01/10 18:00:00;
  binding state active;
  hardware ethernet 00:11:22:33:44:55;
  client-hostname "laptop-01";
}
lease 192.168.1.11 {
  binding state free;
  hardware ethernet aa:bb:cc:dd:ee:99;
}
lease 192.168.1.12 {
  binding state active;
  hardware ethernet 00:11:22:33:44:66;
}
host serveur-nas {
  hardware ethernet 00:aa:bb:cc:dd:ee;
  fixed-address 192.168.1.50;
}
"""


def test_parse_isc_leases():
    baux = D._parse_isc_leases(_ISC)
    par_ip = {b['ip']: b for b in baux}
    assert '192.168.1.11' not in par_ip                 # binding state free → ignoré
    assert par_ip['192.168.1.10']['type'] == D.DYNAMIQUE
    assert par_ip['192.168.1.10']['mac'] == '00:11:22:33:44:55'
    assert par_ip['192.168.1.10']['hostname'] == 'laptop-01'
    assert par_ip['192.168.1.10']['expiration'] == '2026-01-10T18:00:00'
    assert par_ip['192.168.1.50']['type'] == D.STATIQUE   # host { fixed-address }
    assert par_ip['192.168.1.50']['hostname'] == 'serveur-nas'


# ─── Parseur dnsmasq (OpenWrt) ─────────────────────────────────────────────

def test_parse_dnsmasq_leases():
    txt = ("1799999999 00:11:22:33:44:66 192.168.1.20 laptop 01:00:11:22:33:44:66\n"
           "0 00:11:22:33:44:77 192.168.1.21 nas *\n"
           "1799999999 aa:bb:cc:dd:ee:01 192.168.1.22 * *\n")
    par_ip = {b['ip']: b for b in D._parse_dnsmasq_leases(txt)}
    assert par_ip['192.168.1.20']['type'] == D.DYNAMIQUE
    assert par_ip['192.168.1.21']['type'] == D.STATIQUE    # expiry 0 = infini
    assert par_ip['192.168.1.22']['hostname'] == ''        # '*' → pas de hostname


# ─── Parseur export CSV Windows Server ─────────────────────────────────────

def test_parse_windows_dhcp_csv():
    csv_txt = ('#TYPE Microsoft.Management.Infrastructure.CimInstance\r\n'
               'IPAddress,ClientId,HostName,AddressState,LeaseExpiryTime\r\n'
               '192.168.1.30,aa-bb-cc-dd-ee-ff,SRV1,ActiveReservation,\r\n'
               '192.168.1.31,11-22-33-44-55-66,WKS3,Active,2026-01-10 18:00:00\r\n'
               '192.168.1.32,22-33-44-55-66-77,OLD,Declined,\r\n')
    par_ip = {b['ip']: b for b in D._parse_windows_dhcp_csv('﻿' + csv_txt)}
    assert '192.168.1.32' not in par_ip                   # Declined → ignoré
    assert par_ip['192.168.1.30']['type'] == D.STATIQUE   # *Reservation
    assert par_ip['192.168.1.30']['mac'] == 'aa:bb:cc:dd:ee:ff'
    assert par_ip['192.168.1.31']['type'] == D.DYNAMIQUE


# ─── Auto-détection ────────────────────────────────────────────────────────

def test_parser_auto():
    assert D.parser_auto(_ISC)[1] == 'isc'
    assert D.parser_auto('1799999999 00:11:22:33:44:66 192.168.1.20 host id')[1] == 'dnsmasq'
    assert D.parser_auto('IPAddress,ClientId,AddressState\n1.2.3.4,aa-bb-cc-dd-ee-ff,Active')[1] == 'windows'
    assert D.parser_auto('rien de reconnaissable ici') == ([], '')


# ─── Relevé SNMP Mikrotik (faux agent) ────────────────────────────────────

def test_baux_snmp_mikrotik(monkeypatch):
    # mtxrDHCPLeaseAddress = ...6.2.1.2.<idx>  /  MAC = ...6.2.1.3.<idx>
    def faux_walk(oid_base, ip, comm, **kw):
        if oid_base == D._MT_LEASE_ADDR:
            return {'1': '192.168.88.10', '2': '192.168.88.11'}
        if oid_base == D._MT_LEASE_SRV:
            return {'1': 'dhcp1', '2': 'dhcp1'}
        if oid_base == D._MT_LEASE_EXP:
            return {'1': 3600, '2': 0}
        return {}

    def faux_walk_octets(oid_base, ip, comm, **kw):
        if oid_base == D._MT_LEASE_MAC:
            return {'1': bytes.fromhex('001122334455'), '2': bytes.fromhex('aabbccddeeff')}
        return {}

    monkeypatch.setattr('network_diag._snmp_walk', faux_walk)
    monkeypatch.setattr('network_diag._snmp_walk_octets', faux_walk_octets)
    baux = D._baux_snmp_mikrotik('10.0.0.1', ['public'])
    par_ip = {b['ip']: b for b in baux}
    assert par_ip['192.168.88.10']['mac'] == '00:11:22:33:44:55'
    assert par_ip['192.168.88.10']['expiration']            # secs > 0 → date
    assert par_ip['192.168.88.11']['mac'] == 'aa:bb:cc:dd:ee:ff'
    assert par_ip['192.168.88.10']['source_methode'] == 'snmp:mikrotik'


def test_baux_snmp_mikrotik_agent_muet(monkeypatch):
    monkeypatch.setattr('network_diag._snmp_walk', lambda *a, **k: {})
    monkeypatch.setattr('network_diag._snmp_walk_octets', lambda *a, **k: {})
    assert D._baux_snmp_mikrotik('10.0.0.1', ['public']) == []


# ─── Persistance + recoupements ───────────────────────────────────────────

def test_importer_lister_bail_pour_mac(conn, make_client):
    cid = make_client()
    baux = D._parse_isc_leases(_ISC)
    imp = D.importer_baux(conn, cid, baux, source_methode='fichier:isc')
    conn.commit()
    assert imp['ecrits'] == 3
    lst = D.lister_baux(conn, cid)
    assert len(lst) == 3
    b = D.bail_pour_mac(conn, cid, '00-11-22-33-44-55')
    assert b and b['ip'] == '192.168.1.10' and b['hostname'] == 'laptop-01'
    # ré-import de la même source → remplace, pas de doublon
    imp2 = D.importer_baux(conn, cid, baux[:1], source_methode='fichier:isc')
    conn.commit()
    assert imp2['supprimes'] == 3 and len(D.lister_baux(conn, cid)) == 1


def test_baux_hors_inventaire(conn, make_client, make_appareil):
    cid = make_client()
    make_appareil(cid, nom_machine='NAS', adresse_ip='192.168.1.50',
                  adresse_mac='00:aa:bb:cc:dd:ee')
    D.importer_baux(conn, cid, D._parse_isc_leases(_ISC), source_methode='fichier:isc')
    conn.commit()
    fant = D.baux_hors_inventaire(conn, cid)
    macs = {b['mac'] for b in fant}
    assert '00:aa:bb:cc:dd:ee' not in macs                  # dans l'inventaire
    assert '00:11:22:33:44:55' in macs                      # bail sans fiche


# ─── Croisement à l'import de scan ────────────────────────────────────────

def test_importer_scan_utilise_hostname_dhcp_et_signale_conflit_ip(conn, make_client):
    import app as A
    cid = make_client()
    D.importer_baux(conn, cid, [D._bail(ip='10.0.0.5', mac='00:1b:44:11:22:33',
                                        hostname='POSTE-COMPTA', type_bail=D.STATIQUE,
                                        source_methode='fichier:isc')],
                    source_methode='fichier:isc')
    conn.commit()
    # scan : même MAC, hostname NetBIOS absent, IP DIFFÉRENTE du bail
    items = [{'ip': '10.0.0.99', 'ports': [445], 'mac': '00:1b:44:11:22:33', 'type': 'PC'}]
    r = A._importer_appareils_scan(conn, cid, items, origine='scan')
    conn.commit()
    assert r['importes'] == 1
    row = conn.execute("SELECT nom_machine, nom_dns FROM appareils WHERE client_id=?",
                       (cid,)).fetchone()
    assert row[0] == 'POSTE-COMPTA'                         # hostname DHCP repris
    n = conn.execute("SELECT COUNT(*) FROM historique WHERE client_id=? AND action=?",
                     (cid, 'Conflit IP / bail DHCP')).fetchone()[0]
    assert n == 1


# ─── Routes ──────────────────────────────────────────────────────────────

def test_api_importer_format_et_lecture_seule(client, conn, make_client, make_user):
    uid, _, _ = make_user(role='user')
    mine = make_client(auth_user_id=uid)
    login_session(client, uid, mine)

    r = client.post('/api/dhcp/importer', json={'contenu': 'blabla non reconnaissable'})
    assert r.status_code == 400

    r = client.post('/api/dhcp/importer', json={'contenu': _ISC})
    assert r.status_code == 200
    j = r.get_json()
    assert j['format'] == 'isc' and j['ecrits'] == 3

    # accès en lecture seule sur un client partagé → import refusé (can_write False)
    autre_uid, _, _ = make_user(role='user')
    partage = make_client(auth_user_id=autre_uid)
    conn.execute("INSERT INTO client_partages (client_id, auth_user_id, niveau) VALUES (?,?,?)",
                 (partage, uid, 'lecture'))
    conn.commit()
    login_session(client, uid, partage)
    r = client.post('/api/dhcp/importer', json={'contenu': _ISC})
    assert r.status_code == 403


def test_api_baux_borne_au_client(client, conn, make_client, make_user):
    uid, _, _ = make_user(role='user')
    mine = make_client(auth_user_id=uid)
    login_session(client, uid, mine)
    D.importer_baux(conn, mine, D._parse_isc_leases(_ISC), source_methode='fichier:isc')
    conn.commit()
    r = client.get('/api/dhcp/baux')
    assert r.status_code == 200
    j = r.get_json()
    assert j['resume']['total'] == 3 and j['nb_fantomes'] >= 1


def test_api_appareil_dhcp_encart(client, conn, make_client, make_user, make_appareil):
    uid, _, _ = make_user(role='user')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid, nom_machine='PC', adresse_ip='10.0.0.5',
                        adresse_mac='00:1b:44:aa:bb:cc')
    login_session(client, uid, cid)
    r = client.get('/api/appareil/%d/dhcp' % aid)
    assert r.status_code == 200 and r.get_json()['a_montrer'] is False
    D.importer_baux(conn, cid, [D._bail(ip='10.0.0.9', mac='00:1b:44:aa:bb:cc',
                                        hostname='pc-decl', type_bail=D.DYNAMIQUE,
                                        source_methode='snmp:mikrotik')],
                    source_methode='snmp:mikrotik')
    conn.commit()
    j = client.get('/api/appareil/%d/dhcp' % aid).get_json()
    assert j['a_montrer'] and j['bail']['ip_incoherente'] is True    # 10.0.0.9 ≠ 10.0.0.5
