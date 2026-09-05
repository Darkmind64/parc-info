#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie la reprise en profondeur du scan réseau (2026-08-21), demandée
pour obtenir une image la plus complète et fiable possible du réseau sans
identification manuelle par IP/MAC :

  - Bug réel trouvé en creusant l'imprécision du scan : le chemin de
    oui.txt était calculé depuis __file__, qui pointe en exécutable
    packagé (PyInstaller --onefile) vers un dossier d'extraction TEMPORAIRE
    recréé à chaque lancement — jamais le dossier réel de l'exe. Aucun
    exécutable packagé ne pouvait donc jamais charger la base OUI complète,
    même en suivant la documentation à la lettre. Corrigé : oui.txt vit
    désormais à côté des autres données persistantes (_data_base).
  - Téléchargement/mise à jour automatique de la base OUI IEEE (~60 000
    préfixes), demandée explicitement : au démarrage si absente, rafraîchie
    si elle a plus de 30 jours (cron quotidien pour une instance qui tourne
    en continu), et à la demande via /api/oui/telecharger (bouton "Mettre
    à jour" de la page Scan).
  - Détection ARP des hôtes silencieux : un appareil qui bloque ICMP (pare-
    feu, très nombreux objets connectés) répond quand même forcément à une
    résolution ARP pour exister sur le réseau local — jusqu'ici invisible
    du scan (qui abandonnait dès l'échec du ping), il apparaît désormais
    avec le peu d'informations disponibles (IP, MAC, fabricant), signalé
    comme silencieux plutôt que d'être purement absent de la liste.

Usage :
    python test_scan_completude_oui_arp.py
"""

import io
import os
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='scan_completude_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A   # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


class _FausseReponseHTTP:
    def __init__(self, contenu_bytes):
        self._contenu = contenu_bytes

    def read(self, *a):
        return self._contenu

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ═══════════════════════════════════════════════════════════════════════════
print('=== 1. _oui_path() : dans _data_base, jamais relatif à __file__ ===')
chemin = A._oui_path()
verifier(chemin == os.path.join(A._data_base, 'oui.txt'),
          "le chemin de oui.txt est bien construit depuis _data_base", chemin)
verifier(os.path.dirname(os.path.abspath(A.__file__)) not in chemin
          or A._data_base == os.path.dirname(os.path.abspath(A.__file__)),
          "ne dépend pas de __file__ (sauf coïncidence : mode source, _data_base == dossier du script)")

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 2. _oui_telecharger() : téléchargement, remplacement atomique, rechargement ===')
import urllib.request as _ur
_ur_original = _ur.urlopen

OUI_FACTICE = (
    "00-14-22   (hex)\t\tDell Inc.\n"
    "3C-07-54   (hex)\t\tApple, Inc.\n"
    "18-FE-34   (hex)\t\tEspressif Inc.\n"
).encode('utf-8')

_ur.urlopen = lambda req, timeout=None: _FausseReponseHTTP(OUI_FACTICE)
try:
    resultat = A._oui_telecharger(force=True)
finally:
    _ur.urlopen = _ur_original

verifier(resultat.get('ok') is True, "le téléchargement (factice) réussit", str(resultat))
verifier(os.path.exists(A._oui_path()), "oui.txt existe bien sur disque après téléchargement")
verifier(resultat.get('count') == 3, "les 3 préfixes factices sont bien comptés", str(resultat.get('count')))
verifier(A._oui_vendor('3c:07:54:aa:bb:cc') == 'Apple, Inc.',
          "la base rechargée est immédiatement utilisable par _oui_vendor()")
verifier(A.cfg_get('oui_derniere_maj', '') != '', "la date de dernière mise à jour est enregistrée")

print('\n=== 3. _oui_telecharger() : pas de retéléchargement si récent (force=False) ===')
appels = []
_ur.urlopen = lambda req, timeout=None: (appels.append(1) or _FausseReponseHTTP(OUI_FACTICE))
try:
    resultat2 = A._oui_telecharger(force=False)
finally:
    _ur.urlopen = _ur_original
verifier(resultat2.get('skipped') is True, "fichier récent (<30 jours) -> téléchargement sauté", str(resultat2))
verifier(appels == [], "urlopen n'a même pas été appelé")

print('\n=== 4. _oui_telecharger() : échec réseau -> base existante conservée, pas d\'exception ===')
avant = dict(A._OUI_FULL or {})
_ur.urlopen = lambda req, timeout=None: (_ for _ in ()).throw(OSError('réseau indisponible'))
try:
    resultat3 = A._oui_telecharger(force=True)
finally:
    _ur.urlopen = _ur_original
verifier(resultat3.get('ok') is False, "échec réseau signalé proprement (pas d'exception qui remonte)", str(resultat3))
verifier(A._OUI_FULL == avant, "la base précédemment chargée reste intacte après un échec")
verifier(not os.path.exists(A._oui_path() + '.tmp'), "le fichier temporaire est nettoyé après un échec")

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 5. _scan_host() : hôte silencieux (ICMP bloqué) détecté via ARP ===')
_ping_orig     = A._ping
_hostname_orig = A._hostname
_netbios_orig  = A._netbios_name
_ttl_orig      = A._ttl_os_guess
_ports_orig    = A._scan_ports
_arp_orig      = A._mac_from_arp
_snmp_orig     = A._snmp_get
_sleep_orig    = A.time.sleep

A._ping          = lambda ip: False          # ICMP bloqué
A._hostname       = lambda ip: ''
A._netbios_name   = lambda ip: ''
A._ttl_os_guess   = lambda ip: ''             # pas de réponse -> pas de TTL
A._scan_ports     = lambda ip: []             # tous les ports filtrés aussi
A._mac_from_arp   = lambda ip: 'aa:bb:cc:11:22:33'   # ...mais résout bien en ARP
A._snmp_get       = lambda ip, oids, **kw: {}
A.time.sleep      = lambda s: None            # ne pas ralentir le test pour de vrai
try:
    resultat_silencieux = A._scan_host('192.0.2.77')
finally:
    A._ping, A._hostname, A._netbios_name = _ping_orig, _hostname_orig, _netbios_orig
    A._ttl_os_guess, A._scan_ports = _ttl_orig, _ports_orig
    A._mac_from_arp, A._snmp_get = _arp_orig, _snmp_orig
    A.time.sleep = _sleep_orig

verifier(resultat_silencieux is not None,
          "un hôte qui bloque ICMP mais résout en ARP n'est plus ignoré (avant : perdu du scan)")
verifier(resultat_silencieux is not None and resultat_silencieux.get('mac') == 'aa:bb:cc:11:22:33',
          "son adresse MAC est bien remontée malgré le silence IP")
verifier(resultat_silencieux is not None and resultat_silencieux.get('silencieux') is True,
          "marqué 'silencieux' pour que l'utilisateur comprenne pourquoi peu d'infos sont disponibles")

print('\n=== 6. _scan_host() : hôte ni pingable ni résolu en ARP -> toujours absent ===')
A._ping         = lambda ip: False
A._mac_from_arp = lambda ip: ''
A.time.sleep    = lambda s: None
try:
    resultat_absent = A._scan_host('192.0.2.78')
finally:
    A._ping, A._mac_from_arp = _ping_orig, _arp_orig
    A.time.sleep = _sleep_orig
verifier(resultat_absent is None, "un hôte vraiment injoignable (ni ping ni ARP) reste absent du scan, comme avant")

print('\n=== 7. _scan_host() : hôte qui répond normalement -> pas marqué silencieux (non régressé) ===')
A._ping          = lambda ip: True
A._hostname       = lambda ip: 'poste-normal'
A._netbios_name   = lambda ip: ''
A._ttl_os_guess   = lambda ip: 'Windows'
A._scan_ports     = lambda ip: [445]
A._mac_from_arp   = lambda ip: 'dd:ee:ff:44:55:66'
A._snmp_get       = lambda ip, oids, **kw: {}
A.time.sleep      = lambda s: None
try:
    resultat_normal = A._scan_host('192.0.2.79')
finally:
    A._ping, A._hostname, A._netbios_name = _ping_orig, _hostname_orig, _netbios_orig
    A._ttl_os_guess, A._scan_ports = _ttl_orig, _ports_orig
    A._mac_from_arp, A._snmp_get = _arp_orig, _snmp_orig
    A.time.sleep = _sleep_orig
verifier(resultat_normal is not None and resultat_normal.get('silencieux') is False,
          "un hôte qui répond au ping n'est jamais marqué silencieux")

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 8. Découverte mDNS élargie : Matter et présence générique (_workstation) ===')
for service in ('_matter._tcp.local.', '_matterc._udp.local.', '_workstation._tcp.local.'):
    verifier(service in A._MDNS_TYPES_APPAREILS, f"{service} fait bien partie des services mDNS interrogés")
verifier(A._deviner_type('inconnu', [], mdns_service='matter') == 'Objet connecté',
          "service mDNS Matter (appareil commissionné) -> Objet connecté")
verifier(A._deviner_type('inconnu', [], mdns_service='matterc') == 'Objet connecté',
          "service mDNS Matter (en cours d'appairage) -> Objet connecté")

print('\n=== 9. _scan_host() : MAC obtenue via SNMP (ARP d\'un routeur) ou capture, quand '
      'l\'ARP local ne peut structurellement rien donner (sous-réseau routé) ===')
# Signalé en usage réel : des appareils sur un second /24 routé par le même
# routeur restaient invisibles du scan, car l'ARP LOCAL de ce poste ne peut
# jamais résoudre une IP hors de son propre segment L2 — même le repli
# existant (test 5/6 ci-dessus) ne pouvait rien y faire. `snmp_arp_par_ip`
# (table ARP d'un routeur/switch SNMP de l'inventaire) et `capture_arp_par_ip`
# (écoute ARP locale) sont deux sources SUPPLÉMENTAIRES, tentées uniquement
# quand l'ARP local échoue.
A._ping           = lambda ip: False
A._hostname       = lambda ip: ''
A._netbios_name   = lambda ip: ''
A._ttl_os_guess   = lambda ip: ''
A._scan_ports     = lambda ip: []
A._mac_from_arp   = lambda ip: ''          # ARP local : structurellement muet (sous-réseau routé)
A._snmp_get_typed = lambda ip, oids, **kw: {}
A.time.sleep      = lambda s: None
_snmp_arp = {'192.0.2.80': {'mac': 'aa:bb:cc:00:01:02', 'vendor': 'Test Vendor',
                            'sources': [{'nom': 'Routeur-Site', 'ip': '192.0.2.254'}]}}
try:
    resultat_snmp = A._scan_host('192.0.2.80', snmp_arp_par_ip=_snmp_arp)
finally:
    A._ping, A._hostname, A._netbios_name = _ping_orig, _hostname_orig, _netbios_orig
    A._ttl_os_guess, A._scan_ports = _ttl_orig, _ports_orig
    A._mac_from_arp = _arp_orig
    A.time.sleep = _sleep_orig
verifier(resultat_snmp is not None,
          "un hôte injoignable en ARP local mais vu dans la table ARP d'un routeur SNMP n'est plus perdu")
verifier(resultat_snmp is not None and resultat_snmp.get('mac') == 'aa:bb:cc:00:01:02',
          "sa VRAIE MAC (celle du routeur, résolue sur son interface) est bien remontée")
verifier(resultat_snmp is not None and resultat_snmp.get('mac_source') == 'snmp',
          "la provenance de la MAC est explicitement tracée ('snmp', pas confondue avec une résolution locale)")
verifier(resultat_snmp is not None
          and resultat_snmp.get('mac_sources') == [{'nom': 'Routeur-Site', 'ip': '192.0.2.254'}],
          "l'équipement qui l'a vue est rapporté, pour que l'utilisateur sache où regarder")
verifier(resultat_snmp is not None and resultat_snmp.get('silencieux') is True,
          "reste marqué silencieux : il ne répond à AUCUNE sonde active depuis ce poste")

A._ping           = lambda ip: False
A._hostname       = lambda ip: ''
A._netbios_name   = lambda ip: ''
A._ttl_os_guess   = lambda ip: ''
A._scan_ports     = lambda ip: []
A._mac_from_arp   = lambda ip: ''
A._snmp_get_typed = lambda ip, oids, **kw: {}
A.time.sleep      = lambda s: None
_capture_arp = {'192.0.2.81': 'dd:ee:ff:00:01:02'}
try:
    resultat_capture = A._scan_host('192.0.2.81', capture_arp_par_ip=_capture_arp)
finally:
    A._ping, A._hostname, A._netbios_name = _ping_orig, _hostname_orig, _netbios_orig
    A._ttl_os_guess, A._scan_ports = _ttl_orig, _ports_orig
    A._mac_from_arp = _arp_orig
    A.time.sleep = _sleep_orig
verifier(resultat_capture is not None and resultat_capture.get('mac') == 'dd:ee:ff:00:01:02',
          "un hôte muet sur toute sonde active mais vu en écoute ARP locale n'est plus perdu")
verifier(resultat_capture is not None and resultat_capture.get('mac_source') == 'capture',
          "la provenance 'capture' est distincte de 'snmp' (deux sources différentes, pas confondues)")

# Un hôte qui répond normalement en ARP local ne doit JAMAIS se voir
# substituer sa MAC par une source SNMP/capture, même si elle est présente
# (l'ARP local, direct, reste toujours prioritaire — non-régression).
A._ping           = lambda ip: True
A._hostname       = lambda ip: 'poste-normal'
A._netbios_name   = lambda ip: ''
A._ttl_os_guess   = lambda ip: 'Windows'
A._scan_ports     = lambda ip: [445]
A._mac_from_arp   = lambda ip: 'dd:ee:ff:44:55:66'
A._snmp_get_typed = lambda ip, oids, **kw: {}
A.time.sleep      = lambda s: None
try:
    resultat_prio = A._scan_host('192.0.2.82',
                                 snmp_arp_par_ip={'192.0.2.82': {'mac': 'ff:ff:ff:ff:ff:00', 'sources': []}})
finally:
    A._ping, A._hostname, A._netbios_name = _ping_orig, _hostname_orig, _netbios_orig
    A._ttl_os_guess, A._scan_ports = _ttl_orig, _ports_orig
    A._mac_from_arp = _arp_orig
    A.time.sleep = _sleep_orig
verifier(resultat_prio is not None and resultat_prio.get('mac') == 'dd:ee:ff:44:55:66'
          and not resultat_prio.get('mac_source'),
          "l'ARP local reste prioritaire : jamais écrasé par SNMP/capture quand il répond déjà",
          str(resultat_prio.get('mac')))

# Audit réseau 2026-09-05, #08 : un hôte résolu normalement en ARP local ne
# doit JAMAIS recevoir le badge 🌐/📡 même si cette MÊME mac apparaît AUSSI
# dans snmp_arp_par_ip — le cas courant d'une passerelle, que le routeur de
# l'inventaire connaît forcément lui aussi dans sa propre table ARP. L'ancien
# code déduisait la provenance après coup par simple égalité de valeur et se
# trompait précisément dans ce cas ; elle doit être tracée au moment où la
# mac est trouvée, pas redéduite ensuite.
A._ping           = lambda ip: True
A._hostname       = lambda ip: 'passerelle'
A._netbios_name   = lambda ip: ''
A._ttl_os_guess   = lambda ip: 'Linux/Unix'
A._scan_ports     = lambda ip: [80]
A._mac_from_arp   = lambda ip: 'aa:bb:cc:00:01:02'   # même mac que la table ARP du routeur ci-dessous
A._snmp_get_typed = lambda ip, oids, **kw: {}
A.time.sleep      = lambda s: None
try:
    resultat_meme_mac = A._scan_host(
        '192.0.2.83',
        snmp_arp_par_ip={'192.0.2.83': {'mac': 'aa:bb:cc:00:01:02',
                                        'sources': [{'nom': 'Routeur-Site', 'ip': '192.0.2.254'}]}})
finally:
    A._ping, A._hostname, A._netbios_name = _ping_orig, _hostname_orig, _netbios_orig
    A._ttl_os_guess, A._scan_ports = _ttl_orig, _ports_orig
    A._mac_from_arp = _arp_orig
    A.time.sleep = _sleep_orig
verifier(resultat_meme_mac is not None and resultat_meme_mac.get('mac') == 'aa:bb:cc:00:01:02'
          and not resultat_meme_mac.get('mac_source'),
          "une mac identique par coïncidence entre l'ARP local et la table SNMP d'un routeur "
          "n'étiquette PAS l'hôte comme « vu via SNMP » : la provenance suit la RÉSOLUTION, pas la valeur",
          str((resultat_meme_mac.get('mac'), resultat_meme_mac.get('mac_source'))))

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
