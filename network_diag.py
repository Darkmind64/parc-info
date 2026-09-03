"""
network_diag.py — Module de diagnostic réseau de ParcInfo (7 paliers).

Référence détaillée : ``DIAGNOSTIC_RESEAU.md`` (paliers, catégories, OIDs, clés
de config, tables). Résumé :

  1. Diagnostic *actif* (aucune dépendance) : conflits d'adresses IP, qualité de
     liaison (perte/gigue/latence), passerelle/DNS, conflits de noms, DHCP pirate.
  2. Capture *passive* de trames via ``scapy`` (OFF par défaut) : ARP spoofing,
     MAC flapping, tempêtes de broadcast, DHCP multiples, BPDU STP, RA IPv6,
     retransmissions TCP.
  3. Interrogation *SNMP* (v1/v2c lecture seule) des switchs/routeurs/NAS :
     compteurs par port, duplex, erreurs CRC, saturation, flapping.
  4. Topologie *L2* (FDB bridge-MIB + LLDP), recoupée avec la baie de brassage.
  5. *Tendances & baseline* : historique dans ``diag_metriques``, alerte sur
     dégradation relative à la référence.
  6. *Rapport & remédiation* : PDF/HTML + playbook par catégorie.
  7. *Wi-Fi* (côté poste, ``netsh``/``iw``/``system_profiler``) + *onduleurs*
     (UPS-MIB RFC 1628 + repli APC).

Modes : snapshot à la demande (``lancer_snapshot``) et surveillance continue
(``_moniteur_loop``, thread démon démarré à l'import). Évènements historisés
dans ``diag_reseau_evenements``, dédoublonnés par ``signature``.

Aucun import de ``app`` au niveau module (import circulaire) : les helpers d'app
sont importés paresseusement.
"""

from __future__ import annotations

import collections
import hashlib
import ipaddress
import json
import logging
import math
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
    'wifi_signal_faible':    'avertissement',
    'wifi_canal_sature':     'avertissement',
    'wifi_ap_suspect':       'critique',
    'wifi_bande_2ghz':       'info',
    'wifi_debit_faible':     'info',
    'ups_sur_batterie':      'critique',
    'ups_batterie_faible':   'critique',
    'ups_surcharge':         'avertissement',
    'ups_batterie_usee':     'avertissement',
    'ups_alarme':            'avertissement',
    'ups_secteur_instable':  'info',
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
    'wifi_signal_faible':     "Signal Wi-Fi faible (poste)",
    'wifi_canal_sature':      "Canal Wi-Fi encombré",
    'wifi_ap_suspect':        "Point d'accès Wi-Fi suspect (evil twin)",
    'wifi_bande_2ghz':        "Wi-Fi en 2.4 GHz alors que 5 GHz est disponible",
    'wifi_debit_faible':      "Débit Wi-Fi négocié faible",
    'ups_sur_batterie':       "Onduleur sur batterie (coupure secteur)",
    'ups_batterie_faible':    "Onduleur — batterie faible / autonomie critique",
    'ups_surcharge':          "Onduleur en surcharge",
    'ups_batterie_usee':      "Onduleur — batterie à remplacer",
    'ups_alarme':             "Onduleur — alarme active",
    'ups_secteur_instable':   "Onduleur — tension d'entrée hors plage",
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
# ifType « port physique ethernet » : 6 (ethernetCsmacd) est la norme, mais des
# switchs réels annoncent encore 117 (gigabitEthernet, déprécié), 62/69
# (fast/100BaseFX), 7 (iso88023Csmacd) — HP ProCurve annonce 117 par ex.
_IFTYPE_ETHERNET = frozenset({6, 7, 62, 69, 117})

_TYPES_EQUIP_SNMP = ('Switch', 'Switch/AP', 'Routeur/Pare-feu', 'NAS', 'Onduleur / UPS',
                     'Borne Wi-Fi', 'Box internet (FAI)', 'Pont Wi-Fi')
_TYPE_UPS = 'Onduleur / UPS'


def _snmp_walk(oid_base, ip, communautes):
    try:
        from app import _snmp_walk as _w
        return _w(ip, oid_base, communautes) or {}
    except Exception:
        return {}


def _oid_tuple(s):
    try:
        return tuple(int(x) for x in s.split('.'))
    except (ValueError, AttributeError):
        return ()


def _snmp_walk_octets(oid_base, ip, communautes, timeout=1.5, max_rows=2000, port=161):
    """Parcourt `oid_base` et renvoie la valeur des OCTET STRING **en octets bruts**
    (`{suffixe: bytes}`) — `app._snmp_walk` la décode en UTF-8, ce qui détruit une
    MAC. Sert à lire `ipNetToMediaPhysAddress` (table ARP), `dot1dTpFdbAddress`,
    `ifPhysAddress`… GETBULK v2c d'abord (peu de paquets), repli GETNEXT v1.
    Gardes : vérifie error-status et exige un OID strictement croissant (agent
    bas de gamme qui boucle)."""
    import socket as _sock
    try:
        from app import (_ber_sequence, _ber_oid, _ber_entier, _ber_chaine,
                          _ber_lire_tlv, _ber_decoder_oid, _snmp_walk_reqid)
    except Exception:
        return {}
    if isinstance(communautes, str):
        communautes = [communautes]
    pref = oid_base if oid_base.endswith('.') else oid_base + '.'

    def _run(comm, bulk):
        res, courant, prev_t = {}, oid_base, ()
        with _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            for _ in range(max_rows):
                reqid = _snmp_walk_reqid()
                vb = _ber_sequence(0x30, _ber_oid(courant) + b'\x05\x00')
                if bulk:   # GetBulkRequest v2c : non-repeaters=0, max-repetitions=25
                    pdu = _ber_sequence(0xa5, _ber_entier(reqid) + _ber_entier(0)
                                        + _ber_entier(25) + _ber_sequence(0x30, vb))
                    ver = 1
                else:      # GetNextRequest v1
                    pdu = _ber_sequence(0xa1, _ber_entier(reqid) + _ber_entier(0)
                                        + _ber_entier(0) + _ber_sequence(0x30, vb))
                    ver = 0
                s.sendto(_ber_sequence(0x30, _ber_entier(ver) + _ber_chaine(comm) + pdu),
                         (ip, port))
                for _drain in range(4):
                    data, _ = s.recvfrom(65535)
                    _, corps, _ = _ber_lire_tlv(data, 0)
                    q = 0
                    _, _v, q = _ber_lire_tlv(corps, q)
                    _, _c, q = _ber_lire_tlv(corps, q)
                    tag_pdu, pdu_r, _ = _ber_lire_tlv(corps, q)
                    _, rid_b, _ = _ber_lire_tlv(pdu_r, 0)
                    if int.from_bytes(rid_b, 'big', signed=True) == reqid:
                        break
                else:
                    return res
                if tag_pdu != 0xa2:
                    return res
                q = 0
                _, _rid, q = _ber_lire_tlv(pdu_r, q)
                _, err, q = _ber_lire_tlv(pdu_r, q)
                _, _ei, q = _ber_lire_tlv(pdu_r, q)
                if err and int.from_bytes(err, 'big', signed=True) != 0:
                    return res
                _, vblist, q = _ber_lire_tlv(pdu_r, q)
                vp, dernier = 0, None
                while vp < len(vblist):
                    tvb, vbc, vp = _ber_lire_tlv(vblist, vp)
                    if tvb != 0x30:
                        return res
                    bp = 0
                    _, oid_brut, bp = _ber_lire_tlv(vbc, bp)
                    tag_val, val_brut, bp = _ber_lire_tlv(vbc, bp)
                    oid_ret = _ber_decoder_oid(oid_brut)
                    if not oid_ret.startswith(pref) or tag_val in (0x80, 0x81, 0x82):
                        return res
                    t = _oid_tuple(oid_ret)
                    if prev_t and t <= prev_t:        # OID non croissant : agent qui boucle
                        return res
                    prev_t = t
                    res[oid_ret[len(pref):]] = bytes(val_brut) if tag_val == 0x04 else None
                    dernier = oid_ret
                    if len(res) >= max_rows:
                        return res
                if dernier is None:
                    return res
                courant = dernier
        return res

    for comm in communautes:
        for bulk in (True, False):
            try:
                r = _run(comm, bulk)
                if r:
                    return r
            except Exception:
                continue
    return {}


def interroger_equipement(ip: str, communautes) -> dict | None:
    """Relevé SNMP d'un switch/routeur (ifTable + ifXTable + dot3StatsTable),
    par GETBULK multi-colonnes (`_snmp_bulk` → `app._snmp_bulk_cols`, repli
    GETNEXT par colonne) : ~10× moins de paquets qu'un walk par colonne.
    None si l'agent ne répond pas."""
    grp1 = _snmp_bulk(ip, [_OID_IF_DESCR, _OID_IF_TYPE, _OID_IF_NAME, _OID_IF_ALIAS,
                           _OID_IF_OPER, _OID_IF_ADMIN, _OID_IF_SPEED, _OID_IF_HIGHSPEED],
                      communautes)
    descr = grp1.get(_OID_IF_DESCR, {})
    if not descr:
        return None
    grp2 = _snmp_bulk(ip, [_OID_IF_IN_ERRORS, _OID_IF_OUT_ERRORS, _OID_IF_IN_DISCARDS,
                           _OID_IF_OUT_DISCARDS, _OID_IF_HCIN, _OID_IF_HCOUT,
                           _OID_IF_IN_OCTETS, _OID_IF_OUT_OCTETS], communautes)
    grp3 = _snmp_bulk(ip, [_OID_DOT3_ALIGN, _OID_DOT3_FCS, _OID_DOT3_LATECOLL,
                           _OID_DOT3_EXCCOLL, _OID_DOT3_DUPLEX], communautes)
    types, noms, alias = grp1.get(_OID_IF_TYPE, {}), grp1.get(_OID_IF_NAME, {}), grp1.get(_OID_IF_ALIAS, {})
    oper, admin = grp1.get(_OID_IF_OPER, {}), grp1.get(_OID_IF_ADMIN, {})
    speed, highspeed = grp1.get(_OID_IF_SPEED, {}), grp1.get(_OID_IF_HIGHSPEED, {})
    in_err, out_err = grp2.get(_OID_IF_IN_ERRORS, {}), grp2.get(_OID_IF_OUT_ERRORS, {})
    in_disc, out_disc = grp2.get(_OID_IF_IN_DISCARDS, {}), grp2.get(_OID_IF_OUT_DISCARDS, {})
    in_oct = grp2.get(_OID_IF_HCIN, {}) or grp2.get(_OID_IF_IN_OCTETS, {})
    out_oct = grp2.get(_OID_IF_HCOUT, {}) or grp2.get(_OID_IF_OUT_OCTETS, {})
    align, fcs = grp3.get(_OID_DOT3_ALIGN, {}), grp3.get(_OID_DOT3_FCS, {})
    late, exc, duplex = grp3.get(_OID_DOT3_LATECOLL, {}), grp3.get(_OID_DOT3_EXCCOLL, {}), grp3.get(_OID_DOT3_DUPLEX, {})

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
        if _i(types, idx, 6) not in _IFTYPE_ETHERNET:   # port physique ethernet uniquement
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
            f"SELECT id, adresse_ip, type_appareil FROM appareils WHERE client_id=? "
            f"AND type_appareil IN ({placeholders}) AND adresse_ip!='' AND adresse_ip IS NOT NULL",
            (client_id, *_TYPES_EQUIP_SNMP)).fetchall()
        conn.close()
    except Exception:
        return []
    ups_actif = str(_cfg('diag_ups_active', '1')) == '1'
    findings = []
    for appareil_id, ip, type_app in rows:
        try:
            if type_app == _TYPE_UPS:
                if not ups_actif:
                    continue
                ups = interroger_ups(ip, communautes)
                if ups is not None:
                    findings += _analyser_ups(client_id, ip, appareil_id, ups)
                continue
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
#  Palier 7a — supervision SNMP des onduleurs (UPS-MIB RFC 1628 + APC)
# ════════════════════════════════════════════════════════════════════════════

_OID_UPS_MODEL       = '1.3.6.1.2.1.33.1.1.2.0'
_OID_UPS_BATT_STATUS = '1.3.6.1.2.1.33.1.2.1.0'      # 2 normal | 3 low | 4 depleted
_OID_UPS_ON_BATT_S   = '1.3.6.1.2.1.33.1.2.2.0'
_OID_UPS_MIN_REMAIN  = '1.3.6.1.2.1.33.1.2.3.0'
_OID_UPS_CHARGE_PCT  = '1.3.6.1.2.1.33.1.2.4.0'
_OID_UPS_BATT_TEMP   = '1.3.6.1.2.1.33.1.2.7.0'
_OID_UPS_OUT_SOURCE  = '1.3.6.1.2.1.33.1.4.1.0'      # 3 normal | 5 battery | 6 booster | 7 reducer
_OID_UPS_IN_VOLT     = '1.3.6.1.2.1.33.1.3.3.1.3'    # walk (par ligne)
_OID_UPS_OUT_LOAD    = '1.3.6.1.2.1.33.1.4.4.1.5'    # walk (par ligne)
_OID_UPS_ALARMS      = '1.3.6.1.2.1.33.1.6.1.0'
_OID_APC_BATT_REPL   = '1.3.6.1.4.1.318.1.1.1.2.2.4.0'   # 2 = batterie à remplacer
_OID_APC_RUNTIME     = '1.3.6.1.4.1.318.1.1.1.2.2.3.0'   # TimeTicks

_UPS_SOURCE_TXT = {1: 'inconnu', 2: 'aucune', 3: 'secteur', 4: 'bypass',
                   5: 'batterie', 6: 'survolteur', 7: 'dévolteur'}
_UPS_BATT_TXT = {1: 'inconnu', 2: 'normale', 3: 'faible', 4: 'épuisée'}


def interroger_ups(ip: str, communautes) -> dict | None:
    """Interroge un onduleur en SNMP (UPS-MIB, repli APC). None si pas de réponse.

    Utilise _snmp_get_typed : la plupart des scalaires UPS-MIB utiles sont des
    INTEGER, que _snmp_get (OCTET STRING uniquement) laisserait tomber."""
    try:
        from app import _snmp_get_typed
    except Exception:
        return None
    scalaires = [_OID_UPS_MODEL, _OID_UPS_BATT_STATUS, _OID_UPS_ON_BATT_S,
                 _OID_UPS_MIN_REMAIN, _OID_UPS_CHARGE_PCT, _OID_UPS_BATT_TEMP,
                 _OID_UPS_OUT_SOURCE, _OID_UPS_ALARMS]
    rep = {}
    for comm in (communautes or ['public']):
        rep = _snmp_get_typed(ip, scalaires, comm, timeout=1.5)
        if rep:
            _comm_ok = comm
            break
    else:
        return None

    def _n(oid, defaut=None):
        v = rep.get(oid)
        try:
            return int(v)
        except (TypeError, ValueError):
            return defaut

    load = _snmp_walk(_OID_UPS_OUT_LOAD, ip, [_comm_ok])
    volt = _snmp_walk(_OID_UPS_IN_VOLT, ip, [_comm_ok])
    charge_pct = max((v for v in (_to_int(x) for x in load.values()) if v is not None), default=None)
    tension = next((v for v in (_to_int(x) for x in volt.values()) if v is not None), None)
    apc = _snmp_get_typed(ip, [_OID_APC_BATT_REPL, _OID_APC_RUNTIME], _comm_ok, timeout=1.2)

    autonomie = _n(_OID_UPS_MIN_REMAIN)
    if autonomie is None:
        rt = _to_int(apc.get(_OID_APC_RUNTIME))
        if rt:
            autonomie = rt // 6000  # TimeTicks (1/100 s) -> minutes

    return {
        'modele': (rep.get(_OID_UPS_MODEL) or '').strip(),
        'source': _n(_OID_UPS_OUT_SOURCE, 1),
        'source_txt': _UPS_SOURCE_TXT.get(_n(_OID_UPS_OUT_SOURCE, 1), '?'),
        'charge_pct': charge_pct,
        'autonomie_min': autonomie,
        'batterie_pct': _n(_OID_UPS_CHARGE_PCT),
        'batterie_statut': _n(_OID_UPS_BATT_STATUS, 1),
        'batterie_statut_txt': _UPS_BATT_TXT.get(_n(_OID_UPS_BATT_STATUS, 1), '?'),
        'sur_batterie_s': _n(_OID_UPS_ON_BATT_S, 0) or 0,
        'temp_c': _n(_OID_UPS_BATT_TEMP),
        'tension_entree': tension,
        'remplacer_batterie': _to_int(apc.get(_OID_APC_BATT_REPL)) == 2,
        'alarmes': _n(_OID_UPS_ALARMS, 0) or 0,
        'ts': time.time(),
    }


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _analyser_ups(client_id: int, ip: str, appareil_id, ups: dict) -> list:
    seuil_charge = _cfg_int('diag_ups_seuil_charge_pct', 80)
    seuil_auto = _cfg_int('diag_ups_seuil_autonomie_min', 10)
    findings = []
    modele = ups.get('modele') or 'onduleur'
    base = {'equipement': ip, 'modele': modele}

    from database import get_db
    conn = get_db()
    try:
        if ups.get('charge_pct') is not None:
            _enregistrer_metrique(conn, client_id, 'ups_charge_pct', ip, ups['charge_pct'], ups['ts'])
        if ups.get('autonomie_min') is not None:
            _enregistrer_metrique(conn, client_id, 'ups_autonomie_min', ip, ups['autonomie_min'], ups['ts'])
        conn.commit()
    except Exception:
        logger.exception('network_diag: métriques UPS impossibles')
    finally:
        conn.close()

    if ups.get('source') == 5 or ups.get('sur_batterie_s', 0) > 0:
        findings.append(_finding(
            'ups_sur_batterie',
            f"{modele} ({ip}) est sur batterie depuis {ups.get('sur_batterie_s', 0)} s",
            {**base, 'sur_batterie_s': ups.get('sur_batterie_s', 0),
             'autonomie_min': ups.get('autonomie_min')}, ip))

    autonomie = ups.get('autonomie_min')
    # autonomie == 0 : certains agents renvoient 0 sur secteur (valeur non fiable)
    if ups.get('batterie_statut') in (3, 4) or (autonomie is not None and 0 < autonomie < seuil_auto) \
            or (ups.get('batterie_pct') is not None and 0 < ups['batterie_pct'] < 30):
        findings.append(_finding(
            'ups_batterie_faible',
            f"{modele} ({ip}) : batterie {ups.get('batterie_statut_txt', '?')}, "
            f"autonomie {autonomie if autonomie is not None else '?'} min",
            {**base, 'batterie_statut': ups.get('batterie_statut_txt'),
             'autonomie_min': autonomie, 'batterie_pct': ups.get('batterie_pct')}, ip))

    if ups.get('charge_pct') is not None and ups['charge_pct'] > seuil_charge:
        findings.append(_finding(
            'ups_surcharge', f"{modele} ({ip}) : charge de sortie à {ups['charge_pct']} %",
            {**base, 'charge_pct': ups['charge_pct']}, ip))

    if ups.get('remplacer_batterie') or (ups.get('temp_c') is not None and ups['temp_c'] > 40):
        raison = "l'onduleur signale une batterie à remplacer" if ups.get('remplacer_batterie') \
            else f"température batterie {ups.get('temp_c')} °C"
        findings.append(_finding(
            'ups_batterie_usee', f"{modele} ({ip}) : {raison}",
            {**base, 'remplacer_batterie': ups.get('remplacer_batterie'), 'temp_c': ups.get('temp_c')}, ip))

    if ups.get('alarmes', 0) > 0:
        findings.append(_finding(
            'ups_alarme', f"{modele} ({ip}) : {ups['alarmes']} alarme(s) active(s)",
            {**base, 'alarmes': ups['alarmes']}, ip))

    v = ups.get('tension_entree')
    if v is not None and v > 0:
        ok = (195 <= v <= 255) or (95 <= v <= 130)
        if not ok:
            findings.append(_finding(
                'ups_secteur_instable', f"{modele} ({ip}) : tension d'entrée {v} V hors plage",
                {**base, 'tension_entree': v}, ip))

    if appareil_id:
        for f in findings:
            f['appareil_id'] = appareil_id
    return findings


def etat_ups(client_id: int) -> dict:
    """Dernier état connu de chaque onduleur (métriques + évènements actifs)."""
    from database import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, adresse_ip, nom_machine FROM appareils WHERE client_id=? "
            "AND type_appareil=? AND adresse_ip!='' AND adresse_ip IS NOT NULL",
            (client_id, _TYPE_UPS)).fetchall()
        evts = {}
        for cat, dj in conn.execute(
                "SELECT categorie, details_json FROM diag_reseau_evenements WHERE client_id=? "
                "AND resolu=0 AND categorie LIKE 'ups_%'", (client_id,)):
            d = _json_charge(dj)
            evts.setdefault(d.get('equipement'), []).append(
                {'categorie': cat, 'libelle': libelle_categorie(cat)})
        ups_list = []
        for aid, ip, nom in rows:
            def _last(cat):
                r = conn.execute(
                    "SELECT valeur FROM diag_metriques WHERE client_id=? AND categorie=? "
                    "AND cible=? ORDER BY epoch DESC LIMIT 1", (client_id, cat, ip)).fetchone()
                return r[0] if r else None
            ups_list.append({
                'appareil_id': aid, 'ip': ip, 'nom': nom,
                'charge_pct': _last('ups_charge_pct'),
                'autonomie_min': _last('ups_autonomie_min'),
                'findings': evts.get(ip, []),
            })
    finally:
        conn.close()
    return {'actif': str(_cfg('diag_ups_active', '1')) == '1'
            and str(_cfg('diag_snmp_actif', '0')) == '1',
            'onduleurs': ups_list}


# ════════════════════════════════════════════════════════════════════════════
#  Palier 7b — diagnostic Wi-Fi (côté poste)
# ════════════════════════════════════════════════════════════════════════════

def _canal_vers_bande(canal):
    try:
        c = int(canal)
    except (TypeError, ValueError):
        return ''
    return '2.4 GHz' if 1 <= c <= 14 else ('5 GHz' if c < 200 else '6 GHz')


def _signal_pct_vers_dbm(pct):
    try:
        return round(int(str(pct).rstrip('%')) / 2 - 100)
    except (TypeError, ValueError):
        return None


_wifi_cache = {'ts': 0.0, 'val': None}
_WIFI_CACHE_TTL = 60  # s — le scan (`netsh … networks` / `iw scan`) est coûteux
                      # et peut perturber le Wi-Fi du poste ; l'UI le sollicite
                      # toutes les 30 s et chaque cycle moniteur.


def etat_wifi(forcer=False) -> dict:
    """État Wi-Fi du poste + AP visibles. {connecte:False, motif} si indisponible.
    Résultat mis en cache _WIFI_CACHE_TTL s (le scan est lent et intrusif)."""
    if not forcer and _wifi_cache['val'] is not None \
            and (time.time() - _wifi_cache['ts']) < _WIFI_CACHE_TTL:
        return _wifi_cache['val']
    try:
        if IS_WINDOWS:
            val = _wifi_windows()
        elif platform.system() == 'Darwin':
            val = _wifi_macos()
        else:
            val = _wifi_linux()
    except Exception:
        logger.debug('network_diag: lecture Wi-Fi impossible', exc_info=True)
        val = {'connecte': False, 'motif': 'lecture impossible'}
    _wifi_cache.update(ts=time.time(), val=val)
    return val


def _wifi_windows() -> dict:
    out = _run(['netsh', 'wlan', 'show', 'interfaces'], timeout=8).stdout
    if 'no wireless' in out.lower() or 'aucune interface' in out.lower() or not out.strip():
        return {'connecte': False, 'motif': 'aucun adaptateur Wi-Fi'}

    def champ(*cles):
        for ligne in out.splitlines():
            for cle in cles:
                if re.match(rf'^\s*{re.escape(cle)}\b[^:]*:', ligne, re.I):
                    return ligne.split(':', 1)[1].strip()
        return ''

    ssid = champ('SSID')
    if not ssid or champ('State', 'État').lower() not in ('connected', 'connecté', 'connectée'):
        etat = {'connecte': False, 'motif': 'non connecté'}
    else:
        signal = champ('Signal')
        debit_rx = champ('Receive rate', 'Réception', 'Vitesse de réception')
        debit_tx = champ('Transmit rate', 'Transmission', 'Vitesse de transmission')
        canal = champ('Channel', 'Canal')
        etat = {
            'connecte': True, 'ssid': ssid, 'bssid': champ('BSSID').lower(),
            'rssi_dbm': _signal_pct_vers_dbm(signal), 'signal_pct': signal,
            'canal': _to_int(canal), 'bande': _canal_vers_bande(canal),
            'debit_mbps': _to_int((debit_rx or debit_tx or '').split()[0] if (debit_rx or debit_tx) else None),
            'radio': champ('Radio type', 'Type de radio'),
        }
    etat['aps'] = _wifi_windows_aps()
    return etat


def _wifi_windows_aps() -> list:
    try:
        out = _run(['netsh', 'wlan', 'show', 'networks', 'mode=bssid'], timeout=10).stdout
    except Exception:
        return []
    aps, ssid_courant = [], ''
    for ligne in out.splitlines():
        m = re.match(r'^\s*SSID\s+\d+\s*:\s*(.*)$', ligne, re.I)
        if m:
            ssid_courant = m.group(1).strip()
            continue
        m = re.match(r'^\s*BSSID\s+\d+\s*:\s*([0-9a-f:]{17})', ligne, re.I)
        if m:
            aps.append({'ssid': ssid_courant, 'bssid': m.group(1).lower(),
                        'rssi_dbm': None, 'signal_pct': None, 'canal': None, 'bande': ''})
            continue
        if aps:
            m = re.match(r'^\s*(Signal|Signal)\s*:\s*(\d+)%', ligne, re.I)
            if m:
                aps[-1]['signal_pct'] = m.group(2) + '%'
                aps[-1]['rssi_dbm'] = _signal_pct_vers_dbm(m.group(2))
            m = re.match(r'^\s*(Channel|Canal)\s*:\s*(\d+)', ligne, re.I)
            if m:
                aps[-1]['canal'] = int(m.group(2))
                aps[-1]['bande'] = _canal_vers_bande(m.group(2))
    return aps


def _wifi_linux() -> dict:
    dev = ''
    try:
        for ligne in _run(['iw', 'dev'], timeout=5).stdout.splitlines():
            m = re.match(r'\s*Interface\s+(\S+)', ligne)
            if m:
                dev = m.group(1)
                break
    except Exception:
        pass
    if not dev:
        return {'connecte': False, 'motif': 'aucun adaptateur Wi-Fi'}
    link = _run(['iw', 'dev', dev, 'link'], timeout=5).stdout
    if 'Not connected' in link:
        etat = {'connecte': False, 'motif': 'non connecté'}
    else:
        ssid = (re.search(r'SSID:\s*(.+)', link) or [None, ''])[1].strip()
        rssi = re.search(r'signal:\s*(-?\d+)', link)
        freq = re.search(r'freq:\s*(\d+)', link)
        rx = re.search(r'rx bitrate:\s*([\d.]+)', link)
        canal = _freq_vers_canal(int(freq.group(1))) if freq else None
        etat = {'connecte': True, 'ssid': ssid,
                'bssid': (re.search(r'Connected to ([0-9a-f:]{17})', link) or [None, ''])[1],
                'rssi_dbm': int(rssi.group(1)) if rssi else None,
                'canal': canal, 'bande': _canal_vers_bande(canal),
                'debit_mbps': int(float(rx.group(1))) if rx else None, 'radio': ''}
    etat['aps'] = _wifi_linux_aps(dev)
    return etat


def _freq_vers_canal(freq):
    if 2412 <= freq <= 2484:
        return 14 if freq == 2484 else (freq - 2407) // 5
    if 5000 <= freq <= 5900:
        return (freq - 5000) // 5
    if 5955 <= freq <= 7115:
        return (freq - 5950) // 5
    return None


def _wifi_linux_aps(dev) -> list:
    aps = []
    try:
        out = _run(['iw', 'dev', dev, 'scan'], timeout=12).stdout
    except Exception:
        return aps
    bloc = {}
    for ligne in out.splitlines():
        m = re.match(r'BSS ([0-9a-f:]{17})', ligne)
        if m:
            if bloc.get('bssid'):
                aps.append(bloc)
            bloc = {'bssid': m.group(1), 'ssid': '', 'rssi_dbm': None, 'canal': None, 'bande': ''}
        elif bloc:
            m = re.search(r'signal:\s*(-?[\d.]+)', ligne)
            if m:
                bloc['rssi_dbm'] = int(float(m.group(1)))
            m = re.search(r'SSID:\s*(.+)', ligne)
            if m:
                bloc['ssid'] = m.group(1).strip()
            m = re.search(r'DS Parameter set: channel (\d+)', ligne) or re.search(r'primary channel:\s*(\d+)', ligne)
            if m:
                bloc['canal'] = int(m.group(1))
                bloc['bande'] = _canal_vers_bande(m.group(1))
    if bloc.get('bssid'):
        aps.append(bloc)
    return aps


def _wifi_macos() -> dict:
    try:
        out = _run(['system_profiler', '-json', 'SPAirPortDataType'], timeout=15).stdout
        data = json.loads(out)
        ifaces = data.get('SPAirPortDataType', [{}])[0].get('spairport_airport_interfaces', [])
    except Exception:
        return {'connecte': False, 'motif': 'lecture impossible (macOS)'}
    if not ifaces:
        return {'connecte': False, 'motif': 'aucun adaptateur Wi-Fi'}
    cur = ifaces[0].get('spairport_current_network_information', {})
    if not cur:
        return {'connecte': False, 'motif': 'non connecté', 'aps': []}
    canal = _to_int(str(cur.get('spairport_network_channel', '')).split()[0]
                    if cur.get('spairport_network_channel') else None)
    etat = {'connecte': True, 'ssid': cur.get('_name', ''),
            'bssid': (cur.get('spairport_network_bssid') or '').lower(),
            'rssi_dbm': _to_int(str(cur.get('spairport_signal_noise', '')).split()[0]
                                if cur.get('spairport_signal_noise') else None),
            'canal': canal, 'bande': _canal_vers_bande(canal), 'debit_mbps': None, 'radio': ''}
    aps = []
    for res in ifaces[0].get('spairport_airport_other_local_wireless_networks', []):
        c = _to_int(str(res.get('spairport_network_channel', '')).split()[0]
                    if res.get('spairport_network_channel') else None)
        aps.append({'ssid': res.get('_name', ''), 'bssid': '', 'canal': c,
                    'bande': _canal_vers_bande(c), 'rssi_dbm': None})
    etat['aps'] = aps
    return etat


def diagnostiquer_wifi(client_id: int) -> list:
    if str(_cfg('diag_wifi_active', '1')) != '1':
        return []
    etat = etat_wifi()
    if not etat.get('connecte'):
        return []
    findings = []
    seuil_rssi = _cfg_int('diag_wifi_seuil_rssi', -72)
    seuil_aps = _cfg_int('diag_wifi_seuil_aps_canal', 4)
    ssid, bssid, canal, bande = etat.get('ssid'), etat.get('bssid'), etat.get('canal'), etat.get('bande')
    rssi = etat.get('rssi_dbm')
    aps = etat.get('aps') or []

    # RSSI dans les métriques (tendance)
    if rssi is not None and ssid:
        try:
            from database import get_db
            conn = get_db()
            try:
                _enregistrer_metrique(conn, client_id, 'wifi_rssi', ssid, rssi, time.time())
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    if rssi is not None and rssi <= seuil_rssi:
        findings.append(_finding(
            'wifi_signal_faible',
            f"Le poste est connecté à « {ssid} » avec un signal faible ({rssi} dBm)",
            {'ssid': ssid, 'rssi_dbm': rssi, 'canal': canal}, ssid, bssid or ''))

    if canal:
        chevauchants = {canal}
        if bande == '2.4 GHz':
            chevauchants = set(range(max(1, canal - 4), canal + 5))
        nb = sum(1 for a in aps if a.get('canal') in chevauchants and a.get('bssid') != bssid)
        if nb >= seuil_aps:
            findings.append(_finding(
                'wifi_canal_sature',
                f"Canal {canal} ({bande}) : {nb} autres points d'accès sur des canaux chevauchants",
                {'canal': canal, 'bande': bande, 'nb_aps': nb}, ssid, str(canal)))

    # AP suspect : SSID du parc diffusé par >= 2 fabricants RECONNUS distincts.
    # On n'alerte que sur des OUI resolus (2 prefixes inconnus differents sont
    # normaux : bornes dual-band/mesh derivent souvent des BSSID hors OUI).
    ssids_parc = _ssids_parc(client_id)
    if ssids_parc:
        par_ssid = {}
        for a in aps:
            if a.get('ssid') in ssids_parc and a.get('bssid'):
                par_ssid.setdefault(a['ssid'], set()).add(a['bssid'][:8])
        if bssid and ssid in ssids_parc:
            par_ssid.setdefault(ssid, set()).add(bssid[:8])
        for s, prefixes in par_ssid.items():
            vendors = {v for p in prefixes if (v := _vendor(p + ':00:00:00'))}
            if len(vendors) >= 2:
                findings.append(_finding(
                    'wifi_ap_suspect',
                    f"Le SSID du parc « {s} » est diffusé par des équipements de fabricants différents "
                    f"({', '.join(sorted(vendors))}) — point d'accès pirate possible",
                    {'ssid': s, 'fabricants': sorted(vendors), 'bssids': sorted(prefixes)}, s))

    if bande == '2.4 GHz' and any(a.get('ssid') == ssid and a.get('bande') == '5 GHz' for a in aps):
        findings.append(_finding(
            'wifi_bande_2ghz',
            f"Le poste utilise « {ssid} » en 2.4 GHz alors que le 5 GHz est disponible",
            {'ssid': ssid}, ssid, 'bande'))

    return findings


def _ssids_parc(client_id: int) -> set:
    try:
        from database import get_db
        conn = get_db()
        try:
            row = conn.execute("SELECT wifi_ssid, wifi_ssid2 FROM parc_general WHERE client_id=?",
                               (client_id,)).fetchone()
        finally:
            conn.close()
        return {(row[0] or '').strip(), (row[1] or '').strip()} - {''} if row else set()
    except Exception:
        return set()


def diag_wifi_apercu(client_id: int) -> dict:
    """État Wi-Fi + occupation par canal, pour le panneau."""
    etat = etat_wifi()
    etat['actif'] = str(_cfg('diag_wifi_active', '1')) == '1'
    par_canal = {}
    for a in etat.get('aps', []) or []:
        if a.get('canal'):
            par_canal[a['canal']] = par_canal.get(a['canal'], 0) + 1
    etat['par_canal'] = dict(sorted(par_canal.items()))
    return etat


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
_OID_DOT1Q_VLAN_NAMES = '1.3.6.1.2.1.17.7.1.4.3.1.1'    # dot1qVlanStaticName : suffixe = VLAN id
_OID_ARP_PHYS         = '1.3.6.1.2.1.4.22.1.2'          # ipNetToMediaPhysAddress : ifIndex.ip -> MAC (table ARP)
_OID_ARP_PHYS_2       = '1.3.6.1.2.1.4.35.1.4'          # ipNetToPhysicalPhysAddress (IP-MIB moderne)
_OID_BRIDGE_BASE_MAC  = '1.3.6.1.2.1.17.1.1'            # dot1dBaseBridgeAddress
_OID_IF_PHYS_ADDR     = '1.3.6.1.2.1.2.2.1.6'           # ifPhysAddress
_FDB_VLAN_MAX         = 32                              # garde-fou : nb de VLAN sondés par contexte de communauté
_OID_LLDP_REM_CHASSIS_ST = '1.0.8802.1.1.2.1.4.1.1.4'   # lldpRemChassisIdSubtype
_OID_LLDP_REM_CHASSIS    = '1.0.8802.1.1.2.1.4.1.1.5'   # lldpRemChassisId (OCTET STRING, souvent MAC)
_OID_LLDP_REM_PORTID_ST  = '1.0.8802.1.1.2.1.4.1.1.6'   # lldpRemPortIdSubtype (3=MAC, 5=ifName, 7=local)
_OID_LLDP_REM_PORTID  = '1.0.8802.1.1.2.1.4.1.1.7'
_OID_LLDP_REM_SYSNAME = '1.0.8802.1.1.2.1.4.1.1.9'
_OID_LLDP_REM_SYSDESC = '1.0.8802.1.1.2.1.4.1.1.10'
_OID_LLDP_REM_CAP_EN  = '1.0.8802.1.1.2.1.4.1.1.12'     # lldpRemSysCapEnabled (BITS)
_OID_LLDP_LOC_PORTID  = '1.0.8802.1.1.2.1.3.7.1.3'      # lldpLocPortId : localPortNum -> nom d'interface locale
# CDP (CISCO-CDP-MIB, cdpCacheEntry) : index = cdpCacheIfIndex.cdpCacheDeviceIndex
_OID_CDP_ADDR     = '1.3.6.1.4.1.9.9.23.1.2.1.1.4'
_OID_CDP_DEVICE   = '1.3.6.1.4.1.9.9.23.1.2.1.1.6'
_OID_CDP_PORT     = '1.3.6.1.4.1.9.9.23.1.2.1.1.7'
_OID_CDP_PLATFORM = '1.3.6.1.4.1.9.9.23.1.2.1.1.8'
_OID_CDP_CAP      = '1.3.6.1.4.1.9.9.23.1.2.1.1.9'

_LLDP_CAP_BITS = ('other', 'repeater', 'bridge', 'wlan', 'router', 'phone', 'docsis', 'station')
_CDP_CAP_BITS  = ('router', 'bridge', 'bridge', 'bridge', 'host', 'igmp', 'repeater', 'phone')


def _bits_actifs(brut, noms):
    """OCTET STRING de type BITS -> set de libelles. Bit 0 = poids fort du 1er octet."""
    out = set()
    if not brut:
        return out
    for i, nom in enumerate(noms):
        octet, dec = i // 8, 7 - (i % 8)
        if octet < len(brut) and (brut[octet] >> dec) & 1:
            out.add(nom)
    return out


def _cdp_bits(brut):
    """cdpCacheCapabilities : entier 4 octets, bit 0 = router (LSB)."""
    out = set()
    try:
        v = int.from_bytes(brut, 'big') if isinstance(brut, (bytes, bytearray)) else int(brut)
    except (TypeError, ValueError):
        return out
    m = {0: 'router', 1: 'bridge', 2: 'bridge', 3: 'bridge', 4: 'host', 6: 'repeater', 7: 'phone'}
    for b, nom in m.items():
        if v & (1 << b):
            out.add(nom)
    return out


def _voisins_lldp_cdp(ip, communautes, infos):
    """Voisins directs d'un switch/routeur par LLDP puis CDP. Retourne
    `{ifIndex_local: {nom, port, port_subtype, caps:set, mac, platform, source}}`.
    caps ⊂ {bridge, router, wlan, phone, docsis, repeater, host, station}."""
    vois = {}

    # ── LLDP ──
    txt = lambda b: (b.decode('utf-8', 'replace').strip() if isinstance(b, (bytes, bytearray)) else str(b or '')).strip()
    loc = {}   # localPortNum -> nom d'interface locale (pour retrouver l'ifIndex)
    for suf, v in _snmp_walk_octets(_OID_LLDP_LOC_PORTID, ip, communautes, max_rows=400).items():
        try:
            loc[int(suf.split('.')[-1])] = txt(v)
        except (ValueError, IndexError):
            pass
    nom_vers_ifx = {}
    for ifx, m in infos.items():
        if m.get('nom'):
            nom_vers_ifx[str(m['nom'])] = ifx

    sysn = _snmp_walk(_OID_LLDP_REM_SYSNAME, ip, communautes)
    if sysn:
        pid_st = _snmp_walk(_OID_LLDP_REM_PORTID_ST, ip, communautes)
        pid = _snmp_walk_octets(_OID_LLDP_REM_PORTID, ip, communautes, max_rows=400)
        pdesc = _snmp_walk(_OID_LLDP_REM_SYSDESC, ip, communautes)
        cap = _snmp_walk_octets(_OID_LLDP_REM_CAP_EN, ip, communautes, max_rows=400)
        chid = _snmp_walk_octets(_OID_LLDP_REM_CHASSIS, ip, communautes, max_rows=400)
        for suf, nom_v in sysn.items():
            parts = suf.split('.')
            if len(parts) < 3:
                continue
            try:
                locnum = int(parts[1])
            except ValueError:
                continue
            ifx = nom_vers_ifx.get(loc.get(locnum, ''), locnum)
            st = 0
            try:
                st = int(pid_st.get(suf, 0))
            except (TypeError, ValueError):
                pass
            pv = pid.get(suf)
            if st == 3 and pv and len(pv) == 6:            # PortId = MAC
                port_s, port_sub = _mac_octets(pv), 'mac'
            else:
                port_s, port_sub = txt(pv), ('ifname' if st == 5 else 'local' if st == 7 else 'autre')
            cv = chid.get(suf)
            vois[ifx] = {'nom': txt(nom_v), 'port': port_s, 'port_subtype': port_sub,
                         'caps': _bits_actifs(cap.get(suf), _LLDP_CAP_BITS),
                         'mac': _mac_octets(cv) if cv and len(cv) == 6 else '',
                         'platform': txt(pdesc.get(suf, '')), 'source': 'lldp'}

    # ── CDP (complete, n'ecrase pas LLDP) ──
    dev = _snmp_walk(_OID_CDP_DEVICE, ip, communautes)
    if dev:
        port = _snmp_walk(_OID_CDP_PORT, ip, communautes)
        plat = _snmp_walk(_OID_CDP_PLATFORM, ip, communautes)
        cap = _snmp_walk_octets(_OID_CDP_CAP, ip, communautes, max_rows=400)
        addr = _snmp_walk_octets(_OID_CDP_ADDR, ip, communautes, max_rows=400)
        for suf, nom_v in dev.items():
            try:
                ifx = int(suf.split('.')[0])
            except (ValueError, IndexError):
                continue
            if ifx in vois:
                continue
            a = addr.get(suf)
            vois[ifx] = {'nom': txt(nom_v), 'port': txt(port.get(suf, '')), 'port_subtype': 'ifname',
                         'caps': _cdp_bits(cap.get(suf)),
                         'mac': '', 'platform': txt(plat.get(suf, '')),
                         'ip': ('.'.join(str(b) for b in a) if a and len(a) == 4 else ''),
                         'source': 'cdp'}
    return vois


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
                "type_lien, voisin_nom, voisin_port, horodatage, "
                "voisin_caps, voisin_mac, voisin_port_subtype, voisin_source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lignes)
            conn.commit()
            conn.close()
        except Exception:
            logger.exception('network_diag: écriture topologie impossible')
    return findings


def _topologie_equipement(client_id, equip_id, ip, communautes, inventaire, now):
    """Un switch : FDB -> port -> {mac, appareil} + voisins LLDP/CDP + recoupement baie."""
    equipement = interroger_equipement(ip, communautes)
    noms_ports, infos = {}, {}
    if equipement:
        for p in equipement['ports']:
            noms_ports[p['index']] = p['nom']
            infos[p['index']] = {'nom': p['nom'], 'alias': p.get('alias', ''), 'ethernet': True}

    # Table MAC : même relevé unifié que le cycle d'activité et « Deviner le
    # brassage » (bridge dot1q/dot1d + contexte VLAN + ARP + correction de forme
    # pour un agent buggé + réglage `diag_fdb_mode:<ip>`).
    par_if, _meta = _releve_mac_switch(ip, communautes, inventaire)
    par_port = {ifx: sorted(macs) for ifx, macs in par_if.items()}

    # voisins LLDP + CDP (capacités système, MAC du châssis, sous-type de PortID)
    voisins = _voisins_lldp_cdp(ip, communautes, infos)

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

    # ports qui n'ont PAS de MAC apprise mais un voisin LLDP/CDP (lien infra pur)
    tous_ifx = sorted(set(par_port) | set(voisins))
    lignes, findings = [], []
    for ifindex in tous_ifx:
        macs = sorted(set(par_port.get(ifindex, [])))
        port_nom = noms_ports.get(ifindex, f'if{ifindex}')
        vue_appareil_id, vue_nom = None, ''
        if len(macs) == 1 and macs[0] in inventaire:
            vue_appareil_id, vue_nom = inventaire[macs[0]]
        v = voisins.get(ifindex, {})
        v_caps = ','.join(sorted(v.get('caps', ())))
        v_mac = v.get('mac', '')
        # la MAC du châssis LLDP recoupe l'inventaire même sans FDB
        if not vue_appareil_id and v_mac and v_mac in inventaire:
            vue_appareil_id, vue_nom = inventaire[v_mac]
        rows_macs = macs or ([''] if v else [])
        for mac in rows_macs:
            inv = inventaire.get(mac) if mac else None
            lignes.append((client_id, ip, equip_id, ifindex, port_nom, mac,
                           inv[0] if inv else (vue_appareil_id if not mac else None),
                           inv[1] if inv else (vue_nom if not mac else ''),
                           _vendor(mac) if mac else '',
                           v.get('source') or ('mac' if mac else ''),
                           v.get('nom', ''), v.get('port', ''), now,
                           v_caps, v_mac, v.get('port_subtype', ''), v.get('source', '')))
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
    'wifi_rssi':      -60.0,   # dBm — n'alerte que si le signal est vraiment tombé bas
    'ups_autonomie_min': 20.0,  # min — n'alerte que si l'autonomie est vraiment courte
}
# Métriques où « plus bas = pire » (l'inverse du cas général) :
_METRIQUES_INVERSEES = {'wifi_rssi', 'ups_autonomie_min'}
# Perte absolue minimale (unités de la métrique) pour alerter sur ces métriques :
_DELTA_INVERSE = {'wifi_rssi': 12.0, 'ups_autonomie_min': 15.0}


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
            if categorie in _METRIQUES_INVERSEES:
                # « plus bas = pire » (RSSI Wi-Fi, autonomie onduleur) : on alerte
                # sur une perte absolue nette ET une valeur devenue basse.
                delta_min = _DELTA_INVERSE.get(categorie, 0)
                plancher = _METRIQUE_PLANCHER.get(categorie, 0)
                if mediane is None or (mediane - courant) < delta_min or courant > plancher:
                    continue
                findings.append(_finding(
                    'degradation_relative',
                    f"{_libelle_metrique(categorie)} {cible} : {courant:.0f} "
                    f"vs référence {mediane:.0f} ({courant - mediane:+.0f})",
                    {'categorie_metrique': categorie, 'cible': cible, 'valeur': round(courant, 1),
                     'reference': round(mediane, 1), 'delta': round(courant - mediane, 1)},
                    categorie, cible))
                continue
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
    'wifi_rssi': "Signal Wi-Fi", 'ups_charge_pct': "Charge onduleur",
    'ups_autonomie_min': "Autonomie onduleur",
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
    'wifi_signal_faible': {
        'cause': "Le poste capte mal le Wi-Fi : trop loin de la borne, obstacles (murs, métal), borne mal placée ou sous-dimensionnée.",
        'verifier': ["Mesurer le signal à différents endroits",
                     "Compter les murs/planchers entre le poste et la borne"],
        'corriger': ["Rapprocher la borne ou en ajouter une",
                     "Repasser en filaire les postes fixes",
                     "Vérifier la puissance d'émission et l'antenne de la borne"],
    },
    'wifi_canal_sature': {
        'cause': "Trop de réseaux Wi-Fi sur le même canal (ou des canaux qui se chevauchent en 2.4 GHz) : interférences, débit qui s'effondre aux heures de pointe.",
        'verifier': ["Lister l'occupation par canal (panneau Wi-Fi)",
                     "Regarder les réseaux des voisins"],
        'corriger': ["En 2.4 GHz, n'utiliser que les canaux 1, 6 ou 11",
                     "Privilégier le 5 GHz (bien plus de canaux)",
                     "Réduire la largeur de canal (20 MHz en 2.4 GHz)"],
    },
    'wifi_ap_suspect': {
        'cause': "Un point d'accès diffuse le nom de votre réseau Wi-Fi mais provient d'un équipement d'un autre fabricant : borne pirate (evil twin) cherchant à capter des identifiants, ou borne grand public ajoutée sans coordination.",
        'verifier': ["Identifier physiquement toutes les bornes légitimes et leurs MAC",
                     "Comparer aux BSSID vus pour ce SSID"],
        'corriger': ["Localiser et débrancher la borne non autorisée",
                     "Passer en WPA2/WPA3-Entreprise (802.1X) pour empêcher l'usurpation",
                     "Activer la détection de rogue AP sur le contrôleur Wi-Fi si disponible"],
    },
    'wifi_bande_2ghz': {
        'cause': "Le poste s'est accroché au 2.4 GHz (plus lent, plus encombré) alors que le 5 GHz du même réseau est à portée.",
        'verifier': ["Vérifier que la carte Wi-Fi du poste supporte le 5 GHz",
                     "Regarder si le 5 GHz est activé sur la borne pour ce SSID"],
        'corriger': ["Activer le band steering sur la borne",
                     "Forcer le 5 GHz dans les propriétés de la carte (postes fixes)"],
    },
    'wifi_debit_faible': {
        'cause': "Le débit Wi-Fi négocié est très en dessous de ce que la carte peut faire : signal faible, canal encombré, norme ancienne, largeur de canal réduite.",
        'verifier': ["Corréler avec le signal et l'occupation du canal",
                     "Vérifier la norme (Wi-Fi 4/5/6) des deux côtés"],
        'corriger': ["Traiter le signal / le canal en premier",
                     "Mettre à jour le pilote de la carte Wi-Fi",
                     "Remplacer une borne ou une carte trop ancienne"],
    },
    'ups_sur_batterie': {
        'cause': "Coupure ou forte anomalie du secteur en cours : l'onduleur alimente la charge sur sa batterie. L'autonomie est limitée.",
        'verifier': ["Confirmer la coupure (disjoncteur, autres équipements)",
                     "Regarder l'autonomie restante estimée"],
        'corriger': ["Si la coupure dure, arrêter proprement les serveurs avant l'épuisement",
                     "Vérifier le disjoncteur / l'alimentation de la baie",
                     "Contacter le fournisseur d'électricité si la coupure est externe"],
    },
    'ups_batterie_faible': {
        'cause': "La batterie de l'onduleur est faible ou l'autonomie estimée est très courte : batterie vieillissante, charge trop élevée, ou coupure prolongée.",
        'verifier': ["Regarder l'âge de la batterie et la date du dernier remplacement",
                     "Vérifier la charge de sortie"],
        'corriger': ["Planifier le remplacement du bloc batterie",
                     "Réduire la charge branchée sur l'onduleur",
                     "Lancer un test d'autonomie une fois le secteur rétabli"],
    },
    'ups_surcharge': {
        'cause': "La charge branchée sur l'onduleur dépasse le seuil de confort : autonomie fortement réduite, risque de coupure de l'onduleur en cas de pic.",
        'verifier': ["Lister ce qui est branché sur l'onduleur",
                     "Comparer la puissance totale à la capacité (VA/W) de l'onduleur"],
        'corriger': ["Débrancher les équipements non critiques de l'onduleur",
                     "Répartir sur un second onduleur ou en installer un plus puissant"],
    },
    'ups_batterie_usee': {
        'cause': "L'onduleur signale une batterie à remplacer (ou une température anormale) : la batterie ne tiendra pas l'autonomie annoncée.",
        'verifier': ["Confirmer via l'interface de l'onduleur",
                     "Vérifier la ventilation autour de l'onduleur"],
        'corriger': ["Commander et poser un bloc batterie neuf (référence constructeur)",
                     "Après remplacement, réinitialiser le compteur d'âge et tester l'autonomie"],
    },
    'ups_alarme': {
        'cause': "L'onduleur remonte une ou plusieurs alarmes actives : la cause exacte est dans son interface (surchauffe, ventilateur, bypass, auto-test échoué…).",
        'verifier': ["Se connecter à la carte réseau / l'écran de l'onduleur pour lire l'alarme",
                     "Noter le code d'alarme"],
        'corriger': ["Traiter selon le code (ventilation, remplacement de pièce, contrat de maintenance)",
                     "Ouvrir un ticket auprès du constructeur si sous garantie"],
    },
    'ups_secteur_instable': {
        'cause': "La tension d'entrée mesurée par l'onduleur sort de la plage normale : réseau électrique instable, neutre défectueux, ou onduleur qui bascule souvent en survolteur/dévolteur.",
        'verifier': ["Historiser la tension d'entrée sur quelques jours",
                     "Corréler avec des bascules fréquentes sur batterie"],
        'corriger': ["Faire contrôler l'installation électrique par un électricien",
                     "Envisager un onduleur online (double conversion) sur les charges sensibles"],
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


_JOURS_CRON = {'lun': 'mon', 'mar': 'tue', 'mer': 'wed', 'jeu': 'thu',
               'ven': 'fri', 'sam': 'sat', 'dim': 'sun'}


def parse_rapport_cron(chaine: str):
    """'08:00' -> {hour:8, minute:0} (quotidien) ; 'lun 08:00' -> + day_of_week.
    None si vide/invalide."""
    chaine = (chaine or '').strip().lower()
    if not chaine:
        return None
    jour = None
    m = re.match(r'^([a-zéûù]{3})\s+(\d{1,2}):(\d{2})$', chaine)
    if m:
        jour = _JOURS_CRON.get(m.group(1))
        h, mn = int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r'^(\d{1,2}):(\d{2})$', chaine)
        if not m:
            return None
        h, mn = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        return None
    args = {'hour': h, 'minute': mn}
    if jour:
        args['day_of_week'] = jour
    return args


def tache_rapport_planifie():
    """Job cron : envoie le rapport de diagnostic à diag_alerte_destinataire
    pour chaque client ayant des appareils. No-op si SMTP/destinataire absent."""
    dest = str(_cfg('diag_alerte_destinataire', '') or '').strip()
    if '@' not in dest:
        logger.info('network_diag: rapport planifié — pas de destinataire, ignoré')
        return
    try:
        from database import get_db
        from app import _envoyer_email_piece_jointe
        conn = get_db()
        clients = conn.execute(
            "SELECT DISTINCT c.id, c.nom FROM clients c "
            "JOIN appareils a ON a.client_id = c.id").fetchall()
        conn.close()
    except Exception:
        logger.exception('network_diag: rapport planifié — préparation impossible')
        return
    for cid, nom in clients:
        try:
            contenu, mimetype, fichier = generer_rapport_diag(cid)
            if isinstance(contenu, str):
                contenu = contenu.encode('utf-8')
            corps = (f"<html><body style=\"font-family:Arial\">"
                     f"<p>Rapport de diagnostic réseau pour <strong>{nom}</strong>, "
                     f"généré automatiquement par ParcInfo.</p></body></html>")
            _envoyer_email_piece_jointe(dest, f"🩺 ParcInfo — diagnostic réseau ({nom})",
                                        corps, fichier, contenu, mimetype)
            logger.info('network_diag: rapport planifié envoyé pour %s', nom)
        except Exception:
            logger.exception('network_diag: rapport planifié — échec pour client %s', cid)


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


def lancer_snapshot(client_id: int, plage: str = '', avec_capture=None, rapide=None) -> bool:
    """Démarre un snapshot en thread détaché. False si un run est déjà en cours."""
    with _diag_lock:
        if _diag_status['running']:
            return False
        _diag_status.update({'running': True, 'progress': 0, 'client_id': client_id,
                             'message': 'Initialisation…', 'findings': [],
                             'avertissements': [], 'run_id': None, 'fin': None, 'phases': {}})
    threading.Thread(target=_run_snapshot, args=(client_id, plage, avec_capture, rapide),
                     daemon=True, name='DiagReseauSnapshot').start()
    return True


def _maj_statut(**kw):
    with _diag_lock:
        _diag_status.update(kw)


def _run_snapshot(client_id: int, plage: str, avec_capture, rapide=None):
    debut = _now_z()
    t0 = time.time()
    findings, avertissements = [], []
    phases = {}
    if avec_capture is None:
        avec_capture = str(_cfg('diag_capture_active', '0')) == '1'
    if rapide is None:
        rapide = str(_cfg('diag_snapshot_rapide', '0')) == '1'
    budget = _cfg_int('diag_snapshot_budget_s', 120)

    seuil_perte = _cfg_float('diag_seuil_perte_pct', 5)
    seuil_gigue = _cfg_float('diag_seuil_jitter_ms', 30)
    seuil_bc = _cfg_int('diag_seuil_broadcast_pps', 150)
    duree_capture = _cfg_int('diag_snapshot_duree_s', 20)
    n_ping = 8 if rapide else 20
    passerelle = _passerelle_defaut()

    def _phase(nom, progress, libelle):
        _maj_statut(progress=progress,
                    message=f"{libelle} — {int(time.time() - t0)} s")
        return time.time()

    def _fin_phase(nom, tp):
        phases[nom] = round(time.time() - tp, 1)

    def _budget_ok(quoi):
        if budget and (time.time() - t0) > budget:
            avertissements.append(f"{quoi} sautée (budget de {budget} s dépassé)")
            return False
        return True

    try:
        tp = _phase('arp', 10, 'Analyse des tables ARP (conflits d’adresses)')
        findings += detecter_conflits_ip(passerelle, releves=1 if rapide else 2)
        _fin_phase('arp', tp)

        tp = _phase('liaison', 30, 'Test de qualité de liaison (passerelle, DNS)')
        cibles = _cibles_ping(client_id, passerelle)
        stats_liaison = []
        findings += mesurer_qualite_liaison(cibles, seuil_perte, seuil_gigue,
                                            n=n_ping, collecte=stats_liaison)
        enregistrer_metriques_liaison(client_id, stats_liaison)
        _fin_phase('liaison', tp)

        if str(_cfg('diag_wifi_active', '1')) == '1':
            tp = _phase('wifi', 40, 'Diagnostic Wi-Fi du poste')
            findings += diagnostiquer_wifi(client_id)
            _fin_phase('wifi', tp)

        tp = _phase('dns', 50, 'Contrôle de la résolution DNS')
        serveur_dns = next((c['ip'] for c in cibles if c.get('role') == 'dns'), '')
        findings += verifier_dns(serveur_dns)
        _fin_phase('dns', tp)

        tp = _phase('dhcp', 60, 'Recherche d’un serveur DHCP non autorisé')
        attendus = re.split(r'[,;\s]+', str(_cfg('diag_dhcp_serveurs_attendus', '') or ''))
        findings += detecter_dhcp_pirate(attendus)
        _fin_phase('dhcp', tp)

        tp = _phase('noms', 70, 'Détection des conflits de noms réseau')
        findings += detecter_conflits_noms(client_id)
        _fin_phase('noms', tp)

        if str(_cfg('diag_snmp_actif', '0')) == '1' and _budget_ok('Interrogation SNMP'):
            tp = _phase('snmp', 76, 'Interrogation SNMP des équipements réseau')
            findings += interroger_equipements_client(client_id)
            _fin_phase('snmp', tp)
            if str(_cfg('diag_topologie_active', '0')) == '1' and _budget_ok('Cartographie de topologie'):
                tp = _phase('topologie', 84, 'Cartographie de topologie L2')
                findings += decouvrir_topologie(client_id)
                _fin_phase('topologie', tp)

        tp = _phase('baseline', 88, 'Analyse des tendances (baseline)')
        findings += evaluer_baseline(client_id)
        _fin_phase('baseline', tp)

        capture_utilisee = False
        if rapide and avec_capture:
            avertissements.append("Capture passive ignorée (mode rapide)")
        elif avec_capture and _budget_ok('Capture passive'):
            etat = etat_capture()
            if etat['disponible']:
                tp = _phase('capture', 90, f'Capture passive ({duree_capture} s)')
                findings += capture_passive(duree_capture, {'broadcast_pps': seuil_bc})
                _fin_phase('capture', tp)
                capture_utilisee = True
            else:
                avertissements.append(f"Capture passive indisponible : {etat['motif']}")
        elif not avec_capture and str(_cfg('diag_capture_active', '0')) != '1':
            avertissements.append("Capture passive (palier 2) désactivée dans les réglages")

        _maj_statut(progress=92, message='Enregistrement des évènements…')
        nb_nouveaux = _enregistrer_evenements(client_id, findings, 'capture' if capture_utilisee else 'actif')

        run_id = _enregistrer_run(client_id, debut, _now_z(), int(time.time() - t0),
                                  'snapshot', plage, capture_utilisee,
                                  {'nb_findings': len(findings), 'nb_nouveaux': nb_nouveaux,
                                   'cibles': [c.get('ip') for c in cibles],
                                   'rapide': rapide, 'phases': phases,
                                   'avertissements': avertissements})
        _maj_statut(progress=100, running=False,
                    message=f'Terminé en {int(time.time() - t0)} s — {len(findings)} constat(s)',
                    findings=findings, avertissements=avertissements, run_id=run_id,
                    phases=phases, fin=_now_z())
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
            _n = 8 if str(_cfg('diag_snapshot_rapide', '0')) == '1' else 10
            findings += mesurer_qualite_liaison(cibles, seuil_perte, seuil_gigue, n=_n,
                                                collecte=stats_liaison)
            enregistrer_metriques_liaison(cid, stats_liaison)
            findings += detecter_conflits_noms(cid)
            if str(_cfg('diag_wifi_active', '1')) == '1':
                findings += diagnostiquer_wifi(cid)
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


# ════════════════════════════════════════════════════════════════════════════
#  Vue d'activité de la baie — LEDs SNMP « live »
# ════════════════════════════════════════════════════════════════════════════
#
# Pendant qu'un opérateur regarde la page « baie de brassage » (ou le widget
# tableau de bord « Activité réseau »), on interroge les compteurs d'octets et
# d'erreurs par port des switchs de la baie toutes les ~3 s et on calcule le
# débit par delta. Le navigateur envoie un « battement » (GET /api/baie/activite) ;
# tant qu'il bat, un thread démon poll en tâche de fond ; sans battement, le
# thread se rendort. Aucune persistance : tout vit en mémoire et disparaît quand
# plus personne ne regarde. Les tendances (diag_metriques) restent alimentées
# par le cycle de surveillance normal (5 min), inchangé.

_ACTIVITE_INTERVAL      = 3.0     # s — cadence de poll
_ACTIVITE_TTL_HEARTBEAT = 20.0    # s — au-delà, on cesse de poller ce client
_ACTIVITE_MAX_SWITCHS   = 8       # garde-fou par cycle
_ACTIVITE_PURGE         = 120.0   # s — oubli des compteurs/résultats d'un client parti
_ACTIVITE_NOMS_TTL      = 90.0    # s — cache des noms/types d'interface (quasi statiques)
_ACTIVITE_MANQUES_STALE = 3       # relevés manqués consécutifs avant l'état « stale »
_ACTIVITE_EMA           = 0.5     # lissage exponentiel du débit / pps
_ACTIVITE_BPS_MINI      = 500     # bit/s (défaut de `diag_baie_activite_bps_mini`) en dessous : pas de clignotement
_ACTIVITE_DEBOUNCE_ON   = 1       # cycles avant de PASSER en « traffic » (1 = immédiat, comme une vraie LED)
_ACTIVITE_PPS_PEGGE_MAX = 50_000  # pps — sur un switch dont les compteurs d'octets sont bloqués, ceux de
                                  # paquets le sont souvent aussi : au-delà, on ne les croit pas
_ACTIVITE_PPS_MAX_PORT  = 15_000_000   # pps — plafond absolu (10 GbE ≈ 14,9 Mpps) : au-delà = artefact de compteur
_ACTIVITE_DEBOUNCE      = 2       # cycles consécutifs demandant idle<->traffic avant de basculer (anti-scintillement)
_ACTIVITE_FDB_TTL       = 150.0   # s — cache de la table d'apprentissage MAC (bridge-MIB) par switch
_ACTIVITE_FDB_BACKOFF   = 45.0    # s — attente avant de retenter un walk FDB infructueux
_ACTIVITE_FDB_PERIME    = 900.0   # s — au-delà, on cesse de servir une FDB périmée (une MAC bouge peu)
_ACTIVITE_VOISINS_MAX   = 6       # noms d'appareils listés dans l'infobulle d'un port (au-delà : « +N »)
_CPT_SENTINELLE_32      = {2**31 - 1, 2**32 - 1}   # valeurs "compteur indisponible" de certains agents

# OIDs PoE (POWER-ETHERNET-MIB, RFC 3621) — table pethPsePortTable indexée
# groupe.port (PAS l'ifIndex ; le composant « port » = le port physique, qu'on
# recoupe donc directement au numéro de port de la baie).
_OID_POE_DETECT  = '1.3.6.1.2.1.105.1.1.1.6'    # 1 disabled 2 searching 3 deliveringPower 4 fault ...
_OID_POE_CLASS   = '1.3.6.1.2.1.105.1.1.1.7'    # 1..5 -> classe 0..4
_OID_POE_MAIN_W  = '1.3.6.1.2.1.105.1.3.1.2.1'  # pethMainPsePower : budget W (groupe 1)
_OID_POE_CONS_W  = '1.3.6.1.2.1.105.1.3.1.4.1'  # pethMainPseConsumptionPower : W consommés (groupe 1)
_POE_DETECT_TXT  = {1: 'désactivé', 2: 'recherche', 3: 'alimenté', 4: 'défaut', 5: 'test', 6: 'défaut'}
_POE_CLASSE_W    = {1: 15.4, 2: 4.0, 3: 7.0, 4: 15.4, 5: 30.0}   # classe 0..4 -> W max indicatif

# OIDs paquets (ifXTable / ifTable), lecture seule — complètent _OID_IF_* du palier 3
_OID_IF_HCIN_UCAST  = '1.3.6.1.2.1.31.1.1.1.7'
_OID_IF_HCOUT_UCAST = '1.3.6.1.2.1.31.1.1.1.11'
_OID_IF_IN_UCAST    = '1.3.6.1.2.1.2.2.1.11'
_OID_IF_OUT_UCAST   = '1.3.6.1.2.1.2.2.1.17'
# Paquets NON unicast (broadcast + multicast) — Counter32, deprecated mais quasi
# toujours peuplé. Une vraie LED de switch clignote sur CES trames aussi (ARP,
# STP, mDNS, DHCP…), un port de VLAN calme n'a souvent que ça.
_OID_IF_IN_NUCAST   = '1.3.6.1.2.1.2.2.1.12'
_OID_IF_OUT_NUCAST  = '1.3.6.1.2.1.2.2.1.18'

_activite_lock       = threading.Lock()
_activite_thread_lock = threading.Lock()   # démarrage idempotent du thread (course page+modale)
# Écrits SOUS _activite_lock (lus par les threads de requête Flask) :
_activite_heartbeat  = {}   # client_id -> epoch du dernier battement
_activite_resultat   = {}   # client_id -> dict prêt pour l'UI (LEDs)
_activite_detail     = {}   # client_id -> {ts, switchs:[...], ports:[...], interfaces:[...]}
_activite_journal    = collections.deque(maxlen=250)   # évènements, récent en tête
_activite_calib      = None  # (défini plus bas) — SOUS _activite_lock : partagé loop <-> requête
# Touchés UNIQUEMENT par le thread _activite_loop (_cycle_activite + purge, séquentiels) :
_activite_prev       = {}   # (client_id, ip, ifindex) -> {compteurs, etat, *_ema, manques, ts, sut}
_activite_switch_ok  = {}   # (client_id, ip) -> bool (dernier relevé répondu ?)
_activite_etat_mappe = {}   # (client_id, slot_id) -> {clé: dernier etat} (transitions journal)
_activite_hist       = {}   # (client_id, ip, ifindex) -> deque[(ts, bps, pps)] (sparkline moniteur)
_activite_sysinfo    = {}   # ip -> {'ts', 'sysname', 'sysdescr'} (cache ~10 min)
_activite_sut        = {}   # (client_id, ip) -> (sysUpTime_ticks, epoch) : dt exact via l'horloge de l'agent
_activite_fdb        = {}   # ip -> (epoch, {ifindex: set(mac normalisée)}) : FDB bridge-MIB, cache _ACTIVITE_FDB_TTL
_activite_fdb_baseport = {} # ip -> (epoch, {bridge_port: ifIndex}) : dot1dBasePortIfIndex, quasi statique
_activite_fdb_dialecte = {} # ip -> 'dot1q' | 'dot1d' | 'dot1q-vlan' : quel jeu FDB répond
_activite_fdb_echec  = {}   # ip -> epoch du dernier walk FDB infructueux (backoff)
_activite_infra_mac  = {}   # ip -> (epoch, set(mac)) : MAC propres du switch (base bridge + ifPhysAddress)
_activite_echecs     = {}   # client_id -> nb d'échecs consécutifs de _cycle_activite
_ACTIVITE_HIST_MAX   = 60   # échantillons conservés par port pour la sparkline
_ACTIVITE_SYSINFO_TTL = 600.0
_ACTIVITE_ECHECS_MAX = 3    # au-delà : on publie {'actif': False, 'motif': 'erreur_interne'}
# _activite_noms : loop + threads DiagBaieNoms détachés → le flag 'maj_en_cours'
# est testé-et-posé sous _activite_lock (voir _noms_interfaces) ; le reste est
# de l'écriture atomique de clé (remplacement de dict).
_activite_noms       = {}   # ip -> {'ts', 'infos': {ifindex: {'nom','alias','ethernet'}}, 'maj_en_cours', 'retry_after'}
_activite_thread     = None

_OID_SYS_UPTIME = '1.3.6.1.2.1.1.3'    # sysUpTime (TimeTicks, 1/100 s) — base : GETBULK renvoie .0
_OID_SYS_DESCR_B = '1.3.6.1.2.1.1.1'
_OID_SYS_NAME_B  = '1.3.6.1.2.1.1.5'

_capture_baie_lock   = threading.Lock()
_capture_baie_status = {'running': False, 'progress': 0, 'message': '',
                        'resultat': None, 'client_id': None, 'fin': None}


def _journal(message, niveau='info', cible=''):
    """Ajoute une ligne au journal du moniteur. Dédoublonne un message identique
    consécutif (incrémente son compteur au lieu d'empiler une nouvelle ligne).
    NE PAS appeler sous _activite_lock (verrou non réentrant)."""
    with _activite_lock:
        if _activite_journal and _activite_journal[0]['message'] == message \
                and _activite_journal[0]['cible'] == cible:
            _activite_journal[0]['n'] += 1
            _activite_journal[0]['ts'] = _now_z()
            return
        _activite_journal.appendleft({'ts': _now_z(), 'niveau': niveau,
                                      'cible': cible, 'message': message, 'n': 1})


_TYPES_EQUIP_BAIE_RESEAU = ('Switch', 'Switch/AP', 'Routeur/Pare-feu')
_TYPES_BANDEAU = ('Bandeau RJ', 'Patch Panel')


def _switchs_baie(conn, client_id):
    """Slots de baie interrogeables en SNMP : un slot associé à un appareil doté
    d'une IP. On accepte soit un `type_appareil` réseau connu, soit un slot dont
    le `type_equipement` (étiquette de la baie) dit « Switch »/« Routeur » même
    si l'appareil lié est typé autrement — le seul vrai prérequis SNMP est l'IP.
    UN slot par entrée (un switch 48 ports affiché en deux éléments de rack de
    24 → deux entrées, même IP) ; l'appelant mutualise le relevé SNMP par IP.
    Limité à `_ACTIVITE_MAX_SWITCHS` IP distinctes."""
    ph = ','.join('?' * len(_TYPES_EQUIP_SNMP))
    ph2 = ','.join('?' * len(_TYPES_EQUIP_BAIE_RESEAU))
    rows = conn.execute(
        f"SELECT s.id AS slot_id, s.appareil_id, a.adresse_ip, a.nom_machine "
        f"FROM baie_slots s JOIN appareils a ON a.id = s.appareil_id "
        f"WHERE s.client_id=? AND COALESCE(a.adresse_ip,'') <> '' "
        f"AND (a.type_appareil IN ({ph}) OR s.type_equipement IN ({ph2})) "
        f"ORDER BY s.position",
        (client_id, *_TYPES_EQUIP_SNMP, *_TYPES_EQUIP_BAIE_RESEAU)).fetchall()
    ips, switchs = [], []
    for slot_id, aid, ip, nom in rows:
        if ip not in ips:
            if len(ips) >= _ACTIVITE_MAX_SWITCHS:
                continue
            ips.append(ip)
        switchs.append({'slot_id': slot_id, 'appareil_id': aid, 'ip': ip, 'nom': nom or ip})
    return switchs


_RE_PORT_FIN  = re.compile(r'(\d+)(?:[.:]\d+)?\s*$')
_RE_PORT_MOT  = re.compile(r'\b(?:port|interface|if|eth|gi|te|fa|xe|ge|xge|swp)\s*:?\s*(\d+)', re.I)


def _port_physique_depuis_nom(nom):
    """Numéro de port physique déduit du nom (ifName) ou de la description
    (ifDescr) d'interface SNMP :
      'GigabitEthernet1/0/12' → 12 ; 'Gi1/0/12' → 12 ; 'ethernet1/12' → 12 ;
      'xe-0/0/12.0' → 12 ; 'swp12' / 'eth7' → 7 ;
      'Port: 1 Gigabit - Level' (Netgear/Realtek) → 1 ; 'Port 12' → 12.
    None si aucun numéro exploitable."""
    if not nom:
        return None
    s = str(nom).strip()
    tail = s.rsplit('/', 1)[-1] if '/' in s else s     # dernier segment : Gi1/0/12 → '12'
    m = _RE_PORT_FIN.search(tail)
    if m:                                              # numéro en fin de nom
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m = _RE_PORT_MOT.search(s)                         # « Port: 1 … », « eth3 … »
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _numero_logique(numero):
    """Retire l'offset de plage d'un numéro de port de baie (SFP 1001+ → 1,
    WAN 2001+ → 1). Les ports RJ (1-48) sont inchangés."""
    if 1000 < numero <= 2000:
        return numero - 1000
    if 2000 < numero <= 9000:
        return numero - 2000
    return numero


def _mapping_baie_ifindex(conn, client_id, slot_id, appareil_id_switch, infos):
    """Retourne ({numero_baie: ifindex}, {numero: source}, calibre, divergences).
    `infos` = {ifindex: {'nom', 'alias', 'ethernet'}} (les interfaces vues par
    SNMP). Priorité par port :
      1. calibration manuelle (baie_slot_ports.if_index)        → 'manuel'
      2. topologie FDB (diag_topologie : appareil branché vu)   → 'topologie'
      3. nom d'interface (Gi1/0/12 → port 12)                   → 'nom_port'
      4. repli naïf numero==ifIndex — désactivé par défaut,
         `diag_baie_activite_repli_naif` = '1' pour l'activer   → 'repli'
    calibre = True dès qu'au moins un mapping fiable (1/2/3) est trouvé.
    divergences = [numero] où topologie et nom d'interface ne s'accordent pas."""
    ports = conn.execute(
        "SELECT numero, appareil_id, if_index FROM baie_slot_ports WHERE slot_id=?",
        (slot_id,)).fetchall()
    if not ports:
        return {}, {}, False, []

    topo = {}   # appareil_vu_id -> ifindex
    if appareil_id_switch:
        for vu_aid, pidx in conn.execute(
                "SELECT appareil_vu_id, port_index FROM diag_topologie "
                "WHERE client_id=? AND equipement_appareil_id=? AND appareil_vu_id IS NOT NULL",
                (client_id, appareil_id_switch)):
            try:
                topo.setdefault(int(vu_aid), int(pidx))
            except (TypeError, ValueError):
                continue

    _RE_SFP = re.compile(r'(sfp|xfp|qsfp|fiber|fibre|tengig|fortygig|hundredgig|\bte\b|\bxe-|\bfo\b)', re.I)
    par_nom, par_nom_sfp = {}, {}   # port physique déduit du nom -> ifindex
    for ifx, meta in sorted(infos.items()):
        if not meta.get('ethernet', True):
            continue
        nom = str(meta.get('nom') or '')
        pp = _port_physique_depuis_nom(nom) or _port_physique_depuis_nom(meta.get('alias'))
        if pp is not None:
            (par_nom_sfp if _RE_SFP.search(nom) else par_nom).setdefault(pp, ifx)

    repli_naif = str(_cfg('diag_baie_activite_repli_naif', '0')) == '1'
    mapping, sources, calibre, divergences = {}, {}, False, []
    for numero, p_aid, if_manuel in ports:
        try:
            numero = int(numero)
        except (TypeError, ValueError):
            continue
        if_topo = topo.get(p_aid) if p_aid else None
        # nom d'interface : plage RJ (jusqu'à 96, couvre les stacks 48+48 et les
        # châssis 96 ports). Un port SFP de baie (1001+) est mappé sur une
        # interface dont le NOM porte un marqueur fibre/SFP, jamais sur un RJ.
        _log = _numero_logique(numero)
        if numero <= 96:
            if_nom = par_nom.get(numero)
        elif 1000 < numero <= 9000:
            if_nom = par_nom_sfp.get(_log)
        else:
            if_nom = None
        if if_manuel:
            try:
                mapping[numero] = int(if_manuel)
                sources[numero] = 'manuel'
                calibre = True
            except (TypeError, ValueError):
                pass
            else:
                continue
        if if_topo is not None and if_nom is not None and if_topo != if_nom:
            divergences.append(numero)
        if if_topo is not None:
            mapping[numero] = if_topo
            sources[numero] = 'topologie'
            calibre = True
        elif if_nom is not None:
            mapping[numero] = if_nom
            sources[numero] = 'nom_port'
            calibre = True
        elif repli_naif and numero in infos:
            mapping[numero] = numero
            sources[numero] = 'repli'
    return mapping, sources, calibre, divergences


_poll_max_ms = [0]         # durée du plus lent relevé du cycle courant (cadence adaptative)
_cadence     = [_ACTIVITE_INTERVAL]   # intervalle effectif entre deux cycles (affiché dans l'UI)
_activite_rechauffe = [0]  # nb de cycles consécutifs avec des clients actifs (0 = à froid)
_ACTIVITE_RECHAUFFE_CYCLES = 3   # tant qu'on est sous ce seuil, on n'applique pas la cadence lente
                                 # (il faut 2 relevés pour un delta — inutile d'attendre 30 s entre-eux)


def _noms_interfaces(ip, communautes):
    """{ifindex: {'nom','alias','ethernet','speed_mbps'}} pour toutes les
    interfaces du switch. Caché ~90 s (nom/type/vitesse ne changent quasiment
    jamais — inutile de les relever à chaque cycle) ; un cache VIDE n'est PAS
    conservé (on retentera au cycle suivant).
    Le rafraîchissement (lent sur un switch bas de gamme) se fait EN TÂCHE DE
    FOND : on rend tout de suite les données connues, quitte à ce qu'elles aient
    jusqu'à ~90 s — nom/type/vitesse ne bougent pas."""
    ent = _activite_noms.get(ip)
    age = (time.time() - ent['ts']) if ent else 1e9
    if ent and ent['infos'] and age < _ACTIVITE_NOMS_TTL:
        return ent['infos']
    if ent and ent['infos']:            # cache périmé mais utilisable → refresh async
        with _activite_lock:            # test-et-pose atomique : jamais deux threads
            lancer = (not ent.get('maj_en_cours')
                      and time.time() >= ent.get('retry_after', 0))
            if lancer:
                ent['maj_en_cours'] = True
        if lancer:
            threading.Thread(target=_maj_noms_interfaces, args=(ip, communautes),
                             daemon=True, name='DiagBaieNoms').start()
        return ent['infos']
    return _maj_noms_interfaces(ip, communautes)


def _maj_noms_interfaces(ip, communautes):
    cols = _snmp_bulk(ip, [_OID_IF_NAME, _OID_IF_ALIAS, _OID_IF_TYPE, _OID_IF_DESCR], communautes)
    noms, alias, types, descr = (cols.get(_OID_IF_NAME, {}), cols.get(_OID_IF_ALIAS, {}),
                                 cols.get(_OID_IF_TYPE, {}), cols.get(_OID_IF_DESCR, {}))
    vit = _snmp_bulk(ip, [_OID_IF_HIGHSPEED, _OID_IF_SPEED], communautes)
    hs, sp = vit.get(_OID_IF_HIGHSPEED, {}), vit.get(_OID_IF_SPEED, {})

    def _int(d, k):
        try:
            return int(d.get(k))
        except (TypeError, ValueError):
            return 0

    infos = {}
    for suf in set(noms) | set(descr) | set(types):
        try:
            ifx = int(str(suf).split('.')[0])
        except (TypeError, ValueError):
            continue
        try:
            t = int(types.get(suf))
        except (TypeError, ValueError):
            t = None
        infos[ifx] = {
            'nom': str(noms.get(suf) or descr.get(suf) or f'if{ifx}'),
            'alias': str(alias.get(suf) or ''),
            'speed_mbps': _int(hs, suf) or (_int(sp, suf) // 1_000_000),
            # ethernet si ifType connu et dans la liste, ou inconnu (on n'exclut
            # pas un port faute d'info — mieux vaut un port en trop qu'en moins)
            'ethernet': (t is None) or (t in _IFTYPE_ETHERNET),
        }
    if infos:
        _activite_noms[ip] = {'ts': time.time(), 'infos': infos}
    else:
        ent = _activite_noms.get(ip)
        if ent:
            # échec : on ne relance PAS un thread au prochain cycle (sinon un par
            # cycle, indéfiniment, sur un switch au SNMP de noms cassé) — back-off.
            ent['maj_en_cours'] = False
            ent['retry_after'] = time.time() + _ACTIVITE_NOMS_RETRY
    return infos


def _snmp_bulk(ip, oid_bases, communautes):
    try:
        from app import _snmp_bulk_cols
        return _snmp_bulk_cols(ip, oid_bases, communautes)
    except Exception:
        return {}


_activite_hc  = {}   # ip -> bool : le switch expose-t-il les compteurs 64 bits (ifXTable) ?
_activite_poe = {}   # ip -> bool | None : le switch expose-t-il du PoE (POWER-ETHERNET-MIB) ?
_activite_cyc = [0]  # compteur global de cycles (relève des erreurs espacée)
_activite_calib = {}       # (cid, slot_id) -> {numero, ip, debut, last_oper, transitions, trouve}
_CALIB_FENETRE = 90.0      # s — durée de la détection « débranche/rebranche »

# Une capacité (compteurs 64 bits, PoE) n'est déclarée ABSENTE qu'après plusieurs
# relevés négatifs consécutifs — un seul paquet SNMP perdu au premier passage ne
# doit pas la condamner pour toute la vie du process (agents bas de gamme = perte
# UDP fréquente). Et on la re-teste périodiquement même une fois « absente ».
_activite_capa_neg     = {}   # ip -> {'hc': n, 'poe': n} : négatifs consécutifs
_activite_capa_reprobe = {}   # ip -> {'hc': cycle, 'poe': cycle} : prochain re-test
_ACTIVITE_NEG_CONFIRME   = 2
_ACTIVITE_REPROBE_CYCLES = 50

_ACTIVITE_NOMS_RETRY = 30.0    # s — après un échec du relevé des noms d'interface,
                               # délai avant de relancer un thread (sinon : un par cycle)


def assistant_calibration(client_id, slot_id, numero, action):
    """Calibration « par débranchement » : on note l'état oper de toutes les
    interfaces du switch, l'utilisateur débranche puis rebranche le câble du
    port, et l'interface qui a changé d'état est celle du port. action ∈
    'start' | 'stop'. Retourne l'état courant."""
    cle = (client_id, slot_id)
    with _activite_lock:
        if action == 'stop':
            _activite_calib.pop(cle, None)
            return {'etat': 'arrete'}
        _activite_calib[cle] = {'numero': int(numero), 'slot_id': slot_id,
                                'debut': time.time(), 'last_oper': None,
                                'transitions': {}, 'up_cyc': {},
                                'dernier_mouv_cyc': -1, 'trouve': None}
    _demarrer_activite_thread()
    return {'etat': 'attente', 'numero': int(numero)}


def _maj_assistant_calibration(cid, slot_id, ip, cur_ports):
    """Appelé par _cycle_activite (thread _activite_loop) : suit les transitions
    oper pendant la fenêtre. Retourne (slot_id, numero, ifindex) quand un gagnant
    se dégage — l'appelant applique la calibration APRÈS avoir fermé sa connexion
    de lecture (pas d'écriture depuis le GET moniteur_baie)."""
    with _activite_lock:
        a = _activite_calib.get((cid, slot_id))
        if not a or a['trouve']:
            return None
        oper_now = {ix: p['oper'] for ix, p in cur_ports.items()
                    if p.get('oper_ok', True) and p.get('oper') is not None}
        expire = time.time() - a['debut'] > _CALIB_FENETRE

        if a['last_oper'] is not None:
            for ix, o in oper_now.items():
                anc = a['last_oper'].get(ix)
                if anc is not None and o != anc:
                    a['transitions'][ix] = a['transitions'].get(ix, 0) + 1
                    a['dernier_mouv_cyc'] = _activite_cyc[0]
                    if o == 1:              # (re)passe UP : on rebranche à la fin du geste
                        a['up_cyc'][ix] = _activite_cyc[0]
        a['last_oper'] = oper_now

        # débranché PUIS rebranché (>= 2 transitions, état final up) = geste complet.
        complets = [ix for ix, n in a['transitions'].items()
                    if n >= 2 and oper_now.get(ix) == 1]
        # on ne décide QUE lorsque le réseau s'est calmé (un cycle sans nouvelle
        # transition) : sinon un voisin qui flappe avant la fin du geste gagnerait.
        calme = _activite_cyc[0] - a['dernier_mouv_cyc'] >= 1

        gagnant = None
        if complets and calme:
            gagnant = max(complets, key=lambda ix: a['up_cyc'].get(ix, -1))
        elif expire:
            # fenêtre écoulée : on prend la seule interface qui a bougé, sinon rien
            gagnant = (next(iter(a['transitions'])) if len(a['transitions']) == 1
                       else None)

        if gagnant:
            a['trouve'] = gagnant
            return (slot_id, a['numero'], gagnant)
        if expire:
            a['trouve'] = 0                 # 0 = expiré sans résultat
        return None


def _poll_poe(ip, communautes):
    """PoE par port (POWER-ETHERNET-MIB). {'ports': {numero: {statut, statut_txt,
    classe, watts_max}}, 'total_w', 'budget_w'} — {} si le switch n'a pas de PoE.
    La table pethPsePortTable est indexée `groupe.port` : le composant `port`
    correspond au port physique (donc au numéro de port de la baie)."""
    if _activite_poe.get(ip) is False and \
            _activite_cyc[0] < _activite_capa_reprobe.get(ip, {}).get('poe', 0):
        return {}
    cols = _snmp_bulk(ip, [_OID_POE_DETECT, _OID_POE_CLASS], communautes)
    detect = cols.get(_OID_POE_DETECT, {})
    if not detect:
        neg = _activite_capa_neg.setdefault(ip, {})
        neg['poe'] = neg.get('poe', 0) + 1
        if neg['poe'] >= _ACTIVITE_NEG_CONFIRME:      # confirmé : pas de PoE sur ce switch
            _activite_poe[ip] = False
            _activite_capa_reprobe.setdefault(ip, {})['poe'] = _activite_cyc[0] + _ACTIVITE_REPROBE_CYCLES
        return {}
    _activite_poe[ip] = True
    _activite_capa_neg.get(ip, {}).pop('poe', None)
    classe = cols.get(_OID_POE_CLASS, {})
    ports = {}
    for suf, val in detect.items():
        parts = str(suf).split('.')
        try:
            numero = int(parts[-1])          # composant « port » = dernier
            st = int(val)
        except (TypeError, ValueError):
            continue
        cl = None
        try:
            cl = int(classe.get(suf))
        except (TypeError, ValueError):
            pass
        ports[numero] = {
            'statut': st, 'statut_txt': _POE_DETECT_TXT.get(st, '?'),
            'classe': (cl - 1) if cl else None,      # 1..5 -> classe 0..4
            'watts_max': _POE_CLASSE_W.get(cl) if st == 3 else None,
        }
    scal = _snmp_bulk(ip, [_OID_POE_MAIN_W, _OID_POE_CONS_W], communautes)

    def _w(d):
        try:
            return int(next(iter(d.values())))
        except (StopIteration, TypeError, ValueError):
            return None
    return {'ports': ports, 'budget_w': _w(scal.get(_OID_POE_MAIN_W, {})),
            'total_w': _w(scal.get(_OID_POE_CONS_W, {}))}


def _poll_switch_ports(ip, communautes, infos=None):
    """Relevé SNMP des compteurs de TOUS les ports du switch, en UNE requête
    GETBULK multi-colonnes (auto-descriptif : chaque varbind porte son OID —
    pas d'hypothèse d'ordre). MINIMUM vital pour les LEDs : oper + octets +
    paquets = 5 colonnes (les erreurs, plus coûteuses, ne sont relevées qu'1
    cycle sur 8 — l'état « rouge » reste surtout du ressort du palier 3).
    nom/type/vitesse viennent de `_noms_interfaces` (caché). Retourne
    ({ifindex: {...}}, ok, hc, sysuptime_ticks).
    NB : requêtes SÉRIE, jamais parallèles — un agent SNMP de switch bas de
    gamme est mono-thread et *drop* les requêtes concurrentes."""
    infos = infos or {}
    _activite_cyc[0] += 1
    avec_err = (_activite_cyc[0] % 8 == 1)
    a_hc = _activite_hc.get(ip)
    reprobe = (a_hc is False
               and _activite_cyc[0] >= _activite_capa_reprobe.get(ip, {}).get('hc', 0))
    veut_hc = (a_hc is not False) or reprobe   # on demande l'ifXTable ?
    veut_32 = (a_hc is not True) or reprobe    # on demande l'ifTable ?  (au démarrage : les deux, 1 cycle)
    oct_cols = [_OID_IF_IN_NUCAST, _OID_IF_OUT_NUCAST]   # bcast+mcast (Counter32, toujours demandé)
    if veut_hc:
        oct_cols += [_OID_IF_HCIN, _OID_IF_HCOUT, _OID_IF_HCIN_UCAST, _OID_IF_HCOUT_UCAST]
    if veut_32:
        oct_cols += [_OID_IF_IN_OCTETS, _OID_IF_OUT_OCTETS, _OID_IF_IN_UCAST, _OID_IF_OUT_UCAST]
    demandes = ([_OID_SYS_UPTIME, _OID_IF_OPER] + oct_cols
                + ([_OID_IF_IN_ERRORS, _OID_IF_OUT_ERRORS] if avec_err else []))
    cols = _snmp_bulk(ip, demandes, communautes)

    try:
        sysuptime = int(next(iter(cols.get(_OID_SYS_UPTIME, {}).values())))
    except (StopIteration, TypeError, ValueError):
        sysuptime = None

    oper = cols.get(_OID_IF_OPER, {})
    a_du_hc = bool(cols.get(_OID_IF_HCIN) or cols.get(_OID_IF_HCOUT))
    if a_hc is not True:
        if a_du_hc:
            _activite_hc[ip] = True
            _activite_capa_neg.get(ip, {}).pop('hc', None)
            _activite_capa_reprobe.get(ip, {}).pop('hc', None)
        elif oper and veut_hc:                # ifXTable demandée, oper répond, HC absent
            neg = _activite_capa_neg.setdefault(ip, {})
            neg['hc'] = neg.get('hc', 0) + 1
            if neg['hc'] >= _ACTIVITE_NEG_CONFIRME:   # confirmé : le switch n'a pas de compteurs 64 bits
                _activite_hc[ip] = False
                _activite_capa_neg.get(ip, {}).pop('hc', None)
                _activite_capa_reprobe.setdefault(ip, {})['hc'] = _activite_cyc[0] + _ACTIVITE_REPROBE_CYCLES
    hc_mode = _activite_hc.get(ip) is True or a_du_hc

    if not oper and not cols.get(_OID_IF_HCIN) and not cols.get(_OID_IF_IN_OCTETS):
        return {}, False, bool(a_hc), sysuptime

    def _i(col, suf, defaut=0):
        try:
            return int(cols.get(col, {}).get(suf))
        except (TypeError, ValueError):
            return defaut

    peg = [False]

    def _cpt(hc_col, c32_col, suf):
        v = _i(hc_col, suf)
        if v:
            return v, True
        v = _i(c32_col, suf)
        if v in _CPT_SENTINELLE_32:      # compteur 32 bits bloqué (agent défectueux)
            peg[0] = True
            return 0, False
        return v, False

    cles = set(oper) | set(cols.get(_OID_IF_HCIN, {})) | set(cols.get(_OID_IF_IN_OCTETS, {}))
    res, hc_seen = {}, False
    for suf in cles:
        try:
            ifx = int(str(suf).split('.')[0])
        except (TypeError, ValueError):
            continue
        peg[0] = False
        in_o, h1 = _cpt(_OID_IF_HCIN, _OID_IF_IN_OCTETS, suf)
        out_o, h2 = _cpt(_OID_IF_HCOUT, _OID_IF_OUT_OCTETS, suf)
        in_p, h3 = _cpt(_OID_IF_HCIN_UCAST, _OID_IF_IN_UCAST, suf)
        out_p, h4 = _cpt(_OID_IF_HCOUT_UCAST, _OID_IF_OUT_UCAST, suf)
        in_np = _i(_OID_IF_IN_NUCAST, suf)      # broadcast + multicast (Counter32)
        out_np = _i(_OID_IF_OUT_NUCAST, suf)
        if in_np in _CPT_SENTINELLE_32:
            in_np, peg[0] = 0, True
        if out_np in _CPT_SENTINELLE_32:
            out_np, peg[0] = 0, True
        h = h1 or h2 or h3 or h4
        hc_seen = hc_seen or h
        d = {
            # ifOperStatus PAS relevé pour ce port ce cycle (réponse GETBULK
            # partielle/tronquée) : on NE fabrique PAS un « up » — l'appelant
            # gardera le dernier état connu. Sinon les ports débranchés
            # s'allumaient tous pendant un ou deux cycles dégradés.
            'oper': _i(_OID_IF_OPER, suf, None),
            'oper_ok': suf in oper,
            'speed_mbps': (infos.get(ifx) or {}).get('speed_mbps', 0),
            'in_oct': in_o, 'out_oct': out_o, 'in_pkts': in_p, 'out_pkts': out_p,
            'in_npkts': in_np, 'out_npkts': out_np,
            'cpt_pegge': peg[0], 'hc': h,     # compteurs 64 bits ? sinon bouclage 32 bits possible
        }
        if avec_err:      # sinon : clés absentes → l'appelant conserve la valeur connue
            d['in_err'] = _i(_OID_IF_IN_ERRORS, suf)
            d['out_err'] = _i(_OID_IF_OUT_ERRORS, suf)
        res[ifx] = d
    return res, bool(res), hc_seen, sysuptime


def _etat_led(prev, cur, dt, seuils, reboot=False):
    """État d'un port. seuils = {'err', 'sat_pct', 'pps_mini'}.
    Priorité : down > stale > err > sature > traffic > idle. Le clignotement
    « traffic » se déclenche sur les PAQUETS (comme une vraie LED de switch),
    pas seulement au-delà d'un % de bande passante.

    Anti-rebond : le passage idle<->traffic n'est retenu qu'après
    `_ACTIVITE_DEBOUNCE` cycles consécutifs qui le demandent (le bruit de fond
    L2 et l'imprécision des compteurs faisaient scintiller les LEDs et donnaient
    des comptes de « ports actifs » différents d'une instance à l'autre). Les
    états down/stale/err/sature, eux, basculent immédiatement."""
    if cur.get('oper', 1) != 1:
        return {'etat': 'down', 'bps': 0, 'pps': 0.0, 'pct': 0.0,
                'blink_ms': 0, 'err_delta': 0, 'reset': False,
                'bps_ema': 0.0, 'pps_ema': 0.0,
                'etat_pending': None, 'etat_pending_n': 0}

    speed = cur.get('speed_mbps', 0)
    hc = cur.get('hc', True)          # compteurs 64 bits ? sinon bouclage 32 bits à gérer
    if reboot:
        # le switch a redémarré (sysUpTime a reculé) : tous les deltas sont faux
        prev = None
    if prev and dt > 0 and 'in_oct' in prev:
        def _d(k, large=0):
            c = cur.get(k, 0)
            p = prev.get(k, c)
            d = c - p
            if d >= 0:
                return d
            # delta négatif : bouclage d'un compteur 32 bits (prev proche du
            # plafond) OU vrai redémarrage (le compteur repart de ~0).
            if large and p > large // 2:
                return (large - p) + c        # bouclage : delta réel
            return c                          # redémarrage
        oct_large = 0 if hc else 2 ** 32       # ifTable = Counter32
        raw_in = cur.get('in_oct', 0) - prev.get('in_oct', 0)
        raw_out = cur.get('out_oct', 0) - prev.get('out_oct', 0)
        reset = ((raw_in < 0 and prev.get('in_oct', 0) <= (oct_large or 2**32) // 2)
                 or (raw_out < 0 and prev.get('out_oct', 0) <= (oct_large or 2**32) // 2))
        bps_inst = max(_d('in_oct', oct_large), _d('out_oct', oct_large)) * 8 / dt
        # pps = TOUS les paquets (unicast + broadcast/multicast) comme une vraie
        # LED de switch : un port de VLAN calme n'a souvent que du non-unicast.
        pps_inst = (_d('in_pkts', oct_large) + _d('out_pkts', oct_large)
                    + _d('in_npkts', 2 ** 32) + _d('out_npkts', 2 ** 32)) / dt
        err_delta = _d('in_err') + _d('out_err')
        # Un compteur qui vient de « dépéguer » (0x7FFFFFFF → valeur réelle), de
        # boucler, ou de basculer 64↔32 bits d'un cycle à l'autre produit un delta
        # gigantesque et un pic de débit/pps fantaisiste. Un port ne peut pas
        # dépasser la capacité physique de son lien : au-delà, c'est un artefact —
        # on garde la dernière valeur lissée connue plutôt que d'injecter le pic.
        cap_bps = speed * 1e6 * 1.05 if speed else 12e9
        cap_pps = speed * 1500 if speed else _ACTIVITE_PPS_MAX_PORT
        pps_delirant = False
        if cur.get('cpt_pegge'):
            # les compteurs d'OCTETS sont bloqués (agent défectueux) -> pas de
            # débit fiable. Les compteurs de PAQUETS restent souvent bons : on
            # garde un pps PLAUSIBLE (HP ProCurve bas de gamme), mais s'il est
            # délirant c'est qu'ils mentent aussi -> ce cycle ne compte pas.
            bps_inst = 0.0
            if pps_inst > _ACTIVITE_PPS_PEGGE_MAX:
                pps_inst, pps_delirant = 0.0, True
        elif bps_inst > 0:
            # cohérence octets/paquets : un paquet fait ≥ 64 octets (512 bits).
            # Un compteur de paquets qui donne bien plus que ce que les octets
            # permettent est menteur (fréquent sur les mêmes switchs bas de gamme).
            pps_coherent = bps_inst / 512 * 1.1
            if pps_inst > max(pps_coherent, 50):
                pps_inst = pps_coherent
        if bps_inst > cap_bps:
            bps_inst = prev.get('bps_ema', 0.0)
        if pps_inst > cap_pps:
            pps_inst = prev.get('pps_ema', 0.0)
        a = _ACTIVITE_EMA
        bps = a * bps_inst + (1 - a) * prev.get('bps_ema', bps_inst)
        pps = 0.0 if pps_delirant else a * pps_inst + (1 - a) * prev.get('pps_ema', pps_inst)
        # garde-fou : un `bps_ema`/`pps_ema` hérité d'un pic passé (compteur ayant
        # bouclé avant la mise en place du plafond) doit pouvoir se résorber vite
        bps = min(bps, cap_bps)
        pps = min(pps, cap_pps)
    else:
        bps = pps = bps_inst = pps_inst = 0.0
        err_delta = 0
        reset = bool(reboot)

    pct = (bps / (speed * 1e6) * 100) if speed else 0.0
    pend = (prev or {}).get('etat_pending')
    pend_n = (prev or {}).get('etat_pending_n', 0)
    base = {'bps': round(bps), 'pps': round(pps, 1), 'pct': round(pct, 1),
            'err_delta': int(err_delta), 'reset': reset,
            'bps_ema': bps, 'pps_ema': pps,
            'etat_pending': None, 'etat_pending_n': 0}

    if err_delta > seuils['err']:
        return {'etat': 'err', 'blink_ms': 250, **base}
    if speed and pct >= seuils['sat_pct']:
        return {'etat': 'sature', 'blink_ms': 150, **base}

    def _blink(p):
        return int(max(120, min(1200, 1200 / math.log2(max(2.0, p + 2)))))

    # état voulu par CE cycle — sur les valeurs instantanées (pas l'EMA) :
    # l'anti-rebond ci-dessous fournit le lissage de l'état, l'EMA celui des
    # chiffres affichés.
    bps_mini = seuils.get('bps_mini', _ACTIVITE_BPS_MINI)
    if pps_inst >= seuils['pps_mini'] or bps_inst > bps_mini:
        etat_brut, blink = 'traffic', _blink(pps)
    else:
        etat_brut, blink = 'idle', 0

    # anti-rebond ASYMÉTRIQUE : on PASSE en « traffic » vite (_ACTIVITE_DEBOUNCE_ON,
    # défaut 1 = dès le premier relevé, comme une vraie LED de switch) et on en
    # SORT plus lentement (_ACTIVITE_DEBOUNCE relevés calmes), ce qui donne la
    # « persistance » visuelle d'un voyant sans scintiller.
    etat_prec = (prev or {}).get('etat')
    if etat_prec in ('idle', 'traffic') and etat_brut != etat_prec:
        # sortie de « traffic » : plus lente encore sur un switch aux compteurs
        # bloqués (ils bougent par à-coups, un port réellement actif y semble
        # calme un cycle sur deux).
        seuil_off = _ACTIVITE_DEBOUNCE * 2 if cur.get('cpt_pegge') else _ACTIVITE_DEBOUNCE
        seuil_deb = _ACTIVITE_DEBOUNCE_ON if etat_brut == 'traffic' else seuil_off
        pend_n = pend_n + 1 if pend == etat_brut else 1
        pend = etat_brut
        if pend_n < seuil_deb:                   # pas encore confirmé
            etat_brut = etat_prec
            blink = _blink(pps) if etat_prec == 'traffic' else 0
        else:
            pend, pend_n = None, 0
    else:
        pend, pend_n = None, 0
    base['etat_pending'], base['etat_pending_n'] = pend, pend_n

    if etat_brut == 'traffic':
        return {'etat': 'traffic', 'blink_ms': blink, **base}
    return {'etat': 'idle', 'blink_ms': 0, **base}


def _lire_sysinfo(ip, communautes):
    """{'sysname', 'sysdescr'} du switch — caché ~10 min (quasi statique)."""
    ent = _activite_sysinfo.get(ip)
    if ent and time.time() - ent['ts'] < _ACTIVITE_SYSINFO_TTL:
        return ent

    def _first(d):
        try:
            return str(next(iter(d.values())) or '')
        except (StopIteration, TypeError):
            return ''
    cols = _snmp_bulk(ip, [_OID_SYS_NAME_B, _OID_SYS_DESCR_B], communautes)
    ent = {'ts': time.time(),
           'sysname': _first(cols.get(_OID_SYS_NAME_B, {})),
           'sysdescr': _first(cols.get(_OID_SYS_DESCR_B, {}))}
    if ent['sysname'] or ent['sysdescr']:
        _activite_sysinfo[ip] = ent
    return ent


def _modele_court(sysdescr):
    if not sysdescr:
        return ''
    return sysdescr.split('\n')[0].split(',')[0].strip()[:48]


_activite_fdb_lock = threading.Lock()   # _fdb_switch : loop d'activité + requête Flask


def _mac_octets(brut):
    return ':'.join('%02x' % b for b in brut) if brut and len(brut) == 6 else ''


def _bridge_fdb_brute(ip, communautes, baseport_if):
    """{ifIndex: set(mac)} depuis la bridge-MIB. Essaie dot1q, dot1d, puis — si
    les deux sont muets — le contexte de communauté par VLAN (`public@<vlan>`,
    cas Cisco et quelques HP). Mémorise le dialecte qui répond."""
    def _agrege(fdb):
        out = {}
        for suffixe, bridge_port in fdb.items():
            mac = _mac_depuis_suffixe(suffixe, 6)
            try:
                bp = int(bridge_port)
            except (TypeError, ValueError):
                continue
            if not mac or bp <= 0:
                continue
            ifx = baseport_if.get(str(bp), bp)
            try:
                ifx = int(ifx)
            except (TypeError, ValueError):
                ifx = bp
            out.setdefault(ifx, set()).add(_norm_mac(mac))
        return out

    dialecte = _activite_fdb_dialecte.get(ip)
    if dialecte == 'dot1d':
        f = _snmp_walk(_OID_FDB_DOT1D_PORT, ip, communautes)
        if f:
            return _agrege(f)
    elif dialecte == 'dot1q':
        f = _snmp_walk(_OID_FDB_DOT1Q_PORT, ip, communautes)
        if f:
            return _agrege(f)
    elif dialecte == 'dot1q-vlan':
        return _fdb_par_vlan(ip, communautes, baseport_if)
    else:
        for nom, oid in (('dot1q', _OID_FDB_DOT1Q_PORT), ('dot1d', _OID_FDB_DOT1D_PORT)):
            f = _snmp_walk(oid, ip, communautes)
            if f:
                _activite_fdb_dialecte[ip] = nom
                return _agrege(f)
        vlan = _fdb_par_vlan(ip, communautes, baseport_if)
        if vlan:
            _activite_fdb_dialecte[ip] = 'dot1q-vlan'
            return vlan
    return {}


def _fdb_par_vlan(ip, communautes, baseport_if):
    """FDB dot1q relevée VLAN par VLAN avec le contexte de communauté
    `communaute@<vlan>` (indispensable sur beaucoup de switches Cisco / HP)."""
    vlans = _snmp_walk(_OID_DOT1Q_VLAN_NAMES, ip, communautes)
    vids = []
    for suf in vlans:
        try:
            vids.append(int(suf.split('.')[-1]))
        except (ValueError, IndexError):
            pass
    out = {}
    for vid in sorted(set(vids))[:_FDB_VLAN_MAX]:
        ctx = [f'{c}@{vid}' for c in communautes]
        f = _snmp_walk(_OID_FDB_DOT1Q_PORT, ip, ctx)
        for suffixe, bridge_port in f.items():
            mac = _mac_depuis_suffixe(suffixe, 6)
            try:
                bp = int(bridge_port)
            except (TypeError, ValueError):
                continue
            if not mac or bp <= 0:
                continue
            ifx = baseport_if.get(str(bp), bp)
            try:
                ifx = int(ifx)
            except (TypeError, ValueError):
                ifx = bp
            out.setdefault(ifx, set()).add(_norm_mac(mac))
    return out


def _fdb_switch(ip, communautes):
    """Table d'apprentissage MAC du switch, `{ifIndex: set(mac)}` : bridge-MIB
    (dot1q/dot1d, contexte VLAN au besoin) **+ table ARP** (`ipNetToMediaPhysAddress`,
    précieuse pour un routeur / switch L3 sans FDB). Cache `_ACTIVITE_FDB_TTL` s ;
    sur échec de walk, conserve la dernière valeur connue si elle n'est pas trop
    vieille."""
    with _activite_fdb_lock:
        hit = _activite_fdb.get(ip)
        if hit and (time.time() - hit[0]) < _ACTIVITE_FDB_TTL:
            return hit[1]
        ech = _activite_fdb_echec.get(ip)
        if ech and (time.time() - ech) < _ACTIVITE_FDB_BACKOFF and not hit:
            return {}

        bp_hit = _activite_fdb_baseport.get(ip)
        if bp_hit and (time.time() - bp_hit[0]) < _ACTIVITE_SYSINFO_TTL:
            baseport_if = bp_hit[1]
        else:
            baseport_if = _snmp_walk(_OID_FDB_BASEPORT_IF, ip, communautes)
            if baseport_if:
                _activite_fdb_baseport[ip] = (time.time(), baseport_if)
            elif bp_hit:
                baseport_if = bp_hit[1]

        par_if = _bridge_fdb_brute(ip, communautes, baseport_if)

        # table ARP — plafonnée : petit complément si la FDB bridge a répondu,
        # relevé plus large si elle est vide (probable routeur / switch L3).
        arp_cap = 150 if par_if else 800
        arp = _snmp_walk_octets(_OID_ARP_PHYS, ip, communautes, max_rows=arp_cap)
        if not arp:
            arp = _snmp_walk_octets(_OID_ARP_PHYS_2, ip, communautes, max_rows=arp_cap)
        for suf, brut in arp.items():
            mac = _mac_octets(brut)
            if not mac:
                continue
            try:
                ifx = int(suf.split('.')[0])
            except (ValueError, IndexError):
                continue
            par_if.setdefault(ifx, set()).add(mac)

        if par_if:
            _activite_fdb[ip] = (time.time(), par_if)
            _activite_fdb_echec.pop(ip, None)
            return par_if
        _activite_fdb_echec[ip] = time.time()
        if hit and (time.time() - hit[0]) < _ACTIVITE_FDB_PERIME:
            return hit[1]
        return {}


def _macs_infra_switch(ip, communautes):
    """Toutes les MAC « propres » d'un switch : adresse de base bridge
    (`dot1dBaseBridgeAddress`) + `ifPhysAddress` de chaque interface. Un switch
    en a plusieurs (base, une par port, une par VLAN) ; les rattacher toutes à
    l'appareil fiabilise la détection des liens switch ⇄ switch. Cache court."""
    ent = _activite_infra_mac.get(ip)
    if ent and time.time() - ent[0] < _ACTIVITE_SYSINFO_TTL:
        return ent[1]
    macs = set()
    base = _snmp_walk_octets(_OID_BRIDGE_BASE_MAC, ip, communautes, max_rows=4)
    for brut in base.values():
        m = _mac_octets(brut)
        if m:
            macs.add(m)
    for brut in _snmp_walk_octets(_OID_IF_PHYS_ADDR, ip, communautes, max_rows=400).values():
        m = _mac_octets(brut)
        if m and m != '00:00:00:00:00:00':
            macs.add(m)
    if macs:
        _activite_infra_mac[ip] = (time.time(), macs)
    return macs


def _releve_mac_switch(ip, communautes, inv_mac):
    """Point d'entrée unique : `_fdb_switch` (bridge + ARP) puis `_fdb_corriger`
    (hypothèses de forme, réglage `diag_fdb_mode:<ip>`). Retourne `(fdb, meta)`.
    Utilisé par le cycle d'activité, `analyser_brassage_baie` ET la découverte
    de topologie (palier 4)."""
    return _fdb_corriger(_fdb_switch(ip, communautes), inv_mac,
                         str(_cfg(f'diag_fdb_mode:{ip}', '')))


# Hypothèses de « forme » d'une MAC renvoyée par un agent buggé : (nom, fonction
# qui extrait le PRÉFIXE à recouper avec le début d'une vraie MAC, longueur).
# 'exact' = l'agent est correct (recoupement sur la MAC entière).
_FDB_HYPOTHESES = [
    ('exact',      lambda o: o,                                   6),
    ('tronque4',   lambda o: ':'.join(o.split(':')[:4]),          4),   # 2 derniers octets perdus
    ('tronque5',   lambda o: ':'.join(o.split(':')[:5]),          5),   # 1 dernier octet perdu
    ('prefixe2',   lambda o: ':'.join(o.split(':')[2:6]),         4),   # 2 octets parasites + tronqué (ProCurve 1810)
    ('prefixe1',   lambda o: ':'.join(o.split(':')[1:6]),         5),   # 1 octet parasite + tronqué
]
_FDB_MODES = ('', 'auto', 'standard', 'prefixe', 'ignorer')


def _fdb_corriger(par_if, inv_mac, mode=''):
    """Certains agents SNMP renvoient une table d'apprentissage MAC déformée
    (préfixe parasite, MAC tronquée…). On essaie plusieurs **hypothèses de forme**
    (`_FDB_HYPOTHESES`) et on garde celle qui recoupe le mieux l'inventaire
    (`appareils.adresse_mac`). Une hypothèse non-« exact » n'est retenue que si
    elle reconnaît au moins 3 appareils ET nettement plus que l'hypothèse
    « exact ». Les MAC non rattachables sous l'hypothèse retenue sont écartées ;
    une MAC réparée vue sur plusieurs ports (collision de préfixe) aussi.

    `mode` (réglage par switch) : `''`/`'auto'` = détection ; `'standard'` = ne
    jamais transformer ; `'prefixe'` = forcer l'hypothèse ProCurve ; `'ignorer'`
    = renvoyer une FDB vide.

    Retourne `(par_if, meta)` avec meta = {transform, reconnues, total, tronquee,
    fiable}."""
    toutes = {m for macs in par_if.values() for m in macs if m}
    meta = {'transform': 'exact', 'reconnues': sum(1 for m in toutes if m in inv_mac),
            'total': len(toutes), 'tronquee': False, 'fiable': True}
    if mode == 'ignorer':
        return {}, {**meta, 'transform': 'ignore', 'reconnues': 0, 'fiable': False}
    if not toutes:
        return par_if, meta

    # index inventaire par longueur de préfixe
    pref_par_lg = {}
    for lg in {h[2] for h in _FDB_HYPOTHESES}:
        d = {}
        for m in inv_mac:
            d.setdefault(':'.join(m.split(':')[:lg]), m)
        pref_par_lg[lg] = d

    def _score(fn, lg):
        idx = pref_par_lg[lg]
        return len({idx[fn(m)] for m in toutes if fn(m) in idx})

    hyps = _FDB_HYPOTHESES
    if mode == 'standard':
        hyps = hyps[:1]
    elif mode == 'prefixe':
        hyps = [h for h in _FDB_HYPOTHESES if h[0] == 'prefixe2']

    exact_sc = _score(lambda o: o, 6)
    best = ('exact', lambda o: o, 6, exact_sc)
    for nom, fn, lg in hyps:
        if nom == 'exact':
            continue
        sc = _score(fn, lg)
        if sc >= 3 and sc >= exact_sc + 2 and sc > best[3]:
            best = (nom, fn, lg, sc)
    if mode in ('prefixe',) and hyps:
        nom, fn, lg = hyps[0]
        best = (nom, fn, lg, _score(fn, lg))

    nom, fn, lg, sc = best
    meta['transform'], meta['reconnues'] = nom, sc

    if nom == 'exact':
        # agent correct : on garde tout (les MAC hors inventaire sont légitimes)
        meta['fiable'] = True
        return par_if, meta

    # agent déformé : on ne garde QUE ce qu'on sait rattacher (le reste est perdu)
    meta['tronquee'] = True
    idx = pref_par_lg[lg]
    neuf = {}
    for ifx, macs in par_if.items():
        r = {idx[k] for m in macs if (k := fn(m)) in idx}
        if r:
            neuf[ifx] = r
    compte = collections.Counter(m for macs in neuf.values() for m in macs)
    ambigu = {m for m, n in compte.items() if n > 1}
    if ambigu:
        neuf = {ifx: rest for ifx, macs in neuf.items() if (rest := macs - ambigu)}
    meta['reconnues'] = len({m for macs in neuf.values() for m in macs})
    meta['fiable'] = bool(neuf)
    return neuf, meta


def _mac_locale(mac):
    """True si le bit « localement administré » du 1er octet est posé — signe
    d'une MAC aléatoire (téléphone/portable en Wi-Fi moderne), jamais d'une
    carte réseau filaire classique."""
    try:
        return bool(int(mac.split(':')[0], 16) & 0x02)
    except (ValueError, IndexError, AttributeError):
        return False


# fabricants indicatifs — le fabricant seul ne tranche pas, il pondère
_OUI_AP     = ('ubiquiti', 'aruba', 'ruckus', 'meraki', 'aerohive', 'mist',
               'extreme networks', 'cambium', 'engenius', 'mikrotik', 'zyxel')
_OUI_MOBILE = ('apple', 'samsung', 'huawei', 'xiaomi', 'google', 'oneplus',
               'oppo', 'vivo', 'motorola mobility', 'sony mobile', 'nothing')
_TYPES_AP_INV = ('Borne Wi-Fi', 'Switch/AP', 'Pont Wi-Fi', "Point d'accès")


def _classer_cascade(macs, inv_mac, caps=''):
    """Plusieurs MAC sur un même port de switch relié à UNE prise murale → il y
    a un équipement intermédiaire (switch non géré ou borne Wi-Fi). Essaie de
    trancher. `inv_mac` = {mac: (aid, nom, type_appareil)} ; `caps` = capacités
    LLDP/CDP du voisin sur ce port ('bridge,wlan,...') si connues.
    Retourne {type: 'switch'|'wifi'|'indetermine', indices: [...], n_macs}."""
    macs = sorted(macs)
    aleatoires = [m for m in macs if _mac_locale(m)]
    vendors = [(_vendor(m) or '').lower() for m in macs]
    types_inv = [inv_mac[m][2] for m in macs if m in inv_mac and len(inv_mac[m]) > 2 and inv_mac[m][2]]
    caps = set((caps or '').split(','))
    indices, w, s = [], 0, 0

    if 'wlan' in caps:
        w += 4
        indices.append("le voisin LLDP/CDP se déclare « borne Wi-Fi »")
    elif 'bridge' in caps and 'router' not in caps:
        s += 4
        indices.append("le voisin LLDP/CDP se déclare « pont »")

    _wifi_kw = lambda t: any(k in (t or '').lower() for k in ('wi-fi', 'wifi', 'accès', 'wlan'))
    if any(t in _TYPES_AP_INV or _wifi_kw(t) for t in types_inv):
        w += 3
        indices.append("un appareil inventorié de type borne Wi-Fi est vu sur ce port")
    if any(t in ('Switch', 'Switch/AP') for t in types_inv):
        s += 3
        indices.append("un appareil inventorié de type switch est vu sur ce port")
    if aleatoires:
        w += 2
        indices.append(f"{len(aleatoires)} MAC aléatoire(s) — typique de clients Wi-Fi")
    ap_v = sorted({v for v in vendors if any(k in v for k in _OUI_AP)})
    if ap_v:
        w += 1
        indices.append(f"fabricant de point d'accès présent ({ap_v[0]})")
    mob = [v for v in vendors if any(k in v for k in _OUI_MOBILE)]
    if len(mob) >= 2:
        w += 1
        indices.append(f"{len(mob)} appareils de fabricants « mobile »")
    if len(macs) <= 4 and not aleatoires and not ap_v:
        s += 1
        indices.append("peu de MAC, aucune aléatoire — plutôt un switch non géré")
    if len(macs) >= 16:
        indices.append("beaucoup d'appareils — possible lien montant vers le reste du réseau")

    typ = 'wifi' if w > s else 'switch' if s > w else 'indetermine'
    return {'type': typ, 'indices': indices, 'n_macs': len(macs)}


def _voisins_port(macs, inv_mac):
    """Décrit les appareils dont une MAC est apprise sur un port : nom
    d'inventaire si connu, sinon fabricant (OUI), sinon la MAC brute.
    Retourne `{'noms': [...max _ACTIVITE_VOISINS_MAX...], 'n': total,
    'ids': set(appareil_id connus)}`."""
    noms, ids, restants = [], set(), 0
    for mac in sorted(macs):
        hit = inv_mac.get(mac)
        if hit:
            ids.add(hit[0])
            libelle = hit[1] or _vendor(mac) or mac
        else:
            libelle = _vendor(mac) or mac
        if len(noms) < _ACTIVITE_VOISINS_MAX:
            if libelle not in noms:
                noms.append(libelle)
        else:
            restants += 1
    return {'noms': noms, 'n': len(macs), 'ids': ids,
            'restants': max(0, len(macs) - len(noms))}


def _prises_murales_activite(conn, cid, ip_par_slot, etats_par_ip, mapping_par_slot,
                            noms_par_ip, topo_par_ip, fdb_par_ip, inv_mac, etats_prec_get):
    """LEDs d'activité + contrôle de câblage pour les prises murales d'un bandeau RJ.
    Une prise murale N est reliée par le cordon de brassage (baie_slot_ports.lie_*)
    à un port de switch : on réutilise l'état SNMP de ce port, déjà relevé.

    Contrôle de câblage : la table d'apprentissage MAC « live » du switch
    (`fdb_par_ip`, relevée à chaque cycle) dit quelles MAC transitent réellement
    par ce port. Si la MAC de l'appareil déclaré sur la prise (`baie_prises_murales`)
    n'y figure pas alors que le port apprend d'autres MAC → câblage incohérent.
    La topologie L2 (`diag_topologie`, palier 4) sert de repli quand le switch ne
    répond pas à la bridge-MIB.
    Retourne (ports_ui, journal_ops)."""
    ports_ui, journal_ops = [], []
    ph = ','.join('?' * len(_TYPES_BANDEAU))
    for (b_slot_id,) in conn.execute(
            f"SELECT id FROM baie_slots WHERE client_id=? AND type_equipement IN ({ph})",
            (cid, *_TYPES_BANDEAU)).fetchall():
        liens = conn.execute(
            "SELECT numero, lie_slot_id, lie_port_numero FROM baie_slot_ports "
            "WHERE slot_id=? AND lie_slot_id IS NOT NULL AND lie_port_numero IS NOT NULL",
            (b_slot_id,)).fetchall()
        if not liens:
            continue
        declare = {r[0]: (r[1], r[2] or '', _norm_mac(r[3] or '')) for r in conn.execute(
            "SELECT pm.numero, pm.appareil_id, a.nom_machine, a.adresse_mac "
            "FROM baie_prises_murales pm "
            "LEFT JOIN appareils a ON a.id = pm.appareil_id WHERE pm.slot_id=?", (b_slot_id,))}
        _prec = etats_prec_get(b_slot_id)
        for numero, lie_sid, lie_pnum in liens:
            sw_ip = ip_par_slot.get(lie_sid)
            etats = etats_par_ip.get(sw_ip)
            if not etats:
                continue
            try:
                ifindex = (mapping_par_slot.get(lie_sid) or {}).get(int(lie_pnum))
            except (TypeError, ValueError):
                ifindex = None
            led = etats.get(ifindex) if ifindex is not None else None
            if led is None:
                continue
            decl_aid, decl_nom, decl_mac = declare.get(numero, (None, '', ''))
            macs_port = (fdb_par_ip.get(sw_ip) or {}).get(ifindex, set())
            vus_topo = (topo_par_ip.get(sw_ip) or {}).get(ifindex, set())
            voisins = _voisins_port(macs_port, inv_mac) if macs_port else None
            cable, vu_txt = '', ''
            if not decl_aid:
                cable = ''                      # rien de déclaré : pas de contrôle
            elif decl_mac and decl_mac in macs_port:
                cable = 'ok'
            elif macs_port:                      # le port apprend d'autres MAC, pas la bonne
                cable = 'incoherent'
                _n = (voisins or {}).get('noms', [])
                vu_txt = ', '.join(_n[:3]) + ('…' if len(_n) > 3 else '') if _n else 'un autre appareil'
            elif decl_aid in vus_topo:
                cable = 'ok'
            elif vus_topo:
                cable = 'incoherent'
            else:
                cable = 'inconnu'                # ni FDB ni topologie exploitables
            if cable == 'incoherent' and _prec.get(('cable', numero)) != 'incoherent':
                journal_ops.append((f"Prise {numero} — câblage incohérent : le switch voit "
                                    f"{('« ' + vu_txt + ' »') if vu_txt else 'un autre appareil'} "
                                    f"sur ce port, pas « {decl_nom or 'l’appareil déclaré'} »",
                                    'warn', sw_ip))
            _prec[('cable', numero)] = cable

            # plusieurs MAC sur le port du cordon → équipement intermédiaire
            # (switch non géré / borne Wi-Fi) entre la prise et les machines
            cascade = _classer_cascade(macs_port, inv_mac) if len(macs_port) >= 2 else None
            if cascade and _prec.get(('casc', numero)) != cascade['type']:
                lbl = {'wifi': 'une borne Wi-Fi', 'switch': 'un switch non géré',
                       'indetermine': 'un switch ou une borne Wi-Fi'}[cascade['type']]
                journal_ops.append((f"Prise {numero} — {cascade['n_macs']} appareils vus : "
                                    f"{lbl} en aval", 'info', sw_ip))
            _prec[('casc', numero)] = cascade['type'] if cascade else None

            meta = (noms_par_ip.get(sw_ip) or {}).get(ifindex, {})
            ports_ui.append({
                'slot_id': b_slot_id, 'numero': numero, 'prise_murale': True,
                'etat': led['etat'], 'blink_ms': led['blink_ms'],
                'debit_bps': round(led['bps']), 'pps': round(led['pps'], 1),
                'err_delta': led.get('err_delta', 0),
                'nom': meta.get('nom', ''), 'cible': decl_nom, 'cable': cable,
                'voisins': (voisins or {}).get('noms', []),
                'voisins_restants': (voisins or {}).get('restants', 0),
                'cascade': cascade})
    return ports_ui, journal_ops


def _cycle_activite(clients):
    from database import get_local_db
    communautes = _communautes_snmp()
    seuils = {'err': _cfg_int('diag_baie_activite_seuil_err', 20),
              'sat_pct': _cfg_float('diag_snmp_seuil_saturation_pct', 90),
              'pps_mini': _cfg_float('diag_baie_activite_pps_mini', 15),
              'bps_mini': _cfg_float('diag_baie_activite_bps_mini', _ACTIVITE_BPS_MINI)}
    for cid in clients:
        journal_ops = []          # (message, niveau, cible) — émis hors lock
        calib_a_appliquer = []    # (slot_id, numero, ifindex) — écrits après conn.close()
        try:
            conn = get_local_db()
            try:
                switchs = _switchs_baie(conn, cid)
                equipements, ports_ui, detail_sw, detail_ports, detail_ifs = [], [], [], [], []
                nb_muets = 0
                poll_par_ip = {}   # ip -> (infos, cur_ports, ok, hc, dt_switch, reboot, poe, sysinfo, uptime_s)
                etats_par_ip = {}     # ip -> {ifindex: led}  (pour les prises murales d'un bandeau)
                mapping_par_slot = {} # slot_id switch -> {numero: ifindex}
                ip_par_slot = {}      # slot_id switch -> ip
                fdb_par_ip = {}       # ip -> {ifindex: set(mac)} : FDB live (câblage + voisins)
                inv_mac = {}          # mac normalisée -> (appareil_id, nom_machine, type_appareil)
                if switchs:
                    for _aid, _nom, _mac, _typ in conn.execute(
                            "SELECT id, nom_machine, adresse_mac, type_appareil FROM appareils "
                            "WHERE client_id=? AND adresse_mac!='' AND adresse_mac IS NOT NULL", (cid,)):
                        inv_mac[_norm_mac(_mac)] = (_aid, _nom, _typ)

                for sw in switchs:
                    ip, slot_id = sw['ip'], sw['slot_id']

                    # ── relevé SNMP : UNE fois par IP, partagé entre ses slots ──
                    if ip not in poll_par_ip:
                        # FDB en premier : quand elle est due (TTL long), le switch
                        # n'a pas encore été martelé par les GETBULK du cycle — un
                        # agent lent lâche les walks enchaînés (cas ProCurve 1810G).
                        fdb_par_ip[ip], _fdb_meta = _releve_mac_switch(ip, communautes, inv_mac)
                        if _fdb_meta['tronquee']:
                            journal_ops.append((f"{sw['nom']} ({ip}) — table d'apprentissage MAC "
                                                f"déformée (agent SNMP) : {_fdb_meta['reconnues']} "
                                                f"appareil(s) recoupé(s) avec l'inventaire "
                                                f"(hypothèse « {_fdb_meta['transform']} »)", 'warn', ip))
                        t0 = time.time()
                        infos = _noms_interfaces(ip, communautes)
                        cur_ports, ok, hc, sut = _poll_switch_ports(ip, communautes, infos)
                        poe = _poll_poe(ip, communautes) if ok else {}
                        sysinfo = _lire_sysinfo(ip, communautes) if ok else {'sysname': '', 'sysdescr': ''}
                        _poll_max_ms[0] = max(_poll_max_ms[0], int((time.time() - t0) * 1000))
                        now = time.time()

                        # dt via l'horloge de l'agent (sysUpTime), exact quelle que
                        # soit la durée du poll ; détection de redémarrage.
                        prev_sut = _activite_sut.get((cid, ip))
                        dt_switch, reboot, uptime_s = None, False, (sut / 100.0 if sut else None)
                        if sut and prev_sut:
                            d_ticks = sut - prev_sut[0]
                            if d_ticks < 0:
                                reboot = True
                            elif 0 < d_ticks < 2 ** 31:
                                dt_switch = d_ticks / 100.0
                        if sut:
                            _activite_sut[(cid, ip)] = (sut, now)
                        if dt_switch is None and prev_sut:
                            dt_switch = max(0.5, now - prev_sut[1])   # repli horloge poste
                        if reboot:
                            journal_ops.append((f"{sw['nom']} ({ip}) — le switch a redémarré "
                                                f"(compteurs remis à zéro)", 'warn', ip))

                        poll_par_ip[ip] = (infos, cur_ports, ok, hc, dt_switch, reboot,
                                           poe, sysinfo, uptime_s)
                        etait_ok = _activite_switch_ok.get((cid, ip), True)
                        if not ok and etait_ok:
                            journal_ops.append((f"{sw['nom']} ({ip}) — relevé SNMP sans réponse", 'warn', ip))
                        elif ok and not etait_ok:
                            journal_ops.append((f"{sw['nom']} ({ip}) — relevé SNMP rétabli", 'info', ip))
                        _activite_switch_ok[(cid, ip)] = ok
                        if ok:
                            c = _maj_assistant_calibration(cid, slot_id, ip, cur_ports)
                            if c:
                                calib_a_appliquer.append(c)

                    (infos, cur_ports, ok, hc, dt_switch, reboot,
                     poe, sysinfo, uptime_s) = poll_par_ip[ip]
                    poe_ports = poe.get('ports', {})
                    now = time.time()
                    comm = communautes[0] if communautes else 'public'
                    if not ok:
                        nb_muets += 1
                    dt_def = dt_switch or _ACTIVITE_INTERVAL

                    ip_par_slot[slot_id] = ip
                    mapping, sources, calibre, divergences = _mapping_baie_ifindex(
                        conn, cid, slot_id, sw['appareil_id'], infos)
                    mapping_par_slot[slot_id] = mapping
                    cibles = {r[0]: (r[1] or r[2] or '') for r in conn.execute(
                        "SELECT bp.numero, a.nom_machine, p.categorie FROM baie_slot_ports bp "
                        "LEFT JOIN appareils a ON a.id = bp.appareil_id "
                        "LEFT JOIN peripheriques p ON p.id = bp.peripherique_id "
                        "WHERE bp.slot_id=?", (slot_id,))}

                    _etats_prec = _activite_etat_mappe.setdefault((cid, slot_id), {})

                    # ── état LED de CHAQUE port poll (mappé ou non) ──
                    etats = {}
                    for ifindex, p in cur_ports.items():
                        if not p.get('oper_ok', True):
                            # ifOperStatus pas relevé ce cycle (réponse partielle) :
                            # on ne touche à rien, l'état précédent est conservé
                            # (branche « relevé manqué » plus bas).
                            continue
                        cle_all = (cid, ip, ifindex)
                        pr_all = _activite_prev.get(cle_all)
                        dt_all = dt_switch or ((now - pr_all['ts']) if (pr_all and 'ts' in pr_all)
                                               else _ACTIVITE_INTERVAL)
                        led_all = _etat_led(pr_all, p, dt_all, seuils, reboot=reboot)
                        _pr = pr_all or {}
                        _activite_prev[cle_all] = {
                            'in_oct': p['in_oct'], 'out_oct': p['out_oct'],
                            'in_pkts': p['in_pkts'], 'out_pkts': p['out_pkts'],
                            'in_npkts': p.get('in_npkts', 0), 'out_npkts': p.get('out_npkts', 0),
                            'in_err': p.get('in_err', _pr.get('in_err', 0)),
                            'out_err': p.get('out_err', _pr.get('out_err', 0)),
                            'bps_ema': led_all['bps_ema'], 'pps_ema': led_all['pps_ema'],
                            'etat': led_all['etat'], 'manques': 0, 'ts': now,
                            'etat_pending': led_all.get('etat_pending'),
                            'etat_pending_n': led_all.get('etat_pending_n', 0)}
                        etats[ifindex] = led_all
                        meta = infos.get(ifindex, {})
                        if meta.get('ethernet', True):
                            _pp = _port_physique_depuis_nom(meta.get('nom'))
                            detail_ifs.append({
                                'ip': ip, 'switch_nom': sw['nom'], 'ifindex': ifindex,
                                'nom': meta.get('nom') or f'if{ifindex}',
                                'alias': meta.get('alias') or '',
                                'oper': p['oper'], 'speed_mbps': p['speed_mbps'],
                                'bps': round(led_all['bps']), 'pps': round(led_all['pps'], 1),
                                'etat': led_all['etat'], 'cpt_pegge': p.get('cpt_pegge', False),
                                'poe': poe_ports.get(_pp) if _pp is not None else None})

                    etats_par_ip[ip] = etats     # pour les prises murales reliées à ce switch
                    nb_ethernet = sum(1 for m in infos.values() if m.get('ethernet', True)) or len(cur_ports)

                    debit_total, nb_up, nb_actifs, err_total, nb_manques = 0.0, 0, 0, 0, 0
                    pegge_sw = any(p.get('cpt_pegge') for p in cur_ports.values())
                    if pegge_sw and not _etats_prec.get('_pegge'):
                        journal_ops.append((f"{sw['nom']} ({ip}) — compteurs SNMP d'octets bloqués à 2 Go "
                                            f"(agent défectueux) : activité estimée sur les paquets", 'warn', ip))
                    _etats_prec['_pegge'] = pegge_sw

                    for numero, ifindex in sorted(mapping.items()):
                        cle = (cid, ip, ifindex)
                        p = cur_ports.get(ifindex)
                        led = etats.get(ifindex)
                        if led is None:
                            prev = _activite_prev.get(cle)
                            if not prev:
                                continue
                            manques = prev.get('manques', 0) + 1
                            prev['manques'] = manques
                            etat = prev.get('etat', 'idle')
                            if manques >= _ACTIVITE_MANQUES_STALE and etat != 'stale':
                                etat = 'stale'
                                prev['etat'] = 'stale'
                                journal_ops.append(
                                    (f"{sw['nom']} port {numero} — données obsolètes (SNMP)", 'warn', ip))
                            led = {'etat': etat, 'blink_ms': 0 if etat in ('idle', 'stale', 'down') else 400,
                                   'bps': prev.get('bps_ema', 0), 'pps': prev.get('pps_ema', 0),
                                   'pct': 0.0, 'err_delta': 0}
                            nb_manques += 1
                        else:
                            if led.get('reset'):
                                journal_ops.append(
                                    (f"{sw['nom']} port {numero} — compteur SNMP réinitialisé", 'info', ip))
                            # vitesse inconnue : une seule fois, à l'apparition
                            cle_vit = ('vit', numero)
                            if p and not p.get('speed_mbps'):
                                if not _etats_prec.get(cle_vit):
                                    journal_ops.append(
                                        (f"{sw['nom']} port {numero} — vitesse de lien inconnue", 'info', ip))
                                _etats_prec[cle_vit] = True
                            elif _etats_prec.get(cle_vit):
                                _etats_prec[cle_vit] = False
                            _a, _b = _etats_prec.get(numero), led['etat']
                            if _a in ('idle', 'stale') and _b == 'traffic':
                                journal_ops.append((f"{sw['nom']} port {numero} — devient actif", 'info', ip))
                            elif _a == 'traffic' and _b == 'idle':
                                journal_ops.append((f"{sw['nom']} port {numero} — redevient calme", 'info', ip))
                            elif _a and _a != 'down' and _b == 'down':
                                journal_ops.append((f"{sw['nom']} port {numero} — lien coupé", 'warn', ip))
                        _etats_prec[numero] = led['etat']

                        # historique (sparkline) : uniquement pour les ports mappés
                        h = _activite_hist.setdefault(cle, collections.deque(maxlen=_ACTIVITE_HIST_MAX))
                        h.append((round(now), round(led['bps']), round(led['pps'], 1)))

                        # appareils dont une MAC est apprise sur ce port (FDB live)
                        _macs_port = (fdb_par_ip.get(ip) or {}).get(ifindex, set())
                        _vois = _voisins_port(_macs_port, inv_mac) if _macs_port else None

                        ports_ui.append({'slot_id': slot_id, 'numero': numero,
                                         'etat': led['etat'], 'blink_ms': led['blink_ms'],
                                         'debit_bps': round(led['bps']), 'pps': round(led['pps'], 1),
                                         'err_delta': led['err_delta'],
                                         'nom': (infos.get(ifindex) or {}).get('nom', ''),
                                         'alias': (infos.get(ifindex) or {}).get('alias', ''),
                                         'cible': cibles.get(numero, ''),
                                         'voisins': (_vois or {}).get('noms', []),
                                         'voisins_restants': (_vois or {}).get('restants', 0),
                                         'cpt_pegge': (p or {}).get('cpt_pegge', False)})
                        if led['etat'] not in ('down', 'stale'):
                            nb_up += 1
                        if led['etat'] in ('traffic', 'sature', 'err'):
                            nb_actifs += 1
                        debit_total += led['bps']
                        err_total += led['err_delta']

                        meta = infos.get(ifindex, {})
                        pr = _activite_prev.get(cle, {})
                        detail_ports.append({
                            'ip': ip, 'switch_nom': sw['nom'], 'numero': numero, 'ifindex': ifindex,
                            'port_nom': meta.get('nom') or f'if{ifindex}',
                            'port_alias': meta.get('alias') or '',
                            'cible': cibles.get(numero, ''),
                            'oper': (p or {}).get('oper') or 0, 'speed_mbps': (p or {}).get('speed_mbps', 0),
                            'in_oct': (p or {}).get('in_oct'), 'out_oct': (p or {}).get('out_oct'),
                            'in_pkts': (p or {}).get('in_pkts'), 'out_pkts': (p or {}).get('out_pkts'),
                            'bps': round(led['bps']), 'pps': round(led['pps'], 1),
                            'pct': round(led.get('pct', 0), 1), 'etat': led['etat'],
                            'source_mapping': sources.get(numero, 'non_mappé'),
                            'divergence': numero in divergences,
                            'manques': pr.get('manques', 0),
                            'cpt_pegge': (p or {}).get('cpt_pegge', False),
                            'poe': poe_ports.get(numero),
                            'hist': [[t, b, pp] for t, b, pp in list(h)],   # [ts, bps, pps]
                            'voisins': (_vois or {}).get('noms', []),
                            'voisins_n': (_vois or {}).get('n', 0),
                            'stale': led['etat'] == 'stale'})
                        _poe_p = poe_ports.get(numero)
                        if _poe_p and _poe_p['statut'] == 4 and _etats_prec.get(('poe', numero)) != 4:
                            journal_ops.append((f"{sw['nom']} port {numero} — défaut PoE", 'warn', ip))
                        if _poe_p:
                            _etats_prec[('poe', numero)] = _poe_p['statut']

                    if divergences and set(divergences) != set(_etats_prec.get('_div', [])):
                        journal_ops.append(
                            (f"{sw['nom']} ({ip}) — la topologie et le nom d'interface divergent sur "
                             f"le(s) port(s) {', '.join(map(str, sorted(divergences)))} : calibration à vérifier",
                             'warn', ip))
                    _etats_prec['_div'] = list(divergences)

                    # alerte budget PoE
                    poe_alerte = False
                    if poe.get('total_w') and poe.get('budget_w'):
                        poe_alerte = poe['total_w'] / poe['budget_w'] > 0.9
                        if poe_alerte and not _etats_prec.get('_poe_budget'):
                            journal_ops.append(
                                (f"{sw['nom']} ({ip}) — budget PoE presque atteint "
                                 f"({poe['total_w']}/{poe['budget_w']} W)", 'warn', ip))
                        _etats_prec['_poe_budget'] = poe_alerte

                    equipements.append({
                        'ip': ip, 'nom': sw['nom'], 'appareil_id': sw['appareil_id'],
                        'calibre': calibre, 'debit_total_bps': round(debit_total),
                        'nb_ports_up': nb_up, 'nb_actifs': nb_actifs, 'erreurs': err_total})
                    detail_sw.append({
                        'ip': ip, 'nom': sw['nom'], 'appareil_id': sw['appareil_id'],
                        'slot_id': slot_id,
                        'sysname': sysinfo.get('sysname', ''),
                        'modele': _modele_court(sysinfo.get('sysdescr', '')),
                        'uptime_s': uptime_s, 'redemarre': reboot,
                        'derniere_maj': _now_z(), 'duree_poll_ms': _poll_max_ms[0],
                        'dt_s': round(dt_switch, 1) if dt_switch else None,
                        'communaute': comm, 'compteurs_64bits': hc,
                        'nb_ifindex_ethernet': nb_ethernet,
                        'nb_ports_mappes': len(mapping), 'nb_manques': nb_manques,
                        'nb_divergences': len(divergences),
                        'nb_ports_calibres': sum(1 for s in sources.values()
                                                 if s in ('manuel', 'topologie', 'nom_port')),
                        'poe': bool(poe_ports), 'poe_alerte': poe_alerte,
                        'poe_total_w': poe.get('total_w'), 'poe_budget_w': poe.get('budget_w'),
                        'poe_nb_alimentes': sum(1 for x in poe_ports.values() if x['statut'] == 3),
                        'calibre': calibre})

                # ── prises murales d'un bandeau RJ : LED via le port de switch du
                #    cordon de brassage + contrôle de câblage (FDB live, repli topologie) ──
                if etats_par_ip:
                    topo_par_ip = {}
                    for eip, pidx, vaid in conn.execute(
                            "SELECT equipement_ip, port_index, appareil_vu_id FROM diag_topologie "
                            "WHERE client_id=? AND appareil_vu_id IS NOT NULL", (cid,)):
                        try:
                            topo_par_ip.setdefault(eip, {}).setdefault(int(pidx), set()).add(int(vaid))
                        except (TypeError, ValueError):
                            continue
                    noms_par_ip = {ipx: v[0] for ipx, v in poll_par_ip.items()}
                    pm_ports, pm_journal = _prises_murales_activite(
                        conn, cid, ip_par_slot, etats_par_ip, mapping_par_slot,
                        noms_par_ip, topo_par_ip, fdb_par_ip, inv_mac,
                        lambda sid: _activite_etat_mappe.setdefault((cid, sid), {}))
                    ports_ui.extend(pm_ports)
                    journal_ops.extend(pm_journal)
            finally:
                conn.close()
            motif = ''
            if not switchs:
                motif = 'aucun_switch'
            elif not any(e['nb_ports_up'] or e['nb_actifs'] for e in equipements) and nb_muets == len(switchs):
                motif = 'sans_reponse'
            with _activite_lock:
                _activite_resultat[cid] = {
                    'actif': True, 'ts': time.time(), 'nb_switchs': len({s['ip'] for s in switchs}),
                    'nb_muets': nb_muets, 'motif': motif, 'cadence_s': round(_cadence[0]),
                    'calibre': bool(equipements) and all(e['calibre'] for e in equipements),
                    'equipements': equipements, 'ports': ports_ui}
                _activite_detail[cid] = {'ts': _now_z(), 'switchs': detail_sw,
                                         'ports': detail_ports, 'interfaces': detail_ifs}
            _activite_echecs[cid] = 0
        except Exception:
            n = _activite_echecs.get(cid, 0) + 1
            _activite_echecs[cid] = n
            if n >= _ACTIVITE_ECHECS_MAX:
                logger.warning('network_diag: cycle activité — client %s en échec %d fois', cid, n)
                with _activite_lock:
                    _activite_resultat[cid] = {'actif': False, 'motif': 'erreur_interne'}
            else:
                logger.debug('network_diag: cycle activité — client %s en échec', cid, exc_info=True)
        for msg, niv, cib in journal_ops:
            _journal(msg, niv, cib)
        for sid, num, ifx in calib_a_appliquer:      # écriture hors de la connexion de lecture
            if calibrer_port_baie(cid, sid, num, ifx):
                _journal(f"port {num} calibré automatiquement (interface ifIndex {ifx})", 'info')


def _activite_loop():
    time.sleep(2)   # le thread est démarré à la 1re requête navigateur : l'app tourne déjà
    while True:
        try:
            now = time.time()
            with _activite_lock:
                clients = [c for c, t in _activite_heartbeat.items()
                           if now - t < _ACTIVITE_TTL_HEARTBEAT]
                partis = [c for c, t in list(_activite_heartbeat.items())
                          if now - t > _ACTIVITE_PURGE]
                for c in partis:
                    _activite_heartbeat.pop(c, None)
                    _activite_resultat.pop(c, None)
                    _activite_detail.pop(c, None)
                if partis:      # structures par (client, …) d'un client qui ne regarde plus
                    pset = set(partis)
                    for reg in (_activite_switch_ok, _activite_etat_mappe, _activite_calib,
                                _activite_hist, _activite_sut):
                        for k in [k for k in reg if k[0] in pset]:
                            reg.pop(k, None)
                    for c in pset:
                        _activite_echecs.pop(c, None)
                for k in [k for k, v in list(_activite_prev.items())
                          if now - v.get('ts', 0) > _ACTIVITE_PURGE]:
                    _activite_prev.pop(k, None)
                    _activite_hist.pop(k, None)
                for k in [k for k, v in list(_activite_sysinfo.items())
                          if now - v['ts'] > max(_ACTIVITE_PURGE, _ACTIVITE_SYSINFO_TTL * 2)]:
                    _activite_sysinfo.pop(k, None)
                for k in [k for k, v in list(_activite_infra_mac.items())
                          if now - v[0] > max(_ACTIVITE_PURGE, _ACTIVITE_SYSINFO_TTL * 2)]:
                    _activite_infra_mac.pop(k, None)
                for k in [k for k, v in list(_activite_fdb.items())
                          if now - v[0] > max(_ACTIVITE_PURGE, _ACTIVITE_FDB_PERIME + 60)]:
                    _activite_fdb.pop(k, None)
                    _activite_fdb_dialecte.pop(k, None)
                    _activite_fdb_echec.pop(k, None)
                for k in [k for k, v in list(_activite_fdb_baseport.items())
                          if now - v[0] > max(_ACTIVITE_PURGE, _ACTIVITE_SYSINFO_TTL * 2)]:
                    _activite_fdb_baseport.pop(k, None)
                for k in [k for k, v in list(_activite_noms.items())
                          if now - v['ts'] > max(_ACTIVITE_PURGE, _ACTIVITE_NOMS_TTL * 2)]:
                    _activite_noms.pop(k, None)
                # assistant lancé puis abandonné (modale fermée) : bien au-delà de
                # sa fenêtre → personne ne viendra le récupérer, on le retire.
                for k in [k for k, v in list(_activite_calib.items())
                          if now - v.get('debut', 0) > _CALIB_FENETRE * 2]:
                    _activite_calib.pop(k, None)
            if not clients:
                _activite_rechauffe[0] = 0
                time.sleep(5)
                continue
            if str(_cfg('diag_snmp_actif', '0')) != '1':
                with _activite_lock:
                    for c in clients:
                        _activite_resultat[c] = {'actif': False}
                        _activite_detail[c] = {'ts': _now_z(), 'switchs': [], 'ports': []}
                time.sleep(5)
                continue
            _poll_max_ms[0] = 0
            _cycle_activite(clients)
            _activite_rechauffe[0] += 1
        except Exception:
            logger.debug('network_diag: _activite_loop', exc_info=True)
        # cadence adaptative : un switch SNMP lent (bas de gamme) met plusieurs
        # secondes à répondre — inutile (et contre-productif) de le re-solliciter
        # toutes les 3 s. On espace en proportion, plafonné à 30 s.
        # MAIS à froid (les 2-3 premiers cycles), on garde une cadence courte :
        # il faut deux relevés pour calculer un débit, l'utilisateur attend ce
        # deuxième passage — pas la peine de lui imposer 30 s d'attente en plus.
        _cadence[0] = max(_ACTIVITE_INTERVAL, min(30.0, _poll_max_ms[0] / 1000.0 * 1.3))
        # cadence courte : au réchauffement (2 relevés = 1er débit) OU tant qu'un
        # assistant de calibration attend (il compare oper entre deux relevés —
        # 30 s d'écart le rendraient inutilisable).
        if _activite_rechauffe[0] < _ACTIVITE_RECHAUFFE_CYCLES or _activite_calib:
            _cadence[0] = _ACTIVITE_INTERVAL
        time.sleep(_cadence[0])


def _demarrer_activite_thread():
    """Démarre le thread d'activité si besoin (idempotent, lazy). Sous verrou :
    la page et la modale peuvent appeler simultanément au chargement."""
    global _activite_thread
    with _activite_thread_lock:
        if _activite_thread and _activite_thread.is_alive():
            return
        _activite_thread = threading.Thread(target=_activite_loop, daemon=True,
                                            name='DiagBaieActivite')
        _activite_thread.start()


def activite_baie(client_id: int) -> dict:
    """Enregistre un battement et renvoie l'état d'activité déjà calculé.
    Réponse instantanée (aucun SNMP synchrone). `actif` : True = données
    présentes, False = SNMP désactivé, None = premier passage pas encore fait."""
    with _activite_lock:
        _activite_heartbeat[client_id] = time.time()
        res = dict(_activite_resultat.get(client_id, {'actif': None}))
    _demarrer_activite_thread()
    return res


# ── Moniteur (modale au-dessus de la baie) ──────────────────────────────────

def moniteur_baie(client_id: int) -> dict:
    """Données du panneau moniteur : journal, détail par switch/port, liste des
    interfaces (pour la calibration), ports de baie à calibrer, état de la
    capture. Enregistre aussi un battement (comme activite_baie)."""
    with _activite_lock:
        _activite_heartbeat[client_id] = time.time()
        detail = dict(_activite_detail.get(client_id, {}))
        journal = [dict(x) for x in _activite_journal]
    _demarrer_activite_thread()
    cap = statut_capture_baie()
    etat = etat_capture()

    # ports de baie (numéro + appareil branché + calibration actuelle) par slot
    ports_baie = {}
    try:
        from database import get_local_db
        conn = get_local_db()
        try:
            for sw in detail.get('switchs', []):
                sid = sw.get('slot_id')
                if sid is None:
                    continue
                rows = conn.execute(
                    "SELECT bp.numero, bp.if_index, a.nom_machine, p.categorie "
                    "FROM baie_slot_ports bp "
                    "LEFT JOIN appareils a ON a.id = bp.appareil_id "
                    "LEFT JOIN peripheriques p ON p.id = bp.peripherique_id "
                    "WHERE bp.slot_id=? ORDER BY bp.numero", (sid,)).fetchall()
                ports_baie[sw['ip']] = [
                    {'numero': r[0], 'if_index': r[1],
                     'cible': r[2] or r[3] or ''} for r in rows]
        finally:
            conn.close()
    except Exception:
        pass

    # assistant de calibration « par débranchement » en cours pour ce client.
    # LECTURE SEULE : l'application de la calibration est faite par le thread
    # _activite_loop (_maj_assistant_calibration), pas ici.
    calib = None
    with _activite_lock:
        for (c, sid), a in list(_activite_calib.items()):
            if c != client_id:
                continue
            if a['trouve'] and a['trouve'] > 0:
                ifx = a['trouve']
                _activite_calib.pop((c, sid), None)          # affiché une fois, puis oublié
                nom = next((i['nom'] for i in detail.get('interfaces', [])
                            if i['ifindex'] == ifx), f'if{ifx}')
                calib = {'slot_id': sid, 'numero': a['numero'], 'etat': 'trouve',
                         'ifindex': ifx, 'nom': nom}
            elif a['trouve'] == 0:
                _activite_calib.pop((c, sid), None)
                calib = {'slot_id': sid, 'numero': a['numero'], 'etat': 'expire'}
            else:
                calib = {'slot_id': sid, 'numero': a['numero'], 'etat': 'attente',
                         'restant_s': max(0, round(_CALIB_FENETRE - (time.time() - a['debut'])))}
            break

    return {
        'ts': detail.get('ts'),
        'cadence_s': round(_cadence[0]),
        'calibration_assistant': calib,
        'switchs': detail.get('switchs', []),
        'ports': detail.get('ports', []),
        'interfaces': detail.get('interfaces', []),
        'ports_baie': ports_baie,
        'journal': journal,
        'snmp_actif': str(_cfg('diag_snmp_actif', '0')) == '1',
        'repli_naif': str(_cfg('diag_baie_activite_repli_naif', '0')) == '1',
        'capture': {
            'disponible': etat['disponible'], 'motif': etat['motif'],
            'en_cours': cap['running'], 'progress': cap['progress'],
            'resultat': cap['resultat'] if cap.get('client_id') == client_id else None,
        },
    }


def calibrer_port_baie(client_id: int, slot_id: int, numero: int, if_index) -> bool:
    """Fixe (ou efface si if_index falsy) l'ifIndex SNMP d'un port de baie.
    Le slot doit appartenir au client. Retourne True si appliqué."""
    from database import get_db
    conn = get_db()
    try:
        row = conn.execute("SELECT 1 FROM baie_slots WHERE id=? AND client_id=?",
                           (slot_id, client_id)).fetchone()
        if not row:
            return False
        val = None
        if if_index not in (None, '', 0, '0'):
            try:
                val = int(if_index)
            except (TypeError, ValueError):
                return False
        cur = conn.execute(
            "UPDATE baie_slot_ports SET if_index=?, date_maj=? WHERE slot_id=? AND numero=?",
            (val, _now_z(), slot_id, numero))
        if cur.rowcount == 0:      # ce numéro n'est pas un port réel du slot → refus
            return False           # (les lignes baie_slot_ports sont créées d'avance, cf. _reconcilier_ports)
        conn.commit()
        return True
    except Exception:
        logger.exception('network_diag: calibrer_port_baie')
        return False
    finally:
        conn.close()


def calibrer_decalage_baie(client_id: int, slot_id: int, offset) -> int:
    """Calibre tous les ports RJ (1-48) d'un slot en une fois : if_index =
    numero + offset. Retourne le nombre de ports mis à jour (0 si slot absent
    du client ou offset invalide)."""
    from database import get_db
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        return 0
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM baie_slots WHERE id=? AND client_id=?",
                            (slot_id, client_id)).fetchone():
            return 0
        n = 0
        for (numero,) in conn.execute(
                "SELECT numero FROM baie_slot_ports WHERE slot_id=? AND numero<=48 ORDER BY numero",
                (slot_id,)).fetchall():
            v = numero + offset
            if v < 1:
                continue
            conn.execute("UPDATE baie_slot_ports SET if_index=?, date_maj=? "
                         "WHERE slot_id=? AND numero=?", (v, _now_z(), slot_id, numero))
            n += 1
        conn.commit()
        return n
    except Exception:
        logger.exception('network_diag: calibrer_decalage_baie')
        return 0
    finally:
        conn.close()


_BRASSAGE_UPLINK_MAX = 8   # au-delà de N MAC apprises sur un port, c'est un uplink :
                           #   la machine est derrière, pas branchée là → confiance faible


def _elements_baie(conn, client_id):
    """Éléments de rack associés à un appareil. Retourne
    ({appareil_id: {slot_id, nom, type_equipement, type_appareil, ports:[numero]}},
     {mac_normalisée: appareil_id}, {slot_id: nom})."""
    elems, mac_infra, nom_par_slot = {}, {}, {}
    for slot_id, aid, nom_c, typ_eq, nom_m, typ_app, mac in conn.execute(
            "SELECT s.id, s.appareil_id, s.nom_custom, s.type_equipement, "
            "a.nom_machine, a.type_appareil, a.adresse_mac "
            "FROM baie_slots s JOIN appareils a ON a.id = s.appareil_id "
            "WHERE s.client_id=?", (client_id,)):
        nom = nom_c or nom_m or typ_eq or f'#{slot_id}'
        nom_par_slot[slot_id] = nom
        elems[aid] = {'slot_id': slot_id, 'nom': nom, 'type_equipement': typ_eq,
                      'type_appareil': typ_app, 'ports': []}
        if mac:
            mac_infra[_norm_mac(mac)] = aid
    for e in elems.values():
        e['ports'] = [r[0] for r in conn.execute(
            "SELECT numero FROM baie_slot_ports WHERE slot_id=? ORDER BY numero", (e['slot_id'],))]
    # noms de tous les éléments (même sans appareil) pour l'affichage « actuel »
    for slot_id, nom_c, typ_eq in conn.execute(
            "SELECT id, nom_custom, type_equipement FROM baie_slots WHERE client_id=?", (client_id,)):
        nom_par_slot.setdefault(slot_id, nom_c or typ_eq or f'#{slot_id}')
    return elems, mac_infra, nom_par_slot


def analyser_brassage_baie(client_id: int) -> dict:
    """« Carte réseau proposée » : part de TOUTES les MAC apprises sur les switchs
    de la baie, les corrèle à l'inventaire (nom d'inventaire pour les machines
    connues), et en déduit un jeu de propositions. **Ne modifie rien.**

    Groupes (chaque proposition : `action` 'creer'|'modifier' + libellé `actuel*`) :
      - `prises_appareils` : prise murale dont le cordon est posé → l'appareil
        réellement vu sur le port de switch au bout (assigne `baie_prises_murales`)
      - `ports_appareils`  : port de switch NON brassé → l'appareil vu directement
        dessus (assigne `baie_slot_ports.appareil_id`)
      - `cordons`          : bandeau ⇄ switch, déduit d'une machine déclarée sur
        une prise dont la MAC est vue sur un port de switch
      - `liens_baie`       : switch ⇄ autre élément de la baie (MAC d'infra vue,
        port du voisin par LLDP ou FDB réciproque)
    Plus, à titre d'information : `cascades` (switch non géré / borne Wi-Fi en
    aval d'une prise) et `hors_inventaire` (appareil vu mais absent de l'inventaire).
    """
    from database import get_db
    vide = {'prises_appareils': [], 'ports_appareils': [], 'cordons': [],
            'liens_baie': [], 'cascades': [], 'hors_inventaire': [], 'retypage': [],
            'switchs_illisibles': [], 'switchs_tronques': [], 'fdb': []}
    if str(_cfg('diag_snmp_actif', '0')) != '1':
        return {'ok': False, 'motif': 'snmp_inactif', **vide}
    communautes = _communautes_snmp()
    conn = get_db()
    try:
        switchs = _switchs_baie(conn, client_id)
        if not switchs:
            return {'ok': False, 'motif': 'aucun_switch', **vide}

        inv_mac, nom_par_aid, typ_par_aid = {}, {}, {}
        for aid, nom, mac, typ in conn.execute(
                "SELECT id, nom_machine, adresse_mac, type_appareil FROM appareils WHERE client_id=?",
                (client_id,)):
            nom_par_aid[aid] = nom or f'#{aid}'
            typ_par_aid[aid] = (typ or '').strip()
            if mac:
                inv_mac[_norm_mac(mac)] = (aid, nom or f'#{aid}', typ)
        elems, mac_infra, nom_par_slot = _elements_baie(conn, client_id)
        aids_baie = set(elems)

        # relevé mutualisé par IP : noms d'interface + table MAC (bridge + ARP +
        # correction de forme + réglage manuel `diag_fdb_mode:<ip>`)
        infos_par_ip, fdb_par_ip, fdb_meta_par_ip = {}, {}, {}
        for sw in switchs:
            if sw['ip'] not in infos_par_ip:
                infos_par_ip[sw['ip']] = _noms_interfaces(sw['ip'], communautes)
                fdb_par_ip[sw['ip']], fdb_meta_par_ip[sw['ip']] = _releve_mac_switch(
                    sw['ip'], communautes, inv_mac)

        # MAC « propres » de chaque switch, relevées en SNMP (base bridge +
        # ifPhysAddress) → toutes rattachées à l'appareil comme MAC d'infra.
        infra_snmp_par_ip = {}
        for sw in switchs:
            if sw['ip'] not in infra_snmp_par_ip:
                infra_snmp_par_ip[sw['ip']] = _macs_infra_switch(sw['ip'], communautes)
                for mm in infra_snmp_par_ip[sw['ip']]:
                    mac_infra.setdefault(mm, sw['appareil_id'])

        sw_ctx = []
        for sw in switchs:
            mapping, *_r = _mapping_baie_ifindex(conn, client_id, sw['slot_id'],
                                                sw['appareil_id'], infos_par_ip[sw['ip']])
            m = fdb_meta_par_ip.get(sw['ip'], {})
            mgmt = {mm for mm, a in mac_infra.items() if a == sw['appareil_id']}
            sw_ctx.append({**sw, 'ifx_to_num': {ifx: num for num, ifx in mapping.items()},
                           'fdb': fdb_par_ip.get(sw['ip']) or {},
                           'fdb_meta': m, 'fdb_tronquee': bool(m.get('tronquee')),
                           'mgmt_macs': mgmt})
        ctx_par_slot = {c['slot_id']: c for c in sw_ctx}
        # une IP -> son état FDB (pour l'UI) : nom, mode, hypothèse, reconnues/total
        fdb_ui, vus_ip = [], set()
        for c in sw_ctx:
            if c['ip'] in vus_ip:
                continue
            vus_ip.add(c['ip'])
            mm = c['fdb_meta']
            fdb_ui.append({'nom': c['nom'], 'ip': c['ip'],
                           'mode': str(_cfg(f"diag_fdb_mode:{c['ip']}", '')) or 'auto',
                           'transform': mm.get('transform', 'exact'),
                           'reconnues': mm.get('reconnues', 0), 'total': mm.get('total', 0),
                           'fiable': mm.get('fiable', True)})
        switchs_illisibles = sorted({c['nom'] for c in sw_ctx
                                     if c['fdb_tronquee'] and not c['fdb']})

        # état actuel : liens (cordons) et cibles directes de tous les ports du client
        liens, cibles = {}, {}
        for slot_id, numero, l_sid, l_num, p_aid in conn.execute(
                "SELECT slot_id, numero, lie_slot_id, lie_port_numero, appareil_id FROM baie_slot_ports "
                "WHERE slot_id IN (SELECT id FROM baie_slots WHERE client_id=?)", (client_id,)):
            if l_sid:
                liens[(slot_id, numero)] = (l_sid, l_num)
                liens.setdefault((l_sid, l_num), (slot_id, numero))   # lien à sens unique toléré
            if p_aid:
                cibles[(slot_id, numero)] = p_aid

        ph = ','.join('?' * len(_TYPES_BANDEAU))
        prises_decl = {}   # (bandeau_slot, numero) -> (appareil_id, nom, mac)
        for s_id, numero, aid, nom, mac in conn.execute(
                "SELECT pm.slot_id, pm.numero, pm.appareil_id, a.nom_machine, a.adresse_mac "
                "FROM baie_prises_murales pm LEFT JOIN appareils a ON a.id=pm.appareil_id "
                "WHERE pm.slot_id IN (SELECT id FROM baie_slots WHERE client_id=? "
                f"AND type_equipement IN ({ph}))", (client_id, *_TYPES_BANDEAU)):
            prises_decl[(s_id, numero)] = (aid, nom or '', _norm_mac(mac or ''))
        aids_sur_prise = {v[0] for v in prises_decl.values() if v[0]}

        # voisins LLDP/CDP relevés au dernier passage du palier 4 :
        # (appareil_switch, port_index) -> {port, subtype, mac, caps, nom}
        lldp = {}
        for eq_aid, pidx, vp, vst, vmac, vcaps, vnom in conn.execute(
                "SELECT equipement_appareil_id, port_index, voisin_port, "
                "COALESCE(voisin_port_subtype,''), COALESCE(voisin_mac,''), "
                "COALESCE(voisin_caps,''), voisin_nom FROM diag_topologie "
                "WHERE client_id=? AND voisin_nom!='' AND equipement_appareil_id IS NOT NULL", (client_id,)):
            try:
                lldp[(eq_aid, int(pidx))] = {'port': vp or '', 'subtype': vst,
                                             'mac': vmac, 'caps': vcaps, 'nom': vnom}
            except (TypeError, ValueError):
                pass

        prises_appareils, ports_appareils, cordons = [], [], []
        liens_baie, cascades, hors_inv = [], [], []
        vus_liens = set()

        def _cmp_lien(cle, autre_slot, autre_port):
            cur = liens.get(cle)
            if cur == (autre_slot, autre_port):
                return None, None
            if cur:
                return 'modifier', f"{nom_par_slot.get(cur[0], '?')} port {cur[1]}"
            return 'creer', ''

        # ── passe 1 : chaque port mappé de chaque switch ──
        for c in sw_ctx:
            for ifx, macs in c['fdb'].items():
                port_num = c['ifx_to_num'].get(ifx)
                if port_num is None:
                    continue
                cle = (c['slot_id'], port_num)
                infra_ici = [(m, mac_infra[m]) for m in sorted(macs)
                             if m in mac_infra and mac_infra[m] != c['appareil_id']]

                # (D) lien switch ⇄ autre élément de la baie. Voisin = un élément
                #     dont une MAC d'infra est apprise sur ce port, OU un voisin
                #     LLDP/CDP dont le nom/MAC recoupe un élément de la baie.
                lv = lldp.get((c['appareil_id'], ifx))
                autre_aid = infra_ici[0][1] if infra_ici else None
                if autre_aid is None and lv:
                    if lv['mac'] and lv['mac'] in mac_infra:
                        autre_aid = mac_infra[lv['mac']]
                    elif lv['nom']:
                        autre_aid = next((a for a, e in elems.items()
                                          if e['nom'].lower() == lv['nom'].lower()), None)
                if autre_aid is not None:
                    autre = elems.get(autre_aid)
                    paire = autre and tuple(sorted((c['slot_id'], autre['slot_id'])))
                    if autre and autre['slot_id'] != c['slot_id'] and paire not in vus_liens:
                        b_port, via = None, 'fdb'
                        ac = ctx_par_slot.get(autre['slot_id'])
                        # 1) port du voisin par LLDP/CDP, selon le sous-type de PortID
                        if lv:
                            st, pv = lv['subtype'], lv['port']
                            if st == 'mac' and ac:
                                for a_ifx, a_macs in (ac['fdb'] or {}).items():
                                    if pv in a_macs:
                                        b_port, via = ac['ifx_to_num'].get(a_ifx), 'lldp'
                                        break
                            elif st == 'local' and ac and pv.isdigit():
                                b_port, via = ac['ifx_to_num'].get(int(pv)), 'lldp'
                            elif _port_physique_depuis_nom(pv):
                                b_port, via = _port_physique_depuis_nom(pv), 'lldp'
                        # 2) sinon : la mgmt MAC de C vue sur un port du voisin (FDB réciproque)
                        if b_port is None and c['mgmt_macs'] and ac:
                            for a_ifx, a_macs in (ac['fdb'] or {}).items():
                                if c['mgmt_macs'] & a_macs:
                                    b_port = ac['ifx_to_num'].get(a_ifx)
                                    break
                        if b_port and b_port in autre['ports']:
                            act, actu = _cmp_lien(cle, autre['slot_id'], b_port)
                            if act:
                                liens_baie.append({
                                    'a_slot_id': c['slot_id'], 'a_nom': c['nom'], 'a_port': port_num,
                                    'b_slot_id': autre['slot_id'], 'b_nom': autre['nom'], 'b_port': b_port,
                                    'via': via, 'action': act, 'actuel': actu})
                            vus_liens.add(paire)
                    continue

                if len(macs) >= 2:
                    casc = _classer_cascade(macs, inv_mac, lv.get('caps') if lv else '')
                    vois = _voisins_port(macs, inv_mac)
                    b_lien = liens.get(cle)
                    prise = None
                    if b_lien and b_lien in prises_decl:
                        prise = f"{nom_par_slot.get(b_lien[0], '?')} prise {b_lien[1]}"
                    cascades.append({'switch_nom': c['nom'], 'switch_port_numero': port_num,
                                     'type': casc['type'], 'indices': casc['indices'],
                                     'n_macs': casc['n_macs'], 'appareils': vois['noms'], 'prise': prise})
                    continue

                m = next(iter(macs))
                if m not in inv_mac:
                    hors_inv.append({'switch_nom': c['nom'], 'switch_port_numero': port_num,
                                     'mac': m, 'vendor': _vendor(m) or ''})
                    continue
                aid, nom, _typ = inv_mac[m]
                b_lien = liens.get(cle)
                if b_lien and b_lien in prises_decl:
                    decl = prises_decl[b_lien]
                    if decl[0] != aid:
                        prises_appareils.append({
                            'bandeau_slot_id': b_lien[0], 'bandeau_nom': nom_par_slot.get(b_lien[0], '?'),
                            'prise_numero': b_lien[1], 'machine_id': aid, 'machine_nom': nom,
                            'switch_nom': c['nom'], 'switch_port_numero': port_num,
                            'action': 'modifier' if decl[0] else 'creer', 'actuel_nom': decl[1]})
                elif not b_lien:
                    cur = cibles.get(cle)
                    # un appareil déjà déclaré sur une prise murale relève du cordon
                    # (bandeau⇄switch), pas d'une affectation directe au port
                    if cur != aid and aid not in aids_baie and aid not in aids_sur_prise:
                        ports_appareils.append({
                            'switch_slot_id': c['slot_id'], 'switch_nom': c['nom'],
                            'switch_port_numero': port_num, 'machine_id': aid, 'machine_nom': nom,
                            'action': 'modifier' if cur else 'creer',
                            'actuel_nom': nom_par_aid.get(cur, '') if cur else ''})

        # ── passe 2 : cordons bandeau ⇄ switch depuis les machines de prises ──
        for (b_sid, b_num), (aid, m_nom, mac) in prises_decl.items():
            if not aid or not mac or (b_sid, b_num) in liens:
                continue
            cands = []
            for c in sw_ctx:
                for ifx, macs in c['fdb'].items():
                    if mac in macs and c['ifx_to_num'].get(ifx) is not None:
                        cands.append((len(macs), c, c['ifx_to_num'][ifx]))
            if not cands:
                continue
            cands.sort(key=lambda x: x[0])
            vu_avec, c, port_num = cands[0]
            occupe = (c['slot_id'], port_num) in liens
            cordons.append({
                'bandeau_slot_id': b_sid, 'bandeau_nom': nom_par_slot.get(b_sid, '?'),
                'prise_numero': b_num, 'machine_nom': m_nom,
                'switch_slot_id': c['slot_id'], 'switch_nom': c['nom'], 'switch_port_numero': port_num,
                'action': 'creer',
                'confiance': 'faible' if (vu_avec > _BRASSAGE_UPLINK_MAX or occupe) else 'sure',
                'note': ('ce port de switch est déjà brassé ailleurs' if occupe else '')})

        # ── retypage : capacités LLDP du voisin ≠ type d'inventaire ──
        # Le voisin LLDP se déclare pont / routeur / borne Wi-Fi / téléphone.
        # Si l'appareil correspondant en inventaire porte un type franchement
        # incompatible, on le signale (à titre indicatif, aucune modification).
        _CAP_TYPE = (
            (('wlan',), (), ('Borne Wi-Fi', 'Switch/AP', 'Pont Wi-Fi', "Point d'accès",
                             'Borne WiFi'), 'Borne Wi-Fi'),
            (('router',), ('bridge', 'wlan'), ('Routeur/Pare-feu', 'Box internet (FAI)'),
             'Routeur/Pare-feu'),
            (('bridge',), ('router', 'wlan'), ('Switch', 'Switch/AP'), 'Switch'),
            (('phone',), (), ('Telephone IP', 'Téléphone IP'), 'Telephone IP'),
        )
        retypage, vus_retypage = [], set()
        for (eq_aid, ifx), lv in lldp.items():
            caps = (lv.get('caps') or '').lower()
            if not caps:
                continue
            aid = None
            mm = _norm_mac(lv.get('mac') or '')
            if mm and mm in inv_mac:
                aid = inv_mac[mm][0]
            elif mm and mm in mac_infra:
                aid = mac_infra[mm]
            if aid is None and lv.get('nom'):
                aid = next((a for a, e in elems.items()
                            if e['nom'].lower() == lv['nom'].lower()), None)
            if aid is None or aid in vus_retypage:
                continue
            actuel = typ_par_aid.get(aid, '')
            for need, interdits, oks, propose in _CAP_TYPE:
                if all(n in caps for n in need) and not any(i in caps for i in interdits) \
                        and actuel not in oks:
                    retypage.append({
                        'machine_id': aid, 'machine_nom': nom_par_aid.get(aid, f'#{aid}'),
                        'type_actuel': actuel or '(non renseigné)', 'type_propose': propose,
                        'motif': 'LLDP : ' + ', '.join(sorted(set(caps.split(','))))})
                    vus_retypage.add(aid)
                    break

        _k = lambda x: (str(x.get('switch_nom', '')), str(x.get('bandeau_nom', '')),
                        int(x.get('switch_port_numero', x.get('prise_numero', 0)) or 0))
        for g in (prises_appareils, ports_appareils, cordons, liens_baie, cascades, hors_inv):
            g.sort(key=_k)
        retypage.sort(key=lambda x: str(x.get('machine_nom', '')))
        tronques = sorted({c['nom'] for c in sw_ctx if c['fdb_tronquee'] and c['fdb']})
        return {'ok': True, 'nb_switchs': len({s['ip'] for s in switchs}),
                'prises_appareils': prises_appareils, 'ports_appareils': ports_appareils,
                'cordons': cordons, 'liens_baie': liens_baie,
                'cascades': cascades, 'hors_inventaire': hors_inv, 'retypage': retypage,
                'switchs_illisibles': switchs_illisibles, 'switchs_tronques': tronques,
                'fdb': fdb_ui}
    except Exception:
        logger.exception('network_diag: analyser_brassage_baie')
        return {'ok': False, 'motif': 'erreur_interne', **vide}
    finally:
        conn.close()



# ── Capture de trafic à la demande (onglet « Trafic capturé ») ──────────────

def capturer_trafic(duree: int, client_id=None) -> dict:
    """Capture passive courte → stats pour le moniteur : répartition
    broadcast/multicast/unicast, top talkers par MAC, anomalies rapides.
    Retourne toujours un dict ({disponible:False, motif} si scapy indispo)."""
    etat = etat_capture()
    if not etat['disponible']:
        return {'disponible': False, 'motif': etat['motif']}
    s = _charger_scapy()
    if s is None:
        return {'disponible': False, 'motif': 'scapy_absent'}

    stats = {'total': 0, 'broadcast': 0, 'multicast': 0, 'unicast': 0}
    protos = collections.Counter()
    par_mac = collections.Counter()
    oct_mac = collections.Counter()
    arp_grat = collections.Counter()
    dhcp_srv = set()

    def _on(pkt):
        stats['total'] += 1
        try:
            if pkt.haslayer(s.Ether):
                e = pkt[s.Ether]
                dst = (e.dst or '').lower()
                if dst == 'ff:ff:ff:ff:ff:ff':
                    stats['broadcast'] += 1
                elif dst and int(dst.split(':')[0], 16) & 1:
                    stats['multicast'] += 1
                else:
                    stats['unicast'] += 1
                src = _norm_mac(e.src)
                if src:
                    par_mac[src] += 1
                    oct_mac[src] += len(pkt)
            if pkt.haslayer(s.ARP):
                protos['arp'] += 1
                a = pkt[s.ARP]
                if a.op == 2 and a.pdst == a.psrc and a.psrc not in ('', '0.0.0.0'):
                    arp_grat[a.psrc] += 1
            elif pkt.haslayer(s.DHCP):
                protos['dhcp'] += 1
                for opt in pkt[s.DHCP].options:
                    if isinstance(opt, tuple) and opt[0] == 'server_id':
                        dhcp_srv.add(str(opt[1]))
            elif pkt.haslayer(s.TCP):
                protos['tcp'] += 1
            elif pkt.haslayer(s.UDP):
                protos['udp'] += 1
            else:
                protos['autre'] += 1
        except Exception:
            pass

    t0 = time.time()
    try:
        sniffer = s.AsyncSniffer(prn=_on, store=False)
        sniffer.start()
        time.sleep(max(3, duree))
        sniffer.stop()
    except PermissionError:
        return {'disponible': False, 'motif': 'privileges_insuffisants'}
    except Exception:
        logger.debug('network_diag: capturer_trafic interrompue', exc_info=True)
        return {'disponible': False, 'motif': 'privileges_insuffisants'}
    ecoule = max(1.0, time.time() - t0)

    inv = {}
    if client_id:
        try:
            from database import get_local_db
            c = get_local_db()
            for aid, nom, mac in c.execute(
                    "SELECT id, nom_machine, adresse_mac FROM appareils "
                    "WHERE client_id=? AND COALESCE(adresse_mac,'')<>''", (client_id,)):
                inv[_norm_mac(mac)] = (aid, nom)
            c.close()
        except Exception:
            pass

    talkers = []
    for mac, n in par_mac.most_common(15):
        an = inv.get(mac)
        talkers.append({'mac': mac, 'vendor': _vendor(mac),
                        'appareil_id': an[0] if an else None,
                        'appareil_nom': an[1] if an else '',
                        'paquets': n, 'octets': oct_mac[mac]})

    anomalies = []
    seuil_bc = _cfg_int('diag_seuil_broadcast_pps', 150)
    pps_bc = stats['broadcast'] / ecoule
    if pps_bc >= seuil_bc:
        anomalies.append({'niveau': 'alerte',
                          'texte': f"Tempête de broadcast : {pps_bc:.0f} trames/s (seuil {seuil_bc})"})
    for ip, n in arp_grat.items():
        if n >= 8:
            anomalies.append({'niveau': 'avertissement',
                              'texte': f"{n} ARP gratuits pour {ip}"})
    if len(dhcp_srv) >= 2:
        anomalies.append({'niveau': 'alerte',
                          'texte': f"{len(dhcp_srv)} serveurs DHCP vus : {', '.join(sorted(dhcp_srv))}"})

    return {'disponible': True, 'motif': 'ok', 'duree_s': round(ecoule),
            'total': stats['total'], 'broadcast': stats['broadcast'],
            'multicast': stats['multicast'], 'unicast': stats['unicast'],
            'protos': dict(protos), 'talkers': talkers, 'anomalies': anomalies}


def statut_capture_baie() -> dict:
    with _capture_baie_lock:
        return dict(_capture_baie_status)


def lancer_capture_baie(client_id: int, duree: int) -> bool:
    """Démarre une capture en thread détaché. False si une capture tourne déjà."""
    with _capture_baie_lock:
        if _capture_baie_status['running']:
            return False
        _capture_baie_status.update({'running': True, 'progress': 0,
                                     'message': 'Capture en cours…', 'resultat': None,
                                     'client_id': client_id, 'fin': None})
    threading.Thread(target=_run_capture_baie, args=(client_id, max(3, int(duree))),
                     daemon=True, name='DiagCaptureBaie').start()
    return True


def _run_capture_baie(client_id: int, duree: int):
    def _tick():
        for i in range(duree):
            time.sleep(1)
            with _capture_baie_lock:
                if not _capture_baie_status['running']:
                    return
                _capture_baie_status['progress'] = int((i + 1) / duree * 95)
    threading.Thread(target=_tick, daemon=True).start()
    try:
        res = capturer_trafic(duree, client_id)
    except Exception:
        logger.exception('network_diag: capture baie')
        res = {'disponible': False, 'motif': 'erreur'}
    with _capture_baie_lock:
        _capture_baie_status.update({'running': False, 'progress': 100,
                                     'message': 'Terminé', 'resultat': res,
                                     'fin': _now_z()})
