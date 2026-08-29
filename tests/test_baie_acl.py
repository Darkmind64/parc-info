"""
ACL sur les routes de mutation de la baie de brassage — aucune de
/api/baie/* ni des routes de photos (/baie/photo/*) ne vérifiait
can_write() jusqu'ici (CLAUDE.md, § Contrôle d'Accès Multi-Client :
"VÉRIFIER ACL ... NON FACULTATIF") : un utilisateur en accès 'lecture'
pouvait placer, modifier, câbler, déplacer ou supprimer n'importe quel
élément de la baie, uploader/éditer/supprimer une photo, ou changer la
hauteur (U) de la baie. Couvre chaque route une par une, avec un
utilisateur en accès 'lecture' pur (ni propriétaire, ni 'ecriture'), et
vérifie que l'état en base n'a pas bougé.
"""
from conftest import login_session, get_csrf_token


def _partage(conn, cid, auth_user_id, niveau):
    conn.execute("INSERT INTO client_partages (client_id, auth_user_id, niveau) VALUES (?,?,?)",
                 (cid, auth_user_id, niveau))
    conn.commit()


def _poser_slot(conn, cid, position=1, col_index=0, nom_custom='SW-ACL', **extra):
    cols = ['client_id', 'position', 'col_index', 'nom_custom'] + list(extra.keys())
    vals = [cid, position, col_index, nom_custom] + list(extra.values())
    placeholders = ','.join('?' * len(cols))
    cur = conn.execute(f"INSERT INTO baie_slots ({','.join(cols)}) VALUES ({placeholders})", vals)
    conn.commit()
    return cur.lastrowid


def _poser_photo(conn, cid, nom='Vue générale'):
    conn.execute(
        "INSERT INTO baie_photos (client_id, nom, description, nom_fichier, taille, date_upload) "
        "VALUES (?, ?, '', 'test.jpg', 100, '2026-01-01T00:00:00')", (cid, nom))
    conn.commit()
    return conn.execute('SELECT id FROM baie_photos WHERE client_id=? AND nom=?', (cid, nom)).fetchone()[0]


def _setup_lecteur(client, make_client, make_user, conn, niveau='lecture'):
    proprietaire_id, _, _ = make_user(role='admin')
    lecteur_id, _, _ = make_user(role='user')
    cid = make_client(auth_user_id=proprietaire_id)
    _partage(conn, cid, lecteur_id, niveau)
    login_session(client, lecteur_id, cid)
    return cid, lecteur_id


def test_ajouter_slot_refuse_en_lecture(client, make_client, make_user, conn):
    cid, _ = _setup_lecteur(client, make_client, make_user, conn)
    r = client.post('/api/baie/slot', json={'position': 1, 'col_index': 0, 'nom_custom': 'INTRUS'})
    assert r.status_code == 403
    assert conn.execute('SELECT COUNT(*) FROM baie_slots WHERE client_id=?', (cid,)).fetchone()[0] == 0


def test_modifier_slot_refuse_en_lecture(client, make_client, make_user, conn):
    cid, _ = _setup_lecteur(client, make_client, make_user, conn)
    sid = _poser_slot(conn, cid)
    r = client.put(f'/api/baie/slot/{sid}', json={'nom_custom': 'DETOURNE', 'position': 1})
    assert r.status_code == 403
    assert conn.execute('SELECT nom_custom FROM baie_slots WHERE id=?', (sid,)).fetchone()[0] == 'SW-ACL'


def test_supprimer_slot_refuse_en_lecture(client, make_client, make_user, conn):
    cid, _ = _setup_lecteur(client, make_client, make_user, conn)
    sid = _poser_slot(conn, cid)
    r = client.delete(f'/api/baie/slot/{sid}')
    assert r.status_code == 403
    assert conn.execute('SELECT COUNT(*) FROM baie_slots WHERE id=?', (sid,)).fetchone()[0] == 1


def test_modifier_port_refuse_en_lecture(client, make_client, make_user, conn):
    cid, _ = _setup_lecteur(client, make_client, make_user, conn)
    sid = _poser_slot(conn, cid, type_equipement='Switch')
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?, 1)", (sid,))
    conn.commit()
    r = client.put(f'/api/baie/slot/{sid}/port/1', json={'usage_libre': 'Détourné'})
    assert r.status_code == 403
    assert conn.execute('SELECT usage_libre FROM baie_slot_ports WHERE slot_id=? AND numero=1',
                         (sid,)).fetchone()[0] == ''


def test_modifier_prise_murale_refuse_en_lecture(client, make_client, make_user, conn):
    cid, _ = _setup_lecteur(client, make_client, make_user, conn)
    sid = _poser_slot(conn, cid, type_equipement='Bandeau RJ')
    conn.execute("INSERT INTO baie_prises_murales (slot_id, numero) VALUES (?, 1)", (sid,))
    conn.commit()
    r = client.put(f'/api/baie/prise-murale/{sid}/1', json={'piece': 'Bureau détourné'})
    assert r.status_code == 403
    assert conn.execute('SELECT piece FROM baie_prises_murales WHERE slot_id=? AND numero=1',
                         (sid,)).fetchone()[0] == ''


def test_lier_ports_refuse_en_lecture(client, make_client, make_user, conn):
    cid, _ = _setup_lecteur(client, make_client, make_user, conn)
    s1 = _poser_slot(conn, cid, position=1, nom_custom='A')
    s2 = _poser_slot(conn, cid, position=2, nom_custom='B')
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?, 1)", (s1,))
    conn.execute("INSERT INTO baie_slot_ports (slot_id, numero) VALUES (?, 1)", (s2,))
    conn.commit()
    r = client.post('/api/baie/lien-port', json={'slot1_id': s1, 'numero1': 1, 'slot2_id': s2, 'numero2': 1})
    assert r.status_code == 403
    assert conn.execute('SELECT lie_slot_id FROM baie_slot_ports WHERE slot_id=? AND numero=1',
                         (s1,)).fetchone()[0] is None


def test_deplacer_slot_refuse_en_lecture(client, make_client, make_user, conn):
    cid, _ = _setup_lecteur(client, make_client, make_user, conn)
    sid = _poser_slot(conn, cid, position=1, col_index=0)
    r = client.post(f'/api/baie/slot/{sid}/deplacer', json={'position': 5, 'col_index': 0})
    assert r.status_code == 403
    assert conn.execute('SELECT position FROM baie_slots WHERE id=?', (sid,)).fetchone()[0] == 1


def test_supprimer_baie_refuse_en_lecture(client, make_client, make_user, conn):
    cid, _ = _setup_lecteur(client, make_client, make_user, conn)
    _poser_slot(conn, cid)
    r = client.delete('/api/baie?baie=Baie principale')
    assert r.status_code == 403
    assert conn.execute('SELECT COUNT(*) FROM baie_slots WHERE client_id=?', (cid,)).fetchone()[0] == 1


def test_nb_u_refuse_en_lecture(client, make_client, make_user, conn):
    cid, _ = _setup_lecteur(client, make_client, make_user, conn)
    conn.execute('INSERT INTO parc_general (client_id, baie_nb_u) VALUES (?, 12)', (cid,))
    conn.commit()
    r = client.post('/api/baie/nb_u', json={'nb_u': 42})
    assert r.status_code == 403
    assert conn.execute('SELECT baie_nb_u FROM parc_general WHERE client_id=?', (cid,)).fetchone()[0] == 12


def test_upload_photo_refuse_en_lecture(client, make_client, make_user, conn):
    cid, _ = _setup_lecteur(client, make_client, make_user, conn)
    csrf = get_csrf_token(client)
    r = client.post('/baie/photo/upload', data={'csrf_token': csrf, 'nom': 'Intrus'}, follow_redirects=True)
    assert r.status_code == 200
    # Le message passe par `msg|tojson` (voir base.html, window._flashMessages)
    # — les accents y sont échappés en \uXXXX, d'où la recherche sur la
    # partie sans accent plutôt que la chaîne affichée telle quelle.
    assert 'lecture seule' in r.get_data(as_text=True)
    assert conn.execute('SELECT COUNT(*) FROM baie_photos WHERE client_id=?', (cid,)).fetchone()[0] == 0


def test_modifier_photo_refuse_en_lecture(client, make_client, make_user, conn):
    cid, _ = _setup_lecteur(client, make_client, make_user, conn)
    pid = _poser_photo(conn, cid)
    csrf = get_csrf_token(client)
    r = client.put(f'/baie/photo/{pid}', json={'nom': 'Détourné'}, headers={'X-CSRF-Token': csrf})
    assert r.status_code == 403
    assert conn.execute('SELECT nom FROM baie_photos WHERE id=?', (pid,)).fetchone()[0] == 'Vue générale'


def test_supprimer_photo_refuse_en_lecture(client, make_client, make_user, conn):
    cid, _ = _setup_lecteur(client, make_client, make_user, conn)
    pid = _poser_photo(conn, cid)
    csrf = get_csrf_token(client)
    r = client.post(f'/baie/photo/{pid}/supprimer', data={'csrf_token': csrf}, follow_redirects=True)
    assert r.status_code == 200
    assert 'lecture seule' in r.get_data(as_text=True)
    assert conn.execute('SELECT COUNT(*) FROM baie_photos WHERE id=?', (pid,)).fetchone()[0] == 1


def test_ajouter_slot_autorise_en_ecriture(client, make_client, make_user, conn):
    """Contrôle négatif : un accès 'ecriture' (pas seulement 'proprietaire')
    doit toujours pouvoir écrire — can_write() couvre les deux, une
    régression qui bloquerait 'ecriture' serait aussi grave que l'absence
    du contrôle."""
    cid, _ = _setup_lecteur(client, make_client, make_user, conn, niveau='ecriture')
    r = client.post('/api/baie/slot', json={'position': 1, 'col_index': 0, 'nom_custom': 'OK-ECRITURE'})
    assert r.status_code == 200
    assert conn.execute('SELECT COUNT(*) FROM baie_slots WHERE client_id=?', (cid,)).fetchone()[0] == 1
