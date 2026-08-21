#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie la hiérarchie de priorité entre les méthodes de détection au scan
réseau (troisième vague de correctifs d'identification, 2026-08-20) : les
signaux forts (SNMP qui répond, fabricant MAC) ne doivent plus jamais être
écrasés par des heuristiques de port plus faibles (SMB, port 22) arrivant
après eux dans le code.

Ce que le test contrôle :
  - Un agent SNMP qui répond (community "public") est un signal fort à lui
    seul, vérifié AVANT les heuristiques de port SMB/22 : un switch/onduleur
    SNMP dont aucun mot-clé hostname/sysDescr ne matche par ailleurs ne
    retombe plus sur "PC (Windows)" via la seule présence du port 445.
  - RDP (3389) reste prioritaire même sur SNMP (aucun équivalent légitime
    hors Windows, cas assez rare pour ne pas mériter de règle spéciale).
  - Raspberry Pi (fabricant MAC officiel) reconnu comme Linux avant les
    heuristiques de port — un Pi-hole/Home Assistant avec Samba (445) ou
    SSH (22) actif ne devient plus "PC (Windows)".
  - Les noms d'OS/firmware réseau vus en clair dans le sysDescr SNMP
    (FortiOS, SonicOS, DD-WRT, OpenWrt, OPNsense — famille pare-feu/routeur
    non ambiguë) sont reconnus, ainsi que ceux partagés entre routeurs ET
    switches chez un même constructeur (JUNOS, RouterOS, Comware, VRP,
    ArubaOS) qui retombent volontairement sur la catégorie générique
    "Switch/AP" plutôt qu'un sous-type deviné à tort.

Usage :
    python test_scan_priorite_snmp.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='scan_prio_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A   # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


# ═══════════════════════════════════════════════════════════════════════════
print('=== 1. SNMP actif prioritaire sur les heuristiques de port ambiguës ===')
verifier(A._deviner_type('inconnu', [445], snmp_actif=True) == 'Switch/AP',
          "SNMP répond + port 445 (SMB) -> Switch/AP, plus 'PC (Windows)' à tort")
verifier(A._deviner_type('inconnu', [22], snmp_actif=True) == 'Switch/AP',
          "SNMP répond + port 22 -> Switch/AP, pas 'PC/Serveur (Linux)' à tort")
verifier(A._deviner_type('inconnu', [], snmp_actif=True, os_guess='Windows') == 'Switch/AP',
          "SNMP répond -> prioritaire même si le TTL seul suggérerait Windows")
verifier(A._deviner_type('inconnu', [445], snmp_actif=False, os_guess='Windows') == 'PC (Windows)',
          "sans SNMP, le comportement (TTL Windows + SMB) reste inchangé")

print('\n=== 2. RDP reste prioritaire même sur SNMP (aucun équivalent légitime) ===')
verifier(A._deviner_type('inconnu', [3389], snmp_actif=True) == 'PC (Windows)',
          "RDP (3389) l'emporte même si SNMP répond aussi (cas très rare mais RDP ne ment pas)")

print('\n=== 3. Raspberry Pi : reconnu Linux avant les heuristiques de port ===')
verifier(A._deviner_type('pi-hole', [445], vendor='Raspberry Pi Foundation') == 'PC/Serveur (Linux)',
          "Raspberry Pi + Samba (445) actif -> reste Linux, plus jamais Windows")
verifier(A._deviner_type('homeassistant', [22, 8123], vendor='Raspberry Pi Foundation') == 'PC/Serveur (Linux)',
          "Raspberry Pi + SSH -> Linux (comportement déjà correct, non régressé)")

print('\n=== 4. sysDescr SNMP : familles pare-feu/routeur non ambiguës ===')
for texte, attendu in [
    ('Fortinet FortiGate FortiOS v7.2', 'Routeur/Pare-feu'),
    ('SonicWALL TZ370 SonicOS 7.0', 'Routeur/Pare-feu'),
    ('Linux 4.14 DD-WRT v3.0 router', 'Routeur/Pare-feu'),
    ('OpenWrt 22.03 Linux router', 'Routeur/Pare-feu'),
    ('OPNsense 24.1 firewall', 'Routeur/Pare-feu'),
]:
    t = A._deviner_type('inconnu', [], extra_signal=texte)
    verifier(t == attendu, f"sysDescr '{texte}' -> {attendu}", t)

print('\n=== 5. sysDescr SNMP : OS partagés routeur/switch -> catégorie générique ===')
# Quand le nom du fabricant/produit apparaît aussi dans le texte (cas
# largement majoritaire en pratique — un sysDescr réel dit presque toujours
# "Juniper... JUNOS", "H3C Comware...", "RouterOS" contient lui-même le mot
# "router"), c'est ce mot-clé plus spécifique qui l'emporte légitimement,
# avant même d'atteindre le repli générique OS-only ci-dessous.
verifier(A._deviner_type('inconnu', [], extra_signal='Juniper Networks, Inc. JUNOS 21.4') == 'Switch',
          "'Juniper... JUNOS' -> Switch (mot-clé fabricant 'juniper' déjà suffisant, inchangé)")
verifier(A._deviner_type('inconnu', [], extra_signal='MikroTik RouterOS 7.11') == 'Routeur/Pare-feu',
          "'RouterOS' contient 'router' -> Routeur/Pare-feu (cohérent, RouterOS est le plus souvent un routeur)")
verifier(A._deviner_type('inconnu', [], extra_signal='H3C Comware Software S5130') == 'Switch',
          "'H3C Comware' -> Switch (mot-clé fabricant 'h3c' déjà suffisant, inchangé)")
# Le repli générique OS-only sert pour le cas plus rare où SEUL le nom de
# l'OS apparaît, sans le nom du fabricant/produit à côté.
for texte in ('ArubaOS-CX 10.09', 'Comware Software V7 S5130'):
    t = A._deviner_type('inconnu', [], extra_signal=texte)
    verifier(t == 'Switch/AP',
              f"sysDescr '{texte}' (OS seul, sans fabricant) -> Switch/AP", t)

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
