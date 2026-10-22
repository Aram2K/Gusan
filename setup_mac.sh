#!/bin/bash
# setup_mac.sh — Install dependencies on macOS
set -e

echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "Installing dependencies..."
pip install -r requirements.txt --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu

echo ""
echo "Setup complete!"
echo ""
echo "IMPORTANT: The global shortcut needs two permissions on macOS."
echo "Go to: System Settings → Privacy & Security → Accessibility and Input Monitoring"
echo "Add Terminal (or your IDE) to both lists."
echo ""
echo "Run the app with:  bash run.sh"
