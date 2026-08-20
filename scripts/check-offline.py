#!/usr/bin/env python3
"""Refuse to publish a page that loads anything from a third party.

The privacy notice promises that no resource comes from anywhere else. This
checks that promise rather than trusting it, and it runs before every deploy.

    scripts/check-offline.py [directory]      default: website

The distinction that matters: a `<link>` only fetches something for certain
`rel` values. `rel="canonical"` and `rel="alternate" hreflang=...` are metadata
and fetch nothing, so an absolute URL is correct — and required — there. The
first version of this check flagged every absolute href inside a `<link>` and
blocked the German page's hreflang alternates, so the rule now looks at the rel.

Exit 0 = clean, 1 = findings.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: rel values that make a browser fetch something. Anything else is metadata.
LOADING_RELS = frozenset(
    {
        "stylesheet",
        "icon",
        "shortcut",
        "apple-touch-icon",
        "mask-icon",
        "manifest",
        "preload",
        "modulepreload",
        "prefetch",
        "prerender",
        "preconnect",
        "dns-prefetch",
        "import",
    }
)

#: Protocol-relative counts as absolute; `data:` does not leave the page.
ABSOLUTE = re.compile(r"^(?:https?:)?//", re.I)

#: Attributes that always load, whatever the element.
LOADING_ATTRS = ("src", "srcset", "poster", "data")

#: Patterns inside CSS and JavaScript.
CODE_PATTERNS = (
    (re.compile(r"@import\s+(?:url\()?['\"]?(?://|https?://)", re.I), "@import"),
    (re.compile(r"url\(\s*['\"]?(?://|https?://)", re.I), "url()"),
    (re.compile(r"\b(?:fetch|XMLHttpRequest|EventSource|WebSocket)\s*\(", re.I), "network call"),
    (re.compile(r"fonts\.googleapis|fonts\.gstatic|(?<![\w.])cdn[\w-]*\.", re.I), "third-party host"),
)


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def check(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".html", ".css", ".js"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")

        if path.suffix.lower() == ".html":
            for attr in LOADING_ATTRS:
                for m in re.finditer(rf'\b{attr}="([^"]*)"', text):
                    if ABSOLUTE.match(m.group(1)):
                        findings.append(f"{path}:{_line(text, m.start())}: {attr}=\"{m.group(1)}\"")

            for m in re.finditer(r"<link\b[^>]*>", text, re.I):
                tag = m.group(0)
                href = re.search(r'href="([^"]*)"', tag)
                if not href or not ABSOLUTE.match(href.group(1)):
                    continue
                rel_attr = re.search(r'rel="([^"]*)"', tag)
                rels = set(rel_attr.group(1).lower().split()) if rel_attr else set()
                if rels & LOADING_RELS:
                    findings.append(
                        f"{path}:{_line(text, m.start())}: "
                        f'<link rel="{" ".join(sorted(rels))}"> {href.group(1)}'
                    )

        for pattern, what in CODE_PATTERNS:
            for m in pattern.finditer(text):
                snippet = text[m.start() : m.start() + 60].replace("\n", " ")
                findings.append(f"{path}:{_line(text, m.start())}: {what} — {snippet!r}")

    return findings


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "website")
    if not root.is_dir():
        print(f"REFUSED: {root} is not a directory.")
        return 1
    findings = check(root)
    if findings:
        print(f"REFUSED: {len(findings)} third-party reference(s) under {root}/:")
        for f in findings:
            print("  " + f)
        return 1
    print(f"OK: nothing under {root}/ loads from a third party.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
