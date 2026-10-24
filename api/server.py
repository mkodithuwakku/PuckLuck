"""Loopback FastAPI server (SRS §7.1).

Binds strictly to ``127.0.0.1:8877`` — ``validate_bind_host`` rejects
``0.0.0.0`` and any non-loopback address, and CORS is restricted to
local development origins. The ``/api/report`` endpoint serves the same
allowlisted payload that gets published to gh-pages, re-validated on
every read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from scripts.refresh import (
    DEFAULT_REPORT_PATH,
    build_public_report,
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


def create_app(
    *,
    report_path: Union[str, Path] = DEFAULT_REPORT_PATH,
    report_provider: Optional[Callable[[], Dict[str, Any]]] = None,
) -> FastAPI:
    """Build the loopback app: GET /api/health and GET /api/report only."""
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

    @app.get("/api/report")
    def report() -> Dict[str, Any]:
        if report_provider is not None:
            payload = report_provider()
        elif path.is_file():
            payload = json.loads(path.read_text())
        else:
            payload = build_public_report()
        try:
            validate_public_report(payload)
        except ValueError as exc:
            raise HTTPException(
                status_code=500, detail="report failed allowlist validation"
            ) from exc
        return payload

    return app


def main() -> None:
    import os

    import uvicorn

    host = validate_bind_host(os.environ.get("PUCKLUCK_HOST", DEFAULT_HOST))
    port = int(os.environ.get("PUCKLUCK_PORT", str(DEFAULT_PORT)))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
