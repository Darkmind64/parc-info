"""Analyse SNMP par port + classification en clair (netdiag.analyse) — Lot 2."""
from netdiag import analyse


def _port(**o):
    p = dict(index=1, nom='Gi1/0/1', alias='', oper=1, admin=1, speed_mbps=1000,
             in_oct=0, out_oct=0, in_err=0, out_err=0, in_disc=0, out_disc=0,
             align_err=0, fcs_err=0, late_coll=0, exc_coll=0, duplex=3)
    p.update(o)
    return p


def _prec(**o):
    return {k: 0 for k in analyse.COMPTEURS} | o | {'epoch': 1000.0}


# ─── classer_erreur : chaque classe ─────────────────────────────────────────

def test_classe_duplex_par_late_collisions():
    d = analyse.deltas_port(_port(late_coll=5), _prec())
    c = analyse.classer_erreur(_port(late_coll=5), d)
    assert c['classe'] == 'duplex' and c['gravite'] == 'critique'


def test_classe_duplex_par_half_duplex():
    c = analyse.classer_erreur(_port(duplex=2), analyse.deltas_port(_port(duplex=2), _prec()))
    assert c['classe'] == 'duplex'


def test_classe_physique_crc_domine():
    p = _port(fcs_err=300, align_err=10)
    c = analyse.classer_erreur(p, analyse.deltas_port(p, _prec()))
    assert c['classe'] == 'physique' and 'CRC' in c['libelle']


def test_classe_saturation_rejets_sans_erreurs():
    p = _port(in_disc=500)
    c = analyse.classer_erreur(p, analyse.deltas_port(p, _prec()))
    assert c['classe'] == 'saturation'


def test_classe_erreurs_indeterminees():
    p = _port(in_err=40)
    c = analyse.classer_erreur(p, analyse.deltas_port(p, _prec()))
    assert c['classe'] == 'erreurs'


def test_classe_vide_port_sain():
    p = _port()
    assert analyse.classer_erreur(p, analyse.deltas_port(p, _prec()))['classe'] == ''


# ─── deltas robustes au bouclage ───────────────────────────────────────────

def test_delta_bouclage_32bits():
    # compteur repart de ~0 apres avoir depasse 2**32 : delta reel, pas la valeur brute
    assert analyse._delta(50, 2 ** 32 - 100) == 150


def test_delta_reset_agent():
    # prev petit + delta negatif = redemarrage agent -> on repart de la valeur brute
    assert analyse._delta(30, 200) == 30


# ─── analyser_equipement : findings + lignes d'etat ────────────────────────

def test_analyser_equipement_findings_et_etat():
    ports = [_port(index=1, duplex=2), _port(index=2, fcs_err=999)]
    prec = {1: _prec(), 2: _prec()}
    dt = {1: 30.0, 2: 30.0}
    findings, lignes = analyse.analyser_equipement(
        '10.0.0.1', 'sw', ports, prec, dt, {'erreurs': 50, 'saturation_pct': 90}, hc=False)
    cats = sorted(c for c, *_ in findings)
    assert 'duplex_mismatch' in cats and 'port_crc' in cats
    l2 = next(l for l in lignes if l['port_index'] == 2)
    assert l2['classe_erreur'] == 'physique'
    assert l2['crc_min'] > 0
    l1 = next(l for l in lignes if l['port_index'] == 1)
    assert l1['classe_erreur'] == 'duplex' and l1['gravite'] == 'critique'


def test_analyser_equipement_port_sain_pas_de_finding():
    ports = [_port()]
    findings, lignes = analyse.analyser_equipement(
        '10.0.0.1', 'sw', ports, {1: _prec()}, {1: 30.0},
        {'erreurs': 50, 'saturation_pct': 90}, hc=False)
    assert findings == []
    assert lignes[0]['classe_erreur'] == ''


def test_analyser_equipement_sans_precedent_aucun_delta():
    """Premier relevé : pas de findings de delta (mais vitesse_reduite reste possible)."""
    ports = [_port(fcs_err=99999)]
    findings, _ = analyse.analyser_equipement(
        '10.0.0.1', 'sw', ports, {}, {}, {'erreurs': 50, 'saturation_pct': 90}, hc=False)
    assert [c for c, *_ in findings] == []
