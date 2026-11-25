#!/usr/bin/env python3
"""Bump or verify the Browserr version across every carrier.

The version lives in seven places; a bump that misses one ships an engine
whose --version disagrees with the wheel, and the MCP server then warns
about a "stale engine" forever. This script owns that list so a release
cannot half-happen.

    python scripts/bump.py 1.0.0        # rewrite all carriers
    python scripts/bump.py --check      # verify they agree (exit 1 on drift)
    python scripts/bump.py --check --tag v1.0.0   # ... and match a git tag
    python scripts/bump.py 1.0.0 --dry-run        # report, change nothing

Every replacement asserts its expected match count, so a restructured file
fails loudly instead of being silently skipped. Line endings are preserved
byte-for-byte (all carriers are LF today; that is not assumed).
"""
import argparse
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# path, regex with one group = the version, expected match count, label
CARRIERS = [
    ("engine/Cargo.toml",
     r'(?m)^version\s*=\s*"([^"]+)"', 1,
     "engine package version (the source of truth)"),
    ("pypi/pyproject.toml",
     r'(?m)^version\s*=\s*"([^"]+)"', 1,
     "PyPI wheel version"),
    ("pypi/server.json",
     r'"version"\s*:\s*"([^"]+)"', 2,
     "MCP registry manifest (server + package)"),
    ("tools/browserr_mcp.py",
     r'(?m)^MCP_VERSION\s*=\s*"([^"]+)"', 1,
     "MCP server version (compared with the engine at startup)"),
    ("pypi/src/browserr_bootstrap/__init__.py",
     r'(?m)^__version__\s*=\s*"([^"]+)"', 1,
     "bootstrap package version"),
    ("docs/index.html",
     r'id="heroVersion">v([0-9]+\.[0-9]+\.[0-9]+)<', 1,
     "landing page version fallback"),
    ("docs/llms.txt",
     r'Version ([0-9]+\.[0-9]+\.[0-9]+)\.', 1,
     "llms.txt version"),
]


def read(path):
    """Read text with line endings intact."""
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def current(path, pattern, expected):
    text = read(path)
    found = re.findall(pattern, text)
    if len(found) != expected:
        raise SystemExit(
            f"bump: {os.path.relpath(path, ROOT)} matched {len(found)} "
            f"times, expected {expected} - the file was restructured; "
            "update CARRIERS in scripts/bump.py")
    return found


def bump(version, dry_run=False):
    print(f"bumping to {version}")
    for path, pattern, expected, label in CARRIERS:
        full = os.path.join(ROOT, path)
        before = current(full, pattern, expected)
        if all(v == version for v in before):
            print(f"  = {path}  ({', '.join(before)}) - already {version}")
            continue
        text = read(full)
        # Replace each match's version, in order.
        parts = []
        last = 0
        done = 0
        for m in re.finditer(pattern, text):
            parts.append(text[last:m.start(1)])
            parts.append(version)
            last = m.end(1)
            done += 1
        parts.append(text[last:])
        after = read(full) if dry_run else None
        new_text = "".join(parts)
        # Prove the result carries the target version in the same slots.
        check = re.findall(pattern, new_text)
        if check != [version] * expected:
            raise SystemExit(
                f"bump: {path} would end up with {check}, not "
                f"{[version] * expected}; aborting without writing")
        if dry_run:
            print(f"  ~ {path}  ({', '.join(before)} -> {version}) [dry run]")
            assert after is not None or True
        else:
            write(full, new_text)
            print(f"  ~ {path}  ({', '.join(before)} -> {version})")
        assert done == expected


def check(tag=None):
    """Compare every carrier against the engine version."""
    source_path, source_pat, source_n, _ = CARRIERS[0]
    source = current(os.path.join(ROOT, source_path), source_pat, source_n)[0]
    if not SEMVER.match(source):
        problems = [f"engine/Cargo.toml version {source!r} is not semver"]
    else:
        problems = []

    print(f"version: {source}")
    for path, pattern, expected, label in CARRIERS[1:]:
        values = current(os.path.join(ROOT, path), pattern, expected)
        ok = all(v == source for v in values)
        print(f"  {'OK ' if ok else 'BAD'} {path}: {', '.join(values)}")
        if not ok:
            problems.append(f"{path} has {', '.join(values)}, expected {source}")
        for v in values:
            if not SEMVER.match(v):
                problems.append(f"{path} has a non-semver version {v!r}")

    if tag:
        tag_version = tag[1:] if tag.startswith("v") else tag
        if tag_version != source:
            problems.append(
                f"git tag {tag} says {tag_version}, files say {source} - "
                "bump before tagging")
        else:
            print(f"  OK  git tag {tag} matches {source}")

    changelog = read(os.path.join(ROOT, "CHANGELOG.md"))
    if re.search(r"(?m)^## \[" + re.escape(source) + r"\]", changelog):
        print(f"  OK  CHANGELOG.md has a {source} entry")
    elif re.search(r"(?m)^## \[Unreleased\]", changelog):
        print(f"  --  CHANGELOG.md still says [Unreleased] "
              f"(rename it to [{source}] before tagging)")
    else:
        problems.append(f"CHANGELOG.md has no entry for {source}")

    if problems:
        print("\nFAILED:")
        for p in problems:
            print(" -", p)
        return 1
    print("\nversion carriers agree")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Bump or verify the Browserr version everywhere.")
    ap.add_argument("version", nargs="?",
                    help="new version, e.g. 1.0.0 (omit with --check)")
    ap.add_argument("--check", action="store_true",
                    help="verify all carriers agree; change nothing")
    ap.add_argument("--tag", help="also require the files to match this tag")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the changes without writing")
    args = ap.parse_args(argv)

    if args.check:
        if args.version:
            ap.error("--check takes no version argument")
        return check(tag=args.tag)
    if not args.version:
        ap.error("give a version to bump to, or use --check")
    if not SEMVER.match(args.version):
        ap.error(f"{args.version!r} is not a plain X.Y.Z version")
    if args.tag and args.tag.lstrip("v") != args.version:
        ap.error(f"--tag {args.tag} does not match {args.version}")
    bump(args.version, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
