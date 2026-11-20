#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget shortcut (copy to ~/.shortcuts/): tap it, get tonight's low and the hour-by-hour adjusted temperatures as a notification.
# Needs: pkg install python termux-api.  Optional: pkg install matplotlib, and low_plot.py beside low.py in ~, to attach a picture of the forecast, the adjustment and the +- band.
# One Open-Meteo request serves both: low.py saves its result, low_plot.py draws from that file.
json=~/.low_last.json
out=$(python ~/low.py --notify --json-file "$json" "$@" 2>&1)
echo "$out"
img=""
if [ -s "$json" ] && [ -f ~/low_plot.py ] && python -c "import matplotlib" 2>/dev/null; then
  img=$(python ~/low_plot.py --from "$json" 2>/dev/null | tail -1)
fi
act=()
if [ -n "$img" ]; then
  # the viewer app cannot read Termux's private folder ("does not have permission to open"): open a copy in shared storage, in the weather folder with the .env if there is one (run termux-setup-storage once to create ~/storage)
  show="$img"
  dest=~/storage/dcim/weather
  [ -d "$dest" ] || dest=~/storage/shared/Pictures
  if [ -d ~/storage/shared ]; then
    mkdir -p "$dest" && cp "$img" "$dest/low.png" && show="$dest/low.png"
  else
    echo "run termux-setup-storage once (and allow it) so tapping the notification can open the picture"
  fi
  act=(--image-path "$img" --action "termux-open '$show'" --button1 "Picture" --button1-action "termux-open '$show'")
fi
termux-notification --id tonight --title "Tonight: $(echo "$out" | head -1)" --content "$(echo "$out" | tail -n +2)" "${act[@]}" 2>/dev/null
