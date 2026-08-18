#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie que la mise à jour macOS télécharge et installe le bon binaire
selon l'architecture (ARM vs Intel).

Contexte : `_get_platform_key()` renvoyait toujours 'macos_app' (le zip ARM)
quelle que soit l'architecture réelle du Mac — publié en v2.15.1 avec le
binaire Intel, mais jamais branché au sélecteur de téléchargement. Un Mac
Intel se voyait donc proposer le binaire ARM par la mise à jour in-app, que
macOS refuse ensuite d'ouvrir (« n'est pas pris en charge par ce Mac »),
signalé par un test réel après un clic sur « Installer ».

Ce que ce test contrôle :
  - _get_platform_key() renvoie 'macos_app_intel' sur un process x86_64,
    'macos_app' sur arm64 — et ne dépend PAS de RUNNING_IN_DOCKER
  - _install_macos() refuse d'installer un binaire dont l'architecture ne
    correspond pas à la machine en cours, et laisse l'ancienne version
    (qui fonctionne) intacte plutôt que de la remplacer par une version
    qui ne démarrera pas
  - à l'inverse, une architecture qui correspond est installée normalement

Usage :
    python test_maj_macos_architecture.py
"""

import io
import os
import sys
import tempfile
import zipfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_dossier_donnees = tempfile.mkdtemp(prefix='majarch_data_')
import database as _db  # noqa: E402
_db.init_paths(os.path.join(_dossier_donnees, 'parc_info.db'),
               os.path.join(_dossier_donnees, 'uploads'))

import update_checker as UC  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


def _preparer_macos(machine):
    """Simule un exécutable macOS figé, sans jamais toucher au vrai sys.frozen
    au-delà de la portée de ce test (restauré par l'appelant)."""
    sys.frozen = True
    UC.platform.system = lambda: 'Darwin'
    UC.platform.machine = lambda: machine
    # running_in_docker() se rabat sur l'existence de /.dockerenv : sans ce
    # monkeypatch, ce test échouerait faussement s'il tourne à l'intérieur
    # d'un conteneur (vérification Linux/Docker de la suite).
    UC.running_in_docker = lambda: False


_ancien_frozen = getattr(sys, 'frozen', False)
_ancien_platform_system = UC.platform.system
_ancien_platform_machine = UC.platform.machine
_ancien_running_in_docker = UC.running_in_docker
_faux_run_original = UC.subprocess.run
_faux_popen_original = UC.subprocess.Popen

print("=== 0. archi_materielle_macos() résiste à la traduction Rosetta ===")


def _sysctl_repond(sortie, code=0):
    def _run(cmd, **kw):
        if cmd and cmd[0] == 'sysctl':
            class _R:
                stdout = sortie
                stderr = ''
                returncode = code
            return _R()
        raise FileNotFoundError(cmd[0])
    return _run


UC.platform.machine = lambda: 'x86_64'  # process traduit par Rosetta
UC.subprocess.run = _sysctl_repond('1\n')
verifier(UC.archi_materielle_macos() == 'arm64',
         "Mac Apple Silicon sous Rosetta (process x86_64) -> matériel détecté 'arm64'",
         UC.archi_materielle_macos())

UC.subprocess.run = _sysctl_repond('0\n')
verifier(UC.archi_materielle_macos() == 'x86_64',
         "sysctl répond '0' (pas Apple Silicon) -> repli sur platform.machine()",
         UC.archi_materielle_macos())

UC.subprocess.run = _sysctl_repond('', code=1)  # sysctl: unknown oid — vrai Mac Intel
verifier(UC.archi_materielle_macos() == 'x86_64',
         "sysctl échoue (vrai Mac Intel, oid inconnu) -> repli sur platform.machine()",
         UC.archi_materielle_macos())

UC.subprocess.run = _faux_run_original

print("\n=== 1. _get_platform_key() choisit le bon fichier selon l'architecture ===")
checker = UC.UpdateChecker(config_dir=tempfile.mkdtemp(prefix='majarch_cfg_'))

_preparer_macos('x86_64')
verifier(checker._get_platform_key() == 'macos_app_intel',
         "Mac Intel (x86_64) -> clé 'macos_app_intel'", checker._get_platform_key())

_preparer_macos('arm64')
verifier(checker._get_platform_key() == 'macos_app',
         "Mac Apple Silicon (arm64) -> clé 'macos_app'", checker._get_platform_key())

print("\n=== 2. _install_macos() refuse une architecture qui ne correspond pas ===")


def _construire_bundle_zip(dossier, contenu_exe):
    app = dossier / 'ParcInfo.app' / 'Contents' / 'MacOS'
    app.mkdir(parents=True)
    (app / 'ParcInfo').write_text(contenu_exe)
    zip_path = dossier.with_suffix('.zip')
    with zipfile.ZipFile(zip_path, 'w') as zf:
        for racine, _, fichiers in os.walk(dossier):
            for f in fichiers:
                chemin = os.path.join(racine, f)
                zf.write(chemin, os.path.relpath(chemin, dossier))
    return zip_path


from pathlib import Path  # noqa: E402


def _fausse_commande_file(arch_annoncee):
    def _run(cmd, **kw):
        if cmd and cmd[0] == 'file':
            class _R:
                stdout = 'Mach-O 64-bit executable %s\n' % arch_annoncee
                stderr = ''
                returncode = 0
            return _R()
        if cmd and cmd[0] in ('open', 'pgrep'):
            # Simule une relance réussie : la vérification de démarrage
            # ajoutée à _install_macos() ne doit pas dépendre d'un vrai
            # macOS ici — sans ce faux succès immédiat, `pgrep` échouerait
            # (absent sur cette machine) à chaque tentative de la boucle de
            # vérification, jusqu'à épuiser ses ~10 s et faire échouer à tort
            # le test 3 (résultat False au lieu de True).
            class _R:
                stdout = ''
                stderr = ''
                returncode = 0
            return _R()
        # xattr / codesign : laisser échouer naturellement (FileNotFoundError
        # sur cette machine) — déjà couvert par test_migration_donnees_macos.py.
        return _faux_run_original(cmd, **kw)
    return _run


def _fausse_relance(cmd, **kw):
    # Ne pas vraiment relancer/ouvrir quoi que ce soit — mais laisser
    # subprocess.run() (utilisé par _debloquer_gatekeeper_macos) tranquille :
    # il se sert de Popen en interne, un remplacement global le casserait.
    if cmd and cmd[0] == '/bin/sh':
        return None
    return _faux_popen_original(cmd, **kw)


UC.subprocess.Popen = _fausse_relance

# Ancien bundle « en place », qui fonctionne — ne doit pas être touché si le
# nouveau a la mauvaise architecture.
dossier_ancien = Path(tempfile.mkdtemp(prefix='majarch_ancien_')) / 'ParcInfo.app'
(dossier_ancien / 'Contents' / 'MacOS').mkdir(parents=True)
(dossier_ancien / 'Contents' / 'MacOS' / 'ParcInfo').write_text('ancienne version, fonctionnelle')

dossier_nouveau_src = Path(tempfile.mkdtemp(prefix='majarch_nouveau_')) / 'racine'
zip_mauvaise_archi = _construire_bundle_zip(dossier_nouveau_src, 'nouvelle version')

_preparer_macos('x86_64')  # Mac Intel
UC.platform.machine = lambda: 'x86_64'
UC.subprocess.run = _fausse_commande_file('arm64')  # le zip livré est ARM
checker._macos_app_path = lambda: dossier_ancien

resultat = checker._install_macos(zip_mauvaise_archi)
verifier(resultat is False, "installation refusée quand l'architecture ne correspond pas")
verifier((dossier_ancien / 'Contents' / 'MacOS' / 'ParcInfo').read_text() == 'ancienne version, fonctionnelle',
         "l'ancienne version (qui fonctionne) reste intacte, pas remplacée par une version cassée")

print("\n=== 3. _install_macos() installe normalement quand l'architecture correspond ===")
dossier_nouveau_src2 = Path(tempfile.mkdtemp(prefix='majarch_nouveau2_')) / 'racine'
zip_bonne_archi = _construire_bundle_zip(dossier_nouveau_src2, 'nouvelle version, bonne architecture')

UC.subprocess.run = _fausse_commande_file('x86_64')  # le zip livré est bien x86_64, comme la machine
resultat = checker._install_macos(zip_bonne_archi)
verifier(resultat is True, "installation acceptée quand l'architecture correspond")
verifier((dossier_ancien / 'Contents' / 'MacOS' / 'ParcInfo').read_text()
         == 'nouvelle version, bonne architecture',
         "le bundle est bien remplacé dans ce cas")

UC.subprocess.run = _faux_run_original
UC.subprocess.Popen = _faux_popen_original
sys.frozen = _ancien_frozen
UC.platform.system = _ancien_platform_system
UC.platform.machine = _ancien_platform_machine
UC.running_in_docker = _ancien_running_in_docker

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
