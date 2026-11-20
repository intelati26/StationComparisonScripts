#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget shortcut (copy to ~/.shortcuts/): tap it, get tonight's low and the hour-by-hour adjusted temperatures as a notification. Needs: pkg install python termux-api
out=$(python ~/low.py --notify "$@" 2>&1)
echo "$out"
termux-notification --id tonight --title "Tonight: $(echo "$out" | head -1)" --content "$(echo "$out" | tail -n +2)" 2>/dev/null
