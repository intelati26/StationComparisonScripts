# Ideas

What could still be looked at, roughly in order of value, with what is already known. Status: **open** (not tried), **partial** (some evidence), **blocked** (needs something first), **dropped** (tried, no gain: kept at the bottom so it is not repeated).
Numbers are from the autumn 2026 data (1 Oct - 19 Nov) unless said otherwise; "MAE" is the typical error of the night's low in F. See `EQUATION.md` for the equation and `RESEARCH.md` for the studies behind these numbers.

## 1. Seasons and out-of-sample (the data is autumn only)
- **Out-of-season retrospective run - open, highest value.** The station's Ambient history reaches back to at least Dec 2025, and URMA, HRRR and NBM archives go back years. A few nights a month through winter, spring and summer shows which terms drift, with no waiting. Free: URMA from the AWS files (about 2 s per hour), HRRR levels from Open-Meteo, NBM from the AWS archive; the station history through `ambient_history`.
  Hints that something drifts: the dewpoint bias is 0.6 F (NBM, summer) against 2.1 F (fall); the equation underpredicts cold nights (+1.1 F below 30 F) and overpredicts warm ones (-0.7 F above 60 F); a temperature term gained only 0.02 F in fall.
- **Seasonal amplitude - open.** Leaf-on or leaf-off, soil moisture and snow probably change a site's amplitude. `amp.py` run for a fortnight in each season gives it (14 nights is enough for about +-2 F).
- **Night length - open.** Fall nights are 11-14 h, summer nights about 9 h. The equation has a flat night (no time-of-night shape; the fitted amplitude of one was 0 in fall).
- **Snow - partial.** Six usable snow nights, none both clear and calm; the two clear, calm snow-pack nights (25 and 27 Jan) moved the deficit by -0.3 to +0.2 F against autumn. Needs clear, calm nights inside a pack (after a storm passes), from other airports and winters (nearest-airport snow days: 8-14 per winter).
- **A second Appalachian place - open.** The valley place gave the strongest terrain signal (amplitude vs elevation -0.98 over 7 stations). A second mountain place (a plateau cluster scanned at the same time had 9 stations spanning 1,100 ft) would show whether the rule repeats: about -3.8 F per 100 m above the valley floor, zero near 1,000 m.

## 2. Forecast inputs (where the end-to-end error comes from)
Forecast inputs cost 2.5 F against 0.8 F with analysis inputs. Keeping one input as a forecast while the others are analyses costs: dewpoint +0.68, cloud +0.39, temperature +0.33, wind aloft +0.26.
- **Dewpoint forecast - partial, biggest.** Anchoring to the station's evening reading and letting it fall 0.3 F/h took the low MAE from 2.3 to 1.6; the seasonal correction (1 F summer, 3 F fall, 2 F otherwise) is hard-coded in `termux/low.py`. Idea: fit the correction against lead time and season from more data; try the NBM's own hourly dewpoint with a lead-dependent shift (its bias grows with lead: +1.3 F at 1-5 h to +3.0 F at 10-16 h).
- **Cloud forecast - open.** The NBM runs about 4 points cloudier than URMA on average; the error by regime is not known. Try the HRRR's low, mid and high cloud separately (low cloud is what blocks the longwave loss most).
- **Probabilistic inputs - open.** The NBM has percentiles, HREF and GEFS give spread. Run the equation over a few members (or percentiles) and report a range that comes from the forecast's own uncertainty, not only from our error.
- **Which run to use - open.** HRRR runs hourly; the 22Z run was used for every test. Compare the evening runs (20Z-23Z), and the latest run at the time of the notification.

## 3. The equation
- **Column water vapor in place of the surface dewpoint spread - open.** The physical driver of clear-sky cooling is the whole column's water vapour (downwelling longwave); a 2 m temperature-minus-dewpoint spread is nearly zero on humid summer nights. HRRR precipitable water is available. Testable on the fall data now; expected to matter more in other seasons.
- **Regime-specific adjustment - open.** The "in between" nights (partial cloud, moderate wind aloft) have the widest error (MAE 3.1, sd 3.3) against clear and calm (1.9, sd 1.7). Check by air mass (post-frontal against warm sector) and by the HRRR's boundary-layer height or the strength of the inversion (a +0.38 correlation with what the equation misses was found, a 0.1 F gain: partial).
- **Cloud height and type - open.** Linear in cloud fraction beat squared and exponential forms; thin high cloud and low stratus differ physically.
- **Wind direction / fetch - partial.** Residuals by direction aloft: north -0.8, west +0.2 F; a separate north-sector threshold gained 0.01 F (cross-validated). The physical case (a longer fetch to a lower treeline on the north side) has no data of its own: a lidar-based obstruction height by direction would test it.

## 4. Predicting a site's amplitude without its data
Typical amplitude 5 F, the station's own amplitude lowers the mean MAE of 36 stations from 1.80 to 1.34 (1.13 with the offset). Terrain gave little in the lowlands.
- **Trees and buildings from 1 m lidar points - open.** The one physical predictor still untested at the right scale: sky view and obstruction height, by direction, from the point clouds (USGS 3DEP point data), where lidar exists. A coarse stand-in (30 m surface model minus the 8 m terrain) showed no relation.
- **Land cover and impervious area - open.** Suburban sites should pool less and run warmer.
- **Sensor shielding and height - open, hardware.** Part of a site's offset is probably the sensor (radiation shield, height above ground). A second sensor at a different height, or a comparison with an aspirated reference, would separate microclimate from instrument.
- **Valley topographic amplification factor - open.** Air volume in the valley against a plain of the same depth (Wagner, Whiteman): a geometric amplitude predictor from the terrain model.
- **Mountain rule - partial.** Height above the nearest valley floor works over 450 m of relief (one valley).
- **The offset - open.** Stations also differ by a fixed offset of mostly +-1.8 F (up to -6 F for one); is it siting, instrument or both?

## 5. Outputs and tools
- **A range and a bias correction in the notification - done 2026-11-20.** `termux/low.py` shows the predicted low +- 2.8 F (the SD held on both streams measured); the bias is a setting, default 0, because it differs between the NBM bulletin (+1.0 F warm) and Open-Meteo's HRRR history (1.9 F cold); `--log` keeps each night's run so the real bias of the live stream can be scored. See `EQUATION.md`. Still open: a spread that depends on the month (SD 2.4 in summer, about 3 in winter, from few nights) and on the wind aloft.
- **Frost probability - open.** The chance the low is at or below 32 F (or another threshold) from the same error distribution.
- **A per-hour dewpoint fall in `low.py` - open.** See section 2.
- **Scoring in the wild - partial.** `coldpool.py forecast` logs each run's prediction and the NWS hourly values (`log/forecasts.csv`, `log/nws_forecasts.csv`); score them against the station's actual low after a few weeks.
- **Phone - blocked on a phone.** `termux/low.py` and `low-temp.sh` are tested on a desktop only: the Termux:API location, the widget shortcut and how the notification folds its body are untried.
- **`spc925.py` hourly - open.** Run it from cron or Tasker on a night to collect SPC's 925 mb crops; SPC's HRRR and HREF viewers have no 925 mb winds (HREF has 850 mb only, and the app's HREF picture paths returned 404).
- **Desktop `forecast` on the 450 m wind - open.** The phone script and the live page use the wind 450 m above the ground; the desktop command still uses the 925 hPa wind.

## 6. Data and sources
- **GribStream** (URMA, RTMA, NBM at a point): free plan 1,200 credits a day, 1 credit per variable-hour; a quota endpoint is at `/api/v2/quota`.
- **Wunderground history:** the key was throttled after about 1,500 calls in a day (the limit is not documented in the common usage guide; check the key's page).
- **Ambient history paging:** the API answers an empty page after a few quick pages; wait and retry (done in `ambient_history`); a 3-day window loop does not reach old dates because the script pages from now.
- **Open-Meteo:** the history API times out for long ranges with many variables; `hrrr_levels` fetches 6-day chunks.
- **More places:** the same scan (random one-decimal points, 7 or more good stations within 11 km, a large station-elevation spread) can find more; each costs about 350-450 Wunderground calls and 550 URMA hours.

## 7. Housekeeping
- Roll the Ambient, GribStream, Wunderground and USGS keys (several were printed in a working session).
- Set the contact string in `coldpool.py`'s `UA` before using the NWS API.
- Keep places, station ids and keys out of the files: they live in `.env` and `log/` (git-ignored). Re-scan before each push for coordinates, station ids, keys and relative statements ("N km north of ...").

## Tried and dropped (so it is not repeated)
| idea | result |
|---|---|
| time-of-night shape (stronger early, decaying) | fitted amplitude 0 once the dewpoint term was in |
| temperature-level term | 0.02 F |
| north-sector wind threshold | 0.01 F |
| wind direction as a term | about 0 (the south-north component correlates +0.08 with the miss) |
| 3-hour maximum of the wind aloft | 1.68 F with analysis winds, gone with real forecast winds (1.83) |
| URMA's own dewpoint in the equation | no gain; the station's dewpoint helped 0.4 F |
| cap on the dryness term | no difference |
| additive combination of terms | 2.60 F against 1.87 F multiplicative |
| the station's own (sheltered) wind | no better than the open wind, and not forecastable |
| channel distance, drainage area, sky view from terrain, obstruction height as amplitude predictors | mixed signs between places, not reproduced |
| wind height (200-800 m above ground, 850/925 hPa) | flat from 300 to 600 m; in the mountain valley all variants within 0.06 F |
