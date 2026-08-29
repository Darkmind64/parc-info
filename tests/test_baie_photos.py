"""
Édition d'une photo de la baie (nom/description) — PUT /baie/photo/<id>.

Avant cette route, nom/description n'étaient modifiables qu'à l'upload
(voir upload_photo_baie) : aucun moyen de corriger une photo mal nommée
sans la supprimer puis la re-uploader. La création/suppression de la photo
elle-même (upload multipart, signature de fichier) n'est pas retestée ici
— une ligne insérée directement en base suffit à isoler ce qui est
réellement nouveau.

Hors /api/* (route sœur de /baie/photo/upload et /supprimer, pas un
endpoint collecteur) : le CSRF s'applique — voir test_csrf_requis, et
X-CSRF-Token sur chaque autre appel (même en-tête que côté client, voir
PhotoModal.enregistrer() dans baie_brassage.html — request.form uniquement
sinon, ou X-CSRF-Token, jamais le corps JSON, voir auth_utils.py)."""
from conftest import login_session, get_csrf_token


def _poser_photo(conn, cid, nom='Vue générale', description=''):
    conn.execute(
        "INSERT INTO baie_photos (client_id, nom, description, nom_fichier, taille, date_upload) "
        "VALUES (?, ?, ?, 'test.jpg', 100, '2026-01-01T00:00:00')",
        (cid, nom, description))
    conn.commit()
    return conn.execute('SELECT id FROM baie_photos WHERE client_id=? AND nom=?', (cid, nom)).fetchone()[0]


def test_modifier_photo_met_a_jour_nom_et_description(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    pid = _poser_photo(conn, cid)
    csrf = get_csrf_token(client)

    r = client.put(f'/baie/photo/{pid}', json={'nom': 'Face arrière', 'description': 'Après rangement câbles'},
                    headers={'X-CSRF-Token': csrf})
    assert r.status_code == 200
    data = r.get_json()
    assert data['ok'] is True
    assert data['nom'] == 'Face arrière'
    assert data['description'] == 'Après rangement câbles'

    row = conn.execute('SELECT nom, description FROM baie_photos WHERE id=?', (pid,)).fetchone()
    assert row[0] == 'Face arrière'
    assert row[1] == 'Après rangement câbles'


def test_modifier_photo_nom_requis(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    pid = _poser_photo(conn, cid, nom='Original')
    csrf = get_csrf_token(client)

    r = client.put(f'/baie/photo/{pid}', json={'nom': '   ', 'description': 'peu importe'},
                    headers={'X-CSRF-Token': csrf})
    assert r.status_code == 400

    row = conn.execute('SELECT nom FROM baie_photos WHERE id=?', (pid,)).fetchone()
    assert row[0] == 'Original', "un nom vide ne doit jamais écraser le nom existant"


def test_modifier_photo_isolation_client(client, make_client, make_user, conn):
    """Un utilisateur ne doit pas pouvoir éditer la photo d'un AUTRE client
    en devinant/incrémentant l'id dans l'URL — même faille IDOR déjà
    couverte pour appareils/contrats ailleurs dans ce fichier."""
    uid, _, _ = make_user()
    cid_a = make_client(auth_user_id=uid, nom='Client A')
    cid_b = make_client(auth_user_id=uid, nom='Client B')
    pid_b = _poser_photo(conn, cid_b, nom='Photo de B')

    login_session(client, uid, cid_a)
    csrf = get_csrf_token(client)
    r = client.put(f'/baie/photo/{pid_b}', json={'nom': 'Détourné'}, headers={'X-CSRF-Token': csrf})
    assert r.status_code == 404

    row = conn.execute('SELECT nom FROM baie_photos WHERE id=?', (pid_b,)).fetchone()
    assert row[0] == 'Photo de B', "la photo du client B ne doit pas avoir été modifiée"


def test_modifier_photo_introuvable(client, make_client, make_user):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    csrf = get_csrf_token(client)
    r = client.put('/baie/photo/999999', json={'nom': 'X'}, headers={'X-CSRF-Token': csrf})
    assert r.status_code == 404


def test_modifier_photo_csrf_requis(client, make_client, make_user, conn):
    uid, _, _ = make_user()
    cid = make_client(auth_user_id=uid)
    login_session(client, uid, cid)
    pid = _poser_photo(conn, cid)

    r = client.put(f'/baie/photo/{pid}', json={'nom': 'Sans CSRF'})
    assert r.status_code == 403

    row = conn.execute('SELECT nom FROM baie_photos WHERE id=?', (pid,)).fetchone()
    assert row[0] == 'Vue générale'
