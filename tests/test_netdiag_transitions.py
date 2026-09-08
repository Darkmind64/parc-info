"""Proposition #7 — les bascules d'état du diagnostic réseau sont journalisées
dans l'historique du client (`_ecrire_etat_snmp` / `_marquer_equipements_muets`).
"""
from datetime import datetime, timezone

import network_diag as N


def _now_z():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _ligne(pi, classe='', libelle='', nom=None):
    return {'port_index': pi, 'port_nom': nom or f'Gi0/{pi}', 'port_alias': '',
            'oper': 1, 'admin': 1, 'speed_mbps': 1000, 'duplex': 3,
            'err_min': 0, 'disc_min': 0, 'crc_min': 0, 'debit_pct': 0,
            'classe_erreur': classe, 'classe_libelle': libelle,
            'gravite': 'avertissement' if classe else ''}


def _actions(conn, cid):
    return [r[0] for r in conn.execute(
        "SELECT action FROM historique WHERE client_id=? AND entite='diag_reseau' "
        "ORDER BY id", (cid,))]


def test_ports_disparus_sont_purges(conn, make_client, make_appareil):
    """Un port qui disparaît du relevé (SFP débranché, ifIndex changé) voit sa
    ligne d'état effacée — sinon son `classe_erreur` figé gonflait le verdict.
    Garde-fou : pas de purge sur un relevé sensiblement plus court (partiel)."""
    cid = make_client()
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.9.0.9')
    now = _now_z()
    N._ecrire_etat_snmp(conn, cid, '10.9.0.9', sw, 'SW',
                        [_ligne(i, 'physique' if i == 3 else '') for i in range(1, 6)], now, 1.0)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM diag_etat_port WHERE equipement_ip='10.9.0.9'"
                        ).fetchone()[0] == 5
    # relevé suivant : le port 5 a disparu (4 ports au lieu de 5) -> purgé
    N._ecrire_etat_snmp(conn, cid, '10.9.0.9', sw, 'SW',
                        [_ligne(i) for i in range(1, 5)], now, 2.0)
    conn.commit()
    restants = {r[0] for r in conn.execute(
        "SELECT port_index FROM diag_etat_port WHERE equipement_ip='10.9.0.9'")}
    assert restants == {1, 2, 3, 4}
    # relevé PARTIEL (1 port sur 4) -> on NE purge PAS (agent lent)
    N._ecrire_etat_snmp(conn, cid, '10.9.0.9', sw, 'SW', [_ligne(1)], now, 3.0)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM diag_etat_port WHERE equipement_ip='10.9.0.9'"
                        ).fetchone()[0] == 4


def test_etat_equipement_orphelin_efface(conn, make_client, make_appareil):
    """`_marquer_equipements_muets(inventaire_ips=...)` efface l'état d'un
    équipement qui n'est plus dans l'inventaire (supprimé, IP changée)."""
    cid = make_client()
    sw = make_appareil(cid, nom_machine='SW', type_appareil='Switch', adresse_ip='10.9.0.2')
    now = _now_z()
    N._ecrire_etat_snmp(conn, cid, '10.9.0.2', sw, 'SW', [_ligne(1, 'physique', 'CRC')], now, 1.0)
    N._ecrire_etat_snmp(conn, cid, '10.9.0.99', 0, 'VIEUX', [_ligne(1)], now, 1.0)
    conn.commit()
    # 10.9.0.99 n'est plus dans l'inventaire -> ses lignes d'état sont effacées
    N._marquer_equipements_muets(cid, {'10.9.0.2'}, inventaire_ips={'10.9.0.2'})
    eqs = {r[0] for r in conn.execute(
        "SELECT equipement_ip FROM diag_etat_equipement WHERE client_id=?", (cid,))}
    assert eqs == {'10.9.0.2'}
    assert conn.execute("SELECT COUNT(*) FROM diag_etat_port WHERE equipement_ip='10.9.0.99'"
                        ).fetchone()[0] == 0


def test_port_erreur_puis_retabli(conn, make_client, make_appareil):
    cid = make_client()
    sw = make_appareil(cid, nom_machine='SW1', type_appareil='Switch', adresse_ip='10.9.0.2')
    now = _now_z()
    # 1er cycle : le port est déjà en erreur -> PAS de log (pas de bruit au 1er passage)
    N._ecrire_etat_snmp(conn, cid, '10.9.0.2', sw, 'SW1',
                        [_ligne(1, 'physique', 'Couche physique (CRC/FCS)')], now, 1.0)
    conn.commit()
    assert _actions(conn, cid) == []
    # 2e cycle : toujours la même erreur -> toujours rien
    N._ecrire_etat_snmp(conn, cid, '10.9.0.2', sw, 'SW1',
                        [_ligne(1, 'physique', 'Couche physique (CRC/FCS)')], now, 2.0)
    conn.commit()
    assert _actions(conn, cid) == []
    # 3e cycle : le port redevient sain -> PORT_RETABLI
    N._ecrire_etat_snmp(conn, cid, '10.9.0.2', sw, 'SW1', [_ligne(1)], now, 3.0)
    conn.commit()
    assert _actions(conn, cid) == ['DIAG_RESEAU_PORT_RETABLI']
    # 4e cycle : nouvelle erreur (duplex) -> PORT_ERREUR
    N._ecrire_etat_snmp(conn, cid, '10.9.0.2', sw, 'SW1',
                        [_ligne(1, 'duplex', 'Duplex mismatch')], now, 4.0)
    conn.commit()
    assert _actions(conn, cid) == ['DIAG_RESEAU_PORT_RETABLI', 'DIAG_RESEAU_PORT_ERREUR']


def test_equipement_muet_puis_de_retour(conn, make_client, make_appareil):
    cid = make_client()
    sw = make_appareil(cid, nom_machine='SW2', type_appareil='Switch', adresse_ip='10.9.0.3')
    now = _now_z()
    N._ecrire_etat_snmp(conn, cid, '10.9.0.3', sw, 'SW2', [_ligne(1)], now, 1.0)
    conn.commit()
    # devient muet
    N._marquer_equipements_muets(cid, ips_vus=set(), motifs={'10.9.0.3': 'aucune réponse SNMP'})
    assert 'DIAG_RESEAU_EQUIP_INJOIGNABLE' in _actions(conn, cid)
    assert conn.execute("SELECT snmp_ok FROM diag_etat_equipement WHERE client_id=? "
                        "AND equipement_ip='10.9.0.3'", (cid,)).fetchone()[0] == 0
    # répond à nouveau
    N._ecrire_etat_snmp(conn, cid, '10.9.0.3', sw, 'SW2', [_ligne(1)], now, 2.0)
    conn.commit()
    assert _actions(conn, cid)[-1] == 'DIAG_RESEAU_EQUIP_JOIGNABLE'


def test_marquer_muets_ne_touche_pas_un_equipement_vu(conn, make_client, make_appareil):
    cid = make_client()
    sw = make_appareil(cid, nom_machine='SW3', type_appareil='Switch', adresse_ip='10.9.0.4')
    N._ecrire_etat_snmp(conn, cid, '10.9.0.4', sw, 'SW3', [_ligne(1)], _now_z(), 1.0)
    conn.commit()
    N._marquer_equipements_muets(cid, ips_vus={'10.9.0.4'}, motifs={})
    assert conn.execute("SELECT snmp_ok FROM diag_etat_equipement WHERE client_id=? "
                        "AND equipement_ip='10.9.0.4'", (cid,)).fetchone()[0] == 1
    assert _actions(conn, cid) == []
