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

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
