"""netdiag.analyse — analyse SNMP par port, **fonctions pures** (Lot 2).

Transforme un relevé (forme `interroger_equipement` : `{sysname, ts, ports[]}`)
+ le relevé précédent en :

- une liste de *findings* (catégories palier 3 : `duplex_mismatch`, `port_crc`,
  `port_erreurs`, `port_sature`, `port_flapping`, `vitesse_reduite`) ;
- une liste de lignes d'**état de port** pour `diag_etat_port` — chacune portant
  une **classification en clair** de l'erreur (`classe_erreur` + libellé), qui
  backe directement l'écran « Trafic & erreurs » de la refonte.

Aucun accès base ni réseau ici : testable sur des relevés figés.
"""
from __future__ import annotations

# Compteurs cumulés dont on calcule le delta (mêmes clés que network_diag).
COMPTEURS = ('in_oct', 'out_oct', 'in_err', 'out_err', 'in_disc', 'out_disc',
             'align_err', 'fcs_err', 'late_coll', 'exc_coll')

# Catégories de findings qui décrivent une erreur de trafic sur un port —
# éligibles à l'auto-résolution (Lot 2, `events.auto_resoudre_snmp`).
CATEGORIES_PORT = ('duplex_mismatch', 'port_crc', 'port_erreurs', 'port_sature',
                   'port_flapping', 'vitesse_reduite')


def _delta(cur: int, prev: int, large: int = 2 ** 32) -> int:
    """Delta d'un compteur SNMP, robuste au bouclage 32 bits (`large=2**32`) ou
    64 bits (`large=0`). Copie de `network_diag._delta_compteur_32` — gardée ici
    pour que ce module reste sans dépendance (source unique au Lot 6)."""
    d = cur - prev
    if d >= 0:
        return d
    if large and prev > large // 2:
        return (large - prev) + cur
    return cur


def deltas_port(port: dict, precedent: dict | None, hc: bool = False) -> dict:
    """`{compteur: delta}` entre `port` et son relevé précédent (`{compteur: val}`
    ou None → tous les deltas à 0). `hc` : in/out octets en Counter64."""
    if not precedent:
        return {k: 0 for k in COMPTEURS}
    out = {}
    for k in COMPTEURS:
        large = 0 if (hc and k in ('in_oct', 'out_oct')) else 2 ** 32
        out[k] = _delta(int(port.get(k, 0)), int(precedent.get(k, port.get(k, 0))), large)
    return out


def classer_erreur(port: dict, d: dict) -> dict:
    """Classe l'activité d'erreur d'un port en langage clair.

    Retourne `{'classe', 'libelle', 'conseil', 'gravite'}` — `classe` vide si le
    port n'a aucune erreur notable. Priorité : duplex > physique > saturation >
    erreurs indéterminées (voir la doc de dépannage : CRC/FCS dominant sans
    collision = couche physique ; collision tardive / half-duplex = duplex
    mismatch ; rejets sans erreurs = mémoire tampon / saturation)."""
    crc = d.get('fcs_err', 0) + d.get('align_err', 0)
    err = d.get('in_err', 0) + d.get('out_err', 0)
    disc = d.get('in_disc', 0) + d.get('out_disc', 0)
    late = d.get('late_coll', 0)
    half = port.get('duplex') == 2
    actif = port.get('oper') == 1
    rapide = port.get('speed_mbps', 0) >= 100

    if actif and rapide and (late > 0 or half):
        return {'classe': 'duplex', 'gravite': 'critique',
                'libelle': 'Duplex mismatch',
                'conseil': "Forcer l'autonégociation des DEUX côtés du lien, ou "
                           "fixer full-duplex partout. Vérifier le câble."}
    if crc > 0 and crc >= max(err, disc):
        return {'classe': 'physique', 'gravite': 'avertissement',
                'libelle': 'Couche physique (CRC/FCS)',
                'conseil': "Remplacer le câble / cordon / SFP, essayer un autre "
                           "port. Une trame corrompue en transit = défaut matériel."}
    if disc > 0 and disc >= 3 * max(err, crc):
        return {'classe': 'saturation', 'gravite': 'avertissement',
                'libelle': 'Rejets (mémoire tampon / saturation)',
                'conseil': "Lien sous-dimensionné ou trafic en rafales : QoS, "
                           "lien plus rapide (10G), ou agrégation LACP."}
    if err > 0 or crc > 0:
        return {'classe': 'erreurs', 'gravite': 'avertissement',
                'libelle': 'Erreurs de trafic (cause indéterminée)',
                'conseil': "Vérifier d'abord le câble, puis la charge du lien et "
                           "l'autonégociation."}
    return {'classe': '', 'gravite': '', 'libelle': '', 'conseil': ''}


def analyser_port(ip: str, sysname: str, port: dict, precedent: dict | None,
                  dt: float, seuils: dict, gigabit_present: bool, hc: bool,
                  nb_changements_oper: int = 0) -> tuple[list[dict], dict]:
    """Un port → `(findings, ligne_etat)`. `findings` = liste de dicts
    `{categorie, titre, gravite, details}` (le rattachement signature/appareil
    est fait par l'appelant via `network_diag._finding`). `ligne_etat` = ligne
    pour `diag_etat_port`."""
    pi = port['index']
    libelle_port = f"{port['nom']}" + (f" ({port['alias']})" if port.get('alias') else '')
    base = {'equipement': ip, 'sysname': sysname, 'port': libelle_port, 'port_index': pi}
    seuil_err = int(seuils.get('erreurs', 50))
    seuil_sat = float(seuils.get('saturation_pct', 90))

    d = deltas_port(port, precedent, hc)
    classe = classer_erreur(port, d)
    findings = []

    debit_pct = 0.0
    if precedent and dt > 0:
        # Duplex mismatch
        if port.get('oper') == 1 and port.get('speed_mbps', 0) >= 100 and (
                d['late_coll'] > 0 or port.get('duplex') == 2):
            findings.append(('duplex_mismatch',
                             f"{libelle_port} sur {ip} : "
                             + ("half-duplex négocié" if port.get('duplex') == 2
                                else f"{d['late_coll']} late collisions"),
                             'critique',
                             {**base, 'duplex': port.get('duplex'),
                              'delta_late_coll': d['late_coll'],
                              'speed_mbps': port.get('speed_mbps'),
                              'classe': classe['classe']}))
        # CRC / alignement
        if d['fcs_err'] + d['align_err'] >= seuil_err:
            findings.append(('port_crc',
                             f"{libelle_port} sur {ip} : {d['fcs_err'] + d['align_err']} "
                             f"erreurs CRC/alignement depuis le dernier relevé",
                             'avertissement',
                             {**base, 'delta_fcs': d['fcs_err'], 'delta_align': d['align_err'],
                              'classe': classe['classe']}))
        # Erreurs / rejets génériques
        err_io = d['in_err'] + d['out_err']
        disc_io = d['in_disc'] + d['out_disc']
        if max(err_io, disc_io) >= seuil_err:
            findings.append(('port_erreurs',
                             f"{libelle_port} sur {ip} : {err_io} erreurs / {disc_io} rejets de paquets",
                             'avertissement',
                             {**base, 'delta_erreurs': err_io, 'delta_rejets': disc_io,
                              'classe': classe['classe']}))
        # Saturation
        if port.get('speed_mbps', 0) > 0:
            debit_mbps = max(d['in_oct'], d['out_oct']) * 8 / dt / 1_000_000
            debit_pct = debit_mbps / port['speed_mbps'] * 100
            if debit_pct >= seuil_sat:
                findings.append(('port_sature',
                                 f"{libelle_port} sur {ip} : lien à {debit_pct:.0f} % "
                                 f"({debit_mbps:.0f} / {port['speed_mbps']} Mb/s)",
                                 'avertissement',
                                 {**base, 'taux_pct': round(debit_pct),
                                  'debit_mbps': round(debit_mbps),
                                  'speed_mbps': port['speed_mbps'], 'classe': 'saturation'}))
        # Flapping
        if nb_changements_oper >= 3:
            findings.append(('port_flapping',
                             f"{libelle_port} sur {ip} : {nb_changements_oper} changements d'état récents",
                             'avertissement',
                             {**base, 'nb_changements': nb_changements_oper, 'classe': 'instable'}))

    # Vitesse réduite (indépendant de l'historique)
    if port.get('oper') == 1 and gigabit_present and 0 < port.get('speed_mbps', 0) < 1000:
        findings.append(('vitesse_reduite',
                         f"{libelle_port} sur {ip} : négocié à {port['speed_mbps']} Mb/s "
                         f"sur un équipement gigabit",
                         'info',
                         {**base, 'speed_mbps': port['speed_mbps'], 'classe': 'vitesse'}))

    minute = 60.0 / dt if dt > 0 else 0.0
    ligne_etat = {
        'port_index': pi, 'port_nom': port.get('nom', ''), 'port_alias': port.get('alias', ''),
        'oper': port.get('oper', 0), 'admin': port.get('admin', 0),
        'speed_mbps': port.get('speed_mbps', 0), 'duplex': port.get('duplex', 0),
        'err_min': round((d['in_err'] + d['out_err']) * minute, 2),
        'disc_min': round((d['in_disc'] + d['out_disc']) * minute, 2),
        'crc_min': round((d['fcs_err'] + d['align_err']) * minute, 2),
        'debit_pct': round(debit_pct, 1),
        'classe_erreur': classe['classe'], 'classe_libelle': classe['libelle'],
        'gravite': classe['gravite'],
    }
    return findings, ligne_etat


def analyser_equipement(ip: str, sysname: str, ports: list, precedent_par_port: dict,
                        dt_par_port: dict, seuils: dict, hc: bool,
                        changements_oper: dict | None = None) -> tuple[list, list]:
    """Un équipement → `(findings, lignes_etat)`. `precedent_par_port` :
    `{port_index: {compteur: val, 'epoch': ...}}`. `dt_par_port` :
    `{port_index: secondes}`. `changements_oper` : `{port_index: n}`."""
    changements_oper = changements_oper or {}
    gigabit_present = any(p.get('speed_mbps', 0) >= 1000 for p in ports)
    findings, lignes = [], []
    for p in ports:
        pi = p['index']
        prec = precedent_par_port.get(pi)
        dt = dt_par_port.get(pi, 0.0)
        fs, ligne = analyser_port(ip, sysname, p, prec, dt, seuils, gigabit_present, hc,
                                  changements_oper.get(pi, 0))
        findings += fs
        lignes.append(ligne)
    return findings, lignes
