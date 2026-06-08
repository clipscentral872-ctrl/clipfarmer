"""Validate a clip + caption against a Whop campaign's rules before posting.

Looks at the raw campaign rules text and checks the proposed caption +
clip metadata satisfy:
  - any required hashtags (#foo)
  - any required @mentions
  - duration limits (min/max sec)
  - permitted platforms

Returns a CheckResult with reasons. The publisher should treat any
failure as a hard stop (or as a Telegram-approval gate).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

HASHTAG_REQUIRED_RE = re.compile(
    r"(?:must\s+(?:include|tag|use)|required(?:\s+hashtag)?[:\s]+)"
    r"([^\n.]*#[A-Za-z0-9_]+(?:[^\n.]*#[A-Za-z0-9_]+)*)",
    re.IGNORECASE,
)
MENTION_REQUIRED_RE = re.compile(
    r"(?:tag|mention|credit|@)\s+(@[A-Za-z0-9_.]+)",
    re.IGNORECASE,
)
ANY_HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")
ANY_MENTION_RE = re.compile(r"@([A-Za-z0-9_.]+)")


@dataclass
class CheckResult:
    ok: bool
    failures: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def validate(
    *,
    caption: str,
    duration_sec: float,
    platform: str,
    campaign_rules: Optional[str] = None,
    platforms_required: Optional[List[str]] = None,
    min_duration_sec: Optional[int] = None,
    max_duration_sec: Optional[int] = None,
) -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []

    # Platform.
    if platforms_required:
        ok_platforms = [p.lower() for p in platforms_required]
        if platform.lower() not in ok_platforms:
            failures.append(
                f"platform {platform!r} not in campaign's allowed list {ok_platforms}"
            )

    # Duration.
    if min_duration_sec and duration_sec < min_duration_sec:
        failures.append(f"clip is {duration_sec:.1f}s, below min {min_duration_sec}s")
    if max_duration_sec and duration_sec > max_duration_sec:
        failures.append(f"clip is {duration_sec:.1f}s, above max {max_duration_sec}s")

    # Required hashtags / mentions (scraped from rules text).
    if campaign_rules:
        required_hashtags = _extract_required_hashtags(campaign_rules)
        required_mentions = _extract_required_mentions(campaign_rules)
        caption_hashtags = {h.lower() for h in ANY_HASHTAG_RE.findall(caption)}
        caption_mentions = {m.lower() for m in ANY_MENTION_RE.findall(caption)}
        for tag in required_hashtags:
            if tag.lower().lstrip("#") not in caption_hashtags:
                failures.append(f"caption missing required hashtag #{tag.lstrip('#')}")
        for m in required_mentions:
            if m.lower().lstrip("@") not in caption_mentions:
                warnings.append(f"caption may be missing required mention @{m.lstrip('@')}")

    return CheckResult(ok=not failures, failures=failures, warnings=warnings)


def _extract_required_hashtags(rules: str) -> list[str]:
    out: set[str] = set()
    for m in HASHTAG_REQUIRED_RE.finditer(rules):
        for h in ANY_HASHTAG_RE.findall(m.group(1)):
            out.add(h)
    return sorted(out)


def _extract_required_mentions(rules: str) -> list[str]:
    out: set[str] = set()
    for m in MENTION_REQUIRED_RE.finditer(rules):
        out.add(m.group(1).lstrip("@"))
    return sorted(out)
