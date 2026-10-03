"""Every error leaves the API as {"error": <code>, "detail": <text>} (see CONTRACT.md)."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiException(Exception):
    def __init__(self, error: str, detail: str, status: int):
        self.error, self.detail, self.status = error, detail, status


def out_of_coverage(detail: str = "SiteSense covers the contiguous US only") -> ApiException:
    return ApiException("out_of_coverage", detail, 422)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiException)
    async def _api(_: Request, exc: ApiException):
        return JSONResponse(status_code=exc.status, content={"error": exc.error, "detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"][1:]) or "body"
        return JSONResponse(status_code=422, content={"error": "invalid_request", "detail": f"{where}: {first['msg']}"})

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException):
        code = {404: "invalid_request", 429: "rate_limited", 503: "upstream_unavailable"}.get(exc.status_code, "internal")
        return JSONResponse(status_code=exc.status_code, content={"error": code, "detail": str(exc.detail)})

    @app.exception_handler(Exception)
    async def _crash(_: Request, exc: Exception):
        return JSONResponse(status_code=500, content={"error": "internal", "detail": type(exc).__name__})
