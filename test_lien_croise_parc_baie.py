#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie le lien croisé entre parc_general (résumé switch/routeur/UPS) et
la baie de brassage (position physique par U), audit architecture
2026-08-20 : les deux décrivaient le même matériel sans jamais se recouper.

La page /baie liait déjà vers /parc ("Config baie"). Il manquait le sens
inverse : /parc affiche désormais un lien vers /baie, avec le nombre
d'emplacements réellement positionnés dans la baie — pas qu'un simple
lien statique, une vraie donnée live des deux côtés.

Usage :
    python test_lien_croise_parc_baie.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='lien_croise_')
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
conn.execute("INSERT INTO parc_general (client_id, nom_site, switch_marque, baie_nb_u) "
             "VALUES (1, 'Site', 'HP ProCurve 2530', 12)")
conn.commit(); conn.close()

client = A.app.test_client()
with client.session_transaction() as s:
    s['auth_user_id'] = 1
    s['client_id'] = 1

print('=== 1. /parc : lien vers /baie présent, sans compteur quand la baie est vide ===')
html = client.get('/parc').get_data(as_text=True)
verifier('href="/baie"' in html, "un lien vers /baie est bien présent sur la page parc_general")
verifier('emplacement' not in html.split('href="/baie"')[1][:150],
          "aucun compteur affiché quand aucun équipement n'est positionné dans la baie")

print('\n=== 2. /parc : le compteur reflète les emplacements réellement positionnés ===')
conn = A.get_db()
conn.execute("INSERT INTO baie_slots (client_id, position, hauteur_u, nom_custom, type_equipement) "
             "VALUES (1, 1, 1, 'Switch HP', 'switch')")
conn.execute("INSERT INTO baie_slots (client_id, position, hauteur_u, nom_custom, type_equipement) "
             "VALUES (1, 3, 1, 'Routeur Cisco', 'routeur')")
conn.commit(); conn.close()
html = client.get('/parc').get_data(as_text=True)
verifier('2 emplacements positionnés' in html,
          "le compteur affiche bien 2 emplacements après ajout de 2 slots occupés")

print('\n=== 3. /baie : le lien retour vers /parc existe toujours (sens déjà en place) ===')
html_baie = client.get('/baie').get_data(as_text=True)
verifier('href="/parc"' in html_baie, "la page baie de brassage lie toujours vers /parc")

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
