#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie le mécanisme de mise à jour.

Un serveur HTTP local joue le rôle de GitHub : il sert un version.json, un
SHA256SUMS.txt et un faux binaire. On contrôle ce qui compte vraiment :

  - une version plus récente est détectée, une plus ancienne ne l'est pas
  - un binaire dont l'empreinte ne correspond pas est refusé et effacé
  - sans empreinte publiée, le téléchargement est refusé avant de commencer
  - un téléchargement interrompu ne laisse pas de fichier d'apparence valide
  - écarter une version la masque, la version suivante réapparaît

Usage :
    python test_maj.py
"""

import hashlib
import http.server
import io
import json
import os
import socket
import sys
import tempfile
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Le suivi des mises à jour journalise dans la base. Sans chemin explicite,
# database.py se rabat sur le dossier des sources — souvent en lecture seule,
# ce qui noyait la sortie du test sous des traces sans rapport.
import database as _db  # noqa: E402

_dossier_donnees = tempfile.mkdtemp(prefix='maj_data_')
_db.init_paths(os.path.join(_dossier_donnees, 'parc_info.db'),
               os.path.join(_dossier_donnees, 'uploads'))

import update_checker as UC  # noqa: E402

echecs = []


def verifier(condition, libelle, detail=''):
    print('  %s %s%s' % ('OK   ' if condition else 'ÉCHEC', libelle,
                         (' — ' + detail) if detail else ''))
    if not condition:
        echecs.append(libelle)


# ─── Faux dépôt servi en local ───────────────────────────────────────────────

BINAIRE = b'ParcInfo binaire factice ' * 4096          # ~100 Ko
EMPREINTE = hashlib.sha256(BINAIRE).hexdigest()
RACINE = tempfile.mkdtemp(prefix='maj_srv_')

port = socket.socket()
port.bind(('127.0.0.1', 0))
PORT = port.getsockname()[1]
port.close()
BASE = 'http://127.0.0.1:%d' % PORT

VERSION_JSON = {
    'version': '9.9.9',
    'downloads': {
        'windows_installer': BASE + '/ParcInfo-Windows.exe',
        'macos_app': BASE + '/ParcInfo-macOS-ARM.zip',
    },
    'checksums': {'windows_installer_sha256': 'PENDING_BUILD'},
    'notes': {'improvements': ['Une amélioration accentuée : coût réduit']},
    'release_notes_url': BASE + '/notes',
}

with open(os.path.join(RACINE, 'ParcInfo-Windows.exe'), 'wb') as f:
    f.write(BINAIRE)
with open(os.path.join(RACINE, 'version.json'), 'w', encoding='utf-8') as f:
    json.dump(VERSION_JSON, f, ensure_ascii=False)
with open(os.path.join(RACINE, 'SHA256SUMS.txt'), 'w', encoding='utf-8') as f:
    f.write('%s  ParcInfo-Windows.exe\n' % EMPREINTE)


class Serveur(http.server.SimpleHTTPRequestHandler):
    """Sert les fichiers, en honorant l'en-tête Range.

    SimpleHTTPRequestHandler l'ignore et renvoie toujours le fichier entier :
    sans ce complément, un test de reprise éprouverait le repli « le serveur ne
    sait pas reprendre » au lieu de la reprise elle-même.
    """

    #: Codes de réponse observés, pour vérifier qu'une reprise a bien eu lieu.
    reponses = []

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=RACINE, **kw)

    def do_GET(self):
        plage = self.headers.get('Range')
        chemin = self.translate_path(self.path)
        if not plage or not os.path.isfile(chemin):
            Serveur.reponses.append(200)
            return super().do_GET()

        debut = int(plage.split('=', 1)[1].split('-', 1)[0])
        with open(chemin, 'rb') as f:
            f.seek(debut)
            contenu = f.read()
        taille = os.path.getsize(chemin)
        Serveur.reponses.append(206)
        self.send_response(206)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(len(contenu)))
        self.send_header('Content-Range', 'bytes %d-%d/%d' % (debut, taille - 1, taille))
        self.end_headers()
        self.wfile.write(contenu)

    def log_message(self, *a):
        pass


httpd = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Serveur)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

# Le fichier d'empreintes est cherché à une URL construite à partir du dépôt :
# on la détourne vers le serveur local.
UC.SHA256SUMS_URL = BASE + '/SHA256SUMS.txt'


def neuf(version_locale='2.0.0'):
    c = UC.UpdateChecker(config_dir=tempfile.mkdtemp(prefix='maj_cfg_'),
                         version_json_url=BASE + '/version.json')
    c.current_version = version_locale
    return c


print('=== 1. Détection de version ===')
c = neuf('2.0.0')
verifier(c.check_for_updates(force=True) is True, 'version plus récente détectée',
         str(c.latest_version))
verifier(c.release_notes and 'accentuée' in c.release_notes[0],
         'notes de version lues sans perte d\'accents')
c2 = neuf('9.9.9')
verifier(c2.check_for_updates(force=True) is False, 'version identique : rien à annoncer')
c3 = neuf('10.0.0')
verifier(c3.check_for_updates(force=True) is False, 'version locale plus récente : rien à annoncer')
verifier(c3._is_newer_version('2.10.0', '2.9.9') is True, 'comparaison 2.10.0 > 2.9.9')
verifier(c3._is_newer_version('v2.6.43', '2.6.42') is True, 'préfixe v toléré')

print('\n=== 2. Le binaire est vérifié avant d\'être proposé ===')
c = neuf('2.0.0')
c.check_for_updates(force=True)
# On force la plateforme : le test doit valoir quel que soit l'OS qui l'exécute.
c._get_platform_key = lambda: 'windows_installer'
fichier = c.download_update()
verifier(fichier is not None and fichier.exists(), 'téléchargement abouti')
verifier(c._calculate_checksum(fichier) == EMPREINTE, 'empreinte conforme')

print('\n=== 2b. Un téléchargement interrompu reprend où il s\'était arrêté ===')
# Le fichier partiel était effacé à chaque échec : sur un réseau capricieux, la
# mise à jour repartait de zéro et n'aboutissait jamais.
c = neuf('2.0.0')
c.check_for_updates(force=True)
c._get_platform_key = lambda: 'windows_installer'
# Le téléchargement vit dans un sous-dossier dédié, jamais à côté de
# l'exécutable : le fichier partiel s'y trouve donc aussi.
(c.config_dir / 'maj').mkdir(parents=True, exist_ok=True)
partiel = c.config_dir / 'maj' / 'ParcInfo-Windows.exe.part'
partiel.write_bytes(BINAIRE[:40000])          # 40 Ko déjà acquis

Serveur.reponses.clear()
fichier = c.download_update()

verifier(206 in Serveur.reponses, 'le serveur a servi une reprise (206)',
         str(Serveur.reponses))
verifier(fichier is not None and c._calculate_checksum(fichier) == EMPREINTE,
         'le fichier recollé est identique à celui publié')

# Un serveur qui ignore Range doit rester géré : on repart alors de zéro
# plutôt que de coller la suite sur un début déjà présent.
partiel.write_bytes(BINAIRE[:12345])
_do_get = Serveur.do_GET
Serveur.do_GET = lambda self: http.server.SimpleHTTPRequestHandler.do_GET(self)
try:
    fichier = c.download_update()
finally:
    Serveur.do_GET = _do_get
verifier(fichier is not None and c._calculate_checksum(fichier) == EMPREINTE,
         'serveur sans reprise : le fichier reste correct')

print("\n=== 2c. Le téléchargement ne vise jamais l'exécutable en cours ===")
# Cas réel signalé : l'exécutable téléchargé depuis la page des versions
# s'appelle ParcInfo-Windows.exe, et le téléchargement visait ce même nom dans
# le dossier de l'application. Le programme tentait donc d'écraser l'exécutable
# en cours, que Windows verrouille — « WinError 5 : accès refusé », au moment
# précis où le téléchargement venait d'aboutir.
c = neuf('2.0.0')
c.check_for_updates(force=True)
c._get_platform_key = lambda: 'windows_installer'

exe_simule = c.config_dir / 'ParcInfo-Windows.exe'
exe_simule.write_bytes(b'executable en cours')
_executable_reel = UC.sys.executable
UC.sys.executable = str(exe_simule)
try:
    fichier = c.download_update()
    verifier(fichier is not None and os.path.abspath(str(fichier)) != os.path.abspath(str(exe_simule)),
             "le fichier téléchargé n'est pas l'exécutable en cours", str(fichier))
    verifier(fichier is not None and fichier.parent.name == 'maj',
             'le téléchargement va dans un sous-dossier dédié',
             str(fichier.parent.name) if fichier else '')
    verifier(exe_simule.read_bytes() == b'executable en cours',
             "l'exécutable en cours n'a pas été touché")
    verifier(c._calculate_checksum(fichier) == EMPREINTE, 'binaire téléchargé conforme')
finally:
    UC.sys.executable = _executable_reel

# Les reliquats laissés par les versions antérieures sont nettoyés.
reliquat = c.config_dir / 'ParcInfo-Windows.exe.part'
reliquat.write_bytes(b'x' * 1024)
c.download_update()
verifier(not reliquat.exists(), 'reliquat des versions précédentes supprimé')

print('\n=== 3. Un binaire altéré est rejeté ===')
with open(os.path.join(RACINE, 'SHA256SUMS.txt'), 'w', encoding='utf-8') as f:
    f.write('%s  ParcInfo-Windows.exe\n' % ('0' * 64))
c = neuf('2.0.0')
c.check_for_updates(force=True)
c._get_platform_key = lambda: 'windows_installer'
try:
    c.download_update()
    verifier(False, 'empreinte fausse refusée', 'aucune exception levée')
except UC.UpdateCheckError as e:
    verifier('mpreinte' in str(e), 'empreinte fausse refusée', str(e)[:60])
    verifier(not (c.config_dir / 'ParcInfo-Windows.exe').exists(),
             'le fichier rejeté est effacé')

print('\n=== 4. Sans empreinte publiée, rien n\'est téléchargé ===')
os.remove(os.path.join(RACINE, 'SHA256SUMS.txt'))
c = neuf('2.0.0')
c.check_for_updates(force=True)
c._get_platform_key = lambda: 'windows_installer'
try:
    c.download_update()
    verifier(False, 'absence d\'empreinte bloquante', 'aucune exception levée')
except UC.UpdateCheckError as e:
    verifier('mpreinte' in str(e), 'absence d\'empreinte bloquante', str(e)[:60])
    verifier(not (c.config_dir / 'ParcInfo-Windows.exe').exists(),
             'aucun fichier laissé derrière')

print('\n=== 5. Modes d\'exécution ===')
os.environ['RUNNING_IN_DOCKER'] = '1'
verifier(UC.runtime_mode() == 'docker', 'conteneur reconnu', UC.runtime_mode())
verifier(UC.can_self_install() is False, 'un conteneur ne s\'installe pas lui-même')
cmds = UC.docker_pull_commands('2.6.43')
verifier('compose pull' in cmds['compose'] and '2.6.43' in cmds['run'],
         'commandes Docker proposées')
os.environ.pop('RUNNING_IN_DOCKER')
# Sans la variable, la détection retombe sur /.dockerenv : dans un vrai
# conteneur, « docker » reste la bonne réponse. Ce test tourne aussi bien sur
# un poste que dans l'image de la CI.
attendu = 'docker' if os.path.exists('/.dockerenv') else 'source'
verifier(UC.runtime_mode() == attendu,
         'mode déduit sans la variable d\'environnement : %s' % attendu,
         UC.runtime_mode())
verifier(UC.can_self_install() is False, 'pas d\'auto-installation hors exécutable')

print('\n=== 6. État exposé à l\'interface ===')
import update_notifier as UN  # noqa: E402

cfg = tempfile.mkdtemp(prefix='maj_not_')
n = UN.UpdateNotifier(config_dir=cfg)
n.checker.version_json_url = BASE + '/version.json'
n.checker.current_version = '2.0.0'
n.verifier(force=True)
e = n.etat
verifier(e['mise_a_jour_disponible'] is True, 'mise à jour signalée')
verifier(e['version_disponible'] == '9.9.9', 'version annoncée', str(e['version_disponible']))
verifier(e['masquee'] is False, 'bannière visible par défaut')
verifier(e['installable'] is False, 'installation refusée hors exécutable')

n.ecarter()
verifier(n.etat['masquee'] is True, 'version écartée : bannière masquée')

# Une nouvelle instance relit le choix sur disque : redémarrer l'application ne
# doit pas faire réapparaître une annonce déjà écartée.
n2 = UN.UpdateNotifier(config_dir=cfg)
n2.checker.version_json_url = BASE + '/version.json'
n2.checker.current_version = '2.0.0'
n2.verifier(force=True)
verifier(n2.etat['masquee'] is True, 'choix conservé après redémarrage')

# ... mais la version suivante doit réapparaître.
VERSION_JSON['version'] = '9.9.10'
with open(os.path.join(RACINE, 'version.json'), 'w', encoding='utf-8') as f:
    json.dump(VERSION_JSON, f, ensure_ascii=False)
n2.verifier(force=True)
verifier(n2.etat['masquee'] is False, 'la version suivante réapparaît',
         str(n2.etat['version_disponible']))

# Le clic sur le numéro de version redemande à voir une annonce écartée.
# Dossier de configuration neuf : réutiliser le précédent ferait croire à une
# mise à jour venant d'être installée (la version enregistrée y est plus
# ancienne), et « écarter » refermerait cette confirmation au lieu de l'annonce.
cfg_ecart = tempfile.mkdtemp(prefix='maj_ecart_')
n4 = UN.UpdateNotifier(config_dir=cfg_ecart)
n4.checker.version_json_url = BASE + '/version.json'
n4.checker.current_version = '2.0.0'
n4.verifier(force=True)
verifier(n4.etat['masquee'] is False, 'annonce visible au départ')
n4.ecarter()
verifier(n4.etat['masquee'] is True, 'écartée')
n4.reafficher()
verifier(n4.etat['masquee'] is False, 'réaffichée sur demande')

n5 = UN.UpdateNotifier(config_dir=cfg_ecart)
n5.checker.version_json_url = BASE + '/version.json'
n5.checker.current_version = '2.0.0'
n5.verifier(force=True)
verifier(n5.etat['masquee'] is False, 'le réaffichage survit au redémarrage')

print('\n=== 7. Installation impossible hors exécutable ===')
verifier(n2.installer() is False, 'installation refusée proprement')
verifier(bool(n2.erreur), 'raison expliquée', str(n2.erreur)[:70])

print('\n=== 8. Confirmation au premier démarrage après mise à jour ===')
# La version installée est lue depuis __version__ : on la simule le temps du test.
version_reelle = UC.__version__
cfg_inst = tempfile.mkdtemp(prefix='maj_inst_')
with open(os.path.join(cfg_inst, 'update_state.json'), 'w', encoding='utf-8') as f:
    json.dump({'version_vue': '2.6.41'}, f)

UC.__version__ = '2.6.43'
try:
    apres = UN.UpdateNotifier(config_dir=cfg_inst)
    verifier(apres.etat['version_installee'] == '2.6.43',
             'la mise à jour appliquée est annoncée',
             str(apres.etat['version_installee']))
    apres.ecarter()
    verifier(apres.etat['version_installee'] is None, 'annonce refermable')

    # Deuxième démarrage sur la même version : plus rien à annoncer.
    encore = UN.UpdateNotifier(config_dir=cfg_inst)
    verifier(encore.etat['version_installee'] is None,
             'annonce non répétée au démarrage suivant')

    # Une version plus ANCIENNE que celle vue (retour arrière manuel) ne doit
    # pas être présentée comme une mise à jour réussie.
    with open(os.path.join(cfg_inst, 'update_state.json'), 'w', encoding='utf-8') as f:
        json.dump({'version_vue': '2.6.50'}, f)
    recul = UN.UpdateNotifier(config_dir=cfg_inst)
    verifier(recul.etat['version_installee'] is None, 'retour arrière non annoncé')
finally:
    UC.__version__ = version_reelle

print("\n=== 9. Après remplacement, l'application doit rendre la main ===")
# Windows verrouille l'exécutable en cours : tant que le processus vit, le
# script de remplacement ne peut pas déplacer le fichier. Sans arrêt explicite,
# l'installation aboutissait « en apparence » et rien ne changeait.
cfg_arret = tempfile.mkdtemp(prefix='maj_arret_')
notifieur = UN.UpdateNotifier(config_dir=cfg_arret)
notifieur.checker.version_json_url = BASE + '/version.json'
notifieur.checker.current_version = '2.0.0'
notifieur.installable = True          # simule l'exécutable packagé
notifieur.verifier(force=True)

arrets = []
notifieur._arreter_pour_redemarrage = lambda: arrets.append(True)
notifieur.checker.download_update = lambda progress=None: 'faux_binaire.exe'
notifieur.checker.install_update = lambda chemin: True

verifier(notifieur.installer() is True, 'installation engagée')
for _ in range(50):
    if notifieur.phase in ('pret', 'erreur'):
        break
    time.sleep(0.05)
verifier(notifieur.phase == 'pret', 'installation menée à son terme', notifieur.phase)
verifier(arrets == [True], "l'arrêt du processus est demandé", str(arrets))

# Et si le remplacement échoue, surtout ne pas arrêter l'application : elle
# resterait fermée sans qu'aucune nouvelle version ne la remplace.
rate = UN.UpdateNotifier(config_dir=tempfile.mkdtemp(prefix='maj_rate_'))
rate.checker.version_json_url = BASE + '/version.json'
rate.checker.current_version = '2.0.0'
rate.installable = True
rate.verifier(force=True)
arrets_rate = []
rate._arreter_pour_redemarrage = lambda: arrets_rate.append(True)
rate.checker.download_update = lambda progress=None: 'faux_binaire.exe'
rate.checker.install_update = lambda chemin: False
rate.installer()
for _ in range(50):
    if rate.phase in ('pret', 'erreur'):
        break
    time.sleep(0.05)
verifier(rate.phase == 'erreur', 'échec du remplacement signalé', rate.phase)
verifier(arrets_rate == [], "aucun arrêt quand le remplacement a échoué",
         str(arrets_rate))

print("\n=== 10. Le remplacement est confié au nouvel exécutable ===")
import pathlib  # noqa: E402
import applique_maj  # noqa: E402

faux = UC.UpdateChecker(config_dir=tempfile.mkdtemp(prefix='maj_lancement_'))
dossier_maj = pathlib.Path(faux.config_dir) / 'maj'
dossier_maj.mkdir()
binaire = dossier_maj / 'ParcInfo-Windows.exe'
binaire.write_bytes(b'nouveau binaire')
lance = {}
UC.subprocess.Popen = lambda *a, **kw: (lance.setdefault('cmd', a[0]),
                                        lance.update(kw))
try:
    resultat = faux._install_windows(binaire)
finally:
    import subprocess as _sp
    UC.subprocess.Popen = _sp.Popen

verifier(resultat is True, 'remplacement programmé')

commande = lance.get('cmd') or []
verifier(commande and commande[0] == str(binaire),
         "c'est le binaire téléchargé qui est lancé, pas cmd.exe", str(commande[:1]))
verifier(applique_maj.INDICATEUR in commande,
         "il est lancé en mode application de mise à jour", str(commande))
verifier(str(sys.executable) in commande,
         "l'exécutable à remplacer lui est désigné", str(commande))
verifier(str(os.getpid()) in commande,
         "le processus à attendre lui est désigné", str(commande))
verifier(lance.get('cwd') == str(dossier_maj),
         "il ne travaille pas depuis le dossier de l'application", str(lance.get('cwd')))

env_transmis = lance.get('env') or {}
verifier(bool(env_transmis), "un environnement explicite lui est transmis")
verifier(not any(n in env_transmis for n in UC.VARIABLES_BOOTLOADER),
         "l'environnement transmis est débarrassé des repères du lanceur",
         str([n for n in UC.VARIABLES_BOOTLOADER if n in env_transmis]))
verifier('PATH' in env_transmis or 'Path' in env_transmis,
         "le reste de l'environnement est conservé")


print("\n=== 11. Le remplacement lui-même ===")
# Le déroulé complet sur de vrais fichiers : c'est l'étape qui a échoué en
# production sans laisser de trace, elle doit être éprouvée pour de bon.
atelier = pathlib.Path(tempfile.mkdtemp(prefix='maj_applique_'))
cible = atelier / 'ParcInfo-Windows.exe'
source_dir = atelier / 'maj'
source_dir.mkdir()
source = source_dir / 'ParcInfo-Windows.exe'
cible.write_bytes(b'ancienne version')
source.write_bytes(b'nouvelle version' * 1000)
journal = atelier / '_maj.log'

relances = []
vrai_relancer = applique_maj._relancer
applique_maj._relancer = lambda c, t: relances.append(c)
vrai_executable = applique_maj.sys.executable
applique_maj.sys.executable = str(source)
try:
    code = applique_maj.appliquer(['--cible', str(cible), '--journal', str(journal)])
finally:
    applique_maj.sys.executable = vrai_executable

verifier(code == 0, 'le remplacement aboutit', 'code %s' % code)
verifier(cible.read_bytes() == source.read_bytes(),
         "l'exécutable en place est bien le nouveau")
verifier(not (atelier / 'ParcInfo-Windows.exe.old').exists(),
         "l'ancienne version est retirée une fois le contrôle passé")
verifier(relances == [str(cible)], "l'application est relancée", str(relances))
trace = journal.read_text(encoding='utf-8') if journal.exists() else ''
verifier('empreinte identique' in trace,
         "l'empreinte de la copie est confrontée à celle de la source", trace[-200:])

# Une copie tronquée — antivirus, disque plein — donnerait un exécutable qui ne
# démarre pas. L'ancienne version doit alors revenir.
cible.write_bytes(b'ancienne version')
relances.clear()
applique_maj.sys.executable = str(source)
vrai_copie = applique_maj.shutil.copy2
applique_maj.shutil.copy2 = lambda s, d: pathlib.Path(d).write_bytes(
    pathlib.Path(s).read_bytes()[:50])
try:
    code = applique_maj.appliquer(['--cible', str(cible), '--journal', str(journal)])
finally:
    applique_maj.shutil.copy2 = vrai_copie
    applique_maj.sys.executable = vrai_executable
    applique_maj._relancer = vrai_relancer

verifier(code == 5, 'une copie infidèle est refusée', 'code %s' % code)
verifier(cible.read_bytes() == b'ancienne version',
         "l'ancienne version est remise en place", repr(cible.read_bytes()[:40]))
verifier(relances == [str(cible)],
         "l'application est relancée malgré tout", str(relances))

# Les reliquats sont effacés par l'application relancée : le processus qui
# vient d'appliquer la mise à jour s'exécutait depuis le dossier de
# téléchargement, et l'ancien exécutable était encore tenu par Windows.
reliquat = atelier / 'ParcInfo-Windows.exe.old'
reliquat.write_bytes(b'ancienne version')
applique_maj.nettoyer_reliquats(str(source_dir), executable=str(cible))
verifier(not source_dir.exists(), 'le dossier de téléchargement est supprimé')
verifier(not reliquat.exists(), 'la version précédente est supprimée')

httpd.shutdown()
print('\n  ' + ('TOUT OK' if not echecs else 'ÉCHECS : ' + ', '.join(echecs)))
sys.exit(1 if echecs else 0)
