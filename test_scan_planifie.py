#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scan réseau récurrent planifié (Plan 3) — bout en bout.

Vérifie, avec le moteur de scan (`_run_scan`) remplacé par un faux qui
renvoie des résultats figés :
  - `_executer_scan_planifie` importe les appareils, capture un instantané
    d'origine « scan_auto », et n'alerte pas quand rien de notable ne change ;
  - au run suivant, un appareil nouveau (MAC constructeur) déclenche une
    alerte journalisée dans `historique` (action SCAN_AUTO_CHANGEMENTS) ;
  - un « nouveau » à MAC aléatoire (smartphone) ne déclenche PAS d'alerte ;
  - `etat_scan_planifie` ne renvoie « peut_ecrire » que pour un accès en
    écriture.

Usage :
    python test_scan_planifie.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='scan_planifie_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A                       # noqa: E402
from config_helpers import cfg_set    # noqa: E402

A.init_db()
cfg_set('mode_terrain', 'auto')
cfg_set('scan_auto_inclure_candidats', '0')   # pas de sonde réseau réelle
cfg_set('scan_auto_seuil_disparus', '3')

echecs = []


def verifier(cond, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if cond else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not cond:
        echecs.append(libelle)


# Faux moteur de scan : remplit le scan_status global avec des résultats figés.
_PROCHAIN = {'resultats': []}


def _faux_run_scan(plages, nb_threads, enrich_wmi=False, client_id=None):
    with A.scan_lock:
        A.scan_status = {'running': False, 'progress': 100, 'message': 'ok',
                         'results': list(_PROCHAIN['resultats']), 'errors': []}


A._run_scan = _faux_run_scan


def _host(ip, mac, nom, ports=(80,), typ='PC'):
    return {'ip': ip, 'mac': mac, 'netbios': nom, 'hostname': nom,
            'ports': list(ports), 'type': typ, 'vendor': ''}


conn = A.get_db()
cur = conn.execute("INSERT INTO clients (nom, date_creation) VALUES (?,?)",
                   ('ACME planifié', '2026-01-01T00:00:00'))
cid = cur.lastrowid
conn.execute("INSERT INTO parc_general (client_id, plage_ip_locale) VALUES (?,?)",
             (cid, '192.168.77.0/24'))
conn.commit()

# ═══════════════════════════════════════════════════════════════════════════
print('=== 1. Premier run : import + instantané origine scan_auto ===')
_PROCHAIN['resultats'] = [
    _host('192.168.77.10', '00:1b:44:00:00:01', 'PC-A'),
    _host('192.168.77.20', '00:1b:44:00:00:02', 'NAS-1', typ='NAS'),
]
r1 = A._executer_scan_planifie(cid, ['192.168.77.0/24'], 'Scan planifié (quotidien)',
                               declencheur='manuel')
verifier(r1['importes'] == 2, "2 appareils importés", str(r1))
verifier(r1['erreur'] is None, "aucune erreur", str(r1['erreur']))
row = conn.execute("SELECT origine, libelle FROM client_instantane "
                   "WHERE client_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
verifier(row and row[0] == 'scan_auto', "instantané d'origine scan_auto", str(row))
verifier(r1['alerte'] is False, "pas d'alerte au premier run (rien à comparer)")

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 2. Run suivant : un nouvel appareil -> alerte journalisée ===')
_PROCHAIN['resultats'] = [
    _host('192.168.77.10', '00:1b:44:00:00:01', 'PC-A'),
    _host('192.168.77.20', '00:1b:44:00:00:02', 'NAS-1', typ='NAS'),
    _host('192.168.77.30', '00:1b:44:00:00:03', 'PC-INTRUS'),
]
r2 = A._executer_scan_planifie(cid, ['192.168.77.0/24'], 'Scan planifié (quotidien)',
                               declencheur='manuel')
verifier(r2['alerte'] is True, "alerte levée (1 appareil nouveau)", str(r2))
n = conn.execute("SELECT COUNT(*) FROM historique WHERE client_id=? AND action=?",
                 (cid, 'SCAN_AUTO_CHANGEMENTS')).fetchone()[0]
verifier(n == 1, "une entrée SCAN_AUTO_CHANGEMENTS dans l'historique", 'n=%d' % n)

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 3. Nouveau à MAC aléatoire (smartphone) -> pas d\'alerte ===')
_PROCHAIN['resultats'] = [
    _host('192.168.77.10', '00:1b:44:00:00:01', 'PC-A'),
    _host('192.168.77.20', '00:1b:44:00:00:02', 'NAS-1', typ='NAS'),
    _host('192.168.77.30', '00:1b:44:00:00:03', 'PC-INTRUS'),
    _host('192.168.77.44', 'de:ad:be:ef:00:44', 'phone'),   # bit 0x02 -> localement administrée
]
r3 = A._executer_scan_planifie(cid, ['192.168.77.0/24'], 'Scan planifié (quotidien)',
                               declencheur='manuel')
verifier(r3['alerte'] is False, "MAC aléatoire minorée : aucune alerte", str(r3))
n = conn.execute("SELECT COUNT(*) FROM historique WHERE client_id=? AND action=?",
                 (cid, 'SCAN_AUTO_CHANGEMENTS')).fetchone()[0]
verifier(n == 1, "toujours une seule entrée SCAN_AUTO_CHANGEMENTS", 'n=%d' % n)

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 4. etat_scan_planifie : peut_ecrire suit le niveau d\'accès ===')
import scan_planifie as SP
cfg_set('scan_auto:%d' % cid, 'hebdo')
etat = SP.etat_scan_planifie(conn, [
    {'id': cid, 'nom': 'ACME', 'acces': 'lecture'},
])
lg = etat['clients'][0]
verifier(lg['planifie'] is True and lg['cadence'] == 'hebdo', "cadence lue", str(lg))
verifier(lg['peut_ecrire'] is False, "accès 'lecture' -> peut_ecrire False")

conn.close()
print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
