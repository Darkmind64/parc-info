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

print("\n=== 3. Les triggers réels d'init_db() n'utilisent plus INSERT OR REPLACE ===")
# Le test 1 ci-dessus prouve que _ensure_turso_schema() propage bien une
# définition corrigée — mais avec sa propre définition de trigger simulée,
# pas celle qu'app.py écrit réellement. Ce test-ci porte sur celle-là :
# UNIQUE(...) ON CONFLICT REPLACE + INSERT OR REPLACE réussissait pourtant
# ce même scénario en SQLite pur (test 1, ligne ~105) — le souci constaté en
# production ne venait donc pas d'un défaut de syntaxe, mais d'une résolution
# REPLACE qui ne s'appliquait pas de façon fiable une fois le trigger exécuté
# à distance par Turso. DELETE puis INSERT n'a besoin d'aucune résolution de
# conflit, quelle qu'en soit la cause exacte côté Turso.
import io as _io3, os as _os3, tempfile as _tempfile3  # noqa: E402

_os3.environ['DATA_DIR'] = _tempfile3.mkdtemp(prefix='synctrigger_')
_os3.environ['RUNNING_IN_DOCKER'] = '1'
_os3.environ['PARCINFO_BACKUP'] = '0'
import app as A  # noqa: E402

A.init_db()
conn = A.get_db()
sql_reel = conn.execute(
    "SELECT sql FROM sqlite_master WHERE type='trigger' "
    "AND name='_trg_journal_ins_documents_appareils'").fetchone()[0]
verifier('OR REPLACE' not in sql_reel,
         "le trigger réel n'utilise plus INSERT OR REPLACE", sql_reel)
verifier('DELETE FROM _sync_journal' in sql_reel,
         "le trigger réel journalise via DELETE puis INSERT")

# Le scénario qui a échoué en production : une clé déjà présente dans
# _sync_journal (ex. laissée par un cycle antérieur), puis une nouvelle
# écriture sur la même table/ligne/action.
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client Test')")
conn.execute("INSERT INTO appareils (client_id, nom_machine) VALUES (1, 'POSTE-Sync')")
conn.commit()
appareil_id = conn.execute("SELECT id FROM appareils WHERE nom_machine='POSTE-Sync'").fetchone()[0]
conn.execute("INSERT INTO _sync_journal (tbl, record_id, action, timestamp) "
             "VALUES ('appareils', ?, 'UPDATE', '2020-01-01T00:00:00')", (appareil_id,))
conn.commit()
try:
    conn.execute("UPDATE appareils SET nom_machine='POSTE-Sync-2' WHERE id=?", (appareil_id,))
    conn.commit()
    reussi = True
except sqlite3.IntegrityError:
    reussi = False
verifier(reussi, "une écriture sur une clé déjà journalisée ne lève plus d'erreur UNIQUE")
lignes = conn.execute(
    "SELECT COUNT(*) FROM _sync_journal WHERE tbl='appareils' AND record_id=? AND action='UPDATE'",
    (appareil_id,)).fetchone()[0]
verifier(lignes == 1, "une seule ligne de journal pour cette clé, pas de doublon", str(lignes))

print("\n=== 4. Une ligne _sync_applying restée d'un crash n'éteint pas la sync pour toujours ===")
# Contrôle du système de synchronisation : _sync_applying protège contre une
# boucle pendant un pull (voir database.py), mais si le PROCESS entier meurt
# entre l'INSERT et le DELETE (kill, coupure), la ligne reste sur disque et
# survit au redémarrage — WHEN NOT EXISTS (SELECT 1 FROM _sync_applying) ne
# serait alors plus jamais vrai, éteignant tous les triggers pour toujours.
conn.execute("INSERT OR IGNORE INTO _sync_applying (id) VALUES (1)")
conn.commit()
A.init_db()  # simule un redémarrage après un crash laissant la ligne en place
reste = conn.execute("SELECT COUNT(*) FROM _sync_applying").fetchone()[0]
verifier(reste == 0, "la ligne périmée est purgée au (ré)démarrage", str(reste))

conn.execute("INSERT INTO appareils (client_id, nom_machine) VALUES (1, 'POSTE-Apres-Crash')")
conn.commit()
nouvel_id = conn.execute(
    "SELECT id FROM appareils WHERE nom_machine='POSTE-Apres-Crash'").fetchone()[0]
journalise = conn.execute(
    "SELECT COUNT(*) FROM _sync_journal WHERE tbl='appareils' AND record_id=? AND action='INSERT'",
    (nouvel_id,)).fetchone()[0]
verifier(journalise == 1,
         "une écriture locale après purge est bien journalisée (les triggers refonctionnent)",
         str(journalise))

print("\n=== 5. _sync_deletions / _trg_del_* : mécanisme legacy bien retiré ===")
del_triggers = conn.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE '_trg_del_%'"
).fetchone()[0]
verifier(del_triggers == 0,
         "aucun trigger _trg_del_* — mécanisme legacy jamais relu par la sync actuelle, retiré",
         str(del_triggers))
conn.close()

print()
if echecs:
    print('ÉCHECS : %d' % len(echecs))
    for e in echecs:
        print('  - ' + e)
    sys.exit(1)
else:
    print('TOUT OK')
