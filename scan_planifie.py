# -*- coding: utf-8 -*-
"""Scan réseau récurrent planifié (Plan 3).

Un scan réseau complet se déclenche seul, à une cadence configurable **par
client**, alimente `client_instantane` (socle du rapport « Changements depuis
la dernière visite ») et lève une alerte si quelque chose de notable a changé —
sans intervention.

Opt-in **strict** :
  - `scan_auto_actif` == '1' (garde-fou global), ET
  - une cadence `scan_auto:<client_id>` non vide pour ce client.

Prudence :
  - fenêtre horaire (`scan_auto_fenetre`, défaut ``02:00-05:00``) — pas de
    charge surprise en journée ;
  - mode terrain : jamais de scan si le site du client n'est pas joignable
    (évite de scanner le LAN perso d'un technicien en télétravail sous Docker) ;
  - un seul scan planifié à la fois (verrou en mémoire côté app.py + coopération
    entre instances via l'historique `client_instantane` : un client vu
    récemment n'est pas re-scanné) ;
  - scan **lecture seule** réseau — le moteur `_run_scan` d'app.py est inchangé.

Ce module ne contient que de la **logique pure** + des **lectures** (config,
`parc_general`, `client_instantane`). L'exécution — qui appelle `_run_scan` et
`_importer_appareils_scan` d'app.py — vit dans app.py
(`_scan_planifie_periodique`, `_executer_scan_planifie`).
"""

import logging
import re
from datetime import datetime, time as _time

logger = logging.getLogger('parcinfo')

# Cadences nommées → secondes. « mensuel » = 30 j (approximation volontaire :
# la précision au jour près n'a aucun intérêt pour un scan de fond).
_CADENCES = {
    'quotidien': 86400,
    'hebdo': 7 * 86400,
    'mensuel': 30 * 86400,
}

_RE_HEURES = re.compile(r'^(\d{1,4})\s*h$')
_RE_FENETRE = re.compile(r'^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$')


# ─── Fonctions pures (testables sans base ni réseau) ──────────────────────────

def intervalle_secondes(cadence):
    """Cadence → secondes, ou ``None`` si vide / invalide.

    Accepte ``'quotidien'`` / ``'hebdo'`` / ``'mensuel'`` ou ``'<n>h'``
    (n heures, 1 à 8760)."""
    c = (cadence or '').strip().lower()
    if not c:
        return None
    if c in _CADENCES:
        return _CADENCES[c]
    m = _RE_HEURES.match(c)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 8760:
            return n * 3600
    return None


def dans_fenetre(fenetre, maintenant):
    """`maintenant` (datetime **local**) tombe-t-il dans la fenêtre
    ``'HH:MM-HH:MM'`` ?

    Fenêtre vide ou non parsable → toujours vrai (aucune restriction).
    Gère le passage de minuit (``'22:00-06:00'``)."""
    f = (fenetre or '').strip()
    if not f:
        return True
    m = _RE_FENETRE.match(f)
    if not m:
        return True
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    try:
        deb, fin = _time(h1, m1), _time(h2, m2)
    except ValueError:
        return True
    ici = maintenant.time()
    if deb == fin:
        return True
    if deb < fin:
        return deb <= ici < fin
    return ici >= deb or ici < fin          # fenêtre à cheval sur minuit


def est_du(dernier_epoch, cadence, maintenant_ts):
    """Le client est-il « dû » pour un scan auto ?

    `dernier_epoch` = epoch du dernier instantané d'origine ``scan_auto``
    (``None`` = jamais scanné automatiquement)."""
    inter = intervalle_secondes(cadence)
    if inter is None:
        return False
    if dernier_epoch is None:
        return True
    try:
        return (float(maintenant_ts) - float(dernier_epoch)) >= inter
    except (TypeError, ValueError):
        return True


def resume_alerte(changements, seuil_disparus=3):
    """Extrait de ``client_helpers.changements_client(...)`` ce qui mérite une
    alerte, ou ``None``.

    Déclencheurs :
      - au moins un appareil **nouveau** (hors MAC aléatoire — smartphone de
        passage) → matériel non déclaré / rogue potentiel ;
      - **disparus** ≥ `seuil_disparus` → panne d'un switch, coupure de site…

    IP / MAC / type / ports / OS changés sont repris dans le corps du message
    mais ne déclenchent **pas** à eux seuls (trop bruyants)."""
    if not changements or not changements.get('disponible'):
        return None
    try:
        from network_diag import _mac_locale
    except Exception:                       # pragma: no cover - réseau absent
        def _mac_locale(_m):
            return False

    nouveaux = list(changements.get('nouveaux') or [])
    aleatoires = [n for n in nouveaux if n.get('mac') and _mac_locale(n['mac'])]
    ids_alea = {id(n) for n in aleatoires}
    nouveaux_reels = [n for n in nouveaux if id(n) not in ids_alea]
    disparus = list(changements.get('disparus') or [])

    declenche = bool(nouveaux_reels) or len(disparus) >= max(1, int(seuil_disparus or 1))
    if not declenche:
        return None

    lignes = []
    if nouveaux_reels:
        lignes.append('%d appareil(s) nouveau(x) : %s' % (
            len(nouveaux_reels),
            ', '.join((n.get('nom') or n.get('ip') or n.get('mac') or '?')
                      for n in nouveaux_reels[:12])))
    if len(disparus) >= max(1, int(seuil_disparus or 1)):
        lignes.append('%d appareil(s) disparu(s) : %s' % (
            len(disparus),
            ', '.join((d.get('nom') or d.get('ip') or d.get('mac') or '?')
                      for d in disparus[:12])))
    for cle, etiq in (('ip_changees', 'IP changée'), ('mac_changees', 'MAC changée'),
                      ('type_changes', 'type changé'), ('os_changes', 'OS changé')):
        n = len(changements.get(cle) or [])
        if n:
            lignes.append('%d %s' % (n, etiq))

    return {
        'declencheurs': (['nouveaux'] if nouveaux_reels else [])
                        + (['disparus'] if len(disparus) >= max(1, int(seuil_disparus or 1)) else []),
        'nb': changements.get('nb', 0),
        'jours_ecoules': changements.get('jours_ecoules'),
        'nouveaux': [{'nom': n.get('nom'), 'ip': n.get('ip'), 'mac': n.get('mac'),
                      'type': n.get('type')} for n in nouveaux_reels],
        'nouveaux_aleatoires': len(aleatoires),
        'disparus': [{'nom': d.get('nom'), 'ip': d.get('ip'), 'mac': d.get('mac'),
                      'type': d.get('type')} for d in disparus],
        'lignes': lignes,
    }


# ─── Lectures (config / parc_general / client_instantane) ─────────────────────

def _cadence_client(cid):
    from config_helpers import cfg_get
    return (cfg_get('scan_auto:%d' % int(cid), '') or '').strip()


def _dernier_scan_auto_epoch(conn, cid):
    r = conn.execute(
        "SELECT MAX(epoch) FROM client_instantane "
        "WHERE client_id=? AND origine='scan_auto'", (int(cid),)).fetchone()
    return r[0] if r and r[0] is not None else None


def _plages_client(conn, cid):
    r = conn.execute("SELECT plage_ip_locale FROM parc_general WHERE client_id=?",
                     (int(cid),)).fetchone()
    brut = ((r[0] if r else '') or '').strip()
    return [p.strip() for p in brut.replace(';', ',').split(',') if p.strip()]


def _iso(epoch):
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(float(epoch)).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _now_epoch():
    """Même horloge que ``client_helpers.capturer_instantane`` (naïf UTC dont
    ``.timestamp()`` est interprété en heure locale) — indispensable pour
    comparer à ``client_instantane.epoch``."""
    from client_helpers import _utcnow
    return _utcnow().timestamp()


# ─── État pour l'API / l'UI ──────────────────────────────────────────────────

def etat_scan_planifie(conn, clients):
    """Synthèse pour ``GET /api/scan/planifie`` et la page « Scan planifié ».

    `clients` = liste de dicts ``get_clients()`` (déjà filtrée par l'ACL) —
    chaque ligne porte son niveau d'accès (`acces`)."""
    from config_helpers import cfg_get
    now_ts = _now_epoch()
    rows = []
    for cl in clients:
        cid = cl['id']
        cadence = _cadence_client(cid)
        inter = intervalle_secondes(cadence)
        dernier = _dernier_scan_auto_epoch(conn, cid)
        if inter and dernier:
            prochain = float(dernier) + inter
        elif inter:
            prochain = now_ts
        else:
            prochain = None
        rows.append({
            'client_id': cid,
            'nom': cl.get('nom') or '',
            'cadence': cadence,
            'planifie': bool(inter),
            'peut_ecrire': (cl.get('acces') in ('proprietaire', 'ecriture', 'admin')),
            'plages': _plages_client(conn, cid),
            'dernier_scan_auto': _iso(dernier),
            'prochain_estime': _iso(prochain),
            'du': bool(inter) and est_du(dernier, cadence, now_ts),
        })
    return {
        'actif': str(cfg_get('scan_auto_actif', '0')) == '1',
        'fenetre': cfg_get('scan_auto_fenetre', '02:00-05:00'),
        'seuil_disparus': int(cfg_get('scan_auto_seuil_disparus', '3') or 3),
        'inclure_candidats': str(cfg_get('scan_auto_inclure_candidats', '1')) == '1',
        'webhook': cfg_get('scan_auto_webhook', '') or '',
        'clients': rows,
    }


def clients_a_scanner(conn, clients, maintenant_local=None):
    """→ ``{'dus': [...], 'reportes': [...]}``.

    N'applique **pas** le garde-fou global (`scan_auto_actif`) ni le verrou —
    c'est le rôle de l'appelant (`_scan_planifie_periodique`). Applique :
    cadence par client, « est dû ? », plages définies, fenêtre horaire, mode
    terrain.

    `maintenant_local` : datetime local (défaut `datetime.now()`) — sert
    uniquement au test de fenêtre horaire ; la fraîcheur du dernier scan est
    comparée avec l'horloge de `client_instantane`."""
    from config_helpers import cfg_get
    maintenant_local = maintenant_local or datetime.now()
    fenetre = cfg_get('scan_auto_fenetre', '02:00-05:00')
    ouvert = dans_fenetre(fenetre, maintenant_local)
    now_ts = _now_epoch()
    try:
        import site_terrain
        sur_site = site_terrain.clients_sur_site(conn)     # set | None
    except Exception:                                     # pragma: no cover
        logger.debug('scan planifié: site_terrain indisponible', exc_info=True)
        sur_site = None

    dus, reportes = [], []
    for cl in clients:
        cid = cl['id']
        nom = cl.get('nom') or ('client %d' % cid)
        cadence = _cadence_client(cid)
        if intervalle_secondes(cadence) is None:
            continue                                       # pas planifié
        if not est_du(_dernier_scan_auto_epoch(conn, cid), cadence, now_ts):
            continue                                       # pas encore l'heure
        plages = _plages_client(conn, cid)
        if not plages:
            reportes.append({'client_id': cid, 'nom': nom,
                             'raison': 'aucune plage IP dans la fiche parc'})
            continue
        if not ouvert:
            reportes.append({'client_id': cid, 'nom': nom,
                             'raison': 'hors fenêtre horaire (%s)' % fenetre})
            continue
        if sur_site is not None and cid not in sur_site:
            reportes.append({'client_id': cid, 'nom': nom,
                             'raison': 'site non joignable (mode terrain)'})
            continue
        dus.append({'client_id': cid, 'nom': nom, 'plages': plages,
                    'cadence': cadence,
                    'libelle': 'Scan planifié (%s)' % cadence})
    return {'dus': dus, 'reportes': reportes}
