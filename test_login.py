#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie /login et le timeout de session — le seul point d'entrée non
authentifié de l'application n'avait jusqu'ici aucune couverture de test
(constat de l'audit du 2026-08-23).

Ce que le test contrôle :
  - identifiants corrects : session ouverte, redirection vers la page suivante
  - identifiants incorrects : session non ouverte, message d'erreur générique
  - un compte désactivé (actif=0) ne peut pas se connecter
  - rate-limiting : après 10 échecs pour une même IP, la 11e tentative est
    bloquée en 5 minutes, même avec les bons identifiants
  - `next` : seule une destination relative est honorée, un `next` pointant
    vers un autre domaine retombe sur `/` (protection open-redirect déjà en
    place, non testée jusqu'ici)
  - timeout de session (8h) : une session dont `login_time` date de plus de
    8h se retrouve déconnectée à la requête suivante, une session récente
    reste valide

Usage :
    python test_login.py
"""

import io
import os
import sys
import tempfile
from datetime import timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='login_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A                          # noqa: E402
from database import get_db              # noqa: E402
from auth_utils import hash_pwd, reset_attempts  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


A.init_db()
conn = get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (1, 'technicien', ?, 'Technicien', 'user', 1)", (hash_pwd('BonMotDePasse!42'),))
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (2, 'suspendu', ?, 'Compte suspendu', 'user', 0)", (hash_pwd('BonMotDePasse!42'),))
conn.commit()
conn.close()

IP = '203.0.113.7'  # adresse de test (TEST-NET-3, RFC 5737) — dédiée à ce test


def nouveau_client():
    c = A.app.test_client()
    c.environ_base['REMOTE_ADDR'] = IP
    return c


print("=== 1. Identifiants corrects ===")
reset_attempts(IP)
client = nouveau_client()
r = client.post('/login', data={'login': 'technicien', 'password': 'BonMotDePasse!42'})
verifier(r.status_code in (301, 302, 303), "redirection après connexion réussie", str(r.status_code))
with client.session_transaction() as s:
    verifier(s.get('auth_user_id') == 1, "session ouverte pour le bon utilisateur", str(s.get('auth_user_id')))
    verifier(bool(s.get('login_time')), "login_time enregistré en session")

print()
print("=== 2. Identifiants incorrects ===")
reset_attempts(IP)
client = nouveau_client()
r = client.post('/login', data={'login': 'technicien', 'password': 'MauvaisMotDePasse'})
verifier(r.status_code == 200, "la page se réaffiche (pas de redirection)", str(r.status_code))
verifier(b'Identifiants incorrects' in r.data, "message d'erreur générique affiché")
with client.session_transaction() as s:
    verifier(not s.get('auth_user_id'), "aucune session ouverte")

print()
print("=== 3. Compte désactivé ===")
reset_attempts(IP)
client = nouveau_client()
r = client.post('/login', data={'login': 'suspendu', 'password': 'BonMotDePasse!42'})
with client.session_transaction() as s:
    verifier(not s.get('auth_user_id'), "un compte actif=0 ne peut pas se connecter, même avec le bon mot de passe")

print()
print("=== 4. Rate-limiting (10 tentatives / 5 min) ===")
reset_attempts(IP)
client = nouveau_client()
for i in range(10):
    client.post('/login', data={'login': 'technicien', 'password': 'faux'})
r = client.post('/login', data={'login': 'technicien', 'password': 'BonMotDePasse!42'})
verifier(b'Trop de tentatives' in r.data,
         "la 11e tentative est bloquée par le rate-limit, même avec les bons identifiants")
with client.session_transaction() as s:
    verifier(not s.get('auth_user_id'), "aucune session ouverte malgré les bons identifiants")
reset_attempts(IP)  # ne pas polluer la suite du test

print()
print("=== 5. Une connexion réussie réinitialise le compteur d'échecs ===")
client = nouveau_client()
for i in range(9):
    client.post('/login', data={'login': 'technicien', 'password': 'faux'})
r = client.post('/login', data={'login': 'technicien', 'password': 'BonMotDePasse!42'})
with client.session_transaction() as s:
    verifier(s.get('auth_user_id') == 1, "la 10e tentative (bonne) réussit encore avant le seuil")
# Après un succès, de nouveaux échecs repartent d'un compteur à zéro.
client2 = nouveau_client()
for i in range(9):
    client2.post('/login', data={'login': 'technicien', 'password': 'faux'})
r2 = client2.post('/login', data={'login': 'technicien', 'password': 'BonMotDePasse!42'})
with client2.session_transaction() as s:
    verifier(s.get('auth_user_id') == 1,
              "le compteur repart de zéro après une connexion réussie précédente")
reset_attempts(IP)

print()
print("=== 6. Protection open-redirect sur `next` ===")
reset_attempts(IP)
client = nouveau_client()
r = client.post('/login?next=https://exemple-malicieux.test/vol',
                 data={'login': 'technicien', 'password': 'BonMotDePasse!42'})
location = r.headers.get('Location', '')
verifier('exemple-malicieux.test' not in location,
         "un `next` pointant vers un autre domaine n'est jamais suivi", location)

print()
print("=== 7. Timeout de session (8h) ===")
client = nouveau_client()
with client.session_transaction() as s:
    s['auth_user_id'] = 1
    s['auth_user_role'] = 'user'
    s['login_time'] = (A._utcnow() - timedelta(hours=9)).isoformat()
r = client.get('/apropos', follow_redirects=True)
with client.session_transaction() as s:
    verifier(not s.get('auth_user_id'), "une session de plus de 8h est invalidée à la requête suivante")
verifier(b'session a expir' in r.data, "message d'expiration affiché à l'utilisateur")

client2 = nouveau_client()
with client2.session_transaction() as s:
    s['auth_user_id'] = 1
    s['auth_user_role'] = 'user'
    s['login_time'] = (A._utcnow() - timedelta(hours=1)).isoformat()
r2 = client2.get('/apropos')
with client2.session_transaction() as s:
    verifier(s.get('auth_user_id') == 1, "une session récente (moins de 8h) reste valide")

print()
if echecs:
    print("ÉCHEC : %d contrôle(s) en échec" % len(echecs))
    for e in echecs:
        print("  - %s" % e)
    sys.exit(1)
else:
    print("TOUT OK")
