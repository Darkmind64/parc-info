#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie la fin de la double représentation appareil<->périphérique
(audit architecture 2026-08-20, point orange "doublon de représentation") :

  - peripheriques.appareil_id (colonne historique) n'est plus écrite nulle
    part : seule la table pivot peripheriques_appareils (N:N) fait foi,
    déjà utilisée exclusivement par tous les points de lecture existants.
  - Le dédoublonnage USB (VID:PID) du collecteur, qui s'appuyait sur cette
    colonne pour retrouver un périphérique déjà lié à CET appareil, a été
    reporté sur la table pivot pour continuer à fonctionner.
  - _ENTITE_COLS['peripherique'] ne contient plus 'appareil_id' : l'historique
    ne journalise plus de faux écarts sur ce champ (le formulaire poste
    'appareil_ids' au pluriel, jamais 'appareil_id').
  - Suppression d'un périphérique : les tables liées (pivot, contrats,
    documents, maintenances, interventions) sont désormais nettoyées au lieu
    de laisser des lignes orphelines (même pattern que les appareils/contrats).

Usage :
    python test_lien_appareil_peripherique.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='lien_app_periph_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A   # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


A.init_db()
conn = A.get_db()
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client Un')")
now = '2026-08-20T00:00:00'
conn.execute("INSERT INTO appareils (client_id, nom_machine, date_creation, date_maj) "
             "VALUES (1, 'Poste-USB', ?, ?)", (now, now))
app_id = conn.execute("SELECT id FROM appareils WHERE nom_machine='Poste-USB'").fetchone()[0]
conn.commit()
conn.close()

# ═══════════════════════════════════════════════════════════════════════════
print('=== 1. Collecteur : périphérique USB créé, appareil_id (legacy) jamais écrit ===')
conn = A.get_db()
usb_device = {'inventory_name': 'Souris Logitech', 'manufacturer': 'Logitech',
              'vid': '046D', 'pid': 'C077', 'serial': ''}
created = A._sync_collector_peripherals(conn, 1, app_id, [], [], [usb_device])
conn.commit()
verifier(created == 1, "1 périphérique USB créé", str(created))
row = conn.execute(
    "SELECT id, appareil_id FROM peripheriques WHERE client_id=1 AND marque='Logitech'").fetchone()
verifier(row is not None, "le périphérique existe bien en base")
verifier(row[1] is None, "peripheriques.appareil_id (legacy) reste NULL — plus jamais écrit", str(row[1]))
pid = row[0]
lien = conn.execute(
    "SELECT COUNT(*) FROM peripheriques_appareils WHERE peripherique_id=? AND appareil_id=?",
    (pid, app_id)).fetchone()[0]
verifier(lien == 1, "le lien vit bien dans la table pivot peripheriques_appareils")
conn.close()

print('\n=== 2. Collecteur : la même souris rebranchée -> pas de doublon (dédoublonnage via pivot) ===')
conn = A.get_db()
created2 = A._sync_collector_peripherals(conn, 1, app_id, [], [], [usb_device])
conn.commit()
nb = conn.execute("SELECT COUNT(*) FROM peripheriques WHERE client_id=1 AND marque='Logitech'").fetchone()[0]
verifier(created2 == 0, "rien créé une seconde fois (retrouvé via la table pivot)", str(created2))
verifier(nb == 1, "une seule fiche existe toujours en base", str(nb))
conn.close()

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 3. Historique : plus de faux écart sur appareil_id à la modification ===')
verifier('appareil_id' not in A._ENTITE_COLS['peripherique'],
          "_ENTITE_COLS['peripherique'] ne contient plus 'appareil_id'")
conn = A.get_db()
conn.execute("INSERT INTO peripheriques (client_id, categorie, marque, modele, date_creation, date_maj) "
             "VALUES (1, 'Ecran', 'Dell', 'U2412', ?, ?)", (now, now))
periph_edit_id = conn.execute("SELECT id FROM peripheriques WHERE marque='Dell'").fetchone()[0]
conn.commit(); conn.close()

conn = A.get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (1, 'admin', 'x', 'Administrateur', 'admin', 1)")
conn.commit(); conn.close()

client = A.app.test_client()
with client.session_transaction() as s:
    s['auth_user_id'] = 1
    s['client_id'] = 1
    s['csrf_token'] = 'jeton-test-csrf'

r = client.post('/peripherique/%d/editer' % periph_edit_id,
                 data={'categorie': 'Ecran', 'marque': 'Dell', 'modele': 'U2413',
                       'numero_serie': '', 'description': '', 'localisation': '',
                       'statut': 'actif', 'date_achat': '', 'duree_garantie': '0',
                       'date_fin_garantie': '', 'fournisseur': '', 'prix_achat': '',
                       'numero_commande': '', 'notes': ''},
                 headers={'X-CSRF-Token': 'jeton-test-csrf'})
verifier(r.status_code in (200, 302), 'modification périphérique acceptée', str(r.status_code))
conn = A.get_db()
last = conn.execute(
    "SELECT details FROM historique WHERE entite='peripherique' AND entite_id=? "
    "AND action='Modification' ORDER BY id DESC LIMIT 1", (periph_edit_id,)).fetchone()
conn.close()
detail_str = last[0] if last else ''
verifier('appareil_id' not in (detail_str or ''),
          "aucun écart 'appareil_id' dans le détail de l'historique", detail_str)

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 4. Suppression périphérique : nettoyage des tables liées ===')
conn = A.get_db()
conn.execute("INSERT INTO peripheriques (client_id, categorie, marque, modele, date_creation, date_maj) "
             "VALUES (1, 'Ecran', 'HP', 'E24', ?, ?)", (now, now))
periph_del_id = conn.execute("SELECT id FROM peripheriques WHERE marque='HP'").fetchone()[0]
conn.execute("INSERT INTO peripheriques_appareils (peripherique_id, appareil_id) VALUES (?,?)",
             (periph_del_id, app_id))
conn.execute("INSERT INTO contrats (client_id, titre, date_creation) VALUES (1, 'Contrat Ecran', ?)", (now,))
contrat_id = conn.execute("SELECT id FROM contrats WHERE titre='Contrat Ecran'").fetchone()[0]
conn.execute("INSERT INTO contrats_peripheriques (contrat_id, peripherique_id) VALUES (?,?)",
             (contrat_id, periph_del_id))
conn.execute("INSERT INTO documents_peripheriques (peripherique_id, client_id, nom, nom_fichier) "
             "VALUES (?, 1, 'Doc', 'periph_doc.pdf')", (periph_del_id,))
conn.execute("INSERT INTO maintenances (client_id, peripherique_id, type_maintenance, date_planifiee) "
             "VALUES (1, ?, 'Préventive', ?)", (periph_del_id, now))
conn.commit(); conn.close()

r = client.post('/peripherique/%d/supprimer' % periph_del_id,
                 headers={'X-CSRF-Token': 'jeton-test-csrf'})
verifier(r.status_code in (200, 302), 'suppression périphérique acceptée', str(r.status_code))

conn = A.get_db()
verifier(conn.execute("SELECT COUNT(*) FROM peripheriques WHERE id=?", (periph_del_id,)).fetchone()[0] == 0,
          "le périphérique est bien supprimé")
verifier(conn.execute("SELECT COUNT(*) FROM peripheriques_appareils WHERE peripherique_id=?",
                       (periph_del_id,)).fetchone()[0] == 0, "peripheriques_appareils nettoyée (CASCADE)")
verifier(conn.execute("SELECT COUNT(*) FROM contrats_peripheriques WHERE peripherique_id=?",
                       (periph_del_id,)).fetchone()[0] == 0, "contrats_peripheriques nettoyée (CASCADE)")
verifier(conn.execute("SELECT COUNT(*) FROM documents_peripheriques WHERE peripherique_id=?",
                       (periph_del_id,)).fetchone()[0] == 0, "documents_peripheriques nettoyée (CASCADE)")
verifier(conn.execute("SELECT COUNT(*) FROM maintenances WHERE peripherique_id=?",
                       (periph_del_id,)).fetchone()[0] == 0, "maintenances.peripherique_id nettoyée (SET NULL)")
conn.close()

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
