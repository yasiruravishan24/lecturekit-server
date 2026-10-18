"""Request validation on every kind of input: ids, text, numbers, files, bodies."""
import pytest

from tests.conftest import PASSWORD, file_part, make_session, new_user, register_body
from tests.test_live import chunk, open_live


def error_code(r) -> str:
    return r.json()["error"]["code"]


@pytest.mark.parametrize("path", ["/sessions/{}", "/jobs/{}", "/sessions/{}/notes", "/sessions/{}/quiz",
                                  "/sessions/{}/quiz/attempts", "/sessions/{}/export.md", "/modules/{}/sessions", "/courses/{}/modules"])
def test_odd_ids_in_the_path_never_crash(client, path):
    h = new_user(client)
    for bad, expected in (("99999999999999999999999", 422), ("0", 422), ("-3", 422), ("abc", 422)):
        r = client.get(path.format(bad), headers=h)
        assert r.status_code == expected and error_code(r) == "validation", (path, bad, r.text)
    assert client.get(path.format(2_000_000), headers=h).status_code in (404, 405)       # a normal id that does not exist


def test_odd_ids_on_patch_and_delete_never_crash(client):
    h = new_user(client)
    for path in ("/courses/99999999999999999999999", "/modules/99999999999999999999999", "/sessions/99999999999999999999999", "/courses/0", "/modules/abc"):
        for method, body in (("PATCH", {"name": "x", "title": "x"}), ("DELETE", None)):
            r = client.request(method, path, json=body, headers=h)
            assert r.status_code == 422 and error_code(r) == "validation", (method, path, r.text)
    for path in ("/sessions/99999999999999999999999/process", "/sessions/99999999999999999999999/retry", "/sessions/99999999999999999999999/live"):
        assert client.post(path, headers=h).status_code == 422, path


def test_names_and_titles_refuse_control_characters_and_collapse_spaces(client):
    h = new_user(client)
    for body in ({"name": "a\nb"}, {"name": "a\x00b"}, {"name": "tab\there"}, {"name": "   "}, {"name": 5}, {"name": None}, {}):
        r = client.post("/courses", json=body, headers=h)
        assert r.status_code == 422 and error_code(r) == "validation", body
    assert client.post("/courses", json={"name": "  Intro   to   CS "}, headers=h).json()["name"] == "Intro to CS"
    cid = client.post("/courses", json={"name": "C"}, headers=h).json()["id"]
    assert client.patch(f"/courses/{cid}", json={"name": "x\ny"}, headers=h).status_code == 422
    assert client.post(f"/courses/{cid}/modules", json={"name": "w\x07"}, headers=h).status_code == 422
    mid = client.post(f"/courses/{cid}/modules", json={"name": "M"}, headers=h).json()["id"]
    assert client.post(f"/modules/{mid}/sessions", json={"title": "line1\nline2"}, headers=h).status_code == 422
    assert client.post(f"/modules/{mid}/sessions", json={"title": "x" * 161}, headers=h).status_code == 422


def test_bodies_must_be_json_objects(client):
    h = {**new_user(client), "Content-Type": "application/json"}
    for raw in (b"{bad", b"[1]", b'"text"', b"", b"null"):
        r = client.post("/courses", content=raw, headers=h)
        assert r.status_code == 422 and error_code(r) == "validation", raw


def test_register_fields(client):
    ok = register_body("n1@example.com")
    cases = [{"full_name": "A\x00B"}, {"full_name": "A\nB"}, {"full_name": "x" * 121}, {"accept_terms": "yes"}, {"accept_terms": 1},
             {"email": "a b@example.com"}, {"email": "x" * 250 + "@example.com"}, {"password": "x" * 201}, {"password": 12345678}]
    for over in cases:
        r = client.post("/auth/register", json={**ok, **over})
        assert r.status_code == 422 and error_code(r) == "validation", over
    r = client.post("/auth/register", json={**ok, "full_name": "  Ann   Lee "})
    assert r.status_code == 201 and r.json()["user"]["full_name"] == "Ann Lee"


def test_login_and_resend_bodies(client):
    for body in ({}, {"email": "a@example.com"}, {"email": 5, "password": "x"}, {"email": "a@example.com", "password": "x" * 201}):
        assert client.post("/auth/login", json=body).status_code == 422, body
    for body in ({}, {"email": 5}, {"email": "x" * 255}):
        assert client.post("/auth/resend-verification", json=body).status_code == 422, body


def test_verification_code_must_be_six_digits(client):
    for code in ("abcdef", "12345", "1234567", "12 34", "", "1" * 50, 123456):
        r = client.post("/auth/verify-email", json={"email": "a@example.com", "code": code})
        assert r.status_code == 422 and error_code(r) == "validation", code
    r = client.post("/auth/verify-email", json={"email": "a@example.com", "code": "123 456"})
    assert error_code(r) == "invalid_code"                         # a code typed as "123 456" is read as 123456


def test_google_body(client):
    for body in ({}, {"id_token": 5}, {"id_token": "x" * 4001}, {"id_token": "x", "accept_terms": "yes"}):
        assert client.post("/auth/google", json=body).status_code == 422, body


def test_empty_and_wrong_uploads(client):
    h = new_user(client)
    sid = make_session(client, h)
    r = client.post(f"/sessions/{sid}/upload", headers=h, files={"recording": file_part("a.mp4", b"")})
    assert r.status_code == 422 and error_code(r) == "validation" and "empty" in r.json()["error"]["message"]
    assert client.get(f"/sessions/{sid}", headers=h).json()["files"] == []                 # nothing was stored
    r = client.post(f"/sessions/{sid}/upload", headers=h, files={"recording": file_part("notes.txt", b"hi")})
    assert r.status_code == 422
    r = client.post(f"/sessions/{sid}/upload", headers=h, files={"other": file_part("a.mp4")})
    assert r.status_code == 422 and error_code(r) == "missing_input"
    r = client.post(f"/sessions/{sid}/upload", headers=h, files={"recording": file_part("../../evil.mp4", b"data")})
    assert r.status_code == 200 and client.get(f"/sessions/{sid}", headers=h).json()["files"][0]["original_name"].endswith("evil.mp4")


def test_live_numbers_are_checked(client):
    h = new_user(client)
    sid = open_live(client, h)
    for index in ("-1", "x", "1000001", "99999999999999999999"):
        r = client.post(f"/sessions/{sid}/live/chunks", params={"index": index}, headers=h, files={"chunk": file_part("c.wav")})
        assert r.status_code == 422 and error_code(r) == "validation", index
    assert client.post(f"/sessions/{sid}/live/chunks", headers=h, files={"chunk": file_part("c.wav")}).status_code == 422     # no index
    r = chunk(client, h, sid, 500)
    assert r.status_code == 429 and error_code(r) == "chunk_limit"                         # inside the number range, over the limit
    assert chunk(client, h, sid, 0, "c.wav", b"").status_code == 422                       # empty chunk
    assert chunk(client, h, sid, 0, "c.txt").status_code == 422                            # not audio
    for total in ("0", "-1", "1001", "99999999999999999999", "x"):
        assert client.get(f"/sessions/{sid}/live", params={"total": total}, headers=h).status_code == 422, total
        assert client.post(f"/sessions/{sid}/live/finish", params={"total": total}, headers=h).status_code == 422, total
    assert client.post(f"/sessions/{sid}/live/finish", headers=h).status_code == 422       # total is required
    r = client.post(f"/sessions/{sid}/live/finish", params={"total": 201}, headers=h)
    assert r.status_code == 422 and error_code(r) == "validation"                          # more than the chunk limit


def test_quiz_answers_are_bounded(client, app, fake_lk):
    from tests.test_results import done_session
    h = new_user(client)
    sid = done_session(client, h)
    for body in ({"answers": [0] * 101}, {"answers": "abc"}, {"answers": [1.5, 0, 0]}, {"answers": ["a", 0, 0]}, {}, {"answers": [0, 0, 7]},
                 {"answers": [0, 0, -1]}, {"answers": [0, 0]}, {"answers": [0, 0, 0, 0]}):
        r = client.post(f"/sessions/{sid}/quiz/attempts", json=body, headers=h)
        assert r.status_code == 422 and error_code(r) == "validation", body
    assert client.post(f"/sessions/{sid}/quiz/attempts", json={"answers": [10**30, 0, 0]}, headers=h).status_code == 422
