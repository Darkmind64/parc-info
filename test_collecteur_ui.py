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

print("\n=== 3. Les disques physiques se relisent pour la fiche système ===")
# Le collecteur assemble une phrase à partir de champs distincts ; la fiche la
# redécompose pour aligner les colonnes et sortir l'état SMART en badge. Les
# deux doivent rester d'accord — d'où ce test sur le format réellement produit.
CAS = [
    ("Samsung SSD 850 PRO 1TB — SSD — 953.9 GB — Santé (SMART): Healthy",
     dict(nom='Samsung SSD 850 PRO 1TB', type='SSD', taille_go=953.9,
          sante='Sain', sante_niveau='ok', etat=None)),
    ("ST1000LM024 HN-M101MBB — HDD — 931.5 GB — Santé (SMART): Warning (Degraded)",
     dict(nom='ST1000LM024 HN-M101MBB', type='HDD', taille_go=931.5,
          sante='À surveiller', sante_niveau='warn', etat='Degraded')),
    ("X — HDD — 12.5 GB — Santé (SMART): Unhealthy",
     dict(nom='X', type='HDD', taille_go=12.5,
          sante='Défaillant', sante_niveau='danger', etat=None)),
]
for ligne, attendu in CAS:
    obtenu = C.parse_physical_disk(ligne)
    ecarts = [k for k, v in attendu.items() if not obtenu or obtenu.get(k) != v]
    verifier(not ecarts, 'relecture de « %s… »' % ligne[:26],
             str({k: (obtenu or {}).get(k) for k in ecarts}))

# Un état inconnu doit rester affichable, sans être présenté comme sain.
inconnu = C.parse_physical_disk("Y — SSD — 1.0 GB — Santé (SMART): Bizarre")
verifier(inconnu and inconnu['sante_niveau'] == 'muted',
         "un état SMART inattendu n'est pas annoncé comme sain",
         str(inconnu))
# Format d'un autre OS : la ligne doit être rendue telle quelle, pas inventée.
verifier(C.parse_physical_disk('disque macOS 500 Go') is None,
         'un format non reconnu est refusé plutôt que deviné')

print("\n=== 4. Criticité des voies d'accès distant ===")
# Un accès sensible actif (Telnet, ouverture auto) doit passer en rouge ; RDP
# sans NLA aussi ; un service arrêté reste neutre.
rdp_nla_off = C._acces('rdp', 'RDP', True, '', securise=False)
verifier(rdp_nla_off['level'] == 'danger', 'RDP sans NLA est signalé en rouge',
         rdp_nla_off['level'])
rdp_nla_on = C._acces('rdp', 'RDP', True, '', securise=True)
verifier(rdp_nla_on['level'] == 'ok', 'RDP avec NLA est acceptable', rdp_nla_on['level'])
telnet_on = C._acces('t', 'Telnet', True, '', sensible=True)
verifier(telnet_on['level'] == 'danger', 'un serveur Telnet actif est en rouge')
absent = C._acces('s', 'SSH', None, 'service absent')
verifier(absent['level'] == 'muted', 'un service absent reste neutre')

print("\n=== 5. Comptes Thunderbird reconstruits depuis prefs.js ===")
PREFS = '\n'.join([
    'user_pref("mail.account.account1.server", "server1");',
    'user_pref("mail.account.account1.identities", "id1");',
    'user_pref("mail.server.server1.hostname", "imap.exemple.fr");',
    'user_pref("mail.server.server1.type", "imap");',
    'user_pref("mail.server.server1.port", 993);',
    'user_pref("mail.identity.id1.useremail", "jean@exemple.fr");',
    'user_pref("mail.identity.id1.fullName", "Jean Exemple");',
    'user_pref("mail.identity.id1.smtpServer", "smtp1");',
    'user_pref("mail.smtpserver.smtp1.hostname", "smtp.exemple.fr");',
    'user_pref("mail.smtpserver.smtp1.port", 587);',
])
comptes = C._parse_thunderbird_prefs(PREFS, 'profil.default')
verifier(len(comptes) == 1, 'un compte reconstruit', '%d' % len(comptes))
if comptes:
    c0 = comptes[0]
    verifier(c0['email'] == 'jean@exemple.fr', 'adresse reliée via identité', c0['email'])
    verifier(c0['incoming_server'] == 'imap.exemple.fr' and c0['incoming_port'] == 993,
             'serveur entrant + port', '%s:%s' % (c0['incoming_server'], c0['incoming_port']))
    verifier(c0['outgoing_server'] == 'smtp.exemple.fr' and c0['outgoing_port'] == 587,
             'serveur sortant relié par clé smtp', '%s:%s' % (c0['outgoing_server'], c0['outgoing_port']))
    verifier(c0['protocol'] == 'IMAP', 'protocole', c0['protocol'])

print("\n=== 6. Chaque information sort dans la bonne rubrique, une seule fois ===")
# Redémarrage en attente, lecteurs mappés, partages exposés, journal de
# sécurité, certificats et profils utilisateurs vivaient tous dans des
# rubriques sans rapport avec eux (Diagnostic, Applications par défaut) —
# ou, pour le rattachement au domaine, en double avec Environnement. Ce test
# fige leur nouvel emplacement pour que la fiche et le PDF, alignés dessus,
# ne divergent pas à nouveau.
DONNEES_REGROUPEMENT = dict(ETAPE_3, **{
    'domain': 'ANCIEN-CHAMP', 'workgroup': 'ANCIEN-CHAMP',
    'domain_name': 'exemple.local', 'domain_joined': True,
    'default_browser': 'Firefox',
    'reboot_pending': True, 'reboot_reasons': ['Windows Update'],
    'mapped_drives': [{'letter': 'Z:', 'path': r'\\serveur\partage'}],
    'smb_shares': [{'name': 'Public', 'path': r'C:\Public', 'administrative': False}],
    'security_events': [{'compte': 'invite', 'type': 'Échec', 'count': 3}],
    'certificates_expiring': [{'sujet': 'RDS', 'expire_le': '2026-09-01', 'jours_restants': 20}],
    'user_profiles': [{'nom': 'Éric', 'taille_go': 12.5, 'mesure_complete': True}],
})
sections = {s['cle']: s for s in C.build_summary_sections(DONNEES_REGROUPEMENT)}


def contenu(cle):
    # Une rubrique sans aucun contenu est écartée par build_summary_sections
    # (un onglet vide ne se distingue pas d'une collecte en échec) — absente,
    # elle ne peut pas non plus contenir un doublon.
    s = sections.get(cle)
    if not s:
        return ''
    return ' | '.join([str(v) for _, v in s['champs']]
                      + [str(e) for l in s['listes'] for e in l['elements']])


verifier('domain' not in contenu('systeme') and 'ANCIEN-CHAMP' not in contenu('systeme'),
         "le rattachement au domaine n'est plus doublé dans « Système »",
         contenu('systeme')[:120])
verifier('oui' in contenu('hygiene').lower() and 'Windows Update' in contenu('hygiene'),
         'redémarrage en attente dans « Hygiène système »')
verifier('reboot' not in contenu('poste').lower() and 'attente' not in contenu('poste').lower(),
         "redémarrage en attente absent d'« Applications par défaut »")
verifier(r'\\serveur\partage' in contenu('reseau') and 'Public' in contenu('reseau'),
         'lecteurs mappés et partages exposés réunis dans « Réseau »')
verifier(r'\\serveur\partage' not in contenu('poste'),
         "lecteurs mappés absents d'« Applications par défaut »")
verifier('invite' in contenu('securite') and 'RDS' in contenu('securite'),
         'journal de sécurité et certificats dans « Sécurité »')
verifier('invite' not in contenu('diagnostic') and 'RDS' not in contenu('diagnostic'),
         "journal de sécurité et certificats absents de « Diagnostic »")
verifier('12.5' in contenu('comptes'), 'profils utilisateurs dans « Comptes »')
verifier('12.5' not in contenu('diagnostic'), "profils utilisateurs absents de « Diagnostic »")

print("\n=== 7. Onglets de l'interface ===")
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
