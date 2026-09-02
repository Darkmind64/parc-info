#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie les deux derniers points « pistes d'amélioration » de l'audit
architecture (2026-08-20, pas des bugs — des demandes explicites) :

  - Détection réseau enrichie par SNMP (sysDescr/sysName), absente jusqu'ici.
    Client SNMPv1 GET minimal, encodage BER à la main (aucune dépendance).
    Testé contre un agent SNMP factice (vrai aller-retour UDP, pas un mock
    de la fonction) pour vérifier l'encodage/décodage BER de bout en bout.
  - Baie de brassage 100% manuelle : le scan sait maintenant identifier les
    équipements réseau (marque/modèle) — /api/scan/importer suggère
    (jamais n'impose) une entrée dans la baie pour ceux pas encore
    positionnés, sans jamais déduire la position physique.

Usage :
    python test_snmp_et_suggestion_baie.py
"""

import io
import os
import socket
import sys
import tempfile
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='snmp_baie_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A   # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


def agent_snmp_factice(sysdescr, sysname):
    """Démarre un agent SNMP minimal sur 127.0.0.1 (port libre), répond une
    seule fois avec les valeurs données, puis se ferme. Retourne le port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]

    def repondre():
        try:
            sock.settimeout(3)
            data, addr = sock.recvfrom(2048)
            # écho du request-id de la requête (comme un vrai agent SNMP)
            _, corps, _ = A._ber_lire_tlv(data, 0)
            _pp = 0
            _, _v, _pp = A._ber_lire_tlv(corps, _pp)
            _, _c, _pp = A._ber_lire_tlv(corps, _pp)
            _, pdu_req, _ = A._ber_lire_tlv(corps, _pp)
            _, rid_b, _ = A._ber_lire_tlv(pdu_req, 0)
            varbinds = (A._ber_sequence(0x30, A._ber_oid(A._OID_SYS_DESCR) + A._ber_chaine(sysdescr))
                        + A._ber_sequence(0x30, A._ber_oid(A._OID_SYS_NAME) + A._ber_chaine(sysname)))
            pdu_corps = (b'\x02' + A._ber_longueur(len(rid_b)) + rid_b
                         + A._ber_entier(0) + A._ber_entier(0) + A._ber_sequence(0x30, varbinds))
            pdu = A._ber_sequence(0xa2, pdu_corps)
            message = A._ber_sequence(0x30, A._ber_entier(0) + A._ber_chaine('public') + pdu)
            sock.sendto(message, addr)
        except Exception:
            pass
        finally:
            sock.close()

    threading.Thread(target=repondre, daemon=True).start()
    return port


# ═══════════════════════════════════════════════════════════════════════════
print('=== 1. SNMP : aller-retour BER complet contre un agent factice ===')
port = agent_snmp_factice(
    'Cisco IOS Software, C2960 Software (C2960-LANBASEK9-M), Version 12.2(55)SE10',
    'switch-etage2')
resultat = A._snmp_get('127.0.0.1', [A._OID_SYS_DESCR, A._OID_SYS_NAME], port=port, timeout=2)
verifier(resultat.get(A._OID_SYS_DESCR, '').startswith('Cisco IOS Software'),
          "sysDescr décodé correctement", resultat.get(A._OID_SYS_DESCR, ''))
verifier(resultat.get(A._OID_SYS_NAME) == 'switch-etage2',
          "sysName décodé correctement", resultat.get(A._OID_SYS_NAME, ''))

print('\n=== 2. SNMP : accents et caractères UTF-8 dans sysDescr ===')
port2 = agent_snmp_factice('Imprimante réseau — bâtiment nord', 'imprimante-accueil')
resultat2 = A._snmp_get('127.0.0.1', [A._OID_SYS_DESCR, A._OID_SYS_NAME], port=port2, timeout=2)
verifier(resultat2.get(A._OID_SYS_DESCR) == 'Imprimante réseau — bâtiment nord',
          "accents/caractères spéciaux préservés", resultat2.get(A._OID_SYS_DESCR, ''))

print('\n=== 3. SNMP : agent absent -> {} sans exception, jamais bloquant ===')
resultat3 = A._snmp_get('127.0.0.1', [A._OID_SYS_DESCR], port=1, timeout=0.3)
verifier(resultat3 == {}, "aucune réponse -> dict vide, pas d'exception levée")

print('\n=== 4. sysDescr alimente bien la classification (_deviner_type) ===')
t = A._deviner_type('switch-etage2', [], extra_signal='Cisco IOS Software, C2960 Software')
verifier(t == 'Switch', "sysDescr Cisco -> Switch via le mot-clé déjà en place", t)
t2 = A._deviner_type('inconnu', [], extra_signal='HP ETHERNET MULTI-ENVIRONMENT,JETDIRECT')
verifier(t2 == 'Imprimante', "sysDescr HP JetDirect -> Imprimante", t2)

# ═══════════════════════════════════════════════════════════════════════════
print('\n=== 5. /api/scan/importer : suggestion baie pour un nouvel équipement réseau ===')
A.init_db()
conn = A.get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (1, 'admin', 'x', 'Admin', 'admin', 1)")
conn.execute("INSERT OR IGNORE INTO clients (id, nom, auth_user_id) VALUES (1, 'Client Un', 1)")
conn.commit(); conn.close()

client = A.app.test_client()
with client.session_transaction() as s:
    s['auth_user_id'] = 1
    s['client_id'] = 1

r = client.post('/api/scan/importer', json={'appareils': [
    {'ip': '192.168.1.50', 'hostname': 'switch-etage2', 'type': 'Switch',
     'brand': 'Cisco', 'mac': 'AA:BB:CC:DD:EE:01'},
    {'ip': '192.168.1.51', 'hostname': 'poste-julie', 'type': 'PC (Windows)',
     'mac': 'AA:BB:CC:DD:EE:02'},
]})
d = r.get_json()
verifier(r.status_code == 200, 'import accepté', str(r.status_code))
suggestions = d.get('suggestions_baie', [])
noms_suggeres = {s['nom'] for s in suggestions}
verifier('switch-etage2' in noms_suggeres, "le switch détecté est suggéré pour la baie")
verifier('poste-julie' not in noms_suggeres, "le PC n'est jamais suggéré pour la baie")

print('\n=== 6. /api/scan/importer : plus de suggestion une fois positionné dans la baie ===')
conn = A.get_db()
switch_id = conn.execute("SELECT id FROM appareils WHERE nom_machine='switch-etage2'").fetchone()[0]
conn.execute("INSERT INTO baie_slots (client_id, position, hauteur_u, appareil_id) VALUES (1, 1, 1, ?)",
             (switch_id,))
conn.commit(); conn.close()

r2 = client.post('/api/scan/importer', json={'appareils': [
    {'ip': '192.168.1.50', 'hostname': 'switch-etage2', 'type': 'Switch',
     'brand': 'Cisco', 'mac': 'AA:BB:CC:DD:EE:01'},
]})
d2 = r2.get_json()
verifier(d2.get('suggestions_baie', []) == [],
          "aucune suggestion pour un équipement déjà positionné dans la baie", str(d2.get('suggestions_baie')))

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
