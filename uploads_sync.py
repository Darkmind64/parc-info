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


def _push_documents_to_turso(table_name: str, upload_folder: str, turso=None) -> dict:
    """
    Push local → Turso : pour chaque record Turso avec contenu_blob=NULL,
    si le fichier physique existe sur CETTE machine → lit et envoie le BLOB.

    Note : interroge Turso directement (pas la SQLite locale) car :
    - mode 'turso'  : l'INSERT est allé dans Turso, local SQLite est vide
    - mode 'sync'   : sync_once() a déjà poussé le record vers Turso avant l'appel

    turso : connexion partagée optionnelle (évite de créer une connexion par table)
    Retourne {'pushed': int, 'errors': int, 'pending': int} - 'pending' = fichiers
    référencés mais absents sur cette machine (normal si une autre machine les a
    uploadés et ne les a pas encore poussés elle-même).
    """
    result = {'pushed': 0, 'errors': 0, 'pending': 0}
    _own_conn = turso is None
    try:
        if _own_conn:
            turso = _get_turso_connection()
        if not turso:
            return result  # Turso non configuré → rien à faire

        to_push = turso.execute(f'''
            SELECT id, nom_fichier FROM {table_name}
            WHERE contenu_blob IS NULL
              AND nom_fichier IS NOT NULL
              AND nom_fichier != ''
        ''').fetchall()

        if not to_push:
            return result

        now = datetime.now().isoformat()

        for row in to_push:
            doc_id      = row['id']
            nom_fichier = row['nom_fichier']
            local_path  = os.path.join(upload_folder, nom_fichier)

            if not os.path.exists(local_path):
                # Ce fichier n'est pas sur cette machine (normal si une autre
                # machine ne l'a pas encore poussé - à surveiller si ça persiste)
                result['pending'] += 1
                continue

            try:
                with open(local_path, 'rb') as f:
                    blob = f.read()

                turso.execute(f'''
                    UPDATE {table_name}
                    SET contenu_blob=?, sync_status='synced', date_sync=?
                    WHERE id=?
                ''', (blob, now, doc_id))

                result['pushed'] += 1
                logger.debug(
                    f"push {table_name} id={doc_id}: {nom_fichier} "
                    f"({len(blob):,} octets) → Turso"
                )
            except urllib.error.HTTPError as e:
                result['errors'] += 1
                body = e.read().decode('utf-8', errors='replace')
                logger.warning(
                    f"push {table_name} id={doc_id} ({nom_fichier}, {len(blob):,}B): "
                    f"HTTP {e.code} — {body[:300]}"
                )
            except Exception as e:
                result['errors'] += 1
                logger.warning(f"push {table_name} id={doc_id} ({nom_fichier}): ERREUR : {e}")

        if result['pushed']:
            logger.info(f"uploads_sync push: {result['pushed']} fichier(s) envoyé(s) vers Turso ({table_name})")

    except Exception as e:
        result['errors'] += 1
        logger.exception(f"_push_documents_to_turso({table_name}) a échoué")
    finally:
        if _own_conn and turso:
            turso.close()

    return result


def _pull_documents_from_turso(table_name: str, upload_folder: str, turso=None) -> None:
    """
    Pull Turso → local : télécharge depuis Turso les fichiers absents sur cette machine.

    Fetch en deux temps pour éviter de télécharger tous les BLOBs en une seule
    requête HTTP (timeout inévitable si plusieurs gros fichiers existent dans Turso).

    turso : connexion partagée optionnelle (évite de créer une connexion par table)
    Retourne {'pulled': int, 'errors': int, 'empty_blob': int} - 'empty_blob' =
    enregistrements marqués comme ayant un contenu mais le BLOB est en réalité
    vide/absent côté Turso (donnée orpheline, ne peut pas être récupérée).
    """
    result = {'pulled': 0, 'errors': 0, 'empty_blob': 0}
    _own_conn = turso is None
    try:
        if _own_conn:
            turso = _get_turso_connection()
        if not turso:
            return result

        # Étape 1 : récupérer uniquement les IDs + noms (pas les BLOBs)
        meta_rows = turso.execute(f'''
            SELECT id, nom_fichier FROM {table_name}
            WHERE contenu_blob IS NOT NULL
              AND nom_fichier IS NOT NULL AND nom_fichier != ''
        ''').fetchall()

        if not meta_rows:
            return result

        os.makedirs(upload_folder, exist_ok=True)

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
                    result['empty_blob'] += 1
                    logger.warning(f"pull {table_name} id={doc_id} ({nom_fichier}): BLOB vide/orphelin dans Turso")
                    continue

                with open(local_path, 'wb') as f:
                    f.write(blob)
                result['pulled'] += 1
                logger.debug(
                    f"pull {table_name} id={doc_id}: {nom_fichier} "
                    f"({len(blob):,} octets) ← Turso"
                )
            except urllib.error.HTTPError as e:
                result['errors'] += 1
                body = e.read().decode('utf-8', errors='replace')
                logger.warning(
                    f"pull {table_name} id={doc_id} ({nom_fichier}): "
                    f"HTTP {e.code} — {body[:300]}"
                )
            except Exception as e:
                result['errors'] += 1
                logger.warning(f"pull {table_name} id={doc_id} ({nom_fichier}): ERREUR : {e}")

        if result['pulled']:
            logger.info(f"uploads_sync pull: {result['pulled']} fichier(s) récupéré(s) depuis Turso ({table_name})")

    except Exception:
        result['errors'] += 1
        logger.exception(f"_pull_documents_from_turso({table_name}) a échoué")
    finally:
        if _own_conn and turso:
            turso.close()

    return result


def _materialize_local_blobs(table_name: str, upload_folder: str) -> int:
    """
    Écrit sur disque les fichiers présents en base locale (contenu_blob) mais
    absents du dossier uploads.

    Rattrape les enregistrements créés par des versions antérieures de
    /api/device-info/upload-report, qui stockaient le rapport UNIQUEMENT comme
    blob local sans jamais écrire le fichier. Comme contenu_blob est exclu de la
    réplication des lignes et que le push lit depuis le disque, ces documents ne
    pouvaient jamais être transférés : les autres machines voyaient la ligne mais
    obtenaient "Fichier introuvable". Les matérialiser ici les rend synchronisables
    rétroactivement, sans intervention manuelle.

    Retourne le nombre de fichiers écrits.
    """
    count = 0
    try:
        from database import get_local_db
        conn = get_local_db()
        try:
            rows = conn.execute(
                f"SELECT id, nom_fichier FROM {table_name} "
                f"WHERE contenu_blob IS NOT NULL AND nom_fichier IS NOT NULL AND nom_fichier != ''"
            ).fetchall()
        except Exception:
            conn.close()
            return count

        os.makedirs(upload_folder, exist_ok=True)
        for row in rows:
            nom_fichier = row['nom_fichier']
            local_path = os.path.join(upload_folder, nom_fichier)
            if os.path.exists(local_path):
                continue
            try:
                blob_row = conn.execute(
                    f"SELECT contenu_blob FROM {table_name} WHERE id=?", (row['id'],)
                ).fetchone()
                blob = blob_row[0] if blob_row else None
                if not blob:
                    continue
                with open(local_path, 'wb') as f:
                    f.write(blob)
                count += 1
                logger.info(
                    f"materialize {table_name} id={row['id']}: {nom_fichier} "
                    f"({len(blob):,} octets) écrit sur disque (rattrapage)"
                )
            except Exception as e:
                logger.warning(f"materialize {table_name} id={row['id']} ({nom_fichier}): ERREUR : {e}")
        conn.close()
    except Exception:
        logger.exception(f"_materialize_local_blobs({table_name}) a échoué")

    return count


def _cleanup_orphaned_files(upload_folder: str) -> int:
    """
    Supprime les fichiers physiques dont le record DB a été supprimé.

    Collecte tous les nom_fichier référencés dans la DB locale (source de
    vérité sur cette machine), puis supprime du disque tout fichier du
    dossier uploads qui n'est plus référencé.

    Sécurité : ne touche qu'aux fichiers dont le nom commence par un préfixe
    connu de l'app (app, per, ctr, intv, baie) pour ne pas effacer des
    fichiers déposés manuellement.

    Retourne le nombre de fichiers supprimés.
    """
    # Préfixes générés par les routes upload de l'app
    APP_PREFIXES = ('app', 'per', 'ctr', 'intv', 'baie')
    count = 0

    try:
        if not os.path.isdir(upload_folder):
            return count

        from database import get_db
        conn = get_db()

        # Collecter tous les noms de fichiers encore référencés dans la DB
        referenced: set = set()
        tables = [
            'documents_appareils',
            'documents_contrats',
            'documents_peripheriques',
            'documents_interventions',
            'baie_photos',
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

    return count


def sync_uploads() -> None:
    """Cycle complet push + pull + cleanup pour toutes les tables documents.

    Une seule TursoConnection est créée et partagée entre toutes les opérations
    pour éviter de multiples résolutions DNS consécutives (6 par cycle).
    Sur Synology sous Docker, les DNS lookups répétés saturent le resolver
    embarqué et perturbent les autres services réseau (ex. Hyper Backup).

    Journalise un résumé du cycle dans journal_synchronisation (visible dans l'UI)
    - permet de vérifier concrètement que la synchronisation des fichiers a bien
    lieu entre les différentes machines, et de repérer les fichiers en attente
    ou en erreur sans avoir à consulter les logs serveur.
    """
    from database import UPLOAD_FOLDER, log_sync_event

    tables = [
        'documents_appareils',
        'documents_contrats',
        'documents_peripheriques',
        'documents_interventions',
        'baie_photos',
    ]

    # Créer une seule connexion partagée pour tout le cycle
    turso = _get_turso_connection()
    if not turso:
        return  # Turso non configuré - rien à synchroniser, rien à journaliser

    totals = {'pushed': 0, 'pulled': 0, 'errors': 0, 'pending': 0, 'empty_blob': 0}
    per_table = {}
    materialized = 0

    try:
        for table in tables:
            # Rattrapage : rendre synchronisables les documents stockés uniquement
            # en blob local (anciens rapports du collecteur) - doit précéder le push,
            # qui lit le contenu depuis le disque
            materialized += _materialize_local_blobs(table, UPLOAD_FOLDER)
            push_res = _push_documents_to_turso(table, UPLOAD_FOLDER, turso=turso)
            pull_res = _pull_documents_from_turso(table, UPLOAD_FOLDER, turso=turso)
            for k, v in push_res.items():
                totals[k] += v
            for k, v in pull_res.items():
                totals[k] += v
            if any(push_res.values()) or any(pull_res.values()):
                per_table[table] = {**push_res, **{f"pull_{k}": v for k, v in pull_res.items()}}
    finally:
        if turso:
            turso.close()

    # Supprimer les fichiers physiques dont le record a été supprimé
    cleaned = _cleanup_orphaned_files(UPLOAD_FOLDER)

    activity = totals['pushed'] + totals['pulled'] + totals['errors'] + totals['empty_blob'] + cleaned + materialized
    if activity == 0:
        return  # Rien à signaler ce cycle - ne pas bruiter le journal

    statut = 'erreur' if totals['errors'] and not (totals['pushed'] or totals['pulled']) else \
             ('partiel' if totals['errors'] or totals['empty_blob'] else 'succes')
    resume_parts = []
    if materialized:
        resume_parts.append(f"{materialized} fichier(s) restauré(s) depuis la base")
    if totals['pushed']:
        resume_parts.append(f"{totals['pushed']} fichier(s) envoyé(s)")
    if totals['pulled']:
        resume_parts.append(f"{totals['pulled']} fichier(s) reçu(s)")
    if cleaned:
        resume_parts.append(f"{cleaned} orphelin(s) nettoyé(s)")
    if totals['errors']:
        resume_parts.append(f"{totals['errors']} erreur(s)")
    if totals['empty_blob']:
        resume_parts.append(f"{totals['empty_blob']} fichier(s) introuvable(s) (orphelin distant)")

    log_sync_event('fichiers', statut, ' · '.join(resume_parts) or 'Aucune activité',
                    {'totals': totals, 'per_table': per_table,
                     'orphelins_supprimes': cleaned, 'fichiers_restaures': materialized})


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
