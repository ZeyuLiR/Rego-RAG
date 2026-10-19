from types import SimpleNamespace

import pytest

from regorag.usage import BudgetExceeded, UsageLog, estimate_cost


def test_cost_estimate_includes_cache():
    assert estimate_cost("claude-opus-5-5", 1_000_000, 0) == 4.0
    assert estimate_cost("claude-opus-5-5", 0, 1_000_000) == 20.0
    assert estimate_cost("claude-opus-5-5", 0, 0, 0, 1_000_000) == 0.2


def test_budget_blocks_calls_once_reached(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", monthly_budget_usd=1.0)
    usage = SimpleNamespace(input_tokens=200_000, output_tokens=10_000,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0)
    log.check_budget()
    log.record("qa", "claude-opus-5-5", usage)  # $0.80 + $0.20 = $1.00
    with pytest.raises(BudgetExceeded):
        log.check_budget()
    s = log.summary()
    assert s["calls"] == 1 and s["budget_used_pct"] == 100.0
    assert s["by_purpose"]["qa"]["calls"] == 1
