#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recherche globale (palette Ctrl+K) — `search_utils`.

  - `_normaliser_terme` : IP / MAC (avec ou sans séparateurs) / numéro
  - `_score` : exact (100) > préfixe (60) > sous-chaîne (30)
  - `search_global(query, client_ids, actif_id)` : borné aux `client_ids`
    fournis (l'ACL est faite par l'appelant), client actif classé d'abord,
    MAC secondaires reconnues, mot de passe jamais renvoyé.

Usage : python test_recherche.py
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='recherche_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A            # noqa: E402
import search_utils        # noqa: E402
from search_utils import _normaliser_terme, _score, search_global  # noqa: E402

A.init_db()
echecs = []


def verifier(cond, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if cond else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not cond:
        echecs.append(libelle)


print('=== 1. _normaliser_terme ===')
verifier(_normaliser_terme('192.168.1.10')['ip'] == '192.168.1.10', 'IPv4 valide reconnue')
verifier(_normaliser_terme('300.1.1.1')['ip'] is None, 'IPv4 invalide -> pas d\'IP')
verifier(_normaliser_terme('aa:bb:cc:dd:ee:ff')['mac'] == 'aabbccddeeff', 'MAC avec « : »')
verifier(_normaliser_terme('AA-BB-CC-DD-EE-FF')['mac'] == 'aabbccddeeff', 'MAC avec « - » et majuscules')
verifier(_normaliser_terme('aabbccddeeff')['mac'] == 'aabbccddeeff', 'MAC sans séparateur')
verifier(_normaliser_terme('bureau 2')['mac'] is None, 'texte libre -> pas de MAC')

print('\n=== 2. _score : exact > préfixe > sous-chaîne ===')
t = _normaliser_terme('nas')
verifier(_score(t, 'nas') == 100, 'exact = 100')
verifier(_score(t, 'nas-datacenter') == 60, 'préfixe = 60')
verifier(_score(t, 'vieux-nas-01') == 30, 'sous-chaîne = 30')
verifier(_score(t, 'switch') == 0, 'aucune correspondance = 0')

print('\n=== 3. search_global : périmètre, classement, MAC, mot de passe ===')
conn = A.get_db()
ca = conn.execute("INSERT INTO clients (nom, date_creation) VALUES ('Client A', '2026-01-01')").lastrowid
cb = conn.execute("INSERT INTO clients (nom, date_creation) VALUES ('Client B', '2026-01-01')").lastrowid
for cid in (ca, cb):
    conn.execute("INSERT INTO appareils (client_id, nom_machine, type_appareil) "
                 "VALUES (?, 'PC-OMEGA', 'PC')", (cid,))
aid = conn.execute("INSERT INTO appareils (client_id, nom_machine, adresse_ip, adresse_mac) "
                   "VALUES (?, 'SRV-MAC', '10.9.9.9', 'AA:BB:CC:11:22:33')", (ca,)).lastrowid
conn.execute("INSERT INTO appareil_macs (appareil_id, client_id, adresse_mac, source, date_maj) "
             "VALUES (?,?, 'DD:EE:FF:44:55:66', 'manuel', '')", (aid, ca))
crypto = A._get_crypto_shared()
conn.execute("INSERT INTO identifiants (client_id, nom, login, mot_de_passe) VALUES (?,?,?,?)",
             (ca, 'Portail Sophos', 'admin', crypto.encrypt('MotDePasseUltraSecret')))
conn.commit()
conn.close()

r_actif = search_global('OMEGA', [ca], actif_id=ca)
verifier(len(r_actif['appareils']) == 1 and r_actif['appareils'][0]['client_id'] == ca,
         'périmètre = [A] -> seul l\'appareil de A')

r_tous = search_global('OMEGA', [ca, cb], actif_id=ca)
verifier({x['client_id'] for x in r_tous['appareils']} == {ca, cb}, 'périmètre [A,B] -> les deux')
verifier(r_tous['appareils'][0]['client_id'] == ca, 'client actif classé en premier')

for terme in ('aa:bb:cc:11:22:33', 'AABBCC112233', 'dd-ee-ff-44-55-66'):
    verifier(any(x['id'] == aid for x in search_global(terme, [ca], actif_id=ca)['appareils']),
             'MAC « %s » -> trouve SRV-MAC' % terme)

r_ip = search_global('10.9.9.9', [ca], actif_id=ca)
verifier(r_ip['appareils'] and r_ip['appareils'][0]['id'] == aid, 'IP exacte -> l\'appareil')

r_id = search_global('Sophos', [ca], actif_id=ca)
verifier(r_id['identifiants'] and r_id['identifiants'][0]['titre'] == 'Portail Sophos',
         'identifiant trouvé par son nom')
verifier('MotDePasseUltraSecret' not in json.dumps(r_id), 'le mot de passe n\'est jamais renvoyé')

r_cli = search_global('Client A', [ca, cb], actif_id=ca)
verifier(any(x['id'] == ca and x['url'].endswith('/selectionner') for x in r_cli['clients']),
         'client trouvé par son nom -> URL de sélection')

r_court = search_global('a', [ca], actif_id=ca)
verifier(r_court['total'] == 0, 'terme < 2 caractères -> rien')

print()
if echecs:
    print('%d ÉCHEC(S) : %s' % (len(echecs), ', '.join(echecs)))
    sys.exit(1)
print('Recherche globale : tous les contrôles passent.')
