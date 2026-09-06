"""netdiag.collect — collecteur SNMP unifié (Lot 1 de la refonte).

**Une seule passe SNMP par équipement, menée en parallèle sur tout le parc,
sous budget.** Remplace les balayages séquentiels indépendants qui frappaient
chacun les mêmes switchs :

- palier 3 (`network_diag.interroger_equipements_client`) : boucle **séquentielle**,
  3 GETBULK + 1 GET par équipement ;
- palier 4 (`network_diag.decouvrir_topologie`) : parallèle mais balayage séparé,
  refaisait la sonde de présence et le relevé des interfaces.

`balayer()` collecte en **un seul `_snmp_bulk_cols`** (GETBULK multi-colonnes,
auto-descriptif) toutes les colonnes ifTable / ifXTable / dot3StatsTable
demandées, `_TOPO_WORKERS`+ équipements de front, avec une échéance dure. Le
résultat par équipement est fourni dans **la forme exacte de
`interroger_equipement`** (`{sysname, ts, ports[], hc}`) pour que
`_analyser_snmp` fonctionne sans changement.

Les primitives SNMP (`_snmp_bulk_cols`, `_snmp_presence`) sont importées
paresseusement depuis `app`, comme le fait déjà `network_diag` — leur
déménagement dans `netdiag.snmp` est prévu au Lot 6.
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger('parcinfo')

# ─── OIDs (dupliqués depuis network_diag le temps de la refonte — Lot 6 :
#     source unique dans netdiag.snmp). Définis en local plutôt qu'importés
#     pour ne pas dépendre de l'ordre de chargement des modules. ───────────────
_IF_DESCR       = '1.3.6.1.2.1.2.2.1.2'
_IF_TYPE        = '1.3.6.1.2.1.2.2.1.3'
_IF_SPEED       = '1.3.6.1.2.1.2.2.1.5'
_IF_PHYS        = '1.3.6.1.2.1.2.2.1.6'
_IF_ADMIN       = '1.3.6.1.2.1.2.2.1.7'
_IF_OPER        = '1.3.6.1.2.1.2.2.1.8'
_IF_IN_OCTETS   = '1.3.6.1.2.1.2.2.1.10'
_IF_IN_DISCARDS = '1.3.6.1.2.1.2.2.1.13'
_IF_IN_ERRORS   = '1.3.6.1.2.1.2.2.1.14'
_IF_OUT_OCTETS  = '1.3.6.1.2.1.2.2.1.16'
_IF_OUT_DISCARDS = '1.3.6.1.2.1.2.2.1.19'
_IF_OUT_ERRORS  = '1.3.6.1.2.1.2.2.1.20'
_IF_NAME        = '1.3.6.1.2.1.31.1.1.1.1'
_IF_HCIN        = '1.3.6.1.2.1.31.1.1.1.6'
_IF_HCOUT       = '1.3.6.1.2.1.31.1.1.1.10'
_IF_HIGHSPEED   = '1.3.6.1.2.1.31.1.1.1.15'
_IF_ALIAS       = '1.3.6.1.2.1.31.1.1.1.18'
_DOT3_ALIGN     = '1.3.6.1.2.1.10.7.2.1.2'
_DOT3_FCS       = '1.3.6.1.2.1.10.7.2.1.3'
_DOT3_LATECOLL  = '1.3.6.1.2.1.10.7.2.1.11'
_DOT3_EXCCOLL   = '1.3.6.1.2.1.10.7.2.1.12'
_DOT3_DUPLEX    = '1.3.6.1.2.1.10.7.2.1.19'
_SYS_UPTIME_B   = '1.3.6.1.2.1.1.3'    # sysUpTime : base → GETBULK renvoie .0
_SYS_NAME       = '1.3.6.1.2.1.1.5.0'
_SYS_DESCR      = '1.3.6.1.2.1.1.1.0'

# ifType comptés comme ethernet (mêmes valeurs que network_diag._IFTYPE_ETHERNET :
# 117 = gigabitEthernet déprécié annoncé par HP ProCurve).
_IFTYPE_ETHERNET = frozenset({6, 7, 62, 69, 117})

# Colonnes selon les besoins déclarés.
_COLS_BASE = (_IF_DESCR, _IF_TYPE, _IF_NAME, _IF_ALIAS, _IF_OPER, _IF_ADMIN,
              _IF_SPEED, _IF_HIGHSPEED)
_COLS_COMPTEURS = (_IF_IN_ERRORS, _IF_OUT_ERRORS, _IF_IN_DISCARDS, _IF_OUT_DISCARDS,
                   _IF_HCIN, _IF_HCOUT, _IF_IN_OCTETS, _IF_OUT_OCTETS)
_COLS_DOT3 = (_DOT3_ALIGN, _DOT3_FCS, _DOT3_LATECOLL, _DOT3_EXCCOLL, _DOT3_DUPLEX)

# Mêmes types d'appareil que network_diag._TYPES_EQUIP_SNMP (source unique au Lot 6).
_TYPES_EQUIP_SNMP = ('Switch', 'Switch/AP', 'Routeur/Pare-feu', 'NAS', 'Onduleur / UPS',
                     'Borne Wi-Fi', 'Box internet (FAI)', 'Pont Wi-Fi')
_TYPE_UPS = 'Onduleur / UPS'

_WORKERS_DEFAUT = 8
_TIMEOUT_COL = 1.5

# ─── Cache mémoire des derniers relevés (pour releve_frais / la vue baie) ─────
_cache: dict[str, 'ReleveEquipement'] = {}
_cache_lock = threading.Lock()


@dataclass
class ReleveEquipement:
    """Relevé SNMP d'un équipement. `equipement` est la forme compat
    `network_diag.interroger_equipement` (None si rien de lisible)."""
    ip: str
    appareil_id: int | None = None
    type_appareil: str = ''
    joignable: bool = False          # l'agent SNMP a répondu à *quelque chose*
    snmp_ok: bool = False            # une communauté / v3 exploitable
    motif: str = ''                  # pourquoi c'est muet, le cas échéant
    equipement: dict | None = None   # {sysname, ts, ports[], hc}
    ts: float = 0.0
    interfaces: dict = field(default_factory=dict)   # {ifIndex: {nom,alias,ethernet,speed_mbps,oper,admin}}


@dataclass
class ResultatBalayage:
    releves: dict[str, ReleveEquipement] = field(default_factory=dict)
    muets: list[dict] = field(default_factory=list)   # [{ip, detail}]
    duree_s: float = 0.0
    budget_atteint: bool = False


def _i(d, k, defaut=0):
    try:
        return int(d.get(k))
    except (TypeError, ValueError, AttributeError):
        return defaut


def _assembler(ip: str, data: dict, besoin_dot3: bool) -> tuple[dict, dict]:
    """`data` = sortie `_snmp_bulk_cols`. Retourne `(equipement, interfaces)` où
    `equipement` a la forme de `interroger_equipement` et `interfaces` est
    indexé par ifIndex (utile à la topologie, qui n'a plus besoin de
    `_noms_interfaces`)."""
    descr = data.get(_IF_DESCR, {})
    types = data.get(_IF_TYPE, {})
    noms = data.get(_IF_NAME, {})
    alias = data.get(_IF_ALIAS, {})
    oper = data.get(_IF_OPER, {})
    admin = data.get(_IF_ADMIN, {})
    speed = data.get(_IF_SPEED, {})
    highspeed = data.get(_IF_HIGHSPEED, {})
    in_err, out_err = data.get(_IF_IN_ERRORS, {}), data.get(_IF_OUT_ERRORS, {})
    in_disc, out_disc = data.get(_IF_IN_DISCARDS, {}), data.get(_IF_OUT_DISCARDS, {})
    hcin, hcout = data.get(_IF_HCIN, {}), data.get(_IF_HCOUT, {})
    in_oct = hcin or data.get(_IF_IN_OCTETS, {})
    out_oct = hcout or data.get(_IF_OUT_OCTETS, {})
    align, fcs = data.get(_DOT3_ALIGN, {}), data.get(_DOT3_FCS, {})
    late, exc, duplex = (data.get(_DOT3_LATECOLL, {}), data.get(_DOT3_EXCCOLL, {}),
                         data.get(_DOT3_DUPLEX, {}))

    interfaces = {}
    ports = []
    for suf in set(descr) | set(noms) | set(types):
        try:
            ifx = int(str(suf).split('.')[0])
        except (TypeError, ValueError):
            continue
        t = _i(types, suf, None) if suf in types else None
        sp = _i(highspeed, suf) or (_i(speed, suf) // 1_000_000)
        ethernet = (t is None) or (t in _IFTYPE_ETHERNET)
        o, a = _i(oper, suf, 1), _i(admin, suf, 1)
        interfaces[ifx] = {
            'nom': str(noms.get(suf) or descr.get(suf) or f'if{ifx}'),
            'alias': str(alias.get(suf) or ''),
            'ethernet': ethernet, 'speed_mbps': sp, 'oper': o, 'admin': a,
        }
        # Le palier 3 ne s'intéresse qu'aux ports ethernet physiques, hors
        # admin-down (exactement les filtres de interroger_equipement).
        if not ethernet or a == 2:
            continue
        ports.append({
            'index': ifx,
            'nom': interfaces[ifx]['nom'],
            'alias': interfaces[ifx]['alias'],
            'oper': o, 'admin': a, 'speed_mbps': sp,
            'in_oct': _i(in_oct, suf), 'out_oct': _i(out_oct, suf),
            'in_err': _i(in_err, suf), 'out_err': _i(out_err, suf),
            'in_disc': _i(in_disc, suf), 'out_disc': _i(out_disc, suf),
            'align_err': _i(align, suf), 'fcs_err': _i(fcs, suf),
            'late_coll': _i(late, suf), 'exc_coll': _i(exc, suf),
            'duplex': _i(duplex, suf),
        })
    equipement = {'sysname': '', 'ts': time.time(), 'ports': ports, 'hc': bool(hcin)}
    return equipement, interfaces


def _collecter_un(ip: str, communautes, besoins: frozenset, deadline: float) -> ReleveEquipement:
    r = ReleveEquipement(ip=ip)
    try:
        from app import _snmp_presence
        present, exploitable, motif = _snmp_presence(ip, communautes)
    except Exception:
        present, exploitable, motif = False, False, 'sonde SNMP indisponible'
    r.joignable, r.snmp_ok, r.motif = present, exploitable, motif
    if not exploitable:
        return r
    if deadline and time.time() > deadline:
        r.motif = 'budget de balayage atteint avant le relevé'
        r.snmp_ok = False
        return r

    cols = list(_COLS_BASE)
    if 'compteurs' in besoins:
        cols += list(_COLS_COMPTEURS)
    besoin_dot3 = 'dot3' in besoins
    if besoin_dot3:
        cols += list(_COLS_DOT3)
    cols.append(_SYS_UPTIME_B)

    try:
        from app import _snmp_bulk_cols
        data = _snmp_bulk_cols(ip, cols, communautes, timeout=_TIMEOUT_COL) or {}
    except Exception:
        logger.debug('netdiag.collect: GETBULK %s en échec', ip, exc_info=True)
        data = {}
    if not data.get(_IF_DESCR) and not data.get(_IF_NAME):
        r.snmp_ok = False
        r.motif = motif or 'SNMP lisible mais aucune interface exposée'
        return r

    equipement, interfaces = _assembler(ip, data, besoin_dot3)
    r.interfaces = interfaces
    if 'sysinfo' in besoins or 'compteurs' in besoins:
        try:
            from app import _snmp_get_typed
            si = _snmp_get_typed(ip, [_SYS_NAME, _SYS_DESCR], communautes[0] if communautes else 'public',
                                 timeout=1.0) or {}
            equipement['sysname'] = str(si.get(_SYS_NAME, '') or '')
        except Exception:
            pass
    r.equipement = equipement
    r.ts = equipement['ts']
    return r


def balayer(client_id: int, *, besoins=('compteurs',), budget_s: float = 0.0,
            communautes=None, equipements=None, workers: int | None = None,
            inclure_ups: bool = False) -> ResultatBalayage:
    """Balaye en parallèle tous les équipements SNMP du client.

    `besoins` ⊆ {'compteurs', 'dot3', 'sysinfo'} — quelles colonnes relever.
    `budget_s` : plafond de durée de la passe (0 = illimité). Les équipements non
    terminés partent dans `muets` avec le motif — jamais un balayage sauté en
    silence.
    `equipements` : liste `[(appareil_id, ip, type_appareil)]` déjà résolue
    (sinon lue depuis l'inventaire). `inclure_ups` : garder les onduleurs.
    """
    t0 = time.time()
    besoins = frozenset(besoins)
    res = ResultatBalayage()
    if communautes is None:
        try:
            import network_diag
            communautes = network_diag._communautes_snmp()
        except Exception:
            communautes = ['public']

    if equipements is None:
        try:
            from database import get_db
            conn = get_db()
            ph = ','.join('?' * len(_TYPES_EQUIP_SNMP))
            rows = conn.execute(
                f"SELECT id, adresse_ip, type_appareil FROM appareils WHERE client_id=? "
                f"AND type_appareil IN ({ph}) AND adresse_ip!='' AND adresse_ip IS NOT NULL "
                f"ORDER BY id", (client_id, *_TYPES_EQUIP_SNMP)).fetchall()
            conn.close()
            equipements = [(r[0], str(r[1]).strip(), r[2]) for r in rows]
        except Exception:
            logger.debug('netdiag.collect: lecture inventaire impossible', exc_info=True)
            equipements = []

    cibles = [(aid, ip, ta) for aid, ip, ta in equipements
              if ip and (inclure_ups or ta != _TYPE_UPS)]
    # dédup par IP (un même switch peut être dans plusieurs slots de baie)
    vus, uniques = set(), []
    for aid, ip, ta in cibles:
        if ip in vus:
            continue
        vus.add(ip)
        uniques.append((aid, ip, ta))
    if not uniques:
        res.duree_s = round(time.time() - t0, 2)
        return res

    if workers is None:
        try:
            import network_diag
            workers = network_diag._cfg_int('diag_snmp_workers', _WORKERS_DEFAUT)
        except Exception:
            workers = _WORKERS_DEFAUT
    workers = max(1, min(workers, len(uniques)))
    deadline = (t0 + budget_s) if budget_s else 0.0

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers,
                                                 thread_name_prefix='DiagCollect')
    futurs = {pool.submit(_collecter_un, ip, communautes, besoins, deadline): (aid, ip, ta)
              for aid, ip, ta in uniques}
    try:
        restant = (deadline - time.time()) if deadline else None
        try:
            for fut in concurrent.futures.as_completed(futurs, timeout=restant):
                aid, ip, ta = futurs[fut]
                try:
                    rv = fut.result()
                except Exception:
                    logger.debug('netdiag.collect: %s en échec', ip, exc_info=True)
                    continue
                rv.appareil_id, rv.type_appareil = aid, ta
                res.releves[ip] = rv
                if not rv.snmp_ok:
                    res.muets.append({'ip': ip, 'detail': rv.motif or 'sans réponse SNMP'})
        except concurrent.futures.TimeoutError:
            res.budget_atteint = True
            deja = {m['ip'] for m in res.muets}
            for f, (aid, ip, ta) in futurs.items():
                if not f.done() and ip not in deja:
                    res.muets.append({'ip': ip, 'detail': 'budget de balayage atteint pendant le relevé'})
    finally:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:      # Python 3.8
            pool.shutdown(wait=False)

    with _cache_lock:
        for ip, rv in res.releves.items():
            _cache[ip] = rv
    res.duree_s = round(time.time() - t0, 2)
    return res


def releve_frais(ip: str, max_age: float = 12.0) -> ReleveEquipement | None:
    """Dernier relevé de `ip` s'il a moins de `max_age` secondes — pour un
    consommateur temps réel (vue d'activité baie) qui ne veut pas re-poller."""
    with _cache_lock:
        rv = _cache.get(ip)
    if rv and rv.ts and (time.time() - rv.ts) <= max_age:
        return rv
    return None


def vider_cache():
    with _cache_lock:
        _cache.clear()
