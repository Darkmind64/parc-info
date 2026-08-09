#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie le mécanisme de mise à jour.

Un serveur HTTP local joue le rôle de GitHub : il sert un version.json, un
SHA256SUMS.txt et un faux binaire. On contrôle ce qui compte vraiment :

  - une version plus récente est détectée, une plus ancienne ne l'est pas
  - un binaire dont l'empreinte ne correspond pas est refusé et effacé
  - sans empreinte publiée, le téléchargement est refusé avant de commencer
  - un téléchargement interrompu ne laisse pas de fichier d'apparence valide
  - écarter une version la masque, la version suivante réapparaît

Usage :
    python test_maj.py
"""

import hashlib
import http.server
import io
import json
import os
import socket
import sys
import tempfile
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import update_checker as UC  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


# ─── Faux dépôt servi en local ───────────────────────────────────────────────

BINAIRE = b'ParcInfo binaire factice ' * 4096          # ~100 Ko
EMPREINTE = hashlib.sha256(BINAIRE).hexdigest()
RACINE = tempfile.mkdtemp(prefix='maj_srv_')

port = socket.socket()
port.bind(('127.0.0.1', 0))
PORT = port.getsockname()[1]
port.close()
BASE = 'http://127.0.0.1:%d' % PORT

VERSION_JSON = {
    'version': '9.9.9',
    'downloads': {
        'windows_installer': BASE + '/ParcInfo-Windows.exe',
        'macos_app': BASE + '/ParcInfo-macOS-ARM.zip',
    },
    'checksums': {'windows_installer_sha256': 'PENDING_BUILD'},
    'notes': {'improvements': ['Une amélioration accentuée : coût réduit']},
    'release_notes_url': BASE + '/notes',
}

with open(os.path.join(RACINE, 'ParcInfo-Windows.exe'), 'wb') as f:
    f.write(BINAIRE)
with open(os.path.join(RACINE, 'version.json'), 'w', encoding='utf-8') as f:
    json.dump(VERSION_JSON, f, ensure_ascii=False)
with open(os.path.join(RACINE, 'SHA256SUMS.txt'), 'w', encoding='utf-8') as f:
    f.write('%s  ParcInfo-Windows.exe\n' % EMPREINTE)


class Serveur(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=RACINE, **kw)

    def log_message(self, *a):
        pass


httpd = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Serveur)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

# Le fichier d'empreintes est cherché à une URL construite à partir du dépôt :
# on la détourne vers le serveur local.
UC.SHA256SUMS_URL = BASE + '/SHA256SUMS.txt'


def neuf(version_locale='2.0.0'):
    c = UC.UpdateChecker(config_dir=tempfile.mkdtemp(prefix='maj_cfg_'),
                         version_json_url=BASE + '/version.json')
    c.current_version = version_locale
    return c


print('=== 1. Détection de version ===')
c = neuf('2.0.0')
verifier(c.check_for_updates(force=True) is True, 'version plus récente détectée',
         str(c.latest_version))
verifier(c.release_notes and 'accentuée' in c.release_notes[0],
         'notes de version lues sans perte d\'accents')
c2 = neuf('9.9.9')
verifier(c2.check_for_updates(force=True) is False, 'version identique : rien à annoncer')
c3 = neuf('10.0.0')
verifier(c3.check_for_updates(force=True) is False, 'version locale plus récente : rien à annoncer')
verifier(c3._is_newer_version('2.10.0', '2.9.9') is True, 'comparaison 2.10.0 > 2.9.9')
verifier(c3._is_newer_version('v2.6.43', '2.6.42') is True, 'préfixe v toléré')

print('\n=== 2. Le binaire est vérifié avant d\'être proposé ===')
c = neuf('2.0.0')
c.check_for_updates(force=True)
# On force la plateforme : le test doit valoir quel que soit l'OS qui l'exécute.
c._get_platform_key = lambda: 'windows_installer'
fichier = c.download_update()
verifier(fichier is not None and fichier.exists(), 'téléchargement abouti')
verifier(c._calculate_checksum(fichier) == EMPREINTE, 'empreinte conforme')

print('\n=== 3. Un binaire altéré est rejeté ===')
with open(os.path.join(RACINE, 'SHA256SUMS.txt'), 'w', encoding='utf-8') as f:
    f.write('%s  ParcInfo-Windows.exe\n' % ('0' * 64))
c = neuf('2.0.0')
c.check_for_updates(force=True)
c._get_platform_key = lambda: 'windows_installer'
try:
    c.download_update()
    verifier(False, 'empreinte fausse refusée', 'aucune exception levée')
except UC.UpdateCheckError as e:
    verifier('mpreinte' in str(e), 'empreinte fausse refusée', str(e)[:60])
    verifier(not (c.config_dir / 'ParcInfo-Windows.exe').exists(),
             'le fichier rejeté est effacé')

print('\n=== 4. Sans empreinte publiée, rien n\'est téléchargé ===')
os.remove(os.path.join(RACINE, 'SHA256SUMS.txt'))
c = neuf('2.0.0')
c.check_for_updates(force=True)
c._get_platform_key = lambda: 'windows_installer'
try:
    c.download_update()
    verifier(False, 'absence d\'empreinte bloquante', 'aucune exception levée')
except UC.UpdateCheckError as e:
    verifier('mpreinte' in str(e), 'absence d\'empreinte bloquante', str(e)[:60])
    verifier(not (c.config_dir / 'ParcInfo-Windows.exe').exists(),
             'aucun fichier laissé derrière')

print('\n=== 5. Modes d\'exécution ===')
os.environ['RUNNING_IN_DOCKER'] = '1'
verifier(UC.runtime_mode() == 'docker', 'conteneur reconnu', UC.runtime_mode())
verifier(UC.can_self_install() is False, 'un conteneur ne s\'installe pas lui-même')
cmds = UC.docker_pull_commands('2.6.43')
verifier('compose pull' in cmds['compose'] and '2.6.43' in cmds['run'],
         'commandes Docker proposées')
os.environ.pop('RUNNING_IN_DOCKER')
verifier(UC.runtime_mode() == 'source', 'exécution depuis les sources reconnue',
         UC.runtime_mode())
verifier(UC.can_self_install() is False, 'pas d\'auto-installation depuis les sources')

print('\n=== 6. État exposé à l\'interface ===')
import update_notifier as UN  # noqa: E402

cfg = tempfile.mkdtemp(prefix='maj_not_')
n = UN.UpdateNotifier(config_dir=cfg)
n.checker.version_json_url = BASE + '/version.json'
n.checker.current_version = '2.0.0'
n.verifier(force=True)
e = n.etat
verifier(e['mise_a_jour_disponible'] is True, 'mise à jour signalée')
verifier(e['version_disponible'] == '9.9.9', 'version annoncée', str(e['version_disponible']))
verifier(e['masquee'] is False, 'bannière visible par défaut')
verifier(e['installable'] is False, 'installation refusée hors exécutable')

n.ecarter()
verifier(n.etat['masquee'] is True, 'version écartée : bannière masquée')

# Une nouvelle instance relit le choix sur disque : redémarrer l'application ne
# doit pas faire réapparaître une annonce déjà écartée.
n2 = UN.UpdateNotifier(config_dir=cfg)
n2.checker.version_json_url = BASE + '/version.json'
n2.checker.current_version = '2.0.0'
n2.verifier(force=True)
verifier(n2.etat['masquee'] is True, 'choix conservé après redémarrage')

# ... mais la version suivante doit réapparaître.
VERSION_JSON['version'] = '9.9.10'
with open(os.path.join(RACINE, 'version.json'), 'w', encoding='utf-8') as f:
    json.dump(VERSION_JSON, f, ensure_ascii=False)
n2.verifier(force=True)
verifier(n2.etat['masquee'] is False, 'la version suivante réapparaît',
         str(n2.etat['version_disponible']))

print('\n=== 7. Installation impossible hors exécutable ===')
verifier(n2.installer() is False, 'installation refusée proprement')
verifier(bool(n2.erreur), 'raison expliquée', str(n2.erreur)[:70])

print('\n=== 8. Confirmation au premier démarrage après mise à jour ===')
# La version installée est lue depuis __version__ : on la simule le temps du test.
version_reelle = UC.__version__
cfg_inst = tempfile.mkdtemp(prefix='maj_inst_')
with open(os.path.join(cfg_inst, 'update_state.json'), 'w', encoding='utf-8') as f:
    json.dump({'version_vue': '2.6.41'}, f)

UC.__version__ = '2.6.43'
try:
    apres = UN.UpdateNotifier(config_dir=cfg_inst)
    verifier(apres.etat['version_installee'] == '2.6.43',
             'la mise à jour appliquée est annoncée',
             str(apres.etat['version_installee']))
    apres.ecarter()
    verifier(apres.etat['version_installee'] is None, 'annonce refermable')

    # Deuxième démarrage sur la même version : plus rien à annoncer.
    encore = UN.UpdateNotifier(config_dir=cfg_inst)
    verifier(encore.etat['version_installee'] is None,
             'annonce non répétée au démarrage suivant')

    # Une version plus ANCIENNE que celle vue (retour arrière manuel) ne doit
    # pas être présentée comme une mise à jour réussie.
    with open(os.path.join(cfg_inst, 'update_state.json'), 'w', encoding='utf-8') as f:
        json.dump({'version_vue': '2.6.50'}, f)
    recul = UN.UpdateNotifier(config_dir=cfg_inst)
    verifier(recul.etat['version_installee'] is None, 'retour arrière non annoncé')
finally:
    UC.__version__ = version_reelle

httpd.shutdown()
print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
