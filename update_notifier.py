"""
Update Notification Manager for ParcInfo Web Interface

Manages update notifications displayed to users in the Flask app.
- Periodic background checks
- In-app alert notifications
- Update status tracking

Usage:
    from update_notifier import UpdateNotifier
    notifier = UpdateNotifier()
    notifier.start()

    # In Flask routes:
    notifier.get_notification()  # Returns current notification or None
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Dict

from update_checker import UpdateChecker

logger = logging.getLogger("parcinfo.notifier")


class UpdateNotification:
    """Notification object for update status."""

    def __init__(self, notification_type: str, message: str, version: str = None,
                 action_url: str = None):
        """
        Initialize notification.

        Args:
            notification_type: 'update_available' | 'installing' | 'update_complete'
            message: Message to display to user
            version: Version number
            action_url: URL for action button (e.g., to install)
        """
        self.type = notification_type
        self.message = message
        self.version = version
        self.action_url = action_url
        self.created_at = datetime.now()
        self.dismissed = False

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.type,
            "message": self.message,
            "version": self.version,
            "action_url": self.action_url,
            "created_at": self.created_at.isoformat(),
            "dismissed": self.dismissed,
        }

    def is_expired(self, max_age_seconds: int = 3600) -> bool:
        """Check if notification is older than max age."""
        return (datetime.now() - self.created_at).total_seconds() > max_age_seconds


class UpdateNotifier:
    """
    Manages update notifications for web interface.

    Features:
    - Periodic background update checks
    - In-app notification tracking
    - User dismissal support
    """

    CHECK_INTERVAL_SECONDS = 3600  # Check every hour

    def __init__(self, config_dir: Optional[str] = None):
        """
        Initialize notifier.

        Args:
            config_dir: Configuration directory for UpdateChecker
        """
        self.checker = UpdateChecker(config_dir=config_dir)
        self.current_notification: Optional[UpdateNotification] = None
        self.update_available = False
        self.update_version = None
        self.is_checking = False
        self.thread = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start background update check thread."""
        if self.thread and self.thread.is_alive():
            logger.debug("Update notifier already running")
            return

        self.thread = threading.Thread(target=self._background_check, daemon=True)
        self.thread.start()
        logger.info("Update notifier started")

    def stop(self) -> None:
        """Stop background update check thread."""
        self._stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Update notifier stopped")

    def check_now(self) -> bool:
        """
        Check for updates immediately.

        Returns:
            True if update available
        """
        logger.info("Checking for updates (on-demand)...")
        self.is_checking = True

        try:
            if self.checker.check_for_updates(force=True):
                self.update_available = True
                self.update_version = self.checker.latest_version
                self.current_notification = UpdateNotification(
                    notification_type="update_available",
                    message=f"ParcInfo {self.checker.latest_version} is available",
                    version=self.checker.latest_version,
                    action_url="/api/check-updates"
                )
                logger.info(f"✓ Update available: {self.update_version}")
                return True
            else:
                self.update_available = False
                logger.info("✓ Already on latest version")
                return False
        except Exception as e:
            logger.warning(f"On-demand check failed: {e}")
            return False
        finally:
            self.is_checking = False

    def install_update(self) -> bool:
        """
        Install update immediately.

        Returns:
            True if installation started
        """
        if not self.update_available:
            logger.warning("No update available to install")
            return False

        logger.info(f"Starting update to {self.update_version}...")
        self.current_notification = UpdateNotification(
            notification_type="installing",
            message="Updating ParcInfo... Application will restart.",
            version=self.update_version
        )

        def install_in_background():
            try:
                if self.checker.check_and_install_updates(force=True, silent=True):
                    logger.info("✓ Update installed")
                    self.current_notification = UpdateNotification(
                        notification_type="update_complete",
                        message="Update complete! Application will restart.",
                        version=self.update_version
                    )
                else:
                    logger.error("Update installation failed")
                    self.current_notification = UpdateNotification(
                        notification_type="update_available",
                        message="Update failed. Please try again.",
                        version=self.update_version
                    )
            except Exception as e:
                logger.error(f"Installation error: {e}")
                self.current_notification = UpdateNotification(
                    notification_type="update_available",
                    message=f"Update failed: {e}",
                    version=self.update_version
                )

        # Run installation in background thread
        install_thread = threading.Thread(target=install_in_background, daemon=True)
        install_thread.start()

        return True

    def get_notification(self) -> Optional[Dict]:
        """
        Get current notification for web interface.

        Returns:
            Notification dict or None if none available
        """
        if self.current_notification:
            # Remove expired notifications
            if self.current_notification.is_expired(max_age_seconds=86400):  # 24 hours
                self.current_notification = None
                return None

            # Don't return dismissed notifications
            if self.current_notification.dismissed:
                return None

            return self.current_notification.to_dict()

        return None

    def dismiss_notification(self) -> None:
        """Dismiss current notification."""
        if self.current_notification:
            self.current_notification.dismissed = True
            logger.debug("Notification dismissed by user")

    def _background_check(self) -> None:
        """Background update check loop."""
        logger.info(f"Background update check started (interval: {self.CHECK_INTERVAL_SECONDS}s)")

        while not self._stop_event.is_set():
            try:
                # Check every hour
                if self._stop_event.wait(timeout=self.CHECK_INTERVAL_SECONDS):
                    break  # Stop event was set

                logger.debug("Running periodic update check...")

                if self.checker.check_for_updates(force=False):
                    self.update_available = True
                    self.update_version = self.checker.latest_version

                    # Only notify if we don't already have a notification
                    if not self.current_notification or self.current_notification.dismissed:
                        self.current_notification = UpdateNotification(
                            notification_type="update_available",
                            message=f"ParcInfo {self.checker.latest_version} is available",
                            version=self.checker.latest_version
                        )
                        logger.info(f"✓ Update available: {self.update_version}")

            except Exception as e:
                logger.debug(f"Background check error: {e}")

    @property
    def status(self) -> Dict:
        """Get current update status."""
        return {
            "checking": self.is_checking,
            "update_available": self.update_available,
            "version": self.update_version,
            "current_version": self.checker.current_version,
            "notification": self.get_notification(),
        }


# Global notifier instance
_notifier_instance: Optional[UpdateNotifier] = None


def get_notifier(config_dir: Optional[str] = None) -> UpdateNotifier:
    """Get or create global notifier instance."""
    global _notifier_instance

    if _notifier_instance is None:
        _notifier_instance = UpdateNotifier(config_dir=config_dir)
        _notifier_instance.start()

    return _notifier_instance
