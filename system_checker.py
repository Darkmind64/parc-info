"""
ParcInfo System Requirements Checker

Validates system dependencies before application launch:
- Python version
- Required libraries (sqlite3, cryptography, etc.)
- Platform-specific requirements (Visual C++, Xcode)
- Internet connectivity for auto-update checks

Usage:
    from system_checker import check_system_requirements
    missing = check_system_requirements(verbose=True)
    if missing:
        print(f"Missing requirements: {missing}")
        sys.exit(1)
"""

import sys
import platform
import subprocess
import logging
from typing import List, Dict, Optional
from pathlib import Path

from __version__ import MIN_PYTHON_VERSION, MIN_PYTHON_VERSION_STR

logger = logging.getLogger("parcinfo.system")


class SystemCheckError(Exception):
    """Raised when critical system requirement is missing."""
    pass


def check_python_version() -> bool:
    """Check if Python version meets minimum requirement."""
    if sys.version_info[:2] < MIN_PYTHON_VERSION:
        logger.error(
            f"Python {MIN_PYTHON_VERSION_STR}+ required, "
            f"but running {sys.version_info.major}.{sys.version_info.minor}"
        )
        return False
    return True


def check_python_modules() -> List[str]:
    """Check if required Python modules are available."""
    required_modules = [
        "flask",
        "werkzeug",
        "jinja2",
        "sqlite3",
        "hashlib",
        "secrets",
        "cryptography",
        "json",
        "socket",
        "threading",
        "subprocess",
    ]

    missing = []
    for module_name in required_modules:
        try:
            __import__(module_name)
        except ImportError:
            missing.append(module_name)
            logger.warning(f"Missing module: {module_name}")

    return missing


def check_windows_dependencies() -> Dict[str, bool]:
    """Check Windows-specific dependencies."""
    checks = {}

    # Check Visual C++ Runtime (optional, PyInstaller includes it)
    try:
        import ctypes
        ctypes.CDLL("VCRUNTIME140.dll")
        checks["Visual C++ Runtime"] = True
    except OSError:
        logger.warning("Visual C++ Runtime not found (not critical)")
        checks["Visual C++ Runtime"] = False

    # Check registry for Python installation
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            f"SOFTWARE\\Python\\PythonCore\\{sys.version_info.major}.{sys.version_info.minor}"
        )
        winreg.CloseKey(key)
        checks["Windows Registry"] = True
    except Exception:
        logger.debug("Python not in Windows Registry (normal for PyInstaller)")
        checks["Windows Registry"] = False

    return checks


def check_macos_dependencies() -> Dict[str, bool]:
    """Check macOS-specific dependencies."""
    checks = {}

    # Check Xcode CLI tools (optional, but needed for ARP/network scan)
    try:
        result = subprocess.run(
            ["xcode-select", "--print-path"],
            capture_output=True,
            text=True,
            timeout=5
        )
        checks["Xcode CLI Tools"] = result.returncode == 0
        if not checks["Xcode CLI Tools"]:
            logger.warning(
                "Xcode CLI Tools not installed. "
                "Network scan features may be limited. "
                "Install with: xcode-select --install"
            )
    except Exception as e:
        logger.debug(f"Xcode check failed: {e}")
        checks["Xcode CLI Tools"] = False

    # Check for openssl (usually system-provided)
    try:
        result = subprocess.run(
            ["which", "openssl"],
            capture_output=True,
            timeout=5
        )
        checks["OpenSSL"] = result.returncode == 0
    except Exception:
        checks["OpenSSL"] = False

    return checks


def check_linux_dependencies() -> Dict[str, bool]:
    """Check Linux-specific dependencies."""
    checks = {}

    # Check for required system libraries
    required_commands = ["python3", "sqlite3", "openssl"]

    for cmd in required_commands:
        try:
            result = subprocess.run(
                ["which", cmd],
                capture_output=True,
                timeout=5
            )
            checks[cmd] = result.returncode == 0
            if not checks[cmd]:
                logger.warning(f"Command not found: {cmd}")
        except Exception:
            checks[cmd] = False

    return checks


def check_network_connectivity(timeout: int = 5) -> bool:
    """Check if system can reach GitHub (for auto-update checks)."""
    try:
        import socket
        socket.create_connection(("api.github.com", 443), timeout=timeout)
        return True
    except (socket.timeout, socket.error) as e:
        logger.debug(f"Network connectivity check failed: {e}")
        return False


def check_disk_space(min_free_mb: int = 100) -> bool:
    """Check if sufficient disk space available."""
    try:
        import shutil
        stat = shutil.disk_usage("/")
        free_mb = stat.free // (1024 * 1024)
        if free_mb < min_free_mb:
            logger.warning(f"Low disk space: {free_mb} MB free")
            return False
        return True
    except Exception as e:
        logger.debug(f"Disk space check failed: {e}")
        return True  # Don't fail on this


def check_system_requirements(
    verbose: bool = False,
    strict: bool = True
) -> List[str]:
    """
    Comprehensive system requirements check.

    Args:
        verbose: Print detailed check results
        strict: Fail on any missing requirement (vs. warnings only)

    Returns:
        List of missing/failed requirements. Empty list = all good.

    Raises:
        SystemCheckError: If critical requirements fail (if strict=True)
    """
    missing = []

    if verbose:
        logger.info(f"System: {platform.system()} {platform.release()}")
        logger.info(f"Python: {sys.version}")
        logger.info("=" * 60)

    # 1. Check Python version (CRITICAL)
    if not check_python_version():
        missing.append(f"Python {MIN_PYTHON_VERSION_STR}+")
        if strict:
            raise SystemCheckError(f"Python {MIN_PYTHON_VERSION_STR}+ required")

    # 2. Check Python modules (CRITICAL)
    missing_modules = check_python_modules()
    if missing_modules:
        msg = f"Missing Python modules: {', '.join(missing_modules)}"
        missing.append(msg)
        if strict and missing_modules:
            raise SystemCheckError(msg)

    # 3. Platform-specific checks
    platform_checks = {}
    system = platform.system()

    if system == "Windows":
        platform_checks = check_windows_dependencies()
    elif system == "Darwin":
        platform_checks = check_macos_dependencies()
    elif system == "Linux":
        platform_checks = check_linux_dependencies()

    if verbose and platform_checks:
        logger.info(f"{system} Dependencies:")
        for check_name, result in platform_checks.items():
            status = "✅" if result else "❌"
            logger.info(f"  {status} {check_name}")

    # 4. Network connectivity (non-critical, for auto-update)
    has_network = check_network_connectivity()
    if verbose:
        status = "✅" if has_network else "⚠️"
        logger.info(f"{status} Network connectivity: {has_network}")

    # 5. Disk space (non-critical)
    has_space = check_disk_space()
    if verbose:
        status = "✅" if has_space else "⚠️"
        logger.info(f"{status} Disk space: {has_space}")

    if verbose:
        if not missing:
            logger.info("=" * 60)
            logger.info("✅ All system requirements met!")
        else:
            logger.warning("=" * 60)
            logger.warning("❌ Missing requirements found:")
            for item in missing:
                logger.warning(f"  - {item}")

    return missing


def print_system_info():
    """Print detailed system information (for debugging)."""
    print("=" * 60)
    print("ParcInfo System Information")
    print("=" * 60)
    print(f"System: {platform.system()} {platform.release()}")
    print(f"Architecture: {platform.machine()}")
    print(f"Python: {sys.version}")
    print(f"Python Path: {sys.executable}")
    print(f"Platform: {sys.platform}")

    if hasattr(sys, "frozen"):
        print(f"Frozen: Yes (PyInstaller)")
        print(f"Frozen Path: {sys.executable}")

    print("=" * 60)
    print("Running system checks...")
    print("=" * 60)
    check_system_requirements(verbose=True, strict=False)


if __name__ == "__main__":
    # Script de debugging
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--strict", "-s", action="store_true", default=True)
    parser.add_argument("--sysinfo", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.sysinfo:
        print_system_info()
    else:
        missing = check_system_requirements(verbose=args.verbose, strict=args.strict)
        sys.exit(len(missing))
