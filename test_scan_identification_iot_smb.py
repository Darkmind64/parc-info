#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie une deuxième vague de corrections d'identification au scan
réseau, suite à un nouveau retour d'usage réel : un Mac ressortait comme
"PC (Windows)", un switch comme "Serveur", un ESP32 et une prise connectée
comme "PC".

Ce que le test contrôle :
  - SMB (ports 135/445) n'est PLUS un signal Windows fiable à lui seul :
    Samba (Linux) et le partage de fichiers natif de macOS répondent aussi
    sur ces ports. Le TTL garde la priorité quand il pointe clairement
    ailleurs (Linux/Unix ou macOS) ; Windows n'est retenu via ces ports que
    faute de meilleur indice — RDP (3389), lui, reste un signal fort à lui
    seul (aucun équivalent légitime hors Windows).
  - "server"/"srv" nu (mot-clé hostname/bannière) ne suffit plus seul à
    conclure "Serveur" — un switch/imprimante bon marché non reconnu par
    ailleurs peut très bien avoir "Server" dans le titre de sa page
    d'administration embarquée ("Print Server", "Web Server Login"). Il
    faut désormais un vrai port de service serveur à l'appui (les mots-clés
    non ambigus — Exchange/vCenter/ESXi/contrôleur de domaine — suffisent
    toujours seuls).
  - Objets connectés (ESP32/ESP8266, prises/relais/capteurs) reconnus comme
    catégorie propre ('Objet connecté', nouvelle entrée de la liste
    canonique des types) via le fabricant MAC officiel de la puce radio
    (Espressif, très majoritaire sur ce segment) et via les noms de
    firmware/marques les plus courants en hostname (Tasmota, ESPHome,
    Shelly, Sonoff, Tuya) — plutôt qu'une liste de marques commerciales
    condamnée à rater le marché blanche.
  - mDNS élargi aux services HomeKit/Shelly/ESPHome pour les mêmes objets
    connectés qui ne renseignent rien d'utile en hostname/vendor.

Usage :
    python test_scan_identification_iot_smb.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='scan_ident2_')
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
verifier('Objet connecté' in TYPES_CANONIQUES,
          "'Objet connecté' fait bien partie de la liste canonique des types")

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 1. SMB (135/445) ne fait plus deviner "Windows" quand le TTL dit le contraire ===')
verifier(A._deviner_type('inconnu', [445], os_guess='Linux/Unix', vendor='') == 'PC/Serveur (Linux)',
          "Mac/Linux avec partage de fichiers actif (445) et TTL Linux -> reste Linux, plus Windows")
verifier(A._deviner_type('inconnu', [445], os_guess='macOS', vendor='') == 'MacBook',
          "Mac avec partage de fichiers actif (445) et os_guess déjà affiné en macOS -> MacBook")
verifier(A._deviner_type('inconnu', [135, 445], os_guess='Windows', vendor='') == 'PC (Windows)',
          "un vrai Windows (TTL cohérent) avec 135/445 reste bien détecté Windows")
verifier(A._deviner_type('inconnu', [135, 445], os_guess='', vendor='') == 'PC (Windows)',
          "TTL indisponible (os_guess vide) -> 135/445 servent toujours de repli Windows, comme avant")
verifier(A._deviner_type('inconnu', [3389], os_guess='Linux/Unix', vendor='') == 'PC (Windows)',
          "RDP (3389) reste un signal Windows fort, même si le TTL dit autre chose (aucun équivalent légitime)")

print('\n=== 2. "server"/"srv" nu ne suffit plus sans port de service serveur ===')
verifier(A._deviner_type('switch-cave', [80, 443], extra_signal='Print Server Login Page') != 'Serveur',
          "bannière 'Print Server' sans port serveur -> plus classé Serveur à tort")
verifier(A._deviner_type('inconnu', [80], extra_signal='Web Server Setup') != 'Serveur',
          "titre de page 'Web Server Setup' seul -> plus classé Serveur à tort")
verifier(A._deviner_type('inconnu', [3306], extra_signal='MySQL Server 8.0') == 'Serveur',
          "'Server' + vrai port de service serveur (3306) -> toujours classé Serveur")
verifier(A._deviner_type('inconnu', [], extra_signal='VMware ESXi 7.0') == 'Serveur',
          "mot-clé non ambigu (ESXi) -> Serveur sans avoir besoin d'un port")
verifier(A._deviner_type('inconnu', [], extra_signal='Microsoft Exchange Server 2019') == 'Serveur',
          "mot-clé non ambigu (Exchange) -> Serveur sans avoir besoin d'un port")

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 3. Objets connectés : ESP32/ESP8266 via le fabricant de la puce radio ===')
verifier(A._deviner_type('inconnu', [], vendor='Espressif Inc.') == 'Objet connecté',
          "fabricant Espressif (ESP32/ESP8266) -> Objet connecté, plus 'PC'")
verifier(A._deviner_type('esp32-a1b2c3', [80], vendor='') == 'Objet connecté',
          "hostname générique ESP32 même sans fabricant reconnu -> Objet connecté")

print('\n=== 4. Objets connectés : prises/relais reconnus par marque/firmware en hostname ===')
for hostname in ('tasmota-4B2C1A', 'shellyplug-s-A1B2C3', 'sonoff-basic-1234',
                  'esphome-salon', 'tuya-prise-cuisine'):
    t = A._deviner_type(hostname, [80], vendor='')
    verifier(t == 'Objet connecté', f"hostname '{hostname}' -> Objet connecté", t)

print('\n=== 5. Objets connectés : signaux mDNS structurés (HomeKit/Shelly/ESPHome) ===')
verifier(A._deviner_type('inconnu', [], mdns_service='hap') == 'Objet connecté',
          "service mDNS HomeKit (_hap._tcp) -> Objet connecté")
verifier(A._deviner_type('inconnu', [], mdns_service='shelly') == 'Objet connecté',
          "service mDNS Shelly -> Objet connecté")
verifier(A._deviner_type('inconnu', [], mdns_service='esphomelib') == 'Objet connecté',
          "service mDNS ESPHome -> Objet connecté")

print('\n=== 6. Pas de faux positifs : un vrai PC/serveur reste correctement identifié ===')
verifier(A._deviner_type('poste-julie', [445, 139], os_guess='Windows', vendor='Dell Inc.') == 'PC (Windows)',
          "PC Windows classique (Dell, SMB, TTL Windows) toujours détecté correctement")
verifier(A._deviner_type('srv-sql01', [3306], vendor='Dell Inc.', os_guess='Linux/Unix') == 'Serveur',
          "serveur applicatif réel (hostname 'srv' + port MySQL) toujours détecté correctement")

print('\n=== 7. _MDNS_TYPES_APPAREILS élargi aux services IoT ===')
for service in ('_hap._tcp.local.', '_shelly._tcp.local.', '_esphomelib._tcp.local.'):
    verifier(service in A._MDNS_TYPES_APPAREILS, f"{service} fait bien partie des services mDNS interrogés")

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
