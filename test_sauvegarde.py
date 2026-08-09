#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie la sauvegarde automatique de la base et sa rotation.

Points contrôlés :
  - la copie est exploitable (et non un fichier tronqué pris pendant une écriture)
  - les accents survivent au trajet
  - la rotation ne conserve que les trois dernières
  - deux sauvegardes simultanées ne se marchent pas dessus

Usage :
    python test_sauvegarde.py
"""

import os
import sqlite3
import sys
import tempfile
import threading
import time

DATA = os.path.join(tempfile.gettempdir(), 'parcinfo_backup_test')
os.makedirs(DATA, exist_ok=True)
for nom in os.listdir(DATA):
    chemin = os.path.join(DATA, nom)
    if os.path.isfile(chemin):
        try:
            os.remove(chemin)
        except OSError:
            pass
os.environ['DATA_DIR'] = DATA
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'      # pas de thread pendant le test
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as A  # noqa: E402

A.init_db()

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


conn = A.get_db()
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Société Générale & Cie')")
conn.execute("INSERT INTO appareils (client_id, nom_machine, notes) VALUES (1, 'Bureau-Réception', 'Écran cassé — à remplacer')")
conn.commit()
conn.close()

print('=== 1. Une sauvegarde est créée et reste exploitable ===')
chemin, erreur = A.creer_sauvegarde('test')
verifier(erreur is None, 'aucune erreur', erreur or '')
verifier(chemin is not None and os.path.exists(chemin), 'fichier présent')

if chemin:
    copie = sqlite3.connect(chemin)
    # Une copie tronquée passe la simple ouverture : il faut interroger.
    integrite = copie.execute('PRAGMA integrity_check').fetchone()[0]
    verifier(integrite == 'ok', 'intégrité SQLite', integrite)
    nom = copie.execute('SELECT nom FROM clients WHERE id=1').fetchone()[0]
    notes = copie.execute("SELECT notes FROM appareils WHERE nom_machine='Bureau-Réception'").fetchone()
    copie.close()
    verifier(nom == 'Société Générale & Cie', 'accents et esperluette préservés', nom)
    verifier(notes is not None and notes[0] == 'Écran cassé — à remplacer',
             'accents dans les données préservés')

print('\n=== 2. Rotation : seules les trois dernières sont conservées ===')
for i in range(4):
    # L'horodatage est à la seconde : espacer pour obtenir des noms distincts
    time.sleep(1.1)
    A.creer_sauvegarde('test-%d' % i)
restantes = A._backup_files()
verifier(len(restantes) == A.BACKUP_KEEP,
         'exactement %d sauvegardes conservées' % A.BACKUP_KEEP,
         '%d trouvée(s)' % len(restantes))
dates = [os.path.getmtime(f) for f in restantes]
verifier(dates == sorted(dates, reverse=True), 'les plus récentes sont conservées')

print('\n=== 3. Deux sauvegardes simultanées ===')
resultats = []


def lancer():
    resultats.append(A.creer_sauvegarde('concurrente'))


fils = [threading.Thread(target=lancer) for _ in range(2)]
for f in fils:
    f.start()
for f in fils:
    f.join()
refusees = [r for r in resultats if r[0] is None]
verifier(len(refusees) <= 1, 'au plus une des deux est refusée, sans erreur',
         str([r[1] for r in refusees]))
verifier(len(A._backup_files()) == A.BACKUP_KEEP, 'rotation toujours respectée')

print('\n=== 4. La base d\'origine est intacte ===')
conn = A.get_db()
integrite = conn.execute('PRAGMA integrity_check').fetchone()[0]
compte = conn.execute('SELECT COUNT(*) FROM appareils').fetchone()[0]
conn.close()
verifier(integrite == 'ok', 'intégrité de la base source')
verifier(compte >= 1, 'données toujours présentes')

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
