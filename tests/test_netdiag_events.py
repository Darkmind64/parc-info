"""Auto-résolution des évènements de port SNMP (netdiag.events) — Lot 2."""
import time
from datetime import datetime, timedelta, timezone

from netdiag import events


def _z(dt):
    return dt.isoformat(timespec='seconds').replace('+00:00', 'Z')


def _evt(conn, cid, categorie, ip, derniere_occ, resolu=0):
    conn.execute(
        "INSERT INTO diag_reseau_evenements (client_id, categorie, titre, signature, "
        "equipement_ip, resolu, premiere_occurrence, derniere_occurrence, horodatage) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, categorie, f'{categorie} sur {ip}', f'{categorie}|{ip}|1', ip, resolu,
         _z(derniere_occ), _z(derniere_occ), _z(derniere_occ)))
    conn.commit()


def _equip(conn, cid, ip, maj):
    conn.execute(
        "INSERT INTO diag_etat_equipement (client_id, equipement_ip, snmp_ok, derniere_maj, epoch) "
        "VALUES (?,?,1,?,?) ON CONFLICT(client_id, equipement_ip) DO UPDATE SET "
        "derniere_maj=excluded.derniere_maj",
        (cid, ip, _z(maj), maj.timestamp()))
    conn.commit()


def test_auto_resolution_condition_disparue(conn, make_client):
    cid = make_client()
    now = datetime.now(timezone.utc)
    # évènement CRC vieux (condition pas revue depuis 1h), équipement toujours relevé
    _evt(conn, cid, 'port_crc', '10.9.9.1', now - timedelta(hours=1))
    _equip(conn, cid, '10.9.9.1', now - timedelta(seconds=30))

    n = events.auto_resoudre_snmp(cid, absence_s=1800, equip_frais_s=600)
    assert n == 1
    r = conn.execute("SELECT resolu, date_resolu FROM diag_reseau_evenements "
                     "WHERE client_id=? AND categorie='port_crc'", (cid,)).fetchone()
    assert r[0] == 1 and r[1]


def test_pas_de_resolution_si_condition_recente(conn, make_client):
    cid = make_client()
    now = datetime.now(timezone.utc)
    _evt(conn, cid, 'port_erreurs', '10.9.9.2', now - timedelta(minutes=2))
    _equip(conn, cid, '10.9.9.2', now - timedelta(seconds=10))
    assert events.auto_resoudre_snmp(cid, absence_s=1800) == 0


def test_pas_de_resolution_si_equipement_injoignable(conn, make_client):
    """On ne résout pas un problème juste parce que le switch a disparu."""
    cid = make_client()
    now = datetime.now(timezone.utc)
    _evt(conn, cid, 'port_crc', '10.9.9.3', now - timedelta(hours=2))
    _equip(conn, cid, '10.9.9.3', now - timedelta(hours=1))   # relevé trop vieux
    assert events.auto_resoudre_snmp(cid, absence_s=1800, equip_frais_s=600) == 0
    assert conn.execute("SELECT resolu FROM diag_reseau_evenements WHERE client_id=? "
                        "AND categorie='port_crc'", (cid,)).fetchone()[0] == 0


def test_categorie_non_port_ignoree(conn, make_client):
    cid = make_client()
    now = datetime.now(timezone.utc)
    _evt(conn, cid, 'conflit_ip', '10.9.9.4', now - timedelta(hours=3))
    _equip(conn, cid, '10.9.9.4', now - timedelta(seconds=10))
    assert events.auto_resoudre_snmp(cid, absence_s=1800) == 0


def test_desactive_si_seuil_zero(conn, make_client):
    cid = make_client()
    now = datetime.now(timezone.utc)
    _evt(conn, cid, 'port_crc', '10.9.9.5', now - timedelta(hours=5))
    _equip(conn, cid, '10.9.9.5', now - timedelta(seconds=10))
    assert events.auto_resoudre_snmp(cid, absence_s=0) == 0
