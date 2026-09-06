"""Ordonnanceur de la surveillance continue (refonte diag réseau, Lot 3).

Vérifie que le balayage SNMP et la topologie tournent sur LEUR PROPRE cadence,
indépendamment des sondes hôte — qui pouvaient les « sauter » sur budget dans
l'ancien cycle monolithique.
"""
import network_diag as N


def _piloter(monkeypatch, t_ref):
    """Fige l'horloge (`t_ref` = liste mutable [now]) et neutralise l'I/O du
    tick. Retourne le journal des sous-tâches exécutées `[(nom, now)]`."""
    import database
    journal = []
    monkeypatch.setattr(N.time, 'time', lambda: t_ref[0])
    monkeypatch.setattr(N, '_moniteur_clients', lambda: [1])
    monkeypatch.setattr(N, '_enregistrer_evenements', lambda *a, **k: 0)
    monkeypatch.setattr(N, '_purger_anciens', lambda *a, **k: None)
    monkeypatch.setattr(database, 'get_db', lambda: _FakeConn())
    monkeypatch.setattr(N, '_cycle_sondes_hote', lambda cid: journal.append(('hote', t_ref[0])) or [])
    monkeypatch.setattr(N, '_cycle_snmp', lambda cid: journal.append(('snmp', t_ref[0])) or [])
    monkeypatch.setattr(N, '_cycle_topo', lambda cid: journal.append(('topo', t_ref[0])) or [])
    monkeypatch.setattr(N, '_cycle_capture', lambda cid: ([], False))
    cfg = {'diag_snmp_actif': '1', 'diag_topologie_active': '1',
           'diag_intervalle_s': '300', 'diag_snmp_intervalle_s': '120',
           'diag_topo_intervalle_s': '900'}
    monkeypatch.setattr(N, '_cfg', lambda k, d=None: cfg.get(k, d))
    monkeypatch.setattr(N, '_cfg_int', lambda k, d: int(cfg.get(k, d)))
    return journal


class _FakeConn:
    def execute(self, *a, **k): return self
    def fetchone(self): return None
    def fetchall(self): return []
    def commit(self): pass
    def close(self): pass


def test_cadences_independantes(monkeypatch):
    t = [1000.0]
    journal = _piloter(monkeypatch, t)
    prochains = {'hote': 0.0, 'snmp': 0.0, 'topo': 0.0}

    # t=1000 : tout est échu
    N._moniteur_tick(prochains)
    assert sorted(n for n, _ in journal) == ['hote', 'snmp', 'topo']
    journal.clear()

    # +130 s : seul le SNMP est de nouveau échu (intervalle 120 s)
    t[0] = 1130.0
    N._moniteur_tick(prochains)
    assert [n for n, _ in journal] == ['snmp']
    journal.clear()

    # +260 s (t=1260) : SNMP encore (3e passage : prochain était 1250)
    t[0] = 1260.0
    N._moniteur_tick(prochains)
    assert [n for n, _ in journal] == ['snmp']
    journal.clear()

    # t=1310 : hôte échu (prochain 1300) ; SNMP pas encore (prochain 1380)
    t[0] = 1310.0
    N._moniteur_tick(prochains)
    assert [n for n, _ in journal] == ['hote']
    journal.clear()

    # t=1950 : topo enfin échue (prochain était 1900), + SNMP, + hôte
    t[0] = 1950.0
    N._moniteur_tick(prochains)
    assert 'topo' in [n for n, _ in journal] and 'snmp' in [n for n, _ in journal]


def test_snmp_desactive_pas_de_slot_snmp(monkeypatch):
    t = [1000.0]
    journal = _piloter(monkeypatch, t)
    monkeypatch.setattr(N, '_cfg', lambda k, d=None: {'diag_snmp_actif': '0',
                                                      'diag_topologie_active': '1'}.get(k, d))
    prochains = {'hote': 0.0, 'snmp': 0.0, 'topo': 0.0}
    N._moniteur_tick(prochains)
    noms = [n for n, _ in journal]
    assert 'snmp' not in noms and 'topo' not in noms   # topo exige le SNMP
    assert 'hote' in noms
