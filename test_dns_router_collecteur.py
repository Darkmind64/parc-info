#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie les deux options du collecteur ajoutées sur demande explicite,
désactivées par défaut : vérification DNS (dnscheck.tools) et infos de la
box internet (UPnP).

Ce que le test contrôle :
  - le paquet DNS construit pour interroger test.dnscheck.tools est bien
    formé, et le résultat est affiché BRUT (aucun verdict OK/KO fabriqué —
    dnscheck.tools n'expose pas d'API documentée assez précisément pour ça)
  - le analyseur de réponse DNS gère la compression de noms (pointeurs),
    et ne casse jamais sur une réponse tronquée/inattendue
  - la description UPnP (fabricant/modèle/URL de contrôle) et l'appel SOAP
    GetExternalIPAddress sont extraits correctement d'XML synthétiques
  - aucune box qui répond → dict vide, jamais d'exception
  - les deux options ne s'activent QUE si le drapeau correspondant est
    fourni à collect_system_info() — décochées par défaut, comme demandé

Usage :
    python test_dns_router_collecteur.py
"""

import io
import os
import struct
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import collector_core as CC  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


class _FausseReponseHTTP:
    def __init__(self, contenu_bytes):
        self._contenu = contenu_bytes

    def read(self):
        return self._contenu

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reponse_dns_synthetique(question, txn_id, chaines_txt):
    """Construit une réponse DNS minimale avec une seule réponse TXT,
    nom compressé (pointeur vers la question) comme un vrai serveur."""
    flags = 0x8180
    entete = struct.pack('>HHHHHH', txn_id, flags, 1, 1, 0, 0)
    rdata = b''.join(bytes([len(c.encode())]) + c.encode() for c in chaines_txt)
    reponse_nom = b'\xc0\x0c'
    answer = reponse_nom + struct.pack('>HHIH', 16, 1, 60, len(rdata)) + rdata
    return entete + question + answer


print('=== 1. Construction de la requête DNS ===')
requete, txn_id = CC._dns_construire_requete_txt(CC.DNS_CHECK_HOSTNAME)
verifier(len(requete) > 12, 'requête non vide, en-tête présent')
id_paquet = struct.unpack('>H', requete[:2])[0]
verifier(id_paquet == txn_id, "l'identifiant de transaction du paquet correspond à celui retourné")
qdcount = struct.unpack('>H', requete[4:6])[0]
verifier(qdcount == 1, 'une seule question posée', str(qdcount))

print('\n=== 2. Analyse de la réponse : nom compressé, plusieurs segments TXT ===')
question = requete[12:]
reponse = _reponse_dns_synthetique(question, txn_id, ['hello', 'world'])
resultat = CC._dns_parser_txt(reponse, txn_id)
verifier(resultat == ['helloworld'], 'segments TXT concaténés correctement', str(resultat))

print('\n=== 3. La réponse brute est affichée telle quelle, sans verdict fabriqué ===')
reponse2 = _reponse_dns_synthetique(question, txn_id, ['dnscheck.tools test response'])
resultat2 = CC._dns_parser_txt(reponse2, txn_id)
verifier(resultat2 == ['dnscheck.tools test response'], 'contenu TXT préservé tel quel')

print('\n=== 4. Robustesse : transaction id différent, réponse tronquée, aucune exception ===')
verifier(CC._dns_parser_txt(reponse, txn_id + 1) == [], 'id de transaction différent -> ignoré')
verifier(CC._dns_parser_txt(b'\x00\x01', txn_id) == [], 'réponse tronquée -> liste vide, pas d\'exception')
verifier(CC._dns_parser_txt(b'', txn_id) == [], 'réponse vide -> liste vide')

print('\n=== 5. get_dns_check_info() : bout en bout avec un faux socket ===')
class _FauxSocketDNS:
    def __init__(self, *a, **kw):
        self._envoye = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, t):
        pass

    def sendto(self, data, adresse):
        self._envoye = data

    def recvfrom(self, taille):
        txn = struct.unpack('>H', self._envoye[:2])[0]
        q = self._envoye[12:]
        return _reponse_dns_synthetique(q, txn, ['réponse de test']), ('resolveur', 53)


_socket_original = CC.socket.socket
CC.socket.socket = _FauxSocketDNS
resultat_complet = CC.get_dns_check_info({'dns_servers': [{'servers': ['192.0.2.53']}]})
CC.socket.socket = _socket_original
verifier(resultat_complet.get('dns_check_resolveur') == '192.0.2.53',
         'résolveur configuré utilisé (pas un résolveur public arbitraire)',
         str(resultat_complet.get('dns_check_resolveur')))
verifier(resultat_complet.get('dns_check_reponse') == 'réponse de test',
         'réponse transmise telle quelle', str(resultat_complet.get('dns_check_reponse')))

print('\n=== 6. get_dns_check_info() : aucun résolveur connu -> dict vide, pas d\'exception ===')
# {} seul ne suffit pas à isoler ce cas : sur une machine dont /etc/resolv.conf
# est lisible (le cas de la CI Linux), _resolveur_dns_systeme retrouverait un
# vrai résolveur et lancerait une vraie requête réseau — le résolveur est
# donc explicitement neutralisé ici, pour rester indépendant de la machine
# qui exécute le test (Windows sans /etc/resolv.conf, Linux avec).
_resolveur_original = CC._resolveur_dns_systeme
CC._resolveur_dns_systeme = lambda info: None
verifier(CC.get_dns_check_info({}) == {}, 'aucun serveur DNS connu -> {}')
CC._resolveur_dns_systeme = _resolveur_original

print('\n=== 7. UPnP : description XML (fabricant/modèle/URL de contrôle) ===')
XML_DESCRIPTION = b'''<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    <friendlyName>MaBox Internet</friendlyName>
    <manufacturer>Freebox SAS</manufacturer>
    <modelName>Freebox Pop</modelName>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
        <deviceList>
          <device>
            <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
            <serviceList>
              <service>
                <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
                <controlURL>/upnp/control/WANIPConnection</controlURL>
              </service>
            </serviceList>
          </device>
        </deviceList>
      </device>
    </deviceList>
  </device>
</root>'''

_urlopen_original = CC.urlopen
CC.urlopen = lambda req, timeout=None: _FausseReponseHTTP(XML_DESCRIPTION)
description = CC._upnp_description('http://192.168.1.1:1900/desc.xml')
verifier(description.get('manufacturer') == 'Freebox SAS', "fabricant extrait")
verifier(description.get('model_name') == 'Freebox Pop', "modèle extrait")
verifier(description.get('_control_url') == 'http://192.168.1.1:1900/upnp/control/WANIPConnection',
         "URL de contrôle résolue en absolu depuis l'URL de description",
         str(description.get('_control_url')))
verifier(description.get('_service_type') == 'urn:schemas-upnp-org:service:WANIPConnection:1',
         'type de service WAN identifié')

print('\n=== 8. UPnP : réponse SOAP GetExternalIPAddress ===')
XML_SOAP = b'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <u:GetExternalIPAddressResponse xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">
      <NewExternalIPAddress>203.0.113.77</NewExternalIPAddress>
    </u:GetExternalIPAddressResponse>
  </s:Body>
</s:Envelope>'''
CC.urlopen = lambda req, timeout=None: _FausseReponseHTTP(XML_SOAP)
ip = CC._upnp_ip_externe('http://192.168.1.1/control', 'urn:schemas-upnp-org:service:WANIPConnection:1')
verifier(ip == '203.0.113.77', 'IP WAN extraite de la réponse SOAP', str(ip))

print('\n=== 9. get_router_info() : bout en bout ===')
CC.urlopen = lambda req, timeout=None: (
    _FausseReponseHTTP(XML_DESCRIPTION) if 'desc' in req.full_url or 'upnp' not in req.full_url
    else _FausseReponseHTTP(XML_SOAP))
CC._upnp_decouvrir_passerelle = lambda timeout=3: 'http://192.168.1.1:1900/desc.xml'
info_box = CC.get_router_info()
verifier(info_box.get('router_manufacturer') == 'Freebox SAS', 'fabricant remonté')
verifier(info_box.get('router_wan_ip') == '203.0.113.77', 'IP WAN remontée')
CC.urlopen = _urlopen_original

print("\n=== 10. get_router_info() : aucune box ne répond -> dict vide ===")
CC._upnp_decouvrir_passerelle = lambda timeout=3: None
verifier(CC.get_router_info() == {}, 'découverte SSDP infructueuse -> {}, pas d\'exception')

print('\n=== 11. collect_system_info() : les deux options ne s\'activent que sur demande ===')
# Toutes les autres étapes sont remplacées par des réponses immédiates et
# inoffensives, pour que la collecte complète s'exécute en une fraction de
# seconde plutôt que la minute habituelle (35 étapes Windows) — seul le
# comportement des deux nouvelles options nous intéresse ici.
_patches = {
    'is_elevated': lambda: False,
    'get_mac_address': lambda: '00:00:00:00:00:00',
    'get_hostname': lambda: 'test-host',
    'get_fqdn': lambda: 'test-host',
    'get_ip_addresses': lambda: [],
    'get_os_info': lambda: {},
    'get_system_info_windows': lambda *a, **kw: {},
    'get_system_info_mac': lambda *a, **kw: {},
    'get_system_info_linux': lambda *a, **kw: {},
    '_meilleure_carte_physique': lambda *a, **kw: None,
    'get_hosts_file_entries': lambda *a, **kw: [],
    'get_installed_software': lambda: [],
    'check_software_updates': lambda *a, **kw: None,
    'measure_network': lambda *a, **kw: {},
    'get_public_ip_info': lambda: {},
    'collect_usb_devices': lambda: [],
    'get_dns_check_info': lambda info: {'dns_check_reponse': 'appelée'},
    'get_router_info': lambda: {'router_wan_ip': 'appelée'},
}
_originaux = {nom: getattr(CC, nom) for nom in _patches}
for nom, remplacement in _patches.items():
    setattr(CC, nom, remplacement)

try:
    resultat_off = CC.collect_system_info()
    verifier('dns_check_reponse' not in resultat_off,
             'verifier_dns=False (défaut) : dnscheck.tools jamais interrogé')
    verifier('router_wan_ip' not in resultat_off,
             'info_box=False (défaut) : UPnP jamais sondé')

    resultat_on = CC.collect_system_info(verifier_dns=True, info_box=True)
    verifier(resultat_on.get('dns_check_reponse') == 'appelée',
             'verifier_dns=True : dnscheck.tools bien interrogé')
    verifier(resultat_on.get('router_wan_ip') == 'appelée',
             'info_box=True : UPnP bien sondé')
finally:
    for nom, original in _originaux.items():
        setattr(CC, nom, original)

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
