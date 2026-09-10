#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Régression : la synchronisation Turso ne doit plus faire « disparaître »
une modification d'appareil quelques minutes après sa saisie — SANS pour
autant empêcher les modifications légitimes de se synchroniser entre
instances (régression 2.32.9 corrigée en 2.32.10).

`database._proteger_versions_locales` (appliqué au PULL) :
  - table avec `date_maj` → `date_maj` fait autorité : on ne garde la version
    locale que si elle est strictement plus récente (ou égale + édition
    locale en attente). Un recalcul de santé / un ping ne touchant pas
    `date_maj` ne protège plus rien → une modif distante plus récente est
    bien appliquée.
  - table sans `date_maj` → une édition locale en attente de push prime.

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
    id INTEGER PRIMARY KEY, client_id INTEGER, nom_machine TEXT,
    type_appareil TEXT, modele TEXT, localisation TEXT,
    sante_niveau TEXT, date_maj TEXT)"""

_MACS_DDL = """CREATE TABLE appareil_macs (
    id INTEGER PRIMARY KEY, appareil_id INTEGER, adresse_mac TEXT, libelle TEXT)"""

_JOURNAL_DDL = """CREATE TABLE _sync_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl TEXT NOT NULL, record_id TEXT NOT NULL, action TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    UNIQUE(tbl, record_id, action) ON CONFLICT REPLACE)"""


def _db():
    c = sqlite3.connect(':memory:')
    c.execute(_APPAREILS_DDL)
    c.execute(_MACS_DDL)
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


def _jrn(c, tbl, rid, action='UPDATE', ts='2026-09-10T08:14:00'):
    c.execute("INSERT INTO _sync_journal (tbl, record_id, action, timestamp) "
              "VALUES (?, ?, ?, ?)", (tbl, str(rid), action, ts))
    c.commit()


def _val(c, rid, col='type_appareil'):
    r = c.execute(f"SELECT {col} FROM appareils WHERE id=?", (rid,)).fetchone()
    return r[0] if r else None


# ── 1. Écriture de fond périmée sur une autre instance : la locale prime ──
print("=== 1. Une écriture de fond périmée ailleurs n'écrase plus les champs édités ici ===")
local, turso = _db(), _db()
_appareil(local, id=42, client_id=1, nom_machine='192.168.0.151', type_appareil='TV',
          modele='FreeBox Mini 4K', localisation='Chambre', date_maj='2026-09-10T08:14:00')
_appareil(turso, id=42, client_id=1, nom_machine='192.168.0.151', type_appareil='TV',
          modele='FreeBox Mini 4K', localisation='Chambre', date_maj='2026-09-10T08:14:00')
# Instance en retard : recalcul de santé → repousse SA ligne entière, périmée
# (vieux type, modèle vide) SANS bumper date_maj → date_maj de la veille.
_appareil(turso, id=42, client_id=1, nom_machine='192.168.0.151', type_appareil='Camera IP',
          modele='', localisation='', sante_niveau='ok', date_maj='2026-09-09T09:00:00')
_jrn(turso, 'appareils', 42, ts='2026-09-10T08:20:00')

stats, errors = D._sync_using_journal(local, turso)
verifier(not errors, 'sync sans erreur', str(errors))
verifier(_val(local, 42) == 'TV', "le type édité localement est conservé", _val(local, 42))
verifier(_val(local, 42, 'modele') == 'FreeBox Mini 4K', "le modèle est conservé")
verifier(stats.get('appareils', {}).get('versions_locales_conservees') == 1,
         "protection comptée dans les stats")
verifier(_val(turso, 42) == 'TV', "Turso est corrigé (la bonne version y est repoussée)", _val(turso, 42))
verifier(_val(turso, 42, 'modele') == 'FreeBox Mini 4K', "y compris le modèle sur Turso")

# ── 2. RÉGRESSION 2.32.9 : une modif distante PLUS RÉCENTE doit s'appliquer ──
print("\n=== 2. Une modification faite sur une autre instance se synchronise bien ici ===")
local, turso = _db(), _db()
# Ici : dernière version connue à 08:00, PUIS un recalcul de santé local à
# 08:30 (ne touche pas date_maj) → laisse une entrée _sync_journal en attente.
_appareil(local, id=7, client_id=1, nom_machine='PC-01', type_appareil='PC',
          modele='X', sante_niveau='ok', date_maj='2026-09-10T08:00:00')
_jrn(local, 'appareils', 7, ts='2026-09-10T08:30:00')   # écriture de fond (santé)
# Une autre instance a VRAIMENT édité la fiche à 09:00 (date_maj bumpé).
_appareil(turso, id=7, client_id=1, nom_machine='PC-01-RENOMMÉ', type_appareil='Serveur',
          modele='R740', date_maj='2026-09-10T09:00:00')
_jrn(turso, 'appareils', 7, ts='2026-09-10T09:00:05')

stats, errors = D._sync_using_journal(local, turso)
verifier(not errors, 'sync sans erreur', str(errors))
verifier(_val(local, 7) == 'Serveur',
         "la modification distante plus récente est appliquée malgré l'entrée de journal locale",
         _val(local, 7))
verifier(_val(local, 7, 'nom_machine') == 'PC-01-RENOMMÉ', "tous les champs distants sont répliqués")

# ── 3. Édition locale la plus récente : elle prime et est poussée ──
print("\n=== 3. L'édition locale la plus récente gagne et part sur Turso ===")
local, turso = _db(), _db()
_appareil(local, id=9, client_id=1, nom_machine='PC', type_appareil='TV',
          modele='neuf', date_maj='2026-09-10T10:00:00')
_jrn(local, 'appareils', 9, ts='2026-09-10T10:00:00')
_appareil(turso, id=9, client_id=1, nom_machine='PC', type_appareil='Camera IP',
          modele='vieux', date_maj='2026-09-10T09:00:00')
_jrn(turso, 'appareils', 9, ts='2026-09-10T09:00:00')

stats, errors = D._sync_using_journal(local, turso)
verifier(not errors, 'sync sans erreur', str(errors))
verifier(_val(local, 9) == 'TV', "l'édition locale plus récente est conservée", _val(local, 9))
verifier(_val(turso, 9) == 'TV', "et poussée sur Turso", _val(turso, 9))
verifier(local.execute("SELECT COUNT(*) FROM _sync_journal").fetchone()[0] == 0,
         "journal local purgé après push")

# ── 4. Table SANS date_maj : une édition locale en attente prime ──
print("\n=== 4. Table sans date_maj (appareil_macs) : l'édition locale en attente prime ===")
local, turso = _db(), _db()
local.execute("INSERT INTO appareil_macs (id, appareil_id, adresse_mac, libelle) "
              "VALUES (3, 7, 'aa:bb:cc:dd:ee:01', 'saisie ici')")
local.commit()
_jrn(local, 'appareil_macs', 3)
turso.execute("INSERT INTO appareil_macs (id, appareil_id, adresse_mac, libelle) "
              "VALUES (3, 7, 'aa:bb:cc:dd:ee:99', 'ancienne')")
turso.commit()
_jrn(turso, 'appareil_macs', 3, ts='2026-09-10T09:00:00')

stats, errors = D._sync_using_journal(local, turso)
verifier(not errors, 'sync sans erreur', str(errors))
verifier(local.execute("SELECT adresse_mac FROM appareil_macs WHERE id=3").fetchone()[0]
         == 'aa:bb:cc:dd:ee:01', "la MAC saisie localement est conservée")
verifier(turso.execute("SELECT adresse_mac FROM appareil_macs WHERE id=3").fetchone()[0]
         == 'aa:bb:cc:dd:ee:01', "et poussée sur Turso")

# ── 5. Cas normal : sans conflit local, le pull s'applique ──
print("\n=== 5. Sans édition locale concurrente, le pull s'applique normalement ===")
local, turso = _db(), _db()
_appareil(local, id=1, client_id=1, nom_machine='OLD', type_appareil='PC', date_maj='2026-09-01T00:00:00')
_appareil(turso, id=1, client_id=1, nom_machine='NEW', type_appareil='Serveur', date_maj='2026-09-10T10:00:00')
_jrn(turso, 'appareils', 1, ts='2026-09-10T10:00:00')
stats, errors = D._sync_using_journal(local, turso)
verifier(not errors, 'sync sans erreur', str(errors))
verifier(_val(local, 1) == 'Serveur', "la mise à jour distante est appliquée", _val(local, 1))

# ── 6. Un DELETE distant n'est jamais bloqué ──
print("\n=== 6. Une suppression faite ailleurs prime toujours ===")
local, turso = _db(), _db()
_appareil(local, id=5, client_id=1, nom_machine='X', type_appareil='PC', date_maj='2026-09-10T08:00:00')
_jrn(local, 'appareils', 5)
_jrn(turso, 'appareils', 5, action='DELETE', ts='2026-09-10T09:00:00')
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
