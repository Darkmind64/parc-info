"""Rétention par durée des journaux (historique, collectes, journal_maj,
journal_synchronisation) — paramètres ajoutés en plus des plafonds par
nombre de lignes déjà existants. Par défaut (0 jour), la purge par durée
est désactivée : seul le plafond en lignes continue à s'appliquer.
"""
from datetime import timedelta

import app
import client_helpers
import database


def _set_cfg(conn, cle, valeur):
    conn.execute("INSERT OR REPLACE INTO config (cle, valeur) VALUES (?,?)", (cle, valeur))
    conn.commit()
    import config_helpers
    config_helpers.cfg_invalidate()


def test_historique_purge_desactivee_par_defaut(conn, make_client):
    cid = make_client()
    vieux = (app._utcnow() - timedelta(days=9999)).isoformat()
    conn.execute(
        "INSERT INTO historique (client_id,entite,entite_id,entite_nom,action,date_action,details) "
        "VALUES (?,?,?,?,?,?,?)", (cid, 'appareil', 1, 'Vieux', 'Modification', vieux, ''))
    conn.commit()

    client_helpers.log_history(conn, cid, 'appareil', 1, 'Déclencheur', 'Modification', '')

    restant = conn.execute("SELECT COUNT(*) FROM historique WHERE client_id=? AND entite_nom='Vieux'",
                           (cid,)).fetchone()[0]
    assert restant == 1


def test_historique_purge_par_duree_active(conn, make_client):
    cid = make_client()
    _set_cfg(conn, 'historique_max_jours', '30')
    try:
        vieux = (app._utcnow() - timedelta(days=45)).isoformat()
        recent = (app._utcnow() - timedelta(days=1)).isoformat()
        conn.execute(
            "INSERT INTO historique (client_id,entite,entite_id,entite_nom,action,date_action,details) "
            "VALUES (?,?,?,?,?,?,?)", (cid, 'appareil', 1, 'Vieux', 'Modification', vieux, ''))
        conn.execute(
            "INSERT INTO historique (client_id,entite,entite_id,entite_nom,action,date_action,details) "
            "VALUES (?,?,?,?,?,?,?)", (cid, 'appareil', 2, 'Recent', 'Modification', recent, ''))
        conn.commit()

        client_helpers.log_history(conn, cid, 'appareil', 3, 'Déclencheur', 'Modification', '')

        noms = {r[0] for r in conn.execute(
            "SELECT entite_nom FROM historique WHERE client_id=?", (cid,)).fetchall()}
        assert 'Vieux' not in noms
        assert 'Recent' in noms
        assert 'Déclencheur' in noms
    finally:
        _set_cfg(conn, 'historique_max_jours', '0')


def test_collectes_purge_par_duree_active(client, make_user, make_client, make_appareil, conn):
    uid, _l, _p = make_user(role='admin')
    cid = make_client(auth_user_id=uid)
    aid = make_appareil(cid)
    _set_cfg(conn, 'collectes_max_jours', '30')
    try:
        vieux = (app._utcnow() - timedelta(days=45)).isoformat(timespec='seconds')
        conn.execute(
            "INSERT INTO collectes (cle, appareil_id, client_id, horodatage, nb_logiciels, logiciels) "
            "VALUES (?,?,?,?,?,?)", (f'{aid}|vieux', aid, cid, vieux, 0, '[]'))
        conn.commit()

        conn2 = app.get_db()
        ok = app._enregistrer_collecte(conn2, cid, aid, {'disk_total_gb': 500, 'installed_software': []})
        conn2.commit()
        conn2.close()
        assert ok

        restant = conn.execute(
            "SELECT COUNT(*) FROM collectes WHERE appareil_id=? AND horodatage=?", (aid, vieux)).fetchone()[0]
        assert restant == 0
    finally:
        _set_cfg(conn, 'collectes_max_jours', '0')


def test_journal_maj_purge_par_duree_active(conn):
    _set_cfg(conn, 'journal_maj_max_jours', '30')
    try:
        database.creer_journal_maj(conn)
        vieux = (app._utcnow() - timedelta(days=45)).isoformat(timespec='seconds')
        conn.execute(
            "INSERT INTO journal_maj (cle, horodatage, machine, mode, version_avant, version_apres, statut, detail, date_maj) "
            "VALUES ('vieux-cle', ?, 'M', 'docker', '1.0', '1.1', 'ok', '', ?)", (vieux, vieux))
        conn.commit()

        database.log_maj_event('AUTRE-MACHINE', '1.1', '1.2', mode='docker')

        restant = conn.execute("SELECT COUNT(*) FROM journal_maj WHERE cle='vieux-cle'").fetchone()[0]
        assert restant == 0
    finally:
        _set_cfg(conn, 'journal_maj_max_jours', '0')


def test_journal_sync_purge_par_duree_active(conn):
    _set_cfg(conn, 'journal_sync_max_jours', '30')
    try:
        database.creer_journal_synchronisation(conn)
        vieux = (app._utcnow() - timedelta(days=45)).isoformat()
        conn.execute(
            "INSERT INTO journal_synchronisation (horodatage, type, statut, resume, details) "
            "VALUES (?, 'db_sync', 'succes', 'vieux', '')", (vieux,))
        conn.commit()

        database.log_sync_event('db_sync', 'succes', 'déclencheur')

        restant = conn.execute(
            "SELECT COUNT(*) FROM journal_synchronisation WHERE resume='vieux'").fetchone()[0]
        assert restant == 0
    finally:
        _set_cfg(conn, 'journal_sync_max_jours', '0')
