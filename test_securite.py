#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie le durcissement des dépôts de fichiers, de l'API collecteur et de la restauration.

Ce que le test contrôle :
  - seules les extensions de la liste blanche sont acceptées
  - un fichier dont le contenu dément son extension est refusé
  - la taille des requêtes est bornée
  - sans jeton configuré, les collecteurs déjà déployés continuent de passer
  - avec un jeton, l'API refuse qui ne le présente pas
  - la restauration ne charge que des sauvegardes existantes, prend un filet
    de sécurité, et remet réellement les données d'avant

Usage :
    python test_securite.py
"""

import io
import os
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='securite_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A                       # noqa: E402
from config_helpers import cfg_set    # noqa: E402

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
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (2, 'lecteur', 'x', 'Lecteur Éprouvé', 'user', 1)")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Société Générale & Cie')")
conn.commit()
conn.close()

client = A.app.test_client()


def connecter(user_id):
    with client.session_transaction() as session:
        session['auth_user_id'] = user_id
        session['client_id'] = 1


PDF = b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\ntrailer<</Root 1 0 R>>'
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64

print('=== 1. Extensions ===')
verifier(A.allowed_file('rapport.pdf'), 'pdf accepté')
verifier(A.allowed_file('photo.JPG'), 'extension en majuscules acceptée')
verifier(not A.allowed_file('charge.exe'), 'exe refusé')
verifier(not A.allowed_file('script.bat'), 'bat refusé')
verifier(not A.allowed_file('backdoor.php'), 'php refusé')
verifier(not A.allowed_file('sans_extension'), 'fichier sans extension refusé')
verifier(not A.allowed_file('rapport.pdf', A.ALLOWED_IMAGE_EXTENSIONS),
         'pdf refusé là où une image est attendue')

print('\n=== 2. Le contenu doit correspondre à l\'extension ===')


class FauxFichier:
    """Imite le minimum de werkzeug.FileStorage utilisé par la validation."""

    def __init__(self, nom, contenu):
        self.filename = nom
        self.stream = io.BytesIO(contenu)


ok, motif = A.verifier_fichier(FauxFichier('rapport.pdf', PDF))
verifier(ok, 'vrai PDF accepté', motif or '')
ok, motif = A.verifier_fichier(FauxFichier('rapport.pdf', b'MZ\x90\x00 en-tete d\'executable'))
verifier(not ok, 'exécutable renommé en .pdf refusé', motif or '')
ok, _ = A.verifier_fichier(FauxFichier('logo.png', PNG), A.ALLOWED_IMAGE_EXTENSIONS)
verifier(ok, 'vraie image acceptée')
ok, motif = A.verifier_fichier(FauxFichier('logo.png', PDF), A.ALLOWED_IMAGE_EXTENSIONS)
verifier(not ok, 'PDF renommé en .png refusé', motif or '')
ok, _ = A.verifier_fichier(FauxFichier('notes.txt', b'du texte quelconque'))
verifier(ok, 'format sans signature connue laissé passer')

print('\n=== 3. Taille des requêtes bornée ===')
verifier(A.app.config.get('MAX_CONTENT_LENGTH') == A.MAX_UPLOAD_MB * 1024 * 1024,
         'limite appliquée', '%d Mo' % A.MAX_UPLOAD_MB)

print('\n=== 4. API collecteur sans jeton configuré ===')
cfg_set('collecteur_token', '')
charge = {'mac_address': 'AA:BB:CC:DD:EE:01', 'hostname': 'POSTE-Éric',
          'client_id': 1, 'ip_addresses': ['192.168.1.10']}
reponse = client.post('/api/device-info', json=charge)
verifier(reponse.status_code == 200, 'collecteur accepté sans jeton',
         str(reponse.status_code))
verifier(client.get('/api/clients-public').status_code == 200,
         'liste des clients ouverte sans jeton')

print('\n=== 5. API collecteur avec jeton ===')
cfg_set('collecteur_token', 'jeton-Sécurisé-42')
reponse = client.post('/api/device-info', json=charge)
verifier(reponse.status_code == 401, 'refusé sans en-tête', str(reponse.status_code))
reponse = client.post('/api/device-info', json=charge,
                      headers={'Authorization': 'Bearer mauvais'})
verifier(reponse.status_code == 401, 'refusé avec un jeton faux', str(reponse.status_code))
reponse = client.post('/api/device-info', json=charge,
                      headers={'Authorization': 'Bearer jeton-Sécurisé-42'})
verifier(reponse.status_code == 200, 'accepté avec le bon jeton', str(reponse.status_code))
reponse = client.post('/api/device-info', json=charge,
                      headers={'X-Collector-Token': 'jeton-Sécurisé-42'})
verifier(reponse.status_code == 200, 'en-tête X-Collector-Token accepté aussi',
         str(reponse.status_code))
verifier(client.get('/api/clients-public').status_code == 401,
         'liste des clients protégée')
verifier(client.get('/api/clients-public',
                    headers={'Authorization': 'Bearer jeton-Sécurisé-42'}).status_code == 200,
         'liste des clients accessible avec le jeton')
cfg_set('collecteur_token', '')

print('\n=== 6. Restauration ===')
connecter(1)
depart = client.post('/api/db/sauvegarde')
verifier(depart.status_code == 200, 'sauvegarde de départ créée', str(depart.status_code))
fichier_depart = depart.get_json().get('fichier')

# Une donnée qui n'existe QUE après la sauvegarde : si la restauration marche,
# elle doit disparaître.
conn = A.get_db()
conn.execute("INSERT INTO clients (id, nom) VALUES (77, 'Client ajouté après coup')")
conn.commit()
conn.close()
conn = A.get_db()
present = conn.execute("SELECT COUNT(*) FROM clients WHERE id=77").fetchone()[0]
conn.close()
verifier(present == 1, 'donnée ajoutée après la sauvegarde')

connecter(2)
verifier(client.post('/api/db/sauvegarde/restaurer',
                     json={'fichier': fichier_depart}).status_code == 403,
         'restauration refusée à un compte non administrateur')

connecter(1)
for tentative in ('../../parc_info.db', 'inexistant.db', ''):
    code = client.post('/api/db/sauvegarde/restaurer', json={'fichier': tentative}).status_code
    verifier(code == 404, 'chemin refusé : %r' % tentative, str(code))

avant = len(A._backup_files())
reponse = client.post('/api/db/sauvegarde/restaurer', json={'fichier': fichier_depart})
donnees = reponse.get_json()
verifier(reponse.status_code == 200 and donnees.get('ok'), 'restauration effectuée',
         str(donnees))
verifier(bool(donnees.get('filet')), 'sauvegarde de sécurité prise avant',
         str(donnees.get('filet')))

conn = A.get_db()
restant = conn.execute("SELECT COUNT(*) FROM clients WHERE id=77").fetchone()[0]
integrite = conn.execute("PRAGMA integrity_check").fetchone()[0]
accentue = conn.execute("SELECT nom FROM clients WHERE id=1").fetchone()[0]
conn.close()
verifier(restant == 0, "la donnée ajoutée après la sauvegarde a bien disparu")
verifier(integrite == 'ok', 'base restaurée intègre', integrite)
verifier(accentue == 'Société Générale & Cie', 'accents intacts après restauration',
         accentue)

print('\n=== 7. Clés de récupération BitLocker ===')
CLE = '123456-234567-345678-456789-567890-678901-789012-890123'
charge_bl = {
    'mac_address': 'AA:BB:CC:00:11:22', 'hostname': 'POSTE-Réception', 'client_id': 1,
    'ip_addresses': ['192.168.1.50'],
    'system_report': {
        'os_build': '26100', 'hostname': 'POSTE-Réception',
        'bitlocker_keys': [{'volume': 'C:', 'identifiant': 'abc-123', 'protection': 'On',
                            'chiffrement': 'XtsAes128', 'cle': CLE}],
    },
}
reponse = client.post('/api/device-info', json=charge_bl)
verifier(reponse.status_code == 200, 'collecte acceptée', str(reponse.status_code))
appareil_id = (reponse.get_json() or {}).get('device_id')

conn = A.get_db()
rapport = conn.execute('SELECT rapport_systeme_json FROM appareils WHERE id=?',
                       (appareil_id,)).fetchone()[0] or ''
stockee = conn.execute('SELECT valeur FROM cles_recuperation WHERE appareil_id=? AND volume=?',
                       (appareil_id, 'C:')).fetchone()
conn.close()

# Le rapport est repris tel quel dans le PDF joint à l'appareil : une clé de
# déverrouillage de disque n'a rien à y faire.
verifier('bitlocker_keys' not in rapport, 'les clés sont retirées du rapport stocké')
verifier(CLE not in rapport, 'aucune clé en clair dans le rapport')
verifier(bool(stockee), 'clé enregistrée dans sa table dédiée')
verifier(bool(stockee) and CLE not in (stockee[0] or ''), 'clé chiffrée au repos')

connecter(1)
reponse = client.get('/api/appareil/%d/cle-bitlocker?volume=C:' % appareil_id)
verifier(reponse.status_code == 200 and (reponse.get_json() or {}).get('cle') == CLE,
         'clé restituée en clair à la demande')
verifier(client.get('/api/appareil/%d/cle-bitlocker?volume=Z:' % appareil_id).status_code == 404,
         'volume inconnu refusé')

# Cloisonnement multi-client. Un identifiant de client inexistant ne prouverait
# rien : get_client_id() le rejette et retombe sur un client accessible. Il faut
# donc un second client bien réel pour éprouver l'isolement.
conn = A.get_db()
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (2, 'Autre client')")
conn.commit()
conn.close()
with client.session_transaction() as session:
    session['client_id'] = 2
verifier(client.get('/api/appareil/%d/cle-bitlocker?volume=C:' % appareil_id).status_code == 404,
         "clé invisible depuis un autre client")
with client.session_transaction() as session:
    session['client_id'] = 1
verifier(client.get('/api/appareil/%d/cle-bitlocker?volume=C:' % appareil_id).status_code == 200,
         'clé de nouveau visible depuis son client')

conn = A.get_db()
consultations = conn.execute(
    "SELECT COUNT(*) FROM historique WHERE action LIKE '%Consultation%BitLocker%'").fetchone()[0]
conn.close()
verifier(consultations >= 1, 'chaque consultation est tracée', '%d' % consultations)

print("\n=== 8. La fiche appareil s'affiche ===")
# Régression de la 2.8.0 : la requête des clés BitLocker avait été placée après
# la fermeture de la connexion, et toute édition d'appareil répondait 500.
connecter(1)
conn = A.get_db()
conn.execute("INSERT INTO appareils (id, client_id, nom_machine) "
             "VALUES (55, 1, 'POSTE-Sans-Chiffrement')")
conn.commit()
conn.close()
reponse = client.get('/appareil/55/editer')
verifier(reponse.status_code == 200, 'édition sans clé BitLocker', str(reponse.status_code))

reponse = client.get('/appareil/%d/editer' % appareil_id)
page = reponse.get_data(as_text=True)
verifier(reponse.status_code == 200, 'édition avec clé BitLocker', str(reponse.status_code))
verifier('Clés de récupération BitLocker' in page, 'le bloc BitLocker est affiché')
verifier(CLE not in page, "la clé n'est jamais dans le HTML de la page")

print('\n=== 9. Qui peut lancer une mise à jour ===')
# Ouvert à tout compte connecté : sur un poste de travail, celui qui utilise
# l'application est rarement celui qui porte le rôle d'administrateur.
connecter(2)
reponse = client.post('/api/updates/install')
verifier(reponse.status_code != 403,
         "un compte non administrateur n'est plus refusé",
         '%s — %s' % (reponse.status_code, (reponse.get_json() or {}).get('erreur')))
verifier(client.post('/api/updates/check').status_code in (200, 202),
         'un compte non administrateur peut vérifier')

reponse = client.get('/api/updates/status')
verifier(reponse.status_code == 200, "l'état reste lisible par tous")

print("\n=== 10. Décodage de l'état BitLocker ===")
import collector_core as CC  # noqa: E402

# Windows renvoie des entiers sur les versions anciennes et des libellés sur les
# récentes. Non décodés, ils s'affichaient en « D:: 0 (Protection: 0) ».
for etat, protection, etat_attendu, protection_attendue, protege_attendu in (
        ('0', '0', 'Non chiffré', 'Désactivée', False),
        ('1', '1', 'Chiffré', 'Activée', True),
        ('FullyEncrypted', 'On', 'Chiffré', 'Activée', True),
        ('FullyDecrypted', 'Off', 'Non chiffré', 'Désactivée', False),
        ('2', '2', 'Chiffrement en cours', 'Inconnue', False)):
    lu_etat = CC._ETATS_VOLUME.get(etat, etat)
    lu_protection = CC._PROTECTIONS_VOLUME.get(protection, protection)
    protege = protection in ('1', 'On')
    verifier(lu_etat == etat_attendu and lu_protection == protection_attendue
             and protege == protege_attendu,
             'état %r/%r décodé' % (etat, protection),
             '%s / %s' % (lu_etat, lu_protection))

# Un état inconnu ne doit pas être inventé : il ressort tel quel.
verifier(CC._ETATS_VOLUME.get('42', '42') == '42', 'état inconnu laissé tel quel')

eteint = {'bitlocker_actif': False, 'bitlocker_volumes': [
    {'volume': 'C:', 'etat': 'Non chiffré', 'protection': 'Désactivée', 'protege': False}]}
titres = [a['titre'] for a in CC.build_alerts(eteint)]
verifier(any('Aucun volume' in t for t in titres),
         'BitLocker désactivé remonte dans les points de vigilance', str(titres[:3]))

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
