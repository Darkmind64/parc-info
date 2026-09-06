"""Proposition #4 : network_diag.verifier_parc_general recoupe les champs
réseau déclarés du parc avec la réalité observable (indicatif, jamais bloquant).
"""
import network_diag as N


def _parc(conn, cid, **champs):
    cols = ['client_id'] + list(champs)
    vals = [cid] + list(champs.values())
    conn.execute(f"INSERT INTO parc_general ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})", vals)
    conn.commit()


def test_plage_confirmee_et_divergente(conn, make_client, make_appareil, monkeypatch):
    monkeypatch.setattr(N, '_requete_dns_a', lambda *a, **k: True)
    cid = make_client()
    _parc(conn, cid, plage_ip_locale='192.168.1.0/24')
    for i in (10, 11, 12, 13):
        make_appareil(cid, adresse_ip=f'192.168.1.{i}')
    d = N.verifier_parc_general(cid)
    assert d['plage_ip_locale']['etat'] == 'confirme'
    # ajoute 2 appareils hors plage -> divergent (>= 20 %)
    make_appareil(cid, adresse_ip='10.0.5.4')
    make_appareil(cid, adresse_ip='10.0.5.5')
    d = N.verifier_parc_general(cid)
    assert d['plage_ip_locale']['etat'] == 'divergent'
    assert '10.0.5.0/24' in d['plage_ip_locale']['detail']


def test_passerelle_confirmee_par_un_routeur_de_linventaire(conn, make_client, make_appareil, monkeypatch):
    monkeypatch.setattr(N, '_requete_dns_a', lambda *a, **k: True)
    monkeypatch.setattr(N, '_passerelle_defaut', lambda: '')
    cid = make_client()
    _parc(conn, cid, passerelle='192.168.1.1')
    d = N.verifier_parc_general(cid)
    assert d['passerelle']['etat'] == 'non_verifie'
    make_appareil(cid, adresse_ip='192.168.1.1', type_appareil='Routeur/Pare-feu')
    d = N.verifier_parc_general(cid)
    assert d['passerelle']['etat'] == 'confirme'


def test_dns_divergent_si_aucune_reponse(conn, make_client, monkeypatch):
    monkeypatch.setattr(N, '_requete_dns_a', lambda *a, **k: False)
    cid = make_client()
    _parc(conn, cid, serveur_dns='10.9.9.9')
    d = N.verifier_parc_general(cid)
    assert d['serveur_dns']['etat'] == 'divergent'


def test_domaine_confirme_par_les_noms_dns(conn, make_client, make_appareil, monkeypatch):
    monkeypatch.setattr(N, '_requete_dns_a', lambda *a, **k: True)
    cid = make_client()
    _parc(conn, cid, domaine='entreprise.local')
    make_appareil(cid, nom_dns='pc-compta.entreprise.local')
    d = N.verifier_parc_general(cid)
    assert d['domaine']['etat'] == 'confirme'


def test_champs_vides_sont_non_verifies(conn, make_client, monkeypatch):
    monkeypatch.setattr(N, '_requete_dns_a', lambda *a, **k: True)
    cid = make_client()
    _parc(conn, cid)
    d = N.verifier_parc_general(cid)
    assert {v['etat'] for v in d.values()} == {'non_verifie'}
