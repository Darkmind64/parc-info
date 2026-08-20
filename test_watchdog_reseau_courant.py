#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie que la surveillance ping en tâche de fond ne marque plus à tort
« hors ligne » les appareils d'un client dont on a quitté le réseau.

Signalé en usage réel : ParcInfo tourne souvent sur un poste qui se déplace
physiquement d'un client à l'autre (portable technicien). Le watchdog
pingeait jusqu'ici TOUS les appareils de TOUS les clients à chaque cycle —
dès qu'on quitte le réseau d'un client, ses appareils passaient à « hors
ligne » non pas parce qu'ils le sont, mais parce que le ping échoue depuis
un autre réseau.

Ce que le test contrôle :
  - _reseaux_locaux_actuels() dérive bien des réseaux /24 depuis les IP
    locales de CE poste, sans la boucle locale
  - _appareil_sur_reseau_courant() : la plage IP configurée du client
    (parc_general.plage_ip_locale) est prioritaire, avec un repli sur le
    /24 de l'IP de l'appareil quand cette plage est absente ; réseaux
    locaux inconnus -> comportement historique (on pingue quand même)
  - _watchdog_cycle() : un appareil hors du réseau actuel n'est jamais
    pingé NI modifié en base — son dernier statut connu survit tel quel ;
    un appareil sur le réseau actuel, lui, est bien pingé et mis à jour
  - le cycle avance quand même (last_cycle/cycle_count) même quand aucun
    appareil ne correspond au réseau actuel — la topbar ne doit pas
    paraître figée alors que la surveillance tourne normalement

Usage :
    python test_watchdog_reseau_courant.py
"""

import io
import ipaddress
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='watchdog_reseau_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


print('=== 1. _reseaux_locaux_actuels() : dérivé des IP locales, sans la boucle ===')
_ghe_original = A.socket.gethostbyname_ex
A.socket.gethostbyname_ex = lambda h: (h, [], ['192.168.1.42', '10.0.5.7', '127.0.0.1'])
reseaux = A._reseaux_locaux_actuels()
A.socket.gethostbyname_ex = _ghe_original
verifier(ipaddress.ip_network('192.168.1.0/24') in reseaux, "réseau de la première interface retenu")
verifier(ipaddress.ip_network('10.0.5.0/24') in reseaux, "réseau de la seconde interface (multi-interfaces) retenu")
verifier(not any(str(r).startswith('127.') for r in reseaux), "la boucle locale (127.x) est exclue")

print('\n=== 2. _appareil_sur_reseau_courant() ===')
un_reseau = {ipaddress.ip_network('192.168.1.0/24')}
verifier(A._appareil_sur_reseau_courant('192.168.1.50', '192.168.1.0/24', un_reseau) is True,
         "plage du client contient l'appareil, et chevauche notre réseau actuel -> True")
verifier(A._appareil_sur_reseau_courant('192.168.1.77', '', un_reseau) is True,
         "pas de plage client configurée -> repli sur le /24 de l'IP de l'appareil -> True")
verifier(A._appareil_sur_reseau_courant('10.0.0.5', '10.0.0.0/24', un_reseau) is False,
         "réseau du client différent du nôtre -> False")
verifier(A._appareil_sur_reseau_courant('10.0.0.5', '10.0.0.0/24', set()) is True,
         "impossible de déterminer notre réseau -> comportement historique (pinger quand même)")
verifier(A._appareil_sur_reseau_courant('pas-une-ip', '', un_reseau) is False,
         "IP invalide -> False, pas d'exception")

print('\n=== 3. _watchdog_cycle() : bout en bout ===')
A.init_db()
conn = A.get_db()
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client Present')")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (2, 'Client Absent')")
conn.execute("INSERT INTO parc_general (client_id, nom_site, plage_ip_locale) VALUES (1, 'Site', '192.168.1.0/24')")
conn.execute("INSERT INTO parc_general (client_id, nom_site, plage_ip_locale) VALUES (2, 'Site', '10.0.0.0/24')")
conn.execute("INSERT INTO appareils (client_id, nom_machine, adresse_ip, en_ligne, dernier_ping) "
             "VALUES (1, 'Poste-Present', '192.168.1.99', 1, '2026-01-01T00:00:00')")
conn.execute("INSERT INTO appareils (client_id, nom_machine, adresse_ip, en_ligne, dernier_ping) "
             "VALUES (2, 'Poste-Absent', '10.0.0.99', 1, '2026-01-01T00:00:00')")
conn.commit()
id_present = conn.execute("SELECT id FROM appareils WHERE nom_machine='Poste-Present'").fetchone()[0]
id_absent  = conn.execute("SELECT id FROM appareils WHERE nom_machine='Poste-Absent'").fetchone()[0]
conn.close()

# CE poste (simulé) est sur le réseau du Client Present uniquement.
A._reseaux_locaux_actuels = lambda: {ipaddress.ip_network('192.168.1.0/24')}
appels_ping = []


def _faux_ping_once(ip):
    appels_ping.append(ip)
    return False  # simule un appareil éteint, pour vérifier que le statut change bien à 0


A._ping_once = _faux_ping_once
A._watchdog_state['last_cycle'] = None
cycle_avant = A._watchdog_state['cycle_count']
A._watchdog_cycle()

verifier('192.168.1.99' in appels_ping, "l'appareil du client présent est bien pingé")
verifier('10.0.0.99' not in appels_ping, "l'appareil du client absent n'est PAS pingé")

conn = A.get_db()
present = conn.execute("SELECT en_ligne, dernier_ping FROM appareils WHERE id=?", (id_present,)).fetchone()
absent  = conn.execute("SELECT en_ligne, dernier_ping FROM appareils WHERE id=?", (id_absent,)).fetchone()
conn.close()
verifier(present[0] == 0, "statut mis à jour pour l'appareil réellement testé (éteint simulé)")
verifier(absent[0] == 1 and absent[1] == '2026-01-01T00:00:00',
         "dernier statut connu INCHANGÉ pour l'appareil hors réseau (pas de faux « hors ligne »)",
         str(tuple(absent)))
verifier(A._watchdog_state['last_cycle'] is not None, "l'horodatage du cycle avance même partiellement")
verifier(A._watchdog_state['cycle_count'] == cycle_avant + 1, "le compteur de cycle avance normalement")

print('\n=== 4. _watchdog_cycle() : aucun appareil sur le réseau actuel, le cycle avance quand même ===')
A._reseaux_locaux_actuels = lambda: {ipaddress.ip_network('172.16.0.0/24')}  # ni l'un ni l'autre client
appels_ping.clear()
A._watchdog_state['last_cycle'] = None
cycle_avant2 = A._watchdog_state['cycle_count']
A._watchdog_cycle()
verifier(appels_ping == [], "aucun appareil pingé quand on n'est chez aucun client suivi")
verifier(A._watchdog_state['cycle_count'] == cycle_avant2 + 1,
         "le compteur de cycle avance quand même — la topbar ne doit pas sembler figée")
verifier(A._watchdog_state['last_cycle'] is not None, "l'horodatage avance aussi")

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
