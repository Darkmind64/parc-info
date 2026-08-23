#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie que can_write() est réellement appliqué sur la création, la
modification et la suppression des trois entités CRUD les plus sensibles
(appareil, contrat, identifiant) — constat de l'audit du 2026-08-23 : le
mécanisme d'ACL le plus critique du projet était aussi le moins testé,
test_corrections_audit_secondaires.py ne couvrant que la suppression
d'entrées d'historique.

Pour chaque entité, avec un utilisateur en accès 'lecture' (jamais en
écriture) sur le client actif :
  - la création n'insère aucune ligne
  - la modification ne change aucune valeur
  - la suppression ne retire aucune ligne
Puis, avec le même utilisateur promu en accès 'ecriture' sur le même client,
les trois mêmes opérations réussissent (non-régression du chemin légitime).

Usage :
    python test_acl_crud.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='acl_crud_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A               # noqa: E402
from database import get_db   # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


A.init_db()
conn = get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (2, 'utilisateur', 'x', 'Utilisateur test', 'user', 1)")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client test')")
conn.execute("INSERT OR REPLACE INTO client_partages (client_id, auth_user_id, niveau) VALUES (1, 2, 'lecture')")
conn.commit()
conn.close()

client = A.app.test_client()
CSRF = 'test-csrf-token'
with client.session_transaction() as s:
    s['auth_user_id'] = 2
    s['client_id'] = 1
    s['csrf_token'] = CSRF


def niveau(n):
    conn = get_db()
    conn.execute("UPDATE client_partages SET niveau=? WHERE client_id=1 AND auth_user_id=2", (n,))
    conn.commit()
    conn.close()


def compte(table, ou='client_id=1'):
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM %s WHERE %s" % (table, ou)).fetchone()[0]
    conn.close()
    return n


# ─── APPAREIL ───────────────────────────────────────────────────────────────

print("=== Appareil — création ===")
niveau('lecture')
avant = compte('appareils')
client.post('/appareil/nouveau', data={'nom_machine': 'PC-LECTURE', 'csrf_token': CSRF}, follow_redirects=True)
verifier(compte('appareils') == avant, "lecture seule : aucun appareil créé")

niveau('ecriture')
avant = compte('appareils')
client.post('/appareil/nouveau', data={'nom_machine': 'PC-ECRITURE', 'csrf_token': CSRF}, follow_redirects=True)
verifier(compte('appareils') == avant + 1, "écriture : l'appareil est bien créé")
conn = get_db()
appareil_id = conn.execute("SELECT id FROM appareils WHERE client_id=1 AND nom_machine='PC-ECRITURE'").fetchone()[0]
conn.close()

print()
print("=== Appareil — modification ===")
niveau('lecture')
client.post('/appareil/%d/editer' % appareil_id, data={'nom_machine': 'PWNED', 'csrf_token': CSRF}, follow_redirects=True)
conn = get_db()
nom = conn.execute("SELECT nom_machine FROM appareils WHERE id=?", (appareil_id,)).fetchone()[0]
conn.close()
verifier(nom == 'PC-ECRITURE', "lecture seule : le nom n'a pas changé", nom)

niveau('ecriture')
client.post('/appareil/%d/editer' % appareil_id, data={'nom_machine': 'PC-RENOMME', 'csrf_token': CSRF}, follow_redirects=True)
conn = get_db()
nom = conn.execute("SELECT nom_machine FROM appareils WHERE id=?", (appareil_id,)).fetchone()[0]
conn.close()
verifier(nom == 'PC-RENOMME', "écriture : la modification passe bien", nom)

print()
print("=== Appareil — suppression ===")
niveau('lecture')
client.post('/appareil/%d/supprimer' % appareil_id, data={'csrf_token': CSRF}, follow_redirects=True)
verifier(compte('appareils', 'id=%d' % appareil_id) == 1, "lecture seule : l'appareil existe toujours")

niveau('ecriture')
client.post('/appareil/%d/supprimer' % appareil_id, data={'csrf_token': CSRF}, follow_redirects=True)
verifier(compte('appareils', 'id=%d' % appareil_id) == 0, "écriture : la suppression passe bien")


# ─── CONTRAT ────────────────────────────────────────────────────────────────

print()
print("=== Contrat — création ===")
niveau('lecture')
avant = compte('contrats')
client.post('/contrat/nouveau', data={'titre': 'Contrat lecture', 'csrf_token': CSRF}, follow_redirects=True)
verifier(compte('contrats') == avant, "lecture seule : aucun contrat créé")

niveau('ecriture')
avant = compte('contrats')
client.post('/contrat/nouveau', data={'titre': 'Contrat ecriture', 'csrf_token': CSRF}, follow_redirects=True)
verifier(compte('contrats') == avant + 1, "écriture : le contrat est bien créé")
conn = get_db()
contrat_id = conn.execute("SELECT id FROM contrats WHERE client_id=1 AND titre='Contrat ecriture'").fetchone()[0]
conn.close()

print()
print("=== Contrat — modification ===")
niveau('lecture')
client.post('/contrat/%d/editer' % contrat_id, data={'titre': 'PWNED', 'csrf_token': CSRF}, follow_redirects=True)
conn = get_db()
titre = conn.execute("SELECT titre FROM contrats WHERE id=?", (contrat_id,)).fetchone()[0]
conn.close()
verifier(titre == 'Contrat ecriture', "lecture seule : le titre n'a pas changé", titre)

niveau('ecriture')
client.post('/contrat/%d/editer' % contrat_id, data={'titre': 'Contrat renomme', 'csrf_token': CSRF}, follow_redirects=True)
conn = get_db()
titre = conn.execute("SELECT titre FROM contrats WHERE id=?", (contrat_id,)).fetchone()[0]
conn.close()
verifier(titre == 'Contrat renomme', "écriture : la modification passe bien", titre)

print()
print("=== Contrat — suppression ===")
niveau('lecture')
client.post('/contrat/%d/supprimer' % contrat_id, data={'csrf_token': CSRF}, follow_redirects=True)
verifier(compte('contrats', 'id=%d' % contrat_id) == 1, "lecture seule : le contrat existe toujours")

niveau('ecriture')
client.post('/contrat/%d/supprimer' % contrat_id, data={'csrf_token': CSRF}, follow_redirects=True)
verifier(compte('contrats', 'id=%d' % contrat_id) == 0, "écriture : la suppression passe bien")


# ─── IDENTIFIANT ────────────────────────────────────────────────────────────

print()
print("=== Identifiant — création ===")
niveau('lecture')
avant = compte('identifiants')
client.post('/identifiant/nouveau', data={'nom': 'Ident lecture', 'csrf_token': CSRF}, follow_redirects=True)
verifier(compte('identifiants') == avant, "lecture seule : aucun identifiant créé")

niveau('ecriture')
avant = compte('identifiants')
client.post('/identifiant/nouveau', data={'nom': 'Ident ecriture', 'csrf_token': CSRF}, follow_redirects=True)
verifier(compte('identifiants') == avant + 1, "écriture : l'identifiant est bien créé")
conn = get_db()
identifiant_id = conn.execute("SELECT id FROM identifiants WHERE client_id=1 AND nom='Ident ecriture'").fetchone()[0]
conn.close()

print()
print("=== Identifiant — modification ===")
niveau('lecture')
client.post('/identifiant/%d/editer' % identifiant_id, data={'nom': 'PWNED', 'csrf_token': CSRF}, follow_redirects=True)
conn = get_db()
nom = conn.execute("SELECT nom FROM identifiants WHERE id=?", (identifiant_id,)).fetchone()[0]
conn.close()
verifier(nom == 'Ident ecriture', "lecture seule : le nom n'a pas changé", nom)

niveau('ecriture')
client.post('/identifiant/%d/editer' % identifiant_id, data={'nom': 'Ident renomme', 'csrf_token': CSRF}, follow_redirects=True)
conn = get_db()
nom = conn.execute("SELECT nom FROM identifiants WHERE id=?", (identifiant_id,)).fetchone()[0]
conn.close()
verifier(nom == 'Ident renomme', "écriture : la modification passe bien", nom)

print()
print("=== Identifiant — suppression ===")
niveau('lecture')
client.post('/identifiant/%d/supprimer' % identifiant_id, data={'csrf_token': CSRF}, follow_redirects=True)
verifier(compte('identifiants', 'id=%d' % identifiant_id) == 1, "lecture seule : l'identifiant existe toujours")

niveau('ecriture')
client.post('/identifiant/%d/supprimer' % identifiant_id, data={'csrf_token': CSRF}, follow_redirects=True)
verifier(compte('identifiants', 'id=%d' % identifiant_id) == 0, "écriture : la suppression passe bien")


print()
if echecs:
    print("ÉCHEC : %d contrôle(s) en échec" % len(echecs))
    for e in echecs:
        print("  - %s" % e)
    sys.exit(1)
else:
    print("TOUT OK")
