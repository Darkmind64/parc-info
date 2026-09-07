#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mode terrain / détection de site (site_terrain.py).

Le prestataire utilise ParcInfo CHEZ le client (scan, baie, diagnostic) puis
consulte HORS site. Les fonctions « live » ne doivent alors ni importer son
réseau personnel dans l'inventaire du client, ni donner de fausses infos.

Ce que le test contrôle :
  - mode_terrain() : défaut = 'consultation' en Docker, 'auto' sinon ;
    respecte la config explicite
  - detecter_site() : reconnaît un client depuis les MAC de son inventaire
    présentes dans la table ARP locale ; la MAC de la passerelle suffit ;
    un réseau totalement inconnu reste 'indetermine'
  - site_actif() / clients_sur_site() : consultation coupe tout ; 'indetermine'
    (rien d'affirmable) ne bride rien ; fenêtre de grâce
  - /api/scan/importer refuse (409) en mode consultation
  - _filtrer_clients_sur_site() (network_diag) applique la restriction

Usage :
    python test_mode_terrain.py
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='mode_terrain_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A          # noqa: E402
import network_diag as N  # noqa: E402
import config_helpers as C  # noqa: E402
import site_terrain as S  # noqa: E402

A.init_db()
echecs = []


def verifier(cond, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if cond else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not cond:
        echecs.append(libelle)


def _reset():
    S._cache.update(ts=0.0, res=None)
    S._grace.clear()
    S._ip_pub.update(ts=0.0, ip='')


_arp = {}
N._table_arp = lambda: dict(_arp)
N._passerelle_defaut = lambda: _reset.gw if hasattr(_reset, 'gw') else ''
S._ip_publique = lambda: ''

conn = A.get_db()
cur = conn.execute("INSERT INTO clients (nom, date_creation) VALUES ('Client Terrain', '2026-01-01')")
CID = cur.lastrowid
for i, mac in enumerate(('a0:00:00:00:00:01', 'a0:00:00:00:00:02', 'a0:00:00:00:00:03')):
    conn.execute("INSERT INTO appareils (client_id, nom_machine, adresse_ip, adresse_mac) "
                 "VALUES (?,?,?,?)", (CID, 'M%d' % i, '10.9.0.%d' % (10 + i), mac))
conn.commit()

print('=== 1. mode_terrain() ===')
C.cfg_set('mode_terrain', '')
verifier(S.mode_terrain() == 'consultation', "défaut en Docker -> consultation")
_docker = os.environ.pop('RUNNING_IN_DOCKER', None)
verifier(S.mode_terrain() == 'auto', "défaut hors Docker -> auto")
if _docker is not None:
    os.environ['RUNNING_IN_DOCKER'] = _docker
C.cfg_set('mode_terrain', 'terrain')
verifier(S.mode_terrain() == 'terrain', "config explicite respectée")

print('\n=== 2. detecter_site() : reconnaissance par MAC d\'inventaire ===')
C.cfg_set('mode_terrain', 'auto')
_reset()
_arp = {'10.9.0.10': {'a0:00:00:00:00:01'}, '10.9.0.11': {'A0:00:00:00:00:02'},
        '10.9.0.12': {'a0-00-00-00-00-03'}, '10.9.0.99': {'ff:ee:dd:cc:bb:aa'}}
d = S.detecter_site(conn, force=True)
verifier(d['client_id'] == CID and d['confiance'] == 'sur_site',
         "3 MAC reconnues -> sur_site", str(d))
verifier(d['macs_reconnues'] == 3, "compte des MAC reconnues")
verifier(S.site_actif(conn, CID) is True, "site_actif(CID) True")
verifier(CID in (S.clients_sur_site(conn) or set()), "clients_sur_site contient CID")

print('\n=== 3. passerelle reconnue seule ===')
_reset()
_reset.gw = '10.9.0.10'
_arp = {'10.9.0.10': {'a0:00:00:00:00:01'}}
d = S.detecter_site(conn, force=True)
verifier(d['passerelle_ok'] and d['confiance'] == 'sur_site',
         "MAC de passerelle dans l'inventaire -> sur_site", str(d))
del _reset.gw

print('\n=== 4. réseau inconnu -> indetermine, ne bride rien ===')
_reset()
_arp = {'172.31.0.5': {'11:11:11:11:11:11'}}
d = S.detecter_site(conn, force=True)
verifier(d['client_id'] is None and d['confiance'] == 'indetermine', "aucune reconnaissance", str(d))
verifier(S.clients_sur_site(conn) is None, "clients_sur_site -> None (comportement historique préservé)")
verifier(N._filtrer_clients_sur_site([CID, 999]) == [CID, 999],
         "_filtrer_clients_sur_site ne filtre pas quand rien n'est affirmable")

print('\n=== 5. consultation coupe tout ===')
C.cfg_set('mode_terrain', 'consultation')
_reset()
verifier(S.clients_sur_site(conn) == set(), "clients_sur_site -> set() vide")
verifier(S.site_actif(conn, CID) is False, "site_actif -> False")
verifier(N._filtrer_clients_sur_site([CID]) == [], "_filtrer_clients_sur_site -> []")

print('\n=== 6. fenêtre de grâce ===')
C.cfg_set('mode_terrain', 'auto')
_reset()
_arp = {'10.9.0.10': {'a0:00:00:00:00:01'}, '10.9.0.11': {'a0:00:00:00:00:02'},
        '10.9.0.12': {'a0:00:00:00:00:03'}}
S.detecter_site(conn, force=True)
_arp = {}                       # réseau coupé
S._cache.update(ts=0.0, res=None)
verifier(S.site_actif(conn, CID) is True, "grâce : reste actif malgré l'ARP vide")

print('\n=== 7. consultation : le scan manuel reste possible (confirmation côté page) ===')
# Le blocage dur a été écarté : l'utilisateur garde le contrôle (VLAN isolé,
# première visite, client sans MAC en base). La protection est côté page
# (avertissement + confirmation) et sur les fonctions de FOND (section 5).
conn.execute("INSERT INTO auth_users (login, password_hash, nom, role, actif, date_creation) "
             "VALUES ('terr', 'x', 'Terrain', 'user', 1, '2026-01-01')")
uid = conn.execute("SELECT id FROM auth_users WHERE login='terr'").fetchone()[0]
conn.execute("UPDATE clients SET auth_user_id=? WHERE id=?", (uid, CID))
conn.commit()
C.cfg_set('mode_terrain', 'consultation')
cl = A.app.test_client()
with cl.session_transaction() as sess:
    sess['auth_user_id'] = uid
    sess['client_id'] = CID
r = cl.post('/api/scan/importer', json={'appareils': [
    {'ip': '192.168.50.50', 'mac': '00:aa:bb:cc:dd:ee', 'hostname': 'z', 'ports': [80]}]})
verifier(r.status_code == 200, "import HTTP 200 (non bloqué)", 'HTTP %s' % r.status_code)

print('\n=== 8. /api/scan/client-suggere expose le bloc "site" ===')
C.cfg_set('mode_terrain', 'auto')
_reset()
_arp = {'10.9.0.10': {'a0:00:00:00:00:01'}, '10.9.0.11': {'a0:00:00:00:00:02'},
        '10.9.0.12': {'a0:00:00:00:00:03'}}
d = cl.get('/api/scan/client-suggere').get_json()
verifier(isinstance(d.get('site'), dict) and d['site'].get('client_id') == CID,
         "bloc site présent, client reconnu", str(d.get('site')))

C.cfg_set('mode_terrain', '')
conn.close()

print()
if echecs:
    print('%d ÉCHEC(S) : %s' % (len(echecs), ', '.join(echecs)))
    sys.exit(1)
print('Tous les contrôles mode terrain passent.')
