"""The answer eval set: real clerk questions with what a good answer must contain.

Several people add questions (on the /eval page or by editing eval/questions.yaml); one run
asks every question through the normal Q&A path and scores the answers, so a change to the
prompt, the retrieval mode or the knowledge base can be checked before clerks see it.

Each item in eval/questions.yaml:
  id: q-001                      # given automatically
  question: 外州车转入 NSW 需要哪些材料？
  collection: rego               # optional, default rego
  expect: [reg-interstate-transfer]   # articles a good answer cites (any one is enough)
  must_include: ["Blue Slip", "$45"]  # facts the answer must state; figures match in any form
  should_refuse: false           # true: the KB does not cover this, the answer must say so
  added_by: Zeyu
  notes: from the 2026-10 counter log

Scores per answer, all without a model call: cited an expected article, contains every
must_include fact, refused exactly when it should, and has no figure missing from its sources.
`judge=True` adds one model call per question asking whether the answer is faithful to the
cited articles and answers the question (it costs usage, so it is off by default).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .llm import text_of
from .numbers import figures

_lock = threading.Lock()  # several reviewers may save at once from the web page


@dataclass
class EvalItem:
    id: str
    question: str
    collection: str = "rego"
    expect: list[str] = field(default_factory=list)
    must_include: list[str] = field(default_factory=list)
    should_refuse: bool = False
    added_by: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "EvalItem":
        def strings(v) -> list[str]:
            if not v:
                return []
            return [str(x).strip() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]

        return cls(id=str(d.get("id") or ""), question=str(d["question"]).strip(),
                   collection=str(d.get("collection") or "rego"), expect=strings(d.get("expect")),
                   must_include=strings(d.get("must_include")),
                   should_refuse=bool(d.get("should_refuse")), added_by=str(d.get("added_by") or ""),
                   notes=str(d.get("notes") or ""))

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v or k in {"id", "question"}}


def load_items(path: Path) -> list[EvalItem]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    items = [EvalItem.from_dict(d) for d in raw]
    _number(items)
    return items


def save_items(path: Path, items: list[EvalItem]) -> None:
    _number(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump([i.to_dict() for i in items], allow_unicode=True, sort_keys=False, width=100)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# Answer eval set. Edit here or on the /eval page.\n" + text, encoding="utf-8")
    tmp.replace(path)


def _number(items: list[EvalItem]) -> None:
    used = {i.id for i in items if i.id}
    n = 1
    for item in items:
        if not item.id:
            while f"q-{n:03d}" in used:
                n += 1
            item.id = f"q-{n:03d}"
            used.add(item.id)


def upsert(path: Path, data: dict) -> EvalItem:
    """Add a question, or replace the one with the same id."""
    item = EvalItem.from_dict(data)
    if not item.question:
        raise ValueError("问题不能为空")
    with _lock:
        items = load_items(path)
        for i, old in enumerate(items):
            if item.id and old.id == item.id:
                items[i] = item
                break
        else:
            items.append(item)
        save_items(path, items)
    return item


def remove(path: Path, item_id: str) -> None:
    with _lock:
        save_items(path, [i for i in load_items(path) if i.id != item_id])


# ------------------------------------------------------------------ scoring

def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def has_fact(answer: str, fact: str) -> bool:
    """A figure ("$45", "14 days") matches in any written form; other text as a phrase."""
    want = {f.key for f in figures(fact)}
    if want:
        return want <= {f.key for f in figures(answer)}
    return _plain(fact) in _plain(answer)


JUDGE_PROMPT = """\
You check answers given to rego clerks. You get the clerk's question, the knowledge base \
articles the answer cited, and the answer. Reply with JSON only: \
{"faithful": true|false, "answers_question": true|false, "note": "<one short sentence>"}. \
faithful: every statement in the answer is supported by the articles. answers_question: the \
answer addresses what was asked (a plain "the knowledge base does not cover this" counts as \
answering when the articles indeed do not cover it)."""


def _judge(qa, item: EvalItem, answer) -> dict:
    cited = {s.split("#")[0] for s in answer.sources}
    texts = "\n\n".join(a.render_for_model() for a in qa.kb.live(item.collection) if a.id in cited)
    response = qa.llm.create(
        "judge", model=qa.model, max_tokens=2000, output_config={"effort": "low"},
        system=JUDGE_PROMPT,
        messages=[{"role": "user", "content": f"<question>{item.question}</question>\n"
                   f"<articles>\n{texts or '(none cited)'}\n</articles>\n<answer>\n{answer.text}\n</answer>"}])
    raw = text_of(response)
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        verdict = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        verdict = {}
    return {"faithful": verdict.get("faithful"), "answers_question": verdict.get("answers_question"),
            "note": str(verdict.get("note") or raw[:200])}


def score(item: EvalItem, answer) -> dict:
    cited = [s.split("#")[0] for s in answer.sources]
    checks: dict[str, bool] = {}
    if item.expect:
        checks["cited"] = any(e.split("#")[0] in cited for e in item.expect)
    missing = [f for f in item.must_include if not has_fact(answer.text, f)]
    if item.must_include:
        checks["facts"] = not missing
    checks["refusal"] = (not answer.covered) if item.should_refuse else answer.covered
    checks["figures"] = not answer.unverified
    return {"checks": checks, "missing": missing, "passed": all(checks.values())}


def run(qa, items: list[EvalItem], mode: str | None = None, judge: bool = False,
        progress=None) -> dict:
    """Ask every question and score it. Returns the report (see `save_report`)."""
    results = []
    for n, item in enumerate(items, 1):
        if progress:
            progress(n, len(items), item)
        rec = {"id": item.id, "question": item.question, "added_by": item.added_by}
        try:
            ans = qa.ask(item.question, item.collection, mode=mode)
        except Exception as exc:  # one failing question should not lose the whole run
            results.append({**rec, "error": str(exc), "passed": False, "checks": {}})
            continue
        rec.update(score(item, ans), answer=ans.text, sources=ans.sources, unverified=ans.unverified,
                   mode=ans.mode, cost_usd=ans.cost_usd)
        if judge:
            rec["judge"] = _judge(qa, item, ans)
            for k in ("faithful", "answers_question"):
                rec["checks"][k] = rec["judge"][k] is True
            rec["passed"] = all(rec["checks"].values())
        results.append(rec)
    names = sorted({k for r in results for k in r["checks"]})
    rates = {}
    for k in names:
        vals = [r["checks"][k] for r in results if k in r["checks"]]
        rates[k] = round(sum(vals) / len(vals), 3) if vals else None
    return {"ts": dt.datetime.now().isoformat(timespec="seconds"), "mode": mode or qa.retrieval,
            "judge": judge, "total": len(results), "passed": sum(r["passed"] for r in results),
            "rates": rates, "cost_usd": round(sum(r.get("cost_usd", 0) for r in results), 4),
            "results": results}


def save_report(report_dir: Path, report: dict) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{report['ts'].replace(':', '')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def latest_report(report_dir: Path) -> dict | None:
    files = sorted(report_dir.glob("*.json")) if report_dir.exists() else []
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


CHECK_NAMES = {"cited": "引用了对的文章", "facts": "包含必备要点", "refusal": "该拒答时拒答",
               "figures": "数字都有出处", "faithful": "忠于原文（评审）",
               "answers_question": "答到点上（评审）"}
