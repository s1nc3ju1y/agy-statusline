#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${AGY_DATA_DIR:-$HOME/.gemini/antigravity-cli}/plugins/agy-statusline"
REPO_RAW="https://raw.githubusercontent.com/s1nc3ju1y/agy-statusline/main"

mkdir -p "$INSTALL_DIR" "$HOME/.local/bin"

if [ -f "./agy_statusline.py" ]; then
  cp "./agy_statusline.py" "$INSTALL_DIR/agy_statusline.py"
else
  curl -fsSL "$REPO_RAW/agy_statusline.py" -o "$INSTALL_DIR/agy_statusline.py"
fi

chmod +x "$INSTALL_DIR/agy_statusline.py"
python3 "$INSTALL_DIR/agy_statusline.py" --install "$@"

echo ""
echo "✓ agy-statusline installed successfully!"
echo "  Switch presets anytime inside agy:"
echo "    /statusline off"
echo "    /statusline 简洁    (or /statusline minimal)"
echo "    /statusline 标准    (or /statusline standard)"
echo "    /statusline 详细    (or /statusline detailed)"
