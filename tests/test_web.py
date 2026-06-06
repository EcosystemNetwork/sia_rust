"""Tests for the runs-visualizer data layer and HTTP API."""

import json
from pathlib import Path

import pytest

from sia.web import runs as rd


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    """A minimal but realistic runs/ tree: one run, two generations."""
    root = tmp_path / "runs"
    gen1 = root / "run_7" / "gen_1"
    gen2 = root / "run_7" / "gen_2"
    (gen1 / "agent_execution").mkdir(parents=True)
    gen2.mkdir(parents=True)

    (root / "run_7" / "context.md").write_text(
        "# Run Context: run_7\n\n"
        "**Task**: /tasks/gpqa\n"
        "**Meta Model**: kimi\n"
        "**Task Model**: haiku\n"
        "**Agent impl**: openhands\n"
        "**Started**: 2026-06-05 13:31:32\n"
        "**Max Generations**: 3\n\n"
        "---\n\n## Generation 1\n**Status**: ok\n",
        encoding="utf-8",
    )

    (gen1 / "target_agent.py").write_text("print('hello')\n", encoding="utf-8")
    (gen1 / "meta_agent_prompt.txt").write_text("meta prompt body", encoding="utf-8")
    (gen1 / "evaluation_results.json").write_text(
        json.dumps(
            {
                "total_questions": 4,
                "correct": 2,
                "incorrect": 2,
                "accuracy": 0.5,
                "accuracy_percent": 50.0,
                "details": [
                    {"question_id": 1, "domain": "Physics", "is_correct": True},
                    {"question_id": 2, "domain": "Physics", "is_correct": False},
                    {"question_id": 3, "domain": "Biology", "is_correct": True},
                    {"question_id": 4, "domain": "Biology", "is_correct": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    (gen1 / "agent_execution" / "execution_q1.json").write_text(
        json.dumps(
            [
                {"role": "system", "content": [{"type": "text", "text": "You are an expert."}]},
                {"role": "user", "content": "Question 1?"},
                {"role": "assistant", "content": [{"type": "text", "text": "Answer: A"}]},
            ]
        ),
        encoding="utf-8",
    )

    (gen2 / "improvement.md").write_text("# Plan\n- do better\n", encoding="utf-8")

    # Telemetry per generation (issue #64/#88): the {generations, cumulative}
    # shape the telemetry layer writes; the run-level endpoints fold these.
    (gen1 / "telemetry.json").write_text(
        json.dumps(
            {
                "generations": [
                    {
                        "generation": 1,
                        "input_tokens": 100,
                        "output_tokens": 40,
                        "num_api_calls": 3,
                        "num_tool_calls": 5,
                        "duration_ms": 1200,
                    }
                ],
                "cumulative": {
                    "generation": 1,
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "num_api_calls": 3,
                    "num_tool_calls": 5,
                    "duration_ms": 1200,
                },
            }
        ),
        encoding="utf-8",
    )
    (gen2 / "telemetry.json").write_text(
        json.dumps(
            {
                "generations": [
                    {
                        "generation": 2,
                        "input_tokens": 200,
                        "output_tokens": 60,
                        "num_api_calls": 4,
                        "num_tool_calls": 7,
                        "duration_ms": 1800,
                    }
                ],
                "cumulative": {
                    "generation": 2,
                    "input_tokens": 200,
                    "output_tokens": 60,
                    "num_api_calls": 4,
                    "num_tool_calls": 7,
                    "duration_ms": 1800,
                },
            }
        ),
        encoding="utf-8",
    )

    # Closed-loop artifacts (issue #84/#85): a weight decision + its update on gen 2.
    (gen2 / "scheduler_decision.json").write_text(
        json.dumps(
            {
                "generation": 2,
                "decision": "weight",
                "recommended_next": "harness",
                "harness_efficiency": 0.01,
                "weight_efficiency": 0.05,
                "harness_plateaued": True,
                "rationale": "harness plateaued; try weights",
            }
        ),
        encoding="utf-8",
    )
    (gen2 / "weight_update.json").write_text(
        json.dumps(
            {
                "generation": 2,
                "kind": "lora",
                "updater": "demo",
                "num_examples": 12,
                "loss_before": 0.9,
                "loss_after": 0.6,
                "updated": True,
                "details": "ok",
            }
        ),
        encoding="utf-8",
    )
    return root


def test_list_runs_summary(runs_root):
    runs = rd.list_runs(runs_root)
    assert len(runs) == 1
    r = runs[0]
    assert r.name == "run_7"
    assert r.agent_impl == "openhands"
    assert r.task_model == "haiku"
    assert r.max_generations == 3
    assert r.num_generations == 2
    assert r.best_accuracy_percent == 50.0


def test_get_run_detail_and_domains(runs_root):
    detail = rd.get_run(runs_root, "run_7")
    assert detail is not None
    assert detail.context_md is not None
    assert detail.context_md.startswith("# Run Context")
    gen1 = next(g for g in detail.generations if g.name == "gen_1")
    assert gen1.eval is not None
    assert gen1.eval.accuracy_percent == 50.0
    assert "target_agent" in gen1.artifacts
    assert "meta_prompt" in gen1.artifacts
    assert gen1.trajectories == [1]
    domains = {d.domain: d for d in gen1.domains}
    assert domains["Physics"].correct == 1 and domains["Physics"].total == 2
    assert domains["Biology"].accuracy_percent == 50.0


def test_eval_details_and_artifacts(runs_root):
    details = rd.get_eval_details(runs_root, "run_7", "gen_1")
    assert details is not None and len(details) == 4
    assert rd.get_artifact_text(runs_root, "run_7", "gen_1", "target_agent") == "print('hello')\n"
    improvement = rd.get_artifact_text(runs_root, "run_7", "gen_2", "improvement")
    assert improvement is not None and improvement.startswith("# Plan")


def test_trajectory_normalization(runs_root):
    turns = rd.get_trajectory(runs_root, "run_7", "gen_1", 1)
    assert turns is not None
    assert [t["role"] for t in turns] == ["system", "user", "assistant"]
    assert turns[0]["text"] == "You are an expert."
    assert turns[1]["text"] == "Question 1?"
    assert turns[2]["text"] == "Answer: A"


def test_missing_lookups_return_none(runs_root):
    assert rd.get_run(runs_root, "run_999") is None
    assert rd.get_trajectory(runs_root, "run_7", "gen_1", 999) is None
    assert rd.get_artifact_text(runs_root, "run_7", "gen_1", "nope") is None


@pytest.mark.parametrize("evil", ["..", "../etc", "run_7/../run_7", "foo/bar", ".", "/abs"])
def test_path_traversal_is_blocked(runs_root, evil):
    assert rd.get_run(runs_root, evil) is None
    assert rd._resolve_gen(runs_root, evil, "gen_1") is None
    assert rd._resolve_gen(runs_root, "run_7", evil) is None


def test_run_telemetry_folds_generations(runs_root):
    tele = rd.get_run_telemetry(runs_root, "run_7")
    assert tele is not None
    assert len(tele["generations"]) == 2
    c = tele["cumulative"]
    assert c["input_tokens"] == 300  # 100 + 200
    assert c["output_tokens"] == 100  # 40 + 60
    assert c["num_api_calls"] == 7
    assert c["num_tool_calls"] == 12
    assert c["duration_ms"] == 3000
    assert c["generation"] == 2  # number of gens folded
    assert rd.get_run_telemetry(runs_root, "run_999") is None


def test_run_metrics_summary_series(runs_root):
    metrics = rd.get_run_metrics_summary(runs_root, "run_7")
    assert metrics is not None
    rows = metrics["generations"]
    assert [r["generation"] for r in rows] == [1, 2]
    g1 = rows[0]
    assert g1["score"] == 50.0
    assert g1["total_tokens"] == 140  # 100 + 40
    assert rows[1]["score"] is None  # gen_2 has no eval
    totals = metrics["totals"]
    assert totals["num_generations"] == 2
    assert totals["best_score"] == 50.0
    assert totals["total_tokens"] == 400  # 140 + 260
    assert rd.get_run_metrics_summary(runs_root, "run_999") is None


def test_scheduler_decision_and_weights(runs_root):
    decision = rd.get_scheduler_decision(runs_root, "run_7", "gen_2")
    assert decision is not None and decision["decision"] == "weight"
    assert decision["harness_plateaued"] is True
    weights = rd.get_weight_update(runs_root, "run_7", "gen_2")
    assert weights is not None and weights["num_examples"] == 12
    assert weights["loss_before"] == 0.9 and weights["loss_after"] == 0.6
    # gen_1 has neither artifact.
    assert rd.get_scheduler_decision(runs_root, "run_7", "gen_1") is None
    assert rd.get_weight_update(runs_root, "run_7", "gen_1") is None


def test_scheduler_timeline(runs_root):
    timeline = rd.get_scheduler_timeline(runs_root, "run_7")
    assert timeline is not None
    assert timeline["total"] == 1
    assert timeline["counts"] == {"harness": 0, "weight": 1, "both": 0}
    assert timeline["decisions"][0]["generation"] == 2
    assert timeline["decisions"][0]["rationale"].startswith("harness plateaued")
    assert rd.get_scheduler_timeline(runs_root, "run_999") is None


def test_api_endpoints(runs_root):
    from fastapi.testclient import TestClient

    from sia.web import create_app

    client = TestClient(create_app(runs_root))

    assert client.get("/api/runs").json()[0]["name"] == "run_7"
    assert client.get("/api/runs/run_7").json()["agent_impl"] == "openhands"
    assert len(client.get("/api/runs/run_7/gens/gen_1/eval").json()) == 4
    assert "hello" in client.get("/api/runs/run_7/gens/gen_1/artifact/target_agent").text
    assert client.get("/api/runs/run_7/gens/gen_1/trajectory/1").json()[0]["role"] == "system"
    assert client.get("/api/runs/run_7/telemetry").json()["cumulative"]["input_tokens"] == 300
    assert client.get("/api/runs/run_7/metrics").json()["totals"]["best_score"] == 50.0
    assert client.get("/api/runs/run_7/scheduler").json()["total"] == 1
    assert client.get("/api/runs/run_7/gens/gen_2/scheduler").json()["decision"] == "weight"
    assert client.get("/api/runs/run_7/gens/gen_2/weights").json()["num_examples"] == 12
    assert client.get("/api/runs/run_7/gens/gen_2/telemetry").json()["cumulative"]["output_tokens"] == 60
    assert client.get("/api/runs/run_7/gens/gen_1/scheduler").status_code == 404
    assert client.get("/api/runs/run_404").status_code == 404
    assert client.get("/").status_code == 200
