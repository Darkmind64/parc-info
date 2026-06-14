"""
uploads_sync.py — Synchronisation des fichiers uploads entre machines via Turso.

Architecture (3 modes db_type possibles) :
  - 'local' : SQLite locale uniquement, pas de Turso → sync désactivée.
  - 'turso' : Turso en DB principale. Upload INSERT va dans Turso (contenu_blob=NULL).
              Push lit le fichier sur disque et envoie le BLOB dans Turso.
  - 'sync'  : SQLite locale + sync bidirectionnelle locale↔Turso toutes les 30s.
              Upload INSERT va dans SQLite locale, puis sync_once() le pousse vers Turso.
              Push lit le fichier sur disque et envoie le BLOB dans Turso.

Flux complet (PC → NAS) :
  1. Upload sur PC  → fichier sur disque, record DB (contenu_blob=NULL)
  2. Push sur PC    → lit fichier, UPDATE Turso avec le BLOB
  3. Pull sur NAS   → récupère le BLOB depuis Turso, écrit le fichier sur disque

IMPORTANT : get_turso_db() se connecte si url+token sont configurés, INDÉPENDAMMENT
du db_type. C'est différent de get_db() qui retourne local SQLite en mode 'sync'.

Lancé en thread background au démarrage de app.py.
"""

import logging
import time
import os
import urllib.error
from datetime import datetime

logger = logging.getLogger('parcinfo')


def _get_turso_connection(timeout: int = 60):
    """
    Retourne une TursoConnection persistante (keep-alive) si Turso est configuré.
    Timeout 60s pour les transferts de BLOBs (vs 15s pour le sync DB).
    La connexion HTTPS est réutilisée entre les requêtes → 1 seule résolution DNS
    par cycle au lieu d'une par table (6+), ce qui évite de saturer le DNS Docker
    sur Synology et d'interférer avec Hyper Backup.
    """
    try:
        from config_helpers import cfg_get
        url   = cfg_get('turso_url',   '').strip()
        token = cfg_get('turso_token', '').strip()
        if url and token:
            from database import TursoConnection
            return TursoConnection(url, token, timeout=timeout)
    except Exception as e:
        logger.warning(f"uploads_sync: impossible de créer la connexion Turso : {e}")
    return None


def _push_documents_to_turso(table_name: str, upload_folder: str, turso=None) -> None:
    """
    Push local → Turso : pour chaque record Turso avec contenu_blob=NULL,
    si le fichier physique existe sur CETTE machine → lit et envoie le BLOB.

    Note : interroge Turso directement (pas la SQLite locale) car :
    - mode 'turso'  : l'INSERT est allé dans Turso, local SQLite est vide
    - mode 'sync'   : sync_once() a déjà poussé le record vers Turso avant l'appel

    turso : connexion partagée optionnelle (évite de créer une connexion par table)
    """
    _own_conn = turso is None
    try:
        if _own_conn:
            turso = _get_turso_connection()
        if not turso:
            return  # Turso non configuré → rien à faire

        to_push = turso.execute(f'''
            SELECT id, nom_fichier FROM {table_name}
            WHERE contenu_blob IS NULL
              AND nom_fichier IS NOT NULL
              AND nom_fichier != ''
        ''').fetchall()

        if not to_push:
            return

        now   = datetime.now().isoformat()
        count = 0

        for row in to_push:
            doc_id      = row['id']
            nom_fichier = row['nom_fichier']
            local_path  = os.path.join(upload_folder, nom_fichier)

            if not os.path.exists(local_path):
                # Ce fichier n'est pas sur cette machine — ignoré
                continue

            try:
                with open(local_path, 'rb') as f:
                    blob = f.read()

                turso.execute(f'''
                    UPDATE {table_name}
                    SET contenu_blob=?, sync_status='synced', date_sync=?
                    WHERE id=?
                ''', (blob, now, doc_id))

                count += 1
                logger.debug(
                    f"push {table_name} id={doc_id}: {nom_fichier} "
                    f"({len(blob):,} octets) → Turso"
                )
            except urllib.error.HTTPError as e:
                body = e.read().decode('utf-8', errors='replace')
                logger.warning(
                    f"push {table_name} id={doc_id} ({nom_fichier}, {len(blob):,}B): "
                    f"HTTP {e.code} — {body[:300]}"
                )
            except Exception as e:
                logger.warning(f"push {table_name} id={doc_id} ({nom_fichier}): ERREUR : {e}")

        if count:
            logger.info(f"uploads_sync push: {count} fichier(s) envoyé(s) vers Turso ({table_name})")

    except Exception as e:
        logger.exception(f"_push_documents_to_turso({table_name}) a échoué")
    finally:
        if _own_conn and turso:
            turso.close()


def _pull_documents_from_turso(table_name: str, upload_folder: str, turso=None) -> None:
    """
    Pull Turso → local : télécharge depuis Turso les fichiers absents sur cette machine.

    Fetch en deux temps pour éviter de télécharger tous les BLOBs en une seule
    requête HTTP (timeout inévitable si plusieurs gros fichiers existent dans Turso).

    turso : connexion partagée optionnelle (évite de créer une connexion par table)
    """
    _own_conn = turso is None
    try:
        if _own_conn:
            turso = _get_turso_connection()
        if not turso:
            return

        # Étape 1 : récupérer uniquement les IDs + noms (pas les BLOBs)
        meta_rows = turso.execute(f'''
            SELECT id, nom_fichier FROM {table_name}
            WHERE contenu_blob IS NOT NULL
              AND nom_fichier IS NOT NULL AND nom_fichier != ''
        ''').fetchall()

        if not meta_rows:
            return

        os.makedirs(upload_folder, exist_ok=True)
        count = 0

        for meta in meta_rows:
            doc_id      = meta[0]        # id
            nom_fichier = meta[1]        # nom_fichier

            if not nom_fichier:
                continue

            local_path = os.path.join(upload_folder, nom_fichier)
            if os.path.exists(local_path):
                continue  # déjà présent localement

            # Étape 2 : télécharger le BLOB individuellement
            try:
                blob_row = turso.execute(
                    f'SELECT contenu_blob FROM {table_name} WHERE id=?', (doc_id,)
                ).fetchone()
                if not blob_row:
                    continue
                blob = blob_row[0]
                if not blob:
                    continue

                with open(local_path, 'wb') as f:
                    f.write(blob)
                count += 1
                logger.debug(
                    f"pull {table_name} id={doc_id}: {nom_fichier} "
                    f"({len(blob):,} octets) ← Turso"
                )
            except urllib.error.HTTPError as e:
                body = e.read().decode('utf-8', errors='replace')
                logger.warning(
                    f"pull {table_name} id={doc_id} ({nom_fichier}): "
                    f"HTTP {e.code} — {body[:300]}"
                )
            except Exception as e:
                logger.warning(f"pull {table_name} id={doc_id} ({nom_fichier}): ERREUR : {e}")

        if count:
            logger.info(f"uploads_sync pull: {count} fichier(s) récupéré(s) depuis Turso ({table_name})")

    except Exception:
        logger.exception(f"_pull_documents_from_turso({table_name}) a échoué")
    finally:
        if _own_conn and turso:
            turso.close()


def _cleanup_orphaned_files(upload_folder: str) -> None:
    """
    Supprime les fichiers physiques dont le record DB a été supprimé.

    Collecte tous les nom_fichier référencés dans la DB locale (source de
    vérité sur cette machine), puis supprime du disque tout fichier du
    dossier uploads qui n'est plus référencé.

    Sécurité : ne touche qu'aux fichiers dont le nom commence par un préfixe
    connu de l'app (app, per, ctr, intv, baie) pour ne pas effacer des
    fichiers déposés manuellement.
    """
    # Préfixes générés par les routes upload de l'app
    APP_PREFIXES = ('app', 'per', 'ctr', 'intv', 'baie')

    try:
        if not os.path.isdir(upload_folder):
            return

        from database import get_db
        conn = get_db()

        # Collecter tous les noms de fichiers encore référencés dans la DB
        referenced: set = set()
        tables = [
            'documents_appareils',
            'documents_contrats',
            'documents_peripheriques',
        ]
        for tbl in tables:
            try:
                rows = conn.execute(
                    f'SELECT nom_fichier FROM {tbl} WHERE nom_fichier IS NOT NULL'
                ).fetchall()
                for r in rows:
                    if r['nom_fichier']:
                        referenced.add(r['nom_fichier'])
            except Exception:
                pass
        conn.close()

        count = 0
        for fname in os.listdir(upload_folder):
            # Ne toucher qu'aux fichiers uploadés par l'app
            if not fname.startswith(APP_PREFIXES):
                continue
            if fname in referenced:
                continue
            fpath = os.path.join(upload_folder, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                os.remove(fpath)
                count += 1
                logger.debug(f"cleanup: supprimé fichier orphelin : {fname}")
            except Exception as e:
                logger.warning(f"cleanup: impossible de supprimer {fname} : {e}")

        if count:
            logger.info(f"uploads_sync cleanup: {count} fichier(s) orphelin(s) supprimé(s)")

    except Exception:
        logger.exception("_cleanup_orphaned_files a échoué")


def sync_uploads() -> None:
    """Cycle complet push + pull + cleanup pour toutes les tables documents.

    Une seule TursoConnection est créée et partagée entre toutes les opérations
    pour éviter de multiples résolutions DNS consécutives (6 par cycle).
    Sur Synology sous Docker, les DNS lookups répétés saturent le resolver
    embarqué et perturbent les autres services réseau (ex. Hyper Backup).
    """
    from database import UPLOAD_FOLDER

    tables = [
        'documents_appareils',
        'documents_contrats',
        'documents_peripheriques',
    ]

    # Créer une seule connexion partagée pour tout le cycle
    turso = _get_turso_connection()

    try:
        for table in tables:
            _push_documents_to_turso(table, UPLOAD_FOLDER, turso=turso)
            _pull_documents_from_turso(table, UPLOAD_FOLDER, turso=turso)
    finally:
        if turso:
            turso.close()

    # Supprimer les fichiers physiques dont le record a été supprimé
    _cleanup_orphaned_files(UPLOAD_FOLDER)


def start_sync_thread(interval: int = 60) -> None:
    """Lance un thread daemon qui appelle sync_uploads() toutes les `interval` secondes."""
    import threading

    def _loop():
        logger.info(f"uploads_sync: thread démarré (intervalle={interval}s)")
        # Attendre que l'app soit prête avant le premier cycle
        time.sleep(interval)
        while True:
            try:
                logger.debug("uploads_sync: début du cycle de synchronisation")
                sync_uploads()
            except Exception:
                logger.exception("uploads_sync: erreur dans la boucle principale")
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name='uploads-sync')
    t.start()
    logger.info("uploads_sync: thread lancé")
