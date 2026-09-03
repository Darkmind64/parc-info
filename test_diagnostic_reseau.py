#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostic réseau (network_diag.py) — fonctions de détection du palier 1.

Ce que le test contrôle, sans toucher au vrai réseau (tables ARP et sorties
`ping`/`arp` simulées) :

  - detecter_conflits_ip() : une IP portant plusieurs MAC -> conflit ; une MAC
    (hors passerelle) portant beaucoup d'IP -> usurpation probable ; la
    passerelle légitime multi-IP n'est jamais signalée
  - _ping_rafale() : parse correctement les sorties `ping` Windows ET Unix
    (perte, min/moy/max, gigue)
  - mesurer_qualite_liaison() : seuils de perte / gigue respectés ; cible
    injoignable -> passerelle_injoignable si role=passerelle
  - _signature() / _finding() : stable pour un même couple (catégorie, entités),
    insensible au titre et aux détails -> dédoublonnage fiable
  - etat_capture() : renvoie toujours {disponible, motif}

Usage :
    python test_diagnostic_reseau.py
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='diag_reseau_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import network_diag as N  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


print('=== 1. detecter_conflits_ip() : une IP -> plusieurs MAC ===')
N._table_arp = lambda: {
    '192.168.1.50': {'aa:bb:cc:dd:ee:01', 'aa:bb:cc:dd:ee:02'},
    '192.168.1.1':  {'11:22:33:44:55:66'},
    '192.168.1.51': {'aa:bb:cc:dd:ee:03'},
}
f = N.detecter_conflits_ip('192.168.1.1', releves=1)
cats = [x['categorie'] for x in f]
verifier(cats.count('conflit_ip') == 1, "une IP à 2 MAC -> exactement un conflit_ip")
verifier('192.168.1.50' in f[0]['details']['ip'], "le conflit porte sur la bonne IP")
verifier(all(c != 'arp_spoofing' for c in cats), "aucune fausse alerte d'usurpation ici")

print('\n=== 2. detecter_conflits_ip() : une MAC -> beaucoup d\'IP ===')
N._table_arp = lambda: {f'10.0.0.{i}': {'de:ad:be:ef:00:01'} for i in range(2, 13)}
f = N.detecter_conflits_ip('10.0.0.1', releves=1)
verifier(any(x['categorie'] == 'arp_spoofing' for x in f),
         "11 IP derrière une seule MAC -> arp_spoofing")

print('\n=== 2bis. la passerelle légitime multi-IP n\'est pas signalée ===')
N._table_arp = lambda: dict({f'10.0.0.{i}': {'ga:te:wa:y0:00:01'} for i in range(2, 20)},
                            **{'10.0.0.1': {'ga:te:wa:y0:00:01'}})
f = N.detecter_conflits_ip('10.0.0.1', releves=1)
verifier(not any(x['categorie'] == 'arp_spoofing' for x in f),
         "la MAC de la passerelle (10.0.0.1) est mise en liste blanche")

print('\n=== 3. _ping_rafale() : parsing des sorties ping ===')
_run_orig = N._run


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


SORTIE_WINDOWS = """
Envoi d'une requête 'ping' sur 192.168.1.1 avec 32 octets de donnees :
Reponse de 192.168.1.1 : octets=32 temps=2 ms TTL=64
Reponse de 192.168.1.1 : octets=32 temps=5 ms TTL=64
Reponse de 192.168.1.1 : octets=32 temps<1ms TTL=64
Delai d'attente de la demande depasse.

Statistiques Ping pour 192.168.1.1:
    Paquets : envoyes = 4, recus = 3, perdus = 1 (25% de perte),
"""
SORTIE_UNIX = """
PING 192.168.1.1 (192.168.1.1) 56(84) bytes of data.
64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=1.20 ms
64 bytes from 192.168.1.1: icmp_seq=2 ttl=64 time=3.40 ms
64 bytes from 192.168.1.1: icmp_seq=3 ttl=64 time=1.10 ms
64 bytes from 192.168.1.1: icmp_seq=4 ttl=64 time=1.30 ms
--- 192.168.1.1 ping statistics ---
4 packets transmitted, 4 received, 0% packet loss
"""

N._run = lambda cmd, timeout=6: _FakeProc(SORTIE_WINDOWS)
st = N._ping_rafale('192.168.1.1', 4)
verifier(st['recus'] == 3 and st['perte_pct'] == 25.0,
         "Windows : 3 réponses sur 4 -> 25% de perte", str(st))
verifier(st['min'] is not None and st['max'] == 5.0, "Windows : latence max extraite", str(st))

N._run = lambda cmd, timeout=6: _FakeProc(SORTIE_UNIX)
st = N._ping_rafale('192.168.1.1', 4)
verifier(st['recus'] == 4 and st['perte_pct'] == 0.0, "Unix : aucune perte", str(st))
verifier(st['gigue'] is not None and st['gigue'] >= 0, "Unix : gigue calculée", str(st))
N._run = _run_orig

print('\n=== 4. mesurer_qualite_liaison() : seuils ===')
N._ping_rafale = lambda ip, n=20: {'ip': ip, 'envoyes': n, 'recus': n, 'perte_pct': 12.0,
                                   'min': 1.0, 'moy': 2.0, 'max': 40.0, 'gigue': 45.0}
f = N.mesurer_qualite_liaison([{'ip': '192.168.1.1', 'libelle': 'Passerelle', 'role': 'passerelle'}],
                              seuil_perte=5, seuil_gigue=30)
verifier(len(f) == 1 and f[0]['categorie'] == 'qualite_liaison',
         "perte 12% > seuil 5% ET gigue 45 > 30 -> une alerte qualite_liaison")

N._ping_rafale = lambda ip, n=20: {'ip': ip, 'envoyes': n, 'recus': n, 'perte_pct': 1.0,
                                   'min': 1.0, 'moy': 2.0, 'max': 3.0, 'gigue': 2.0}
f = N.mesurer_qualite_liaison([{'ip': '192.168.1.1', 'libelle': 'DNS', 'role': 'dns'}],
                              seuil_perte=5, seuil_gigue=30)
verifier(f == [], "liaison saine -> aucune alerte")

N._ping_rafale = lambda ip, n=20: {'ip': ip, 'envoyes': n, 'recus': 0, 'perte_pct': 100.0,
                                   'min': None, 'moy': None, 'max': None, 'gigue': None}
f = N.mesurer_qualite_liaison([{'ip': '192.168.1.1', 'libelle': 'Passerelle', 'role': 'passerelle'}],
                              seuil_perte=5, seuil_gigue=30)
verifier(len(f) == 1 and f[0]['categorie'] == 'passerelle_injoignable',
         "passerelle qui ne répond à rien -> passerelle_injoignable")

print('\n=== 5. _signature() / _finding() : stable pour le dédoublonnage ===')
a = N._finding('conflit_ip', 'Titre A', {'ip': '1.2.3.4'}, '1.2.3.4')
b = N._finding('conflit_ip', 'Titre completement different', {'ip': '1.2.3.4', 'extra': 99}, '1.2.3.4')
c = N._finding('conflit_ip', 'Titre A', {'ip': '9.9.9.9'}, '9.9.9.9')
verifier(a['signature'] == b['signature'], "même (catégorie, entité) -> même signature (titre/détails ignorés)")
verifier(a['signature'] != c['signature'], "entité différente -> signature différente")

print('\n=== 6. etat_capture() : contrat de retour ===')
etat = N.etat_capture()
verifier(set(etat) == {'disponible', 'motif'}, "renvoie {disponible, motif}", str(etat))
verifier(isinstance(etat['disponible'], bool), "'disponible' est un booléen")

print('\n=== 7. Palier 3 — décodeurs BER SNMP ===')
import app as A  # noqa: E402
verifier(A._ber_decoder_oid(bytes.fromhex('2b06010201020201020a')) == '1.3.6.1.2.1.2.2.1.2.10',
         "OID multi-octets décodé")
verifier(A._ber_decoder_valeur(0x41, (123456).to_bytes(3, 'big')) == 123456, "Counter32")
verifier(A._ber_decoder_valeur(0x46, (10 ** 12).to_bytes(6, 'big')) == 10 ** 12, "Counter64")
verifier(A._ber_decoder_valeur(0x02, b'\x02') == 2, "INTEGER")
verifier(A._ber_decoder_valeur(0x04, b'Gi1/0/1') == 'Gi1/0/1', "OCTET STRING")
verifier(A._ber_decoder_valeur(0x82, b'') is None, "endOfMibView -> None")

print('\n=== 8. Palier 3 — _snmp_walk contre un agent SNMP factice ===')
import socket as _sock
import threading as _th

_TABLE = sorted([
    ('1.3.6.1.2.1.2.2.1.2.1', 0x04, b'Gi1/0/1'),
    ('1.3.6.1.2.1.2.2.1.2.2', 0x04, b'Gi1/0/2'),
    ('1.3.6.1.2.1.2.2.1.3.1', 0x02, b'\x06'),   # hors sous-arbre ifDescr → stop
], key=lambda t: [int(x) for x in t[0].split('.')])


def _oid_key(o):
    return [int(x) for x in o.split('.')]


def _faux_agent(sock):
    while True:
        try:
            data, exp = sock.recvfrom(4096)
        except OSError:
            return
        try:
            _, corps, _ = A._ber_lire_tlv(data, 0)
            p = 0
            _, _v, p = A._ber_lire_tlv(corps, p)
            _, _c, p = A._ber_lire_tlv(corps, p)
            _tag, pdu, _ = A._ber_lire_tlv(corps, p)          # 0xa1 GetNext
            pp = 0
            _, reqid, pp = A._ber_lire_tlv(pdu, pp)
            _, _e, pp = A._ber_lire_tlv(pdu, pp)
            _, _ei, pp = A._ber_lire_tlv(pdu, pp)
            _, vbl, pp = A._ber_lire_tlv(pdu, pp)
            bp = 0
            _, vb, bp = A._ber_lire_tlv(vbl, bp)
            bbp = 0
            _, oid_brut, bbp = A._ber_lire_tlv(vb, bbp)
            demande = A._ber_decoder_oid(oid_brut)
            suivant = next((t for t in _TABLE if _oid_key(t[0]) > _oid_key(demande)), None)
            if suivant is None:
                oid_r, tag_r, val_r = demande, 0x82, b''
            else:
                oid_r, tag_r, val_r = suivant
            vb_r = A._ber_sequence(0x30, A._ber_oid(oid_r) + bytes([tag_r]) + A._ber_longueur(len(val_r)) + val_r)
            pdu_r = A._ber_sequence(0xa2, A._ber_sequence(0x02, reqid) + A._ber_entier(0)
                                   + A._ber_entier(0) + A._ber_sequence(0x30, vb_r))
            msg = A._ber_sequence(0x30, A._ber_entier(1) + A._ber_chaine('public') + pdu_r)
            sock.sendto(msg, exp)
        except Exception:
            pass


_srv = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
_srv.bind(('127.0.0.1', 0))
_port = _srv.getsockname()[1]
_th.Thread(target=_faux_agent, args=(_srv,), daemon=True).start()

_r = A._snmp_walk('127.0.0.1', '1.3.6.1.2.1.2.2.1.2', ['public'], timeout=1.0, port=_port)
_srv.close()
verifier(_r == {'1': 'Gi1/0/1', '2': 'Gi1/0/2'},
         "walk d'ifDescr : deux ports, s'arrête en sortie de sous-arbre", str(_r))

print('\n=== 9. Palier 3 — détections SNMP (équipement simulé) ===')
os.environ.setdefault('PARCINFO_BACKUP', '0')
A.init_db()
_c = A.get_db()
_c.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (901, 'Client SNMP')")
_c.commit(); _c.close()


def _equip(ts, **over):
    port = dict(index=1, nom='Gi1/0/1', alias='uplink', oper=1, admin=1, speed_mbps=1000,
                in_oct=0, out_oct=0, in_err=0, out_err=0, in_disc=0, out_disc=0,
                align_err=0, fcs_err=0, late_coll=0, exc_coll=0, duplex=3)
    port.update(over)
    return {'sysname': 'sw-test', 'ts': ts, 'ports': [port]}


verifier(N._analyser_snmp(901, '10.9.9.9', 1, _equip(1000.0)) == [],
         "premier relevé (pas d'historique) -> aucun finding")
_f = N._analyser_snmp(901, '10.9.9.9', 1, _equip(1030.0, late_coll=4, fcs_err=200,
                                                 in_oct=900 * 1000 * 1000 // 8 * 30))
_cats = sorted(x['categorie'] for x in _f)
verifier('duplex_mismatch' in _cats, "late collisions -> duplex_mismatch", str(_cats))
verifier('port_crc' in _cats, "Δ FCS >= seuil -> port_crc", str(_cats))
verifier('port_sature' in _cats, "débit ~90 % de la vitesse -> port_sature", str(_cats))
_f = N._analyser_snmp(901, '10.9.9.9', 1, _equip(1060.0, duplex=2))
verifier([x['categorie'] for x in _f] == ['duplex_mismatch'],
         "half-duplex négocié sur port gigabit -> duplex_mismatch", str([x['categorie'] for x in _f]))
_e = N.etat_snmp(901)
verifier(_e['equipements'] and _e['equipements'][0]['ip'] == '10.9.9.9',
         "etat_snmp expose le dernier relevé par port")
_s1 = N._finding('port_crc', 't', {'equipement': '10.0.0.1', 'port_index': 3}, '10.0.0.1', 3)
_s2 = N._finding('port_crc', 'autre', {'equipement': '10.0.0.1', 'port_index': 3, 'x': 1}, '10.0.0.1', 3)
_s3 = N._finding('port_crc', 't', {'equipement': '10.0.0.1', 'port_index': 4}, '10.0.0.1', 4)
verifier(_s1['signature'] == _s2['signature'] and _s1['signature'] != _s3['signature'],
         "signature stable par (équipement, port), distincte d'un autre port")

print('\n=== 10. Palier 4 — topologie L2 + recoupement baie ===')
import time as _t
_c = A.get_db()
_c.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (902, 'Topo')")
_c.execute("INSERT INTO appareils (id, client_id, nom_machine, type_appareil, adresse_ip) "
           "VALUES (500, 902, 'SW', 'Switch', '10.2.0.1')")
_c.execute("INSERT INTO appareils (id, client_id, nom_machine, adresse_mac) "
           "VALUES (501, 902, 'PC-VU', 'aa:00:00:00:00:11')")
_c.execute("INSERT INTO appareils (id, client_id, nom_machine, adresse_mac) "
           "VALUES (502, 902, 'PC-DECLARE', 'aa:00:00:00:00:22')")
_c.execute("INSERT INTO baie_slots (id, client_id, position, appareil_id) VALUES (9, 902, 1, 500)")
_c.execute("INSERT INTO baie_slot_ports (slot_id, numero, appareil_id) VALUES (9, 5, 502)")
_c.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (9, 6)")
for k in ('diag_snmp_actif', 'diag_topologie_active'):
    _c.execute("INSERT OR REPLACE INTO config (cle, valeur) VALUES (?, '1')", (k,))
_c.commit(); _c.close()
from config_helpers import cfg_invalidate as _inv
_inv()

N.interroger_equipement = lambda ip, comm: {'sysname': 'sw', 'ts': _t.time(), 'ports': [
    {'index': 5, 'nom': 'Gi0/5', 'alias': '', 'oper': 1, 'admin': 1, 'speed_mbps': 1000,
     'in_oct': 0, 'out_oct': 0, 'in_err': 0, 'out_err': 0, 'in_disc': 0, 'out_disc': 0,
     'align_err': 0, 'fcs_err': 0, 'late_coll': 0, 'exc_coll': 0, 'duplex': 3}]}
_MAC_DEC = '.'.join(str(int('aa0000000011'[i:i + 2], 16)) for i in range(0, 12, 2))
N._snmp_walk = lambda oid, ip, comm: (
    {f'1.{_MAC_DEC}': '5'} if oid == N._OID_FDB_DOT1Q_PORT else
    {'5': '5'} if oid == N._OID_FDB_BASEPORT_IF else {})
N._snmp_walk_octets = lambda *a, **k: {}     # table ARP / MAC infra : vide (pas de vrai reseau)
N._activite_fdb.clear(); N._activite_fdb_baseport.clear(); N._activite_fdb_dialecte.clear()
_ft = N.decouvrir_topologie(902)
verifier([f['categorie'] for f in _ft] == ['cablage_incoherent'],
         "switch voit PC-VU sur port 5, la baie déclare PC-DECLARE -> cablage_incoherent",
         str([f['categorie'] for f in _ft]))
_et = N.etat_topologie(902)
verifier(_et['equipements'] and _et['equipements'][0]['ports'][0]['hotes'][0]['appareil_nom'] == 'PC-VU',
         "etat_topologie associe la MAC au bon appareil")
_c = A.get_db()
_c.execute("INSERT INTO diag_topologie (client_id, equipement_ip, equipement_appareil_id, "
           "port_index, appareil_vu_id, horodatage) VALUES (902,'10.2.0.1',500,6,501,'x')")
_c.commit(); _c.close()
verifier(N.appliquer_topologie_baie(902)['maj'] == 1, "appliquer-baie remplit le port 6 (vide)")
_c = A.get_db()
_p5 = _c.execute("SELECT appareil_id FROM baie_slot_ports WHERE slot_id=9 AND numero=5").fetchone()[0]
_p6 = _c.execute("SELECT appareil_id FROM baie_slot_ports WHERE slot_id=9 AND numero=6").fetchone()[0]
_c.close()
verifier(_p5 == 502 and _p6 == 501, "port occupé inchangé, port vide renseigné", f"p5={_p5} p6={_p6}")

print('\n=== 11. Palier 5 — baseline (dégradation relative) ===')
_c = A.get_db()
_ep = _t.time()
for i in range(30):
    _c.execute("INSERT INTO diag_metriques (client_id,categorie,cible,horodatage,epoch,valeur) "
               "VALUES (903,'liaison_latence','1.1.1.1','x',?,?)", (_ep - 600 * i, 6.0 + (i % 2)))
_c.execute("INSERT INTO diag_metriques (client_id,categorie,cible,horodatage,epoch,valeur) "
           "VALUES (903,'liaison_latence','1.1.1.1','x',?,?)", (_ep + 5, 60.0))
_c.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (903, 'BL')")
_c.commit(); _c.close()
_bf = N.evaluer_baseline(903)
verifier([f['categorie'] for f in _bf] == ['degradation_relative'],
         "60 ms vs référence ~6 ms -> degradation_relative", str([f['categorie'] for f in _bf]))
_c = A.get_db()
_c.execute("DELETE FROM diag_metriques WHERE client_id=903")
_ep = _t.time()
for i in range(30):
    _c.execute("INSERT INTO diag_metriques (client_id,categorie,cible,horodatage,epoch,valeur) "
               "VALUES (903,'liaison_latence','1.1.1.1','x',?,?)", (_ep - 600 * i, 6.0 + (i % 2)))
_c.execute("INSERT INTO diag_metriques (client_id,categorie,cible,horodatage,epoch,valeur) "
           "VALUES (903,'liaison_latence','1.1.1.1','x',?,?)", (_ep + 5, 10.0))
_c.commit(); _c.close()
verifier(N.evaluer_baseline(903) == [], "10 ms (sous le plancher de 20 ms) -> aucune alerte")
_serie = N.serie_metrique(903, 'liaison_latence', '1.1.1.1')
verifier(_serie['points'] and _serie['mediane'] is not None, "serie_metrique renvoie points + médiane")

print('\n=== 12. Palier 6 — remédiation + rapport ===')
verifier(N.remediation('duplex_mismatch') and 'corriger' in N.remediation('duplex_mismatch'),
         "remediation('duplex_mismatch') a des étapes de correction")
verifier(N.remediation('inconnu') is None, "catégorie inconnue -> None")
_ct, _mt, _fn = N.generer_rapport_diag(903)
verifier(_ct[:4] == b'%PDF' or _mt.startswith('text/html'),
         "generer_rapport_diag -> PDF ou HTML", _mt)
_ch, _mh, _fh = N.generer_rapport_diag(903, forcer_html=True)
verifier(_mh.startswith('text/html') and '<html' in _ch.lower(), "forcer_html -> HTML")

print('\n=== 13. Consolidation — parse_rapport_cron ===')
verifier(N.parse_rapport_cron('08:00') == {'hour': 8, 'minute': 0}, "'08:00' -> quotidien 08h00")
verifier(N.parse_rapport_cron('lun 07:30') == {'hour': 7, 'minute': 30, 'day_of_week': 'mon'},
         "'lun 07:30' -> hebdo lundi")
verifier(N.parse_rapport_cron('') is None and N.parse_rapport_cron('n\'importe quoi') is None
         and N.parse_rapport_cron('25:00') is None, "entrées vides / invalides -> None")

print('\n=== 14. Consolidation — mode rapide + budget du snapshot ===')
_c = A.get_db()
_c.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (904, 'Rapide')")
_c.commit(); _c.close()
_calls = {'n': None, 'capture': 0}
_orig_ping = N._ping_rafale
N._ping_rafale = lambda ip, n=20: (_calls.__setitem__('n', n) or
                                   {'ip': ip, 'envoyes': n, 'recus': n, 'perte_pct': 0.0,
                                    'min': 1.0, 'moy': 1.0, 'max': 1.0, 'gigue': 0.0})
_orig_capt = N.capture_passive
N.capture_passive = lambda *a, **k: (_calls.__setitem__('capture', _calls['capture'] + 1) or [])
N._passerelle_defaut = lambda: '192.168.255.254'
from config_helpers import cfg_invalidate as _inv2
_c = A.get_db()
_c.execute("INSERT OR REPLACE INTO config (cle, valeur) VALUES ('diag_capture_active', '1')")
_c.commit(); _c.close(); _inv2()
N._run_snapshot(904, '', None, rapide=True)
verifier(_calls['n'] == 8, "mode rapide -> ping n=8", str(_calls['n']))
verifier(_calls['capture'] == 0, "mode rapide -> capture passive non exécutée")
st = N.statut_snapshot()
verifier(isinstance(st.get('phases'), dict) and st['phases'], "resume phases renseigné", str(st.get('phases')))
N._ping_rafale, N.capture_passive = _orig_ping, _orig_capt

print('\n=== 15. Palier 7a — Wi-Fi (parsing + détections) ===')
_SAMPLE_IF = """
    Name                   : Wi-Fi
    State                  : connected
    SSID                   : PARC-WIFI
    BSSID                  : a0:b1:c2:d3:e4:f5
    Signal                 : 30%
    Channel                : 6
    Receive rate (Mbps)    : 72
"""
_SAMPLE_NET = """
SSID 1 : PARC-WIFI
    BSSID 1 : a0:b1:c2:d3:e4:f5
         Signal : 30%
         Channel : 6
    BSSID 2 : 00:11:22:33:44:55
         Signal : 55%
         Channel : 6
SSID 2 : VOISIN-A
    BSSID 1 : de:ad:be:ef:00:01
         Signal : 60%
         Channel : 4
SSID 3 : VOISIN-B
    BSSID 1 : de:ad:be:ef:00:02
         Signal : 62%
         Channel : 8
"""
_orig_run = N._run
N.IS_WINDOWS = True
N._run = lambda cmd, timeout=6: type('R', (), {
    'stdout': _SAMPLE_IF if 'interfaces' in cmd else _SAMPLE_NET, 'returncode': 0})()
w = N._wifi_windows()
verifier(w['connecte'] and w['ssid'] == 'PARC-WIFI' and w['canal'] == 6 and w['rssi_dbm'] == -85,
         "netsh : SSID / canal / RSSI (30% -> -85 dBm)", str({k: w[k] for k in ('ssid', 'canal', 'rssi_dbm')}))
verifier(len(w['aps']) == 4, "netsh networks : 4 BSSID extraits", str(len(w['aps'])))
N._run = _orig_run

_c = A.get_db()
_c.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (905, 'WiFi')")
_c.execute("INSERT INTO parc_general (client_id, wifi_ssid) VALUES (905, 'PARC-WIFI')")
_c.commit(); _c.close()
N.etat_wifi = lambda forcer=False: {'connecte': True, 'ssid': 'PARC-WIFI', 'bssid': 'aa:bb:cc:d3:e4:f5',
                       'rssi_dbm': -80, 'canal': 6, 'bande': '2.4 GHz', 'debit_mbps': 72,
                       'aps': [
                           {'ssid': 'PARC-WIFI', 'bssid': 'aa:bb:cc:d3:e4:f5', 'canal': 6, 'bande': '2.4 GHz'},
                           {'ssid': 'PARC-WIFI', 'bssid': 'dd:ee:ff:00:00:01', 'canal': 6, 'bande': '2.4 GHz'},
                           {'ssid': 'X', 'bssid': '00:00:01:00:00:01', 'canal': 5, 'bande': '2.4 GHz'},
                           {'ssid': 'Y', 'bssid': '00:00:02:00:00:01', 'canal': 7, 'bande': '2.4 GHz'},
                           {'ssid': 'Z', 'bssid': '00:00:03:00:00:01', 'canal': 8, 'bande': '2.4 GHz'},
                       ]}
_orig_vendor = N._vendor
N._vendor = lambda mac: {'aa:bb:cc': 'FabricantA', 'dd:ee:ff': 'FabricantB'}.get(mac[:8], '')
_wf = [x['categorie'] for x in N.diagnostiquer_wifi(905)]
N._vendor = _orig_vendor
verifier('wifi_signal_faible' in _wf, "RSSI -80 -> wifi_signal_faible", str(_wf))
verifier('wifi_canal_sature' in _wf, "5 AP sur canaux chevauchants -> wifi_canal_sature", str(_wf))
verifier('wifi_ap_suspect' in _wf,
         "SSID du parc diffusé par 2 fabricants RECONNUS -> wifi_ap_suspect", str(_wf))
N.etat_wifi = lambda forcer=False: {'connecte': False, 'motif': 'aucun adaptateur Wi-Fi'}
verifier(N.diagnostiquer_wifi(905) == [], "pas de Wi-Fi -> aucun finding")

print('\n=== 16. Palier 7b — onduleurs SNMP ===')
_c = A.get_db()
_c.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (906, 'UPS')")
_c.execute("INSERT INTO appareils (id, client_id, nom_machine, type_appareil, adresse_ip) "
           "VALUES (600, 906, 'UPS-SRV', 'Onduleur / UPS', '10.6.0.1')")
_c.commit(); _c.close()
from config_helpers import cfg_invalidate as _inv3
_c = A.get_db()
_c.execute("INSERT OR REPLACE INTO config (cle, valeur) VALUES ('diag_snmp_actif', '1')")
_c.commit(); _c.close(); _inv3()

def _fake_ups_get(ip, oids, comm='public', timeout=1.5, port=161):
    m = {N._OID_UPS_MODEL: 'Smart-UPS 1500', N._OID_UPS_OUT_SOURCE: 5,
         N._OID_UPS_MIN_REMAIN: 5, N._OID_UPS_BATT_STATUS: 3,
         N._OID_UPS_ON_BATT_S: 90, N._OID_UPS_ALARMS: 1,
         N._OID_APC_BATT_REPL: 2, N._OID_UPS_CHARGE_PCT: 20}
    return {o: m[o] for o in oids if o in m}
_orig_get_typed = A._snmp_get_typed
A._snmp_get_typed = _fake_ups_get
N._snmp_walk = lambda oid, ip, comm: ({'1': '95'} if oid == N._OID_UPS_OUT_LOAD else {})
_ups = N.interroger_ups('10.6.0.1', ['public'])
verifier(_ups and _ups['source_txt'] == 'batterie' and _ups['charge_pct'] == 95,
         "interroger_ups : source batterie, charge 95 %", str(_ups and _ups.get('source_txt')))
_uf = sorted(x['categorie'] for x in N._analyser_ups(906, '10.6.0.1', 600, _ups))
verifier({'ups_sur_batterie', 'ups_batterie_faible', 'ups_surcharge', 'ups_batterie_usee', 'ups_alarme'}
         <= set(_uf), "_analyser_ups : les 5 findings attendus", str(_uf))
# routage : un appareil UPS passe par interroger_ups
_routes = []
_orig_iu, _orig_ie = N.interroger_ups, N.interroger_equipement
N.interroger_ups = lambda ip, c: _routes.append(('ups', ip)) or None
N.interroger_equipement = lambda ip, c: _routes.append(('equip', ip)) or None
N.interroger_equipements_client(906)
N.interroger_ups, N.interroger_equipement = _orig_iu, _orig_ie
verifier(_routes == [('ups', '10.6.0.1')], "l'appareil Onduleur/UPS est routé vers interroger_ups", str(_routes))
_e = N.etat_ups(906)
verifier(_e['onduleurs'] and _e['onduleurs'][0]['ip'] == '10.6.0.1', "etat_ups liste l'onduleur")

A._snmp_get_typed = _orig_get_typed  # restaure la vraie fonction pour le test 17

print('\n=== 17. Revue — décodeur SNMP typé + faux positif wifi_ap_suspect ===')
# _snmp_get_typed doit rendre les INTEGER (que _snmp_get laisse tomber)
_srv2 = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
_srv2.bind(('127.0.0.1', 0))
_port2 = _srv2.getsockname()[1]


def _agent_scalaire(sock):
    while True:
        try:
            data, exp = sock.recvfrom(4096)
        except OSError:
            return
        try:
            _, corps, _ = A._ber_lire_tlv(data, 0)
            p = 0
            _, _v, p = A._ber_lire_tlv(corps, p)
            _, _c, p = A._ber_lire_tlv(corps, p)
            _t, pdu, _ = A._ber_lire_tlv(corps, p)
            pp = 0
            _, reqid, pp = A._ber_lire_tlv(pdu, pp)
            _, _e2, pp = A._ber_lire_tlv(pdu, pp)
            _, _ei, pp = A._ber_lire_tlv(pdu, pp)
            _, vbl, pp = A._ber_lire_tlv(pdu, pp)
            _, vb, _ = A._ber_lire_tlv(vbl, 0)
            _, oid_brut, _ = A._ber_lire_tlv(vb, 0)
            # repond INTEGER 42 pour n'importe quel OID demande
            vb_r = A._ber_sequence(0x30, A._ber_oid(A._ber_decoder_oid(oid_brut))
                                   + A._ber_sequence(0x02, (42).to_bytes(1, 'big')))
            pdu_r = A._ber_sequence(0xa2, A._ber_sequence(0x02, reqid) + A._ber_entier(0)
                                    + A._ber_entier(0) + A._ber_sequence(0x30, vb_r))
            sock.sendto(A._ber_sequence(0x30, A._ber_entier(1) + A._ber_chaine('public') + pdu_r), exp)
        except Exception:
            pass


_th.Thread(target=_agent_scalaire, args=(_srv2,), daemon=True).start()
_gt = A._snmp_get_typed('127.0.0.1', ['1.3.6.1.2.1.33.1.4.1.0'], ['public'][0], timeout=1.0, port=_port2)
_srv2.close()
verifier(_gt.get('1.3.6.1.2.1.33.1.4.1.0') == 42, "_snmp_get_typed rend un INTEGER", str(_gt))

# wifi_ap_suspect : 2 prefixes MAC inconnus (OUI non resolu) -> AUCUNE alerte
_c = A.get_db()
_c.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (907, 'W2')")
_c.execute("INSERT INTO parc_general (client_id, wifi_ssid) VALUES (907, 'PARC-W')")
_c.commit(); _c.close()
N._vendor = _orig_vendor
N.etat_wifi = lambda forcer=False: {'connecte': True, 'ssid': 'PARC-W', 'bssid': 'ab:cd:ef:00:00:01',
    'rssi_dbm': -50, 'canal': 6, 'bande': '2.4 GHz', 'aps': [
        {'ssid': 'PARC-W', 'bssid': 'ab:cd:ef:00:00:01', 'canal': 6, 'bande': '2.4 GHz'},
        {'ssid': 'PARC-W', 'bssid': 'fe:dc:ba:00:00:02', 'canal': 6, 'bande': '2.4 GHz'}]}
verifier(not any(x['categorie'] == 'wifi_ap_suspect' for x in N.diagnostiquer_wifi(907)),
         "2 préfixes MAC de fabricant INCONNU -> pas de faux positif wifi_ap_suspect")

print('\n=== 18. Vue d\'activité de la baie (LEDs live SNMP) ===')
from database import get_local_db as _get_local_db
_seuils = {'err': 20, 'sat_pct': 90, 'pps_mini': 1}
_prev = dict(in_oct=0, out_oct=0, in_pkts=0, out_pkts=0, in_err=0, out_err=0, ts=0.0)
# débit faible (800 bit/s) mais 3 paquets/s : doit clignoter quand même (v2.19.8 —
# avant, seul le % de bande passante comptait, un port bureautique restait "idle").
_cur = dict(oper=1, speed_mbps=1000, in_oct=100, out_oct=0, in_pkts=3, out_pkts=0,
            in_err=0, out_err=0)
_led = N._etat_led(_prev, _cur, 1.0, _seuils)
verifier(_led['etat'] == 'traffic' and 120 <= _led['blink_ms'] <= 1200,
         "port up, 3 pkt/s -> 'traffic' même à faible débit, clignotement borné", str(_led))
verifier(N._etat_led(_prev, dict(_cur, in_oct=120_000_000, in_pkts=3000), 1.0, _seuils)['etat'] == 'sature',
         "~96 % de la vitesse -> 'sature'")
verifier(N._etat_led(_prev, dict(_cur, in_err=30), 1.0, _seuils)['etat'] == 'err',
         "Δ erreurs 30 (> 20) -> 'err' (prioritaire sur le débit)")
verifier(N._etat_led(_prev, dict(_cur, oper=2), 1.0, _seuils)['etat'] == 'down',
         "ifOperStatus down -> 'down'")
verifier(N._etat_led(_prev, dict(_cur, in_oct=0, in_pkts=0), 1.0, _seuils)['etat'] == 'idle',
         "up, aucun paquet -> 'idle'")
_reboot = N._etat_led(dict(in_oct=10**9, out_oct=0, in_pkts=0, out_pkts=0, in_err=0, out_err=0, ts=0.0),
                      _cur, 1.0, _seuils)
verifier(_reboot['bps'] >= 0 and _reboot['reset'] is True,
         "compteur qui recule (reboot) -> débit jamais négatif, reset détecté")

verifier(N._port_physique_depuis_nom('GigabitEthernet1/0/12') == 12
         and N._port_physique_depuis_nom('xe-0/0/5.0') == 5
         and N._port_physique_depuis_nom('swp8') == 8,
         "_port_physique_depuis_nom : dernier segment du nom d'interface")

_c = A.get_db()
_c.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (908, 'Activite')")
_c.execute("INSERT INTO appareils (id, client_id, nom_machine, type_appareil, adresse_ip) "
           "VALUES (700, 908, 'SW-ACT', 'Switch', '10.8.0.1')")
_c.execute("INSERT INTO appareils (id, client_id, nom_machine, adresse_ip) "
           "VALUES (701, 908, 'PC-ACT', '10.8.0.50')")
_c.execute("INSERT INTO baie_slots (id, client_id, position, appareil_id) VALUES (80, 908, 1, 700)")
_c.execute("INSERT INTO baie_slot_ports (slot_id, numero, appareil_id) VALUES (80, 4, 701)")
_c.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (80, 9)")
_c.commit(); _c.close()

# l'ifIndex n'est PAS le numéro de port : port baie 4 = Gi1/0/4 = ifIndex 44
_infos = {44: {'nom': 'Gi1/0/4', 'alias': '', 'ethernet': True, 'speed_mbps': 1000},
          49: {'nom': 'Gi1/0/9', 'alias': '', 'ethernet': True, 'speed_mbps': 1000}}
_m, _src, _cal, _div = N._mapping_baie_ifindex(_get_local_db(), 908, 80, 700, _infos)
verifier(_m == {4: 44, 9: 49} and _cal is True and _src[4] == 'nom_port',
         "mapping par NOM d'interface (Gi1/0/4 -> port 4), pas par ifIndex brut", str((_m, _src)))
_c = A.get_db()
_c.execute("INSERT INTO diag_topologie (client_id, equipement_ip, equipement_appareil_id, "
           "port_index, appareil_vu_id, horodatage) VALUES (908,'10.8.0.1',700,15,701,'x')")
_c.commit(); _c.close()
_m2, _src2, _cal2, _div2 = N._mapping_baie_ifindex(_get_local_db(), 908, 80, 700, _infos)
verifier(_m2.get(4) == 15 and _src2[4] == 'topologie',
         "topologie : PC vu sur ifIndex 15 -> l'emporte sur le nom", str((_m2, _src2)))
_c = A.get_db()
_c.execute("DELETE FROM diag_topologie WHERE client_id=908")   # suite : mapping par nom
_c.commit(); _c.close()

# relevé par GETBULK multi-colonnes (mock) + liste complète des interfaces
N._activite_noms.pop('10.8.0.1', None)
N._noms_interfaces = lambda ip, comm: dict(_infos)
N._fdb_switch = lambda ip, comm: {}
N._poll_switch_ports = lambda ip, comm, infos=None: (
    {44: dict(oper=1, speed_mbps=1000, in_oct=0, out_oct=0, in_pkts=0, out_pkts=0, in_err=0, out_err=0),
     49: dict(oper=2, speed_mbps=0, in_oct=0, out_oct=0, in_pkts=0, out_pkts=0, in_err=0, out_err=0)},
    True, True, None)
N._cycle_activite([908])
with N._activite_lock:
    _res = N._activite_resultat.get(908)
    _detail = N._activite_detail.get(908)
_num = {p['numero']: p['etat'] for p in (_res or {}).get('ports', [])}
verifier(_res and _res['actif'] is True and _num.get(4) == 'idle' and _num.get(9) == 'down',
         "_cycle_activite : port 4 -> Gi1/0/4/ifIndex 44 (up, calme) ; port 9 -> ifIndex 49 (down)",
         str(_num))
verifier(_detail and _detail['switchs'][0]['compteurs_64bits'] is True
         and any(i['ifindex'] == 44 for i in _detail.get('interfaces', [])),
         "_activite_detail : compteurs 64 bits + liste complète des interfaces pour la calibration")
verifier(N.activite_baie(908).get('actif') is True and 908 in N._activite_heartbeat,
         "activite_baie() enregistre un battement et renvoie l'état en cache")

verifier(N.calibrer_port_baie(908, 80, 9, 49) is True,
         "calibrer_port_baie() fixe l'ifIndex d'un port de baie")
_c = A.get_db()
_iv = _c.execute("SELECT if_index FROM baie_slot_ports WHERE slot_id=80 AND numero=9").fetchone()[0]
_c.close()
verifier(_iv == 49, "calibration persistée dans baie_slot_ports.if_index", str(_iv))

_mon = N.moniteur_baie(908)
verifier(set(_mon) >= {'switchs', 'ports', 'interfaces', 'ports_baie', 'journal', 'capture', 'snmp_actif'},
         "moniteur_baie() renvoie interfaces + ports_baie + journal + capture", str(sorted(_mon)))

# capture indisponible (pas de scapy dans l'environnement de test) -> motif explicite
_orig_etat_capture = N.etat_capture
N.etat_capture = lambda: {'disponible': False, 'motif': 'scapy_absent'}
verifier(N.capturer_trafic(5) == {'disponible': False, 'motif': 'scapy_absent'},
         "capturer_trafic() renvoie le motif quand scapy est indisponible")
N.etat_capture = _orig_etat_capture

print('\n=== 19. GETBULK multi-colonnes (_snmp_bulk_cols) contre agent factice ===')
_BULK = {
    '1.3.6.1.2.1.31.1.1.1.1': {1: (0x04, b'Gi1/0/1'), 2: (0x04, b'Gi1/0/2'), 3: (0x04, b'Gi1/0/3')},
    '1.3.6.1.2.1.2.2.1.8':     {1: (0x02, b'\x01'),   2: (0x02, b'\x02'),   3: (0x02, b'\x01')},
}


def _agent_bulk(sock):
    while True:
        try:
            data, exp = sock.recvfrom(65535)
        except OSError:
            return
        try:
            _, corps, _ = A._ber_lire_tlv(data, 0)
            p = 0
            _, _v, p = A._ber_lire_tlv(corps, p)
            _, _c, p = A._ber_lire_tlv(corps, p)
            tag, pdu, _ = A._ber_lire_tlv(corps, p)          # 0xa5 = GetBulk
            pp = 0
            _, reqid, pp = A._ber_lire_tlv(pdu, pp)
            _, _nr, pp = A._ber_lire_tlv(pdu, pp)
            _, maxrep_b, pp = A._ber_lire_tlv(pdu, pp)
            maxrep = int.from_bytes(maxrep_b, 'big') if maxrep_b else 1
            _, vbl, pp = A._ber_lire_tlv(pdu, pp)
            demandes = []
            bp = 0
            while bp < len(vbl):
                _, vb, bp = A._ber_lire_tlv(vbl, bp)
                bbp = 0
                _, ob, bbp = A._ber_lire_tlv(vb, bbp)
                demandes.append(A._ber_decoder_oid(ob))
            colonnes = []
            for d in demandes:
                base = next((b for b in _BULK if d == b or d.startswith(b + '.')), None)
                dep = int(d[len(base) + 1:]) if (base and d != base and d[len(base) + 1:].isdigit()) else 0
                suivants = [(f'{base}.{i}', t, v) for i, (t, v) in sorted(_BULK.get(base, {}).items())
                            if i > dep][:maxrep]
                colonnes.append(suivants)
            # ordre COLONNE-majeur (≠ ordre des OID demandés) → prouve l'attribution par préfixe
            vbs = b''
            for col in colonnes:
                for oid_r, tag_r, val_r in col:
                    vbs += A._ber_sequence(0x30, A._ber_oid(oid_r) + bytes([tag_r])
                                           + A._ber_longueur(len(val_r)) + val_r)
            pdu_r = A._ber_sequence(0xa2, A._ber_sequence(0x02, reqid) + A._ber_entier(0)
                                    + A._ber_entier(0) + A._ber_sequence(0x30, vbs))
            sock.sendto(A._ber_sequence(0x30, A._ber_entier(1) + A._ber_chaine('public') + pdu_r), exp)
        except Exception:
            pass


_srv2 = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
_srv2.bind(('127.0.0.1', 0))
_port_b = _srv2.getsockname()[1]
_th.Thread(target=_agent_bulk, args=(_srv2,), daemon=True).start()
_rb = A._snmp_bulk_cols('127.0.0.1', ['1.3.6.1.2.1.31.1.1.1.1', '1.3.6.1.2.1.2.2.1.8'],
                        ['public'], timeout=1.0, port=_port_b)
_srv2.close()
verifier(_rb.get('1.3.6.1.2.1.31.1.1.1.1') == {'1': 'Gi1/0/1', '2': 'Gi1/0/2', '3': 'Gi1/0/3'}
         and _rb.get('1.3.6.1.2.1.2.2.1.8') == {'1': 1, '2': 2, '3': 1},
         "GETBULK 2 colonnes, réponse en ordre colonne-majeur -> attribution par préfixe (pas par position)",
         str(_rb))

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
