"""
database.py — Connexion SQLite / Turso et utilitaires bas niveau.
DATABASE et UPLOAD_FOLDER sont initialisés par app.py au démarrage.
"""
import sqlite3, threading, json as _json, urllib.request, urllib.error, base64

# Chemins configurés au démarrage par app.py (ou le launcher)
DATABASE:     str = ''
UPLOAD_FOLDER: str = ''

# Garde anti-récursion pour get_db() ↔ cfg_get()
_tl = threading.local()


# ─── INITIALISATION DES CHEMINS ────────────────────────────────────────────────

def init_paths(db_path: str, upload_path: str) -> None:
    """Initialise les chemins DATABASE et UPLOAD_FOLDER de façon centralisée et robuste.

    DOIT être appelée une seule fois au démarrage (par app.py ou launcher.py).
    Résout le problème de fragmentation des chemins en deux endroits.
    """
    global DATABASE, UPLOAD_FOLDER
    DATABASE = db_path
    UPLOAD_FOLDER = upload_path
    logger = __import__('logging').getLogger('parcinfo')
    logger.debug(f'Database initialized: {DATABASE}')


# ─── CONNEXION PRINCIPALE ────────────────────────────────────────────────────

def get_db():
    """Retourne une connexion DB (Turso ou SQLite local selon la config)."""
    if getattr(_tl, 'reading_cfg', False):
        return _local_db()
    _tl.reading_cfg = True
    try:
        from config_helpers import cfg_get
        if cfg_get('db_type', 'local') == 'turso':
            url   = cfg_get('turso_url',   '').strip()
            token = cfg_get('turso_token', '').strip()
            if url and token:
                return TursoConnection(url, token)
    except Exception:
        pass
    finally:
        _tl.reading_cfg = False
    return _local_db()


def get_local_db():
    """Retourne toujours une connexion SQLite locale (ignore la config Turso)."""
    return _local_db()


def _ip_sort_key(ip):
    """Pad each IP octet to 3 digits so text sort is correct for IPs."""
    if not ip:
        return ''
    try:
        return '.'.join(f'{int(p):03d}' for p in ip.strip().split('.'))
    except Exception:
        return ip or ''


def _local_db():
    import database as _self
    import os
    import logging

    # Déterminer le chemin de la base de données
    db_path = _self.DATABASE
    if not db_path:
        # Si DATABASE n'est pas initialisé, chercher parc_info.db dans le répertoire courant
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'parc_info.db')
        # Log warning: init_paths() n'a pas été appelée (bug potentiel)
        logger = logging.getLogger('parcinfo')
        logger.warning(f'DATABASE not initialized via init_paths(), using fallback: {db_path}')

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.create_function('ip_sort_key', 1, _ip_sort_key)
    # Augmente le timeout de verrouillage SQLite (5 secondes par défaut)
    conn.execute('PRAGMA busy_timeout = 5000')
    # Activer WAL (Write-Ahead Logging) pour meilleure concurrence
    # WAL permet aux lecteurs et writers de fonctionner en parallèle
    try:
        conn.execute('PRAGMA journal_mode = WAL')
        conn.commit()  # ← CRITICAL: Must commit the PRAGMA change
    except Exception:
        # Si l'activation du WAL échoue, continuer avec le mode par défaut
        pass
    return conn


def row_to_dict(row) -> dict:
    """Convertit une sqlite3.Row / _TRow (ou None) en dict Python."""
    if row is None:
        return {}
    try:
        return dict(row)
    except Exception:
        return {}


# ─── ENCODAGE / DÉCODAGE TURSO ───────────────────────────────────────────────

def _t_enc(v):
    """Encode une valeur Python en argument Turso {"type": ..., "value": ...}."""
    if v is None:
        return {"type": "null", "value": None}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, bytes):
        # L'API Turso libSQL utilise "base64" (pas "value") pour les BLOBs
        return {"type": "blob", "base64": base64.b64encode(v).decode()}
    return {"type": "text", "value": str(v)}


def _t_dec(v):
    """Décode une valeur Turso → type Python natif."""
    if v is None:
        return None
    t = v.get("type", "text")
    if t == "null":
        return None
    if t == "blob":
        # Turso retourne {"type": "blob", "base64": "..."} — clé "base64" pas "value"
        b64 = v.get("base64") or v.get("value")
        if not b64:
            return None
        # Turso omet parfois le padding '=' → compléter avant décodage
        b64 += '=' * ((-len(b64)) % 4)
        return base64.b64decode(b64)
    val = v.get("value")
    if val is None:
        return None
    if t == "integer":
        return int(val)
    if t == "float":
        return float(val)
    return val  # text


# ─── WRAPPER LIGNE ───────────────────────────────────────────────────────────

class _TRow:
    """Émule sqlite3.Row pour les résultats Turso."""
    __slots__ = ('_keys', '_vals')

    def __init__(self, cols, vals):
        self._keys = cols
        self._vals = vals

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return self._vals[self._keys.index(key)]

    def keys(self):
        return list(self._keys)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __repr__(self):
        return str(dict(zip(self._keys, self._vals)))


# ─── WRAPPER CURSEUR ─────────────────────────────────────────────────────────

class _TCursor:
    def __init__(self, cols, rows, last_insert_rowid=None, rows_affected=0):
        self._cols  = cols
        self._rows  = [_TRow(cols, r) for r in rows] if cols else []
        self.lastrowid      = last_insert_rowid
        self.rowcount       = rows_affected
        self._pos = 0

    def fetchone(self):
        if self._pos < len(self._rows):
            r = self._rows[self._pos]; self._pos += 1; return r
        return None

    def fetchall(self):
        r = self._rows[self._pos:]; self._pos = len(self._rows); return r

    def __iter__(self):
        return iter(self._rows)


# ─── CONNEXION TURSO ─────────────────────────────────────────────────────────

class TursoConnection:
    """Connexion Turso cloud via l'API HTTP pipeline libSQL."""

    def __init__(self, url: str, token: str):
        # Turso URLs peuvent être libsql:// ou https:// — l'API HTTP ne supporte que https://
        url = url.rstrip('/')
        url = url.replace('libsql://', 'https://', 1)
        self._url    = url
        self._token  = token
        self.lastrowid  = None
        self.rowcount   = 0

    def _pipeline(self, statements: list) -> list:
        """Envoie une liste de requêtes en un seul appel HTTP. Retourne la liste des résultats."""
        payload = _json.dumps({
            "requests": statements + [{"type": "close"}]
        }).encode()
        req = urllib.request.Request(
            f"{self._url}/v2/pipeline",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read())
        results = data.get("results", [])
        # Last item is the "close" response — ignore it
        return results[: len(statements)]

    def execute(self, sql: str, params=()):
        """Exécute une seule requête et retourne un _TCursor."""
        stmt = {
            "type": "execute",
            "stmt": {
                "sql": sql,
                "args": [_t_enc(p) for p in params],
            }
        }
        results = self._pipeline([stmt])
        cur = self._parse_result(results[0] if results else {})
        # Mémoriser lastrowid/rowcount sur self pour le pattern c = conn.cursor(); c.execute(); c.lastrowid
        self.lastrowid = cur.lastrowid
        self.rowcount  = cur.rowcount
        return cur

    def _parse_result(self, res: dict) -> _TCursor:
        if res.get("type") == "error":
            err = res.get("error", {})
            msg = err.get("message") or err.get("code") or str(err) or "Turso error"
            raise Exception(msg)
        inner = res.get("response", {}).get("result", {})
        cols  = [c["name"] for c in inner.get("cols", [])]
        rows  = [[_t_dec(v) for v in r] for r in inner.get("rows", [])]
        last_id     = inner.get("last_insert_rowid")
        rows_aff    = inner.get("affected_row_count", 0)
        if last_id is not None:
            try: last_id = int(last_id)
            except Exception: last_id = None
        return _TCursor(cols, rows, last_id, rows_aff)

    def pipeline_exec(self, statements: list):
        """Exécute plusieurs requêtes en batch. statements = liste de (sql, params)."""
        reqs = [{
            "type": "execute",
            "stmt": {"sql": s, "args": [_t_enc(p) for p in (par or [])]}
        } for s, par in statements]
        results = self._pipeline(reqs)
        return [self._parse_result(r) for r in results]

    def cursor(self):
        """Retourne self pour compatibilité avec le pattern conn.cursor().execute()."""
        return self

    def executemany(self, sql: str, seq_of_params):
        for params in seq_of_params:
            self.execute(sql, params)

    def commit(self): pass   # auto-commit en Turso HTTP
    def close(self):  pass

    def row_factory(self, *_): pass   # compat shim (non utilisé)


# ─── TEST DE CONNEXION ───────────────────────────────────────────────────────

def test_turso(url: str, token: str):
    """Teste la connexion Turso. Retourne (ok: bool, message: str)."""
    try:
        conn = TursoConnection(url, token)
        conn.execute("SELECT 1")
        return True, "Connexion réussie"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return False, f"URL invalide ou injoignable: {e.reason}"
    except Exception as e:
        return False, str(e)


# ─── MIGRATION ───────────────────────────────────────────────────────────────

_BATCH_SIZE = 150   # lignes par requête pipeline Turso

def migrate_db(source, target):
    """
    Copie toutes les tables de `source` vers `target`.
    source/target sont des connexions (sqlite3.Connection ou TursoConnection).
    Retourne (ok: bool, stats: dict, error: str|None).
    """
    try:
        is_target_turso = isinstance(target, TursoConnection)
        is_source_turso = isinstance(source, TursoConnection)

        # ── 1. Récupérer le schéma depuis source ──────────────────────────
        if is_source_turso:
            cur = source.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        else:
            cur = source.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = [(r[0], r[1]) for r in cur.fetchall() if r[1]]

        stats = {}

        # Désactiver les FK sur la cible pour éviter les erreurs d'ordre
        try:
            target.execute("PRAGMA foreign_keys = OFF")
            if not is_target_turso:
                target.commit()
        except Exception:
            pass

        try:
            for tbl_name, ddl in tables:
                # Rendre le CREATE idempotent
                safe_ddl = ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1)
                target.execute(safe_ddl)
                if not is_target_turso:
                    target.commit()

                # ── Ajouter les colonnes manquantes dans la cible ─────────────
                try:
                    src_cols = {r[1]: r for r in source.execute(f"PRAGMA table_info([{tbl_name}])").fetchall()}
                    tgt_cols = {r[1] for r in target.execute(f"PRAGMA table_info([{tbl_name}])").fetchall()}
                    for col_name, col_info in src_cols.items():
                        if col_name not in tgt_cols:
                            col_type = col_info[2] or 'TEXT'
                            col_dflt = col_info[4]
                            alter_sql = f"ALTER TABLE [{tbl_name}] ADD COLUMN [{col_name}] {col_type}"
                            if col_dflt is not None:
                                alter_sql += f" DEFAULT {col_dflt}"
                            try:
                                target.execute(alter_sql)
                                if not is_target_turso:
                                    target.commit()
                            except Exception:
                                pass
                except Exception:
                    pass

                # ── 2. Lire toutes les lignes de la table source ──────────────
                rows_cur = source.execute(f"SELECT * FROM [{tbl_name}]")
                rows = rows_cur.fetchall()
                if not rows:
                    stats[tbl_name] = 0
                    continue

                cols = rows_cur._cols if isinstance(rows_cur, _TCursor) else [d[0] for d in rows_cur.description] if hasattr(rows_cur, 'description') else list(rows[0].keys())

                placeholders = ','.join(['?'] * len(cols))
                col_list     = ','.join([f'[{c}]' for c in cols])
                sql_insert   = f"INSERT OR REPLACE INTO [{tbl_name}] ({col_list}) VALUES ({placeholders})"

                if is_target_turso:
                    # Batch pipeline
                    all_rows = [list(r) for r in rows]
                    for i in range(0, len(all_rows), _BATCH_SIZE):
                        batch = all_rows[i: i + _BATCH_SIZE]
                        stmts = [(sql_insert, row) for row in batch]
                        target.pipeline_exec(stmts)
                else:
                    # SQLite local — executemany
                    data = [tuple(r) for r in rows]
                    target.executemany(sql_insert, data)
                    target.commit()

                stats[tbl_name] = len(rows)
        finally:
            # Réactiver les FK sur la cible dans tous les cas
            try:
                target.execute("PRAGMA foreign_keys = ON")
                if not is_target_turso:
                    target.commit()
            except Exception:
                pass

        return True, stats, None

    except Exception as e:
        import traceback
        return False, {}, traceback.format_exc()


# ─── SYNCHRONISATION BIDIRECTIONNELLE ────────────────────────────────────────

_sync_state: dict = {
    'last_sync':  None,   # ISO datetime string
    'last_error': None,   # message d'erreur ou None
    'running':    False,  # sync en cours
    'stats':      {},     # {table: {'pushed': n, 'pulled': n}}
}


def get_sync_state() -> dict:
    """Retourne une copie de l'état courant de la synchronisation."""
    return dict(_sync_state)


def sync_once() -> tuple:
    """
    Effectue une synchronisation complète local ↔ Turso.
    Règle de conflit : l'enregistrement avec date_maj la plus récente gagne.
    Retourne (ok: bool, stats: dict, error: str|None).
    """
    from config_helpers import cfg_get
    url   = cfg_get('turso_url',   '').strip()
    token = cfg_get('turso_token', '').strip()
    if not url or not token:
        return False, {}, 'Turso non configuré (URL ou token manquant)'
    if _sync_state['running']:
        return False, {}, 'Synchronisation déjà en cours'

    _sync_state['running'] = True
    try:
        turso = TursoConnection(url, token)
        local = _local_db()
        try:
            _ensure_turso_schema(local, turso)
            stats, errors = _bidirectional_sync(local, turso)
        finally:
            local.commit()
            local.close()

        from datetime import datetime as _dt
        _sync_state['last_sync']  = _dt.now().isoformat(timespec='seconds')
        _sync_state['last_error'] = '; '.join(errors) if errors else None
        _sync_state['stats']      = stats
        return (len(errors) == 0), stats, _sync_state['last_error']
    except Exception as e:
        err = str(e)
        _sync_state['last_error'] = err
        return False, {}, err
    finally:
        _sync_state['running'] = False


def _ensure_turso_schema(local, turso):
    """Crée les tables manquantes et ajoute les colonnes manquantes dans Turso."""
    cur = local.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    for row in cur.fetchall():
        tbl, ddl = row[0], row[1]
        if not ddl:
            continue
        # 1. Créer la table si absente
        safe_ddl = ddl.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)
        try:
            turso.execute(safe_ddl)
        except Exception:
            pass
        # 2. Ajouter les colonnes manquantes (ALTER TABLE ADD COLUMN)
        try:
            local_cols = {r[1]: r for r in local.execute(f"PRAGMA table_info([{tbl}])").fetchall()}
            turso_cols_raw = turso.execute(f"PRAGMA table_info([{tbl}])").fetchall()
            turso_cols = {r[1] for r in turso_cols_raw}
            for col_name, col_info in local_cols.items():
                if col_name not in turso_cols:
                    col_type = col_info[2] or 'TEXT'
                    col_dflt = col_info[4]
                    alter_sql = f"ALTER TABLE [{tbl}] ADD COLUMN [{col_name}] {col_type}"
                    if col_dflt is not None:
                        alter_sql += f" DEFAULT {col_dflt}"
                    try:
                        turso.execute(alter_sql)
                    except Exception:
                        pass
        except Exception:
            pass


def _get_cols(conn, tbl: str) -> list:
    """Retourne les noms de colonnes d'une table via PRAGMA table_info."""
    try:
        cur = conn.execute(f"PRAGMA table_info([{tbl}])")
        rows = cur.fetchall()
        # PRAGMA table_info: cid | name | type | notnull | dflt_value | pk
        cols = [r[1] for r in rows]
        if cols:
            return cols
    except Exception:
        pass
    # Fallback pour Turso ou si PRAGMA échoue : lire une ligne et en déduire les colonnes
    try:
        cur = conn.execute(f"SELECT * FROM [{tbl}] LIMIT 1")
        row = cur.fetchone()
        if row is not None:
            return row.keys() if hasattr(row, 'keys') else list(row._keys)
    except Exception:
        pass
    return []


def _get_user_tables(conn) -> list:
    """Retourne la liste des tables utilisateur (hors tables système SQLite)."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    return [r[0] for r in cur.fetchall()]


def _sync_deletion_log(local, turso):
    """
    Synchronise _sync_deletions bidirectionnellement (merge simple).
    Doit être appelée AVANT la sync des tables de données.
    """
    # Push local → Turso
    try:
        local_dels = local.execute(
            "SELECT tbl, record_id, deleted_at FROM _sync_deletions").fetchall()
        if local_dels:
            sql = "INSERT OR IGNORE INTO _sync_deletions (tbl, record_id, deleted_at) VALUES (?,?,?)"
            stmts = [(sql, [r[0], r[1], r[2]]) for r in local_dels]
            for i in range(0, len(stmts), _BATCH_SIZE):
                turso.pipeline_exec(stmts[i: i + _BATCH_SIZE])
    except Exception:
        pass
    # Pull Turso → local
    try:
        turso_dels = turso.execute(
            "SELECT tbl, record_id, deleted_at FROM _sync_deletions").fetchall()
        if turso_dels:
            local.executemany(
                "INSERT OR IGNORE INTO _sync_deletions (tbl, record_id, deleted_at) VALUES (?,?,?)",
                [(r[0], r[1], r[2]) for r in turso_dels])
            local.commit()
    except Exception:
        pass


def _load_deletion_log(local) -> dict:
    """
    Retourne {table: set(record_ids)} pour toutes les suppressions connues.
    """
    try:
        rows = local.execute("SELECT tbl, record_id FROM _sync_deletions").fetchall()
        result: dict = {}
        for r in rows:
            result.setdefault(r[0], set()).add(r[1])
        return result
    except Exception:
        return {}


def _cleanup_deletion_log(local, turso, days: int = 30):
    """Supprime les entrées de _sync_deletions vieilles de plus de `days` jours."""
    cutoff = f"datetime('now','-{days} days')"
    sql = f"DELETE FROM _sync_deletions WHERE deleted_at < {cutoff}"
    try:
        local.execute(sql); local.commit()
    except Exception:
        pass
    try:
        turso.execute(sql)
    except Exception:
        pass


def _bidirectional_sync(local, turso) -> tuple:
    """
    Synchronise toutes les tables local ↔ Turso avec gestion des suppressions.
    Retourne (stats_dict, errors_list).
    """
    # Désactiver les FK locales pour éviter les erreurs d'ordre lors du pull
    try:
        local.execute("PRAGMA foreign_keys = OFF")
        local.commit()
    except Exception:
        pass

    try:
        # Étape 1 : synchroniser le journal des suppressions en premier
        _sync_deletion_log(local, turso)

        # Étape 2 : charger le journal mergé
        deleted_by_table = _load_deletion_log(local)

        # Étape 3 : synchroniser toutes les tables de données (sauf _sync_deletions)
        tables = [t for t in _get_user_tables(local) if t != '_sync_deletions']
        stats, errors = {}, []
        for tbl in tables:
            try:
                pushed, pulled = _sync_one_table(tbl, local, turso,
                                                 deleted_by_table.get(tbl, set()))
                stats[tbl] = {'pushed': pushed, 'pulled': pulled}
            except Exception as e:
                errors.append(f'{tbl}: {e}')

        # Étape 4 : nettoyage des anciennes suppressions
        try:
            _cleanup_deletion_log(local, turso)
        except Exception:
            pass

        return stats, errors
    finally:
        # Réactiver les FK locales dans tous les cas
        try:
            local.execute("PRAGMA foreign_keys = ON")
            local.commit()
        except Exception:
            pass


def _sync_one_table(tbl: str, local, turso, deleted_ids: set = None) -> tuple:
    """
    Synchronise une table bidirectionnellement en tenant compte du journal
    de suppressions.
    Retourne (pushed_count, pulled_count).
    """
    if deleted_ids is None:
        deleted_ids = set()

    cols = _get_cols(local, tbl)
    if not cols:
        cols = _get_cols(turso, tbl)
    if not cols:
        return 0, 0

    has_id   = 'id'      in cols
    date_col = next((c for c in ('date_maj', 'date') if c in cols), None)

    col_str      = ', '.join(f'[{c}]' for c in cols)
    col_list_br  = ', '.join(f'[{c}]' for c in cols)
    placeholders = ', '.join(['?'] * len(cols))
    sql_replace  = f"INSERT OR REPLACE INTO [{tbl}] ({col_list_br}) VALUES ({placeholders})"

    # ── Lire les deux côtés ───────────────────────────────────────────────
    local_raw = local.execute(f"SELECT {col_str} FROM [{tbl}]").fetchall()
    try:
        turso_raw = turso.execute(f"SELECT {col_str} FROM [{tbl}]").fetchall()
    except Exception:
        turso_raw = []   # table absente sur Turso

    push_list: list = []        # local → Turso (upsert)
    pull_list: list = []        # Turso → local (upsert)
    del_from_turso: list = []   # IDs à supprimer sur Turso
    del_from_local: list = []   # IDs à supprimer en local

    if has_id:
        id_idx    = cols.index('id')
        local_map = {r[id_idx]: list(r) for r in local_raw}
        turso_map = {r[id_idx]: list(r) for r in turso_raw}
        all_ids   = set(local_map) | set(turso_map)

        for rid in all_ids:
            lr = local_map.get(rid)
            tr = turso_map.get(rid)

            if lr is not None and tr is None:
                # Existe en local, absent de Turso
                if rid in deleted_ids:
                    # Supprimé sur une autre machine et propagé → supprimer en local
                    del_from_local.append(rid)
                else:
                    # Nouveau en local → pousser vers Turso
                    push_list.append(lr)

            elif tr is not None and lr is None:
                # Existe sur Turso, absent en local
                if rid in deleted_ids:
                    # Supprimé localement → propager la suppression à Turso
                    del_from_turso.append(rid)
                else:
                    # Nouveau sur Turso (autre machine) → tirer en local
                    pull_list.append(tr)

            elif lr is not None and tr is not None and date_col:
                # Existe des deux côtés → conflit résolu par date_maj
                d_idx = cols.index(date_col)
                ld    = str(lr[d_idx] or '')
                td    = str(tr[d_idx] or '')
                if ld > td:
                    push_list.append(lr)
                elif td > ld:
                    pull_list.append(tr)
    else:
        # Tables sans colonne id : INSERT OR IGNORE
        sql_replace = f"INSERT OR IGNORE INTO [{tbl}] ({col_list_br}) VALUES ({placeholders})"
        push_list = [list(r) for r in local_raw]
        pull_list = [list(r) for r in turso_raw]

    # ── Push local → Turso (upsert) ──────────────────────────────────────
    if push_list:
        stmts = [(sql_replace, row) for row in push_list]
        for i in range(0, len(stmts), _BATCH_SIZE):
            turso.pipeline_exec(stmts[i: i + _BATCH_SIZE])

    # ── Pull Turso → local (upsert) ──────────────────────────────────────
    if pull_list:
        local.executemany(sql_replace, pull_list)
        local.commit()

    # ── Propager suppressions locales → Turso ────────────────────────────
    if del_from_turso and has_id:
        del_stmts = [(f"DELETE FROM [{tbl}] WHERE id=?", [rid])
                     for rid in del_from_turso]
        for i in range(0, len(del_stmts), _BATCH_SIZE):
            try:
                turso.pipeline_exec(del_stmts[i: i + _BATCH_SIZE])
            except Exception:
                pass

    # ── Appliquer suppressions distantes → local ─────────────────────────
    if del_from_local and has_id:
        for rid in del_from_local:
            try:
                local.execute(f"DELETE FROM [{tbl}] WHERE id=?", (rid,))
            except Exception:
                pass
        local.commit()

    return len(push_list), len(pull_list)
