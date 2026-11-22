#!/usr/bin/env python3
"""Deterministic flaky-test backlog builder for symfony/symfony GitHub Actions.

Pipeline (each step reads/writes files under --dir, so any agent can resume):

  collect      gh API  -> cache/runs, cache/jobs, cache/logs   (needs `gh` with repo read access)
  ingest-mcp   GitHub MCP JSON dumps -> same cache             (for agents that only have MCP tools)
  parse        cache/logs -> failures.jsonl                    (pure, offline)
  classify     failures.jsonl -> backlog.json                  (pure, offline; keeps manual fields)
  link         backlog.json + `gh api search/issues` -> backlog.json links (cached in cache/search)
  report       backlog.json -> BACKLOG.md                      (pure, offline)
  resolve      UNATTRIBUTED jobs: tail -> annotations -> full log -> none (RESOLVE.md)
  all          parse + classify + report

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
MANUAL_FIELDS = ("status", "owner", "notes", "classification_override")
STATUSES = ("new", "triaged", "reported", "fix-proposed", "fixed", "wontfix", "not-flaky")

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
    and `get_job_logs failed_only=true return_content=true` (as saved to files)."""
    files = sorted({f for pat in args.files for f in glob.glob(pat)})
    nruns = njobs = 0
    for f in files:
        try:
            d = jload(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"skip (not JSON): {f}", file=sys.stderr)
            continue
        if isinstance(d, dict) and "workflow_runs" in d:
            for r in d["workflow_runs"]:
                if r.get("conclusion") == "failure":
                    save_run(args, r)
                    nruns += 1
        elif isinstance(d, dict) and "logs" in d and "run_id" in d:
            for j in d["logs"]:
                c = j.get("logs_content") or ""
                if c:
                    save_job(args, d["run_id"], j["job_id"], j["job_name"], c, j.get("original_length"))
                    njobs += 1
        elif isinstance(d, dict) and "job_id" in d and "logs_content" in d:  # single-job call
            save_job(args, args.run_id or 0, d["job_id"], d.get("job_name", ""), d["logs_content"],
                     d.get("original_length"))
            njobs += 1
    print(f"ingested {nruns} failed runs, {njobs} job logs from {len(files)} files", file=sys.stderr)


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
    out = []
    for meta_path in sorted(glob.glob(p(args, "cache", "jobs", "*.json"))):
        job = jload(meta_path)
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
    out.sort(key=lambda r: (r["key"], r["run_id"], r["job_id"]))
    with open(p(args, "failures.jsonl"), "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"{len(out)} failure records from {len(glob.glob(p(args, 'cache', 'jobs', '*.json')))} jobs", file=sys.stderr)


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


def cmd_classify(args):
    recs = [json.loads(l) for l in open(p(args, "failures.jsonl"), encoding="utf-8") if l.strip()]
    old = jload(p(args, "backlog.json"), {}) or {}
    groups = {}
    for r in recs:
        groups.setdefault(r["key"], []).append(r)
    parsed_jobs = {r["job_id"] for r in recs}  # current parse is authoritative for these jobs
    backlog = {}
    for key in sorted(set(groups) | set(old)):
        prev = old.get(key, {})
        kept = [o for o in prev.get("occurrences", []) if o["job_id"] not in parsed_jobs]
        occ = groups.get(key)
        if not occ:
            # history from older windows / other caches is append-only; drop entries that a
            # re-parse made obsolete unless a human/agent already annotated them
            if kept or any(prev.get(f) not in (None, "", "new") for f in MANUAL_FIELDS):
                backlog[key] = {**prev, "occurrences": kept}
            continue
        # merge occurrences with those already known (older scans / other agents)
        merged = {(o["run_id"], o["job_id"]): o for o in kept}
        for o in occ:
            merged[(o["run_id"], o["job_id"])] = {k: o[k] for k in
                ("run_id", "job_id", "job", "branch", "workflow", "event", "sha", "date", "excerpt")}
        occurrences = [merged[k] for k in sorted(merged)]
        kind = occ[0]["kind"]
        cls, why = classify(key, kind, occurrences)
        entry = {
            "key": key, "kind": kind,
            "component": next((o["component"] for o in occ if o.get("component")), prev.get("component")),
            "classification": cls, "reason": why,
            "occurrences": occurrences,
            "links": prev.get("links", []),
        }
        for f in MANUAL_FIELDS:
            entry[f] = prev.get(f, "new" if f == "status" else "")
        backlog[key] = entry
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
        -len(e["occurrences"]), e["key"]))
    meta = jload(p(args, "scan.json"), {}) or {}
    out = ["# Flaky test backlog — symfony/symfony", "",
           f"Window: {meta.get('since', '?')} → {meta.get('until', '?')} · workflows: "
           f"{', '.join(meta.get('workflows', WORKFLOWS))} · events: {', '.join(meta.get('events', ['push']))}",
           "", "Generated by `flaky_scan.py report` from `backlog.json` (do not edit this file; edit backlog.json manual fields).",
           "", "| # | test / signature | class | why | occ | branches | jobs | first excerpt | runs | links | status |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, e in enumerate(rows, 1):
        occ = e["occurrences"]
        cls = e.get("classification_override") or e["classification"]
        runs = sorted({o["run_id"] for o in occ})
        out.append("| {} | `{}` | **{}** | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            i, md(e["key"]), cls, md(e["reason"]), len(occ),
            ", ".join(sorted({o["branch"] for o in occ})),
            md("; ".join(sorted({o["job"] for o in occ}))[:120]),
            md(occ[0]["excerpt"][:110]),
            " ".join(f"[{r}]({run_url(r)})" for r in runs[:4]) + (" …" if len(runs) > 4 else ""),
            " ".join(f"#{l['number']}{'(PR)' if l['pr'] else ''}" for l in e.get("links", [])[:4]) or "—",
            md(e.get("status", "new")) + (f" — {md(e['notes'])}" if e.get("notes") else ""),
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
    jdump(p(args, "backlog.json"), backlog)
    print(f"{hits[0]}: status={e['status']} override={e['classification_override'] or '-'}", file=sys.stderr)


def cmd_all(args):
    cmd_parse(args)
    cmd_classify(args)
    cmd_report(args)


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

    i = sub.add_parser("ingest-mcp", help="load GitHub MCP JSON dumps into the cache")
    i.add_argument("files", nargs="+", help="files or globs")
    i.add_argument("--run-id", type=int, help="run id for single-job dumps")

    sub.add_parser("parse")
    sub.add_parser("classify")
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
    sub.add_parser("all", help="parse + classify + report")

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
     "link": cmd_link, "report": cmd_report, "annotate": cmd_annotate, "resolve": cmd_resolve, "all": cmd_all}[args.cmd](args)


if __name__ == "__main__":
    main()
