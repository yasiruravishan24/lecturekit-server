"""The WebSocket (events.py) and the notifications list."""
import pytest
from starlette.websockets import WebSocketDisconnect

from tests.conftest import file_part, make_session, new_user, wait_job


def token_of(h):
    return h["Authorization"].removeprefix("Bearer ")


def open_socket(client, h):
    ws = client.websocket_connect("/api/ws")
    sock = ws.__enter__()
    sock.send_json({"token": token_of(h)})
    assert sock.receive_json() == {"type": "hello"}
    return ws, sock


def until(sock, wanted, limit=40):
    """Read events until one of this type arrives (progress events may come in between)."""
    for _ in range(limit):
        event = sock.receive_json()
        if event["type"] == wanted:
            return event
    raise AssertionError(f"no {wanted} event")


def test_a_socket_without_a_good_token_is_closed(client):
    with client.websocket_connect("/api/ws") as ws:
        ws.send_json({"token": "nonsense"})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401
    with client.websocket_connect("/api/ws") as ws:                      # a link for a <video> is not a login token either
        ws.send_text("not json")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_what_a_user_changes_reaches_their_open_socket_but_nobody_elses(client):
    a, b = new_user(client, "a@example.com"), new_user(client, "b@example.com")
    wa, sa = open_socket(client, a)
    wb, sb = open_socket(client, b)
    try:
        client.post("/courses", headers=a, json={"name": "Maths"})            # for example from the mobile app or the extension
        event = until(sa, "sync")
        assert event["reason"] == "change"
        client.post("/courses", headers=b, json={"name": "Physics"})
        assert until(sb, "sync")["reason"] == "change"                       # b got only its own event
    finally:
        wa.__exit__(None, None, None)
        wb.__exit__(None, None, None)


def test_starting_a_recording_and_processing_make_notifications_that_are_pushed(client, app, fake_lk):
    h = new_user(client)
    ws, sock = open_socket(client, h)
    try:
        sid = make_session(client, h, "Online lecture")
        client.post(f"/sessions/{sid}/live", headers=h)                        # the extension starts recording
        n = until(sock, "notification")["notification"]
        assert (n["kind"], n["session_id"], n["title"], n["read"]) == ("recording_started", sid, "Online lecture", False)
        client.post(f"/sessions/{sid}/live/chunks", params={"index": 0}, headers=h, files={"chunk": file_part("c.webm", b"x")})
        job = client.post(f"/sessions/{sid}/live/finish", params={"total": 1}, headers=h).json()["job_id"]
        assert until(sock, "notification")["notification"]["kind"] == "processing_started"
        wait_job(client, h, job)
        kinds = []
        for _ in range(30):
            event = sock.receive_json()
            if event["type"] == "notification":
                kinds.append(event["notification"]["kind"])
                if kinds[-1] == "notes_ready":
                    break
        assert kinds[-1] == "notes_ready"
    finally:
        ws.__exit__(None, None, None)
    items = client.get("/notifications", headers=h).json()
    assert [i["kind"] for i in items["items"]][:3] == ["notes_ready", "processing_started", "recording_started"] and items["unread"] == 3


def test_job_progress_is_pushed(client, fake_lk):
    h = new_user(client)
    sid = make_session(client, h)
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.mp4"))])
    ws, sock = open_socket(client, h)
    try:
        client.post(f"/sessions/{sid}/process", headers=h)
        event = until(sock, "job")
        assert event["session_id"] == sid and event["kind"] == "process" and 0 <= event["percent"] <= 100
    finally:
        ws.__exit__(None, None, None)


def test_mark_read_clear_and_not_someone_elses(client):
    a, b = new_user(client, "a@example.com"), new_user(client, "b@example.com")
    sid = make_session(client, a, "L")
    client.post(f"/sessions/{sid}/live", headers=a)
    items = client.get("/notifications", headers=a).json()
    nid = items["items"][0]["id"]
    assert items["unread"] == 1
    assert client.post(f"/notifications/{nid}/read", headers=b).status_code == 404            # not b's
    assert client.get("/notifications", headers=b).json() == {"items": [], "unread": 0}
    assert client.post(f"/notifications/{nid}/read", headers=a).status_code == 204
    assert client.get("/notifications", headers=a).json()["unread"] == 0
    client.post(f"/sessions/{sid}/live", headers=a)
    assert client.post("/notifications/read-all", headers=a).status_code == 204
    assert client.delete("/notifications", headers=a).status_code == 204
    assert client.get("/notifications", headers=a).json() == {"items": [], "unread": 0}
    assert client.get("/notifications").status_code == 401
