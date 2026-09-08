"""Recherche globale ParcInfo (palette Ctrl+K).

Cherche en une frappe dans **appareils, périphériques, contrats, services,
utilisateurs finaux, identifiants, clients** — par nom, n° de série, IP, MAC
(avec ou sans séparateurs), login, n° de contrat, localisation…

- Toujours borné aux clients **accessibles** (l'appelant passe la liste d'ids ;
  `/api/search` la construit depuis `get_clients()`).
- Les identifiants ne renvoient **jamais** le mot de passe — seulement le
  libellé / login / URL.
- Classement (`_score`) : correspondance exacte (n° série, IP, MAC entière,
  n° de contrat) > début de champ > sous-chaîne ; le client actif d'abord.

Approche volontairement simple : requêtes `LIKE` directes (une par entité), pas
de FTS5 — suffisant jusqu'à quelques milliers de lignes par table. `_normaliser_
terme` fait le gros du travail utile (IP / MAC / numéro).
"""
import ipaddress
import logging
import re

from database import get_db

logger = logging.getLogger('parcinfo')

_MIN = 2                       # longueur minimale du terme
_RE_MAC_LIBRE = re.compile(r'^[0-9a-f]{2}([:\-. ]?[0-9a-f]{2}){3,5}$', re.I)
_RE_IP = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
_RE_NUM = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\-_/]{3,}$')


def _normaliser_terme(q: str) -> dict:
    """Analyse le terme : `{texte, like, ip, mac, mac_like, est_num}`.

    - `ip` : le terme EST une IPv4 → correspondance exacte sur `adresse_ip`.
    - `mac` : le terme ressemble à une MAC → `mac` = hex sans séparateurs,
      `mac_like` = motif `%aabbcc%` pour un `REPLACE(...)` SQL.
    - `est_num` : ressemble à un n° de série / commande / contrat.
    """
    t = (q or '').strip()
    bas = t.lower()
    d = {'texte': t, 'like': '%%%s%%' % t.replace('%', r'\%').replace('_', r'\_'),
         'ip': None, 'mac': None, 'mac_like': None, 'est_num': False}
    if _RE_IP.match(t):
        try:
            ipaddress.ip_address(t)
            d['ip'] = t
        except ValueError:
            pass
    hexbrut = re.sub(r'[^0-9a-f]', '', bas)
    if _RE_MAC_LIBRE.match(t) and 8 <= len(hexbrut) <= 12:
        d['mac'] = hexbrut
        d['mac_like'] = '%%%s%%' % hexbrut
    if _RE_NUM.match(t) and not d['ip'] and not d['mac']:
        d['est_num'] = True
    return d


def _score(terme: dict, *champs) -> int:
    """Score d'un résultat : 100 exact, 60 préfixe, 30 sous-chaîne, 0 sinon —
    sur le meilleur des `champs` fournis (chaînes)."""
    bas = terme['texte'].lower()
    best = 0
    for c in champs:
        c = (c or '').strip().lower()
        if not c:
            continue
        if c == bas:
            return 100
        if c.startswith(bas):
            best = max(best, 60)
        elif bas in c:
            best = max(best, 30)
    return best


def _macs_hex(conn, ids_ph, params):
    """`{appareil_id: [hex de chaque MAC]}` (principale + `appareil_macs`),
    MAC réduites à leurs octets hexadécimaux — pour un `REPLACE` SQL propre on
    filtrerait côté base, mais un post-filtrage Python reste simple et suffit
    au volume visé."""
    out = {}
    for aid, mac in conn.execute(
            "SELECT id, adresse_mac FROM appareils WHERE client_id IN (%s) "
            "AND COALESCE(adresse_mac,'')<>''" % ids_ph, params):
        out.setdefault(aid, []).append(re.sub(r'[^0-9a-f]', '', (mac or '').lower()))
    try:
        for aid, mac in conn.execute(
                "SELECT appareil_id, adresse_mac FROM appareil_macs WHERE client_id IN (%s)"
                % ids_ph, params):
            out.setdefault(aid, []).append(re.sub(r'[^0-9a-f]', '', (mac or '').lower()))
    except Exception:
        pass
    return out


_ICONES = {'appareils': '🖥', 'peripheriques': '🖱', 'contrats': '📋',
           'services': '🔧', 'utilisateurs': '👤', 'identifiants': '🔐',
           'clients': '🏢'}


def search_global(query: str, client_ids, actif_id=None, limit: int = 8) -> dict:
    """`{<entite>: [résultats], total, tronque}`. `client_ids` = liste des ids
    de clients accessibles à balayer (jamais élargie ici : l'ACL est faite par
    l'appelant)."""
    vide = {k: [] for k in _ICONES}
    vide.update({'total': 0, 'tronque': False, 'query': query or ''})
    if not query or len(query.strip()) < _MIN or not client_ids:
        return vide

    ids = [int(i) for i in client_ids]
    ph = ','.join('?' * len(ids))
    T = _normaliser_terme(query)
    lk = T['like']
    res = {k: [] for k in _ICONES}
    lim = max(1, min(int(limit or 8), 25))
    par_page = lim + 4        # on en prend un peu plus pour bien classer

    try:
        conn = get_db()
        nom_client = dict(conn.execute(
            "SELECT id, nom FROM clients WHERE id IN (%s)" % ph, ids))

        def _fin(rows, entite, faire):
            items = []
            for r in rows:
                it = faire(r)
                if it:
                    it['entite'] = entite
                    it['icone'] = _ICONES[entite]
                    it['client_nom'] = nom_client.get(it.get('client_id'), '')
                    items.append(it)
            items.sort(key=lambda x: (-(x.get('_sc') or 0),
                                      0 if x.get('client_id') == actif_id else 1,
                                      x.get('titre', '')))
            res[entite] = items[:lim]

        # ── APPAREILS ──
        if T['ip']:
            rows = conn.execute(
                "SELECT id, client_id, nom_machine, type_appareil, adresse_ip, "
                "numero_serie, marque, modele, localisation FROM appareils "
                "WHERE client_id IN (%s) AND adresse_ip=? LIMIT ?" % ph,
                (*ids, T['ip'], par_page)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, client_id, nom_machine, type_appareil, adresse_ip, "
                "numero_serie, marque, modele, localisation FROM appareils "
                "WHERE client_id IN (%s) AND ("
                "nom_machine LIKE ? ESCAPE '\\' OR adresse_ip LIKE ? ESCAPE '\\' "
                "OR numero_serie LIKE ? ESCAPE '\\' OR marque LIKE ? ESCAPE '\\' "
                "OR modele LIKE ? ESCAPE '\\' OR localisation LIKE ? ESCAPE '\\' "
                "OR utilisateur LIKE ? ESCAPE '\\' OR nom_dns LIKE ? ESCAPE '\\') "
                "LIMIT ?" % ph,
                (*ids, lk, lk, lk, lk, lk, lk, lk, lk, par_page * 3)).fetchall()
        vus = {r[0] for r in rows}
        if T['mac']:
            macs = _macs_hex(conn, ph, tuple(ids))
            for aid, lst in macs.items():
                if aid not in vus and any(T['mac'] in h for h in lst):
                    ar = conn.execute(
                        "SELECT id, client_id, nom_machine, type_appareil, adresse_ip, "
                        "numero_serie, marque, modele, localisation FROM appareils "
                        "WHERE id=?", (aid,)).fetchone()
                    if ar:
                        rows.append(ar); vus.add(aid)

        def _mk_app(r):
            sub = ' · '.join(x for x in (
                ('%s %s' % (r[6] or '', r[7] or '')).strip(), r[4] or '',
                r[8] or '') if x)
            sc = _score(T, r[2], r[5], r[4])
            if T['ip'] and r[4] == T['ip']:
                sc = 100
            return {'id': r[0], 'client_id': r[1], 'titre': r[2] or '(sans nom)',
                    'sous_titre': sub or (r[3] or ''),
                    'url': '/appareil/%s/fiche-systeme' % r[0], '_sc': sc}
        _fin(rows, 'appareils', _mk_app)

        # ── PÉRIPHÉRIQUES ──
        rows = conn.execute(
            "SELECT id, client_id, categorie, marque, modele, numero_serie, localisation "
            "FROM peripheriques WHERE client_id IN (%s) AND ("
            "marque LIKE ? ESCAPE '\\' OR modele LIKE ? ESCAPE '\\' "
            "OR numero_serie LIKE ? ESCAPE '\\' OR categorie LIKE ? ESCAPE '\\' "
            "OR localisation LIKE ? ESCAPE '\\') LIMIT ?" % ph,
            (*ids, lk, lk, lk, lk, lk, par_page * 2)).fetchall()
        _fin(rows, 'peripheriques', lambda r: {
            'id': r[0], 'client_id': r[1],
            'titre': ('%s %s' % (r[3] or '', r[4] or '')).strip() or (r[2] or 'Périphérique'),
            'sous_titre': ' · '.join(x for x in (r[2] or '', r[5] or '', r[6] or '') if x),
            'url': '/peripherique/%s/editer' % r[0],
            '_sc': _score(T, r[3], r[4], r[5], r[2])})

        # ── CONTRATS ──
        rows = conn.execute(
            "SELECT id, client_id, titre, fournisseur, type_contrat, numero_contrat "
            "FROM contrats WHERE client_id IN (%s) AND ("
            "titre LIKE ? ESCAPE '\\' OR fournisseur LIKE ? ESCAPE '\\' "
            "OR numero_contrat LIKE ? ESCAPE '\\' OR type_contrat LIKE ? ESCAPE '\\') "
            "LIMIT ?" % ph, (*ids, lk, lk, lk, lk, par_page * 2)).fetchall()
        _fin(rows, 'contrats', lambda r: {
            'id': r[0], 'client_id': r[1], 'titre': r[2] or '(contrat)',
            'sous_titre': ' · '.join(x for x in (r[4] or '', r[3] or '',
                                                 ('n° ' + r[5]) if r[5] else '') if x),
            'url': '/contrat/%s' % r[0],
            '_sc': _score(T, r[2], r[5], r[3])})

        # ── SERVICES ──
        rows = conn.execute(
            "SELECT id, client_id, nom, description, responsable FROM services "
            "WHERE client_id IN (%s) AND (nom LIKE ? ESCAPE '\\' "
            "OR description LIKE ? ESCAPE '\\' OR responsable LIKE ? ESCAPE '\\') "
            "LIMIT ?" % ph, (*ids, lk, lk, lk, par_page)).fetchall()
        _fin(rows, 'services', lambda r: {
            'id': r[0], 'client_id': r[1], 'titre': r[2] or '(service)',
            'sous_titre': (r[3] or '')[:80],
            'url': '/service/%s/editer' % r[0], '_sc': _score(T, r[2])})

        # ── UTILISATEURS FINAUX ──
        rows = conn.execute(
            "SELECT id, client_id, prenom, nom, email, poste, login_windows, login_mail "
            "FROM utilisateurs WHERE client_id IN (%s) AND ("
            "prenom LIKE ? ESCAPE '\\' OR nom LIKE ? ESCAPE '\\' "
            "OR email LIKE ? ESCAPE '\\' OR poste LIKE ? ESCAPE '\\' "
            "OR login_windows LIKE ? ESCAPE '\\' OR login_mail LIKE ? ESCAPE '\\') "
            "LIMIT ?" % ph, (*ids, lk, lk, lk, lk, lk, lk, par_page * 2)).fetchall()
        _fin(rows, 'utilisateurs', lambda r: {
            'id': r[0], 'client_id': r[1],
            'titre': ('%s %s' % (r[2] or '', r[3] or '')).strip() or (r[4] or 'Utilisateur'),
            'sous_titre': ' · '.join(x for x in (r[5] or '', r[4] or '') if x),
            'url': '/utilisateur/%s/editer' % r[0],
            '_sc': _score(T, ('%s %s' % (r[2] or '', r[3] or '')).strip(), r[4], r[6])})

        # ── IDENTIFIANTS (jamais le mot de passe) ──
        rows = conn.execute(
            "SELECT id, client_id, nom, categorie, login, url FROM identifiants "
            "WHERE client_id IN (%s) AND (nom LIKE ? ESCAPE '\\' "
            "OR categorie LIKE ? ESCAPE '\\' OR login LIKE ? ESCAPE '\\' "
            "OR url LIKE ? ESCAPE '\\') LIMIT ?" % ph,
            (*ids, lk, lk, lk, lk, par_page * 2)).fetchall()
        _fin(rows, 'identifiants', lambda r: {
            'id': r[0], 'client_id': r[1], 'titre': r[2] or '(identifiant)',
            'sous_titre': ' · '.join(x for x in (r[3] or '', r[4] or '',
                                                 (r[5] or '')[:40]) if x),
            'url': '/identifiant/%s/editer' % r[0],
            '_sc': _score(T, r[2], r[4])})

        # ── CLIENTS ──
        rows = conn.execute(
            "SELECT id, nom, contact, email FROM clients WHERE id IN (%s) AND ("
            "nom LIKE ? ESCAPE '\\' OR contact LIKE ? ESCAPE '\\' "
            "OR email LIKE ? ESCAPE '\\')" % ph, (*ids, lk, lk, lk)).fetchall()
        _fin(rows, 'clients', lambda r: {
            'id': r[0], 'client_id': r[0], 'titre': r[1] or '(client)',
            'sous_titre': ' · '.join(x for x in (r[2] or '', r[3] or '') if x),
            'url': '/client/%s/selectionner' % r[0], '_sc': _score(T, r[1])})

        conn.close()
    except Exception:
        logger.exception("Recherche globale (q=%r)", query)
        return vide

    total = sum(len(res[k]) for k in _ICONES)
    tronque = any(len(res[k]) >= lim for k in _ICONES)
    for it_list in res.values():
        for it in it_list:
            it.pop('_sc', None)
    return {**res, 'total': total, 'tronque': tronque, 'query': query}


# ─── Compat : autocomplete d'un seul type (tom-select) ──────────────────────

def search_autocomplete(query: str, client_id: int, entity_type: str, limit: int = 10):
    if not query or len(query) < 2 or not client_id:
        return []
    lk = '%%%s%%' % query
    m = {'appareil': ("SELECT id, nom_machine, adresse_ip FROM appareils "
                      "WHERE client_id=? AND nom_machine LIKE ? LIMIT ?",
                      lambda r: {'id': r[0], 'value': r[0],
                                 'text': ('%s (%s)' % (r[1], r[2])) if r[2] else r[1]}),
         'contrat': ("SELECT id, titre, fournisseur FROM contrats "
                     "WHERE client_id=? AND titre LIKE ? LIMIT ?",
                     lambda r: {'id': r[0], 'value': r[0],
                                'text': ('%s - %s' % (r[1], r[2])) if r[2] else r[1]}),
         'utilisateur': ("SELECT id, prenom, nom, email FROM utilisateurs "
                         "WHERE client_id=? AND (prenom LIKE ? OR nom LIKE ? OR email LIKE ?) LIMIT ?",
                         lambda r: {'id': r[0], 'value': r[0],
                                    'text': ('%s %s (%s)' % (r[1], r[2], r[3])).strip()
                                    if r[3] else ('%s %s' % (r[1], r[2])).strip()}),
         'service': ("SELECT id, nom FROM services WHERE client_id=? AND nom LIKE ? LIMIT ?",
                     lambda r: {'id': r[0], 'value': r[0], 'text': r[1]}),
         'peripherique': ("SELECT id, categorie, marque, modele FROM peripheriques "
                          "WHERE client_id=? AND (marque LIKE ? OR modele LIKE ?) LIMIT ?",
                          lambda r: {'id': r[0], 'value': r[0],
                                     'text': ('%s %s (%s)' % (r[2], r[3], r[1])).strip()})}
    if entity_type not in m:
        return []
    sql, mk = m[entity_type]
    n_lk = sql.count('LIKE ?')
    try:
        conn = get_db()
        rows = conn.execute(sql, (client_id, *([lk] * n_lk), limit)).fetchall()
        conn.close()
        return [mk(r) for r in rows]
    except Exception:
        logger.exception("Autocomplete %s (q=%r)", entity_type, query)
        return []
