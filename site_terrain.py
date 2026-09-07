"""Détection de présence sur le site d'un client + « mode terrain ».

Contexte : le prestataire utilise ParcInfo CHEZ le client (scan, câblage baie,
diagnostic), puis repart avec sa machine et s'en sert HORS site (retour au
bureau, instance Docker de consultation à la maison). Les fonctions « live »
(scan réseau, pré-chauffe de la baie, diagnostic SNMP, cartographie) ne doivent
alors pas :
  - importer le réseau perso de l'utilisateur dans l'inventaire du client ;
  - remplacer des données par ce qu'elles voient sur un autre réseau.

Principe de détection (même idée que l'auto-détection du client dans le
collecteur) : comparer la table ARP/voisins de CETTE machine à l'inventaire de
TOUS les clients. Une adresse MAC est globalement unique — retrouver plusieurs
MAC du client X (ou la MAC de sa passerelle) = on est sur son site.
"""
import logging
import os
import re
import time

logger = logging.getLogger('parcinfo')

_MAC_OK = re.compile(r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$')
_IPV4 = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')

_GRACE_S = 7200.0          # 2 h : un site confirmé le reste malgré une coupure réseau
_CACHE_S = 120.0          # ré-évaluation de la détection (arp -a est un sous-process)
_IP_PUB_TTL = 600.0

_grace = {}               # client_id -> epoch de la dernière confirmation « sur site »
_cache = {'ts': 0.0, 'res': None}
_ip_pub = {'ts': 0.0, 'ip': ''}


def _norm(m):
    m = (m or '').lower().replace('-', ':').strip()
    return m if _MAC_OK.match(m) else ''


def mode_terrain() -> str:
    """`terrain` (force « sur site », aucune détection) · `consultation` (jamais
    de live) · `auto` (détection). Défaut : `consultation` en Docker, `auto`
    sinon — une instance Docker sert typiquement à consulter depuis chez soi."""
    try:
        from config_helpers import cfg_get
        v = (cfg_get('mode_terrain', '') or '').strip().lower()
    except Exception:
        v = ''
    if v in ('terrain', 'consultation', 'auto'):
        return v
    return 'consultation' if os.environ.get('RUNNING_IN_DOCKER') else 'auto'


def _arp_local():
    try:
        from network_diag import _table_arp
        return _table_arp() or {}
    except Exception:
        logger.debug('site_terrain: lecture ARP impossible', exc_info=True)
        return {}


def _passerelle_mac(arp):
    try:
        from network_diag import _passerelle_defaut
        gw = _passerelle_defaut()
    except Exception:
        gw = ''
    if not gw:
        return ''
    for m in arp.get(gw, ()):
        n = _norm(m)
        if n:
            return n
    return ''


def _ip_publique():
    """IP publique de sortie de ce poste (cache 10 min). '' si indisponible ou
    si la vérification est désactivée."""
    try:
        from config_helpers import cfg_get
        if str(cfg_get('mode_terrain_ip_publique', '1')) != '1':
            return ''
    except Exception:
        pass
    now = time.time()
    if _ip_pub['ip'] and now - _ip_pub['ts'] < _IP_PUB_TTL:
        return _ip_pub['ip']
    for url in ('https://api.ipify.org', 'https://ifconfig.me/ip'):
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=3) as r:
                ip = r.read().decode('utf-8', 'ignore').strip()
            if _IPV4.match(ip):
                _ip_pub.update(ts=now, ip=ip)
                return ip
        except Exception:
            continue
    return ''


def detecter_site(conn, force: bool = False) -> dict:
    """Où suis-je ? Retourne :
      {mode, client_id, nom, confiance, macs_reconnues, total_arp,
       passerelle_ok, ip_publique_ok}
    `confiance` ∈ 'sur_site' | 'probable' | 'indetermine'. Caché ~2 min."""
    mode = mode_terrain()
    now = time.time()
    if not force and _cache['res'] and now - _cache['ts'] < _CACHE_S:
        return dict(_cache['res'])
    res = {'mode': mode, 'client_id': None, 'nom': '', 'confiance': 'indetermine',
           'macs_reconnues': 0, 'total_arp': 0, 'passerelle_ok': False,
           'ip_publique_ok': False}
    if mode == 'consultation':
        _cache.update(ts=now, res=res)
        return dict(res)
    if mode == 'terrain':
        res['confiance'] = 'sur_site'
        _cache.update(ts=now, res=res)
        return dict(res)

    arp = _arp_local()
    macs_vues = {n for s in arp.values() for n in (_norm(x) for x in s) if n}
    res['total_arp'] = len(macs_vues)
    gw_mac = _passerelle_mac(arp)

    par_client, noms = {}, {}
    try:
        for req in (
                "SELECT client_id, adresse_mac FROM appareils WHERE COALESCE(adresse_mac,'')<>''",
                "SELECT client_id, adresse_mac FROM appareil_macs WHERE COALESCE(adresse_mac,'')<>''"):
            for cid, mac in conn.execute(req):
                m = _norm(mac)
                if m:
                    par_client.setdefault(m, set()).add(cid)
        noms = {r[0]: r[1] for r in conn.execute("SELECT id, nom FROM clients")}
    except Exception:
        logger.debug('site_terrain: lecture inventaire impossible', exc_info=True)
        _cache.update(ts=now, res=res)
        return dict(res)

    score = {}
    for m in macs_vues:
        for cid in par_client.get(m, ()):
            score[cid] = score.get(cid, 0) + 1
    gw_client = None
    if gw_mac:
        for cid in par_client.get(gw_mac, ()):
            gw_client, score[cid] = cid, score.get(cid, 0) + 3

    if score:
        cid = max(score, key=score.get)
        n = sum(1 for m in macs_vues if cid in par_client.get(m, ()))
        res.update(client_id=cid, nom=noms.get(cid, ''), macs_reconnues=n,
                   passerelle_ok=(gw_client == cid))
        top = score[cid]
        second = max((v for c, v in score.items() if c != cid), default=0)
        if (top >= 3 and top >= second + 2) or (res['passerelle_ok'] and n >= 1):
            res['confiance'] = 'sur_site'
        elif n >= 1:
            res['confiance'] = 'probable'

    # Repli / renfort : IP publique déclarée du parc (utile quand l'ARP n'a rien
    # reconnu — VLAN isolé, tout en DHCP, peu d'appareils inventoriés).
    if res['confiance'] != 'sur_site':
        pub = _ip_publique()
        if pub:
            try:
                for cid, ipp in conn.execute(
                        "SELECT client_id, ip_publique FROM parc_general "
                        "WHERE COALESCE(ip_publique,'')<>''"):
                    if (ipp or '').strip() == pub:
                        res['ip_publique_ok'] = True
                        if res['client_id'] in (None, cid):
                            res.update(client_id=cid, nom=noms.get(cid, ''),
                                       confiance='sur_site' if res['confiance'] == 'probable'
                                       or res['client_id'] == cid else 'probable')
                        break
            except Exception:
                pass

    if res['confiance'] == 'sur_site' and res['client_id']:
        _grace[res['client_id']] = now
    _cache.update(ts=now, res=res)
    return dict(res)


def site_actif(conn, client_id: int) -> bool:
    """Peut-on opérer en LIVE sur ce client maintenant ? (détection « sur_site »,
    fenêtre de grâce de 2 h après une confirmation, ou mode `terrain` forcé)."""
    m = mode_terrain()
    if m == 'terrain':
        return True
    if m == 'consultation':
        return False
    if time.time() - _grace.get(client_id, 0) < _GRACE_S:
        return True
    d = detecter_site(conn)
    return d['client_id'] == client_id and d['confiance'] == 'sur_site'


def clients_sur_site(conn):
    """Ids des clients dont le site est actif (grâce comprise). `None` = « on ne
    peut rien affirmer » (aucune détection, aucune grâce) → l'appelant garde son
    comportement historique. `set()` = consultation stricte."""
    m = mode_terrain()
    if m == 'consultation':
        return set()
    d = detecter_site(conn)      # rafraîchit _grace
    if m == 'terrain':
        try:
            return {r[0] for r in conn.execute("SELECT id FROM clients")}
        except Exception:
            return None
    now = time.time()
    ids = {c for c, t in _grace.items() if now - t < _GRACE_S}
    if ids:
        return ids
    # rien de confirmé : si la détection est franchement muette (aucune MAC
    # reconnue nulle part), on ne bride pas — sinon on bride à l'ensemble vide.
    return None if d['confiance'] == 'indetermine' and d['macs_reconnues'] == 0 else set()
