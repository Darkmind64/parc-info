"""netdiag.dhcp — baux DHCP du routeur / serveur DHCP du client (Plan 1).

Lit la table des baux **directement à la source** — le routeur / serveur DHCP —
et la croise avec le scan et l'inventaire. La source connaît de façon fiable,
pour tout son périmètre : l'association IP ↔ MAC ↔ hostname (option 12 annoncée
par le client), les sous-réseaux routés, et surtout la distinction **bail
dynamique / réservation statique** — une imprimante ou un serveur en bail
dynamique alors qu'il devrait être réservé est un signal utile.

Sources (best-effort, on garde tout ce qui répond) :

- **SNMP** — Mikrotik `mtxrDHCPLeaseTable` (`.1.3.6.1.4.1.14988.1.1.6`), très
  répandu en PME. Colonnes hétérogènes selon la version de RouterOS : on ne
  s'appuie que sur les deux stables (adresse `.2`, MAC `.3`) et on lit les
  autres en best-effort.
- **Fichier importé** — `dhcpd.leases` (ISC : pfSense, OPNsense, ISC dhcpd),
  `dhcp.leases` dnsmasq (OpenWrt), export CSV Windows Server
  (`Get-DhcpServerv4Lease | Export-Csv`). Parseurs **purs**, auto-détection du
  format.

Le relevé SSH (Mikrotik / OpenWrt / pfSense en direct) est laissé à un lot
ultérieur — il demande une dépendance SSH (`paramiko`), décision à trancher.

Ce module contient : les **parseurs purs** + le **relevé SNMP** + les
**helpers base** (`importer_baux`, `lister_baux`, `baux_hors_inventaire`,
`bail_pour_mac`). L'orchestration périodique (`_dhcp_releve_periodique`) et les
routes vivent dans `app.py`.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
from datetime import datetime, timezone

logger = logging.getLogger('parcinfo')

# ── Types de bail ───────────────────────────────────────────────────────────
DYNAMIQUE = 'dynamique'
STATIQUE = 'statique'
INCONNU = 'inconnu'

_RE_MAC = re.compile(r'([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}')
_RE_MAC_NUE = re.compile(r'^[0-9A-Fa-f]{12}$')


def _norm_mac(brut) -> str:
    """→ `aa:bb:cc:dd:ee:ff` minuscule, ou '' si non exploitable.

    Même forme que `appareils.adresse_mac` (séparateur `:`), pour que les
    recoupements par MAC fonctionnent sans normalisation supplémentaire."""
    if not brut:
        return ''
    s = str(brut).strip().lower().replace('-', ':').replace('.', ':').replace(' ', '')
    hexs = re.sub(r'[^0-9a-f]', '', s)
    if len(hexs) != 12:
        return ''
    return ':'.join(hexs[i:i + 2] for i in range(0, 12, 2))


def _ip_valide(s) -> str:
    try:
        import ipaddress
        return str(ipaddress.ip_address(str(s).strip()))
    except (ValueError, TypeError):
        return ''


def _bail(ip='', mac='', hostname='', type_bail=INCONNU, debut='', expiration='',
          source_methode='') -> dict:
    return {'ip': _ip_valide(ip), 'mac': _norm_mac(mac),
            'hostname': (hostname or '').strip()[:120], 'type': type_bail,
            'debut': (debut or '').strip(), 'expiration': (expiration or '').strip(),
            'source_methode': source_methode}


def _fusionner(baux: list) -> list:
    """Dédoublonne sur (ip, mac) en gardant la ligne la plus informative
    (type connu > hostname non vide > date d'expiration)."""
    par_cle: dict = {}
    for b in baux:
        if not b['ip'] or not b['mac']:
            continue
        cle = (b['ip'], b['mac'])
        anc = par_cle.get(cle)
        if anc is None:
            par_cle[cle] = b
            continue
        score = (b['type'] != INCONNU, bool(b['hostname']), bool(b['expiration']))
        score_anc = (anc['type'] != INCONNU, bool(anc['hostname']), bool(anc['expiration']))
        if score > score_anc:
            par_cle[cle] = b
    return list(par_cle.values())


# ════════════════════════════════════════════════════════════════════════════
#  Parseurs de fichiers — PURS (aucune base, aucun réseau)
# ════════════════════════════════════════════════════════════════════════════

_RE_ISC_LEASE = re.compile(r'lease\s+(\d+\.\d+\.\d+\.\d+)\s*\{([^}]*)\}', re.S)
_RE_ISC_HOST = re.compile(r'host\s+([^\s{]+)\s*\{([^}]*)\}', re.S)


def _iso(dt_str: str) -> str:
    """`3 2024/01/10 18:30:00` (ISC, UTC) ou `2024/01/10 18:30:00` → ISO."""
    m = re.search(r'(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})', dt_str or '')
    if not m:
        return ''
    y, mo, d, h, mi, s = m.groups()
    return '%s-%s-%sT%s:%s:%s' % (y, mo, d, h, mi, s)


def _parse_isc_leases(texte: str) -> list:
    """`dhcpd.leases` ISC (pfSense, OPNsense, ISC dhcpd). Les blocs `lease` ne
    portent que des baux **dynamiques** ; les `host { fixed-address }` (souvent
    dans le même fichier chez pfSense, ou dans `dhcpd.conf`) sont **statiques**.
    Un bail `binding state free/expired/released` est ignoré (plus actif).
    Sur plusieurs blocs pour la même IP, le dernier gagne (ordre du fichier)."""
    texte = texte or ''
    out: list = []
    par_ip: dict = {}
    for ip, corps in _RE_ISC_LEASE.findall(texte):
        etat = re.search(r'binding state\s+(\w+)', corps)
        if etat and etat.group(1).lower() != 'active':
            par_ip.pop(ip, None)
            continue
        mac = re.search(r'hardware ethernet\s+([0-9a-fA-F:]+)', corps)
        host = re.search(r'client-hostname\s+"([^"]*)"', corps)
        starts = re.search(r'starts\s+(.+?);', corps)
        ends = re.search(r'ends\s+(.+?);', corps)
        fin = ''
        if ends:
            fin = 'never' if 'never' in ends.group(1) else _iso(ends.group(1))
        par_ip[ip] = _bail(ip=ip, mac=mac.group(1) if mac else '',
                           hostname=host.group(1) if host else '',
                           type_bail=DYNAMIQUE,
                           debut=_iso(starts.group(1)) if starts else '',
                           expiration=fin, source_methode='fichier:isc')
    out.extend(par_ip.values())
    for nom, corps in _RE_ISC_HOST.findall(texte):
        mac = re.search(r'hardware ethernet\s+([0-9a-fA-F:]+)', corps)
        fixed = re.search(r'fixed-address\s+([0-9.]+)', corps)
        if not mac or not fixed:
            continue
        hn = re.search(r'ddns-hostname\s+"([^"]*)"', corps)
        out.append(_bail(ip=fixed.group(1), mac=mac.group(1),
                         hostname=(hn.group(1) if hn else nom),
                         type_bail=STATIQUE, source_methode='fichier:isc'))
    return _fusionner(out)


def _parse_dnsmasq_leases(texte: str) -> list:
    """`/tmp/dhcp.leases` dnsmasq (OpenWrt, LEDE, certains NAS). Une ligne par
    bail : `<expiry_epoch> <mac> <ip> <hostname|*> <client-id|*>`.
    `expiry == 0` → bail « infini » = réservation statique."""
    out: list = []
    for ligne in (texte or '').splitlines():
        p = ligne.split()
        if len(p) < 4 or not p[0].isdigit():
            continue
        exp_epoch = int(p[0])
        mac, ip, host = p[1], p[2], p[3]
        exp = ''
        if exp_epoch > 0:
            try:
                exp = datetime.fromtimestamp(exp_epoch, timezone.utc).replace(
                    tzinfo=None).isoformat(timespec='seconds')
            except (OverflowError, OSError, ValueError):
                exp = ''
        out.append(_bail(ip=ip, mac=mac, hostname='' if host == '*' else host,
                         type_bail=STATIQUE if exp_epoch == 0 else DYNAMIQUE,
                         expiration=exp, source_methode='fichier:dnsmasq'))
    return _fusionner(out)


def _parse_windows_dhcp_csv(texte: str) -> list:
    """Export `Get-DhcpServerv4Lease [-AllLeases] | Export-Csv`. Colonnes utiles :
    `IPAddress`, `ClientId` (MAC `aa-bb-cc-dd-ee-ff`), `HostName`,
    `LeaseExpiryTime`, `AddressState` (`Active`, `ActiveReservation`,
    `InactiveReservation`, `Declined`, `Expired`…). Un `#TYPE …` en 1ʳᵉ ligne
    (ajouté par les vieux PowerShell) est ignoré."""
    texte = (texte or '').lstrip('﻿')
    lignes = texte.splitlines()
    if lignes and lignes[0].startswith('#TYPE'):
        lignes = lignes[1:]
    if not lignes:
        return []
    out: list = []
    try:
        rdr = csv.DictReader(io.StringIO('\n'.join(lignes)))
        for row in rdr:
            low = {(k or '').strip().lower(): (v or '').strip() for k, v in row.items()}
            ip = low.get('ipaddress') or low.get('ip')
            mac = low.get('clientid') or low.get('mac') or low.get('macaddress')
            etat = (low.get('addressstate') or '').lower()
            if etat in ('declined',) or (etat.startswith('inactive') and 'reservation' not in etat):
                continue
            type_bail = STATIQUE if 'reservation' in etat else (
                DYNAMIQUE if etat else INCONNU)
            out.append(_bail(ip=ip, mac=mac, hostname=low.get('hostname', ''),
                             type_bail=type_bail,
                             expiration=low.get('leaseexpirytime', ''),
                             source_methode='fichier:windows'))
    except (csv.Error, ValueError):
        return []
    return _fusionner(out)


def parser_auto(texte: str) -> tuple:
    """Devine le format et parse. Retourne `(baux, format)` ; `format` ∈
    `isc` | `dnsmasq` | `windows` | `''` (non reconnu)."""
    t = texte or ''
    tete = t[:4000].lower()
    if 'ipaddress' in tete and ('clientid' in tete or 'addressstate' in tete):
        return _parse_windows_dhcp_csv(t), 'windows'
    if re.search(r'^\s*lease\s+\d+\.\d+\.\d+\.\d+\s*\{', t, re.M) or 'binding state' in tete:
        return _parse_isc_leases(t), 'isc'
    # dnsmasq : lignes « epoch mac ip host » — au moins une ligne plausible
    for ligne in t.splitlines()[:50]:
        p = ligne.split()
        if len(p) >= 4 and p[0].isdigit() and _norm_mac(p[1]) and _ip_valide(p[2]):
            return _parse_dnsmasq_leases(t), 'dnsmasq'
    if _RE_ISC_HOST.search(t):
        return _parse_isc_leases(t), 'isc'
    return [], ''


# ════════════════════════════════════════════════════════════════════════════
#  Relevé SNMP — Mikrotik
# ════════════════════════════════════════════════════════════════════════════

# mtxrDHCPLeaseTable = .1.3.6.1.4.1.14988.1.1.6.2.1
_MT_LEASE_ENTRY = '1.3.6.1.4.1.14988.1.1.6.2.1'
_MT_LEASE_ADDR = _MT_LEASE_ENTRY + '.2'      # IpAddress  — stable
_MT_LEASE_MAC = _MT_LEASE_ENTRY + '.3'       # PhysAddress — stable
_MT_LEASE_SRV = _MT_LEASE_ENTRY + '.4'       # ServerName  — best-effort
_MT_LEASE_EXP = _MT_LEASE_ENTRY + '.5'       # secondes restantes — best-effort


def _baux_snmp_mikrotik(ip: str, communautes) -> list:
    """Relevé de la table des baux d'un routeur Mikrotik en SNMP lecture seule.

    On ne s'appuie que sur les deux colonnes stables entre versions de RouterOS
    (adresse `.2`, MAC `.3`, zippées par index de ligne). Le nom du serveur
    (`.4`) et l'expiration relative (`.5`) sont lus en plus si présents. RouterOS
    n'expose pas de façon fiable le hostname ni le caractère statique/dynamique
    dans cette table → `type = inconnu`, `hostname = ''` (le fichier exporté, lui,
    les donne). Renvoie `[]` en cas d'échec (agent muet, table absente)."""
    try:
        from network_diag import _snmp_walk, _snmp_walk_octets
    except Exception:                                        # pragma: no cover
        return []
    try:
        addrs = _snmp_walk(_MT_LEASE_ADDR, ip, communautes, max_vars=4000)
    except Exception:
        return []
    if not addrs:
        return []
    macs_raw = {}
    try:
        macs_raw = _snmp_walk_octets(_MT_LEASE_MAC, ip, communautes, max_rows=4000)
    except Exception:
        macs_raw = {}
    srv = {}
    exp = {}
    try:
        srv = _snmp_walk(_MT_LEASE_SRV, ip, communautes, max_vars=4000)
        exp = _snmp_walk(_MT_LEASE_EXP, ip, communautes, max_vars=4000)
    except Exception:
        pass

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out: list = []
    for idx, ipv in addrs.items():
        macb = macs_raw.get(idx)
        mac = ''
        if isinstance(macb, (bytes, bytearray)) and len(macb) >= 6:
            mac = ':'.join('%02x' % b for b in bytes(macb)[-6:])
        elif isinstance(macb, str):
            mac = _norm_mac(macb)
        expiration = ''
        try:
            secs = int(exp.get(idx) or 0)
            if secs > 0:
                from datetime import timedelta
                expiration = (now + timedelta(seconds=secs)).isoformat(timespec='seconds')
        except (TypeError, ValueError):
            pass
        out.append(_bail(ip=ipv, mac=mac,
                         hostname=str(srv.get(idx) or '').strip(),
                         type_bail=INCONNU, expiration=expiration,
                         source_methode='snmp:mikrotik'))
    return _fusionner(out)


def relever_baux(conn, client_id: int, *, budget_s: float = 20.0) -> dict:
    """Interroge chaque routeur / box FAI / pare-feu de l'inventaire du client
    (SNMP), agrège les baux. Retourne
    `{'baux': [...], 'sources': [{ip, appareil_id, methode, nb, motif}], 'ok': bool}`.
    Best-effort : jamais d'exception propagée, s'arrête à `budget_s`."""
    deadline = time.monotonic() + max(3.0, budget_s)
    try:
        from network_diag import _communautes_snmp
        communautes = _communautes_snmp()
    except Exception:
        communautes = ['public']
    try:
        rows = conn.execute(
            "SELECT id, adresse_ip FROM appareils WHERE client_id=? "
            "AND type_appareil IN ('Routeur/Pare-feu','Box internet (FAI)') "
            "AND COALESCE(adresse_ip,'')<>'' ORDER BY id", (int(client_id),)).fetchall()
    except Exception:
        return {'baux': [], 'sources': [], 'ok': False}

    tous: list = []
    sources: list = []
    for aid, ip in rows:
        if time.monotonic() > deadline:
            sources.append({'ip': ip, 'appareil_id': aid, 'methode': '',
                            'nb': 0, 'motif': 'budget dépassé'})
            continue
        ip = str(ip).strip()
        baux = []
        try:
            baux = _baux_snmp_mikrotik(ip, communautes)
        except Exception:
            logger.debug('dhcp: relevé SNMP %s en échec', ip, exc_info=True)
        for b in baux:
            b['source_equipement_id'] = aid
        tous.extend(baux)
        sources.append({'ip': ip, 'appareil_id': aid,
                        'methode': 'snmp:mikrotik' if baux else '',
                        'nb': len(baux),
                        'motif': '' if baux else 'aucun bail (pas un Mikrotik, SNMP refusé, ou table vide)'})
    return {'baux': _fusionner(tous), 'sources': sources, 'ok': True}


# ════════════════════════════════════════════════════════════════════════════
#  Persistance + recoupements
# ════════════════════════════════════════════════════════════════════════════

def importer_baux(conn, client_id: int, baux: list, *, source_methode: str = '',
                  source_equipement_id=None, remplacer_source: bool = True) -> dict:
    """Écrit les baux dans `dhcp_baux`. `remplacer_source` : purge d'abord les
    baux de la même `source_methode` pour ce client (relevé complet qui
    remplace le précédent). Retourne `{'ecrits': n, 'supprimes': n}`."""
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
    supprimes = 0
    methodes = {source_methode} if source_methode else {
        b.get('source_methode') for b in baux if b.get('source_methode')}
    if remplacer_source and methodes:
        ph = ','.join('?' * len(methodes))
        cur = conn.execute(
            f"DELETE FROM dhcp_baux WHERE client_id=? AND source_methode IN ({ph})",
            (int(client_id), *methodes))
        supprimes = cur.rowcount or 0
    ecrits = 0
    for b in baux:
        ip, mac = b.get('ip'), b.get('mac')
        if not ip or not mac:
            continue
        # ON CONFLICT : on ne REMPLACE un champ que par une valeur plus
        # informative — un relevé SNMP (hostname vide, type « inconnu ») ne doit
        # pas écraser un hostname / un type venus d'un fichier importé.
        conn.execute(
            "INSERT INTO dhcp_baux (client_id, adresse_ip, adresse_mac, hostname, "
            "type, debut, expiration, source_equipement_id, source_methode, vu_le) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(client_id, adresse_ip, adresse_mac) DO UPDATE SET "
            "hostname=COALESCE(NULLIF(excluded.hostname,''), dhcp_baux.hostname), "
            "type=CASE WHEN excluded.type='inconnu' THEN dhcp_baux.type ELSE excluded.type END, "
            "debut=COALESCE(NULLIF(excluded.debut,''), dhcp_baux.debut), "
            "expiration=COALESCE(NULLIF(excluded.expiration,''), dhcp_baux.expiration), "
            "source_equipement_id=COALESCE(excluded.source_equipement_id, dhcp_baux.source_equipement_id), "
            "source_methode=excluded.source_methode, vu_le=excluded.vu_le",
            (int(client_id), ip, mac, b.get('hostname', ''), b.get('type', INCONNU),
             b.get('debut', ''), b.get('expiration', ''),
             b.get('source_equipement_id', source_equipement_id),
             b.get('source_methode') or source_methode, now))
        ecrits += 1
    return {'ecrits': ecrits, 'supprimes': supprimes}


def lister_baux(conn, client_id: int) -> list:
    try:
        rows = conn.execute(
            "SELECT adresse_ip, adresse_mac, hostname, type, debut, expiration, "
            "source_methode, source_equipement_id, vu_le FROM dhcp_baux "
            "WHERE client_id=? ORDER BY adresse_ip", (int(client_id),)).fetchall()
    except Exception:
        return []
    return [{'ip': r[0], 'mac': r[1], 'hostname': r[2], 'type': r[3], 'debut': r[4],
             'expiration': r[5], 'source_methode': r[6], 'source_equipement_id': r[7],
             'vu_le': r[8]} for r in rows]


def bail_pour_mac(conn, client_id: int, mac: str) -> dict | None:
    m = _norm_mac(mac)
    if not m:
        return None
    try:
        r = conn.execute(
            "SELECT adresse_ip, adresse_mac, hostname, type, expiration, source_methode "
            "FROM dhcp_baux WHERE client_id=? AND adresse_mac=? LIMIT 1",
            (int(client_id), m)).fetchone()
    except Exception:
        return None
    if not r:
        return None
    return {'ip': r[0], 'mac': r[1], 'hostname': r[2], 'type': r[3],
            'expiration': r[4], 'source_methode': r[5]}


def baux_hors_inventaire(conn, client_id: int) -> list:
    """Baux (surtout `statique`) dont la MAC n'est dans AUCUN `appareils` ni
    `appareil_macs` du client → appareil « fantôme » à forte confiance (une
    réservation = un appareil qui compte, simplement absent de l'inventaire)."""
    baux = lister_baux(conn, client_id)
    if not baux:
        return []
    try:
        connues = {_norm_mac(r[0]) for r in conn.execute(
            "SELECT adresse_mac FROM appareils WHERE client_id=? AND COALESCE(adresse_mac,'')<>''",
            (int(client_id),))}
        connues |= {_norm_mac(r[0]) for r in conn.execute(
            "SELECT m.adresse_mac FROM appareil_macs m JOIN appareils a ON a.id=m.appareil_id "
            "WHERE a.client_id=?", (int(client_id),))}
    except Exception:
        return []
    connues.discard('')
    return [b for b in baux if b['mac'] not in connues]


def resume_client(conn, client_id: int) -> dict:
    baux = lister_baux(conn, client_id)
    fant = baux_hors_inventaire(conn, client_id)
    return {'total': len(baux),
            'statiques': sum(1 for b in baux if b['type'] == STATIQUE),
            'dynamiques': sum(1 for b in baux if b['type'] == DYNAMIQUE),
            'fantomes': len(fant),
            'sources': sorted({b['source_methode'] for b in baux if b['source_methode']})}
