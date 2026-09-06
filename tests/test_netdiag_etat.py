"""Read models de l'interface diagnostic réseau (netdiag.etat) — Lot 4."""
from datetime import datetime, timezone

import pytest

from netdiag import etat


def _now_z():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


@pytest.fixture
def parc(conn, make_client, make_appareil, monkeypatch):
    import network_diag
    monkeypatch.setattr(network_diag, '_cfg', lambda k, d=None: '1' if k == 'diag_snmp_actif' else d)
    cid = make_client()
    sw = make_appareil(cid, nom_machine='SW-CORE', type_appareil='Switch', adresse_ip='10.0.0.2')
    pc = make_appareil(cid, nom_machine='PC-COMPTA', adresse_ip='10.0.0.50')
    now = _now_z()
    conn.execute(
        "INSERT INTO diag_etat_equipement (client_id, equipement_ip, appareil_id, sysname, "
        "joignable, snmp_ok, nb_ports, nb_ports_up, nb_ports_erreur, derniere_maj, epoch) "
        "VALUES (?,?,?,?,1,1,3,3,2,?,?)", (cid, '10.0.0.2', sw, 'SW-CORE', now, 1))
    # port 1 : erreur physique (CRC) critique-ish
    conn.execute(
        "INSERT INTO diag_etat_port (client_id, equipement_ip, port_index, appareil_id, "
        "appareil_vu_id, port_nom, oper, speed_mbps, duplex, err_min, disc_min, crc_min, "
        "classe_erreur, classe_libelle, gravite, depuis, derniere_maj) "
        "VALUES (?,?,?,?,?,?,1,1000,3,0,0,120,'physique','Couche physique (CRC/FCS)',"
        "'avertissement',?,?)", (cid, '10.0.0.2', 1, sw, pc, 'Gi1/0/1', now, now))
    # port 2 : duplex mismatch (critique)
    conn.execute(
        "INSERT INTO diag_etat_port (client_id, equipement_ip, port_index, appareil_id, "
        "port_nom, oper, speed_mbps, duplex, err_min, disc_min, crc_min, "
        "classe_erreur, classe_libelle, gravite, depuis, derniere_maj) "
        "VALUES (?,?,?,?,?,1,100,2,10,0,5,'duplex','Duplex mismatch','critique',?,?)",
        (cid, '10.0.0.2', 2, sw, 'Gi1/0/2', now, now))
    # port 3 : sain
    conn.execute(
        "INSERT INTO diag_etat_port (client_id, equipement_ip, port_index, appareil_id, "
        "port_nom, oper, speed_mbps, duplex, classe_erreur, derniere_maj) "
        "VALUES (?,?,?,?,?,1,1000,3,'',?)", (cid, '10.0.0.2', 3, sw, 'Gi1/0/3', now))
    conn.commit()
    return cid


def test_trafic_trie_pire_dabord(parc):
    d = etat.trafic(parc)
    assert d['actif'] and d['nb_erreur'] == 2 and d['nb_critique'] == 1
    assert d['nb_actifs'] == 3
    assert d['verdict'] == 'critique'
    # le port critique (duplex) passe avant l'avertissement (physique)
    assert [p['classe'] for p in d['ports_en_erreur']] == ['duplex', 'physique']
    p = d['ports_en_erreur'][0]
    assert p['equipement_nom'] == 'SW-CORE' and p['conseil']
    # port sain non listé par défaut
    assert d['ports_sains'] == []


def test_trafic_tous_inclut_les_ports_sains(parc):
    d = etat.trafic(parc, tous=True)
    assert len(d['ports_sains']) == 1 and d['ports_sains'][0]['port_nom'] == 'Gi1/0/3'


def test_trafic_appareil_vu_resolu(parc):
    d = etat.trafic(parc)
    phys = next(p for p in d['ports_en_erreur'] if p['classe'] == 'physique')
    assert phys['appareil_vu_nom'] == 'PC-COMPTA'


def test_verdict_critique(parc, conn):
    conn.execute(
        "INSERT INTO diag_reseau_evenements (client_id, categorie, gravite, titre, "
        "signature, resolu, derniere_occurrence) VALUES (?,?,?,?,?,0,?)",
        (parc, 'duplex_mismatch', 'critique', 'x', 'sig1', _now_z()))
    conn.commit()
    v = etat.verdict(parc)
    assert v['niveau'] == 'critique' and v['nb_evenements_critiques'] == 1
    assert v['nb_equipements'] == 1 and v['nb_equipements_ok'] == 1
    assert v['nb_ports_erreur'] == 2


def test_verdict_ok_quand_rien(conn, make_client, monkeypatch):
    import network_diag
    monkeypatch.setattr(network_diag, '_cfg', lambda k, d=None: d)
    cid = make_client()
    v = etat.verdict(cid)
    assert v['niveau'] == 'ok' and 'Aucun problème' in v['phrase']


def test_pour_appareil_switch(parc, conn):
    sw = conn.execute("SELECT id FROM appareils WHERE nom_machine='SW-CORE' AND client_id=?",
                      (parc,)).fetchone()[0]
    d = etat.pour_appareil(parc, sw)
    assert d['a_montrer'] and d['equipement'] and d['equipement']['snmp_ok']
    assert len(d['ports_en_erreur']) == 2
    assert d['vu_sur'] is None


def test_pour_appareil_poste_vu_sur_switch(parc, conn):
    pc = conn.execute("SELECT id FROM appareils WHERE nom_machine='PC-COMPTA' AND client_id=?",
                      (parc,)).fetchone()[0]
    d = etat.pour_appareil(parc, pc)
    assert d['a_montrer'] and d['vu_sur']
    assert d['vu_sur']['equipement_nom'] == 'SW-CORE'
    assert d['vu_sur']['port_nom'] == 'Gi1/0/1'
    assert d['vu_sur']['classe'] == 'physique'


def test_pour_appareil_rien_a_montrer(conn, make_client, make_appareil):
    cid = make_client()
    a = make_appareil(cid, nom_machine='POSTE-ISOLE')
    d = etat.pour_appareil(cid, a)
    assert d['a_montrer'] is False
