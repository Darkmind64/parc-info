#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Baux DHCP relevés à la source (Plan 1) — bout en bout.

  1. import d'un fichier `dhcpd.leases` ISC → table `dhcp_baux` peuplée,
     réservation vs bail dynamique, bail périmé (`free`) ignoré ;
  2. relevé SNMP Mikrotik simulé (faux `_snmp_walk` / `_snmp_walk_octets`)
     via `_dhcp_relever_client` ;
  3. `_importer_appareils_scan` reprend le hostname annoncé au DHCP et
     journalise un « Conflit IP / bail DHCP » quand l'IP scannée diffère ;
  4. `baux_hors_inventaire` repère une réservation dont la MAC n'a pas de
     fiche ;
  5. `network_diag.verifier_parc_general` confirme `serveur_dhcp` dès qu'il
     y a des baux.

Usage :
    python test_dhcp_baux.py
"""

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='dhcp_baux_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A                       # noqa: E402
import network_diag                   # noqa: E402
from netdiag import dhcp as D         # noqa: E402
from config_helpers import cfg_set    # noqa: E402

A.init_db()
cfg_set('diag_snmp_actif', '1')

echecs = []


def verifier(cond, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if cond else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not cond:
        echecs.append(libelle)


conn = A.get_db()
cid = conn.execute("INSERT INTO clients (nom, date_creation) VALUES (?,?)",
                   ('ACME DHCP', '2026-01-01T00:00:00')).lastrowid
rid = conn.execute(
    "INSERT INTO appareils (client_id, nom_machine, type_appareil, adresse_ip, adresse_mac, "
    "statut, date_creation) VALUES (?,?,?,?,?, 'actif','2026-01-01')",
    (cid, 'Routeur', 'Routeur/Pare-feu', '192.168.1.1', 'de:ad:be:ef:00:01')).lastrowid
conn.execute("INSERT INTO parc_general (client_id, serveur_dhcp, plage_ip_locale) VALUES (?,?,?)",
             (cid, '192.168.1.1', '192.168.1.0/24'))
conn.commit()

# ═══════════════════════════════════════════════════════════════════════════
print('=== 1. Import fichier dhcpd.leases ISC ===')
ISC = """
lease 192.168.1.10 { binding state active; hardware ethernet 00:1b:44:11:22:33;
  client-hostname "POSTE-COMPTA"; ends 3 2026/06/10 18:00:00; }
lease 192.168.1.11 { binding state free; hardware ethernet aa:bb:cc:dd:ee:99; }
host imprimante-rh { hardware ethernet 00:1b:44:44:55:66; fixed-address 192.168.1.50; }
"""
baux, fmt = D.parser_auto(ISC)
imp = D.importer_baux(conn, cid, baux, source_methode='fichier:%s' % fmt)
conn.commit()
verifier(fmt == 'isc', "format ISC détecté")
verifier(imp['ecrits'] == 2, "2 baux écrits (le bail 'free' est ignoré)", str(imp))
types = {b['ip']: b['type'] for b in D.lister_baux(conn, cid)}
verifier(types.get('192.168.1.50') == 'statique', "host { fixed-address } → statique")
verifier(types.get('192.168.1.10') == 'dynamique', "lease actif → dynamique")

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 2. Relevé SNMP Mikrotik simulé ===')
_orig_walk, _orig_octets = network_diag._snmp_walk, network_diag._snmp_walk_octets


def _faux_walk(oid_base, ip, comm, **kw):
    if ip != '192.168.1.1':
        return {}
    if oid_base == D._MT_LEASE_ADDR:
        return {'1': '192.168.1.10', '2': '192.168.1.77'}
    return {}


def _faux_octets(oid_base, ip, comm, **kw):
    if ip == '192.168.1.1' and oid_base == D._MT_LEASE_MAC:
        return {'1': bytes.fromhex('001b44112233'), '2': bytes.fromhex('001b4499aabb')}
    return {}


network_diag._snmp_walk = _faux_walk
network_diag._snmp_walk_octets = _faux_octets
try:
    r = A._dhcp_relever_client(conn, cid, declencheur='manuel')
finally:
    network_diag._snmp_walk, network_diag._snmp_walk_octets = _orig_walk, _orig_octets
conn.commit()
verifier(r['releves'] == 2, "2 baux relevés en SNMP", str(r))
snmp_ip = {b['ip'] for b in D.lister_baux(conn, cid) if b['source_methode'] == 'snmp:mikrotik'}
verifier('192.168.1.77' in snmp_ip, "nouveau bail SNMP ajouté (192.168.1.77)")

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 3. Import de scan : hostname DHCP + conflit IP ===')
items = [{'ip': '192.168.1.199', 'ports': [445], 'mac': '00:1b:44:11:22:33', 'type': 'PC'}]
res = A._importer_appareils_scan(conn, cid, items, origine='scan')
conn.commit()
verifier(res['importes'] == 1, "1 appareil importé")
row = conn.execute("SELECT nom_machine FROM appareils WHERE client_id=? AND adresse_ip='192.168.1.199'",
                   (cid,)).fetchone()
verifier(row and row[0] == 'POSTE-COMPTA', "hostname DHCP repris comme nom", str(row))
n = conn.execute("SELECT COUNT(*) FROM historique WHERE client_id=? AND action='Conflit IP / bail DHCP'",
                 (cid,)).fetchone()[0]
verifier(n == 1, "conflit IP / bail DHCP journalisé", 'n=%d' % n)

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 4. Réservation sans fiche = fantôme ===')
fant = {b['mac'] for b in D.baux_hors_inventaire(conn, cid)}
verifier('00:1b:44:44:55:66' in fant, "imprimante-rh (réservation, aucune fiche) → fantôme")
verifier('00:1b:44:11:22:33' not in fant, "MAC désormais dans l'inventaire → pas fantôme")

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 5. verifier_parc_general confirme serveur_dhcp ===')
v = network_diag.verifier_parc_general(cid)
verifier(v.get('serveur_dhcp', {}).get('etat') == 'confirme',
         "serveur_dhcp confirmé (des baux existent)", str(v.get('serveur_dhcp')))

conn.close()
print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
