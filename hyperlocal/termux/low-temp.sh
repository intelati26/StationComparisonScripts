#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget shortcut (copy to ~/.shortcuts/): tap it, get tonight's low and the hour-by-hour adjusted temperatures as a notification.
# Needs: pkg install python termux-api.  Optional: pkg install matplotlib, to attach a picture (tapping it opens it full size).
#
# ONE place for everything: low.py, low_plot.py and the .env live in DIR; the pictures go to DIR/results/ (a dated file per tap, like the plotter's results folder; the last two weeks are kept) and the last result is DIR/.low_last.json. Set it here once (or export LOW_DIR).
# Use a folder in shared storage (run termux-setup-storage once) so you can edit .env from the phone's file manager and a viewer app can open the picture.
DIR="${LOW_DIR:-$HOME/storage/dcim/weather}"
[ -f "$DIR/low.py" ] || { [ -f "$HOME/low.py" ] && DIR="$HOME"; }       # older installs kept the files in ~

json="$DIR/.low_last.json"
out=$(python "$DIR/low.py" --notify --json-file "$json" "$@" 2>&1)
echo "$out"
act=()
if [ -s "$json" ] && [ -f "$DIR/low_plot.py" ] && python -c "import matplotlib" 2>/dev/null; then
  res="$DIR/results"; mkdir -p "$res"
  img=$(python "$DIR/low_plot.py" "$res/$(date +%Y-%m-%d-%H-%M)_low.png" --from "$json" 2>/dev/null | tail -1)
  find "$res" -name '*_low.png' -mtime +14 -delete 2>/dev/null          # only this script's own pictures, older than two weeks
  [ -n "$img" ] && act=(--image-path "$img" --action "termux-open '$img'" --button1 "Picture" --button1-action "termux-open '$img'")
fi
termux-notification --id tonight --title "Tonight: $(echo "$out" | head -1)" --content "$(echo "$out" | tail -n +2)" "${act[@]}" 2>/dev/null
