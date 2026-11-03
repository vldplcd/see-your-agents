import json

import pytest

from see_your_agents.features.looks import looks
from see_your_agents.i18n import Problem

PATH = "/api/looks"
SID = "5e1d0001-7c2a-4b19-9d0e-2f6a8c3b0001"
FULL = {"shape": "star", "hue": 200, "desk": "round", "items": ["mug", "cat"], "label": "Бухгалтер"}


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("SYA_CONFIG_DIR", str(tmp_path))
    return tmp_path


def post(payload):
    try:
        status, _, body = looks.handle_post(None, PATH, json.dumps(payload).encode(), None)
    except Problem as refused:  # what the host answers for it
        return refused.status, {"error": refused.text()}
    return status, json.loads(body)


def get():
    status, _, body = looks.handle_get(None, PATH, {})
    assert status == 200
    return json.loads(body)


def test_empty_document_when_nothing_saved():
    assert get() == {"sessions": {}, "projects": {}}


def test_save_for_session_and_project_then_read_back(config):
    assert post({"session_id": SID, "look": FULL})[0] == 200
    status, doc = post({"project": "payments-api", "look": {"hue": None, "items": ["plant"]}})

    assert status == 200
    assert doc["sessions"][SID] == FULL
    assert doc["projects"]["payments-api"] == {
        "shape": None, "hue": None, "desk": None, "items": ["plant"], "label": "",
    }  # fmt: skip
    assert get() == doc
    assert json.loads((config / "looks.json").read_text()) == doc
    assert list(config.glob("*.tmp")) == []


def test_null_look_resets():
    post({"session_id": SID, "look": FULL})
    status, doc = post({"session_id": SID, "look": None})

    assert status == 200
    assert doc["sessions"] == {}


def test_label_is_trimmed_and_cut_to_40_chars():
    _, doc = post({"session_id": SID, "look": {"label": "  " + "я" * 60 + "  "}})

    assert doc["sessions"][SID]["label"] == "я" * 40


@pytest.mark.parametrize(
    "look",
    [
        {"colour": 1},
        {"hue": 360},
        {"hue": -1},
        {"hue": 1.5},
        {"hue": True},
        {"hue": "200"},
        {"shape": "Circle"},
        {"shape": "x" * 25},
        {"desk": 3},
        {"items": ["a", "b", "c", "d", "e"]},
        {"items": ["Mug"]},
        {"items": "mug"},
        {"label": 5},
        "circle",
    ],
)
def test_invalid_look_is_refused_and_nothing_is_written(look, config):
    status, body = post({"session_id": SID, "look": look})

    assert status == 400
    assert body["error"] and not body["error"].startswith("looks.")  # worded: the key is in the catalog
    assert not (config / "looks.json").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"look": FULL},
        {"session_id": SID, "project": "p", "look": FULL},
        {"session_id": SID},
        {"session_id": SID, "look": FULL, "extra": 1},
        {"session_id": "../etc", "look": FULL},
        {"project": "", "look": FULL},
        {"project": 5, "look": FULL},
    ],
)
def test_bad_request_shape_is_refused(payload):
    assert post(payload)[0] == 400


def test_non_json_body_is_refused():
    with pytest.raises(Problem) as refused:
        looks.handle_post(None, PATH, b"nope", None)
    assert (refused.value.key, refused.value.status) == ("app.bad_body", 400)


def test_a_refusal_is_worded_in_the_page_language():
    with pytest.raises(Problem) as refused:
        looks.clean_look({"items": ["a"] * 5})
    assert refused.value.text("en") == "items must be a list of at most 4 ids"
    assert refused.value.text("ru") == "items — список не длиннее 4 id"


def test_other_paths_are_not_ours():
    assert looks.handle_get(None, PATH + "/x", {}) is None
    assert looks.handle_post(None, PATH + "/x", b"{}", None) is None


def test_corrupt_file_is_set_aside_not_overwritten(config):
    (config / "looks.json").write_text("{broken")

    assert get() == {"sessions": {}, "projects": {}}
    assert (config / "looks.json.bad").read_text() == "{broken"


def test_invalid_entries_in_the_file_are_not_served(config):
    (config / "looks.json").write_text(
        json.dumps({"sessions": {SID: FULL, "other": {"hue": 999}}, "projects": []})
    )

    assert get() == {"sessions": {SID: FULL}, "projects": {}}
