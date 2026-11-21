# flaky-scan — deterministic flaky-test backlog for symfony/symfony CI

`flaky_scan.py` (Python ≥ 3.9, standard library only) turns failed GitHub Actions
jobs into a ranked, annotated backlog. Every step reads and writes files in this
directory, so any agent (or a human) can pick up where the last one stopped.

```
collect / ingest-mcp ──► cache/{runs,jobs,logs}/       raw data (git-ignored, ~13 MB for 2 weeks)
parse                ──► failures.jsonl                one line per (failure, job)
classify             ──► backlog.json                  one entry per test/signature, + manual fields
link                 ──► backlog.json .links           related issues/PRs (search cached in cache/search)
report               ──► BACKLOG.md                    ranked table: flaky › unknown › real, by occurrences
annotate             ──► backlog.json manual fields    status / owner / notes / classification_override
all = parse + classify + report
```

**Determinism contract:** the same `cache/` always gives byte-identical
`failures.jsonl`, `backlog.json` and `BACKLOG.md`. No wall-clock time is used,
everything is sorted, the time window is explicit, and network answers are cached.
`classify` is incremental:
- occurrences from older windows are kept;
- a re-parse is authoritative for the jobs it covers;
- the manual fields (`status`, `owner`, `notes`, `classification_override`) are never overwritten.

## Quick start

### A. Agent with `gh` and full API access (normal laptop / CI)
```bash
cd flaky-scan
python3 flaky_scan.py collect --since 2026-11-20 --until 2026-11-27            # push runs, 3 workflows
python3 flaky_scan.py collect --since 2026-11-20 --until 2026-11-27 --events pull_request --branches 8.2   # optional
python3 flaky_scan.py all
python3 flaky_scan.py link --quick          # ~2 search calls per new flaky/unknown entry, rate-limited
python3 flaky_scan.py report
```
`collect` downloads the **full** job logs, so the `UNATTRIBUTED` rows usually go away.

### B. Agent with only the GitHub MCP tools (e.g. Claude Code cloud sessions)
`gh api` can't reach the log blob storage there, and MCP logs are capped at 5000 lines.
1. `mcp__github__actions_list` `list_workflow_runs`: use `resource_id=<workflow>.yml`, `event=push`, `status=completed`, `perPage=100`.
2. Take each failed run id and call `mcp__github__get_job_logs` with `run_id=<id>`, `failed_only=true`, `return_content=true` and `tail_lines=5000`.
3. The harness saves oversized results to `…/tool-results/*.txt`. Feed those files in as they are:
```bash
python3 flaky_scan.py ingest-mcp '/root/.claude/projects/*/*/tool-results/mcp-github-*.txt'
python3 flaky_scan.py all && python3 flaky_scan.py link --quick && python3 flaky_scan.py report
```
Set the window by hand in `scan.json` (`since`/`until`/`workflows`/`events`), because `collect` isn't used.

## How to continue the backlog (agent protocol)
1. **Extend the window:** run `collect` or `ingest-mcp` for the new dates, then `all`. New occurrences are merged into existing entries.
2. **Work the top of `BACKLOG.md`.** Take rows that are `flaky` or `unknown`, with status `new` or `triaged`, ordered by occurrences.
   - **Investigate:** read the test, then search the excerpt and the method name.
   - **Check whether a fix already exists:** open the PRs listed under `links`. Links are *candidates* found by full-text search and some are noise. When a fix merged, check that no occurrence is dated after the merge.
   - **Record the outcome**, always with `annotate`. Never hand-edit `BACKLOG.md`:
     ```bash
     python3 flaky_scan.py annotate 'testTimeoutOnDestruct' --status reported --notes 'issue #NNNN' --owner my-agent
     python3 flaky_scan.py annotate 'SchemaTool' --override real
     python3 flaky_scan.py report
     ```
     Statuses: `new → triaged → reported → fix-proposed → fixed`, or `wontfix` / `not-flaky`.
3. **`UNATTRIBUTED <job>` rows:** the failing test wasn't in the stored log. Re-fetch that job's full log with `gh run view --job <id> --log` (or path A), then run `all`. The row then splits into real test keys.
4. **Tune the rules, not the output.** The `REAL_PATTERNS` and `FLAKY_PATTERNS` lists sit at the top of the script and the first match wins. After editing them, run `all` and look at the `git diff` of `backlog.json`/`BACKLOG.md` to review the effect.
5. **Several agents at once:** split the work by `--match` regex or by component, and give each agent its own `--owner`. Commit only `backlog.json`, `BACKLOG.md`, `scan.json` and `failures.jsonl`. Never commit `cache/`. When there's a conflict, re-run `all`: the merge is order-independent.

## Classification rules (first match wins)
| result | rule |
|---|---|
| unknown | `UNATTRIBUTED` (the failing test isn't visible in the log) |
| real | PHPUnit deprecation entry; `cannot extend final class`; `is deprecated`; class not found / undefined method |
| real | the same key fails in ≥ 3 jobs of one commit (breaks across PHP versions/OSes) |
| flaky | timeout / idle timeout / `Timeout src/…`; localhost port / connection refused; `Could not set up/count/…`; filesystem timing (`false is true`); ordering/leftover state |
| flaky | fails on ≥ 2 different commits |
| unknown | anything else (single occurrence, no signature) |

## Seed state (2026-11-21)
The tool was seeded from the MCP scan of push runs between 2026-11-07 and 2026-11-19. That covers 29 failed runs and 39 job logs, and gives 19 backlog entries: 10 flaky, 3 real, 6 unknown.
Open leads:
- `CurlHttpClientTest::testTimeoutOnDestruct` (7.4 Windows) has no issue.
- The AmpSql transport tests on Windows have no issue.
- Three AmpHttpClient tests are not covered by #68183/#68184.
