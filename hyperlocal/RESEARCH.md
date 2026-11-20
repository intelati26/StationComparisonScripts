# Research notes: cold-air drainage and pooling

Kept with the single-site cold-pool work (`coldpool.py`, `analysis/terrain.py`, `analysis/terrain_local.py`). Written 2026-11-19 from web search results
and their summaries: **the papers themselves have not been read**, so every claim below is as far as a search result's
abstract or summary goes. Before citing one, open it. Nothing here is a number the model depends on.

## What the literature supports (direction, not our numbers)

- **Cold air collects in hollows.** On clear, calm nights a shallow layer next to the ground is cooled from below, the cold air drains
  downslope and settles in depressions ("frost hollows", "frost pockets"). Concave slopes are generally more frost prone than convex ones.
  Alberta Agriculture's frost-protection guide gives a field example: a basin about a mile across with a frost-free season about 75 days
  shorter than the crest two-thirds of a mile away, and notes that most damaging frosts come on clear, calm nights.
  <https://www1.agric.gov.ab.ca/$department/deptdocs.nsf/all/agdex9>
- **Small-scale drainage features regulate local cooling.** A fibre-optic (DTS) study found that airflow through small drainage
  features regulated the local cooling rate, with slope and distance along the drainage strongly influencing it.
  Search result: <https://par.nsf.gov/servlets/purl/10315187> (check the title and authors on opening).
- **Slope position, elevation and dissection predict nocturnal temperature.** An Idaho mountain study built a cold-air drainage
  index from elevation, slope position and dissection indices. This is the closest published match to a TPI-style position index.
  <https://escholarship.org/uc/item/9w99h5z7> (and a related USFS record, <https://research.fs.usda.gov/treesearch/39232>)
- **Mapping cold-air pooling automatically.** A published method finds places that get much colder minimums than the regional
  average on clear, calm nights, using a topographic amplification factor along valleys to tell pooling from drainage.
  <https://researchportal.port.ac.uk/en/publications/automated-algorithm-for-mapping-regions-of-cold-air-pooling-in-co/>
- **Local atmospheric decoupling.** Daly et al. (2009), Int. J. Climatology, on decoupling of the near-surface layer in
  complex terrain, the basis for PRISM's treatment of cold pools.
  <https://www.prism.oregonstate.edu/documents/pubs/2009intjclim_localAtmospheric_daly.pdf>
- **Long-running measurement networks.** The HJ Andrews Experimental Forest cold-air-drainage transects (2002 to present):
  <https://andrewsforest.oregonstate.edu/data/datacatalog/MS036/metadata-document>
- **A cold pool in a small valley is common and quantifiable.** In the UK COLPEX work a cold-pool night is one where the valley
  minimum is at least 1 C colder than the hilltops; the white-rose copy of the climatology paper is
  <https://eprints.whiterose.ac.uk/130494/1/COLPEX_climatology_weather_v3_submit.pdf>. For a citation, look for
  Jemmett-Smith, Ross and Sheridan (2018), *Weather*, on COLPEX cold pools in small valleys (named by the search, not opened).
- **Season matters.** An AMS 2018 poster reports topography matters less than synoptic forcing for pooling in winter, and the
  reverse in summer. <https://ams.confex.com/ams/98Annual/webprogram/Handout/Paper339828/AMS%202018_Poster_Ali%20Malek_Jan%2003.pdf>

## A caution from the same search

- A Bayreuth MSc thesis (Pfister) reports that predicting cold-air dynamics from micro-topography alone can fall short: valley-scale
  flows can override local terrain. <https://www.bayceer.uni-bayreuth.de/meteo/de/top/diss/138327/MSc_thesis_Lena_Pfister.pdf>
- A web article (wfy24.com) puts pooling at 10-20 C colder than ridges; that is far above the 1 C threshold used in the research
  literature above and is not an authoritative source. Not relied on.

## How our finding sits against it

Finding (first place, around the home station, 6 distinct WU sites plus the station, clear and calm nights, station minus URMA): the station, very close to a modelled drainage channel, runs coldest (-5.7 F), sites 112-237 m from a channel run warmer; correlation with distance to channel
+0.92, with position within 500 m (0 bottom, 1 top) +0.81. Elevation alone +0.50.

- **Consistent in direction:** hollow / drainage-line positions are colder; slope position beats elevation alone; small drainage
  features control local cooling. All three appear in the sources above.
- **Not established by them:** the specific measure. "Distance to a channel from D8 flow accumulation of 5 ha or more" is our own
  construction (the literature uses slope position / TPI, cold-air-drainage indices, topographic amplification factors). Seven sites, twelve
  indices tried, one chosen: that is a hint, not a result. The second place (place B, a random one-decimal point) is the
  test, with the two indices fixed beforehand.
- **To try next, from the literature:** slope position / TPI at several radii (done), plan and profile curvature (concave vs convex),
  a topographic amplification factor for the valley the site sits in, and a valley-scale term alongside the local one (the Bayreuth caution).

## Reference document supplied by the user (2026-11-19)

`Mountain_Valley_Cold-Air_Pools_and_Diurnal_Winds1.html`: a compiled guide (not a paper) citing COLPEX (Jemmett-Smith, Ross and Sheridan),
Rupp et al. (2020, H.J. Andrews), Colgan, Sun and Holmes (NOAA), Mount Washington Observatory's Hubbard Brook work, and Alexa (Innsbruck/DLR, TEAMx).
Its citations were not checked against the originals, and its "interactive model" equations are labelled educational (not validated). What it reports:

- **Sky clarity (COLPEX):** with Flw = LW_down / LW_up at 2 m, all cold-pool nights (> 1 C valley-minus-hilltop) had Flw < 0.93 at least 80% of the time;
  no strong pool (> 4 C) formed when Flw > 0.93. Cloud is the main brake.
- **Decoupling wind (COLPEX):** hilltop (30 m) winds below about 5 m/s (11 mph) let the valley floor decouple and drain down-valley; above that, mixing flushes weak pools.
- **How common:** pools in 45-56% of nights, strong ones in 12-20%, peaking 9.0 and 9.6 C across 160 m of relief on calm, snowy January nights; missed
  by 40 km station spacing and 1.5 km model grids (the case for a site-scale model).
- **Narrow valleys:** Rupp et al.: pooling far more frequent in 1-2 km valleys than basin-wide data suggested. Topographic amplification factor TAF = V_plain / V_valley
  (Wagner 1932, Whiteman 2000, Schmidli 2013): the same heat loss cools a smaller air volume more.
- **Dams and ceilings:** pool depth capped where inflow balances outflow (Hubbard Brook, ~145 m); road and rail embankments can dam drainage into frost pockets
  (attributed to Quenol et al. 2007, via Wikipedia). Snow cover strengthens pools. Radiosondes from elevated sites underestimate the valley heat deficit by > 30%.

### Our own data against it (single site, 1 Oct - 18 Nov 2026)

- **Wind threshold, clear nights (cloud < 20%, 300 hours; deficit = URMA minus station against URMA 10 m wind):**
  0-3 mph 6.2 F, 3-6 mph 5.8 F, 6-9 mph 5.0 F, 9-12 mph 2.1 F, above 12 mph 0.3 F. Flat to about 9 mph (4 m/s) then it collapses by 12 mph (5.4 m/s): a threshold
  near COLPEX's 5 m/s (their winds are 30 m hilltop, ours 10 m grid, so only the order of magnitude is comparable). The model's current wind factor is linear.
  A threshold form exp(-(U/U0)^p) with U0 = 10 mph, p = 3 fits slightly better (all hours rmse 1.87 F against 1.92 F; clear and calm nights 2.07 F against 2.18 F). Not adopted yet.
- **Cloud, URMA wind under 5 mph:** 0-20% cloud 5.9 F, 20-50% 3.6 F, 50-80% 2.5 F, over 80% 0.9 F: monotonic, in line with sky clarity as the main brake.
- **Snow cover:** the guide (and the earlier search) say it strengthens pools; the model has no snow term and the data span has no known snow nights to test it on.

## Our data: which wind controls the deficit (single site, 50 nights, 533 night hours; 2026-11-19)

Scripts: `analysis/wind_check.py`, `analysis/check_equation.py`, `analysis/hrrr_leads.py`. Deficit = URMA 2 m temperature minus the station's; the rest of the equation as fitted
(cloud weight 0.8, dewpoint spread scale 12 F). Cross-validated = leave-one-night-out.

- **Response curve is a threshold.** Clear hours, deficit / what the other terms predict, against URMA 10 m wind: about 1.0 up to 6 mph, 0.78 at 6-8,
  0.48 at 8-10, 0.20 at 10-12, 0.06 at 12-15 mph. Against HRRR wind at 925 hPa: about 1.0 up to 20 mph, 0.65 at 20-25, 0.43 at 25-30, 0.26 at 30-40,
  0.08 above 40 mph. This matches COLPEX's decoupling picture (valley floor decoupled below about 5 m/s hilltop wind, flushed above it).
- **The shape matters little.** exp(-(U/U0)^p) 1.89 F, logistic 1.90, hinge 1.90, straight line 1.98, no wind term 2.37 (cross-validated rmse).
- **The wind that is used matters more.** Cross-validated rmse with the exp(-(U/U0)^p) shape: URMA 10 m 1.89 F; URMA 10 m strongest of 3 h 1.85;
  HRRR 10 m 1.88; HRRR 925 hPa 1.82; **HRRR 925 hPa strongest of the last 3 h 1.68** (U0 30 mph, p 4); strongest of 6 h 1.76. The station's own
  sheltered wind is no better than the open wind (1.85 F in-sample, U0 2 mph) and cannot be forecast. Consistent with the research reading: the control is the
  flow above the valley, and a pool takes a few hours to erode, so a gust 2-3 hours ago still counts.
- **Caveat that matters (the user's point: HRRR runs every hour).** That 925 hPa series comes from Open-Meteo's historical-forecast API, which stitches the first hours of
  each hourly run: it is effectively HRRR's analysis, not a forecast at a lead. For a forecast made in the evening the wind comes from the latest run at a lead of
  1-15 h, so 1.68 F is an upper bound. Test under way: the 22Z run's 925 hPa wind at leads 0-15 h for each night, from NOAA's HRRR archive on AWS
  (`analysis/hrrr_leads.py`, `log/hrrr925_run22.json`), refitted with the same 3 h maximum. Results to be added here.
- Six wind variants were compared on one site: some of the gain may be selection even though cross-validated. Not yet adopted in the model.

## Second place: the channel-distance finding did not replicate (2026-11-19)

Test: place B (a random one-decimal point), 8 Weather Underground stations 3-10 km from the centre, station minus URMA at the centre on that
place's clear, calm nights (22 nights, URMA cloud < 20% and the stations' mean wind < 1 mph), indices from 3DEP 8 m terrain (`analysis/terrain_local.py`, `analysis/site_analysis.py`). The two indices fixed beforehand were distance to channel and relative position within 500 m.

| index (correlation with how warm the site runs vs URMA) | Home, 6 sites | place B, 8 sites | pooled, 14 (each place re-centred) |
|---|---|---|---|
| distance to channel (D8, 5 ha) | +0.89 | +0.11 | +0.34 |
| relative position within 500 m | +0.77 | +0.42 | +0.53 |
| TPI within 1 km | +0.49 | +0.50 | +0.40 |
| area draining to the site | +0.26 | -0.52 | -0.30 |

- **Channel distance does not carry over:** the three place-B sites within 30 m of a channel are cold (-4.6, -3.2, -1.8 F), but another cold site (a station, -4.5 F) is 265 m from one on flat ground, and the warmest site (+2.0 F) is 66 m from one. The the home place +0.9 came from few sites and many indices tried.
- **What both places share:** broad position. Sites lower than their surroundings within 1 km (TPI) and low within 500 m (relative position) run colder at both places, at +0.4 to +0.5 each, modest. Spread between stations 6.6 F at place B (about 9.8 F at the home place).
- Not adopted in the model. More places would be needed for a regression (about 40 stations); sensor siting and shielding noise is not removable here.

## Snow nights at the station, and our numbers beside the studies (2026-11-19)

**Snow test (`analysis/snow_nights.py`, `analysis/snow_effect.py`):** nights where the nearest airport reported snow in the previous 72 h and stayed at or below 40 F since:
9 nights in winter 2025-26 (1 Dec, 24-27 Jan, 30 Jan - 1 Feb, 15 Mar). Three of them (25, 27, 31 Jan) lost to station data gaps (2-4 hours of 14), leaving 6 nights.
Deficit (URMA minus station) on those 6: 0.2, -0.2, 0.5, 0.6, 0.3, 0.7 F. None was both clear and calm: cloud 58-81% on three, URMA wind 9-18 mph on four, the clearest
(26 Jan, 3% cloud) had 16 mph wind. The autumn equation overshoots by 0.9 F on average (worst 3.0 F on 1 Feb: 7% cloud, 9 mph wind, equation 3.3 F, actual 0.3 F).
**No evidence either way about snow strengthening pools** (the sample holds no clear, calm snow night); it does show the wind and cloud suppression holds in winter on these nights.
A fair test needs the clear, calm nights after a storm passes (probably the lost 25, 27, 31 Jan), from other stations or other airports/winters.

**Our autumn numbers against the COLPEX-type measures** (definitions differ: COLPEX is valley-floor minimum against hilltops over 100-150 m of relief; ours is the station against URMA's 2.5 km cell, hourly, 48 nights 1 Oct - 18 Nov):

| measure | COLPEX (UK, 9 months) | here |
|---|---|---|
| nights with a pool of more than 1 C | 45-56% | night-mean deficit above 1 C on 67%; some hour above 1 C on 79% |
| nights with a strong pool of more than 4 C | 12-20% | night-mean above 4 C on 8%; some hour above 4 C on 46% |
| largest | 9.0 and 9.6 C (calm, snowy January) | 7.8 C (14.1 F), one hour |
| decoupling wind | hilltop 30 m below about 5 m/s | deficit flat to about 9 mph (4 m/s) at URMA 10 m, gone by 12 mph (5.4 m/s) |
| clear sky needed | Flw < 0.93; no strong pool above it | 5.9 F at 0-20% cloud, 0.9 F above 80% |

Same order of magnitude throughout; the threshold agreement (about 5 m/s) is the closest.

## Third place and leave-one-place-out (2026-11-19)

Place C (a random one-decimal point), 9 Wunderground stations, 26 clear and calm nights, spread between stations 6.4 F (coldest a station -4.9 F at 1,126 ft, warmest a station +1.5 F at 1,286 ft).
The two indices fixed beforehand did not hold: channel distance +0.05, position within 500 m +0.11. Elevation +0.75 here (it was +0.50 at the home place, -0.05 at place B), TPI 1 km -0.17 (+0.49, +0.50).

Slope per standard deviation (F) of how warm a site runs against URMA, by place, and the leave-one-place-out error (fit on two places, predict the third; rmse F; no model: home 3.03, place B 2.24, place C 1.99, mean 2.42):

| index | Home | Place B | Place C | mean leave-one-place-out rmse |
|---|---|---|---|---|
| elevation | +0.95 | -0.12 | +1.51 | 2.43 |
| TPI 1 km | +1.47 | +1.12 | -0.33 | 2.49 |
| position within 500 m | +2.34 | +0.89 | +0.15 | 2.38 |
| height above lowest within 500 m | +1.48 | -0.71 | -0.20 | 2.58 |
| area draining to the site | +1.06 | -1.09 | -0.45 | 2.55 |
| distance to terrain channel | +2.68 | +0.25 | +0.12 | 2.53 |

**No single terrain index beats "no model" across places** (best 2.38 F against 2.42 F; most worse); the signs change from place to place. The home-place hints (channel distance, TPI) were
small-sample findings. Stations in one place differ by 6-10 F on clear, calm nights and the terrain indices here do not explain it; sensor siting and shielding, canopy and buildings are
untested candidates. Elevation matters at the high-relief place (place C) as the drainage picture says.

## The whole suite over 36 stations in five places (2026-11-20; `analysis/all_stations.py`)
Analysis inputs (URMA temperature and cloud at the place's centre, the HRRR's wind about 450 m above its ground, each station's own hourly dewpoint), 1,640 station-nights, scored on each station's nightly low.
Mean over stations of each station's mean absolute error (F) and how many stations improved on the forecast low alone:

| prediction | MAE | stations improved | bias |
|---|---|---|---|
| the forecast (URMA) low alone | 2.36 | - | +1.38 |
| equation, typical amplitude 5 F | 1.80 | 28 of 36 | +0.36 |
| equation, amplitude 10 F | 1.60 | 22 of 36 | -0.78 |
| equation, the station's own amplitude | 1.34 | 30 of 36 | +0.11 |
| equation, amplitude predicted from terrain | 1.73 | 28 of 36 | +0.25 |
| equation, own amplitude and offset | 1.13 | 32 of 36 | +0.15 |

By place (forecast | typical 5 | own): home 2.99 | 2.29 | 1.96; place B 1.50 | 1.33 | 0.99; place C 2.26 | 1.63 | 1.35; place D 1.36 | 1.05 | 1.07; the mountain valley 3.79 | 2.76 | 1.43.
Mountain valley place (7 stations, 650-1,101 m): the amplitude falls with station elevation, 12.9, 12.0, 10.6, 9.8, 4.4, -1.0, -2.0 F from the lowest to the highest (correlation -0.98 with elevation, about -3.8 F per 100 m, crossing zero near 1,015 m): valley floors pool, the higher stations sit in the warmer air above
(a thermal belt) and the equation does not apply to them. In the lowlands, with 30-180 ft of relief, the same elevation signal was weak or absent. Wind height tested there (7 stations): all variants within 0.06 F (local 700 m 1.77, 850 hPa 1.78, 450 m 1.78, 925 hPa 1.82, 10 m 1.83).

## Predicting a site's amplitude without its data (2026-11-20; `analysis/svf.py`, `analysis/all_stations.py`)
36 stations in five places, amplitude b = the station's own fit of (model - station) against the weather factor. Correlation within each place (re-centred) and pooled over the places:

| predictor | what it showed |
|---|---|
| height above the valley floor (the mountain valley, 7 stations, 450 m of relief) | -0.98, about -3.8 F per 100 m; the highest stations show no pooling at all. One valley only |
| the same in the lowlands (30-180 ft of relief) | weak or absent |
| position within 500 m, TPI within 1 km | pooled about -0.55 (same sign in all four lowland places); predicting a held-out place helps at 14-15 of 29 sites, 90% range touching zero |
| area draining to the site, distance to a channel (terrain or mapped) | mixed signs between places; not replicated |
| sky view from the terrain (16 directions, 300 m to 2 km) | -0.48 to +0.58 by place, pooled -0.2: no consistent effect (the spread of sky view among the stations of a lowland place is only 0.03-0.04) |
| obstruction height (30 m surface model minus the 8 m terrain, within 100-500 m) | pooled +0.05 to +0.19, signs mixed: no relation at this resolution |

Not tested: trees and buildings from lidar points, land cover and impervious area, soil moisture and nearby water, the shielding and height of the sensor, the fetch in each direction (treeline), a valley topographic amplification factor.
So: no dependable physical predictor of a site's amplitude from terrain alone apart from the large-relief rule; the typical 5 F or two weeks of the station's own data (`analysis/amp.py`) are the practical choices.
