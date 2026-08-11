"""Application d'une mise à jour, exécutée par le NOUVEL exécutable.

Le remplacement était jusqu'ici confié à un script .bat écrit à la volée par
l'application sortante. Trois défauts, tous rencontrés :

  - aucune trace exploitable : quand le redémarrage échouait, il ne restait
    qu'une boîte de dialogue et rien à examiner ;
  - c'était l'ANCIENNE version qui pilotait le remplacement, donc un correctif
    du mécanisme ne s'appliquait jamais à la mise à jour qui l'installait ;
  - un script batch impose ses propres contraintes — encodage, guillemets,
    codes de retour — sur un enchaînement qui demande de la précision.

Ici, c'est le binaire téléchargé, déjà vérifié et non verrouillé, qui fait le
travail : il attend la sortie de l'application, se recopie sur elle, contrôle
que la copie est fidèle, puis relance. Tout est journalisé.

Ce module est importé au tout début du lanceur : il ne doit rien importer de
lourd, pour que le mode « application de mise à jour » démarre sans embarquer
Flask ni le reste.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

INDICATEUR = '--appliquer-maj'

#: Variables posées par le lanceur PyInstaller. Un processus lancé depuis
#: l'application packagée en hérite ; on ne les transmet pas plus loin.
_VARIABLES_LANCEUR = (
    '_MEIPASS', '_MEIPASS2', '_PYI_APPLICATION_HOME_DIR',
    '_PYI_ARCHIVE_FILE', '_PYI_PARENT_PROCESS_LEVEL', '_PYI_SPLASH_IPC',
)

#: Durée maximale d'attente de la sortie de l'application.
ATTENTE_MAX_S = 90
#: Tentatives de remplacement du fichier, tant qu'il reste verrouillé.
TENTATIVES = 20


def environnement_propre():
    """Environnement débarrassé des repères du lanceur PyInstaller."""
    propre = dict(os.environ)
    for nom in _VARIABLES_LANCEUR:
        propre.pop(nom, None)
    return propre


def _empreinte(chemin):
    sha = hashlib.sha256()
    with open(chemin, 'rb') as f:
        for bloc in iter(lambda: f.read(1024 * 1024), b''):
            sha.update(bloc)
    return sha.hexdigest()


def _attendre_sortie(pid, tracer):
    """Attend la fin du processus appelant.

    Le fichier ne peut être remplacé qu'une fois l'application sortie. On
    interroge le système plutôt que d'attendre une durée fixe : une machine
    chargée met plus longtemps, et une attente trop courte faisait échouer le
    remplacement en silence.
    """
    if not pid:
        return True
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        noyau = ctypes.windll.kernel32
        poignee = noyau.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not poignee:
            tracer('processus %s déjà terminé' % pid)
            return True
        resultat = noyau.WaitForSingleObject(poignee, ATTENTE_MAX_S * 1000)
        noyau.CloseHandle(poignee)
        if resultat == 0:
            tracer('processus %s terminé' % pid)
            return True
        tracer('processus %s toujours actif après %d s' % (pid, ATTENTE_MAX_S))
        return False
    except Exception as e:
        # Sans API Windows (autre système, ou appel refusé), on retombe sur une
        # attente simple plutôt que d'abandonner.
        tracer('attente par défaut (%s)' % e)
        time.sleep(5)
        return True


def _emplacement_sauvegarde(cible, tracer):
    """Choisit où mettre l'ancienne version de côté.

    Le reliquat d'une mise à jour précédente peut rester verrouillé — observé
    en production, plus de deux heures durant. Le supprimer relève du ménage et
    n'a rien à voir avec la mise à jour en cours : il ne doit pas la retarder.
    On tente donc une fois, et si le fichier résiste on prend un nom libre.
    """
    for i in range(10):
        chemin = cible + '.old' + ('' if i == 0 else '.%d' % i)
        if not os.path.exists(chemin):
            return chemin
        try:
            os.remove(chemin)
            return chemin
        except OSError as e:
            tracer('reliquat verrouillé, il sera supprimé au prochain démarrage '
                   ': %s (%s)' % (os.path.basename(chemin), e))
    return cible + '.remplace'


def appliquer(arguments):
    """Applique la mise à jour. Retourne le code de sortie du processus."""
    valeurs = {}
    for i, mot in enumerate(arguments):
        if mot.startswith('--') and i + 1 < len(arguments):
            valeurs[mot] = arguments[i + 1]

    cible = valeurs.get('--cible')
    pid = valeurs.get('--pid')
    journal = valeurs.get('--journal') or (
        os.path.join(os.path.dirname(cible or sys.executable), '_maj.log'))

    def tracer(message):
        ligne = '%s  %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), message)
        try:
            with open(journal, 'a', encoding='utf-8') as f:
                f.write(ligne + '\n')
        except OSError:
            pass

    source = sys.executable
    tracer('--- application de la mise à jour ---')
    tracer('source : %s' % source)
    tracer('cible  : %s' % cible)

    if not cible:
        tracer('ÉCHEC : aucune cible indiquée')
        return 2
    if os.path.abspath(source) == os.path.abspath(cible):
        tracer('ÉCHEC : la source et la cible sont le même fichier')
        return 2

    _attendre_sortie(pid, tracer)

    empreinte_source = _empreinte(source)
    tracer('empreinte de la source : %s' % empreinte_source)
    sauvegarde = _emplacement_sauvegarde(cible, tracer)

    # Mise à l'écart de l'ancienne version. Windows garde le verrou un court
    # instant après la sortie du processus : on réessaie au lieu de renoncer.
    # Seul ce déplacement-là mérite d'attendre — le ménage des reliquats a été
    # fait plus haut, sans droit de blocage.
    deplace = False
    for tentative in range(1, TENTATIVES + 1):
        try:
            if os.path.exists(cible):
                os.replace(cible, sauvegarde)
            deplace = True
            break
        except OSError as e:
            tracer('tentative %d : fichier encore verrouillé (%s)' % (tentative, e))
            time.sleep(2)

    if not deplace:
        tracer('ÉCHEC : impossible de mettre l\'ancienne version de côté')
        _relancer(cible, tracer)
        return 3

    # Copie plutôt que déplacement : la source doit rester lisible pour la
    # vérification, et un déplacement entre volumes n'est pas atomique.
    try:
        shutil.copy2(source, cible)
    except OSError as e:
        tracer('ÉCHEC de la copie (%s) — retour à la version précédente' % e)
        try:
            os.replace(sauvegarde, cible)
        except OSError:
            pass
        _relancer(cible, tracer)
        return 4

    # Contrôle du fichier écrit : une copie tronquée, ou amputée par un
    # antivirus, donnerait un exécutable qui ne démarre pas — précisément le
    # symptôme qu'on cherche à écarter.
    empreinte_ecrite = _empreinte(cible)
    if empreinte_ecrite != empreinte_source:
        tracer('ÉCHEC : la copie ne correspond pas (%s) — retour à la version précédente'
               % empreinte_ecrite)
        try:
            os.remove(cible)
            os.replace(sauvegarde, cible)
        except OSError as e:
            tracer('restauration impossible : %s' % e)
        _relancer(cible, tracer)
        return 5

    tracer('remplacement vérifié, empreinte identique')
    try:
        os.remove(sauvegarde)
    except OSError:
        pass

    _relancer(cible, tracer)
    tracer('--- terminé ---')
    return 0


def _relancer(cible, tracer):
    """Relance l'application, dans un environnement propre."""
    try:
        subprocess.Popen(
            [cible], cwd=os.path.dirname(cible) or None,
            env=environnement_propre(), close_fds=True,
            creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0))
        tracer('application relancée')
    except Exception as e:
        tracer('relance impossible : %s' % e)


def nettoyer_reliquats(dossier_telechargement, executable=None, tracer=None):
    """Efface ce que la mise à jour laisse derrière elle.

    Appelé par l'application au démarrage, et pas par le processus qui vient
    d'appliquer la mise à jour : celui-ci s'exécute depuis le dossier de
    téléchargement, et Windows tient encore l'image de l'ancien exécutable à
    l'instant où il rend la main — la suppression y échoue (constaté).
    """
    try:
        if dossier_telechargement and os.path.isdir(dossier_telechargement):
            shutil.rmtree(dossier_telechargement, ignore_errors=True)
            if tracer:
                tracer('dossier de téléchargement supprimé : %s'
                       % dossier_telechargement)
    except Exception:
        pass

    # `.old`, ses variantes numérotées et le nom de dernier recours : un
    # reliquat encore verrouillé au moment du remplacement a fait prendre un
    # autre nom, et lui aussi doit finir par disparaître.
    base = executable or sys.executable
    reliquats = [base + '.old'] + [base + '.old.%d' % i for i in range(1, 10)]
    reliquats.append(base + '.remplace')
    for chemin in reliquats:
        try:
            if os.path.isfile(chemin):
                os.remove(chemin)
                if tracer:
                    tracer('version précédente supprimée : %s' % chemin)
        except OSError as e:
            if tracer:
                tracer('version précédente encore verrouillée : %s (%s)' % (chemin, e))


def mode_mise_a_jour(argv=None):
    """Retourne les arguments si l'exécutable est lancé en applicateur."""
    argv = argv if argv is not None else sys.argv
    return argv[1:] if INDICATEUR in argv else None
