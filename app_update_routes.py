"""
Update Notification Routes for ParcInfo Flask App

Add these routes to app.py to enable update notifications in the web interface.

Usage (in app.py):
    from app_update_routes import register_update_routes
    register_update_routes(app)
"""

from flask import jsonify
from update_notifier import get_notifier


def register_update_routes(app):
    """
    Register update notification routes to Flask app.

    Routes:
    - GET /api/updates/status — Get current update status
    - POST /api/updates/check — Check for updates now
    - POST /api/updates/install — Install update now
    - POST /api/updates/dismiss — Dismiss notification
    """

    @app.route('/api/updates/status', methods=['GET'])
    def get_update_status():
        """Get current update status and notification."""
        notifier = get_notifier()
        return jsonify(notifier.status)

    @app.route('/api/updates/check', methods=['POST'])
    def check_updates():
        """Check for updates immediately."""
        notifier = get_notifier()

        if notifier.is_checking:
            return jsonify({
                "status": "already_checking",
                "message": "Update check already in progress"
            }), 202

        has_update = notifier.check_now()

        return jsonify({
            "status": "check_complete",
            "update_available": has_update,
            "version": notifier.update_version,
            "notification": notifier.get_notification()
        })

    @app.route('/api/updates/install', methods=['POST'])
    def install_update():
        """Install update now."""
        notifier = get_notifier()

        if not notifier.update_available:
            return jsonify({
                "status": "error",
                "message": "No update available"
            }), 400

        if notifier.install_update():
            return jsonify({
                "status": "installing",
                "message": "Update installation started",
                "version": notifier.update_version,
                "notification": notifier.get_notification()
            }), 202
        else:
            return jsonify({
                "status": "error",
                "message": "Failed to start installation"
            }), 500

    @app.route('/api/updates/dismiss', methods=['POST'])
    def dismiss_notification():
        """Dismiss update notification."""
        notifier = get_notifier()
        notifier.dismiss_notification()

        return jsonify({
            "status": "dismissed",
            "notification": None
        })

    # Initialize notifier on first request
    @app.before_request
    def init_notifier():
        get_notifier()
