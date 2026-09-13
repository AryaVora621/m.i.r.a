#!/bin/bash
# Double-click this in Finder to start everything needed for Task 15
# manual verification: guardrail, daemon, and the voice loop, each in
# its own Terminal window, plus the live FSM tester in your browser.

REPO="/Users/aryavora/Desktop/Personal Projects/mira"

osascript <<EOF
tell application "Terminal"
    activate
    do script "cd \"$REPO\" && echo '--- mira-guardrail ---' && cargo run --bin mira-guardrail"
    delay 1
    do script "cd \"$REPO\" && echo '--- mira-daemon ---' && cargo run --bin mira-daemon"
    delay 2
    do script "cd \"$REPO\" && echo '--- voice loop (say Mira to wake) ---' && voice/.venv/bin/python -m voice.main"
end tell
EOF

sleep 1
open "$REPO/voice/tester/index.html"

echo ""
echo "Started: mira-guardrail, mira-daemon, voice.main (3 Terminal windows)."
echo "Tester UI opened in your browser."
echo ""
echo "Checklist:"
echo "  1. Say 'Mira' -> confirm listening starts"
echo "  2. Say 'set the volume to forty' -> confirm real volume changes + spoken confirmation"
echo "  3. Interrupt mid-reply -> confirm barge-in stops speech"
echo "  4. Kill the mira-daemon window (Ctrl-C) -> confirm voice loop goes OFFLINE, not a crash"
echo ""
echo "Close the 3 Terminal windows when done. Press any key to close this launcher window."
read -n 1 -s
