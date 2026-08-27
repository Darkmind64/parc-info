"""
Synchronisation multi-instance des ports de baie — baie_slot_ports.

Signalé en usage réel : le positionnement des éléments de la baie
(baie_slots) se synchronisait bien entre instances, mais pas leurs ports ni
leur câblage (numéro, appareil/périphérique associé, usage libre, lien
port-à-port lie_slot_id/lie_port_numero) — la table baie_slot_ports avait
été oubliée de _TRACKED_JOURNAL (voir app.py:init_db) depuis sa création :
aucun trigger ne journalisait ses écritures, donc _sync_journal ne les
voyait jamais et sync_once() ne les poussait jamais vers Turso.

Couvre : le trigger journalise désormais bien les écritures FUTURES sur
baie_slot_ports, et rattraper_sync_baie_slot_ports() journalise
rétroactivement les ports déjà en base avant ce correctif (une seule fois).
"""
from conftest import login_session


def _journal_entries(conn, record_id):
    return conn.execute(
        "SELECT action FROM _sync_journal WHERE tbl='baie_slot_ports' AND record_id=?",
        (record_id,)
    ).fetchall()


def test_creation_port_journalisee_pour_la_sync(client, make_client, make_user, conn):
    """Non-régression du bug : poser un switch (qui crée ses lignes de port)
    doit journaliser chaque port créé, comme pour n'importe quelle autre
    table suivie."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    conn.execute("DELETE FROM _sync_journal WHERE tbl='baie_slot_ports'")
    conn.commit()

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-SYNC', 'nb_ports': 4,
    }).get_json()
    port_ids = [r[0] for r in conn.execute(
        'SELECT id FROM baie_slot_ports WHERE slot_id=?', (slot['id'],)).fetchall()]
    assert len(port_ids) == 4

    for pid in port_ids:
        entries = _journal_entries(conn, pid)
        assert any(a == 'INSERT' for (a,) in entries), \
            f"port {pid} : aucune entrée INSERT dans _sync_journal, ne sera jamais poussé vers Turso"


def test_liaison_port_journalisee_pour_la_sync(client, make_client, make_user, make_appareil, conn):
    """Modifier un port existant (l'associer à un appareil — le câblage)
    doit aussi journaliser un UPDATE, pas seulement la création."""
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    app_id = make_appareil(cid, nom_machine='SERVEUR-SYNC')

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-SYNC2', 'nb_ports': 2,
    }).get_json()
    port_id = conn.execute(
        'SELECT id FROM baie_slot_ports WHERE slot_id=? AND numero=1', (slot['id'],)).fetchone()[0]

    conn.execute("DELETE FROM _sync_journal WHERE tbl='baie_slot_ports'")
    conn.commit()

    client.put(f"/api/baie/slot/{slot['id']}/port/1", json={'appareil_id': app_id})

    entries = _journal_entries(conn, port_id)
    assert any(a == 'UPDATE' for (a,) in entries), \
        "la liaison d'un port à un appareil (câblage) doit être journalisée pour se propager"


def test_rattrapage_journalise_les_ports_existants_une_seule_fois(
        client, make_client, make_user, conn):
    """Simule des ports créés AVANT ce correctif (jamais journalisés) et
    vérifie que le rattrapage les journalise rétroactivement, une seule fois."""
    import app as A

    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)

    slot = client.post('/api/baie/slot', json={
        'position': 1, 'col_index': 0, 'baie_nom': 'Baie principale',
        'type_equipement': 'Switch', 'nom_custom': 'SW-RATTRAPAGE', 'nb_ports': 3,
    }).get_json()
    port_ids = [r[0] for r in conn.execute(
        'SELECT id FROM baie_slot_ports WHERE slot_id=?', (slot['id'],)).fetchall()]

    # Simule l'état "jamais journalisé" (ports créés avant le correctif) et
    # réinitialise le marqueur de rattrapage / le flag process pour rejouer
    # le scénario "premier démarrage après mise à jour" dans ce test.
    conn.execute("DELETE FROM _sync_journal WHERE tbl='baie_slot_ports'")
    conn.execute("DELETE FROM config WHERE cle=?", (A._CLE_RATTRAPAGE_BAIE_PORTS,))
    conn.commit()
    A._rattrapage_baie_ports_fait = False
    from config_helpers import cfg_invalidate
    cfg_invalidate()

    nb = A.rattraper_sync_baie_slot_ports()
    assert nb >= len(port_ids)
    for pid in port_ids:
        entries = _journal_entries(conn, pid)
        assert any(a == 'INSERT' for (a,) in entries)

    # Rejoué (même process ou après redémarrage) : ne double-journalise pas.
    conn.execute("DELETE FROM _sync_journal WHERE tbl='baie_slot_ports'")
    conn.commit()
    A._rattrapage_baie_ports_fait = False  # simule un redémarrage : le marqueur persisté doit suffire

    nb2 = A.rattraper_sync_baie_slot_ports()
    assert nb2 == 0, "le rattrapage ne doit pas rejouer une fois le marqueur posé"
    for pid in port_ids:
        assert _journal_entries(conn, pid) == [], \
            "un rattrapage déjà fait ne doit rien réinjecter dans le journal"
