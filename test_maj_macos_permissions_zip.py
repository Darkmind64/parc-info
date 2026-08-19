#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie que le bit exécutable survit à l'extraction du zip macOS.

Contexte : signalé en usage réel via le journal parcinfo.log (voir 2.18.7) —
la mise à jour macOS téléchargeait et remplaçait bien le bundle, mais le
lancement direct de l'exécutable (2.18.10, qui a remplacé `open` pour
éliminer la translocation macOS) échouait avec [Errno 13] Permission denied.

Cause : le zip macOS est créé par `zip -r` (voir .github/workflows/*.yml),
qui embarque bien le mode Unix de chaque fichier dans
ZipInfo.external_attr — mais zipfile.ZipFile.extractall() de la bibliothèque
standard Python ignore silencieusement cette métadonnée, laissant chaque
fichier extrait avec les permissions par défaut du système qui extrait,
sans le bit +x. `open` (Launch Services), utilisé jusqu'à la 2.18.9,
tolérait apparemment cette absence ; subprocess.Popen() directement sur le
binaire, non — c'est justement le passage au lancement direct qui a
démasqué ce problème resté invisible depuis toujours derrière des
diagnostics Gatekeeper qui n'étaient pas la vraie cause.

Ce que ce test contrôle :
  - un fichier marqué exécutable (rwxr-xr-x) dans les métadonnées du zip
    reçoit bien un chmod restaurant ce mode après extraction
  - un fichier sans métadonnées Unix dans le zip (external_attr=0, cas rare
    d'un zip créé sans passer par un outil Unix) n'est pas touché

os.chmod est mocké plutôt que vérifié via os.stat() après coup : le modèle
de permissions Windows (la machine qui fait tourner ce test) ne préserve
pas les bits Unix de la même façon que macOS/Linux — mocker isole la
LOGIQUE testée (quel mode est calculé, sur quel fichier) de la plateforme.

Usage :
    python test_maj_macos_permissions_zip.py
"""

import io
import os
import sys
import tempfile
import zipfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import update_checker as UC  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


print("=== 1. Le bit exécutable d'une entrée zip Unix est restauré ===")

dossier_zip = tempfile.mkdtemp(prefix='majperm_zip_')
chemin_zip = os.path.join(dossier_zip, 'ParcInfo-macOS-Intel.zip')

with zipfile.ZipFile(chemin_zip, 'w') as zf:
    # Exécutable : rwxr-xr-x, comme `zip -r` sur Unix pour Contents/MacOS/ParcInfo
    info_exe = zipfile.ZipInfo('ParcInfo.app/Contents/MacOS/ParcInfo')
    info_exe.external_attr = (0o100755) << 16
    zf.writestr(info_exe, b'faux binaire')

    # Fichier ordinaire : rw-r--r--, ne doit pas se retrouver exécutable
    info_txt = zipfile.ZipInfo('ParcInfo.app/Contents/Info.plist')
    info_txt.external_attr = (0o100644) << 16
    zf.writestr(info_txt, b'<plist/>')

    # Troisième entrée, écrite normalement (ZipFile.writestr() attribue lui-même
    # un external_attr par défaut — 0o600 << 16 — quand on ne le précise pas, donc
    # impossible de produire un vrai external_attr=0 par ce chemin ; le cas "zip
    # sans métadonnées Unix" est couvert séparément plus bas, en isolation).
    zf.writestr('ParcInfo.app/Contents/Resources/icon.icns', b'donnees')

dossier_extraction = tempfile.mkdtemp(prefix='majperm_extract_')
with zipfile.ZipFile(chemin_zip) as zf:
    zf.extractall(dossier_extraction)

    appels_chmod = []
    _chmod_original = os.chmod

    def _chmod_espion(path, mode):
        appels_chmod.append((path, mode))

    os.chmod = _chmod_espion
    try:
        UC._restaurer_permissions_unix_zip(zf, dossier_extraction)
    finally:
        os.chmod = _chmod_original

chemins_chmod = {os.path.relpath(p, dossier_extraction).replace('\\', '/'): m for p, m in appels_chmod}

verifier('ParcInfo.app/Contents/MacOS/ParcInfo' in chemins_chmod,
         "chmod appelé sur l'exécutable")
verifier(chemins_chmod.get('ParcInfo.app/Contents/MacOS/ParcInfo') == 0o755,
         "mode restauré = 0o755 (rwxr-xr-x)",
         oct(chemins_chmod.get('ParcInfo.app/Contents/MacOS/ParcInfo', 0)))
verifier(chemins_chmod.get('ParcInfo.app/Contents/Info.plist') == 0o644,
         "fichier ordinaire restauré en 0o644, pas rendu exécutable",
         oct(chemins_chmod.get('ParcInfo.app/Contents/Info.plist', 0)))
verifier('ParcInfo.app/Contents/Resources/icon.icns' in chemins_chmod,
         "un fichier écrit sans mode explicite reçoit quand même un chmod "
         "(writestr() lui attribue 0o600 par défaut)")

print("\n=== 2. Une entrée sans la moindre métadonnée Unix (external_attr=0) ne plante pas ===")
# Cas rare (zip produit par un outil qui n'écrit pas le mode Unix) : isolé de
# writestr(), qui attribue toujours un external_attr par défaut (voir plus
# haut) — un faux objet minimal reproduit exactement ce que
# _restaurer_permissions_unix_zip lit réellement (infolist(), is_dir(),
# external_attr, filename), sans dépendre du comportement de writestr().


class _FausseEntreeZip:
    def __init__(self, filename, external_attr, is_dir=False):
        self.filename = filename
        self.external_attr = external_attr
        self._is_dir = is_dir

    def is_dir(self):
        return self._is_dir


class _FauxZip:
    def __init__(self, entries):
        self._entries = entries

    def infolist(self):
        return self._entries


dossier_isole = tempfile.mkdtemp(prefix='majperm_isole_')
os.makedirs(os.path.join(dossier_isole, 'sous_dossier'), exist_ok=True)
with open(os.path.join(dossier_isole, 'sans_metadonnees'), 'w', encoding='utf-8') as f:
    f.write('x')

faux_zip = _FauxZip([
    _FausseEntreeZip('sans_metadonnees', external_attr=0),
    _FausseEntreeZip('sous_dossier/', external_attr=(0o40755) << 16, is_dir=True),
])

appels_chmod2 = []
os.chmod = lambda path, mode: appels_chmod2.append((path, mode))
try:
    leve = False
    try:
        UC._restaurer_permissions_unix_zip(faux_zip, dossier_isole)
    except Exception as e:
        leve = e
finally:
    os.chmod = _chmod_original

verifier(leve is False, "aucune exception même avec external_attr=0", str(leve))
verifier(len(appels_chmod2) == 0,
         "ni le fichier sans métadonnées ni le dossier ne déclenchent de chmod",
         str(appels_chmod2))

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
