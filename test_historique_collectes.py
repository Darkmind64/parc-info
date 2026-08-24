#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie l'historique des collectes et les tendances qu'on en tire.

Ce que le test contrôle :
  - chaque collecte laisse un relevé, sans écraser les précédents
  - l'historique est borné (les plus anciens relevés partent)
  - la table est suivie par la synchronisation
  - un disque qui se remplit donne une date de saturation plausible
  - un disque stable n'en donne aucune
  - deux relevés rapprochés ne suffisent pas à conclure
  - les logiciels ajoutés et retirés sont détectés, accents compris
  - un changement de numéro de série ou de mémoire est signalé

Usage :
    python test_historique_collectes.py
"""

import io
import json
import os
import sys
import tempfile
from datetime import timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='historique_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


A.init_db()
conn = A.get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (1, 'admin', 'x', 'Administrateur', 'admin', 1)")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client Éprouvé')")
conn.execute("INSERT INTO appareils (id, client_id, nom_machine) VALUES (1, 1, 'POSTE-Réception')")
conn.commit()
conn.close()

print('=== 1. Chaque collecte laisse un relevé ===')
conn = A.get_db()
for i in range(3):
    A._enregistrer_collecte(conn, 1, 1, {
        'disk_total_gb': 500, 'disk_used_gb': 100 + i, 'disk_free_gb': 400 - i,
        'ram_gb': 16, 'installed_software': [{'name': 'Suite bureautique', 'version': '2024'}],
        'os_version': '10.0.26100', 'serial_number': 'ABC123',
    })
conn.commit()
compte = conn.execute("SELECT COUNT(*) FROM collectes WHERE appareil_id=1").fetchone()[0]
conn.close()
verifier(compte == 3, 'trois relevés conservés', '%d' % compte)

print('\n=== 2. La table est suivie par la synchronisation ===')
conn = A.get_db()
triggers = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '%collectes%'"
).fetchall()]
suivies = conn.execute("SELECT COUNT(*) FROM _sync_journal WHERE tbl='collectes'").fetchone()[0]
conn.close()
verifier(len(triggers) == 3, 'déclencheurs présents', str(len(triggers)))
verifier(suivies >= 3, 'relevés marqués pour la synchronisation', '%d' % suivies)

print('\n=== 3. L\'historique est borné ===')
conn = A.get_db()
base = A._utcnow() - timedelta(days=200)
for i in range(A.COLLECTES_CONSERVEES + 15):
    quand = (base + timedelta(days=i)).isoformat(timespec='seconds')
    conn.execute(
        "INSERT OR REPLACE INTO collectes (cle, appareil_id, client_id, horodatage, "
        "disque_total_go, disque_libre_go, nb_logiciels, logiciels) VALUES (?,?,?,?,?,?,?,?)",
        ('2|%s' % quand, 2, 1, quand, 500, 400, 10, '[]'))
conn.commit()
A._enregistrer_collecte(conn, 1, 2, {'disk_total_gb': 500, 'disk_free_gb': 399,
                                     'installed_software': []})
conn.commit()
restants = conn.execute("SELECT COUNT(*) FROM collectes WHERE appareil_id=2").fetchone()[0]
conn.close()
verifier(restants == A.COLLECTES_CONSERVEES, 'au plus %d relevés' % A.COLLECTES_CONSERVEES,
         '%d' % restants)

print('\n=== 4. Tendance du disque ===')


def releves_synthetiques(depart_libre, par_jour, jours, pas=7):
    """Relevés espacés de `pas` jours, l'espace libre variant de `par_jour`."""
    sortie = []
    origine = A._utcnow() - timedelta(days=jours)
    for j in range(0, jours + 1, pas):
        sortie.append({'horodatage': (origine + timedelta(days=j)).isoformat(timespec='seconds'),
                       'disque_libre_go': depart_libre + par_jour * j})
    return list(reversed(sortie))   # l'appelant fournit du plus récent au plus ancien


qui_se_remplit = A._tendance_disque(releves_synthetiques(200, -1.0, 60))
verifier(qui_se_remplit is not None, 'tendance calculée sur un disque qui se remplit')
if qui_se_remplit:
    verifier(qui_se_remplit.get('saturation') is not None, 'date de saturation annoncée',
             str(qui_se_remplit.get('saturation')))
    # 140 Go restants à 1 Go/jour : la date doit tomber autour de 140 jours.
    jours = qui_se_remplit.get('jours_restants', 0)
    verifier(120 <= jours <= 160, 'échéance cohérente avec le rythme', '%d jours' % jours)

stable = A._tendance_disque(releves_synthetiques(200, 0.0, 60))
verifier(stable is not None and stable.get('saturation') is None,
         'disque stable : aucune date annoncée')

qui_se_libere = A._tendance_disque(releves_synthetiques(100, 0.5, 60))
verifier(qui_se_libere is not None and qui_se_libere.get('saturation') is None,
         'espace qui augmente : aucune date annoncée')

trop_court = A._tendance_disque(releves_synthetiques(200, -5.0, 3, pas=1))
verifier(trop_court is None, 'moins d\'une semaine de recul : aucune conclusion')

verifier(A._tendance_disque([]) is None, 'aucun relevé : aucune conclusion')

print('\n=== 5. Comparaison de deux relevés ===')
recent = {'logiciels': json.dumps(['Éditeur de texte|3.1', 'Antivirus|12',
                                   'Nouveau logiciel|1.0'], ensure_ascii=False),
          'os_version': '10.0.26200', 'ram_go': 32, 'numero_serie': 'XYZ999',
          'cpu': 'Intel Core i7'}
avant = {'logiciels': json.dumps(['Éditeur de texte|3.0', 'Antivirus|12',
                                  'Ancien logiciel|2.2'], ensure_ascii=False),
         'os_version': '10.0.26100', 'ram_go': 16, 'numero_serie': 'ABC123',
         'cpu': 'Intel Core i7'}
diff = A._comparer_collectes(recent, avant)
verifier(any('Nouveau logiciel' in n for n in diff['ajoutes']), 'logiciel ajouté détecté',
         str(diff['ajoutes']))
verifier(any('Ancien logiciel' in n for n in diff['retires']), 'logiciel retiré détecté',
         str(diff['retires']))
verifier(any('Éditeur de texte 3.1' == n for n in diff['ajoutes'])
         and any('Éditeur de texte 3.0' == n for n in diff['retires']),
         'changement de version vu des deux côtés, accents compris')
champs = {c['champ'] for c in diff['materiel']}
verifier('Numéro de série' in champs, 'changement de numéro de série signalé', str(champs))
verifier('Mémoire (Go)' in champs, 'changement de mémoire signalé')
verifier('Processeur' not in champs, 'processeur inchangé : rien signalé')

print('\n=== 6. Restitution par la fiche système ===')
import time  # noqa: E402

client = A.app.test_client()
with client.session_transaction() as session:
    session['auth_user_id'] = 1
    session['client_id'] = 1
reponse = client.get('/appareil/1/fiche-systeme')
html = reponse.get_data(as_text=True)
verifier(reponse.status_code == 200, 'fiche servie', str(reponse.status_code))
verifier('Évolution' in html, 'rubrique Évolution présente')

conn = A.get_db()
donnees = A.historique_appareil(conn, 1, 1)
conn.close()
verifier(donnees and donnees['nombre'] == 3, 'historique relu depuis la base',
         str(donnees and donnees['nombre']))
verifier(donnees and donnees['comparaison'] is not None,
         'comparaison disponible dès deux relevés')

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
