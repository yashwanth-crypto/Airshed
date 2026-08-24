"""FastAPI application.

Endpoints are deliberately few and each answers a question the demo asks:

    GET /api/status              what is cached, and how stale (R8)
    GET /api/days                which dates replay can be driven to
    GET /api/replay/{date}       what we would have forecast, vs what happened
    GET /api/surface/{stamp}     the downscaled grid for one hour
    GET /                        the UI

Nothing here fetches from a remote source. If ingestion has not run, the
endpoints report stale data rather than reaching for the network.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import repo_root
from .service import Service

log = logging.getLogger(__name__)

app = FastAPI(
    title="Airshed",
    description="Coupled air pollution and weather forecasting for Delhi NCR (SIH26082)",
    version="0.1.0",
)
service = Service()

STATIC = repo_root() / "src" / "airshed" / "api" / "static"


@app.get("/api/status")
def status() -> dict:
    return service.status()


@app.get("/api/days")
def days() -> dict:
    return service.available_days()


@app.get("/api/stations")
def stations() -> dict:
    return {
        "stations": [
            {
                "id": s.id, "name": s.name, "city": s.city,
                "agency": s.agency, "lat": s.lat, "lon": s.lon,
            }
            for s in service.cfg.stations
        ]
    }


@app.get("/api/grap/thresholds")
def grap_thresholds() -> dict:
    from .. import grap

    return {
        "stages": [
            {"stage": stage, "name": name, "pm25_low": round(lo, 1), "pm25_high": round(hi, 1)}
            for stage, name, lo, hi in grap.stage_bounds(service.cfg)
        ]
    }


@app.get("/api/replay/{date}")
def replay(date: str, horizon: int = 24) -> dict:
    try:
        result = service.replay(date, horizon=horizon)
    except Exception as exc:  # a bad date must not 500 the demo
        log.exception("replay failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/api/forecast")
def forecast() -> dict:
    """The live 72-hour forecast — what the system says about tomorrow."""
    try:
        return service.forecast()
    except Exception as exc:
        log.exception("forecast failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/surface/{stamp}")
def surface(stamp: str) -> dict:
    try:
        return service.surface(stamp)
    except Exception as exc:
        log.exception("surface failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
