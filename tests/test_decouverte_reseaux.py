"""Scan réseau — découverte de sous-réseaux hors des plages saisies.

`network_diag.decouvrir_reseaux` agrège table de routage + DNS + ARP du poste
+ SNMP de l'inventaire ; `reseaux_hors_plage` remonte les /24 où des appareils
ont répondu pendant un scan sans être dans les plages balayées."""
import ipaddress
import os

import network_diag as N
from conftest import login_session


def test_cidr_scannable_gardes():
    ok = ipaddress.ip_network
    assert N._cidr_scannable(ok('192.168.5.0/24'))
    assert N._cidr_scannable(ok('10.20.0.0/16'))
    assert not N._cidr_scannable(ok('10.0.0.0/8'))       # trop large
    assert not N._cidr_scannable(ok('169.254.0.0/16'))   # link-local
    assert not N._cidr_scannable(ok('1.2.3.4/32'))


def test_decouvrir_reseaux_agrege_et_exclut(conn, make_client, monkeypatch):
    cid = make_client()
    conn.execute("INSERT INTO parc_general (client_id, plage_ip_locale) VALUES (?, '192.168.1.0/24')",
                 (cid,))
    conn.commit()
    monkeypatch.delenv('RUNNING_IN_DOCKER', raising=False)   # dé-brider les sondes locales
    monkeypatch.setattr(N, '_routes_locales_poste',
                        lambda: {'192.168.1.0/24', '192.168.30.0/24', '10.5.0.0/16'})
    monkeypatch.setattr(N, '_dns_configures_poste', lambda: {'192.168.42.53', '1.1.1.1'})
    monkeypatch.setattr(N, '_table_arp',
                        lambda: {'192.168.30.7': {'aa:00:00:00:00:07'},
                                 '192.168.30.8': {'aa:00:00:00:00:08'}})

    d = N.decouvrir_reseaux(cid)
    par = {x['cidr']: x for x in d['detectes']}
    assert '192.168.1.0/24' not in par                # plage déclarée -> exclue
    assert '10.5.0.0/16' not in par                   # /16 route-only sans hôte -> écarté
    assert '192.168.42.0/24' in par                   # DNS interne -> ok (faible)
    assert not any('1.1.1' in c for c in par)         # résolveur public -> non
    assert {s['via'] for s in par['192.168.30.0/24']['sources']} == {'routage_local', 'arp_local'}
    assert par['192.168.30.0/24']['hint_hotes'] == 2
    assert par['192.168.30.0/24']['confiance'] == 'forte'   # hôtes vus en ARP
    assert par['192.168.42.0/24']['confiance'] == 'faible'  # DNS seul


def test_route_only_large_sans_hote_ecartee(conn, make_client, monkeypatch):
    """Sur un portable Windows, `route print` liste des /20 Hyper-V/WSL sans
    aucun hôte — on ne les propose pas (ni même en faible)."""
    cid = make_client()
    monkeypatch.delenv('RUNNING_IN_DOCKER', raising=False)
    monkeypatch.setattr(N, '_routes_locales_poste',
                        lambda: {'172.22.224.0/20', '192.168.7.0/24'})
    monkeypatch.setattr(N, '_dns_configures_poste', lambda: set())
    monkeypatch.setattr(N, '_table_arp', lambda: {})
    d = N.decouvrir_reseaux(cid)
    cidrs = {x['cidr'] for x in d['detectes']}
    assert '172.22.224.0/20' not in cidrs        # /20 sans hôte -> écarté
    assert '192.168.7.0/24' in cidrs             # /24 route-only -> faible mais gardé


def test_reseaux_hors_plage():
    hp = N.reseaux_hors_plage(
        ['192.168.1.0/24'],
        {'192.168.1.5': 1},                            # dans la plage -> ignoré
        {'192.168.60.2': 1, '192.168.60.3': 1},        # -> 192.168.60.0/24 (2)
        {'10.1.2.3': 1})                               # -> 10.1.2.0/24 (1)
    m = {x['cidr']: x['hint_hotes'] for x in hp}
    assert '192.168.1.0/24' not in m
    assert m.get('192.168.60.0/24') == 2
    assert m.get('10.1.2.0/24') == 1


def test_docker_court_circuite_les_sondes_locales(monkeypatch):
    monkeypatch.setenv('RUNNING_IN_DOCKER', '1')
    assert N._routes_locales_poste() == set()
    assert N._dns_configures_poste() == set()


def test_decouvrir_reseaux_actif_lot_b(conn, make_client, monkeypatch):
    """Lot B : traceroute + SNMP passerelle hors inventaire + passerelles
    voisines s'ajoutent au passif, en confiance forte."""
    import app as A
    cid = make_client()
    conn.execute("INSERT INTO parc_general (client_id, plage_ip_locale, serveur_dns) "
                 "VALUES (?, '192.168.1.0/24', '192.168.1.53')", (cid,))
    conn.commit()
    monkeypatch.delenv('RUNNING_IN_DOCKER', raising=False)
    # passif : rien
    monkeypatch.setattr(N, '_routes_locales_poste', lambda: set())
    monkeypatch.setattr(N, '_dns_configures_poste', lambda: set())
    monkeypatch.setattr(N, '_table_arp', lambda: {})
    # actif
    monkeypatch.setattr(N, '_passerelle_defaut', lambda: '192.168.1.1')
    monkeypatch.setattr(N, '_traceroute',
                        lambda cible, **k: ['192.168.1.1', '62.4.16.1', '8.8.8.8']
                        if cible == '8.8.8.8' else ['192.168.1.1', '192.168.50.1'])
    monkeypatch.setattr(A, '_snmp_presence', lambda ip, c, **k: (True, True, 'ok'))
    monkeypatch.setattr(N, '_sous_reseaux_equipement',
                        lambda ip, c: ['192.168.99.0/24'] if ip == '192.168.1.1' else [])
    monkeypatch.setattr(N, '_echo_reply_ok', lambda ip: ip in ('192.168.5.1', '10.0.0.1'))

    d = N.decouvrir_reseaux_actif(cid, budget_s=10)
    par = {x['cidr']: x for x in d['detectes']}
    assert '192.168.50.0/24' in par                       # saut privé du traceroute vers le DNS
    assert par['192.168.50.0/24']['confiance'] == 'forte'
    assert not any(c.startswith('8.8.8') or c.startswith('62.4') for c in par)  # sauts publics ignorés
    assert '192.168.99.0/24' in par                       # SNMP sur la passerelle hors inventaire
    assert '192.168.5.0/24' in par and '10.0.0.0/24' in par  # passerelles voisines (echo-reply strict)
    assert par['192.168.5.0/24']['confiance'] == 'forte'


def test_echo_reply_ok_rejette_unreachable(monkeypatch):
    """`_echo_reply_ok` doit rejeter une réponse « Destination host unreachable »
    (Windows renvoie ça avec un code retour 0)."""
    class _R:
        def __init__(self, s): self.stdout = s
    monkeypatch.setattr(N, 'IS_WINDOWS', True)
    monkeypatch.setattr(N, '_run', lambda *a, **k: _R(
        "Pinging 192.168.9.1 with 32 bytes of data:\n"
        "Reply from 192.168.1.1: Destination host unreachable.\n"))
    assert N._echo_reply_ok('192.168.9.1') is False
    monkeypatch.setattr(N, '_run', lambda *a, **k: _R(
        "Reply from 192.168.9.1: bytes=32 time=1ms TTL=64\n"))
    assert N._echo_reply_ok('192.168.9.1') is True


def test_api_scan_sous_reseaux_route(client, conn, make_user, make_client, monkeypatch):
    uid, _l, _p = make_user()
    cid = make_client(auth_user_id=uid)
    conn.execute("INSERT INTO parc_general (client_id, plage_ip_locale) VALUES (?, '10.0.0.0/24')",
                 (cid,))
    conn.commit()
    monkeypatch.delenv('RUNNING_IN_DOCKER', raising=False)
    monkeypatch.setattr(N, '_routes_locales_poste', lambda: {'10.0.9.0/24'})
    monkeypatch.setattr(N, '_dns_configures_poste', lambda: set())
    monkeypatch.setattr(N, '_table_arp', lambda: {})
    login_session(client, uid, cid)
    r = client.get('/api/scan/sous-reseaux')
    assert r.status_code == 200
    d = r.get_json()
    assert d['ok'] and any(x['cidr'] == '10.0.9.0/24' for x in d['detectes'])
