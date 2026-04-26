#!/usr/bin/env python3
"""
ParcInfo Installer

Executable installer that:
- Copies compiled binaries to installation directory
- Creates shortcuts (Windows/macOS)
- Sets up registry entries (Windows)
- Launches application

This script is meant to be compiled with PyInstaller:
    pyinstaller --onefile --windowed --icon=static/icon.ico installer.py

Usage:
    installer.exe              (GUI installer)
    installer.exe --silent     (silent install to default location)
"""

import argparse
import logging
import os
import platform
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

from __version__ import __version__, APP_NAME

logger = logging.getLogger("installer")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s"
)


class Installer:
    """Cross-platform installer for ParcInfo."""

    def __init__(self, source_exe: Optional[str] = None):
        """
        Initialize installer.

        Args:
            source_exe: Path to ParcInfo.exe to install
                       (auto-detected if not provided)
        """
        self.system = platform.system()
        self.source_exe = self._find_source_exe(source_exe)
        self.version = __version__

        # Default installation paths
        if self.system == "Windows":
            self.install_dir = Path("C:/Program Files") / APP_NAME
        elif self.system == "Darwin":
            self.install_dir = Path("/Applications") / f"{APP_NAME}.app"
        else:
            self.install_dir = Path.home() / f".{APP_NAME.lower()}"

        self.data_dir = None
        self.success = False

    def _find_source_exe(self, source_exe: Optional[str]) -> Optional[Path]:
        """Find source executable to install."""
        if source_exe:
            return Path(source_exe)

        # Look for compiled binaries in common locations
        candidates = [
            Path("dist") / f"{APP_NAME}.exe",
            Path("dist") / APP_NAME,
            Path("dist") / f"{APP_NAME}.app",
            Path.cwd() / f"{APP_NAME}.exe",
            Path.cwd() / APP_NAME,
        ]

        for candidate in candidates:
            if candidate.exists():
                logger.info(f"Found source: {candidate}")
                return candidate

        return None

    def validate(self) -> bool:
        """Validate installation prerequisites."""
        logger.info("Validating installation prerequisites...")

        if not self.source_exe or not self.source_exe.exists():
            logger.error(f"Source executable not found: {self.source_exe}")
            return False

        # Check disk space (minimum 200 MB)
        try:
            stat = shutil.disk_usage(self.install_dir.parent)
            free_mb = stat.free // (1024 * 1024)
            if free_mb < 200:
                logger.error(f"Insufficient disk space: {free_mb}MB free")
                return False
        except Exception as e:
            logger.warning(f"Could not check disk space: {e}")

        logger.info("✓ Prerequisites OK")
        return True

    def install(self, install_dir: Optional[Path] = None) -> bool:
        """
        Execute installation.

        Args:
            install_dir: Custom installation directory

        Returns:
            True if installation succeeded
        """
        if install_dir:
            self.install_dir = install_dir

        logger.info(f"Installing {APP_NAME} {self.version}...")
        logger.info(f"Source: {self.source_exe}")
        logger.info(f"Destination: {self.install_dir}")

        try:
            # Create installation directory
            self.install_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"✓ Created directory: {self.install_dir}")

            # Copy executable and resources
            if self.system == "Windows":
                self._install_windows()
            elif self.system == "Darwin":
                self._install_macos()
            else:
                self._install_linux()

            # Create data directory
            self._create_data_directory()

            self.success = True
            logger.info(f"✓ Installation completed successfully!")
            return True

        except Exception as e:
            logger.error(f"✗ Installation failed: {e}")
            return False

    def _install_windows(self) -> None:
        """Install on Windows."""
        # Copy executable
        exe_name = self.source_exe.name
        dest_exe = self.install_dir / exe_name
        shutil.copy2(self.source_exe, dest_exe)
        logger.info(f"✓ Copied: {dest_exe}")

        # Copy support files if in dist/ folder
        dist_dir = self.source_exe.parent
        if dist_dir.name == "dist":
            support_dir = dist_dir / APP_NAME
            if support_dir.exists():
                dest_support = self.install_dir / APP_NAME
                if dest_support.exists():
                    shutil.rmtree(dest_support)
                shutil.copytree(support_dir, dest_support)
                logger.info(f"✓ Copied support files")

        # Create shortcuts
        self._create_windows_shortcuts(dest_exe)

        # Add to registry (Add/Remove Programs)
        self._add_windows_registry(dest_exe)

    def _install_macos(self) -> None:
        """Install on macOS."""
        # Copy .app bundle
        if self.source_exe.suffix == ".app" or self.source_exe.is_dir():
            shutil.copytree(self.source_exe, self.install_dir, dirs_exist_ok=True)
        else:
            # Create .app bundle
            app_dir = self.install_dir
            app_dir.mkdir(parents=True, exist_ok=True)
            contents = app_dir / "Contents" / "MacOS"
            contents.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.source_exe, contents / self.source_exe.name)

        logger.info(f"✓ Copied: {self.install_dir}")

        # Remove quarantine attribute
        try:
            subprocess.run(
                ["xattr", "-d", "com.apple.quarantine", str(self.install_dir)],
                check=False
            )
            logger.info("✓ Removed quarantine attribute")
        except Exception:
            pass

    def _install_linux(self) -> None:
        """Install on Linux."""
        bin_dir = self.install_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)

        dest_exe = bin_dir / self.source_exe.name
        shutil.copy2(self.source_exe, dest_exe)
        dest_exe.chmod(0o755)
        logger.info(f"✓ Copied: {dest_exe}")

    def _create_windows_shortcuts(self, exe_path: Path) -> None:
        """Create Windows shortcuts."""
        try:
            # Try to create shortcut using COM (requires pywin32)
            try:
                import win32com.client
                shell = win32com.client.Dispatch("WScript.Shell")

                # Start Menu shortcut
                start_menu = Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs"
                start_menu.mkdir(parents=True, exist_ok=True)

                shortcut_path = start_menu / f"{APP_NAME}.lnk"
                shortcut = shell.CreateShortcut(str(shortcut_path))
                shortcut.TargetPath = str(exe_path)
                shortcut.IconLocation = str(exe_path)
                shortcut.Save()
                logger.info(f"✓ Created Start Menu shortcut")

                # Desktop shortcut
                desktop = Path.home() / "Desktop"
                desktop_shortcut = desktop / f"{APP_NAME}.lnk"
                shortcut = shell.CreateShortcut(str(desktop_shortcut))
                shortcut.TargetPath = str(exe_path)
                shortcut.IconLocation = str(exe_path)
                shortcut.Save()
                logger.info(f"✓ Created Desktop shortcut")

            except ImportError:
                # Fallback: use PowerShell
                ps_script = f"""
                $WshShell = New-Object -ComObject WScript.Shell
                $Shortcut = $WshShell.CreateShortcut('{Path.home()}/Desktop/{APP_NAME}.lnk')
                $Shortcut.TargetPath = '{exe_path}'
                $Shortcut.IconLocation = '{exe_path}'
                $Shortcut.Save()
                """
                subprocess.run(["powershell", "-Command", ps_script], check=True)
                logger.info("✓ Created Desktop shortcut (PowerShell)")

        except Exception as e:
            logger.warning(f"Could not create shortcuts: {e}")

    def _add_windows_registry(self, exe_path: Path) -> None:
        """Add ParcInfo to Windows registry (Add/Remove Programs)."""
        try:
            import winreg

            # Create registry keys
            reg_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ParcInfo"
            try:
                key = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
            except PermissionError:
                # Fallback to HKEY_CURRENT_USER
                key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg_path)

            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, f"{APP_NAME} {self.version}")
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, self.version)
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(exe_path))
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(self.install_dir))
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "ParcInfo Team")
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, str(exe_path))
            winreg.CloseKey(key)

            logger.info("✓ Added registry entries")

        except Exception as e:
            logger.warning(f"Could not add registry entries: {e}")

    def _create_data_directory(self) -> None:
        """Create user data directory."""
        if self.system == "Windows":
            self.data_dir = Path.home() / "AppData/Roaming" / APP_NAME
        elif self.system == "Darwin":
            self.data_dir = Path.home() / f".{APP_NAME.lower()}"
        else:
            self.data_dir = Path.home() / f".{APP_NAME.lower()}"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"✓ Created data directory: {self.data_dir}")

    def launch(self) -> bool:
        """Launch application after installation."""
        try:
            if self.system == "Windows":
                exe = list(self.install_dir.glob("*.exe"))[0]
                subprocess.Popen(str(exe))
            elif self.system == "Darwin":
                subprocess.Popen(["open", str(self.install_dir)])
            else:
                exe = list(self.install_dir.glob("*"))[0]
                subprocess.Popen(str(exe))

            logger.info("✓ Application launched")
            return True
        except Exception as e:
            logger.error(f"Could not launch application: {e}")
            return False


class InstallerGUI:
    """GUI for installer using tkinter."""

    def __init__(self, root: tk.Tk, installer: Installer):
        self.root = root
        self.installer = installer

        # Window setup
        root.title(f"{APP_NAME} Installer")
        root.geometry("500x300")
        root.resizable(False, False)

        # Center window
        root.update_idletasks()
        x = (root.winfo_screenwidth() // 2) - (root.winfo_width() // 2)
        y = (root.winfo_screenheight() // 2) - (root.winfo_height() // 2)
        root.geometry(f"+{x}+{y}")

        self._create_widgets()

    def _create_widgets(self) -> None:
        """Create GUI widgets."""
        # Title
        title = ttk.Label(
            self.root,
            text=f"{APP_NAME} Installer",
            font=("Arial", 16, "bold")
        )
        title.pack(pady=20)

        # Version
        version_label = ttk.Label(
            self.root,
            text=f"Version {self.installer.version}",
            font=("Arial", 10)
        )
        version_label.pack()

        # Info frame
        info_frame = ttk.Frame(self.root)
        info_frame.pack(pady=20, padx=20, fill=tk.BOTH, expand=True)

        ttk.Label(info_frame, text="Installation Directory:").grid(row=0, column=0, sticky=tk.W)
        self.install_dir_var = tk.StringVar(value=str(self.installer.install_dir))
        ttk.Entry(info_frame, textvariable=self.install_dir_var, width=50).grid(row=0, column=1)

        # Buttons
        button_frame = ttk.Frame(self.root)
        button_frame.pack(pady=20)

        ttk.Button(button_frame, text="Install", command=self._install).pack(side=tk.LEFT, padx=10)
        ttk.Button(button_frame, text="Cancel", command=self.root.quit).pack(side=tk.LEFT, padx=10)

        # Progress
        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(pady=10, padx=20, fill=tk.X)

        # Status
        self.status_var = tk.StringVar(value="Ready to install")
        self.status_label = ttk.Label(self.root, textvariable=self.status_var)
        self.status_label.pack(pady=10)

    def _install(self) -> None:
        """Execute installation."""
        self.progress.start()
        self.status_var.set("Installing...")

        install_dir = Path(self.install_dir_var.get())

        try:
            if not self.installer.validate():
                raise Exception("Installation validation failed")

            if not self.installer.install(install_dir):
                raise Exception("Installation failed")

            self.status_var.set("Installation successful!")
            messagebox.showinfo("Success", f"{APP_NAME} installed successfully!")

            # Launch application
            if messagebox.askyesno("Launch", "Launch application now?"):
                self.installer.launch()

            self.root.quit()

        except Exception as e:
            self.status_var.set(f"Error: {e}")
            messagebox.showerror("Installation Error", str(e))
            self.progress.stop()


def main():
    parser = argparse.ArgumentParser(
        description=f"{APP_NAME} Installer"
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Silent installation (no GUI)"
    )
    parser.add_argument(
        "--source",
        help="Path to source executable"
    )
    parser.add_argument(
        "--install-dir",
        help="Installation directory"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose output"
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Create installer
    installer = Installer(source_exe=args.source)

    if not installer.source_exe:
        logger.error(f"Could not find {APP_NAME} executable to install")
        if not args.silent:
            messagebox.showerror("Error", f"Could not find {APP_NAME} executable")
        sys.exit(1)

    # Silent installation
    if args.silent:
        if not installer.validate():
            sys.exit(1)

        install_dir = Path(args.install_dir) if args.install_dir else installer.install_dir
        if not installer.install(install_dir):
            sys.exit(1)

        installer.launch()
        sys.exit(0)

    # GUI installation
    root = tk.Tk()
    gui = InstallerGUI(root, installer)
    root.mainloop()


if __name__ == "__main__":
    main()
