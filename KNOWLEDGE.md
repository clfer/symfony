# Symfony flaky-test knowledge base

The source of truth for fixing flaky tests in `symfony/symfony` from the fork `clfer/symfony`.
This branch (`flaky-kb`) is an **orphan branch**: it shares no history with Symfony, so it can never end up in an upstream PR.

To load it in a new session:
```bash
git fetch origin flaky-kb && git worktree add ../flaky-kb origin/flaky-kb
```
Then read this file and `flaky-scan/BACKLOG.md`.

## 0. Sessions and in-progress work (update when it changes)
- **Coordinator:** session `<session>`. It owns fix #2 and keeps this file consistent. Child sessions report to it with `mcp__claude-code-remote__send_message`.
- **Upstream CI scan:** `<session>` (source symfony/symfony, read-only).
- **Fix #2 driver patch:** `<session>` (source clfer/amp-sqlite3-fork, branch `fix-startup-busy-retry`).
- **Review/PR orchestrator:** `<session>`. It gets each ready fix from the coordinator, starts an independent review session and, on a GREEN verdict, a PR session that opens a draft PR (or produces a compare URL). Children report to it.
  - Review of fix #1: `<session>` (GREEN). PR session for fix #1: `<session>` (compare URL; create_pull_request not possible, see CONTRIBUTING-CHECKLIST.md).
- **Other sessions:** one fix per session. When you push `flaky-kb` and the push is rejected, run `git pull --rebase origin flaky-kb` and push again. Never force-push.

### Live status board (single place for fix status)
- Artifact: <board URL> ("Symfony flaky fixes"). One record per fix: `data/flaky/fixes/fix-NN`.
- **Update only your own record**, with the ArtifactData tool: `action: "update"`, `url: <artifact>`, `collection: "data/flaky/fixes"`, `doc_id: "fix-NN"`, `data: {stage, note, fix_branch?, pr_url?, updated_at (ISO UTC), updated_by (your session id)}`.
  - **Version check:** `get` the record first and pass its `version` as `if_version`. On a version conflict, re-read and redo the write.
  - **New fix:** create the next `fix-NN` with `set` and fields `num`, `test`, `base`, `session`.
- **Stages:** `investigating`, `on-hold`, `fix-proposed`, `in-review`, `changes-needed`, `green`, `draft-pr`, `ready` (the user only), `merged`, `rejected`.
- **Fallback:** if your session has no ArtifactData tool, put the same fields in your `send_message` to the coordinator, which writes them for you.
- **Status vs. knowledge:** status lives on the board only. This file keeps durable knowledge, and the fix log here is updated when a fix reaches a final state (draft-pr, rejected, merged).

## 1. Workflow (one fix at a time)
1. **Pick** the top `flaky` row in `flaky-scan/BACKLOG.md` with status `new` or `triaged`.
2. **Check for duplicates.** Run both searches, because the search API requires `is:issue` or `is:pull-request`:
   `gh api -X GET search/issues -f q='repo:symfony/symfony is:pull-request <testName>'`
   and the same with `is:issue`. Also run `git log upstream/<branch> -S'<testName>'` to find a past fix.
3. **Pick the base branch**: the lowest maintained branch where the test exists *and* the flake was seen (`git show upstream/6.4:<path>`).
4. **Create a worktree per fix**: `git worktree add ../sfXY upstream/X.Y`, run `composer install`, then `git checkout -b fix-<slug> upstream/X.Y`.
5. **Fix and verify**:
   - Run `./phpunit <TestFile> --filter <method>`.
   - Check the group is honoured: run again with `--exclude-group <group>`; the result should be "No tests executed".
   - Run the class with the exclusions listed in section 4.
6. **Push** the branch `fix-<slug>` to `origin` (clfer/symfony) and draft the PR text (section 5). The user opens the upstream PR.
7. **Update** this file (fix log, new patterns) and record the outcome with `python3 flaky-scan/flaky_scan.py annotate '<key>' --status fix-proposed --notes '...'`, then run `report`. Commit and push `flaky-kb`.

## 2. Environment facts (Claude Code cloud sessions)
- **Same-name limit:** a session can't attach both `clfer/symfony` and `symfony/symfony`, because both check out to `<cwd>/symfony`.
  - Use a fork session to fix tests and push.
  - Use a child session started with `source_url=https://github.com/symfony/symfony` to read Actions (GitHub MCP `actions_list` and `get_job_logs`). It can't push.
- **Logs:**
  - **Tail cap:** MCP `get_job_logs` returns at most the last 5000 lines.
  - **Full logs:** downloaded from `productionresultssa<N>.blob.core.windows.net`. The environment must allow `*.blob.core.windows.net` (done 2026-11-21).
- **Signed URLs:** the scan session's safety check refused to fetch the signed log URLs ("Undermining Oversight", twice, 2026-11-21). Running `resolve` with `--log-urls` needs the user's explicit approval in that session. The URLs expire about 5 minutes after `get_job_logs return_content=false` issues them.
- **`gh` access:** in the fork session, `gh api` works for `search/*` only; the Actions API returns 403 for upstream.
- **PHP version:** the container has PHP 8.3. Branches 8.x need PHP ≥ 8.4.1 (`composer install` fails there); 6.4 and 7.4 work.
- **Blocked hosts: ask, don't work around.** When the network proxy refuses a host (403 `agent proxy: no rule…`), stop and ask the user to allow that exact host. Don't try alternative routes. User rule, 2026-11-21. Hosts allowed so far: `*.blob.core.windows.net` (full Actions logs), `deb.debian.org` (apt inside Docker images). Still blocked: `ppa.launchpadcontent.net` (ondrej PHP PPA).
- **PHP 8.4 for 8.x branches**, via Docker:
  1. Build the image: `docker build --network host --build-arg HTTPS_PROXY="$HTTPS_PROXY" -t sf-php84 -f php84.Dockerfile .` (copy `/root/.ccr/ca-bundle.crt` to `ccr.crt` next to it first).
  2. Create the worktree: `git worktree add ../sf82 upstream/8.2`, then `cd ../sf82`.
  3. Install dependencies: `WD=$PWD ../flaky-kb/php84.sh php /usr/local/bin/composer install --ignore-platform-req='ext-*'`.
  4. Run tests: `WD=$PWD ../flaky-kb/php84.sh php phpunit src/...`.

  Pitfalls:
  - Mount the main `.git`, because a worktree's `.git` is a pointer file. Without it, the path repos resolve as `dev-main` and Composer fails.
  - Set `COMPOSER_ROOT_VERSION=8.2.x-dev`.
  - Don't run `composer install` on the host with `--ignore-platform-reqs`: it leaves a `composer.lock` and `.phpunit` that break the container.
  - Simulate a slow runner with `DOCKER_OPTS="--cpus=0.5"`.
  - PHPUnit 13 `--repeat` does not repeat; loop in the shell instead.
- **Docker:** the daemon isn't running at start. Start it with `dockerd &`. Pulling images from Docker Hub works (tested with `redis:6.2.8`).
- **Upstream remote:** `git remote add upstream https://github.com/symfony/symfony && git fetch upstream 6.4 7.4 8.0`.

## 2b. Reproducing Windows-only flakes (fork GitHub Actions)
- The fork `clfer/symfony` is public, so Windows runners are free. Actions is enabled.
- Branch `ci-windows-repro` (based on 8.2) has `.github/workflows/windows-repro.yml`. It copies the upstream `x86 / minimal-exts / lowest-php` setup (windows-2022, PHP 8.4.1 x86, minimal extensions) and loops `php phpunit $TEST_PATH --filter $FILTER` in two phases:
  - Phase A runs the tests alone.
  - Phase B runs them with every core busy.
  - Each failure is printed in a `::group::`, and each phase ends with a `PHASE A RESULT` or `PHASE B RESULT` line.
- **Reuse:** `workflow_dispatch` takes inputs `filter`, `path` and `iterations`. To trigger a run, either have the user dispatch it, or push a commit that changes the defaults.
- **Reading results:** use `mcp__github__actions_list list_workflow_runs resource_id=windows-repro.yml` and `get_job_logs`, both on clfer/symfony.
- Upstream push workflows only run on branches named `*.*`, so repro and fix branches don't trigger the full Symfony CI.
- A Windows VM or Windows containers are impossible here (no /dev/kvm, Linux kernel).

## 3. Symfony conventions for flaky tests
- **Windows group:** `#[Group('transient-on-windows')]` (PHPUnit attribute, 7.4+; `@group` docblock on 6.4) is excluded by `.github/workflows/windows.yml` (all 3 phpunit invocations).
  - Precedent: symfony/symfony#66077, commit d6e861ab69a (7.4 only; its PR table said "Bug fix? no"; the short hash only resolves with enough history, e.g. `--shallow-since=2025-06-01`) "[HttpClient] Skip the timing-sensitive timeout tests on Windows" (Alexandre Daubois, 2026-09-15), which covered AmpHttpClientTest and AsyncDecoratorTraitTest.
  - Rationale used upstream: *"The Windows workflow runs the HttpClient suite concurrently with the rest of src\Symfony on a 2-core runner, and these tests have margins too tight to survive that contention."*
- **Transient on all platforms:** `#[Group('transient')]`.
- **Time-dependent tests:** prefer `ClockMock` with `#[Group('time-sensitive')]` (PhpUnitBridge) or `MockClock` over real `sleep`/`microtime`.
- **Inline skip:** `markTestSkipped('Too transient on Windows')` under `if ('\\' === \DIRECTORY_SEPARATOR)` is an older pattern (see `CurlHttpClientTest::testTimeoutIsNotAFatalError`). Prefer the group attribute.
- **Test servers:** the HttpClient tests use the `TestHttpServer` PHP built-in server on ports 8057/8067/8077 + offset (`self::port()`). This server is single-threaded, so concurrent requests to the same port are serialized.

## 4. Running tests locally
- **Pitfall — IPv6:** the container has no IPv6. `CurlHttpClientTest::testIPv6Resolve` and `testBindToPortV6` **hang**. Exclude them:
  `./phpunit src/Symfony/Component/HttpClient/Tests/CurlHttpClientTest.php --exclude-group network,tty,benchmark --filter '/^(?!.*(IPv6|V6)).*$/'`
- **Pitfall — proxy:** the container's `http(s)_proxy`/`no_proxy` variables make `HttpClient` `testProxy*` fail. Unset them, or expect those 2 failures.
- **6.4 vs 7.4 Windows:** 6.4 runs HttpClient sequentially (windows.yml:119-121, 133-135); 7.4 runs it concurrently, which explains why timing flakes there are 7.4+ only.
- **Timeouts:** always wrap long runs in `timeout <secs>` and use `--debug` to find the test that hangs.

## 5. PR rules and text
See `CONTRIBUTING-CHECKLIST.md` (rules with sources and a last-verified date; re-check each one before every PR). Body files and compare URLs are produced by the PR session.

## 6. Classification patterns learned
| Signature | Cause | Usual fix |
|---|---|---|
| `Idle timeout reached for "http://localhost:80x7/timeout-header"` and `TransportExceptionInterface expected`, Windows only | 50 ms margin (server sleeps 300 ms, client timeout 250 ms) on a 2-core Windows runner | `transient-on-windows` |
| `Remaining direct deprecation notices (N)` in high-deps | a new dependency release | REAL, not flaky |
| `cannot extend final class` in lowest-php | a dependency release | REAL |

## 6b. Investigation lessons
- Always get the **full exception chain** ("Caused by") from the CI log before guessing. The outer Symfony message hides the cause.
- Repro scripts for SQLite races are in the session scratchpad; the pattern is 3 background `php` processes on the same fresh file, ×300.

## 7. Fix log
| # | Test | Branch | Fix branch | Status | Notes |
|---|---|---|---|---|---|
| 2 | `Messenger\Bridge\AmpSql\…\AmpSqlTransportTest::testSetupIsIdempotent` and `AmpSqlTransportFactoryTest::testTheTransportCanBeUsedAgainAfterBeingClosed` | 8.2 | - | waiting for driver session | CI causes (PHP 8.4.1, Windows, fabpot/amphp-sqlite3 v1.1.0): (a) `Could not start the SQLite child process: Unable to prepare statement: database is locked` (2 concurrent connections opening a fresh file); (b) `…: Starting the process failed` (Windows process spawn). Plain SQLite reproduces a lock race on Linux: 3 concurrent opens of a fresh file, `PRAGMA journal_mode=WAL` gives SQLITE_BUSY (code 5) without the busy handler, ~1-5% of opens. The driver already retries this in `enableWal()`, and the real driver on PHP 8.4.28 Linux showed 0 failures in 400 opens. The Windows failure is at *prepare* time, which is likely an earlier startup statement that is not retried. Can't reproduce on Linux. | Windows fork repro (run 44952026059, same setup as upstream): **0/100 alone, 0/100 under CPU load**. The tests only fail inside the full parallel suite.
| 1 | `HttpClient\Tests\CurlHttpClientTest::testTimeoutOnDestruct` | 7.4 | `fix-curl-timeout-on-destruct-windows` | green; compare URL handed to user (2026-11-21) | Adds `transient-on-windows`, like the AmpHttpClientTest override (#66077). CI runs 43843829136 and 43872663191. Review: timing contention (50 ms margin), base 7.4 (not seen on 6.4), no duplicates. Full class run: 174 tests, only `testProxy*` fail (proxy variables). |

## 8. Prior contributions
- symfony/symfony#67636, #67637 (user's earlier flaky-test fixes).
