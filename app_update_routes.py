"""
Routes de mise à jour pour l'interface web de ParcInfo.

    from app_update_routes import register_update_routes
    register_update_routes(app)

Routes :
    GET  /api/updates/status   état courant (version, phase, progression)
    POST /api/updates/check    vérification immédiate
    POST /api/updates/install  téléchargement + remplacement (administrateurs)
    POST /api/updates/dismiss  masquer l'annonce de cette version
"""

import logging

from flask import jsonify, request

from auth_utils import login_required, get_auth_user
from update_notifier import get_notifier

logger = logging.getLogger('parcinfo')


def register_update_routes(app):
    """Déclare les routes de mise à jour sur l'application Flask."""

    @app.route('/api/updates/status', methods=['GET'])
    @login_required
    def get_update_status():
        return jsonify(get_notifier().etat)

    @app.route('/api/updates/check', methods=['POST'])
    @login_required
    def check_updates():
        notifier = get_notifier()
        if notifier.phase == 'verification':
            return jsonify(notifier.etat), 202
        notifier.verifier(force=True)
        return jsonify(notifier.etat)

    @app.route('/api/updates/install', methods=['POST'])
    @login_required
    def install_update():
        # Ouvert à tout compte connecté, sur demande explicite : sur un poste
        # de travail, celui qui utilise l'application est rarement celui qui
        # porte le rôle d'administrateur dans ParcInfo, et la réserver aux
        # administrateurs revenait à empêcher les mises à jour.
        # L'auteur est tracé : l'opération redémarre l'application pour tous.
        user = get_auth_user()
        logger.info('Mise à jour demandée par %s (ip=%s)',
                    (user or {}).get('login', '?'), request.remote_addr)

        notifier = get_notifier()
        if not notifier.installer():
            return jsonify({'erreur': notifier.erreur or "Installation impossible",
                            'etat': notifier.etat}), 400
        return jsonify(notifier.etat), 202

    @app.route('/api/updates/dismiss', methods=['POST'])
    @login_required
    def dismiss_notification():
        notifier = get_notifier()
        notifier.ecarter()
        return jsonify(notifier.etat)

    @app.route('/api/updates/undismiss', methods=['POST'])
    @login_required
    def restore_notification():
        """Réaffiche une annonce écartée — déclenché par le clic sur la version."""
        notifier = get_notifier()
        notifier.reafficher()
        return jsonify(notifier.etat)

    # Le suivi démarre à la première page servie plutôt qu'à l'import : les
    # scripts qui importent app.py (tests, outils) n'ont pas à lancer de thread
    # ni à interroger GitHub.
    @app.before_request
    def _demarrer_suivi_maj():
        get_notifier()
        # Rattrapage des fiches déjà collectées, une seule fois par base. Il vit
        # ici parce qu'init_db() s'exécute avant que la fonction ne soit définie.
        try:
            from app import completer_fiches_existantes
            completer_fiches_existantes()
        except Exception:
            pass
        # Rattrapage sync baie_slot_ports (ports/câblage de baie créés avant
        # ce correctif, jamais journalisés) — même raison d'être ici que
        # ci-dessus : init_db() s'exécute avant que la fonction ne soit
        # définie.
        try:
            from app import rattraper_sync_baie_slot_ports
            rattraper_sync_baie_slot_ports()
        except Exception:
            pass
        # Idem pour baie_prises_murales (prises murales issues de la
        # migration piece/appareil/périphérique d'un port de bandeau RJ,
        # voir init_db()) — même raison d'être.
        try:
            from app import rattraper_sync_baie_prises_murales
            rattraper_sync_baie_prises_murales()
        except Exception:
            pass
