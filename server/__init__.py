"""FastAPI server for lecturekit (SERVER_PLAN.md).

lecturekit reads LK_CACHE / LK_LOG when it is first imported, so they are pointed into DATA_DIR here,
before anything imports lecturekit.
"""
import os
from pathlib import Path

_data = Path(os.getenv("DATA_DIR", "/data"))
os.environ.setdefault("LK_CACHE", str(_data / "cache"))
os.environ.setdefault("LK_LOG", str(_data / "logs"))
