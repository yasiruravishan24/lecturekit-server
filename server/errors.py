"""One error shape for every failure: {"error": {"code": "...", "message": "..."}} (SERVER_PLAN.md section 7)."""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("lecturekit.server")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def not_found() -> ApiError:
    return ApiError(404, "not_found", "Not found")


def conflict(message: str, code: str = "conflict") -> ApiError:
    return ApiError(409, code, message)


def unprocessable(message: str, code: str = "validation") -> ApiError:
    return ApiError(422, code, message)


def _body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def job_error(code: str, message: str) -> tuple[str, str]:
    """Map a lecturekit error code and message to what is stored on a job and shown to the user.

    Provider details and secrets never reach the user (SERVER_PLAN.md section 7)."""
    low = message.lower()
    if code == "missing_input":
        if "anthropic_api_key" in low:
            return "llm", "The AI service is not configured, try again later"
        return code, message
    if code == "validation":
        return code, message
    if code == "external_tool":
        if "libreoffice" in low or "convert" in low or "merge" in low:
            return code, "Slides could not be converted"
        if "ffmpeg" in low or "ffprobe" in low or "audio" in low:
            return code, "ffmpeg could not read this file"
        return code, "A processing tool failed"
    if code == "llm":
        return code, "The AI service failed, try again later"
    return "unexpected", "Unexpected error"


def register(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse(_body(exc.code, exc.message), status_code=exc.status, headers=getattr(exc, "headers", None))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        code = {401: "unauthorized", 404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
        message = "Not found" if exc.status_code == 404 else str(exc.detail)
        return JSONResponse(_body(code, message), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p not in ("body", "query"))
        return JSONResponse(_body("validation", f"Invalid request: {where or 'body'}: {first.get('msg', 'invalid')}"),
                            status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        logger.exception("unexpected error on %s %s", request.method, request.url.path)   # details only in the log
        return JSONResponse(_body("unexpected", "Unexpected error"), status_code=500)
