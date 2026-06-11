"""Sanitization of untrusted external text (market descriptions, X posts, news).

Per FR-SOC-003 and NFR-SEC-004 every byte of externally sourced text MUST be
treated as untrusted and neutralized before it can reach the LLM agent. We:
  * strip control characters and zero-width / bidi override characters,
  * collapse excessive whitespace and truncate to a bounded length,
  * detect (and flag, not silently drop) common prompt-injection patterns,
  * wrap the result so downstream code always knows it is untrusted.
The original text is preserved verbatim in the audit store; only the sanitized
form is exposed to the model."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Zero-width, BOM, and bidirectional override characters abused to smuggle text.
_INVISIBLE = re.compile(
    "[\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff]"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"[ \t]{3,}")
_NEWLINES = re.compile(r"\n{3,}")

# Heuristic prompt-injection signatures. Matches are *flagged*, never executed.
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|prompts?|context)",
        r"disregard (?:all |the )?(?:previous|prior|above) ",
        r"you are now (?:a |an )?",
        r"new (?:system )?(?:instructions?|prompt|directive)",
        r"system prompt",
        r"</?(?:system|assistant|user|tool)>",
        r"\[/?(?:system|inst|instructions)\]",
        r"developer mode",
        r"reveal (?:your |the )?(?:system )?(?:prompt|instructions|api[_ ]?key|secret|private[_ ]?key)",
        r"print (?:the |your )?(?:secret|api[_ ]?key|private[_ ]?key|seed phrase|token)",
        # Action verb + (within ~40 chars) a sensitive credential term, to catch
        # phrasings with words in between (e.g. "reveal the signing vault private key").
        r"(?:reveal|show|print|expose|dump|leak|send|give|output|disclose)\b.{0,40}?"
        r"\b(?:private[_ ]?key|api[_ ]?key|secret|seed phrase|mnemonic|vault|bearer|password|credential)",
        r"override (?:risk|safety|compliance|all) (?:rules?|checks?|limits?|gates?)",
        # Proximity form: "override ... safety ... checks/gates" with words between.
        r"(?:override|bypass|disable|ignore|turn off)\b.{0,40}?"
        r"\b(?:risk|safety|compliance|guardrail|gate|limit|check|rule)s?",
        r"execute (?:the following )?(?:shell|bash|command|code)",
        r"(?:enable|unlock|activate) live (?:mode|trading|adapter)",
    )
]

MAX_LEN = 4000

# Common confusable homoglyphs folded to ASCII for the *detection* pass only, so
# Cyrillic/Greek look-alikes (e.g. "ignоre") cannot smuggle instructions past the
# pattern matcher. The user-visible text keeps its original (normalized) form.
_CONFUSABLES = str.maketrans({
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c",
    "\u0445": "x", "\u0443": "y", "\u0456": "i", "\u0455": "s", "\u04bb": "h",
    "\u03bf": "o", "\u03b1": "a", "\u03b5": "e", "\u03c1": "p", "\u03bd": "v",
    "\u0405": "s", "\u0410": "a", "\u0415": "e", "\u041e": "o", "\u0420": "p",
})


@dataclass(frozen=True)
class SanitizedText:
    """Bounded, neutralized text plus provenance flags. ``is_untrusted`` is always
    ``True`` so downstream serializers can label it for the dashboard/agent."""

    text: str
    is_untrusted: bool = True
    injection_flags: tuple[str, ...] = field(default_factory=tuple)
    truncated: bool = False
    original_length: int = 0

    @property
    def suspected_injection(self) -> bool:
        return bool(self.injection_flags)

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "is_untrusted": self.is_untrusted,
            "suspected_injection": self.suspected_injection,
            "injection_flags": list(self.injection_flags),
            "truncated": self.truncated,
            "original_length": self.original_length,
        }


def sanitize_untrusted(raw: str | None, max_len: int = MAX_LEN) -> SanitizedText:
    """Neutralize ``raw`` external text and report any injection signatures found."""
    if raw is None:
        return SanitizedText(text="", original_length=0)
    original_length = len(raw)

    # Normalize unicode to fold look-alike/compatibility forms, then replace
    # invisible characters with a SPACE (not delete) so they cannot silently join
    # words, and turn control characters into spaces.
    text = unicodedata.normalize("NFKC", raw)
    text = _INVISIBLE.sub(" ", text)
    text = _CONTROL.sub(" ", text)

    # Detection runs on a whitespace-collapsed, homoglyph-folded copy so that
    # tabs/newlines/extra spaces and Cyrillic/Greek look-alikes can't defeat the
    # patterns. The user-visible text is produced separately below.
    detection = re.sub(r"\s+", " ", text).strip().translate(_CONFUSABLES)
    flags = sorted({p.pattern for p in _INJECTION_PATTERNS if p.search(detection)})

    text = _WS.sub("  ", text)
    text = _NEWLINES.sub("\n\n", text).strip()

    truncated = False
    if len(text) > max_len:
        text = text[:max_len].rstrip() + " …[truncated]"
        truncated = True

    return SanitizedText(
        text=text,
        injection_flags=tuple(flags),
        truncated=truncated,
        original_length=original_length,
    )
