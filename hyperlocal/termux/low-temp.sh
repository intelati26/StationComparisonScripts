#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget shortcut (copy to ~/.shortcuts/): tap it, get tonight's low and the hour-by-hour adjusted temperatures as a notification.
# Needs: pkg install python termux-api.  Optional: pkg install matplotlib, and low_plot.py beside low.py in ~, to attach a picture of the forecast, the adjustment and the +- band.
out=$(python ~/low.py --notify "$@" 2>&1)
echo "$out"
img=""
if [ -f ~/low_plot.py ] && python -c "import matplotlib" 2>/dev/null; then
  img=$(python ~/low_plot.py "$@" 2>/dev/null | tail -1)
fi
termux-notification --id tonight --title "Tonight: $(echo "$out" | head -1)" --content "$(echo "$out" | tail -n +2)" ${img:+--image-path "$img"} 2>/dev/null
