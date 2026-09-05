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
import json
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
# Audit #07 : la topologie ne refait plus un interroger_equipement complet, elle
# consomme le cache des noms d'interface alimenté par la phase SNMP.
N._noms_interfaces = lambda ip, comm: {
    5: {'nom': 'Gi0/5', 'alias': 'PC-Comptabilite', 'ethernet': True, 'speed_mbps': 1000}}
# Sonde d'existence : _topologie_equipement commence par un GET sysDescr pour ne
# pas enchaîner vingt parcours SNMP sur un équipement muet. On la fait répondre
# « agent lisible » ici, et on vérifie plus bas qu'un agent muet coupe court.
A._snmp_presence = lambda ip, comm=('public',), port=161, timeout=1.2: (True, True, 'v1/v2c (public)')
_MAC_DEC = '.'.join(str(int('aa0000000011'[i:i + 2], 16)) for i in range(0, 12, 2))
N._snmp_walk = lambda oid, ip, comm, **k: (
    {f'1.{_MAC_DEC}': '5'} if oid == N._OID_FDB_DOT1Q_PORT else
    {'5': '5'} if oid == N._OID_FDB_BASEPORT_IF else
    {'5': '10'} if oid == N._OID_DOT1Q_PVID else {})
N._snmp_walk_octets = lambda *a, **k: {}     # table ARP / MAC infra : vide (pas de vrai reseau)
N._activite_fdb.clear(); N._activite_fdb_baseport.clear(); N._activite_fdb_dialecte.clear()
N._activite_pvid.clear()
_res = N.decouvrir_topologie(902)
_ft = _res['findings']
verifier([f['categorie'] for f in _ft] == ['cablage_incoherent'],
         "switch voit PC-VU sur port 5, la baie déclare PC-DECLARE -> cablage_incoherent",
         str([f['categorie'] for f in _ft]))
verifier(_ft and _ft[0]['details'].get('port_baie') == 5,
         "audit #03 : le constat nomme le port de FAÇADE, pas l'ifIndex brut",
         str(_ft[0]['details'] if _ft else None))
_et = N.etat_topologie(902)
verifier(_et['equipements'] and _et['equipements'][0]['ports'][0]['hotes'][0]['appareil_nom'] == 'PC-VU',
         "etat_topologie associe la MAC au bon appareil")
verifier(_et['equipements'][0].get('age_s') is not None,
         "audit #19 : etat_topologie expose la fraîcheur de la carte",
         str(_et['equipements'][0].get('age_s')))
verifier(_et['equipements'][0]['ports'][0]['est_uplink'] is False
         and _et['equipements'][0]['ports'][0]['nb_macs'] == 1,
         "audit #02 : un port à une seule MAC est un port d'accès, pas un uplink")
verifier(_et['equipements'][0]['ports'][0].get('port_alias') == 'PC-Comptabilite',
         "audit réseau 2026-09-05, #22 : ifAlias (déjà relevé par _noms_interfaces) "
         "est exposé sur le port, pas jeté",
         str(_et['equipements'][0]['ports'][0].get('port_alias')))
_c = A.get_db()
_c.execute("INSERT INTO diag_topologie (client_id, equipement_ip, equipement_appareil_id, "
           "port_index, port_nom, appareil_vu_id, appareil_vu_nom, horodatage, "
           "nb_macs_port, est_uplink) VALUES (902,'10.2.0.1',500,6,'Gi0/6',501,'PC-VU','x',1,0)")
_c.commit(); _c.close()
_ap = N.proposer_topologie_baie(902)
verifier([(p['port_numero'], p['machine_id']) for p in _ap['propositions']] == [(6, 501)],
         "audit #16 : l'aperçu propose le port 6 (vide) et rien d'autre",
         str(_ap['propositions']))
verifier(any('occupé' in i['motif'] for i in _ap['ignores']),
         "aperçu : le port 5 déjà occupé est listé comme ignoré, pas écrasé",
         str(_ap['ignores']))
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
N._snmp_walk = lambda oid, ip, comm, **k: ({'1': '95'} if oid == N._OID_UPS_OUT_LOAD else {})
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
N._fdb_switch = lambda ip, comm: ({}, {})
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

print('\n=== 20. Cartographie : ni blocage sur un agent muet, ni sur un agent lent ===')
import time as _t20

# (a) Equipement MUET : la sonde d'existence doit couper court AVANT les ~20
#     parcours SNMP secondaires. Sans elle, chacun expirait sur son delai et la
#     cartographie restait figee sur « Relevé de 1/2 » plusieurs minutes.
_appels = []
A._snmp_presence = lambda ip, comm=('public',), port=161, timeout=1.2: (False, False, 'aucune réponse SNMP')
_noms_orig, _releve_orig = N._noms_interfaces, N._releve_mac_switch
N._noms_interfaces = lambda ip, comm: (_appels.append('noms'), {})[1]
N._releve_mac_switch = lambda ip, comm, inv: (_appels.append('fdb'), ({}, {}))[1]
_r20 = N._topologie_equipement(902, 500, '10.2.0.99', ['public'], {}, 'x')
verifier(_r20[4] is True and _appels == [],
         "agent muet -> sortie immédiate, aucun relevé secondaire tenté",
         str(_appels))

# (b) La cartographie complète remonte l'équipement muet au lieu de le taire,
#     avec le POURQUOI (déjà calculé par _snmp_presence, plus jeté).
A._snmp_presence = lambda ip, comm=('public',), port=161, timeout=1.2: (True, False, 'refusé')
N._noms_interfaces, N._releve_mac_switch = _noms_orig, _releve_orig
_res20 = N.decouvrir_topologie(902, budget_s=30)
verifier(_res20.get('muets') == [{'ip': '10.2.0.1', 'detail': 'refusé'}],
         "un agent qui refuse le SNMP est remonté dans `muets` avec le motif, pas ignoré en silence",
         str(_res20.get('muets')))

# (c) Equipement LENT : le budget doit etre respecte. `as_completed` etait
#     appele SANS delai et le budget n'etait verifie qu'entre deux lots — avec
#     un seul lot il ne l'etait donc jamais, et un seul switch lent figeait tout.
A._snmp_presence = lambda ip, comm=('public',), port=161, timeout=1.2: (True, True, 'v1/v2c')
_topo_orig = N._topologie_equipement
N._topologie_equipement = lambda *a, **k: (_t20.sleep(20), ([], [], [], [], False, ''))[1]
_t_debut = _t20.time()
_res20b = N.decouvrir_topologie(902, budget_s=3)
_duree = _t20.time() - _t_debut
N._topologie_equipement = _topo_orig
verifier(_duree < 10,
         "un relevé qui traîne n'immobilise plus le job : rendu sous le budget",
         "%.1f s pour un budget de 3 s" % _duree)

# (d) La progression est emise a CHAQUE equipement, pas une fois par lot.
_vus = []
N._topologie_equipement = lambda *a, **k: ([], [], [], [], False, '')
N.decouvrir_topologie(902, _progress=lambda pct, msg: _vus.append(msg), budget_s=30)
N._topologie_equipement = _topo_orig
verifier(any('Relevé' in m for m in _vus) and _vus[-1] == 'Terminé',
         "la progression est rapportée puis close par « Terminé »", str(_vus[-3:]))

print('\n=== 21. sous_reseaux_detectes() : sous-réseaux supplémentaires via SNMP ===')
# Signalé en usage réel : des appareils sur un second /24 routé (derrière le
# même routeur) n'apparaissaient nulle part (scan, baie, diag) faute que
# quiconque pense à taper cette plage à la main. Le routeur, lui, la connaît.
verifier(N._parse_plages(' 192.168.1.0/24 ,192.168.0.0/24 ; pas-une-ip ')
         == [N.ipaddress.ip_network('192.168.1.0/24'), N.ipaddress.ip_network('192.168.0.0/24')],
         "_parse_plages : virgule/point-virgule/espace, jetons invalides ignorés")

from config_helpers import cfg_invalidate as _inv21

conn = A.get_db()
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (905, 'SousReseaux')")
conn.execute("INSERT INTO parc_general (client_id, nom_site, plage_ip_locale) VALUES (905, 'Site', '192.168.1.0/24')")
conn.execute("INSERT OR REPLACE INTO config (cle, valeur) VALUES ('diag_snmp_actif', '0')")
conn.commit(); conn.close()
_inv21()
verifier(N.sous_reseaux_detectes(905) == {'ok': False, 'motif': 'snmp_inactif', 'configurees': [], 'detectes': []},
         "SNMP désactivé -> motif explicite, aucune requête tentée")

conn = A.get_db()
conn.execute("INSERT OR REPLACE INTO config (cle, valeur) VALUES ('diag_snmp_actif', '1')")
conn.commit(); conn.close()
_inv21()
verifier(N.sous_reseaux_detectes(905)['motif'] == 'aucun_equipement',
         "aucun équipement réseau inventorié -> motif explicite")

conn = A.get_db()
conn.execute("INSERT INTO appareils (id, client_id, nom_machine, type_appareil, adresse_ip) "
             "VALUES (951, 905, 'Routeur-Site', 'Routeur/Pare-feu', '192.168.1.254')")
conn.commit(); conn.close()

# (a) l'équipement ne répond pas en SNMP -> sonde d'existence coupe court,
#     _sous_reseaux_equipement n'est JAMAIS appelée (pas de parcours inutile).
_appels_sre = []
N._sous_reseaux_equipement = lambda ip, comm: (_appels_sre.append(ip), ['192.168.0.0/24'])[1]
A._snmp_presence = lambda ip, comm=('public',), port=161, timeout=1.2: (False, False, 'aucune réponse SNMP')
_r21 = N.sous_reseaux_detectes(905)
verifier(_r21['motif'] == 'aucune_reponse_snmp' and _appels_sre == [],
         "routeur muet -> motif explicite, table IP jamais interrogée", str((_r21, _appels_sre)))

# (b) l'équipement répond : le sous-réseau déjà déclaré est exclu, un
#     sous-réseau NOUVEAU est proposé avec sa source.
A._snmp_presence = lambda ip, comm=('public',), port=161, timeout=1.2: (True, True, 'v1/v2c (public)')
N._sous_reseaux_equipement = lambda ip, comm: ['192.168.1.0/24', '192.168.0.0/24']
_r21b = N.sous_reseaux_detectes(905)
verifier(_r21b['ok'] and _r21b['configurees'] == ['192.168.1.0/24'],
         "la plage déjà déclarée est bien lue depuis parc_general", str(_r21b))
verifier([d['cidr'] for d in _r21b['detectes']] == ['192.168.0.0/24'],
         "seul le sous-réseau NON déjà déclaré est proposé (192.168.1.0/24 exclu)",
         str(_r21b['detectes']))
verifier(_r21b['detectes'][0]['sources'] == [{'nom': 'Routeur-Site', 'ip': '192.168.1.254'}],
         "la source (équipement qui l'a vu) est rapportée", str(_r21b['detectes']))

# (c) deux équipements voient le MÊME sous-réseau nouveau -> une seule entrée,
#     deux sources (pas de doublon).
conn = A.get_db()
conn.execute("INSERT INTO appareils (id, client_id, nom_machine, type_appareil, adresse_ip) "
             "VALUES (952, 905, 'SW-Coeur', 'Switch', '192.168.1.253')")
conn.commit(); conn.close()
_r21c = N.sous_reseaux_detectes(905)
verifier(len(_r21c['detectes']) == 1 and len(_r21c['detectes'][0]['sources']) == 2,
         "même sous-réseau vu par 2 équipements -> une entrée, deux sources",
         str(_r21c['detectes']))

print('\n=== 22. _ip_depuis_suffixe_arp() : les 2 formats d\'index de table ARP SNMP ===')
verifier(N._ip_depuis_suffixe_arp('5.192.168.1.50') == '192.168.1.50',
         "ipNetToMediaPhysAddress (5 composants : ifIndex.A.B.C.D)")
verifier(N._ip_depuis_suffixe_arp('5.1.4.192.168.1.51') == '192.168.1.51',
         "ipNetToPhysicalPhysAddress (7 composants : ifIndex.type.longueur.A.B.C.D)")
verifier(N._ip_depuis_suffixe_arp('5.1') == '', "suffixe trop court -> chaîne vide, pas d'exception")
verifier(N._ip_depuis_suffixe_arp('5.1.4.192.168.1.999') == '',
         "octet hors 0-255 -> rejeté plutôt qu'une IP invalide silencieuse")

print('\n=== 23. hotes_vus_snmp() : croiser les tables ARP des routeurs/switchs SNMP ===')
conn = A.get_db()
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (909, 'HotesSNMP')")
conn.execute("INSERT OR REPLACE INTO config (cle, valeur) VALUES ('diag_snmp_actif', '0')")
conn.commit(); conn.close()
_inv21()
verifier(N.hotes_vus_snmp(909) == {'ok': False, 'motif': 'snmp_inactif', 'hotes': {}},
         "SNMP désactivé -> motif explicite, aucune requête tentée")

conn = A.get_db()
conn.execute("INSERT OR REPLACE INTO config (cle, valeur) VALUES ('diag_snmp_actif', '1')")
conn.commit(); conn.close()
_inv21()
verifier(N.hotes_vus_snmp(909)['motif'] == 'aucun_equipement',
         "aucun équipement réseau inventorié -> motif explicite")

conn = A.get_db()
conn.execute("INSERT INTO appareils (id, client_id, nom_machine, type_appareil, adresse_ip) "
             "VALUES (961, 909, 'Routeur-Site', 'Routeur/Pare-feu', '192.168.1.254')")
conn.commit(); conn.close()

# (a) le routeur ne répond pas en SNMP -> sonde d'existence coupe court, sa
#     table ARP n'est JAMAIS lue (pas d'attente inutile sur un agent muet).
_appels_arp = []


def _walk_octets_espion(oid, ip, comm, **kw):
    _appels_arp.append((oid, ip))
    return {}


N._snmp_walk_octets = _walk_octets_espion
A._snmp_presence = lambda ip, comm=('public',), port=161, timeout=1.2: (False, False, 'aucune réponse SNMP')
_r22 = N.hotes_vus_snmp(909)
verifier(_r22['motif'] == 'aucune_reponse_snmp' and _appels_arp == [],
         "routeur muet -> motif explicite, table ARP jamais interrogée", str((_r22, _appels_arp)))

# (b) le routeur répond : sa table ARP (format ipNetToMediaPhysAddress, 5
#     composants) est lue et convertie en {ip: mac}, avec la source rapportée.
A._snmp_presence = lambda ip, comm=('public',), port=161, timeout=1.2: (True, True, 'v1/v2c (public)')
_mac_hex = 'aabbcc001122'
_mac_brut = bytes.fromhex(_mac_hex)
N._snmp_walk_octets = lambda oid, ip, comm, **kw: (
    {'5.192.168.0.80': _mac_brut} if oid == N._OID_ARP_PHYS else {})
A._oui_vendor = lambda mac: 'Test Vendor'
_r22b = N.hotes_vus_snmp(909)
verifier(_r22b['ok'] and _r22b['hotes'].get('192.168.0.80', {}).get('mac') == 'aa:bb:cc:00:11:22',
         "l'IP vue (sur un AUTRE sous-réseau que celui du routeur) et sa vraie MAC sont remontées",
         str(_r22b))
verifier(_r22b['hotes']['192.168.0.80']['vendor'] == 'Test Vendor',
         "le fabricant est résolu depuis la MAC (OUI)")
verifier(_r22b['hotes']['192.168.0.80']['sources'] == [{'nom': 'Routeur-Site', 'ip': '192.168.1.254'}],
         "l'équipement qui l'a vue est rapporté, pour que l'utilisateur sache où regarder")

# (c) repli sur le format ipNetToPhysicalPhysAddress (7 composants) quand le
#     premier format ne renvoie rien (agent IP-MIB moderne).
N._snmp_walk_octets = lambda oid, ip, comm, **kw: (
    {} if oid == N._OID_ARP_PHYS else
    {'5.1.4.192.168.0.81': _mac_brut} if oid == N._OID_ARP_PHYS_2 else {})
_r22c = N.hotes_vus_snmp(909)
verifier(_r22c['hotes'].get('192.168.0.81', {}).get('mac') == 'aa:bb:cc:00:11:22',
         "repli sur ipNetToPhysicalPhysAddress si ipNetToMediaPhysAddress est vide")

# (d) deux équipements voient la MÊME IP -> une seule entrée, deux sources
#     (pas de doublon, même si les deux tables ARP se recoupent).
conn = A.get_db()
conn.execute("INSERT INTO appareils (id, client_id, nom_machine, type_appareil, adresse_ip) "
             "VALUES (962, 909, 'SW-Coeur', 'Switch', '192.168.1.253')")
conn.commit(); conn.close()
N._snmp_walk_octets = lambda oid, ip, comm, **kw: (
    {'5.192.168.0.80': _mac_brut} if oid == N._OID_ARP_PHYS else {})
_r22d = N.hotes_vus_snmp(909)
verifier(len(_r22d['hotes']) == 1 and len(_r22d['hotes']['192.168.0.80']['sources']) == 2,
         "même IP vue par 2 équipements -> une entrée, deux sources", str(_r22d['hotes']))

print('\n=== 24. detecter_dhcp_pirate() : DHCPDISCOVER actif + réponses simulées ===')
# Audit réseau 2026-09-05, #19 : cette fonction n'était vérifiée par AUCUN
# test — ce qui a directement permis à un bug de passer inaperçu (corrigé
# dans cette même session) : `_mac_locale()` (l'adresse MAC de CE poste)
# était écrasée par une fonction homonyme incompatible définie plus bas
# dans network_diag.py (`_mac_locale(mac)`, un test "MAC localement
# administrée ?" sans rapport), si bien que `detecter_dhcp_pirate` levait
# une TypeError avant même d'ouvrir la socket, absorbée en silence par le
# `except Exception` englobant — ce détecteur n'envoyait donc JAMAIS le
# moindre DHCPDISCOVER. Renommée en `_mac_locale_poste()`.


class _FauxSocketDHCP:
    """bind() réussit toujours (aucun privilège nécessaire dans un test) ;
    sendto() capture le paquet envoyé (pour vérifier sa construction et en
    extraire le xid, nécessaire pour fabriquer des DHCPOFFER cohérents) ;
    recvfrom() sert les réponses programmées puis lève socket.timeout."""
    def __init__(self, serveurs, capture):
        self._serveurs = list(serveurs)
        self._capture = capture
        self.xid = b''

    def setsockopt(self, *a, **k):
        pass

    def bind(self, addr):
        self._capture['bind'] = addr

    def settimeout(self, t):
        pass

    def sendto(self, data, addr):
        self.xid = data[4:8]
        self._capture['paquet'] = data

    def recvfrom(self, n):
        if not self._serveurs:
            raise _sock.timeout()
        srv = self._serveurs.pop(0)
        return _fausse_offre(self.xid, srv), (srv, 67)

    def close(self):
        pass


def _fausse_offre(xid, serveur_ip):
    # op(1) htype(1) hlen(1) hops(1) + xid(4) + secs/flags(4) + ciaddr..giaddr(16)
    # + chaddr(16) + sname(64) + file(128) = 236 octets, + magic cookie(4) = 240.
    entete = bytes([2, 1, 6, 0]) + xid + bytes(4) + bytes(16) + bytes(16) + bytes(64) + bytes(128)
    magique = bytes([99, 130, 83, 99])
    ip_octets = bytes(int(x) for x in serveur_ip.split('.'))
    options = bytes([53, 1, 2]) + bytes([54, 4]) + ip_octets + bytes([255])
    return entete + magique + options


# (a) Cas nominal : la fonction atteint bien l'envoi RÉEL du DHCPDISCOVER —
# preuve directe que _mac_locale_poste()/_construire_dhcp_discover n'ont pas
# levé d'exception avant même d'ouvrir la socket.
_capture24 = {}
_socket_orig = N.socket.socket
N.socket.socket = lambda *a, **k: _FauxSocketDHCP(['192.168.1.1', '192.168.1.99'], _capture24)
try:
    _f24 = N.detecter_dhcp_pirate(['192.168.1.1'])
finally:
    N.socket.socket = _socket_orig
verifier('bind' in _capture24,
         "detecter_dhcp_pirate atteint l'envoi réel du DHCPDISCOVER (_mac_locale_poste() ne lève plus d'exception)")
verifier(_capture24.get('paquet', b'')[236:240] == bytes([99, 130, 83, 99]),
         "le paquet envoyé porte le bon magic cookie DHCP", str(_capture24.get('paquet', b'')[:10]))
verifier(any(f['categorie'] == 'dhcp_pirate' for f in _f24),
         "un serveur non déclaré (192.168.1.99) parmi les réponses -> dhcp_pirate", str(_f24))

# (b) Tous les serveurs sont attendus -> aucune alerte.
_capture24b = {}
N.socket.socket = lambda *a, **k: _FauxSocketDHCP(['192.168.1.1'], _capture24b)
try:
    _f24b = N.detecter_dhcp_pirate(['192.168.1.1'])
finally:
    N.socket.socket = _socket_orig
verifier(_f24b == [], "seul le serveur attendu répond -> aucune alerte", str(_f24b))

print('\n=== 25. _mac_locale_poste() / _dhcp_server_id() : fonctions pures du palier 1 DHCP ===')
verifier(len(N._mac_locale_poste()) == 6, "_mac_locale_poste() renvoie 6 octets (une adresse MAC)")
_xid = b'\x01\x02\x03\x04'
_offre = _fausse_offre(_xid, '10.0.0.1')
verifier(N._dhcp_server_id(_offre, _xid) == '10.0.0.1',
         "_dhcp_server_id extrait la bonne IP d'un DHCPOFFER (option 54)")
verifier(N._dhcp_server_id(_offre, b'\x99\x99\x99\x99') == '',
         "un xid différent (réponse à une AUTRE requête) est rejeté")
_pas_offre = bytearray(_offre)
_pas_offre[240 + 2] = 1   # option 53 (message-type) = 1 -> DHCPDISCOVER, pas OFFER
verifier(N._dhcp_server_id(bytes(_pas_offre), _xid) == '',
         "un message-type != OFFER (2) ne renvoie pas de serveur")

print('\n=== 26. detecter_conflits_noms() : deux IP annoncent le même nom NetBIOS ===')
conn = A.get_db()
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (910, 'ConflitsNoms')")
conn.execute("INSERT INTO appareils (id, client_id, nom_machine, adresse_ip) "
             "VALUES (971, 910, 'PC-A', '192.168.1.31')")
conn.execute("INSERT INTO appareils (id, client_id, nom_machine, adresse_ip) "
             "VALUES (972, 910, 'PC-B', '192.168.1.32')")
conn.execute("INSERT INTO appareils (id, client_id, nom_machine, adresse_ip) "
             "VALUES (973, 910, 'PC-C', '192.168.1.33')")
conn.commit(); conn.close()
_netbios_name_orig = A._netbios_name
_netbios_par_ip = {'192.168.1.31': 'DOUBLON', '192.168.1.32': 'DOUBLON', '192.168.1.33': 'UNIQUE'}
A._netbios_name = lambda ip: _netbios_par_ip.get(ip, '')
try:
    _f26 = N.detecter_conflits_noms(910)
finally:
    A._netbios_name = _netbios_name_orig
verifier(len(_f26) == 1 and _f26[0]['categorie'] == 'conflit_nom',
         "2 IP annonçant le même nom NetBIOS -> conflit_nom", str(_f26))
verifier(sorted(_f26[0]['details']['ips']) == ['192.168.1.31', '192.168.1.32'],
         "les 2 bonnes IP sont rapportées (pas la 3e, nom différent)", str(_f26[0]['details']))

print('\n=== 27. _stp_switch() : sens des liens par STP (racine, port racine, pont amont) ===')
# Audit réseau 2026-09-05, #18 : cette fonction (constat d'audit #12 de
# 2.19.32 — donner le SENS des liens même sans LLDP) n'était vérifiée par
# aucun test.
_root_id = bytes([0x80, 0x00]) + bytes.fromhex('aabbccddeeff')       # racine du spanning tree
_design_id_p5 = bytes([0x80, 0x00]) + bytes.fromhex('112233445566')  # pont amont vu par le port 5
N._snmp_walk = lambda oid, ip, comm, **k: (
    {'5': '5'} if oid == N._OID_STP_PORT_STATE else     # baseport 5 : état 5 = passant
    {'0': '5'} if oid == N._OID_STP_ROOT_PORT else       # le port racine EST le baseport 5
    {})
N._snmp_walk_octets = lambda oid, ip, comm, **k: (
    {'5': _design_id_p5} if oid == N._OID_STP_DESIGN_BR else
    {'0': _root_id} if oid == N._OID_STP_DESIGN_ROOT else
    {})
_ports_stp, _meta_stp = N._stp_switch('10.9.0.1', ['public'], {'5': '10'})
verifier(10 in _ports_stp, "le baseport 5 est bien résolu vers son ifIndex de façade (10)", str(_ports_stp))
verifier(_ports_stp.get(10, {}).get('etat') == 'passant', "état STP décodé (5 -> passant)", str(_ports_stp))
verifier(_ports_stp.get(10, {}).get('amont') is True,
         "le port racine (baseport 5 == port_racine) est marqué 'amont'", str(_ports_stp))
verifier(_ports_stp.get(10, {}).get('pont_amont') == N._mac_octets(_design_id_p5[2:]),
         "la MAC du pont amont (voisin) est décodée depuis dot1dStpPortDesignatedBridge",
         str(_ports_stp))
verifier(_meta_stp.get('racine') == N._mac_octets(_root_id[2:]) and _meta_stp.get('port_racine') == 10,
         "la racine du spanning tree et le port qui y mène sont exposés", str(_meta_stp))

print('\n=== 27bis. _stp_switch() : équipement qui ne fait pas de STP -> ({}, {}) ===')
N._snmp_walk = lambda oid, ip, comm, **k: {}
N._snmp_walk_octets = lambda oid, ip, comm, **k: {}
verifier(N._stp_switch('10.9.0.2', ['public'], {}) == ({}, {}),
         "aucune table STP exposée -> résultat vide, pas d'exception")


print('\n=== 28. decouvrir_topologie() : découverte récursive + plafond de profondeur ===')
# Audit réseau 2026-09-05, #18 : la découverte récursive (constat d'audit #10
# de 2.19.32) et son plafond _TOPO_PROFONDEUR_MAX n'étaient testés que de
# façon indirecte (topologie à un seul saut, section 10 ci-dessus). Ici,
# _topologie_equipement est directement remplacée par un faux relevé qui
# rapporte TOUJOURS un nouveau voisin jamais vu — si le plafond de profondeur
# ne fonctionnait pas, la boucle ne s'arrêterait qu'à _TOPO_EQUIP_MAX (40).
conn = A.get_db()
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (912, 'TopoRecursive')")
conn.execute("INSERT INTO appareils (id, client_id, nom_machine, type_appareil, adresse_ip) "
             "VALUES (981, 912, 'SW-Racine', 'Switch', '10.6.0.1')")
for k in ('diag_snmp_actif', 'diag_topologie_active'):
    conn.execute("INSERT OR REPLACE INTO config (cle, valeur) VALUES (?, '1')", (k,))
conn.commit(); conn.close()
from config_helpers import cfg_invalidate as _inv27
_inv27()

_compteur_topo = [0]


def _faux_topologie_equipement(client_id, aid, ip, communautes, inventaire, now, deadline):
    _compteur_topo[0] += 1
    nouvel_ip = f'10.6.0.{100 + _compteur_topo[0]}'
    voisins = [(nouvel_ip, f'SW-decouvert-{_compteur_topo[0]}', 'Modele-X')]
    return ([], [], voisins, [], False, '')


_topologie_equipement_orig = N._topologie_equipement
N._topologie_equipement = _faux_topologie_equipement
try:
    _res28 = N.decouvrir_topologie(912)
finally:
    N._topologie_equipement = _topologie_equipement_orig

verifier(_compteur_topo[0] == N._TOPO_PROFONDEUR_MAX + 1,
         f"la récursion s'arrête après {N._TOPO_PROFONDEUR_MAX} sauts (profondeurs 0 à "
         f"{N._TOPO_PROFONDEUR_MAX}), pas avant _TOPO_EQUIP_MAX (40)",
         f"{_compteur_topo[0]} équipements relevés")
verifier(len(_res28['decouverts']) == N._TOPO_PROFONDEUR_MAX,
         "chaque saut sauf le dernier découvre et met en file un nouveau switch hors inventaire",
         str(_res28['decouverts']))
verifier(all(d['ip'].startswith('10.6.0.1') for d in _res28['decouverts']),
         "les switches découverts sont bien ceux rapportés par le faux relevé", str(_res28['decouverts']))

print('\n=== 29. _wifi_linux() : analyseur de sortie `iw` (audit réseau 2026-09-05, #20) ===')
# Seul _wifi_windows était testé jusqu'ici ; un déploiement Linux pouvait donc
# avoir un diagnostic Wi-Fi cassé sans qu'aucun test ne le révèle.
_SORTIE_IW_DEV = """phy#0
\tInterface wlan0
\t\tifindex 3
\t\taddr aa:bb:cc:dd:ee:ff
\t\ttype managed
"""
_SORTIE_IW_LINK_CONNECTE = """Connected to a1:b2:c3:d4:e5:f6 (on wlan0)
\tSSID: MonReseauWifi
\tfreq: 5180
\tRX: 1234 bytes (10 packets)
\tTX: 5678 bytes (20 packets)
\tsignal: -55 dBm
\trx bitrate: 866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2
"""
_SORTIE_IW_LINK_DECONNECTE = "Not connected.\n"
_SORTIE_IW_SCAN = """BSS aa:11:22:33:44:55(on wlan0)
\tsignal: -60.00 dBm
\tSSID: VoisinWifi
\tDS Parameter set: channel 6
BSS bb:11:22:33:44:66(on wlan0)
\tsignal: -70.00 dBm
\tSSID: AutreVoisin
\tprimary channel: 44
"""


def _run_iw(cmd, timeout=6):
    if cmd == ['iw', 'dev']:
        return _FakeProc(_SORTIE_IW_DEV)
    if cmd == ['iw', 'dev', 'wlan0', 'link']:
        return _FakeProc(_etat_link[0])
    if cmd == ['iw', 'dev', 'wlan0', 'scan']:
        return _FakeProc(_SORTIE_IW_SCAN)
    raise AssertionError(f"commande iw inattendue : {cmd}")


_etat_link = [_SORTIE_IW_LINK_CONNECTE]
N._run = _run_iw
try:
    _wifi_lin = N._wifi_linux()
finally:
    N._run = _run_orig
verifier(_wifi_lin['connecte'] is True and _wifi_lin['ssid'] == 'MonReseauWifi',
         "interface + SSID extraits de 'iw dev' / 'iw dev wlan0 link'", str(_wifi_lin))
verifier(_wifi_lin['bssid'] == 'a1:b2:c3:d4:e5:f6', "BSSID extrait", str(_wifi_lin.get('bssid')))
verifier(_wifi_lin['rssi_dbm'] == -55, "signal (dBm) extrait", str(_wifi_lin.get('rssi_dbm')))
verifier(_wifi_lin['canal'] == 36 and _wifi_lin['bande'] == N._canal_vers_bande(36),
         "fréquence (5180 MHz) convertie en canal (36) puis en bande", str(_wifi_lin))
verifier(_wifi_lin['debit_mbps'] == 866, "débit rx extrait (866.7 -> 866)", str(_wifi_lin.get('debit_mbps')))
verifier(len(_wifi_lin['aps']) == 2 and {a['ssid'] for a in _wifi_lin['aps']} == {'VoisinWifi', 'AutreVoisin'},
         "2 BSS voisins extraits du scan ('DS Parameter set' ET 'primary channel')", str(_wifi_lin['aps']))
verifier(next(a for a in _wifi_lin['aps'] if a['ssid'] == 'VoisinWifi')['canal'] == 6,
         "canal du 1er voisin (DS Parameter set: channel 6)")
verifier(next(a for a in _wifi_lin['aps'] if a['ssid'] == 'AutreVoisin')['canal'] == 44,
         "canal du 2e voisin (primary channel: 44)")

print('\n=== 29bis. _wifi_linux() : non connecté / aucun adaptateur ===')
_etat_link[0] = _SORTIE_IW_LINK_DECONNECTE
N._run = _run_iw
try:
    _wifi_dc = N._wifi_linux()
finally:
    N._run = _run_orig
verifier(_wifi_dc == {'connecte': False, 'motif': 'non connecté', 'aps': _wifi_dc.get('aps')},
         "'Not connected.' -> connecte=False", str(_wifi_dc))

N._run = lambda cmd, timeout=6: _FakeProc("")   # 'iw dev' sans aucune ligne Interface
try:
    _wifi_sans_carte = N._wifi_linux()
finally:
    N._run = _run_orig
verifier(_wifi_sans_carte == {'connecte': False, 'motif': 'aucun adaptateur Wi-Fi'},
         "aucune interface Wi-Fi détectée -> motif explicite", str(_wifi_sans_carte))


print('\n=== 30. _wifi_macos() : analyseur JSON de `system_profiler` ===')
_JSON_AIRPORT_CONNECTE = json.dumps({
    "SPAirPortDataType": [{
        "spairport_airport_interfaces": [{
            "spairport_current_network_information": {
                "_name": "MonReseauWifi",
                "spairport_network_channel": "36 (5GHz, 80MHz)",
                "spairport_network_bssid": "A1:B2:C3:D4:E5:F6",
                "spairport_signal_noise": "-55 dBm / -90 dBm",
            },
            "spairport_airport_other_local_wireless_networks": [
                {"_name": "VoisinWifi", "spairport_network_channel": "6"},
            ],
        }],
    }],
})
N._run = lambda cmd, timeout=6: _FakeProc(_JSON_AIRPORT_CONNECTE)
try:
    _wifi_mac = N._wifi_macos()
finally:
    N._run = _run_orig
verifier(_wifi_mac['connecte'] is True and _wifi_mac['ssid'] == 'MonReseauWifi',
         "SSID extrait du JSON system_profiler", str(_wifi_mac))
verifier(_wifi_mac['bssid'] == 'a1:b2:c3:d4:e5:f6', "BSSID normalisé en minuscules", str(_wifi_mac.get('bssid')))
verifier(_wifi_mac['canal'] == 36, "canal extrait du premier nombre de spairport_network_channel",
         str(_wifi_mac.get('canal')))
verifier(_wifi_mac['rssi_dbm'] == -55, "signal (premier nombre de spairport_signal_noise)",
         str(_wifi_mac.get('rssi_dbm')))
verifier(len(_wifi_mac['aps']) == 1 and _wifi_mac['aps'][0]['ssid'] == 'VoisinWifi',
         "réseau voisin extrait de spairport_airport_other_local_wireless_networks",
         str(_wifi_mac['aps']))

print('\n=== 30bis. _wifi_macos() : non connecté / aucun adaptateur / JSON illisible ===')
_JSON_AIRPORT_DECONNECTE = json.dumps({
    "SPAirPortDataType": [{"spairport_airport_interfaces": [{}]}]})
N._run = lambda cmd, timeout=6: _FakeProc(_JSON_AIRPORT_DECONNECTE)
try:
    _wifi_mac_dc = N._wifi_macos()
finally:
    N._run = _run_orig
verifier(_wifi_mac_dc['connecte'] is False and _wifi_mac_dc['motif'] == 'non connecté',
         "interface présente mais sans réseau courant -> non connecté", str(_wifi_mac_dc))

_JSON_AIRPORT_SANS_CARTE = json.dumps({"SPAirPortDataType": [{"spairport_airport_interfaces": []}]})
N._run = lambda cmd, timeout=6: _FakeProc(_JSON_AIRPORT_SANS_CARTE)
try:
    _wifi_mac_sc = N._wifi_macos()
finally:
    N._run = _run_orig
verifier(_wifi_mac_sc == {'connecte': False, 'motif': 'aucun adaptateur Wi-Fi'},
         "aucune interface -> motif explicite", str(_wifi_mac_sc))

N._run = lambda cmd, timeout=6: _FakeProc("ceci n'est pas du JSON")
try:
    _wifi_mac_ko = N._wifi_macos()
finally:
    N._run = _run_orig
verifier(_wifi_mac_ko == {'connecte': False, 'motif': 'lecture impossible (macOS)'},
         "sortie illisible (JSON invalide) -> motif explicite, pas d'exception", str(_wifi_mac_ko))

print('\n=== 31. reveiller_appareil() : paquet magique Wake-on-LAN (audit réseau 2026-09-05, #26) ===')
class _FauxSocketWoL:
    def __init__(self, capture):
        self._capture = capture
    def setsockopt(self, *a, **k):
        pass
    def sendto(self, data, addr):
        self._capture['data'] = data
        self._capture['addr'] = addr
    def close(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


_capture31 = {}
_socket_orig31 = N.socket.socket
N.socket.socket = lambda *a, **k: _FauxSocketWoL(_capture31)
try:
    _ok31, _motif31 = N.reveiller_appareil('AA:BB:CC:DD:EE:FF')
finally:
    N.socket.socket = _socket_orig31
verifier(_ok31 is True and not _motif31, "envoi réussi -> (True, '')", str((_ok31, _motif31)))
verifier(_capture31.get('addr') == ('255.255.255.255', 9),
         "diffusion en broadcast, port 9 (Wake-on-LAN)", str(_capture31.get('addr')))
_attendu31 = b'\xff' * 6 + bytes.fromhex('aabbccddeeff') * 16
verifier(_capture31.get('data') == _attendu31,
         "paquet magique bien formé (6 x FF suivis de 16 x la MAC)", str(_capture31.get('data')))

print('\n=== 31bis. reveiller_appareil() : MAC invalide -> échec propre, pas d\'exception ===')
verifier(N.reveiller_appareil('pas-une-mac') == (False, "adresse MAC invalide"),
         "MAC invalide détectée sans exception")
verifier(N.reveiller_appareil('') == (False, "adresse MAC invalide"),
         "MAC vide détectée sans exception")

print('\n=== 32. inventaire_host_resources() : HOST-RESOURCES-MIB (audit réseau 2026-09-05, #24) ===')
N._activite_hostres = {}
N._snmp_walk = lambda oid, ip, comm, **k: (
    {'1': 'nginx', '2': 'OpenSSH', '3': ''} if oid == N._OID_HR_SW_INST_NAME else
    {'1': 'sshd', '2': 'nginx'} if oid == N._OID_HR_SW_RUN_NAME else
    {'1': '/', '2': 'Physical memory'} if oid == N._OID_HR_STORAGE_DESCR else
    {'1': '4096', '2': '1'} if oid == N._OID_HR_STORAGE_ALLOC else
    {'1': '1000000', '2': '2000000'} if oid == N._OID_HR_STORAGE_SIZE else
    {'1': '400000', '2': '900000'} if oid == N._OID_HR_STORAGE_USED else
    {})
_hr = N.inventaire_host_resources('10.9.0.9', ['public'])
verifier(_hr.get('logiciels') == ['OpenSSH', 'nginx'],
         "logiciels installés triés, chaînes vides écartées", str(_hr.get('logiciels')))
verifier(_hr.get('processus') == ['nginx', 'sshd'],
         "processus en cours triés", str(_hr.get('processus')))
_disque = next((s for s in _hr.get('stockage', []) if s['descr'] == '/'), None)
verifier(_disque is not None and _disque['taille_mo'] == round(1000000 * 4096 / 1_048_576, 1),
         "taille disque convertie en Mo (unités d'allocation x taille)", str(_disque))
verifier(_disque is not None and _disque['utilise_mo'] == round(400000 * 4096 / 1_048_576, 1),
         "utilisé disque converti en Mo", str(_disque))

print('\n=== 32bis. inventaire_host_resources() : agent sans HOST-RESOURCES-MIB (switch réseau) -> {} ===')
N._activite_hostres = {}
N._snmp_walk = lambda oid, ip, comm, **k: {}
verifier(N.inventaire_host_resources('10.9.0.10', ['public']) == {},
         "aucun hrSWInstalledName -> {} propre, pas d'exception")

print('\n=== 32ter. inventaire_host_resources() : cache (2e appel ne re-sonde pas) ===')
N._activite_hostres = {}
_appels = [0]


def _walk_espion(oid, ip, comm, **k):
    _appels[0] += 1
    return {'1': 'ToolX'} if oid == N._OID_HR_SW_INST_NAME else {}


N._snmp_walk = _walk_espion
N.inventaire_host_resources('10.9.0.11', ['public'])
_appels_apres_1er = _appels[0]
N.inventaire_host_resources('10.9.0.11', ['public'])
verifier(_appels[0] == _appels_apres_1er, "2e appel servi depuis le cache (aucun nouveau relevé SNMP)")

print('\n=== 33. _snmp_trap_parse() : décodage d\'un trap SNMP v1/v2c (audit réseau 2026-09-05, #27) ===')


def _construire_trap_v1(community='public', enterprise='1.3.6.1.4.1.9', agent_ip=(10, 0, 0, 1),
                        generic=3, specific=0, varbinds=()):
    vb = b''.join(A._ber_sequence(0x30, A._ber_oid(o) + A._ber_chaine(v)) for o, v in varbinds)
    pdu_corps = (A._ber_oid(enterprise)
                + bytes([0x40, 4]) + bytes(agent_ip)
                + A._ber_entier(generic) + A._ber_entier(specific)
                + bytes([0x43, 1, 0])
                + A._ber_sequence(0x30, vb))
    pdu = A._ber_sequence(0xa4, pdu_corps)
    return A._ber_sequence(0x30, A._ber_entier(0) + A._ber_chaine(community) + pdu)


def _construire_trap_v2c(community='public', trap_oid='1.3.6.1.6.3.1.1.5.3', varbinds_extra=()):
    vb_sysuptime = A._ber_sequence(0x30, A._ber_oid('1.3.6.1.2.1.1.3.0') + bytes([0x43, 1, 5]))
    vb_trapoid = A._ber_sequence(0x30, A._ber_oid('1.3.6.1.6.3.1.1.4.1.0') + A._ber_oid(trap_oid))
    vb_extra = b''.join(A._ber_sequence(0x30, A._ber_oid(o) + A._ber_chaine(v)) for o, v in varbinds_extra)
    pdu_corps = (A._ber_entier(1234) + A._ber_entier(0) + A._ber_entier(0)
                + A._ber_sequence(0x30, vb_sysuptime + vb_trapoid + vb_extra))
    pdu = A._ber_sequence(0xa7, pdu_corps)
    return A._ber_sequence(0x30, A._ber_entier(1) + A._ber_chaine(community) + pdu)


_paquet_v1 = _construire_trap_v1(generic=2, varbinds=[('1.3.6.1.2.1.2.2.1.1.5', 'ifIndex5')])
_t1 = A._snmp_trap_parse(_paquet_v1)
verifier(_t1 is not None and _t1['type'] == 'v1', "trap v1 reconnu", str(_t1))
verifier(_t1['generic_trap'] == 2 and _t1['generic_trap_libelle'] == 'linkDown',
         "generic-trap décodé et son libellé associé", str(_t1))
verifier(_t1['agent_adresse'] == '10.0.0.1', "adresse de l'agent (IpAddress BER) décodée", str(_t1))
verifier(_t1['varbinds'] == [('1.3.6.1.2.1.2.2.1.1.5', 'ifIndex5')],
         "varbind-list décodée (OID + valeur)", str(_t1['varbinds']))

_paquet_v2c = _construire_trap_v2c(trap_oid='1.3.6.1.6.3.1.1.5.4')
_t2 = A._snmp_trap_parse(_paquet_v2c)
verifier(_t2 is not None and _t2['type'] == 'v2c', "trap v2c reconnu", str(_t2))
verifier(_t2['trap_oid'] == '1.3.6.1.6.3.1.1.5.4',
         "snmpTrapOID (2e varbind, convention SNMPv2) exposé comme trap_oid", str(_t2))
verifier(_t2['communaute'] == 'public', "communauté décodée")

verifier(A._snmp_trap_parse(b'\x30\x03\x02\x01\x03') == {'version': 3, 'chiffre': True, 'categorie_pdu': None},
         "version 3 (chiffrement potentiel non vérifié ici) -> signalé explicitement, pas décodé à tort")
verifier(A._snmp_trap_parse(b'') is None, "paquet vide -> None, pas d'exception")
verifier(A._snmp_trap_parse(b'\xff\xff\xff') is None, "paquet n'importe quoi -> None, pas d'exception")

print('\n=== 33bis. _snmp_trap_parse() : PDU d\'un autre type (ex. une réponse GET) ignorée ===')
_get_response = A._ber_sequence(0x30, A._ber_entier(1) + A._ber_chaine('public')
                                + A._ber_sequence(0xa2, A._ber_entier(1) + A._ber_entier(0)
                                                  + A._ber_entier(0) + A._ber_sequence(0x30, b'')))
verifier(A._snmp_trap_parse(_get_response) is None,
         "PDU 0xa2 (GetResponse) n'est ni un Trap-PDU ni un SNMPv2-Trap-PDU -> None")

print('\n=== 34. _trap_vers_finding() / _client_pour_ip_trap() : rattachement au bon client ===')
conn = A.get_db()
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (913, 'ClientTraps')")
conn.execute("INSERT INTO appareils (id, client_id, nom_machine, type_appareil, adresse_ip) "
             "VALUES (982, 913, 'Switch-Trap', 'Switch', '10.7.0.5')")
conn.commit()
_cid_trouve = N._client_pour_ip_trap(conn, '10.7.0.5')
_cid_absent = N._client_pour_ip_trap(conn, '10.7.0.99')
conn.close()
verifier(_cid_trouve == 913, "IP source du trap retrouvée dans l'inventaire -> bon client_id", str(_cid_trouve))
verifier(_cid_absent is None, "IP source inconnue de tout inventaire -> None (pas d'évènement orphelin)")

_f_v1 = N._trap_vers_finding(_t1, '10.7.0.5')
verifier(_f_v1['categorie'] == 'trap_snmp' and _f_v1['gravite'] == 'avertissement',
         "finding v1 catégorisé 'trap_snmp', gravité par défaut appliquée", str(_f_v1))
verifier('linkDown' in _f_v1['titre'] and '10.7.0.5' in _f_v1['titre'],
         "le titre nomme le type de trap et l'IP source", _f_v1['titre'])

_f_v2 = N._trap_vers_finding(_t2, '10.7.0.5')
verifier(_f_v2['details']['trap_oid'] == '1.3.6.1.6.3.1.1.5.4',
         "finding v2c porte le trap_oid dans ses détails", str(_f_v2['details']))

_nouveaux = N._enregistrer_evenements(913, [_f_v1], 'trap')
verifier(_nouveaux == 1, "le finding trap s'enregistre normalement dans diag_reseau_evenements")
conn = A.get_db()
_ligne = conn.execute("SELECT categorie, titre FROM diag_reseau_evenements WHERE client_id=913 "
                      "AND categorie='trap_snmp'").fetchone()
conn.close()
verifier(_ligne is not None, "l'évènement trap est bien retrouvable en base pour ce client", str(_ligne))

print('\n=== 35. _ndp_linux() / _ndp_windows() / _ndp_macos() : lecture du cache NDP (audit #28) ===')
_SORTIE_IP_NEIGH = """fe80::1 dev eth0 lladdr aa:bb:cc:00:04:01 router REACHABLE
2001:db8::10 dev eth0 lladdr aa:bb:cc:00:04:02 STALE
ff02::1:ff00:1 dev eth0 lladdr 33:33:ff:00:00:01 STALE
"""
N._run = lambda cmd, timeout=6: _FakeProc(_SORTIE_IP_NEIGH)
try:
    _ndp_lin = N._ndp_linux()
finally:
    N._run = _run_orig
verifier(len(_ndp_lin) == 3, "3 lignes de voisinage NDP extraites de 'ip -6 neighbor show'", str(_ndp_lin))
verifier(_ndp_lin[0] == {'ip': 'fe80::1', 'interface': 'eth0', 'mac': 'aa:bb:cc:00:04:01', 'etat': 'router'},
         "1re entrée (lien-local, routeur) décodée", str(_ndp_lin[0]))
verifier(_ndp_lin[1]['ip'] == '2001:db8::10' and _ndp_lin[1]['etat'] == 'STALE',
         "2e entrée (adresse globale) décodée", str(_ndp_lin[1]))

_SORTIE_NETSH_NDP = """Interface 12: Wi-Fi

Internet Address                              Physical Address   Type
-------------------------------------------  -----------------  -----------
fe80::1                                        aa-bb-cc-00-04-03   Router (Reachable)
2001:db8::20                                   aa-bb-cc-00-04-04   Stale
"""
N._run = lambda cmd, timeout=8: _FakeProc(_SORTIE_NETSH_NDP)
try:
    _ndp_win = N._ndp_windows()
finally:
    N._run = _run_orig
verifier(len(_ndp_win) == 2, "2 lignes extraites de 'netsh interface ipv6 show neighbors'", str(_ndp_win))
verifier(_ndp_win[0]['ip'] == 'fe80::1' and _ndp_win[0]['mac'] == 'aa:bb:cc:00:04:03'
          and _ndp_win[0]['interface'] == 'Wi-Fi',
          "adresse + MAC (tirets normalisés en deux-points) + interface associée", str(_ndp_win[0]))

_SORTIE_NDP_AN = """Neighbor                             Linklayer Address  Netif Expire    S Flags
fe80::1%en0                          aa:bb:cc:00:04:05  en0   23h59m59s R
2001:db8::30                         aa:bb:cc:00:04:06  en0   permanent    S
"""
N._run = lambda cmd, timeout=6: _FakeProc(_SORTIE_NDP_AN)
try:
    _ndp_mac = N._ndp_macos()
finally:
    N._run = _run_orig
verifier(len(_ndp_mac) == 2, "2 lignes extraites de 'ndp -an'", str(_ndp_mac))
verifier(_ndp_mac[0]['ip'] == 'fe80::1' and _ndp_mac[0]['mac'] == 'aa:bb:cc:00:04:05',
         "suffixe de zone (%en0) retiré de l'adresse lien-local", str(_ndp_mac[0]))

print('\n=== 35bis. voisinage_ipv6() : ping multicast + filtrage du bruit multicast ===')
# _ndp_linux() est forcée (IS_WINDOWS=False + platform.system() -> 'Linux')
# plutôt que de dépendre de l'OS réel qui exécute ce test (Windows/Linux/macOS
# indifféremment) — les 3 fonctions _ndp_* sont déjà testées individuellement
# ci-dessus, indépendamment de l'OS réel ; seul le dispatch + le ping + le
# filtrage sont couverts ici.
_is_windows_orig = N.IS_WINDOWS
_platform_system_orig = N.platform.system
N.IS_WINDOWS = False
N.platform.system = lambda: 'Linux'
_appels_ping = []


def _run_avec_ping(cmd, timeout=6):
    if cmd[0] == 'ping':
        _appels_ping.append(cmd)
        return _FakeProc('')
    return _FakeProc(_SORTIE_IP_NEIGH)


N._run = _run_avec_ping
try:
    _v6 = N.voisinage_ipv6(rafraichir=True)
finally:
    N._run = _run_orig
    N.IS_WINDOWS = _is_windows_orig
    N.platform.system = _platform_system_orig
verifier(len(_appels_ping) == 1 and 'ff02::1' in _appels_ping[0],
         "un ping multicast vers ff02::1 est tenté avant la lecture du cache", str(_appels_ping))
verifier(_v6['ping_multicast_ok'] is True, "ping réussi (returncode 0) -> ping_multicast_ok=True")
verifier(_v6['nb'] == 2 and all(not v['ip'].startswith('ff') for v in _v6['voisins']),
         "l'entrée multicast (ff02::1:ff00:1) est filtrée, les 2 vraies entrées restent",
         str(_v6['voisins']))

print('\n=== 35ter. voisinage_ipv6(rafraichir=False) : pas de ping tenté ===')
N.IS_WINDOWS = False
N.platform.system = lambda: 'Linux'
_appels_ping2 = []
N._run = lambda cmd, timeout=6: (_appels_ping2.append(cmd) or _FakeProc('')) if cmd[0] == 'ping' else _FakeProc('')
try:
    _v6b = N.voisinage_ipv6(rafraichir=False)
finally:
    N._run = _run_orig
    N.IS_WINDOWS = _is_windows_orig
    N.platform.system = _platform_system_orig
verifier(_appels_ping2 == [], "rafraichir=False -> aucun ping multicast envoyé", str(_appels_ping2))
verifier(_v6b['ping_multicast_ok'] is None, "ping_multicast_ok=None quand aucun ping n'a été tenté")

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
