"""Recherche globale (palette Ctrl+K) — `search_utils` + `/api/search`.

Normalisation du terme (IP / MAC / numéro), classement, et surtout l'ACL :
un client B ne fuite jamais en scope `actif`, et n'apparaît en scope `tous`
que si l'utilisateur y a accès. Le mot de passe d'un identifiant ne sort
jamais.
"""
import search_utils
from search_utils import _normaliser_terme, _score, search_global
from conftest import login_session, get_csrf_token


def test_normaliser_terme():
    assert _normaliser_terme('192.168.1.10')['ip'] == '192.168.1.10'
    assert _normaliser_terme('999.1.1.1')['ip'] is None            # pas une IP valide
    assert _normaliser_terme('aa:bb:cc:dd:ee:ff')['mac'] == 'aabbccddeeff'
    assert _normaliser_terme('AABBCCDDEEFF')['mac'] == 'aabbccddeeff'
    assert _normaliser_terme('aa-bb-cc-dd')['mac'] == 'aabbccdd'    # MAC partielle
    assert _normaliser_terme('SN-12345')['est_num'] is True
    assert _normaliser_terme('PC compta')['ip'] is None and _normaliser_terme('PC compta')['mac'] is None


def test_score_exact_prefixe_souschaine():
    t = _normaliser_terme('compta')
    assert _score(t, 'compta') == 100
    assert _score(t, 'COMPTABILITE') == 60          # préfixe
    assert _score(t, 'poste-compta-01') == 30       # sous-chaîne
    assert _score(t, 'serveur') == 0


def test_acl_stricte_scope_actif_et_tous(conn, make_client, make_user):
    ua, _l, _p = make_user()
    a = make_client(nom='ClientA', auth_user_id=ua)
    b = make_client(nom='ClientB')          # PAS accessible à `ua`
    for cid, nom in ((a, 'PC-ALPHA-SECRET'), (b, 'PC-ALPHA-SECRET')):
        conn.execute("INSERT INTO appareils (client_id, nom_machine, type_appareil) "
                     "VALUES (?,?, 'PC')", (cid, nom))
    conn.commit()

    # scope actif = client A seul
    r = search_global('ALPHA-SECRET', [a], actif_id=a)
    assert len(r['appareils']) == 1 and r['appareils'][0]['client_id'] == a
    # scope "tous" mais on ne passe QUE les ids accessibles -> B invisible
    r2 = search_global('ALPHA-SECRET', [a], actif_id=a)
    assert all(x['client_id'] == a for x in r2['appareils'])
    # si B était accessible, il apparaîtrait (et serait étiqueté)
    r3 = search_global('ALPHA-SECRET', [a, b], actif_id=a)
    assert {x['client_id'] for x in r3['appareils']} == {a, b}
    assert r3['appareils'][0]['client_id'] == a      # client actif d'abord


def test_recherche_mac_avec_ou_sans_separateur(conn, make_client):
    cid = make_client()
    aid = conn.execute("INSERT INTO appareils (client_id, nom_machine, adresse_mac) "
                       "VALUES (?, 'SRV', 'AA:BB:CC:11:22:33')", (cid,)).lastrowid
    conn.execute("INSERT INTO appareil_macs (appareil_id, client_id, adresse_mac, source, date_maj) "
                 "VALUES (?,?, 'DD:EE:FF:44:55:66', 'manuel', '')", (aid, cid))
    conn.commit()
    for terme in ('aa:bb:cc:11:22:33', 'AABBCC112233', 'dd-ee-ff-44-55-66'):
        r = search_global(terme, [cid], actif_id=cid)
        assert any(x['id'] == aid for x in r['appareils']), terme


def test_ip_exacte_score_max(conn, make_client):
    cid = make_client()
    conn.execute("INSERT INTO appareils (client_id, nom_machine, adresse_ip) "
                 "VALUES (?, 'GW', '10.0.0.1')", (cid,))
    conn.execute("INSERT INTO appareils (client_id, nom_machine, notes) "
                 "VALUES (?, 'DOC', 'voir 10.0.0.100')", (cid,))
    conn.commit()
    r = search_global('10.0.0.1', [cid], actif_id=cid)
    assert r['appareils'] and r['appareils'][0]['titre'] == 'GW'


def test_identifiant_sans_mot_de_passe(conn, make_client, make_identifiant):
    cid = make_client()
    make_identifiant(cid, nom='VPN Fortinet', login='admin', mot_de_passe='TopSecret42!')
    r = search_global('Fortinet', [cid], actif_id=cid)
    assert r['identifiants'] and r['identifiants'][0]['titre'] == 'VPN Fortinet'
    import json
    assert 'TopSecret42' not in json.dumps(r)


def test_route_scope_et_acl(client, conn, make_user, make_client):
    ua, _l, _p = make_user()
    a = make_client(nom='ACME', auth_user_id=ua)
    b = make_client(nom='AutreBoite')      # pas partagé à ua
    conn.execute("INSERT INTO appareils (client_id, nom_machine, type_appareil) VALUES (?, 'POSTE-XZ', 'PC')", (a,))
    conn.execute("INSERT INTO appareils (client_id, nom_machine, type_appareil) VALUES (?, 'POSTE-XZ', 'PC')", (b,))
    conn.commit()
    login_session(client, ua, a)

    d1 = client.get('/api/search?q=POSTE-XZ&scope=actif').get_json()
    assert d1['total'] == 1
    d2 = client.get('/api/search?q=POSTE-XZ&scope=tous').get_json()
    assert d2['total'] == 1          # B inaccessible -> toujours 1, pas 2
    assert d2['scope'] == 'tous'
    assert client.get('/api/search?q=a').get_json()['total'] == 0   # < 2 caractères


def test_route_trouve_le_client_par_nom(client, conn, make_user, make_client):
    ua, _l, _p = make_user()
    a = make_client(nom='Boulangerie Durand', auth_user_id=ua)
    login_session(client, ua, a)
    d = client.get('/api/search?q=Durand&scope=tous').get_json()
    assert any(x['id'] == a and x['url'].endswith('/selectionner') for x in d.get('clients', []))
