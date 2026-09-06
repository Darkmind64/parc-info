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

import logging
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
        rows = [dict(zip(cols, r)) for r in conn.execute(
            "SELECT * FROM diag_etat_port WHERE client_id=?", (client_id,))]
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


def verdict(client_id: int) -> dict:
    """Synthèse pour le bandeau : hôte + SNMP + trafic + évènements."""
    from database import get_db
    conn = get_db()
    try:
        eq = conn.execute(
            "SELECT COUNT(*), SUM(snmp_ok), SUM(nb_ports_erreur), MAX(derniere_maj) "
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

    if nb_crit:
        niveau, phrase = 'critique', f"{nb_crit} alerte(s) critique(s) active(s)"
    elif nb_avert or nb_ports_err:
        niveau = 'attention'
        bits = []
        if nb_ports_err:
            bits.append(f"{nb_ports_err} port(s) en erreur")
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
