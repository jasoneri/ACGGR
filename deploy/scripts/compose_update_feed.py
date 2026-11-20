#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Compose the update feed shipped on the permanent ``updater`` release tag.

Shipped clients poll one fixed URL (releases/download/updater/update.json): a versioned asset
would move with every tag, and releases/latest skips prereleases / serves stale JSON when a
release lacks assets. One fixed tag makes the URL stable, the feed rewritable atomically
(--clobber), and a rollback a one-file upload.

Usage:
    python deploy/scripts/compose_update_feed.py --tag v0.2.0 --body-file body.md --output update.json
    python deploy/scripts/compose_update_feed.py --tag v0.2.0 --body "notes" --dry-run

stdlib-only on purpose: it runs on a bare ``python`` in Actions before deps are installed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

#: Same tag shape the client's parser accepts, so a feed can never advertise a
#: version the app would refuse to compare (``v0.2.0``, ``0.2.0-beta``, ``1.0``).
TAG_RE = re.compile(
    r"""^v?\d+(?:\.\d+)*
    (?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview|dev|post)[-_.]?\d*)?
    $""",
    re.IGNORECASE | re.VERBOSE,
)
PRE_TOKEN_RE = re.compile(r"(?:a|b|c|rc|alpha|beta|pre|preview|dev)[-_.]?\d*$", re.IGNORECASE)


def normalize_tag(tag: str) -> str:
    raw = str(tag or "").strip()
    if not raw or not TAG_RE.match(raw):
        raise SystemExit(f"invalid tag: {tag!r} (expected vX.Y.Z[-pre])")
    return raw


def version_of(tag: str) -> str:
    return normalize_tag(tag).lstrip("vV")


def channel_of(tag: str) -> str:
    return "preview" if PRE_TOKEN_RE.search(version_of(tag)) else "stable"


def read_body(args) -> str:
    if args.body_file:
        return pathlib.Path(args.body_file).read_text(encoding="utf-8")
    if args.body is not None:
        return args.body
    if args.body_stdin:
        return sys.stdin.read()
    return ""


def build_feed(tag: str, body: str, *, repo: str, published_at: str) -> dict:
    version = version_of(tag)
    normalized_tag = normalize_tag(tag)
    payload = {
        "version": version,
        "channel": channel_of(tag),
        "published_at": published_at,
        "notes": body,
        "release_url": f"https://github.com/{repo}/releases/tag/{normalized_tag}",
    }
    return payload


def default_published_at() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_published_at(value: str) -> str:
    """ISO-8601 in, ``...Z`` out; empty means "now", junk is a hard error.

    The timestamp comes from the releases API through a shell variable, so a
    malformed value is a pipeline bug: letting it through would ship a feed the
    client cannot render into a date, and silently substituting "now" would make
    an old release look freshly published.
    """
    raw = str(value or "").strip()
    if not raw:
        return default_published_at()
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(f"invalid --published-at: {raw!r} (expected ISO-8601, e.g. 2026-11-30T12:00:00Z)")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compose the updater feed JSON.")
    parser.add_argument("--tag", required=True, help="release tag, e.g. v0.2.0 or v0.2.0-beta")
    parser.add_argument("--body-file", help="file with the release body (markdown)")
    parser.add_argument("--body", help="release body passed inline")
    parser.add_argument("--body-stdin", action="store_true", help="read the release body from stdin")
    parser.add_argument("--repo", default="jasoneri/acggr", help="owner/name for the release URL")
    parser.add_argument("--published-at", default="", help="ISO timestamp; defaults to now (UTC)")
    parser.add_argument("--output", help="where to write update.json (required unless --dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="print the payload instead of writing it")
    args = parser.parse_args()

    body = read_body(args)
    if not body.strip():
        # Not fatal: a release without notes still ships a valid feed (the UI
        # falls back to its "no notes" wording), but the gap must be visible in CI.
        print("warning: release body is empty; the feed will carry no notes", file=sys.stderr)

    published_at = normalize_published_at(args.published_at)
    feed = build_feed(args.tag, body, repo=args.repo, published_at=published_at)
    text = json.dumps(feed, ensure_ascii=False, indent=2) + "\n"

    if args.dry_run:
        sys.stdout.write(text)
        return 0
    if not args.output:
        parser.error("--output is required unless --dry-run")
    path = pathlib.Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path} version={feed['version']} channel={feed['channel']} notes={len(body)} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())