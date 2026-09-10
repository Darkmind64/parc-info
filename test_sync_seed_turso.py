#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Régression : la copie INITIALE des données vers Turso.

Le `_sync_journal` ne propage que les écritures postérieures à la pose de ses
triggers. Une table ajoutée à `_TRACKED_JOURNAL` après coup (`appareil_macs`,
`licences_appareils`, `client_instantane`…) restait **vide sur Turso
indéfiniment** — signalé en usage réel. `database._seed_tables_vides_sur_turso`
journalise, une seule fois par table et par instance, toutes les lignes locales
d'une table suivie qui est encore vide sur Turso.

Usage : python test_sync_seed_turso.py
"""

import sqlite3
import sys

import database as D

echecs = []


def verifier(cond, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if cond else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not cond:
        echecs.append(libelle)


_JOURNAL_DDL = """CREATE TABLE _sync_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl TEXT NOT NULL, record_id TEXT NOT NULL, action TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    UNIQUE(tbl, record_id, action) ON CONFLICT REPLACE)"""


def _mk(with_trigger=True):
    c = sqlite3.connect(':memory:')
    c.execute("CREATE TABLE appareil_macs (id INTEGER PRIMARY KEY, adresse_mac TEXT)")
    c.execute("CREATE TABLE contrats (id INTEGER PRIMARY KEY, titre TEXT, date_maj TEXT)")
    c.execute(_JOURNAL_DDL)
    c.execute("CREATE TABLE _sync_meta (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE _sync_applying (id INTEGER PRIMARY KEY)")
    if with_trigger:
        for t in ('appareil_macs', 'contrats'):
            c.execute(f"""CREATE TRIGGER _trg_journal_upd_{t} AFTER UPDATE ON {t}
                WHEN NOT EXISTS (SELECT 1 FROM _sync_applying) BEGIN
                    INSERT OR REPLACE INTO _sync_journal (tbl,record_id,action,timestamp)
                    VALUES ('{t}', NEW.id, 'UPDATE', datetime('now')); END""")
    c.commit()
    return c


# ── 1. Une table suivie vide sur Turso est seedée depuis le local ──
print("=== 1. Copie initiale d'une table restée vide sur Turso ===")
local, turso = _mk(), _mk()
for i in range(1, 6):
    local.execute("INSERT INTO appareil_macs (id, adresse_mac) VALUES (?, ?)",
                  (i, f'aa:bb:cc:00:00:0{i}'))
local.commit()
# Turso a la table (créée par _ensure_turso_schema) mais aucune ligne.

stats, errors = D._sync_using_journal(local, turso)
verifier(not errors, 'sync sans erreur', str(errors))
n = turso.execute("SELECT COUNT(*) FROM appareil_macs").fetchone()[0]
verifier(n == 5, "les 5 lignes locales sont copiées sur Turso", str(n))
verifier(stats.get('appareil_macs', {}).get('seed') == 5, "le seed est compté dans les stats")

# ── 2. Idempotent : deuxième cycle, rien de plus, pas de re-seed ──
print("\n=== 2. Le seed ne se rejoue pas au cycle suivant ===")
local.execute("INSERT INTO appareil_macs (id, adresse_mac) VALUES (6, 'aa:bb:cc:00:00:06')")
local.commit()   # ligne ajoutée SANS passer par un trigger INSERT (pas de journal)
stats2, errors2 = D._sync_using_journal(local, turso)
verifier(not errors2, 'sync sans erreur', str(errors2))
verifier('seed' not in stats2.get('appareil_macs', {}),
         "pas de nouveau seed (drapeau _sync_meta posé)")
verifier(turso.execute("SELECT COUNT(*) FROM appareil_macs").fetchone()[0] == 5,
         "la 6e ligne n'est PAS renvoyée par le seed (le drapeau protège)")

# ── 3. Table déjà peuplée sur Turso : jamais réécrite ──
print("\n=== 3. Une table déjà peuplée sur Turso n'est pas touchée par le seed ===")
local, turso = _mk(), _mk()
local.execute("INSERT INTO contrats (id, titre, date_maj) VALUES (1, 'LOCAL', '2026-01-01')")
local.commit()
turso.execute("INSERT INTO contrats (id, titre, date_maj) VALUES (1, 'DISTANT', '2026-05-01')")
turso.commit()
stats3, errors3 = D._sync_using_journal(local, turso)
verifier(not errors3, 'sync sans erreur', str(errors3))
verifier(turso.execute("SELECT titre FROM contrats WHERE id=1").fetchone()[0] == 'DISTANT',
         "la ligne distante existante est intacte (pas de seed par-dessus)")
verifier('seed' not in stats3.get('contrats', {}), "aucun seed pour une table déjà peuplée")

print()
if echecs:
    print('ÉCHECS : %d' % len(echecs))
    for e in echecs:
        print('  - ' + e)
    sys.exit(1)
print('TOUT OK')
