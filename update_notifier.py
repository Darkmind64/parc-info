"""
État des mises à jour tel que l'interface web le voit.

Un seul objet partagé par l'application : il détecte les versions en tâche de
fond, conserve l'avancement d'une installation en cours, et se souvient de la
version que l'utilisateur a écartée — écarter 2.6.42 ne doit pas faire taire
l'annonce de 2.6.43.

Usage :
    from update_notifier import get_notifier
    get_notifier().etat
"""

import json
import logging
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

from update_checker import (UpdateChecker, UpdateCheckError, runtime_mode,
                            can_self_install, docker_pull_commands)

logger = logging.getLogger("parcinfo.notifier")


class UpdateNotifier:
    """Suit la disponibilité d'une mise à jour et son installation."""

    # Vérification de fond. La borne réelle est celle d'UpdateChecker
    # (CHECK_INTERVAL_HOURS) : ce réveil ne fait que la solliciter.
    INTERVALLE_SECONDES = 3600

    def __init__(self, config_dir: Optional[str] = None):
        self.checker = UpdateChecker(config_dir=config_dir)
        self.mode = runtime_mode()
        self.installable = can_self_install()

        self.phase = 'inactif'      # inactif | verification | telechargement
                                    # | installation | pret | erreur
        self.progression = 0
        self.message = None
        self.erreur = None
        self.derniere_verification = None

        self._etat_file = Path(self.checker.config_dir) / 'update_state.json'
        enregistre = self._charger_etat()
        self.version_ecartee = enregistre.get('version_ecartee')

        # Une version différente de celle vue au démarrage précédent signifie
        # qu'une mise à jour a bien été appliquée. Sans cette trace, le
        # remplacement se faisait dans le dos de l'utilisateur : l'application
        # redémarrait et rien n'indiquait ce qui avait changé.
        vue = enregistre.get('version_vue')
        self.version_installee = None
        if vue and vue != self.checker.current_version and \
                self.checker._is_newer_version(self.checker.current_version, vue):
            self.version_installee = self.checker.current_version
            logger.info("Démarrage sur la version %s (précédente : %s)",
                        self.checker.current_version, vue)
            self._journaliser(vue, self.checker.current_version)
        if vue != self.checker.current_version:
            self._enregistrer_etat()

        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    def _journaliser(self, avant: str, apres: str) -> None:
        """Consigne la mise à jour dans le journal partagé entre instances."""
        try:
            from database import log_maj_event
            log_maj_event(machine=socket.gethostname(),
                          version_avant=avant, version_apres=apres,
                          mode=self.mode, statut='succes')
        except Exception as e:
            # Le journal est un confort : son échec ne doit pas empêcher
            # l'application de démarrer sur sa nouvelle version.
            logger.debug("Journal des mises à jour non alimenté : %s", e)

    # ─────────────────────────────────────────────────────────────────────────
    # Cycle de vie
    # ─────────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Démarre la vérification périodique en tâche de fond."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._boucle, daemon=True,
                                        name='VerificationMaj')
        self._thread.start()
        logger.info("Vérification des mises à jour démarrée (mode %s)", self.mode)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _boucle(self) -> None:
        # Première vérification tout de suite : au lancement, l'utilisateur doit
        # savoir dès la première page qu'une version l'attend.
        while not self._stop.is_set():
            try:
                self.verifier(force=False)
            except Exception:
                logger.debug("Vérification de fond en échec", exc_info=True)
            if self._stop.wait(timeout=self.INTERVALLE_SECONDES):
                break

    # ─────────────────────────────────────────────────────────────────────────
    # Actions
    # ─────────────────────────────────────────────────────────────────────────

    def verifier(self, force: bool = True) -> bool:
        """Interroge le dépôt. Retourne True si une mise à jour est disponible."""
        if self.phase in ('telechargement', 'installation'):
            return self.checker.available_update

        self.phase = 'verification'
        try:
            disponible = self.checker.check_for_updates(force=force)
            self.derniere_verification = datetime.now().isoformat()
            self.erreur = None
            self.phase = 'inactif'
            return disponible
        except Exception as e:
            self.erreur = str(e)
            self.phase = 'erreur'
            logger.warning("Vérification impossible : %s", e)
            return False

    def installer(self) -> bool:
        """
        Lance téléchargement puis remplacement, en tâche de fond.
        Retourne True si l'opération a bien démarré.
        """
        if not self.checker.available_update:
            self.erreur = "Aucune mise à jour disponible"
            return False
        if not self.installable:
            self.erreur = ("L'installation automatique n'est pas possible dans ce "
                           "mode d'exécution (%s)" % self.mode)
            return False

        with self._lock:
            if self.phase in ('telechargement', 'installation'):
                return True
            self.phase = 'telechargement'
            self.progression = 0
            self.erreur = None
            self.message = "Téléchargement de la version %s" % self.checker.latest_version

        def travail():
            try:
                fichier = self.checker.download_update(
                    progress=lambda p: setattr(self, 'progression', p))
                if not fichier:
                    raise UpdateCheckError("Téléchargement sans résultat")

                self.phase = 'installation'
                self.progression = 100
                self.message = "Installation de la version %s" % self.checker.latest_version

                if self.checker.install_update(fichier):
                    self.phase = 'pret'
                    self.message = ("Version %s installée — l'application redémarre"
                                    % self.checker.latest_version)
                    logger.info("Mise à jour appliquée : %s", self.checker.latest_version)
                    # Sans cette sortie, rien n'aboutit : Windows verrouille
                    # l'exécutable en cours, le script de remplacement échoue à
                    # déplacer le fichier, et l'application continue de tourner
                    # sur l'ancienne version comme si de rien n'était.
                    self._arreter_pour_redemarrage()
                else:
                    raise UpdateCheckError("Le remplacement de l'application a échoué")
            except Exception as e:
                self.phase = 'erreur'
                self.erreur = str(e)
                self.message = None
                logger.error("Mise à jour interrompue : %s", e)
                # Un échec vaut d'être consigné autant qu'une réussite : c'est
                # la seule trace exploitable depuis un autre poste.
                try:
                    from database import log_maj_event
                    log_maj_event(machine=socket.gethostname(),
                                  version_avant=self.checker.current_version,
                                  version_apres=self.checker.latest_version or '',
                                  mode=self.mode, statut='echec', detail=str(e)[:400])
                except Exception:
                    pass

        threading.Thread(target=travail, daemon=True, name='InstallationMaj').start()
        return True

    #: Délai avant l'arrêt, une fois le remplacement programmé. Laisse à la
    #: bannière le temps d'afficher la confirmation et au navigateur celui de
    #: recevoir la dernière réponse. Le script de remplacement, lui, attend plus
    #: longtemps que ce délai avant de toucher au fichier.
    DELAI_ARRET_SECONDES = 2

    def _arreter_pour_redemarrage(self) -> None:
        """Rend la main sur l'exécutable pour que le remplacement puisse avoir lieu.

        os._exit et non sys.exit : cette méthode tourne dans un fil, où sys.exit
        ne terminerait que ce fil et laisserait l'application — donc le verrou
        sur le fichier — bien vivante.
        """
        def arret():
            time.sleep(self.DELAI_ARRET_SECONDES)
            logger.info("Arrêt pour laisser le remplacement s'effectuer")
            os._exit(0)

        threading.Thread(target=arret, daemon=True, name='ArretMaj').start()

    def ecarter(self) -> None:
        """Masque l'annonce en cours : disponibilité, ou confirmation d'installation."""
        if self.version_installee:
            self.version_installee = None
            return
        if self.checker.latest_version:
            self.version_ecartee = self.checker.latest_version
            self._enregistrer_etat()

    def reafficher(self) -> None:
        """Annule l'écartement : l'utilisateur redemande à voir l'annonce."""
        if self.version_ecartee:
            self.version_ecartee = None
            self._enregistrer_etat()

    # ─────────────────────────────────────────────────────────────────────────
    # État exposé à l'interface
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def etat(self) -> Dict:
        maj = self.checker.available_update
        version = self.checker.latest_version
        # Une version écartée reste masquée tant qu'aucune plus récente n'arrive.
        masquee = bool(maj and version and version == self.version_ecartee
                       and self.phase == 'inactif')

        etat = {
            'version_actuelle': self.checker.current_version,
            'version_disponible': version if maj else None,
            'mise_a_jour_disponible': bool(maj),
            'masquee': masquee,
            'mode': self.mode,
            'installable': self.installable,
            'phase': self.phase,
            'progression': self.progression,
            # Débit du téléchargement en cours : une lenteur devient
            # visible au lieu d'être seulement ressentie.
            'debit_ko_s': self.checker.download_rate_kbs,
            'message': self.message,
            'erreur': self.erreur,
            'derniere_verification': self.derniere_verification,
            'notes': (self.checker.release_notes or [])[:6],
            'url_notes': self.checker.release_url,
            # Renseigné au premier démarrage suivant une mise à jour réussie.
            'version_installee': self.version_installee,
        }
        if self.mode == 'docker':
            etat['commandes'] = docker_pull_commands(version)
        return etat

    # ─────────────────────────────────────────────────────────────────────────
    # Persistance
    # ─────────────────────────────────────────────────────────────────────────

    def _charger_etat(self) -> Dict:
        try:
            if self._etat_file.exists():
                with open(self._etat_file, 'r', encoding='utf-8') as f:
                    return json.load(f) or {}
        except Exception:
            pass
        return {}

    def _enregistrer_etat(self) -> None:
        try:
            with open(self._etat_file, 'w', encoding='utf-8') as f:
                json.dump({'version_ecartee': self.version_ecartee,
                           'version_vue': self.checker.current_version},
                          f, ensure_ascii=False)
        except Exception as e:
            logger.debug("État de mise à jour non enregistré : %s", e)


_instance: Optional[UpdateNotifier] = None
_instance_lock = threading.Lock()


def get_notifier(config_dir: Optional[str] = None) -> UpdateNotifier:
    """Instance partagée, créée et démarrée au premier appel."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                if config_dir is None:
                    config_dir = os.environ.get('DATA_DIR') or None
                notifier = UpdateNotifier(config_dir=config_dir)
                notifier.start()
                _instance = notifier
    return _instance
