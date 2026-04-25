"""
migrate_uploads.py — Migre les fichiers uploads existants vers le stockage BLOB.

À lancer une seule fois après le déploiement pour remplir les colonnes contenu_blob
des documents existants et les marquer pour synchronisation.

Usage:
    python migrate_uploads.py
"""

import sqlite3
import os
import sys
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger('migrate_uploads')

def get_db(db_path):
    """Connexion SQLite locale."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def migrate_table(conn, upload_folder, table_name):
    """
    Migre tous les documents d'une table en remplissant contenu_blob.

    Args:
        conn: connexion SQLite
        upload_folder: chemin du dossier uploads/
        table_name: 'documents_appareils' | 'documents_contrats' | 'documents_peripheriques'
    """
    logger.info(f"Migrating {table_name}...")

    # Récupérer tous les documents avec nom_fichier mais contenu_blob NULL
    docs = conn.execute(f'''
        SELECT id, nom_fichier FROM {table_name}
        WHERE nom_fichier IS NOT NULL AND nom_fichier != ''
        AND (contenu_blob IS NULL OR sync_status IS NULL)
    ''').fetchall()

    if not docs:
        logger.info(f"  → Aucun document à migrer dans {table_name}")
        return 0

    now = datetime.now().isoformat()
    count = 0
    errors = 0

    for row in docs:
        doc_id = row['id']
        nom_fichier = row['nom_fichier']
        filepath = os.path.join(upload_folder, nom_fichier)

        # Vérifier que le fichier existe
        if not os.path.exists(filepath):
            logger.warning(f"  ⚠ Fichier manquant: {nom_fichier} (doc_id={doc_id})")
            errors += 1
            continue

        try:
            # Lire le fichier en BLOB
            with open(filepath, 'rb') as f:
                blob_data = f.read()

            # Mettre à jour le document
            conn.execute(f'''
                UPDATE {table_name}
                SET contenu_blob=?, sync_status='local', date_sync=?
                WHERE id=?
            ''', (blob_data, now, doc_id))

            count += 1
            if count % 10 == 0:
                logger.info(f"  → Traité {count} documents...")

        except Exception as e:
            logger.error(f"  ✗ Erreur pour {nom_fichier}: {e}")
            errors += 1

    conn.commit()
    logger.info(f"  ✓ {table_name}: {count} documents migrés, {errors} erreurs")
    return count

def main():
    """Lance la migration complète des uploads."""
    # Déterminer le chemin de la DB (même logique que app.py)
    if getattr(sys, 'frozen', False):
        # Mode exécutable
        _data_base = os.path.dirname(sys.executable)
    else:
        # Mode dev
        _data_base = os.path.dirname(os.path.abspath(__file__))

    DATABASE = os.path.join(_data_base, 'parc_info.db')
    UPLOAD_FOLDER = os.path.join(_data_base, 'uploads')

    if not os.path.exists(DATABASE):
        logger.error(f"Database not found: {DATABASE}")
        sys.exit(1)

    logger.info(f"Database: {DATABASE}")
    logger.info(f"Upload folder: {UPLOAD_FOLDER}")

    conn = get_db(DATABASE)

    # Vérifier que les colonnes existent
    c = conn.cursor()
    try:
        c.execute("SELECT contenu_blob FROM documents_appareils LIMIT 1")
    except sqlite3.OperationalError:
        logger.error("Colonnes BLOB non trouvées. Lancer init_db() d'abord.")
        conn.close()
        sys.exit(1)

    # Migrer les 3 tables
    tables = [
        'documents_appareils',
        'documents_contrats',
        'documents_peripheriques'
    ]

    total = 0
    for table in tables:
        total += migrate_table(conn, UPLOAD_FOLDER, table)

    conn.close()

    logger.info(f"\n{'='*50}")
    logger.info(f"Migration complète: {total} documents migrés")
    logger.info(f"{'='*50}")
    logger.info("Les fichiers seront synchronisés vers Turso dans 60 secondes.")

if __name__ == '__main__':
    main()
