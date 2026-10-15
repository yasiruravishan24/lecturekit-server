from sqlalchemy import select

from server.models import Course, Job, LectureSession, Module
from tests.conftest import make_session, new_user


def test_course_module_session_crud(client):
    h = new_user(client)
    c = client.post("/courses", json={"name": "  Intro CS  "}, headers=h)
    assert c.status_code == 201 and c.json()["name"] == "Intro CS"
    cid = c.json()["id"]
    assert [x["name"] for x in client.get("/courses", headers=h).json()] == ["Intro CS"]
    assert client.patch(f"/courses/{cid}", json={"name": "CS 101"}, headers=h).json()["name"] == "CS 101"

    m = client.post(f"/courses/{cid}/modules", json={"name": "Week 1"}, headers=h)
    mid = m.json()["id"]
    assert m.status_code == 201 and m.json()["course_id"] == cid
    assert client.patch(f"/modules/{mid}", json={"name": "Week 2"}, headers=h).json()["name"] == "Week 2"
    assert [x["name"] for x in client.get(f"/courses/{cid}/modules", headers=h).json()] == ["Week 2"]

    s = client.post(f"/modules/{mid}/sessions", json={"title": "Loops"}, headers=h)
    sid = s.json()["id"]
    assert s.status_code == 201 and s.json()["state"] == "empty" and s.json()["has_notes"] is False
    assert s.json()["files"] == [] and s.json()["latest_job"] is None
    assert client.patch(f"/sessions/{sid}", json={"title": "Loops and lists"}, headers=h).json()["title"] == "Loops and lists"
    assert [x["id"] for x in client.get(f"/modules/{mid}/sessions", headers=h).json()] == [sid]
    assert client.get(f"/sessions/{sid}", headers=h).json()["title"] == "Loops and lists"

    assert client.delete(f"/sessions/{sid}", headers=h).status_code == 204
    assert client.get(f"/sessions/{sid}", headers=h).status_code == 404
    assert client.delete(f"/modules/{mid}", headers=h).status_code == 204
    assert client.delete(f"/courses/{cid}", headers=h).status_code == 204
    assert client.get("/courses", headers=h).json() == []


def test_names_are_validated(client):
    h = new_user(client)
    for body in ({"name": ""}, {"name": "   "}, {"name": "x" * 121}, {}):
        assert client.post("/courses", json=body, headers=h).status_code == 422
    cid = client.post("/courses", json={"name": "CS"}, headers=h).json()["id"]
    assert client.post(f"/courses/{cid}/modules", json={"name": ""}, headers=h).status_code == 422
    mid = client.post(f"/courses/{cid}/modules", json={"name": "W"}, headers=h).json()["id"]
    assert client.post(f"/modules/{mid}/sessions", json={"title": ""}, headers=h).status_code == 422


def test_user_b_cannot_touch_user_a_data(client):
    a = new_user(client, "a@example.com")
    b = new_user(client, "b@example.com")
    cid = client.post("/courses", json={"name": "A's course"}, headers=a).json()["id"]
    mid = client.post(f"/courses/{cid}/modules", json={"name": "A's module"}, headers=a).json()["id"]
    sid = client.post(f"/modules/{mid}/sessions", json={"title": "A's lecture"}, headers=a).json()["id"]

    requests_ = [
        ("get", f"/courses/{cid}/modules", None), ("patch", f"/courses/{cid}", {"name": "x"}),
        ("post", f"/courses/{cid}/modules", {"name": "x"}), ("delete", f"/courses/{cid}", None),
        ("patch", f"/modules/{mid}", {"name": "x"}), ("delete", f"/modules/{mid}", None),
        ("get", f"/modules/{mid}/sessions", None), ("post", f"/modules/{mid}/sessions", {"title": "x"}),
        ("get", f"/sessions/{sid}", None), ("patch", f"/sessions/{sid}", {"title": "x"}), ("delete", f"/sessions/{sid}", None),
        ("get", f"/sessions/{sid}/notes", None), ("get", f"/sessions/{sid}/quiz", None),
        ("post", f"/sessions/{sid}/quiz/attempts", {"answers": []}), ("get", f"/sessions/{sid}/quiz/attempts", None),
        ("get", f"/sessions/{sid}/export.md", None), ("post", f"/sessions/{sid}/process", None),
        ("post", f"/sessions/{sid}/retry", None), ("post", f"/sessions/{sid}/live", None),
        ("get", f"/sessions/{sid}/live", None), ("post", f"/sessions/{sid}/live/finish?total=1", None),
    ]
    for method, path, body in requests_:
        r = getattr(client, method)(path, headers=b, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (method, path, r.status_code)
        assert r.json() == {"error": {"code": "not_found", "message": "Not found"}}, (method, path)
    assert client.get("/courses", headers=b).json() == []                         # B sees nothing of A's
    # nothing of A's was changed or deleted
    assert client.get("/courses", headers=a).json()[0]["name"] == "A's course"
    assert client.get(f"/sessions/{sid}", headers=a).json()["title"] == "A's lecture"
    # unknown ids look exactly the same as other people's ids
    assert client.get("/sessions/99999", headers=b).json() == client.get(f"/sessions/{sid}", headers=b).json()


def test_deleting_a_course_removes_children_and_files(client, app, settings):
    h = new_user(client)
    sid = make_session(client, h)
    uid = client.get("/auth/me", headers=h).json()["user"]["id"]
    folder = settings.users_dir / str(uid) / "sessions" / str(sid)
    (folder / "out").mkdir(parents=True)
    (folder / "out" / "A.md").write_text("notes")
    cid = client.get("/courses", headers=h).json()[0]["id"]
    assert client.delete(f"/courses/{cid}", headers=h).status_code == 204
    assert not folder.exists()
    with app.state.session_factory() as db:
        assert db.scalars(select(Course)).all() == [] and db.scalars(select(Module)).all() == []
        assert db.scalars(select(LectureSession)).all() == []


def test_cannot_delete_while_a_lecture_is_processing(client, app):
    h = new_user(client)
    sid = make_session(client, h)
    with app.state.session_factory() as db:
        db.get(LectureSession, sid).state = "processing"
        db.commit()
    cid = client.get("/courses", headers=h).json()[0]["id"]
    for path in (f"/sessions/{sid}", f"/courses/{cid}"):
        r = client.delete(path, headers=h)
        assert r.status_code == 409 and r.json()["error"]["code"] == "busy"
    # the account cannot be deleted while a job of its own is queued or running either
    with app.state.session_factory() as db:
        uid = db.get(LectureSession, sid).user_id
        db.add(Job(session_id=sid, user_id=uid, kind="process", state="processing"))
        db.commit()
    r = client.delete("/auth/me", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "busy"
