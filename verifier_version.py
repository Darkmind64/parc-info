#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie que les cinq sources de version concordent.

Le numéro de version est écrit à cinq endroits : __version__.py, version.json,
Dockerfile, VERSION et README.md. Une seule qui reste en arrière et le
mécanisme de mise à jour propose une version qui ne correspond pas au binaire
publié — c'est arrivé, un tag a été posé sur du code portant le numéro
précédent.

Lancé par la CI avant toute construction, et utilisable à la main :
    python verifier_version.py
"""

import io
import json
import os
import re
import sys

RACINE = os.path.dirname(os.path.abspath(__file__))


def lire(nom):
    return io.open(os.path.join(RACINE, nom), encoding='utf-8').read()


def extraire(motif, contenu, source):
    trouve = re.search(motif, contenu)
    if not trouve:
        raise SystemExit("Version introuvable dans %s" % source)
    return trouve.group(1)


def main():
    versions = {
        '__version__.py': extraire(r'__version__\s*=\s*"([^"]+)"',
                                   lire('__version__.py'), '__version__.py'),
        'version.json': json.loads(lire('version.json'))['version'],
        'Dockerfile': extraire(r'LABEL version="([^"]+)"', lire('Dockerfile'), 'Dockerfile'),
        'VERSION': extraire(r'PARCINFO_VERSION=(\S+)', lire('VERSION'), 'VERSION'),
    }

    # Le tuple doit suivre la chaîne : il sert aux comparaisons de version.
    tuple_declare = extraire(r'__version_tuple__\s*=\s*\(([^)]+)\)',
                             lire('__version__.py'), '__version__.py')
    tuple_attendu = ', '.join(versions['__version__.py'].split('.'))

    # README : seuls les liens de téléchargement et la version déclarée sont
    # contrôlés. Exiger que TOUS les numéros cités soient le courant refusait
    # les rappels historiques légitimes (« avant 2.7.1, la mise à jour… »),
    # alors que le risque réel est un lien pointant vers une version disparue.
    readme = lire('README.md')
    dans_readme = sorted(set(
        re.findall(r'releases/download/v(\d+\.\d+\.\d+)/', readme)
        + re.findall(r'\*\*Version\*\*\s*:\s*(\d+\.\d+\.\d+)', readme)
        + re.findall(r'docker pull darkmind64/parcinfo:v?(\d+\.\d+\.\d+)', readme)))

    print("Versions déclarées :")
    for source, valeur in versions.items():
        print("  %-16s %s" % (source, valeur))
    print("  %-16s %s" % ('__version_tuple__', tuple_declare))
    print("  %-16s %s" % ('README.md', ', '.join(dans_readme) or '(aucun)'))

    erreurs = []
    distinctes = set(versions.values())
    if len(distinctes) != 1:
        erreurs.append("les sources divergent : %s" % versions)
    attendue = versions['__version__.py']
    if tuple_declare.replace(' ', '') != tuple_attendu.replace(' ', ''):
        erreurs.append("__version_tuple__ vaut (%s), attendu (%s)"
                       % (tuple_declare, tuple_attendu))
    if dans_readme and dans_readme != [attendue]:
        erreurs.append("README.md cite %s au lieu de %s uniquement"
                       % (', '.join(dans_readme), attendue))

    # Sur un tag, le nom du tag doit lui aussi correspondre : c'est le contrôle
    # qui manquait le jour où v2.6.33 a été posé sur du code en 2.6.32.
    ref = os.environ.get('GITHUB_REF_NAME', '')
    if re.match(r'^v\d+\.\d+\.\d+$', ref) and ref[1:] != attendue:
        erreurs.append("le tag %s ne correspond pas à la version %s" % (ref, attendue))

    if erreurs:
        print("\nÉCHEC :")
        for e in erreurs:
            print("  - %s" % e)
        return 1

    print("\n  Les cinq sources concordent sur %s." % attendue)
    return 0


if __name__ == '__main__':
    sys.exit(main())
