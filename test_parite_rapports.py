#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie que la fiche système et le rapport PDF montrent les mêmes données.

Les deux rendus sont écrits séparément — l'un en Jinja pour le web, l'autre en
reportlab — et ont dérivé une première fois jusqu'à 60 clés présentes d'un côté
et absentes de l'autre. Ce test compare les clés du snapshot que chacun
consomme et échoue dès qu'une donnée n'est rendue que d'un seul côté.

Usage :
    python test_parite_rapports.py
"""

import io
import os
import re
import sys

RACINE = os.path.dirname(os.path.abspath(__file__))

# Clés volontairement présentes d'un seul côté, avec la raison. Toute autre
# divergence fait échouer le test.
TOLERANCES = {
    # Rendues via une variable dédiée ou une forme dérivée
    'gpu': 'le PDF et la fiche affichent gpu_details, plus complet',
    'installed_software': 'la fiche le reçoit via la variable `logiciels`',
    'ram_gb': 'consommé par les vignettes chiffrées (_pdf_kpis)',
    'ram_free_gb': 'consommé par les vignettes chiffrées (_pdf_kpis)',
    'disk_used_gb': 'consommé par les vignettes chiffrées (_pdf_kpis)',
    'disk_total_gb': 'consommé par les vignettes chiffrées (_pdf_kpis)',
    'disk_free_gb': 'affiché dans les barres par volume',
    'memory_slots_free': 'le PDF affiche occupés/total, qui le contient',
    'elevated': "l'avertissement d'élévation est rendu hors tableau",
    'rdp_enabled': 'rendu dans « Accès distant » via remote_access ; la clé reste '
                   "lue par build_alerts pour le point d'attention",
    'rdp_nla': 'idem rdp_enabled — rendu via remote_access',
    'firewall': 'remplacé par firewall_profiles quand il est disponible',
    'bios_release_date': "utilisé pour calculer l'âge du matériel",
    'battery_charge_percent': 'consommé par les vignettes chiffrées',
    # Compteurs agrégés : la fiche les rend via le bandeau de points
    # d'attention, alimenté par la même fonction build_alerts que le PDF.
    'unexpected_shutdowns': "rendu via le bandeau de points d'attention",
    'disk_error_events': "rendu via le bandeau de points d'attention",
    'pending_updates_security': "rendu via le bandeau de points d'attention",
    'problem_devices_count': "rendu via le bandeau de points d'attention ; "
                             "la fiche compte elle-même les périphériques listés",
    'uptime_hours': 'consommé par les vignettes chiffrées',
    # Icônes réelles d'application (32x32, base64) : décoratif, propre à la
    # fiche web. Le PDF n'affiche aucune icône pour cette section (ni avant,
    # ni après leur ajout) — texte seul, comme le reste du document imprimé.
    'default_browser_icon': "pas d'icône dans le PDF, texte seul",
    'default_mail_icon': "pas d'icône dans le PDF, texte seul",
}


def cles_fiche():
    chemin = os.path.join(RACINE, 'templates', 'fiche_systeme.html')
    contenu = io.open(chemin, encoding='utf-8').read()
    return set(re.findall(r'rapport\.([a-z_0-9]+)', contenu))


def cles_pdf():
    """Toutes les clés lues par la chaîne de rendu PDF, helpers compris."""
    contenu = io.open(os.path.join(RACINE, 'collector_core.py'), encoding='utf-8').read()
    debut = contenu.index('# RAPPORT PDF')
    portion = contenu[debut:]
    cles = set(re.findall(r"info\.get\('([a-z_0-9]+)'", portion))
    cles |= set(re.findall(r"info\['([a-z_0-9]+)'\]", portion))
    # Les vignettes et les alertes sont partagées entre les deux rendus
    for fonction in ('_pdf_kpis', 'build_alerts', '_fiche', '_disks_html'):
        for bloc in re.findall(r'def %s\w*\(.*?(?=\ndef )' % fonction, contenu, re.S):
            cles |= set(re.findall(r"info\.get\('([a-z_0-9]+)'", bloc))
            cles |= set(re.findall(r"info\['([a-z_0-9]+)'\]", bloc))
    return cles


def main():
    fiche, pdf = cles_fiche(), cles_pdf()
    absent_pdf = sorted(k for k in fiche - pdf if k not in TOLERANCES)
    absent_fiche = sorted(k for k in pdf - fiche if k not in TOLERANCES)

    print('  fiche système : %d clés' % len(fiche))
    print('  rapport PDF   : %d clés' % len(pdf))
    print('  tolérances    : %d' % len(TOLERANCES))

    if absent_pdf:
        print('\n  ABSENT DU RAPPORT PDF (%d) :' % len(absent_pdf))
        for k in absent_pdf:
            print('     -', k)
    if absent_fiche:
        print('\n  ABSENT DE LA FICHE SYSTÈME (%d) :' % len(absent_fiche))
        for k in absent_fiche:
            print('     -', k)

    if absent_pdf or absent_fiche:
        print('\n  ÉCHEC : une donnée est rendue d\'un seul côté.')
        print('  Ajoutez-la au rendu manquant, ou inscrivez-la dans TOLERANCES')
        print('  avec la raison si la divergence est voulue.')
        return 1

    print('\n  OK : les deux rendus couvrent les mêmes données.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
