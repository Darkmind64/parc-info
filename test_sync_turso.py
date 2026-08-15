#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie que la réplication de schéma vers Turso met à jour un trigger
_trg_journal_* périmé, sans pour autant le retoucher à chaque cycle.

Contexte : _ensure_turso_schema() copie les triggers de suivi des
modifications (_trg_journal_*) vers Turso à chaque cycle de sync (~30s,
potentiellement depuis plusieurs instances). Un `CREATE TRIGGER IF NOT
EXISTS` aveugle laissait un trigger déjà présent sur Turso — créé par une
version antérieure du code — figé indéfiniment (même défaut que celui déjà
corrigé côté local, voir _TRACKED_JOURNAL dans app.py), ce qui a fini par
provoquer un vrai « UNIQUE constraint failed » sur _sync_journal en
production. Fige le comportement corrigé : DROP+CREATE seulement quand la
définition diffère de celle déjà sur Turso — jamais en aveugle, pour ne pas
rouvrir une fenêtre sans trigger à chaque cycle.

Usage :
    python test_sync_turso.py
"""

import sqlite3
import sys

import database as D

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


_SYNC_JOURNAL_DDL = """CREATE TABLE _sync_journal (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl       TEXT    NOT NULL,
    record_id TEXT    NOT NULL,
    action    TEXT    NOT NULL,
    timestamp TEXT    NOT NULL,
    UNIQUE(tbl, record_id, action) ON CONFLICT REPLACE)"""


def _fabriquer_local():
    """DB locale minimale avec une table suivie et son trigger _trg_journal_*."""
    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE TABLE documents_appareils (id INTEGER PRIMARY KEY, nom TEXT)")
    conn.execute(_SYNC_JOURNAL_DDL)
    conn.execute("""CREATE TRIGGER _trg_journal_ins_documents_appareils
        AFTER INSERT ON documents_appareils BEGIN
            INSERT OR REPLACE INTO _sync_journal (tbl, record_id, action, timestamp)
            VALUES ('documents_appareils', NEW.id, 'INSERT', datetime('now'));
        END""")
    conn.commit()
    return conn


class _SpyConn:
    """Enveloppe une connexion sqlite3 (simule Turso) et journalise le SQL exécuté."""
    def __init__(self, conn):
        self._conn = conn
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append(sql)
        return self._conn.execute(sql, params)

    def commit(self):
        self._conn.commit()


print("=== 1. Un trigger périmé sur Turso est remplacé, pas laissé tel quel ===")
local = _fabriquer_local()

turso_reel = sqlite3.connect(':memory:')
turso_reel.execute("CREATE TABLE documents_appareils (id INTEGER PRIMARY KEY, nom TEXT)")
turso_reel.execute(_SYNC_JOURNAL_DDL)
# Version périmée : sans OR REPLACE, comme une édition antérieure du code aurait pu l'être.
turso_reel.execute("""CREATE TRIGGER _trg_journal_ins_documents_appareils
    AFTER INSERT ON documents_appareils BEGIN
        INSERT INTO _sync_journal (tbl, record_id, action, timestamp)
        VALUES ('documents_appareils', NEW.id, 'INSERT', datetime('now'));
    END""")
turso_reel.commit()

turso = _SpyConn(turso_reel)
D._ensure_turso_schema(local, turso)

nouvelle_sql = turso_reel.execute(
    "SELECT sql FROM sqlite_master WHERE type='trigger' "
    "AND name='_trg_journal_ins_documents_appareils'").fetchone()[0]
locale_sql = local.execute(
    "SELECT sql FROM sqlite_master WHERE type='trigger' "
    "AND name='_trg_journal_ins_documents_appareils'").fetchone()[0]
verifier(nouvelle_sql == locale_sql, 'le trigger périmé est remplacé par la définition actuelle')
verifier(any('DROP TRIGGER' in s for s in turso.executed),
         'un DROP TRIGGER a bien été émis pour la définition périmée')

# Le trigger corrigé fonctionne réellement : plus de conflit silencieux sur une
# clé déjà journalisée (c'est précisément l'erreur constatée en production).
turso_reel.execute("INSERT INTO _sync_journal (tbl, record_id, action, timestamp) "
                    "VALUES ('documents_appareils', 1, 'INSERT', '2020-01-01')")
turso_reel.commit()
try:
    turso_reel.execute("INSERT INTO documents_appareils (id, nom) VALUES (1, 'x')")
    turso_reel.commit()
    ok_reinsertion = True
except sqlite3.IntegrityError:
    ok_reinsertion = False
verifier(ok_reinsertion, "réinsertion sur une clé déjà journalisée : plus d'échec UNIQUE")

print("\n=== 2. Un trigger déjà à jour n'est pas retouché à chaque cycle ===")
local2 = _fabriquer_local()
turso_reel2 = sqlite3.connect(':memory:')
turso_reel2.execute("CREATE TABLE documents_appareils (id INTEGER PRIMARY KEY, nom TEXT)")
turso_reel2.execute(_SYNC_JOURNAL_DDL)
# Turso a déjà exactement la même définition que le local (cycle précédent à jour).
sql_locale = local2.execute(
    "SELECT sql FROM sqlite_master WHERE type='trigger' "
    "AND name='_trg_journal_ins_documents_appareils'").fetchone()[0]
turso_reel2.execute(sql_locale)
turso_reel2.commit()

turso2 = _SpyConn(turso_reel2)
D._ensure_turso_schema(local2, turso2)
verifier(not any('DROP TRIGGER' in s for s in turso2.executed),
         "aucun DROP TRIGGER émis quand la définition est déjà à jour "
         "— pas de fenêtre sans trigger ouverte à chaque cycle")

print()
if echecs:
    print('ÉCHECS : %d' % len(echecs))
    for e in echecs:
        print('  - ' + e)
    sys.exit(1)
else:
    print('TOUT OK')
