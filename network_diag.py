"""
network_diag.py — Module de diagnostic réseau de ParcInfo.

Deux paliers :

  • Palier 1 — diagnostic *actif*, aucune dépendance : réutilise l'infrastructure
    socket/subprocess déjà présente dans app.py (ping, ARP, NetBIOS…). Détecte les
    conflits d'adresses IP, la qualité de liaison dégradée (perte / gigue /
    latence), la joignabilité passerelle/DNS, les conflits de noms, et fait une
    tentative best-effort de repérage d'un serveur DHCP pirate.

  • Palier 2 — capture *passive* de trames via ``scapy`` (OFF par défaut,
    ``diag_capture_active``). Nécessite des privilèges + un pilote de capture
    (Npcap sous Windows, libpcap ailleurs). Détecte l'ARP spoofing / ARP gratuits
    en rafale, le MAC flapping, les tempêtes de broadcast, la présence de
    plusieurs serveurs DHCP, les BPDU STP en rafale, les Router Advertisements
    IPv6 pirates et les retransmissions TCP.

Modes : snapshot à la demande (``run_snapshot``) et surveillance continue
(``_moniteur_loop``, thread démon démarré à l'import — même schéma que le
watchdog ping d'app.py). Les évènements sont historisés dans
``diag_reseau_evenements`` et dédoublonnés par ``signature``.

Aucun import de ``app`` au niveau module (import circulaire) : les helpers d'app
sont importés paresseusement dans les fonctions qui en ont besoin.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import platform
import re
import socket
import struct
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger('parcinfo')

IS_WINDOWS = platform.system() == 'Windows'

# ─── Gravité par défaut selon la catégorie ───────────────────────────────────
_GRAVITE_DEFAUT = {
    'conflit_ip':            'critique',
    'arp_spoofing':          'critique',
    'dhcp_pirate':           'critique',
    'ra_pirate':             'critique',
    'mac_flapping':          'avertissement',
    'tempete_broadcast':     'avertissement',
    'qualite_liaison':       'avertissement',
    'conflit_nom':           'avertissement',
    'stp_instable':          'avertissement',
    'passerelle_injoignable':'critique',
    'dns_degrade':           'avertissement',
    'tcp_retransmissions':   'info',
    'duplex_mismatch':       'critique',
    'port_crc':              'avertissement',
    'port_erreurs':          'avertissement',
    'port_sature':           'avertissement',
    'port_flapping':         'avertissement',
    'vitesse_reduite':       'info',
    'cablage_incoherent':    'avertissement',
    'degradation_relative':  'avertissement',
}

_CATEGORIES_LIBELLES = {
    'conflit_ip':             "Conflit d'adresse IP",
    'arp_spoofing':           "ARP spoofing / usurpation",
    'dhcp_pirate':            "Serveur DHCP non autorisé",
    'ra_pirate':              "Router Advertisement IPv6 non autorisé",
    'mac_flapping':           "MAC instable (flapping)",
    'tempete_broadcast':      "Tempête de broadcast",
    'qualite_liaison':        "Qualité de liaison dégradée",
    'conflit_nom':            "Conflit de nom réseau",
    'stp_instable':           "Topologie STP instable",
    'passerelle_injoignable': "Passerelle injoignable",
    'dns_degrade':            "Résolution DNS dégradée",
    'tcp_retransmissions':    "Retransmissions TCP élevées",
    'duplex_mismatch':        "Duplex mismatch (port)",
    'port_crc':               "Erreurs CRC/FCS en hausse (port)",
    'port_erreurs':           "Erreurs / rejets de paquets (port)",
    'port_sature':            "Lien saturé (port)",
    'port_flapping':          "Port instable (flapping)",
    'vitesse_reduite':        "Vitesse de lien réduite (port)",
    'cablage_incoherent':     "Câblage baie incohérent avec la topologie",
    'degradation_relative':   "Dégradation par rapport à la référence",
}


def libelle_categorie(cat: str) -> str:
    return _CATEGORIES_LIBELLES.get(cat, cat)


# ════════════════════════════════════════════════════════════════════════════
#  Utilitaires
# ════════════════════════════════════════════════════════════════════════════

def _now_z() -> str:
    """Horodatage ISO UTC avec 'Z' explicite (même convention que _watchdog_state)."""
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _cfg(cle: str, defaut=None):
    from config_helpers import cfg_get
    return cfg_get(cle, defaut)


def _cfg_int(cle: str, defaut: int) -> int:
    try:
        return int(float(_cfg(cle, str(defaut))))
    except (TypeError, ValueError):
        return defaut


def _cfg_float(cle: str, defaut: float) -> float:
    try:
        return float(_cfg(cle, str(defaut)))
    except (TypeError, ValueError):
        return defaut


def _signature(categorie: str, *entites) -> str:
    brut = categorie + '|' + '|'.join(sorted(str(e).lower() for e in entites if e))
    return hashlib.sha1(brut.encode('utf-8', 'replace')).hexdigest()[:16]


def _finding(categorie: str, titre: str, details: dict, *entites, gravite: str = None) -> dict:
    return {
        'categorie': categorie,
        'gravite': gravite or _GRAVITE_DEFAUT.get(categorie, 'info'),
        'titre': titre,
        'details': details or {},
        'signature': _signature(categorie, *(entites or details.values())),
    }


def _run(cmd, timeout=6):
    """Exécute une commande sans fenêtre (délègue à app._run_hidden si dispo)."""
    try:
        from app import _run_hidden
        return _run_hidden(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        import subprocess
        kw = {}
        if IS_WINDOWS:
            kw['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


_MAC_RE = re.compile(r'([0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}')
_MAC_NULLES = {'ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00'}


def _norm_mac(mac: str) -> str:
    return (mac or '').replace('-', ':').lower().strip()


def _passerelle_defaut() -> str:
    """Adresse IP de la passerelle par défaut du poste (best-effort)."""
    try:
        if IS_WINDOWS:
            out = _run(['route', 'print', '-4'], timeout=5).stdout
            for ligne in out.splitlines():
                p = ligne.split()
                if len(p) >= 3 and p[0] == '0.0.0.0' and p[1] == '0.0.0.0':
                    try:
                        return str(ipaddress.ip_address(p[2]))
                    except ValueError:
                        continue
        else:
            out = _run(['ip', 'route', 'show', 'default'], timeout=5).stdout
            m = re.search(r'default\s+via\s+([0-9.]+)', out)
            if m:
                return m.group(1)
            # macOS / BSD : pas de commande `ip` — repli portable via netstat
            # (fonctionne aussi sous Linux si `ip` a échoué).
            out = _run(['netstat', '-rn'], timeout=5).stdout
            for ligne in out.splitlines():
                p = ligne.split()
                if len(p) >= 2 and p[0] in ('default', '0.0.0.0'):
                    try:
                        return str(ipaddress.ip_address(p[1]))
                    except ValueError:
                        continue
    except Exception:
        logger.debug('network_diag: passerelle par défaut introuvable', exc_info=True)
    return ''


def _table_arp() -> dict:
    """Retourne {ip: set(mac)} depuis les tables ARP/voisins de l'OS."""
    binding: dict[str, set] = {}

    def _ajouter(ip, mac):
        mac = _norm_mac(mac)
        if not mac or mac in _MAC_NULLES or mac.startswith('01:') or mac.startswith('ff:'):
            return
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            return
        binding.setdefault(ip, set()).add(mac)

    # /proc/net/arp (Linux)
    try:
        with open('/proc/net/arp', 'r') as f:
            for ligne in f.readlines()[1:]:
                p = ligne.split()
                if len(p) >= 4:
                    _ajouter(p[0], p[3])
    except Exception:
        pass

    # arp -a (Windows + macOS)
    try:
        out = _run(['arp', '-a'], timeout=6).stdout
        for ligne in out.splitlines():
            m_ip = re.search(r'(\d{1,3}(?:\.\d{1,3}){3})', ligne)
            m_mac = _MAC_RE.search(ligne)
            if m_ip and m_mac:
                _ajouter(m_ip.group(1), m_mac.group(0))
    except Exception:
        pass

    # ip neigh (Linux moderne)
    try:
        out = _run(['ip', 'neigh', 'show'], timeout=6).stdout
        for ligne in out.splitlines():
            if 'FAILED' in ligne or 'INCOMPLETE' in ligne:
                continue
            m_ip = re.search(r'(\d{1,3}(?:\.\d{1,3}){3})', ligne)
            m_mac = _MAC_RE.search(ligne)
            if m_ip and m_mac:
                _ajouter(m_ip.group(1), m_mac.group(0))
    except Exception:
        pass

    return binding


# ════════════════════════════════════════════════════════════════════════════
#  Palier 1 — diagnostic actif
# ════════════════════════════════════════════════════════════════════════════

def detecter_conflits_ip(passerelle: str = '', releves: int = 2, pause: float = 1.5) -> list:
    """Une IP portant plusieurs MAC = conflit ; une MAC (hors passerelle)
    portant beaucoup d'IP = usurpation probable / proxy ARP.

    Plusieurs relevés espacés : une entrée ARP transitoire (bail DHCP qui vient
    de changer de main) ne suffit pas — il faut voir les deux MAC coexister.
    """
    findings = []
    cumul: dict[str, set] = {}
    inverse: dict[str, set] = {}
    for i in range(max(1, releves)):
        for ip, macs in _table_arp().items():
            cumul.setdefault(ip, set()).update(macs)
            for m in macs:
                inverse.setdefault(m, set()).add(ip)
        if i < releves - 1:
            time.sleep(pause)

    for ip, macs in cumul.items():
        if len(macs) >= 2:
            findings.append(_finding(
                'conflit_ip',
                f"L'adresse {ip} est revendiquée par {len(macs)} machines",
                {'ip': ip, 'macs': sorted(macs), 'fabricants': [_vendor(m) for m in sorted(macs)]},
                ip,
            ))

    passerelle = _norm_mac_pour_ip(passerelle, cumul)
    for mac, ips in inverse.items():
        if mac == passerelle:
            continue
        # Une MAC légitime peut porter 2-3 IP (interface multi-adressée). Au-delà
        # de 5 sur un LAN, c'est un signal d'usurpation ou de proxy ARP.
        if len(ips) >= 6:
            findings.append(_finding(
                'arp_spoofing',
                f"La machine {mac} répond pour {len(ips)} adresses IP",
                {'mac': mac, 'fabricant': _vendor(mac), 'ips': sorted(ips)[:20], 'nb_ips': len(ips)},
                mac,
            ))
    return findings


def _norm_mac_pour_ip(ip: str, table: dict) -> str:
    for m in table.get(ip, ()):
        return m
    return ''


def _vendor(mac: str) -> str:
    try:
        from app import _oui_vendor
        return _oui_vendor(mac) or ''
    except Exception:
        return ''


_RE_TEMPS = re.compile(r'(?:time|temps|tiempo|zeit)[=<]\s*([0-9]+(?:[.,][0-9]+)?)\s*m?s', re.I)


def _ping_rafale(ip: str, n: int = 20) -> dict:
    """Envoie n echo requests et mesure perte / latence / gigue."""
    if IS_WINDOWS:
        cmd = ['ping', '-n', str(n), '-w', '1000', ip]
        timeout = n * 1.3 + 5
    else:
        cmd = ['ping', '-c', str(n), '-i', '0.25', '-W', '1', ip]
        timeout = n * 0.4 + 8
    try:
        out = _run(cmd, timeout=timeout).stdout
    except Exception:
        return {'ip': ip, 'envoyes': n, 'recus': 0, 'perte_pct': 100.0,
                'min': None, 'moy': None, 'max': None, 'gigue': None}

    # _RE_TEMPS capture aussi « temps<1ms » (le « < » est dans la classe [=<]) → 1.0
    rtts = [float(x.replace(',', '.')) for x in _RE_TEMPS.findall(out)]
    recus = len(rtts)
    if recus == 0:
        # dernier recours : compter les lignes de réponse
        recus = len(re.findall(r'(?:Reply from|Réponse de|bytes from|octets de)', out, re.I))
    perte = round(100.0 * (n - recus) / n, 1) if n else 100.0
    stats = {'ip': ip, 'envoyes': n, 'recus': recus, 'perte_pct': max(0.0, perte)}
    if rtts:
        stats['min'] = round(min(rtts), 1)
        stats['max'] = round(max(rtts), 1)
        stats['moy'] = round(sum(rtts) / len(rtts), 1)
        if len(rtts) > 1:
            ecarts = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
            stats['gigue'] = round(sum(ecarts) / len(ecarts), 1)
        else:
            stats['gigue'] = 0.0
    else:
        stats['min'] = stats['moy'] = stats['max'] = stats['gigue'] = None
    return stats


def mesurer_qualite_liaison(cibles: list, seuil_perte: float, seuil_gigue: float,
                            n: int = 20, collecte: list = None) -> list:
    findings = []
    for cible in cibles:
        ip = cible.get('ip') if isinstance(cible, dict) else cible
        libelle = cible.get('libelle', ip) if isinstance(cible, dict) else ip
        if not ip:
            continue
        st = _ping_rafale(ip, n)
        st['libelle'] = libelle
        if collecte is not None:
            collecte.append(st)
        if st['recus'] == 0:
            findings.append(_finding(
                'passerelle_injoignable' if cible.get('role') == 'passerelle' else 'qualite_liaison',
                f"{libelle} ({ip}) ne répond à aucun des {n} paquets",
                st, ip,
                gravite='critique',
            ))
            continue
        problemes = []
        if st['perte_pct'] >= seuil_perte:
            problemes.append(f"{st['perte_pct']} % de perte")
        if st['gigue'] is not None and st['gigue'] >= seuil_gigue:
            problemes.append(f"gigue {st['gigue']} ms")
        if problemes:
            findings.append(_finding(
                'qualite_liaison',
                f"{libelle} ({ip}) : " + ', '.join(problemes),
                st, ip,
            ))
    return findings


def verifier_dns(serveur_dns: str, noms=('www.google.com', 'www.microsoft.com', 'cloudflare.com')) -> list:
    """Résout quelques noms témoins et mesure latence + taux d'échec."""
    if not serveur_dns:
        return []
    findings = []
    echecs, latences = 0, []
    for nom in noms:
        t0 = time.perf_counter()
        ok = _requete_dns_a(serveur_dns, nom, timeout=2.0)
        dt = (time.perf_counter() - t0) * 1000
        if ok:
            latences.append(dt)
        else:
            echecs += 1
    taux = round(100.0 * echecs / len(noms), 0)
    lat_moy = round(sum(latences) / len(latences), 0) if latences else None
    if echecs == len(noms):
        findings.append(_finding(
            'dns_degrade', f"Le serveur DNS {serveur_dns} ne résout aucun nom témoin",
            {'serveur': serveur_dns, 'taux_echec_pct': taux}, serveur_dns, gravite='critique'))
    elif echecs:
        findings.append(_finding(
            'dns_degrade', f"Le serveur DNS {serveur_dns} échoue sur {echecs}/{len(noms)} résolutions",
            {'serveur': serveur_dns, 'taux_echec_pct': taux, 'latence_moy_ms': lat_moy}, serveur_dns))
    elif lat_moy and lat_moy > 300:
        findings.append(_finding(
            'dns_degrade', f"Le serveur DNS {serveur_dns} répond lentement ({lat_moy:.0f} ms en moyenne)",
            {'serveur': serveur_dns, 'latence_moy_ms': lat_moy}, serveur_dns, gravite='info'))
    return findings


def _requete_dns_a(serveur: str, nom: str, timeout: float = 2.0) -> bool:
    """Requête DNS A minimale, construite à la main (pas de dnspython)."""
    try:
        tid = 0x1234
        entete = struct.pack('>HHHHHH', tid, 0x0100, 1, 0, 0, 0)
        q = b''.join(struct.pack('B', len(p)) + p.encode('ascii') for p in nom.split('.')) + b'\x00'
        paquet = entete + q + struct.pack('>HH', 1, 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(paquet, (serveur, 53))
        data, _ = s.recvfrom(2048)
        s.close()
        if len(data) < 12:
            return False
        _, flags, _, ancount = struct.unpack('>HHHH', data[:8])
        return bool(flags & 0x8000) and (flags & 0x000F) == 0 and ancount > 0
    except Exception:
        return False


def detecter_dhcp_pirate(serveurs_attendus: list) -> list:
    """Best-effort : envoie un DHCPDISCOVER et collecte les DHCPOFFER.

    Souvent bloqué sans privilèges (port 68 tenu par le client DHCP de l'OS) —
    la détection *fiable* passe par le palier 2 (capture passive). Retourne une
    liste vide (et journalise) si l'écoute n'a pas pu se faire.
    """
    try:
        mac = _mac_locale()
        xid = os.urandom(4)
        paquet = _construire_dhcp_discover(mac, xid)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind(('', 68))
        except OSError:
            s.close()
            logger.debug('network_diag: port 68 indisponible, détection DHCP palier 1 sautée')
            return []
        s.settimeout(1.0)
        s.sendto(paquet, ('255.255.255.255', 67))
        offres = {}
        fin = time.time() + 4
        while time.time() < fin:
            try:
                data, exp = s.recvfrom(2048)
            except socket.timeout:
                break
            sid = _dhcp_server_id(data, xid)
            if sid:
                offres[sid] = exp[0]
        s.close()
    except Exception:
        logger.debug('network_diag: détection DHCP palier 1 en échec', exc_info=True)
        return []

    attendus = {a.strip() for a in serveurs_attendus if a.strip()}
    findings = []
    for sid in offres:
        if attendus and sid not in attendus:
            findings.append(_finding(
                'dhcp_pirate', f"Un serveur DHCP non déclaré a répondu : {sid}",
                {'serveur': sid, 'attendus': sorted(attendus), 'source': 'discover'}, sid))
    if not attendus and len(offres) >= 2:
        findings.append(_finding(
            'dhcp_pirate', f"{len(offres)} serveurs DHCP répondent sur le réseau",
            {'serveurs': sorted(offres), 'source': 'discover'}, *sorted(offres)))
    return findings


def _mac_locale() -> bytes:
    import uuid
    n = uuid.getnode()
    return n.to_bytes(6, 'big')


def _construire_dhcp_discover(mac: bytes, xid: bytes) -> bytes:
    p = struct.pack('>BBBB', 1, 1, 6, 0)          # op, htype, hlen, hops
    p += xid + struct.pack('>HH', 0, 0x8000)      # secs, flags (broadcast)
    p += b'\x00' * 12                              # ciaddr/yiaddr/siaddr/giaddr
    p += mac + b'\x00' * 10                        # chaddr (16)
    p += b'\x00' * 64 + b'\x00' * 128             # sname + file
    p += bytes([99, 130, 83, 99])                 # magic cookie
    p += bytes([53, 1, 1])                        # option 53 : DHCPDISCOVER
    p += bytes([55, 3, 1, 3, 6])                  # option 55 : requête params
    p += bytes([255])                             # fin
    return p


def _dhcp_server_id(data: bytes, xid: bytes) -> str:
    try:
        if len(data) < 240 or data[0] != 2 or data[4:8] != xid:
            return ''
        opts = data[240:]
        i = 0
        type_msg = None
        server_id = ''
        while i < len(opts):
            code = opts[i]
            if code == 255:
                break
            if code == 0:
                i += 1
                continue
            ln = opts[i + 1]
            val = opts[i + 2:i + 2 + ln]
            if code == 53 and ln == 1:
                type_msg = val[0]
            elif code == 54 and ln == 4:
                server_id = '.'.join(str(b) for b in val)
            i += 2 + ln
        return server_id if type_msg == 2 else ''
    except Exception:
        return ''


def detecter_conflits_noms(client_id: int) -> list:
    """Deux IP vivantes qui annoncent le même nom NetBIOS = conflit de nom."""
    findings = []
    try:
        from database import get_db
        from app import _netbios_name
        conn = get_db()
        rows = conn.execute(
            "SELECT adresse_ip FROM appareils WHERE client_id=? AND adresse_ip!='' "
            "AND adresse_ip IS NOT NULL", (client_id,)).fetchall()
        conn.close()
    except Exception:
        return findings
    ips = [r[0] for r in rows[:60]]

    def _nom(ip):
        try:
            return ip, (_netbios_name(ip) or '').strip().upper()
        except Exception:
            return ip, ''

    par_nom: dict[str, list] = {}
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(20, len(ips) or 1)) as ex:
        for ip, nom in ex.map(_nom, ips):
            if nom:
                par_nom.setdefault(nom, []).append(ip)
    for nom, ips in par_nom.items():
        if len(ips) >= 2:
            findings.append(_finding(
                'conflit_nom', f"Le nom réseau « {nom} » est utilisé par {len(ips)} machines",
                {'nom': nom, 'ips': ips}, nom))
    return findings


def _cibles_ping(client_id: int, passerelle: str) -> list:
    """Construit la liste des cibles de test de qualité de liaison."""
    cibles = []
    perso = str(_cfg('diag_cibles_ping', '') or '').strip()
    if perso:
        for x in re.split(r'[,;\s]+', perso):
            if x:
                cibles.append({'ip': x, 'libelle': x, 'role': 'perso'})
        return cibles
    serveur_dns = ''
    try:
        from database import get_db
        conn = get_db()
        row = conn.execute('SELECT passerelle, serveur_dns FROM parc_general WHERE client_id=?',
                           (client_id,)).fetchone()
        conn.close()
        if row:
            passerelle = passerelle or (row[0] or '').strip()
            serveur_dns = (row[1] or '').strip()
    except Exception:
        pass
    if passerelle:
        cibles.append({'ip': passerelle, 'libelle': 'Passerelle', 'role': 'passerelle'})
    for d in re.split(r'[,;\s]+', serveur_dns):
        if d:
            cibles.append({'ip': d, 'libelle': f'DNS {d}', 'role': 'dns'})
    return cibles


# ════════════════════════════════════════════════════════════════════════════
#  Palier 2 — capture passive (scapy)
# ════════════════════════════════════════════════════════════════════════════

_scapy_cache = {'teste': False, 'module': None}


def _charger_scapy():
    if not _scapy_cache['teste']:
        _scapy_cache['teste'] = True
        try:
            from scapy.all import AsyncSniffer  # noqa: F401
            import scapy.all as _s
            _scapy_cache['module'] = _s
        except Exception:
            _scapy_cache['module'] = None
    return _scapy_cache['module']


def etat_capture() -> dict:
    """{disponible, motif} — motif ∈ scapy_absent | docker_bridge |
    privileges_insuffisants | aucune_interface | ok."""
    if os.environ.get('RUNNING_IN_DOCKER'):
        # En bridge Docker, la capture ne voit que le trafic conteneur : inutile
        # tant que network_mode:host + cap_add ne sont pas activés (documenté).
        s = _charger_scapy()
        if s is None:
            return {'disponible': False, 'motif': 'scapy_absent'}
        return {'disponible': False, 'motif': 'docker_bridge'}
    s = _charger_scapy()
    if s is None:
        return {'disponible': False, 'motif': 'scapy_absent'}
    try:
        ifaces = s.get_if_list()
        reelles = [i for i in ifaces if i not in ('lo', 'lo0')]
        if not reelles:
            return {'disponible': False, 'motif': 'aucune_interface'}
    except Exception:
        return {'disponible': False, 'motif': 'aucune_interface'}
    if IS_WINDOWS:
        # Sans Npcap (ou WinPcap), scapy sait lister les interfaces mais pas
        # capturer : conf.use_pcap/use_npcap traduit la présence du pilote.
        try:
            conf = s.conf
            if not (getattr(conf, 'use_pcap', False) or getattr(conf, 'use_npcap', False)):
                return {'disponible': False, 'motif': 'privileges_insuffisants'}
        except Exception:
            return {'disponible': False, 'motif': 'privileges_insuffisants'}
    elif hasattr(os, 'geteuid') and os.geteuid() != 0:
        return {'disponible': False, 'motif': 'privileges_insuffisants'}
    return {'disponible': True, 'motif': 'ok'}


def capture_passive(duree: int, seuils: dict) -> list:
    """Sniffe pendant `duree` secondes et retourne les évènements détectés."""
    s = _charger_scapy()
    if s is None:
        return []
    findings = []
    liaisons: dict[str, str] = {}          # ip -> mac (première vue)
    arp_gratuits: dict[str, int] = {}
    mac_ips: dict[str, set] = {}
    dhcp_serveurs: set = set()
    bpdu_tcn = [0]
    ra_routeurs: set = set()
    compteur = {'total': 0, 'broadcast': 0, 'debut': time.time()}
    tcp_flux: dict = {}
    tcp_retx: dict[str, int] = {}

    def _on(pkt):
        compteur['total'] += 1
        try:
            if pkt.haslayer(s.Ether) and pkt[s.Ether].dst == 'ff:ff:ff:ff:ff:ff':
                compteur['broadcast'] += 1
            if pkt.haslayer(s.ARP):
                a = pkt[s.ARP]
                ip_src, mac_src = a.psrc, _norm_mac(a.hwsrc)
                if ip_src and ip_src != '0.0.0.0' and mac_src:
                    mac_ips.setdefault(mac_src, set()).add(ip_src)
                    if a.op == 2 and a.pdst == a.psrc:      # ARP gratuit
                        arp_gratuits[ip_src] = arp_gratuits.get(ip_src, 0) + 1
                    ancienne = liaisons.get(ip_src)
                    if ancienne and ancienne != mac_src:
                        findings.append(_finding(
                            'arp_spoofing',
                            f"L'adresse {ip_src} a changé de MAC en cours de capture "
                            f"({ancienne} → {mac_src})",
                            {'ip': ip_src, 'mac_avant': ancienne, 'mac_apres': mac_src,
                             'fabricant_apres': _vendor(mac_src)}, ip_src))
                    liaisons.setdefault(ip_src, mac_src)
            if pkt.haslayer(s.DHCP):
                for opt in pkt[s.DHCP].options:
                    if isinstance(opt, tuple) and opt[0] == 'server_id':
                        dhcp_serveurs.add(str(opt[1]))
            if pkt.haslayer(s.IPv6) and pkt.haslayer(s.ICMPv6ND_RA):
                ra_routeurs.add(_norm_mac(pkt[s.Ether].src))
            if pkt.haslayer(s.STP):
                try:
                    if int(pkt[s.STP].bpdutype) == 0x80 or int(pkt[s.STP].bpduflags) & 0x01:
                        bpdu_tcn[0] += 1
                except Exception:
                    pass
            if pkt.haslayer(s.TCP) and pkt.haslayer(s.IP):
                ip4, tcp = pkt[s.IP], pkt[s.TCP]
                if pkt.haslayer(s.Raw):
                    cle = (ip4.src, tcp.sport, ip4.dst, tcp.dport)
                    seq = int(tcp.seq)
                    dernier = tcp_flux.get(cle)
                    if dernier is not None and seq < dernier:
                        k = f"{ip4.src}:{tcp.sport}→{ip4.dst}:{tcp.dport}"
                        tcp_retx[k] = tcp_retx.get(k, 0) + 1
                    tcp_flux[cle] = max(seq, dernier or 0)
        except Exception:
            pass

    try:
        sniffer = s.AsyncSniffer(prn=_on, store=False)
        sniffer.start()
        time.sleep(max(3, duree))
        sniffer.stop()
    except PermissionError:
        return []
    except Exception:
        logger.debug('network_diag: capture passive interrompue', exc_info=True)
        return []

    ecoule = max(1.0, time.time() - compteur['debut'])
    pps_broadcast = compteur['broadcast'] / ecoule
    seuil_bc = seuils.get('broadcast_pps', 150)
    if pps_broadcast >= seuil_bc:
        findings.append(_finding(
            'tempete_broadcast',
            f"{pps_broadcast:.0f} trames de broadcast/s (seuil {seuil_bc})",
            {'pps': round(pps_broadcast, 1), 'seuil': seuil_bc,
             'total_paquets': compteur['total']}, 'broadcast'))

    for ip, n in arp_gratuits.items():
        if n >= 8:
            findings.append(_finding(
                'arp_spoofing', f"{n} ARP gratuits émis pour {ip} pendant la capture",
                {'ip': ip, 'nb_arp_gratuits': n, 'mac': liaisons.get(ip, '')}, ip,
                gravite='avertissement'))

    for mac, ips in mac_ips.items():
        if len(ips) >= 6:
            findings.append(_finding(
                'mac_flapping', f"La MAC {mac} a été vue avec {len(ips)} adresses IP",
                {'mac': mac, 'fabricant': _vendor(mac), 'ips': sorted(ips)[:20]}, mac))

    if len(dhcp_serveurs) >= 2:
        findings.append(_finding(
            'dhcp_pirate', f"{len(dhcp_serveurs)} serveurs DHCP observés en capture",
            {'serveurs': sorted(dhcp_serveurs), 'source': 'capture'}, *sorted(dhcp_serveurs)))

    if len(ra_routeurs) >= 2:
        findings.append(_finding(
            'ra_pirate', f"{len(ra_routeurs)} routeurs IPv6 émettent des Router Advertisements",
            {'routeurs': sorted(ra_routeurs)}, *sorted(ra_routeurs)))

    if bpdu_tcn[0] >= 5:
        findings.append(_finding(
            'stp_instable', f"{bpdu_tcn[0]} BPDU de changement de topologie (TCN) captés",
            {'nb_tcn': bpdu_tcn[0], 'fenetre_s': round(ecoule)}, 'stp'))

    for flux, n in tcp_retx.items():
        if n >= 15:
            findings.append(_finding(
                'tcp_retransmissions', f"{n} retransmissions TCP sur le flux {flux}",
                {'flux': flux, 'nb_retransmissions': n}, flux))
    return findings


# ════════════════════════════════════════════════════════════════════════════
#  Palier 3 — interrogation SNMP des switchs / routeurs
# ════════════════════════════════════════════════════════════════════════════
#
# Une sonde host-based ne voit ni les collisions, ni les erreurs CRC/FCS, ni un
# duplex mismatch : ces compteurs vivent dans l'équipement. On les lit en SNMP
# lecture seule (v1/v2c, community), on compare deux relevés successifs, et on
# lève des évènements sur les tendances anormales. Opt-in (diag_snmp_actif).

_OID_IF_DESCR       = '1.3.6.1.2.1.2.2.1.2'
_OID_IF_TYPE        = '1.3.6.1.2.1.2.2.1.3'
_OID_IF_SPEED       = '1.3.6.1.2.1.2.2.1.5'
_OID_IF_ADMIN       = '1.3.6.1.2.1.2.2.1.7'
_OID_IF_OPER        = '1.3.6.1.2.1.2.2.1.8'
_OID_IF_IN_OCTETS   = '1.3.6.1.2.1.2.2.1.10'
_OID_IF_IN_DISCARDS = '1.3.6.1.2.1.2.2.1.13'
_OID_IF_IN_ERRORS   = '1.3.6.1.2.1.2.2.1.14'
_OID_IF_OUT_OCTETS  = '1.3.6.1.2.1.2.2.1.16'
_OID_IF_OUT_DISCARDS= '1.3.6.1.2.1.2.2.1.19'
_OID_IF_OUT_ERRORS  = '1.3.6.1.2.1.2.2.1.20'
_OID_IF_NAME        = '1.3.6.1.2.1.31.1.1.1.1'
_OID_IF_HCIN        = '1.3.6.1.2.1.31.1.1.1.6'
_OID_IF_HCOUT       = '1.3.6.1.2.1.31.1.1.1.10'
_OID_IF_HIGHSPEED   = '1.3.6.1.2.1.31.1.1.1.15'
_OID_IF_ALIAS       = '1.3.6.1.2.1.31.1.1.1.18'
_OID_DOT3_ALIGN     = '1.3.6.1.2.1.10.7.2.1.2'
_OID_DOT3_FCS       = '1.3.6.1.2.1.10.7.2.1.3'
_OID_DOT3_LATECOLL  = '1.3.6.1.2.1.10.7.2.1.11'
_OID_DOT3_EXCCOLL   = '1.3.6.1.2.1.10.7.2.1.12'
_OID_DOT3_DUPLEX    = '1.3.6.1.2.1.10.7.2.1.19'

_TYPES_EQUIP_SNMP = ('Switch', 'Switch/AP', 'Routeur/Pare-feu', 'NAS')


def _snmp_walk(oid_base, ip, communautes):
    try:
        from app import _snmp_walk as _w
        return _w(ip, oid_base, communautes) or {}
    except Exception:
        return {}


def interroger_equipement(ip: str, communautes) -> dict | None:
    """Walk SNMP d'un switch/routeur. None si l'agent ne répond pas."""
    descr = _snmp_walk(_OID_IF_DESCR, ip, communautes)
    if not descr:
        return None
    types = _snmp_walk(_OID_IF_TYPE, ip, communautes)
    noms = _snmp_walk(_OID_IF_NAME, ip, communautes)
    alias = _snmp_walk(_OID_IF_ALIAS, ip, communautes)
    oper = _snmp_walk(_OID_IF_OPER, ip, communautes)
    admin = _snmp_walk(_OID_IF_ADMIN, ip, communautes)
    speed = _snmp_walk(_OID_IF_SPEED, ip, communautes)
    highspeed = _snmp_walk(_OID_IF_HIGHSPEED, ip, communautes)
    in_err = _snmp_walk(_OID_IF_IN_ERRORS, ip, communautes)
    out_err = _snmp_walk(_OID_IF_OUT_ERRORS, ip, communautes)
    in_disc = _snmp_walk(_OID_IF_IN_DISCARDS, ip, communautes)
    out_disc = _snmp_walk(_OID_IF_OUT_DISCARDS, ip, communautes)
    in_oct = _snmp_walk(_OID_IF_HCIN, ip, communautes) or _snmp_walk(_OID_IF_IN_OCTETS, ip, communautes)
    out_oct = _snmp_walk(_OID_IF_HCOUT, ip, communautes) or _snmp_walk(_OID_IF_OUT_OCTETS, ip, communautes)
    align = _snmp_walk(_OID_DOT3_ALIGN, ip, communautes)
    fcs = _snmp_walk(_OID_DOT3_FCS, ip, communautes)
    late = _snmp_walk(_OID_DOT3_LATECOLL, ip, communautes)
    exc = _snmp_walk(_OID_DOT3_EXCCOLL, ip, communautes)
    duplex = _snmp_walk(_OID_DOT3_DUPLEX, ip, communautes)

    sysname = ''
    try:
        from app import _snmp_get, _OID_SYS_NAME
        sysname = (_snmp_get(ip, [_OID_SYS_NAME], communautes[0] if communautes else 'public')
                   .get(_OID_SYS_NAME, ''))
    except Exception:
        pass

    def _i(d, k, defaut=0):
        v = d.get(k)
        try:
            return int(v)
        except (TypeError, ValueError):
            return defaut

    ports = []
    for idx in descr:
        if _i(types, idx, 6) != 6:          # ethernetCsmacd uniquement
            continue
        if _i(admin, idx, 1) == 2:          # admin down : ignoré
            continue
        sp = _i(highspeed, idx) or (_i(speed, idx) // 1_000_000)
        ports.append({
            'index': int(idx.split('.')[0]) if idx.split('.')[0].isdigit() else idx,
            'nom': (noms.get(idx) or descr.get(idx) or f'if{idx}'),
            'alias': alias.get(idx, ''),
            'oper': _i(oper, idx, 1),
            'admin': _i(admin, idx, 1),
            'speed_mbps': sp,
            'in_oct': _i(in_oct, idx), 'out_oct': _i(out_oct, idx),
            'in_err': _i(in_err, idx), 'out_err': _i(out_err, idx),
            'in_disc': _i(in_disc, idx), 'out_disc': _i(out_disc, idx),
            'align_err': _i(align, idx), 'fcs_err': _i(fcs, idx),
            'late_coll': _i(late, idx), 'exc_coll': _i(exc, idx),
            'duplex': _i(duplex, idx),
        })
    return {'sysname': sysname, 'ts': time.time(), 'ports': ports}


_COMPTEURS_PORT = ('in_oct', 'out_oct', 'in_err', 'out_err', 'in_disc', 'out_disc',
                   'align_err', 'fcs_err', 'late_coll', 'exc_coll')


def _dernier_releve(conn, client_id, ip, port_index):
    row = conn.execute(
        "SELECT epoch, compteurs_json, oper_status FROM diag_snmp_releves "
        "WHERE client_id=? AND equipement_ip=? AND port_index=? "
        "ORDER BY epoch DESC LIMIT 1", (client_id, ip, port_index)).fetchone()
    if not row:
        return None
    try:
        cpt = json.loads(row[1] or '{}')
    except Exception:
        cpt = {}
    return {'epoch': row[0], 'compteurs': cpt, 'oper': row[2]}


def _analyser_snmp(client_id: int, ip: str, appareil_id, equipement: dict) -> list:
    """Compare l'équipement au dernier relevé, lève des findings, stocke le
    nouveau relevé."""
    from database import get_db
    seuil_err = _cfg_int('diag_snmp_seuil_erreurs', 50)
    seuil_sat = _cfg_float('diag_snmp_seuil_saturation_pct', 90)
    now = _now_z()
    findings = []
    ports = equipement.get('ports', [])
    gigabit_present = any(p['speed_mbps'] >= 1000 for p in ports)
    conn = get_db()
    try:
        for p in ports:
            pi = p['index']
            precedent = _dernier_releve(conn, client_id, ip, pi)
            libelle_port = f"{p['nom']}" + (f" ({p['alias']})" if p['alias'] else '')
            base = {'equipement': ip, 'sysname': equipement.get('sysname', ''),
                    'port': libelle_port, 'port_index': pi}

            if precedent and precedent['epoch']:
                dt = max(1.0, equipement['ts'] - precedent['epoch'])
                delta = {}
                for k in _COMPTEURS_PORT:
                    d = p[k] - precedent['compteurs'].get(k, p[k])
                    delta[k] = d if d >= 0 else p[k]  # reset compteur / reboot → on repart de la valeur brute

                # Duplex mismatch
                if p['oper'] == 1 and p['speed_mbps'] >= 100 and (
                        delta['late_coll'] > 0 or p['duplex'] == 2):
                    findings.append(_finding(
                        'duplex_mismatch',
                        f"{libelle_port} sur {ip} : "
                        + ("half-duplex négocié" if p['duplex'] == 2
                           else f"{delta['late_coll']} late collisions"),
                        {**base, 'duplex': p['duplex'], 'delta_late_coll': delta['late_coll'],
                         'speed_mbps': p['speed_mbps']},
                        ip, pi))

                # CRC / alignement
                if delta['fcs_err'] + delta['align_err'] >= seuil_err:
                    findings.append(_finding(
                        'port_crc',
                        f"{libelle_port} sur {ip} : {delta['fcs_err'] + delta['align_err']} "
                        f"erreurs CRC/alignement depuis le dernier relevé",
                        {**base, 'delta_fcs': delta['fcs_err'], 'delta_align': delta['align_err']},
                        ip, pi))

                # Erreurs / rejets génériques
                err_io = delta['in_err'] + delta['out_err']
                disc_io = delta['in_disc'] + delta['out_disc']
                if max(err_io, disc_io) >= seuil_err:
                    findings.append(_finding(
                        'port_erreurs',
                        f"{libelle_port} sur {ip} : {err_io} erreurs / {disc_io} rejets de paquets",
                        {**base, 'delta_erreurs': err_io, 'delta_rejets': disc_io}, ip, pi))

                # Métriques temporelles (palier 5) : erreurs + débit par port
                _cible_m = f"{ip}:{pi}"
                _enregistrer_metrique(conn, client_id, 'port_erreurs', _cible_m,
                                      err_io + disc_io + delta['fcs_err'] + delta['align_err'],
                                      equipement['ts'])

                # Saturation de lien
                if p['speed_mbps'] > 0:
                    debit_mbps = max(delta['in_oct'], delta['out_oct']) * 8 / dt / 1_000_000
                    taux = debit_mbps / p['speed_mbps'] * 100
                    _enregistrer_metrique(conn, client_id, 'port_debit_pct', _cible_m,
                                          round(taux, 1), equipement['ts'])
                    if taux >= seuil_sat:
                        findings.append(_finding(
                            'port_sature',
                            f"{libelle_port} sur {ip} : lien à {taux:.0f} % "
                            f"({debit_mbps:.0f} / {p['speed_mbps']} Mb/s)",
                            {**base, 'taux_pct': round(taux), 'debit_mbps': round(debit_mbps),
                             'speed_mbps': p['speed_mbps']}, ip, pi))

                # Flapping : oper_status a changé plusieurs fois récemment
                changements = _compter_changements_oper(conn, client_id, ip, pi, p['oper'])
                if changements >= 3:
                    findings.append(_finding(
                        'port_flapping',
                        f"{libelle_port} sur {ip} : {changements} changements d'état récents",
                        {**base, 'nb_changements': changements}, ip, pi))

            # Vitesse réduite (indépendant de l'historique)
            if p['oper'] == 1 and gigabit_present and 0 < p['speed_mbps'] < 1000:
                findings.append(_finding(
                    'vitesse_reduite',
                    f"{libelle_port} sur {ip} : négocié à {p['speed_mbps']} Mb/s "
                    f"sur un équipement gigabit",
                    {**base, 'speed_mbps': p['speed_mbps']}, ip, pi))

            conn.execute(
                "INSERT INTO diag_snmp_releves (client_id, appareil_id, equipement_ip, "
                "port_index, port_nom, horodatage, epoch, compteurs_json, duplex, "
                "speed_mbps, oper_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (client_id, appareil_id, ip, pi, libelle_port, now, equipement['ts'],
                 json.dumps({k: p[k] for k in _COMPTEURS_PORT}), p['duplex'],
                 p['speed_mbps'], p['oper']))
        conn.commit()
    except Exception:
        logger.exception('network_diag: analyse SNMP impossible')
    finally:
        conn.close()
    if appareil_id:
        for f in findings:
            f['appareil_id'] = appareil_id
    return findings


def _compter_changements_oper(conn, client_id, ip, port_index, oper_actuel):
    rows = conn.execute(
        "SELECT oper_status FROM diag_snmp_releves WHERE client_id=? AND equipement_ip=? "
        "AND port_index=? ORDER BY epoch DESC LIMIT 4", (client_id, ip, port_index)).fetchall()
    suite = [oper_actuel] + [r[0] for r in rows]
    return sum(1 for i in range(1, len(suite)) if suite[i] != suite[i - 1])


def interroger_equipements_client(client_id: int) -> list:
    """Poll SNMP de tous les switchs/routeurs/NAS du client. Retourne les findings."""
    if str(_cfg('diag_snmp_actif', '0')) != '1':
        return []
    communautes = [c.strip() for c in re.split(r'[,;\s]+',
                   str(_cfg('diag_snmp_communautes', 'public') or 'public')) if c.strip()]
    if not communautes:
        communautes = ['public']
    try:
        from database import get_db
        conn = get_db()
        placeholders = ','.join('?' * len(_TYPES_EQUIP_SNMP))
        rows = conn.execute(
            f"SELECT id, adresse_ip FROM appareils WHERE client_id=? "
            f"AND type_appareil IN ({placeholders}) AND adresse_ip!='' AND adresse_ip IS NOT NULL",
            (client_id, *_TYPES_EQUIP_SNMP)).fetchall()
        conn.close()
    except Exception:
        return []
    findings = []
    for appareil_id, ip in rows:
        try:
            equipement = interroger_equipement(ip, communautes)
            if equipement is None:
                continue
            findings += _analyser_snmp(client_id, ip, appareil_id, equipement)
        except Exception:
            logger.debug('network_diag: SNMP %s en échec', ip, exc_info=True)
    return findings


def etat_snmp(client_id: int) -> dict:
    """Dernier état par équipement / par port pour l'affichage du panneau."""
    from database import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT r.* FROM diag_snmp_releves r "
            "JOIN (SELECT equipement_ip, port_index, MAX(epoch) AS m "
            "      FROM diag_snmp_releves WHERE client_id=? GROUP BY equipement_ip, port_index) d "
            "  ON r.equipement_ip=d.equipement_ip AND r.port_index=d.port_index AND r.epoch=d.m "
            "WHERE r.client_id=? ORDER BY r.equipement_ip, r.port_index",
            (client_id, client_id)).fetchall()
        cols = [c[1] for c in conn.execute("PRAGMA table_info(diag_snmp_releves)").fetchall()]
    finally:
        conn.close()
    equipements = {}
    for r in rows:
        d = dict(zip(cols, r))
        try:
            d['compteurs'] = json.loads(d.pop('compteurs_json', '{}') or '{}')
        except Exception:
            d['compteurs'] = {}
        equipements.setdefault(d['equipement_ip'], {
            'ip': d['equipement_ip'], 'appareil_id': d['appareil_id'], 'ports': [],
        })['ports'].append(d)
    return {
        'actif': str(_cfg('diag_snmp_actif', '0')) == '1',
        'equipements': list(equipements.values()),
    }


# ════════════════════════════════════════════════════════════════════════════
#  Palier 4 — cartographie de topologie L2 (FDB bridge-MIB + LLDP/CDP)
# ════════════════════════════════════════════════════════════════════════════
#
# Recoupe les tables MAC des switchs (« telle MAC est vue sur tel port ») avec
# l'inventaire ParcInfo et le câblage manuel de la baie de brassage. Signale
# les incohérences ; peut pré-remplir les ports de baie vides.

_OID_FDB_DOT1D_PORT   = '1.3.6.1.2.1.17.4.3.1.2'        # dot1dTpFdbPort : MAC -> bridge port
_OID_FDB_BASEPORT_IF  = '1.3.6.1.2.1.17.1.4.1.2'        # dot1dBasePortIfIndex : bridge port -> ifIndex
_OID_FDB_DOT1Q_PORT   = '1.3.6.1.2.1.17.7.1.2.2.1.2'    # dot1qTpFdbPort : VLAN.MAC -> bridge port
_OID_LLDP_REM_SYSNAME = '1.0.8802.1.1.2.1.4.1.1.9'
_OID_LLDP_REM_PORTID  = '1.0.8802.1.1.2.1.4.1.1.7'


def _mac_depuis_suffixe(suffixe: str, decimal_count: int = 6) -> str:
    """Les 6 derniers sous-identifiants d'un OID FDB = la MAC en décimal."""
    parts = suffixe.split('.')
    if len(parts) < decimal_count:
        return ''
    try:
        return ':'.join('%02x' % int(x) for x in parts[-decimal_count:])
    except ValueError:
        return ''


def decouvrir_topologie(client_id: int) -> list:
    """Découvre la topologie L2 des switchs SNMP du client. Retourne les
    findings de câblage incohérent ; peuple la table diag_topologie."""
    if str(_cfg('diag_topologie_active', '0')) != '1' or str(_cfg('diag_snmp_actif', '0')) != '1':
        return []
    communautes = _communautes_snmp()
    try:
        from database import get_db
        conn = get_db()
        placeholders = ','.join('?' * len(_TYPES_EQUIP_SNMP))
        equipements = conn.execute(
            f"SELECT id, adresse_ip FROM appareils WHERE client_id=? "
            f"AND type_appareil IN ({placeholders}) AND adresse_ip!='' AND adresse_ip IS NOT NULL",
            (client_id, *_TYPES_EQUIP_SNMP)).fetchall()
        inventaire = {}
        for aid, nom, mac in conn.execute(
                "SELECT id, nom_machine, adresse_mac FROM appareils "
                "WHERE client_id=? AND adresse_mac!='' AND adresse_mac IS NOT NULL", (client_id,)):
            inventaire[_norm_mac(mac)] = (aid, nom)
        conn.close()
    except Exception:
        return []

    findings = []
    now = _now_z()
    for equip_id, ip in equipements:
        try:
            lignes, findings_eq = _topologie_equipement(client_id, equip_id, ip, communautes, inventaire, now)
        except Exception:
            logger.debug('network_diag: topologie %s en échec', ip, exc_info=True)
            continue
        findings += findings_eq
        try:
            from database import get_db
            conn = get_db()
            conn.execute("DELETE FROM diag_topologie WHERE client_id=? AND equipement_ip=?",
                         (client_id, ip))
            conn.executemany(
                "INSERT INTO diag_topologie (client_id, equipement_ip, equipement_appareil_id, "
                "port_index, port_nom, mac_vue, appareil_vu_id, appareil_vu_nom, vendor, "
                "type_lien, voisin_nom, voisin_port, horodatage) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", lignes)
            conn.commit()
            conn.close()
        except Exception:
            logger.exception('network_diag: écriture topologie impossible')
    return findings


def _topologie_equipement(client_id, equip_id, ip, communautes, inventaire, now):
    """Un switch : FDB -> port -> {mac, appareil} + voisins LLDP + recoupement baie."""
    equipement = interroger_equipement(ip, communautes)
    noms_ports = {}
    if equipement:
        noms_ports = {p['index']: p['nom'] for p in equipement['ports']}

    baseport_if = _snmp_walk(_OID_FDB_BASEPORT_IF, ip, communautes)   # {bridge_port: ifIndex}
    fdb = _snmp_walk(_OID_FDB_DOT1Q_PORT, ip, communautes)
    decal = 6
    if not fdb:
        fdb = _snmp_walk(_OID_FDB_DOT1D_PORT, ip, communautes)
    else:
        decal = 6  # dot1q : suffixe = vlan.macs(6) → on prend quand même les 6 derniers

    # port bridge -> [macs]
    par_port = {}
    for suffixe, bridge_port in fdb.items():
        mac = _mac_depuis_suffixe(suffixe, decal)
        try:
            bp = int(bridge_port)
        except (TypeError, ValueError):
            continue
        if not mac or bp <= 0:
            continue
        ifindex = baseport_if.get(str(bp), bp)
        try:
            ifindex = int(ifindex)
        except (TypeError, ValueError):
            ifindex = bp
        par_port.setdefault(ifindex, []).append(mac)

    # voisins LLDP
    voisins = {}
    lldp_noms = _snmp_walk(_OID_LLDP_REM_SYSNAME, ip, communautes)
    lldp_ports = _snmp_walk(_OID_LLDP_REM_PORTID, ip, communautes)
    for suffixe, nom_voisin in lldp_noms.items():
        # suffixe LLDP = timeMark.locPortNum.remIndex → locPortNum au milieu
        parts = suffixe.split('.')
        loc = parts[1] if len(parts) >= 2 else parts[0]
        try:
            voisins[int(loc)] = {'nom': str(nom_voisin), 'port': str(lldp_ports.get(suffixe, ''))}
        except ValueError:
            pass

    # câblage baie de ce switch : {numero_port: (appareil_id, nom)}
    baie = {}
    try:
        from database import get_db
        conn = get_db()
        for numero, aid, anom in conn.execute(
                "SELECT p.numero, p.appareil_id, a.nom_machine "
                "FROM baie_slot_ports p JOIN baie_slots s ON s.id=p.slot_id "
                "LEFT JOIN appareils a ON a.id=p.appareil_id "
                "WHERE s.client_id=? AND s.appareil_id=? AND p.appareil_id IS NOT NULL",
                (client_id, equip_id)):
            baie[int(numero)] = (aid, anom)
        conn.close()
    except Exception:
        pass

    lignes, findings = [], []
    for ifindex, macs in sorted(par_port.items()):
        macs = sorted(set(macs))
        port_nom = noms_ports.get(ifindex, f'if{ifindex}')
        vue_appareil_id, vue_nom = None, ''
        if len(macs) == 1 and macs[0] in inventaire:
            vue_appareil_id, vue_nom = inventaire[macs[0]]
        v = voisins.get(ifindex, {})
        for mac in macs:
            inv = inventaire.get(mac)
            lignes.append((client_id, ip, equip_id, ifindex, port_nom, mac,
                           inv[0] if inv else None, inv[1] if inv else '',
                           _vendor(mac), 'lldp' if v else 'mac',
                           v.get('nom', ''), v.get('port', ''), now))
        # recoupement baie : le switch voit un appareil connu, seul, différent
        # de celui déclaré dans la baie pour ce numéro de port
        baie_port = baie.get(ifindex)
        if vue_appareil_id and baie_port and baie_port[0] and baie_port[0] != vue_appareil_id:
            findings.append(_finding(
                'cablage_incoherent',
                f"Port {port_nom} de {ip} : le switch voit « {vue_nom} », "
                f"la baie déclare « {baie_port[1]} »",
                {'equipement': ip, 'port_index': ifindex, 'port': port_nom,
                 'vu_par_snmp': vue_nom, 'declare_baie': baie_port[1]},
                ip, ifindex))
    if findings:
        for f in findings:
            f['appareil_id'] = equip_id
    return lignes, findings


def _communautes_snmp():
    c = [x.strip() for x in re.split(r'[,;\s]+',
         str(_cfg('diag_snmp_communautes', 'public') or 'public')) if x.strip()]
    return c or ['public']


def etat_topologie(client_id: int) -> dict:
    from database import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT t.*, a.nom_machine FROM diag_topologie t "
            "LEFT JOIN appareils a ON a.id = t.equipement_appareil_id "
            "WHERE t.client_id=? ORDER BY t.equipement_ip, t.port_index", (client_id,)).fetchall()
        cols = [c[1] for c in conn.execute("PRAGMA table_info(diag_topologie)").fetchall()] + ['equipement_nom']
        incoherents = {
            f"{d['equipement']}:{d['port_index']}"
            for (dj,) in conn.execute(
                "SELECT details_json FROM diag_reseau_evenements WHERE client_id=? "
                "AND resolu=0 AND categorie='cablage_incoherent'", (client_id,))
            for d in [_json_charge(dj)] if d
        }
    finally:
        conn.close()
    equipements = {}
    for r in rows:
        d = dict(zip(cols, r))
        cle = d['equipement_ip']
        eq = equipements.setdefault(cle, {
            'ip': cle, 'nom': d.get('equipement_nom') or '',
            'appareil_id': d['equipement_appareil_id'], 'ports': {}, 'voisins': []})
        p = eq['ports'].setdefault(d['port_index'], {
            'port_index': d['port_index'], 'port_nom': d['port_nom'], 'hotes': [],
            'incoherent': f"{cle}:{d['port_index']}" in incoherents})
        p['hotes'].append({'mac': d['mac_vue'], 'appareil_id': d['appareil_vu_id'],
                           'appareil_nom': d['appareil_vu_nom'], 'vendor': d['vendor']})
        if d['voisin_nom'] and not any(v['nom'] == d['voisin_nom'] for v in eq['voisins']):
            eq['voisins'].append({'nom': d['voisin_nom'], 'port': d['voisin_port'],
                                  'port_local': d['port_nom']})
    for eq in equipements.values():
        eq['ports'] = sorted(eq['ports'].values(), key=lambda x: x['port_index'])
    return {'actif': str(_cfg('diag_topologie_active', '0')) == '1',
            'equipements': list(equipements.values())}


def _json_charge(s):
    try:
        return json.loads(s or '{}')
    except Exception:
        return {}


def appliquer_topologie_baie(client_id: int) -> dict:
    """Pré-remplit les ports de baie VIDES avec l'appareil vu par SNMP. Ne
    touche jamais un port déjà affecté. Retourne {maj: n}."""
    from database import get_db
    conn = get_db()
    maj = 0
    try:
        rows = conn.execute(
            "SELECT DISTINCT t.equipement_appareil_id, t.port_index, t.appareil_vu_id "
            "FROM diag_topologie t "
            "WHERE t.client_id=? AND t.appareil_vu_id IS NOT NULL", (client_id,)).fetchall()
        for equip_aid, port_index, vu_aid in rows:
            if not equip_aid:
                continue
            slot = conn.execute(
                "SELECT id FROM baie_slots WHERE client_id=? AND appareil_id=? LIMIT 1",
                (client_id, equip_aid)).fetchone()
            if not slot:
                continue
            port = conn.execute(
                "SELECT id, appareil_id, peripherique_id, usage_libre FROM baie_slot_ports "
                "WHERE slot_id=? AND numero=?", (slot[0], port_index)).fetchone()
            now = _now_z()
            if port and not (port[1] or port[2] or (port[3] or '').strip()):
                conn.execute("UPDATE baie_slot_ports SET appareil_id=?, date_maj=? WHERE id=?",
                             (vu_aid, now, port[0]))
                maj += 1
            elif not port:
                conn.execute(
                    "INSERT OR IGNORE INTO baie_slot_ports (slot_id, numero, appareil_id, date_maj) "
                    "VALUES (?,?,?,?)", (slot[0], port_index, vu_aid, now))
                maj += 1
        conn.commit()
    except Exception:
        logger.exception('network_diag: application topologie -> baie impossible')
    finally:
        conn.close()
    return {'maj': maj}


# ════════════════════════════════════════════════════════════════════════════
#  Palier 5 — tendances & baseline
# ════════════════════════════════════════════════════════════════════════════

_METRIQUE_PLANCHER = {   # en-dessous : pas d'alerte relative, même si le ratio est élevé
    'liaison_latence': 20.0,   # ms
    'liaison_gigue':   15.0,   # ms
    'liaison_perte':    2.0,   # %
    'port_debit_pct':  50.0,   # %
    'port_erreurs':    10.0,   # Δ erreurs
}


def _enregistrer_metrique(conn, client_id, categorie, cible, valeur, epoch):
    conn.execute(
        "INSERT INTO diag_metriques (client_id, categorie, cible, horodatage, epoch, valeur) "
        "VALUES (?,?,?,?,?,?)",
        (client_id, categorie, cible, _now_z(), epoch, float(valeur)))


def enregistrer_metriques_liaison(client_id: int, stats_list: list):
    """stats_list : sortie de _ping_rafale (une entrée par cible testée)."""
    if not stats_list:
        return
    from database import get_db
    conn = get_db()
    ep = time.time()
    try:
        for st in stats_list:
            cible = st.get('ip')
            if not cible:
                continue
            if st.get('perte_pct') is not None:
                _enregistrer_metrique(conn, client_id, 'liaison_perte', cible, st['perte_pct'], ep)
            if st.get('moy') is not None:
                _enregistrer_metrique(conn, client_id, 'liaison_latence', cible, st['moy'], ep)
            if st.get('gigue') is not None:
                _enregistrer_metrique(conn, client_id, 'liaison_gigue', cible, st['gigue'], ep)
        conn.commit()
    except Exception:
        logger.exception('network_diag: enregistrement métriques liaison impossible')
    finally:
        conn.close()


def _percentile(valeurs, p):
    if not valeurs:
        return None
    s = sorted(valeurs)
    k = (len(s) - 1) * p
    f = int(k)
    if f + 1 < len(s):
        return s[f] + (s[f + 1] - s[f]) * (k - f)
    return s[f]


def evaluer_baseline(client_id: int) -> list:
    """Compare la dernière valeur de chaque (catégorie, cible) à sa référence
    (médiane + p90 sur diag_baseline_jours). Alerte sur écart relatif net."""
    if str(_cfg('diag_baseline_active', '1')) != '1':
        return []
    jours = _cfg_int('diag_baseline_jours', 7)
    facteur = _cfg_float('diag_baseline_facteur', 2.5)
    from database import get_db
    depuis = time.time() - jours * 86400
    conn = get_db()
    findings = []
    try:
        couples = conn.execute(
            "SELECT DISTINCT categorie, cible FROM diag_metriques "
            "WHERE client_id=? AND epoch>=?", (client_id, depuis)).fetchall()
        for categorie, cible in couples:
            vals = [r[0] for r in conn.execute(
                "SELECT valeur FROM diag_metriques WHERE client_id=? AND categorie=? "
                "AND cible=? AND epoch>=? ORDER BY epoch", (client_id, categorie, cible, depuis))]
            if len(vals) < 8:
                continue
            courant = vals[-1]
            reference = vals[:-1]
            mediane = _percentile(reference, 0.5)
            p90 = _percentile(reference, 0.9) or mediane or 0
            plancher = _METRIQUE_PLANCHER.get(categorie, 0)
            if courant >= plancher and p90 > 0 and courant >= p90 * facteur:
                ratio = courant / (mediane or p90 or 1)
                findings.append(_finding(
                    'degradation_relative',
                    f"{_libelle_metrique(categorie)} {cible} : {courant:.0f} "
                    f"vs référence {mediane:.0f} (×{ratio:.1f})",
                    {'categorie_metrique': categorie, 'cible': cible, 'valeur': round(courant, 1),
                     'reference': round(mediane or 0, 1), 'p90': round(p90, 1), 'ratio': round(ratio, 1)},
                    categorie, cible))
        # rattache au client (pas d'appareil précis pour une cible IP générique)
    except Exception:
        logger.exception('network_diag: évaluation baseline impossible')
    finally:
        conn.close()
    return findings


_LIBELLE_METRIQUE = {
    'liaison_perte': "Perte", 'liaison_latence': "Latence", 'liaison_gigue': "Gigue",
    'port_debit_pct': "Débit", 'port_erreurs': "Erreurs",
}


def _libelle_metrique(cat):
    return _LIBELLE_METRIQUE.get(cat, cat)


def serie_metrique(client_id: int, categorie: str, cible: str, points_max: int = 120) -> dict:
    jours = _cfg_int('diag_baseline_jours', 7)
    depuis = time.time() - jours * 86400
    from database import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT epoch, valeur FROM diag_metriques WHERE client_id=? AND categorie=? "
            "AND cible=? AND epoch>=? ORDER BY epoch", (client_id, categorie, cible, depuis)).fetchall()
    finally:
        conn.close()
    if not rows:
        return {'points': [], 'mediane': None, 'p90': None}
    if len(rows) > points_max:
        pas = len(rows) / points_max
        rows = [rows[int(i * pas)] for i in range(points_max)]
    vals = [r[1] for r in rows]
    return {
        'points': [{'t': r[0], 'v': r[1]} for r in rows],
        'mediane': round(_percentile(vals, 0.5), 1),
        'p90': round(_percentile(vals, 0.9), 1),
        'libelle': _libelle_metrique(categorie),
    }


def cibles_metriques(client_id: int) -> list:
    """Liste des (categorie, cible) disponibles pour le sélecteur de graphe."""
    from database import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT categorie, cible FROM diag_metriques WHERE client_id=? "
            "ORDER BY categorie, cible", (client_id,)).fetchall()
    finally:
        conn.close()
    return [{'categorie': c, 'cible': t, 'libelle': f"{_libelle_metrique(c)} — {t}"} for c, t in rows]


# ════════════════════════════════════════════════════════════════════════════
#  Palier 6 — remédiation guidée + rapport
# ════════════════════════════════════════════════════════════════════════════

_REMEDIATION = {
    'conflit_ip': {
        'cause': "Deux machines utilisent la même adresse IP (bail DHCP dupliqué, IP fixe mal choisie, VM clonée).",
        'verifier': ["Identifier les deux MAC via la table ARP et le fabricant",
                     "Vérifier la plage DHCP et les réservations",
                     "Chercher une IP fixe posée dans la plage dynamique"],
        'corriger': ["Repasser l'un des postes en DHCP, ou lui donner une IP hors plage dynamique",
                     "Ajouter une réservation DHCP par MAC pour les équipements fixes"],
    },
    'arp_spoofing': {
        'cause': "Une machine répond pour de nombreuses IP : usurpation ARP, proxy ARP mal configuré, ou passerelle mal identifiée.",
        'verifier': ["Confirmer la MAC légitime de la passerelle",
                     "Repérer la machine qui répond en masse (fabricant, port switch via SNMP)"],
        'corriger': ["Isoler la machine suspecte le temps de l'analyse",
                     "Activer Dynamic ARP Inspection / DHCP snooping sur le switch si disponible"],
    },
    'dhcp_pirate': {
        'cause': "Un serveur DHCP non déclaré distribue des baux (box perso branchée, routeur Wi-Fi en mode routeur, VM avec DHCP actif).",
        'verifier': ["Comparer l'IP du serveur DHCP vu à celle attendue",
                     "Localiser le port switch d'où viennent les offres (SNMP / capture)"],
        'corriger': ["Débrancher ou reconfigurer l'équipement fautif en point d'accès (DHCP désactivé)",
                     "Activer DHCP snooping et n'autoriser que le port du serveur légitime"],
    },
    'ra_pirate': {
        'cause': "Plusieurs routeurs émettent des Router Advertisements IPv6 : équipement grand public branché, IPv6 non maîtrisé.",
        'verifier': ["Lister les adresses lien-local des routeurs RA",
                     "Identifier l'équipement non prévu"],
        'corriger': ["Désactiver l'IPv6 / le RA sur l'équipement parasite",
                     "Activer RA Guard sur le switch"],
    },
    'tempete_broadcast': {
        'cause': "Trafic de broadcast anormalement élevé : boucle réseau, carte défaillante, application bavarde.",
        'verifier': ["Chercher une boucle (deux ports reliés, STP désactivé)",
                     "Repérer le port avec le plus de trafic broadcast via SNMP"],
        'corriger': ["Rétablir STP / RSTP sur tous les switchs",
                     "Isoler le port ou la machine à l'origine du bruit"],
    },
    'stp_instable': {
        'cause': "Changements de topologie STP fréquents : lien qui flappe, équipement qui redémarre, STP mal réglé.",
        'verifier': ["Identifier le port qui change d'état (SNMP, logs switch)",
                     "Vérifier le câblage et l'alimentation de l'équipement concerné"],
        'corriger': ["Activer PortFast/edge sur les ports d'accès terminaux",
                     "Remplacer le câble/SFP douteux, fiabiliser l'alimentation"],
    },
    'qualite_liaison': {
        'cause': "Perte de paquets, latence ou gigue élevée : lien saturé, câble/connecteur abîmé, Wi-Fi encombré, équipement surchargé.",
        'verifier': ["Tester en filaire pour écarter le Wi-Fi",
                     "Regarder les compteurs d'erreurs du port switch (SNMP)",
                     "Vérifier la charge CPU de la passerelle"],
        'corriger': ["Remplacer le câble, changer de port switch",
                     "Décharger le lien (QoS, planifier les sauvegardes hors heures ouvrées)"],
    },
    'passerelle_injoignable': {
        'cause': "La passerelle ne répond plus : équipement éteint/planté, câble débranché, mauvaise IP de passerelle.",
        'verifier': ["Vérifier l'alimentation et les LED de la box/routeur",
                     "Confirmer l'adresse de passerelle configurée"],
        'corriger': ["Redémarrer la passerelle", "Rétablir le câblage, corriger la configuration IP"],
    },
    'dns_degrade': {
        'cause': "Le serveur DNS répond mal ou lentement : serveur surchargé, DNS distant injoignable, cache corrompu.",
        'verifier': ["Tester la résolution vers un DNS public (8.8.8.8, 1.1.1.1)",
                     "Regarder la charge du serveur DNS interne"],
        'corriger': ["Ajouter un DNS secondaire fiable",
                     "Redémarrer le service DNS, vider le cache"],
    },
    'conflit_nom': {
        'cause': "Deux machines annoncent le même nom NetBIOS/hôte : clonage sans renommage, doublon de configuration.",
        'verifier': ["Identifier les deux IP/MAC concernées"],
        'corriger': ["Renommer l'une des machines et redémarrer"],
    },
    'duplex_mismatch': {
        'cause': "Un côté du lien est en half-duplex, l'autre en full : autonégociation ratée, réglage forcé d'un seul côté.",
        'verifier': ["Regarder le duplex négocié des deux côtés (switch SNMP + poste)",
                     "Repérer les late collisions sur le port"],
        'corriger': ["Remettre les DEUX extrémités en autonégociation",
                     "Sinon forcer la même vitesse/duplex des deux côtés",
                     "Remplacer le câble si les erreurs persistent"],
    },
    'port_crc': {
        'cause': "Erreurs CRC/FCS en hausse : câble ou connecteur endommagé, interférences, SFP défaillant, duplex mismatch.",
        'verifier': ["Vérifier le sertissage / l'état du câble et des prises",
                     "Écarter le câble des sources d'interférence (alim, néons)",
                     "Contrôler le duplex du port"],
        'corriger': ["Remplacer le câble / le cordon de brassage / le SFP",
                     "Changer de port switch pour confirmer"],
    },
    'port_erreurs': {
        'cause': "Erreurs ou paquets rejetés : congestion (buffers pleins), lien de mauvaise qualité, boucle.",
        'verifier': ["Regarder le taux d'utilisation du port",
                     "Vérifier s'il s'agit d'erreurs d'entrée (câble) ou de sortie (congestion)"],
        'corriger': ["Augmenter la capacité du lien (agrégation, 1G→10G)",
                     "Répartir la charge, activer la QoS"],
    },
    'port_sature': {
        'cause': "Le lien tourne près de sa capacité maximale : sauvegardes, transferts massifs, lien sous-dimensionné.",
        'verifier': ["Identifier ce qui consomme (sens du trafic, horaires)",
                     "Vérifier la vitesse négociée du port"],
        'corriger': ["Planifier les gros transferts hors heures ouvrées",
                     "Passer le lien en 10G ou agréger deux ports (LACP)"],
    },
    'port_flapping': {
        'cause': "Le port change d'état sans arrêt : câble/connecteur défaillant, équipement qui redémarre, SFP incompatible.",
        'verifier': ["Regarder l'historique d'état du port (logs switch)",
                     "Tester avec un autre câble et un autre port"],
        'corriger': ["Remplacer le câble / SFP", "Fiabiliser l'alimentation de l'équipement au bout"],
    },
    'vitesse_reduite': {
        'cause': "Le port a négocié 10 ou 100 Mb/s sur du matériel gigabit : câble à 2 paires, câble trop long/abîmé, port forcé.",
        'verifier': ["Vérifier la catégorie et la longueur du câble (Cat5e+ / <100 m)",
                     "Contrôler si la vitesse est forcée quelque part"],
        'corriger': ["Remplacer par un câble Cat5e/Cat6 4 paires en bon état",
                     "Remettre le port en autonégociation"],
    },
    'mac_flapping': {
        'cause': "La même MAC est vue sur plusieurs ports rapidement : boucle réseau, deux liens actifs vers le même équipement.",
        'verifier': ["Chercher une boucle physique", "Vérifier les liens redondants sans agrégation"],
        'corriger': ["Rétablir STP", "Agréger correctement (LACP) les liens redondants"],
    },
    'tcp_retransmissions': {
        'cause': "Retransmissions TCP élevées : perte de paquets sur le chemin, congestion, MTU incohérente.",
        'verifier': ["Corréler avec les erreurs de port et la saturation",
                     "Tester la MTU de bout en bout"],
        'corriger': ["Traiter la perte sous-jacente (câble, saturation)",
                     "Harmoniser la MTU (jumbo frames tout ou rien)"],
    },
    'cablage_incoherent': {
        'cause': "Le switch voit un appareil sur un port différent de celui déclaré dans la baie de brassage : brassage modifié sans mise à jour, erreur de saisie.",
        'verifier': ["Suivre physiquement le cordon de brassage du port concerné",
                     "Confronter l'étiquetage à la réalité"],
        'corriger': ["Corriger l'affectation du port dans la baie de brassage",
                     "Utiliser « Reporter dans la baie » pour partir de l'état réel"],
    },
    'degradation_relative': {
        'cause': "Une métrique s'est nettement dégradée par rapport à sa valeur habituelle, même si le seuil absolu n'est pas franchi : début de panne, nouvelle charge, changement d'environnement.",
        'verifier': ["Regarder la courbe : dégradation brutale ou progressive ?",
                     "Corréler avec un changement récent (matériel, câblage, trafic)"],
        'corriger': ["Traiter la cause identifiée sur la courbe",
                     "Si c'est le nouveau normal (charge légitime), ajuster les attentes / dimensionner"],
    },
}


def remediation(categorie: str):
    return _REMEDIATION.get(categorie)


def generer_rapport_diag(client_id: int, forcer_html: bool = False):
    """Rapport de diagnostic réseau. Retourne (contenu, mimetype, filename)."""
    from database import get_db
    conn = get_db()
    try:
        client = conn.execute("SELECT nom FROM clients WHERE id=?", (client_id,)).fetchone()
        nom_client = client[0] if client else f'client {client_id}'
        _cols = ('gravite', 'categorie', 'titre', 'nb_occurrences', 'derniere_occurrence')
        evts = [dict(zip(_cols, r)) for r in conn.execute(
            "SELECT gravite, categorie, titre, nb_occurrences, derniere_occurrence "
            "FROM diag_reseau_evenements WHERE client_id=? AND resolu=0 "
            "ORDER BY CASE gravite WHEN 'critique' THEN 0 WHEN 'avertissement' THEN 1 ELSE 2 END",
            (client_id,)).fetchall()]
    finally:
        conn.close()

    par_gravite = {}
    for e in evts:
        par_gravite[e['gravite']] = par_gravite.get(e['gravite'], 0) + 1
    snmp = etat_snmp(client_id)
    topo = etat_topologie(client_id)
    date_str = _now_z().replace('T', ' ').replace('Z', ' UTC')

    try:
        from app import REPORTLAB_AVAILABLE
    except Exception:
        REPORTLAB_AVAILABLE = False

    if REPORTLAB_AVAILABLE and not forcer_html:
        try:
            return _rapport_pdf(nom_client, date_str, evts, par_gravite, snmp, topo)
        except Exception:
            logger.exception('network_diag: génération PDF rapport échouée, repli HTML')
    return _rapport_html(nom_client, date_str, evts, par_gravite, snmp, topo)


def _rapport_pdf(nom_client, date_str, evts, par_gravite, snmp, topo):
    from io import BytesIO
    from app import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                     getSampleStyleSheet, ParagraphStyle, colors, A4, mm)
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=15 * mm, leftMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle('h1', parent=styles['Heading1'], fontSize=20,
                        textColor=colors.HexColor('#0a0d12'))
    h2 = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=13,
                        textColor=colors.HexColor('#00558a'))
    small = ParagraphStyle('s', parent=styles['Normal'], fontSize=8,
                           textColor=colors.HexColor('#444'))
    story = [Paragraph(f"Diagnostic réseau — {nom_client}", h1),
             Paragraph(date_str, small), Spacer(1, 8)]

    resume = ' · '.join(f"{n} {g}" for g, n in par_gravite.items()) or "aucun évènement actif"
    story += [Paragraph("Résumé", h2), Paragraph(resume, styles['Normal']), Spacer(1, 10)]

    if evts:
        story.append(Paragraph("Évènements actifs", h2))
        data = [["Gravité", "Catégorie", "Description", "Occ."]]
        for e in evts:
            data.append([e['gravite'], libelle_categorie(e['categorie']),
                         Paragraph(e['titre'], small), str(e['nb_occurrences'])])
        t = Table(data, colWidths=[22 * mm, 34 * mm, 100 * mm, 12 * mm])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0a0d12')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#ccc')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
        story += [t, Spacer(1, 10)]

        story.append(Paragraph("Pistes de remédiation", h2))
        for cat in dict.fromkeys(e['categorie'] for e in evts):
            r = remediation(cat)
            if not r:
                continue
            story.append(Paragraph(f"<b>{libelle_categorie(cat)}</b> — {r['cause']}", small))
            for c in r['corriger']:
                story.append(Paragraph(f"&nbsp;&nbsp;• {c}", small))
            story.append(Spacer(1, 4))

    if snmp.get('equipements'):
        story += [Spacer(1, 6), Paragraph("État des ports (SNMP)", h2)]
        for eq in snmp['equipements']:
            story.append(Paragraph(f"<b>{eq['ip']}</b> — {len(eq['ports'])} port(s)", small))

    if topo.get('equipements'):
        story += [Spacer(1, 6), Paragraph("Topologie découverte", h2)]
        for eq in topo['equipements']:
            lignes = [f"{p['port_nom']}: " + ', '.join(
                h['appareil_nom'] or h['mac'] for h in p['hotes'])
                for p in eq['ports'] if p['hotes']]
            story.append(Paragraph(f"<b>{eq['ip']}</b>: " + ' | '.join(lignes[:12]), small))

    story += [Spacer(1, 14), Paragraph("ParcInfo — rapport généré automatiquement", small)]
    doc.build(story)
    buf.seek(0)
    ts = date_str.replace(':', '').replace(' ', '-')[:15]
    return buf.getvalue(), 'application/pdf', f'diagnostic-reseau-{ts}.pdf'


def _rapport_html(nom_client, date_str, evts, par_gravite, snmp, topo):
    from html import escape as e
    resume = ' · '.join(f"{n} {g}" for g, n in par_gravite.items()) or "aucun évènement actif"
    lignes = ''.join(
        f"<tr><td>{e2['gravite']}</td><td>{e(libelle_categorie(e2['categorie']))}</td>"
        f"<td>{e(e2['titre'])}</td><td>{e2['nb_occurrences']}</td></tr>" for e2 in evts)
    remed = ''
    for cat in dict.fromkeys(x['categorie'] for x in evts):
        r = remediation(cat)
        if r:
            remed += (f"<h4>{e(libelle_categorie(cat))}</h4><p><em>{e(r['cause'])}</em></p><ul>"
                      + ''.join(f"<li>{e(c)}</li>" for c in r['corriger']) + "</ul>")
    html = (f"<html><head><meta charset='utf-8'><title>Diagnostic réseau — {e(nom_client)}</title>"
            f"<style>body{{font-family:Arial;margin:2rem}}table{{border-collapse:collapse}}"
            f"td,th{{border:1px solid #ccc;padding:4px 8px;font-size:13px}}</style></head><body>"
            f"<h1>Diagnostic réseau — {e(nom_client)}</h1><p>{e(date_str)}</p>"
            f"<h2>Résumé</h2><p>{e(resume)}</p>"
            f"<h2>Évènements actifs</h2><table><tr><th>Gravité</th><th>Catégorie</th>"
            f"<th>Description</th><th>Occ.</th></tr>{lignes}</table>"
            f"<h2>Pistes de remédiation</h2>{remed}"
            f"<p style='color:#888;margin-top:2rem'>ParcInfo — rapport généré automatiquement</p>"
            f"</body></html>")
    ts = date_str.replace(':', '').replace(' ', '-')[:15]
    return html, 'text/html; charset=utf-8', f'diagnostic-reseau-{ts}.html'


# ════════════════════════════════════════════════════════════════════════════
#  Orchestration : snapshot + persistance
# ════════════════════════════════════════════════════════════════════════════

_diag_status = {
    'running': False, 'progress': 0, 'message': '', 'client_id': None,
    'findings': [], 'avertissements': [], 'run_id': None, 'fin': None,
}
_diag_lock = threading.Lock()

_diag_moniteur_state = {'running': False, 'last_cycle': None, 'cycle_count': 0}


def statut_snapshot() -> dict:
    with _diag_lock:
        return dict(_diag_status)


def lancer_snapshot(client_id: int, plage: str = '', avec_capture=None) -> bool:
    """Démarre un snapshot en thread détaché. False si un run est déjà en cours."""
    with _diag_lock:
        if _diag_status['running']:
            return False
        _diag_status.update({'running': True, 'progress': 0, 'client_id': client_id,
                             'message': 'Initialisation…', 'findings': [],
                             'avertissements': [], 'run_id': None, 'fin': None})
    threading.Thread(target=_run_snapshot, args=(client_id, plage, avec_capture),
                     daemon=True, name='DiagReseauSnapshot').start()
    return True


def _maj_statut(**kw):
    with _diag_lock:
        _diag_status.update(kw)


def _run_snapshot(client_id: int, plage: str, avec_capture):
    debut = _now_z()
    t0 = time.time()
    findings, avertissements = [], []
    if avec_capture is None:
        avec_capture = str(_cfg('diag_capture_active', '0')) == '1'

    seuil_perte = _cfg_float('diag_seuil_perte_pct', 5)
    seuil_gigue = _cfg_float('diag_seuil_jitter_ms', 30)
    seuil_bc = _cfg_int('diag_seuil_broadcast_pps', 150)
    duree_capture = _cfg_int('diag_snapshot_duree_s', 20)
    passerelle = _passerelle_defaut()

    try:
        _maj_statut(progress=10, message='Analyse des tables ARP (conflits d’adresses)…')
        findings += detecter_conflits_ip(passerelle)

        _maj_statut(progress=30, message='Test de qualité de liaison (passerelle, DNS)…')
        cibles = _cibles_ping(client_id, passerelle)
        stats_liaison = []
        findings += mesurer_qualite_liaison(cibles, seuil_perte, seuil_gigue, collecte=stats_liaison)
        enregistrer_metriques_liaison(client_id, stats_liaison)

        _maj_statut(progress=50, message='Contrôle de la résolution DNS…')
        serveur_dns = next((c['ip'] for c in cibles if c.get('role') == 'dns'), '')
        findings += verifier_dns(serveur_dns)

        _maj_statut(progress=60, message='Recherche d’un serveur DHCP non autorisé…')
        attendus = re.split(r'[,;\s]+', str(_cfg('diag_dhcp_serveurs_attendus', '') or ''))
        findings += detecter_dhcp_pirate(attendus)

        _maj_statut(progress=70, message='Détection des conflits de noms réseau…')
        findings += detecter_conflits_noms(client_id)

        if str(_cfg('diag_snmp_actif', '0')) == '1':
            _maj_statut(progress=76, message='Interrogation SNMP des équipements réseau…')
            findings += interroger_equipements_client(client_id)
            if str(_cfg('diag_topologie_active', '0')) == '1':
                _maj_statut(progress=84, message='Cartographie de topologie L2…')
                findings += decouvrir_topologie(client_id)

        _maj_statut(progress=88, message='Analyse des tendances (baseline)…')
        findings += evaluer_baseline(client_id)

        capture_utilisee = False
        if avec_capture:
            etat = etat_capture()
            if etat['disponible']:
                _maj_statut(progress=80, message=f'Capture passive ({duree_capture} s)…')
                findings += capture_passive(duree_capture, {'broadcast_pps': seuil_bc})
                capture_utilisee = True
            else:
                avertissements.append(f"Capture passive indisponible : {etat['motif']}")
        elif str(_cfg('diag_capture_active', '0')) != '1':
            avertissements.append("Capture passive (palier 2) désactivée dans les réglages")

        _maj_statut(progress=92, message='Enregistrement des évènements…')
        nb_nouveaux = _enregistrer_evenements(client_id, findings, 'capture' if capture_utilisee else 'actif')

        run_id = _enregistrer_run(client_id, debut, _now_z(), int(time.time() - t0),
                                  'snapshot', plage, capture_utilisee,
                                  {'nb_findings': len(findings), 'nb_nouveaux': nb_nouveaux,
                                   'cibles': [c.get('ip') for c in cibles],
                                   'avertissements': avertissements})
        _maj_statut(progress=100, running=False, message=f'Terminé — {len(findings)} constat(s)',
                    findings=findings, avertissements=avertissements, run_id=run_id, fin=_now_z())
    except Exception as e:
        logger.exception('network_diag: snapshot en échec')
        _maj_statut(running=False, progress=100, message=f'Erreur : {e}',
                    findings=findings, avertissements=avertissements, fin=_now_z())


def _purger_anciens(conn, client_id: int):
    """Purge paresseuse (modèle historique_max_jours) : évènements RÉSOLUS et
    runs plus vieux que diag_reseau_max_jours. Les évènements non résolus ne
    sont jamais supprimés par l'âge — un problème persistant reste visible."""
    try:
        jours = _cfg_int('diag_reseau_max_jours', 30)
        if jours <= 0:
            return
        from datetime import timedelta
        limite = (datetime.now(timezone.utc) - timedelta(days=jours)).isoformat(
            timespec='seconds').replace('+00:00', 'Z')
        conn.execute(
            "DELETE FROM diag_reseau_evenements WHERE client_id=? AND resolu=1 "
            "AND COALESCE(date_resolu, derniere_occurrence) < ?", (client_id, limite))
        conn.execute("DELETE FROM diag_reseau_runs WHERE client_id=? AND debut < ?",
                     (client_id, limite))
        # Relevés SNMP au-delà de la fenêtre d'âge : les deltas ne comparent
        # jamais qu'au relevé le plus récent (quelques minutes), sans risque.
        conn.execute("DELETE FROM diag_snmp_releves WHERE client_id=? AND horodatage < ?",
                     (client_id, limite))
        conn.execute("DELETE FROM diag_metriques WHERE client_id=? AND horodatage < ?",
                     (client_id, limite))
    except Exception:
        logger.debug('network_diag: purge par âge en échec', exc_info=True)


def _enregistrer_run(client_id, debut, fin, duree_s, mode, plage, capture, resume: dict) -> int:
    try:
        from database import get_db
        conn = get_db()
        _purger_anciens(conn, client_id)
        cur = conn.execute(
            "INSERT INTO diag_reseau_runs "
            "(client_id, debut, fin, duree_s, mode, plage, capture_utilisee, resume_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (client_id, debut, fin, duree_s, mode, plage or '', 1 if capture else 0,
             json.dumps(resume, ensure_ascii=False)))
        conn.commit()
        rid = cur.lastrowid
        conn.close()
        return rid
    except Exception:
        logger.exception('network_diag: enregistrement du run impossible')
        return 0


def _appareil_pour_finding(conn, client_id: int, details: dict):
    """Rattache l'évènement à un appareil du client par IP ou MAC, si possible."""
    ips = [details.get('ip'), details.get('equipement')] + list(details.get('ips', []) or [])
    macs = ([details.get('mac'), details.get('mac_apres'), details.get('mac_attendue')]
            + list(details.get('macs', []) or []))
    for ip in [x for x in ips if x]:
        row = conn.execute(
            "SELECT id FROM appareils WHERE client_id=? AND adresse_ip=? LIMIT 1",
            (client_id, ip)).fetchone()
        if row:
            return row[0]
    for mac in [_norm_mac(x) for x in macs if x]:
        row = conn.execute(
            "SELECT id FROM appareils WHERE client_id=? AND LOWER(REPLACE(adresse_mac,'-',':'))=? LIMIT 1",
            (client_id, mac)).fetchone()
        if row:
            return row[0]
    return None


def _enregistrer_evenements(client_id: int, findings: list, source: str) -> int:
    """Upsert par signature. Retourne le nombre d'évènements nouveaux."""
    if not findings:
        return 0
    from database import get_db
    now = _now_z()
    nouveaux = 0
    critiques_nouveaux = []
    conn = get_db()
    try:
        for f in findings:
            sig = f['signature']
            existant = conn.execute(
                "SELECT id, resolu, nb_occurrences FROM diag_reseau_evenements "
                "WHERE client_id=? AND signature=?", (client_id, sig)).fetchone()
            details = json.dumps(f.get('details', {}), ensure_ascii=False)
            appareil_id = f.get('appareil_id') or _appareil_pour_finding(conn, client_id, f.get('details', {}))
            if existant:
                conn.execute(
                    "UPDATE diag_reseau_evenements SET derniere_occurrence=?, "
                    "nb_occurrences=nb_occurrences+1, gravite=?, titre=?, details_json=?, "
                    "source=?, appareil_id=COALESCE(appareil_id, ?), resolu=0, "
                    "date_resolu=CASE WHEN resolu=1 THEN NULL ELSE date_resolu END "
                    "WHERE id=?",
                    (now, f['gravite'], f['titre'], details, source, appareil_id, existant[0]))
            else:
                conn.execute(
                    "INSERT INTO diag_reseau_evenements "
                    "(client_id, horodatage, gravite, categorie, titre, details_json, source, "
                    " signature, appareil_id, resolu, premiere_occurrence, derniere_occurrence, nb_occurrences) "
                    "VALUES (?,?,?,?,?,?,?,?,?,0,?,?,1)",
                    (client_id, now, f['gravite'], f['categorie'], f['titre'], details,
                     source, sig, appareil_id, now, now))
                nouveaux += 1
                if f['gravite'] == 'critique':
                    critiques_nouveaux.append(f)
        conn.commit()
    except Exception:
        logger.exception('network_diag: enregistrement des évènements impossible')
    finally:
        conn.close()
    if critiques_nouveaux:
        _alerter_email(client_id, critiques_nouveaux)
    return nouveaux


def _alerter_email(client_id: int, findings: list):
    """E-mail sur nouvel évènement critique (opt-in : diag_alerte_email +
    destinataire). Best-effort, ne bloque jamais le cycle."""
    if str(_cfg('diag_alerte_email', '0')) != '1':
        return
    dest = str(_cfg('diag_alerte_destinataire', '') or '').strip()
    if '@' not in dest:
        return
    try:
        from html import escape as _esc
        from app import _send_email
        from database import get_db
        conn = get_db()
        row = conn.execute('SELECT nom FROM clients WHERE id=?', (client_id,)).fetchone()
        conn.close()
        # Les titres d'évènements embarquent des données non fiables du LAN
        # (hostname NetBIOS, reverse-DNS, MAC) — échappées avant insertion HTML.
        nom_client = _esc(row[0]) if row else f'client {client_id}'

        def _ligne(f):
            r = remediation(f['categorie'])
            cause = f"<br><span style=\"color:#666;font-size:.9em\">{_esc(r['cause'])}</span>" if r else ''
            return (f"<li><strong>{_esc(libelle_categorie(f['categorie']))}</strong> — "
                    f"{_esc(f['titre'])}{cause}</li>")
        lignes = ''.join(_ligne(f) for f in findings)
        corps = (f"<html><body style=\"font-family:Arial\">"
                 f"<h2>Diagnostic réseau — {nom_client}</h2>"
                 f"<p>{len(findings)} nouvel(le)(s) alerte(s) critique(s) détectée(s) :</p>"
                 f"<ul>{lignes}</ul>"
                 f"<p><em>Détail dans ParcInfo → Inventaire → Diagnostic réseau.</em></p>"
                 f"</body></html>")
        sujet = f"🩺 ParcInfo — alerte réseau critique ({row[0] if row else client_id})"
        _send_email(dest, sujet, corps)
    except Exception:
        logger.debug('network_diag: envoi e-mail alerte en échec', exc_info=True)


# ════════════════════════════════════════════════════════════════════════════
#  Surveillance continue (thread démon, modèle _watchdog_loop)
# ════════════════════════════════════════════════════════════════════════════

def _moniteur_cycle():
    if str(_cfg('diag_surveillance_active', '0')) != '1':
        return
    try:
        from database import get_db
        conn = get_db()
        clients = [r[0] for r in conn.execute(
            "SELECT DISTINCT client_id FROM appareils WHERE adresse_ip!='' AND adresse_ip IS NOT NULL"
        ).fetchall()]
        conn.close()
    except Exception:
        return
    if not clients:
        return

    # Ne surveiller que les clients dont le réseau est joignable depuis ce poste
    try:
        from app import _reseaux_locaux_actuels, _appareil_sur_reseau_courant
        reseaux = _reseaux_locaux_actuels()
    except Exception:
        reseaux, _appareil_sur_reseau_courant = set(), None

    seuil_perte = _cfg_float('diag_seuil_perte_pct', 5)
    seuil_gigue = _cfg_float('diag_seuil_jitter_ms', 30)
    seuil_bc = _cfg_int('diag_seuil_broadcast_pps', 150)
    avec_capture = str(_cfg('diag_capture_active', '0')) == '1'
    avec_snmp = str(_cfg('diag_snmp_actif', '0')) == '1'
    avec_topo = str(_cfg('diag_topologie_active', '0')) == '1'
    passerelle = _passerelle_defaut()

    capture_faite = False
    for cid in clients:
        try:
            from database import get_db
            conn = get_db()
            row = conn.execute('SELECT plage_ip_locale FROM parc_general WHERE client_id=?',
                               (cid,)).fetchone()
            conn.close()
            plage = (row[0] if row else '') or ''
            if _appareil_sur_reseau_courant and reseaux and not _appareil_sur_reseau_courant('', plage, reseaux):
                continue

            findings = detecter_conflits_ip(passerelle, releves=1)
            cibles = _cibles_ping(cid, passerelle)
            stats_liaison = []
            findings += mesurer_qualite_liaison(cibles, seuil_perte, seuil_gigue, n=10,
                                                collecte=stats_liaison)
            enregistrer_metriques_liaison(cid, stats_liaison)
            findings += detecter_conflits_noms(cid)
            if avec_snmp:
                findings += interroger_equipements_client(cid)
                if avec_topo:
                    findings += decouvrir_topologie(cid)
            findings += evaluer_baseline(cid)
            src = 'actif'
            if avec_capture and not capture_faite and etat_capture()['disponible']:
                findings += capture_passive(_cfg_int('diag_snapshot_duree_s', 20),
                                            {'broadcast_pps': seuil_bc})
                capture_faite, src = True, 'capture'
            _enregistrer_evenements(cid, findings, src if src == 'capture' else 'actif')
            conn = get_db()
            _purger_anciens(conn, cid)
            conn.commit()
            conn.close()
        except Exception:
            logger.debug('network_diag: cycle moniteur — client %s en échec', cid, exc_info=True)

    _diag_moniteur_state['last_cycle'] = _now_z()
    _diag_moniteur_state['cycle_count'] += 1


def _moniteur_loop():
    _diag_moniteur_state['running'] = True
    time.sleep(15)  # laisser l'app finir de démarrer
    while True:
        try:
            _moniteur_cycle()
        except Exception:
            logger.debug('network_diag: _moniteur_cycle', exc_info=True)
        time.sleep(max(60, _cfg_int('diag_intervalle_s', 300)))


def etat_moniteur() -> dict:
    d = dict(_diag_moniteur_state)
    d['active'] = str(_cfg('diag_surveillance_active', '0')) == '1'
    d['intervalle_s'] = _cfg_int('diag_intervalle_s', 300)
    return d


_moniteur_thread = threading.Thread(target=_moniteur_loop, daemon=True, name='DiagReseauMoniteur')
_moniteur_thread.start()
