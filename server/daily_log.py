"""Daily log files, one per day, named like Laravel's: logs/server-2026-10-18.log and logs/error-2026-10-18.log.

server-DATE.log has every message from INFO up (one line per request, job events, warnings, errors with tracebacks);
error-DATE.log has only errors. Files older than LOG_RETENTION_DAYS are deleted when a new day starts."""
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

_DATED = re.compile(r"^(server|error)-(\d{4}-\d{2}-\d{2})\.log$")


class DailyFileHandler(logging.Handler):
    def __init__(self, directory: Path, prefix: str, level: int, retention_days: int):
        super().__init__(level)
        self.directory, self.prefix, self.retention_days = Path(directory), prefix, retention_days
        self._day: date | None = None
        self._stream = None
        self.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))

    def _today(self) -> date:
        return datetime.now().date()

    def _open(self, day: date) -> None:
        if self._stream:
            self._stream.close()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stream = open(self.directory / f"{self.prefix}-{day.isoformat()}.log", "a", encoding="utf-8")
        self._day = day
        self._delete_old(day)

    def _delete_old(self, today: date) -> None:
        limit = today - timedelta(days=self.retention_days)
        for f in self.directory.iterdir():
            m = _DATED.match(f.name)
            if m and m.group(1) == self.prefix and date.fromisoformat(m.group(2)) < limit:
                f.unlink(missing_ok=True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = self._today()
            if day != self._day or self._stream is None:
                self._open(day)
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:                                   # logging must never break a request
            self.handleError(record)

    def close(self) -> None:
        if self._stream:
            self._stream.close()
            self._stream = None
        super().close()


def setup_daily_logs(directory: Path, retention_days: int = 14, logger_name: str = "lecturekit") -> None:
    """Attach the two daily files to the lecturekit logger tree (the server and the pipeline). Safe to call again."""
    root = logging.getLogger(logger_name)
    for h in [h for h in root.handlers if isinstance(h, DailyFileHandler)]:
        root.removeHandler(h)
        h.close()
    root.addHandler(DailyFileHandler(directory, "server", logging.INFO, retention_days))
    root.addHandler(DailyFileHandler(directory, "error", logging.ERROR, retention_days))
