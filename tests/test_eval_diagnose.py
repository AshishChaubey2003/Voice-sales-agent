import json

from evals import diagnose_retrieval as d


def test_similarity_is_one_minus_distance():
    assert d.similarity(0.25) == 0.75
    assert d.similarity(0.0) == 1.0


def test_gate_uses_distance_lower_is_closer():
    assert d.gate_status(0.30, 0.30) == "within"      # boundary passes, like gate_by_distance
    assert d.gate_status(0.31, 0.30) == "outside"
    assert d.gate_status(0.05, 0.30) == "within"
    assert d.gate_status(0.9, None) == "no-gate"


def test_gate_matches_production_gate():
    import uuid

    from api.knowledge import RetrievedChunk, gate_by_distance

    for distance in (0.10, 0.30, 0.3001, 0.45):
        chunk = RetrievedChunk(uuid.uuid4(), "t", "h", "c", vector_distance=distance)
        kept, _ = gate_by_distance([chunk], [], 0.30)
        assert bool(kept) == (d.gate_status(distance, 0.30) == "within")


def test_failed_empty_case_selection():
    lines = [
        {"case_id": "a", "status": "ok", "passed": False, "scores": {"empty_retrieval": 1.0}},
        {"case_id": "b", "status": "ok", "passed": False, "scores": {"empty_retrieval": 0.0}},
        {"case_id": "c", "status": "ok", "passed": True, "scores": {"empty_retrieval": 1.0}},
        {"case_id": "d", "status": "skipped", "passed": None, "scores": {}},
        {"case_id": "e", "status": "error", "passed": False, "scores": {}},
    ]
    assert d.failed_empty_case_ids("\n".join(json.dumps(x) for x in lines) + "\n\n") == ["a"]


def test_analyze_sorts_and_reports_expected_gap():
    out = d.analyze([("far", 0.50), ("want", 0.34), ("near", 0.20)], ["want"], 0.30)
    assert [c["heading"] for c in out["candidates"]] == ["near", "want", "far"]
    assert out["top"]["heading"] == "near" and out["top"]["gate"] == "within"
    exp = out["expected"][0]
    assert exp["rank"] == 2 and exp["gate"] == "outside"
    assert round(exp["distance_over_gate"], 6) == 0.04
    assert out["within_gate_count"] == 1


def test_analyze_expected_missing_and_empty():
    assert d.analyze([("x", 0.2)], ["nope"], 0.3)["expected"] == [{"heading": "nope", "found": False}]
    out = d.analyze([], ["nope"], 0.3)
    assert out["top"] is None and out["within_gate_count"] == 0


def test_render_table_states_direction():
    row = {"case_id": "c", "analysis": d.analyze([("h", 0.4)], ["h"], 0.30)}
    text = d.render_table([row], 0.30)
    assert "lower = closer" in text and "+0.100" in text and "outside" in text


def test_main_requires_selection(capsys):
    assert d.main([]) == 2
