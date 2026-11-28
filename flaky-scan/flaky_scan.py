#!/usr/bin/env python3
"""Deterministic flaky-test backlog builder for symfony/symfony GitHub Actions.

Pipeline (each step reads/writes files under --dir, so any agent can resume):

  collect      gh API  -> cache/runs, cache/jobs, cache/logs   (needs `gh` with repo read access)
  ingest-mcp   GitHub MCP JSON dumps -> same cache             (for agents that only have MCP tools)
  parse        cache/logs -> failures.jsonl + runs.jsonl       (pure, offline; keeps records of jobs not in cache)
  classify     failures.jsonl -> backlog.json                  (offline; keeps manual fields; --repo checks fixes)
  link         backlog.json + `gh api search/issues` -> backlog.json links (cached in cache/search)
  report       backlog.json -> BACKLOG.md                      (pure, offline)
  resolve      UNATTRIBUTED jobs: tail -> annotations -> full log -> none (RESOLVE.md)
  candidates   backlog.json -> candidates/*.md + candidates/README.md (one brief per open flaky candidate)
  seen         "run already classified? / error already seen? / already fixed?" lookups
  backfill     fixed_by from "fixed by #NNNN" notes (needs --repo, an upstream clone)
  all          parse + classify + report + candidates

Determinism: no wall-clock in outputs, every list is sorted, the time window is
an explicit --since/--until, and network results are cached on disk, so the same
cache always produces byte-identical failures.jsonl / backlog.json / BACKLOG.md.
Stdlib only (Python >= 3.9).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

REPO = "symfony/symfony"
WORKFLOWS = ["unit-tests.yml", "integration-tests.yml", "windows.yml"]
# Manual fields an agent may edit in backlog.json; never overwritten by `classify`.
MANUAL_FIELDS = ("status", "owner", "notes", "classification_override", "fixed_by", "board")
MANUAL_DEFAULTS = {"status": "new", "fixed_by": {}}
STATUSES = ("new", "triaged", "reported", "fix-proposed", "fixed", "regressed", "wontfix", "not-flaky")
OPEN_STATUSES = ("new", "triaged", "regressed")
# kinds whose excerpt is noise (log tails, timings): one signature per key
CONSTANT_SIG_KINDS = ("unattributed", "timeout", "infra")

ANSI = re.compile(r"\x1b\[[0-9;]*m|\[[0-9;]*m")
TS = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ")
NUMBERED = re.compile(r"^(\d+)\) (\S.*)$")
TEST_ID = re.compile(r"^(?P<cls>[A-Za-z_\\][\w\\]+)::(?P<meth>\w+)")
KO = re.compile(r"KO (src[\\/][\w\\/]+)")
TIMEOUT_COMPONENT = re.compile(r"^Timeout (src[\\/][\w\\/]+)")
FATAL = re.compile(r"(?:PHP )?Fatal error: +(.*)")
# CI infrastructure failures (composer downloads): flaky by nature, not a test problem
INFRA_KEYS = [
    (r"is a corrupted zip archive", "INFRA composer: corrupted (0-byte) dist download"),
    (r"(?:Failed to connect to|Could not resolve host:?) \S*packagist|packagist\S* port 443 after \d+ ms: Timeout", "INFRA composer: packagist network timeout"),
]
INFRA = re.compile("|".join(f"(?:{p})" for p, _ in INFRA_KEYS))
SECTION = re.compile(r"^(?:There (?:was|were) \d+|\d+ tests? triggered \d+(?: PHPUnit)?) (failures?|errors?|deprecations?|notices?|risky tests?|warnings?):")

# Deterministic classification rules (first match wins). Edit/extend here, then re-run `all`.
REAL_PATTERNS = [
    (r"cannot extend final class", "dependency made a parent class final"),
    (r"is deprecated|deprecation", "deprecation triggered (usually a dependency release)"),
    (r"Class \"?[\w\\]+\"? not found|Call to undefined method", "API break / missing class"),
]
FLAKY_PATTERNS = [
    (r"Idle timeout reached|timed out|Timeout src|Maximum execution time", "timeout"),
    (r"localhost:\d+|127\.0\.0\.1:\d+|Connection refused|Could not connect|Address already in use", "network/port"),
    (r"Could not (set up|count|send|get|ack|reject)", "external service / transport race"),
    (r"filemtime|touch\(|is not fresh|false is true", "filesystem timing"),
    (r"two arrays are identical|actual size \d+ matches expected size", "ordering / leftover state"),
]


# ---------------------------------------------------------------- helpers
def p(args, *parts):
    return os.path.join(args.dir, *parts)


def jdump(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def jload(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def gh(*argv, raw=False):
    cmd = ["gh", "api", *argv]
    for attempt in range(4):
        r = subprocess.run(cmd, capture_output=True, text=not raw)
        if r.returncode == 0:
            return r.stdout
        err = r.stderr if not raw else r.stderr.decode(errors="replace")
        if "rate limit" in err.lower() or "502" in err or "503" in err:
            time.sleep(10 * (attempt + 1))
            continue
        raise RuntimeError(f"{' '.join(cmd)} failed: {err.strip()[:300]}")
    raise RuntimeError(f"{' '.join(cmd)} failed after retries")


def clean(line):
    return ANSI.sub("", TS.sub("", line.rstrip("\r\n")))


SIG_RULES = [
    (re.compile(r"https?://[^\s\"']+"), "<url>"),
    (re.compile(r"\b[A-Za-z]:\\[^\s\"'():]*|(?<![\w\\.])/(?:[\w.\-]+/)+[\w.\-]*"), "<path>"),
    (re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"), "<hex>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|seconds?|minutes?)\b"), "<dur>"),
    (re.compile(r":\d{2,5}\b"), ":<port>"),
    (re.compile(r"#\d+"), "#<n>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def normalise(text):
    for rx, sub_ in SIG_RULES:
        text = rx.sub(sub_, text)
    return text.strip()


def sig_of(kind, excerpt):
    """Error signature: sha1 of the normalised excerpt (constant for noisy kinds)."""
    import hashlib
    basis = kind if kind in CONSTANT_SIG_KINDS else normalise(excerpt or "")
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:8]


def branch_version(b):
    m = re.match(r"^(\d+)\.(\d+)$", b or "")
    return (int(m.group(1)), int(m.group(2))) if m else (99, 99)  # 8.x / main: newest


def job_url(run_id, job_id):
    return f"https://github.com/{REPO}/actions/runs/{run_id}/job/{job_id}"


def load_ledger(args):
    path = p(args, "runs.jsonl")
    if not os.path.exists(path):
        return {}
    return {r["run_id"]: r for r in (json.loads(l) for l in open(path, encoding="utf-8") if l.strip())}


def short_job(name):
    return name.replace("Unit Tests ", "unit ").strip()


def run_url(run_id):
    return f"https://github.com/{REPO}/actions/runs/{run_id}"


def save_run(args, r):
    jdump(p(args, "cache", "runs", f"{r['id']}.json"), {
        "id": r["id"],
        "workflow": os.path.basename(r.get("path") or r.get("workflow") or r.get("name", "")),
        "name": r.get("name", ""),
        "branch": r.get("head_branch", r.get("branch", "")),
        "event": r.get("event", ""),
        "sha": (r.get("head_sha") or r.get("sha") or "")[:10],
        "created_at": r.get("created_at", ""),
        "title": (r.get("display_title") or r.get("title") or "")[:120],
        "conclusion": r.get("conclusion", ""),
    })


def save_job(args, run_id, job_id, name, content, original_lines=None):
    """Store a job log; keep the longest copy if one already exists."""
    log_path = p(args, "cache", "logs", f"{job_id}.log")
    lines = content.count("\n") + 1
    meta_path = p(args, "cache", "jobs", f"{job_id}.json")
    old = jload(meta_path)
    if old and old.get("log_lines", 0) >= lines:
        return
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(content)
    jdump(meta_path, {
        "id": int(job_id), "run_id": int(run_id), "name": name, "log_lines": lines,
        "truncated": bool(original_lines and original_lines > lines),
        "original_lines": original_lines or lines,
    })


# ---------------------------------------------------------------- collect (gh)
def cmd_collect(args):
    window = f"{args.since}..{args.until}"
    ledger = load_ledger(args)
    for wf in args.workflows:
        for event in args.events:
            out = gh("-X", "GET", f"repos/{REPO}/actions/workflows/{wf}/runs",
                     "-f", f"event={event}", "-f", "status=failure", "-f", f"created={window}",
                     "-f", "per_page=100", "--paginate", "--jq", ".workflow_runs[] | tojson")
            runs = [json.loads(x) for x in out.splitlines() if x.strip()]
            if args.branches:
                runs = [r for r in runs if r["head_branch"] in args.branches]
            print(f"{wf} {event}: {len(runs)} failed runs in {window}", file=sys.stderr)
            for r in sorted(runs, key=lambda r: r["id"]):
                if r["id"] in ledger and not args.force:  # already processed (runs.jsonl)
                    continue
                save_run(args, r)
                jobs_out = gh(f"repos/{REPO}/actions/runs/{r['id']}/jobs?filter=latest&per_page=100",
                              "--paginate", "--jq", ".jobs[] | tojson")
                for j in (json.loads(x) for x in jobs_out.splitlines() if x.strip()):
                    if j.get("conclusion") != "failure":
                        continue
                    if os.path.exists(p(args, "cache", "logs", f"{j['id']}.log")) and not args.refresh:
                        continue
                    try:
                        log = gh(f"repos/{REPO}/actions/jobs/{j['id']}/logs")
                    except RuntimeError as e:  # logs expired / proxy blocked
                        print(f"  ! job {j['id']}: {e}", file=sys.stderr)
                        continue
                    save_job(args, r["id"], j["id"], j["name"], log)
                    print(f"  + {r['id']} {j['name']}", file=sys.stderr)


# ---------------------------------------------------------------- ingest (MCP dumps)
def cmd_ingest_mcp(args):
    """Accept raw JSON returned by GitHub MCP `actions_list list_workflow_runs`
    and `get_job_logs failed_only=true return_content=true` (as saved to files).
    Runs already in runs.jsonl are skipped unless --force."""
    files = sorted({f for pat in args.files for f in glob.glob(pat)})
    ledger = load_ledger(args)
    listed, new_runs, njobs, skipped = set(), set(), 0, set()
    for f in files:
        try:
            d = jload(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"skip (not JSON): {f}", file=sys.stderr)
            continue
        if isinstance(d, dict) and "workflow_runs" in d:
            for r in d["workflow_runs"]:
                if r.get("conclusion") == "failure":
                    if r["id"] in ledger and not args.force:
                        skipped.add(r["id"])
                        continue
                    save_run(args, r)
                    listed.add(r["id"])
        elif isinstance(d, dict) and "logs" in d and "run_id" in d:
            if int(d["run_id"]) in ledger and not args.force:
                skipped.add(int(d["run_id"]))
                continue
            for j in d["logs"]:
                c = j.get("logs_content") or ""
                if c:
                    save_job(args, d["run_id"], j["job_id"], j["job_name"], c, j.get("original_length"))
                    new_runs.add(int(d["run_id"]))
                    njobs += 1
        elif isinstance(d, dict) and "job_id" in d and "logs_content" in d:  # single-job call
            if (args.run_id or 0) in ledger and not args.force:
                skipped.add(args.run_id)
                continue
            save_job(args, args.run_id or 0, d["job_id"], d.get("job_name", ""), d["logs_content"],
                     d.get("original_length"))
            new_runs.add(args.run_id or 0)
            njobs += 1
    print(f"ingested {len(new_runs)} new runs ({njobs} job logs), {len(listed)} failed runs listed; "
          f"skipped {len(skipped)} runs already in runs.jsonl; {len(files)} files", file=sys.stderr)


# ---------------------------------------------------------------- parse
def parse_log(text):
    """Return list of failure dicts found in one job log (order of appearance)."""
    lines = [clean(l) for l in text.splitlines()]
    found, component, section = [], None, None
    for i, line in enumerate(lines):
        m = re.match(r"^Testing (src[\\/]\S+)", line) or re.search(r"Configuration: .*?(src[\\/][\w\\/]+?)[\\/]phpunit", line)
        if m:
            component = m.group(1).replace("\\", "/")
        s = SECTION.match(line)
        if s:
            section = re.sub(r"s$", "", s.group(1)).replace("risky test", "risky")
            continue
        m = NUMBERED.match(line)
        if m and section:
            subject = m.group(2).strip()
            detail = next((x.strip() for x in lines[i + 1:i + 4] if x.strip()), "")
            t = TEST_ID.match(subject)
            if t:
                key = f"{t['cls']}::{t['meth']}"
                kind = "deprecation" if section == "deprecation" else "test"
            else:  # e.g. "1) /path/vendor/x.php:96" deprecation sources
                key = re.sub(r"^.*?/vendor/", "vendor/", subject.replace("\\", "/"))
                key = re.sub(r"^.*?/src/Symfony/", "src/Symfony/", key)
                kind = "deprecation"
            found.append({"key": key, "kind": kind, "section": section, "raw": subject[:200],
                          "excerpt": detail[:200], "component": component})
            continue
        m = FATAL.search(line)
        if m and not line.startswith("PHP Fatal"):
            msg = re.sub(r" in [A-Z]:\\\S+| in /\S+", "", m.group(1)).strip()
            src = re.search(r"src[\\/]Symfony[\\/][\w\\/]+\.php", m.group(1))
            key = "FATAL " + (src.group(0).replace("\\", "/") if src else msg[:80])
            found.append({"key": key, "kind": "fatal", "section": "fatal", "raw": line[:200],
                          "excerpt": msg[:200], "component": component})
            continue
        m = INFRA.search(line)
        if m:
            key = next(k for pat, k in INFRA_KEYS if re.search(pat, line))
            if not any(f["key"] == key for f in found):
                found.append({"key": key, "kind": "infra", "section": "infra", "raw": line[:200],
                              "excerpt": scrub(line.strip())[:200], "component": component})
            continue
        if line.strip() == "An error occurred inside PHPUnit.":
            msg = next((x.strip() for x in lines[i + 1:i + 4] if x.startswith("Message:")), "Message: ?")
            obj = next((re.search(r"Object\(([\w\\]+Test)\)", x) for x in lines[i + 1:i + 40]
                        if re.search(r"Object\(([\w\\]+Test)\)", x)), None)
            meth = next((re.search(r"build(?:DataProviderTestSuite)?\(.*?'(test\w*)", x) for x in lines[i + 1:i + 40]
                         if re.search(r"build(?:DataProviderTestSuite)?\(.*?'(test\w*)", x)), None)
            key = "PHPUNIT-ERROR " + (obj.group(1) if obj else "?") + (f"::{meth.group(1)}" if meth else "")
            found.append({"key": key, "kind": "phpunit-error", "section": "phpunit-error", "raw": line[:200],
                          "excerpt": msg[:200], "component": component})
            continue
        m = TIMEOUT_COMPONENT.match(line)
        if m:
            comp = m.group(1).replace("\\", "/")
            found.append({"key": f"TIMEOUT {comp}", "kind": "timeout", "section": "timeout",
                          "raw": line[:200], "excerpt": line[:200], "component": comp})
    if any(f["kind"] != "infra" for f in found):  # packagist warnings also appear in jobs that fail for other reasons
        found = [f for f in found if f["kind"] != "infra"]
    return found, lines


def cmd_parse(args):
    out, cached_jobs = [], set()
    for meta_path in sorted(glob.glob(p(args, "cache", "jobs", "*.json"))):
        job = jload(meta_path)
        cached_jobs.add(job["id"])
        run = jload(p(args, "cache", "runs", f"{job['run_id']}.json"), {}) or {}
        with open(p(args, "cache", "logs", f"{job['id']}.log"), encoding="utf-8", errors="replace") as f:
            failures, lines = parse_log(f.read())
        base = {"run_id": job["run_id"], "job_id": job["id"], "job": short_job(job["name"]),
                "branch": run.get("branch", "?"), "workflow": run.get("workflow", "?"),
                "event": run.get("event", "?"), "sha": run.get("sha", "?"),
                "date": (run.get("created_at") or "")[:10]}
        if not failures:
            end = next((k for k, l in enumerate(lines) if "Process completed with exit code" in l), len(lines))
            tail = [l for l in lines[:end] if l.strip() and not l.startswith("##[")][-3:]
            reason = "log head truncated" if job.get("truncated") else "no numbered failure in log"
            hint = next((l for l in reversed(lines) if re.search(r"Remaining (direct|indirect) deprecation|Unsilenced", l)), "")
            failures = [{"key": f"UNATTRIBUTED {base['job']}", "kind": "unattributed", "section": reason,
                         "raw": "", "excerpt": (hint or " | ".join(tail))[:200], "component": None}]
        seen = set()
        for f_ in failures:
            ident = (f_["key"], f_["kind"])
            if ident in seen:  # PHPUnit repeats the same deprecation N times per job
                continue
            seen.add(ident)
            out.append({**base, **f_})
    # records of jobs whose log is not in this cache (older scans, other agents) are kept
    old_path = p(args, "failures.jsonl")
    if os.path.exists(old_path):
        # ...unless backlog.json already attributes that job to another key (a later `resolve`)
        known = {}
        for e in (jload(p(args, "backlog.json"), {}) or {}).values():
            for o in e.get("occurrences", []):
                known.setdefault(o["job_id"], set()).add(e["key"])
        out += [r for r in (json.loads(l) for l in open(old_path, encoding="utf-8") if l.strip())
                if r["job_id"] not in cached_jobs and r["key"] in known.get(r["job_id"], {r["key"]})]
    for r in out:
        r["sig"] = sig_of(r["kind"], r.get("excerpt", ""))
    out.sort(key=lambda r: (r["key"], r["sig"], r["run_id"], r["job_id"]))
    with open(old_path, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    write_ledger(args, out)
    print(f"{len(out)} failure records ({len(cached_jobs)} jobs in cache)", file=sys.stderr)


def write_ledger(args, recs):
    """runs.jsonl: one line per processed run (a run with at least one parsed job log)."""
    ledger = load_ledger(args)
    runs = {}
    for meta_path in sorted(glob.glob(p(args, "cache", "jobs", "*.json"))):
        job = jload(meta_path)
        runs.setdefault(job["run_id"], {"failed": set(), "parsed": set(), "unattributed": set()})["failed"].add(job["id"])
    for r in recs:
        e = runs.setdefault(r["run_id"], {"failed": set(), "parsed": set(), "unattributed": set()})
        e["failed"].add(r["job_id"])
        e["unattributed" if r["kind"] == "unattributed" else "parsed"].add(r["job_id"])
    first = {}
    for r in recs:
        first.setdefault(r["run_id"], r)
    rows = []
    for rid in sorted(set(runs) | set(ledger)):
        old = ledger.get(rid, {})
        cached = jload(p(args, "cache", "runs", f"{rid}.json"), {}) or {}
        rec = first.get(rid, {})
        e = runs.get(rid, {"failed": set(), "parsed": set(), "unattributed": set()})
        rows.append({
            "run_id": rid,
            "workflow": cached.get("workflow") or old.get("workflow") or rec.get("workflow", "?"),
            "event": cached.get("event") or old.get("event") or rec.get("event", "?"),
            "branch": cached.get("branch") or old.get("branch") or rec.get("branch", "?"),
            "sha": cached.get("sha") or old.get("sha") or rec.get("sha", "?"),
            "date": (cached.get("created_at") or "")[:10] or old.get("date") or rec.get("date", "?"),
            "conclusion": cached.get("conclusion") or old.get("conclusion") or "failure",
            "failed_job_ids": sorted(e["failed"] | set(old.get("failed_job_ids", []))),
            "parsed_job_ids": sorted(e["parsed"] | set(old.get("parsed_job_ids", []))),
            "unattributed_job_ids": sorted((e["unattributed"] | set(old.get("unattributed_job_ids", [])))
                                           - (e["parsed"] - e["unattributed"])),
        })
    with open(p(args, "runs.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- classify
def classify(key, kind, occ):
    text = " ".join([key] + [o["excerpt"] for o in occ])
    shas = {o["sha"] for o in occ}
    per_sha = {}
    for o in occ:
        per_sha.setdefault(o["sha"], set()).add(o["job_id"])
    max_jobs_same_sha = max(len(v) for v in per_sha.values())
    if kind == "unattributed":
        return "unknown", "failing test not visible in log (re-fetch full log)"
    if kind == "deprecation":
        return "real", "deprecation"
    if kind == "infra":
        return "flaky", "CI infrastructure (composer download), not a test"
    for pat, why in REAL_PATTERNS:
        if re.search(pat, text, re.I):
            return "real", why
    if max_jobs_same_sha >= 3:
        return "real", f"fails in {max_jobs_same_sha} jobs of the same commit"
    for pat, why in FLAKY_PATTERNS:
        if re.search(pat, text, re.I):
            return "flaky", why
    if len(shas) >= 2:
        return "flaky", f"fails intermittently on {len(shas)} unrelated commits"
    return "unknown", "single occurrence, no flaky signature"


def migrate_legacy(old):
    """Old backlogs were keyed by test key only; split them into `key#sig` entries.
    Every signature split from a legacy entry inherits its manual fields."""
    out = {}
    for oid in sorted(old):
        e = old[oid]
        if e.get("id") == oid and "#" in oid:
            out[oid] = e
            continue
        bysig = {}
        for o in e.get("occurrences", []):
            bysig.setdefault(sig_of(e["kind"], o.get("excerpt", "")), []).append(o)
        if not bysig:
            bysig[sig_of(e["kind"], "")] = []
        for sg in sorted(bysig):
            nid = f"{e['key']}#{sg}"
            prev = out.get(nid)
            occ = bysig[sg] + (prev["occurrences"] if prev else [])
            out[nid] = {**e, "id": nid, "sig": sg, "occurrences": occ}
    return out


class Git:
    """`git merge-base --is-ancestor` with a cache; None when a commit is unknown to the clone."""
    def __init__(self, repo):
        self.repo, self.cache = repo, {}

    def is_ancestor(self, a, b):
        if (a, b) not in self.cache:
            r = subprocess.run(["git", "-C", self.repo, "merge-base", "--is-ancestor", a, b], capture_output=True)
            self.cache[(a, b)] = {0: True, 1: False}.get(r.returncode)
        return self.cache[(a, b)]


def fix_check(entry, git, prev_check):
    """Compare each occurrence with fixed_by.commits {branch: sha}.
    open_branches: occurrences on a branch below every fix branch (the fix never reaches it).
    regressed: occurrences whose commit already contains the fix (needs --repo; kept from the
    previous run otherwise, so `all` without --repo stays deterministic)."""
    commits = (entry.get("fixed_by") or {}).get("commits") or {}
    if not commits:
        return {}
    open_b, regressed = set(), set()
    for o in entry["occurrences"]:
        relevant = [c for b, c in sorted(commits.items()) if branch_version(b) <= branch_version(o["branch"])]
        if not relevant:
            open_b.add(o["branch"])
        elif git and any(git.is_ancestor(c, o["sha"]) for c in relevant):
            regressed.add(o["run_id"])
    check = {"open_branches": sorted(open_b)}
    check["regressed_runs"] = sorted(regressed) if git else list(prev_check.get("regressed_runs", []))
    return check


def cmd_classify(args):
    recs = [json.loads(l) for l in open(p(args, "failures.jsonl"), encoding="utf-8") if l.strip()]
    old = migrate_legacy(jload(p(args, "backlog.json"), {}) or {})
    git = Git(args.repo) if getattr(args, "repo", None) else None
    # a shorter (tail-only) log must not downgrade a job the backlog already attributes to a test:
    # drop its UNATTRIBUTED record and keep the old occurrences of that job
    attributed = {o["job_id"] for e in old.values() if e["kind"] != "unattributed" for o in e.get("occurrences", [])}
    kinds_by_job = {}
    for r in recs:
        kinds_by_job.setdefault(r["job_id"], set()).add(r["kind"])
    downgraded = {j for j, k in kinds_by_job.items() if k == {"unattributed"} and j in attributed}
    recs = [r for r in recs if r["job_id"] not in downgraded]
    groups = {}
    for r in recs:
        sg = r.get("sig") or sig_of(r["kind"], r.get("excerpt", ""))
        groups.setdefault(f"{r['key']}#{sg}", []).append(r)
    parsed_jobs = {r["job_id"] for r in recs}  # current parse is authoritative for these jobs
    backlog = {}
    for eid in sorted(set(groups) | set(old)):
        prev = old.get(eid, {})
        kept = [o for o in prev.get("occurrences", []) if o["job_id"] not in parsed_jobs]
        occ = groups.get(eid)
        if not occ:
            # history from older windows / other caches is append-only; drop entries that a
            # re-parse made obsolete unless a human/agent already annotated them
            if kept or any(prev.get(f) not in (None, "", "new", {}) for f in MANUAL_FIELDS):
                backlog[eid] = {**prev, "occurrences": kept}
            continue
        # merge occurrences with those already known (older scans / other agents)
        merged = {(o["run_id"], o["job_id"]): o for o in kept}
        for o in occ:
            merged[(o["run_id"], o["job_id"])] = {k: o[k] for k in
                ("run_id", "job_id", "job", "branch", "workflow", "event", "sha", "date", "excerpt")}
        occurrences = [merged[k] for k in sorted(merged)]
        kind = occ[0]["kind"]
        key, sg = eid.rsplit("#", 1)
        cls, why = classify(key, kind, occurrences)
        entry = {
            "id": eid, "key": key, "sig": sg, "kind": kind,
            "component": next((o["component"] for o in occ if o.get("component")), prev.get("component")),
            "classification": cls, "reason": why,
            "occurrences": occurrences,
            "links": prev.get("links", []),
            "fix_check": prev.get("fix_check", {}),
        }
        for f in MANUAL_FIELDS:
            entry[f] = prev.get(f, MANUAL_DEFAULTS.get(f, ""))
        backlog[eid] = entry
    for e in backlog.values():
        e.setdefault("fixed_by", {})
        e.setdefault("board", "")
        e["fix_check"] = fix_check(e, git, e.get("fix_check") or {})
        if e["fix_check"].get("regressed_runs"):
            e["status"] = "regressed"
    jdump(p(args, "backlog.json"), backlog)
    c = {}
    for e in backlog.values():
        k = e.get("classification_override") or e.get("classification")
        c[k] = c.get(k, 0) + 1
    print(f"backlog: {len(backlog)} entries {dict(sorted(c.items()))}", file=sys.stderr)


# ---------------------------------------------------------------- link (search)
def search_terms(entry):
    key = entry["key"]
    if "::" in key:
        cls, meth = key.split("::")
        return [meth, cls.rsplit("\\", 1)[-1]]
    if key.startswith("FATAL "):  # file name + first significant word of the message
        base = key.split(" ", 1)[1].rsplit("/", 1)[-1].replace(".php", "")
        word = next((w for w in re.findall(r"[A-Za-z]{4,}", entry["occurrences"][0]["excerpt"])
                     if w.lower() not in ("class", "fatal", "error", "cannot")), "")
        return [f"{base} {word}".strip()]
    if key.startswith("TIMEOUT "):  # component names are too generic; search CI timeouts on Windows
        return [f'"{key.split("/")[-1]}" timeout windows']
    return []  # UNATTRIBUTED: nothing to search until the full log is fetched


def cmd_link(args):
    backlog = jload(p(args, "backlog.json"), {})
    todo = [k for k, e in sorted(backlog.items())
            if (e.get("classification_override") or e.get("classification")) in args.classes
            and (args.refresh or not e.get("links"))
            and (not args.match or re.search(args.match, k))]
    for key in todo:
        links = {}
        for term in search_terms(backlog[key])[:1 if args.quick else 2]:
            for kind in ("is:issue", "is:pull-request"):
                q = f"repo:{REPO} {kind} {term}"
                cache = p(args, "cache", "search", re.sub(r"[^\w.-]+", "_", q) + ".json")
                items = jload(cache)
                if items is None:
                    out = gh("-X", "GET", "search/issues", "-f", f"q={q}", "-f", "per_page=10",
                             "--jq", "[.items[] | {number, title, state, created_at, pr: (.pull_request != null)}]")
                    items = json.loads(out or "[]")
                    jdump(cache, items)
                    time.sleep(args.sleep)  # search API: ~10-30 req/min
                for it in items:
                    if re.match(r"Release v\d", it["title"]):  # release PRs list every change: noise
                        continue
                    links[it["number"]] = {"number": it["number"], "title": it["title"][:120],
                                           "state": it["state"], "pr": it["pr"], "date": it["created_at"][:10],
                                           "matched": term}
        backlog[key]["links"] = [links[n] for n in sorted(links, reverse=True)][:6]
        print(f"{key}: {len(links)} links", file=sys.stderr)
    jdump(p(args, "backlog.json"), backlog)


# ---------------------------------------------------------------- report
def md(s):
    return (s or "").replace("|", "\\|").replace("\n", " ")


def cmd_report(args):
    backlog = jload(p(args, "backlog.json"), {})
    order = {"flaky": 0, "unknown": 1, "real": 2}
    rows = sorted(backlog.values(), key=lambda e: (
        order.get(e.get("classification_override") or e["classification"], 9),
        -len(e["occurrences"]), e["id"]))
    meta = jload(p(args, "scan.json"), {}) or {}
    out = ["# Flaky test backlog — symfony/symfony", "",
           f"Window: {meta.get('since', '?')} → {meta.get('until', '?')} · workflows: "
           f"{', '.join(meta.get('workflows', WORKFLOWS))} · events: {', '.join(meta.get('events', ['push']))}",
           "", "Generated by `flaky_scan.py report` from `backlog.json` (do not edit this file; edit backlog.json manual fields).",
           "", "| # | test / signature | sig | class | why | occ | branches | jobs | first excerpt | runs | links | status |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, e in enumerate(rows, 1):
        occ = e["occurrences"]
        cls = e.get("classification_override") or e["classification"]
        runs = sorted({o["run_id"] for o in occ})
        fb = e.get("fixed_by") or {}
        fixed = f" (fixed by #{fb['pr']})" if fb.get("pr") else ""
        if (e.get("fix_check") or {}).get("open_branches"):
            fixed += f" — open on {', '.join(e['fix_check']['open_branches'])}"
        out.append("| {} | `{}` | {} | **{}** | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            i, md(e["key"]), e.get("sig", ""), cls, md(e["reason"]), len(occ),
            ", ".join(sorted({o["branch"] for o in occ})),
            md("; ".join(sorted({o["job"] for o in occ}))[:120]),
            md(occ[0]["excerpt"][:110] if occ else "(no occurrence left)"),
            " ".join(f"[{r}]({run_url(r)})" for r in runs[:4]) + (" …" if len(runs) > 4 else ""),
            " ".join(f"#{l['number']}{'(PR)' if l['pr'] else ''}" for l in e.get("links", [])[:4]) or "—",
            md(e.get("status", "new")) + md(fixed) + (f" — {md(e['notes'])}" if e.get("notes") else ""),
        ))
    with open(p(args, "BACKLOG.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"wrote {p(args, 'BACKLOG.md')} ({len(rows)} rows)", file=sys.stderr)


# ---------------------------------------------------------------- resolve (truncated logs)
GREP = re.compile(r"^\d+\) Symfony\\|KO src[\\/]|Remaining (direct|self|indirect).*deprecation|Unsilenced deprecation|"
                  r"Legacy deprecation|Other deprecation|Fatal error|Timeout src[\\/]|^(FAILURES|ERRORS)!|^\s*\d+x(: | in )")


def host_of(url):
    from urllib.parse import urlsplit
    return urlsplit(url).hostname or "?"


def scrub(text):
    """Replace any URL by its host so signed query strings are never printed or stored."""
    return re.sub(r"https?://[^\s\"']+", lambda m: host_of(m.group(0)), text)


def grep_lines(text, limit=40):
    lines = [clean(x) for x in text.splitlines()]
    out = []
    for i, l in enumerate(lines):
        if GREP.search(l):
            block = [l] + ([lines[i + 1].strip()] if re.match(r"^\s*\d+x", l) and i + 1 < len(lines) else [])
            for b in block:
                b = scrub(b)[:220]
                if b and b not in out:
                    out.append(b)
    return out[:limit]


def fetch_url(url, dest):
    r = subprocess.run(["curl", "-sS", "-L", "--max-time", "180", "-o", dest, "-w", "%{http_code}", url],
                       capture_output=True, text=True)
    return (r.stdout.strip() or "000"), scrub(r.stderr.strip())[:300]


def cmd_resolve(args):
    """UNATTRIBUTED jobs: stored tail -> check-run annotations -> full log -> none."""
    recs = [json.loads(l) for l in open(p(args, "failures.jsonl"), encoding="utf-8") if l.strip()]
    meta_by_job = {r["job_id"]: r for r in recs}
    jobs = sorted({r["job_id"] for r in recs if r["kind"] == "unattributed"} | set(args.jobs or []))
    urls = jload(args.log_urls, {}) if args.log_urls else {}
    rows = []
    for jid in jobs:
        m = meta_by_job.get(jid, {})
        row = {"job_id": jid, "run_id": m.get("run_id"), "branch": m.get("branch", "?"),
               "workflow": m.get("workflow", "?"), "job": m.get("job", "?"),
               "source": "none", "lines": [], "log_host": None, "fetch": None}
        log_path = p(args, "cache", "logs", f"{jid}.log")
        tail = open(log_path, encoding="utf-8", errors="replace").read() if os.path.exists(log_path) else ""
        if any(re.match(r"^\d+\) Symfony\\|.*KO src|.*Fatal error", l) for l in grep_lines(tail)):
            row.update(source="tail", lines=grep_lines(tail))
        if row["source"] == "none" and not args.no_gh:
            try:
                ann = json.loads(gh(f"repos/{REPO}/check-runs/{jid}/annotations") or "[]")
                msgs = [scrub(f"{a.get('annotation_level')}: {a.get('message', '')}".replace("\n", " "))[:220] for a in ann]
                msgs = [x for x in msgs if not re.search(r"Process completed with exit code|Node\.js \d+", x)]
                if msgs:
                    row.update(source="annotations", lines=msgs)
            except RuntimeError as e:
                row["annotations_error"] = scrub(str(e))[:200]
        if row["source"] == "none":
            dest = p(args, "cache", "full", f"{jid}.log")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            url, ok = urls.get(str(jid)), False
            if url:
                row["log_host"] = host_of(url)
                code, err = fetch_url(url, dest)
                row["fetch"] = f"HTTP {code}" + (f" - {err}" if err else "")
                ok = code == "200"
            elif not args.no_gh:
                try:
                    with open(dest, "w", encoding="utf-8") as f:
                        f.write(gh(f"repos/{REPO}/actions/jobs/{jid}/logs"))
                    row["fetch"], ok = "gh api ok", True
                except RuntimeError as e:
                    row["fetch"] = scrub(str(e))[:300]
            else:
                row["fetch"] = "no URL and --no-gh"
            if ok and os.path.getsize(dest) > 0:
                text = open(dest, encoding="utf-8", errors="replace").read()
                row.update(source="full log", lines=grep_lines(text))
                save_job(args, row["run_id"] or 0, jid, m.get("job", ""), text)  # longest log wins; `all` re-attributes
            elif os.path.exists(dest):
                os.remove(dest)
        if row["source"] == "none":
            row["lines"] = grep_lines(tail)[:3] or ["unattributed (log truncated)"]
        rows.append(row)
    jdump(p(args, "resolve.json"), rows)
    out = ["# Unattributed jobs - resolution attempts", "",
           "Order: stored tail -> check-run annotations -> full log (signed URL / gh) -> none. Signed query strings are never stored.", "",
           "| job | branch | job name | run | source | log host | fetch | extracted lines |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append("| {} | {} | {} | [{}]({}) | **{}** | {} | {} | {} |".format(
            r["job_id"], r["branch"], md(r["job"]), r["run_id"], run_url(r["run_id"]), r["source"],
            r["log_host"] or "-", md(r["fetch"] or "-"), md("<br>".join(r["lines"][:10]))))
    with open(p(args, "RESOLVE.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    by = {}
    for r in rows:
        by[r["source"]] = by.get(r["source"], 0) + 1
    print(f"resolve: {len(rows)} jobs {dict(sorted(by.items()))}; wrote RESOLVE.md (run `all` next)", file=sys.stderr)


def cmd_annotate(args):
    backlog = jload(p(args, "backlog.json"), {})
    hits = [k for k in sorted(backlog) if re.search(args.match, k)]
    if len(hits) > 1 and args.all_sigs:
        for h in hits:
            sub_args = argparse.Namespace(**{**vars(args), "match": "^" + re.escape(h) + "$", "all_sigs": False})
            cmd_annotate(sub_args)
        return
    if len(hits) != 1:
        sys.exit(f"--match must select exactly one entry, got {len(hits)}: {hits[:5]}")
    e = backlog[hits[0]]
    if args.status:
        e["status"] = args.status
    if args.owner is not None:
        e["owner"] = args.owner
    if args.override is not None:
        e["classification_override"] = args.override
    if args.notes is not None:
        e["notes"] = args.notes
    if args.board is not None:
        e["board"] = args.board
    if args.fixed_by is not None or args.fix_commit:
        fb = dict(e.get("fixed_by") or {})
        if args.fixed_by is not None:
            fb["pr"] = int(args.fixed_by)
        if args.merged_at:
            fb["merged_at"] = args.merged_at
        commits = dict(fb.get("commits") or {})
        for spec in args.fix_commit or []:
            b, _, sha = spec.partition("=")
            commits[b] = sha[:11]
        fb["commits"] = commits
        e["fixed_by"] = fb
    jdump(p(args, "backlog.json"), backlog)
    print(f"{hits[0]}: status={e['status']} override={e['classification_override'] or '-'}"
          f"{' fixed_by=#' + str(e['fixed_by']['pr']) if (e.get('fixed_by') or {}).get('pr') else ''}", file=sys.stderr)


# ---------------------------------------------------------------- seen / backfill
def entry_line(e):
    occ = e["occurrences"]
    dates = sorted(o["date"] for o in occ) or ["?"]
    fb = e.get("fixed_by") or {}
    fix = f" fixed_by=#{fb['pr']}" if fb.get("pr") else ""
    opn = (e.get("fix_check") or {}).get("open_branches")
    return (f"{e['id']} | {e.get('classification_override') or e['classification']} | {e.get('status', 'new')}{fix}"
            f"{' open_on=' + ','.join(opn) if opn else ''} | first {dates[0]} | last {dates[-1]} | {len(occ)} occ")


def cmd_seen(args):
    """Answer: run already classified? / error already seen? / already fixed?"""
    backlog = jload(p(args, "backlog.json"), {}) or {}
    ledger = load_ledger(args)
    if args.run:
        r = ledger.get(args.run)
        print(f"run {args.run}: " + (f"in runs.jsonl ({r['branch']} {r['sha']} {r['date']}, "
                                      f"{len(r['parsed_job_ids'])} parsed / {len(r['unattributed_job_ids'])} unattributed jobs)"
                                      if r else "NOT in runs.jsonl (not processed yet)"))
    hits = []
    for eid in sorted(backlog):
        e = backlog[eid]
        if args.key and not re.search(args.key, e["key"]):
            continue
        if args.run and not any(o["run_id"] == args.run for o in e["occurrences"]):
            continue
        if args.job and not any(o["job_id"] == args.job for o in e["occurrences"]):
            continue
        if args.excerpt is not None and (e["kind"] in CONSTANT_SIG_KINDS or e.get("sig") != sig_of(e["kind"], args.excerpt)):
            continue
        hits.append(e)
    if args.excerpt is not None and not hits:
        print(f"signature {sig_of('test', args.excerpt)}: never seen")
    for e in hits:
        print(entry_line(e))
    if not (args.run or args.job or args.excerpt is not None or args.key):
        sys.exit("give --run, --job, --excerpt and/or --key")


FIX_NOTE = re.compile(r"(?:[Ff]ixed (?:in CI )?by|merged on \S+ by) #(\d+)")


def cmd_backfill(args):
    """Fill fixed_by {pr, merged_at, commits{branch: sha}} from "fixed by #NNNN" notes, using the
    merge commit found with `git log --first-parent --grep` on the lowest branch of an upstream clone."""
    backlog = jload(p(args, "backlog.json"), {}) or {}
    branches = sorted({o["branch"] for e in backlog.values() for o in e["occurrences"]} | set(args.branches),
                      key=branch_version)
    found = {}
    for eid in sorted(backlog):
        e = backlog[eid]
        if (e.get("fixed_by") or {}).get("commits") and not args.refresh:
            continue
        m = FIX_NOTE.search(e.get("notes") or "")
        if not m:
            continue
        pr = int(m.group(1))
        if pr not in found:
            found[pr] = None
            for b in branches:
                r = subprocess.run(["git", "-C", args.repo, "log", f"{args.remote}/{b}", "--first-parent", "-E",
                                    f"--grep=#{pr}([^0-9]|$)", "--format=%H %cI"], capture_output=True, text=True)
                lines = [l for l in r.stdout.splitlines() if l.strip()]
                if r.returncode == 0 and lines:
                    sha, date = lines[-1].split()
                    found[pr] = (b, sha[:11], date)
                    break
        if not found[pr]:
            print(f"{eid}: #{pr} not found in {args.repo}", file=sys.stderr)
            continue
        b, sha, date = found[pr]
        e["fixed_by"] = {"pr": pr, "merged_at": date, "commits": {b: sha}}
        print(f"{eid}: fixed_by #{pr} {b}={sha}", file=sys.stderr)
    jdump(p(args, "backlog.json"), backlog)


# ---------------------------------------------------------------- candidates
def kb_patterns(path):
    """Rows of KNOWLEDGE.md section 6: (first cell, literal fragments to look for)."""
    if not os.path.exists(path):
        return []
    text = open(path, encoding="utf-8").read()
    m = re.search(r"^## 6\. .*?$(.*?)(?=^## )", text, re.M | re.S)
    rows = []
    for line in (m.group(1).splitlines() if m else []):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("Signature", "") or set(cells[0]) <= set("-"):
            continue
        frags = []
        for f_ in re.findall(r"`([^`]+)`", cells[0]):
            f_ = re.split(r"[\"(]", f_)[0].strip()
            if len(f_) >= 10:
                frags.append(f_)
        rows.append((cells[0], cells[1], cells[2], frags))
    return rows


def short_component(e):
    comp = e.get("component") or ""
    m = re.search(r"src/Symfony/(?:Component|Bridge|Bundle|Contracts)/([\w/]+)", comp)
    if m:
        return m.group(1).replace("/", "-")
    m = re.match(r"Symfony\\(?:Component|Bridge|Bundle|Contracts)\\(\w+)", e["key"])
    return m.group(1) if m else re.sub(r"\W+", "-", e["key"].split(" ")[0]).strip("-") or "misc"


def test_path(key):
    if "::" not in key:
        return None
    return "src/" + key.split("::")[0].replace("\\", "/") + ".php"


def candidate_scopes(e, out_of_scope=()):
    """(scope suffix, occurrences) pairs that make this entry a candidate.
    Occurrences on out-of-scope branches (scan.json out_of_scope_branches) never count."""
    cls = e.get("classification_override") or e["classification"]
    if cls not in ("flaky", "unknown") or e["kind"] in ("infra", "unattributed"):
        return []
    occ = [o for o in e["occurrences"] if o["branch"] not in out_of_scope]
    if not occ:
        return []
    if e.get("status", "new") in OPEN_STATUSES:
        return [("", occ)]
    opn = [b for b in (e.get("fix_check") or {}).get("open_branches") or [] if b not in out_of_scope]
    if opn:
        return [("--open-" + "-".join(opn), [o for o in occ if o["branch"] in opn])]
    return []


def cmd_candidates(args):
    backlog = jload(p(args, "backlog.json"), {}) or {}
    kb = kb_patterns(args.kb or os.path.join(args.dir, "..", "KNOWLEDGE.md"))
    out_of_scope = tuple((jload(p(args, "scan.json"), {}) or {}).get("out_of_scope_branches", []))
    outdir = p(args, "candidates")
    os.makedirs(outdir, exist_ok=True)
    written, index = set(), []
    for eid in sorted(backlog):
        e = backlog[eid]
        for scope, occ in candidate_scopes(e, out_of_scope):
            meth = e["key"].split("::")[-1] if "::" in e["key"] else re.sub(r"\W+", "-", e["key"]).strip("-")
            fname = re.sub(r"[^\w.\-]+", "-", f"{short_component(e)}--{meth}--{e['sig']}{scope}") + ".md"
            written.add(fname)
            cls = e.get("classification_override") or e["classification"]
            fb = e.get("fixed_by") or {}
            branches = sorted({o["branch"] for o in occ}, key=branch_version)
            text = " ".join([e["key"]] + [o["excerpt"] for o in occ])
            kb_hits = [r for r in kb if any(f_ in text for f_ in r[3])]
            windows_only = all(o["workflow"] == "windows.yml" for o in occ)
            tp = test_path(e["key"])
            lines = [
                f"# {e['key']}", "",
                "<!-- generated by flaky_scan.py candidates; do not edit: change backlog.json with `annotate` -->", "",
                "| field | value |", "|---|---|",
                f"| test | `{md(e['key'])}` |",
                f"| signature | `{e['sig']}` (entry id `{md(eid)}`) |",
                f"| class | {cls} ({md(e['reason'])}) |",
                f"| status | {e.get('status', 'new')}{' (scope: branches ' + ', '.join(branches) + ' not reached by the fix)' if scope else ''} |",
                f"| owner | {md(e.get('owner')) or '—'} |",
                f"| board record | {md(e.get('board')) or '— (not picked)'} |",
                f"| branches | {', '.join(branches)} |",
                f"| first / last seen | {min(o['date'] for o in occ)} / {max(o['date'] for o in occ)} |", "",
                "## Occurrences", "",
                "| run | job | branch | job name | sha | date | excerpt |", "|---|---|---|---|---|---|---|"]
            for o in sorted(occ, key=lambda o: (o["date"], o["run_id"], o["job_id"])):
                lines.append(f"| [{o['run_id']}]({run_url(o['run_id'])}) | [{o['job_id']}]({job_url(o['run_id'], o['job_id'])}) "
                             f"| {o['branch']} | {md(o['job'])} | {o['sha']} | {o['date']} | {md(o['excerpt'][:160])} |")
            lines += ["", "## Notes, links and fix status", ""]
            lines.append(f"- Notes: {md(e.get('notes')) or '—'}")
            lines.append("- Related issues/PRs (full-text search, may be noise): " +
                         (", ".join(f"#{l['number']}{' (PR)' if l['pr'] else ''} {md(l['title'][:60])}" for l in e.get("links", [])) or "—"))
            if fb.get("pr"):
                lines.append(f"- Fixed by #{fb['pr']} (merged {fb.get('merged_at', '?')}; commits "
                             f"{', '.join(f'{b}={c}' for b, c in sorted((fb.get('commits') or {}).items()))}).")
            chk = e.get("fix_check") or {}
            if chk.get("open_branches"):
                lines.append(f"- The fix does not reach: {', '.join(chk['open_branches'])} (lower than every fix commit).")
            if chk.get("regressed_runs"):
                lines.append(f"- REGRESSED after the fix in runs {', '.join(map(str, chk['regressed_runs']))}.")
            if not fb.get("pr"):
                lines.append("- No upstream fix recorded.")
            lines.append("- KNOWLEDGE.md section 6 pattern: " + (
                "; ".join(f"{md(r[0][:120])} → {md(r[2][:120])}" for r in kb_hits) if kb_hits else "none matched"))
            lines += ["", "## Worker brief", "",
                      f"- Base branch to try: **{branches[0]}** (lowest branch with an occurrence); first check lower maintained "
                      f"branches with `git show upstream/<branch>:{tp or '<test file>'}` (KNOWLEDGE.md section 1 step 3).",
                      f"- Test: `{tp}`" if tp else f"- Signature `{md(e['key'])}` is not a single test: find the failing test in the run logs first.",
                      "- Duplicates: search merged PRs and issues by method name (KNOWLEDGE.md section 1 step 2).",
                      "- Repro: " + ("Windows only: KNOWLEDGE.md section 2b (fork Actions repro). " if windows_only else "")
                      + "Inject the delay at the suspected point rather than looping (KNOWLEDGE.md section 6b).",
                      "- Done when: the fix branch is pushed (or the entry is rejected), `flaky_scan.py annotate "
                      f"'{(meth + '#' + e['sig']) if '::' in e['key'] else re.escape(eid)}' --status fix-proposed|wontfix --owner <you> --notes ... [--board fix-NN]` is committed, "
                      "and the board record is updated."]
            with open(os.path.join(outdir, fname), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            index.append((fname, e, cls, branches, occ))
    for old in sorted(glob.glob(os.path.join(outdir, "*.md"))):
        if os.path.basename(old) not in written and os.path.basename(old) != "README.md":
            os.remove(old)
    out = ["# Flaky candidates", "",
           "Generated by `flaky_scan.py candidates` (part of `all`): one file per open flaky/unknown entry "
           "(status new, triaged or regressed, or a branch a fix does not reach). Do not edit; use `annotate`."
           + (f" Out-of-scope branches (scan.json): {', '.join(out_of_scope)}." if out_of_scope else ""), "",
           "| file | test | sig | class | status | branches | occ | last seen | board |", "|---|---|---|---|---|---|---|---|---|"]
    for fname, e, cls, branches, occ in sorted(index, key=lambda x: (-len(x[4]), x[0])):
        out.append(f"| [{fname}]({fname}) | `{md(e['key'].split(chr(92))[-1])}` | {e['sig']} | {cls} | {e.get('status', 'new')} "
                   f"| {', '.join(branches)} | {len(occ)} | {max(o['date'] for o in occ)} | {md(e.get('board')) or '—'} |")
    with open(os.path.join(outdir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"candidates: {len(index)} files in {outdir}", file=sys.stderr)


def cmd_all(args):
    cmd_parse(args)
    cmd_classify(args)
    cmd_report(args)
    cmd_candidates(args)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)), help="state directory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="fetch failed runs/jobs/logs with gh")
    c.add_argument("--since", required=True, help="YYYY-MM-DD (inclusive)")
    c.add_argument("--until", required=True, help="YYYY-MM-DD (inclusive)")
    c.add_argument("--workflows", nargs="+", default=WORKFLOWS)
    c.add_argument("--events", nargs="+", default=["push"])
    c.add_argument("--branches", nargs="*", default=[], help="only these branches (default: all)")
    c.add_argument("--refresh", action="store_true", help="re-download logs already cached")
    c.add_argument("--force", action="store_true", help="also re-process runs already in runs.jsonl")

    i = sub.add_parser("ingest-mcp", help="load GitHub MCP JSON dumps into the cache")
    i.add_argument("files", nargs="+", help="files or globs")
    i.add_argument("--run-id", type=int, help="run id for single-job dumps")
    i.add_argument("--force", action="store_true", help="also re-ingest runs already in runs.jsonl")

    sub.add_parser("parse")
    cl = sub.add_parser("classify")
    cl.add_argument("--repo", help="upstream clone for fix checks (git merge-base --is-ancestor)")
    l = sub.add_parser("link", help="attach related issues/PRs via the search API")
    l.add_argument("--classes", nargs="+", default=["flaky", "unknown"])
    l.add_argument("--refresh", action="store_true")
    l.add_argument("--quick", action="store_true", help="search only the method name")
    l.add_argument("--match", help="only entries whose key matches this regex")
    l.add_argument("--sleep", type=float, default=6.0)
    sub.add_parser("report")
    rs = sub.add_parser("resolve", help="find the failing test of UNATTRIBUTED jobs (annotations / full log)")
    rs.add_argument("--log-urls", help="JSON {job_id: logs_url} e.g. from MCP get_job_logs return_content=false")
    rs.add_argument("--jobs", nargs="*", type=int, help="extra job ids to resolve")
    rs.add_argument("--no-gh", action="store_true", help="do not call gh (annotations / logs endpoints)")
    a = sub.add_parser("annotate", help="set manual fields on one backlog entry")
    a.add_argument("match", help="regex selecting exactly one key")
    a.add_argument("--status", choices=STATUSES)
    a.add_argument("--owner")
    a.add_argument("--override", choices=["", "flaky", "real", "unknown"])
    a.add_argument("--notes")
    a.add_argument("--board", help="board record, e.g. fix-12")
    a.add_argument("--fixed-by", help="upstream PR number that fixed it")
    a.add_argument("--merged-at", help="merge date of that PR (ISO 8601)")
    a.add_argument("--fix-commit", action="append", metavar="BRANCH=SHA",
                   help="merge commit of the fix on a branch (repeatable)")
    a.add_argument("--all-sigs", action="store_true", help="apply to every entry the regex matches")
    se = sub.add_parser("seen", help="run already classified? / error already seen? / already fixed?")
    se.add_argument("--run", type=int)
    se.add_argument("--job", type=int)
    se.add_argument("--excerpt", help="first error line; matched by signature")
    se.add_argument("--key", help="regex on the test key")
    bf = sub.add_parser("backfill", help="fixed_by from 'fixed by #NNNN' notes (git log in an upstream clone)")
    bf.add_argument("--repo", required=True)
    bf.add_argument("--remote", default="origin")
    bf.add_argument("--branches", nargs="*", default=[], help="extra branches to search")
    bf.add_argument("--refresh", action="store_true")
    cd = sub.add_parser("candidates", help="write candidates/*.md for open flaky/unknown entries")
    cd.add_argument("--kb", help="path to KNOWLEDGE.md (default: ../KNOWLEDGE.md)")
    al = sub.add_parser("all", help="parse + classify + report + candidates")
    al.add_argument("--repo", help="upstream clone for fix checks")
    al.add_argument("--kb", help="path to KNOWLEDGE.md")

    args = ap.parse_args()
    os.makedirs(args.dir, exist_ok=True)
    if args.cmd == "collect":
        scan = jload(p(args, "scan.json"), {}) or {}
        scan.update({"since": min(filter(None, [scan.get("since"), args.since])),
                     "until": max(filter(None, [scan.get("until"), args.until])),
                     "workflows": sorted(set(scan.get("workflows", [])) | set(args.workflows)),
                     "events": sorted(set(scan.get("events", [])) | set(args.events))})
        jdump(p(args, "scan.json"), scan)
    {"collect": cmd_collect, "ingest-mcp": cmd_ingest_mcp, "parse": cmd_parse, "classify": cmd_classify,
     "link": cmd_link, "report": cmd_report, "annotate": cmd_annotate, "resolve": cmd_resolve, "seen": cmd_seen,
     "backfill": cmd_backfill, "candidates": cmd_candidates, "all": cmd_all}[args.cmd](args)


if __name__ == "__main__":
    main()
