"""
database.py — Connexion SQLite / Turso et utilitaires bas niveau.
DATABASE et UPLOAD_FOLDER sont initialisés par app.py au démarrage.
"""
import sqlite3, threading, json as _json, urllib.request, urllib.error, base64, http.client
from urllib.parse import urlparse

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
    """Connexion Turso cloud via l'API HTTP pipeline libSQL.

    Utilise une connexion HTTPS persistante (keep-alive) pour éviter une
    résolution DNS par requête — important sur Synology où les requêtes DNS
    répétées depuis Docker perturbent le réseau hôte (Hyper Backup, etc).
    """

    def __init__(self, url: str, token: str, timeout: int = 15):
        # Turso URLs peuvent être libsql:// ou https:// — l'API HTTP ne supporte que https://
        url = url.rstrip('/')
        url = url.replace('libsql://', 'https://', 1)
        self._url     = url
        self._token   = token
        self._timeout = timeout
        parsed        = urlparse(url)
        self._host    = parsed.netloc
        self._conn: http.client.HTTPSConnection | None = None
        self.lastrowid  = None
        self.rowcount   = 0

    def _get_conn(self) -> http.client.HTTPSConnection:
        """Retourne la connexion persistante, en la recréant si nécessaire."""
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(self._host, timeout=self._timeout)
        return self._conn

    def close(self):
        """Ferme la connexion persistante."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _pipeline(self, statements: list) -> list:
        """Envoie une liste de requêtes en un seul appel HTTP persistant."""
        payload = _json.dumps({
            "requests": statements + [{"type": "close"}]
        }).encode()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        conn = self._get_conn()
        try:
            conn.request("POST", "/v2/pipeline", body=payload, headers=headers)
            resp = conn.getresponse()
            data = _json.loads(resp.read())
        except Exception:
            # Connexion brisée → reset pour le prochain appel
            self.close()
            raise
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
        """Exécute plusieurs requêtes en batch. statements = liste de (sql, params).

        PRAGMA foreign_keys = OFF est inclus en tête du batch : chaque appel HTTP
        Turso ouvre une connexion indépendante, donc le PRAGMA doit être dans le même
        pipeline pour éviter les erreurs FK sur les tables pivot (contrats_appareils, etc.)
        dont les enregistrements orphelins existent en local (SQLite FK désactivées).
        """
        _fk_off = {"type": "execute", "stmt": {"sql": "PRAGMA foreign_keys = OFF", "args": []}}
        reqs = [_fk_off] + [{
            "type": "execute",
            "stmt": {"sql": s, "args": [_t_enc(p) for p in (par or [])]}
        } for s, par in statements]
        results = self._pipeline(reqs)
        # Ignorer le résultat du PRAGMA (premier élément)
        return [self._parse_result(r) for r in results[1:]]

    def cursor(self):
        """Retourne self pour compatibilité avec le pattern conn.cursor().execute()."""
        return self

    def executemany(self, sql: str, seq_of_params):
        for params in seq_of_params:
            self.execute(sql, params)

    def commit(self): pass   # auto-commit en Turso HTTP

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


def log_sync_event(event_type: str, statut: str, resume: str, details: dict = None) -> None:
    """Enregistre un événement dans le journal de synchronisation (visible dans l'UI).

    event_type : 'db_sync' (réplication des lignes) ou 'fichiers' (push/pull des BLOBs)
    statut     : 'succes' | 'erreur' | 'partiel'
    Conserve seulement les 500 entrées les plus récentes (purge automatique).
    """
    try:
        from datetime import datetime as _dt
        conn = _local_db()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS journal_synchronisation ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, horodatage TEXT NOT NULL, "
            "type TEXT NOT NULL, statut TEXT NOT NULL, resume TEXT DEFAULT '', "
            "details TEXT DEFAULT '')"
        )
        details_json = _json.dumps(details, ensure_ascii=False) if details else ''
        conn.execute(
            "INSERT INTO journal_synchronisation (horodatage, type, statut, resume, details) VALUES (?,?,?,?,?)",
            (_dt.utcnow().isoformat(), event_type, statut, resume, details_json)
        )
        conn.execute(
            "DELETE FROM journal_synchronisation WHERE id NOT IN "
            "(SELECT id FROM journal_synchronisation ORDER BY id DESC LIMIT 500)"
        )
        conn.commit()
        conn.close()
    except Exception:
        import logging as _logging
        _logging.getLogger('parcinfo').exception("log_sync_event a échoué (non-critique)")


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

        from datetime import datetime as _dt, timezone as _tz
        # UTC + suffixe 'Z' explicite : sans indicateur de fuseau, le JS qui calcule
        # "il y a Xh" (templates/base.html:_syncRelTime) interprète la chaîne comme
        # une heure LOCALE AU NAVIGATEUR, alors qu'elle était l'heure locale du
        # serveur — un décalage silencieux dès que serveur et navigateur ne sont pas
        # dans le même fuseau (ex: serveur en UTC dans Docker, navigateur à Paris).
        _sync_state['last_sync'] = _dt.now(_tz.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        _sync_state['last_error'] = '; '.join(errors) if errors else None
        _sync_state['stats']      = stats

        total = sum((v.get('pushed', 0) + v.get('pulled', 0)) for v in stats.values()) if stats else 0
        if errors:
            log_sync_event('db_sync', 'erreur' if total == 0 else 'partiel',
                            f"{total} enregistrement(s) - {len(errors)} erreur(s)",
                            {'stats': stats, 'errors': errors})
        elif total:
            log_sync_event('db_sync', 'succes', f"{total} enregistrement(s) synchronisé(s)", {'stats': stats})
        # Rien à synchroniser (total=0, pas d'erreur) : pas d'entrée pour ne pas bruiter le journal

        return (len(errors) == 0), stats, _sync_state['last_error']
    except Exception as e:
        err = str(e)
        _sync_state['last_error'] = err
        log_sync_event('db_sync', 'erreur', 'Échec de synchronisation', {'error': err})
        return False, {}, err
    finally:
        _sync_state['running'] = False


def _ensure_turso_schema(local, turso):
    """Crée les tables, colonnes et triggers manquants dans Turso."""
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

    # 3. Répliquer les triggers _trg_journal_* sur Turso : indispensable pour que
    #    Turso tienne son propre _sync_journal, alimenté par les écritures de
    #    TOUTES les instances (pas seulement celle qui exécute ce cycle de sync).
    #    C'est ce journal Turso que chaque instance relit ensuite (pull) pour
    #    récupérer les changements faits ailleurs.
    try:
        trig_cur = local.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE '_trg_journal_%'")
        for row in trig_cur.fetchall():
            ddl = row[1]
            if not ddl:
                continue
            safe_ddl = ddl.replace('CREATE TRIGGER', 'CREATE TRIGGER IF NOT EXISTS', 1)
            try:
                turso.execute(safe_ddl)
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


def _cleanup_deletion_log(local, turso, days: int = 30):
    """Supprime les entrées de _sync_deletions vieilles de plus de `days` jours.

    Table legacy conservée pour compatibilité (encore alimentée par les triggers
    _trg_del_* dans app.py) mais non utilisée par la sync actuelle, basée sur
    _sync_journal (voir _sync_using_journal ci-dessous)."""
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


def _cleanup_sync_journal(turso, days: int = 30):
    """Purge les entrées _sync_journal de Turso vieilles de plus de `days` jours.

    Borne la croissance du journal partagé. Suppose qu'aucune instance ne reste
    hors-ligne plus de `days` jours sans se resynchroniser (sinon elle manquera
    les changements les plus anciens et nécessitera une resync complète manuelle).
    """
    cutoff = f"datetime('now','-{days} days')"
    try:
        turso.execute(f"DELETE FROM _sync_journal WHERE timestamp < {cutoff}")
    except Exception:
        pass


_BLOB_SYNC_EXCLUDE = frozenset({'contenu_blob'})


def _pk_column(conn, tbl: str) -> str:
    """Retourne le nom de la colonne clé primaire d'une table (fallback 'id').

    Toujours résolu depuis `local` (jamais Turso : PRAGMA table_info via l'API
    HTTP Turso n'est pas fiable), le schéma Turso étant un miroir du schéma local.
    """
    try:
        info = conn.execute(f"PRAGMA table_info([{tbl}])").fetchall()
        for row in info:
            if row[5] == 1:   # row[5] = pk flag
                return row[1]
    except Exception:
        pass
    return 'id'


def _apply_table_changes(source, target, tbl: str, pk_col: str, changes: dict):
    """
    Applique à `target` les changements (INSERT/UPDATE/DELETE) d'une table lus
    depuis `source`. `changes` = {'INSERT': set(ids), 'UPDATE': set(ids), 'DELETE': set(ids)}.

    Utilisé dans les deux sens : push (source=local, target=turso) et
    pull (source=turso, target=local). Toute exception réseau remonte à
    l'appelant (qui décide de retenter au prochain cycle plutôt que de
    perdre silencieusement la modification).
    """
    all_cols = _get_cols(source, tbl)
    if not all_cols:
        all_cols = _get_cols(target, tbl)
    if not all_cols:
        return

    blob_excluded = [c for c in all_cols if c in _BLOB_SYNC_EXCLUDE]
    cols = [c for c in all_cols if c not in _BLOB_SYNC_EXCLUDE]
    col_list_br  = ', '.join(f'[{c}]' for c in cols)
    placeholders = ', '.join(['?'] * len(cols))
    has_pk = pk_col in cols
    to_turso = isinstance(target, TursoConnection)

    upsert_ids = changes.get('INSERT', set()) | changes.get('UPDATE', set())
    if upsert_ids:
        rows = source.execute(
            f"SELECT {col_list_br} FROM [{tbl}] WHERE [{pk_col}] IN "
            f"({','.join('?' * len(upsert_ids))})",
            list(upsert_ids)
        ).fetchall()

        if rows:
            if has_pk and blob_excluded:
                # ON CONFLICT DO UPDATE pour préserver les BLOBs déjà présents côté cible
                non_pk_cols = [c for c in cols if c != pk_col]
                set_clause = ', '.join(f'[{c}]=excluded.[{c}]' for c in non_pk_cols)
                sql_upsert = (
                    f"INSERT INTO [{tbl}] ({col_list_br}) VALUES ({placeholders})"
                    f" ON CONFLICT([{pk_col}]) DO UPDATE SET {set_clause}"
                )
            else:
                sql_upsert = f"INSERT OR REPLACE INTO [{tbl}] ({col_list_br}) VALUES ({placeholders})"

            if to_turso:
                stmts = [(sql_upsert, list(r)) for r in rows]
                for i in range(0, len(stmts), _BATCH_SIZE):
                    target.pipeline_exec(stmts[i: i + _BATCH_SIZE])
            else:
                target.executemany(sql_upsert, [list(r) for r in rows])
                target.commit()

    del_ids = changes.get('DELETE', set())
    if del_ids and has_pk:
        if to_turso:
            del_stmts = [(f"DELETE FROM [{tbl}] WHERE [{pk_col}]=?", [rid]) for rid in del_ids]
            for i in range(0, len(del_stmts), _BATCH_SIZE):
                target.pipeline_exec(del_stmts[i: i + _BATCH_SIZE])
        else:
            for rid in del_ids:
                target.execute(f"DELETE FROM [{tbl}] WHERE [{pk_col}]=?", (rid,))
            target.commit()


def _sync_using_journal(local, turso) -> tuple:
    """
    Synchronise en utilisant le journal de modifications _sync_journal, dans les
    DEUX sens :

    - PUSH (local → Turso) : les entrées du journal LOCAL sont envoyées à Turso.
      Seules les entrées effectivement poussées avec succès sont retirées du
      journal local — une erreur réseau ne fait donc pas perdre la modification,
      elle est retentée au cycle suivant.

    - PULL (Turso → local) : Turso tient son PROPRE _sync_journal, alimenté par
      les mêmes triggers (répliqués sur Turso par _ensure_turso_schema) qui se
      déclenchent quand N'IMPORTE QUELLE instance y écrit. Cette instance retient
      un curseur local (_sync_meta.last_pulled_journal_id) et ne relit que les
      entrées Turso plus récentes que ce curseur — peu importe qui les a produites.

    Avec < 10 modifications/jour, ceci réduit les reads/writes Turso de ~99%
    tout en synchronisant réellement toutes les instances entre elles.
    Retourne (stats_dict, errors_list).
    """
    local.execute("""CREATE TABLE IF NOT EXISTS _sync_meta (
        key   TEXT PRIMARY KEY,
        value TEXT)""")
    local.execute("CREATE TABLE IF NOT EXISTS _sync_applying (id INTEGER PRIMARY KEY)")
    local.commit()

    stats, errors = {}, []

    # ── PUSH : journal local → Turso ──────────────────────────────────────
    try:
        journal_entries = local.execute(
            "SELECT id, tbl, record_id, action FROM _sync_journal ORDER BY id"
        ).fetchall()
    except Exception:
        journal_entries = []

    if journal_entries:
        by_table: dict = {}
        for jid, tbl, record_id, action in journal_entries:
            g = by_table.setdefault(tbl, {'INSERT': set(), 'UPDATE': set(), 'DELETE': set(), 'jids': []})
            g[action].add(record_id)
            g['jids'].append(jid)

        for tbl, changes in by_table.items():
            try:
                pk_col = _pk_column(local, tbl)
                _apply_table_changes(local, turso, tbl, pk_col, changes)
                # Succès uniquement : purger les entrées de journal traitées pour cette table
                jids = changes['jids']
                for i in range(0, len(jids), _BATCH_SIZE):
                    chunk = jids[i:i + _BATCH_SIZE]
                    local.execute(
                        f"DELETE FROM _sync_journal WHERE id IN ({','.join('?' * len(chunk))})",
                        chunk)
                local.commit()
                stats.setdefault(tbl, {})['pushed'] = len(changes['INSERT'] | changes['UPDATE'])
                stats[tbl]['pushed_deletes'] = len(changes['DELETE'])
            except Exception as e:
                # Échec (réseau/Turso) : on NE supprime PAS ces entrées → retentées au prochain cycle
                errors.append(f'push {tbl}: {e}')

    # ── PULL : journal Turso (toutes instances) → local ──────────────────
    try:
        row = local.execute(
            "SELECT value FROM _sync_meta WHERE key='last_pulled_journal_id'").fetchone()
        last_pulled = int(row[0]) if row and row[0] else 0
    except Exception:
        last_pulled = 0

    try:
        remote_entries = turso.execute(
            "SELECT id, tbl, record_id, action FROM _sync_journal WHERE id > ? ORDER BY id",
            (last_pulled,)
        ).fetchall()
    except Exception:
        remote_entries = []

    if remote_entries:
        by_table = {}
        max_id = last_pulled
        for rid, tbl, record_id, action in remote_entries:
            g = by_table.setdefault(tbl, {'INSERT': set(), 'UPDATE': set(), 'DELETE': set()})
            g[action].add(record_id)
            max_id = max(max_id, rid)

        # Garde anti-rebouclage : le temps d'appliquer le pull, les triggers locaux
        # _trg_journal_* ne doivent pas se redéclencher (sinon la donnée qu'on vient
        # de recevoir est aussitôt re-marquée "modifiée localement", et un futur push
        # la renverrait — avec un état potentiellement périmé si entre-temps la ligne
        # est réécrite — ce qui écraserait silencieusement les changements distants).
        local.execute("INSERT OR IGNORE INTO _sync_applying (id) VALUES (1)")
        local.commit()
        try:
            for tbl, changes in by_table.items():
                try:
                    pk_col = _pk_column(local, tbl)
                    _apply_table_changes(turso, local, tbl, pk_col, changes)
                    stats.setdefault(tbl, {})['pulled'] = len(changes['INSERT'] | changes['UPDATE'])
                    stats[tbl]['pulled_deletes'] = len(changes['DELETE'])
                except Exception as e:
                    errors.append(f'pull {tbl}: {e}')
        finally:
            local.execute("DELETE FROM _sync_applying")
            local.commit()

        # Le curseur avance même si certaines tables ont échoué : ces erreurs sont
        # rares (table renommée/supprimée) et une nouvelle tentative indéfinie sur
        # la même entrée n'apporterait rien ; elles sont visibles dans errors/logs.
        try:
            local.execute(
                "INSERT INTO _sync_meta (key, value) VALUES ('last_pulled_journal_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(max_id),))
            local.commit()
        except Exception:
            pass

    return stats, errors


def _bidirectional_sync(local, turso) -> tuple:
    """
    Synchronise toutes les tables via le journal de modifications, dans les deux
    sens (push local→Turso, pull Turso→local — voir _sync_using_journal).
    Retourne (stats_dict, errors_list).
    """
    # Désactiver les FK locales pour éviter les erreurs d'ordre lors du pull
    try:
        local.execute("PRAGMA foreign_keys = OFF")
        local.commit()
    except Exception:
        pass

    try:
        stats, errors = _sync_using_journal(local, turso)

        try:
            _cleanup_deletion_log(local, turso)
            _cleanup_sync_journal(turso)
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
