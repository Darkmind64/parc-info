"""netdiag.etat — read models pour l'interface du diagnostic réseau (Lot 4).

Lit les tables d'**état courant** (`diag_etat_equipement` / `diag_etat_port`,
Lot 2) : la page n'a plus à recalculer quoi que ce soit depuis les relevés
bruts. Deux vues :

- ``verdict(client_id)`` — la synthèse « ton réseau va bien / voici les N
  problèmes » affichée en bandeau ;
- ``trafic(client_id)`` — l'écran **Trafic & erreurs** : chaque port en erreur,
  trié pire d'abord, avec sa classification en clair et un conseil.
"""
from __future__ import annotations

import json as _json
import logging
import re as _re
import time
from datetime import datetime, timezone

logger = logging.getLogger('parcinfo')

_CATEGORIES_PORT = ('duplex_mismatch', 'port_crc', 'port_erreurs', 'port_sature',
                    'port_flapping', 'vitesse_reduite')

# Conseil par classe d'erreur (le libellé est déjà stocké dans diag_etat_port ;
# le conseil, lui, est reconstruit ici pour ne pas alourdir la table).
_CONSEIL = {
    'duplex': "Forcer l'autonégociation des DEUX côtés du lien (ou fixer "
              "full-duplex partout). Vérifier le câble.",
    'physique': "Remplacer le câble / cordon / SFP, essayer un autre port. "
                "Une trame corrompue en transit = défaut matériel.",
    'saturation': "Lien sous-dimensionné ou trafic en rafales : QoS, lien plus "
                  "rapide (10G), ou agrégation LACP.",
    'erreurs': "Vérifier d'abord le câble, puis la charge du lien et "
               "l'autonégociation.",
    'instable': "Lien instable : fiabiliser l'alimentation, remplacer câble / SFP.",
    'vitesse': "Câble Cat5e+ 4 paires en bon état, vérifier l'autonégociation "
               "des deux côtés.",
}
_ORDRE_GRAVITE = {'critique': 0, 'avertissement': 1, 'info': 2, '': 3}


def _age_s(iso: str) -> int | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return max(0, int((datetime.now(timezone.utc) - t).total_seconds()))
    except (ValueError, TypeError):
        return None


def _speed_mbps(txt) -> int | None:
    """« 1 Gbps » / « 100 Mbps » / « 2.5 Gbps » → Mbit/s. None si illisible."""
    m = _re.match(r'\s*([\d.,]+)\s*([GMK]?)\s*b', str(txt or ''), _re.I)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(',', '.'))
    except ValueError:
        return None
    return int(round(v * {'G': 1000, 'M': 1, 'K': 0.001}.get(m.group(2).upper(), 1)))


def _incoherences_reseau(rapport_json: str, vu: dict | None) -> list[str]:
    """Proposition #3 : recoupe ce que le switch voit du poste (port SNMP :
    débit négocié, duplex) avec ce que le collecteur-agent ParcInfo remonte de
    ses cartes réseau. Signale les écarts (câble/port qui bride un Gigabit,
    half-duplex…)."""
    if not vu:
        return []
    try:
        rap = _json.loads(rapport_json or '{}')
    except (ValueError, TypeError):
        return []
    nics = [a for a in (rap.get('network_adapter_details') or [])
            if isinstance(a, dict) and a.get('physical') and a.get('connected')]
    out = []
    sp = vu.get('speed_mbps') or 0
    cap = max((_speed_mbps(a.get('link_speed')) or 0 for a in nics), default=0)
    if cap and sp and sp < cap and sp in (10, 100) and cap >= 1000:
        out.append(f"Le switch voit ce poste négocié à {sp} Mb/s, mais sa carte réseau "
                   f"est donnée pour {cap} Mb/s par le collecteur — câble (Cat5e+ 4 paires) "
                   f"ou port à vérifier.")
    if vu.get('duplex') == 2:
        out.append("Le port de switch est en half-duplex — forcer l'autonégociation "
                   "(ou full-duplex) des deux côtés du lien.")
    return out


def _cfg1(cle, defaut):
    try:
        import network_diag
        return str(network_diag._cfg(cle, defaut)) == '1'
    except Exception:
        return defaut == '1'


def trafic(client_id: int, tous: bool = False) -> dict:
    """Écran « Trafic & erreurs ». `tous=True` inclut aussi les ports actifs
    sans erreur (pour le « voir tous les ports »)."""
    from database import get_db
    conn = get_db()
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(diag_etat_port)")]
        # Un port dont le switch ne répond plus (snmp_ok=0) porte des données
        # périmées — on ne l'affiche pas dans « Trafic & erreurs » (le bandeau
        # verdict compte les équipements muets à part).
        rows = [dict(zip(cols, r)) for r in conn.execute(
            "SELECT p.* FROM diag_etat_port p JOIN diag_etat_equipement e "
            "  ON e.client_id=p.client_id AND e.equipement_ip=p.equipement_ip "
            "WHERE p.client_id=? AND e.snmp_ok=1", (client_id,))]
        # noms d'appareils (équipement porteur + appareil branché vu)
        aids = {r['appareil_id'] for r in rows if r['appareil_id']} | \
               {r['appareil_vu_id'] for r in rows if r['appareil_vu_id']}
        noms = {}
        if aids:
            qs = ','.join('?' * len(aids))
            noms = {a: n for a, n in conn.execute(
                f"SELECT id, nom_machine FROM appareils WHERE id IN ({qs})", tuple(aids))}
        # métriques pour la sparkline (taux d'erreur agrégé par port)
        spark = {}
        for cible, val in conn.execute(
                "SELECT cible, valeur FROM diag_metriques WHERE client_id=? "
                "AND categorie='port_erreurs' ORDER BY epoch", (client_id,)):
            spark.setdefault(cible, []).append(val)
    finally:
        conn.close()

    en_erreur, sains = [], []
    for r in rows:
        cible = f"{r['equipement_ip']}:{r['port_index']}"
        item = {
            'equipement_ip': r['equipement_ip'], 'appareil_id': r['appareil_id'],
            'equipement_nom': noms.get(r['appareil_id']) or r['equipement_ip'],
            'port_index': r['port_index'], 'port_nom': r['port_nom'],
            'port_alias': r['port_alias'],
            'appareil_vu_id': r['appareil_vu_id'],
            'appareil_vu_nom': noms.get(r['appareil_vu_id']) or '',
            'baie_slot_id': r['baie_slot_id'], 'baie_port': r['baie_port'],
            'oper': r['oper'], 'speed_mbps': r['speed_mbps'], 'duplex': r['duplex'],
            'err_min': r['err_min'], 'disc_min': r['disc_min'], 'crc_min': r['crc_min'],
            'debit_pct': r['debit_pct'],
            'classe': r['classe_erreur'], 'classe_libelle': r['classe_libelle'],
            'conseil': _CONSEIL.get(r['classe_erreur'], ''),
            'gravite': r['gravite'], 'depuis': r['depuis'],
            'depuis_age_s': _age_s(r['depuis']),
            'spark': (spark.get(cible) or [])[-40:],
        }
        if r['classe_erreur']:
            en_erreur.append(item)
        elif r['oper'] == 1:
            sains.append(item)

    en_erreur.sort(key=lambda x: (_ORDRE_GRAVITE.get(x['gravite'], 3),
                                  -(x['err_min'] + x['crc_min'] + x['disc_min'])))
    nb_crit = sum(1 for x in en_erreur if x['gravite'] == 'critique')
    return {
        'actif': _cfg1('diag_snmp_actif', '0'),
        'ports_en_erreur': en_erreur,
        'ports_sains': sains if tous else [],
        'nb_actifs': len(en_erreur) + len(sains),
        'nb_erreur': len(en_erreur), 'nb_critique': nb_crit,
        'verdict': ('critique' if nb_crit else 'attention' if en_erreur else 'ok'),
    }


def pour_appareil(client_id: int, appareil_id: int) -> dict:
    """Encart « Réseau (diagnostic) » de la fiche d'un appareil (refonte Lot 5).

    - si c'est un switch / routeur relevé en SNMP : son état + ses ports en erreur ;
    - sinon : sur quel port de quel switch il est vu, et l'état de ce port ;
    - toujours : les évènements de diagnostic réseau actifs rattachés à lui.
    """
    from database import get_db
    conn = get_db()
    try:
        ap = conn.execute("SELECT nom_machine, adresse_ip, type_appareil, "
                          "COALESCE(rapport_systeme_json,'') FROM appareils "
                          "WHERE id=? AND client_id=?", (appareil_id, client_id)).fetchone()
        if not ap:
            return {}
        nom, ip, type_ap, rapport_json = ap
        cols_p = [c[1] for c in conn.execute("PRAGMA table_info(diag_etat_port)")]
        # équipement lui-même ?
        eq = None
        if ip:
            r = conn.execute(
                "SELECT sysname, snmp_ok, motif, nb_ports, nb_ports_up, nb_ports_erreur, "
                "derniere_maj FROM diag_etat_equipement WHERE client_id=? AND equipement_ip=?",
                (client_id, ip)).fetchone()
            if r:
                eq = {'sysname': r[0], 'snmp_ok': bool(r[1]), 'motif': r[2],
                      'nb_ports': r[3], 'nb_ports_up': r[4], 'nb_ports_erreur': r[5],
                      'age_s': _age_s(r[6])}
        ports_eq = []
        if eq and ip:
            for pr in conn.execute(
                    "SELECT * FROM diag_etat_port WHERE client_id=? AND equipement_ip=? "
                    "AND classe_erreur!=''", (client_id, ip)):
                d = dict(zip(cols_p, pr))
                ports_eq.append({'port_nom': d['port_nom'], 'classe': d['classe_erreur'],
                                 'classe_libelle': d['classe_libelle'], 'gravite': d['gravite'],
                                 'conseil': _CONSEIL.get(d['classe_erreur'], '')})
        # cet appareil vu sur un port de switch ?
        vu = None
        r = conn.execute(
            "SELECT * FROM diag_etat_port WHERE client_id=? AND appareil_vu_id=? LIMIT 1",
            (client_id, appareil_id)).fetchone()
        if r:
            d = dict(zip(cols_p, r))
            eq_nom = conn.execute(
                "SELECT nom_machine FROM appareils WHERE client_id=? AND adresse_ip=? LIMIT 1",
                (client_id, d['equipement_ip'])).fetchone()
            vu = {'equipement_ip': d['equipement_ip'],
                  'equipement_nom': (eq_nom[0] if eq_nom else d['equipement_ip']),
                  'port_nom': d['port_nom'], 'speed_mbps': d['speed_mbps'],
                  'duplex': d['duplex'], 'baie_slot_id': d['baie_slot_id'],
                  'classe': d['classe_erreur'], 'classe_libelle': d['classe_libelle'],
                  'conseil': _CONSEIL.get(d['classe_erreur'], '')}
        evts = [{'categorie': c, 'gravite': g, 'titre': t,
                 'derniere_occurrence': o}
                for c, g, t, o in conn.execute(
                    "SELECT categorie, gravite, titre, derniere_occurrence "
                    "FROM diag_reseau_evenements WHERE client_id=? AND appareil_id=? "
                    "AND resolu=0 ORDER BY derniere_occurrence DESC LIMIT 12",
                    (client_id, appareil_id))]
    finally:
        conn.close()
    incoherences = _incoherences_reseau(rapport_json, vu)
    return {'nom': nom, 'ip': ip or '', 'type': type_ap or '',
            'equipement': eq, 'ports_en_erreur': ports_eq,
            'vu_sur': vu, 'evenements': evts, 'incoherences': incoherences,
            'a_montrer': bool(eq or vu or evts or incoherences)}


def sante_baie(client_id: int) -> dict:
    """Proposition #1 : pastille de santé par emplacement de baie, tirée de
    `diag_etat_equipement`. `{slot_id: {niveau, snmp_ok, nb_ports_erreur,
    nb_ports, age_s, sysname, phrase}}` — un seul niveau `ok` / `attention` /
    `critique` par équipement monté en rack et relevé en SNMP. Les emplacements
    sans relevé SNMP ne sont pas renvoyés (pas de badge « non vérifié » qui
    encombrerait le rack)."""
    from database import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT s.id, a.nom_machine, e.snmp_ok, e.nb_ports_erreur, e.nb_ports, "
            "       e.motif, e.derniere_maj, e.sysname "
            "FROM baie_slots s JOIN appareils a ON a.id = s.appareil_id "
            "JOIN diag_etat_equipement e "
            "  ON e.client_id = s.client_id AND e.equipement_ip = a.adresse_ip "
            "WHERE s.client_id=? AND COALESCE(a.adresse_ip,'') <> ''",
            (client_id,)).fetchall()
    finally:
        conn.close()
    out = {}
    for sid, nom, snmp_ok, nb_err, nb_ports, motif, maj, sysname in rows:
        if not snmp_ok:
            niveau = 'critique'
            phrase = f"Ne répond plus en SNMP ({motif or 'aucune réponse'})"
        elif nb_err:
            niveau = 'attention'
            phrase = f"{nb_err} port(s) en erreur de trafic"
        else:
            niveau = 'ok'
            phrase = f"{nb_ports or 0} port(s) relevé(s), aucune erreur"
        out[str(sid)] = {
            'niveau': niveau, 'phrase': phrase, 'snmp_ok': bool(snmp_ok),
            'nb_ports_erreur': nb_err or 0, 'nb_ports': nb_ports or 0,
            'sysname': sysname or nom or '', 'age_s': _age_s(maj),
        }
    return out


def verdict(client_id: int) -> dict:
    """Synthèse pour le bandeau : hôte + SNMP + trafic + évènements."""
    from database import get_db
    conn = get_db()
    try:
        eq = conn.execute(
            "SELECT COUNT(*), SUM(snmp_ok), "
            "  SUM(CASE WHEN snmp_ok=1 THEN nb_ports_erreur ELSE 0 END), MAX(derniere_maj) "
            "FROM diag_etat_equipement WHERE client_id=?", (client_id,)).fetchone()
        nb_eq = eq[0] or 0
        nb_eq_ok = eq[1] or 0
        nb_ports_err = eq[2] or 0
        derniere_maj = eq[3] or ''
        ev = conn.execute(
            "SELECT gravite, COUNT(*) FROM diag_reseau_evenements "
            "WHERE client_id=? AND resolu=0 GROUP BY gravite", (client_id,)).fetchall()
        par_grav = {g: n for g, n in ev}
        run = conn.execute(
            "SELECT fin, resume_json FROM diag_reseau_runs WHERE client_id=? "
            "ORDER BY id DESC LIMIT 1", (client_id,)).fetchone()
    finally:
        conn.close()

    nb_crit = par_grav.get('critique', 0)
    nb_avert = par_grav.get('avertissement', 0)
    snmp_actif = _cfg1('diag_snmp_actif', '0')

    nb_muets = max(0, nb_eq - nb_eq_ok)
    if nb_crit:
        niveau, phrase = 'critique', f"{nb_crit} alerte(s) critique(s) active(s)"
    elif nb_avert or nb_ports_err or nb_muets:
        niveau = 'attention'
        bits = []
        if nb_ports_err:
            bits.append(f"{nb_ports_err} port(s) en erreur")
        if nb_muets:
            bits.append(f"{nb_muets} équipement(s) SNMP muet(s)")
        if nb_avert:
            bits.append(f"{nb_avert} avertissement(s)")
        phrase = ' · '.join(bits)
    else:
        niveau = 'ok'
        phrase = "Aucun problème détecté sur ce segment"

    return {
        'niveau': niveau, 'phrase': phrase,
        'snmp_actif': snmp_actif,
        'nb_equipements': nb_eq, 'nb_equipements_ok': nb_eq_ok,
        'nb_equipements_muets': max(0, nb_eq - nb_eq_ok),
        'nb_ports_erreur': nb_ports_err,
        'nb_evenements_critiques': nb_crit, 'nb_evenements_avertissements': nb_avert,
        'derniere_maj': derniere_maj, 'age_s': _age_s(derniere_maj),
        'dernier_run_fin': run[0] if run else '',
    }
