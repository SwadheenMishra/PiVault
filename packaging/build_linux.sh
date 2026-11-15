#!/bin/sh
# Builds dist/PiVault-linux-<arch> (a single file) for the machine you run this on.
#   Linux PC (x86_64)  -> dist/PiVault-linux-x86_64
#   Raspberry Pi 64-bit -> dist/PiVault-linux-aarch64
# A Linux build only runs on the same kind of CPU, so build the Pi version ON a Pi.
# Run:  sh packaging/build_linux.sh
set -e
cd "$(dirname "$0")/.."
ARCH=$(uname -m)
python3 -m venv .build-venv || { echo "Could not create a virtual environment. On Debian/Ubuntu/Raspberry Pi OS try:  sudo apt install python3-venv"; exit 1; }
. .build-venv/bin/activate
pip install -q -r requirements.txt pyinstaller
pyinstaller --onefile --name "PiVault-linux-$ARCH" --paths server --add-data "webui:webui" server/server.py
rm -rf .build-venv build "PiVault-linux-$ARCH.spec"
echo
echo "Done. Your program is dist/PiVault-linux-$ARCH"
echo "Run it with:  chmod +x dist/PiVault-linux-$ARCH && ./dist/PiVault-linux-$ARCH"
