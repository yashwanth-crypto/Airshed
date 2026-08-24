# Coupling proof — does pollution improve the weather forecast?

The problem statement asks for a coupled system. Weather-to-pollution is already in the model. This measures the hard direction: whether knowing Delhi's aerosol load improves a *weather* forecast — visibility — that the physics model produces without it.

- 28,544 training hours, 5,904 held-out hours (2025-12-12 to 2026-06-30)
- median observed visibility 2.5 km; 1217 hours below 1 km
- weather-only model sees 80 features, pollution-informed sees 122


## Visibility RMSE by horizon (km, lower is better)

| model | 24 h | 48 h | 72 h | overall |
|---|---|---|---|---|
| model-visibility | 21.27 | 21.29 | 21.30 | 21.28 |
| persistence | 1.09 | 1.17 | 1.29 | 1.19 |
| weather-only | 1.12 | 1.14 | 1.29 | 1.19 |
| pollution-informed | 1.01 | 1.06 | 1.15 | 1.08 |

## Low-visibility recall (observed below 1 km)

The hours that matter for disaster management: flight diversions at IGI, highway pile-ups on the NH-44 and NH-48 corridors.

| model | hours below 1 km | recall |
|---|---|---|
| model-visibility | 1217 | 0.0% |
| persistence | 1217 | 58.7% |
| weather-only | 1217 | 0.2% |
| pollution-informed | 1217 | 0.7% |

## Fog alarms from the distribution, not the median

Thresholding a median forecast is a poor alarm: it regresses to the mean and so rarely dips below 1 km, whatever its RMSE — which is why the recall column above reads near zero for both correctors. Firing instead on P(visibility < 1 km) uses the whole predicted distribution, the same treatment the GRAP layer gives stage thresholds. Missing a fog event costs a diverted flight; a false alarm costs a cautious one.

| model | alarm at P >= | recall | precision | false alarm rate |
|---|---|---|---|---|
| weather-only | 0.05 | 84.1% | 44.9% | 26.8% |
| weather-only | 0.10 | 44.1% | 65.0% | 6.2% |
| weather-only | 0.20 | 30.0% | 69.8% | 3.4% |
| weather-only | 0.30 | 16.1% | 76.0% | 1.3% |
| weather-only | 0.50 | 0.2% | 50.0% | 0.1% |
| pollution-informed | 0.05 | 91.0% | 41.9% | 32.8% |
| pollution-informed | 0.10 | 46.8% | 65.9% | 6.3% |
| pollution-informed | 0.20 | 30.2% | 73.0% | 2.9% |
| pollution-informed | 0.30 | 14.6% | 80.9% | 0.9% |
| pollution-informed | 0.50 | 0.7% | 90.0% | 0.0% |

## The physical test: does the gain grow with pollution?

If this is aerosol-driven coupling, the improvement must concentrate where the aerosol is. If it were merely a larger feature set, it would not care how dirty the air was.

| observed PM2.5 | hours | median visibility | model | weather-only | **pollution-informed** | gain |
|---|---|---|---|---|---|---|
| clean (<60) | 1,614 | 4.5 km | 19.44 | 0.91 | **0.91** | +0.3% |
| moderate (60-120) | 975 | 2.9 km | 21.04 | 1.23 | **1.14** | +7.2% |
| poor (120-250) | 1,430 | 1.4 km | 22.49 | 1.31 | **1.19** | +8.9% |
| severe (>250) | 607 | 0.9 km | 22.97 | 1.14 | **1.04** | +8.3% |

## Verdict

Raw physics visibility: **21.28 km** RMSE. Correcting it with weather alone: **1.19 km**. Adding pollution: **1.08 km** (+9.5% against weather-only).

**Pollution information improves the weather forecast.** That is the coupled direction the problem statement asks for, stated as a number on held-out data rather than asserted from architecture.
