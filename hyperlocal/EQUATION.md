# The cold-pool equation, useful terms only (2026-11-19)

The home station, 1 Oct - 18 Nov 2026 (49 nights, 531 night hours). Deficit = what URMA's 2.5 km cell reads minus the station, in F. Every term below was kept because
removing it made the leave-one-night-out error worse; everything else was tried and dropped.

    low(t) = NWS forecast temperature(t) - drop(t)

    drop(t) = A  x  N(t)  x  W(U925)  x  (1 - 0.8 x cloud/100)  x  min(1, (T - Td) / 12)

| term | form | value | why it stays (cross-validated error without it) |
|---|---|---|---|
| A | maximum drop | 10 F | the scale |
| N(t) night | 0 by day; ramps 0 to 1 over the 2 h after sunset; 1 until sunrise; ramps down over 2 h | | structural: the effect exists only at night |
| W wind | exp(-(U / 30 mph)^3), U = the wind about 450 m above the ground (the HRRR's pressure levels interpolated in height; 925 hPa where the ground is near 300 m) | threshold about 30 mph aloft | 2.37 F without any wind term; 1.71 F with the 925 hPa forecast wind against 1.89 F with URMA's 10 m wind and 1.98 F with HRRR's 10 m wind |
| cloud | 1 - 0.8 x cloud fraction | linear | 2.35 F without it; linear beats squared and exponential |
| dryness | min(1, (T - Td) / 12 F); T the forecast temperature, Td the station's dewpoint read in the evening | 12 F | 2.25 F without it (URMA's dewpoint does not help; the station's own does) |

Fit error 1.71 F cross-validated (hours with real forecast leads from the 22Z run), against 3.2 F for guessing the mean deficit.

## Tried and dropped
- relief factor 1 + (rim - floor)/3000: a constant at one site, absorbed in A
- time-of-night shape (1 + amp x exp(-hours/tau)): fitted amplitude 0
- temperature-level term: 0.02 F gain
- shelter ratio and the station's own wind: no better than the open wind and not forecastable
- the 3-hour maximum of the wind: gain gone with real forecast winds
- cap on the dryness term: no difference (either form works); additive combination: 2.60 F against 1.87 F
- snow term: no clear, calm snow-pack night in the data to fit it (two nights, change from autumn -0.3 to +0.2 F)

## Across sites (candidate, not part of the equation yet)
Each site scales the same weather factor by its own amplitude b (the station's A is about 10 F; 23 stations in 3 places range from about 0 to 12 F, mean 5.0, sd 3.3), plus a small fixed offset
(mostly within +-1.8 F; siting or instrument). Amplitude is lower on higher ground: correlation with TPI within 1 km -0.74, -0.63, -0.52 in the three places (pooled -0.60), with position within 500 m -0.66, -0.52, -0.69.
Predicting a held-out place's amplitudes from the other two helps at 13 of 23 sites (mean squared error -3 F2, 90% range -7 to +0.6): suggestive, not proven.

## End to end, on real forecast inputs (45 nights, 1 Oct - 18 Nov 2026)

Inputs from the 22Z NBM run (temperature, dewpoint, cloud) and the 22Z HRRR run (925 hPa wind), scored on the station's lowest hourly temperature each night (`end_to_end.py`).

| prediction | MAE | bias |
|---|---|---|
| the forecast (NBM) low alone | 5.1 F | +4.7 |
| equation, evening dewpoint held at the station's reading | 3.2 | +2.5 |
| equation, dewpoint falling 0.3 F/h from the station's evening reading | 2.6 | +1.5 |
| equation, NBM hourly dewpoint | 3.7 | +2.9 |
| equation, NBM dewpoint shifted to the station's evening reading | 2.5 | +1.3 |
| equation, NBM dewpoint less 2 / 3 / 5 F | 3.1 / 2.9 / 2.7 | +2.0 / +1.6 / +0.8 |
| equation with the NBM 10 m wind (threshold 10 mph) in place of the 925 hPa wind | 2.6 (3.1 with 2 F off the dewpoint) | +1.4 |
| mental rule (evening dewpoint) | 3.3 | +2.1 |
| equation with the analysis inputs (URMA T and cloud, actual hourly dewpoint) | 1.1 | +0.6 |

NBM dewpoint minus the station's: +2.1 F in fall (45 nights), +0.6 F in summer (87 hours, 11 nights). NBM temperature minus URMA: +0.3 F, rmse 2.0 F. NBM cloud minus URMA: +4 points.
The gap between 1.1 F (analysis inputs) and 2.5-2.6 F (forecast inputs) is the forecast's own error; the 925 hPa wind's advantage in the hourly fit (1.71 against 1.98 F) does not show in the nightly low (2.6 F either way).
Best simple settings found: anchor the dewpoint to the station's evening reading (or take 3-5 F off the forecast's), falling 0.2-0.3 F/h; chosen on these nights, so slightly optimistic.

## The wind level, by height above the ground (2026-11-19)
Winds interpolated to a fixed height above the ground (HRRR pressure levels with their geopotential heights, the point's elevation, and the 10 m wind), 533 night hours, leave-one-night-out error with the threshold form refitted each time:
100 m 1.97 F, 200 m 1.77, 300 m 1.72, **400 m 1.69, 500 m 1.68**, 600 m 1.70, 800 m 1.84, 1000 m 1.79; the nearest pressure level to 400 m 1.70; the fixed 925 hPa level 1.69 (it is about 520 m above the ground at this site).
Pressure levels alone: 10 m 2.03, 950 hPa 1.74, 925 1.69, 900 1.83, 850 1.87, 800 2.13, 700 2.34. The threshold is the same across the flat range (about 28-29 mph, exponent 3).
So the wind to use is the wind about 450 m above the ground, which stays defined over high terrain, where 925 hPa is underground (`termux/low.py` and `maps/tonight_live.html` interpolate it; checked at a plains point and at a point near 1,600 m).

## How far to trust the low (2026-11-20)
The equation with A = 10 and analysis inputs, over every night of the record (647 nights, Feb 2025 - Nov 2026, the night's lowest hour against the station's): the typical miss is 1.1 F, the standard deviation 1.6 F.
By part of the year: Apr-Jul and Oct-Nov 0.8 F typical miss (SD 1.1, bias +0.05); Aug, Sep, Dec, Mar 1.25 F (SD 1.8, bias -0.4); Jan-Feb 1.8 F (SD 2.3, bias -1.0). The two years agree month by month except September (0.76 F in 2025, 1.75 F in 2026).
With a real forecast in (the 19Z NBM text bulletin, NBS, interpolated to hourly, 287 nights; the wind aloft still the analysis wind): the predicted low runs **1.0 F warm** (mean error +0.98 F), standard deviation **2.8 F**, typical miss 2.4 F. By season: winter 3.1 (14 nights), spring 3.0, summer 2.4, autumn 2.6; by month 1.6 to 3.6. The forecast adds about 2.3 F in quadrature to the equation's own 1.65 F. The NBS low alone: +3.8 F warm, SD 3.7, typical miss 4.3 F.
So `termux/low.py` reports **the predicted low minus 1 F, +- 2.8 F** (`--bias`, `--sd`, or HYPERLOCAL_BIAS and HYPERLOCAL_SD in .env); the hourly table is left as the equation gives it.
