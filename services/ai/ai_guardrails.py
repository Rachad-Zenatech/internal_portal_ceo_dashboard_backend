"""AI guardrails: screening what goes into the model and redacting personal data from what comes out.

Input screening (screen_input) is a weighted pattern classifier for jailbreak and prompt-injection attempts:
instruction overrides, system prompt extraction, role hijacks ("you are now DAN"), safety bypass requests, forced
verdicts ("return NO_CONCERNS"), fake role/delimiter markers, privilege escalation and hidden/encoded payloads. Each
category that matches adds its weight (combined as 1 - prod(1 - w)), and the score maps to a verdict:

    ALLOW  below AI_GUARD_FLAG_THRESHOLD (0.4)
    FLAG   recorded in the trace and prompt log, the request still goes ahead
    BLOCK  at or above AI_GUARD_BLOCK_THRESHOLD (0.7)

What a verdict does is up to the caller: the assistant chat refuses BLOCK, while content analysis never refuses (a
post that tries to steer the analysis is itself something reviewers need to see), it only records the verdict.

Output redaction (redact_pii) replaces emails, phone numbers, US SSNs, payment card numbers (Luhn-checked), IBANs,
IP addresses and credentials (API keys, JWTs, private keys) with [REDACTED_<KIND>]. StreamRedactor does the same
for streamed text without letting a match slip through a chunk boundary. PII_ALLOWED_EMAIL_DOMAINS keeps company
addresses (e.g. "zenatech.com") readable.
"""

import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Tuple

Verdict = Literal["ALLOW", "FLAG", "BLOCK"]


def _threshold(name: str, default: float) -> float:
    try:
        return min(1.0, max(0.0, float(os.getenv(name, default))))
    except ValueError:
        return default


FLAG_THRESHOLD = _threshold("AI_GUARD_FLAG_THRESHOLD", 0.4)
BLOCK_THRESHOLD = _threshold("AI_GUARD_BLOCK_THRESHOLD", 0.7)

_I = re.IGNORECASE | re.MULTILINE
_PRIOR = r"(?:all\s+|any\s+|the\s+|your\s+|these\s+|those\s+)*(?:previous|prior|above|earlier|preceding|original|system)"
_RULES = r"(?:instructions?|rules|prompts?|messages|directions|guidelines|context)"

# (category, weight, patterns). Patterns run on normalised text: NFKC, zero-width characters removed, single spaces.
_CATEGORIES: List[Tuple[str, float, List["re.Pattern[str]"]]] = [
    ("instruction_override", 0.6, [
        re.compile(rf"\b(?:ignore|disregard|forget|skip|override|bypass)\s+(?:about\s+)?{_PRIOR}\s+{_RULES}", _I),
        re.compile(rf"\b(?:ignore|disregard|forget)\s+(?:everything|all)\s+(?:you(?:'ve| have)?\s+(?:been\s+)?(?:told|said)|above|before)", _I),
        re.compile(r"\bnew\s+instructions?\s*:", _I),
        re.compile(r"\bfrom\s+now\s+on,?\s+(?:you\s+)?(?:will|must|are\s+to)\s+(?:only\s+)?(?:obey|follow|answer|respond)", _I),
    ]),
    ("system_prompt_extraction", 0.6, [
        re.compile(r"\b(?:reveal|show|print|repeat|output|display|leak|tell\s+me|what\s+(?:is|are))\s+(?:me\s+)?(?:your|the)\s+"
                   r"(?:full\s+|exact\s+|hidden\s+|secret\s+|initial\s+|original\s+)*(?:system\s+prompt|system\s+message|instructions|"
                   r"hidden\s+prompt|initial\s+prompt|developer\s+message)", _I),
        re.compile(r"\b(?:repeat|print)\s+(?:the\s+)?(?:text|words)\s+above\b", _I),
    ]),
    ("role_hijack", 0.7, [
        re.compile(r"\byou\s+are\s+(?:now\s+)?(?:DAN|an?\s+(?:unrestricted|unfiltered|uncensored|jailbroken)\b)", _I),
        re.compile(r"\b(?:do\s+anything\s+now|developer\s+mode|jailbreak(?:ed)?\s+mode|god\s+mode)\b", _I),
        re.compile(r"\b(?:act|behave|respond)\s+as\s+(?:if\s+you\s+(?:were|are)\s+)?(?:an?\s+)?(?:AI|assistant|model|chatbot)?\s*"
                   r"(?:with(?:out)?|that\s+has\s+no)\s+(?:any\s+)?(?:restrictions|filters|rules|guidelines|limits)", _I),
        re.compile(r"\bpretend\s+(?:that\s+)?(?:you\s+(?:have|are\s+under)|there\s+are)\s+no\s+(?:rules|restrictions|guidelines|filters)", _I),
    ]),
    ("policy_bypass", 0.6, [
        re.compile(r"\b(?:bypass|disable|turn\s+off|switch\s+off|override|ignore|circumvent)\s+(?:your|the|all|any)\s+"
                   r"(?:safety|guardrails?|filters?|restrictions|content\s+polic(?:y|ies)|moderation|compliance\s+checks?)", _I),
    ]),
    ("forced_verdict", 0.5, [
        # Content trying to dictate the analysis result (the domain's version of an injection payload).
        re.compile(r"\b(?:return|output|respond|answer|reply|mark\s+(?:this|it)\s+as|classify\s+(?:this|it)\s+as)\s+"
                   r"(?:only\s+)?(?:with\s+)?(?:only\s+)?[\"'`]?(?:NO_CONCERNS|APPROVED|COMPLIANT|PASS(?:ED)?|SAFE|GREEN|LOW_RISK|no\s+(?:concerns|findings|issues))\b", _I),
        re.compile(r"\b(?:do\s+not|don't|never)\s+(?:flag|report|mention)\s+(?:this|any(?:thing)?|the\s+following)", _I),
    ]),
    ("delimiter_injection", 0.5, [
        re.compile(r"^\s*(?:system|assistant|developer)\s*:", _I),
        re.compile(r"<\|(?:im_start|im_end|system|endoftext)\|>|\[/?INST\]|<</?SYS>>|</?(?:system|assistant)>", _I),
        re.compile(r"\bend\s+of\s+(?:user\s+)?(?:input|content|post)\b.{0,40}\b(?:system|instructions?)\b", _I),
    ]),
    ("privilege_escalation", 0.5, [
        re.compile(r"\b(?:grant|give|assign|make)\s+(?:me|myself|my\s+account|this\s+user)\s+(?:the\s+)?"
                   r"(?:super\s*admin|admin(?:istrator)?|all\s+(?:permissions|roles|access)|full\s+access)", _I),
    ]),
]
# Checked on the raw text: these are about how the text is built, not what it says.
_ENCODED_BLOB = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")
_HIDDEN_CHARS = re.compile(r"[​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_HIDDEN_WEIGHTS = {"encoded_payload": 0.2, "hidden_characters": 0.3}


@dataclass
class GuardResult:
    verdict: Verdict
    score: float
    categories: List[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict == "BLOCK"


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _HIDDEN_CHARS.sub("", text)
    return re.sub(r"[ \t]+", " ", text)


def screen_input(text: str) -> GuardResult:
    """Score text for jailbreak / prompt-injection attempts. Pure and fast: safe to run on every request."""
    if not text:
        return GuardResult("ALLOW", 0.0)
    clean = _normalise(text)
    weights: Dict[str, float] = {}
    for category, weight, patterns in _CATEGORIES:
        if any(p.search(clean) for p in patterns):
            weights[category] = weight
    if _ENCODED_BLOB.search(text):
        weights["encoded_payload"] = _HIDDEN_WEIGHTS["encoded_payload"]
    if _HIDDEN_CHARS.search(text):
        weights["hidden_characters"] = _HIDDEN_WEIGHTS["hidden_characters"]
    remaining = 1.0
    for w in weights.values():
        remaining *= 1.0 - w
    score = round(1.0 - remaining, 3)
    verdict: Verdict = "BLOCK" if score >= BLOCK_THRESHOLD else "FLAG" if score >= FLAG_THRESHOLD else "ALLOW"
    return GuardResult(verdict, score, sorted(weights))


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

def _allowed_email_domains() -> List[str]:
    return [d.strip().lower().lstrip("@") for d in os.getenv("PII_ALLOWED_EMAIL_DOMAINS", "").split(",") if d.strip()]


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _keep_email(m: "re.Match[str]") -> bool:
    domain = m.group(0).rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith("." + d) for d in _allowed_email_domains())


def _keep_card(m: "re.Match[str]") -> bool:
    digits = re.sub(r"\D", "", m.group(0))
    return not (13 <= len(digits) <= 19 and _luhn(digits))


def _keep_ip(m: "re.Match[str]") -> bool:
    return not all(0 <= int(p) <= 255 for p in m.group(0).split("."))


# (kind, pattern, keep): keep(match) -> True leaves that match as it is. Order matters: credentials and emails go
# first so their digits aren't half-redacted as phone or card numbers.
_PII: List[Tuple[str, "re.Pattern[str]", object]] = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), None),
    ("API_KEY", re.compile(r"\b(?:AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|sk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}|"
                           r"gh[pousr]_[A-Za-z0-9]{36,}|xox[abpr]-[A-Za-z0-9-]{10,})\b"), None),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), None),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), _keep_email),
    ("CARD_NUMBER", re.compile(r"(?<![\d-])\d{4}(?:[ -]?\d{4}){2}[ -]?\d{1,7}(?![\d-])"), _keep_card),
    ("SSN", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), None),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b"), None),
    # Phone numbers need separators or a leading +, so figures such as "2,500,000" or "20250101" stay untouched.
    ("PHONE", re.compile(r"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?!\w)"
                         r"|(?<![\w+])\+\d{1,3}(?:[ .-]\d{2,4}){2,4}(?!\w)"), None),
    ("IP_ADDRESS", re.compile(r"\b(?<!\.)(?:\d{1,3}\.){3}\d{1,3}(?!\.\d)\b"), _keep_ip),
]


def _spans(text: str) -> List[Tuple[int, int, str]]:
    """Non-overlapping (start, end, kind) of every PII match, earlier kinds winning overlaps."""
    taken: List[Tuple[int, int, str]] = []
    for kind, pattern, keep in _PII:
        for m in pattern.finditer(text):
            if keep and keep(m):  # type: ignore[operator]
                continue
            s, e = m.span()
            if any(s < te and ts < e for ts, te, _ in taken):
                continue
            taken.append((s, e, kind))
    return sorted(taken)


def redact_pii(text: str) -> Tuple[str, Dict[str, int]]:
    """The text with personal data and credentials replaced by [REDACTED_<KIND>], and how many of each were found."""
    if not text:
        return text, {}
    counts: Dict[str, int] = {}
    out: List[str] = []
    pos = 0
    for s, e, kind in _spans(text):
        out += [text[pos:s], f"[REDACTED_{kind}]"]
        counts[kind] = counts.get(kind, 0) + 1
        pos = e
    out.append(text[pos:])
    return "".join(out), counts


class StreamRedactor:
    """Redacts streamed text. It holds back the last HOLD characters (cut at whitespace, never inside a match) so a
    phone number or key split across two chunks is still caught; flush() releases the rest at the end."""

    HOLD = 256
    MAX_BUFFER = 8192

    def __init__(self) -> None:
        self._buffer = ""
        self.counts: Dict[str, int] = {}

    def _emit(self, text: str) -> str:
        redacted, counts = redact_pii(text)
        for k, v in counts.items():
            self.counts[k] = self.counts.get(k, 0) + v
        return redacted

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        if len(self._buffer) <= self.HOLD:
            return ""
        cut = len(self._buffer) - self.HOLD
        if len(self._buffer) < self.MAX_BUFFER:
            space = max(self._buffer.rfind(" ", 0, cut), self._buffer.rfind("\n", 0, cut))
            if space <= 0:
                return ""
            cut = space
        for s, e, _ in _spans(self._buffer):
            if s < cut < e:
                cut = s
                break
        if cut <= 0:
            return ""
        out, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return self._emit(out)

    def flush(self) -> str:
        out, self._buffer = self._buffer, ""
        return self._emit(out) if out else ""
