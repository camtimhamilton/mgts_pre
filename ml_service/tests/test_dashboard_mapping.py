from app.knowledge import ROOT, load_mapping


def test_dashboard_mapping_is_explicit_and_keeps_ambiguous_questions_unknown():
    mapping = load_mapping(ROOT / "mappings/dashboard-demo.yaml")
    assert mapping.version == "dashboard-demo-1"
    ratings = [entry for entry in mapping.entries if entry.type == "rating"]
    assert len(ratings) == 6
    assert {entry.factor_id for entry in ratings} == {"R03", "R04", "R07"}
    assert not any(entry.question_id in {"f_motivation", "f_future", "f_mentor"} for entry in ratings)
    assert not any(entry.comparison_key for entry in ratings if entry.question_id == "f_workload")
    assert all((entry.scale_min, entry.scale_max) == (1, 10) for entry in ratings)
