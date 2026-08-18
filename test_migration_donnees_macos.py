#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie que les données macOS (BD, uploads, secret.key, backups) ne
peuvent plus disparaître à la mise à jour.

Contexte : sur macOS, une mise à jour remplace le bundle .app en entier
(update_checker.py::_install_macos) — l'ancien bundle est mis de côté puis
supprimé. Stocker les données persistantes « à côté de l'exe », comme le
fait Windows en sécurité (une mise à jour n'y remplace qu'un seul fichier),
les plaçait DANS ce bundle condamné.

Ce que ce test contrôle :
  - launcher.data() pointe vers ~/Library/Application Support/ParcInfo sur
    macOS (frozen), et se comporte comme avant partout ailleurs
  - launcher._migrer_donnees_macos_si_besoin() rattrape une installation
    antérieure au correctif (données encore dans l'ancien emplacement)
  - ce rattrapage ne fait rien si le nouvel emplacement a déjà des données,
    ni si l'ancien n'en a pas
  - update_checker.py protège aussi le chemin de mise à jour in-app lui-même :
    _migrer_donnees_bundle_macos() sauve les données de l'ancien bundle
    avant qu'_install_macos() ne le supprime

Usage :
    python test_migration_donnees_macos.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import launcher                              # noqa: E402
import update_checker as UC                  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


class _Logger:
    def info(self, *a, **k): pass
    def error(self, *a, **k): pass


print('=== 1. data() : macOS va dans Application Support, le reste ne change pas ===')
# Un faux bundle sous tempfile, jamais un chemin réel comme /Applications/... :
# os.path.dirname('/Applications/...') résout sous la racine du lecteur
# courant sur Windows (ex. E:\Applications\...), hors du projet.
_ancien_frozen = getattr(sys, 'frozen', False)
_ancien_executable = sys.executable
_faux_bundle_racine = tempfile.mkdtemp(prefix='faux_app_')
sys.frozen = True
sys.executable = os.path.join(_faux_bundle_racine, 'ParcInfo.app', 'Contents', 'MacOS', 'ParcInfo')

nouveau_dossier = tempfile.mkdtemp(prefix='appsupport_')
os.environ['PARCINFO_MACOS_DATA_DIR'] = nouveau_dossier
launcher.platform.system = lambda: 'Darwin'
verifier(launcher.data() == nouveau_dossier,
         'data() pointe vers le dossier Application Support (macOS)', launcher.data())

launcher.platform.system = lambda: 'Windows'
verifier(launcher.data() == os.path.dirname(sys.executable),
         "data() reste à côté de l'exe sur Windows (comportement historique, sûr)")

print('\n=== 2. Rattrapage (launcher.py) : une installation antérieure au correctif ===')
launcher.platform.system = lambda: 'Darwin'
ancien_dossier = os.path.dirname(sys.executable)
os.makedirs(ancien_dossier, exist_ok=True)
with open(os.path.join(ancien_dossier, 'parc_info.db'), 'w') as f:
    f.write('base de données factice')
with open(os.path.join(ancien_dossier, 'secret.key'), 'w') as f:
    f.write('clé factice')
os.makedirs(os.path.join(ancien_dossier, 'uploads'), exist_ok=True)
with open(os.path.join(ancien_dossier, 'uploads', 'doc.pdf'), 'w') as f:
    f.write('document factice')

launcher._migrer_donnees_macos_si_besoin(_Logger())

verifier(os.path.exists(os.path.join(nouveau_dossier, 'parc_info.db')),
         'la BD a rejoint le nouvel emplacement')
verifier(os.path.exists(os.path.join(nouveau_dossier, 'secret.key')),
         'la clé de chiffrement a rejoint le nouvel emplacement — sinon les '
         'identifiants stockés deviennent illisibles à jamais')
verifier(os.path.exists(os.path.join(nouveau_dossier, 'uploads', 'doc.pdf')),
         'les uploads ont rejoint le nouvel emplacement')
verifier(not os.path.exists(os.path.join(ancien_dossier, 'parc_info.db')),
         "l'ancien emplacement ne garde pas une copie orpheline")

print('\n=== 3. Le rattrapage ne rejoue pas si le nouvel emplacement a déjà des données ===')
with open(os.path.join(ancien_dossier, 'parc_info.db'), 'w') as f:
    f.write('une autre BD, laissée par une collecte suivante')
launcher._migrer_donnees_macos_si_besoin(_Logger())
with open(os.path.join(nouveau_dossier, 'parc_info.db')) as f:
    verifier(f.read() == 'base de données factice',
             'la BD déjà en place au nouvel emplacement est intouchée '
             '(pas de rejeu qui écraserait des données réelles)')

print('\n=== 4. Rien à migrer : le rattrapage ne fait rien ===')
dossier_vierge = tempfile.mkdtemp(prefix='appsupport_vierge_')
os.environ['PARCINFO_MACOS_DATA_DIR'] = dossier_vierge
sys.executable = os.path.join(tempfile.mkdtemp(prefix='faux_app2_'),
                              'ParcInfo.app', 'Contents', 'MacOS', 'ParcInfo')
os.makedirs(os.path.dirname(sys.executable), exist_ok=True)
launcher._migrer_donnees_macos_si_besoin(_Logger())
verifier(not os.path.exists(os.path.join(dossier_vierge, 'parc_info.db')),
         "aucune BD n'apparaît quand il n'y avait rien à rattraper")

sys.frozen = _ancien_frozen
sys.executable = _ancien_executable
launcher.platform.system = __import__('platform').system
os.environ.pop('PARCINFO_MACOS_DATA_DIR', None)

print("\n=== 5. update_checker.py : protège aussi le chemin de mise à jour in-app ===")
# _install_macos() met l'ancien bundle de côté sous ce nom avant de le
# supprimer ; _migrer_donnees_bundle_macos() doit agir avant cette
# suppression, pas après.
from pathlib import Path  # noqa: E402

ancien_bundle = Path(tempfile.mkdtemp(prefix='ancien_bundle_')) / 'ParcInfo.app.old'
ancien_macos = ancien_bundle / 'Contents' / 'MacOS'
ancien_macos.mkdir(parents=True)
(ancien_macos / 'parc_info.db').write_text('base réelle avant mise à jour')
(ancien_macos / 'secret.key').write_text('clé réelle avant mise à jour')

nouveau_appsupport = tempfile.mkdtemp(prefix='appsupport_maj_')
os.environ['PARCINFO_MACOS_DATA_DIR'] = nouveau_appsupport

UC.UpdateChecker._migrer_donnees_bundle_macos(ancien_bundle)

verifier(Path(nouveau_appsupport, 'parc_info.db').exists(),
         "la BD de l'ancien bundle est sauvée avant sa suppression par "
         "_install_macos()")
verifier(Path(nouveau_appsupport, 'secret.key').exists(),
         "la clé de chiffrement de l'ancien bundle est sauvée elle aussi")

print('\n=== 6. update_checker.py : ne fait rien si rien à sauver ou déjà sauvé ===')
bundle_vide = Path(tempfile.mkdtemp(prefix='bundle_vide_')) / 'ParcInfo.app.old'
(bundle_vide / 'Contents' / 'MacOS').mkdir(parents=True)
UC.UpdateChecker._migrer_donnees_bundle_macos(bundle_vide)  # ne doit pas lever

(ancien_macos / 'parc_info.db').write_text('une base plus récente, sans rapport')
UC.UpdateChecker._migrer_donnees_bundle_macos(ancien_bundle)
verifier(Path(nouveau_appsupport, 'parc_info.db').read_text() == 'base réelle avant mise à jour',
         'une BD déjà migrée au nouvel emplacement est intouchée')

os.environ.pop('PARCINFO_MACOS_DATA_DIR', None)

print("\n=== 7. Déblocage Gatekeeper (xattr -cr + signature ad hoc) ===")
# xattr/codesign n'existent pas forcément sur la machine qui fait tourner ce
# test (Windows, ou Linux en CI) — _debloquer_gatekeeper_macos() doit
# dégrader proprement (logguer, ne jamais lever) plutôt que planter le
# remplacement de bundle pour une histoire de commande absente.
# time.sleep neutralisé : la vérification spctl est répétée à quelques
# secondes d'intervalle en usage réel (laisser à syspolicyd le temps de
# prendre en compte le retrait de quarantaine) — inutile de ralentir ce test
# pour autant, spctl échouera de la même façon immédiatement ou 5 secondes
# plus tard sur une machine qui n'a pas cette commande.
_faux_sleep = UC.time.sleep
UC.time.sleep = lambda s: None

faux_bundle = Path(tempfile.mkdtemp(prefix='gatekeeper_'))
try:
    UC.UpdateChecker._debloquer_gatekeeper_macos(faux_bundle)
    leve = False
except Exception as e:
    leve = e
verifier(leve is False, "aucune exception même si xattr/codesign sont absents", str(leve))

UC.time.sleep = _faux_sleep

print("\n=== 8. spctl --add : dernier recours après xattr/codesign ===")
# Simule uniquement 'osascript' (invite mot de passe) et 'spctl' (vérif finale) —
# tout le reste (xattr, codesign) lève FileNotFoundError naturellement sur cette
# machine, comme dans la section 7.
_faux_run_gatekeeper = UC.subprocess.run


def _mock_spctl_add(code_osascript, spctl_accepte_ensuite):
    def _run(cmd, **kw):
        if cmd and cmd[0] == 'osascript':
            class _R:
                returncode = code_osascript
                stdout = ''
                stderr = '' if code_osascript == 0 else 'execution error: User canceled. (-128)'
            return _R()
        if cmd and cmd[0] == 'spctl':
            class _R:
                returncode = 0 if spctl_accepte_ensuite else 1
                stdout = ''
                stderr = ''
            return _R()
        raise FileNotFoundError(cmd[0])
    return _run


faux_bundle_spctladd = Path(tempfile.mkdtemp(prefix='gatekeeper_spctladd_'))

UC.subprocess.run = _mock_spctl_add(0, True)
resultat = UC.UpdateChecker._autoriser_via_spctl_add(faux_bundle_spctladd)
verifier(resultat is True, "spctl --add posé avec succès -> Gatekeeper accepte ensuite le bundle")

UC.subprocess.run = _mock_spctl_add(1, False)
resultat = UC.UpdateChecker._autoriser_via_spctl_add(faux_bundle_spctladd)
verifier(resultat is False,
         "invite mot de passe annulée par l'utilisateur (-128) -> échec propre, pas d'exception")

UC.subprocess.run = _faux_run_gatekeeper

print("\n=== 9. Délai de grâce après xattr -cr : évite la signature si spctl finit par accepter ===")
# Reproduit le scénario signalé en usage réel : xattr -cr seul suffit, mais
# syspolicyd met un instant à en tenir compte. Si la 3e vérification (sur les
# 5 tentées) accepte enfin le bundle, codesign ne doit JAMAIS être appelé.
_faux_sleep3 = UC.time.sleep
UC.time.sleep = lambda s: None
_compteur_spctl = {'n': 0}
_codesign_appele = {'valeur': False}


def _mock_delai_grace(cmd, **kw):
    class _R:
        returncode = 0
        stdout = ''
        stderr = ''
    if cmd and cmd[0] == 'xattr':
        return _R()
    if cmd and cmd[0] == 'spctl':
        _compteur_spctl['n'] += 1
        r = _R()
        r.returncode = 0 if _compteur_spctl['n'] >= 3 else 1
        return r
    if cmd and cmd[0] == 'codesign':
        _codesign_appele['valeur'] = True
        return _R()
    raise FileNotFoundError(cmd[0])


UC.subprocess.run = _mock_delai_grace
faux_bundle_delai = Path(tempfile.mkdtemp(prefix='gatekeeper_delai_'))
UC.UpdateChecker._debloquer_gatekeeper_macos(faux_bundle_delai)
verifier(_compteur_spctl['n'] == 3,
         "spctl est réinterrogé plusieurs fois avant d'abandonner", str(_compteur_spctl['n']))
verifier(_codesign_appele['valeur'] is False,
         "codesign n'est jamais appelé si spctl finit par accepter tout seul")

UC.subprocess.run = _faux_run_gatekeeper
UC.time.sleep = _faux_sleep3

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
