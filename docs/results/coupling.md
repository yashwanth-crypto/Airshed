# Coupling proof — does pollution improve the weather forecast?

The problem statement asks for a coupled system. Weather-to-pollution is already in the model. This measures the hard direction: whether knowing Delhi's aerosol load improves a *weather* forecast — visibility — that the physics model produces without it.

- 28,616 training hours, 5,904 held-out hours (2025-12-12 to 2026-06-30)
- median observed visibility 2.5 km; 1217 hours below 1 km
- weather-only model sees 88 features, pollution-informed sees 130


## Visibility RMSE by horizon (km, lower is better)

| model | 24 h | 48 h | 72 h | overall |
|---|---|---|---|---|
| model-visibility | 21.27 | 21.28 | 21.30 | 21.28 |
| persistence | 1.09 | 1.17 | 1.29 | 1.19 |
| weather-only | 1.11 | 1.16 | 1.28 | 1.19 |
| pollution-informed | 1.02 | 1.08 | 1.18 | 1.10 |

## Low-visibility recall (observed below 1 km)

The hours that matter for disaster management: flight diversions at IGI, highway pile-ups on the NH-44 and NH-48 corridors.

| model | hours below 1 km | recall |
|---|---|---|
| model-visibility | 1217 | 0.0% |
| persistence | 1217 | 58.7% |
| weather-only | 1217 | 1.8% |
| pollution-informed | 1217 | 0.6% |

## Fog alarms from the distribution, not the median

Thresholding a median forecast is a poor alarm: it regresses to the mean and so rarely dips below 1 km, whatever its RMSE — which is why the recall column above reads near zero for both correctors. Firing instead on P(visibility < 1 km) uses the whole predicted distribution, the same treatment the GRAP layer gives stage thresholds. Missing a fog event costs a diverted flight; a false alarm costs a cautious one.

| model | alarm at P >= | recall | precision | false alarm rate |
|---|---|---|---|---|
| weather-only | 0.05 | 89.2% | 40.9% | 33.5% |
| weather-only | 0.10 | 48.6% | 63.9% | 7.1% |
| weather-only | 0.20 | 33.9% | 68.3% | 4.1% |
| weather-only | 0.30 | 18.7% | 71.9% | 1.9% |
| weather-only | 0.50 | 1.8% | 73.3% | 0.2% |
| pollution-informed | 0.05 | 83.0% | 43.2% | 28.4% |
| pollution-informed | 0.10 | 42.6% | 68.8% | 5.0% |
| pollution-informed | 0.20 | 25.8% | 77.0% | 2.0% |
| pollution-informed | 0.30 | 9.9% | 81.6% | 0.6% |
| pollution-informed | 0.50 | 0.6% | 77.8% | 0.0% |

## The physical test: does the gain grow with pollution?

If this is aerosol-driven coupling, the improvement must concentrate where the aerosol is. If it were merely a larger feature set, it would not care how dirty the air was.

| observed PM2.5 | hours | median visibility | model | weather-only | **pollution-informed** | gain |
|---|---|---|---|---|---|---|
| clean (<60) | 1,734 | 4.5 km | 19.45 | 0.94 | **0.90** | +4.0% |
| moderate (60-120) | 852 | 2.5 km | 21.24 | 1.25 | **1.18** | +5.6% |
| poor (120-250) | 1,400 | 1.4 km | 22.48 | 1.30 | **1.23** | +6.0% |
| severe (>250) | 640 | 1.0 km | 22.96 | 1.14 | **1.08** | +5.0% |

## Verdict

Raw physics visibility: **21.28 km** RMSE. Correcting it with weather alone: **1.19 km**. Adding pollution: **1.10 km** (+7.5% against weather-only).

**Pollution information improves the weather forecast.** That is the coupled direction the problem statement asks for, stated as a number on held-out data rather than asserted from architecture.

**But the mechanism test does not pass.** The gain is roughly uniform across pollution bands (+4.0% in clean air, +5.0% in the dirtiest, spread 1.9%), so this looks like the pollution columns carrying general information rather than aerosol driving visibility. The RMSE improvement is real and reportable; **the coupling claim is not established by it.** Quote the headline number, not a physical mechanism, until the gain concentrates.
