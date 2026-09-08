#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Score de santé synthétique par appareil (`sante.py`).

`sante_appareil(appareil, ctx)` agrège, sans aucune requête réseau :
  - les points d'attention du collecteur (`collector_core.build_alerts`)
  - le cycle de vie (garantie/contrat, âge, fraîcheur de collecte, injoignable)
  - l'état réseau (`diag_etat_equipement` / `diag_etat_port`)
→ `{niveau: ok|attention|critique, score, raisons[]}`.

`charger_contexte_sante(conn, client_id)` pré-charge le contexte en quelques
requêtes groupées (pas de N+1 sur une liste de centaines d'appareils).

Usage : python test_sante_appareil.py
"""
import io
import json
import os
import sys
import tempfile
from datetime import date, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='sante_')
os.environ['RUNNING_IN_DOCKER'] = '1'
os.environ['PARCINFO_BACKUP'] = '0'

import app as A            # noqa: E402
import sante               # noqa: E402

A.init_db()
echecs = []


def verifier(cond, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if cond else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not cond:
        echecs.append(libelle)


def il_y_a(j):
    return (date.today() - timedelta(days=j)).isoformat()


def ctx(**kw):
    base = {'reglages': {**sante._DEFAUTS, 'desactivees': set()},
            'appareils_sous_contrat': set(), 'equip_muet_ip': set(),
            'equip_par_appareil': {}, 'ports_erreur_par_appareil': {},
            'series_doublon': set()}
    base.update(kw)
    return base


print('=== 1. règles isolées (fonction pure) ===')
sain = sante.sante_appareil(
    {'id': 1, 'type_appareil': 'PC fixe', 'statut': 'actif', 'date_maj': il_y_a(2),
     'date_creation': il_y_a(2), 'rapport_systeme_json': '{}'}, ctx())
verifier(sain['niveau'] == 'ok' and not sain['raisons'], 'appareil neuf et collecté -> ok')

g = sante.sante_appareil(
    {'id': 1, 'type_appareil': 'PC', 'statut': 'actif',
     'date_fin_garantie': il_y_a(30), 'date_maj': il_y_a(1)}, ctx())
verifier('garantie_expiree_sans_contrat' in {r['code'] for r in g['raisons']},
         'garantie expirée sans contrat -> raison attention')
g2 = sante.sante_appareil(
    {'id': 1, 'type_appareil': 'PC', 'statut': 'actif',
     'date_fin_garantie': il_y_a(30), 'date_maj': il_y_a(1)}, ctx(appareils_sous_contrat={1}))
verifier('garantie_expiree_sans_contrat' not in {r['code'] for r in g2['raisons']},
         'même appareil sous contrat -> plus de raison garantie')

vx = {'id': 1, 'statut': 'actif', 'date_achat': il_y_a(int(7 * 365.25)), 'date_maj': il_y_a(1)}
verifier('materiel_ancien' in {r['code'] for r in
         sante.sante_appareil({**vx, 'type_appareil': 'PC'}, ctx())['raisons']},
         'PC de 7 ans (seuil 6) -> matériel ancien')
verifier('materiel_ancien' not in {r['code'] for r in
         sante.sante_appareil({**vx, 'type_appareil': 'Switch'}, ctx())['raisons']},
         'Switch de 7 ans (seuil 10) -> pas signalé')

jc = sante.sante_appareil(
    {'id': 1, 'type_appareil': 'PC portable', 'statut': 'actif',
     'date_maj': il_y_a(1), 'date_creation': il_y_a(90)}, ctx())
verifier(any(r['code'] == 'jamais_collecte' and r['gravite'] == 'info' for r in jc['raisons'])
         and jc['niveau'] == 'ok',
         'PC portable ancien sans rapport -> nudge info (pastille reste ok)')

col = sante.sante_appareil(
    {'id': 1, 'type_appareil': 'PC', 'statut': 'actif', 'rapport_systeme_json': '{}',
     'derniere_synchro': il_y_a(60)}, ctx())
verifier(any(r['code'] == 'collecte_ancienne' and r['gravite'] == 'info' for r in col['raisons'])
         and col['niveau'] == 'ok', 'collecte de 60 j -> info, pastille reste ok')

print('\n=== 2. réutilisation de collector_core.build_alerts ===')
disq = sante.sante_appareil(
    {'id': 1, 'type_appareil': 'PC', 'statut': 'actif', 'date_maj': il_y_a(1),
     'rapport_systeme_json': json.dumps({'disk_total_gb': 500, 'disk_used_gb': 485,
                                         'antivirus': 'Defender'})}, ctx())
verifier(disq['niveau'] == 'critique'
         and any(r['code'].startswith('collecteur:') for r in disq['raisons']),
         'disque à 97 % -> alerte collecteur critique')

noav = sante.sante_appareil(
    {'id': 1, 'type_appareil': 'PC', 'statut': 'actif', 'date_maj': il_y_a(1),
     'rapport_systeme_json': json.dumps({'antivirus': ''})}, ctx())
verifier(any('antivirus' in r['texte'].lower() for r in noav['raisons']),
         'aucun antivirus dans le rapport -> alerte collecteur')

print('\n=== 3. signaux réseau (diag_etat_*) ===')
mut = sante.sante_appareil(
    {'id': 7, 'type_appareil': 'Switch', 'statut': 'actif', 'adresse_ip': '10.0.0.9',
     'date_maj': il_y_a(1)},
    ctx(equip_par_appareil={7: {'snmp_ok': False, 'nb_ports_erreur': 0, 'motif': 'timeout'}}))
verifier('reseau:equipement_muet' in {r['code'] for r in mut['raisons']},
         'équipement SNMP muet -> raison réseau')
prt = sante.sante_appareil(
    {'id': 3, 'type_appareil': 'Serveur', 'statut': 'actif', 'date_maj': il_y_a(1)},
    ctx(ports_erreur_par_appareil={3: [{'classe': 'physique', 'libelle': 'CRC',
                                        'gravite': 'critique'}]}))
verifier(prt['niveau'] == 'critique', 'port de switch en erreur critique -> critique')

print('\n=== 4. cumul, désactivation, statut ===')
cum = sante.sante_appareil(
    {'id': 1, 'type_appareil': 'PC', 'statut': 'actif', 'date_maj': il_y_a(1),
     'date_fin_garantie': il_y_a(10),
     'rapport_systeme_json': json.dumps({'disk_total_gb': 500, 'disk_used_gb': 495,
                                         'firewall': ['Profil: désactivé']})}, ctx())
verifier(cum['niveau'] == 'critique' and cum['score'] == 100, 'cumul -> critique, score plafonné 100')

c_off = ctx()
c_off['reglages']['desactivees'] = {'materiel_ancien'}
verifier('materiel_ancien' not in {r['code'] for r in sante.sante_appareil(
         {'id': 1, 'type_appareil': 'PC', 'statut': 'actif',
          'date_achat': il_y_a(int(9 * 365.25)), 'date_maj': il_y_a(1)}, c_off)['raisons']},
         'règle désactivée -> pas de raison')

ret = sante.sante_appareil(
    {'id': 1, 'type_appareil': 'Switch', 'statut': 'retire',
     'date_fin_garantie': il_y_a(400), 'adresse_ip': '10.0.0.9'},
    ctx(equip_muet_ip={'10.0.0.9'}))
verifier(ret['niveau'] == 'ok' and not ret['raisons'], 'appareil retiré -> aucun signal')

print('\n=== 5. charger_contexte_sante (base réelle) ===')
conn = A.get_db()
cur = conn.execute("INSERT INTO clients (nom, date_creation) VALUES ('Santé', '2026-01-01')")
CID = cur.lastrowid
a1 = conn.execute("INSERT INTO appareils (client_id, nom_machine, numero_serie, type_appareil) "
                  "VALUES (?, 'A1', 'DUP', 'PC')", (CID,)).lastrowid
conn.execute("INSERT INTO appareils (client_id, nom_machine, numero_serie, type_appareil) "
             "VALUES (?, 'A2', 'DUP', 'PC')", (CID,))
sw = conn.execute("INSERT INTO appareils (client_id, nom_machine, type_appareil, adresse_ip) "
                  "VALUES (?, 'SW', 'Switch', '10.0.0.9')", (CID,)).lastrowid
ctid = conn.execute("INSERT INTO contrats (client_id, titre, date_fin, statut) "
                    "VALUES (?, 'Maint', ?, 'actif')", (CID, il_y_a(-400))).lastrowid
conn.execute("INSERT INTO contrats_appareils (contrat_id, appareil_id) VALUES (?,?)", (ctid, a1))
conn.execute("INSERT INTO diag_etat_equipement (client_id, equipement_ip, appareil_id, snmp_ok) "
             "VALUES (?, '10.0.0.9', ?, 0)", (CID, sw))
conn.execute("INSERT INTO diag_etat_port (client_id, equipement_ip, port_index, appareil_vu_id, "
             "classe_erreur, classe_libelle, gravite) "
             "VALUES (?, '10.0.0.9', 5, ?, 'duplex', 'Duplex mismatch', 'attention')", (CID, a1))
conn.commit()
c = sante.charger_contexte_sante(conn, CID)
conn.close()
verifier(a1 in c['appareils_sous_contrat'], 'appareil couvert par un contrat -> dans le contexte')
verifier('10.0.0.9' in c['equip_muet_ip'], 'switch SNMP muet -> equip_muet_ip')
verifier(c['equip_par_appareil'][sw]['snmp_ok'] is False, 'equip_par_appareil rattaché par id')
verifier(c['ports_erreur_par_appareil'][a1][0]['classe'] == 'duplex', 'port en erreur rattaché à l\'appareil vu')
verifier('DUP' in c['series_doublon'], 'numéro de série en double -> series_doublon')

print('\n=== 6. recalculer : cache mis à jour uniquement sur changement ===')
conn = A.get_db()
cur = conn.execute("INSERT INTO clients (nom, date_creation) VALUES ('Santé2', '2026-01-01')")
CID2 = cur.lastrowid
aid = conn.execute("INSERT INTO appareils (client_id, nom_machine, type_appareil, "
                   "date_fin_garantie, date_creation) VALUES (?, 'KO', 'PC', ?, ?)",
                   (CID2, il_y_a(30), il_y_a(2))).lastrowid
conn.commit()
n1 = sante.recalculer(conn, CID2)
verifier(n1 >= 1, 'premier calcul -> au moins une pastille écrite')
row = conn.execute("SELECT sante_niveau, sante_raisons FROM appareils WHERE id=?", (aid,)).fetchone()
verifier(row[0] == 'attention' and 'garantie' in (row[1] or ''),
         'garantie expirée -> sante_niveau=attention, raison stockée')
verifier(sante.recalculer(conn, CID2) == 0, 'rien changé -> 0 réécriture')
conn.execute("UPDATE appareils SET date_fin_garantie=? WHERE id=?", (il_y_a(-400), aid))
conn.commit()
verifier(sante.recalculer(conn, CID2) == 1, 'garantie de nouveau valide -> 1 réécriture')
verifier(conn.execute("SELECT sante_niveau FROM appareils WHERE id=?", (aid,)).fetchone()[0] == 'ok',
         '  -> pastille repasse à ok')

print('\n=== 7. bascules du seuil journalisées + resume_cache (lots 3-4) ===')
bid = conn.execute("INSERT INTO appareils (client_id, nom_machine, type_appareil, date_creation) "
                   "VALUES (?, 'BASCULE', 'PC', ?)", (CID2, il_y_a(2))).lastrowid
conn.commit()
_nb = lambda act: conn.execute(
    "SELECT COUNT(*) FROM historique WHERE client_id=? AND entite_id=? AND action=?",
    (CID2, bid, act)).fetchone()[0]
sante.recalculer(conn, CID2)          # 1er calcul : ok, aucune bascule journalisée
verifier(_nb('Santé dégradée') == 0, '1er calcul -> aucune bascule journalisée')
conn.execute("UPDATE appareils SET rapport_systeme_json=? WHERE id=?",
             ('{"disk_total_gb":500,"disk_used_gb":496}', bid))
conn.commit()
sante.recalculer(conn, CID2)
verifier(_nb('Santé dégradée') == 1, 'ok -> critique -> ligne « Santé dégradée » dans historique')
conn.execute("UPDATE appareils SET rapport_systeme_json='{}' WHERE id=?", (bid,))
conn.commit()
sante.recalculer(conn, CID2)
verifier(_nb('Santé rétablie') == 1, 'critique -> ok -> ligne « Santé rétablie »')

conn.execute("INSERT INTO appareils (client_id, nom_machine, type_appareil, statut, sante_niveau, "
             "sante_score, sante_raisons) VALUES (?, 'C1', 'PC', 'actif', 'critique', 55, ?)",
             (CID2, '[{"texte":"Disque saturé","gravite":"critique"}]'))
conn.execute("INSERT INTO appareils (client_id, nom_machine, type_appareil, statut, sante_niveau) "
             "VALUES (?, 'A1', 'PC', 'actif', 'attention')", (CID2,))
conn.commit()
rc = sante.resume_cache(conn, CID2)
verifier(rc['compte']['critique'] >= 1 and rc['compte']['attention'] >= 1,
         'resume_cache compte les niveaux depuis le cache')
verifier(rc['a_traiter'] and rc['a_traiter'][0]['niveau'] == 'critique',
         '  -> a_traiter : critique en tête')
conn.close()

print()
if echecs:
    print('%d ÉCHEC(S) : %s' % (len(echecs), ', '.join(echecs)))
    sys.exit(1)
print('Score de santé : tous les contrôles passent.')
