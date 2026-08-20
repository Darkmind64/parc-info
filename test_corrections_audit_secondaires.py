#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie les 3 corrections « moins urgentes » du même audit architecture
(2026-08-20) que test_corrections_audit_architecture.py (les 5 points rouges) :

  1. Suppression d'entrées d'historique : un utilisateur en lecture seule ne
     peut plus supprimer une entrée individuelle ni vider les erreurs — ces
     deux routes ne vérifiaient que @login_required, jamais can_write().
  2. Widget « Alertes critiques » du dashboard : respecte désormais le délai
     configuré (garantie_alerte_jours), au lieu d'un seuil de 30 jours en
     dur, et n'affiche plus un appareil dont l'alerte garantie a été
     explicitement mise en sourdine (garantie_alerte_ignoree).
  3. Notifications de maintenance : une maintenance déjà notifiée (n'importe
     quel jour passé) n'est plus renotifiée un jour après l'autre pendant
     toute sa fenêtre de préavis de 3 jours.

Usage :
    python test_corrections_audit_secondaires.py
"""

import io
import os
import sys
import tempfile
from datetime import date, timedelta, datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='audit_arch_2_')
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
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (2, 'lecteur', 'x', 'Lecteur Seul', 'user', 1)")
conn.execute("INSERT OR IGNORE INTO clients (id, nom, auth_user_id) VALUES (1, 'Client Un', 1)")
# Le lecteur a un accès en LECTURE seule à ce client (partagé, pas propriétaire) :
# get_client_id() ne retient un client en session que si l'utilisateur y a
# effectivement accès (proprietaire OU partagé) — sans ce partage, un simple
# 'user' sans aucun client n'aurait jamais de client_id actif du tout.
conn.execute("INSERT OR IGNORE INTO client_partages (client_id, auth_user_id, niveau, date_partage) "
             "VALUES (1, 2, 'lecture', ?)", (datetime.utcnow().isoformat(),))
conn.commit()
conn.close()

client = A.app.test_client()
CSRF = 'jeton-test-csrf'
HDR = {'X-CSRF-Token': CSRF}


def connecter(uid, cid):
    with client.session_transaction() as s:
        s['auth_user_id'] = uid
        s['client_id'] = cid
        s['csrf_token'] = CSRF


# ═══════════════════════════════════════════════════════════════════════════
print('=== 1. Suppression historique : ACL manquante (can_write) ===')
conn = A.get_db()
now = '2026-08-20T00:00:00'
conn.execute("INSERT INTO historique (client_id, entite, entite_id, entite_nom, action, date_action) "
             "VALUES (1, 'appareil', 1, 'Poste Test', 'Erreur', ?)", (now,))
hist_id = conn.execute("SELECT id FROM historique WHERE entite_nom='Poste Test'").fetchone()[0]
conn.commit(); conn.close()

connecter(2, 1)  # lecteur : pas propriétaire, pas de partage -> can_write() False
r = client.post('/historique/%d/supprimer' % hist_id, headers=HDR)
verifier(r.status_code == 403, "lecture seule : suppression d'une entrée refusée (403)", str(r.status_code))
r = client.post('/historique/vider-erreurs', headers=HDR)
verifier(r.status_code == 403, "lecture seule : vider-erreurs refusé (403)", str(r.status_code))
conn = A.get_db()
verifier(conn.execute("SELECT COUNT(*) FROM historique WHERE id=?", (hist_id,)).fetchone()[0] == 1,
          "l'entrée survit au refus (rien supprimé)")
conn.close()

connecter(1, 1)  # admin -> proprietaire -> can_write() True
r = client.post('/historique/%d/supprimer' % hist_id, headers=HDR)
verifier(r.status_code == 200, "propriétaire : suppression autorisée", str(r.status_code))
conn = A.get_db()
verifier(conn.execute("SELECT COUNT(*) FROM historique WHERE id=?", (hist_id,)).fetchone()[0] == 0,
          "l'entrée est bien supprimée")
conn.close()

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 2. Widget « Alertes critiques » : délai configuré + drapeau ignoré ===')
today = date.today()
cfg_set('garantie_alerte_jours', '10')
conn = A.get_db()
# Expire dans 20 jours : hors de l'ancien seuil fixe (30j aurait pu l'inclure
# par coïncidence) -> on choisit un cas qui distingue clairement 10 vs 30.
loin = (today + timedelta(days=20)).isoformat()
proche = (today + timedelta(days=5)).isoformat()
conn.execute("INSERT INTO appareils (client_id, nom_machine, date_fin_garantie, date_creation, date_maj) "
             "VALUES (1, 'PC-Garantie-Loin', ?, ?, ?)", (loin, now, now))
conn.execute("INSERT INTO appareils (client_id, nom_machine, date_fin_garantie, date_creation, date_maj) "
             "VALUES (1, 'PC-Garantie-Proche', ?, ?, ?)", (proche, now, now))
conn.execute("INSERT INTO appareils (client_id, nom_machine, date_fin_garantie, garantie_alerte_ignoree, date_creation, date_maj) "
             "VALUES (1, 'PC-Garantie-Ignoree', ?, 1, ?, ?)", (proche, now, now))
conn.commit()
result = A._compute_critical_alerts(conn, 1, today)
conn.close()
devices_in_alert = {a['device'] for a in result['alerts'] if a.get('type', '').startswith('warranty')}
verifier('PC-Garantie-Proche' in devices_in_alert,
          "appareil dans le délai configuré (10j) -> présent dans les alertes")
verifier('PC-Garantie-Loin' not in devices_in_alert,
          "appareil hors du délai configuré (20j > 10j) -> absent des alertes")
verifier('PC-Garantie-Ignoree' not in devices_in_alert,
          "appareil avec garantie_alerte_ignoree=1 -> jamais dans les alertes critiques")
cfg_set('garantie_alerte_jours', '90')

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 3. Notifications de maintenance : plus de renvoi en boucle ===')
conn = A.get_db()
hier = (today - timedelta(days=1)).isoformat()
demain = (today + timedelta(days=1)).isoformat()
conn.execute(
    "INSERT INTO maintenances (client_id, type_maintenance, date_planifiee, responsable, statut) "
    "VALUES (1, 'Préventive', ?, 'tech@test.local', 'programmee')", (demain,))
maint_id = conn.execute(
    "SELECT id FROM maintenances WHERE responsable='tech@test.local'").fetchone()[0]
# Simule : une notification a déjà été envoyée HIER pour cette même
# maintenance (toujours dans sa fenêtre de préavis de 3 jours aujourd'hui).
conn.execute(
    "INSERT INTO maintenance_notifications (maintenance_id, notification_date) VALUES (?, ?)",
    (maint_id, hier))
conn.commit(); conn.close()

emails_envoyes = []
_send_email_original = A._send_email
A._send_email = lambda to, subject, body: emails_envoyes.append(to) or True
A._notify_upcoming_maintenances()
A._send_email = _send_email_original

verifier(emails_envoyes == [],
          "déjà notifiée hier -> aucun nouvel email aujourd'hui (fin du renvoi en boucle)",
          str(emails_envoyes))
conn = A.get_db()
verifier(conn.execute(
    "SELECT COUNT(*) FROM maintenance_notifications WHERE maintenance_id=?", (maint_id,)
    ).fetchone()[0] == 1, "toujours une seule ligne de notification enregistrée pour cette maintenance")
conn.close()

# Cas nominal : une maintenance JAMAIS notifiée dans la fenêtre reçoit bien
# son unique email.
conn = A.get_db()
conn.execute(
    "INSERT INTO maintenances (client_id, type_maintenance, date_planifiee, responsable, statut) "
    "VALUES (1, 'Corrective', ?, 'autre@test.local', 'programmee')", (demain,))
maint_id2 = conn.execute(
    "SELECT id FROM maintenances WHERE responsable='autre@test.local'").fetchone()[0]
conn.commit(); conn.close()

emails_envoyes2 = []
A._send_email = lambda to, subject, body: emails_envoyes2.append(to) or True
A._notify_upcoming_maintenances()
A._send_email = _send_email_original
verifier(emails_envoyes2 == ['autre@test.local'],
          "maintenance jamais notifiée -> reçoit bien son email", str(emails_envoyes2))

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
