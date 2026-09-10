#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Régression : la synchronisation Turso ne doit plus faire « disparaître »
une modification d'appareil quelques minutes après sa saisie.

Contexte (signalé en usage réel, multi-instance Docker + PC) : PULL s'exécute
AVANT PUSH et réappliquait aveuglément la ligne distante par-dessus la ligne
locale. Deux conséquences :

  1. une édition locale encore en attente de push était écrasée par le pull,
     puis le push renvoyait la version écrasée sur Turso (perte définitive) ;
  2. une instance en retard qui touche un seul champ (recalcul de santé,
     ping…) repoussait la ligne ENTIÈRE périmée — un champ édité ailleurs
     revenait à une vieille valeur (« croisement entre rubriques »).

Ce test fige le correctif `database._proteger_versions_locales`.

Usage : python test_sync_versions_locales.py
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


_APPAREILS_DDL = """CREATE TABLE appareils (
    id INTEGER PRIMARY KEY,
    client_id INTEGER,
    nom_machine TEXT,
    type_appareil TEXT,
    modele TEXT,
    localisation TEXT,
    sante_niveau TEXT,
    date_maj TEXT)"""

_JOURNAL_DDL = """CREATE TABLE _sync_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl TEXT NOT NULL, record_id TEXT NOT NULL, action TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    UNIQUE(tbl, record_id, action) ON CONFLICT REPLACE)"""


def _db():
    c = sqlite3.connect(':memory:')
    c.execute(_APPAREILS_DDL)
    c.execute(_JOURNAL_DDL)
    c.commit()
    return c


def _appareil(c, **kw):
    cols = ('id', 'client_id', 'nom_machine', 'type_appareil', 'modele',
            'localisation', 'sante_niveau', 'date_maj')
    d = {k: kw.get(k) for k in cols}
    c.execute("INSERT OR REPLACE INTO appareils (%s) VALUES (%s)"
              % (','.join(cols), ','.join('?' * len(cols))),
              [d[k] for k in cols])
    c.commit()


def _jrn(c, rid, action='UPDATE', ts='2026-09-10T08:14:00'):
    c.execute("INSERT INTO _sync_journal (tbl, record_id, action, timestamp) "
              "VALUES ('appareils', ?, ?, ?)", (str(rid), action, ts))
    c.commit()


def _type(c, rid):
    return c.execute("SELECT type_appareil FROM appareils WHERE id=?", (rid,)).fetchone()[0]


def _modele(c, rid):
    return c.execute("SELECT modele FROM appareils WHERE id=?", (rid,)).fetchone()[0]


# ── 1. Ligne distante périmée (date_maj plus ancien) : la locale est conservée ──
print("=== 1. Une écriture de fond périmée sur une autre instance n'écrase plus "
      "les champs édités ici ===")
local, turso = _db(), _db()
# L'utilisateur a édité la fiche ici à 08:14 et ça a DÉJÀ été poussé sur Turso.
_appareil(local, id=42, client_id=1, nom_machine='192.168.0.151', type_appareil='TV',
          modele='FreeBox Mini 4K', localisation='Chambre', date_maj='2026-09-10T08:14:00')
_appareil(turso, id=42, client_id=1, nom_machine='192.168.0.151', type_appareil='TV',
          modele='FreeBox Mini 4K', localisation='Chambre', date_maj='2026-09-10T08:14:00')
# Une instance en retard recalcule la santé : elle repousse SA ligne entière,
# périmée (vieux type, modèle vide, date_maj de la veille) + une entrée de journal.
_appareil(turso, id=42, client_id=1, nom_machine='192.168.0.151', type_appareil='Camera IP',
          modele='', localisation='', sante_niveau='ok', date_maj='2026-09-09T09:00:00')
_jrn(turso, 42, ts='2026-09-10T08:20:00')

stats, errors = D._sync_using_journal(local, turso)
verifier(not errors, 'sync sans erreur', str(errors))
verifier(_type(local, 42) == 'TV', "le type édité localement est conservé", _type(local, 42))
verifier(_modele(local, 42) == 'FreeBox Mini 4K',
         "le modèle édité localement est conservé", _modele(local, 42))
verifier(stats.get('appareils', {}).get('versions_locales_conservees') == 1,
         "la protection est comptée dans les stats")
verifier(_type(turso, 42) == 'TV',
         "Turso est corrigé : la bonne version y est repoussée", _type(turso, 42))
verifier(_modele(turso, 42) == 'FreeBox Mini 4K',
         "y compris le modèle", _modele(turso, 42))

# ── 2. Édition locale encore en attente de push : le pull ne l'écrase pas ──
print("\n=== 2. Une édition locale non encore poussée survit à un pull concurrent ===")
local, turso = _db(), _db()
_appareil(local, id=7, client_id=1, nom_machine='PC-01', type_appareil='TV',
          modele='X', date_maj='2026-09-10T08:00:00')
_jrn(local, 7)   # <-- édition locale en attente
# Turso a une version différente, même PLUS récente, + entrée de journal.
_appareil(turso, id=7, client_id=1, nom_machine='PC-01', type_appareil='Camera IP',
          modele='Y', date_maj='2026-09-11T09:00:00')
_jrn(turso, 7, ts='2026-09-10T09:00:00')

stats, errors = D._sync_using_journal(local, turso)
verifier(not errors, 'sync sans erreur', str(errors))
verifier(_type(local, 7) == 'TV', "l'édition locale en attente est préservée", _type(local, 7))
verifier(_type(turso, 7) == 'TV',
         "et elle est poussée sur Turso (elle gagne)", _type(turso, 7))
verifier(local.execute("SELECT COUNT(*) FROM _sync_journal").fetchone()[0] == 0,
         "le journal local est purgé après un push réussi")

# ── 3. Cas normal : sans conflit local, la ligne distante est bien appliquée ──
print("\n=== 3. Sans édition locale concurrente, le pull s'applique normalement ===")
local, turso = _db(), _db()
_appareil(local, id=9, client_id=1, nom_machine='OLD', type_appareil='PC',
          modele='', date_maj='2026-09-01T00:00:00')
_appareil(turso, id=9, client_id=1, nom_machine='NEW', type_appareil='Serveur',
          modele='R740', date_maj='2026-09-10T10:00:00')
_jrn(turso, 9, ts='2026-09-10T10:00:00')

stats, errors = D._sync_using_journal(local, turso)
verifier(not errors, 'sync sans erreur', str(errors))
verifier(_type(local, 9) == 'Serveur', "la mise à jour distante légitime est appliquée",
         _type(local, 9))
verifier(local.execute("SELECT nom_machine FROM appareils WHERE id=9").fetchone()[0] == 'NEW',
         "tous les champs distants sont répliqués")

# ── 4. Un DELETE distant n'est jamais bloqué par la protection ──
print("\n=== 4. Une suppression faite ailleurs prime toujours ===")
local, turso = _db(), _db()
_appareil(local, id=5, client_id=1, nom_machine='A-SUPPRIMER', type_appareil='PC',
          date_maj='2026-09-10T08:00:00')
_jrn(local, 5)   # édition locale en attente...
_jrn(turso, 5, action='DELETE', ts='2026-09-10T09:00:00')   # ...mais supprimé ailleurs

stats, errors = D._sync_using_journal(local, turso)
verifier(not errors, 'sync sans erreur', str(errors))
verifier(local.execute("SELECT COUNT(*) FROM appareils WHERE id=5").fetchone()[0] == 0,
         "l'appareil est bien supprimé localement malgré l'édition locale en attente")

print()
if echecs:
    print('ÉCHECS : %d' % len(echecs))
    for e in echecs:
        print('  - ' + e)
    sys.exit(1)
print('TOUT OK')
