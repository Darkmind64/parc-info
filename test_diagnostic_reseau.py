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

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
