#!/bin/bash
# Double-click this in Finder. Records you saying "Mira" 5 times and enrolls your voice,
# so Mira only wakes for you — not friends or family saying the same wake word.

REPO="/Users/aryavora/Desktop/Personal Projects/mira"
cd "$REPO" || exit 1

voice/.venv/bin/python -m voice.tools.enroll_speaker

echo ""
echo "Press any key to close this window."
read -n 1 -s
