"""
client_helpers.py — Accès clients, pagination, audit, formatage.
"""
import json
import logging
import re
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from flask import session

logger = logging.getLogger('parcinfo')


def _utcnow() -> datetime:
    """Équivalent de _utcnow() (dépréciée depuis 3.12), même valeur
    naïve en UTC — voir app.py pour le même helper."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── UTILITAIRES INTERNES ──────────────────────────────────────────────────────

def _compute_sec_status(label_val: str, date_fin_val: str) -> str:
    """
    Calcule le statut de sécurité (AV/EDR/RMM).
    Retourne: 'none' | 'expired' | 'expiring' | 'active'
    """
    if not label_val:
        return 'none'
    if date_fin_val:
        try:
            fin_d = date.fromisoformat(date_fin_val)
            today_d = date.today()
            if fin_d < today_d:
                return 'expired'
            elif fin_d <= today_d + timedelta(days=30):
                return 'expiring'
            else:
                return 'active'
        except (ValueError, TypeError):
            pass
    return 'active'


def _format_date_field(data: dict, field_name: str, date_format: str = '%d/%m/%Y') -> None:
    """Ajoute une version formatée d'une date ISO à un dictionnaire."""
    value = data.get(field_name) or ''
    if value:
        try:
            data[f'{field_name}_fmt'] = date.fromisoformat(value).strftime(date_format)
        except (ValueError, TypeError):
            data[f'{field_name}_fmt'] = value
    else:
        data[f'{field_name}_fmt'] = ''


# ─── PAGINATION ───────────────────────────────────────────────────────────────

def paginate(query: str, params: tuple, page: int, per_page: int = None):
    """
    Exécute une requête paginée.
    Retourne (rows, pagination_dict).
    query doit être une SELECT sans LIMIT/OFFSET.
    """
    if per_page is None:
        from config_helpers import cfg_get
        try:
            per_page = int(cfg_get('lignes_par_page', '50') or 50)
            per_page = max(5, min(per_page, 1000))
        except (ValueError, TypeError):
            per_page = 50
    from database import get_db
    conn = get_db()
    try:
        count_row = conn.execute(f'SELECT COUNT(*) FROM ({query})', params).fetchone()
        total = count_row[0] if count_row else 0
        pages = max(1, (total + per_page - 1) // per_page)
        page  = max(1, min(page, pages))
        rows  = conn.execute(
            f'{query} LIMIT ? OFFSET ?',
            params + (per_page, (page - 1) * per_page)
        ).fetchall()
        return rows, {'page': page, 'pages': pages, 'per_page': per_page, 'total': total}
    finally:
        conn.close()


# ─── ACCÈS CLIENTS ────────────────────────────────────────────────────────────

def get_client_access(client_id) -> Optional[str]:
    from database import get_db
    uid = session.get('auth_user_id')
    if not uid:
        return None
    conn = get_db()
    try:
        role_row = conn.execute('SELECT role FROM auth_users WHERE id=?', (uid,)).fetchone()
        if not role_row:
            logger.warning(f'User {uid} has no role defined in auth_users, using default: user')
            role = 'user'
        else:
            role = role_row[0]
        if role == 'admin':
            return 'proprietaire'
        own = conn.execute(
            'SELECT id FROM clients WHERE id=? AND auth_user_id=?', (client_id, uid)).fetchone()
        if own:
            return 'proprietaire'
        shared = conn.execute(
            'SELECT niveau FROM client_partages WHERE client_id=? AND auth_user_id=?',
            (client_id, uid)).fetchone()
        return shared[0] if shared else None
    finally:
        conn.close()


def can_write(client_id=None) -> bool:
    if client_id is None:
        client_id = get_client_id()
    if not client_id:
        return False
    return get_client_access(client_id) in ('proprietaire', 'ecriture')


def get_client_with_acces(cid) -> dict:
    from database import get_db, row_to_dict
    conn = get_db()
    try:
        cl = row_to_dict(conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone() or {})
        if cl:
            cl['acces'] = get_client_access(cid) or 'lecture'
        return cl
    finally:
        conn.close()


def get_client_id():
    """Retourne le client_id actif depuis la session, parmi les clients accessibles."""
    from database import get_db
    uid = session.get('auth_user_id')
    conn = get_db()
    try:
        if uid:
            role_row = conn.execute('SELECT role FROM auth_users WHERE id=?', (uid,)).fetchone()
            if not role_row:
                logger.warning(f'User {uid} has no role defined, using default: user')
                role = 'user'
            else:
                role = role_row[0]
        else:
            role = 'user'
        cid = session.get('client_id')
        if cid:
            if role == 'admin':
                if conn.execute('SELECT id FROM clients WHERE id=?', (cid,)).fetchone():
                    return cid
            else:
                own    = conn.execute('SELECT id FROM clients WHERE id=? AND auth_user_id=?', (cid, uid)).fetchone()
                shared = conn.execute('SELECT id FROM client_partages WHERE client_id=? AND auth_user_id=?',
                                      (cid, uid)).fetchone() if uid else None
                if own or shared:
                    return cid
        if role == 'admin':
            first = conn.execute('SELECT id FROM clients ORDER BY id LIMIT 1').fetchone()
        elif uid:
            first = conn.execute('SELECT id FROM clients WHERE auth_user_id=? ORDER BY id LIMIT 1', (uid,)).fetchone()
            if not first:
                first = conn.execute(
                    'SELECT c.id FROM clients c JOIN client_partages cp ON c.id=cp.client_id '
                    'WHERE cp.auth_user_id=? ORDER BY c.id LIMIT 1', (uid,)).fetchone()
        else:
            first = conn.execute('SELECT id FROM clients ORDER BY id LIMIT 1').fetchone()
        if first:
            session['client_id'] = first[0]
            return first[0]
    finally:
        conn.close()
    return None


def get_clients() -> list:
    """Retourne les clients accessibles par l'utilisateur connecté."""
    from database import get_db, row_to_dict
    uid = session.get('auth_user_id')
    if not uid:
        return []
    conn = get_db()
    try:
        role_row = conn.execute('SELECT role FROM auth_users WHERE id=?', (uid,)).fetchone()
        role = role_row[0] if role_row else 'user'
        if role == 'admin':
            all_cl = [row_to_dict(r) for r in conn.execute(
                """SELECT c.*, au.login as owner_login, au.nom as owner_nom,
                   CASE WHEN c.auth_user_id=? THEN 'proprietaire'
                        ELSE COALESCE((SELECT niveau FROM client_partages
                                       WHERE client_id=c.id AND auth_user_id=?), 'admin')
                   END as acces
                   FROM clients c LEFT JOIN auth_users au ON c.auth_user_id=au.id ORDER BY c.nom""",
                (uid, uid)).fetchall()]
            return all_cl
        own = [row_to_dict(r) for r in conn.execute(
            "SELECT *, 'proprietaire' as acces FROM clients WHERE auth_user_id=? ORDER BY nom",
            (uid,)).fetchall()]
        shared = [row_to_dict(r) for r in conn.execute(
            "SELECT c.*, cp.niveau as acces FROM clients c "
            "JOIN client_partages cp ON c.id=cp.client_id WHERE cp.auth_user_id=? ORDER BY c.nom",
            (uid,)).fetchall()]
        seen = set(); result = []
        for cl in own + shared:
            if cl['id'] not in seen:
                seen.add(cl['id']); result.append(cl)
        return result
    finally:
        conn.close()


def get_clients_for_filter(clients_selection=None) -> list:
    """
    Récupère clients accessibles pour filtrage multi-client.
    - Si clients_selection=None : client_id session
    - Si clients_selection=['all'] : tous les clients accessibles
    - Sinon : clients_selection[] IDs (intersection avec accessibles)
    """
    all_accessible = get_clients()

    if not clients_selection:
        active_id = get_client_id()
        return [c for c in all_accessible if c['id'] == active_id]

    if clients_selection == ['all']:
        return all_accessible

    try:
        selected_ids = [int(cid) for cid in clients_selection]
    except (ValueError, TypeError):
        return [c for c in all_accessible if c['id'] == get_client_id()]

    return [c for c in all_accessible if c['id'] in selected_ids]


# ─── AUDIT ────────────────────────────────────────────────────────────────────

def log_history(conn, client_id, entite, entite_id, entite_nom, action, details=''):
    """Enregistre une entrée dans le journal d'historique."""
    conn.execute(
        '''INSERT INTO historique (client_id,entite,entite_id,entite_nom,action,date_action,details)
           VALUES (?,?,?,?,?,?,?)''',
        (client_id, entite, entite_id, str(entite_nom), action,
         _utcnow().isoformat(), str(details)))
    # Nettoyage automatique selon les paramètres de rétention (lignes ET/OU durée)
    try:
        from config_helpers import cfg_get
        max_l = int(cfg_get('historique_max_lignes') or 500)
        if max_l > 0:
            conn.execute(
                '''DELETE FROM historique WHERE client_id=? AND id NOT IN (
                   SELECT id FROM historique WHERE client_id=? ORDER BY id DESC LIMIT ?)''',
                (client_id, client_id, max_l))
        max_j = int(cfg_get('historique_max_jours') or 0)
        if max_j > 0:
            limite = (_utcnow() - timedelta(days=max_j)).isoformat()
            conn.execute(
                'DELETE FROM historique WHERE client_id=? AND date_action < ?',
                (client_id, limite))
    except Exception as e:
        logger.error(f'Erreur nettoyage historique pour client {client_id}: {e}', exc_info=True)


def log_error(conn, client_id, url, exc, trace=''):
    """Enregistre une erreur applicative dans le journal d'historique."""
    import json as _j
    details = _j.dumps({
        'message': str(exc)[:600],
        'url':     str(url)[:200],
        'trace':   str(trace)[-800:] if trace else '',
    }, ensure_ascii=False)
    log_history(conn, client_id, 'système', 0,
                str(url)[:120] or 'Erreur système', 'Erreur', details)


# ─── INSTANTANÉS & CHANGEMENTS ENTRE VISITES (v2.22.x) ─────────────────────────
#
# « Je scanne un client, quelques semaines plus tard je reviens : montre-moi ce
# qui a changé. » Un instantané = photo compacte de l'inventaire du client à un
# instant T (créée automatiquement à la fin de chaque scan). Le rapport
# « Changements » diffe les deux derniers instantanés (ou la référence épinglée
# et le dernier).

_INSTANTANES_GARDES = 20        # par client, hors référence épinglée
_MAC_RE = re.compile(r'^[0-9a-f]{2}(:[0-9a-f]{2}){5}$')


def _norm_mac(m: str) -> str:
    m = re.sub(r'[\s\-.]', ':', (m or '').strip().lower())
    return m if _MAC_RE.match(m) else ''


def _etat_inventaire(conn, client_id: int) -> list:
    """Photo de l'inventaire d'un client pour un instantané : l'essentiel qui
    peut bouger d'une visite à l'autre (nom, IP, MAC, type, ports ouverts,
    statut) + les MAC secondaires."""
    appareils = []
    macs_sec = {}
    try:
        for aid, mac in conn.execute(
                "SELECT appareil_id, adresse_mac FROM appareil_macs WHERE client_id=?",
                (client_id,)):
            m = _norm_mac(mac)
            if m:
                macs_sec.setdefault(aid, []).append(m)
    except Exception:
        pass
    for r in conn.execute(
            "SELECT id, nom_machine, adresse_ip, adresse_mac, type_appareil, "
            "ports_ouverts, statut, COALESCE(os,''), COALESCE(version_os,'') "
            "FROM appareils WHERE client_id=? ORDER BY id", (client_id,)):
        appareils.append({
            'id': r[0], 'nom': r[1] or '', 'ip': (r[2] or '').strip(),
            'mac': _norm_mac(r[3]), 'type': r[4] or '',
            'ports': sorted(int(p) for p in re.split(r'[,\s]+', r[5] or '') if p.isdigit()),
            'statut': r[6] or 'actif',
            'os': (r[7] or '').strip(), 'version_os': (r[8] or '').strip(),
            'macs_sec': sorted(macs_sec.get(r[0], [])),
        })
    return appareils


# Champs de parc_general suivis d'une visite à l'autre (libellé lisible).
_PARC_CHAMPS = [
    ('type_connexion', 'Type de connexion'), ('debit_descendant', 'Débit descendant'),
    ('debit_montant', 'Débit montant'), ('fournisseur_internet', 'Fournisseur (FAI)'),
    ('ip_publique', 'IP publique'), ('plage_ip_locale', 'Plage(s) IP locale(s)'),
    ('nb_machines', 'Nb de machines'), ('nb_utilisateurs', 'Nb d\'utilisateurs'),
    ('domaine', 'Domaine'), ('serveur_dns', 'Serveur(s) DNS'), ('passerelle', 'Passerelle'),
    ('os_principal', 'OS principal'), ('antivirus', 'Antivirus'),
    ('suite_bureautique', 'Suite bureautique'),
]


def _etat_parc(conn, client_id: int) -> dict:
    r = conn.execute(
        "SELECT %s FROM parc_general WHERE client_id=?" % ','.join(k for k, _ in _PARC_CHAMPS),
        (client_id,)).fetchone()
    if not r:
        return {}
    return {k: ('' if v is None else str(v).strip()) for (k, _), v in zip(_PARC_CHAMPS, r)}


def _etat_cablage(conn, client_id: int) -> list:
    """Câblage DÉCLARÉ de la baie : par (emplacement, port de façade), ce qui est
    branché — un cordon vers un autre port, un appareil/périphérique direct, ou
    « libre »."""
    slot_nom = {}
    try:
        for sid, pos, nomc in conn.execute(
                "SELECT id, position, nom_custom FROM baie_slots WHERE client_id=?",
                (client_id,)):
            slot_nom[sid] = (nomc or '').strip() or f"U{pos}"
    except Exception:
        return []
    out = []
    try:
        for sid, num, aid, pid, libre, lsid, lnum in conn.execute(
                "SELECT p.slot_id, p.numero, p.appareil_id, p.peripherique_id, "
                "p.usage_libre, p.lie_slot_id, p.lie_port_numero FROM baie_slot_ports p "
                "JOIN baie_slots s ON s.id = p.slot_id WHERE s.client_id=?", (client_id,)):
            if lsid:
                cible = f"cordon → {slot_nom.get(lsid, '?')} port {lnum}"
            elif aid:
                cible = f"appareil #{aid}"
            elif pid:
                cible = f"périphérique #{pid}"
            elif libre:
                cible = 'libre'
            else:
                continue
            out.append({'emplacement': slot_nom.get(sid, '?'), 'port': num, 'cible': cible})
    except Exception:
        pass
    return sorted(out, key=lambda x: (x['emplacement'], x['port']))


def capturer_instantane(conn, client_id: int, origine: str = 'auto',
                        libelle: str = '', reference: bool = False) -> int:
    """Crée un instantané de l'inventaire du client. `origine` : 'scan' | 'manuel'
    | 'auto'. Purge les plus anciens (garde `_INSTANTANES_GARDES` + la référence).
    Retourne l'id créé (0 si échec)."""
    try:
        now = _utcnow()
        cur = conn.execute(
            "INSERT INTO client_instantane (client_id, horodatage, epoch, origine, "
            "libelle, reference, donnees_json) VALUES (?,?,?,?,?,?,?)",
            (client_id, now.isoformat(), now.timestamp(), origine, libelle,
             1 if reference else 0,
             json.dumps({'appareils': _etat_inventaire(conn, client_id),
                         'parc': _etat_parc(conn, client_id),
                         'cablage': _etat_cablage(conn, client_id)},
                        ensure_ascii=False)))
        nid = cur.lastrowid
        garder = [r[0] for r in conn.execute(
            "SELECT id FROM client_instantane WHERE client_id=? AND reference=0 "
            "ORDER BY id DESC LIMIT ?", (client_id, _INSTANTANES_GARDES))]
        if garder:
            conn.execute(
                f"DELETE FROM client_instantane WHERE client_id=? AND reference=0 "
                f"AND id NOT IN ({','.join('?' * len(garder))})",
                (client_id, *garder))
        return nid
    except Exception:
        logger.error("capturer_instantane client %s", client_id, exc_info=True)
        return 0


def _charger_instantane(conn, iid: int):
    r = conn.execute(
        "SELECT id, horodatage, epoch, origine, libelle, reference, donnees_json "
        "FROM client_instantane WHERE id=?", (iid,)).fetchone()
    if not r:
        return None
    try:
        d = json.loads(r[6] or '{}')
    except (ValueError, TypeError):
        d = {}
    return {'id': r[0], 'horodatage': r[1], 'epoch': r[2], 'origine': r[3],
            'libelle': r[4], 'reference': bool(r[5]),
            'appareils': d.get('appareils', []),
            'parc': d.get('parc', {}), 'cablage': d.get('cablage', [])}


def lister_instantanes(conn, client_id: int) -> list:
    return [{'id': r[0], 'horodatage': r[1], 'origine': r[2], 'libelle': r[3],
             'reference': bool(r[4]), 'nb_appareils': _nb_app(r[5])}
            for r in conn.execute(
                "SELECT id, horodatage, origine, libelle, reference, donnees_json "
                "FROM client_instantane WHERE client_id=? ORDER BY id DESC", (client_id,))]


def _nb_app(dj):
    try:
        return len(json.loads(dj or '{}').get('appareils', []))
    except (ValueError, TypeError):
        return 0


def changements_client(conn, client_id: int, avant_id=None, apres_id=None) -> dict:
    """Diff entre deux instantanés du client. Par défaut : la **référence
    épinglée** (ou l'avant-dernier) → le **dernier**. Catégories :
    appareils nouveaux / disparus, IP changée, MAC principale changée, type
    changé, ports ouverts changés, OS/version changés (Lot 2), câblage DÉCLARÉ
    de la baie modifié (Lot 2), champs `parc_general` modifiés (Lot 2), + le
    câblage RÉEL observé en SNMP (`diag_topologie_mouvements`) sur la période."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM client_instantane WHERE client_id=? ORDER BY id DESC LIMIT 2",
        (client_id,))]
    ref = conn.execute(
        "SELECT id FROM client_instantane WHERE client_id=? AND reference=1 "
        "ORDER BY id DESC LIMIT 1", (client_id,)).fetchone()
    apres_id = apres_id or (ids[0] if ids else None)
    if avant_id is None:
        avant_id = ref[0] if (ref and ref[0] != apres_id) else (ids[1] if len(ids) > 1 else None)
    if not apres_id or not avant_id or avant_id == apres_id:
        return {'disponible': False, 'nb_instantanes': len(lister_instantanes(conn, client_id))}
    a = _charger_instantane(conn, avant_id)
    b = _charger_instantane(conn, apres_id)
    if not a or not b:
        return {'disponible': False}

    pa = {x['id']: x for x in a['appareils']}
    pb = {x['id']: x for x in b['appareils']}
    # index MAC → rattacher un "disparu" à un "nouveau" de même MAC (fiche recréée)
    mac_a = {x['mac']: x for x in a['appareils'] if x['mac']}

    nouveaux, disparus, ip_changees, mac_changees, type_changes, ports_changes = [], [], [], [], [], []
    os_changes = []

    def _os(z):
        return ' '.join(p for p in (z.get('os', ''), z.get('version_os', '')) if p).strip()

    for i, x in pb.items():
        if i in pa:
            y = pa[i]
            if x['ip'] and y['ip'] and x['ip'] != y['ip']:
                ip_changees.append({'id': i, 'nom': x['nom'], 'avant': y['ip'], 'apres': x['ip']})
            if x['mac'] and y['mac'] and x['mac'] != y['mac']:
                mac_changees.append({'id': i, 'nom': x['nom'], 'avant': y['mac'], 'apres': x['mac']})
            if x['type'] and y['type'] and x['type'] != y['type']:
                type_changes.append({'id': i, 'nom': x['nom'], 'avant': y['type'], 'apres': x['type']})
            if _os(x) and _os(y) and _os(x) != _os(y):
                os_changes.append({'id': i, 'nom': x['nom'], 'avant': _os(y), 'apres': _os(x)})
            if x['ports'] != y['ports']:
                ouverts = sorted(set(x['ports']) - set(y['ports']))
                fermes = sorted(set(y['ports']) - set(x['ports']))
                if ouverts or fermes:
                    ports_changes.append({'id': i, 'nom': x['nom'],
                                          'ouverts': ouverts, 'fermes': fermes})
        elif x['mac'] and x['mac'] in mac_a:
            # même MAC qu'un appareil de l'ancien instantané → fiche recréée,
            # pas un nouveau matériel
            y = mac_a[x['mac']]
            if x['ip'] and y['ip'] and x['ip'] != y['ip']:
                ip_changees.append({'id': i, 'nom': x['nom'], 'avant': y['ip'], 'apres': x['ip']})
        else:
            nouveaux.append({'id': i, 'nom': x['nom'], 'ip': x['ip'], 'mac': x['mac'],
                             'type': x['type']})
    macs_b = {x['mac'] for x in b['appareils'] if x['mac']}
    for i, x in pa.items():
        if i not in pb and not (x['mac'] and x['mac'] in macs_b):
            disparus.append({'id': i, 'nom': x['nom'], 'ip': x['ip'], 'mac': x['mac'],
                             'type': x['type']})

    # nom de chaque appareil connu (pour rendre lisibles les "#id" du câblage)
    nom_par_id = {x['id']: x['nom'] for x in b['appareils']}
    nom_par_id.update({x['id']: x['nom'] for x in a['appareils'] if x['id'] not in nom_par_id})

    def _lisible(cible):
        m = re.match(r'^(appareil|périphérique) #(\d+)$', cible)
        if m and m.group(1) == 'appareil' and int(m.group(2)) in nom_par_id:
            return f"appareil {nom_par_id[int(m.group(2))]}"
        return cible

    # câblage DÉCLARÉ de la baie (baie_slot_ports) : ajouté / retiré / modifié
    cablage_declare = []
    ca = {(c['emplacement'], c['port']): c['cible'] for c in a.get('cablage', [])}
    cb = {(c['emplacement'], c['port']): c['cible'] for c in b.get('cablage', [])}
    for k in sorted(set(ca) | set(cb)):
        av, ap = ca.get(k), cb.get(k)
        if av == ap:
            continue
        cablage_declare.append({
            'emplacement': k[0], 'port': k[1],
            'avant': _lisible(av) if av else '', 'apres': _lisible(ap) if ap else '',
            'genre': 'ajout' if not av else 'retrait' if not ap else 'modif'})

    # champs parc_general modifiés
    parc_changes = []
    pca, pcb = a.get('parc', {}), b.get('parc', {})
    for cle, libelle in _PARC_CHAMPS:
        av, ap = pca.get(cle, ''), pcb.get(cle, '')
        if av != ap and (av or ap):
            parc_changes.append({'champ': libelle, 'avant': av, 'apres': ap})

    # câblage réel (SNMP) sur la période
    cablage = []
    try:
        for h, genre, anom, mac, eip, enom, pav, pap in conn.execute(
                "SELECT horodatage, genre, appareil_nom, mac, equipement_ip, "
                "equipement_nom, port_avant, port_apres FROM diag_topologie_mouvements "
                "WHERE client_id=? AND horodatage > ? AND horodatage <= ? "
                "ORDER BY horodatage DESC LIMIT 200",
                (client_id, a['horodatage'], b['horodatage'])):
            cablage.append({'horodatage': h, 'genre': genre, 'appareil': anom or mac,
                            'equipement': enom or eip, 'port_avant': pav, 'port_apres': pap})
    except Exception:
        pass

    nb = (len(nouveaux) + len(disparus) + len(ip_changees) + len(mac_changees)
          + len(type_changes) + len(ports_changes) + len(os_changes)
          + len(cablage_declare) + len(parc_changes) + len(cablage))
    jours = None
    try:
        jours = max(0, round((b['epoch'] - a['epoch']) / 86400))
    except (TypeError, KeyError):
        pass
    return {
        'disponible': True, 'nb': nb, 'jours_ecoules': jours,
        'avant': {'id': a['id'], 'horodatage': a['horodatage'], 'libelle': a['libelle'],
                  'reference': a['reference']},
        'apres': {'id': b['id'], 'horodatage': b['horodatage'], 'libelle': b['libelle']},
        'nouveaux': nouveaux, 'disparus': disparus,
        'ip_changees': ip_changees, 'mac_changees': mac_changees,
        'type_changes': type_changes, 'ports_changes': ports_changes,
        'os_changes': os_changes, 'cablage_declare': cablage_declare,
        'parc_changes': parc_changes, 'cablage_reel': cablage,
    }


# ─── FORMATAGE ────────────────────────────────────────────────────────────────

def garantie_active(s: str) -> bool:
    if s:
        try:
            return date.fromisoformat(s) >= date.today()
        except (ValueError, TypeError):
            pass
    return False


def human_size(b: float) -> str:
    for u in ['o', 'Ko', 'Mo', 'Go']:
        if b < 1024:
            return f'{b:.0f} {u}'
        b /= 1024
    return f'{b:.1f} Go'


def fmt_appareils(appareils: list) -> list:
    for a in appareils:
        # Garantie générale
        a['garantie_active'] = garantie_active(a.get('date_fin_garantie', ''))
        _format_date_field(a, 'date_fin_garantie', '%d/%m/%Y')

        # Dernier ping
        if a.get('dernier_ping'):
            try:
                a['dernier_ping_fmt'] = datetime.fromisoformat(
                    a['dernier_ping']).strftime('%d/%m %H:%M')
            except (ValueError, TypeError):
                a['dernier_ping_fmt'] = ''
        else:
            a['dernier_ping_fmt'] = ''

        # Antivirus
        av_label = a.get('av_nom') or a.get('av_marque') or ''
        a['av_status'] = _compute_sec_status(av_label, a.get('av_date_fin') or '')
        _format_date_field(a, 'av_date_debut', '%d/%m/%Y')
        _format_date_field(a, 'av_date_fin', '%d/%m/%Y')

        # EDR
        edr_label = a.get('edr_nom') or a.get('edr_marque') or ''
        a['edr_status'] = _compute_sec_status(edr_label, a.get('edr_date_fin') or '')
        _format_date_field(a, 'edr_date_fin', '%d/%m/%Y')

        # RMM
        rmm_label = a.get('rmm_nom') or a.get('rmm_marque') or ''
        a['rmm_status'] = _compute_sec_status(rmm_label, a.get('rmm_date_fin') or '')
        _format_date_field(a, 'rmm_date_fin', '%d/%m/%Y')

    return appareils


def fmt_garantie_periph(p: dict) -> dict:
    from config_helpers import cfg_get
    seuil = int(cfg_get('garantie_alerte_jours', 90))
    p['garantie_active']  = garantie_active(p.get('date_fin_garantie', ''))
    p['garantie_bientot'] = False
    p['garantie_depassee'] = False
    if p.get('date_fin_garantie'):
        try:
            df = date.fromisoformat(p['date_fin_garantie'])
            delta = (df - date.today()).days
            p['garantie_bientot']  = 0 <= delta <= seuil
            p['garantie_depassee'] = delta < 0
            p['date_fin_garantie_fmt'] = df.strftime('%d/%m/%Y')
        except (ValueError, TypeError):
            p['date_fin_garantie_fmt'] = p['date_fin_garantie']
    else:
        p['date_fin_garantie_fmt'] = ''
    return p


def fmt_contrat(c_: dict) -> dict:
    c_['expire_bientot'] = False
    c_['expire_depasse'] = False
    c_['jours_restants'] = None
    if c_.get('date_fin'):
        try:
            df = date.fromisoformat(c_['date_fin'])
            delta = (df - date.today()).days
            c_['jours_restants'] = delta
            c_['date_fin_fmt'] = df.strftime('%d/%m/%Y')
            preavis = c_.get('preavis_jours') or 30
            if delta < 0:
                c_['expire_depasse'] = True
            elif delta <= preavis:
                c_['expire_bientot'] = True
        except (ValueError, TypeError):
            c_['date_fin_fmt'] = c_['date_fin']
    else:
        c_['date_fin_fmt'] = ''
    if c_.get('date_debut'):
        try:
            c_['date_debut_fmt'] = date.fromisoformat(c_['date_debut']).strftime('%d/%m/%Y')
        except (ValueError, TypeError):
            c_['date_debut_fmt'] = c_['date_debut']
    else:
        c_['date_debut_fmt'] = ''
    return c_

def fmt_intervention(i_: dict) -> dict:
    """Formate une intervention pour l'affichage."""
    # Dates formatées
    if i_.get('date_intervention'):
        try:
            di = date.fromisoformat(i_['date_intervention'])
            i_['date_intervention_fmt'] = di.strftime('%d/%m/%Y')
            i_['date_intervention_jj'] = di.strftime('%d')
            i_['date_intervention_mm'] = di.strftime('%b').upper()
        except (ValueError, TypeError):
            i_['date_intervention_fmt'] = i_['date_intervention']
            i_['date_intervention_jj'] = ''
            i_['date_intervention_mm'] = ''
    else:
        i_['date_intervention_fmt'] = ''
        i_['date_intervention_jj'] = ''
        i_['date_intervention_mm'] = ''

    # Couleur et emoji par statut
    i_['statut_color'] = {
        'planifiee': '#2196F3',      # 🔵 blue
        'en_cours': '#FF5722',       # 🔴 red
        'completee': '#4CAF50',      # 🟢 green
        'reportee': '#FF9800',       # 🟠 orange
        'archivee': '#9E9E9E'        # ⚫ grey
    }.get(i_.get('statut', 'completee'), '#757575')

    i_['statut_emoji'] = {
        'planifiee': '🔵',
        'en_cours': '🔴',
        'completee': '🟢',
        'reportee': '🟠',
        'archivee': '⚫'
    }.get(i_.get('statut', 'completee'), '❓')

    # Durée lisible
    if i_.get('duree_minutes'):
        try:
            m = int(i_['duree_minutes'])
            h = m // 60
            m = m % 60
            if h > 0:
                i_['duree_fmt'] = f"{h}h{m}min"
            else:
                i_['duree_fmt'] = f"{m}min"
        except (ValueError, TypeError):
            i_['duree_fmt'] = ''
    else:
        i_['duree_fmt'] = ''

    # Horaire lisible
    if i_.get('heure_debut') and i_.get('heure_fin'):
        i_['horaire'] = f"{i_['heure_debut']} - {i_['heure_fin']}"
    elif i_.get('heure_debut'):
        i_['horaire'] = f"À partir de {i_['heure_debut']}"
    else:
        i_['horaire'] = 'Horaire non précisé'

    return i_
