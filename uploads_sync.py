"""
uploads_sync.py — Synchronisation des fichiers uploads entre machines via Turso.

Architecture :
  - Les deux machines (PC et NAS) utilisent Turso comme DB principale.
  - Un upload crée un record dans Turso avec contenu_blob=NULL.
  - Push  : cette machine lit son fichier local et envoie le BLOB vers Turso.
  - Pull  : cette machine télécharge depuis Turso les BLOBs des fichiers absents localement.

Pourquoi "push interroge Turso" (et non la SQLite locale) :
  Quand db_type='turso', get_db() retourne TursoConnection → l'INSERT de
  l'upload va dans Turso. La SQLite locale est vide/inutilisée. L'ancienne
  version interrogeait get_local_db() (SQLite locale vide) → ne trouvait
  jamais rien → BLOB jamais envoyé → fichier absent sur les autres machines.

Lancé en thread background au démarrage de app.py.
"""

import logging
import time
import os
from datetime import datetime

logger = logging.getLogger('parcinfo')


def get_turso_db():
    """Retourne la connexion Turso, ou None si non configurée / hors-ligne."""
    try:
        from config_helpers import cfg_get
        if cfg_get('db_type', 'local') != 'turso':
            return None
        from database import TursoConnection
        url   = cfg_get('turso_url',   '').strip()
        token = cfg_get('turso_token', '').strip()
        if url and token:
            return TursoConnection(url, token)
    except Exception as e:
        logger.warning(f"uploads_sync: impossible d'ouvrir Turso : {e}")
    return None


def _push_documents_to_turso(table_name: str, upload_folder: str) -> None:
    """
    Push local → Turso : pour chaque record Turso avec contenu_blob=NULL,
    si le fichier physique existe sur CETTE machine → lit et envoie le BLOB.

    Logique :
      1. Interroge Turso (pas la SQLite locale) pour contenu_blob IS NULL.
      2. Pour chaque record, vérifie si le fichier existe dans upload_folder.
      3. Si oui : lit → UPDATE Turso avec le BLOB → marque sync_status='synced'.
      4. Si non : ignoré (le fichier est sur une autre machine qui le poussera).
    """
    try:
        turso_db = get_turso_db()
        if not turso_db:
            return

        to_push = turso_db.execute(f'''
            SELECT id, nom_fichier FROM {table_name}
            WHERE contenu_blob IS NULL
              AND nom_fichier IS NOT NULL
              AND nom_fichier != ''
        ''').fetchall()

        if not to_push:
            turso_db.close()
            return

        now   = datetime.now().isoformat()
        count = 0

        for row in to_push:
            doc_id      = row['id']
            nom_fichier = row['nom_fichier']
            local_path  = os.path.join(upload_folder, nom_fichier)

            if not os.path.exists(local_path):
                # Ce fichier n'est pas sur cette machine — une autre le poussera
                continue

            try:
                with open(local_path, 'rb') as f:
                    blob = f.read()

                turso_db.execute(f'''
                    UPDATE {table_name}
                    SET contenu_blob=?, sync_status='synced', date_sync=?
                    WHERE id=?
                ''', (blob, now, doc_id))

                count += 1
                logger.debug(f"push: {nom_fichier} → Turso ({table_name} id={doc_id}, {len(blob)} octets)")

            except Exception as e:
                logger.warning(f"push: erreur {table_name} id={doc_id} : {e}")

        if count:
            turso_db.commit()
            logger.info(f"push: {count} fichier(s) envoyé(s) vers Turso ({table_name})")
        else:
            turso_db.rollback()   # rien à écrire, on nettoie quand même

        turso_db.close()

    except Exception as e:
        logger.exception(f"_push_documents_to_turso({table_name}) a échoué")


def _pull_documents_from_turso(table_name: str, upload_folder: str) -> None:
    """
    Pull Turso → local : télécharge les fichiers présents dans Turso (BLOB non NULL)
    mais absents physiquement sur CETTE machine.
    """
    try:
        turso_db = get_turso_db()
        if not turso_db:
            return

        remote_docs = turso_db.execute(f'''
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
                continue   # déjà présent localement

            try:
                os.makedirs(upload_folder, exist_ok=True)
                with open(local_path, 'wb') as f:
                    f.write(blob)
                count += 1
                logger.debug(f"pull: {nom_fichier} ← Turso ({len(blob)} octets)")
            except Exception as e:
                logger.warning(f"pull: erreur écriture {local_path} : {e}")

        if count:
            logger.info(f"pull: {count} fichier(s) récupéré(s) depuis Turso ({table_name})")

        turso_db.close()

    except Exception as e:
        logger.exception(f"_pull_documents_from_turso({table_name}) a échoué")


def sync_uploads() -> None:
    """
    Cycle complet push + pull pour toutes les tables documents.
    Appelé périodiquement par start_sync_thread().
    """
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
        while True:
            try:
                time.sleep(interval)
                sync_uploads()
            except Exception:
                logger.exception("uploads_sync: erreur dans la boucle principale")
                time.sleep(5)

    t = threading.Thread(target=_loop, daemon=True, name='uploads-sync')
    t.start()
    logger.info("uploads_sync: thread lancé")
