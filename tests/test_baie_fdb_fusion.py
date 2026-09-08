"""Vue d'activité de la baie — un relevé SNMP tronqué (agent lent) ne doit plus
faire « disparaître » les appareils de certains ports au hasard (retour terrain).

Deux mécanismes :
  - `_fusion_fdb(cache, frais)` : fusionne une table MAC incomplète avec le
    dernier relevé complet ;
  - `_maj_noms_interfaces` : un relevé d'interfaces nettement plus court que le
    précédent est fusionné, pas substitué ;
  - `_snmp_walk` transmet un `timeout` au walk de fond.
"""
import network_diag as ND


def test_fusion_fdb_conserve_les_ports_absents_du_releve_tronque():
    cache = {10: {'aa:aa:aa:00:00:01'}, 11: {'aa:aa:aa:00:00:02'},
             12: {'aa:aa:aa:00:00:03', 'aa:aa:aa:00:00:04'}}
    # relevé tronqué : le walk s'est arrêté après l'ifIndex 10
    frais = {10: {'aa:aa:aa:00:00:01'}}
    out = ND._fusion_fdb(cache, frais)
    assert out[10] == {'aa:aa:aa:00:00:01'}
    assert out[11] == {'aa:aa:aa:00:00:02'}          # conservé du cache
    assert out[12] == {'aa:aa:aa:00:00:03', 'aa:aa:aa:00:00:04'}


def test_fusion_fdb_une_mac_qui_a_bouge_suit_le_releve_frais():
    cache = {10: {'aa:aa:aa:00:00:01'}, 11: {'aa:aa:aa:00:00:02'}}
    frais = {12: {'aa:aa:aa:00:00:01'}}              # la MAC .01 est maintenant sur 12
    out = ND._fusion_fdb(cache, frais)
    assert out.get(10, set()) == set()               # retirée de son ancien port
    assert out[11] == {'aa:aa:aa:00:00:02'}
    assert out[12] == {'aa:aa:aa:00:00:01'}


def test_fusion_fdb_ajoute_un_nouveau_port():
    out = ND._fusion_fdb({10: {'m1'}}, {10: {'m1'}, 20: {'m2'}})
    assert out == {10: {'m1'}, 20: {'m2'}}


def test_noms_interfaces_releve_court_est_fusionne(monkeypatch):
    ND._activite_noms.clear()
    complet = {i: {'nom': 'Gi1/0/%d' % i, 'alias': '', 'speed_mbps': 1000,
                   'ethernet': True} for i in range(1, 25)}
    ND._activite_noms['1.2.3.4'] = {'ts': 0, 'infos': complet}

    # _maj_noms_interfaces reconstruit `infos` depuis _snmp_bulk : on simule un
    # relevé tronqué (4 interfaces sur 24).
    def _bulk_court(ip, oids, comm):
        return {ND._OID_IF_NAME: {str(i): 'Gi1/0/%d' % i for i in range(1, 5)},
                ND._OID_IF_TYPE: {str(i): '6' for i in range(1, 5)}}
    monkeypatch.setattr(ND, '_snmp_bulk', _bulk_court)

    infos = ND._maj_noms_interfaces('1.2.3.4', ['public'])
    assert len(infos) == 24                           # fusionné, pas réduit à 4
    assert infos[20]['nom'] == 'Gi1/0/20'


def test_snmp_walk_transmet_le_timeout(monkeypatch):
    vus = {}

    def _faux_app_walk(ip, oid, comm, **kw):
        vus.update(kw)
        return {'1': 'x'}

    import app
    monkeypatch.setattr(app, '_snmp_walk', _faux_app_walk)
    ND._snmp_walk('1.3.6', '10.0.0.1', ['public'], max_vars=50, timeout=3.0)
    assert vus.get('timeout') == 3.0 and vus.get('max_vars') == 50
