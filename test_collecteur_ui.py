#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie l'aperçu en onglets du collecteur graphique.

Ce que le test contrôle :
  - les rubriques se construisent sans exception sur des données partielles
    (l'interface les affiche au fil de la collecte, donc toujours incomplètes)
  - un onglet apparaît par rubrique, dans l'ordre, et se remplit
  - les onglets déjà ouverts ne sont pas recréés quand les données s'étoffent
  - les accents et caractères spéciaux traversent l'affichage intacts

Sans serveur graphique (CI Linux), le test s'arrête proprement.

Usage :
    python test_collecteur_ui.py
"""

import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import collector_core as C  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


# ─── Jeux de données : ce que l'interface reçoit à trois instants ────────────

ETAPE_1 = {
    'hostname': 'POSTE-Réception', 'mac_address': '00:11:22:33:44:55',
    'ip_addresses': ['192.168.1.42'], 'elevated': True,
}
ETAPE_2 = dict(ETAPE_1, **{
    'os_name': 'Windows 11 Professionnel', 'os_version': '10.0.26100',
    'cpu': 'Intel Core i7-1265U', 'ram_gb': 32,
    'brand': 'Dell', 'model': 'Latitude 5430',
})
ETAPE_3 = dict(ETAPE_2, **{
    'disk_total_gb': 476, 'disk_used_gb': 210, 'disk_free_gb': 266,
    'disk_drives': ['C: 476 GB — 210 GB utilisés'],
    'usb_devices': [
        {'name': 'Clé USB SanDisk', 'manufacturer': 'SanDisk', 'model': 'Cruzer Blade',
         'categorie': 'Stockage', 'vid': '0781', 'pid': '5567', 'serial': 'ABC123',
         'install_date': '2026-01-15', 'inventoriable': True},
        {'name': 'Concentrateur USB générique', 'categorie': 'Autre', 'inventoriable': False},
    ],
    'users_details': [
        {'name': 'Éric', 'role': 'Administrateur', 'account_type': 'Microsoft',
         'status': 'Actif', 'enabled': True, 'admin': True},
    ],
    'network_adapter_details': [
        {'name': 'Ethernet', 'physical': True, 'mac_address': '00:11:22:33:44:55',
         'link_speed': '1 Gbps',
         'ip_addresses': [{'address': '192.168.1.42', 'prefix': 24}]},
    ],
    'installed_software': [{'name': 'Suite bureautique — édition Pro', 'version': '2024'}],
    'licenses': [],
})

print('=== 1. Les rubriques se construisent sur des données partielles ===')
tailles = []
for i, donnees in enumerate((ETAPE_1, ETAPE_2, ETAPE_3), 1):
    try:
        sections = C.build_summary_sections(donnees)
        tailles.append(len(sections))
        verifier(True, 'étape %d : %d rubrique(s)' % (i, len(sections)))
    except Exception as e:
        verifier(False, 'étape %d' % i, '%s: %s' % (type(e).__name__, e))
        tailles.append(0)

verifier(tailles == sorted(tailles), 'le nombre de rubriques ne décroît pas', str(tailles))

sections = C.build_summary_sections(ETAPE_3)
cles = [s['cle'] for s in sections]
verifier('usb' in cles, 'rubrique USB présente', str(cles))
usb = [s for s in sections if s['cle'] == 'usb'][0]
tous = [e for b in usb['listes'] for e in b['elements']]
verifier(any('SanDisk' in e and '0781:5567' in e for e in tous),
         'périphérique USB détaillé (marque et identifiant)')
comptes = [s for s in sections if s['cle'] == 'comptes'][0]
verifier(any('Éric' in e and 'Microsoft' in e for b in comptes['listes'] for e in b['elements']),
         'compte accentué et type de compte préservés')

print('\n=== 2. La sortie texte du CLI dérive des mêmes rubriques ===')
lignes = C.build_summary_lines(ETAPE_3)
texte = '\n'.join(lignes)
verifier('POSTE-Réception' in texte, 'accents intacts dans la sortie texte')
verifier('IDENTIFICATION' in texte, 'rubriques titrées')
verifier(len(lignes) > 15, 'résumé non vide', '%d lignes' % len(lignes))

print("\n=== 3. Onglets de l'interface ===")
try:
    import tkinter as tk
    racine = tk.Tk()
    racine.withdraw()
except Exception as e:
    print('  (ignoré : aucun serveur graphique — %s)' % e)
    print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
    sys.exit(1 if echecs else 0)

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    'collecteur_gui', os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'system-info-collector-gui.py'))
gui_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui_module)

# La construction lance normalement collecte et appel réseau : on les neutralise
# pour n'éprouver que l'affichage.
gui_module.CollectorGUI._collect_info = lambda self: None
gui_module.CollectorGUI._fetch_clients = lambda self: None

fenetre = tk.Toplevel(racine)
app = gui_module.CollectorGUI(fenetre, 'http://localhost:3456')

app._update_summary(ETAPE_1)
onglets_1 = len(app.notebook.tabs())
cadres_1 = dict(app.onglets)
verifier(onglets_1 >= 1, 'onglets créés dès les premières données', '%d' % onglets_1)

app._update_summary(ETAPE_3)
onglets_3 = len(app.notebook.tabs())
verifier(onglets_3 > onglets_1, 'de nouveaux onglets apparaissent',
         '%d → %d' % (onglets_1, onglets_3))
verifier(all(app.onglets[c]['cadre'] is cadres_1[c]['cadre'] for c in cadres_1),
         'les onglets existants sont réutilisés, pas recréés')

titres = [app.notebook.tab(t, 'text') for t in app.notebook.tabs()]
verifier(any('Identification' in t for t in titres), 'onglet Identification', str(titres[:3]))
verifier(any('USB' in t for t in titres), 'onglet USB')

contenu = app.onglets['identification']['texte'].get('1.0', tk.END)
verifier('POSTE-Réception' in contenu, "contenu de l'onglet rempli et accentué")
contenu_usb = app.onglets['usb']['texte'].get('1.0', tk.END)
verifier('SanDisk' in contenu_usb, 'onglet USB rempli')

verifier(str(app.pdf_btn['state']) == 'disabled',
         'le bouton PDF reste inactif tant que la collecte n\'est pas finie')

racine.destroy()
print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
