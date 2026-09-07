"""Mode terrain / détection de site : ParcInfo doit savoir s'il est CHEZ le
client (fonctions live utiles) ou hors site (consultation — ne rien importer,
ne rien écraser). Détection basée sur la table ARP de ce poste croisée avec
l'inventaire de tous les clients (même principe que l'auto-détection du client
dans le collecteur)."""
from conftest import login_session

import network_diag as N
import config_helpers as C
import site_terrain as S


import pytest


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Vide les caches du module + neutralise l'accès réseau réel."""
    S._cache.update(ts=0.0, res=None)
    S._grace.clear()
    S._ip_pub.update(ts=0.0, ip='')
    monkeypatch.setattr(N, '_table_arp', lambda: {})
    monkeypatch.setattr(N, '_passerelle_defaut', lambda: '')
    monkeypatch.setattr(S, '_ip_publique', lambda: '')
    C.cfg_set('mode_terrain', 'auto')
    yield
    C.cfg_set('mode_terrain', 'auto')   # défaut de la suite (voir conftest)


def test_mode_defaut_docker_est_consultation(monkeypatch):
    C.cfg_set('mode_terrain', '')          # revient au défaut
    monkeypatch.setenv('RUNNING_IN_DOCKER', '1')
    assert S.mode_terrain() == 'consultation'


def test_mode_defaut_hors_docker_est_auto(monkeypatch):
    C.cfg_set('mode_terrain', '')
    monkeypatch.delenv('RUNNING_IN_DOCKER', raising=False)
    assert S.mode_terrain() == 'auto'


def test_consultation_coupe_tout(conn, monkeypatch):
    C.cfg_set('mode_terrain', 'consultation')
    d = S.detecter_site(conn)
    assert d['mode'] == 'consultation' and d['confiance'] == 'indetermine'
    assert S.clients_sur_site(conn) == set()
    assert S.site_actif(conn, 1) is False


def test_terrain_force_sur_site(conn):
    C.cfg_set('mode_terrain', 'terrain')
    d = S.detecter_site(conn)
    assert d['confiance'] == 'sur_site'
    assert S.site_actif(conn, 12345) is True


def test_detection_par_macs_inventaire(conn, make_client, make_appareil, monkeypatch):
    cid = make_client()
    for i, mac in enumerate(('a2:bb:cc:00:00:01', 'a2:bb:cc:00:00:02', 'a2:bb:cc:00:00:03')):
        make_appareil(cid, adresse_ip='10.2.0.%d' % (10 + i), adresse_mac=mac)
    monkeypatch.setattr(N, '_table_arp', lambda: {
        '10.2.0.10': {'a2:bb:cc:00:00:01'},
        '10.2.0.11': {'A2:BB:CC:00:00:02'},
        '10.2.0.12': {'a2-bb-cc-00-00-03'},
        '10.2.0.99': {'de:ad:be:ef:99:99'},
    })
    d = S.detecter_site(conn, force=True)
    assert d['client_id'] == cid and d['confiance'] == 'sur_site'
    assert d['macs_reconnues'] == 3
    assert S.site_actif(conn, cid) is True
    assert cid in S.clients_sur_site(conn)


def test_passerelle_reconnue_suffit(conn, make_client, make_appareil, monkeypatch):
    cid = make_client()
    make_appareil(cid, adresse_ip='192.168.1.1', adresse_mac='11:22:33:44:55:66')
    monkeypatch.setattr(N, '_passerelle_defaut', lambda: '192.168.1.1')
    monkeypatch.setattr(N, '_table_arp', lambda: {'192.168.1.1': {'11:22:33:44:55:66'}})
    d = S.detecter_site(conn, force=True)
    assert d['client_id'] == cid and d['passerelle_ok'] is True
    assert d['confiance'] == 'sur_site'


def test_reseau_inconnu_reste_indetermine(conn, make_client, make_appareil, monkeypatch):
    cid = make_client()
    make_appareil(cid, adresse_ip='10.0.0.10', adresse_mac='aa:bb:cc:00:00:01')
    monkeypatch.setattr(N, '_table_arp', lambda: {'172.16.0.5': {'99:99:99:99:99:99'}})
    d = S.detecter_site(conn, force=True)
    assert d['client_id'] is None and d['confiance'] == 'indetermine'
    # rien d'affirmable -> on ne bride pas les fonctions de fond
    assert S.clients_sur_site(conn) is None


def test_grace_survit_a_une_coupure(conn, make_client, make_appareil, monkeypatch):
    cid = make_client()
    make_appareil(cid, adresse_ip='10.4.0.10', adresse_mac='a4:bb:cc:00:00:01')
    make_appareil(cid, adresse_ip='10.4.0.11', adresse_mac='a4:bb:cc:00:00:02')
    make_appareil(cid, adresse_ip='10.4.0.12', adresse_mac='a4:bb:cc:00:00:03')
    monkeypatch.setattr(N, '_table_arp', lambda: {
        '10.4.0.10': {'a4:bb:cc:00:00:01'}, '10.4.0.11': {'a4:bb:cc:00:00:02'},
        '10.4.0.12': {'a4:bb:cc:00:00:03'}})
    assert S.detecter_site(conn, force=True)['confiance'] == 'sur_site'
    # réseau coupé : plus rien en ARP, mais la fenêtre de grâce tient
    monkeypatch.setattr(N, '_table_arp', lambda: {})
    S._cache.update(ts=0.0, res=None)
    assert S.site_actif(conn, cid) is True


def test_consultation_coupe_les_fonctions_de_fond(conn, make_client, make_appareil):
    """En consultation, aucune fonction LIVE de fond ne tourne (pré-chauffe baie,
    surveillance SNMP) — le scan manuel, lui, reste possible (l'utilisateur
    confirme côté page : cas du VLAN isolé / première visite)."""
    cid = make_client()
    make_appareil(cid, adresse_ip='10.0.0.1', adresse_mac='aa:00:00:00:00:aa')
    C.cfg_set('mode_terrain', 'consultation')
    try:
        assert N._filtrer_clients_sur_site([cid]) == []
        assert S.clients_sur_site(conn) == set()
    finally:
        C.cfg_set('mode_terrain', 'auto')


def test_api_site_detection_route(client, conn, make_user, make_client, make_appareil, monkeypatch):
    uid, _l, _p = make_user()
    cid = make_client(auth_user_id=uid)
    make_appareil(cid, adresse_ip='10.1.0.5', adresse_mac='ab:cd:ef:01:02:03')
    login_session(client, uid, cid)
    monkeypatch.setattr(N, '_table_arp', lambda: {'10.1.0.5': {'ab:cd:ef:01:02:03'}})
    S._cache.update(ts=0.0, res=None)
    r = client.get('/api/site/detection?forcer=1')
    assert r.status_code == 200
    d = r.get_json()
    assert d['client_actif'] == cid
    assert d['mode'] == 'auto'
