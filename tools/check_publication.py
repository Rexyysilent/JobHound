"""Fail-closed privacy check for a public folder or the exact Git index.

Only exact, reviewed synthetic lines may be exempted. Findings contain paths,
line numbers and categories, never matched values. This heuristic supplements
manual review and exclusion of private provenance; it is not a secret oracle.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import quote, quote_plus

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "google_key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{30,})\b"),
    "aws_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "telegram_token": re.compile(r"\b[0-9]{7,12}:[0-9A-Za-z_-]{25,}\b"),
    "vendor_token": re.compile(r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|xox[baprs]-[A-Za-z0-9-]{20,})\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
    "email": re.compile(r"(?<![\w.+-])[\w.+-]+@(?:[\w-]+\.)+[A-Za-z]{2,}(?![\w.-])"),
    "home_path": re.compile(r"(?:[A-Za-z]:[/\\]+Users[/\\]+[^\s\"'<>/\\]+|/(?:home|Users)/[^\s\"'<>/]+)", re.I),
    "url_userinfo": re.compile(r"https?://[^\s/:]+:[^\s/@]+@", re.I),
    "credential_query": re.compile(r"(?:[?&]|&amp;)(?:app_id|app_key|api_key|apikey|access_token|token|key|password|secret|signature|sig|auth|utm_source)=([^\s&#\"<>]+)", re.I),
    "credential_assignment": re.compile(r"\b(?:api[_-]?key|app[_-]?key|access[_-]?token|password|secret|authorization)\b[\"']?\s*[:=]\s*[\"']([^\"'\r\n]{8,})[\"']", re.I),
    "authorization_header": re.compile(r"\b(?:Authorization|Proxy-Authorization)\s*:\s*(?:Bearer|Basic)\s+[^\s\"']+", re.I),
    "api_key_header": re.compile(r"\b(?:X-API-Key|X-RapidAPI-Key|api-key)\s*:\s*[^\s\"']+", re.I),
}
SAFE_DOMAINS = {"example.com", "example.org", "example.net", "example.invalid", "localhost"}
GENERATED = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
PRIVATE_ROOTS = {"data", "roadmap", "private", "review", "keys"}
PRIVATE_FILES = {".env", "config.yaml", "profile.yaml", "platform_registry.yaml", "platforms_to_join.yaml"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".p12", ".pfx", ".zip", ".ipynb", ".pyc"}


def private_path(name: str) -> bool:
    parts = Path(name).parts
    if not parts:
        return True
    return (parts[0] in PRIVATE_ROOTS or name in PRIVATE_FILES or Path(name).name == ".env"
            or (Path(name).name.startswith(".env.") and Path(name).name != ".env.example")
            or Path(name).suffix.lower() in FORBIDDEN_SUFFIXES
            or any(part in GENERATED for part in parts))


def secret_needles(path: Path | None) -> list[str]:
    if path is None:
        return []
    result = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        _key, value = line.split("=", 1)
        value = value.strip().strip("\"'")
        if len(value) >= 6:
            result.update((value, quote(value, safe=""), quote_plus(value), html.escape(value),
                           json.dumps(value)[1:-1], base64.b64encode(value.encode()).decode(),
                           value.replace(" ", "")))
    return sorted(value for value in result if len(value) >= 6)


def line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def scan_text(name: str, text: str, needles: list[str], exceptions: list[dict]) -> tuple[list[dict], int]:
    findings, reviewed = [], 0
    approved = {(item["path"], item["kind"], item["sha256"]) for item in exceptions}
    for number, line in enumerate(text.splitlines(), 1):
        if any(value in line for value in needles):
            # Exact configured values can NEVER be waived by a test exception.
            findings.append({"path": name, "line": number, "kind": "configured_private_value"})
        for kind, rule in RULES.items():
            for match in rule.finditer(line):
                if kind == "email":
                    domain = match.group().rsplit("@", 1)[-1].lower()
                    if domain in SAFE_DOMAINS or domain.endswith((".example", ".invalid", ".test")):
                        continue
                if (name, kind, line_hash(line)) in approved:
                    reviewed += 1
                else:
                    findings.append({"path": name, "line": number, "kind": kind})
                break
    return findings, reviewed


def check(root: Path, *, index: bool = False, env_file: Path | None = None) -> dict:
    root = root.resolve()
    exceptions_path = "tools/publication_exceptions.json"
    if index:
        top = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--show-toplevel"]).decode().strip()
        if Path(top).resolve() != root:
            raise ValueError("Index scan must cover the repository root, not a subtree")
        names = subprocess.check_output(["git", "-C", str(root), "ls-files", "--stage", "-z"]).decode().split("\0")
        entries = []
        for row in filter(None, names):
            meta, name = row.split("\t", 1)
            mode, oid, stage = meta.split()
            if mode not in {"100644", "100755"} or stage != "0":
                raise ValueError("Index contains a symlink, submodule or unresolved entry")
            entries.append((name, subprocess.check_output(["git", "-C", str(root), "cat-file", "blob", oid])))
    else:
        entries = []
        for path in sorted(root.rglob("*")):
            name = path.relative_to(root).as_posix()
            if any(part in GENERATED for part in path.relative_to(root).parts):
                continue
            if path.is_symlink():
                raise ValueError("Symlink rejected")
            if path.is_file():
                entries.append((name, path.read_bytes()))
    contents = dict(entries)
    if exceptions_path not in contents:
        raise ValueError("Reviewed exception manifest is missing")
    exceptions = json.loads(contents[exceptions_path].decode("utf-8"))
    needles = secret_needles(env_file)
    findings, reviewed = [], 0
    for name, raw in entries:
        if private_path(name):
            findings.append({"path": name, "line": 0, "kind": "private_or_generated_file"})
        try:
            text = raw.decode("utf-8-sig")
            if b"\0" in raw:
                raise UnicodeError()
        except UnicodeError:
            findings.append({"path": name, "line": 0, "kind": "unreviewed_binary"})
            continue
        detected, waived = scan_text(name, text, needles, exceptions)
        findings.extend(detected)
        reviewed += waived
    return {"passed": not findings, "files_checked": len(entries),
            "bytes_checked": sum(len(raw) for _, raw in entries),
            "reviewed_synthetic_matches": reviewed, "findings": findings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--index", action="store_true", help="Scan staged bytes, not working-tree substitutes")
    parser.add_argument("--env-file", type=Path, help="Optional private env to compare without printing values")
    args = parser.parse_args()
    try:
        report = check(args.root, index=args.index, env_file=args.env_file)
    except (OSError, ValueError, subprocess.SubprocessError):
        print(json.dumps({"passed": False, "error": "Cannot verify publication inputs"}))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
