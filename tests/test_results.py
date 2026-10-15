from sqlalchemy import select

from server.models import Attempt, AttemptAnswer, Question
from tests.conftest import file_part, make_session, new_user, wait_job


def done_session(client, h, fake_lk=None):
    sid = make_session(client, h)
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.mp4"))])
    job = wait_job(client, h, client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"])
    assert job["state"] == "done"
    return sid


def test_notes_and_quiz_exist_only_when_done(client, fake_lk):
    h = new_user(client)
    sid = make_session(client, h)
    for path in ("notes", "quiz", "quiz/attempts", "export.md"):
        assert client.get(f"/sessions/{sid}/{path}", headers=h).status_code == 404, path
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.mp4"))])
    fake_lk.gate.clear()
    jid = client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"]
    assert client.get(f"/sessions/{sid}/notes", headers=h).status_code == 404        # still being processed
    fake_lk.gate.set()
    wait_job(client, h, jid)
    notes = client.get(f"/sessions/{sid}/notes", headers=h).json()
    assert notes["title"] == "Test lecture" and notes["config"] == "A" and notes["model"] == "fake-model"
    assert notes["full_notes"][0]["heading"] == "Loops" and notes["short_notes"] == ["Loops repeat", "Use for"]


def test_quiz_hides_the_answers(client, fake_lk):
    h = new_user(client)
    sid = done_session(client, h)
    r = client.get(f"/sessions/{sid}/quiz", headers=h)
    qs = r.json()["questions"]
    assert len(qs) == 3 and [q["position"] for q in qs] == [1, 2, 3]
    assert set(qs[0]) == {"id", "position", "question", "options", "bloom_level"}
    for word in ("answer", "explanation", "Because", "correct"):
        assert word not in r.text, word


def test_attempt_scores_and_reveals_answers_only_after_submitting(client, fake_lk):
    h = new_user(client)
    sid = done_session(client, h)
    # no GET before an attempt reveals an answer or an explanation
    for path in ("", "/notes", "/quiz", "/quiz/attempts"):
        text = client.get(f"/sessions/{sid}{path}", headers=h).text
        assert "Because" not in text and "answer_index" not in text and "correct_index" not in text, path
    # the fake quiz has answer_index 0, 1, 2 for the three questions
    r = client.post(f"/sessions/{sid}/quiz/attempts", json={"answers": [0, 3, None]}, headers=h)
    assert r.status_code == 201
    body = r.json()
    assert body["score"] == 1 and body["total"] == 3
    assert [x["correct_index"] for x in body["results"]] == [0, 1, 2]
    assert [x["correct"] for x in body["results"]] == [True, False, False]
    assert [x["chosen_index"] for x in body["results"]] == [0, 3, None]
    assert body["results"][1]["explanation"] == "Because 1."
    # a second, perfect attempt; the history keeps both and shows only score and time
    client.post(f"/sessions/{sid}/quiz/attempts", json={"answers": [0, 1, 2]}, headers=h)
    hist = client.get(f"/sessions/{sid}/quiz/attempts", headers=h)
    assert [(a["score"], a["total"]) for a in hist.json()] == [(1, 3), (3, 3)]
    assert all(set(a) == {"id", "score", "total", "submitted_at"} for a in hist.json())
    assert "Because" not in hist.text and "correct" not in hist.text and "chosen" not in hist.text


def test_bad_attempts_are_rejected(client, fake_lk, app):
    h = new_user(client)
    sid = done_session(client, h)
    for answers in ([0, 1], [0, 1, 2, 3], [0, 1, 9], [0, 1, -1], ["a", 1, 2]):
        r = client.post(f"/sessions/{sid}/quiz/attempts", json={"answers": answers}, headers=h)
        assert r.status_code == 422, answers
    assert client.post(f"/sessions/{sid}/quiz/attempts", json={}, headers=h).status_code == 422
    assert client.get(f"/sessions/{sid}/quiz/attempts", headers=h).json() == []        # nothing stored


def test_processing_again_replaces_notes_quiz_and_attempts(client, fake_lk, app):
    h = new_user(client)
    sid = done_session(client, h)
    client.post(f"/sessions/{sid}/quiz/attempts", json={"answers": [0, 1, 2]}, headers=h)
    fake_lk.fail_with = None
    job = wait_job(client, h, client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"])
    assert job["state"] == "done"
    assert client.get(f"/sessions/{sid}/quiz/attempts", headers=h).json() == []        # the old quiz's attempts went with it
    with app.state.session_factory() as db:
        assert len(db.scalars(select(Question)).all()) == 3                            # replaced, not doubled
        assert db.scalars(select(Attempt)).all() == [] and db.scalars(select(AttemptAnswer)).all() == []


def test_export_markdown_and_job_status(client, fake_lk):
    h = new_user(client)
    other = new_user(client, "b@example.com")
    sid = make_session(client, h)
    client.post(f"/sessions/{sid}/upload", headers=h, files=[("recording", file_part("a.mp4"))])
    jid = client.post(f"/sessions/{sid}/process", headers=h).json()["job_id"]
    job = wait_job(client, h, jid)
    assert set(job) >= {"id", "state", "stage", "percent", "message", "error_code", "error_message"}
    assert job["error_code"] is None and job["stage"] == "done"
    assert client.get(f"/jobs/{jid}", headers=other).status_code == 404                # not someone else's job
    r = client.get(f"/sessions/{sid}/export.md", headers=h)
    assert r.status_code == 200 and r.text.startswith("# Test lecture")
    assert r.headers["content-type"].startswith("text/markdown")
    assert f"lecture-{sid}.md" in r.headers["content-disposition"]
    assert client.get("/jobs/424242", headers=h).status_code == 404
