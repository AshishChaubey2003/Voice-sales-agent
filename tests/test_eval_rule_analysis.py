import json

import pytest

from evals import rule_analysis as ra

GATE = 0.30


def case(cid, dist, kw=(), vec=("a", "b", "c")):
    return {"case_id": cid, "top_distance": dist, "vector_top": list(vec),
            "keyword_headings": list(kw), "keyword_count": len(kw)}


def test_confusion_math():
    m = ra.confusion({"a": True, "b": False, "c": True, "d": False}, {"a": True, "b": True, "c": False, "d": False})
    assert (m["tp"], m["fn"], m["fp"], m["tn"]) == (1, 1, 1, 1)
    assert m["precision"] == 0.5 and m["recall"] == 0.5 and m["specificity"] == 0.5
    assert m["balanced_accuracy"] == 0.5


def test_confusion_undefined_ratios_are_none_not_zero():
    m = ra.confusion({"a": False}, {"a": False})  # no positives, nothing accepted
    assert m["precision"] is None and m["recall"] is None and m["balanced_accuracy"] is None
    assert m["specificity"] == 1.0


def test_rule_semantics_gate_boundary_and_direction():
    assert ra.distance_ok(case("x", 0.30), GATE)        # boundary passes, like the live gate
    assert not ra.distance_ok(case("x", 0.31), GATE)    # larger distance = farther = rejected
    assert ra.distance_ok(case("x", 0.10), GATE)
    assert not ra.keyword_ok(case("x", 0.1))
    assert ra.keyword_ok(case("x", 0.9, kw=["h"]))
    assert ra.keyword_top_in_vector_top(case("x", 0.9, kw=["a", "z"]))
    assert not ra.keyword_top_in_vector_top(case("x", 0.9, kw=["z", "a"]))


def test_and_or_combinations():
    cases = [case("near_kw", 0.2, ["h"]), case("near_nokw", 0.2), case("far_kw", 0.5, ["h"]), case("far_nokw", 0.5)]
    labels = {c["case_id"]: {"answerable": True} for c in cases}
    result = ra.analyze(cases, labels, GATE)
    rules = {r["id"]: r["decisions"] for r in result["rules"]}
    assert rules["3"] == {"near_kw": True, "near_nokw": False, "far_kw": False, "far_nokw": False}
    assert rules["4"] == {"near_kw": True, "near_nokw": True, "far_kw": True, "far_nokw": False}


def test_unlabeled_case_is_an_error_not_a_guess():
    with pytest.raises(ValueError, match="no label"):
        ra.analyze([case("ghost", 0.1)], {}, GATE)


def test_uncertain_label_excluded_from_second_view():
    cases = [case("inj-discount", 0.5), case("ok", 0.2)]
    labels = {"inj-discount": {"answerable": True}, "ok": {"answerable": True}}
    rule1 = ra.analyze(cases, labels, GATE)["rules"][0]
    assert rule1["all_cases"]["fn"] == 1 and rule1["without_uncertain"]["fn"] == 0


def test_loader_accepts_tool_json_list_and_fixture_object(tmp_path):
    row = {"case_id": "x", "analysis": {"candidates": [{"heading": "h2", "distance": 0.5}, {"heading": "h1", "distance": 0.2}]},
           "keyword_headings": ["h1"]}
    listed = tmp_path / "a.json"
    listed.write_text(json.dumps([row]))
    wrapped = tmp_path / "b.json"
    wrapped.write_text(json.dumps({"provenance": {"gate": 0.3}, "rows": [row]}))
    for path in (listed, wrapped):
        rows, _ = ra.load_rows(path)
        c = ra.to_case(rows[0])
        assert c["top_distance"] == 0.2 and c["vector_top"] == ["h1", "h2"] and c["keyword_count"] == 1


def test_fixture_matches_dataset_labels_and_known_outcomes():
    rows, provenance = ra.load_rows(ra.DEFAULT_INPUT)
    cases = [ra.to_case(r) for r in rows]
    labels = ra.load_labels(ra.DEFAULT_DATASET)
    assert len(cases) == 19 and provenance["gate"] == 0.3
    assert sum(labels[c["case_id"]]["answerable"] for c in cases) == 9
    result = ra.analyze(cases, labels, 0.30)
    by_id = {r["id"]: r for r in result["rules"]}
    # Locks the transcription against the printed diagnostic output.
    assert by_id["1"]["false_accepts"] == ["unans-api", "unans-enterprise"]
    assert len(by_id["1"]["false_rejects"]) == 9
    assert by_id["2"]["false_accepts"] == ["inj-free-pro", "unans-api", "unans-ceo", "unans-enterprise", "unans-salesforce"]
    assert by_id["2"]["false_rejects"] == []
    assert by_id["3"]["all_cases"]["tp"] == 0 and by_id["4"]["all_cases"]["tp"] == 9
    # Every printed top-1 distance for the answerable misses is outside the gate.
    assert all(c["top_distance"] > 0.30 for c in cases if labels[c["case_id"]]["answerable"])


def test_main_runs_and_refuses_to_overwrite(tmp_path, capsys):
    out = tmp_path / "r.json"
    assert ra.main(["--json-out", str(out)]) == 0
    assert "NOT generalizable" in capsys.readouterr().out and out.exists()
    assert ra.main(["--json-out", str(out)]) == 2


def test_multiple_inputs_are_combined_and_duplicates_rejected(tmp_path, capsys):
    rows, _ = ra.load_rows(ra.DEFAULT_INPUT)
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text(json.dumps(rows[:9]))
    second.write_text(json.dumps(rows[9:]))
    assert ra.main(["--input", str(first), "--input", str(second)]) == 0
    combined = capsys.readouterr().out
    assert "19 cases (9 answerable, 10 unanswerable/off-topic)" in combined
    assert "diagnose_retrieval --json output" in combined
    assert ra.main(["--input", str(first), "--input", str(first)]) == 2
