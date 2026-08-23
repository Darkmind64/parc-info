#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie le cloisonnement multi-client sur editer_appareil/supprimer_appareil
(audit du 2026-08-23).

Avant correctif, ces deux routes ne filtraient les appareils que par leur
`id`, jamais par `client_id` — ni sur les SELECT, ni sur les UPDATE/DELETE.
Un utilisateur avec un accès en écriture sur un seul client pouvait donc
consulter, modifier ou supprimer l'appareil de n'importe quel autre client en
devinant/incrémentant l'id dans l'URL, malgré le modèle ACL multi-client qui
est censé rendre ça impossible.

Ce que le test contrôle :
  - GET  /appareil/<id>/editer    sur un appareil d'un autre client : redirige
    sans jamais exposer ses données (nom machine absent de la réponse)
  - POST /appareil/<id>/editer    sur un appareil d'un autre client : aucune
    modification en base
  - POST /appareil/<id>/supprimer sur un appareil d'un autre client : aucune
    suppression en base
  - Contrôle positif : les deux routes fonctionnent toujours normalement une
    fois le bon client actif en session (pas de régression du chemin légitime)

Usage :
    python test_isolation_client_appareil.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='isolation_appareil_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A                       # noqa: E402
from database import get_db           # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


A.init_db()
conn = get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (2, 'attaquant', 'x', 'Utilisateur', 'user', 1)")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client A')")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (2, 'Client B (victime)')")
# Accès écriture sur le client 1 uniquement — aucun accès au client 2.
conn.execute("INSERT OR REPLACE INTO client_partages (client_id, auth_user_id, niveau) "
             "VALUES (1, 2, 'ecriture')")
conn.execute("INSERT INTO appareils (id, client_id, nom_machine) VALUES (99, 2, 'PC-VICTIME')")
conn.commit()
conn.close()

client = A.app.test_client()
CSRF = 'test-csrf-token'

with client.session_transaction() as s:
    s['auth_user_id'] = 2
    s['client_id'] = 1   # client actif = A, la cible appartient à B
    s['csrf_token'] = CSRF

print("=== 1. GET editer_appareil sur un appareil d'un autre client ===")
r = client.get('/appareil/99/editer', follow_redirects=True)
verifier(r.status_code == 200, "réponse OK (page de redirection)", str(r.status_code))
verifier(b'PC-VICTIME' not in r.data, "les données de l'appareil victime ne sont pas exposées")

print()
print("=== 2. POST editer_appareil sur un appareil d'un autre client ===")
client.post('/appareil/99/editer', data={'nom_machine': 'PWNED', 'csrf_token': CSRF},
            follow_redirects=True)
conn = get_db()
row = conn.execute("SELECT nom_machine, client_id FROM appareils WHERE id=99").fetchone()
conn.close()
verifier(row is not None, "l'appareil victime existe toujours")
verifier(row and row[0] == 'PC-VICTIME', "le nom n'a pas été modifié par l'attaquant", str(tuple(row) if row else row))
verifier(row and row[1] == 2, "l'appareil appartient toujours au client 2", str(tuple(row) if row else row))

print()
print("=== 3. POST supprimer_appareil sur un appareil d'un autre client ===")
client.post('/appareil/99/supprimer', data={'csrf_token': CSRF}, follow_redirects=True)
conn = get_db()
row2 = conn.execute("SELECT id FROM appareils WHERE id=99").fetchone()
conn.close()
verifier(row2 is not None, "l'appareil victime n'a pas été supprimé")

print()
print("=== 4. Contrôle positif : édition légitime (bon client actif) ===")
conn = get_db()
conn.execute("INSERT OR REPLACE INTO client_partages (client_id, auth_user_id, niveau) "
             "VALUES (2, 2, 'ecriture')")
conn.commit()
conn.close()
with client.session_transaction() as s:
    s['client_id'] = 2   # cette fois, accès légitime au client propriétaire
r3 = client.post('/appareil/99/editer', data={'nom_machine': 'RENOMME-LEGITIME', 'csrf_token': CSRF},
                  follow_redirects=True)
conn = get_db()
row3 = conn.execute("SELECT nom_machine FROM appareils WHERE id=99").fetchone()
conn.close()
verifier(r3.status_code == 200, "réponse OK", str(r3.status_code))
verifier(row3 and row3[0] == 'RENOMME-LEGITIME',
         "l'édition légitime fonctionne toujours", str(tuple(row3) if row3 else row3))

print()
print("=== 5. Contrôle positif : suppression légitime (bon client actif) ===")
client.post('/appareil/99/supprimer', data={'csrf_token': CSRF}, follow_redirects=True)
conn = get_db()
row4 = conn.execute("SELECT id FROM appareils WHERE id=99").fetchone()
conn.close()
verifier(row4 is None, "la suppression légitime fonctionne toujours")

print()
if echecs:
    print("ÉCHEC : %d contrôle(s) en échec" % len(echecs))
    for e in echecs:
        print("  - %s" % e)
    sys.exit(1)
else:
    print("TOUT OK")
