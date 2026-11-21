"""FastAPI app for the bed dashboard.

    hcalloc dashboard            -> http://127.0.0.1:8000  (page)  and  /api/plan  (JSON)

Endpoints
    GET /api/health
    GET /api/plan?date=YYYY-MM-DD   recommendation for the 7 days after ``date`` (default: just before a surge)
    GET /api/range                  first and last date that can be planned

The team's React app can call /api/plan directly; CORS is open for local development.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from .service import DemoSource, recommend

PAGE = Path(__file__).with_name("dashboard.html")


@lru_cache(maxsize=1)
def source() -> DemoSource:
    return DemoSource()


def create_app() -> FastAPI:
    app = FastAPI(title="hcalloc bed dashboard", version="2.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return PAGE.read_text(encoding="utf-8")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/range")
    def date_range() -> dict:
        s = source()
        return {"first": f"{s.series.dates[s.valid[0]]:%Y-%m-%d}", "last": f"{s.series.dates[s.valid[-1]]:%Y-%m-%d}",
                "default": f"{s.series.dates[s.default_origin]:%Y-%m-%d}"}

    @app.get("/api/plan")
    def plan(date: str | None = None) -> dict:
        s = source()
        try:
            origin = s.origin_for(date)
        except (ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return recommend(s.series, s.cfg, origin)

    return app


app = create_app()
