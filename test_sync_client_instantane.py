#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`client_instantane` (rapport « Changements depuis la dernière visite »)
entre dans la synchronisation Turso en v2.32.11.

Deux points vérifiés :
  1. `app._reclef_client_instantane` décale les id des instantanés DÉJÀ en base
     dans la plage propre à la machine (~2^48), une seule fois (drapeau
     `config`), sans passer par les triggers de journal, et remet
     `sqlite_sequence` en cohérence. Sans ça, deux instances qui ont chacune
     des instantanés (id 1, 2, 3…) se les écraseraient au premier seed.
  2. La table est bien suivie : `database._tables_suivies` la liste, et
     `_seed_tables_vides_sur_turso` la copie sur un Turso vide.

Usage : python test_sync_client_instantane.py
"""

import os
import sqlite3
import sys
import tempfile

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='inst_'))
os.environ.setdefault('RUNNING_IN_DOCKER', '1')
os.environ.setdefault('PARCINFO_BACKUP', '0')

import database as D  # noqa: E402

echecs = []


def verifier(cond, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if cond else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not cond:
        echecs.append(libelle)


# ── 1. Ré-indexation anti-collision d'une base qui a déjà des instantanés ──
print("=== 1. _reclef_client_instantane décale les id existants, une seule fois ===")
import app as A  # noqa: E402

conn = sqlite3.connect(':memory:')
conn.executescript("""
    CREATE TABLE config (cle TEXT PRIMARY KEY, valeur TEXT, date_maj TEXT);
    CREATE TABLE _sync_applying (id INTEGER PRIMARY KEY);
    CREATE TABLE client_instantane (
        id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER,
        horodatage TEXT, epoch REAL, origine TEXT, libelle TEXT,
        reference INTEGER DEFAULT 0, donnees_json TEXT DEFAULT '{}');
""")
for i in (1, 2, 3):
    conn.execute("INSERT INTO client_instantane (id, client_id, reference) VALUES (?, 7, ?)",
                 (i, 1 if i == 1 else 0))
conn.commit()   # AUTOINCREMENT -> sqlite_sequence('client_instantane', 3) auto-créé

OFFSET = 900000000000
A._reclef_client_instantane(conn, OFFSET)
conn.commit()

ids = sorted(r[0] for r in conn.execute("SELECT id FROM client_instantane").fetchall())
verifier(ids == [OFFSET + 1, OFFSET + 2, OFFSET + 3],
         "les 3 id sont décalés de l'offset", str(ids))
verifier(conn.execute("SELECT reference FROM client_instantane WHERE id=?",
                      (OFFSET + 1,)).fetchone()[0] == 1,
         "le drapeau `reference` (épinglé) suit sa ligne")
seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='client_instantane'").fetchone()[0]
verifier(seq == OFFSET + 3, "sqlite_sequence recalé sur le max", str(seq))
verifier(conn.execute("SELECT COUNT(*) FROM _sync_applying").fetchone()[0] == 0,
         "_sync_applying est bien revidé (triggers réactivés)")
flag = conn.execute("SELECT valeur FROM config WHERE cle='_client_instantane_reclef_v1'").fetchone()
verifier(bool(flag and flag[0]), "drapeau anti-rejeu posé dans config")

# 2e appel : ne doit RIEN refaire (sinon +offset une 2e fois)
A._reclef_client_instantane(conn, OFFSET)
conn.commit()
ids2 = sorted(r[0] for r in conn.execute("SELECT id FROM client_instantane").fetchall())
verifier(ids2 == ids, "un 2e appel ne re-décale pas", str(ids2))

# ── 2. Table suivie + copiée sur un Turso vide ──
print("\n=== 2. client_instantane est suivie et copiée sur Turso ===")

_J = """CREATE TABLE _sync_journal (id INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl TEXT, record_id TEXT, action TEXT, timestamp TEXT,
    UNIQUE(tbl, record_id, action) ON CONFLICT REPLACE)"""
_T = """CREATE TABLE client_instantane (id INTEGER PRIMARY KEY, client_id INTEGER,
    donnees_json TEXT)"""


def _db(trig=True):
    c = sqlite3.connect(':memory:')
    c.execute(_T); c.execute(_J)
    c.execute("CREATE TABLE _sync_meta (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE _sync_applying (id INTEGER PRIMARY KEY)")
    if trig:
        c.execute("""CREATE TRIGGER _trg_journal_upd_client_instantane
            AFTER UPDATE ON client_instantane
            WHEN NOT EXISTS (SELECT 1 FROM _sync_applying) BEGIN
                INSERT OR REPLACE INTO _sync_journal (tbl,record_id,action,timestamp)
                VALUES ('client_instantane', NEW.id, 'UPDATE', datetime('now')); END""")
    c.commit()
    return c


local, turso = _db(), _db()
verifier('client_instantane' in D._tables_suivies(local),
         "_tables_suivies() liste client_instantane")
for i in (900000000001, 900000000002):
    local.execute("INSERT INTO client_instantane (id, client_id, donnees_json) VALUES (?, 7, '{}')", (i,))
local.commit()

stats, errors = D._sync_using_journal(local, turso)
verifier(not errors, 'sync sans erreur', str(errors))
n = turso.execute("SELECT COUNT(*) FROM client_instantane").fetchone()[0]
verifier(n == 2, "les 2 instantanés locaux sont copiés sur Turso", str(n))

print()
if echecs:
    print('ÉCHECS : %d' % len(echecs))
    for e in echecs:
        print('  - ' + e)
    sys.exit(1)
print('TOUT OK')
