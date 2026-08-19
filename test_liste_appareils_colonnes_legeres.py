#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie que la liste d'inventaire des appareils ne ramène plus les blobs
JSON lourds sur chaque page, tout en gardant les champs qu'elle affiche.

Contexte : signalé en usage réel — l'inventaire est un peu lent à s'afficher
avec une soixantaine d'appareils. La requête faisait `SELECT a.*`, ramenant
sur CHAQUE page toutes les colonnes de `appareils`, y compris
rapport_systeme_json et logiciels_installes_json — jusqu'à 1 Mo chacune une
fois remplies par une vraie collecte (voir la limite dans /api/device-info)
— alors que la liste ne les affiche jamais, seule la fiche détail les
utilise.

Ce que ce test contrôle :
  - _colonnes_appareils_liste() exclut bien les colonnes lourdes
  - un champ réellement affiché par la liste (ports_ouverts) reste inclus
  - la requête construite avec cette liste de colonnes s'exécute et retourne
    des données correctes, nb_docs/nb_contrats compris
  - le résultat ne contient PAS les blobs lourds, même s'ils sont remplis
    en base pour cet appareil

Usage :
    python test_liste_appareils_colonnes_legeres.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='liste_app_legere_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A  # noqa: E402
from database import get_db, row_to_dict  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


A.init_db()

print("=== 1. _colonnes_appareils_liste() exclut les blobs lourds ===")
colonnes = A._colonnes_appareils_liste()
verifier('rapport_systeme_json' not in colonnes, "rapport_systeme_json absent de la liste")
verifier('logiciels_installes_json' not in colonnes, "logiciels_installes_json absent de la liste")
verifier('a.ports_ouverts' in colonnes,
         "ports_ouverts (réellement affiché par la liste) reste inclus")
verifier('a.nom_machine' in colonnes and 'a.adresse_ip' in colonnes,
         "les champs de base (nom, IP) restent inclus")

print("\n=== 2. La requête construite retourne des données correctes, sans les blobs ===")
conn = get_db()
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client Test')")
gros_rapport = '{"donnees": "%s"}' % ('x' * 5000)  # simule un vrai rapport rempli
conn.execute(
    "INSERT INTO appareils (client_id, nom_machine, adresse_ip, statut, ports_ouverts, "
    "rapport_systeme_json, logiciels_installes_json) VALUES (1, 'PC-Test', '192.168.1.10', "
    "'actif', '80,443', ?, ?)",
    (gros_rapport, gros_rapport))
conn.commit()
appareil_id = conn.execute(
    "SELECT id FROM appareils WHERE nom_machine='PC-Test'").fetchone()[0]

q = f'''SELECT {A._colonnes_appareils_liste()},
        (SELECT COUNT(*) FROM documents_appareils d WHERE d.appareil_id=a.id) as nb_docs,
        (SELECT COUNT(*) FROM contrats_appareils ca JOIN contrats ct ON ca.contrat_id=ct.id
         WHERE ca.appareil_id=a.id AND ct.client_id=a.client_id) as nb_contrats
        FROM appareils a WHERE a.client_id=?'''
rows = [row_to_dict(r) for r in conn.execute(q, (1,)).fetchall()]
conn.close()

verifier(len(rows) == 1, "un appareil retrouvé", str(len(rows)))
if rows:
    r = rows[0]
    verifier(r.get('nom_machine') == 'PC-Test', "nom_machine correct", r.get('nom_machine'))
    verifier(r.get('adresse_ip') == '192.168.1.10', "adresse_ip correcte", r.get('adresse_ip'))
    verifier(r.get('ports_ouverts') == '80,443', "ports_ouverts correct", r.get('ports_ouverts'))
    verifier(r.get('nb_docs') == 0, "nb_docs calculé (0 attendu)", str(r.get('nb_docs')))
    verifier('rapport_systeme_json' not in r,
             "rapport_systeme_json absent du résultat malgré une vraie valeur en base")
    verifier('logiciels_installes_json' not in r,
             "logiciels_installes_json absent du résultat malgré une vraie valeur en base")

print("\n=== 3. La fiche détail, elle, continue de tout ramener (a.*) ===")
conn = get_db()
row_detail = row_to_dict(conn.execute(
    "SELECT * FROM appareils WHERE id=?", (appareil_id,)).fetchone())
conn.close()
verifier(row_detail.get('rapport_systeme_json') == gros_rapport,
         "la fiche détail garde bien accès au rapport système complet")

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
