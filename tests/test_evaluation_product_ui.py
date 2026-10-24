from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


def test_landing_has_reliability_evaluation_section():
    assert 'id="evaluation"' in INDEX
    assert "RELIABILITY EVALUATION" in INDEX
    assert "Evaluation is a separate engineering surface." in INDEX
    assert 'id="evalCoverage"' in INDEX
    assert 'id="evalDeepEvalStatus"' in INDEX
    assert 'id="evalSyntheticStatus"' in INDEX


def test_evaluation_methodology_explains_result_equivalence():
    assert 'id="evaluationMethodModal"' in INDEX
    assert "Schema / Semantic Grounding" in INDEX
    assert "Result Evaluation" in INDEX
    assert "result/equivalence checks" in INDEX


def test_product_metrics_are_loaded_from_api_not_hardcoded():
    assert "fetch('/api/evaluation/product-summary'" in APP
    assert "data.metrics.result_accuracy" in APP
    assert "data.metrics.sql_execution_success" in APP
    assert "data.metrics.schema_grounding" in APP
    assert "data.metrics.hallucinated_identifiers" in APP
    assert "data.metrics.correct_rejection" in APP


def test_product_ui_does_not_embed_fake_percentages():
    section = INDEX.split('id="evaluation"', 1)[1].split('<footer', 1)[0]
    assert "100%" not in section
    assert "99%" not in section
    assert "95%" not in section
