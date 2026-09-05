#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit baie de brassage, lot 5 (#7) : SNMPv3 (USM, authNoPriv) + distinction
« SNMP présent mais refusé » vs « pas de matériel SNMP ».

Ce que le test contrôle :
  - _v3_ku / _v3_kul : vecteurs de test RFC 3414 Appendix A.3 (MD5 et SHA-1)
  - _v3_message + _v3_signer : le message se re-parse, la signature HMAC est
    valide (un vérificateur indépendant recalcule sur le message à zéro)
  - _v3_discover : découverte d'engine contre un faux agent (non authentifié)
  - _snmp_v3_exchange + _snmp_get_typed : GET authNoPriv bout en bout, le faux
    agent vérifie le HMAC ; mauvais mot de passe -> Report usmStatsWrongDigests
  - _snmp_presence : agent présent mais refusé -> (True, False) ; silence ->
    (False, False)

Usage :
    python test_snmp_v3.py
"""

import hashlib
import hmac
import io
import os
import socket
import sys
import tempfile
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='snmpv3_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A   # noqa: E402

echecs = []


def verifier(cond, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if cond else 'ÉCHEC', libelle, (' — ' + detail) if detail else ''))
    if not cond:
        echecs.append(libelle)


# ── helpers BER minimaux pour le faux agent ────────────────────────────────
def _lire_len(b, off):
    n = b[off]; off += 1
    if n & 0x80:
        k = n & 0x7f
        n = int.from_bytes(b[off:off + k], 'big'); off += k
    return n, off


def _skip(b, off):
    off += 1
    ln, off = _lire_len(b, off)
    return off + ln


def _authparams_span(msg):
    """(début, fin) des octets de msgAuthenticationParameters dans un
    SNMPv3Message brut."""
    off = 1
    _, off = _lire_len(msg, off)          # contenu de l'enveloppe
    off = _skip(msg, off)                 # msgVersion
    off = _skip(msg, off)                 # msgGlobalData
    assert msg[off] == 0x04               # msgSecurityParameters
    _, spc = _lire_len(msg, off + 1)
    assert msg[spc] == 0x30               # USM SEQUENCE
    _, p = _lire_len(msg, spc + 1)
    for _ in range(4):                    # engineID, boots, time, user
        p = _skip(msg, p)
    assert msg[p] == 0x04                 # authParams
    ln, ap = _lire_len(msg, p + 1)
    return ap, ap + ln


def _tlv(b, off):
    """(tag, valeur_brute, off_suivant)."""
    tag = b[off]
    ln, voff = _lire_len(b, off + 1)
    return tag, b[voff:voff + ln], voff + ln


def _scoped_oids(msg):
    """(pdu_tag, request_id, [oids demandés]) dans la ScopedPDU d'un message brut."""
    _, contenu, _ = _tlv(msg, 0)
    p = 0
    _, _v, p = _tlv(contenu, p)           # msgVersion
    _, _g, p = _tlv(contenu, p)           # msgGlobalData
    _, _s, p = _tlv(contenu, p)           # msgSecurityParameters
    _, scoped, p = _tlv(contenu, p)       # msgData = ScopedPDU
    q = 0
    _, _ce, q = _tlv(scoped, q)           # contextEngineID
    _, _cn, q = _tlv(scoped, q)           # contextName
    pdu_tag, pdu_body, _ = _tlv(scoped, q)
    r = 0
    _, rid_b, r = _tlv(pdu_body, r)       # request-id
    _, _es, r = _tlv(pdu_body, r)
    _, _ei, r = _tlv(pdu_body, r)
    _, vbl, r = _tlv(pdu_body, r)         # varbind list
    oids, vp = [], 0
    while vp < len(vbl):
        _, vbc, vp = _tlv(vbl, vp)
        _, oidb, _ = _tlv(vbc, 0)
        oids.append(A._ber_decoder_oid(oidb))
    return pdu_tag, int.from_bytes(rid_b, 'big'), oids


# ── faux agent SNMPv3 ─────────────────────────────────────────────────────
FAKE_ENGINE = bytes.fromhex('80001f8880' + '1122334455')   # 10 octets
USER = 'monitor'
PASSWORD = 'chocolat-au-lait'
HFN, TAGLEN = hashlib.sha1, 12
KU = A._v3_ku(HFN, PASSWORD)
KUL = A._v3_kul(HFN, KU, FAKE_ENGINE)


def _pdu_reponse(reqid, oids, tag=0xa2, valeurs=None):
    vbs = b''
    canned = valeurs or {A._OID_SYS_DESCR: (0x04, b'FauxSwitch v3'),
                        A._OID_SYS_NAME:  (0x04, b'SW-V3-TEST')}
    for o in oids:
        t, v = canned.get(o, (0x04, b''))
        vbs += A._ber_sequence(0x30, A._ber_oid(o) + bytes([t]) + A._ber_longueur(len(v)) + v)
    return A._ber_sequence(tag, A._ber_entier(reqid) + A._ber_entier(0)
                           + A._ber_entier(0) + A._ber_sequence(0x30, vbs))


def _report(reqid, stat_oid):
    vb = A._ber_sequence(0x30, A._ber_oid(stat_oid) + b'\x41\x01\x01')  # Counter32 = 1
    return A._ber_sequence(0xa8, A._ber_entier(reqid) + A._ber_entier(0)
                           + A._ber_entier(0) + A._ber_sequence(0x30, vb))


def _agent(sock, stop, muet_auth=False):
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except (socket.timeout, OSError):
            continue
        try:
            d = A._v3_parse(data)
            eid = d['engine_id']
            pdu_tag, reqid, oids = _scoped_oids(data)
            if not eid:
                # découverte -> Report avec notre engineID (non authentifié)
                rep = A._v3_message(1, FAKE_ENGINE, 3, 555, b'',
                                    _report(reqid, '1.3.6.1.6.3.15.1.1.4.0'), taglen=0)
                sock.sendto(rep, addr)
                continue
            # requête authentifiée : vérifier le HMAC
            a0, a1 = _authparams_span(data)
            recu = data[a0:a1]
            zero = data[:a0] + b'\x00' * (a1 - a0) + data[a1:]
            attendu = hmac.new(KUL, zero, HFN).digest()[:TAGLEN]
            if recu == attendu and not muet_auth:
                pdu = _pdu_reponse(reqid, oids)
                base = A._v3_message(reqid, FAKE_ENGINE, 3, 556, USER.encode(), pdu, taglen=TAGLEN)
                sock.sendto(A._v3_signer(base, HFN, TAGLEN, KUL), addr)
            else:
                pdu = _report(reqid, '1.3.6.1.6.3.15.1.1.5.0')   # usmStatsWrongDigests
                sock.sendto(A._v3_message(reqid, FAKE_ENGINE, 3, 556, b'', pdu, taglen=0), addr)
        except Exception as e:
            print('   [agent] exception', e)


def _demarrer_agent(**kw):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 0))
    s.settimeout(0.3)
    stop = threading.Event()
    t = threading.Thread(target=_agent, args=(s, stop), kwargs=kw, daemon=True)
    t.start()
    return s.getsockname()[1], stop, s


# ── audit réseau 2026-09-05, #09 : ni _v3_discover ni _snmp_v3_exchange ne
# vérifiaient que la réponse reçue correspondait au request-id envoyé — le
# premier datagramme UDP arrivant sur le port éphémère était accepté tel
# quel. Ces deux agents envoient délibérément une réponse PÉRIMÉE (mauvais
# request-id, comme un paquet tardif d'un échange précédent) AVANT la vraie
# réponse, pour vérifier que le client sait l'ignorer.
def _agent_reponse_perimee(sock, stop):
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except (socket.timeout, OSError):
            continue
        try:
            d = A._v3_parse(data)
            eid = d['engine_id']
            _pdu_tag, reqid, oids = _scoped_oids(data)
            if not eid:
                rep = A._v3_message(1, FAKE_ENGINE, 3, 555, b'',
                                    _report(reqid, '1.3.6.1.6.3.15.1.1.4.0'), taglen=0)
                sock.sendto(rep, addr)
                continue
            a0, a1 = _authparams_span(data)
            recu = data[a0:a1]
            zero = data[:a0] + b'\x00' * (a1 - a0) + data[a1:]
            attendu = hmac.new(KUL, zero, HFN).digest()[:TAGLEN]
            if recu == attendu:
                # 1) réponse au MAUVAIS request-id (paquet tardif simulé), avec
                #    une valeur DIFFÉRENTE de la vraie — si le client l'acceptait
                #    à tort, le test le verrait immédiatement dans la valeur lue.
                pdu_perime = _pdu_reponse(reqid + 999, oids,
                                          valeurs={A._OID_SYS_DESCR: (0x04, b'REPONSE-PERIMEE-IGNOREZ-MOI')})
                base_perime = A._v3_message(reqid, FAKE_ENGINE, 3, 556, USER.encode(), pdu_perime, taglen=TAGLEN)
                sock.sendto(A._v3_signer(base_perime, HFN, TAGLEN, KUL), addr)
                # 2) la vraie réponse, avec le bon request-id
                pdu = _pdu_reponse(reqid, oids)
                base = A._v3_message(reqid, FAKE_ENGINE, 3, 556, USER.encode(), pdu, taglen=TAGLEN)
                sock.sendto(A._v3_signer(base, HFN, TAGLEN, KUL), addr)
            else:
                pdu = _report(reqid, '1.3.6.1.6.3.15.1.1.5.0')
                sock.sendto(A._v3_message(reqid, FAKE_ENGINE, 3, 556, b'', pdu, taglen=0), addr)
        except Exception as e:
            print('   [agent-perime] exception', e)


def _agent_discover_perime(sock, stop):
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except (socket.timeout, OSError):
            continue
        try:
            d = A._v3_parse(data)
            if d and not d['engine_id']:
                _pdu_tag, reqid, _oids = _scoped_oids(data)
                rep_perime = A._v3_message(1, FAKE_ENGINE, 3, 555, b'',
                                           _report(reqid + 999, '1.3.6.1.6.3.15.1.1.4.0'), taglen=0)
                sock.sendto(rep_perime, addr)
                rep = A._v3_message(1, FAKE_ENGINE, 3, 555, b'',
                                    _report(reqid, '1.3.6.1.6.3.15.1.1.4.0'), taglen=0)
                sock.sendto(rep, addr)
        except Exception as e:
            print('   [agent-discover-perime] exception', e)


def _demarrer(fn, **kw):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 0))
    s.settimeout(0.3)
    stop = threading.Event()
    t = threading.Thread(target=fn, args=(s, stop), kwargs=kw, daemon=True)
    t.start()
    return s.getsockname()[1], stop, s


print('=== 1. RFC 3414 A.3 — dérivation et localisation de clé ===')
eid_rfc = bytes.fromhex('000000000000000000000002')
for proto, hf, ku_att, kul_att in [
        ('MD5', hashlib.md5, '9faf3283884e92834ebc9847d8edd963', '526f5eed9fcce26f8964c2930787d82b'),
        ('SHA', hashlib.sha1, '9fb5cc0381497b3793528939ff788d5d79145211',
         '6695febc9288e36282235fc7151f128497b38f3f')]:
    ku = A._v3_ku(hf, 'maplesyrup')
    kul = A._v3_kul(hf, ku, eid_rfc)
    verifier(ku.hex() == ku_att, f'{proto} : Ku', ku.hex())
    verifier(kul.hex() == kul_att, f'{proto} : Kul (localisée)', kul.hex())

print('\n=== 2. _v3_message + _v3_signer ===')
pdu = A._ber_sequence(0xa0, A._ber_entier(9) + A._ber_entier(0) + A._ber_entier(0)
                      + A._ber_sequence(0x30, b''))
base = A._v3_message(9, FAKE_ENGINE, 1, 2, USER.encode(), pdu, taglen=12)
signed = A._v3_signer(base, hashlib.sha1, 12, KUL)
verifier(signed is not None and len(signed) == len(base), 'signature insérée sans changer la taille')
a0, a1 = _authparams_span(signed)
zero = signed[:a0] + b'\x00' * 12 + signed[a1:]
verifier(hmac.new(KUL, zero, hashlib.sha1).digest()[:12] == signed[a0:a1],
         'HMAC vérifiable par un tiers (recalcul sur le message à zéro)')
d = A._v3_parse(signed)
verifier(d and d['engine_id'] == FAKE_ENGINE and d['user'] == USER.encode(),
         '_v3_parse relit engineID + user', str(d and d['user']))

print('\n=== 3. _v3_discover + _snmp_presence (agent présent) ===')
A._v3_engine_cache.clear(); A._v3_engine_negatif.clear()
port, stop, sock = _demarrer_agent()
try:
    disc = A._v3_discover('127.0.0.1', port=port, timeout=1.0)
    verifier(disc and disc[0] == FAKE_ENGINE, 'engine ID découvert', disc and disc[0].hex())
    # sans utilisateur v3 configuré : présent mais non exploitable
    present, exploit, detail = A._snmp_presence('127.0.0.1', ['public'], port=port, timeout=1.0)
    verifier(present and not exploit, 'agent présent mais refusé (pas de creds)', detail)
finally:
    stop.set(); sock.close()

print('\n=== 4. GET authNoPriv bout en bout ===')
A._v3_engine_cache.clear(); A._v3_engine_negatif.clear()
A.cfg_set('diag_snmp_v3_user', USER)
A.cfg_set('diag_snmp_v3_auth_proto', 'SHA')
A.cfg_set('diag_snmp_v3_auth_pass', PASSWORD)
port, stop, sock = _demarrer_agent()
try:
    r = A._snmp_get_typed('127.0.0.1', [A._OID_SYS_DESCR, A._OID_SYS_NAME], port=port, timeout=1.5)
    verifier(r.get(A._OID_SYS_DESCR) == 'FauxSwitch v3', 'sysDescr lu en SNMPv3', str(r))
    verifier(r.get(A._OID_SYS_NAME) == 'SW-V3-TEST', 'sysName lu en SNMPv3')
    present, exploit, detail = A._snmp_presence('127.0.0.1', ['public'], port=port, timeout=1.5)
    verifier(present and exploit and 'SNMPv3' in detail, 'presence : exploitable via v3', detail)
finally:
    stop.set(); sock.close()

print('\n=== 5. Mauvais mot de passe -> refus authentifié ===')
A._v3_engine_cache.clear(); A._v3_engine_negatif.clear()
A.cfg_set('diag_snmp_v3_auth_pass', 'mauvais-mot-de-passe')
port, stop, sock = _demarrer_agent()
try:
    b, st = A._snmp_v3_exchange('127.0.0.1', pdu, port=port, timeout=1.5)
    verifier(b is None and st.startswith('refuse:'), 'échec d\'auth signalé', st)
finally:
    stop.set(); sock.close()

print('\n=== 6. Aucun agent -> silence ===')
A._v3_engine_cache.clear(); A._v3_engine_negatif.clear()
verifier(A._v3_discover('127.0.0.1', port=1, timeout=0.4) is None, 'découverte : rien sur un port fermé')
present, exploit, _ = A._snmp_presence('127.0.0.1', ['public'], port=1, timeout=0.4)
verifier(not present and not exploit, 'presence : aucun SNMP -> (False, False)')

print('\n=== 7. _snmp_v3_exchange ignore une réponse au mauvais request-id (audit #09) ===')
A._v3_engine_cache.clear(); A._v3_engine_negatif.clear()
A.cfg_set('diag_snmp_v3_auth_pass', PASSWORD)
port, stop, sock = _demarrer(_agent_reponse_perimee)
try:
    r = A._snmp_get_typed('127.0.0.1', [A._OID_SYS_DESCR], port=port, timeout=1.5)
    verifier(r.get(A._OID_SYS_DESCR) == 'FauxSwitch v3',
             "la vraie réponse (bon request-id) est retenue malgré une réponse périmée envoyée avant elle",
             str(r))
finally:
    stop.set(); sock.close()

print('\n=== 8. _v3_discover ignore aussi une réponse au mauvais request-id ===')
A._v3_engine_cache.clear(); A._v3_engine_negatif.clear()
port, stop, sock = _demarrer(_agent_discover_perime)
try:
    disc = A._v3_discover('127.0.0.1', port=port, timeout=1.0)
    verifier(disc is not None and disc[0] == FAKE_ENGINE,
             "la découverte d'engine ignore la réponse périmée et retient la bonne",
             str(disc))
finally:
    stop.set(); sock.close()

print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
