#!/bin/bash
################################################################################
# ParcInfo macOS Universal Installer
#
# Installs ParcInfo by compiling from source on the target machine.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/darkmind64/parc-info/master/install.sh | bash
#
# Or locally:
#   chmod +x install.sh
#   ./install.sh
#
# What it does:
#   1. Checks Python 3.8+ availability
#   2. Verifies Xcode Command Line Tools
#   3. Downloads/clones ParcInfo source
#   4. Creates isolated Python environment
#   5. Installs dependencies
#   6. Compiles with PyInstaller
#   7. Installs app to /Applications
#   8. Launches automatically
#
# Requirements:
#   - macOS 10.13+
#   - Python 3.8+ (will prompt to install if missing)
#   - Xcode Command Line Tools (will prompt to install if missing)
#   - 2GB free disk space
#
################################################################################

set -e  # Exit on error

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
APP_NAME="ParcInfo"
REPO_URL="https://github.com/darkmind64/parc-info.git"
INSTALL_DIR="/Applications"
TEMP_DIR=$(mktemp -d)
PYTHON_MIN_VERSION="3.8"

# Cleanup on exit
cleanup() {
    if [ -d "$TEMP_DIR" ]; then
        rm -rf "$TEMP_DIR"
    fi
}
trap cleanup EXIT

################################################################################
# Utility Functions
################################################################################

print_header() {
    echo -e "${BLUE}════════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}════════════════════════════════════════════════════════════════${NC}"
}

print_step() {
    echo -e "${BLUE}▶${NC} $1"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

################################################################################
# Checks
################################################################################

check_macos_version() {
    print_step "Checking macOS version..."

    local os_version=$(sw_vers -productVersion)
    local major_version=$(echo "$os_version" | cut -d. -f1)

    if [ "$major_version" -lt 10 ]; then
        print_error "macOS 10.13+ required (you have $os_version)"
        exit 1
    fi

    print_success "macOS $os_version (compatible)"
}

check_python() {
    print_step "Checking Python availability..."

    # Try python3 first
    if command -v python3 &> /dev/null; then
        PYTHON_BIN="python3"
    elif command -v python &> /dev/null; then
        PYTHON_BIN="python"
    else
        print_error "Python not found"
        print_warning "Install Python 3.8+ from:"
        echo "  https://www.python.org/downloads/macos/"
        echo ""
        echo "Or via Homebrew:"
        echo "  brew install python@3.11"
        exit 1
    fi

    # Check version
    local python_version=$($PYTHON_BIN --version 2>&1 | awk '{print $2}')
    local major_minor=$(echo "$python_version" | cut -d. -f1-2)

    print_success "Found $PYTHON_BIN $python_version"

    # Verify minimum version
    if [ "$(printf '%s\n' "$PYTHON_MIN_VERSION" "$major_minor" | sort -V | head -n1)" != "$PYTHON_MIN_VERSION" ]; then
        print_error "Python $PYTHON_MIN_VERSION+ required (you have $python_version)"
        exit 1
    fi
}

check_xcode_tools() {
    print_step "Checking Xcode Command Line Tools..."

    if ! command -v xcode-select &> /dev/null || ! xcode-select -p &> /dev/null; then
        print_warning "Xcode Command Line Tools not found"
        echo ""
        echo "Installing Xcode Command Line Tools..."
        echo "(This may take several minutes)"
        echo ""

        xcode-select --install

        # Wait for installation
        while ! xcode-select -p &> /dev/null; do
            sleep 5
        done

        print_success "Xcode tools installed"
    else
        print_success "Xcode tools found"
    fi
}

check_disk_space() {
    print_step "Checking disk space..."

    local available=$(df /tmp | awk 'NR==2 {print $4}')
    local required=$((2 * 1024 * 1024))  # 2GB in KB

    if [ "$available" -lt "$required" ]; then
        print_error "Insufficient disk space (need 2GB, have $((available / 1024 / 1024))GB)"
        exit 1
    fi

    print_success "Enough disk space available"
}

################################################################################
# Installation
################################################################################

download_source() {
    print_step "Downloading ParcInfo source code..."

    cd "$TEMP_DIR"

    # Try git clone first (better for large repos)
    if command -v git &> /dev/null; then
        git clone --depth 1 "$REPO_URL" parc_info 2>&1 | grep -v "warning"
    else
        # Fallback to download ZIP
        print_warning "Git not found, downloading ZIP instead"
        curl -fsSL "${REPO_URL//.git/}/archive/refs/heads/master.zip" -o parc_info.zip
        unzip -q parc_info.zip
        mv parc-info-master parc_info
    fi

    cd parc_info
    print_success "Source code downloaded"
}

create_venv() {
    print_step "Creating Python virtual environment..."

    $PYTHON_BIN -m venv venv
    source venv/bin/activate

    print_success "Virtual environment created"
}

install_dependencies() {
    print_step "Installing dependencies (this may take a few minutes)..."

    # Upgrade pip first
    pip install -q --upgrade pip setuptools wheel

    # Install requirements
    pip install -q -r requirements.txt

    # Install PyInstaller and build dependencies
    pip install -q pyinstaller pillow pystray

    print_success "Dependencies installed"
}

compile_app() {
    print_step "Compiling with PyInstaller..."

    pyinstaller parcinfo.spec 2>&1 | tail -5

    print_success "Compilation complete"
}

install_to_applications() {
    print_step "Installing to /Applications..."

    # Remove old installation if exists
    if [ -d "$INSTALL_DIR/$APP_NAME.app" ]; then
        rm -rf "$INSTALL_DIR/$APP_NAME.app"
    fi

    # Move compiled app
    mv dist/$APP_NAME.app "$INSTALL_DIR/"

    # Remove quarantine attribute
    xattr -d com.apple.quarantine "$INSTALL_DIR/$APP_NAME.app" 2>/dev/null || true

    print_success "Installed to $INSTALL_DIR/$APP_NAME.app"
}

create_launcher_script() {
    print_step "Creating launcher script..."

    cat > "$INSTALL_DIR/$APP_NAME.app/Contents/MacOS/launcher.sh" << 'LAUNCHER_EOF'
#!/bin/bash
# Quick launcher for ParcInfo
open -a "$APP_NAME"
LAUNCHER_EOF

    chmod +x "$INSTALL_DIR/$APP_NAME.app/Contents/MacOS/launcher.sh"
    print_success "Launcher script created"
}

################################################################################
# Main Installation
################################################################################

main() {
    print_header "$APP_NAME Universal Installer for macOS"
    echo ""
    echo "This installer will:"
    echo "  1. Verify prerequisites (Python, Xcode tools)"
    echo "  2. Download source code from GitHub"
    echo "  3. Create isolated Python environment"
    echo "  4. Compile application locally"
    echo "  5. Install to /Applications"
    echo ""

    # Pre-flight checks
    check_macos_version
    check_python
    check_xcode_tools
    check_disk_space

    echo ""
    print_header "Installation Starting"
    echo ""

    # Installation steps
    download_source
    create_venv
    install_dependencies
    compile_app
    install_to_applications

    echo ""
    print_header "Installation Complete"
    echo ""
    print_success "$APP_NAME installed successfully!"
    echo ""
    echo "Location: $INSTALL_DIR/$APP_NAME.app"
    echo ""

    # Launch app
    print_step "Launching $APP_NAME..."
    open "$INSTALL_DIR/$APP_NAME.app"

    echo ""
    print_success "All done! $APP_NAME is running."
    echo ""
    echo "Tips:"
    echo "  • Add to Dock: Right-click app → Options → Keep in Dock"
    echo "  • Updates: Available from within the app (auto-update)"
    echo "  • Uninstall: Drag $APP_NAME.app to Trash"
    echo ""
}

# Run main
main
