#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Découverte de sous-réseaux hors des plages saisies (page Scan réseau).

`plage_ip_locale` est tapée à la main : sur un site multi-VLAN dont on ne
connaît pas toutes les plages, c'est insuffisant. `network_diag.decouvrir_reseaux`
agrège sans saisie :
  - la table de routage de CE poste (`_routes_locales_poste`)
  - ses serveurs DNS (`_dns_configures_poste`, hors résolveurs publics)
  - son cache ARP (`_table_arp`, IP hors des plages connues)
  - le SNMP des routeurs de l'inventaire (`sous_reseaux_detectes`, déjà éprouvé)

et `reseaux_hors_plage(plages, *sources_ip)` remonte, après un scan, les /24
où des appareils (UPnP/mDNS/ONVIF/ARP-SNMP) ont répondu sans être dans les
plages scannées.

Contrôles :
  - un CIDR déjà dans `plage_ip_locale` n'est jamais proposé
  - un CIDR trop large (/8) ou link-local n'est jamais proposé
  - la source (`via`) est correctement étiquetée et dédoublonnée
  - les résolveurs publics (8.8.8.8…) ne produisent pas de candidat
  - `reseaux_hors_plage` : dans la plage → ignoré ; hors plage → /24 + compte
  - en Docker (RUNNING_IN_DOCKER=1), les sondes LOCALES sont court-circuitées

Usage : python test_decouverte_reseaux.py
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='decouv_reseaux_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A            # noqa: E402
import network_diag as N   # noqa: E402

A.init_db()
echecs = []


def verifier(cond, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if cond else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not cond:
        echecs.append(libelle)


conn = A.get_db()
cur = conn.execute("INSERT INTO clients (nom, date_creation) VALUES ('Découverte', '2026-01-01')")
CID = cur.lastrowid
conn.execute("INSERT INTO parc_general (client_id, plage_ip_locale) VALUES (?, '192.168.1.0/24')", (CID,))
conn.commit()

print('=== 1. _cidr_scannable : garde-fous ===')
import ipaddress
verifier(N._cidr_scannable(ipaddress.ip_network('192.168.5.0/24')), '/24 privé -> scannable')
verifier(not N._cidr_scannable(ipaddress.ip_network('10.0.0.0/8')), '/8 -> trop large')
verifier(not N._cidr_scannable(ipaddress.ip_network('169.254.0.0/16')), 'link-local -> non')
verifier(not N._cidr_scannable(ipaddress.ip_network('127.0.0.0/8')), 'loopback -> non')
verifier(not N._cidr_scannable(ipaddress.ip_network('1.2.3.4/32')), '/32 -> non')

print('\n=== 2. Docker : sondes locales court-circuitées ===')
verifier(N._routes_locales_poste() == set(), 'routage local -> vide en Docker')
verifier(N._dns_configures_poste() == set(), 'DNS local -> vide en Docker')

print('\n=== 3. decouvrir_reseaux : agrégat, exclusion, étiquettes ===')
# les sondes ARP sont court-circuitées en Docker : on retire le drapeau le
# temps de ce bloc (même convention que test_watchdog_reseau_courant.py).
_docker = os.environ.pop('RUNNING_IN_DOCKER', None)
# on injecte des sources contrôlées
N._routes_locales_poste = lambda: {'192.168.1.0/24', '192.168.20.0/24', '10.10.0.0/16'}
N._dns_configures_poste = lambda: {'192.168.99.53', '8.8.8.8'}
N._table_arp = lambda: {'192.168.1.10': {'aa:aa:aa:00:00:01'},
                        '192.168.20.5': {'bb:bb:bb:00:00:02'},
                        '192.168.20.6': {'bb:bb:bb:00:00:03'}}
# pas de SNMP actif -> sous_reseaux_detectes renvoie snmp_inactif, ignoré
d = N.decouvrir_reseaux(CID)
if _docker is not None:
    os.environ['RUNNING_IN_DOCKER'] = _docker
cidrs = {x['cidr']: x for x in d['detectes']}
verifier('192.168.1.0/24' not in cidrs, 'plage déjà déclarée -> exclue')
verifier('10.10.0.0/16' not in cidrs, '/16 routé SANS hôte -> écarté (bruit Hyper-V)')
verifier('192.168.20.0/24' in cidrs, '/24 vu en route ET en ARP -> proposé')
verifier(cidrs.get('192.168.20.0/24', {}).get('confiance') == 'forte', '  -> confiance forte (hôtes ARP)')
verifier('192.168.99.0/24' in cidrs, '/24 du serveur DNS interne -> proposé')
verifier(cidrs.get('192.168.99.0/24', {}).get('confiance') == 'faible', '  -> confiance faible (DNS seul)')
verifier('8.8.8.0/24' not in cidrs and '8.8.8.8/24' not in str(cidrs),
         'résolveur public -> aucun candidat')
if '192.168.20.0/24' in cidrs:
    vias = {s['via'] for s in cidrs['192.168.20.0/24']['sources']}
    verifier(vias == {'routage_local', 'arp_local'}, 'sources dédoublonnées et étiquetées',
             str(vias))
    verifier(cidrs['192.168.20.0/24']['hint_hotes'] == 2, 'hint_hotes = nb IP vues en ARP')

print('\n=== 4. reseaux_hors_plage ===')
hp = N.reseaux_hors_plage(
    ['192.168.1.0/24'],
    {'192.168.1.50': 'x'},                       # dans la plage -> ignoré
    {'192.168.77.10': 'y', '192.168.77.11': 'z'},  # hors plage -> 192.168.77.0/24
    {'10.9.9.9': 'w'})                            # hors plage -> 10.9.9.0/24
nets = {x['cidr']: x['hint_hotes'] for x in hp}
verifier('192.168.1.0/24' not in nets, 'IP dans la plage scannée -> pas remontée')
verifier(nets.get('192.168.77.0/24') == 2, '2 IP hors plage sur le même /24 -> compte=2')
verifier('10.9.9.0/24' in nets, 'autre /24 hors plage -> remonté')

conn.close()
print()
if echecs:
    print('%d ÉCHEC(S) : %s' % (len(echecs), ', '.join(echecs)))
    sys.exit(1)
print('Découverte de sous-réseaux : tous les contrôles passent.')
