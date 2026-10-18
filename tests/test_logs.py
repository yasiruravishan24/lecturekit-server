import logging
from datetime import date, datetime, timedelta

from server.daily_log import DailyFileHandler, setup_daily_logs
from tests.conftest import new_user


def test_requests_and_errors_go_to_files_named_by_date(client, settings):
    new_user(client)
    logging.getLogger("lecturekit.server").error("something broke")
    today = date.today().isoformat()
    server = (settings.log_dir / f"server-{today}.log").read_text()
    error = (settings.log_dir / f"error-{today}.log").read_text()
    assert "POST /api/auth/register 201" in server and "something broke" in server
    assert "something broke" in error and "POST /api/auth/register" not in error       # the error file has errors only
    assert "Passw" not in server and "password" not in server.lower()                   # bodies and passwords are never logged


def test_a_new_day_opens_a_new_file_and_old_files_are_deleted(tmp_path):
    h = DailyFileHandler(tmp_path, "server", logging.INFO, retention_days=3)
    days = {"now": date(2026, 10, 10)}
    h._today = lambda: days["now"]
    record = logging.LogRecord("lecturekit.server", logging.INFO, __file__, 1, "hello", None, None)
    (tmp_path / "server-2026-10-01.log").write_text("old")
    (tmp_path / "server-2026-10-08.log").write_text("recent")
    (tmp_path / "error-2026-10-01.log").write_text("other handler's file")
    (tmp_path / "keep-me.txt").write_text("not a log")
    h.emit(record)
    days["now"] = date(2026, 10, 11)
    h.emit(record)
    h.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert "server-2026-10-10.log" in names and "server-2026-10-11.log" in names
    assert "server-2026-10-01.log" not in names                                          # older than 3 days: deleted
    assert "server-2026-10-08.log" in names and "error-2026-10-01.log" in names and "keep-me.txt" in names


def test_setup_twice_does_not_write_every_line_twice(tmp_path):
    setup_daily_logs(tmp_path, 14, "lecturekit.testlogs")
    setup_daily_logs(tmp_path, 14, "lecturekit.testlogs")
    logging.getLogger("lecturekit.testlogs").setLevel(logging.INFO)
    logging.getLogger("lecturekit.testlogs").info("once")
    text = (tmp_path / f"server-{date.today().isoformat()}.log").read_text()
    assert text.count("once") == 1
