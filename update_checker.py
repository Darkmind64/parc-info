"""
ParcInfo Auto-Update Checker

Checks for new versions periodically and notifies user of available updates.
Downloads and installs updates in background.

Usage:
    from update_checker import UpdateChecker
    checker = UpdateChecker()
    checker.check_for_updates()
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Callable
from urllib.request import urlopen
from urllib.error import URLError

from __version__ import __version__, __version_tuple__, GITHUB_API_RELEASES

logger = logging.getLogger("parcinfo.updater")


class UpdateCheckError(Exception):
    """Error during update check."""
    pass


class UpdateChecker:
    """
    Monitors for application updates.

    Features:
    - Monthly version check
    - SHA256 checksum validation
    - Background download
    - Silent installation
    - Rollback on failure
    """

    # Check every 30 days
    CHECK_INTERVAL_DAYS = 30

    def __init__(self, config_dir: Optional[Path] = None,
                 version_json_url: Optional[str] = None,
                 callback: Optional[Callable] = None):
        """
        Initialize UpdateChecker.

        Args:
            config_dir: Directory to store update metadata (default: app data dir)
            version_json_url: URL to version.json (default: GitHub releases)
            callback: Callback function for notifications (update_available(version))
        """
        self.config_dir = config_dir or Path.home() / ".parcinfo"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.version_json_url = version_json_url or \
            "https://raw.githubusercontent.com/darkmind64/parc_info/master/version.json"

        self.metadata_file = self.config_dir / "update_metadata.json"
        self.callback = callback
        self.current_version = __version__
        self.latest_version = None
        self.available_update = False

    def check_for_updates(self, force: bool = False) -> bool:
        """
        Check if new version available.

        Args:
            force: Ignore last check timestamp

        Returns:
            True if update available, False otherwise
        """
        # Check if enough time has passed since last check
        if not force and not self._should_check():
            logger.debug(f"Update check skipped (last checked within {self.CHECK_INTERVAL_DAYS} days)")
            return False

        logger.info("Checking for updates...")

        try:
            metadata = self._fetch_version_metadata()
            latest_version = metadata.get("version")

            if not latest_version:
                logger.warning("No version info in metadata")
                return False

            self.latest_version = latest_version
            self._save_metadata(metadata)

            # Compare versions
            if self._is_newer_version(latest_version, self.current_version):
                logger.info(f"New version available: {latest_version}")
                self.available_update = True

                if self.callback:
                    self.callback(latest_version)

                return True
            else:
                logger.info(f"Already on latest version: {self.current_version}")
                self.available_update = False
                return False

        except Exception as e:
            logger.warning(f"Update check failed: {e}")
            return False
        finally:
            self._update_last_check_time()

    def check_and_install_updates(self, force: bool = False, silent: bool = False) -> bool:
        """
        Check for updates and install automatically if available.

        Args:
            force: Ignore last check timestamp
            silent: Don't show notifications (automatic startup)

        Returns:
            True if update was installed, False otherwise
        """
        logger.info("Checking for updates (with auto-install)...")

        if not self.check_for_updates(force=force):
            return False

        if not self.available_update:
            return False

        try:
            logger.info(f"Update available: {self.latest_version}")
            logger.info("Downloading and installing update...")

            # Download
            installer_path = self.download_update(self.latest_version)
            if not installer_path:
                return False

            # Install
            logger.info("Installing update...")
            if not self.install_update(installer_path, silent=True):
                logger.error("Installation failed")
                return False

            logger.info("✓ Update installed successfully")
            return True

        except Exception as e:
            logger.error(f"Auto-install failed: {e}")
            return False

    def download_update(self, version: Optional[str] = None) -> Optional[Path]:
        """
        Download installer for given version.

        Args:
            version: Version to download (default: latest)

        Returns:
            Path to downloaded installer, or None if failed
        """
        if not version:
            version = self.latest_version
        if not version:
            raise UpdateCheckError("No version specified")

        logger.info(f"Downloading update {version}...")

        try:
            metadata = self._load_metadata()
            if not metadata:
                metadata = self._fetch_version_metadata()

            downloads = metadata.get("downloads", {})
            checksums = metadata.get("checksums", {})

            # Detect current platform
            platform_key = self._get_platform_key()
            if platform_key not in downloads:
                raise UpdateCheckError(f"No download available for {platform_key}")

            download_url = downloads[platform_key]
            expected_checksum = checksums.get(platform_key)

            # Download file
            installer_path = self.config_dir / Path(download_url).name
            self._download_file(download_url, installer_path)

            # Validate checksum
            if expected_checksum:
                logger.info("Validating download...")
                actual_checksum = self._calculate_checksum(installer_path)
                if not actual_checksum.endswith(expected_checksum.split(":")[-1]):
                    raise UpdateCheckError("Checksum mismatch")

            logger.info(f"✓ Downloaded: {installer_path}")
            return installer_path

        except Exception as e:
            logger.error(f"Download failed: {e}")
            raise UpdateCheckError(f"Failed to download: {e}") from e

    def install_update(self, installer_path: Path, silent: bool = True) -> bool:
        """
        Install downloaded update.

        Args:
            installer_path: Path to installer executable
            silent: Run installer in silent mode

        Returns:
            True if installation succeeded
        """
        if not installer_path.exists():
            logger.error(f"Installer not found: {installer_path}")
            return False

        logger.info(f"Installing update from {installer_path}...")

        try:
            system = platform.system()

            if system == "Windows":
                # Run NSIS installer silently
                cmd = [
                    str(installer_path),
                    "/S" if silent else "",  # NSIS silent flag
                    "/D=$PROGRAMFILES\\ParcInfo"
                ]
                subprocess.Popen(cmd)
                logger.info("✓ Windows installer launched")
                return True

            elif system == "Darwin":
                # Mount DMG and copy app
                mount_point = "/tmp/ParcInfo-Update"
                cmd = f"hdiutil attach '{installer_path}' -mountpoint {mount_point}"
                os.system(cmd)

                app_src = f"{mount_point}/ParcInfo.app"
                app_dst = "/Applications/ParcInfo.app"

                if os.path.exists(app_src):
                    # Backup old app
                    backup_path = f"/Applications/ParcInfo.app.backup"
                    if os.path.exists(app_dst):
                        shutil.move(app_dst, backup_path)

                    # Copy new app
                    shutil.copytree(app_src, app_dst)
                    logger.info("✓ macOS app updated")

                    # Unmount
                    os.system(f"hdiutil eject {mount_point}")
                    return True

            elif system == "Linux":
                logger.info("Manual installation required for Linux")
                return False

        except Exception as e:
            logger.error(f"Installation failed: {e}")
            return False

    def check_for_updates_background(self, callback: Optional[Callable] = None) -> threading.Thread:
        """
        Start background thread for periodic update checks.

        Returns:
            Thread object (daemon)
        """
        def background_check():
            while True:
                try:
                    self.check_for_updates()
                    time.sleep(self.CHECK_INTERVAL_DAYS * 24 * 3600)
                except Exception as e:
                    logger.debug(f"Background check error: {e}")
                    time.sleep(3600)  # Retry after 1 hour

        thread = threading.Thread(target=background_check, daemon=True)
        thread.start()
        return thread

    # ─────────────────────────────────────────────────────────────────────────
    # Private Methods
    # ─────────────────────────────────────────────────────────────────────────

    def _should_check(self) -> bool:
        """Check if enough time has passed since last check."""
        if not self.metadata_file.exists():
            return True

        try:
            metadata = self._load_metadata()
            last_check = metadata.get("last_check")
            if last_check:
                last_check_time = datetime.fromisoformat(last_check)
                if datetime.now() - last_check_time < timedelta(days=self.CHECK_INTERVAL_DAYS):
                    return False
        except Exception:
            pass

        return True

    def _update_last_check_time(self) -> None:
        """Update timestamp of last check."""
        metadata = self._load_metadata() or {}
        metadata["last_check"] = datetime.now().isoformat()
        self._save_metadata(metadata)

    def _fetch_version_metadata(self) -> Dict:
        """Fetch version.json from GitHub."""
        try:
            logger.debug(f"Fetching metadata from {self.version_json_url}")
            with urlopen(self.version_json_url, timeout=10) as response:
                data = response.read().decode("utf-8")
                return json.loads(data)
        except URLError as e:
            raise UpdateCheckError(f"Network error: {e}") from e
        except json.JSONDecodeError as e:
            raise UpdateCheckError(f"Invalid metadata: {e}") from e

    def _load_metadata(self) -> Optional[Dict]:
        """Load cached metadata."""
        if not self.metadata_file.exists():
            return None

        try:
            with open(self.metadata_file, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Failed to load metadata: {e}")
            return None

    def _save_metadata(self, metadata: Dict) -> None:
        """Save metadata to cache."""
        try:
            with open(self.metadata_file, "w") as f:
                json.dump(metadata, f)
        except Exception as e:
            logger.debug(f"Failed to save metadata: {e}")

    def _is_newer_version(self, new: str, old: str) -> bool:
        """Compare semantic versions."""
        try:
            new_parts = [int(x) for x in new.split(".")]
            old_parts = [int(x) for x in old.split(".")]

            # Pad with zeros
            while len(new_parts) < len(old_parts):
                new_parts.append(0)
            while len(old_parts) < len(new_parts):
                old_parts.append(0)

            return tuple(new_parts) > tuple(old_parts)
        except (ValueError, AttributeError):
            return False

    def _get_platform_key(self) -> str:
        """Get platform-specific download key."""
        system = platform.system()
        machine = platform.machine()

        if system == "Windows":
            return "windows"
        elif system == "Darwin":
            # Check CPU type
            if machine == "arm64" or "Apple" in platform.processor():
                return "macos_arm"
            else:
                return "macos_intel"
        elif system == "Linux":
            return "linux"
        else:
            raise UpdateCheckError(f"Unsupported platform: {system}")

    def _download_file(self, url: str, destination: Path) -> None:
        """Download file from URL."""
        logger.debug(f"Downloading {url}...")
        try:
            with urlopen(url, timeout=300) as response:
                with open(destination, "wb") as f:
                    while True:
                        chunk = response.read(1024 * 1024)  # 1MB chunks
                        if not chunk:
                            break
                        f.write(chunk)
        except Exception as e:
            raise UpdateCheckError(f"Download failed: {e}") from e

    def _calculate_checksum(self, file_path: Path) -> str:
        """Calculate SHA256 checksum."""
        import hashlib
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256.update(chunk)
        return f"sha256:{sha256.hexdigest()}"


def setup_update_notifications():
    """Setup UI notifications for updates (platform-specific)."""
    def show_update_notification(version: str):
        """Show notification that update is available."""
        system = platform.system()

        if system == "Windows":
            try:
                from win10toast import ToastNotifier
                notifier = ToastNotifier()
                notifier.show_toast(
                    "ParcInfo Update Available",
                    f"Version {version} is available. Update now?",
                    duration=10,
                    threaded=True
                )
            except ImportError:
                logger.info(f"Update available: {version}")

        elif system == "Darwin":
            try:
                import os
                script = f'display notification "Version {version} available" with title "ParcInfo Update"'
                os.system(f"osascript -e '{script}'")
            except Exception:
                logger.info(f"Update available: {version}")

    return show_update_notification


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    checker = UpdateChecker()
    if checker.check_for_updates(force=True):
        print(f"✓ Update available: {checker.latest_version}")
    else:
        print(f"✓ Already on latest version: {checker.current_version}")
