#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie les améliorations de détection du scan réseau (points 1 à 4
proposés après une revue du code de détection matériel) :

  1. Les données WMI (marque/modèle/n° série/RAM/CPU/disque) sont bien
     enregistrées à l'import — auparavant calculées puis jamais écrites.
  2. Bannières de service (SSH/FTP/HTTP...) sur les ports déjà trouvés
     ouverts, jamais sur un port fermé.
  3. Découverte UPnP (ssdp:all), pas restreinte à la box Internet comme
     côté collecteur — un appareil UPnP quelconque du segment.
  4. Découverte mDNS élargie (imprimantes, Apple, Chromecast, NAS), au-delà
     de la seule recherche d'autres instances ParcInfo.

Et que le tout se combine pour une détection de type plus précise et un
import qui ne perd ni n'écrase jamais une valeur déjà présente.

Usage :
    python test_scan_reseau_precision.py
"""

import io
import os
import socket
import struct
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='scan_precision_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


class _FausseReponseHTTP:
    def __init__(self, contenu_bytes, headers=None):
        self._contenu = contenu_bytes
        self.headers = headers or {}

    def read(self, *a):
        return self._contenu

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


print('=== 1. _deviner_type() : les signaux structurés (UPnP/mDNS) priment sur le texte ===')
# Un hostname anodin, mais un deviceType UPnP explicite : doit l'emporter.
verifier(A._deviner_type('poste-1', [], upnp_device_type='urn:schemas-upnp-org:device:InternetGatewayDevice:1') == 'Routeur/Pare-feu',
         "deviceType InternetGatewayDevice -> Routeur/Pare-feu")
verifier(A._deviner_type('poste-2', [], upnp_device_type='urn:schemas-upnp-org:device:Printer:1') == 'Imprimante',
         "deviceType Printer -> Imprimante")
verifier(A._deviner_type('poste-3', [], mdns_service='ipp') == 'Imprimante',
         "service mDNS ipp -> Imprimante")
verifier(A._deviner_type('poste-4', [], mdns_service='smb') == 'Serveur',
         "service mDNS smb -> Serveur")
verifier(A._deviner_type('poste-5', [9100], vendor='') == 'Imprimante',
         "comportement existant inchangé sans signal structuré (port 9100)")
verifier(A._deviner_type('poste-6', [], extra_signal='cisco ios switch') == 'Switch',
         "extra_signal (bannière/nom UPnP) alimente bien la détection texte")

print('\n=== 2. Bannières de service ===')
# Bannière "texte" (salutation immédiate, ex: SSH) via un faux socket.
_socket_original = A.socket.socket


class _FauxSocketBanniere:
    def __init__(self, *a, **kw):
        pass

    def settimeout(self, t):
        pass

    def connect(self, adresse):
        pass

    def recv(self, taille):
        return b'SSH-2.0-OpenSSH_8.9 Ubuntu\r\n'

    def close(self):
        pass


A.socket.socket = _FauxSocketBanniere
banniere = A._grab_banniere_texte('192.0.2.10', 22)
A.socket.socket = _socket_original
verifier('OpenSSH' in banniere, "bannière SSH lue et nettoyée des retours à la ligne", banniere)

# Bannière HTTP (Server + titre) via un faux urlopen.
import urllib.request as _ur
_ur_original = _ur.urlopen
_ur.urlopen = lambda req, timeout=None, context=None: _FausseReponseHTTP(
    b'<html><head><title>NAS Login</title></head><body></body></html>',
    headers={'Server': 'lighttpd/1.4'})
banniere_http = A._grab_banniere_http('192.0.2.11', 80, https=False)
_ur.urlopen = _ur_original
verifier('lighttpd' in banniere_http and 'NAS Login' in banniere_http,
         "en-tête Server + titre de page combinés", banniere_http)

# Gating : seuls les ports EFFECTIVEMENT ouverts et pertinents sont sondés.
appels = []
_grab_original = A._grab_banniere
A._grab_banniere = lambda ip, p: (appels.append(p), 'x')[1]
resultat_bannieres = A._grab_bannieres('192.0.2.12', [22, 80, 12345, 9999])
A._grab_banniere = _grab_original
verifier(sorted(appels) == [22, 80],
         "seuls les ports ouverts ET pertinents (22, 80) sont sondés — 12345/9999 ignorés",
         str(sorted(appels)))
verifier(A._grab_bannieres('192.0.2.13', []) == {}, "aucun port ouvert -> {}")

print('\n=== 3. Découverte UPnP (ssdp:all) : pas restreinte à la box Internet ===')


class _FauxSocketSSDP:
    def __init__(self, *a, **kw):
        self._envoye = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, t):
        pass

    def sendto(self, data, adresse):
        self._envoye = True

    def recvfrom(self, taille):
        if not getattr(self, '_deja_repondu', False):
            self._deja_repondu = True
            reponse = (
                b'HTTP/1.1 200 OK\r\n'
                b'LOCATION: http://192.0.2.50:8200/desc.xml\r\n'
                b'ST: upnp:rootdevice\r\n\r\n'
            )
            return reponse, ('192.0.2.50', 1900)
        raise socket.timeout()


A.socket.socket = _FauxSocketSSDP
emplacements = A._ssdp_decouvrir_tout(timeout=1)
A.socket.socket = _socket_original
verifier(emplacements.get('192.0.2.50') == 'http://192.0.2.50:8200/desc.xml',
         "LOCATION extraite avec l'IP source de la réponse (pas seulement l'IGD)",
         str(emplacements))

XML_DESCRIPTION_UPNP = b'''<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>NAS-Bureau</friendlyName>
    <manufacturer>Synology</manufacturer>
    <modelName>DS220+</modelName>
  </device>
</root>'''
_ur.urlopen = lambda req, timeout=None, context=None: _FausseReponseHTTP(XML_DESCRIPTION_UPNP)
description = A._upnp_description_appareil('http://192.0.2.50:8200/desc.xml')
_ur.urlopen = _ur_original
verifier(description.get('manufacturer') == 'Synology', "fabricant NAS extrait (pas un routeur)")
verifier(description.get('device_type') == 'urn:schemas-upnp-org:device:MediaServer:1',
         "deviceType MediaServer conservé pour la classification")

print('\n=== 4. Découverte mDNS élargie (imprimantes, Apple, NAS...) ===')


class _FausseInfoZC:
    def __init__(self, adresses):
        self._adresses = adresses

    def parsed_addresses(self):
        return self._adresses


class _FauxZeroconf:
    def get_service_info(self, type_service, nom, timeout=None):
        return _FausseInfoZC(['192.0.2.60'])

    def close(self):
        pass


class _FauxServiceBrowser:
    def __init__(self, zc, type_service, ecouteur):
        # Simule immédiatement UNE imprimante trouvée sur le type _ipp._tcp,
        # rien sur les autres types parcourus.
        if type_service == '_ipp._tcp.local.':
            ecouteur.add_service(zc, type_service, 'Imprimante-Bureau._ipp._tcp.local.')


_zeroconf_module = sys.modules.get('zeroconf')
_zc_cls_original = _zeroconf_module.Zeroconf
_sb_cls_original = _zeroconf_module.ServiceBrowser
_zeroconf_module.Zeroconf = lambda: _FauxZeroconf()
_zeroconf_module.ServiceBrowser = _FauxServiceBrowser
_sleep_original = A.time.sleep
A.time.sleep = lambda s: None
decouvertes_mdns = A._decouverte_mdns_reseau(timeout=1)
A.time.sleep = _sleep_original
_zeroconf_module.Zeroconf = _zc_cls_original
_zeroconf_module.ServiceBrowser = _sb_cls_original
verifier(decouvertes_mdns.get('192.0.2.60', {}).get('service') == 'ipp',
         "imprimante trouvée via _ipp._tcp identifiée par IP", str(decouvertes_mdns))

print('\n=== 5. importer_scan() : les données détectées sont bien enregistrées ===')
A.init_db()
conn = A.get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (1, 'admin', 'x', 'Administrateur', 'admin', 1)")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client Scan')")
conn.commit()
conn.close()

client = A.app.test_client()
with client.session_transaction() as session:
    session['auth_user_id'] = 1
    session['client_id'] = 1

item_wmi = {
    'ip': '192.0.2.100', 'mac': 'aa:bb:cc:dd:ee:01', 'vendor': 'Dell Inc.',
    'type': 'PC (Windows)', 'ports': [3389, 445],
    'brand': 'Dell Inc.', 'model': 'OptiPlex 7090', 'serial_number': 'SN12345',
    'ram_gb': 16, 'cpu': 'Intel Core i7', 'disk_total_gb': 512,
}
reponse = client.post('/api/scan/importer', json={'appareils': [item_wmi]})
verifier(reponse.status_code == 200, "import accepté", str(reponse.status_code))
conn = A.get_db()
ligne = conn.execute(
    "SELECT marque, modele, numero_serie, cpu, ram, stockage FROM appareils WHERE adresse_ip='192.0.2.100'"
).fetchone()
conn.close()
verifier(ligne and ligne[0] == 'Dell Inc.', "marque WMI enregistrée (auparavant jamais écrite)",
         str(ligne and tuple(ligne)))
verifier(ligne and ligne[1] == 'OptiPlex 7090', "modèle WMI enregistré")
verifier(ligne and ligne[2] == 'SN12345', "numéro de série WMI enregistré")
verifier(ligne and ligne[3] == 'Intel Core i7', "CPU WMI enregistré")
verifier(ligne and ligne[4] == '16 Go', "RAM WMI formatée et enregistrée", str(ligne and ligne[4]))
verifier(ligne and ligne[5] == '512 Go', "espace disque WMI formaté et enregistré", str(ligne and ligne[5]))

print('\n=== 6. importer_scan() : marque_detectee (UPnP/mDNS) utilisée si pas de WMI ===')
item_upnp = {
    'ip': '192.0.2.101', 'mac': 'aa:bb:cc:dd:ee:02', 'vendor': '',
    'type': 'Serveur', 'ports': [],
    'marque_detectee': 'Synology', 'modele_detectee': 'DS220+',
}
client.post('/api/scan/importer', json={'appareils': [item_upnp]})
conn = A.get_db()
ligne2 = conn.execute("SELECT marque, modele FROM appareils WHERE adresse_ip='192.0.2.101'").fetchone()
conn.close()
verifier(ligne2 is not None and tuple(ligne2) == ('Synology', 'DS220+'),
         "marque/modèle UPnP utilisés en l'absence de WMI", str(ligne2 and tuple(ligne2)))

print("\n=== 7. importer_scan() : une valeur déjà présente n'est JAMAIS écrasée ===")
conn = A.get_db()
conn.execute("UPDATE appareils SET marque='Marque saisie à la main' WHERE adresse_ip='192.0.2.100'")
conn.commit()
conn.close()
client.post('/api/scan/importer', json={'appareils': [dict(item_wmi, brand='Autre Marque WMI')]})
conn = A.get_db()
marque_apres = conn.execute("SELECT marque FROM appareils WHERE adresse_ip='192.0.2.100'").fetchone()[0]
conn.close()
verifier(marque_apres == 'Marque saisie à la main',
         "la correction manuelle survit à une nouvelle détection", marque_apres)

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
