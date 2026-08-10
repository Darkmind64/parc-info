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

import hashlib
import json
import logging
import os
import platform
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

        destination = self.config_dir / nom_fichier
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
        nouvelle application. Windows verrouille l'exe en cours d'exécution, donc
        le remplacement est délégué à un script détaché qui attend la fin de ce
        processus, échange les fichiers puis relance l'application.
        """
        current_exe = Path(sys.executable)
        sauvegarde = current_exe.with_suffix('.exe.old')
        bat_path = self.config_dir / "_apply_update.bat"

        journal = self.config_dir / "_apply_update.log"

        # Le script réessaie au lieu d'attendre une durée fixe : tant que
        # l'application n'a pas rendu la main, Windows garde le verrou sur son
        # exécutable et le déplacement échoue. Une attente figée ne pardonnait
        # rien — un arrêt un peu lent et la mise à jour était perdue en silence.
        # L'ancien exécutable est conservé le temps du remplacement, et remis en
        # place si la copie échoue à mi-chemin : mieux vaut l'ancienne version
        # qu'aucune application.
        bat_content = (
            "@echo off\r\n"
            f'echo [%DATE% %TIME%] remplacement demande > "{journal}"\r\n'
            "timeout /t 3 /nobreak > NUL\r\n"
            f'if exist "{sauvegarde}" del /q "{sauvegarde}"\r\n'
            "set TENTATIVE=0\r\n"
            ":essai\r\n"
            "set /a TENTATIVE+=1\r\n"
            f'move /y "{current_exe}" "{sauvegarde}" > NUL 2>&1\r\n'
            "if not errorlevel 1 goto deplace\r\n"
            "if %TENTATIVE% GEQ 15 goto abandon\r\n"
            f'echo [%TIME%] fichier encore verrouille, tentative %TENTATIVE% >> "{journal}"\r\n'
            "timeout /t 2 /nobreak > NUL\r\n"
            "goto essai\r\n"
            ":deplace\r\n"
            f'move /y "{installer_path}" "{current_exe}" > NUL 2>&1\r\n'
            "if errorlevel 1 (\r\n"
            f'  echo [%TIME%] copie impossible, retour a la version precedente >> "{journal}"\r\n'
            f'  move /y "{sauvegarde}" "{current_exe}" > NUL 2>&1\r\n'
            "  goto relance\r\n"
            ")\r\n"
            f'echo [%TIME%] remplacement effectue >> "{journal}"\r\n'
            f'del /q "{sauvegarde}" > NUL 2>&1\r\n'
            "goto relance\r\n"
            ":abandon\r\n"
            f'echo [%TIME%] abandon : executable toujours verrouille >> "{journal}"\r\n'
            ":relance\r\n"
            f'start "" "{current_exe}"\r\n'
            'del "%~f0"\r\n'
        )
        # Encodage OEM : un .bat lu par cmd.exe n'est pas en UTF-8, et un chemin
        # accentué (« C:\\Users\\Éric\\… ») y deviendrait illisible.
        bat_path.write_text(bat_content, encoding='cp1252', errors='replace')

        creationflags = 0
        if hasattr(subprocess, 'CREATE_NO_WINDOW') and hasattr(subprocess, 'DETACHED_PROCESS'):
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        subprocess.Popen(['cmd', '/c', str(bat_path)],
                         creationflags=creationflags, close_fds=True)

        logger.info("Remplacement programmé — l'application va redémarrer")
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

        # L'archive téléchargée est dépourvue d'attribut de quarantaine tant
        # qu'elle vient de nous, mais macOS en pose un dès le téléchargement :
        # sans ce nettoyage, Gatekeeper refuse d'ouvrir l'application.
        subprocess.run(['xattr', '-cr', str(app_actuelle)], check=False)
        shutil.rmtree(sauvegarde, ignore_errors=True)

        # Relance différée : le processus courant doit d'abord rendre la main.
        subprocess.Popen(['/bin/sh', '-c',
                          'sleep 3; open "%s"' % app_actuelle],
                         start_new_session=True)
        logger.info("Application macOS remplacée — redémarrage en cours")
        return True

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
        cles = {'windows': 'windows_installer', 'macos': 'macos_app', 'docker': 'docker'}
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
