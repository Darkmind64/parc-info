#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie que le curseur de synchronisation (pull) ne saute jamais une
entrée en échec.

Contexte : signalé en usage réel — des fiches système ne se synchronisaient
pas entre postes, sans aucune erreur visible après coup. `_sync_using_journal`
faisait avancer le curseur de lecture (`_sync_meta.last_pulled_journal_id`)
jusqu'à la dernière entrée du lot, MÊME quand une table du lot avait échoué à
s'appliquer — l'entrée en échec n'était donc jamais retentée au cycle
suivant, perdue silencieusement (l'erreur elle-même ne survivait que le temps
du cycle où elle s'était produite, dans `_sync_state['last_error']`).

Ce que ce test contrôle :
  - une table qui échoue à s'appliquer ne fait PAS avancer le curseur au-delà
    de sa plus ancienne entrée en échec
  - une table qui réussit dans le même lot reste appliquée normalement
  - au cycle suivant, l'entrée en échec est relue et retentée — pas perdue

Usage :
    python test_sync_curseur_echec.py
"""

import io
import os
import sqlite3
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database as db  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


def _curseur(conn):
    row = conn.execute(
        "SELECT value FROM _sync_meta WHERE key='last_pulled_journal_id'").fetchone()
    return int(row[0]) if row and row[0] else 0


# ── Prépare deux bases SQLite locales, l'une jouant le rôle de « turso »
#    (source du pull), l'autre de la base locale (cible du pull). Une simple
#    connexion sqlite3 sert de source : _apply_table_changes n'y voit rien
#    de spécifique à Turso puisque la cible (locale) n'est jamais un
#    TursoConnection dans ce sens de synchronisation. ──────────────────────
turso = sqlite3.connect(':memory:')
turso.execute("CREATE TABLE _sync_journal (id INTEGER PRIMARY KEY, tbl TEXT, "
              "record_id TEXT, action TEXT)")
turso.execute("CREATE TABLE table_ok (id INTEGER PRIMARY KEY, nom TEXT)")
turso.execute("CREATE TABLE table_cassee (id INTEGER PRIMARY KEY, nom TEXT)")
turso.execute("INSERT INTO table_ok VALUES (1, 'avant échec')")
turso.execute("INSERT INTO table_cassee VALUES (1, 'ne peut pas arriver localement')")
turso.execute("INSERT INTO table_ok VALUES (2, 'après échec, même lot')")
# Journal turso : id=1 (table_ok/1), id=2 (table_cassee/1, échouera), id=3
# (table_ok/2) — la table en échec est au milieu du lot, pas à la fin.
turso.executemany(
    "INSERT INTO _sync_journal (id, tbl, record_id, action) VALUES (?, ?, ?, 'INSERT')",
    [(1, 'table_ok', '1', ), (2, 'table_cassee', '1'), (3, 'table_ok', '2')])
turso.commit()

fichier_local = os.path.join(tempfile.mkdtemp(prefix='sync_curseur_'), 'local.db')
local = sqlite3.connect(fichier_local)
local.execute("CREATE TABLE table_ok (id INTEGER PRIMARY KEY, nom TEXT)")
# table_cassee n'existe PAS localement : _apply_table_changes échouera dessus
# (« no such table »), simulant une vraie panne de pull sans rien inventer.
local.commit()

print("=== 1. Premier cycle : la table en échec ne doit pas faire sauter le curseur ===")
stats, errors = db._sync_using_journal(local, turso)

verifier(any('table_cassee' in e for e in errors),
         "l'échec de table_cassee est bien remonté dans errors", str(errors))
verifier(local.execute("SELECT nom FROM table_ok WHERE id=1").fetchone() == ('avant échec',),
         "table_ok/1 (avant l'échec dans le lot) est bien appliquée")
verifier(_curseur(local) < 2,
         "le curseur ne dépasse PAS l'entrée en échec (id=2)", str(_curseur(local)))

print("\n=== 2. Deuxième cycle, la table cassée existe maintenant : rattrapage ===")
local.execute("CREATE TABLE table_cassee (id INTEGER PRIMARY KEY, nom TEXT)")
local.commit()
stats2, errors2 = db._sync_using_journal(local, turso)

verifier(not errors2, "plus aucune erreur une fois la table réparée", str(errors2))
verifier(local.execute("SELECT nom FROM table_cassee WHERE id=1").fetchone()
         == ('ne peut pas arriver localement',),
         "table_cassee/1 finit par arriver — rien n'a été perdu")
verifier(local.execute("SELECT nom FROM table_ok WHERE id=2").fetchone()
         == ('après échec, même lot',),
         "table_ok/2 (jamais appliquée au premier cycle) arrive aussi")
verifier(_curseur(local) == 3, "le curseur rattrape enfin la fin du lot", str(_curseur(local)))

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
