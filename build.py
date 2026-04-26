#!/usr/bin/env python3
"""
Simple build script for ParcInfo

Compiles:
1. Main application (parcinfo.spec)
2. Installer (installer.spec)

Usage:
    python build.py              # Build both
    python build.py --app-only   # Build app only
    python build.py --installer-only
    python build.py --clean
"""

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def run_cmd(cmd: list) -> bool:
    """Run command and return success status."""
    try:
        logger.info(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=True)
        return result.returncode == 0
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed: {e}")
        return False


def build_app():
    """Build main application with PyInstaller."""
    logger.info("\n" + "="*60)
    logger.info("Building ParcInfo Application")
    logger.info("="*60)

    if not run_cmd(["pyinstaller", "parcinfo.spec"]):
        return False

    logger.info("✓ Application built: dist/ParcInfo.exe (or .app)")
    return True


def build_installer():
    """Build standalone installer with embedded ParcInfo.exe."""
    logger.info("\n" + "="*60)
    logger.info("Building ParcInfo Standalone Installer")
    logger.info("="*60)

    # Verify ParcInfo.exe exists (must build app first)
    app_exe = Path("dist") / "ParcInfo.exe"
    if not app_exe.exists():
        logger.error(f"❌ ParcInfo.exe not found. Build application first with: python build.py --app-only")
        return False

    logger.info(f"✓ Found embedded app: {app_exe}")

    if not run_cmd(["pyinstaller", "installer.spec"]):
        return False

    logger.info("✓ Standalone installer built: dist/installer.exe")
    logger.info("  → Single executable contains both installer + ParcInfo.exe")
    return True


def clean():
    """Clean build artifacts."""
    logger.info("Cleaning build artifacts...")

    dirs_to_clean = [
        Path("dist"),
        Path("build"),
        Path(".pytest_cache"),
        Path("*.egg-info"),
    ]

    for dir_path in dirs_to_clean:
        if dir_path.exists():
            if dir_path.is_dir():
                shutil.rmtree(dir_path)
            else:
                dir_path.unlink()
            logger.info(f"✓ Removed: {dir_path}")


def main():
    parser = argparse.ArgumentParser(description="ParcInfo Build Script")
    parser.add_argument(
        "--app-only",
        action="store_true",
        help="Build application only"
    )
    parser.add_argument(
        "--installer-only",
        action="store_true",
        help="Build installer only"
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean build artifacts"
    )

    args = parser.parse_args()

    if args.clean:
        clean()
        return 0

    logger.info(f"\n🔨 ParcInfo Build System\n")

    # Determine what to build
    build_app_flag = args.app_only or not args.installer_only
    build_installer_flag = args.installer_only or not args.app_only

    success = True

    if build_app_flag:
        if not build_app():
            success = False

    if build_installer_flag:
        if not build_installer():
            success = False

    # Summary
    logger.info("\n" + "="*60)
    if success:
        logger.info("✅ Build completed successfully!")
        logger.info("\n📦 STANDALONE INSTALLER READY")
        logger.info("   File: dist/installer.exe")
        logger.info("   Contains: ParcInfo.exe + Installer")
        logger.info("   Size: Single executable (no separate files needed)")
        logger.info("\nUser instructions:")
        logger.info("1. Download dist/installer.exe")
        logger.info("2. Run installer.exe")
        logger.info("3. Installer auto-extracts ParcInfo and installs")
        logger.info("4. App auto-updates when new version available")
        logger.info("="*60)
        return 0
    else:
        logger.error("❌ Build failed!")
        logger.error("="*60)
        return 1


if __name__ == "__main__":
    sys.exit(main())
