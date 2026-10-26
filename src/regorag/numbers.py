"""Check the figures in an answer against the articles it cites.

Fees, dates, time limits, percentages and form numbers are where a wrong answer costs the
most, so after every answer each such figure is looked up in the cited articles. A figure
that cannot be found is not hidden: the clerk is told to have it checked, and the answer
goes on the reviewers' list so the knowledge base can be corrected. No model call is made.

Figures are compared in a normal form, so "$45" matches "$45.00" (or "45 美元": amounts are
compared without their currency), "1 July 2026" matches "2026年7月1日", and "14 天" matches
"14 days", "14-day" or "fourteen days".
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_MONTH = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\.?"

_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
_WORDS.update({"thirty": "30", "sixty": "60", "ninety": "90"})
_ZH_DIGITS = {"一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7",
              "八": "8", "九": "9", "十": "10"}

_UNITS = [  # (pattern, normal unit)
    (r"business\s+days?|working\s+days?|个?工作日", "business-days"),
    (r"days?|天|日(?!期)", "days"),
    (r"weeks?|个?星期|周", "weeks"),
    (r"months?|个月|月(?![份\d])", "months"),
    (r"years?|年(?![\d份])", "years"),
    (r"hours?|小时", "hours"),
]
_NUM = r"(\d+(?:\.\d+)?)"

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("date", re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")),
    ("date", re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")),
    ("date_dmy", re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r",?\s+(\d{4})\b", re.I)),
    ("date_mdy", re.compile(r"\b" + _MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)),
    ("date_slash", re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")),
    ("month", re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月(?!\s*\d)")),
    ("month_en", re.compile(r"\b" + _MONTH + r"\s+(\d{4})\b", re.I)),
    ("money", re.compile(r"(?:A?\$|(?:AUD|USD|RMB|CNY|EUR)\s?|[¥￥€£])\s?(\d[\d,]*(?:\.\d+)?)")),
    ("money", re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:澳元|澳币|美元|美金|欧元|英镑|人民币|元|块|dollars?|euros?"
                         r"|AUD|USD|RMB|CNY|EUR)", re.I)),
    ("percent", re.compile(_NUM + r"\s?%")),
    ("duration", re.compile(_NUM + r"\s*-?\s*(" + "|".join(p for p, _ in _UNITS) + r")", re.I)),
    ("code", re.compile(r"\b(?:form|表格?)\s*([A-Z]*\d{2,6}[A-Z]?)\b", re.I)),
    ("code", re.compile(r"\b([A-Z]{1,4}\d{1,5}[A-Z]?)\b")),
]


@dataclass(frozen=True)
class Figure:
    key: str   # normal form used for comparison, e.g. "money:45" or "date:2026-07-01"
    text: str  # as written in the answer


def _money(v: str) -> str:
    n = float(v.replace(",", ""))
    return f"{n:.2f}".rstrip("0").rstrip(".")


def _unit(word: str) -> str:
    for pattern, unit in _UNITS:
        if re.fullmatch(pattern, word, re.I):
            return unit
    return word.lower()


def _date(y: str, m: str | int, d: str) -> str | None:
    try:
        return dt.date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def _normalise_words(text: str) -> str:
    """Spell-out numbers before units ("five years", "十四天") become digits."""
    text = re.sub(r"\b(" + "|".join(_WORDS) + r")\b(?=[\s-]+(?:business\s+|working\s+)?"
                  r"(?:days?|weeks?|months?|years?|hours?)\b)",
                  lambda m: _WORDS[m.group(1).lower()], text, flags=re.I)
    def zh(m: re.Match) -> str:
        try:
            return _zh_number(m.group(1))
        except KeyError:  # a range such as 一两天 / 三四个月: leave it as written
            return m.group(1)

    def _zh_number(s: str) -> str:
        if s == "十":
            return "10"
        if s.startswith("十"):
            return "1" + _ZH_DIGITS.get(s[1:], "0")
        if s.endswith("十"):
            return _ZH_DIGITS[s[0]] + "0"
        if "十" in s:
            a, b = s.split("十")
            return _ZH_DIGITS[a] + _ZH_DIGITS[b]
        return _ZH_DIGITS[s]
    return re.sub(r"([一二两三四五六七八九十]{1,3})(?=\s*(?:个?工作日|天|个?星期|周|个月|年|小时))", zh, text)


def figures(text: str) -> list[Figure]:
    """Every fee, date, time limit, percentage and form number in the text."""
    text = _normalise_words(text)
    out: dict[str, Figure] = {}
    taken: list[tuple[int, int]] = []

    def add(key: str | None, m: re.Match) -> None:
        if not key or any(s < m.end() and m.start() < e for s, e in taken):
            return
        taken.append((m.start(), m.end()))
        out.setdefault(key, Figure(key, m.group(0).strip()))

    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            g = m.groups()
            if kind == "date":
                add(f"date:{_date(*g)}" if _date(*g) else None, m)
            elif kind == "date_dmy":
                add(f"date:{_date(g[2], _MONTHS[g[1].lower()], g[0])}" if _date(g[2], _MONTHS[g[1].lower()], g[0]) else None, m)
            elif kind == "date_mdy":
                add(f"date:{_date(g[2], _MONTHS[g[0].lower()], g[1])}" if _date(g[2], _MONTHS[g[0].lower()], g[1]) else None, m)
            elif kind == "date_slash":  # Australian day/month/year
                add(f"date:{_date(g[2], g[1], g[0])}" if _date(g[2], g[1], g[0]) else None, m)
            elif kind == "month":
                add(f"month:{int(g[0]):04d}-{int(g[1]):02d}", m)
            elif kind == "month_en":
                add(f"month:{int(g[1]):04d}-{_MONTHS[g[0].lower()]:02d}", m)
            elif kind == "money":
                add(f"money:{_money(g[0])}", m)
            elif kind == "percent":
                add(f"percent:{float(g[0]):g}", m)
            elif kind == "duration":
                add(f"duration:{float(g[0]):g}:{_unit(g[1])}", m)
            elif kind == "code":
                add(f"code:{g[0].upper()}", m)
    return list(out.values())


def _known(source_text: str) -> set[str]:
    keys = {f.key for f in figures(source_text)}
    # A full date in the source also vouches for its month ("July 2026").
    keys |= {"month:" + k[5:12] for k in keys if k.startswith("date:")}
    return keys


def unverified(answer: str, sources: list[str], question: str = "") -> list[str]:
    """Figures in the answer that appear in none of the sources (nor in the question)."""
    known = _known("\n".join(sources)) | _known(question)
    return [f.text for f in figures(answer) if f.key not in known]
