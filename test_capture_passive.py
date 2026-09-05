#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit réseau 2026-09-05, constat #17 : `network_diag.capture_passive`
(palier 2, écoute passive de trames) n'était vérifiée par AUCUN test alors
qu'elle alimente à elle seule 6 catégories de détection (arp_spoofing,
tempete_broadcast, mac_flapping, dhcp_pirate, ra_pirate, stp_instable,
tcp_retransmissions) dans un unique callback scapy opaque.

Principe : `_charger_scapy()` est remplacée par un faux module scapy minimal
(classes de repère + un `AsyncSniffer` qui rejoue une liste de faux paquets
de façon synchrone dans `.start()`) — aucune dépendance à scapy ni au réseau
réel n'est nécessaire pour exercer le callback `_on(pkt)`.

Usage :
    python test_capture_passive.py
"""
import io
import os
import sys
import tempfile
import time as _time_mod
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='capture_passive_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import network_diag as N  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


# ── faux scapy minimal ──────────────────────────────────────────────────────
class _Couche:
    """Un simple sac d'attributs représentant une couche de paquet."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Ether(_Couche): pass
class ARP(_Couche): pass
class DHCP(_Couche): pass
class IPv6(_Couche): pass
class ICMPv6ND_RA(_Couche): pass
class STP(_Couche): pass
class TCP(_Couche): pass
class IP(_Couche): pass
class Raw(_Couche): pass


class FauxPaquet:
    """`pkt.haslayer(X)` / `pkt[X]` sur des instances de couches passées
    positionnellement — le type de chaque instance EST la clé (comme un vrai
    paquet scapy empile des couches typées)."""
    def __init__(self, *couches):
        self._c = {type(c): c for c in couches}

    def haslayer(self, cls):
        return cls in self._c

    def __getitem__(self, cls):
        return self._c[cls]


class FauxAsyncSniffer:
    """Rejoue `PAQUETS_A_REJOUER` (liste module-level, fixée avant chaque
    scénario) de façon SYNCHRONE dans `.start()` — pas de vrai thread, pas de
    vrai réseau. `.stop()` ne fait rien."""
    _paquets = []

    def __init__(self, prn=None, store=False, filter=None):
        self._prn = prn

    def start(self):
        for pkt in FauxAsyncSniffer._paquets:
            self._prn(pkt)

    def stop(self):
        pass


FAUX_SCAPY = types.SimpleNamespace(
    Ether=Ether, ARP=ARP, DHCP=DHCP, IPv6=IPv6, ICMPv6ND_RA=ICMPv6ND_RA,
    STP=STP, TCP=TCP, IP=IP, Raw=Raw, AsyncSniffer=FauxAsyncSniffer,
)

_charger_scapy_orig = N._charger_scapy
_sleep_orig = N.time.sleep


def _capturer(paquets, seuils=None):
    """Lance `capture_passive` avec les faux paquets injectés, sans jamais
    dormir réellement (`duree` est ignoré ici : le faux sniffer est
    synchrone)."""
    FauxAsyncSniffer._paquets = paquets
    N._charger_scapy = lambda: FAUX_SCAPY
    N.time.sleep = lambda s: None
    try:
        return N.capture_passive(3, seuils or {})
    finally:
        N._charger_scapy = _charger_scapy_orig
        N.time.sleep = _sleep_orig


def _cat(findings):
    return sorted(f['categorie'] for f in findings)


print('=== 1. Rien de suspect -> aucun constat ===')
paquets = [FauxPaquet(Ether(dst='aa:bb:cc:dd:ee:01')) for _ in range(3)]
f = _capturer(paquets, seuils={'broadcast_pps': 150})
verifier(f == [], "trafic calme -> liste vide", str(_cat(f)))


print('\n=== 2. arp_spoofing : une IP change de MAC EN COURS de capture ===')
paquets = [
    FauxPaquet(ARP(psrc='192.168.1.50', pdst='192.168.1.1', hwsrc='aa:bb:cc:00:00:01', op=1)),
    FauxPaquet(ARP(psrc='192.168.1.50', pdst='192.168.1.1', hwsrc='aa:bb:cc:00:00:02', op=1)),
]
f = _capturer(paquets)
verifier('arp_spoofing' in _cat(f), "changement de MAC pour la même IP -> arp_spoofing", str(_cat(f)))
spoof = next((x for x in f if x['categorie'] == 'arp_spoofing'), None)
verifier(spoof is not None and spoof['details']['mac_avant'] == 'aa:bb:cc:00:00:01'
         and spoof['details']['mac_apres'] == 'aa:bb:cc:00:00:02',
         "l'ancienne ET la nouvelle MAC sont rapportées", str(spoof and spoof['details']))


print('\n=== 3. arp_spoofing (ARP gratuits en rafale, >= 8) ===')
paquets = [FauxPaquet(ARP(psrc='192.168.1.77', pdst='192.168.1.77',
                          hwsrc='aa:bb:cc:00:00:09', op=2)) for _ in range(9)]
f = _capturer(paquets)
verifier('arp_spoofing' in _cat(f), "9 ARP gratuits pour la même IP -> arp_spoofing", str(_cat(f)))

print('\n=== 3bis. Sous le seuil (< 8) -> aucune alerte ===')
paquets = [FauxPaquet(ARP(psrc='192.168.1.78', pdst='192.168.1.78',
                          hwsrc='aa:bb:cc:00:00:0a', op=2)) for _ in range(5)]
f = _capturer(paquets)
verifier('arp_spoofing' not in _cat(f), "5 ARP gratuits (< 8) -> pas d'alerte", str(_cat(f)))


print('\n=== 4. mac_flapping : une MAC vue avec >= 6 IP différentes ===')
paquets = [FauxPaquet(ARP(psrc=f'192.168.1.{100+i}', pdst='0.0.0.0',
                          hwsrc='de:ad:be:ef:00:01', op=1)) for i in range(7)]
f = _capturer(paquets)
verifier('mac_flapping' in _cat(f), "1 MAC derrière 7 IP -> mac_flapping", str(_cat(f)))


print('\n=== 5. dhcp_pirate (capture) : >= 2 serveurs DHCP observés ===')
paquets = [
    FauxPaquet(DHCP(options=[('server_id', '192.168.1.1'), ('message-type', 2)])),
    FauxPaquet(DHCP(options=[('server_id', '192.168.1.250'), ('message-type', 2)])),
]
f = _capturer(paquets)
verifier('dhcp_pirate' in _cat(f), "2 serveurs DHCP distincts en capture -> dhcp_pirate", str(_cat(f)))
dp = next((x for x in f if x['categorie'] == 'dhcp_pirate'), None)
verifier(dp is not None and dp['details'].get('source') == 'capture',
         "la source est bien tracée comme 'capture' (vs 'discover' du palier 1 actif)",
         str(dp and dp['details']))

print('\n=== 5bis. Un seul serveur DHCP -> pas d\'alerte ===')
paquets = [FauxPaquet(DHCP(options=[('server_id', '192.168.1.1')]))]
f = _capturer(paquets)
verifier('dhcp_pirate' not in _cat(f), "un seul serveur DHCP -> pas d'alerte", str(_cat(f)))


print('\n=== 6. ra_pirate : >= 2 routeurs IPv6 émettent des Router Advertisements ===')
# Ether a besoin de .dst (vérifié en premier dans _on, pour le compteur de
# broadcast) même sur un paquet qui ne nous intéresse ici que pour .src :
# sans lui, l'AttributeError est absorbée par le try/except englobant de
# _on et court-circuite silencieusement le test IPv6/RA qui suit.
paquets = [
    FauxPaquet(IPv6(), ICMPv6ND_RA(), Ether(src='aa:bb:cc:00:01:01', dst='33:33:00:00:00:01')),
    FauxPaquet(IPv6(), ICMPv6ND_RA(), Ether(src='aa:bb:cc:00:01:02', dst='33:33:00:00:00:01')),
]
f = _capturer(paquets)
verifier('ra_pirate' in _cat(f), "2 sources de RA IPv6 -> ra_pirate", str(_cat(f)))


print('\n=== 7. stp_instable : >= 5 BPDU de changement de topologie (TCN) ===')
paquets = [FauxPaquet(STP(bpdutype=0x80, bpduflags=0)) for _ in range(6)]
f = _capturer(paquets)
verifier('stp_instable' in _cat(f), "6 BPDU TCN (type 0x80) -> stp_instable", str(_cat(f)))

print('\n=== 7bis. BPDU normales (ni TCN, ni flag TC) -> pas d\'alerte ===')
paquets = [FauxPaquet(STP(bpdutype=0x00, bpduflags=0)) for _ in range(10)]
f = _capturer(paquets)
verifier('stp_instable' not in _cat(f), "BPDU de config normales -> pas d'alerte", str(_cat(f)))


print('\n=== 8. tcp_retransmissions : >= 15 sur un même flux (seq qui recule) ===')
paquets = []
seq = 1000
for i in range(16):
    seq += 100
    paquets.append(FauxPaquet(   # trame normale : fait avancer le seq maximum vu
        IP(src='192.168.1.10', dst='192.168.1.20'),
        TCP(sport=51000, dport=443, seq=seq),
        Raw(load=b'x')))
    paquets.append(FauxPaquet(   # retransmission : seq < maximum déjà vu sur ce flux
        IP(src='192.168.1.10', dst='192.168.1.20'),
        TCP(sport=51000, dport=443, seq=seq - 50),
        Raw(load=b'x')))
f = _capturer(paquets)
verifier('tcp_retransmissions' in _cat(f), "de nombreux seq en recul sur le même flux -> tcp_retransmissions",
         str(_cat(f)))


print('\n=== 9. tempete_broadcast : seuil paramétrable (broadcast_pps) ===')
paquets = [FauxPaquet(Ether(dst='ff:ff:ff:ff:ff:ff')) for _ in range(5)]
f = _capturer(paquets, seuils={'broadcast_pps': 3})
verifier('tempete_broadcast' in _cat(f), "5 trames broadcast avec un seuil à 3 -> tempete_broadcast",
         str(_cat(f)))
f2 = _capturer(paquets, seuils={'broadcast_pps': 1000})
verifier('tempete_broadcast' not in _cat(f2), "même trafic avec un seuil élevé -> pas d'alerte", str(_cat(f2)))


print('\n=== 10. Un paquet malformé (attribut manquant) ne casse pas toute la capture ===')
class _PaquetCasse:
    def haslayer(self, cls):
        return cls is Ether
    def __getitem__(self, cls):
        raise RuntimeError("paquet corrompu")
paquets = [_PaquetCasse(),
           FauxPaquet(ARP(psrc='192.168.1.50', pdst='192.168.1.1', hwsrc='aa:bb:cc:00:00:01', op=1))]
f = _capturer(paquets)
verifier(isinstance(f, list), "un paquet qui lève une exception dans le callback n'interrompt pas la capture",
         str(f))


print('\n=== 11. scapy indisponible -> liste vide, jamais d\'exception ===')
N._charger_scapy = lambda: None
try:
    f = N.capture_passive(3, {})
finally:
    N._charger_scapy = _charger_scapy_orig
verifier(f == [], "scapy absent -> [] immédiatement, sans tenter de sniffer")


print('\n=== 12. capture_dhcp_fingerprints() : empreinte DHCP passive (audit #23) ===')
_cfg_orig = N._cfg
_etat_capture_orig = N.etat_capture


def _capturer_dhcp(paquets, actif=True):
    FauxAsyncSniffer._paquets = paquets
    N._charger_scapy = lambda: FAUX_SCAPY
    N.time.sleep = lambda s: None
    N._cfg = lambda cle, defaut=None: '1' if (cle == 'diag_capture_active' and actif) else defaut
    # RUNNING_IN_DOCKER=1 (mis par ce script) fait toujours répondre
    # etat_capture() 'docker_bridge' quel que soit le faux scapy injecté —
    # sans ce mock, la fonction testée renverrait toujours {} pour une
    # raison sans rapport avec ce qu'on teste ici.
    N.etat_capture = lambda: {'disponible': True, 'motif': 'ok'}
    try:
        return N.capture_dhcp_fingerprints(5)
    finally:
        N._charger_scapy = _charger_scapy_orig
        N.time.sleep = _sleep_orig
        N._cfg = _cfg_orig
        N.etat_capture = _etat_capture_orig


_paquet_win = FauxPaquet(
    Ether(src='aa:bb:cc:00:00:01', dst='ff:ff:ff:ff:ff:ff'),
    DHCP(options=[('message-type', 1), ('param_req_list', [1, 3, 6, 15, 31, 33, 43, 44, 46, 47, 119, 121, 249, 252])]))
_paquet_apple = FauxPaquet(
    Ether(src='aa:bb:cc:00:00:02', dst='ff:ff:ff:ff:ff:ff'),
    DHCP(options=[('message-type', 1), ('param_req_list', [1, 3, 6, 15, 119, 95, 252, 44, 46])]))
_paquet_iot = FauxPaquet(
    Ether(src='aa:bb:cc:00:00:03', dst='ff:ff:ff:ff:ff:ff'),
    DHCP(options=[('message-type', 1), ('param_req_list', [1, 3, 6, 15])]))

_r12 = _capturer_dhcp([_paquet_win, _paquet_apple, _paquet_iot])
verifier(set(_r12) == {'aa:bb:cc:00:00:01', 'aa:bb:cc:00:00:02', 'aa:bb:cc:00:00:03'},
         "les 3 empreintes sont capturées, une par MAC source", str(sorted(_r12)))
verifier(_r12['aa:bb:cc:00:00:01']['options'] == tuple(sorted({1, 3, 6, 15, 31, 33, 43, 44, 46, 47, 119, 121, 249, 252})),
         "la liste de paramètres demandés (option 55) est capturée telle quelle, triée",
         str(_r12['aa:bb:cc:00:00:01']['options']))
verifier('Windows' in _r12['aa:bb:cc:00:00:01']['famille'],
         "option 249 (route statique MS) -> famille Windows", str(_r12['aa:bb:cc:00:00:01']))
verifier('Apple' in _r12['aa:bb:cc:00:00:02']['famille'],
         "options 119+95 -> famille Apple", str(_r12['aa:bb:cc:00:00:02']))
verifier('embarqué' in _r12['aa:bb:cc:00:00:03']['famille'],
         "liste courte (4 options de base) -> objet embarqué", str(_r12['aa:bb:cc:00:00:03']))

print('\n=== 12bis. capture_dhcp_fingerprints() : désactivée par réglage / scapy absent ===')
verifier(_capturer_dhcp([_paquet_win], actif=False) == {},
         "diag_capture_active=0 -> {} immédiatement, sans tenter de sniffer")
N._cfg = lambda cle, defaut=None: '1' if cle == 'diag_capture_active' else defaut
N.etat_capture = lambda: {'disponible': True, 'motif': 'ok'}
N._charger_scapy = lambda: None
try:
    _r12c = N.capture_dhcp_fingerprints(5)
finally:
    N._charger_scapy = _charger_scapy_orig
    N._cfg = _cfg_orig
    N.etat_capture = _etat_capture_orig
verifier(_r12c == {}, "scapy absent -> {} sans exception")

print('\n=== 12ter. Un paquet DHCP sans option 55 (ACK, RENEW sans param_req_list) est ignoré ===')
_paquet_sans_option = FauxPaquet(
    Ether(src='aa:bb:cc:00:00:09', dst='ff:ff:ff:ff:ff:ff'),
    DHCP(options=[('message-type', 5)]))
_r12d = _capturer_dhcp([_paquet_sans_option])
verifier(_r12d == {}, "aucune option param_req_list -> pas d'entrée pour cette MAC", str(_r12d))

print('\n=== 13. _p0f_ttl_initial() / _p0f_famille() : fonctions pures (audit #25) ===')
verifier(N._p0f_ttl_initial(60) == 64, "TTL observé 60 (4 sauts) -> TTL initial 64", str(N._p0f_ttl_initial(60)))
verifier(N._p0f_ttl_initial(125) == 128, "TTL observé 125 (3 sauts) -> TTL initial 128")
verifier(N._p0f_ttl_initial(250) == 255, "TTL observé 250 -> TTL initial 255")
verifier(N._p0f_ttl_initial('bidon') is None, "TTL illisible -> None, pas d'exception")
verifier('Windows' in N._p0f_famille(128, False, False), "TTL initial 128 -> Windows")
verifier('réseau' in N._p0f_famille(255, False, False), "TTL initial 255 -> équipement réseau")
verifier('Linux' in N._p0f_famille(64, True, True), "TTL 64 + WScale + Timestamp -> Linux ou assimilé")
verifier('macOS' in N._p0f_famille(64, False, False), "TTL 64 sans WScale/Timestamp -> macOS/BSD")
verifier(N._p0f_famille(None, False, False) == 'Indéterminé', "TTL initial inconnu -> Indéterminé")

print('\n=== 14. capture_os_fingerprints() : empreinte TCP/IP passive façon p0f (audit #25) ===')


def _capturer_os(paquets, actif=True):
    FauxAsyncSniffer._paquets = paquets
    N._charger_scapy = lambda: FAUX_SCAPY
    N.time.sleep = lambda s: None
    N._cfg = lambda cle, defaut=None: '1' if (cle == 'diag_capture_active' and actif) else defaut
    N.etat_capture = lambda: {'disponible': True, 'motif': 'ok'}
    try:
        return N.capture_os_fingerprints(5)
    finally:
        N._charger_scapy = _charger_scapy_orig
        N.time.sleep = _sleep_orig
        N._cfg = _cfg_orig
        N.etat_capture = _etat_capture_orig


_syn_win = FauxPaquet(IP(src='10.5.0.11', ttl=127),
                      TCP(flags=0x02, window=8192, options=[('MSS', 1460)]))
_syn_lin = FauxPaquet(IP(src='10.5.0.12', ttl=63),
                      TCP(flags=0x02, window=29200,
                          options=[('MSS', 1460), ('SAckOK', b''), ('Timestamp', (1, 0)),
                                   ('NOP', None), ('WScale', 7)]))
_synack_ignore = FauxPaquet(IP(src='10.5.0.99', ttl=64), TCP(flags=0x12, window=65535, options=[]))

_r14 = _capturer_os([_syn_win, _syn_lin, _synack_ignore])
verifier(set(_r14) == {'10.5.0.11', '10.5.0.12'},
         "seuls les SYN initiaux (pas les SYN-ACK) produisent une entrée", str(sorted(_r14)))
verifier(_r14['10.5.0.11']['ttl_initial_estime'] == 128 and 'Windows' in _r14['10.5.0.11']['os_probable'],
         "TTL observé 127 + fenêtre 8192 -> Windows probable", str(_r14['10.5.0.11']))
verifier(_r14['10.5.0.12']['ttl_initial_estime'] == 64 and 'Linux' in _r14['10.5.0.12']['os_probable'],
         "TTL observé 63 + WScale/Timestamp -> Linux probable", str(_r14['10.5.0.12']))

print('\n=== 14bis. capture_os_fingerprints() : désactivée par réglage / scapy absent ===')
verifier(_capturer_os([_syn_win], actif=False) == {},
         "diag_capture_active=0 -> {} immédiatement, sans tenter de sniffer")
N._cfg = lambda cle, defaut=None: '1' if cle == 'diag_capture_active' else defaut
N.etat_capture = lambda: {'disponible': True, 'motif': 'ok'}
N._charger_scapy = lambda: None
try:
    _r14c = N.capture_os_fingerprints(5)
finally:
    N._charger_scapy = _charger_scapy_orig
    N._cfg = _cfg_orig
    N.etat_capture = _etat_capture_orig
verifier(_r14c == {}, "scapy absent -> {} sans exception")

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
