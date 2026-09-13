#!/bin/bash
# Double-click this in Finder. Records you saying "Mira" a few times, shows what
# faster-whisper actually hears, and tests whether a lower wake-word threshold
# would catch it.

REPO="/Users/aryavora/Desktop/Personal Projects/mira"
cd "$REPO" || exit 1

voice/.venv/bin/python -m voice.tools.diagnose_wake

echo ""
echo "Press any key to close this window."
read -n 1 -s
