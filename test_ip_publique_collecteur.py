#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie la collecte de l'IP publique et de l'opérateur (FAI).

Demandé explicitement : que le collecteur relève l'IP publique de chaque
poste (et l'opérateur si possible), et que la fiche appareil en soit
renseignée.

Ce que le test contrôle :
  - collector_core.get_public_ip_info() lit bien ip/org depuis le service
    externe, et ne casse jamais la collecte en cas de panne réseau ou de
    réponse inattendue (même contrat que le reste de measure_network)
  - get_api_payload() transmet ces deux champs au serveur
  - /api/device-info les enregistre dans adresse_ip_publique /
    operateur_ip_publique, à la création ET à la mise à jour
  - contrairement aux champs "déduits" (av_marque, utilisateur...), une
    IP publique différente lors d'une collecte suivante REMPLACE
    l'ancienne — c'est une IP publique qui change (poste itinérant),
    pas une correction manuelle à préserver

Usage :
    python test_ip_publique_collecteur.py
"""

import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='ip_publique_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import collector_core as CC  # noqa: E402
import app as A              # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


class _FausseReponse:
    def __init__(self, contenu):
        self._contenu = contenu.encode('utf-8')

    def read(self):
        return self._contenu

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


print('=== 1. get_public_ip_info() : cas normal ===')
_urlopen_original = CC.urlopen
CC.urlopen = lambda req, timeout=None: _FausseReponse(
    json.dumps({'ip': '203.0.113.42', 'org': 'Orange S.A.'}))
resultat = CC.get_public_ip_info()
verifier(resultat.get('public_ip') == '203.0.113.42', 'IP publique lue', str(resultat.get('public_ip')))
verifier(resultat.get('public_ip_isp') == 'Orange S.A.', 'opérateur lu', str(resultat.get('public_ip_isp')))

print('\n=== 2. get_public_ip_info() : service en panne, ne casse jamais la collecte ===')
def _panne(req, timeout=None):
    raise OSError('réseau injoignable')
CC.urlopen = _panne
resultat_panne = CC.get_public_ip_info()
verifier(resultat_panne == {}, 'dict vide en cas de panne, aucune exception', str(resultat_panne))

print('\n=== 3. get_public_ip_info() : réponse avec erreur signalée par le service ===')
CC.urlopen = lambda req, timeout=None: _FausseReponse(json.dumps({'error': True, 'reason': 'rate limited'}))
resultat_erreur = CC.get_public_ip_info()
verifier(resultat_erreur == {}, 'dict vide quand le service signale une erreur', str(resultat_erreur))

CC.urlopen = _urlopen_original

print('\n=== 4. get_api_payload() transmet bien les deux champs ===')
info = {'mac_address': 'AA:BB:CC:DD:EE:FF', 'public_ip': '198.51.100.7', 'public_ip_isp': 'Free SAS'}
payload = CC.get_api_payload(info)
verifier(payload.get('public_ip') == '198.51.100.7', 'public_ip dans le payload')
verifier(payload.get('public_ip_isp') == 'Free SAS', 'public_ip_isp dans le payload')
verifier(payload.get('system_report', {}).get('public_ip') == '198.51.100.7',
         'également présent dans system_report (snapshot complet)')

print('\n=== 5. Bout en bout : /api/device-info enregistre les deux colonnes ===')
A.init_db()
conn = A.get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (1, 'admin', 'x', 'Administrateur', 'admin', 1)")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client Éprouvé')")
conn.commit()
conn.close()

client = A.app.test_client()
charge = {'mac_address': 'AA:BB:CC:IP:PUB:01', 'hostname': 'POSTE-Itinerant',
          'client_id': 1, 'ip_addresses': ['192.168.1.90'],
          'public_ip': '203.0.113.10', 'public_ip_isp': 'Orange S.A.'}
reponse = client.post('/api/device-info', json=charge)
verifier(reponse.status_code == 200, 'collecte acceptée', str(reponse.status_code))
appareil_id = (reponse.get_json() or {}).get('device_id')

conn = A.get_db()
ligne = conn.execute('SELECT adresse_ip_publique, operateur_ip_publique FROM appareils WHERE id=?',
                     (appareil_id,)).fetchone()
conn.close()
verifier(ligne and ligne[0] == '203.0.113.10', 'IP publique enregistrée à la création', str(ligne and ligne[0]))
verifier(ligne and ligne[1] == 'Orange S.A.', 'opérateur enregistré à la création', str(ligne and ligne[1]))

print('\n=== 6. Une IP publique différente REMPLACE l\'ancienne (poste itinérant) ===')
# Contrairement à l'utilisateur/l'antivirus (déduits, jamais écrasés une fois
# saisis à la main), l'IP publique est un fait technique qui change — même
# traitement qu'adresse_ip, toujours resynchronisée.
charge2 = dict(charge, public_ip='198.51.100.99', public_ip_isp='Bouygues Telecom')
client.post('/api/device-info', json=charge2)
conn = A.get_db()
ligne2 = conn.execute('SELECT adresse_ip_publique, operateur_ip_publique FROM appareils WHERE id=?',
                      (appareil_id,)).fetchone()
conn.close()
verifier(ligne2 and ligne2[0] == '198.51.100.99', 'IP publique mise à jour au lieu de rester figée',
         str(ligne2 and ligne2[0]))
verifier(ligne2 and ligne2[1] == 'Bouygues Telecom', 'opérateur mis à jour', str(ligne2 and ligne2[1]))

print('\n=== 7. La fiche appareil affiche l\'IP publique et l\'opérateur ===')
with client.session_transaction() as session:
    session['auth_user_id'] = 1
    session['client_id'] = 1
page = client.get('/appareil/%s/editer' % appareil_id).get_data(as_text=True)
verifier('198.51.100.99' in page, "l'IP publique apparaît sur la fiche")
verifier('Bouygues Telecom' in page, "l'opérateur apparaît sur la fiche")

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
