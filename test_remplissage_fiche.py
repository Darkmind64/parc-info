#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie le remplissage automatique de la fiche appareil depuis la collecte.

Ce que le test contrôle :
  - les champs Utilisateur, Marque et Nom d'antivirus se remplissent tout seuls
  - une valeur saisie par un technicien n'est JAMAIS écrasée
  - le rapprochement avec les listes curées tombe juste, et ne force rien quand
    la valeur détectée est inconnue
  - les appareils déjà collectés sont rattrapés sans nouvelle collecte
  - le rattrapage ne s'exécute qu'une fois

Usage :
    python test_remplissage_fiche.py
"""

import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='remplissage_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A                              # noqa: E402
from config_helpers import LISTE_DEFAULTS    # noqa: E402

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
conn.commit()
conn.close()

client = A.app.test_client()

RAPPORT = {
    'hostname': 'POSTE-Réception',
    'logged_on_user': 'MONDOMAINE\\Éric',
    'antivirus': 'Windows Defender',
    'antivirus_products': [{'name': 'Windows Defender', 'enabled': True, 'status': 'Actif'}],
}

print('=== 1. Rapprochement avec les listes curées ===')
for detecte, marque_attendue in (
        ('Windows Defender', 'Windows Defender'),
        ('Bitdefender Endpoint Security Tools', 'Bitdefender'),
        ('ESET Endpoint Antivirus', 'ESET'),
        ('Kaspersky Endpoint Security', 'Kaspersky')):
    trouve = A._rapprocher_liste(detecte, LISTE_DEFAULTS['marques_antivirus'])
    verifier(trouve == marque_attendue, 'marque déduite de %r' % detecte,
             trouve or '(aucune)')

# Un produit inconnu ne doit pas être rapproché de force d'une entrée de la
# liste : mieux vaut la valeur brute qu'une marque fausse.
inconnu = A._rapprocher_liste('Antivirus maison SARL', LISTE_DEFAULTS['marques_antivirus'])
verifier(not inconnu, 'produit inconnu : aucun rapprochement forcé', inconnu or '(aucun)')

print('\n=== 2. Champs déduits, et respect de la saisie manuelle ===')
conn = A.get_db()
vierge = A.champs_deduits_du_collecteur(conn, 1, RAPPORT, {})
verifier(vierge.get('utilisateur') == 'Éric', 'utilisateur déduit, domaine retiré',
         str(vierge.get('utilisateur')))
verifier(vierge.get('av_marque') == 'Windows Defender', 'marque antivirus déduite',
         str(vierge.get('av_marque')))
verifier('Microsoft Defender' in (vierge.get('av_nom') or ''), 'nom antivirus déduit',
         str(vierge.get('av_nom')))

saisi = A.champs_deduits_du_collecteur(conn, 1, RAPPORT, {
    'utilisateur': 'Corrigé à la main', 'av_marque': 'ESET'})
verifier('utilisateur' not in saisi, "l'utilisateur saisi n'est pas écrasé", str(saisi))
verifier('av_marque' not in saisi, "la marque saisie n'est pas écrasée")
verifier('av_nom' in saisi, 'les champs restés vides sont tout de même remplis')

# Une chaîne d'espaces vaut un champ vide : sinon un champ « effacé » resterait
# bloqué à jamais sans jamais se remplir.
espaces = A.champs_deduits_du_collecteur(conn, 1, RAPPORT, {'utilisateur': '   '})
verifier('utilisateur' in espaces, 'un champ rempli d\'espaces est traité comme vide')
conn.close()

print('\n=== 3. Bout en bout : collecte puis formulaire ===')
charge = {'mac_address': 'AA:BB:CC:11:22:33', 'hostname': 'POSTE-Réception',
          'client_id': 1, 'ip_addresses': ['192.168.1.60'], 'system_report': RAPPORT}
reponse = client.post('/api/device-info', json=charge)
verifier(reponse.status_code == 200, 'collecte acceptée', str(reponse.status_code))
appareil_id = (reponse.get_json() or {}).get('device_id')

conn = A.get_db()
ligne = conn.execute('SELECT utilisateur, av_marque, av_nom FROM appareils WHERE id=?',
                     (appareil_id,)).fetchone()
conn.close()
verifier(ligne and ligne[0] == 'Éric', 'utilisateur enregistré', str(ligne and ligne[0]))
verifier(ligne and ligne[1] == 'Windows Defender', 'marque enregistrée')

with client.session_transaction() as session:
    session['auth_user_id'] = 1
    session['client_id'] = 1
page = client.get('/appareil/%s/editer' % appareil_id).get_data(as_text=True)
verifier('value="Éric"' in page, 'le formulaire affiche l\'utilisateur')
verifier('value="Windows Defender"' in page, 'le formulaire affiche la marque')

print('\n=== 4. Une seconde collecte n\'écrase pas une correction ===')
conn = A.get_db()
conn.execute("UPDATE appareils SET utilisateur='Corrigé par le technicien' WHERE id=?",
             (appareil_id,))
conn.commit()
conn.close()
client.post('/api/device-info', json=charge)
conn = A.get_db()
apres = conn.execute('SELECT utilisateur FROM appareils WHERE id=?', (appareil_id,)).fetchone()[0]
conn.close()
verifier(apres == 'Corrigé par le technicien', 'la correction survit à une nouvelle collecte',
         apres)

print('\n=== 5. Rattrapage des appareils déjà collectés ===')
# Un appareil dont le rapport est en base mais dont les champs sont restés vides,
# comme tous ceux collectés avant cette version.
conn = A.get_db()
conn.execute(
    "INSERT INTO appareils (client_id, nom_machine, rapport_systeme_json, utilisateur, av_marque) "
    "VALUES (1, 'POSTE-Ancien', ?, '', '')",
    (json.dumps({'logged_on_user': 'MONDOMAINE\\Sophie',
                 'antivirus': 'ESET Endpoint Antivirus'}, ensure_ascii=False),))
conn.commit()
ancien_id = conn.execute(
    "SELECT id FROM appareils WHERE nom_machine='POSTE-Ancien'").fetchone()[0]
conn.close()

# La section 3 a servi une page, ce qui a déjà déclenché le rattrapage et posé
# son marqueur. On simule ici une base qui ne l'a pas encore subi.
from config_helpers import cfg_set  # noqa: E402

cfg_set(A._CLE_RATTRAPAGE_FICHES, '')
A._rattrapage_fait = False
completes = A.completer_fiches_existantes()
verifier(completes >= 1, 'au moins une fiche complétée', '%d' % completes)

conn = A.get_db()
ligne = conn.execute('SELECT utilisateur, av_marque FROM appareils WHERE id=?',
                     (ancien_id,)).fetchone()
conn.close()
verifier(ligne and ligne[0] == 'Sophie', 'utilisateur rattrapé', str(ligne and ligne[0]))
verifier(ligne and ligne[1] == 'ESET', 'marque rattrapée', str(ligne and ligne[1]))

A._rattrapage_fait = False
verifier(A.completer_fiches_existantes() == 0, 'le rattrapage ne rejoue pas')

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
