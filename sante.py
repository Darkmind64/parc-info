"""sante.py — score de santé synthétique par appareil.

Un **feu tricolore** (`ok` / `attention` / `critique`) + une **liste de raisons
actionnables**, agrégés depuis des signaux **déjà présents en base** : aucune
requête réseau, aucun appel SNMP, aucun accès disque.

Trois familles de signaux :

  1. **Collecteur système** — on réutilise tel quel `collector_core.build_alerts`
     (disque saturé, antivirus absent, TPM/Secure Boot, pare-feu, BitLocker,
     batterie, fin de support Windows, certificats, arrêts inattendus…). Le
     jugement est donc identique à celui de la fiche système et du rapport PDF.
  2. **Cycle de vie côté inventaire** — ce que le collecteur ne voit pas :
     garantie expirée sans contrat de maintenance, matériel ancien, abonnement
     AV/EDR expiré, collecte jamais faite ou trop ancienne, appareil injoignable.
  3. **Réseau** — `diag_etat_equipement` / `diag_etat_port` du diagnostic :
     équipement SNMP muet, port en erreur de trafic là où l'appareil est vu.

`sante_appareil(appareil, ctx)` est **pure** (dict → dict).
`charger_contexte_sante(conn, client_id)` pré-charge `ctx` en quelques requêtes
groupées pour éviter le N+1 sur une liste de plusieurs centaines d'appareils.

Ce n'est pas une note sur 100 : `score` ne sert qu'au **tri** « à traiter en
priorité », `niveau` à la **pastille**.
"""
import json
import logging
import re
from datetime import date, datetime

logger = logging.getLogger('parcinfo')

_G_CRITIQUE, _G_ATTENTION, _G_INFO = 'critique', 'attention', 'info'
_POIDS = {_G_CRITIQUE: 30, _G_ATTENTION: 12, _G_INFO: 0}
_NIVEAU_DEPUIS_LEVEL = {'danger': _G_CRITIQUE, 'warn': _G_ATTENTION, 'info': _G_INFO}

# Âge (années) au-delà duquel on signale le matériel, par famille de type.
_AGE_MAX_ANS = {'serveur': 8, 'nas': 8, 'switch': 10, 'routeur': 10,
                'pare-feu': 10, 'onduleur': 8, '_defaut': 6}
# Familles pour lesquelles « injoignable » est un vrai problème (un poste ou un
# portable éteint, non).
_FAMILLES_INFRA = ('serveur', 'nas', 'switch', 'routeur', 'pare-feu', 'onduleur',
                   'borne', 'imprimante', 'camera')
# Types censés remonter une collecte système (sinon « jamais collecté »).
_TYPES_AVEC_COLLECTE = ('poste', 'pc', 'ordinateur', 'portable', 'laptop',
                        'serveur', 'workstation', 'station', 'mac', 'fixe')

_DEFAUTS = {'collecte_jours': 45, 'injoignable_jours': 3}


# ─── helpers purs ─────────────────────────────────────────────────────────────

def _parse_json(raw, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return fallback


def _jours_depuis(iso):
    """Nombre de jours écoulés depuis une date/datetime ISO, ou `None`."""
    if not iso:
        return None
    s = str(iso).strip()[:19]
    for conv in (lambda x: date.fromisoformat(x[:10]),
                 lambda x: datetime.fromisoformat(x).date()):
        try:
            return (date.today() - conv(s)).days
        except (ValueError, TypeError):
            continue
    return None


def _slug(s):
    return re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-')[:48] or 'x'


def _int(v, d):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return d


def _famille_type(type_appareil):
    t = (type_appareil or '').lower()
    for cle in _AGE_MAX_ANS:
        if cle != '_defaut' and cle in t:
            return cle
    return '_defaut'


# ─── alertes du collecteur (import paresseux : collector_core est volumineux) ──

_bt = {'fn': None}


def _alertes_collecteur(rapport):
    if not rapport:
        return []
    if _bt['fn'] is None:
        try:
            from collector_core import build_alerts
            _bt['fn'] = build_alerts
        except Exception:
            logger.debug('sante: collector_core.build_alerts indisponible', exc_info=True)
            _bt['fn'] = False
    if not _bt['fn']:
        return []
    try:
        return _bt['fn'](rapport) or []
    except Exception:
        logger.debug('sante: build_alerts a levé', exc_info=True)
        return []


def _age_materiel_ans(appareil, rapport):
    bios = (rapport or {}).get('bios_release_date')
    if bios:
        try:
            from collector_core import hardware_age_years
            a = hardware_age_years(bios)
            if a is not None:
                return a
        except Exception:
            pass
    j = _jours_depuis(appareil.get('date_achat') or '')
    return (j / 365.25) if (j is not None and j > 0) else None


# ─── contexte (requêtes groupées, 1 fois par client) ──────────────────────────

def _reglages():
    try:
        from config_helpers import cfg_get
        r = {k: _int(cfg_get('sante_%s' % k, str(v)), v) for k, v in _DEFAUTS.items()}
        r['desactivees'] = {c.strip() for c in
                            str(cfg_get('sante_regles_desactivees', '') or '').split(',')
                            if c.strip()}
        return r
    except Exception:
        return {**_DEFAUTS, 'desactivees': set()}


def charger_contexte_sante(conn, client_id):
    """Tout ce dont `sante_appareil` a besoin en plus de la ligne `appareils`."""
    ctx = {'reglages': _reglages(),
           'appareils_sous_contrat': set(), 'equip_muet_ip': set(),
           'equip_par_appareil': {}, 'ports_erreur_par_appareil': {},
           'series_doublon': set()}
    auj = date.today().isoformat()

    try:
        for (aid,) in conn.execute(
                "SELECT DISTINCT ca.appareil_id FROM contrats_appareils ca "
                "JOIN contrats c ON c.id = ca.contrat_id "
                "WHERE c.client_id=? AND COALESCE(c.statut,'actif')<>'resilie' "
                "AND (COALESCE(c.date_fin,'')='' OR c.date_fin >= ?)",
                (client_id, auj)):
            ctx['appareils_sous_contrat'].add(aid)
    except Exception:
        logger.debug('sante: contrats', exc_info=True)

    try:
        for ip, aid, snmp_ok, nb_err, motif in conn.execute(
                "SELECT equipement_ip, appareil_id, snmp_ok, nb_ports_erreur, motif "
                "FROM diag_etat_equipement WHERE client_id=?", (client_id,)):
            if not snmp_ok:
                ctx['equip_muet_ip'].add(ip)
            if aid:
                ctx['equip_par_appareil'][aid] = {
                    'snmp_ok': bool(snmp_ok), 'nb_ports_erreur': nb_err or 0,
                    'motif': motif or ''}
    except Exception:
        logger.debug('sante: diag_etat_equipement', exc_info=True)

    try:
        for aid, classe, libelle, gravite in conn.execute(
                "SELECT appareil_vu_id, classe_erreur, classe_libelle, gravite "
                "FROM diag_etat_port WHERE client_id=? AND appareil_vu_id IS NOT NULL "
                "AND COALESCE(classe_erreur,'')<>''", (client_id,)):
            ctx['ports_erreur_par_appareil'].setdefault(aid, []).append(
                {'classe': classe, 'libelle': libelle or classe,
                 'gravite': gravite or _G_ATTENTION})
    except Exception:
        logger.debug('sante: diag_etat_port', exc_info=True)

    try:
        for (ns,) in conn.execute(
                "SELECT numero_serie FROM appareils WHERE client_id=? "
                "AND COALESCE(numero_serie,'')<>'' "
                "GROUP BY numero_serie HAVING COUNT(*) > 1", (client_id,)):
            ctx['series_doublon'].add(ns)
    except Exception:
        pass

    return ctx


# ─── le calcul (pur) ─────────────────────────────────────────────────────────

def sante_appareil(appareil, ctx=None):
    """Feu tricolore + raisons pour un appareil. **Pure.**

    `appareil` : dict d'une ligne `appareils`.
    `ctx` : sortie de `charger_contexte_sante` (ou `None` → seuls les signaux
    internes à la ligne sont évalués).

    Retour : `{'niveau': 'ok'|'attention'|'critique', 'score': int,
    'raisons': [{'code', 'gravite', 'texte', 'lien'}]}`.
    """
    ctx = ctx or {}
    reg = ctx.get('reglages') or _reglages()
    off = reg.get('desactivees') or set()
    aid = appareil.get('id')
    type_ap = appareil.get('type_appareil') or ''
    actif = (appareil.get('statut') or 'actif') == 'actif'
    rapport = _parse_json(appareil.get('rapport_systeme_json'), {})
    raisons = []

    def ajoute(code, gravite, texte, lien=''):
        if code in off or code.split(':', 1)[0] in off:
            return
        raisons.append({'code': code, 'gravite': gravite, 'texte': texte, 'lien': lien})

    # 1. collecteur système
    for al in _alertes_collecteur(rapport):
        ajoute('collecteur:%s' % _slug(al.get('titre', '')),
               _NIVEAU_DEPUIS_LEVEL.get(al.get('level'), _G_INFO),
               al.get('titre', ''), lien='fiche-systeme')

    if actif:
        # 2. cycle de vie
        from client_helpers import garantie_active, _compute_sec_status
        dfg = appareil.get('date_fin_garantie') or ''
        sous_contrat = aid in (ctx.get('appareils_sous_contrat') or set())
        if dfg and not garantie_active(dfg) and not sous_contrat:
            ajoute('garantie_expiree_sans_contrat', _G_ATTENTION,
                   'Garantie expirée et aucun contrat de maintenance', lien='garantie')

        ans = _age_materiel_ans(appareil, rapport)
        seuil = _AGE_MAX_ANS.get(_famille_type(type_ap), _AGE_MAX_ANS['_defaut'])
        if ans is not None and ans >= seuil:
            ajoute('materiel_ancien', _G_ATTENTION,
                   'Matériel de %d ans (seuil %d ans)' % (round(ans), seuil),
                   lien='garantie')

        for pref, label in (('av', 'antivirus'), ('edr', 'EDR')):
            st = appareil.get('%s_status' % pref) or _compute_sec_status(
                appareil.get('%s_nom' % pref) or appareil.get('%s_marque' % pref) or '',
                appareil.get('%s_date_fin' % pref) or '')
            if st == 'expired':
                ajoute('%s_expire' % pref, _G_ATTENTION, 'Abonnement %s expiré' % label)

        a_un_rapport = bool((appareil.get('rapport_systeme_json') or '').strip())
        attendu = any(k in type_ap.lower() for k in _TYPES_AVEC_COLLECTE)
        j_collecte = _jours_depuis(appareil.get('derniere_synchro')
                                   or appareil.get('date_maj') or '')
        j_creation = _jours_depuis(appareil.get('date_creation') or '')
        if (attendu and not a_un_rapport
                and (j_creation is None or j_creation > reg['collecte_jours'])):
            # `info`, pas `attention` : « on pourrait avoir plus de données »,
            # pas « quelque chose ne va pas ». Grâce pour un appareil récent.
            ajoute('jamais_collecte', _G_INFO,
                   'Jamais remonté par le collecteur système', lien='fiche-systeme')
        elif a_un_rapport and j_collecte is not None and j_collecte > reg['collecte_jours']:
            ajoute('collecte_ancienne', _G_INFO,
                   'Dernière collecte il y a %d jours' % j_collecte, lien='fiche-systeme')

        if (appareil.get('en_ligne') in (0, None) and appareil.get('dernier_ping')
                and any(f in type_ap.lower() for f in _FAMILLES_INFRA)):
            j = _jours_depuis(appareil.get('dernier_ping'))
            if j is not None and j >= reg['injoignable_jours']:
                ajoute('injoignable', _G_ATTENTION, 'Injoignable depuis %d jours' % j)

        # 3. réseau
        e = (ctx.get('equip_par_appareil') or {}).get(aid)
        ip = (appareil.get('adresse_ip') or '').strip()
        if e and not e['snmp_ok']:
            ajoute('reseau:equipement_muet', _G_ATTENTION,
                   'Ne répond plus en SNMP (%s)' % (e['motif'] or 'aucune réponse'),
                   lien='diag-reseau')
        elif not e and ip and ip in (ctx.get('equip_muet_ip') or set()):
            ajoute('reseau:equipement_muet', _G_ATTENTION,
                   'Ne répond plus en SNMP', lien='diag-reseau')
        for p in (ctx.get('ports_erreur_par_appareil') or {}).get(aid, []):
            g = _G_CRITIQUE if str(p['gravite']).startswith('crit') else _G_ATTENTION
            ajoute('reseau:port_erreur:%s' % p['classe'], g,
                   'Port de switch en erreur : %s' % p['libelle'], lien='diag-reseau')

    # 4. divers
    ns = (appareil.get('numero_serie') or '').strip()
    if ns and ns in (ctx.get('series_doublon') or set()):
        ajoute('doublon_serie', _G_ATTENTION,
               'Numéro de série « %s » partagé avec un autre appareil' % ns)

    grav = {r['gravite'] for r in raisons}
    niveau = (_G_CRITIQUE if _G_CRITIQUE in grav
              else _G_ATTENTION if _G_ATTENTION in grav else 'ok')
    score = min(100, sum(_POIDS.get(r['gravite'], 0) for r in raisons))
    return {'niveau': niveau, 'score': score, 'raisons': raisons}


_COLS_RECALC = ('id', 'nom_machine', 'type_appareil', 'statut', 'date_fin_garantie',
                'date_achat', 'date_creation', 'derniere_synchro', 'date_maj',
                'en_ligne', 'dernier_ping', 'numero_serie', 'adresse_ip',
                'av_nom', 'av_marque', 'av_date_fin', 'edr_nom', 'edr_marque',
                'edr_date_fin', 'rapport_systeme_json')


def recalculer(conn, client_id, appareil_ids=None):
    """(Re)calcule et met en cache `sante_niveau` / `sante_score` /
    `sante_raisons` / `sante_maj` pour les appareils d'un client.

    N'ÉCRIT que les lignes dont le niveau ou le score a changé — un balayage
    périodique ne génère donc quasiment aucune écriture (ni bruit de sync).
    Retourne le nombre de lignes réellement mises à jour.
    """
    ctx = charger_contexte_sante(conn, client_id)
    q = "SELECT %s FROM appareils WHERE client_id=?" % ', '.join(_COLS_RECALC)
    p = [client_id]
    if appareil_ids:
        ids = [int(i) for i in appareil_ids]
        q += " AND id IN (%s)" % ','.join('?' * len(ids))
        p += ids
    maj = _now_iso()
    ecrits = 0
    for row in conn.execute(q, p).fetchall():
        ap = dict(zip(_COLS_RECALC, row))
        s = sante_appareil(ap, ctx)
        anc = conn.execute(
            "SELECT COALESCE(sante_niveau,''), COALESCE(sante_score,0) "
            "FROM appareils WHERE id=?", (ap['id'],)).fetchone()
        if anc and anc[0] == s['niveau'] and anc[1] == s['score']:
            continue          # inchangé : aucune écriture (pas de bruit de sync)
        conn.execute(
            "UPDATE appareils SET sante_niveau=?, sante_score=?, sante_raisons=?, sante_maj=? "
            "WHERE id=?",
            (s['niveau'], s['score'], json.dumps(s['raisons'], ensure_ascii=False),
             maj, ap['id']))
        ecrits += 1
    # horodatage du balayage : par client, dans `config` — ne touche pas les
    # lignes `appareils` stables.
    if not appareil_ids:
        try:
            from config_helpers import cfg_set
            cfg_set('_sante_balayage:%s' % client_id, maj)
        except Exception:
            pass
    conn.commit()
    return ecrits


def _now_iso():
    try:
        from client_helpers import _utcnow
        return _utcnow().isoformat()
    except Exception:
        return datetime.utcnow().isoformat()


def resume_client(appareils_sante):
    """Compteurs + top des appareils à traiter, à partir d'une liste de
    `(appareil, sante)` — pour la tuile « Santé du parc » du tableau de bord."""
    compte = {'ok': 0, 'attention': 0, 'critique': 0}
    classe = []
    for ap, s in appareils_sante:
        compte[s['niveau']] = compte.get(s['niveau'], 0) + 1
        if s['niveau'] != 'ok':
            classe.append({'id': ap.get('id'), 'nom': ap.get('nom_machine') or '',
                           'niveau': s['niveau'], 'score': s['score'],
                           'raison': s['raisons'][0]['texte'] if s['raisons'] else ''})
    classe.sort(key=lambda x: (-{'critique': 2, 'attention': 1}.get(x['niveau'], 0),
                               -x['score'], x['nom']))
    return {'compte': compte, 'total': sum(compte.values()), 'a_traiter': classe}
