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


def _get_turso_connection():
    """
    Retourne une TursoConnection si url+token sont configurés, None sinon.
    Fonctionne pour tous les modes (local, turso, sync).
    """
    try:
        from config_helpers import cfg_get
        url   = cfg_get('turso_url',   '').strip()
        token = cfg_get('turso_token', '').strip()
        if url and token:
            from database import TursoConnection
            return TursoConnection(url, token)
    except Exception as e:
        logger.warning(f"uploads_sync: impossible de créer la connexion Turso : {e}")
    return None


def _push_documents_to_turso(table_name: str, upload_folder: str) -> None:
    """
    Push local → Turso : pour chaque record Turso avec contenu_blob=NULL,
    si le fichier physique existe sur CETTE machine → lit et envoie le BLOB.

    Note : interroge Turso directement (pas la SQLite locale) car :
    - mode 'turso'  : l'INSERT est allé dans Turso, local SQLite est vide
    - mode 'sync'   : sync_once() a déjà poussé le record vers Turso avant l'appel
    """
    try:
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


def _pull_documents_from_turso(table_name: str, upload_folder: str) -> None:
    """
    Pull Turso → local : télécharge depuis Turso les fichiers absents sur cette machine.
    """
    try:
        turso = _get_turso_connection()
        if not turso:
            return

        remote_docs = turso.execute(f'''
            SELECT id, nom_fichier, contenu_blob FROM {table_name}
            WHERE contenu_blob IS NOT NULL
        ''').fetchall()

        count = 0

        for row in remote_docs:
            nom_fichier = row['nom_fichier']
            blob        = row['contenu_blob']

            if not nom_fichier or not blob:
                continue

            local_path = os.path.join(upload_folder, nom_fichier)
            if os.path.exists(local_path):
                continue  # déjà présent localement

            try:
                os.makedirs(upload_folder, exist_ok=True)
                with open(local_path, 'wb') as f:
                    f.write(blob)
                count += 1
                logger.debug(
                    f"pull {table_name} id={row['id']}: {nom_fichier} "
                    f"({len(blob):,} octets) ← Turso"
                )
            except Exception as e:
                logger.warning(f"pull {table_name} id={row['id']}: ERREUR écriture {local_path} : {e}")

        if count:
            logger.info(f"uploads_sync pull: {count} fichier(s) récupéré(s) depuis Turso ({table_name})")

    except Exception as e:
        logger.exception(f"_pull_documents_from_turso({table_name}) a échoué")


def sync_uploads() -> None:
    """Cycle complet push + pull pour toutes les tables documents."""
    from database import UPLOAD_FOLDER

    tables = [
        'documents_appareils',
        'documents_contrats',
        'documents_peripheriques',
    ]

    for table in tables:
        _push_documents_to_turso(table, UPLOAD_FOLDER)
        _pull_documents_from_turso(table, UPLOAD_FOLDER)


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
