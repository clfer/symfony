# flaky-scan — deterministic flaky-test backlog for symfony/symfony CI

`flaky_scan.py` (Python ≥ 3.9, standard library only) turns failed GitHub Actions
jobs into a ranked, annotated backlog. Every step reads and writes files in this
directory, so any agent (or a human) can pick up where the last one stopped.

```
collect / ingest-mcp ──► cache/{runs,jobs,logs}/       raw data (git-ignored, ~13 MB for 2 weeks); skips runs in runs.jsonl
parse                ──► failures.jsonl + runs.jsonl   one line per (failure, job) with its error `sig`; one line per processed run
classify [--repo]    ──► backlog.json                  one entry per `key#sig`, + manual fields; --repo checks fixes against an upstream clone
link                 ──► backlog.json .links           related issues/PRs (search cached in cache/search)
report               ──► BACKLOG.md                    ranked table: flaky › unknown › real, by occurrences
candidates           ──► candidates/*.md + README.md   one brief per open flaky/unknown entry (generated; never edit)
annotate             ──► backlog.json manual fields    status / owner / notes / classification_override / fixed_by / board
backfill --repo      ──► backlog.json fixed_by         from "fixed by #NNNN" notes (merge commit found with git log)
seen                 ──► stdout                        run already classified? error already seen? already fixed?
all [--repo] = parse + classify + report + candidates
```

### Files (commit all of them except `cache/`)
| file | content |
|---|---|
| `runs.jsonl` | run ledger: `run_id, workflow, event, branch, sha, date, conclusion, failed_job_ids, parsed_job_ids, unattributed_job_ids`. `collect`/`ingest-mcp` skip a run listed here unless `--force`. |
| `failures.jsonl` | one record per (failure, job), with `sig`. Records of jobs whose log isn't in your `cache/` are kept, so an agent without the cache never loses history. |
| `backlog.json` | entries keyed `key#sig`. Manual fields: `status`, `owner`, `notes`, `classification_override`, `fixed_by {pr, merged_at, commits{branch: sha}}`, `board` (board record, e.g. `fix-12`). Computed: `fix_check {open_branches, regressed_runs}`. |
| `BACKLOG.md` | ranked table (generated). |
| `candidates/` | one `<component>--<method>--<sig>[--open-<branches>].md` per candidate + `README.md` index (generated). |

### Error signature (`sig`)
`sig` = first 8 hex chars of sha1 of the excerpt (the first error line) after normalisation: URLs, absolute paths, hex ids/SHAs, durations, ports, `#N` and every other number are replaced by placeholders. The same test failing with a different message gets a different entry. `UNATTRIBUTED`, `TIMEOUT` and `INFRA` keys use one constant signature (their excerpt is a log tail). Old key-only entries are migrated on the first `classify`: each signature found in their occurrences becomes an entry that inherits the manual fields.

### Fix tracking
- Record a fix: `annotate '<regex>' --fixed-by 68318 --merged-at 2026-11-23T09:07:50Z --fix-commit 7.4=cc3c1c9a3d8` (repeat `--fix-commit` per branch if the fix was merged separately). `backfill --repo <upstream clone>` does it for every entry whose notes say "fixed by #NNNN" / "merged on X.Y by #NNNN", using the first-parent merge commit on the lowest branch.
- `classify`/`all` with `--repo <upstream clone>` (branches fetched as `origin/X.Y`, history back to the fixes) checks every occurrence on a branch at or above a fix branch with `git merge-base --is-ancestor <fix> <occurrence sha>`. An occurrence that contains the fix sets status **`regressed`** (`fix_check.regressed_runs`). Without `--repo` the previous result is kept, so output stays deterministic.
- An occurrence on a branch *below* every fix branch is not covered by the fix (`fix_check.open_branches`): the entry gets its own candidate file `…--open-<branches>.md`, even when its status is `fixed`.

### Lookups (`seen`)
```bash
python3 flaky_scan.py seen --run 45144322785      # in runs.jsonl? which entries did it produce?
python3 flaky_scan.py seen --job 137589730919
python3 flaky_scan.py seen --excerpt 'Failed asserting that 2.3 is less than 1.0.' --key AsyncDecorator   # same signature seen before?
```
Each hit prints `id | class | status [fixed_by=#PR] [open_on=…] | first | last | occurrences`.

**Determinism contract:** the same `cache/` (and, with `--repo`, the same upstream clone) always gives byte-identical
`failures.jsonl`, `runs.jsonl`, `backlog.json`, `BACKLOG.md` and `candidates/`; running `all` twice gives an empty `git diff`. No wall-clock time is used,
everything is sorted, the time window is explicit, and network answers are cached.
`classify` is incremental:
- occurrences from older windows are kept;
- a re-parse is authoritative for the jobs it covers;
- the manual fields (`status`, `owner`, `notes`, `classification_override`, `fixed_by`, `board`) are never overwritten, except that `classify --repo` sets `status` to `regressed` when an occurrence contains the fix.

## Quick start

### A. Agent with `gh` and full API access (normal laptop / CI)
```bash
cd flaky-scan
python3 flaky_scan.py collect --since 2026-11-20 --until 2026-11-27            # push runs, 3 workflows
python3 flaky_scan.py collect --since 2026-11-20 --until 2026-11-27 --events pull_request --branches 8.2   # optional
python3 flaky_scan.py all --repo ../../symfony   # upstream clone with origin/X.Y fetched
python3 flaky_scan.py link --quick          # ~2 search calls per new flaky/unknown entry, rate-limited
python3 flaky_scan.py all
```
`collect` downloads the **full** job logs, so the `UNATTRIBUTED` rows usually go away.

### B. Agent with only the GitHub MCP tools (e.g. Claude Code cloud sessions)
`gh api` can't reach the log blob storage there, and MCP logs are capped at 5000 lines.
1. `mcp__github__actions_list` `list_workflow_runs`: use `resource_id=<workflow>.yml`, `event=push`, `status=completed`, `perPage=100`.
2. Take each failed run id and call `mcp__github__get_job_logs` with `run_id=<id>`, `failed_only=true`, `return_content=true` and `tail_lines=5000`.
3. The harness saves oversized results to `…/tool-results/*.txt`. Feed those files in as they are:
```bash
python3 flaky_scan.py ingest-mcp '/root/.claude/projects/*/*/tool-results/mcp-github-*.txt'
python3 flaky_scan.py all --repo <upstream clone> && python3 flaky_scan.py link --quick && python3 flaky_scan.py all
```
The `ingest-mcp` summary line says how many runs were new and how many were skipped because they are already in `runs.jsonl`.
Set the window by hand in `scan.json` (`since`/`until`/`workflows`/`events`), because `collect` isn't used.

## How to continue the backlog (agent protocol)
1. **Extend the window:** run `collect` or `ingest-mcp` for the new dates, then `all --repo …`. New occurrences are merged into existing entries; runs already in `runs.jsonl` are skipped. Before triaging a failure by hand, ask `seen` (run, job or first error line).
2. **Work from `candidates/README.md`.** Each candidate file has the runs table (run and job links), notes, related PRs, the matching KNOWLEDGE.md section 6 pattern, the fix status and a worker brief. The files are generated: change the backlog with `annotate`, then run `all`.
   - **Investigate:** read the test, then search the excerpt and the method name.
   - **Check whether a fix already exists:** open the PRs listed under `links`. Links are *candidates* found by full-text search and some are noise. When a fix merged, check that no occurrence is dated after the merge.
   - **Record the outcome**, always with `annotate`. Never hand-edit `BACKLOG.md`:
     ```bash
     python3 flaky_scan.py annotate 'testRender#1b60bd89' --status fix-proposed --owner my-agent --board fix-12 --notes 'branch fix-…'
     python3 flaky_scan.py annotate 'SchemaTool' --override real --all-sigs     # every signature of that key
     python3 flaky_scan.py annotate 'testParse#3afb' --fixed-by 68441 --fix-commit 6.4=3fe9efa8326
     python3 flaky_scan.py all
     ```
     Statuses: `new → triaged → reported → fix-proposed → fixed`, `regressed` (set by `classify --repo`), or `wontfix` / `not-flaky`. `annotate` matches the regex against entry ids (`key#sig`) and needs exactly one hit unless `--all-sigs`.
3. **`UNATTRIBUTED <job>` rows:** run `resolve`. It tries the stored tail, then check-run annotations, then the full log (signed URL from MCP `get_job_logs return_content=false`, passed as `--log-urls urls.json`, or `gh api …/logs`), then none. Query strings are scrubbed; never commit `urls.json`. Then run `all`. Fallback: the failing test wasn't in the stored log. Re-fetch that job's full log with `gh run view --job <id> --log` (or path A), then run `all`. The row then splits into real test keys.
4. **Tune the rules, not the output.** The `REAL_PATTERNS` and `FLAKY_PATTERNS` lists sit at the top of the script and the first match wins. After editing them, run `all` and look at the `git diff` of `backlog.json`/`BACKLOG.md` to review the effect.
5. **Several agents at once:** split the work by `--match` regex or by component, and give each agent its own `--owner`. Commit `backlog.json`, `BACKLOG.md`, `scan.json`, `failures.jsonl`, `runs.jsonl` and `candidates/`. Never commit `cache/`. When there's a conflict, re-run `all`: the merge is order-independent.

## Classification rules (first match wins)
| result | rule |
|---|---|
| unknown | `UNATTRIBUTED` (the failing test isn't visible in the log) |
| real | PHPUnit deprecation entry; `cannot extend final class`; `is deprecated`; class not found / undefined method |
| real | the same key fails in ≥ 3 jobs of one commit (breaks across PHP versions/OSes) |
| flaky | timeout / idle timeout / `Timeout src/…`; localhost port / connection refused; `Could not set up/count/…`; filesystem timing (`false is true`); ordering/leftover state |
| flaky | fails on ≥ 2 different commits |
| unknown | anything else (single occurrence, no signature) |

## Scan 2026-11-20..27
Push runs of the 3 workflows on 5.4, 6.4, 7.4, 8.1, 8.2, 8.3: 524 runs, 104 failed, 282 failed-job logs (MCP 5000-line tails); plus 11 failed Windows pull_request runs sampled (all UrlSanitizer). Summary: `SCAN-2026-11-27.md`. Ledger, signatures, fix tracking and `candidates/` were added with this scan; `fixed_by` was back-filled from the notes.

## Seed state (2026-11-21)
The tool was seeded from the MCP scan of push runs between 2026-11-07 and 2026-11-19. That covers 29 failed runs and 39 job logs, and gives 19 backlog entries: 10 flaky, 3 real, 6 unknown.
Open leads:
- `CurlHttpClientTest::testTimeoutOnDestruct` (7.4 Windows) has no issue.
- The AmpSql transport tests on Windows have no issue.
- Three AmpHttpClient tests are not covered by #68183/#68184.

## Starting a fixer from a candidate
Only for candidates with ≥ 2 occurrences, or 1 occurrence plus a known KNOWLEDGE.md section 6 pattern or sibling fix. The fixer follows the "Fixer protocol" in KNOWLEDGE.md section 1 and records results with `annotate`; it never edits the generated candidate files.
