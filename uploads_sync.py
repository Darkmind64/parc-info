"""
uploads_sync.py — Synchronisation des fichiers uploads entre SQLite local et Turso.

Gère:
- Push: fichiers locaux → Turso (lecture disque en arrière-plan, non bloquant)
- Pull: récupération depuis Turso si fichier manquant localement
- Nettoyage automatique des fichiers supprimés

Lancé en thread background au démarrage de app.py.
"""

import logging
import time
import os
import sqlite3
from datetime import datetime

logger = logging.getLogger('parcinfo')

def get_db():
    from database import get_db as _get_db
    return _get_db()

def get_local_db():
    from database import get_local_db as _get_local_db
    return _get_local_db()

def get_turso_db():
    """Récupère la connexion Turso (ou None si non configurée)."""
    try:
        from config_helpers import cfg_get
        db_type = cfg_get('db_type', 'local')
        if db_type == 'turso':
            from database import TursoConnection
            url = cfg_get('turso_url', '').strip()
            token = cfg_get('turso_token', '').strip()
            if url and token:
                return TursoConnection(url, token)
    except Exception as e:
        logger.warning(f"Cannot create Turso connection for uploads_sync: {e}")
    return None


def _push_documents_to_turso(table_name: str, upload_folder: str):
    """
    Push local → Turso : envoie les fichiers non-synced (sync_status='local').

    Lit le contenu depuis le disque (non-bloquant pour l'upload HTTP).
    Ne pousse que si le fichier physique existe localement.
    """
    try:
        local_db = get_local_db()
        turso_db = get_turso_db()

        if not turso_db:
            local_db.close()
            return

        # Récupérer les documents non-syncs — avec ou sans BLOB (le BLOB sera lu depuis le disque)
        unsync = local_db.execute(f'''
            SELECT id, nom_fichier FROM {table_name}
            WHERE sync_status='local'
        ''').fetchall()

        if not unsync:
            local_db.close()
            turso_db.close()
            return

        now = datetime.now().isoformat()
        count = 0

        for row in unsync:
            doc_id = row['id']
            nom_fichier = row['nom_fichier']

            if not nom_fichier:
                continue

            local_path = os.path.join(upload_folder, nom_fichier)
            if not os.path.exists(local_path):
                logger.warning(f"Fichier manquant pour sync: {local_path}")
                continue

            try:
                with open(local_path, 'rb') as f:
                    blob = f.read()

                # Upsert dans Turso avec le contenu
                turso_db.execute(f'''
                    UPDATE {table_name}
                    SET contenu_blob=?, sync_status='synced', date_sync=?
                    WHERE id=?
                ''', (blob, now, doc_id))

                # Marquer comme synced localement
                local_db.execute(f'''
                    UPDATE {table_name}
                    SET sync_status='synced', date_sync=?
                    WHERE id=?
                ''', (now, doc_id))

                count += 1
            except Exception as e:
                logger.warning(f"Error syncing {table_name} id={doc_id}: {e}")

        turso_db.commit()
        local_db.commit()

        if count > 0:
            logger.info(f"Pushed {count} documents to Turso ({table_name})")

        local_db.close()
        turso_db.close()

    except Exception as e:
        logger.exception(f"Error in _push_documents_to_turso({table_name})")


def _pull_documents_from_turso(table_name: str, upload_folder: str):
    """
    Pull Turso → local : récupère les fichiers depuis Turso si manquants localement.

    Utile pour synchroniser une nouvelle machine qui reçoit les documents d'une autre.
    """
    try:
        local_db = get_local_db()
        turso_db = get_turso_db()

        if not turso_db:
            local_db.close()
            return

        # Récupérer TOUS les documents de Turso qui ont un BLOB
        remote_docs = turso_db.execute(f'''
            SELECT id, nom_fichier, contenu_blob FROM {table_name}
            WHERE contenu_blob IS NOT NULL
        ''').fetchall()

        count = 0

        for row in remote_docs:
            doc_id = row['id']
            nom_fichier = row['nom_fichier']
            blob = row['contenu_blob']

            if not nom_fichier or not blob:
                continue

            local_path = os.path.join(upload_folder, nom_fichier)

            if not os.path.exists(local_path):
                try:
                    os.makedirs(upload_folder, exist_ok=True)
                    with open(local_path, 'wb') as f:
                        f.write(blob)
                    # Marquer comme synced localement si ce n'est pas déjà le cas
                    local_db.execute(f'''
                        UPDATE {table_name}
                        SET sync_status='synced', date_sync=?
                        WHERE id=? AND sync_status != 'synced'
                    ''', (datetime.now().isoformat(), doc_id))
                    count += 1
                except Exception as e:
                    logger.warning(f"Error writing {local_path}: {e}")

        if count > 0:
            local_db.commit()
            logger.info(f"Pulled {count} documents from Turso to uploads/ ({table_name})")

        local_db.close()
        turso_db.close()

    except Exception as e:
        logger.exception(f"Error in _pull_documents_from_turso({table_name})")


def sync_uploads():
    """
    Synchronisation complète des uploads (local ↔ Turso).

    1. Push local → Turso (lit le fichier depuis le disque, non-bloquant)
    2. Pull Turso → local (fichiers manquants écrits sur disque)
    """
    from database import UPLOAD_FOLDER

    tables = [
        'documents_appareils',
        'documents_contrats',
        'documents_peripheriques'
    ]

    for table in tables:
        _push_documents_to_turso(table, UPLOAD_FOLDER)
        _pull_documents_from_turso(table, UPLOAD_FOLDER)


def start_sync_thread(interval: int = 60):
    """Lance un thread background pour la synchronisation périodique."""
    import threading

    def sync_loop():
        logger.info(f"Starting uploads sync thread (interval={interval}s)")
        while True:
            try:
                time.sleep(interval)
                sync_uploads()
            except Exception as e:
                logger.exception("Error in sync_loop")
                time.sleep(5)

    thread = threading.Thread(target=sync_loop, daemon=True)
    thread.start()
    logger.info("Uploads sync thread started")
