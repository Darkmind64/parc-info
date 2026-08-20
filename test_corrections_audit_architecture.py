#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie les 5 corrections issues de l'audit architecture (2026-08-20) :

  1. Scan réseau : une modale demande toujours le client cible avant de
     scanner, avec pré-sélection uniquement si un seul client correspond
     sans ambiguïté au réseau physique courant (endpoint
     /api/scan/client-suggere).
  2. API collecteur (/api/device-info & co) : un client peut désormais
     définir son propre jeton dédié (clients.collecteur_token), fermant la
     faille « un jeton valide pour un client donne accès à tous les
     autres » — sans rien casser pour les déploiements qui n'utilisent
     qu'un jeton global unique ou aucun jeton du tout.
  3. Suppression d'un appareil / d'un contrat : les tables liées sont
     désormais nettoyées (PRAGMA foreign_keys=ON + nettoyage manuel pour
     les colonnes sans FK déclarée) au lieu de laisser des lignes
     orphelines.
  4. Collecteur (/api/device-info) : marque/modèle/n° de série ne sont
     écrasés que si le champ est encore vide en base — IP, MAC, OS, RAM,
     etc. restent toujours resynchronisés à chaque collecte.
  5. Import scan réseau et import CSV (appareils/périphériques) : chaque
     création ou mise à jour est désormais journalisée dans l'historique.

Usage :
    python test_corrections_audit_architecture.py
"""

import io
import ipaddress
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='audit_arch_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A                       # noqa: E402
from config_helpers import cfg_set    # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


A.init_db()
conn = A.get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (1, 'admin', 'x', 'Administrateur', 'admin', 1)")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client Un')")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (2, 'Client Deux')")
conn.commit()
conn.close()

client = A.app.test_client()
CSRF = 'jeton-test-csrf'
HDR = {'X-CSRF-Token': CSRF}


def connecter(cid):
    with client.session_transaction() as s:
        s['auth_user_id'] = 1
        s['client_id'] = cid
        s['csrf_token'] = CSRF


# ═══════════════════════════════════════════════════════════════════════════
print('=== 1. Point 3 — suppression appareil : nettoyage des tables liées ===')
connecter(1)
conn = A.get_db()
now = '2026-08-20T00:00:00'
conn.execute("INSERT INTO appareils (client_id, nom_machine, date_creation, date_maj) "
             "VALUES (1, 'Poste-A-Supprimer', ?, ?)", (now, now))
app_id = conn.execute("SELECT id FROM appareils WHERE nom_machine='Poste-A-Supprimer'").fetchone()[0]

conn.execute("INSERT INTO contrats (client_id, titre, date_creation) VALUES (1, 'Contrat annexe', ?)", (now,))
contrat_annexe_id = conn.execute("SELECT id FROM contrats WHERE titre='Contrat annexe'").fetchone()[0]

conn.execute("INSERT INTO peripheriques (client_id, appareil_id, categorie, marque, modele) "
             "VALUES (1, ?, 'Ecran', 'Dell', 'U2412')", (app_id,))
periph_id = conn.execute("SELECT id FROM peripheriques WHERE marque='Dell'").fetchone()[0]
conn.execute("INSERT INTO peripheriques_appareils (peripherique_id, appareil_id) VALUES (?,?)", (periph_id, app_id))
conn.execute("INSERT INTO contrats_appareils (contrat_id, appareil_id) VALUES (?,?)", (contrat_annexe_id, app_id))
conn.execute("INSERT INTO documents_appareils (appareil_id, client_id, nom, nom_fichier) "
             "VALUES (?, 1, 'Doc', ?)", (app_id, 'app%d_doc.pdf' % app_id))
conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (1, 1, ?)", (app_id,))
conn.execute("INSERT INTO maintenances (client_id, appareil_id, type_maintenance, date_planifiee) "
             "VALUES (1, ?, 'Préventive', ?)", (app_id, now))
conn.execute("INSERT INTO licences_appareils (appareil_id, client_id, editeur, produit) "
             "VALUES (?, 1, 'Microsoft', 'Office')", (app_id,))
conn.execute("INSERT INTO cles_recuperation (cle, appareil_id, client_id, volume) "
             "VALUES ('CLE-TEST-1', ?, 1, 'C:')", (app_id,))
conn.execute("INSERT INTO collectes (cle, appareil_id, client_id, horodatage) "
             "VALUES ('COL-TEST-1', ?, 1, ?)", (app_id, now))
conn.commit()
conn.close()

reponse = client.post('/appareil/%d/supprimer' % app_id, headers=HDR)
verifier(reponse.status_code in (200, 302), 'suppression appareil acceptée', str(reponse.status_code))

conn = A.get_db()
verifier(conn.execute("SELECT COUNT(*) FROM appareils WHERE id=?", (app_id,)).fetchone()[0] == 0,
          "l'appareil est bien supprimé")
verifier(conn.execute("SELECT appareil_id FROM peripheriques WHERE id=?", (periph_id,)).fetchone()[0] is None,
          "peripheriques.appareil_id -> NULL (SET NULL)")
verifier(conn.execute("SELECT COUNT(*) FROM peripheriques_appareils WHERE appareil_id=?", (app_id,)).fetchone()[0] == 0,
          "peripheriques_appareils nettoyée (CASCADE)")
verifier(conn.execute("SELECT COUNT(*) FROM contrats_appareils WHERE appareil_id=?", (app_id,)).fetchone()[0] == 0,
          "contrats_appareils nettoyée (CASCADE)")
verifier(conn.execute("SELECT COUNT(*) FROM documents_appareils WHERE appareil_id=?", (app_id,)).fetchone()[0] == 0,
          "documents_appareils nettoyée (CASCADE)")
verifier(conn.execute("SELECT appareil_id FROM baie_slots WHERE position=1 AND client_id=1").fetchone()[0] is None,
          "baie_slots.appareil_id -> NULL (SET NULL)")
verifier(conn.execute("SELECT COUNT(*) FROM maintenances WHERE appareil_id=?", (app_id,)).fetchone()[0] == 0,
          "maintenances nettoyée (SET NULL -> appareil_id absent du filtre)")
verifier(conn.execute("SELECT COUNT(*) FROM licences_appareils WHERE appareil_id=?", (app_id,)).fetchone()[0] == 0,
          "licences_appareils nettoyée (CASCADE)")
verifier(conn.execute("SELECT COUNT(*) FROM cles_recuperation WHERE appareil_id=?", (app_id,)).fetchone()[0] == 0,
          "cles_recuperation nettoyée manuellement (pas de FK déclarée)")
verifier(conn.execute("SELECT COUNT(*) FROM collectes WHERE appareil_id=?", (app_id,)).fetchone()[0] == 0,
          "collectes nettoyée manuellement (pas de FK déclarée)")
verifier(conn.execute(
    "SELECT COUNT(*) FROM historique WHERE entite='appareil' AND entite_id=? AND action='Suppression'",
    (app_id,)).fetchone()[0] == 1, "suppression journalisée dans l'historique")
conn.close()

print('\n=== 2. Point 3 — suppression contrat : nettoyage des tables liées ===')
conn = A.get_db()
conn.execute("INSERT INTO appareils (client_id, nom_machine, av_contrat_id, edr_contrat_id, rmm_contrat_id, date_creation, date_maj) "
             "VALUES (1, 'Poste-Lie-Contrat', 999, 999, 999, ?, ?)", (now, now))
app2_id = conn.execute("SELECT id FROM appareils WHERE nom_machine='Poste-Lie-Contrat'").fetchone()[0]
conn.execute("INSERT INTO contrats (id, client_id, titre, date_creation) VALUES (999, 1, 'Contrat Cible', ?)", (now,))
conn.execute("INSERT INTO contrats_appareils (contrat_id, appareil_id) VALUES (999, ?)", (app2_id,))
conn.execute("INSERT INTO peripheriques (client_id, categorie, marque, modele) VALUES (1, 'Ecran', 'HP', 'E24')")
periph2_id = conn.execute("SELECT id FROM peripheriques WHERE marque='HP'").fetchone()[0]
conn.execute("INSERT INTO contrats_peripheriques (contrat_id, peripherique_id) VALUES (999, ?)", (periph2_id,))
conn.execute("INSERT INTO documents_contrats (contrat_id, client_id, nom, nom_fichier) "
             "VALUES (999, 1, 'Doc contrat', 'ctr999_doc.pdf')")
conn.execute("INSERT INTO maintenances (client_id, contrat_id, type_maintenance, date_planifiee) "
             "VALUES (1, 999, 'Corrective', ?)", (now,))
conn.execute("INSERT INTO licences_appareils (appareil_id, client_id, editeur, produit, contrat_id) "
             "VALUES (?, 1, 'Adobe', 'Acrobat', 999)", (app2_id,))
conn.commit()
conn.close()

reponse = client.post('/contrat/999/supprimer', headers=HDR)
verifier(reponse.status_code in (200, 302), 'suppression contrat acceptée', str(reponse.status_code))

conn = A.get_db()
verifier(conn.execute("SELECT COUNT(*) FROM contrats WHERE id=999").fetchone()[0] == 0,
          "le contrat est bien supprimé")
verifier(conn.execute("SELECT COUNT(*) FROM contrats_appareils WHERE contrat_id=999").fetchone()[0] == 0,
          "contrats_appareils nettoyée (CASCADE)")
verifier(conn.execute("SELECT COUNT(*) FROM contrats_peripheriques WHERE contrat_id=999").fetchone()[0] == 0,
          "contrats_peripheriques nettoyée (CASCADE)")
verifier(conn.execute("SELECT COUNT(*) FROM documents_contrats WHERE contrat_id=999").fetchone()[0] == 0,
          "documents_contrats nettoyée (CASCADE)")
verifier(conn.execute("SELECT COUNT(*) FROM maintenances WHERE contrat_id=999").fetchone()[0] == 0,
          "maintenances.contrat_id nettoyée (SET NULL)")
row = conn.execute("SELECT contrat_id FROM licences_appareils WHERE appareil_id=?", (app2_id,)).fetchone()
verifier(row is not None and row[0] is None, "licences_appareils.contrat_id -> NULL (SET NULL)")
row = conn.execute("SELECT av_contrat_id, edr_contrat_id, rmm_contrat_id FROM appareils WHERE id=?", (app2_id,)).fetchone()
verifier(tuple(row) == (None, None, None),
          "av_contrat_id/edr_contrat_id/rmm_contrat_id nettoyés manuellement (pas de FK déclarée)",
          str(tuple(row)))
verifier(conn.execute(
    "SELECT COUNT(*) FROM historique WHERE entite='contrat' AND entite_id=999 AND action='Suppression'"
    ).fetchone()[0] == 1, "suppression journalisée dans l'historique")
conn.close()

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 3. Point 4 — collecteur : marque/modèle/n° série figés une fois posés ===')
cfg_set('collecteur_token', '')
conn = A.get_db()
conn.execute("INSERT INTO appareils (client_id, nom_machine, adresse_mac, marque, modele, numero_serie, adresse_ip, date_creation, date_maj) "
             "VALUES (1, 'Poste-Fige', 'AA:BB:CC:DD:EE:10', 'Dell', 'Latitude 5420', 'SN-ORIGINAL', '192.168.1.50', ?, ?)",
             (now, now))
conn.commit(); conn.close()

charge = {'mac_address': 'AA:BB:CC:DD:EE:10', 'client_id': 1, 'hostname': 'POSTE-FIGE',
          'ip_addresses': ['192.168.1.51'], 'brand': 'HP', 'model': 'EliteBook',
          'serial_number': 'SN-MAUVAIS-SCAN'}
reponse = client.post('/api/device-info', json=charge)
verifier(reponse.status_code == 200, 'collecte acceptée', str(reponse.status_code))
conn = A.get_db()
row = conn.execute("SELECT marque, modele, numero_serie, adresse_ip FROM appareils WHERE nom_machine='Poste-Fige'").fetchone()
conn.close()
verifier(row[0] == 'Dell', 'marque NON écrasée (déjà renseignée)', row[0])
verifier(row[1] == 'Latitude 5420', 'modèle NON écrasé (déjà renseigné)', row[1])
verifier(row[2] == 'SN-ORIGINAL', 'numéro de série NON écrasé (déjà renseigné)', row[2])
verifier(row[3] == '192.168.1.51', 'adresse IP TOUJOURS resynchronisée (champ variable)', row[3])

print('\n=== 4. Point 4 — collecteur : champs vides toujours remplissables ===')
conn = A.get_db()
conn.execute("INSERT INTO appareils (client_id, nom_machine, adresse_mac, date_creation, date_maj) "
             "VALUES (1, 'Poste-Vide', 'AA:BB:CC:DD:EE:11', ?, ?)", (now, now))
conn.commit(); conn.close()
charge2 = {'mac_address': 'AA:BB:CC:DD:EE:11', 'client_id': 1, 'hostname': 'POSTE-VIDE',
           'ip_addresses': ['192.168.1.52'], 'brand': 'Lenovo', 'model': 'ThinkPad',
           'serial_number': 'SN-PREMIERE-COLLECTE'}
reponse = client.post('/api/device-info', json=charge2)
verifier(reponse.status_code == 200, 'collecte acceptée', str(reponse.status_code))
conn = A.get_db()
row = conn.execute("SELECT marque, modele, numero_serie FROM appareils WHERE nom_machine='Poste-Vide'").fetchone()
conn.close()
verifier(tuple(row) == ('Lenovo', 'ThinkPad', 'SN-PREMIERE-COLLECTE'),
          'champs vides bien remplis à la première collecte', str(tuple(row)))

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 5. Point 2 — jeton collecteur par client (sans jeton global) ===')
cfg_set('collecteur_token', '')
conn = A.get_db()
conn.execute("UPDATE clients SET collecteur_token='' WHERE id IN (1,2)")
conn.commit(); conn.close()

charge_c1 = {'mac_address': 'AA:BB:CC:DD:EE:20', 'client_id': 1, 'hostname': 'POSTE-C1',
             'ip_addresses': ['192.168.1.60']}
charge_c2 = {'mac_address': 'AA:BB:CC:DD:EE:21', 'client_id': 2, 'hostname': 'POSTE-C2',
             'ip_addresses': ['192.168.2.60']}
verifier(client.post('/api/device-info', json=charge_c1).status_code == 200,
          'sans aucun jeton configuré, client 1 accessible (comportement historique préservé)')
verifier(client.post('/api/device-info', json=charge_c2).status_code == 200,
          'sans aucun jeton configuré, client 2 accessible aussi')

conn = A.get_db()
conn.execute("UPDATE clients SET collecteur_token='secret-client-1' WHERE id=1")
conn.commit(); conn.close()

verifier(client.post('/api/device-info', json=charge_c1).status_code == 401,
          "client 1 protégé par son propre jeton : refusé sans jeton")
reponse = client.post('/api/device-info', json=charge_c1, headers={'X-Collector-Token': 'mauvais'})
verifier(reponse.status_code == 401, 'refusé avec un mauvais jeton dédié', str(reponse.status_code))
reponse = client.post('/api/device-info', json=charge_c1, headers={'X-Collector-Token': 'secret-client-1'})
verifier(reponse.status_code == 200, 'accepté avec le bon jeton dédié', str(reponse.status_code))
verifier(client.post('/api/device-info', json=charge_c2).status_code == 200,
          "client 2 (sans jeton dédié) reste accessible sans jeton — l'opt-in de l'un n'affecte pas l'autre")

print('\n=== 6. Point 2 — le jeton global reste un passe-partout (rétrocompatibilité) ===')
cfg_set('collecteur_token', 'jeton-global-maitre')
verifier(client.post('/api/device-info', json=charge_c1).status_code == 401,
          'sans rien : refusé (jeton global obligatoire dès qu\'il est configuré)')
reponse = client.post('/api/device-info', json=charge_c1, headers={'X-Collector-Token': 'jeton-global-maitre'})
verifier(reponse.status_code == 200, 'jeton global valide -> accepté même sur un client à jeton dédié',
         str(reponse.status_code))
reponse = client.post('/api/device-info', json=charge_c2, headers={'X-Collector-Token': 'jeton-global-maitre'})
verifier(reponse.status_code == 200, 'jeton global valide -> accepté sur un client sans jeton dédié',
         str(reponse.status_code))
cfg_set('collecteur_token', '')
conn = A.get_db()
conn.execute("UPDATE clients SET collecteur_token='' WHERE id IN (1,2)")
conn.commit(); conn.close()

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 7. Point 5 — audit trail : import scan réseau ===')
conn = A.get_db()
avant = conn.execute("SELECT COUNT(*) FROM historique WHERE client_id=1").fetchone()[0]
conn.close()
reponse = client.post('/api/scan/importer', json={'appareils': [
    {'ip': '192.168.1.70', 'hostname': 'SCAN-NOUVEAU', 'mac': 'AA:BB:CC:DD:EE:30'}]})
verifier(reponse.status_code == 200, 'import scan accepté', str(reponse.status_code))
conn = A.get_db()
verifier(conn.execute(
    "SELECT COUNT(*) FROM historique WHERE client_id=1 AND entite='appareil' AND action='Création (scan réseau)'"
    ).fetchone()[0] == 1, 'création via scan journalisée')
conn.close()
reponse = client.post('/api/scan/importer', json={'appareils': [
    {'ip': '192.168.1.70', 'hostname': 'SCAN-NOUVEAU', 'mac': 'AA:BB:CC:DD:EE:30'}]})
verifier(reponse.status_code == 200, 'ré-import (mise à jour) accepté', str(reponse.status_code))
conn = A.get_db()
verifier(conn.execute(
    "SELECT COUNT(*) FROM historique WHERE client_id=1 AND entite='appareil' AND action='Auto-remplissage (scan réseau)'"
    ).fetchone()[0] == 1, 'mise à jour via scan journalisée')
conn.close()

print('\n=== 8. Point 5 — audit trail : import CSV appareils ===')
csv_appareils = ('nom_machine;type_appareil;marque;modele;numero_serie\n'
                  'CSV-Nouveau;PC;Acer;Aspire;SN-CSV-1\n').encode('utf-8-sig')
reponse = client.post('/appareils/import', headers=HDR,
                      data={'fichier': (io.BytesIO(csv_appareils), 'appareils.csv')},
                      content_type='multipart/form-data')
verifier(reponse.status_code in (200, 302), 'import CSV appareils accepté', str(reponse.status_code))
conn = A.get_db()
verifier(conn.execute(
    "SELECT COUNT(*) FROM historique WHERE client_id=1 AND entite='appareil' AND action='Création (import CSV)'"
    ).fetchone()[0] == 1, 'création via CSV journalisée')
conn.close()
reponse = client.post('/appareils/import', headers=HDR,
                      data={'fichier': (io.BytesIO(csv_appareils), 'appareils.csv')},
                      content_type='multipart/form-data')
verifier(reponse.status_code in (200, 302), 'ré-import CSV (mise à jour) accepté', str(reponse.status_code))
conn = A.get_db()
verifier(conn.execute(
    "SELECT COUNT(*) FROM historique WHERE client_id=1 AND entite='appareil' AND action='Mise à jour (import CSV)'"
    ).fetchone()[0] == 1, 'mise à jour via CSV journalisée')
conn.close()

print('\n=== 9. Point 5 — audit trail : import CSV périphériques ===')
csv_periph = 'categorie;marque;modele;numero_serie\nEcran;Samsung;S27;SN-CSV-2\n'.encode('utf-8-sig')
reponse = client.post('/peripheriques/import', headers=HDR,
                      data={'fichier': (io.BytesIO(csv_periph), 'periph.csv')},
                      content_type='multipart/form-data')
verifier(reponse.status_code in (200, 302), 'import CSV périphériques accepté', str(reponse.status_code))
conn = A.get_db()
verifier(conn.execute(
    "SELECT COUNT(*) FROM historique WHERE client_id=1 AND entite='peripherique' AND action='Création (import CSV)'"
    ).fetchone()[0] == 1, 'création périphérique via CSV journalisée')
conn.close()

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 10. Point 1 — /api/scan/client-suggere : suggestion réseau ===')
conn = A.get_db()
conn.execute("INSERT OR IGNORE INTO parc_general (client_id, nom_site) VALUES (1, 'Site 1')")
conn.execute("INSERT OR IGNORE INTO parc_general (client_id, nom_site) VALUES (2, 'Site 2')")
conn.execute("UPDATE parc_general SET plage_ip_locale='' WHERE client_id IN (1,2)")
conn.commit(); conn.close()

_reseaux_original = A._reseaux_locaux_actuels
A._reseaux_locaux_actuels = lambda: {ipaddress.ip_network('192.168.9.0/24')}

reponse = client.get('/api/scan/client-suggere')
d = reponse.get_json()
verifier(reponse.status_code == 200, 'endpoint accessible', str(reponse.status_code))
verifier(d.get('suggestion') is None, 'aucune plage configurée -> aucune suggestion (rien pré-coché)')
ids_listes = {c['id'] for c in d.get('clients', [])}
verifier({1, 2}.issubset(ids_listes), 'les deux clients de test sont listés parmi les accessibles', str(ids_listes))

conn = A.get_db()
conn.execute("UPDATE parc_general SET plage_ip_locale='192.168.9.0/24' WHERE client_id=1")
conn.commit(); conn.close()
reponse = client.get('/api/scan/client-suggere')
d = reponse.get_json()
verifier(d.get('suggestion') == 1, 'un seul client correspond sans ambiguïté -> pré-sélectionné', str(d))

conn = A.get_db()
conn.execute("UPDATE parc_general SET plage_ip_locale='192.168.9.0/24' WHERE client_id=2")
conn.commit(); conn.close()
reponse = client.get('/api/scan/client-suggere')
d = reponse.get_json()
verifier(d.get('suggestion') is None,
          'deux clients correspondent (ambiguïté) -> aucune suggestion, rien pré-coché', str(d))

conn = A.get_db()
conn.execute("UPDATE parc_general SET plage_ip_locale='' WHERE client_id IN (1,2)")
conn.commit(); conn.close()
A._reseaux_locaux_actuels = _reseaux_original

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
