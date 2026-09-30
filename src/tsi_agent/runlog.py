"""Journal de run : un événement JSON par ligne, résumé et suivi des coûts.

Le journal courant est porté par un contextvar pour que les composants Haystack
restent sérialisables (aucun objet journal dans leurs paramètres d'init).
"""

import contextvars
import csv
import json
import logging
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_logger = logging.getLogger("tsi_agent")
_current: contextvars.ContextVar["RunLog | None"] = contextvars.ContextVar("tsi_runlog", default=None)

LLM_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
COSTS_HEADER = ["run_id", "started_at", "llm_calls", *LLM_TOKEN_FIELDS, "cost_usd", "duration_s"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunLog:
    def __init__(self, logs_dir: str | Path, run_id: str | None = None):
        self.logs_dir = Path(logs_dir)
        (self.logs_dir / "runs").mkdir(parents=True, exist_ok=True)
        if run_id is None:
            base_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.run_id = base_id
            counter = 2
            while (self.logs_dir / "runs" / f"{self.run_id}.jsonl").exists():
                self.run_id = f"{base_id}-{counter}"
                counter += 1
        else:
            self.run_id = run_id
        self.path = self.logs_dir / "runs" / f"{self.run_id}.jsonl"
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._t0 = time.monotonic()
        self.counts: Counter[str] = Counter()
        self.alerts: list[str] = []
        self.llm_calls = 0
        self.llm_tokens: Counter[str] = Counter()
        self.cost_usd = 0.0
        self.extra: dict[str, Any] = {}
        self.final_summary: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")

    def event(self, component: str, action: str, target: str | None = None, result: str = "ok", **fields: Any) -> None:
        record = {"ts": _now(), "run_id": self.run_id, "component": component, "action": action,
                  "target": target, "result": result, **fields}
        with self._lock:
            self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
            self.counts[f"{action}:{result}"] += 1
            # Les embeddings (pipeline `index`) comptent dans les tokens et le cout, pas dans llm_calls.
            if action in ("llm_call", "embed"):
                if action == "llm_call":
                    self.llm_calls += 1
                for name in LLM_TOKEN_FIELDS:
                    self.llm_tokens[name] += int(fields.get(name) or 0)
                self.cost_usd += float(fields.get("cost_usd") or 0.0)
            if action == "alert":
                self.alerts.append(str(fields.get("message") or target))

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id,
                "started_at": self.started_at,
                "duration_s": round(time.monotonic() - self._t0, 1),
                "counts": dict(sorted(self.counts.items())),
                "alerts": list(self.alerts),
                "llm_calls": self.llm_calls,
                "tokens": {name: self.llm_tokens[name] for name in LLM_TOKEN_FIELDS},
                "cost_usd": round(self.cost_usd, 4),
                **self.extra,
            }

    def close(self) -> dict[str, Any]:
        summary = self.summary()
        self._fh.close()
        (self.logs_dir / "runs" / f"{self.run_id}.summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        costs = self.logs_dir / "costs.csv"
        new_file = not costs.exists()
        with costs.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(COSTS_HEADER)
            writer.writerow([self.run_id, self.started_at, self.llm_calls,
                             *(summary["tokens"][n] for n in LLM_TOKEN_FIELDS),
                             summary["cost_usd"], summary["duration_s"]])
        return summary


@contextmanager
def open_run(logs_dir: str | Path, run_id: str | None = None) -> Iterator[RunLog]:
    run = RunLog(logs_dir, run_id)
    token = _current.set(run)
    run.extra["status"] = "ok"
    try:
        yield run
    except BaseException as exc:
        run.extra["status"] = "error"
        run.extra["error"] = repr(exc)[:500]
        raise
    finally:
        _current.reset(token)
        run.final_summary = run.close()


def current() -> RunLog | None:
    return _current.get()


def event(component: str, action: str, target: str | None = None, result: str = "ok", **fields: Any) -> None:
    run = _current.get()
    if run is None:
        _logger.info("%s %s %s %s %s", component, action, target, result, fields)
        return
    run.event(component, action, target, result, **fields)
