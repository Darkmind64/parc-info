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
import tempfile

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

print("\n=== 7. Agents, mots de passe, maintenance et démarrage — bonne rubrique ===")
DONNEES_AGENTS = dict(ETAPE_3, **{
    'local_password_policy': {'min_length': 12, 'complexity': True, 'lockout_threshold': 5},
    'rdp_allowed_users': ['Alice'],
    'saved_rdp_credentials': ['ANCIEN-SERVEUR'],
    'remote_support_agents': [{'marque': 'AnyDesk', 'nom': 'AnyDesk',
                               'service': 'AnyDesk Service', 'actif': True}],
    'edr_agents': [{'marque': 'CrowdStrike', 'nom': 'CrowdStrike Falcon',
                    'service': 'CSFalconService', 'actif': True}],
    'power_plan': 'Performances élevées', 'fast_startup': False,
    'defender_last_quick_scan': '2026-08-11 10:12',
    'dotnet_versions': ['.NET Framework 4.8.1'],
    'boot_mode': 'UEFI',
    'disk_partition_styles': [{'number': 0, 'style': 'GPT', 'boot': True}],
    'rdp_logon_history': [{'user': 'Alice', 'ip': '82.1.2.3', 'when': '2026-08-10 09:00'}],
    'malware_detections': [{'threat': 'Trojan:Win32/Wacatac.B!ml', 'category': 'Trojan',
                             'level': 'danger', 'process': 'chrome.exe',
                             'resource': r'C:\Users\Davy\Downloads\x.exe',
                             'when': '2026-08-01 14:00', 'cleaned': True}],
    'malware_detections_total': 1,
    'system_errors': [{'provider': 'Disk', 'event_id': 7, 'message': 'Erreur E/S disque',
                        'count': 3, 'last_seen': '2026-08-12 08:00'}],
    'application_errors': [{'application': 'app.exe', 'type': 'Plantage', 'module': 'ucrtbase.dll',
                             'exception': "Violation d'accès mémoire",
                             'path': r'C:\Program Files\App\app.exe',
                             'count': 2, 'last_seen': '2026-08-11 09:00'}],
    'shutdown_history': [{'when': '2026-08-10 18:00', 'action': 'Redémarrage',
                           'reason': "Mise à jour Windows (planifié)", 'planned': True, 'user': 'SYSTEM'}],
    'top_processes_cpu': [{'name': 'msedge', 'cpu_pct': 12.4, 'ram_mb': 900.1}],
    'top_processes_ram': [{'name': 'vmmem', 'cpu_pct': 0, 'ram_mb': 1872.3}],
    'unsigned_drivers': [{'device': 'EldoS PnP virtual bus', 'version': '1.0.0.1',
                          'provider': 'EldoS Corporation'}],
    'group_policies': [{'name': 'Stratégie de groupe locale', 'scope': 'Utilisateur',
                        'enabled': True, 'denied': False}],
    'firewall_rules': [{'name': 'ShareMouse', 'protocol': 'TCP/UDP', 'port': 'Tout',
                        'profiles': 'Domaine, Privé, Public'}],
    'firewall_rules_total': 1,
})
sections2 = {s['cle']: s for s in C.build_summary_sections(DONNEES_AGENTS)}


def contenu2(cle):
    s = sections2.get(cle)
    if not s:
        return ''
    return ' | '.join([str(v) for _, v in s['champs']]
                      + [str(e) for l in s['listes'] for e in l['elements']])


verifier('12' in contenu2('securite') and 'ANYDESK' not in contenu2('securite').upper(),
         'politique de mot de passe dans « Sécurité »', contenu2('securite')[:150])
verifier('Alice' in contenu2('securite') and '82.1.2.3' in contenu2('securite'),
         'historique des connexions RDP entrantes dans « Sécurité »')
verifier('Alice' in contenu2('acces') and 'ANCIEN-SERVEUR' in contenu2('acces'),
         'membres RDP et identifiants enregistrés dans « Accès distant »')
verifier('AnyDesk' in contenu2('acces') and 'CrowdStrike' in contenu2('acces'),
         'agents de télémaintenance et EDR dans « Accès distant »')
verifier('lements' not in contenu2('acces'), 'pas de résidu de sérialisation dans « Accès distant »')
verifier('lev' in contenu2('hygiene') and '4.8.1' in contenu2('hygiene'),
         "plan d'alimentation et .NET dans « Hygiène système »")
verifier('UEFI' in contenu2('stockage') and 'GPT' in contenu2('stockage'),
         'mode de démarrage et style de partition dans « Stockage »')
verifier('Wacatac' in contenu2('securite') and 'Wacatac' not in contenu2('diagnostic'),
         'détections antivirus dans « Sécurité », pas dans « Diagnostic »')
verifier('Disk' in contenu2('diagnostic') and "Violation d'accès mémoire" in contenu2('diagnostic')
         and 'Redémarrage' in contenu2('diagnostic'),
         'erreurs système, erreurs applicatives et arrêts/redémarrages dans « Diagnostic »')
verifier("Violation d'accès mémoire" not in contenu2('securite') and 'Redémarrage' not in contenu2('securite'),
         'erreurs applicatives et arrêts/redémarrages absents de « Sécurité »')
verifier('msedge' in contenu2('diagnostic') and 'vmmem' in contenu2('diagnostic'),
         'processus les plus gourmands (CPU et RAM) dans « Diagnostic »')
verifier('EldoS' in contenu2('securite') and 'EldoS' not in contenu2('diagnostic'),
         'pilotes non signés dans « Sécurité », pas dans « Diagnostic »')
verifier('Stratégie de groupe locale' in contenu2('environnement')
         and 'Stratégie de groupe locale' not in contenu2('securite'),
         'stratégies de groupe dans « Environnement », pas dans « Sécurité »')
verifier('ShareMouse' in contenu2('securite') and 'Domaine, Privé, Public' in contenu2('securite'),
         'règles de pare-feu dans « Sécurité »')

print("\n=== 8. Détection des agents par sous-chaîne du nom affiché ===")
SERVICES_TEST = [
    {'DisplayName': 'AnyDesk Service', 'State': 'Running'},
    {'DisplayName': 'TeamViewer', 'State': 'Stopped'},
    {'DisplayName': 'CrowdStrike Falcon Sensor Service', 'State': 'Running'},
    {'DisplayName': 'Spouleur d\'impression', 'State': 'Running'},
]
rmm_trouves = C.chercher_agents(SERVICES_TEST, C._AGENTS_RMM)
verifier(len(rmm_trouves) == 2, '2 agents RMM/télémaintenance trouvés', str(rmm_trouves))
anydesk = next((a for a in rmm_trouves if a['nom'] == 'AnyDesk'), None)
verifier(anydesk is not None and anydesk['actif'] is True,
         'AnyDesk détecté et actif (service Running)')
teamviewer = next((a for a in rmm_trouves if 'TeamViewer' in a['nom']), None)
verifier(teamviewer is not None and teamviewer['actif'] is False,
         'TeamViewer détecté et inactif (service Stopped)')
edr_trouves = C.chercher_agents(SERVICES_TEST, C._AGENTS_EDR)
verifier(len(edr_trouves) == 1 and edr_trouves[0]['marque'] == 'CrowdStrike',
         'CrowdStrike détecté comme EDR, pas comme agent RMM', str(edr_trouves))
tous = C.chercher_agents(SERVICES_TEST, C._AGENTS_RMM) + edr_trouves
verifier(not any('Spouleur' in a['service'] for a in tous),
         "le spouleur d'impression n'est pas pris pour un agent", str(tous))

DOUBLON = [{'DisplayName': 'AnyDesk Service', 'State': 'Running'},
          {'DisplayName': 'AnyDesk Client Service', 'State': 'Running'}]
verifier(len(C.chercher_agents(DOUBLON, C._AGENTS_RMM)) == 1,
         'un même produit détecté par deux services ne compte qu\'une fois',
         str(C.chercher_agents(DOUBLON, C._AGENTS_RMM)))

print("\n=== 9. Format d'un identifiant AnyDesk ===")
for candidat, attendu in (('1418397731', True), ('141 839 773', True),
                          ("Usage: anydesk [options]", False), ('', False), ('12', False)):
    verifier(bool(C._RE_ANYDESK_ID.match(candidat)) == attendu,
             'reconnaissance de « %s »' % (candidat or '(vide)'), 'attendu=%s' % attendu)

print("\n=== 10. Version .NET Framework depuis le numéro de build ===")
for build, attendu in ((533320, '4.8.1'), (528040, '4.8'), (461808, '4.7.2'),
                       (378389, '4.5'), (100000, None), (0, None)):
    obtenu = C._dotnet_version_from_release(build)
    verifier(obtenu == attendu, 'build %d → %s' % (build, attendu), 'obtenu=%s' % obtenu)

print("\n=== 11. Ressource Defender ramenée à un chemin lisible ===")
for brut, attendu in (
    (r'file:_C:\Users\Davy\AppData\Local\Temp\x.exe', r'C:\Users\Davy\AppData\Local\Temp\x.exe'),
    (r'webfile:_D:\Téléchargements\x.exe|https://exemple.fr/x.exe|pid:123,ProcessStart:456',
     r'D:\Téléchargements\x.exe'),
    ('', ''),
):
    obtenu = C._chemin_ressource_defender(brut)
    verifier(obtenu == attendu, 'nettoyage de « %s… »' % brut[:30], 'obtenu=%r' % obtenu)

print("\n=== 12. Catégorie d'une menace, depuis le préfixe de son nom ===")
for nom, categorie_attendue, niveau_attendu in (
    ('Trojan:Win32/Wacatac.B!ml', 'Trojan', 'danger'),
    ('PUA:Win32/DownloadAdmin', 'PUA', 'warn'),
    ('Ransom:Win32/Something', 'Ransom', 'danger'),
    ('MenaceSansDeuxPoints', 'Autre', 'muted'),
):
    categorie, niveau = C._categoriser_menace(nom)
    verifier(categorie == categorie_attendue and niveau == niveau_attendu,
             'catégorie de « %s »' % nom, '%s/%s' % (categorie, niveau))

print("\n=== 13. Plantage (1000) et blocage (1002) ne partagent pas le même schéma de champs ===")
# Un 1002 (« ne répond plus ») a un schéma à 10 champs, sans équivalent aux
# positions module/exception/chemin d'un 1000 — les y lire renvoyait un
# horodatage ou un GUID travesti en « module ». Fige le comportement corrigé.
type_1000, module, exception, chemin = C._champs_erreur_application(
    1000, 'ucrtbase.dll', 'c0000005', r'C:\Program Files\App\app.exe')
verifier(type_1000 == 'Plantage' and module == 'ucrtbase.dll'
         and exception == "Violation d'accès mémoire" and chemin == r'C:\Program Files\App\app.exe',
         'un 1000 conserve module, exception et chemin', str((type_1000, module, exception, chemin)))

type_1002, module2, exception2, chemin2 = C._champs_erreur_application(
    1002, '01dd1793f839d42d', '7ac407ac-ca53-4004-853f-e06dea69ce5b', None)
verifier(type_1002 == 'Ne répond plus' and module2 is None and exception2 is None and chemin2 is None,
         "un 1002 n'hérite pas des positions d'un 1000 (horodatage/GUID pris pour module/exception)",
         str((type_1002, module2, exception2, chemin2)))

code_inconnu = C._champs_erreur_application(1000, None, 'deadbeef', None)
verifier(code_inconnu[2] == 'deadbeef',
         'un code d\'exception non répertorié est affiché brut plutôt que masqué')

print("\n=== 14. Code STOP d'un écran bleu, depuis le param1 de l'événement 1001 ===")
for param1, code_attendu, label_attendu in (
    ('0x0000001a (0x0000000000041792, 0xffffbc058fc0d010, 0x0, 0x5)',
     '0x0000001a', 'MEMORY_MANAGEMENT'),
    ('0x00000124 (0x0, 0xffffb0011a2b3040, 0xb2000000, 0x51000)',
     '0x00000124', 'WHEA_UNCORRECTABLE_ERROR (défaillance matérielle probable)'),
    ('0x000000ab (paramètres non répertoriés)', '0x000000ab', None),
    ('', None, None),
    (None, None, None),
):
    code, label = C._code_arret_depuis_param1(param1)
    verifier(code == code_attendu and label == label_attendu,
             'code depuis « %s »' % (param1 or '(vide)')[:40], '%s/%s' % (code, label))

print("\n=== 15. Rapport gpresult /X : GPO appliquées, périmètres utilisateur et ordinateur ===")
_XML_GPO = """<?xml version="1.0"?>
<Rsop xmlns="http://www.microsoft.com/GroupPolicy/Rsop">
  <UserResults>
    <GPO><Name>Stratégie de groupe locale</Name><Enabled>true</Enabled><AccessDenied>false</AccessDenied></GPO>
    <GPO><Name>Restriction USB</Name><Enabled>false</Enabled><AccessDenied>false</AccessDenied></GPO>
  </UserResults>
  <ComputerResults>
    <GPO><Name>Pare-feu renforcé</Name><Enabled>true</Enabled><AccessDenied>true</AccessDenied></GPO>
  </ComputerResults>
</Rsop>"""
_fd, _chemin_gpo = tempfile.mkstemp(suffix='.xml')
with os.fdopen(_fd, 'w', encoding='utf-8') as _f:
    _f.write(_XML_GPO)
gpos = C._lire_rapport_gpo(_chemin_gpo).get('group_policies', [])
os.remove(_chemin_gpo)
verifier(len(gpos) == 3, '3 GPO lues (2 utilisateur + 1 ordinateur)', str(len(gpos)))
verifier(any(g['name'] == 'Stratégie de groupe locale' and g['scope'] == 'Utilisateur' and g['enabled']
             for g in gpos), 'GPO utilisateur active correctement typée')
verifier(any(g['name'] == 'Restriction USB' and not g['enabled'] for g in gpos),
         'GPO désactivée correctement détectée')
verifier(any(g['name'] == 'Pare-feu renforcé' and g['scope'] == 'Ordinateur' and g['denied']
             for g in gpos), 'GPO ordinateur refusée correctement détectée')

_fd2, _chemin_vide = tempfile.mkstemp(suffix='.xml')
with os.fdopen(_fd2, 'w', encoding='utf-8') as _f:
    _f.write('<?xml version="1.0"?><Rsop xmlns="http://www.microsoft.com/GroupPolicy/Rsop">'
             '<UserResults></UserResults></Rsop>')
verifier(C._lire_rapport_gpo(_chemin_vide) == {},
         'aucune GPO appliquée → dict vide, pas une liste vide bruyante')
os.remove(_chemin_vide)

print("\n=== 16. Règles de pare-feu : parsing, filtrage et fusion ===")
# Reproduit le format réel de « netsh advfirewall firewall show rule » —
# labels FR, « LocalPort »/« Profiles » volontairement non traduits par
# Microsoft lui-même (constaté sur une collecte réelle).
_TEXTE_PARE_FEU = """
Nom de la règle :                     AnyDesk
----------------------------------------------------------------------
Activé :                              Oui
Direction :                           Actif
Profiles :                            Domaine,Privé,Public
Groupement :
LocalIP :                             Tout
RemoteIP :                            Tout
Protocole :                           TCP
LocalPort :                           Tout
RemotePort :                          Tout
Action :                              Autoriser

Nom de la règle :                     AnyDesk
----------------------------------------------------------------------
Activé :                              Oui
Direction :                           Actif
Profiles :                            Public
Groupement :
LocalIP :                             Tout
RemoteIP :                            Tout
Protocole :                           UDP
LocalPort :                           Tout
RemotePort :                          Tout
Action :                              Autoriser

Nom de la règle :                     Découverte du réseau (mDNS-In)
----------------------------------------------------------------------
Activé :                              Oui
Direction :                           Actif
Profiles :                            Privé
Groupement :                          Découverte du réseau
LocalIP :                             Tout
RemoteIP :                            Tout
Protocole :                           UDP
LocalPort :                           5355
RemotePort :                          Tout
Action :                              Autoriser

Nom de la règle :                     Ancienne règle désactivée
----------------------------------------------------------------------
Activé :                              Non
Direction :                           Actif
Profiles :                            Public
Groupement :
LocalIP :                             Tout
RemoteIP :                            Tout
Protocole :                           TCP
LocalPort :                           4444
RemotePort :                          Tout
Action :                              Autoriser

Nom de la règle :                     Blocage explicite d'un logiciel
----------------------------------------------------------------------
Activé :                              Oui
Direction :                           Actif
Profiles :                            Public
Groupement :
LocalIP :                             Tout
RemoteIP :                            Tout
Protocole :                           TCP
LocalPort :                           Tout
RemotePort :                          Tout
Action :                              Bloquer

Nom de la règle :                     HNS Container Networking - DNS (UDP-In) - C15A218E-FD5B-45AC-BDB3-6A342DDD224F - 0
----------------------------------------------------------------------
Activé :                              Oui
Direction :                           Actif
Profiles :                            Domaine,Privé,Public
Groupement :
LocalIP :                             Tout
RemoteIP :                            Tout
Protocole :                           UDP
LocalPort :                           53
RemotePort :                          Tout
Action :                              Autoriser
"""

toutes = C._parse_regles_pare_feu(_TEXTE_PARE_FEU)
verifier(len(toutes) == 6, '6 règles reconnues dans le texte', str(len(toutes)))
verifier(toutes[0]['nom'] == 'AnyDesk' and toutes[0]['actif'] and toutes[0]['action'],
         'première règle correctement typée (nom, actif, action)')
verifier(toutes[2]['groupe'] == 'Découverte du réseau',
         'groupement accentué correctement lu', toutes[2].get('groupe'))

resultat = C._filtrer_fusionner_regles_pare_feu(toutes)
regles = {r['name']: r for r in resultat.get('firewall_rules', [])}
verifier(set(regles) == {'AnyDesk'}, 'seule AnyDesk survit au filtre', str(sorted(regles)))
verifier(regles.get('AnyDesk', {}).get('protocol') == 'TCP/UDP',
         'les deux protocoles AnyDesk sont fusionnés en une entrée')
verifier(regles.get('AnyDesk', {}).get('profiles') == 'Domaine, Privé, Public',
         'les profils des deux règles AnyDesk sont réunis, pas juste concaténés',
         regles.get('AnyDesk', {}).get('profiles'))
verifier('Découverte du réseau (mDNS-In)' not in regles,
         'une règle groupée (fonctionnalité Windows) est écartée')
verifier('Ancienne règle désactivée' not in regles, 'une règle désactivée est écartée')
verifier("Blocage explicite d'un logiciel" not in regles, 'une règle de blocage est écartée')
verifier(not any('HNS Container' in n for n in regles),
         'une règle au nom généré (suffixe GUID) est écartée')
verifier(resultat.get('firewall_rules_total') == 1,
         'le total reflète le nombre après filtre+fusion, pas le nombre brut')

verifier(C._filtrer_fusionner_regles_pare_feu([]) == {},
         'aucune règle en entrée → dict vide')
verifier(C._parse_regles_pare_feu('') == [], 'texte vide → liste vide, pas une exception')

print("\n=== 17. Onglets de l'interface ===")
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
