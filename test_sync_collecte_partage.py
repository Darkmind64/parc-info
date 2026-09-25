#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Régression : les résultats du collecteur ne doivent plus « disparaître » de
la synchronisation multi-instance (signalé : client créé par un utilisateur,
scan + collecte, puis partage à un autre utilisateur — la liste des appareils
arrivait mais sans les résultats de la collecte).

Causes couvertes :
  1. /api/device-info ne bumpait pas `appareils.date_maj` : le rapport collecté
     paraissait aussi vieux que la fiche (scan initial) détenue par une autre
     instance, dont un ping en attente de push gagnait alors dans
     `_proteger_versions_locales` et repoussait sa ligne périmée sur Turso.
  2. TursoConnection._pipeline traitait un refus HTTP (corps trop gros…) comme
     un succès vide → journal purgé sans rien écrire sur Turso.
  3. pipeline_exec envoyait jusqu'à 150 lignes (donc 150 rapports de 1 Mo) dans
     une seule requête → découpage par taille.
  4. Le partage (client_partages) et les tables de collecte à clé texte
     (collectes, cles_recuperation) se répliquent bien vers l'autre instance.

Usage : python test_sync_collecte_partage.py
"""

import io
import os
import sqlite3
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='sync_collecte_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import database as D  # noqa: E402
import app as A       # noqa: E402

echecs = []


def verifier(cond, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if cond else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not cond:
        echecs.append(libelle)


# ── 1. Bout en bout : la collecte bumpe date_maj ─────────────────────────────
print('=== 1. /api/device-info met à jour date_maj de la fiche ===')
A.init_db()
conn = A.get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (1, 'admin', 'x', 'Administrateur', 'admin', 1)")
conn.execute("INSERT OR IGNORE INTO clients (id, nom) VALUES (1, 'Client partagé')")
conn.execute("INSERT INTO appareils (client_id, nom_machine, adresse_ip, adresse_mac, "
             "type_appareil, date_maj) VALUES (1, 'POSTE-01', '192.168.1.50', "
             "'AA:BB:CC:00:00:01', 'PC', '2026-01-01T00:00:00')")
aid = conn.execute("SELECT id FROM appareils WHERE nom_machine='POSTE-01'").fetchone()[0]
conn.commit()
conn.close()

client = A.app.test_client()
rep = client.post('/api/device-info', json={
    'client_id': 1, 'mac_address': 'AA:BB:CC:00:00:01', 'hostname': 'POSTE-01',
    'ip_addresses': ['192.168.1.50'], 'os_name': 'Windows',
    'system_report': {'cpu': 'Intel', 'ram_gb': 16}})
verifier(rep.status_code == 200, 'collecte acceptée', str(rep.status_code))
conn = A.get_db()
row = conn.execute("SELECT date_maj, rapport_systeme_json FROM appareils WHERE id=?",
                   (aid,)).fetchone()
conn.close()
verifier(row and row[1], 'rapport enregistré')
verifier(row and row[0] > '2026-01-01T00:00:00',
         'date_maj a avancé (la collecte est vue comme plus récente)', str(row and row[0]))


print('\n=== 1bis. Rattrapage : les collectes antérieures au correctif sont renvoyées ===')
conn = A.get_db()
# État d'avant correctif : rapport présent, date_maj restée à celle du scan.
conn.execute("UPDATE appareils SET date_maj='2026-01-01T00:00:00' WHERE id=?", (aid,))
conn.execute("DELETE FROM _sync_journal")
conn.commit()
derniere = conn.execute("SELECT derniere_synchro FROM appareils WHERE id=?", (aid,)).fetchone()[0]
conn.execute("DELETE FROM config WHERE cle=?", (A._CLE_RATTRAPAGE_COLLECTES,))
conn.commit()
conn.close()
A.cfg_invalidate()
A._rattrapage_collectes_fait = False
n = A.rattraper_sync_collectes()
conn = A.get_db()
dm = conn.execute("SELECT date_maj FROM appareils WHERE id=?", (aid,)).fetchone()[0]
jrn = {(r[0], r[1]) for r in conn.execute("SELECT tbl, action FROM _sync_journal")}
conn.close()
verifier(n >= 1, 'des enregistrements sont journalisés', str(n))
verifier(dm == derniere, 'date_maj ramené à la date de la collecte', '%s / %s' % (dm, derniere))
verifier(('appareils', 'UPDATE') in jrn, 'la fiche collectée sera repoussée vers Turso')
A._rattrapage_collectes_fait = False
verifier(A.rattraper_sync_collectes() == 0, 'le rattrapage ne rejoue pas')


# ── Schéma minimal pour les scénarios de synchro ─────────────────────────────
_JOURNAL = """CREATE TABLE _sync_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl TEXT NOT NULL, record_id TEXT NOT NULL, action TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    UNIQUE(tbl, record_id, action) ON CONFLICT REPLACE)"""


def _db():
    c = sqlite3.connect(':memory:')
    c.execute("CREATE TABLE appareils (id INTEGER PRIMARY KEY, client_id INTEGER, "
              "nom_machine TEXT, en_ligne INTEGER, rapport_systeme_json TEXT, date_maj TEXT, "
              "derniere_synchro TEXT, os TEXT, localisation TEXT)")
    c.execute("CREATE TABLE collectes (cle TEXT PRIMARY KEY, appareil_id INTEGER, "
              "client_id INTEGER, horodatage TEXT, date_maj TEXT)")
    c.execute("CREATE TABLE cles_recuperation (cle TEXT PRIMARY KEY, appareil_id INTEGER, "
              "client_id INTEGER, volume TEXT, valeur TEXT, date_maj TEXT)")
    c.execute("CREATE TABLE client_partages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "client_id INTEGER, auth_user_id INTEGER, niveau TEXT, date_partage TEXT, "
              "UNIQUE(client_id, auth_user_id))")
    c.execute(_JOURNAL)
    c.commit()
    return c


def _jrn(c, tbl, rid, action='UPDATE', ts='2026-09-10T08:14:00'):
    c.execute("INSERT INTO _sync_journal (tbl, record_id, action, timestamp) VALUES (?,?,?,?)",
              (tbl, str(rid), action, ts))
    c.commit()


# ── 2. Instance en retard avec un ping en attente : la collecte survit ──────
print('\n=== 2. Un ping en attente sur l\'autre instance n\'efface plus la collecte ===')
inst_b, turso = _db(), _db()
# B connaît la fiche telle que créée par le scan (T0), et un ping est en attente.
inst_b.execute("INSERT INTO appareils (id, client_id, nom_machine, en_ligne, rapport_systeme_json, date_maj) "
               "VALUES (42, 1, 'POSTE-01', 1, NULL, '2026-09-10T08:00:00')")
inst_b.commit()
_jrn(inst_b, 'appareils', 42, ts='2026-09-10T08:30:00')
# La collecte (instance A) a poussé sur Turso, avec date_maj bumpé.
turso.execute("INSERT INTO appareils (id, client_id, nom_machine, en_ligne, rapport_systeme_json, date_maj, "
              "derniere_synchro) VALUES (42, 1, 'POSTE-01', 1, '{\"cpu\":\"Intel\"}', "
              "'2026-09-10T09:00:00', '2026-09-10T09:00:00')")
turso.commit()
_jrn(turso, 'appareils', 42, ts='2026-09-10T09:00:05')

stats, errors = D._sync_using_journal(inst_b, turso)
verifier(not errors, 'sync sans erreur', str(errors))
verifier(inst_b.execute("SELECT rapport_systeme_json FROM appareils WHERE id=42").fetchone()[0],
         'le rapport collecté arrive sur l\'instance B')
verifier(turso.execute("SELECT rapport_systeme_json FROM appareils WHERE id=42").fetchone()[0],
         'et n\'est pas effacé sur Turso par la ligne périmée de B')


def _rapport(c, rid):
    r = c.execute("SELECT rapport_systeme_json FROM appareils WHERE id=?", (rid,)).fetchone()
    return r[0] if r else None


def _loc(c, rid):
    r = c.execute("SELECT localisation FROM appareils WHERE id=?", (rid,)).fetchone()
    return r[0] if r else None


print('\n=== 2bis. Fiche retouchée plus tard ailleurs (date_maj plus récent) : la collecte survit ===')
# Cas du signalement : l'autre instance a modifié la fiche APRÈS la collecte
# (scan, édition, ping) → date_maj plus récent, mais aucun rapport.
inst_b, turso = _db(), _db()
inst_b.execute("INSERT INTO appareils (id, client_id, nom_machine, en_ligne, date_maj, localisation) "
               "VALUES (44, 1, 'POSTE-02', 1, '2026-09-10T12:00:00', 'Bureau B')")
inst_b.commit()
_jrn(inst_b, 'appareils', 44, ts='2026-09-10T12:00:00')
turso.execute("INSERT INTO appareils (id, client_id, nom_machine, en_ligne, rapport_systeme_json, date_maj, "
              "derniere_synchro, os) VALUES (44, 1, 'POSTE-02', 1, '{\"cpu\":\"AMD\"}', "
              "'2026-09-10T09:00:00', '2026-09-10T09:00:00', 'Windows')")
turso.commit()
_jrn(turso, 'appareils', 44, ts='2026-09-10T09:00:05')
stats, errors = D._sync_using_journal(inst_b, turso)
verifier(not errors, 'sync sans erreur', str(errors))
verifier(_rapport(inst_b, 44) == '{"cpu":"AMD"}', 'la collecte est reprise sur l\'instance qui a retouché la fiche')
verifier(_loc(inst_b, 44) == 'Bureau B', 'sa modification locale est conservée')
verifier(_rapport(turso, 44) == '{"cpu":"AMD"}', 'Turso ne perd pas le rapport')
verifier(_loc(turso, 44) == 'Bureau B', 'et reçoit la modification locale')
verifier(inst_b.execute("SELECT os FROM appareils WHERE id=44").fetchone()[0] == 'Windows',
         'les colonnes système (os…) arrivent aussi')

print('\n=== 2ter. Ligne distante périmée plus récente : la collecte locale est remise ===')
inst_a, turso = _db(), _db()
inst_a.execute("INSERT INTO appareils (id, client_id, nom_machine, en_ligne, rapport_systeme_json, date_maj, "
               "derniere_synchro) VALUES (45, 1, 'POSTE-03', 1, '{\"cpu\":\"Intel\"}', "
               "'2026-09-10T09:00:00', '2026-09-10T09:00:00')")
inst_a.commit()
turso.execute("INSERT INTO appareils (id, client_id, nom_machine, en_ligne, date_maj, localisation) "
              "VALUES (45, 1, 'POSTE-03', 1, '2026-09-10T12:00:00', 'Salle 2')")
turso.commit()
_jrn(turso, 'appareils', 45, ts='2026-09-10T12:00:05')
stats, errors = D._sync_using_journal(inst_a, turso)
verifier(not errors, 'sync sans erreur', str(errors))
verifier(_loc(inst_a, 45) == 'Salle 2', 'la modification distante plus récente est appliquée')
verifier(_rapport(inst_a, 45) == '{"cpu":"Intel"}', 'sans effacer la collecte locale')
verifier(_rapport(turso, 45) == '{"cpu":"Intel"}', 'et la collecte est renvoyée sur Turso')

print('\n=== 2quater. Une collecte plus récente d\'une autre instance n\'est pas écrasée par une plus ancienne ===')
inst_b, turso = _db(), _db()
inst_b.execute("INSERT INTO appareils (id, client_id, nom_machine, rapport_systeme_json, date_maj, "
               "derniere_synchro) VALUES (46, 1, 'POSTE-04', '{\"v\":1}', '2026-09-10T08:00:00', "
               "'2026-09-10T08:00:00')")
inst_b.commit()
_jrn(inst_b, 'appareils', 46, ts='2026-09-10T08:30:00')
turso.execute("INSERT INTO appareils (id, client_id, nom_machine, rapport_systeme_json, date_maj, "
              "derniere_synchro) VALUES (46, 1, 'POSTE-04', '{\"v\":2}', '2026-09-10T08:00:00', "
              "'2026-09-10T11:00:00')")
turso.commit()
D._sync_using_journal(inst_b, turso)
verifier(_rapport(turso, 46) == '{"v":2}', 'la collecte la plus récente reste sur Turso', str(_rapport(turso, 46)))
verifier(_rapport(inst_b, 46) == '{"v":2}', 'et est reprise localement', str(_rapport(inst_b, 46)))


# ── 3. Partage + tables de collecte à clé texte ─────────────────────────────
print('\n=== 3. Partage et tables de collecte se répliquent vers l\'autre instance ===')
inst_a, inst_b, turso = _db(), _db(), _db()
inst_a.execute("INSERT INTO client_partages (client_id, auth_user_id, niveau, date_partage) "
               "VALUES (1, 2, 'lecture', '2026-09-10')")
inst_a.execute("INSERT INTO collectes VALUES ('42|2026-09-10T09:00:00.000001', 42, 1, "
               "'2026-09-10T09:00:00', '2026-09-10T09:00:00')")
inst_a.execute("INSERT INTO cles_recuperation VALUES ('42|C:|{id}', 42, 1, 'C:', 'chiffre', "
               "'2026-09-10T09:00:00')")
inst_a.commit()
part_id = inst_a.execute("SELECT id FROM client_partages").fetchone()[0]
_jrn(inst_a, 'client_partages', part_id, 'INSERT')
_jrn(inst_a, 'collectes', '42|2026-09-10T09:00:00.000001', 'INSERT')
_jrn(inst_a, 'cles_recuperation', '42|C:|{id}', 'INSERT')

_, errors = D._sync_using_journal(inst_a, turso)
verifier(not errors, 'push A → Turso sans erreur', str(errors))
# En production, les triggers répliqués sur Turso journalisent chaque écriture.
_jrn(turso, 'client_partages', part_id, 'INSERT')
_jrn(turso, 'collectes', '42|2026-09-10T09:00:00.000001', 'INSERT')
_jrn(turso, 'cles_recuperation', '42|C:|{id}', 'INSERT')
_, errors = D._sync_using_journal(inst_b, turso)
verifier(not errors, 'pull Turso → B sans erreur', str(errors))
verifier(inst_b.execute("SELECT niveau FROM client_partages WHERE auth_user_id=2").fetchone(),
         'le partage arrive sur B')
verifier(inst_b.execute("SELECT COUNT(*) FROM collectes").fetchone()[0] == 1,
         'la collecte historisée arrive sur B')
verifier(inst_b.execute("SELECT COUNT(*) FROM cles_recuperation").fetchone()[0] == 1,
         'la clé de récupération arrive sur B')

# Changement de niveau fait sur une instance qui n'a pas encore relu le journal
# Turso (cas d'une instance en retard) : UPDATE de la même ligne, comme la route.
inst_a.execute("UPDATE client_partages SET niveau='ecriture', date_partage='2026-09-11' "
               "WHERE auth_user_id=2")
inst_a.commit()
_jrn(inst_a, 'client_partages', part_id, 'UPDATE', ts='2026-09-11T09:00:00')
D._sync_using_journal(inst_a, turso)
_jrn(turso, 'client_partages', part_id, 'UPDATE', ts='2026-09-11T09:00:05')
D._sync_using_journal(inst_b, turso)
niveaux = [r[0] for r in inst_b.execute("SELECT niveau FROM client_partages WHERE auth_user_id=2")]
verifier(niveaux == ['ecriture'], 'le changement de droits est répliqué sans doublon', str(niveaux))


print('\n=== 3bis. La route de partage garde le même id quand le niveau change ===')
conn = A.get_db()
conn.execute("INSERT OR REPLACE INTO auth_users (id, login, password_hash, nom, role, actif) "
             "VALUES (2, 'davy', 'x', 'Davy', 'user', 1)")
conn.commit()
conn.close()
with client.session_transaction() as s:
    s['auth_user_id'] = 1
    s['client_id'] = 1
    s['csrf_token'] = 'tok'
for niv in ('lecture', 'ecriture', 'nimportequoi'):
    client.post('/client/1/partager', data={'action': 'ajouter', 'user_id': '2',
                                            'niveau': niv, 'csrf_token': 'tok'})
    conn = A.get_db()
    lignes = conn.execute("SELECT id, niveau FROM client_partages "
                          "WHERE client_id=1 AND auth_user_id=2").fetchall()
    conn.close()
    if niv == 'lecture':
        id_initial = lignes[0][0] if lignes else None
    verifier(len(lignes) == 1 and lignes[0][0] == id_initial,
             "niveau '%s' : une seule ligne, id inchangé" % niv, str([tuple(l) for l in lignes]))
verifier(lignes and lignes[0][1] == 'lecture', 'un niveau invalide retombe sur « lecture »',
         str(lignes and lignes[0][1]))


# ── 4. Transport Turso : refus HTTP ≠ succès ────────────────────────────────
print('\n=== 4. TursoConnection._pipeline lève sur un refus HTTP ===')


class _Rep:
    def __init__(self, status, corps):
        self.status, self._c = status, corps

    def read(self):
        return self._c


class _Conn:
    def __init__(self, rep):
        self._rep = rep

    def request(self, *a, **k):
        pass

    def getresponse(self):
        return self._rep

    def close(self):
        pass


def _pipeline_avec(status, corps, nb_stmt=2):
    t = D.TursoConnection('https://x.example', 'tok')
    t._conn = _Conn(_Rep(status, corps))
    try:
        t._pipeline([{'type': 'execute', 'stmt': {'sql': 'SELECT 1', 'args': []}}] * nb_stmt)
        return None
    except Exception as e:
        return str(e)


err = _pipeline_avec(413, b'{"error": "payload too large"}')
verifier(err and '413' in err, 'HTTP 413 avec corps JSON sans « results » → exception', str(err))
err = _pipeline_avec(200, b'{"error": {"message": "boom"}}')
verifier(err and 'boom' in err, 'HTTP 200 sans « results » → exception', str(err))
err = _pipeline_avec(502, b'<html>bad gateway</html>')
verifier(err and '502' in err, 'corps non JSON → exception', str(err))
ok = _pipeline_avec(200, b'{"results": [{"type":"ok"},{"type":"ok"},{"type":"ok"}]}')
verifier(ok is None, 'réponse normale acceptée', str(ok))

print('\n=== 5. pipeline_exec découpe par taille ===')
gros = 'x' * 400_000
stmts = [('INSERT INTO t VALUES (?)', [gros]) for _ in range(5)] + \
        [('INSERT INTO t VALUES (?)', ['a']) for _ in range(3)]
lots = list(D._lots_par_taille(stmts))
verifier(len(lots) >= 3, 'les gros rapports sont répartis sur plusieurs requêtes', str(len(lots)))
verifier(sum(len(l) for l in lots) == len(stmts), 'aucune instruction perdue')
verifier(all(sum(D._taille_stmt(*s) for s in l) <= D._MAX_PIPELINE_BYTES or len(l) == 1 for l in lots),
         'chaque lot respecte la limite (sauf instruction unique plus grosse)')
seul = list(D._lots_par_taille([('INSERT', ['y' * 2_000_000])]))
verifier(len(seul) == 1 and len(seul[0]) == 1, 'une instruction géante part seule, sans être perdue')

print()
if echecs:
    print('ÉCHECS : %d' % len(echecs))
    for e in echecs:
        print('  - ' + e)
    sys.exit(1)
print('TOUT OK')
