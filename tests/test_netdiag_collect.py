"""Collecteur SNMP unifié (netdiag.collect) — refonte diagnostic réseau, Lot 1."""
import time

import pytest

from netdiag import collect


def _table_switch(nb_ports=4):
    """Sortie plausible de _snmp_bulk_cols pour un switch nb_ports ethernet."""
    r = range(1, nb_ports + 1)
    return {
        collect._IF_DESCR:     {str(i): f'Gi1/0/{i}' for i in r},
        collect._IF_TYPE:      {str(i): 6 for i in r},
        collect._IF_NAME:      {str(i): f'Gi1/0/{i}' for i in r},
        collect._IF_ALIAS:     {'1': 'PC-Compta'},
        collect._IF_OPER:      {str(i): 1 for i in r},
        collect._IF_ADMIN:     {str(i): 1 for i in r},
        collect._IF_HIGHSPEED: {str(i): 1000 for i in r},
        collect._IF_HCIN:      {str(i): 1000 * i for i in r},
        collect._IF_HCOUT:     {str(i): 500 * i for i in r},
        collect._IF_IN_ERRORS: {str(i): 0 for i in r},
        collect._DOT3_FCS:     {str(i): 0 for i in r},
    }


@pytest.fixture
def bouchons(monkeypatch):
    """Remplace les primitives SNMP d'app par des bouchons synchrones."""
    import app as A
    appels = {'presence': [], 'bulk': [], 'typed': []}

    def _presence(ip, communautes=('public',), port=161, timeout=1.2):
        appels['presence'].append(ip)
        return True, True, 'v1/v2c (public)'

    def _bulk(ip, bases, comm=('public',), timeout=1.5, **k):
        appels['bulk'].append((ip, tuple(bases)))
        t = _table_switch()
        return {b: dict(t.get(b, {})) for b in bases}

    def _typed(ip, oids, communaute='public', timeout=1.0, port=161, **k):
        appels['typed'].append(ip)
        return {A._OID_SYS_NAME: 'SW-T', A._OID_SYS_DESCR: 'desc'}

    monkeypatch.setattr(A, '_snmp_presence', _presence)
    monkeypatch.setattr(A, '_snmp_bulk_cols', _bulk)
    monkeypatch.setattr(A, '_snmp_get_typed', _typed)
    collect.vider_cache()
    return appels


def test_balayer_forme_compatible_interroger_equipement(bouchons):
    eq = [(10, '10.0.0.1', 'Switch'), (11, '10.0.0.2', 'Routeur/Pare-feu')]
    res = collect.balayer(0, besoins=('compteurs', 'dot3'), communautes=['public'],
                          equipements=eq)
    assert set(res.releves) == {'10.0.0.1', '10.0.0.2'}
    rv = res.releves['10.0.0.1']
    assert rv.appareil_id == 10 and rv.snmp_ok
    d = rv.equipement
    # forme exacte attendue par _analyser_snmp
    assert set(d) == {'sysname', 'ts', 'ports', 'hc'}
    assert d['sysname'] == 'SW-T' and d['hc'] is True and d['ts'] > 0
    p1 = next(p for p in d['ports'] if p['index'] == 1)
    assert p1['alias'] == 'PC-Compta' and p1['speed_mbps'] == 1000
    assert p1['in_oct'] == 1000 and p1['out_oct'] == 500
    assert set(p1) >= {'index', 'nom', 'alias', 'oper', 'admin', 'speed_mbps',
                       'in_oct', 'out_oct', 'in_err', 'out_err', 'in_disc',
                       'out_disc', 'align_err', 'fcs_err', 'late_coll',
                       'exc_coll', 'duplex'}


def test_balayer_une_seule_passe_par_equipement(bouchons):
    """Chaque équipement : 1 sonde de présence + 1 GETBULK multi-colonnes
    (+ 1 GET sysName), pas 3 GETBULK comme l'ancienne boucle."""
    eq = [(10, '10.0.0.1', 'Switch')]
    collect.balayer(0, besoins=('compteurs', 'dot3'), communautes=['public'], equipements=eq)
    assert bouchons['presence'] == ['10.0.0.1']
    assert len(bouchons['bulk']) == 1
    ip, bases = bouchons['bulk'][0]
    assert ip == '10.0.0.1'
    # toutes les colonnes ifTable+ifXTable+dot3 dans le MÊME appel
    assert collect._IF_DESCR in bases and collect._IF_HCIN in bases and collect._DOT3_FCS in bases


def test_balayer_parallele(monkeypatch):
    """8 équipements lents (0,2 s/appel) doivent finir bien plus vite que la
    somme séquentielle (~0,6 s × 8 = 4,8 s)."""
    import app as A
    lat = 0.2

    def _slow_presence(ip, communautes=('public',), port=161, timeout=1.2):
        time.sleep(lat); return True, True, 'ok'

    def _slow_bulk(ip, bases, comm=('public',), timeout=1.5, **k):
        time.sleep(lat)
        t = _table_switch()
        return {b: dict(t.get(b, {})) for b in bases}

    def _slow_typed(ip, oids, communaute='public', timeout=1.0, port=161, **k):
        time.sleep(lat); return {A._OID_SYS_NAME: 'x'}

    monkeypatch.setattr(A, '_snmp_presence', _slow_presence)
    monkeypatch.setattr(A, '_snmp_bulk_cols', _slow_bulk)
    monkeypatch.setattr(A, '_snmp_get_typed', _slow_typed)
    collect.vider_cache()
    eq = [(i, f'10.0.1.{i}', 'Switch') for i in range(1, 9)]
    t0 = time.time()
    res = collect.balayer(0, communautes=['public'], equipements=eq, workers=8)
    d = time.time() - t0
    assert len(res.releves) == 8
    assert d < 2.0, f"balayage parallèle trop lent : {d:.2f}s (séquentiel ≈ 4,8s)"


def test_balayer_budget_muets(monkeypatch):
    """Sous budget, les équipements non terminés partent dans `muets` avec un
    motif — jamais un balayage sauté en silence."""
    import app as A

    def _very_slow(ip, communautes=('public',), port=161, timeout=1.2):
        time.sleep(3.0); return True, True, 'ok'

    monkeypatch.setattr(A, '_snmp_presence', _very_slow)
    monkeypatch.setattr(A, '_snmp_bulk_cols', lambda *a, **k: {})
    collect.vider_cache()
    eq = [(i, f'10.0.2.{i}', 'Switch') for i in range(1, 5)]
    res = collect.balayer(0, communautes=['public'], equipements=eq, budget_s=1.0, workers=4)
    assert res.budget_atteint
    assert len(res.muets) == 4
    assert all('budget' in m['detail'] for m in res.muets)


def test_balayer_agent_muet_coupe_court(bouchons, monkeypatch):
    import app as A
    monkeypatch.setattr(A, '_snmp_presence',
                        lambda ip, c=('public',), port=161, timeout=1.2: (False, False, 'aucune réponse SNMP'))
    res = collect.balayer(0, communautes=['public'], equipements=[(1, '10.0.3.1', 'Switch')])
    rv = res.releves['10.0.3.1']
    assert not rv.snmp_ok and rv.equipement is None
    assert res.muets == [{'ip': '10.0.3.1', 'detail': 'aucune réponse SNMP'}]
    # pas de GETBULK tenté sur un agent muet
    assert bouchons['bulk'] == []


def test_balayer_dedup_par_ip(bouchons):
    """Un même switch présent dans deux slots de baie n'est relevé qu'une fois."""
    eq = [(10, '10.0.0.1', 'Switch'), (10, '10.0.0.1', 'Switch'), (11, '10.0.0.2', 'Switch')]
    res = collect.balayer(0, communautes=['public'], equipements=eq)
    assert sorted(res.releves) == ['10.0.0.1', '10.0.0.2']
    assert bouchons['presence'].count('10.0.0.1') == 1


def test_balayer_exclut_ups_par_defaut(bouchons):
    eq = [(1, '10.0.0.1', 'Onduleur / UPS'), (2, '10.0.0.2', 'Switch')]
    res = collect.balayer(0, communautes=['public'], equipements=eq)
    assert list(res.releves) == ['10.0.0.2']


def test_meta_cache_seconde_passe_ne_reparcourt_pas_les_metadonnees(bouchons):
    """2e cycle : les colonnes quasi statiques (ifDescr/ifName/ifAlias/vitesse)
    ne sont PAS redemandées, mais le relevé reste complet (servi du cache)."""
    eq = [(10, '10.0.0.1', 'Switch')]
    collect.balayer(0, besoins=('compteurs', 'dot3'), communautes=['public'], equipements=eq)
    collect.balayer(0, besoins=('compteurs', 'dot3'), communautes=['public'], equipements=eq)
    bases_c1 = set(bouchons['bulk'][0][1])
    bases_c2 = set(bouchons['bulk'][1][1])
    assert collect._IF_DESCR in bases_c1 and collect._IF_NAME in bases_c1
    assert collect._IF_DESCR not in bases_c2 and collect._IF_ALIAS not in bases_c2
    assert collect._IF_OPER in bases_c2 and collect._IF_HCIN in bases_c2   # état + compteurs toujours
    # relevé du 2e cycle complet malgré tout
    rv = collect.balayer(0, besoins=('compteurs',), communautes=['public'],
                         equipements=eq).releves['10.0.0.1']
    p1 = next(p for p in rv.equipement['ports'] if p['index'] == 1)
    assert p1['alias'] == 'PC-Compta' and p1['speed_mbps'] == 1000 and p1['nom'] == 'Gi1/0/1'


def test_meta_cache_rafraichi_si_port_ajoute(monkeypatch):
    """Un port qui apparaît dans les compteurs et pas dans le cache force un
    relevé neuf des métadonnées dans le même cycle."""
    import app as A
    monkeypatch.setattr(A, '_snmp_presence',
                        lambda ip, c=('public',), port=161, timeout=1.2: (True, True, 'ok'))
    monkeypatch.setattr(A, '_snmp_get_typed',
                        lambda ip, oids, communaute='public', timeout=1.0, port=161, **k: {})
    etat = {'ports': 4}
    bulk_appels = []

    def _bulk(ip, bases, comm=('public',), timeout=1.5, **k):
        bulk_appels.append(tuple(bases))
        r = range(1, etat['ports'] + 1)
        t = {
            collect._IF_DESCR: {str(i): f'Gi1/0/{i}' for i in r},
            collect._IF_NAME:  {str(i): f'Gi1/0/{i}' for i in r},
            collect._IF_TYPE:  {str(i): 6 for i in r},
            collect._IF_OPER:  {str(i): 1 for i in r},
            collect._IF_ADMIN: {str(i): 1 for i in r},
            collect._IF_HCIN:  {str(i): 10 * i for i in r},
            collect._IF_HCOUT: {str(i): 10 * i for i in r},
        }
        return {b: dict(t.get(b, {})) for b in bases}

    monkeypatch.setattr(A, '_snmp_bulk_cols', _bulk)
    collect.vider_cache()
    eq = [(10, '10.0.0.1', 'Switch')]
    collect.balayer(0, communautes=['public'], equipements=eq)          # cache 4 ports
    etat['ports'] = 6                                                   # 2 ports ajoutés
    bulk_appels.clear()
    rv = collect.balayer(0, communautes=['public'], equipements=eq).releves['10.0.0.1']
    # un 2e GETBULK métadonnées a été déclenché dans le cycle
    assert any(collect._IF_DESCR in b and collect._IF_OPER not in b for b in bulk_appels)
    assert {p['index'] for p in rv.equipement['ports']} == {1, 2, 3, 4, 5, 6}
    assert all(p['nom'] == f"Gi1/0/{p['index']}" for p in rv.equipement['ports'])


def test_meta_cache_agent_muet_ne_fabrique_pas_de_releve(monkeypatch):
    """Cache présent mais l'agent ne répond plus ce cycle : relevé muet, pas
    un faux relevé bâti sur les seules métadonnées en cache."""
    import app as A
    monkeypatch.setattr(A, '_snmp_presence',
                        lambda ip, c=('public',), port=161, timeout=1.2: (True, True, 'ok'))
    monkeypatch.setattr(A, '_snmp_get_typed',
                        lambda *a, **k: {})
    reponses = [_table_switch(), {}]

    def _bulk(ip, bases, comm=('public',), timeout=1.5, **k):
        t = reponses[min(len(_bulk.n), 1)]
        _bulk.n.append(1)
        return {b: dict(t.get(b, {})) for b in bases}
    _bulk.n = []

    monkeypatch.setattr(A, '_snmp_bulk_cols', _bulk)
    collect.vider_cache()
    eq = [(10, '10.0.0.1', 'Switch')]
    collect.balayer(0, communautes=['public'], equipements=eq)
    res = collect.balayer(0, communautes=['public'], equipements=eq)
    rv = res.releves['10.0.0.1']
    assert not rv.snmp_ok and rv.equipement is None
    assert res.muets and 'aucune réponse' in res.muets[0]['detail']


def test_meta_cache_port_retire_ne_ressurgit_pas(monkeypatch):
    """Un port disparu du relevé frais n'est pas réinjecté depuis le cache."""
    import app as A
    monkeypatch.setattr(A, '_snmp_presence',
                        lambda ip, c=('public',), port=161, timeout=1.2: (True, True, 'ok'))
    monkeypatch.setattr(A, '_snmp_get_typed', lambda *a, **k: {})
    etat = {'ports': 4}

    def _bulk(ip, bases, comm=('public',), timeout=1.5, **k):
        r = range(1, etat['ports'] + 1)
        meta_r = range(1, 5)   # le cache connaîtra toujours 4 ports
        t = {
            collect._IF_DESCR: {str(i): f'Gi1/0/{i}' for i in meta_r},
            collect._IF_NAME:  {str(i): f'Gi1/0/{i}' for i in meta_r},
            collect._IF_TYPE:  {str(i): 6 for i in meta_r},
            collect._IF_OPER:  {str(i): 1 for i in r},
            collect._IF_ADMIN: {str(i): 1 for i in r},
            collect._IF_HCIN:  {str(i): 10 for i in r},
        }
        return {b: dict(t.get(b, {})) for b in bases}

    monkeypatch.setattr(A, '_snmp_bulk_cols', _bulk)
    collect.vider_cache()
    eq = [(10, '10.0.0.1', 'Switch')]
    collect.balayer(0, communautes=['public'], equipements=eq)
    etat['ports'] = 2                                        # 2 ports retirés
    rv = collect.balayer(0, communautes=['public'], equipements=eq).releves['10.0.0.1']
    assert {p['index'] for p in rv.equipement['ports']} == {1, 2}


def test_rafraichir_meta_force_le_releve(bouchons):
    eq = [(10, '10.0.0.1', 'Switch')]
    collect.balayer(0, communautes=['public'], equipements=eq)
    collect.balayer(0, communautes=['public'], equipements=eq, rafraichir_meta=True)
    assert collect._IF_DESCR in set(bouchons['bulk'][1][1])


def test_releve_frais(bouchons):
    collect.balayer(0, communautes=['public'], equipements=[(1, '10.0.0.9', 'Switch')])
    rv = collect.releve_frais('10.0.0.9', max_age=60)
    assert rv is not None and rv.ip == '10.0.0.9'
    # trop vieux -> None
    collect._cache['10.0.0.9'].ts = time.time() - 999
    assert collect.releve_frais('10.0.0.9', max_age=60) is None
