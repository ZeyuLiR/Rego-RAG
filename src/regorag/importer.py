"""Background import jobs for the web page: extraction takes a while per file, so the
upload returns at once and the page polls for progress. Jobs run one at a time so each
proposal is compared against the same knowledge base the reviewer will see."""

from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

ALLOWED_SUFFIXES = {".pdf", ".docx", ".eml", ".msg", ".txt", ".md", ".html", ".htm",
                    ".xlsx", ".pptx"}
MAX_FILE_BYTES = 25 * 1024 * 1024


@dataclass
class Job:
    id: str
    name: str
    status: str = "queued"  # queued | running | done | error
    proposals: list[str] = field(default_factory=list)  # inbox folder names once done
    changes: int | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))


class ImportQueue:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="regorag-import")

    def submit(self, name: str, work: Callable[[], list[Path]]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], name=name)
        with self._lock:
            self._jobs[job.id] = job
        self._pool.submit(self._run, job, work)
        return job

    def _run(self, job: Job, work: Callable[[], list[Path]]) -> None:
        job.status = "running"
        try:
            folders = work()
            job.changes = sum(len(json.loads((f / "proposal.json").read_text(encoding="utf-8"))["changes"])
                              for f in folders)
            job.proposals, job.status = [f.name for f in folders], "done"
        except Exception as exc:  # shown to the reviewer on the page
            job.status, job.error = "error", str(exc) or exc.__class__.__name__

    def jobs(self) -> list[dict]:
        with self._lock:
            return [asdict(j) for j in sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)]

    def wait(self) -> None:
        """Block until queued work is done (tests)."""
        self._pool.submit(lambda: None).result()
