#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit baie de brassage, lot 3 (#17) : plusieurs adresses MAC par appareil.

Ce que le test contrôle :
  - _norm_mac_pi : normalise les formes courantes (tirets, compact, casse)
  - _parse_macs_form : textarea « MAC + libellé » -> liste de couples
  - création/édition d'un appareil avec le champ « MAC supplémentaires »
    (table appareil_macs, source 'manuel', vidage quand on efface le champ)
  - la MAC principale saisie dans le même champ est ignorée (pas de doublon)
  - /api/device-info : les cartes réseau PHYSIQUES du collecteur deviennent
    des MAC secondaires ; les interfaces virtuelles (Hyper-V/WSL) sont exclues
  - /api/scan/importer : une MAC vue au scan, différente de la principale
    d'un appareil retrouvé par IP, est ajoutée en secondaire
  - la corrélation de brassage reconnaît l'appareil sur n'importe laquelle
    de ses MAC (network_diag._macs_secondaires)

Usage :
    python test_appareil_macs_multiples.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='macs_multi_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A            # noqa: E402
import network_diag as N   # noqa: E402

echecs = []


def verifier(cond, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if cond else 'ÉCHEC', libelle, (' — ' + detail) if detail else ''))
    if not cond:
        echecs.append(libelle)


A.init_db()
conn = A.get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (1, 'admin', 'x', 'Administrateur', 'admin', 1)")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client MAC')")
conn.commit()
conn.close()

client = A.app.test_client()
csrf = 'test-csrf-token'
with client.session_transaction() as s:
    s['auth_user_id'] = 1
    s['client_id'] = 1
    s['csrf_token'] = csrf


print('=== 1. _norm_mac_pi ===')
for brut, attendu in [
        ('AA-BB-CC-DD-EE-FF', 'aa:bb:cc:dd:ee:ff'),
        ('aabbccddeeff', 'aa:bb:cc:dd:ee:ff'),
        ('AA:BB:CC:DD:EE:FF', 'aa:bb:cc:dd:ee:ff'),
        ('  a1:b2:c3:d4:e5:f6 ', 'a1:b2:c3:d4:e5:f6'),
        ('pas une mac', ''),
        ('aa:bb:cc', '')]:
    got = A._norm_mac_pi(brut)
    verifier(got == attendu, f'_norm_mac_pi({brut!r})', repr(got))

print('\n=== 2. _parse_macs_form ===')
parsed = A._parse_macs_form("AA:BB:CC:00:00:01 WAN\nAA-BB-CC-00-00-02\n\nnimportequoi\nAA:BB:CC:00:00:03  carte admin")
verifier(parsed == [('aa:bb:cc:00:00:01', 'WAN'), ('aa:bb:cc:00:00:02', ''),
                    ('aa:bb:cc:00:00:03', 'carte admin')],
         'parsing textarea (libellé optionnel, lignes invalides ignorées)', str(parsed))

print('\n=== 3. Création d\'un appareil avec MAC supplémentaires ===')
r = client.post('/appareil/nouveau', data={
    'csrf_token': csrf, 'nom_machine': 'SRV-BI-NIC', 'adresse_mac': 'AA:00:00:00:00:01',
    'macs_supplementaires': 'BB:00:00:00:00:02 LAN\nAA:00:00:00:00:01 (doublon principale)\n'
                            'CC:00:00:00:00:03 iDRAC',
}, follow_redirects=True)
verifier(r.status_code == 200, 'formulaire accepté', str(r.status_code))
conn = A.get_db()
aid = conn.execute("SELECT id FROM appareils WHERE nom_machine='SRV-BI-NIC'").fetchone()[0]
rows = conn.execute("SELECT adresse_mac, libelle, source FROM appareil_macs WHERE appareil_id=? ORDER BY adresse_mac", (aid,)).fetchall()
conn.close()
verifier([x[0] for x in rows] == ['bb:00:00:00:00:02', 'cc:00:00:00:00:03'],
         'les 2 MAC secondaires enregistrées, la principale ignorée', str([x[0] for x in rows]))
verifier(all(x[2] == 'manuel' for x in rows), 'source = manuel')
verifier(dict((x[0], x[1]) for x in rows)['bb:00:00:00:00:02'] == 'LAN', 'libellé conservé')

print('\n=== 4. Édition : mise à jour et vidage du champ ===')
r = client.post(f'/appareil/{aid}/editer', data={
    'csrf_token': csrf, 'nom_machine': 'SRV-BI-NIC', 'adresse_mac': 'AA:00:00:00:00:01',
    'macs_supplementaires': 'BB:00:00:00:00:02 LAN renommé',
}, follow_redirects=True)
conn = A.get_db()
rows = [tuple(x) for x in conn.execute(
    "SELECT adresse_mac, libelle FROM appareil_macs WHERE appareil_id=?", (aid,)).fetchall()]
conn.close()
verifier(rows == [('bb:00:00:00:00:02', 'LAN renommé')],
         'iDRAC retirée, libellé de LAN mis à jour', str(rows))

r = client.post(f'/appareil/{aid}/editer', data={
    'csrf_token': csrf, 'nom_machine': 'SRV-BI-NIC', 'adresse_mac': 'AA:00:00:00:00:01',
    'macs_supplementaires': '',
}, follow_redirects=True)
conn = A.get_db()
n = conn.execute("SELECT COUNT(*) FROM appareil_macs WHERE appareil_id=? AND source='manuel'", (aid,)).fetchone()[0]
conn.close()
verifier(n == 0, 'champ vidé -> plus aucune MAC manuelle')

print('\n=== 5. /api/device-info : cartes physiques -> MAC secondaires ===')
charge = {
    'mac_address': 'AA:00:00:00:00:01', 'hostname': 'SRV-BI-NIC', 'client_id': 1,
    'ip_addresses': ['192.168.1.10'],
    'system_report': {'network_adapter_details': [
        {'name': 'Ethernet', 'mac_address': 'AA:00:00:00:00:01', 'physical': True},
        {'name': 'Ethernet 2', 'mac_address': 'DD:00:00:00:00:04', 'physical': True},
        {'name': 'vEthernet (WSL)', 'mac_address': 'EE:00:00:00:00:99', 'physical': False},
    ]},
}
rep = client.post('/api/device-info', json=charge)
verifier(rep.status_code == 200, 'collecte acceptée', str(rep.status_code))
conn = A.get_db()
rows = conn.execute("SELECT adresse_mac, source FROM appareil_macs WHERE appareil_id=?", (aid,)).fetchall()
conn.close()
macs = {x[0] for x in rows}
verifier(macs == {'dd:00:00:00:00:04'},
         'seule la 2e carte physique ajoutée (principale + virtuelle exclues)', str(macs))
verifier(rows and rows[0][1] == 'collecteur', 'source = collecteur')

print('\n=== 6. /api/scan/importer : MAC vue au scan != principale ===')
rep = client.post('/api/scan/importer', json={'appareils': [
    {'ip': '192.168.1.10', 'mac': 'ff:00:00:00:00:05', 'ports': [22]},
]})
verifier(rep.status_code == 200, 'import scan accepté', str(rep.status_code))
conn = A.get_db()
macs = {x[0] for x in conn.execute("SELECT adresse_mac FROM appareil_macs WHERE appareil_id=?", (aid,)).fetchall()}
conn.close()
verifier('ff:00:00:00:00:05' in macs, 'MAC du scan ajoutée en secondaire', str(macs))

print('\n=== 7. Corrélation : _macs_secondaires ===')
conn = A.get_db()
sec = N._macs_secondaires(conn, 1)
conn.close()
verifier(sec.get('dd:00:00:00:00:04') == aid and sec.get('ff:00:00:00:00:05') == aid,
         'toutes les MAC secondaires pointent vers le bon appareil', str(sorted(sec)))

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
