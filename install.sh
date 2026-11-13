#!/bin/bash
# ─────────────────────────────────────────────
#  ytdpro-web installer
#  Usage:  curl -sSL https://raw.githubusercontent.com/ethicalvulnlab/ytdpro/main/install.sh | bash
# ─────────────────────────────────────────────

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}✔  $1${RESET}"; }
info() { echo -e "${YELLOW}➜  $1${RESET}"; }
err()  { echo -e "${RED}✘  $1${RESET}"; exit 1; }

echo ""
echo -e "${CYAN}${BOLD}  ytdpro — web installer${RESET}"
echo -e "${CYAN}  ─────────────────────────────${RESET}"
echo ""

# ── Check Python ─────────────────────────────
if ! command -v python3 &>/dev/null; then
    err "Python 3 is required. Install it from https://python.org"
fi
ok "Python3 found: $(python3 --version)"

# ── Check pip ────────────────────────────────
if ! python3 -m venv --help &>/dev/null; then
    err "python3-venv is required. Install it with: sudo apt install python3-venv"
fi

# ── Check ffmpeg ─────────────────────────────
if ! command -v ffmpeg &>/dev/null; then
    info "ffmpeg not found — installing…"
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y ffmpeg
    elif command -v brew &>/dev/null; then
        brew install ffmpeg
    else
        err "Please install ffmpeg manually: https://ffmpeg.org/download.html"
    fi
fi
ok "ffmpeg found"

# ── Install directory ────────────────────────
INSTALL_DIR="$HOME/.ytdpro"
mkdir -p "$INSTALL_DIR"
info "Installing to: $INSTALL_DIR"

# ── Download app files ───────────────────────
BASE_URL="https://raw.githubusercontent.com/ethicalvulnlab/ytdpro/main"

mkdir -p "$INSTALL_DIR/templates"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
curl -fsSL "$BASE_URL/server.py"            -o "$TMP_DIR/server.py"
curl -fsSL "$BASE_URL/templates/index.html" -o "$TMP_DIR/index.html"
curl -fsSL "$BASE_URL/requirements.txt"     -o "$TMP_DIR/requirements.txt"
mv "$TMP_DIR/server.py"        "$INSTALL_DIR/server.py"
mv "$TMP_DIR/index.html"       "$INSTALL_DIR/templates/index.html"
mv "$TMP_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
ok "App files downloaded"

# ── Virtual environment ──────────────────────
if [ ! -d "$INSTALL_DIR/venv" ]; then
    info "Creating virtual environment…"
    python3 -m venv "$INSTALL_DIR/venv"
fi
info "Installing Python packages into the venv…"
"$INSTALL_DIR/venv/bin/pip" install --quiet -U -r "$INSTALL_DIR/requirements.txt"
ok "Python packages installed"

# ── Create launcher script ───────────────────
LAUNCHER="$HOME/.local/bin/ytdpro"
mkdir -p "$HOME/.local/bin"
cat > "$LAUNCHER" << EOF
#!/bin/bash
cd "$INSTALL_DIR"
"$INSTALL_DIR/venv/bin/python" server.py &
SERVER_PID=\$!
sleep 1

# Open browser
if command -v xdg-open &>/dev/null; then
    xdg-open "http://localhost:\${YT_PORT:-8080}" </dev/null
elif command -v open &>/dev/null; then
    open "http://localhost:\${YT_PORT:-8080}"
fi

echo ""
echo "  ytdpro is running at http://localhost:\${YT_PORT:-8080}"
echo "  Press Ctrl+C to stop."
wait \$SERVER_PID
EOF
chmod +x "$LAUNCHER"
ok "Launcher created at $LAUNCHER"

# ── Add to PATH if needed ────────────────────
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc" 2>/dev/null || true
    info "Added ~/.local/bin to PATH (restart terminal or run: source ~/.bashrc)"
fi

echo ""
echo -e "${GREEN}${BOLD}  Installation complete!${RESET}"
echo ""
echo -e "  Run it anytime with:  ${BOLD}ytdpro${RESET}"
echo -e "  Files are saved to:   ${BOLD}~/Downloads/ytdpro${RESET}"
echo -e "  Or:  ${BOLD}python3 $INSTALL_DIR/server.py${RESET}"
echo ""
