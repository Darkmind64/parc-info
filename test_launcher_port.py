#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie launcher.get_port() : patience sur le port préféré uniquement
pendant la relance macOS après mise à jour (PARCINFO_RELANCE_MAJ).

Signalé en usage réel (macOS Intel) : après une mise à jour, la nouvelle
version redémarrait sur un port différent de 3456. Cause : le lancement
direct macOS démarre la nouvelle instance AVANT d'arrêter l'ancienne (pour
vérifier qu'elle survit) — l'ancienne tient donc encore le port préféré
jusqu'à ~12 s après le démarrage de la nouvelle. Sans patience, get_port()
basculait aussitôt sur un port au hasard, et y restait bloqué pour le reste
de l'exécution (rien ne le fait jamais revenir sur 3456 une fois choisi).

Points contrôlés :
  - lancement normal (pas de PARCINFO_RELANCE_MAJ) : port occupé -> bascule
    immédiatement sur un port libre, sans attendre
  - relance après mise à jour (PARCINFO_RELANCE_MAJ=1) : port occupé
    temporairement -> patiente puis récupère le port préféré dès qu'il se
    libère
  - dans les deux cas, un port déjà libre est retourné tel quel, sans délai

Usage :
    python test_launcher_port.py
"""

import io
import os
import socket
import sys
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import launcher  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


def _port_libre():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


print("=== 1. Port déjà libre : retourné tel quel, sans délai, dans les deux cas ===")
os.environ.pop('PARCINFO_RELANCE_MAJ', None)
port_test = _port_libre()
t0 = time.monotonic()
resultat = launcher.get_port(preferred=port_test)
duree = time.monotonic() - t0
verifier(resultat == port_test, "port préféré libre retourné tel quel", str(resultat))
verifier(duree < 0.5, "aucun délai quand le port est déjà libre", f"{duree:.2f}s")

print("\n=== 2. Lancement normal, port occupé : bascule immédiate sur un port libre ===")
port_test2 = _port_libre()
occupant = socket.socket()
occupant.bind(('127.0.0.1', port_test2))
os.environ.pop('PARCINFO_RELANCE_MAJ', None)
t0 = time.monotonic()
resultat2 = launcher.get_port(preferred=port_test2)
duree2 = time.monotonic() - t0
occupant.close()
verifier(resultat2 != port_test2, "port différent du port préféré occupé", str(resultat2))
verifier(duree2 < 0.5, "bascule immédiate, sans attendre (comportement normal inchangé)", f"{duree2:.2f}s")

print("\n=== 3. Relance après mise à jour, port occupé temporairement : patiente puis le récupère ===")
port_test3 = _port_libre()
occupant3 = socket.socket()
occupant3.bind(('127.0.0.1', port_test3))
occupant3.listen(1)


def _liberer_apres_delai():
    time.sleep(1.2)
    occupant3.close()


threading.Thread(target=_liberer_apres_delai, daemon=True).start()

os.environ['PARCINFO_RELANCE_MAJ'] = '1'
t0 = time.monotonic()
resultat3 = launcher.get_port(preferred=port_test3)
duree3 = time.monotonic() - t0
os.environ.pop('PARCINFO_RELANCE_MAJ', None)
verifier(resultat3 == port_test3,
         "le port préféré est bien récupéré une fois libéré par l'ancienne instance",
         str(resultat3))
verifier(duree3 >= 1.0,
         "a bien attendu la libération plutôt que de basculer aussitôt", f"{duree3:.2f}s")

print("\n  " + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
