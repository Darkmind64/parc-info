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


print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
