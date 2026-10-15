"""Public health check for monitoring: no secrets, no paths."""
from fastapi import APIRouter, Request

from lecturekit import api as lk

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request):
    env = lk.check_environment()
    return {"ok": env["ok"], "checks": env["checks"], "optional_missing": env["optional_missing"],
            "models": env["models"], "whisper": env["whisper"],
            "queue_length": request.app.state.worker.queue_length()}
