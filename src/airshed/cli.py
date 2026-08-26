"""Command line entry point."""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

import polars as pl
import typer
from rich.console import Console
from rich.table import Table

from . import grap as grap_mod
from . import store
from .config import load_config
from .env import load_dotenv
from .keepawake import keep_awake
from .features import build as feat
from .eval import ablation as ablation_mod
from .eval import camsoffset as camsoffset_mod
from .eval import leadmatch as leadmatch_mod
from .eval import grap_eval
from .eval import loso as loso_mod
from .eval import rolling as rolling_mod
from .eval import visibility as vis_mod
from .features import splits as split_mod
from .ingest import cams, cpcb, fires, kaggle_history, metar, meteo, repair

app = typer.Typer(add_completion=False, help="Airshed — Delhi NCR air quality forecasting.")
ingest_app = typer.Typer(help="Fetch source data into the local Parquet store.")
cpcb_app = typer.Typer(help="CPCB ground-truth utilities.")
app.add_typer(ingest_app, name="ingest")
app.add_typer(cpcb_app, name="cpcb")

console = Console()


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    load_dotenv()
    # httpx logs every request URL at INFO, and FIRMS puts the API key in the
    # URL *path*. Left on, the daily job would write the key into
    # data/archive.log on every run.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------
@ingest_app.command("cams")
def ingest_cams(
    start: str = typer.Option(None, help="YYYY-MM-DD"),
    end: str = typer.Option(None, help="YYYY-MM-DD"),
    live: bool = typer.Option(False, help="Archive today's forecast run instead of backfilling."),
) -> None:
    """CAMS PM2.5 — the physics forecast we correct."""
    if live:
        paths = cams.archive_run()
        console.print(f"[green]archived CAMS run[/] -> {len(paths)} partition(s)")
        return
    _require_range(start, end)
    paths = cams.backfill(start, end)
    console.print(f"[green]cams_archive[/] {start}..{end} -> {len(paths)} partition(s)")


@ingest_app.command("meteo")
def ingest_meteo(
    start: str = typer.Option(None),
    end: str = typer.Option(None),
    live: bool = typer.Option(False, help="Archive the live serving run instead of backfilling."),
) -> None:
    """Forecast meteorology. Training reads archived forecasts, never reanalysis (R1)."""
    if live:
        paths = meteo.archive_run()
        console.print(f"[green]archived meteo run[/] -> {len(paths)} partition(s)")
        return
    _require_range(start, end)
    paths = meteo.backfill(start, end)
    console.print(f"[green]meteo_archive[/] {start}..{end} -> {len(paths)} partition(s)")


@ingest_app.command("meteo-leadmatched")
def ingest_meteo_leadmatched(
    start: str = typer.Option(...), end: str = typer.Option(...)
) -> None:
    """Meteorology at the lead it will actually be used at (Previous Runs API).

    `meteo_archive` holds the best available forecast for each past hour, which
    is a short-lead one — so a 72 h score computed from it is not a 72 h score.
    This caches, for each valid hour, the forecast from the run 1, 2 and 3 days
    earlier. BLH and the pressure-level variables have no such form and are
    unaffected; see `lead_matched_unavailable` in config.toml.
    """
    paths = meteo.backfill_previous_runs(start, end)
    console.print(
        f"[green]meteo_leadmatched[/] {start}..{end} -> {len(paths)} partition(s)"
    )


@ingest_app.command("metar")
def ingest_metar(start: str = typer.Option(...), end: str = typer.Option(...)) -> None:
    """METAR visibility for VIDP — the independent check on the fog coupling."""
    paths = metar.backfill(start, end)
    console.print(f"[green]metar[/] {start}..{end} -> {len(paths)} partition(s)")


@ingest_app.command("cpcb")
def ingest_cpcb(start: str = typer.Option(...), end: str = typer.Option(...)) -> None:
    """CPCB PM2.5 ground truth from the keyless OpenAQ bulk archive."""
    paths = cpcb.backfill(start, end)
    console.print(f"[green]cpcb[/] {start}..{end} -> {len(paths)} partition(s)")


@ingest_app.command("upwind")
def ingest_upwind(start: str = typer.Option(...), end: str = typer.Option(...)) -> None:
    """Upwind corridor PM2.5 (Punjab/Haryana) — the airshed leading indicator."""
    paths = cpcb.backfill_upwind(start, end)
    console.print(f"[green]cpcb_upwind[/] {start}..{end} -> {len(paths)} partition(s)")


@ingest_app.command("live")
def ingest_live(hours: int = typer.Option(96, help="How far back to refresh.")) -> None:
    """Recent CPCB observations from the OpenAQ API — feeds the live forecast.

    The S3 bulk archive lags several days, which is fine for training and
    useless for a forecast that needs this morning's readings.
    """
    logging.getLogger("airshed").setLevel(logging.INFO)
    paths = cpcb.sync_recent(hours=hours)
    console.print(f"[green]cpcb (live)[/] -> {len(paths)} partition(s)")


@ingest_app.command("history")
def ingest_history(
    directory: str = typer.Option("data/manual", help="Where stations.csv and station_hour.csv live."),
    upwind: bool = typer.Option(False, help="Load the upwind corridor instead of NCR."),
) -> None:
    """Pre-2025 CPCB history from the Kaggle archive (2015-2020)."""
    logging.getLogger("airshed").setLevel(logging.INFO)
    paths = kaggle_history.backfill(directory, upwind=upwind)
    console.print(f"[green]history[/] -> {len(paths)} partition(s)")


@ingest_app.command("fires")
def ingest_fires(start: str = typer.Option(...), end: str = typer.Option(...)) -> None:
    """FIRMS active fire detections. Empty outside burning season is normal."""
    paths = fires.backfill(start, end)
    console.print(f"[green]fires[/] {start}..{end} -> {len(paths)} partition(s)")


@ingest_app.command("all")
def ingest_all(start: str = typer.Option(...), end: str = typer.Option(...)) -> None:
    """Everything for a date range, in dependency order."""
    for name, fn in (
        ("cams", cams.backfill),
        ("meteo", meteo.backfill),
        ("metar", metar.backfill),
        ("cpcb", cpcb.backfill),
        ("fires", fires.backfill),
    ):
        try:
            paths = fn(start, end)
            console.print(f"[green]{name:8s}[/] -> {len(paths)} partition(s)")
        except Exception as exc:  # one dead source must not stop the rest (R8)
            console.print(f"[red]{name:8s}[/] failed: {exc}")


# ---------------------------------------------------------------------------
# cpcb helpers
# ---------------------------------------------------------------------------
@cpcb_app.command("resolve-ids")
def cpcb_resolve(
    apply: bool = typer.Option(False, help="Write the resolved ids back into config.toml."),
    max_km: float = typer.Option(3.0, help="Maximum match distance."),
    emit_toml: str = typer.Option(
        None, help="Write a replacement stations block, with OpenAQ coordinates, to this path."
    ),
) -> None:
    """Match configured stations to OpenAQ location ids (needs OPENAQ_API_KEY)."""
    logging.getLogger("airshed").setLevel(logging.INFO)
    records = cpcb.resolve_stations(max_km=max_km)
    console.print(f"resolved {len(records)} / {len(load_config().stations)} stations")
    if records:
        console.print(
            pl.DataFrame(records).select(
                ["station_id", "openaq_id", "openaq_name", "distance_km"]
            )
        )
    if emit_toml:
        with open(emit_toml, "w", encoding="utf-8") as fh:
            fh.write(cpcb.emit_station_toml(records))
        console.print(f"[green]wrote station block[/] {emit_toml}")
    if apply:
        n = cpcb.apply_ids_to_config({r["station_id"]: r["openaq_id"] for r in records})
        console.print(f"[green]wrote {n} ids into config.toml[/]")
    elif not emit_toml:
        console.print("[yellow]dry run — pass --apply to write them into config.toml[/]")


@cpcb_app.command("discover")
def cpcb_discover(
    live_within_days: int = typer.Option(14, help="Ignore feeds stale longer than this."),
    emit: str = typer.Option(None, help="Write TOML station lines to this path."),
) -> None:
    """Find official-network stations in the NCR bbox that config does not carry.

    The inverse of `resolve-ids`: it starts from what OpenAQ actually serves
    rather than from a hand-written roster, so coordinates are operator-
    published and liveness is checked rather than assumed. Writes nothing to
    config — review the list, then paste the emitted lines in.
    """
    logging.getLogger("airshed").setLevel(logging.INFO)
    records = cpcb.discover_stations(live_within_days=live_within_days)
    if not records:
        console.print("[green]nothing new[/] — config already has every live station")
        return

    keep = [r for r in records if not r.get("colocated")]
    dropped = [r for r in records if r.get("colocated")]
    ids = cpcb.next_station_ids(keep)

    table = Table(title=f"{len(keep)} live official station(s) to add")
    for col in ("proposed id", "name", "city", "km", "first", "last", "nearest", "openaq_id"):
        table.add_column(col)
    for sid, r in zip(ids, keep):
        near = f"{r['nearest_km']:.1f} km {r['nearest_configured']}" if r.get("nearest_km") is not None else ""
        # Flag anything close enough to share a grid cell, without excluding it:
        # two real stations can be neighbours, and that is a judgement call.
        style = "yellow" if (r.get("nearest_km") or 99) < cpcb.NEIGHBOUR_NOTE_KM else None
        table.add_row(
            sid, r["name"][:30], cpcb.city_for(r), f"{r['km_from_centre']:.0f}",
            r["first"], r["last"], near, str(r["openaq_id"]), style=style,
        )
    console.print(table)

    if dropped:
        console.print(
            f"\n[yellow]{len(dropped)} co-located candidate(s) excluded[/] "
            f"(within {cpcb.COLOCATED_KM} km of a configured station). A twin at "
            "zero distance double-weights that site in the city average and "
            "breaks leave-one-station-out, which would then be reading the "
            "held-out station off its own duplicate (R7):"
        )
        for r in dropped:
            console.print(
                f"  {r['openaq_name']} (id {r['openaq_id']}) — "
                f"{r['nearest_km']:.2f} km from {r['nearest_name']} "
                f"({r['nearest_configured']})"
            )
    console.print(
        "[yellow]Adding stations changes the evaluation row set[/], so every "
        "number in docs/results/ becomes non-comparable until regenerated."
    )
    if emit:
        Path(emit).write_text(cpcb.emit_new_station_lines(records), encoding="utf-8")
        console.print(f"[green]wrote station lines[/] {emit}")


@cpcb_app.command("quality")
def cpcb_quality(start: str = typer.Option(...), end: str = typer.Option(...)) -> None:
    """Flag stations with step changes in level — relocation or instrument swap (R6)."""
    df = store.read_range("cpcb", start, end)
    if df.is_empty():
        console.print("[red]no cached CPCB data for that range[/]")
        raise typer.Exit(1)
    console.print(
        df.group_by("quality_flag").agg(pl.len().alias("hours")).sort("hours", descending=True)
    )
    steps = cpcb.flag_step_changes(df)
    if steps.is_empty():
        console.print("[green]no step changes detected[/]")
    else:
        console.print("[yellow]step changes — inspect before trusting these stations:[/]")
        console.print(steps)


# ---------------------------------------------------------------------------
# status / features / gate
# ---------------------------------------------------------------------------
@ingest_app.command("expand-stations")
def ingest_expand(
    dataset: str = typer.Option(None, help="cams_archive or meteo_archive; default both."),
) -> None:
    """Give stations added after a backfill their gridded data, without refetching.

    Model output is fetched per grid cell, so a new station's values already sit
    on disk under a cell-mate. Ground truth is never expanded this way.
    """
    logging.getLogger("airshed").setLevel(logging.INFO)
    for name in [dataset] if dataset else list(repair.GRIDDED):
        n = repair.expand(name)
        console.print(f"[green]{name}[/] -> {n} partition(s) expanded")


@app.command("health")
def health() -> None:
    """Is the forecast-run archive still alive? Exits non-zero if not.

    `status` prints a table and always succeeds, which makes it useless as an
    alarm. This answers yes or no, and fails loudly, because archived runs
    cannot be backfilled: a week of silence is a week of episode-season evidence
    gone for good.
    """
    worst = 0
    stale_after_h = 36
    now = dt.datetime.now(dt.timezone.utc)
    for name in ("cams_runs", "meteo_runs"):
        info = store.coverage(name)
        if not info.get("last"):
            console.print(f"[red]{name}: EMPTY[/] — no forecast runs archived at all")
            worst = 2
            continue
        last = dt.date.fromisoformat(str(info["last"]))
        age_h = max(
            0.0,
            (now - dt.datetime.combine(last, dt.time(23, 59), tzinfo=dt.timezone.utc))
            .total_seconds() / 3600,
        )
        if age_h > stale_after_h:
            console.print(
                f"[red]{name}: STALE[/] — newest run {info['last']}, "
                f"{age_h:.0f} h old. Archived runs cannot be backfilled."
            )
            worst = max(worst, 2)
        else:
            console.print(
                f"[green]{name}: ok[/] — {info['days']} run(s), "
                f"newest {info['last']}, {age_h:.0f} h old"
            )

    # Is the loop itself alive? Freshness alone cannot answer this: a loop that
    # archived this morning and died at noon leaves runs that look perfectly
    # healthy for another 36 hours. That happened three times in two days.
    worst = max(worst, _report_loop_state())

    # Are the recent days actually whole? A partition that exists counts as
    # cached by every backfill, so a day hollowed out to one hour is invisible
    # to every other check here and is never repaired by itself.
    worst = max(worst, _report_observation_completeness())

    if not os.environ.get("AIRSHED_BACKUP_DIR"):
        console.print(
            "[yellow]AIRSHED_BACKUP_DIR is not set[/] — the run stores exist on "
            "this machine only, and cannot be re-fetched (~90 MB/year)."
        )
        worst = max(worst, 1)
    raise typer.Exit(worst)


MIN_OBS_HOURS = 18
OBS_CHECK_DAYS = 7


def _report_observation_completeness() -> int:
    """Flag recent CPCB days holding suspiciously few hours.

    Existence is not completeness, and every other check here only asks about
    existence. A day truncated to a single hour still satisfies `available_dates`,
    still counts as cached by `backfill(skip_existing=True)`, and still lets the
    forecast serve — on lag features built from almost nothing.

    Today is excluded because it is legitimately partial, and a threshold below
    24 h is deliberate: stations drop out, and R6 says a real gap stays a gap.
    """
    today = dt.date.today()
    start = today - dt.timedelta(days=OBS_CHECK_DAYS)
    obs = store.read_range("cpcb", start, today - dt.timedelta(days=1))
    if obs.is_empty():
        console.print("[red]observations: NONE cached for the last week[/]")
        return 2

    per_day = (
        obs.with_columns(pl.col("time").dt.date().alias("day"))
        .group_by("day")
        .agg(pl.col("time").n_unique().alias("hours"))
        .sort("day")
    )
    thin = per_day.filter(pl.col("hours") < MIN_OBS_HOURS)
    if thin.is_empty():
        console.print(
            f"[green]observations: complete[/] — {per_day.height} of the last "
            f"{OBS_CHECK_DAYS} days, all at least {MIN_OBS_HOURS} h"
        )
        return 0
    listed = ", ".join(
        f"{r['day']} ({r['hours']} h)" for r in thin.head(4).iter_rows(named=True)
    )
    console.print(
        f"[yellow]observations: {thin.height} thin day(s)[/] — {listed}. "
        "Repair with `airshed ingest cpcb --start <day> --end <day>`; a partition "
        "that exists is never refilled by a plain backfill."
    )
    return 1


def _report_loop_state() -> int:
    """Print whether the archive loop is running. Returns its severity."""
    from .procs import lock_state

    state = lock_state(load_config().data_root / "archive.lock")
    start = "start it with [bold]scripts\\run_archive_hidden.vbs[/]"

    if not state["held"]:
        console.print(f"[red]archive loop: NOT RUNNING[/] — no lock held; {start}")
        return 2
    if state["running"] is False:
        console.print(
            f"[red]archive loop: DEAD[/] — lock held by pid {state['pid']}, which "
            f"is gone. Nothing is archiving; {start}"
        )
        return 2
    if state["running"] is None:
        console.print(
            "[yellow]archive loop: UNKNOWN[/] — the lock names no pid, so it was "
            "written by an older build. Restart it to get a definite answer."
        )
        return 1
    age = state["age_min"]
    # Silence well past a tick means it is alive but wedged — mid-fetch at worst,
    # stuck at best, and either way not the same as healthy.
    if age is not None and age > 90:
        console.print(
            f"[yellow]archive loop: QUIET[/] — pid {state['pid']} alive but has "
            f"not checked in for {age:.0f} min (it should every 30)."
        )
        return 1
    console.print(
        f"[green]archive loop: running[/] — pid {state['pid']}, "
        f"last check-in {age:.0f} min ago"
    )
    return 0


@app.command("camsoffset")
def run_camsoffset(
    out: str = typer.Option(None, help="Markdown path; defaults to docs/results/camsoffset.md."),
) -> None:
    """Measure the gap between the CAMS we train on and the CAMS we serve.

    Reads only the local run and archive stores, so it costs nothing and can be
    re-run any time. It gets more informative with every day the daily archive
    job survives, and with nothing else.
    """
    logging.getLogger("airshed").setLevel(logging.INFO)
    table, ok = camsoffset_mod.run()
    if table.is_empty():
        console.print("[yellow]no run/archive overlap yet[/] — run `airshed archive`")
    else:
        console.print(table.select(
            ["lead_day", "rows", "run_days", "archive_mean", "run_mean", "bias", "rmse"]
        ))
        console.print(
            f"settled run days: {ok['settled_days']}/{ok['needed']} needed to fit"
        )
    path = camsoffset_mod.write(table, ok, Path(out) if out else None)
    console.print(f"[green]wrote[/] {path}")


@app.command("leadmatch")
def run_leadmatch(
    start: str = typer.Option("2025-02-18", help="First date of cached ground truth."),
    end: str = typer.Option(None, help="Defaults to the last cached CPCB day."),
    evaluate_on: str = typer.Option("test", help="test or val."),
    out: str = typer.Option(None, help="Markdown path; defaults to docs/results/leadmatch.md."),
) -> None:
    """Re-score with meteorology at real forecast lead, not short lead.

    Answers whether the reported 72 h skill was resting on an input production
    will never have. Needs `airshed ingest meteo-leadmatched` first.
    """
    logging.getLogger("airshed").setLevel(logging.INFO)
    if end is None:
        days = store.available_dates("cpcb")
        if not days:
            console.print("[red]no cached CPCB data[/]")
            raise typer.Exit(1)
        end = days[-1].isoformat()

    table, meta = leadmatch_mod.run(start, end, evaluate_on=evaluate_on)
    console.print(table.select(["model", "meteorology", "horizon_h", "n", "rmse", "bias"]))
    path = leadmatch_mod.write(table, meta, Path(out) if out else None)
    console.print(f"[green]wrote[/] {path}")


@app.command("ablation")
def run_ablation(
    start: str = typer.Option("2025-02-18", help="First date of cached ground truth."),
    end: str = typer.Option(None, help="Defaults to the last cached CPCB day."),
    evaluate_on: str = typer.Option("test", help="test or val."),
    out: str = typer.Option(None, help="Markdown path; defaults to docs/results/ablation.md."),
) -> None:
    """Phase 2: fit every model, score on the holdout, write the table."""
    logging.getLogger("airshed").setLevel(logging.INFO)
    if end is None:
        days = store.available_dates("cpcb")
        if not days:
            console.print("[red]no cached CPCB data[/]")
            raise typer.Exit(1)
        end = days[-1].isoformat()

    table, meta = ablation_mod.run(start, end, evaluate_on=evaluate_on)
    console.print(table.filter(pl.col("horizon_h") > 0).select(
        ["model", "horizon_h", "n", "rmse", "mae", "bias", "skill_vs_baseline"]
    ))
    path = ablation_mod.write(table, meta, Path(out) if out else None)
    console.print(f"[green]wrote[/] {path}")

    importance = meta["models"].get("full")
    if importance is not None and hasattr(importance, "importance"):
        console.print("")
        console.print("top features (72 h median head):")
        console.print(importance.importance(top=12))


@app.command("grap")
def run_grap(
    start: str = typer.Option("2025-02-18"),
    end: str = typer.Option(None, help="Defaults to the last cached CPCB day."),
    out: str = typer.Option(None, help="Markdown path; defaults to docs/results/grap.md."),
) -> None:
    """Phase 3: GRAP stage probabilities, per-class recall and lead time."""
    logging.getLogger("airshed").setLevel(logging.INFO)
    if end is None:
        days = store.available_dates("cpcb")
        if not days:
            console.print("[red]no cached CPCB data[/]")
            raise typer.Exit(1)
        end = days[-1].isoformat()

    table, lead, meta = grap_eval.run(start, end)
    console.print(table.filter(pl.col("stage") >= 2))
    console.print(lead)
    path = Path(out) if out else ablation_mod.RESULTS / "grap.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(grap_eval.to_markdown(table, lead, meta), encoding="utf-8")
    console.print(f"[green]wrote[/] {path}")


@app.command("loso")
def run_loso(
    start: str = typer.Option("2025-06-01"),
    end: str = typer.Option("2026-02-28"),
    stations: str = typer.Option(None, help="Comma-separated station ids; default is a spread of six."),
    out: str = typer.Option(None, help="Markdown path; defaults to docs/results/loso.md."),
) -> None:
    """Phase 4: leave-one-station-out spatial validation."""
    logging.getLogger("airshed").setLevel(logging.INFO)
    ids = [s.strip() for s in stations.split(",")] if stations else None
    table, meta = loso_mod.run(start, end, stations=ids)
    console.print(table.select(
        ["station", "n", "observed_mean", "rmse_graph", "rmse_idw", "rmse_cams"]
    ))
    path = Path(out) if out else ablation_mod.RESULTS / "loso.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(loso_mod.to_markdown(table, meta), encoding="utf-8")
    console.print(f"[green]wrote[/] {path}")


@app.command("archive")
def archive_runs(
    forecast_days: int = typer.Option(5, help="How far ahead to store each run."),
) -> None:
    """Archive today's CAMS and meteorology forecast runs. Run this daily.

    This is the only way to obtain *true* archived forecasts. Open-Meteo's
    air-quality archive is built from short-lead CAMS output, so a row in
    `cams_archive` is not a 72 h forecast and training on it leaves a
    train/serve gap of exactly the kind R1 exists to prevent. `cams_runs`
    accumulates real runs, stamped with issue time and lead time, from the day
    this cron starts. Every day it is not running is a day of archive lost.
    """
    logging.getLogger("airshed").setLevel(logging.INFO)
    ok = True
    for name, fn in (("cams", cams.archive_run), ("meteo", meteo.archive_run)):
        try:
            paths = fn(forecast_days=forecast_days)
            console.print(f"[green]{name}_runs[/] -> {len(paths)} partition(s)")
        except Exception as exc:  # one source failing must not stop the other (R8)
            console.print(f"[red]{name}_runs[/] failed: {exc}")
            ok = False
    for name in ("cams_runs", "meteo_runs"):
        c = store.coverage(name)
        console.print(f"  {name}: {c['days']} run(s) archived, latest {c['last']}")
    raise typer.Exit(0 if ok else 1)


@app.command("coupling")
def run_coupling(
    start: str = typer.Option("2025-02-18"),
    end: str = typer.Option(None, help="Defaults to the last cached CPCB day."),
    out: str = typer.Option(None, help="Markdown path; defaults to docs/results/coupling.md."),
) -> None:
    """Does knowing the pollution improve the weather forecast? The coupling proof."""
    logging.getLogger("airshed").setLevel(logging.INFO)
    if end is None:
        days = store.available_dates("cpcb")
        end = days[-1].isoformat() if days else None
    table, strata, meta = vis_mod.run(start, end)
    console.print(table.filter(pl.col("horizon_h") == 0).select(
        ["model", "n", "rmse_km", "mae_km", "low_vis_recall"]
    ))
    console.print(strata)
    path = Path(out) if out else ablation_mod.RESULTS / "coupling.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(vis_mod.to_markdown(table, strata, meta), encoding="utf-8")
    console.print(f"[green]wrote[/] {path}")


@app.command("rolling")
def run_rolling(
    start: str = typer.Option("2025-02-18"),
    end: str = typer.Option(None, help="Defaults to the last cached CPCB day."),
    fresh: bool = typer.Option(False, help="Ignore checkpoints and recompute every fold."),
    lead_matched: bool = typer.Option(
        False, help="Add a pass with meteorology at real forecast lead."
    ),
    out: str = typer.Option(None, help="Markdown path; defaults to docs/results/rolling.md."),
) -> None:
    """Rolling-origin evaluation: error bars for every claim.

    Checkpointed per fold, so a sleeping laptop or a cancelled run costs at
    most the fold in flight. Re-run the same command to resume.
    """
    logging.getLogger("airshed").setLevel(logging.INFO)
    if end is None:
        days = store.available_dates("cpcb")
        end = days[-1].isoformat() if days else None

    with keep_awake("rolling-origin evaluation"):
        table, meta = rolling_mod.run(start, end, resume=not fresh, lead_matched=lead_matched)

    console.print(pl.DataFrame(meta["per_model"]))
    for row in meta["paired"]:
        console.print(
            f"{row['claim']}: {row['mean_gain']:+.2%} mean gain, "
            f"{row['folds_better']}/{row['folds']} folds better"
        )
    path = Path(out) if out else ablation_mod.RESULTS / "rolling.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rolling_mod.to_markdown(table, meta), encoding="utf-8")
    table.write_csv(path.with_suffix(".csv"))
    console.print(f"[green]wrote[/] {path}")


@app.command()
def status() -> None:
    """What is cached locally, and how fresh it is."""
    table = Table(title="local store")
    for col in ("dataset", "days", "first", "last", "rows"):
        table.add_column(col)
    for name in ("cams_archive", "cams_runs", "meteo_archive", "meteo_leadmatched", "meteo_runs", "cpcb", "metar", "fires"):
        c = store.coverage(name)
        table.add_row(name, str(c["days"]), str(c["first"]), str(c["last"]), f"{c['rows']:,}")
    console.print(table)

    cfg = load_config()
    resolved = sum(1 for s in cfg.stations if s.resolved)
    console.print(f"stations: {len(cfg.stations)} configured, {resolved} with an OpenAQ id")


@app.command("features")
def build_features(
    start: str = typer.Option(...),
    end: str = typer.Option(...),
    out: str = typer.Option(None, help="Optional Parquet path for the supervised table."),
) -> None:
    """Build the aligned hourly frame and the supervised table. Reads cache only."""
    logging.getLogger("airshed").setLevel(logging.INFO)
    base = feat.build_base(start, end)
    console.print(f"base frame: {base.height:,} rows x {len(base.columns)} cols")
    coverage = {
        "pm25 (target)": base["pm25_clean"].is_not_null().sum(),
        "cams_pm2_5": base["cams_pm2_5"].is_not_null().sum(),
        "met_boundary_layer_height": base["met_boundary_layer_height"].is_not_null().sum(),
        "metar_visibility_km": base["metar_visibility_km"].is_not_null().sum(),
    }
    for k, v in coverage.items():
        console.print(f"  {k:28s} {v:>8,} / {base.height:,} rows ({v / base.height:.1%})")

    sup = feat.build_supervised(base)
    console.print(f"supervised: {sup.height:,} rows")
    if not sup.is_empty():
        console.print(split_mod.summarise(split_mod.assign_split(sup)))
    if out:
        sup.write_parquet(out, compression="zstd")
        console.print(f"[green]wrote[/] {out}")


@app.command("episodes")
def episodes(start: str = typer.Option(...), end: str = typer.Option(...)) -> None:
    """City AQI and GRAP stage frequency over a range — how rare severe really is (R5)."""
    obs = store.read_range("cpcb", start, end)
    if obs.is_empty():
        console.print("[red]no cached CPCB data[/]")
        raise typer.Exit(1)
    with_aqi = grap_mod.add_rolling_aqi(obs)
    city = grap_mod.city_aqi(with_aqi)
    console.print(grap_mod.stage_frequency(city))


@app.command("gate")
def gate(
    winter_start: str = typer.Option("2025-11-04", help="Start of the November test week."),
    summer_start: str = typer.Option("2025-06-10", help="Start of the June test week."),
) -> None:
    """Phase 1 gate: reconstruct every feature for a past week with no network call."""
    from .verify import run_gate

    ok = run_gate(winter_start, summer_start, console=console)
    raise typer.Exit(0 if ok else 1)


def _require_range(start: str | None, end: str | None) -> None:
    if not start or not end:
        raise typer.BadParameter("--start and --end are required unless --live is passed")
    dt.date.fromisoformat(start)
    dt.date.fromisoformat(end)


if __name__ == "__main__":
    app()
