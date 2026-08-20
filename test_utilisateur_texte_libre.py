#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie le signalement d'une saisie libre "Utilisateur" sans correspondance,
audit architecture 2026-08-20 : "une coquille ou un surnom casse le lien
silencieusement, sans le moindre signalement".

appareils.utilisateur reste un champ texte libre (aucune migration de
données, aucun changement de comportement pour l'existant) : le formulaire
propose désormais une autocomplétion (datalist) sur les utilisateurs connus
du client, et affiche un avertissement quand la saisie ne correspond à
aucune fiche — avec la même normalisation (accents, casse, ordre Nom/Prénom)
que _resolve_utilisateur_id(), qui fait le rattachement réel côté serveur.

Usage :
    python test_utilisateur_texte_libre.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='utilisateur_libre_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A   # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


A.init_db()
conn = A.get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (1, 'admin', 'x', 'Admin', 'admin', 1)")
conn.execute("INSERT OR IGNORE INTO clients (id, nom, auth_user_id) VALUES (1, 'Client Un', 1)")
conn.execute("INSERT INTO utilisateurs (client_id, prenom, nom, statut) VALUES (1, 'Éric', 'Dupont', 'actif')")
conn.commit(); conn.close()

print('=== 1. _utilisateurs_pour_formulaire() : noms affichables + variantes normalisées ===')
conn = A.get_db()
noms, variantes = A._utilisateurs_pour_formulaire(conn, 1)
conn.close()
verifier(noms == ['Éric Dupont'], "nom affichable au format Prénom Nom", str(noms))
verifier('eric dupont' in variantes, "variante Prénom Nom normalisée (accent retiré)", str(variantes))
verifier('dupont eric' in variantes, "variante Nom Prénom normalisée (ordre inversé)", str(variantes))
verifier('dupont' in variantes, "nom seul aussi accepté comme variante", str(variantes))

print('\n=== 2. Le formulaire appareil affiche datalist + variantes pour le JS ===')
client = A.app.test_client()
with client.session_transaction() as s:
    s['auth_user_id'] = 1
    s['client_id'] = 1
html = client.get('/appareil/nouveau').get_data(as_text=True)
verifier('datalist-utilisateurs' in html, "la datalist d'autocomplétion est bien présente")
verifier('Éric Dupont' in html, "le nom de l'utilisateur apparaît dans les options")
verifier('eric dupont' in html, "les variantes normalisées sont bien injectées pour le JS")
verifier('warn-utilisateur-inconnu' in html, "l'élément d'avertissement est bien présent dans le DOM")

print('\n=== 3. Cohérence avec la résolution réelle côté serveur (_resolve_utilisateur_id) ===')
conn = A.get_db()
# Les mêmes saisies que testées comme "variantes connues" doivent
# effectivement se résoudre côté serveur — sinon l'avertissement client
# mentirait (silencieux là où il devrait alerter, ou l'inverse).
for saisie in ('Éric Dupont', 'Dupont Éric', 'DUPONT', 'éric   dupont'):
    resolu = A._resolve_utilisateur_id(conn, 1, saisie)
    verifier(resolu is not None, f"'{saisie}' se résout bien côté serveur (cohérent avec l'avertissement JS)")
resolu_typo = A._resolve_utilisateur_id(conn, 1, 'Eric Dupon')  # coquille volontaire
verifier(resolu_typo is None,
          "une vraie coquille ('Dupon' sans t) ne se résout à rien — c'est ce cas que l'avertissement doit signaler")
conn.close()

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
