#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie le journal des mises à jour, partagé entre les postes.

Points contrôlés :
  - une mise à jour est consignée avec le nom de l'appareil et les deux versions
  - la table est bien suivie par la synchronisation (sinon les autres postes ne
    verront jamais rien, ce qui est tout l'objet de la fonction)
  - deux machines mises à jour le même jour ne s'écrasent pas
  - un échec est consigné avec son motif
  - la page Journal affiche le tout, accents compris, et distingue ce poste

Usage :
    python test_journal_maj.py
"""

import io
import os
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='journal_maj_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A            # noqa: E402
from database import log_maj_event  # noqa: E402

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
conn.commit()
conn.close()

print('=== 1. Enregistrement des mises à jour ===')
log_maj_event('NAS-Synologie', '2.6.42', '2.6.43', mode='docker')
log_maj_event('POSTE-Réception', '2.6.41', '2.6.43', mode='windows')
log_maj_event('MacBook-Éric', '2.6.42', '2.6.43', mode='macos', statut='echec',
              detail="Empreinte SHA-256 introuvable : installation refusée")

conn = A.get_db()
lignes = conn.execute("SELECT machine, version_avant, version_apres, mode, statut, detail "
                      "FROM journal_maj ORDER BY machine").fetchall()
verifier(len(lignes) == 3, 'trois entrées enregistrées', '%d trouvée(s)' % len(lignes))
par_machine = {r[0]: r for r in lignes}
verifier('POSTE-Réception' in par_machine, 'nom de machine accentué préservé',
         ', '.join(sorted(par_machine)))
poste = par_machine.get('POSTE-Réception')
verifier(poste and poste[1] == '2.6.41' and poste[2] == '2.6.43',
         'versions d\'origine et d\'arrivée conservées')
mac = par_machine.get('MacBook-Éric')
verifier(mac and mac[4] == 'echec' and 'Empreinte' in (mac[5] or ''),
         'échec consigné avec son motif')

print('\n=== 2. La table est suivie par la synchronisation ===')
# Sans ces déclencheurs, rien ne serait répliqué et les autres postes ne
# verraient jamais ces lignes — ce qui viderait la fonction de son sens.
triggers = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '%journal_maj%'"
).fetchall()]
verifier(len(triggers) == 3, 'déclencheurs insert/update/delete présents', str(triggers))
suivies = conn.execute(
    "SELECT COUNT(*) FROM _sync_journal WHERE tbl='journal_maj'").fetchone()[0]
verifier(suivies == 3, 'les trois lignes sont marquées pour la synchronisation',
         '%d marquée(s)' % suivies)

print('\n=== 3. Deux machines le même jour ne s\'écrasent pas ===')
# Une clé auto-incrémentée aurait produit le même identifiant sur chaque
# instance, et la synchronisation aurait gardé une seule des deux lignes.
avant = conn.execute("SELECT COUNT(*) FROM journal_maj").fetchone()[0]
conn.close()
log_maj_event('POSTE-A', '2.6.43', '2.6.44', mode='windows')
log_maj_event('POSTE-B', '2.6.43', '2.6.44', mode='windows')
conn = A.get_db()
apres = conn.execute("SELECT COUNT(*) FROM journal_maj").fetchone()[0]
verifier(apres == avant + 2, 'les deux entrées coexistent',
         '%d → %d' % (avant, apres))

print('\n=== 4. Rejouer le même démarrage ne duplique pas ===')
log_maj_event('POSTE-A', '2.6.43', '2.6.44', mode='windows')
encore = conn.execute("SELECT COUNT(*) FROM journal_maj").fetchone()[0]
verifier(encore == apres, 'aucun doublon', '%d entrées' % encore)
conn.close()

print('\n=== 5. Affichage dans la page Journal ===')
client = A.app.test_client()
with client.session_transaction() as session:
    session['auth_user_id'] = 1
    session['login_time'] = None
reponse = client.get('/journal-synchronisation')
html = reponse.get_data(as_text=True)
verifier(reponse.status_code == 200, 'page servie', str(reponse.status_code))
for attendu in ('NAS-Synologie', 'POSTE-Réception', 'MacBook-Éric',
                '2.6.42', 'Installée', 'Échec', 'Empreinte SHA-256'):
    verifier(attendu in html, 'affiché : %s' % attendu)
verifier('maj-locale' in html or 'ce poste' in html,
         'la machine courante est distinguée')

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
