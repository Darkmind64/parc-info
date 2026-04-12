"""
client_helpers.py — Accès clients, pagination, audit, formatage.
"""
import logging
from datetime import datetime, date, timedelta
from flask import session

logger = logging.getLogger('parcinfo')


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

def get_client_access(client_id) -> str | None:
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


# ─── AUDIT ────────────────────────────────────────────────────────────────────

def log_history(conn, client_id, entite, entite_id, entite_nom, action, details=''):
    """Enregistre une entrée dans le journal d'historique."""
    conn.execute(
        '''INSERT INTO historique (client_id,entite,entite_id,entite_nom,action,date_action,details)
           VALUES (?,?,?,?,?,?,?)''',
        (client_id, entite, entite_id, str(entite_nom), action,
         datetime.utcnow().isoformat(), str(details)))
    # Nettoyage automatique selon le paramètre de rétention
    try:
        from config_helpers import cfg_get
        max_l = int(cfg_get('historique_max_lignes') or 500)
        if max_l > 0:
            conn.execute(
                '''DELETE FROM historique WHERE client_id=? AND id NOT IN (
                   SELECT id FROM historique WHERE client_id=? ORDER BY id DESC LIMIT ?)''',
                (client_id, client_id, max_l))
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
