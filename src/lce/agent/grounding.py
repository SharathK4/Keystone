"""Every number in an answer must come from a tool.

The model is told to quote figures exactly as the tools print them. This checks
that it did: each money amount, percentage and plain number in the answer must
appear in a tool result (or in the question itself). Merchant ids are names, not
numbers, and are skipped. An answer that fails is not shown; a sentence built
only from tool figures is shown instead, and the page says which it was.
"""

from __future__ import annotations

import re

MERCHANT = re.compile(r"\bm\d{4}\b", re.IGNORECASE)
MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?[KMB]?\b", re.IGNORECASE)
PERCENT = re.compile(r"\d+(?:\.\d+)?\s?%")
NUMBER = re.compile(r"(?<![\w.$])\d+(?:\.\d+)?(?![\w%])")


def _money_key(token: str) -> str:
    return token.replace(" ", "").replace(",", "").upper()


def _numbers(text: str) -> set[float]:
    clean = MERCHANT.sub(" ", text)
    return {float(n) for n in re.findall(r"\d+(?:\.\d+)?", clean.replace(",", ""))}


def ungrounded(answer: str, sources: list[str]) -> list[str]:
    """The figures in ``answer`` that no source contains. Empty means grounded."""
    corpus = " ".join(sources)
    money_ok = {_money_key(t) for t in MONEY.findall(corpus)}
    pct_ok = {t.replace(" ", "") for t in PERCENT.findall(corpus)}
    numbers_ok = _numbers(corpus)

    text = MERCHANT.sub(" ", answer)
    bad: list[str] = []
    for token in MONEY.findall(text):
        if _money_key(token) not in money_ok:
            bad.append(token.strip())
    text = MONEY.sub(" ", text)
    for token in PERCENT.findall(text):
        if token.replace(" ", "") not in pct_ok:
            bad.append(token.strip())
    text = PERCENT.sub(" ", text)
    for token in NUMBER.findall(text):
        if float(token) not in numbers_ok:
            bad.append(token)
    return bad
