#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie la collecte des réseaux Wi-Fi enregistrés et leur remontée comme
identifiants chiffrés — jamais en clair dans la fiche appareil ou le PDF.

Ce que le test contrôle :
  - le XML exporté par `netsh wlan export profile` est correctement lu (SSID,
    sécurité, mot de passe), y compris pour un réseau ouvert (sans clé)
  - `authentification` prend une poignée de valeurs documentées par Microsoft ;
    une valeur non reconnue est renvoyée telle quelle plutôt que forcée
  - sans `inclure_mdp`, le mot de passe n'est jamais lu, même présent dans le XML
  - côté serveur, un réseau déjà connu (même client + même SSID) est mis à
    jour plutôt que dupliqué, et son mot de passe n'est écrasé que si CE
    relevé en apporte un nouveau — jamais vidé par une collecte sans case cochée
  - un identifiant Wi-Fi déjà personnalisé à la main (nom, description) garde
    ces champs intacts après une synchronisation automatique
  - bout en bout : POST /api/device-info/wifi-credentials crée puis met à jour

Usage :
    python test_wifi_credentials.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='wifi_creds_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import collector_core as C                   # noqa: E402
import app as A                              # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


def _ecrire_xml(contenu):
    fd, chemin = tempfile.mkstemp(suffix='.xml', prefix='wlan_')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(contenu)
    return chemin


_XML_WPA2 = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>Bureau-Principal</name>
    <SSIDConfig><SSID><name>Bureau-Principal</name></SSID></SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>auto</connectionMode>
    <MSM><security>
        <authEncryption>
            <authentication>WPA2PSK</authentication>
            <encryption>AES</encryption>
            <useOneX>false</useOneX>
        </authEncryption>
        <sharedKey>
            <keyType>passPhrase</keyType>
            <protected>false</protected>
            <keyMaterial>SuperMotDePasse123</keyMaterial>
        </sharedKey>
    </security></MSM>
</WLANProfile>"""

_XML_OUVERT = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>Invites</name>
    <SSIDConfig><SSID><name>Invites</name></SSID></SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>auto</connectionMode>
    <MSM><security>
        <authEncryption>
            <authentication>open</authentication>
            <encryption>none</encryption>
            <useOneX>false</useOneX>
        </authEncryption>
    </security></MSM>
</WLANProfile>"""

_XML_WPA3 = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>Labo-5G</name>
    <SSIDConfig><SSID><name>Labo-5G</name></SSID></SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>auto</connectionMode>
    <MSM><security>
        <authEncryption>
            <authentication>WPA3SAE</authentication>
            <encryption>AES</encryption>
            <useOneX>false</useOneX>
        </authEncryption>
        <sharedKey><keyMaterial>MotDePasseWPA3</keyMaterial></sharedKey>
    </security></MSM>
</WLANProfile>"""

_XML_AUTH_INCONNUE = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>Reseau-Exotique</name>
    <SSIDConfig><SSID><name>Reseau-Exotique</name></SSID></SSIDConfig>
    <MSM><security>
        <authEncryption><authentication>OWE</authentication><encryption>AES</encryption></authEncryption>
    </security></MSM>
</WLANProfile>"""

_XML_SANS_SSID = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>Cassé</name>
    <MSM><security>
        <authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption></authEncryption>
    </security></MSM>
</WLANProfile>"""


print("=== 1. Lecture du XML exporté par netsh wlan export profile ===")
p = C._parse_wifi_profile_xml(_ecrire_xml(_XML_WPA2), inclure_mdp=True)
verifier(p is not None and p['ssid'] == 'Bureau-Principal', 'SSID lu')
verifier(p['authentification'] == 'WPA2', 'WPA2PSK → libellé WPA2', p['authentification'])
verifier(p['chiffrement'] == 'AES', 'chiffrement lu')
verifier(p.get('password') == 'SuperMotDePasse123', 'mot de passe lu avec inclure_mdp=True')

p_sans_mdp = C._parse_wifi_profile_xml(_ecrire_xml(_XML_WPA2), inclure_mdp=False)
verifier('password' not in p_sans_mdp,
         "le mot de passe n'est jamais lu sans inclure_mdp, même présent dans le XML")

p_ouvert = C._parse_wifi_profile_xml(_ecrire_xml(_XML_OUVERT), inclure_mdp=True)
verifier(p_ouvert['authentification'] == 'Ouvert', 'open → libellé Ouvert', p_ouvert['authentification'])
verifier('password' not in p_ouvert, "un réseau ouvert n'a pas de clé à lire")

p_wpa3 = C._parse_wifi_profile_xml(_ecrire_xml(_XML_WPA3), inclure_mdp=True)
verifier(p_wpa3['authentification'] == 'WPA3', 'WPA3SAE → libellé WPA3')
verifier(p_wpa3.get('password') == 'MotDePasseWPA3', 'mot de passe WPA3 lu')

p_inconnu = C._parse_wifi_profile_xml(_ecrire_xml(_XML_AUTH_INCONNUE), inclure_mdp=True)
verifier(p_inconnu['authentification'] == 'OWE',
         "une authentification non répertoriée est renvoyée telle quelle, pas forcée à WPA2",
         p_inconnu['authentification'])

verifier(C._parse_wifi_profile_xml(_ecrire_xml(_XML_SANS_SSID)) is None,
         'un profil sans SSID exploitable est ignoré (None)')

print("\n=== 2. get_wifi_profiles() reste silencieux hors Windows / sans adaptateur ===")
resultat = C.get_wifi_profiles()
verifier(isinstance(resultat, list), 'toujours une liste, jamais une exception', str(type(resultat)))


print("\n=== 3. Synchronisation serveur : identifiants Wi-Fi chiffrés ===")
A.init_db()
conn = A.get_db()
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client Wi-Fi')")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (2, 'Autre Client')")
conn.commit()

crypto = A._get_crypto_shared()

crees, maj = A._sync_wifi_credentials_from_collector(conn, 1, [
    {'ssid': 'Bureau-Principal', 'authentification': 'WPA2', 'chiffrement': 'AES',
     'password': 'SuperMotDePasse123'},
    {'ssid': 'Invites', 'authentification': 'Ouvert'},
])
conn.commit()
verifier((crees, maj) == (2, 0), 'deux réseaux créés, aucune mise à jour', str((crees, maj)))

ligne = conn.execute(
    "SELECT mot_de_passe, wifi_securite, categorie FROM identifiants "
    "WHERE client_id=1 AND wifi_ssid='Bureau-Principal'").fetchone()
verifier(ligne is not None and ligne[2] == 'Wi-Fi', 'catégorie Wi-Fi assignée')
verifier(ligne is not None and ligne[0] != 'SuperMotDePasse123',
         "le mot de passe n'est jamais stocké en clair en base")
verifier(ligne is not None and crypto.decrypt(ligne[0]) == 'SuperMotDePasse123',
         'le mot de passe se déchiffre correctement')

ligne_invites = conn.execute(
    "SELECT mot_de_passe FROM identifiants WHERE client_id=1 AND wifi_ssid='Invites'").fetchone()
verifier(ligne_invites is not None and not ligne_invites[0],
         "un réseau ouvert (aucun mot de passe collecté) n'a rien à chiffrer")

print("\n=== 4. Une collecte sans mot de passe ne vide jamais celui déjà enregistré ===")
crees2, maj2 = A._sync_wifi_credentials_from_collector(conn, 1, [
    {'ssid': 'Bureau-Principal', 'authentification': 'WPA2'},  # pas de 'password' cette fois
])
conn.commit()
verifier((crees2, maj2) == (0, 1), 'mise à jour, pas de doublon créé', str((crees2, maj2)))
ligne = conn.execute(
    "SELECT mot_de_passe FROM identifiants WHERE client_id=1 AND wifi_ssid='Bureau-Principal'").fetchone()
verifier(crypto.decrypt(ligne[0]) == 'SuperMotDePasse123',
         "le mot de passe existant survit à une collecte sans la case cochée")

print("\n=== 5. Une collecte avec un nouveau mot de passe le remplace ===")
crees3, maj3 = A._sync_wifi_credentials_from_collector(conn, 1, [
    {'ssid': 'Bureau-Principal', 'authentification': 'WPA2', 'password': 'NouveauMotDePasse456'},
])
conn.commit()
verifier((crees3, maj3) == (0, 1), 'mise à jour sur le même SSID')
ligne = conn.execute(
    "SELECT mot_de_passe FROM identifiants WHERE client_id=1 AND wifi_ssid='Bureau-Principal'").fetchone()
verifier(crypto.decrypt(ligne[0]) == 'NouveauMotDePasse456',
         'le mot de passe est bien mis à jour quand un nouveau est fourni')

print("\n=== 6. Une entrée personnalisée à la main garde son nom et sa description ===")
conn.execute(
    "UPDATE identifiants SET nom='WiFi salle serveur (accès restreint)', "
    "description='Ne pas partager — demander au responsable infra' "
    "WHERE client_id=1 AND wifi_ssid='Bureau-Principal'")
conn.commit()
A._sync_wifi_credentials_from_collector(conn, 1, [
    {'ssid': 'Bureau-Principal', 'authentification': 'WPA3', 'password': 'EncoreUnAutre789'},
])
conn.commit()
ligne = conn.execute(
    "SELECT nom, description, wifi_securite FROM identifiants "
    "WHERE client_id=1 AND wifi_ssid='Bureau-Principal'").fetchone()
verifier(ligne[0] == 'WiFi salle serveur (accès restreint)',
         'le nom personnalisé à la main survit à une resynchronisation')
verifier(ligne[1] == 'Ne pas partager — demander au responsable infra',
         'la description personnalisée survit aussi')
verifier(ligne[2] == 'WPA3', 'la sécurité, elle, est bien rafraîchie', ligne[2])

print("\n=== 7. Isolation entre clients ===")
A._sync_wifi_credentials_from_collector(conn, 2, [
    {'ssid': 'Bureau-Principal', 'authentification': 'WPA2', 'password': 'PourAutreClient'},
])
conn.commit()
total_client1 = conn.execute(
    "SELECT COUNT(*) FROM identifiants WHERE client_id=1 AND wifi_ssid='Bureau-Principal'").fetchone()[0]
verifier(total_client1 == 1,
         "le même SSID pour un autre client crée une entrée distincte, ne touche pas celle du premier")
conn.close()

print("\n=== 8. Bout en bout : POST /api/device-info/wifi-credentials ===")
client = A.app.test_client()
conn = A.get_db()
conn.execute("INSERT OR IGNORE INTO appareils (id, client_id, nom_machine) "
             "VALUES (999, 1, 'POSTE-Test-Wifi')")
conn.commit()
conn.close()

reponse = client.post('/api/device-info/wifi-credentials', json={
    'device_id': 999, 'client_id': 1,
    'profiles': [{'ssid': 'Reseau-Nouveau', 'authentification': 'WPA2', 'password': 'Xyz123'}],
})
verifier(reponse.status_code == 200, 'requête acceptée', str(reponse.status_code))
corps = reponse.get_json() or {}
verifier(corps.get('created') == 1, 'un réseau créé via l\'API', str(corps))

conn = A.get_db()
ligne = conn.execute(
    "SELECT mot_de_passe FROM identifiants WHERE client_id=1 AND wifi_ssid='Reseau-Nouveau'").fetchone()
conn.close()
verifier(ligne is not None and crypto.decrypt(ligne[0]) == 'Xyz123',
         "le mot de passe envoyé par l'API est bien chiffré en base")

reponse2 = client.post('/api/device-info/wifi-credentials', json={
    'device_id': 999, 'client_id': 1,
    'profiles': [{'ssid': 'Reseau-Nouveau', 'authentification': 'WPA2'}],
})
corps2 = reponse2.get_json() or {}
verifier(corps2.get('updated') == 1 and corps2.get('created') == 0,
         'un second envoi met à jour plutôt que dupliquer', str(corps2))

reponse_client_invalide = client.post('/api/device-info/wifi-credentials', json={
    'device_id': 999, 'client_id': 999999, 'profiles': [{'ssid': 'X', 'authentification': 'WPA2'}],
})
verifier(reponse_client_invalide.status_code == 404, 'client_id inconnu rejeté',
         str(reponse_client_invalide.status_code))


print()
if echecs:
    print('ÉCHECS : %d' % len(echecs))
    for e in echecs:
        print('  - ' + e)
    sys.exit(1)
else:
    print('TOUT OK')
