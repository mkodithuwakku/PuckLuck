"""Loopback FastAPI server (SRS §7.1).

Binds strictly to ``127.0.0.1:8877`` — ``validate_bind_host`` rejects
``0.0.0.0`` and any non-loopback address, and CORS is restricted to
local development origins. ``/api/report`` and ``/report.json`` serve
the same allowlisted payload that gets published to gh-pages,
re-validated on every read, and ``web/`` is mounted at ``/`` so the PWA
renders out of the box. When no report exists on disk yet, a populated
in-memory demo warehouse drives the payload so a clean checkout serves
a working dashboard immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import db
from scripts.refresh import (
    DEFAULT_REPORT_PATH,
    run_warehouse_pipeline,
    seed_demo_warehouse,
    validate_public_report,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8877
ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOWED_CORS_ORIGINS = [
    "http://127.0.0.1:8877",
    "http://localhost:8877",
    "http://127.0.0.1:3000",
    "http://localhost:3000",
]


def validate_bind_host(host: str) -> str:
    """Return ``host`` iff loopback; raise ValueError otherwise (SRS §7.1)."""
    if host not in ALLOWED_LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to bind non-loopback host {host!r}; "
            "PuckLuck API is local-only (SRS §7.1)"
        )
    return host


def _demo_report() -> Dict[str, Any]:
    """Populated report from an ephemeral in-memory demo warehouse."""
    conn = db.init_db(":memory:")
    try:
        seed_demo_warehouse(conn)
        return run_warehouse_pipeline(conn)
    finally:
        conn.close()


def create_app(
    *,
    report_path: Union[str, Path] = DEFAULT_REPORT_PATH,
    report_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    web_dir: Optional[Union[str, Path]] = Path("web"),
) -> FastAPI:
    """Build the loopback app: health, allowlisted report, static PWA."""
    path = Path(report_path)
    app = FastAPI(title="PuckLuck", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_CORS_ORIGINS,
        allow_methods=["GET"],
    )

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        return {"status": "ok", "host": DEFAULT_HOST, "port": DEFAULT_PORT}

    @app.get("/report.json")
    @app.get("/api/report")
    def report() -> Dict[str, Any]:
        if report_provider is not None:
            payload = report_provider()
        elif path.is_file():
            payload = json.loads(path.read_text())
        else:
            payload = _demo_report()
        try:
            validate_public_report(payload)
        except ValueError as exc:
            raise HTTPException(
                status_code=500, detail="report failed allowlist validation"
            ) from exc
        return payload

    if web_dir is not None and Path(web_dir).is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(web_dir), html=True),
            name="puckluck-web",
        )

    return app


def main() -> None:
    import os

    import uvicorn

    host = validate_bind_host(os.environ.get("PUCKLUCK_HOST", DEFAULT_HOST))
    port = int(os.environ.get("PUCKLUCK_PORT", str(DEFAULT_PORT)))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
