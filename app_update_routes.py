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

from flask import jsonify

from auth_utils import login_required, get_auth_user
from update_notifier import get_notifier


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
        # Remplacer l'exécutable dépasse ce qu'un compte de consultation doit
        # pouvoir déclencher : l'opération redémarre l'application pour tous.
        user = get_auth_user()
        if not user or user.get('role') != 'admin':
            return jsonify({'erreur': "Réservé aux administrateurs"}), 403

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
