#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie l'identification du matériel au scan réseau, suite à un retour
d'usage réel : un MacBook ressortait comme "machine Linux", et switches /
bornes WiFi n'étaient pas identifiés comme tels (retombaient sur "PC" ou,
pire, un équipement Ubiquiti quelconque devenait systématiquement
"Routeur/Pare-feu" même s'il s'agissait d'une borne ou d'un switch).

Ce que le test contrôle :
  - _scan_host() : le TTL seul ne distingue pas macOS de Linux (les deux
    valent 64 par défaut) — le fabricant MAC officiel Apple affine
    désormais os_guess en 'macOS' plutôt que de laisser 'Linux/Unix'.
  - _deviner_type() : un Mac (fabricant Apple OU hostname mDNS typique) est
    reconnu comme 'MacBook', pas comme 'PC/Serveur (Linux)' — sauf pour les
    appareils Apple qui n'en sont pas (iPhone, iPad, AirPort...).
  - _deviner_type() : les fabricants réseau courants (Ubiquiti, MikroTik,
    TP-Link, Netgear, Aruba, etc.) sans indice de sous-type dans le hostname
    tombent sur 'Switch/AP' (catégorie existante de la liste des types)
    plutôt que d'être supposés "Routeur/Pare-feu" par défaut.
  - Les valeurs renvoyées correspondent exactement à la liste canonique des
    types d'appareils (config_helpers.LISTE_DEFAULTS['types_appareils']) :
    'Borne Wi-Fi' (pas 'Borne WiFi'), 'NAS' (pas 'Serveur'), 'Switch/AP'
    (pas 'Équipement réseau', absent de la liste) — un import de scan qui
    renvoie une valeur hors liste casse silencieusement le sélecteur de la
    fiche appareil (aucune option ne matche -> retombe sur la première au
    prochain enregistrement, effaçant le type détecté sans avertissement).

Usage :
    python test_scan_identification_materiel.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='scan_ident_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A                                    # noqa: E402
from config_helpers import LISTE_DEFAULTS          # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


TYPES_CANONIQUES = set(LISTE_DEFAULTS['types_appareils'])

print('=== 1. Valeurs renvoyées par _deviner_type() : toutes dans la liste canonique ===')
cas = [
    ('poste', [], '', 'Apple, Inc.', '', '', ''),
    ('quelconque', [], '', 'Ubiquiti Inc', '', '', ''),
    ('nas-synology', [], '', '', 'synology ds220', '', ''),
    ('borne-ap-1', [], '', '', 'ap-etage2', '', ''),
    ('poste2', [], 'Network', '', '', '', ''),
]
for hostname, ports, os_guess, vendor, extra, updt, mdns in cas:
    t = A._deviner_type(hostname, ports, os_guess=os_guess, vendor=vendor,
                        extra_signal=extra, upnp_device_type=updt, mdns_service=mdns)
    verifier(t in TYPES_CANONIQUES, f"'{t}' (cas hostname={hostname!r}) est dans la liste canonique", t)

print('\n=== 2. MacBook : plus jamais classé "Linux" ===')
verifier(A._deviner_type('Julie-MacBook-Pro.local', [], os_guess='Linux/Unix', vendor='') == 'MacBook',
          "hostname mDNS typique ('...-MacBook-Pro.local') -> MacBook, même sans fabricant connu")
verifier(A._deviner_type('inconnu', [22], os_guess='Linux/Unix', vendor='Apple, Inc.') == 'MacBook',
          "fabricant MAC officiel Apple -> MacBook, même avec le port 22 ouvert (Remote Login activé)")
verifier(A._deviner_type('inconnu', [], os_guess='macOS', vendor='') == 'MacBook',
          "os_guess déjà affiné en 'macOS' (par _scan_host) -> MacBook")
verifier(A._deviner_type('inconnu', [], os_guess='Linux/Unix', vendor='') == 'PC/Serveur (Linux)',
          "sans aucun signal Apple, un vrai Linux reste bien 'PC/Serveur (Linux)' (pas de faux positif)")

print('\n=== 3. Appareils Apple qui ne sont PAS un MacBook : exclus malgré le fabricant ===')
verifier(A._deviner_type("Julies-iPhone", [], vendor='Apple, Inc.') != 'MacBook',
          "iPhone (fabricant Apple) -> pas classé MacBook")
verifier(A._deviner_type("Bureau-AirPort-Extreme", [], vendor='Apple, Inc.') != 'MacBook',
          "AirPort Extreme (fabricant Apple, mais routeur) -> pas classé MacBook")

print('\n=== 4. _scan_host() : le fabricant Apple affine os_guess (macOS, pas Linux/Unix) ===')
_ping_orig      = A._ping
_hostname_orig  = A._hostname
_netbios_orig   = A._netbios_name
_ttl_orig       = A._ttl_os_guess
_ports_orig     = A._scan_ports
_arp_orig       = A._mac_from_arp
_oui_orig       = A._oui_vendor
A._ping           = lambda ip: True
A._hostname        = lambda ip: ''
A._netbios_name    = lambda ip: 'Julies-MacBook-Air'
A._ttl_os_guess    = lambda ip: 'Linux/Unix'   # TTL 64, comme un vrai Mac
A._scan_ports      = lambda ip: []
A._mac_from_arp    = lambda ip: 'a4:83:e7:aa:bb:cc'
A._oui_vendor      = lambda mac: 'Apple, Inc.'
try:
    resultat = A._scan_host('192.0.2.99')
finally:
    A._ping, A._hostname, A._netbios_name = _ping_orig, _hostname_orig, _netbios_orig
    A._ttl_os_guess, A._scan_ports = _ttl_orig, _ports_orig
    A._mac_from_arp, A._oui_vendor = _arp_orig, _oui_orig

verifier(resultat is not None, "_scan_host() retourne bien un résultat")
verifier(resultat.get('os_guess') == 'macOS',
          "os_guess affiné en 'macOS' (pas laissé sur 'Linux/Unix')", str(resultat.get('os_guess')))
verifier(resultat.get('type') == 'MacBook',
          "type final -> MacBook, plus jamais 'PC/Serveur (Linux)'", str(resultat.get('type')))

print('\n=== 5. Switch / borne WiFi : fabricant réseau reconnu, sous-type indéterminé ===')
for marque in ['Ubiquiti Inc', 'MikroTik', 'TP-LINK CORPORATION LIMITED', 'NETGEAR',
               'Aruba, a Hewlett Packard Enterprise Company']:
    t = A._deviner_type('inconnu', [], vendor=marque)
    verifier(t == 'Switch/AP',
              f"fabricant '{marque}' sans indice de sous-type -> Switch/AP (pas systématiquement routeur)", t)
# Cisco reste un cas à part : la marque seule ('cisco' dans le mot-clé
# fabricant historique) suffit à conclure Switch, y compris pour sa
# filiale Meraki — comportement déjà en place avant ce correctif, conservé.
verifier(A._deviner_type('inconnu', [], vendor='Cisco Meraki') == 'Switch',
          "fabricant 'Cisco Meraki' -> Switch (mot-clé 'cisco' historique, inchangé)")

print('\n=== 6. Ubiquiti : le sous-type explicite dans le hostname prime toujours sur le fabricant seul ===')
verifier(A._deviner_type('UAP-AC-PRO', [], vendor='Ubiquiti Inc', extra_signal='borne wifi') == 'Borne Wi-Fi',
          "borne UniFi identifiée par hostname -> Borne Wi-Fi, pas Routeur/Pare-feu")
verifier(A._deviner_type('EdgeRouter-X', [], vendor='Ubiquiti Inc') == 'Routeur/Pare-feu',
          "routeur UniFi identifié par hostname -> Routeur/Pare-feu")
verifier(A._deviner_type('USW-24-PoE', [], vendor='Ubiquiti Inc') == 'Switch',
          "switch UniFi identifié par hostname -> Switch")

print('\n=== 7. Casse/orthographe alignées sur la liste canonique des types ===')
verifier(A._deviner_type('ap-salle-reunion', [], vendor='') == 'Borne Wi-Fi',
          "'Borne Wi-Fi' (avec le trait d'union, comme dans la liste canonique)")
verifier('Borne WiFi' != A._deviner_type('ap-salle-reunion', [], vendor=''),
          "plus jamais 'Borne WiFi' sans trait d'union (absent de la liste canonique)")
verifier(A._deviner_type('poste', [], os_guess='Network') == 'Switch/AP',
          "repli TTL réseau (os_guess='Network') -> 'Switch/AP' (catégorie existante)")
verifier('Équipement réseau' != A._deviner_type('poste', [], os_guess='Network'),
          "plus jamais 'Équipement réseau' (absent de la liste canonique)")

print('\n=== 8. NAS : catégorie dédiée, plus confondu avec "Serveur" ===')
verifier(A._deviner_type('synology-ds220', [], vendor='') == 'NAS', "hostname Synology -> NAS")
verifier(A._deviner_type('qnap-nas01', [], vendor='') == 'NAS', "hostname QNAP -> NAS")
verifier(A._deviner_type('poste', [], upnp_device_type='urn:schemas-upnp-org:device:MediaServer:1') == 'NAS',
          "deviceType UPnP MediaServer -> NAS (plus 'Serveur')")

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
