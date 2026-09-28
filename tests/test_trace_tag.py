"""Trace tags: the overall session number; the other fields sit beside it, "-" if unknown."""
import json

import pytest

import inventory
import trace_tag


@pytest.fixture(autouse=True)
def _systems(tmp_path, monkeypatch):
    f = tmp_path / "systems_inventory.yaml"
    f.write_text("version: 1\nsystems:\n"
                 "  gotcha3:\n    site: lebanon\n    components:\n"
                 "      dumbo1: {type: acoustic, address: 10.0.0.1}\n"
                 "  gotcha5:\n    components: {}\n")
    monkeypatch.setattr(inventory, "SYSTEMS", f)
    inventory.load.cache_clear()
    inventory._systems_doc.cache_clear()
    (tmp_path / "t").mkdir()


def _write(d, name, entry):
    (d / f"{name}.jsonl").write_text(json.dumps(entry) + "\n")


def _rows(d):
    return {r["id"]: r for r in trace_tag.describe(d)}


def test_tag_is_only_the_overall_session_number(tmp_path):
    d = tmp_path / "t"
    _write(d, "a", {"origin": {"via": "slack", "user": "gal", "ts": 1790000000},
                    "findings": [{"data": {"node": "dumbo1"}}],
                    "usage": {"input": 9000, "output": 3000, "cache_read": 400}})
    r = _rows(d)["a"]
    assert r["tag"] == "0001"
    assert (r["system"], r["location"], r["tokens"], r["asker"]) == \
        ("gotcha3", "lebanon", 12400, "gal")


def test_unknown_fields_are_none(tmp_path):
    # by="console" is the channel, not a person; no usage recorded is not zero.
    d = tmp_path / "t"
    _write(d, "b", {"origin": {"by": "console", "question": "no signal", "ts": 1790000000}})
    r = _rows(d)["b"]
    assert (r["system"], r["location"], r["tokens"], r["asker"]) == (None,) * 4


def test_system_names_match_whole_words_only(tmp_path):
    _write(tmp_path / "t", "c", {"question": "is gotcha30 ok?", "started_at": 1790000000})
    assert _rows(tmp_path / "t")["c"]["system"] is None


def test_two_systems_is_unknown_not_a_guess(tmp_path):
    _write(tmp_path / "t", "d", {"question": "gotcha5 and gotcha3", "started_at": 1790000000})
    assert _rows(tmp_path / "t")["d"]["system"] is None


def test_tag_carries_overall_number_and_system_number_is_separate(tmp_path):
    d = tmp_path / "t"
    _write(d, "late", {"question": "gotcha3", "started_at": 1790000300})
    _write(d, "mid", {"question": "no system named", "started_at": 1790000200})
    _write(d, "early", {"question": "gotcha3", "started_at": 1790000100})
    rows = {r["id"]: r for r in trace_tag.describe(d)}
    assert [rows[k]["session"] for k in ("early", "mid", "late")] == [1, 2, 3]
    assert rows["mid"]["tag"] == "0002"
    assert rows["late"]["tag"] == "0003"
    assert rows["late"]["system_serial"] == 2 and rows["mid"]["system_serial"] is None


def test_numbers_stick_as_sessions_are_added(tmp_path):
    d = tmp_path / "t"
    _write(d, "a", {"question": "gotcha3", "started_at": 1790000100})
    first = _rows(d)
    _write(d, "b", {"question": "gotcha5", "started_at": 1790000200})
    again = {r["id"]: r for r in trace_tag.describe(d)}
    assert again["a"]["tag"] == first["a"]["tag"] == "0001"
    assert again["b"]["tag"] == "0002" and again["b"]["system_serial"] == 1
