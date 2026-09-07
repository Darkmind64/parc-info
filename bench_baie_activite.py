#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bench du démarrage de la vue d'activité de la baie de brassage, SNMP RÉEL
(faux agent SNMPv2c en UDP local — GET / GETNEXT / GETBULK multi-colonnes).

Objectif : chiffrer combien de temps prend `_cycle_activite` (le cœur des LEDs
qui clignotent) selon l'état du switch :
  - switch sain, rapide
  - switch sain mais lent (latence par paquet)
  - switch injoignable (pas de réponse SNMP) — le cas qui fait « plusieurs minutes »
  - mauvaise communauté
  - v3 configuré mais non fonctionnel (le switch ne parle que v2c)
  - plusieurs switchs

    python bench_baie_activite.py
"""
import io
import os
import socket
import sys
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace',
                              line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='bench_baie_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A                       # noqa: E402
import network_diag as N              # noqa: E402
import config_helpers as C            # noqa: E402
from app import (_ber_sequence, _ber_oid, _ber_entier, _ber_chaine,   # noqa: E402
                 _ber_lire_tlv, _ber_decoder_oid, _ber_longueur)

A.init_db()

# Config AVANT d'ouvrir la connexion longue durée du bench (sinon SQLite lock).
C.cfg_set('diag_snmp_actif', '1')
C.cfg_set('diag_snmp_communautes', 'public')
C.cfg_set('diag_baie_prechauffe', '0')

# ─────────────────────────── faux agent SNMPv2c ────────────────────────────

_IF = N._OID_IF_DESCR.rsplit('.', 2)[0]   # 1.3.6.1.2.1.2.2.1
_IFX = N._OID_IF_NAME.rsplit('.', 2)[0]   # 1.3.6.1.2.1.31.1.1.1

CT32, CT64, TICKS, INT, OCTETS, GAUGE = 0x41, 0x46, 0x43, 0x02, 0x04, 0x42


def _enc_val(tag, val):
    if tag == 0x82:                       # endOfMibView
        return b'\x82\x00'
    if tag == INT:
        return _ber_entier(int(val))
    if tag in (CT32, CT64, TICKS, GAUGE):
        v = int(val)
        b = v.to_bytes(max(1, (v.bit_length() + 7) // 8), 'big') if v else b'\x00'
        return bytes([tag]) + _ber_longueur(len(b)) + b
    return _ber_chaine(str(val))          # OCTET STRING


def _t(oid):
    return tuple(int(x) for x in oid.split('.'))


def _build_mib(nports, tick0, sans_ifx=False):
    """Table triée [(oid_tuple, tag, valeur)] d'un switch nports ports.
    `sans_ifx` : pas d'ifXTable / HC / dot3 (cas HP ProCurve 1810G — compteurs
    32 bits seulement, agent minimal)."""
    m = {}
    m['1.3.6.1.2.1.1.1.0'] = (OCTETS, 'FakeSwitch %dp bench' % nports)
    m['1.3.6.1.2.1.1.3.0'] = (TICKS, tick0)
    m['1.3.6.1.2.1.1.5.0'] = (OCTETS, 'SW-BENCH')
    cols32 = {N._OID_IF_OPER: INT, N._OID_IF_TYPE: INT, N._OID_IF_SPEED: GAUGE,
              N._OID_IF_IN_OCTETS: CT32, N._OID_IF_OUT_OCTETS: CT32,
              N._OID_IF_IN_UCAST: CT32, N._OID_IF_OUT_UCAST: CT32,
              N._OID_IF_IN_NUCAST: CT32, N._OID_IF_OUT_NUCAST: CT32,
              N._OID_IF_IN_ERRORS: CT32, N._OID_IF_OUT_ERRORS: CT32}
    colsx = {} if sans_ifx else {
             N._OID_IF_NAME: OCTETS, N._OID_IF_ALIAS: OCTETS,
             N._OID_IF_HCIN: CT64, N._OID_IF_HCOUT: CT64,
             N._OID_IF_HCIN_UCAST: CT64, N._OID_IF_HCOUT_UCAST: CT64,
             N._OID_IF_HIGHSPEED: GAUGE}
    for i in range(1, nports + 1):
        traf = i * 1_000_000        # trafic fictif proportionnel à l'index
        m['%s.%d' % (N._OID_IF_DESCR, i)] = (OCTETS, 'GigabitEthernet0/%d' % i)
        for oid, tag in cols32.items():
            if oid == N._OID_IF_OPER:
                v = 1 if i % 7 else 2
            elif oid == N._OID_IF_TYPE:
                v = 6
            elif oid == N._OID_IF_SPEED:
                v = 1_000_000_000
            elif 'ERRORS' in oid or oid in (N._OID_IF_IN_ERRORS, N._OID_IF_OUT_ERRORS):
                v = 0
            else:
                v = traf
            m['%s.%d' % (oid, i)] = (tag, v)
        for oid, tag in colsx.items():
            if oid == N._OID_IF_NAME:
                v = 'Gi0/%d' % i
            elif oid == N._OID_IF_ALIAS:
                v = ''
            elif oid == N._OID_IF_HIGHSPEED:
                v = 1000
            else:
                v = traf * 8
            m['%s.%d' % (oid, i)] = (tag, v)
    return sorted(((_t(o), tag, val) for o, (tag, val) in m.items()), key=lambda x: x[0])


class FauxSwitch:
    def __init__(self, nports=48, delai=0.0, communaute='public', muet=False,
                 bind_ip='127.0.0.1', sans_ifx=False):
        self.nports, self.delai, self.communaute, self.muet = nports, delai, communaute, muet
        self.sans_ifx = sans_ifx
        self.t0 = time.time()
        self.polls = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind_ip, 0))
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.th = threading.Thread(target=self._loop, daemon=True)
        self.th.start()

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass

    def _mib(self):
        ticks = int((time.time() - self.t0) * 100) + 100000
        # compteurs qui montent : +8 Mbit/s * index et par seconde écoulée
        return _build_mib(self.nports, ticks, sans_ifx=self.sans_ifx)

    def _succ(self, mib, oid_t):
        for entry in mib:
            if entry[0] > oid_t:
                return entry
        return None

    def _loop(self):
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            if self.muet:
                continue
            if self.delai:
                time.sleep(self.delai)
            try:
                resp = self._handle(data)
            except Exception:
                resp = None
            if resp:
                try:
                    self.sock.sendto(resp, addr)
                except OSError:
                    pass

    def _handle(self, data):
        _, corps, _ = _ber_lire_tlv(data, 0)
        p = 0
        _, ver, p = _ber_lire_tlv(corps, p)
        _, comm, p = _ber_lire_tlv(corps, p)
        if comm.decode('latin1') != self.communaute:
            return None                       # mauvaise communauté : silence
        tag_pdu, pdu, _ = _ber_lire_tlv(corps, p)
        q = 0
        _, rid, q = _ber_lire_tlv(pdu, q)
        _, nonrep, q = _ber_lire_tlv(pdu, q)
        _, maxrep, q = _ber_lire_tlv(pdu, q)
        _, vbl, q = _ber_lire_tlv(pdu, q)
        maxrep_i = int.from_bytes(maxrep, 'big') or 1
        # liste des OID demandés
        oids = []
        vp = 0
        while vp < len(vbl):
            t, vbc, vp = _ber_lire_tlv(vbl, vp)
            bp = 0
            _, ob, bp = _ber_lire_tlv(vbc, bp)
            oids.append(_ber_decoder_oid(ob))
        mib = self._mib()
        out = []
        if tag_pdu == 0xa0:                    # GET
            table = {'.'.join(map(str, e[0])): e for e in mib}
            for o in oids:
                e = table.get(o)
                if e:
                    out.append((o, e[1], e[2]))
                else:
                    out.append((o, 0x82, b''))     # endOfMibView
            self.polls += 1
        elif tag_pdu in (0xa1, 0xa5):          # GETNEXT / GETBULK
            reps = maxrep_i if tag_pdu == 0xa5 else 1
            cur = list(oids)
            for _r in range(reps):
                nxt = []
                for o in cur:
                    e = self._succ(mib, _t(o))
                    if e is None:
                        out.append((o, 0x82, b''))
                    else:
                        oo = '.'.join(map(str, e[0]))
                        out.append((oo, e[1], e[2]))
                        nxt.append(oo)
                cur = nxt
                if not cur:
                    break
            self.polls += 1
        # encodage réponse
        vbs = b''.join(_ber_sequence(0x30, _ber_oid(o) + _enc_val(tag, val))
                       for (o, tag, val) in out)
        resp_pdu = _ber_sequence(0xa2, _ber_entier(int.from_bytes(rid, 'big', signed=True))
                                 + _ber_entier(0) + _ber_entier(0)
                                 + _ber_sequence(0x30, vbs))
        return _ber_sequence(0x30, _ber_entier(1) + _ber_chaine(self.communaute) + resp_pdu)


# ─────────────────────────── mise en place ────────────────────────────

conn = A.get_db()
cur = conn.execute("INSERT INTO clients (nom, date_creation) VALUES ('Bench', '2026-01-01')")
CID = cur.lastrowid


_POS = [0]


def _monter_switch(ip, port, nom='SW'):
    _POS[0] += 1
    conn.execute("INSERT INTO appareils (client_id, nom_machine, type_appareil, adresse_ip) "
                 "VALUES (?,?,?,?)", (CID, nom, 'Switch', ip))
    aid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO baie_slots (client_id, position, appareil_id) VALUES (?,?,?)",
                 (CID, _POS[0], aid))
    sid = conn.execute("SELECT id FROM baie_slots WHERE appareil_id=?", (aid,)).fetchone()[0]
    for n in range(1, 25):
        conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?,?)", (sid, n))
    conn.commit()
    return aid, sid


# le faux agent écoute sur 127.0.0.1:<port> ; on fait croire à ParcInfo que le
# switch est à "127.0.0.1" et on route le SNMP vers le bon port via un patch.
_PORTS = {}          # ip -> port du faux agent


def _patch_port():
    """Force le `port` de chaque primitive SNMP de bas niveau vers le faux agent
    quand l'IP est l'une des nôtres. `port_pos` = index de l'argument positionnel
    `port` (None = seulement en kwarg)."""
    import app as _A
    specs = {'_snmp_get': 4, '_snmp_get_typed': 3, '_snmp_walk': None,
             '_snmp_bulk_cols': None, '_snmp_presence': 2, '_snmp_sysinfo': 3,
             '_snmp_v3_exchange': 2, '_v3_discover': 1}
    for fn, ppos in specs.items():
        orig = getattr(_A, fn, None)
        if orig is None:
            continue

        def faire(orig, ppos):
            def wrap(*a, **k):
                ip = a[0] if a else k.get('ip_str') or k.get('ip')
                if ip in _PORTS:
                    pr = _PORTS[ip]
                    if ppos is not None and len(a) > ppos:
                        a = a[:ppos] + (pr,) + a[ppos + 1:]
                    else:
                        k['port'] = pr
                return orig(*a, **k)
            return wrap
        setattr(_A, fn, faire(orig, ppos))


_patch_port()


def _chrono(label, fn, repet=1):
    N._activite_rechauffe[0] = 0
    for k in ('_activite_prev', '_activite_sut', '_activite_noms', '_activite_switch_ok',
              '_activite_etat_mappe', '_activite_hc', '_activite_resultat', '_activite_detail',
              '_activite_sysinfo', '_activite_noms_froid', '_activite_poe', '_activite_capa_neg',
              '_activite_capa_reprobe', '_activite_fdb'):
        try:
            getattr(N, k).clear()
        except Exception:
            pass
    for k in ('_presence_baie',):
        getattr(N, k).clear()
    try:
        A._v3_engine_cache.clear(); A._v3_engine_negatif.clear()
        A._bulk_col_absente.clear()
    except Exception:
        pass
    ts = []
    for i in range(repet):
        t0 = time.time()
        fn()
        ts.append(time.time() - t0)
        N._activite_rechauffe[0] += 1
    detail = ' | '.join('%.1fs' % x for x in ts)
    print('  %-48s %s' % (label, detail))
    return ts


print('=== Bench démarrage vue d\'activité baie (SNMP réel, faux agent) ===\n')

_IPN = [0]


def _cas(titre, agents, repet=3):
    """agents = liste de (kwargs_FauxSwitch). Monte un switch de baie par agent,
    chrono `_cycle_activite`, nettoie."""
    objs = []
    for kw in agents:
        kw = dict(kw)
        nom = kw.pop('_nom', 'SW')
        _IPN[0] += 1
        ip = '127.0.%d.%d' % (7 + _IPN[0] // 250, 1 + _IPN[0] % 250)
        s = FauxSwitch(bind_ip=ip, **kw)
        _PORTS[ip] = s.port
        _monter_switch(ip, s.port, nom)
        objs.append(s)
    print('--- %s ---' % titre)
    ts = _chrono('cycle 1 (froid) puis suivants', lambda: N._cycle_activite([CID]), repet=repet)
    for s in objs:
        s.stop()
    conn.execute("DELETE FROM baie_slots WHERE client_id=?", (CID,))
    conn.execute("DELETE FROM appareils WHERE client_id=?", (CID,))
    conn.commit()
    print()
    return ts


_cas('1 switch 48 ports, agent instantané', [{'nports': 48}])
_cas('1 switch 48 ports, +30 ms par paquet', [{'nports': 48, 'delai': 0.030}])
_cas('1 switch INJOIGNABLE (aucune réponse SNMP)', [{'nports': 48, 'muet': True}], repet=2)
_cas('1 switch, MAUVAISE COMMUNAUTÉ (agent=prod, config=public)',
     [{'nports': 48, 'communaute': 'prod'}], repet=2)

C.cfg_set('diag_snmp_v3_user', 'monitor')
C.cfg_set('diag_snmp_v3_auth_proto', 'SHA')
C.cfg_set('diag_snmp_v3_auth_pass', 'monitor-pass-1')
_cas('1 switch v2c, mais SNMPv3 configuré (échoue, repli v2c)', [{'nports': 48}])
C.cfg_set('diag_snmp_v3_user', '')

_cas('3 switchs 48 ports (+10 ms/paquet), relevés en parallèle',
     [{'nports': 48, 'delai': 0.010}] * 3)

_cas('LE CAS RÉEL : 1 switch sain + 1 switch INJOIGNABLE dans la même baie',
     [{'nports': 48, 'delai': 0.005, '_nom': 'SW-SAIN'},
      {'nports': 48, 'muet': True, '_nom': 'SW-MORT'}])

# Le cas du terrain : HP ProCurve 1810G — répond (présence OK) mais lent
# (~40 ms/paquet) et agent minimal (pas d'ifXTable / HC / dot3). Sans le cache
# de colonnes absentes, chaque cycle relançait un GETNEXT complet sur chaque
# colonne manquante.
_cas('HP 1810G : répond mais lent (40 ms/pqt) + agent minimal (32 bits, pas d\'ifX)',
     [{'nports': 24, 'delai': 0.040, 'sans_ifx': True}], repet=4)

conn.close()
print('Fait.')
