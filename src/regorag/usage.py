"""Record every Claude API call this app makes, and enforce a monthly spend cap.

Each call appends one JSON line to data/usage.jsonl. Costs are estimates from the
price table below (USD per million tokens); check them against the Claude Console
billing page, which is authoritative.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path

# model -> (input, output, cache write, cache read), USD per 1M tokens.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}
_FALLBACK_PRICE = PRICES["claude-opus-5-5"]


class BudgetExceeded(Exception):
    pass


@dataclass
class UsageRecord:
    ts: str
    purpose: str  # "qa", "rewrite", "extract", ...
    model: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: float
    backend: str = "api"  # "claude-code" calls run on the Claude plan; cost is the list-price equivalent


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
    p_in, p_out, p_write, p_read = PRICES.get(model, _FALLBACK_PRICE)
    return round(
        (input_tokens * p_in + output_tokens * p_out
         + cache_write_tokens * p_write + cache_read_tokens * p_read) / 1_000_000,
        6,
    )


class UsageLog:
    def __init__(self, path: Path, monthly_budget_usd: float = 0.0):
        self.path = path
        self.monthly_budget_usd = monthly_budget_usd

    def record(self, purpose: str, model: str, usage, backend: str = "api") -> UsageRecord:
        """`usage` is the `response.usage` object from the Anthropic SDK."""
        rec = UsageRecord(
            ts=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            purpose=purpose,
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cost_usd=0.0,
            backend=backend,
        )
        rec.cost_usd = estimate_cost(rec.model, rec.input_tokens, rec.output_tokens,
                                     rec.cache_write_tokens, rec.cache_read_tokens)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        return rec

    def records(self) -> list[UsageRecord]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(UsageRecord(**json.loads(line)))
        return out

    def month_total(self, month: str | None = None, backend: str = "api") -> float:
        """Estimated spend for a month, 'YYYY-MM' (default: current UTC month), for one backend.

        The budget only counts API calls: Claude Code calls are covered by the Claude plan."""
        month = month or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
        return round(sum(r.cost_usd for r in self.records()
                         if r.ts.startswith(month) and r.backend == backend), 4)

    def check_budget(self) -> None:
        if self.monthly_budget_usd > 0 and self.month_total() >= self.monthly_budget_usd:
            raise BudgetExceeded(
                f"本月 API 用量已达上限 ${self.monthly_budget_usd:.2f}"
                f"（已用约 ${self.month_total():.2f}）。可在 .env 调整 REGORAG_MONTHLY_BUDGET_USD。"
            )

    def summary(self, month: str | None = None) -> dict:
        month = month or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
        rows = [r for r in self.records() if r.ts.startswith(month)]
        by_purpose: dict[str, dict] = {}
        for r in rows:
            agg = by_purpose.setdefault(r.purpose, {"calls": 0, "cost_usd": 0.0,
                                                    "input_tokens": 0, "output_tokens": 0,
                                                    "cache_read_tokens": 0})
            agg["calls"] += 1
            agg["cost_usd"] = round(agg["cost_usd"] + r.cost_usd, 4)
            agg["input_tokens"] += r.input_tokens
            agg["output_tokens"] += r.output_tokens
            agg["cache_read_tokens"] += r.cache_read_tokens
        total = self.month_total(month)
        return {
            "month": month,
            "calls": len(rows),
            "cost_usd": total,
            "claude_code_calls": sum(1 for r in rows if r.backend == "claude-code"),
            "claude_code_list_price_usd": self.month_total(month, "claude-code"),
            "budget_usd": self.monthly_budget_usd,
            "budget_used_pct": round(100 * total / self.monthly_budget_usd, 1)
            if self.monthly_budget_usd > 0 else None,
            "by_purpose": by_purpose,
        }
