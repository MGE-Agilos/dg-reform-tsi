import contextvars
import csv
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tsi_agent import runlog


def test_events_summary_and_costs(tmp_path):
    with runlog.open_run(tmp_path, run_id="r1") as run:
        runlog.event("C", "fetch", "u1")
        runlog.event("C", "llm_call", "p1", input_tokens=100, output_tokens=20, cost_usd=0.01)
        runlog.event("C", "llm_call", "p2", input_tokens=50, output_tokens=10, cache_read_tokens=5, cost_usd=0.02)
        runlog.event("C", "alert", "FR", "warning", message="0 projects")
    lines = [json.loads(l) for l in (tmp_path / "runs" / "r1.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [l["action"] for l in lines] == ["fetch", "llm_call", "llm_call", "alert"]
    assert lines[0]["run_id"] == "r1" and lines[0]["target"] == "u1"
    summary = json.loads((tmp_path / "runs" / "r1.summary.json").read_text(encoding="utf-8"))
    assert summary["llm_calls"] == 2
    assert summary["tokens"] == {"input_tokens": 150, "output_tokens": 30, "cache_read_tokens": 5,
                                 "cache_creation_tokens": 0}
    assert summary["cost_usd"] == 0.03
    assert summary["alerts"] == ["0 projects"]
    assert run.final_summary == summary
    rows = list(csv.reader((tmp_path / "costs.csv").open(encoding="utf-8")))
    assert rows[0] == runlog.COSTS_HEADER and rows[1][0] == "r1" and rows[1][2] == "2"


def test_event_outside_run_does_not_fail():
    runlog.event("C", "fetch", "u")  # va au logging standard


def test_threads_need_copied_context(tmp_path):
    with runlog.open_run(tmp_path, run_id="r2") as run:
        with ThreadPoolExecutor(2) as pool:
            for i in range(4):
                pool.submit(contextvars.copy_context().run, runlog.event, "C", "write", f"p{i}")
        assert run.counts["write:ok"] == 4


def test_crash_is_recorded_and_reraised(tmp_path):
    import pytest
    with pytest.raises(RuntimeError):
        with runlog.open_run(tmp_path, run_id="boom"):
            runlog.event("C", "fetch", "u")
            raise RuntimeError("kaput")
    summary = json.loads((tmp_path / "runs" / "boom.summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "error" and "kaput" in summary["error"]
    assert summary["counts"] == {"fetch:ok": 1}


def test_clean_run_has_ok_status(tmp_path):
    with runlog.open_run(tmp_path, run_id="fine") as run:
        pass
    assert run.final_summary["status"] == "ok"


def test_automatic_run_ids_do_not_collide(tmp_path):
    first = runlog.RunLog(tmp_path)
    second = runlog.RunLog(tmp_path)
    assert first.run_id != second.run_id
    first.close()
    second.close()


def test_embed_events_count_tokens_and_cost_but_not_llm_calls(tmp_path):
    from tsi_agent import runlog
    with runlog.open_run(tmp_path, "e1") as run:
        runlog.event("VectorLayerWriter", "embed", "m", input_tokens=1000, cost_usd=0.00002)
    s = run.final_summary
    assert s["llm_calls"] == 0 and s["tokens"]["input_tokens"] == 1000 and s["cost_usd"] == 0.0
    assert run.cost_usd == pytest.approx(0.00002)
