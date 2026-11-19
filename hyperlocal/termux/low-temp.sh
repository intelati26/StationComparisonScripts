#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget shortcut (copy to ~/.shortcuts/): tap it, get the low as a notification. Needs: pkg install python termux-api
out=$(python ~/low.py "$@" 2>&1)
echo "$out"
termux-notification --title "Tonight's low" --content "$(echo "$out" | head -1)" 2>/dev/null
