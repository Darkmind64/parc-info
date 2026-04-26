"""
ParcInfo Version Management

Central source of truth for versioning across the application,
installers, and CI/CD pipeline.

Usage:
    from __version__ import __version__, __version_tuple__, GITHUB_REPO
"""

# Semantic versioning (MAJOR.MINOR.PATCH)
__version__ = "2.5.0"
__version_tuple__ = (2, 5, 0)

# Build metadata
GITHUB_REPO = "darkmind64/parc_info"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
GITHUB_API_RELEASES = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RAW_CONTENT = f"https://raw.githubusercontent.com/{GITHUB_REPO}/master"

# Application info
APP_NAME = "ParcInfo"
APP_DISPLAY_NAME = "ParcInfo — IT Asset Management"
APP_PUBLISHER = "ParcInfo Team"
APP_WEBSITE = f"https://github.com/{GITHUB_REPO}"
APP_EMAIL = "support@parcinfo.local"

# Minimum requirements
MIN_PYTHON_VERSION = (3, 8)  # Python 3.8+
MIN_PYTHON_VERSION_STR = "3.8"

# Platform-specific metadata
PLATFORM_INFO = {
    "windows": {
        "name": "Windows",
        "min_version": "10",
        "arch": "x64",
        "installer_name": f"ParcInfo-Setup-{__version__}.exe",
        "install_dir": "C:\\Program Files\\ParcInfo",
    },
    "macos": {
        "name": "macOS",
        "min_version": "10.13",
        "universal": True,
        "dmg_name": f"ParcInfo-{__version__}.dmg",
        "app_dir": "/Applications/ParcInfo.app",
    },
    "linux": {
        "name": "Linux",
        "min_version": "Ubuntu 18.04+",
        "supported": False,  # Future support
    },
}


def version_string() -> str:
    """Return formatted version string."""
    return __version__


def version_tuple() -> tuple:
    """Return version as tuple for comparisons."""
    return __version_tuple__


def is_development() -> bool:
    """Check if running in development mode."""
    import sys
    return hasattr(sys, "_called_from_test") or "pytest" in sys.modules


def get_install_dir(platform: str = None) -> str:
    """Get installation directory for given platform."""
    if platform is None:
        import sys
        if sys.platform == "win32":
            platform = "windows"
        elif sys.platform == "darwin":
            platform = "macos"
        else:
            platform = "linux"

    if platform in PLATFORM_INFO:
        return PLATFORM_INFO[platform].get("install_dir", "")
    return ""


if __name__ == "__main__":
    print(f"ParcInfo v{__version__}")
    print(f"Repository: {GITHUB_REPO}")
    print(f"Minimum Python: {MIN_PYTHON_VERSION_STR}")
