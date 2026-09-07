"""Diagnostic réseau — passe performance (Lot 7).

Les sondes du poste (ARP, ping, DNS, DHCP, noms, Wi-Fi) et le relevé SNMP des
switchs de la baie tournaient en SÉRIE. Ici on vérifie que :
  - `_executer_sondes` lance vraiment ses tâches en parallèle et isole une tâche
    qui lève ;
  - `mesurer_qualite_liaison` pingue ses cibles en parallèle (durée ≈ la plus
    lente, pas la somme) ;
  - `_sondes_hote` agrège les constats et respecte `avec_dns` / `avec_dhcp` ;
  - `_relever_switch_activite` rend un relevé « muet » exploitable si le switch
    ne répond pas / lève, sans faire tomber les autres.
"""
import time

import network_diag as N


def test_executer_sondes_parallele_et_isole_les_erreurs():
    def _lente():
        time.sleep(0.3)
        return ['a']

    def _casse():
        raise RuntimeError('boum')

    t0 = time.time()
    res = N._executer_sondes({'x': _lente, 'y': _lente, 'z': _casse})
    dt = time.time() - t0

    assert dt < 0.6, f"3 sondes de 0,3 s en parallèle devraient tenir en < 0,6 s (mis {dt:.2f})"
    assert res['x'][0] == ['a'] and res['y'][0] == ['a']
    assert res['z'][0] == []          # la tâche qui lève rend [] sans propager
    assert all(d >= 0 for _, d in res.values())


def test_executer_sondes_vide():
    assert N._executer_sondes({}) == {}


def test_mesurer_qualite_liaison_pingue_en_parallele(monkeypatch):
    appels = []

    def _faux_ping(ip, n=20):
        appels.append(ip)
        time.sleep(0.3)
        return {'ip': ip, 'envoyes': n, 'recus': n, 'perte_pct': 0.0,
                'min': 1.0, 'moy': 1.0, 'max': 1.0, 'gigue': 0.0}

    monkeypatch.setattr(N, '_ping_rafale', _faux_ping)
    cibles = [{'ip': f'10.0.0.{i}', 'libelle': f'C{i}', 'role': 'perso'} for i in range(4)]
    collecte = []
    t0 = time.time()
    findings = N.mesurer_qualite_liaison(cibles, seuil_perte=5, seuil_gigue=30, collecte=collecte)
    dt = time.time() - t0

    assert findings == []
    assert len(collecte) == 4 and {c['ip'] for c in collecte} == {c['ip'] for c in cibles}
    assert dt < 0.9, f"4 cibles × 0,3 s en série feraient 1,2 s ; en parallèle < 0,9 s (mis {dt:.2f})"


def test_mesurer_qualite_liaison_role_passerelle_injoignable(monkeypatch):
    monkeypatch.setattr(N, '_ping_rafale', lambda ip, n=20: {
        'ip': ip, 'envoyes': n, 'recus': 0, 'perte_pct': 100.0,
        'min': None, 'moy': None, 'max': None, 'gigue': None})
    f = N.mesurer_qualite_liaison(
        [{'ip': '192.168.1.1', 'libelle': 'Passerelle', 'role': 'passerelle'}],
        seuil_perte=5, seuil_gigue=30)
    assert len(f) == 1 and f[0]['categorie'] == 'passerelle_injoignable'


def test_sondes_hote_agrege_et_respecte_les_bascules(monkeypatch):
    lances = []

    def _note(nom, ret=None):
        def _f(*a, **k):
            lances.append(nom)
            return ret or []
        return _f

    monkeypatch.setattr(N, '_passerelle_defaut', lambda: '192.168.1.254')
    monkeypatch.setattr(N, '_cibles_ping', lambda cid, p: [
        {'ip': '192.168.1.254', 'libelle': 'Passerelle', 'role': 'passerelle'},
        {'ip': '1.1.1.1', 'libelle': 'DNS 1.1.1.1', 'role': 'dns'}])
    monkeypatch.setattr(N, '_cfg', lambda k, d=None: '1' if k == 'diag_wifi_active' else (d or ''))
    monkeypatch.setattr(N, 'detecter_conflits_ip', _note('arp', [{'x': 1}]))
    monkeypatch.setattr(N, 'mesurer_qualite_liaison', _note('liaison'))
    monkeypatch.setattr(N, 'detecter_conflits_noms', _note('noms'))
    monkeypatch.setattr(N, 'verifier_dns', _note('dns'))
    monkeypatch.setattr(N, 'detecter_dhcp_pirate', _note('dhcp'))
    monkeypatch.setattr(N, 'diagnostiquer_wifi', _note('wifi'))

    findings, stats, cibles, phases = N._sondes_hote(1)
    assert {'x': 1} in findings
    assert set(lances) == {'arp', 'liaison', 'noms', 'dns', 'dhcp', 'wifi'}
    assert set(phases) >= {'arp', 'liaison', 'noms', 'dns', 'dhcp'}
    assert len(cibles) == 2

    lances.clear()
    N._sondes_hote(1, avec_dns=False, avec_dhcp=False)
    assert 'dns' not in lances and 'dhcp' not in lances
    assert 'arp' in lances and 'liaison' in lances


def test_relever_switch_activite_muet_si_le_switch_leve(monkeypatch):
    monkeypatch.setattr(N, '_presence_baie_ok', lambda *a, **k: True)
    monkeypatch.setattr(N, '_releve_mac_switch',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('agent muet')))
    r = N._relever_switch_activite(1, '10.0.0.9', 42, 'SW', ['public'], {})
    infos, cur_ports, ok, hc, dt, reboot, poe, sysinfo, uptime = r['poll']
    assert ok is False and cur_ports == {} and r['fdb'] == {}
    assert r['calib'] is None and r['journal'] == []


def test_relever_switch_activite_sans_fdb_saute_le_walk(monkeypatch):
    appels = {'fdb': 0}
    monkeypatch.setattr(N, '_presence_baie_ok', lambda *a, **k: True)
    monkeypatch.setattr(N, '_releve_mac_switch',
                        lambda *a, **k: appels.__setitem__('fdb', appels['fdb'] + 1) or ({}, {}))
    monkeypatch.setattr(N, '_noms_interfaces', lambda ip, c: {})
    monkeypatch.setattr(N, '_poll_switch_ports', lambda ip, c, infos=None: ({}, True, False, None))
    monkeypatch.setattr(N, '_poll_poe', lambda ip, c: {})
    monkeypatch.setattr(N, '_lire_sysinfo', lambda ip, c: {'sysname': 'SW', 'sysdescr': ''})
    monkeypatch.setattr(N, '_maj_assistant_calibration', lambda *a, **k: None)

    N._relever_switch_activite(1, '10.0.0.9', 42, 'SW', ['public'], {}, avec_fdb=False)
    assert appels['fdb'] == 0
    N._relever_switch_activite(1, '10.0.0.9', 42, 'SW', ['public'], {}, avec_fdb=True)
    assert appels['fdb'] == 1
