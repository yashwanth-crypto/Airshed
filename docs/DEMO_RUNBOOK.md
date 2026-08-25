# Demo runbook

For the internal hackathon. Read once the night before; keep open on your phone
on the day.

---

## Before you leave the house

```
scripts\run_dashboard.bat
```

Double-click it. A black window opens, the browser opens at
`http://localhost:8018`, and the page loads with the replay already run.
**Leave the black window open** — closing it stops the dashboard.

Check three things and you are ready:

1. The **Live forecast** table has three rows with numbers, not "loading…".
2. The **Historical replay** chart has drawn, and the line under it says
   **"Held-out day — never seen in training"**.
3. **Cache status** has no red "Archive stalled" banner.

Takes about 20 seconds from double-click to a loaded page. Do this once at home
before the day, not for the first time in the room.

---

## The 3-minute pitch

**Open on the thesis, not the architecture.**

> "Delhi already has a physics forecast. It's wrong in a *learnable* way. So we
> correct it instead of trying to rebuild it."

**Then point at the replay chart, which is already on screen.**

> "This is 22 December — a severe episode our model has never seen; it's a
> held-out day. The grey line is the free physics forecast, CAMS. The white line
> is what actually happened. The blue line is us. CAMS was 122 µg/m³ off. We
> were 91. And we do that at 24, 48 and 72 hours."

**Then the decision, which is the actual product.**

> "But a number isn't a decision. GRAP is what closes schools and stops
> construction. So we output the *probability* each GRAP stage is reached, and
> we catch Stage III with 72 hours of lead time. That's the difference between a
> forecast and a warning."

**Then the credibility move — this is your differentiator.**

> "We report persistence in every table, because 'tomorrow is like today' beats
> most published air-quality models. We beat it on 5 of 5 seasonal folds. And
> three things we tried didn't work — the upwind corridor, the coupled
> multi-output model, and fires helped less than the physics says they should.
> They're all in our results with the numbers."

Most teams show only what worked. Reporting what didn't is what makes the rest
believable, and it is the single strongest thing you have.

---

## Numbers to know cold

| claim | number |
|---|---|
| vs raw CAMS | **+31.5%** RMSE, 5 of 5 folds |
| vs persistence | **+20.6%** RMSE, 5 of 5 folds |
| The held-out episode on screen | model **91**, CAMS **123**, observed **259** |
| GRAP Stage III | **74%** recall at **66%** precision, 72 h lead |
| Spatial, leave-one-station-out | **81.8 µg/m³** |
| Stations | **77** across Delhi NCR, plus 24 upwind |
| Cost of every data source | **zero** |

---

## Questions you will get

**"MoES already funds SAFAR and the Decision Support System. Why you?"**
> "We don't replace the physics model, we correct it. Here's the bias in the
> operational forecast, here's our correction, here's the RMSE reduction with
> error bars across five seasons — and the same method applies to their model as
> easily as to CAMS."

**"Was that day in your training data?"**
> "No — the dashboard says so on screen. It picks the dirtiest day inside a
> held-out block automatically."

**"How accurate is the map?"**
> "Honestly? 82 µg/m³ held-out error against an observed mean of 167. It's good
> enough to say which part of the city is worse this morning. It is not good
> enough to quote a number for an unmonitored block, and we don't let the
> interface pretend otherwise."
>
> Say this plainly. Volunteering a limitation is worth more than the limitation
> costs.

**"Does pollution affect the weather too?"**
> "We measured it on visibility at the airport. Adding pollution improves the
> visibility forecast by 7.5% on held-out data. But we can't yet claim it's the
> aerosol physics — the gain doesn't concentrate in dirty air the way the
> mechanism predicts, so we report the number and say the mechanism is
> unresolved."
>
> **Do not claim the mechanism.** Three separate checks declined to support it.

**"What's missing?"**
> "One winter of trainable data. Everything we can't resolve — whether fires
> help, whether the upwind corridor helps — needs a second episode season, and
> that's November. Not more features."

---

## If something breaks

**Page won't load / "can't reach this page"**
The server isn't running. Double-click `scripts\run_dashboard.bat`. If the black
window flashes and closes, the port is already taken — just open
`http://localhost:8018`.

**Forecast row says an error**
Skip it and demo the replay. The replay is the stronger half anyway: it shows a
severe episode instead of monsoon air, and it's scored against ground truth.

**Chart is empty**
Pick a date and press Replay. Good held-out episode days: **2025-12-22**,
2025-12-27, 2025-12-30.

**Nothing works at all**
Show the overview page — `docs/airshed-overview.html`, opens in any browser with
no server — and talk from that. It carries every result.

**Never** open a terminal and start debugging in front of judges. Fall back to
the overview page and keep talking.

---

## What not to say

- Don't quote 48 h or 72 h skill without the caveat that archived meteorology is
  short-lead — we measured that at 0.9% and it's in the results.
- Don't claim the aerosol-coupling mechanism.
- Don't claim neighbourhood-level accuracy from the map.
- Don't say "AI" or "deep learning". It's gradient-boosted trees correcting a
  physics model, and saying so precisely reads as competence.

---

## After the internal round

In priority order: keep the archive job alive through November (it cannot be
backfilled), then re-run every result once the second episode season lands.
`docs/STATUS.md` has the full list.
