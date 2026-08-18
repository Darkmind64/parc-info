#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie qu'un appareil supprimé ne revient pas tout seul après plusieurs
cycles de synchronisation.

Contexte : signalé en usage réel — un appareil supprimé dans l'inventaire
disparaissait bien immédiatement sur toutes les instances, mais réapparaissait
de lui-même après plusieurs cycles de synchronisation, sans qu'aucune
suppression manuelle n'ait eu lieu entre-temps. Reproduit avec plusieurs
appareils, avec et sans fiche système.

Cause : `_sync_using_journal` poussait D'ABORD le journal local vers Turso,
puis tirait ENSUITE les changements distants. Une instance en retard (pas
encore passée par un cycle de sync depuis la suppression faite ailleurs) peut
garder dans son propre journal local une modification de CET appareil datant
d'AVANT sa suppression — l'appareil existe toujours localement sur cette
instance. En poussant avant de tirer, cette entrée périmée recrée l'appareil
sur Turso via INSERT OR REPLACE, avec un nouvel identifiant de journal
POSTÉRIEUR à la suppression d'origine — qui se propage ensuite normalement à
toutes les autres instances. La suppression semblait alors s'annuler toute
seule, plusieurs cycles plus tard, sans action de personne.

Corrigé en inversant l'ordre : tirer D'ABORD les changements distants. Une
instance en retard apprend ainsi la suppression et l'applique localement
avant de pousser quoi que ce soit — son entrée périmée ne trouve alors plus
rien à lire localement pour cet appareil et ne pousse donc plus rien.

Ce que ce test contrôle :
  - une entrée locale périmée (modification d'un appareil supprimé ailleurs,
    pas encore appris localement) ne le ressuscite PAS sur Turso
  - l'appareil reste supprimé localement ET sur Turso après le cycle
  - l'entrée périmée est purgée du journal local (sans effet, mais retirée)

Usage :
    python test_sync_resurrection_suppression.py
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


print("=== Un appareil supprimé sur une autre instance ne revient pas ===")

# ── « Turso » : la suppression de l'appareil 42 a déjà réussi (plus de ligne
#    dans appareils), et son propre journal en garde la trace (id=3), au-delà
#    de ce que l'instance en retard a déjà tiré (curseur local = 2).
turso = sqlite3.connect(':memory:')
turso.execute("CREATE TABLE _sync_journal (id INTEGER PRIMARY KEY, tbl TEXT, "
              "record_id TEXT, action TEXT)")
turso.execute("CREATE TABLE appareils (id INTEGER PRIMARY KEY, nom TEXT)")
turso.executemany(
    "INSERT INTO _sync_journal (id, tbl, record_id, action) VALUES (?, ?, ?, ?)",
    [(1, 'appareils', '42', 'INSERT'),
     (2, 'appareils', '42', 'UPDATE'),
     (3, 'appareils', '42', 'DELETE')])
turso.commit()

# ── Instance « en retard » : connaît encore l'appareil 42 dans l'état d'avant
#    sa suppression (curseur bloqué à 2), ET garde en attente une modification
#    locale de ce même appareil faite AVANT d'avoir appris la suppression —
#    exactement le scénario signalé en usage réel.
fichier_local = os.path.join(tempfile.mkdtemp(prefix='sync_resurrection_'), 'local.db')
local = sqlite3.connect(fichier_local)
local.execute("CREATE TABLE appareils (id INTEGER PRIMARY KEY, nom TEXT)")
local.execute("CREATE TABLE _sync_meta (key TEXT PRIMARY KEY, value TEXT)")
local.execute("CREATE TABLE _sync_journal (id INTEGER PRIMARY KEY, tbl TEXT, "
              "record_id TEXT, action TEXT)")
local.execute("INSERT INTO appareils VALUES (42, 'Ancien PC (modifié localement)')")
local.execute("INSERT INTO _sync_meta VALUES ('last_pulled_journal_id', '2')")
# Entrée périmée : une modification locale de l'appareil 42, en attente de
# push, faite avant que cette instance n'apprenne sa suppression ailleurs.
local.execute(
    "INSERT INTO _sync_journal (id, tbl, record_id, action) VALUES (100, 'appareils', '42', 'UPDATE')")
local.commit()

stats, errors = db._sync_using_journal(local, turso)

verifier(not errors, "aucune erreur pendant le cycle", str(errors))
verifier(local.execute("SELECT * FROM appareils WHERE id=42").fetchone() is None,
         "l'appareil reste supprimé localement après le cycle")
verifier(turso.execute("SELECT * FROM appareils WHERE id=42").fetchone() is None,
         "l'appareil n'est PAS ressuscité sur Turso par l'entrée périmée")
verifier(local.execute("SELECT COUNT(*) FROM _sync_journal").fetchone()[0] == 0,
         "l'entrée périmée est purgée du journal local (sans effet)")
verifier(local.execute("SELECT value FROM _sync_meta WHERE key='last_pulled_journal_id'").fetchone()
         == ('3',), "le curseur local a bien avancé jusqu'à la suppression")

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
