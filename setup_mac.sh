#!/bin/bash
# setup_mac.sh — Install dependencies on macOS
set -e

echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "Installing dependencies..."
pip install -r requirements.txt --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu

echo "Building the Interpreter's call-sound helper..."
mkdir -p "$HOME/Library/Application Support/Transcribe/bin"
swiftc -O -o "$HOME/Library/Application Support/Transcribe/bin/gusan-tap" interpreter/mac/gusan_tap.swift 2>/dev/null \
  || echo "  (skipped: needs Xcode's command line tools - xcode-select --install)"

echo ""
echo "Setup complete!"
echo ""
echo "IMPORTANT: The global shortcut needs two permissions on macOS."
echo "Go to: System Settings → Privacy & Security → Accessibility and Input Monitoring"
echo "Add Terminal (or your IDE) to both lists."
echo "The Interpreter also asks for System Audio Recording (to hear the call), and for Speak"
echo "needs BlackHole as the call's microphone:  brew install --cask blackhole-2ch"
echo ""
echo "Run the app with:  bash run.sh"
