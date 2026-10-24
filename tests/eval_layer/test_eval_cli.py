"""The command-line surface, including the credentials-free promise."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import config
from evaluation import REPO_ROOT
from evaluation.cli import EXIT_ERROR, EXIT_OK, EXIT_THRESHOLD, main
from modules import sql_generator


def run_cli(*argv):
    return main(list(argv))


def test_oracle_run_writes_json_and_markdown_reports(eval_db, tmp_path, capsys):
    rc = run_cli("run", "--mode", "oracle", "--db", eval_db, "--output-dir", str(tmp_path), "--name", "t")
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "QuerySure evaluation" in out and "Execution accuracy" in out and "100.0%" in out
    data = json.load(open(tmp_path / "t.json"))
    assert data["metrics"]["overall"]["behavior_accuracy"] == 1.0
    md = (tmp_path / "t.md").read_text()
    assert md.startswith("# QuerySure evaluation report") and "| Execution accuracy" in md
    assert "Read this first" in md and "self-consistency" in md


def test_no_write_and_quiet_flags(eval_db, tmp_path, capsys):
    rc = run_cli("run", "--db", eval_db, "--limit", "3", "--no-write", "--quiet", "--output-dir", str(tmp_path))
    assert rc == EXIT_OK and capsys.readouterr().out == "" and list(tmp_path.iterdir()) == []


def test_thresholds_set_the_exit_code(eval_db, capsys):
    common = ["run", "--mode", "synthetic", "--db", eval_db, "--no-write", "--quiet"]
    assert run_cli(*common, "--min-execution-accuracy", "0.5") == EXIT_OK
    assert run_cli(*common, "--min-execution-accuracy", "0.99") == EXIT_THRESHOLD
    err = capsys.readouterr().err
    assert "THRESHOLD FAILURES" in err and "execution_accuracy" in err
    assert run_cli(*common, "--max-hallucination-rate", "0.01") == EXIT_THRESHOLD
    assert run_cli(*common, "--min-rejection-accuracy", "0.99") == EXIT_THRESHOLD
    # the perfect oracle clears every gate
    assert run_cli("run", "--mode", "oracle", "--db", eval_db, "--no-write", "--quiet",
                   "--min-execution-accuracy", "1", "--min-sql-validity", "1", "--min-behavior-accuracy", "1",
                   "--min-rejection-accuracy", "1", "--max-hallucination-rate", "0") == EXIT_OK


@pytest.mark.parametrize("argv", [
    ["run", "--category", "sorcery"],
    ["run", "--ids", "nope-999"],
    ["run", "--difficulty", "extreme"],
    ["run", "--mode", "replay"],                       # no --responses
    ["run", "--dataset", "/nonexistent/bench.json"],
    ["run", "--db", "/nonexistent/x.db"],
])
def test_usage_and_data_errors_exit_2_with_a_message(argv, capsys):
    assert run_cli(*argv, "--no-write", "--quiet") == EXIT_ERROR
    assert capsys.readouterr().err.startswith("error:")


def test_list_cases_and_validate_dataset(eval_db, capsys):
    assert run_cli("list-cases", "--category", "ambiguous") == EXIT_OK
    out = capsys.readouterr().out
    assert out.count("amb-") == 5 and "filter-001" not in out
    assert run_cli("validate-dataset", "--db", eval_db) == EXIT_OK
    assert "all gold SQL validates" in capsys.readouterr().out


def test_validate_dataset_reports_a_broken_benchmark(eval_db, tmp_path, capsys):
    bench = {"version": "t", "cases": [{
        "id": "bad-001", "category": "aggregation", "difficulty": "easy", "question": "count",
        "expected_behavior": "answer", "gold_sql": "SELECT COUNT(nope) FROM orders", "expected_tables": ["orders"]}]}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bench))
    assert run_cli("validate-dataset", "--dataset", str(p), "--db", eval_db) == EXIT_ERROR
    assert "bad-001" in capsys.readouterr().out


def test_custom_dataset_is_supported(eval_db, tmp_path, capsys):
    bench = {"version": "mini", "cases": [
        {"id": "one-001", "category": "aggregation", "difficulty": "easy", "question": "How many stores?",
         "expected_behavior": "answer", "gold_sql": "SELECT COUNT(*) AS n FROM stores", "expected_tables": ["stores"]},
        {"id": "two-001", "category": "impossible", "difficulty": "easy", "question": "Employee salaries?",
         "expected_behavior": "reject", "decline_reason": "nonexistent_data"}]}
    p = tmp_path / "mini.json"
    p.write_text(json.dumps(bench))
    assert run_cli("run", "--dataset", str(p), "--db", eval_db, "--no-write") == EXIT_OK
    assert "2/2 cases" in capsys.readouterr().out


# ---- live command --------------------------------------------------------

@pytest.fixture()
def no_credentials(monkeypatch):
    for mod in (config, sql_generator):
        for name in ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, "")

    def boom(*a, **k):
        raise AssertionError("network provider called")
    for name in ("_call_groq", "_call_openai", "_call_anthropic"):
        monkeypatch.setattr(sql_generator, name, boom)


def test_live_command_without_credentials_exits_2_and_says_how_to_run_offline(eval_db, no_credentials, capsys):
    assert run_cli("live", "--db", eval_db, "--no-write") == EXIT_ERROR
    err = capsys.readouterr().err
    assert "No LLM credentials" in err and "GROQ_API_KEY" in err and "--mode oracle" in err


def test_live_command_refuses_to_spend_money_non_interactively_without_yes(eval_db, no_credentials, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert run_cli("live", "--api-key", "k", "--db", eval_db, "--limit", "1", "--no-write") == EXIT_ERROR
    captured = capsys.readouterr()
    assert "real money" in captured.out and "pass --yes" in captured.err


def test_live_command_with_yes_records_responses(eval_db, no_credentials, monkeypatch, tmp_path, capsys):
    def fake(model, key, sp, up):
        return json.dumps({"sql": "SELECT COUNT(*) AS n FROM stores", "explanation": "x", "confidence": "High", "complexity": "Simple"})
    monkeypatch.setattr(sql_generator, "_call_groq", fake)
    rec = tmp_path / "rec.json"
    rc = run_cli("live", "--api-key", "k", "--yes", "--db", eval_db, "--ids", "agg-002,grp-001", "--no-write",
                 "--record-responses", str(rec))
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "LIVE evaluation" in out and "groq:" in out and "Estimated LLM cost: $" in out
    data = json.load(open(rec))
    assert set(data["responses"]) == {"agg-002", "grp-001"} and data["meta"]["recorded_from_mode"] == "live"
    # ...and the recording replays offline
    assert run_cli("run", "--mode", "replay", "--responses", str(rec), "--db", eval_db, "--ids", "agg-002,grp-001",
                   "--no-write", "--quiet") == EXIT_OK


def test_dotenv_loading_is_narrow_and_never_overrides_the_environment(tmp_path, monkeypatch):
    from evaluation.cli import load_llm_env_from_dotenv
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=from-dotenv\nOPENAI_API_KEY=also-dotenv\n"
                        "ENVIRONMENT=production\nDATABASE_URL=postgres://nope\nJWT_SECRET_KEY=x\n")
    for name in ("GROQ_API_KEY", "OPENAI_API_KEY", "ENVIRONMENT", "DATABASE_URL", "JWT_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "already-set")
    loaded = load_llm_env_from_dotenv(str(env_file))
    assert loaded == ["GROQ_API_KEY"]
    assert os.environ["GROQ_API_KEY"] == "from-dotenv"
    assert os.environ["OPENAI_API_KEY"] == "already-set"                 # real env wins
    for untouched in ("ENVIRONMENT", "DATABASE_URL", "JWT_SECRET_KEY"):  # non-LLM settings are never imported
        assert untouched not in os.environ
    monkeypatch.delenv("GROQ_API_KEY")
    assert load_llm_env_from_dotenv(str(tmp_path / "missing.env")) == []


def test_offline_run_does_not_read_dotenv(eval_db, monkeypatch):
    import evaluation.cli as cli
    monkeypatch.setattr(cli, "load_llm_env_from_dotenv", lambda *a, **k: (_ for _ in ()).throw(AssertionError(".env read")))
    assert run_cli("run", "--db", eval_db, "--limit", "2", "--no-write", "--quiet") == EXIT_OK


# ---- a real process, with every credential removed --------------------------

def _clean_env():
    env = {k: v for k, v in os.environ.items()
           if k not in ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ENVIRONMENT")}
    env["PYTHONWARNINGS"] = "ignore"
    return env


def test_module_entrypoint_runs_with_no_credentials_in_a_fresh_process():
    proc = subprocess.run(
        [sys.executable, "-m", "evaluation", "run", "--mode", "oracle", "--limit", "5", "--no-write", "--quiet"],
        cwd=REPO_ROOT, env=_clean_env(), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


def test_module_entrypoint_live_without_credentials_fails_cleanly_in_a_fresh_process():
    proc = subprocess.run([sys.executable, "-m", "evaluation", "live", "--no-write"],
                          cwd=REPO_ROOT, env=_clean_env(), capture_output=True, text=True, timeout=120)
    assert proc.returncode == EXIT_ERROR
    assert "No LLM credentials" in proc.stderr and "Traceback" not in proc.stderr
