"""
Détection et installation des mises à jour de ParcInfo.

Trois modes d'exécution, trois comportements :

  - exécutable Windows / macOS : téléchargement puis remplacement sur place,
    déclenché par l'utilisateur depuis l'interface ;
  - conteneur Docker : aucune installation possible — un conteneur ne peut pas
    se remplacer lui-même. On signale la version et la commande à lancer ;
  - sources (développement) : détection seule.

Le binaire téléchargé n'est jamais exécuté sans que son empreinte SHA-256 ait
été confrontée au fichier SHA256SUMS.txt publié avec la version. Sans empreinte
vérifiable, l'installation est refusée : télécharger un exécutable et le lancer
sans contrôle offre à quiconque détourne la connexion un chemin direct vers la
machine.

Usage :
    from update_checker import UpdateChecker
    checker = UpdateChecker()
    checker.check_for_updates()
"""

import applique_maj
import hashlib
import json
import logging
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Callable
from urllib.request import Request, urlopen
from urllib.error import URLError

from __version__ import __version__, GITHUB_REPO, GITHUB_RAW_CONTENT

logger = logging.getLogger("parcinfo.updater")

# Fichier d'empreintes publié avec chaque version par le workflow de release.
SHA256SUMS_URL = ("https://github.com/%s/releases/download/v{version}/SHA256SUMS.txt"
                  % GITHUB_REPO)


class UpdateCheckError(Exception):
    """Échec d'une étape de mise à jour."""
    pass


def running_in_docker() -> bool:
    """Vrai si l'application tourne dans un conteneur."""
    if str(os.environ.get('RUNNING_IN_DOCKER', '')).lower() in ('1', 'true', 'yes'):
        return True
    # Repli : le fichier existe dans tout conteneur Docker classique.
    return os.path.exists('/.dockerenv')


def is_frozen() -> bool:
    """Vrai si l'on tourne depuis un exécutable packagé (PyInstaller)."""
    return bool(getattr(sys, 'frozen', False))


def runtime_mode() -> str:
    """'docker' | 'windows' | 'macos' | 'linux' | 'source'."""
    if running_in_docker():
        return 'docker'
    if not is_frozen():
        return 'source'
    system = platform.system()
    return {'Windows': 'windows', 'Darwin': 'macos'}.get(system, 'linux')


def can_self_install() -> bool:
    """Vrai si ce mode d'exécution sait se remplacer lui-même."""
    return runtime_mode() in ('windows', 'macos')


def archi_materielle_macos() -> str:
    """Architecture matérielle réelle du Mac ('arm64' ou 'x86_64'), fiable
    même sous Rosetta.

    platform.machine() reflète l'architecture du PROCESSUS EN COURS, pas
    celle de la puce : un exécutable Intel tournant sous Rosetta sur un Mac
    Apple Silicon y répond 'x86_64' indéfiniment. Un Mac qui se serait
    retrouvé une fois sur le mauvais binaire (ancien bug de sélection,
    téléchargement manuel...) resterait donc bloqué dessus pour toujours :
    chaque mise à jour reconfirmerait 'x86_64' comme architecture attendue et
    retéléchargerait le même binaire Intel, jamais l'ARM natif. hw.optional.arm64
    interroge le matériel lui-même, jamais traduit par Rosetta.
    """
    try:
        r = subprocess.run(['sysctl', '-n', 'hw.optional.arm64'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip() == '1':
            return 'arm64'
    except Exception:
        pass
    return 'x86_64' if platform.machine() == 'x86_64' else 'arm64'


#: Variables posées par le lanceur de PyInstaller pour désigner le dossier
#: temporaire où l'application a été décompressée. Un processus lancé depuis
#: l'application packagée en hérite ; on ne les transmet pas plus loin, par
#: hygiène — les essais menés depuis n'ont pas montré qu'elles suffisaient à
#: provoquer l'échec « Failed to load Python DLL » observé sur un poste.
VARIABLES_BOOTLOADER = applique_maj._VARIABLES_LANCEUR

#: Conservé sous son ancien nom : le reste du code et les tests s'y réfèrent.
environnement_sans_bootloader = applique_maj.environnement_propre


class UpdateChecker:
    """
    Surveille les versions publiées et applique la mise à jour sur demande.
    """

    # Fréquence de vérification. Une version publiée doit être visible le jour
    # même : à 30 jours, l'information arrivait après coup et ne servait plus.
    CHECK_INTERVAL_HOURS = 6

    def __init__(self, config_dir: Optional[Path] = None,
                 version_json_url: Optional[str] = None,
                 callback: Optional[Callable] = None):
        """
        Args:
            config_dir: dossier des métadonnées (défaut : ~/.parcinfo)
            version_json_url: URL de version.json (défaut : branche master)
            callback: appelé avec la nouvelle version quand il y en a une
        """
        self.config_dir = Path(config_dir) if config_dir else Path.home() / ".parcinfo"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.version_json_url = version_json_url or f"{GITHUB_RAW_CONTENT}/version.json"

        self.metadata_file = self.config_dir / "update_metadata.json"
        self.callback = callback
        self.current_version = __version__
        self.latest_version = None
        self.available_update = False
        self.release_notes = None
        self.release_url = None
        # Progression du téléchargement en cours, 0-100 (None hors téléchargement)
        self.download_progress = None
        # Débit observé, en Ko/s : sans lui, « c'est lent » reste invérifiable.
        self.download_rate_kbs = None
        # Raison précise du dernier install_update() en échec, si connue —
        # UpdateNotifier s'en sert pour un message d'erreur exploitable plutôt
        # que le générique « le remplacement a échoué », visible dans l'UI et
        # journalisé (journal_maj, synchronisé) pour un diagnostic à distance.
        self.last_install_error = None

    # ─────────────────────────────────────────────────────────────────────────
    # Détection
    # ─────────────────────────────────────────────────────────────────────────

    def check_for_updates(self, force: bool = False) -> bool:
        """Interroge le dépôt. Retourne True si une version plus récente existe."""
        if not force and not self._should_check():
            logger.debug("Vérification ignorée (moins de %sh depuis la dernière)",
                         self.CHECK_INTERVAL_HOURS)
            return self.available_update

        logger.info("Recherche d'une mise à jour...")

        try:
            metadata = self._fetch_version_metadata()
            latest_version = metadata.get("version")

            if not latest_version:
                logger.warning("Aucune version dans les métadonnées distantes")
                return False

            self.latest_version = latest_version
            self.release_url = metadata.get("release_notes_url")
            self.release_notes = (metadata.get("notes") or {}).get("improvements") or []
            self._save_metadata(metadata)

            if self._is_newer_version(latest_version, self.current_version):
                logger.info("Nouvelle version disponible : %s", latest_version)
                self.available_update = True
                if self.callback:
                    self.callback(latest_version)
                return True

            logger.info("Déjà à jour (%s)", self.current_version)
            self.available_update = False
            return False

        except Exception as e:
            logger.warning("Échec de la vérification : %s", e)
            return False
        finally:
            self._update_last_check_time()

    # ─────────────────────────────────────────────────────────────────────────
    # Téléchargement et installation
    # ─────────────────────────────────────────────────────────────────────────

    def download_update(self, version: Optional[str] = None,
                        progress: Optional[Callable] = None) -> Optional[Path]:
        """
        Télécharge le binaire de la version puis vérifie son empreinte.

        Args:
            version: version à télécharger (défaut : la dernière détectée)
            progress: appelé avec un pourcentage entier pendant le téléchargement

        Retourne le chemin du fichier vérifié.
        """
        version = version or self.latest_version
        if not version:
            raise UpdateCheckError("Aucune version à télécharger")

        metadata = self._load_metadata() or self._fetch_version_metadata()
        downloads = metadata.get("downloads", {})

        platform_key = self._get_platform_key()
        if platform_key not in downloads:
            raise UpdateCheckError("Aucun téléchargement pour %s" % platform_key)

        download_url = downloads[platform_key]
        nom_fichier = download_url.rsplit('/', 1)[-1]

        # L'empreinte est réclamée AVANT de télécharger : inutile de tirer
        # 30 Mo pour découvrir ensuite qu'on ne pourra pas les valider.
        attendue = self._expected_sha256(version, nom_fichier, metadata, platform_key)
        if not attendue:
            raise UpdateCheckError(
                "Empreinte SHA-256 introuvable pour %s : installation refusée. "
                "Téléchargez la version manuellement depuis la page des versions."
                % nom_fichier)

        # Sous-dossier dédié, jamais à côté de l'exécutable. Le binaire publié
        # s'appelle ParcInfo-Windows.exe, et c'est aussi le nom sous lequel il
        # tourne quand on l'a pris sur la page des versions : écrire là revenait
        # à tenter d'écraser l'exécutable en cours, que Windows verrouille
        # (« WinError 5 : accès refusé »). Le remplacement est le travail du
        # script différé, pas celui du téléchargement.
        dossier = self.config_dir / 'maj'
        dossier.mkdir(parents=True, exist_ok=True)
        destination = dossier / nom_fichier

        try:
            if os.path.samefile(str(destination), sys.executable):
                raise UpdateCheckError(
                    "Le téléchargement viserait l'exécutable en cours (%s)" % destination)
        except (OSError, ValueError):
            # samefile lève si la cible n'existe pas encore : c'est le cas normal.
            pass

        # Reliquats des versions antérieures, qui téléchargeaient à côté de
        # l'exécutable : jusqu'à 30 Mo abandonnés là après chaque échec.
        for reste in self.config_dir.glob('*.part'):
            try:
                reste.unlink()
                logger.info("Fichier de téléchargement abandonné supprimé : %s", reste.name)
            except OSError:
                pass
        logger.info("Téléchargement de %s (%s)...", nom_fichier, version)
        self.download_progress = 0
        try:
            self._download_file(download_url, destination, progress=progress)

            obtenue = self._calculate_checksum(destination)
            if obtenue.lower() != attendue.lower():
                destination.unlink(missing_ok=True)
                raise UpdateCheckError(
                    "Empreinte incorrecte : le fichier téléchargé ne correspond pas "
                    "à la version publiée. Téléchargement abandonné.")

            logger.info("Téléchargement vérifié : %s", destination)
            return destination
        finally:
            self.download_progress = None

    def install_update(self, installer_path: Path) -> bool:
        """Remplace l'application par le fichier téléchargé. Retourne True si engagé."""
        installer_path = Path(installer_path)
        if not installer_path.exists():
            logger.error("Fichier de mise à jour introuvable : %s", installer_path)
            return False

        mode = runtime_mode()
        if mode == 'windows':
            return self._install_windows(installer_path)
        if mode == 'macos':
            return self._install_macos(installer_path)

        logger.warning("Installation automatique indisponible dans le mode « %s »", mode)
        return False

    def _install_windows(self, installer_path: Path) -> bool:
        """
        ParcInfo est un exécutable portable unique : le fichier téléchargé EST la
        nouvelle application. Windows verrouille l'exe en cours d'exécution, le
        remplacement revient donc à un autre processus.

        C'est le binaire téléchargé lui-même qui s'en charge, relancé avec
        « --appliquer-maj ». Il n'est pas verrouillé, son empreinte vient d'être
        vérifiée, et il porte la version la plus récente du mécanisme : un
        correctif s'applique dès la mise à jour qui l'installe, au lieu d'attendre
        la suivante. Le script batch qui tenait ce rôle ne laissait par ailleurs
        aucune trace exploitable quand il échouait.
        """
        current_exe = Path(sys.executable)
        journal = self.config_dir / "_maj.log"

        commande = [
            str(installer_path), applique_maj.INDICATEUR,
            '--cible', str(current_exe),
            '--pid', str(os.getpid()),
            '--journal', str(journal),
        ]

        creationflags = 0
        if hasattr(subprocess, 'CREATE_NO_WINDOW') and hasattr(subprocess, 'DETACHED_PROCESS'):
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        try:
            subprocess.Popen(
                commande, creationflags=creationflags, close_fds=True,
                # Hors du dossier de l'application : un processus dont le
                # répertoire courant est celui de l'exe y maintient une prise.
                cwd=str(installer_path.parent),
                env=applique_maj.environnement_propre())
        except OSError as e:
            logger.error("Lancement du programme de remplacement impossible : %s", e)
            return False

        logger.info("Remplacement confié à %s — l'application va redémarrer",
                    installer_path.name)
        return True

    def _install_macos(self, archive_path: Path) -> bool:
        """
        La version macOS est publiée en archive ZIP contenant ParcInfo.app.
        (Le code précédent tentait un `hdiutil attach`, réservé aux images DMG :
        le montage échouait à chaque fois et aucune mise à jour n'aboutissait.)
        """
        app_actuelle = self._macos_app_path()
        if not app_actuelle:
            logger.error("Impossible de localiser ParcInfo.app à remplacer")
            return False

        with tempfile.TemporaryDirectory() as tmp:
            try:
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(tmp)
            except zipfile.BadZipFile:
                logger.error("Archive macOS illisible : %s", archive_path)
                return False

            source = None
            for racine, dossiers, _ in os.walk(tmp):
                for d in dossiers:
                    if d.endswith('.app'):
                        source = Path(racine) / d
                        break
                if source:
                    break

            if not source:
                logger.error("Aucun bundle .app dans l'archive")
                return False

            # Filet de sécurité : le sélecteur de version.json vient d'être
            # corrigé après avoir renvoyé le zip ARM à un Mac Intel (l'app
            # remplacée refusait ensuite de démarrer, « n'est pas prise en
            # charge par ce Mac », sans qu'aucun rollback ne se déclenche).
            # Un contrôle direct sur le binaire téléchargé, ici, protège
            # aussi contre une éventuelle prochaine confusion côté
            # publication — en refusant AVANT de toucher à la version qui
            # fonctionne, pas après coup une fois l'ancienne effacée.
            archi_attendue = archi_materielle_macos()
            executable_source = source / 'Contents' / 'MacOS' / 'ParcInfo'
            if executable_source.exists():
                try:
                    r = subprocess.run(['file', str(executable_source)],
                                       capture_output=True, text=True, timeout=15)
                    if archi_attendue not in r.stdout:
                        logger.error(
                            "Architecture du binaire téléchargé incompatible avec ce Mac "
                            "(attendu %s) : %s — mise à jour annulée, version actuelle conservée",
                            archi_attendue, r.stdout.strip())
                        self.last_install_error = (
                            "Le fichier téléchargé ne correspond pas à l'architecture de ce "
                            "Mac (attendu %s) — version actuelle conservée." % archi_attendue)
                        return False
                except Exception as e:
                    logger.debug("Vérification d'architecture impossible (non bloquant) : %s", e)

            sauvegarde = Path(str(app_actuelle) + '.old')
            if sauvegarde.exists():
                shutil.rmtree(sauvegarde, ignore_errors=True)
            try:
                if app_actuelle.exists():
                    shutil.move(str(app_actuelle), str(sauvegarde))
                shutil.copytree(str(source), str(app_actuelle), symlinks=True)
            except Exception as e:
                logger.error("Remplacement du bundle impossible : %s", e)
                if sauvegarde.exists() and not app_actuelle.exists():
                    shutil.move(str(sauvegarde), str(app_actuelle))
                return False

        # Avant les versions embarquant le correctif de launcher.py::data(),
        # la BD/uploads/secret.key vivaient à côté de l'exe — c'est-à-dire
        # DANS Contents/MacOS/ du bundle qu'on vient de mettre de côté. Une
        # installation déjà à jour n'a plus rien à cet endroit (ses données
        # sont dans ~/Library/Application Support/ParcInfo) ; ce rattrapage
        # ne fait donc rien pour elle — il protège uniquement la toute
        # première mise à jour depuis une version antérieure au correctif,
        # sans quoi ces fichiers disparaîtraient avec `sauvegarde` juste en
        # dessous, sans aucun moyen de les récupérer.
        self._migrer_donnees_bundle_macos(sauvegarde)

        self._debloquer_gatekeeper_macos(app_actuelle)
        shutil.rmtree(sauvegarde, ignore_errors=True)

        # Relance VÉRIFIÉE, pas un tir à l'aveugle suivi d'un arrêt inconditionnel
        # de l'instance actuelle par l'appelant (UpdateNotifier._arreter_pour_
        # redemarrage, sur la seule foi du True retourné ici). Signalé en usage
        # réel sur Mac Intel : l'ancienne instance disparaissait (tuée dès que
        # cette méthode rendait la main) et la nouvelle ne s'ouvrait jamais —
        # rien ne tournait, sans qu'aucune erreur n'explique pourquoi, faute de
        # vérification à cet endroit. macOS ne verrouille pas un bundle .app en
        # cours d'exécution (contrairement à un .exe Windows) : rien n'empêche
        # de vérifier que la nouvelle version démarre bien avant de laisser
        # l'appelant arrêter l'ancienne — la nouvelle prend simplement un autre
        # port libre le temps du recouvrement (voir launcher.py).
        #
        # Lancement DIRECT de l'exécutable — jamais via `open` (Launch
        # Services). Diagnostic confirmé sur plusieurs cycles de mise à jour
        # réels (2.18.6 à 2.18.9, journal parcinfo.log à l'appui) : la
        # vérification échouait alors même qu'AUCUN avertissement Gatekeeper
        # n'était journalisé — signe que le bundle était accepté, mais que le
        # process ne démarrait quand même pas au chemin attendu. Cause
        # documentée : la « translocation » macOS (Gatekeeper Path
        # Randomization) copie un bundle non notarié vers un chemin aléatoire
        # en lecture seule dès qu'il est ouvert via Launch Services (Finder ou
        # `open`) ET porte encore une trace de quarantaine — deux des trois
        # conditions que `open` réunissait justement ici. Lancer l'exécutable
        # directement (comme depuis un terminal) élimine cette condition
        # d'office, quel que soit l'état de la quarantaine — confirmé par
        # plusieurs sources indépendantes (voir CHANGELOG). Bénéfice
        # secondaire : le PID est connu directement, la vérification n'a plus
        # besoin de deviner un chemin via pgrep (qui échouait justement à
        # trouver un process translocé, invisible à ce chemin précis).
        executable_cible = app_actuelle / 'Contents' / 'MacOS' / 'ParcInfo'
        processus = None
        try:
            processus = subprocess.Popen(
                [str(executable_cible)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True)
            logger.info("Lancement direct de %s (pid %s)", executable_cible, processus.pid)
        except Exception as e:
            logger.warning("Échec du lancement direct de %s : %s", executable_cible, e)

        # Un Popen encore vivant après quelques secondes est un signal bien
        # plus fiable qu'une recherche par chemin (pgrep) : il vient
        # directement du fork/exec, indépendant de la façon dont macOS a pu
        # déplacer le bundle. Fenêtre inchangée (~10 s, le temps normal d'un
        # démarrage Flask) ; `for…else` : le `else` ne s'exécute que si la
        # boucle est allée à son terme SANS `break`, donc si le process n'est
        # jamais sorti pendant toute la fenêtre d'attente.
        lancee = False
        code_sortie_immediat = None
        if processus is not None:
            for _ in range(20):
                code_sortie_immediat = processus.poll()
                if code_sortie_immediat is not None:
                    break
                time.sleep(0.5)
            else:
                lancee = True

        if not lancee:
            if processus is None:
                logger.error("Le lancement direct n'a jamais pu démarrer (voir l'avertissement ci-dessus).")
            else:
                logger.error(
                    "Le process relancé directement s'est arrêté tout seul (code de "
                    "sortie %s) au lieu de continuer à tourner — pas un blocage "
                    "Gatekeeper/translocation (le lancement direct l'élimine), plutôt "
                    "un plantage immédiat de la nouvelle version elle-même.",
                    code_sortie_immediat)

            logger.error(
                "La nouvelle version ne démarre pas après remplacement. L'instance "
                "actuelle n'est PAS arrêtée : mieux vaut rester sur l'ancienne version "
                "que de ne laisser tourner ni l'une ni l'autre.")
            self.last_install_error = (
                "L'application a été remplacée mais ne démarre pas. L'ancienne "
                "version continue de tourner, rien n'a été perdu. Essayez de "
                "l'ouvrir manuellement pour voir le message exact, ou consultez "
                "le journal de l'application (parcinfo.log).")
            return False

        logger.info("Application macOS remplacée et relancée avec succès (pid %s)", processus.pid)
        return True

    @staticmethod
    def _migrer_donnees_bundle_macos(ancien_bundle: Path) -> None:
        """Déplace BD/uploads/secret.key/backups s'ils sont restés dans
        l'ancien bundle .app (voir _install_macos ci-dessus)."""
        ancien = ancien_bundle / 'Contents' / 'MacOS'
        if not (ancien / 'parc_info.db').exists():
            return
        # PARCINFO_MACOS_DATA_DIR : surcharge réservée aux tests, pour ne
        # jamais écrire dans le vrai dossier utilisateur pendant la suite.
        nouveau = Path(os.environ.get('PARCINFO_MACOS_DATA_DIR') or (
            Path(os.path.expanduser('~')) / 'Library' / 'Application Support' / 'ParcInfo'))
        if (nouveau / 'parc_info.db').exists():
            return
        nouveau.mkdir(parents=True, exist_ok=True)
        for nom in ('parc_info.db', 'secret.key', 'uploads', 'backups'):
            src, dst = ancien / nom, nouveau / nom
            if src.exists() and not dst.exists():
                try:
                    shutil.move(str(src), str(dst))
                except OSError as e:
                    logger.error("Migration de %s impossible : %s", nom, e)

    @staticmethod
    def _gatekeeper_accepte(bundle: Path) -> bool:
        """True si `spctl` — la même évaluation que macOS fait réellement à
        l'ouverture — accepterait de lancer ce bundle maintenant. Interrogé
        directement plutôt que supposé à partir de ce qu'on vient de faire :
        voir la note dans `_debloquer_gatekeeper_macos` sur pourquoi deviner
        s'est révélé peu fiable ici."""
        try:
            r = subprocess.run(['spctl', '--assess', '--type', 'execute', str(bundle)],
                               capture_output=True, text=True, timeout=15)
            return r.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _autoriser_via_spctl_add(bundle: Path) -> bool:
        """Dernier recours, SEULEMENT si xattr -cr et la signature ad hoc ont
        toutes deux échoué à convaincre `spctl` : inscrit explicitement une
        exception pour ce bundle dans la base de confiance de Gatekeeper
        (`spctl --add`), plutôt que de simplement nettoyer le fichier et
        espérer que ça passe. Mécanisme différent de tout ce qui précède —
        une autorisation posée en dur, pas un nettoyage d'attributs.

        Nécessite les droits administrateur : contrairement à xattr/codesign,
        `spctl --add` modifie une base système partagée. Demandés via
        `osascript ... with administrator privileges`, qui déclenche
        l'invite mot de passe/Touch ID native de macOS — la mise à jour n'est
        donc plus silencieuse à cette étape précise, uniquement atteinte
        après l'échec des deux méthodes silencieuses. Un mot de passe refusé
        ou une invite annulée (erreur AppleScript -128) n'est pas une panne :
        on redescend simplement sur le message d'échec déjà en place.

        Pas de certitude que ceci fonctionne sur toutes les versions de
        macOS : Apple a resserré `spctl --add` ces dernières années
        précisément pour empêcher ce genre d'auto-approbation par un
        logiciel — à vérifier en usage réel plutôt qu'à supposer.
        """
        try:
            commande = 'spctl --add --label ParcInfo ' + shlex.quote(str(bundle))
            script = 'do shell script %s with administrator privileges' % (
                json.dumps(commande),)
            r = subprocess.run(['osascript', '-e', script],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                detail = (r.stderr or '').strip()
                if '-128' in detail:
                    logger.info("spctl --add annulé par l'utilisateur (invite mot de passe refusée)")
                else:
                    logger.warning("spctl --add a échoué (code %s) : %s",
                                   r.returncode, detail[:300])
                return False
        except FileNotFoundError:
            logger.debug("osascript indisponible sur ce Mac")
            return False
        except Exception as e:
            logger.warning("spctl --add a échoué : %s", e)
            return False
        return UpdateChecker._gatekeeper_accepte(bundle)

    @staticmethod
    def _debloquer_gatekeeper_macos(bundle: Path) -> None:
        """Lève le blocage Gatekeeper sur un bundle non signé, en vérifiant
        à chaque étape avec `spctl --assess` plutôt qu'en supposant qu'une
        approche fonctionne partout.

        1. `com.apple.quarantine` : posé sur ce que télécharge un navigateur,
           pas par `urllib` — donc pas censé être présent ici puisque ParcInfo
           télécharge lui-même l'archive. Retiré quand même, sans coût, pour
           couvrir un mécanisme de tagging qu'on n'aurait pas anticipé. La
           vérification `spctl` qui suit est répétée quelques secondes,
           plutôt qu'une seule fois immédiatement : la réparation manuelle
           qui fonctionne sur certains Mac (xattr -cr, rien d'autre) laisse
           naturellement passer quelques secondes entre la commande et le
           lancement, le temps que syspolicyd prenne en compte le retrait de
           la quarantaine — une vérification scriptée, elle, enchaînait tout
           en quelques millisecondes.
        2. Signature ad hoc (`codesign --sign -`), SEULEMENT si `spctl`
           rejette encore le bundle après le retrait de la quarantaine et son
           délai de grâce.
        3. `spctl --add` (voir _autoriser_via_spctl_add), SEULEMENT si les
           deux premières étapes échouent encore — demande le mot de passe
           administrateur, dernier recours avant le message d'échec manuel.

        Le point 2 a changé de logique après un diagnostic en usage réel (Mac
        Intel, `spctl -a -vv` + `codesign -dv --verbose=4` + `xattr -l`) :
        - `xattr -l` vide : la quarantaine était déjà correctement levée ;
        - la signature ad hoc posée par cette fonction était pourtant
          parfaitement valide (`codesign -dv` ne montrait aucune corruption,
          contrairement à l'hypothèse de la 2.17.1 sur `--deep`) ;
        - et `spctl -a` REJETAIT quand même ce bundle signé ad hoc.
        Sur ce Mac, la réparation manuelle qui fonctionne (retélécharger,
        remplacer, `xattr -cr` — sans jamais appeler `codesign`) laisse le
        bundle SANS AUCUNE signature. C'est cette absence de signature qui
        passait l'évaluation de Gatekeeper, pas une signature ad hoc : sur
        cette version de macOS, apposer une identité ad hoc sans Team ID ni
        notarisation durcit l'évaluation au lieu de l'assouplir — Gatekeeper
        se met à juger une identité qu'il ne peut pas faire confiance, là où
        un bundle nu n'était tout simplement pas évalué de la même façon.

        La 2.16.1 (avant ce changement) rapportait l'inverse : un bundle NON
        signé restait bloqué même quarantaine levée. Les deux observations
        peuvent être vraies sur des versions de macOS différentes — d'où la
        vérification systématique avec `spctl` plutôt qu'un choix figé : la
        signature ad hoc n'est tentée que si la quarantaine seule ne suffit
        pas, jamais par défaut.

        Codesign nécessite les outils en ligne de commande Xcode — absents
        sur certains Mac ; échec silencieux dans ce cas (log seulement), pas
        d'écran bloquant pour l'utilisateur. Si le blocage persiste malgré
        les trois étapes, `_install_macos` le détecte à la vérification de
        démarrage et n'arrête pas l'instance actuelle pour autant.
        """
        try:
            r = subprocess.run(['xattr', '-cr', str(bundle)],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                logger.warning("xattr -cr a échoué (code %s) : %s",
                               r.returncode, (r.stderr or '').strip()[:300])
        except Exception as e:
            logger.warning("xattr -cr a échoué : %s", e)

        # Plusieurs vérifications espacées, pas une seule immédiate : signalé
        # en usage réel, la réparation manuelle qui fonctionne (xattr -cr,
        # rien d'autre) laisse naturellement passer quelques secondes entre
        # la commande tapée dans un Terminal et le lancement de l'app — ce
        # script, lui, enchaînait la vérification en quelques millisecondes.
        # Si syspolicyd (le service que `spctl` interroge) met un instant à
        # prendre en compte le retrait de la quarantaine, une vérification
        # trop hâtive pouvait lire un rejet qui se serait résolu tout seul.
        for _ in range(5):
            if UpdateChecker._gatekeeper_accepte(bundle):
                return
            time.sleep(1)

        try:
            r = subprocess.run(['codesign', '--force', '--sign', '-', str(bundle)],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                logger.warning("codesign ad hoc a échoué (code %s) : %s",
                               r.returncode, (r.stderr or '').strip()[:300])
        except FileNotFoundError:
            logger.debug("codesign indisponible sur ce Mac (outils en ligne "
                         "de commande Xcode absents ?)")
        except Exception as e:
            logger.warning("codesign ad hoc a échoué : %s", e)

        if UpdateChecker._gatekeeper_accepte(bundle):
            return

        if UpdateChecker._autoriser_via_spctl_add(bundle):
            return

        logger.warning(
            "Gatekeeper rejette encore %s après xattr -cr, signature ad hoc "
            "et spctl --add — seule une approbation manuelle (Réglages "
            "Système > Confidentialité et sécurité > Ouvrir quand même) "
            "débloque ce cas, macOS ne permet pas de l'automatiser plus "
            "loin.", bundle)

    def _macos_app_path(self) -> Optional[Path]:
        """Chemin du bundle .app en cours d'exécution, sinon /Applications."""
        for parent in Path(sys.executable).parents:
            if parent.suffix == '.app':
                return parent
        defaut = Path('/Applications/ParcInfo.app')
        return defaut if defaut.exists() else None

    # ─────────────────────────────────────────────────────────────────────────
    # Empreintes
    # ─────────────────────────────────────────────────────────────────────────

    def _expected_sha256(self, version: str, nom_fichier: str,
                         metadata: Dict, platform_key: str) -> Optional[str]:
        """
        Empreinte attendue, cherchée d'abord dans SHA256SUMS.txt publié avec la
        version, puis dans version.json en repli.
        """
        url = SHA256SUMS_URL.format(version=version.lstrip('v'))
        try:
            with urlopen(url, timeout=30) as reponse:
                contenu = reponse.read().decode('utf-8', errors='replace')
            for ligne in contenu.splitlines():
                parts = ligne.split()
                if len(parts) >= 2 and parts[-1].lstrip('*') == nom_fichier:
                    return parts[0]
            logger.warning("%s absent de SHA256SUMS.txt", nom_fichier)
        except Exception as e:
            logger.warning("SHA256SUMS.txt indisponible (%s) : %s", url, e)

        # Repli : version.json. Les clés y sont suffixées « _sha256 » — le code
        # précédent interrogeait la clé nue, ne trouvait rien, et sautait la
        # vérification en silence.
        checksums = metadata.get("checksums") or {}
        for cle in ('%s_sha256' % platform_key, platform_key):
            valeur = checksums.get(cle)
            if valeur and valeur != 'PENDING_BUILD':
                return valeur.split(':')[-1]
        return None

    def _calculate_checksum(self, file_path: Path) -> str:
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    # ─────────────────────────────────────────────────────────────────────────
    # Interne
    # ─────────────────────────────────────────────────────────────────────────

    def _should_check(self) -> bool:
        if not self.metadata_file.exists():
            return True
        try:
            metadata = self._load_metadata()
            last_check = metadata.get("last_check")
            if last_check:
                ecoule = datetime.now() - datetime.fromisoformat(last_check)
                if ecoule < timedelta(hours=self.CHECK_INTERVAL_HOURS):
                    return False
        except Exception:
            pass
        return True

    def _update_last_check_time(self) -> None:
        metadata = self._load_metadata() or {}
        metadata["last_check"] = datetime.now().isoformat()
        self._save_metadata(metadata)

    def _fetch_version_metadata(self) -> Dict:
        try:
            logger.debug("Lecture des métadonnées : %s", self.version_json_url)
            with urlopen(self.version_json_url, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except URLError as e:
            raise UpdateCheckError("Erreur réseau : %s" % e) from e
        except json.JSONDecodeError as e:
            raise UpdateCheckError("Métadonnées illisibles : %s" % e) from e

    def _load_metadata(self) -> Optional[Dict]:
        if not self.metadata_file.exists():
            return None
        try:
            with open(self.metadata_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug("Métadonnées locales illisibles : %s", e)
            return None

    def _save_metadata(self, metadata: Dict) -> None:
        try:
            with open(self.metadata_file, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False)
        except Exception as e:
            logger.debug("Écriture des métadonnées impossible : %s", e)

    def _is_newer_version(self, new: str, old: str) -> bool:
        """Compare deux versions sémantiques."""
        try:
            new_parts = [int(x) for x in str(new).lstrip('v').split(".")]
            old_parts = [int(x) for x in str(old).lstrip('v').split(".")]
            while len(new_parts) < len(old_parts):
                new_parts.append(0)
            while len(old_parts) < len(new_parts):
                old_parts.append(0)
            return tuple(new_parts) > tuple(old_parts)
        except (ValueError, AttributeError):
            return False

    def _get_platform_key(self) -> str:
        """Clé de la section « downloads » de version.json."""
        mode = runtime_mode()
        if mode == 'macos':
            # Deux binaires macOS distincts depuis la 2.15.1 (ARM / Intel) —
            # jusqu'ici toujours 'macos_app' (ARM) était renvoyé, quelle que
            # soit l'architecture réelle : un Mac Intel se voyait proposer le
            # binaire ARM, que macOS refuse ensuite d'ouvrir (« n'est pas pris
            # en charge par ce Mac »). archi_materielle_macos() interroge le
            # matériel réel (hw.optional.arm64), pas l'archi du processus en
            # cours : un exécutable Intel tournant sous Rosetta sur un Mac
            # Apple Silicon bascule maintenant vers le binaire ARM natif au
            # prochain cycle, au lieu de reconfirmer indéfiniment l'Intel.
            return 'macos_app_intel' if archi_materielle_macos() == 'x86_64' else 'macos_app'
        cles = {'windows': 'windows_installer', 'docker': 'docker'}
        if mode in cles:
            return cles[mode]
        raise UpdateCheckError("Mise à jour automatique non prise en charge (%s)" % mode)

    #: Nombre de tentatives. Chacune reprend là où la précédente s'est arrêtée.
    TENTATIVES_TELECHARGEMENT = 4
    #: En dessous de ce débit soutenu, la tentative est abandonnée et relancée
    #: sur une connexion neuve. Sans ce garde-fou, une connexion qui traîne à
    #: quelques kilo-octets par seconde bloque des heures : le délai réseau ne
    #: se déclenche que s'il n'arrive PLUS RIEN, jamais si les données arrivent
    #: trop lentement.
    DEBIT_MINIMAL_KO_S = 20
    #: Durée pendant laquelle le débit doit rester sous le seuil pour conclure.
    FENETRE_STAGNATION_S = 45

    def _download_file(self, url: str, destination: Path,
                       progress: Optional[Callable] = None) -> None:
        temporaire = destination.with_suffix(destination.suffix + '.part')
        journal = self.config_dir / '_telechargement.log'

        def tracer(message):
            """Trace le déroulé : sans elle, une lenteur ne laisse rien à examiner."""
            ligne = '%s %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), message)
            logger.info('Téléchargement : %s', message)
            try:
                with open(journal, 'a', encoding='utf-8') as f:
                    f.write(ligne + '\n')
            except OSError:
                pass

        proxys = urllib.request.getproxies()
        tracer('début %s (proxy : %s)' % (url, proxys or 'aucun'))

        derniere_erreur = None
        for tentative in range(1, self.TENTATIVES_TELECHARGEMENT + 1):
            # Reprise : le fichier partiel est conservé d'une tentative à
            # l'autre. Il était effacé à chaque échec, ce qui repartait de zéro
            # et rendait un réseau capricieux définitivement bloquant.
            deja = temporaire.stat().st_size if temporaire.exists() else 0
            entetes = {'User-Agent': 'ParcInfo/%s' % self.current_version}
            if deja:
                entetes['Range'] = 'bytes=%d-' % deja

            try:
                requete = Request(url, headers=entetes)
                with urlopen(requete, timeout=60) as reponse:
                    reprise = reponse.status == 206
                    if deja and not reprise:
                        # Le serveur ignore la reprise : on repart proprement.
                        deja = 0
                    restant = int(reponse.headers.get('Content-Length') or 0)
                    total = deja + restant
                    recu = deja
                    debut = time.time()
                    dernier_rapport = 0
                    repere_temps, repere_octets = debut, recu

                    with open(temporaire, 'ab' if deja else 'wb') as f:
                        while True:
                            bloc = reponse.read(256 * 1024)
                            if not bloc:
                                break
                            f.write(bloc)
                            recu += len(bloc)

                            maintenant = time.time()
                            if total and recu - dernier_rapport >= 512 * 1024:
                                dernier_rapport = recu
                                pct = int(recu * 100 / total)
                                self.download_progress = pct
                                ecoule = max(maintenant - debut, 0.001)
                                self.download_rate_kbs = round(
                                    (recu - deja) / 1024 / ecoule, 1)
                                if progress:
                                    progress(pct)

                            # Débit soutenu trop faible : on coupe et on reprend.
                            if maintenant - repere_temps >= self.FENETRE_STAGNATION_S:
                                debit = ((recu - repere_octets) / 1024
                                         / (maintenant - repere_temps))
                                if debit < self.DEBIT_MINIMAL_KO_S:
                                    raise UpdateCheckError(
                                        'débit trop faible (%.1f Ko/s)' % debit)
                                repere_temps, repere_octets = maintenant, recu

                duree = max(time.time() - debut, 0.001)
                tracer('tentative %d : %d octets reçus en %.0f s (%.0f Ko/s), total %d'
                       % (tentative, recu - deja, duree, (recu - deja) / 1024 / duree, recu))

                if total and temporaire.stat().st_size < total:
                    raise UpdateCheckError('fichier incomplet (%d/%d octets)'
                                           % (temporaire.stat().st_size, total))

                # Le fichier ne prend son nom définitif qu'une fois complet : un
                # téléchargement interrompu ne doit pas ressembler à un binaire prêt.
                temporaire.replace(destination)
                tracer('terminé : %s' % destination.name)
                return

            except Exception as e:
                derniere_erreur = e
                acquis = temporaire.stat().st_size if temporaire.exists() else 0
                tracer('tentative %d interrompue à %d octets : %s' % (tentative, acquis, e))
                if tentative < self.TENTATIVES_TELECHARGEMENT:
                    time.sleep(3)

        # Le fichier partiel est laissé en place : la prochaine demande de mise
        # à jour reprendra là où celle-ci s'est arrêtée.
        raise UpdateCheckError(
            "Téléchargement échoué après %d tentatives : %s. Détail dans %s"
            % (self.TENTATIVES_TELECHARGEMENT, derniere_erreur, journal))


def docker_pull_commands(version: Optional[str] = None) -> Dict[str, str]:
    """
    Commandes de mise à jour d'un conteneur, selon la façon dont il a été lancé.
    Un conteneur ne peut pas se remplacer lui-même : c'est à l'hôte d'agir.
    """
    tag = ('v%s' % str(version).lstrip('v')) if version else 'latest'
    return {
        'compose': "docker compose pull && docker compose up -d",
        'run': "docker pull darkmind64/parcinfo:%s" % tag,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    checker = UpdateChecker()
    print("Mode d'exécution :", runtime_mode())
    if checker.check_for_updates(force=True):
        print("Mise à jour disponible :", checker.latest_version)
    else:
        print("Déjà à jour :", checker.current_version)
