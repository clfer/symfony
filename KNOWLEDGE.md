# Symfony flaky-test knowledge base

The source of truth for fixing flaky tests in `symfony/symfony` from the fork `clfer/symfony`.
This branch (`flaky-kb`) is an **orphan branch**: it shares no history with Symfony, so it can never end up in an upstream PR.

To load it in a new session:
```bash
git fetch origin flaky-kb && git worktree add ../flaky-kb origin/flaky-kb
```
Then read this file, `flaky-scan/BACKLOG.md` and the status board (section 0).

## 0. Sessions, status and handovers (private board)
This branch is public, so session IDs, handovers and the user's waiting list are **not** kept here. They live on the private status board, an artifact titled "Flaky-test fixes": find its URL with the Artifact tool (`action: "list"`), or ask the user.
- **Read first:** `data/flaky/sessions/current` (live sessions, who reports to whom, in-progress work), then the latest `data/flaky/sessions/user-handoff-*` and `handover-*` documents, then `data/flaky/fixes`.
- **Write:** ArtifactData on collection `data/flaky/sessions`. Update `current` when a session starts, hands over or ends (`get`, then `update` with `if_version`). A handover is a new document `handover-<YYYY-MM-DD>-<role>` with fields `title`, `text` (markdown), `updated_at`, `updated_by`. Move superseded lines out of `current` instead of piling them up.
- **Never** write session IDs, scheduled-task IDs or the board URL in this repository (files or commit messages). Refer to sessions by role (`fix-03 fixer`, `orchestrator`).
- When you push `flaky-kb` and the push is rejected, run `git pull --rebase origin flaky-kb` and push again. Force-push (`--force-with-lease`) is allowed on this branch and on other non-PR branches (`ci-repro-*`, `investigate-*`) when needed, e.g. a history cleanup (user rule, 2026-11-22); after one, tell live sessions to re-fetch `flaky-kb` and rebase their unpushed commits.

### Lessons from the 2026-11-22 handovers
- Every github.com / api.github.com request goes through the session proxy; for a repo not attached to the session it answers 403 "GitHub access to this repository is not enabled for this session". `add_repo` read access on a public repo attaches nothing: anonymous git fetch works (including `refs/pull/N/head`), the API doesn't. `add_repo` push for amphp/sql-common was refused by the permission classifier; don't route around a refusal from another session.
- request_credential does not cover GitHub; a token goes in the environment's Network secrets, entered by the user.
- Board page: a field holding two values breaks its link (fix_branch); one record per PR instead.
- Text claims: keep a root cause "likely" when only an injected repro exists (user decision for #68320, fix #8, sql-common issue).

### Live status board (single place for fix status)
- Artifact "Flaky-test fixes" (see section 0 for how to find it). One record per fix: `data/flaky/fixes/fix-NN`.
- **One record per pull request** (user rule, 2026-11-22). A fix that needs a PR in another repository gets a second record with the same `num` (e.g. `fix-02-sqlcommon`). Fields: `repo` (target repository, default `symfony/symfony`; the fix branch link uses the `clfer/<name>` fork), `base` (target branch), `summary` (one sentence, at most 20 words: where it stands and who acts next; shown in the table), `note` (full description, collapsed under the summary), `issue_link` (prefilled new-issue URL when an issue must be opened first), `draft_link` (prefilled compare URL). **Update `summary` together with `note` on every write.**
- **Update only your own record**, with the ArtifactData tool: `action: "update"`, `url: <artifact>`, `collection: "data/flaky/fixes"`, `doc_id: "fix-NN"`, `data: {stage, note, fix_branch?, pr_url?, updated_at (ISO UTC), updated_by (your session id)}`.
  - **Version check:** `get` the record first and pass its `version` as `if_version`. On a version conflict, re-read and redo the write.
  - **New fix:** create the next `fix-NN` with `set` and fields `num`, `test`, `base`, `session`.
- **PR links (`draft_link` field, historical name):** symfony/symfony doesn't accept draft PRs, so the user opens a normal PR from the link. sessions can't create upstream PRs (see CONTRIBUTING-CHECKLIST.md). Put the prefilled compare URL in `draft_link`. `pr_url` is only for a real PR. When you set `pr_url`, delete `draft_link` in the same write (`{"__delete__": true}`), or the page keeps showing the "open PR" link.
- **Stages:** `investigating`, `on-hold`, `fix-proposed`, `in-review`, `changes-needed`, `green`, `draft-pr` (link handed over / PR opened), `ready` (the user only; PR ready for review), `merged`, `rejected`.
- **Fallback:** if your session has no ArtifactData tool, put the same fields in your `send_message` to the coordinator, which writes them for you.
- **Status vs. knowledge:** status lives on the board only. This file keeps durable knowledge, and the fix log here is updated when a fix reaches a final state (draft-pr, rejected, merged).

- **PR stages (USER RULE, 2026-11-22):**
  - `created`: the PR exists and is ready for review by Symfony's rules (Symfony refuses draft PRs), but its CI checks are still running.
  - `ready-for-review`: all PR checks are green.
  - `draft-pr`: kept only for repositories that accept drafts.
  - `ready` is retired; use `ready-for-review`.

### Starting sessions
- **USER RULE:** every new session (create_session) starts with `permission_mode: "auto"`, unless the user explicitly asks for another mode.

### Communication budget (user rule, 2026-11-22)
Each `send_message` wakes the receiver for a full turn, which re-reads its whole context: hundreds of thousands of tokens on a long session. A repeated sentence costs almost nothing; an extra wake-up costs a lot.
1. **The board is the only status channel.** Write status to your own board record. Never send a message only to say "board updated" or to repeat a status.
2. **Message only when the receiver must act or decide:** a verdict, a blocker, a request, or a new rule. No acknowledgements, no FYI.
3. **The "waiting on the user" list has one owner: the coordinator.** It keeps it in its own replies (not on the board; `data/flaky/sessions` holds sessions and handovers only). Other sessions mention a pending user item once, when it is new, then never repeat it.
4. **No double relays.** A child reports to its parent only. The parent forwards to the coordinator only what changes its plans. A fact or rule is sent to a given session by exactly one sender.
5. **Short user replies, own scope only** (user rule, restated 2026-11-23). Say only what is new and inside your own scope. Don't end replies with a recap of pending user items owned by others; e.g. only the coordinator lists "PRs waiting for you to open" (each board record's `summary` already says who acts next). For everything else, point to the board.
6. **Long sessions:** past roughly 400k context tokens, prefer handing over to a fresh session. KNOWLEDGE.md and the board hold the state, so pending scheduled tasks and child-session IDs go in the board document `data/flaky/sessions/current` before you hand over.

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
6a. **Commit author on fix branches (user rule, 2026-11-22):** author and committer = `Clément FERRIER <6459155+clfer@users.noreply.github.com>` (set `GIT_AUTHOR_NAME/EMAIL` and `GIT_COMMITTER_NAME/EMAIL`, or `git -c user.name=… -c user.email=…`). Never `Claude`, and never any other real person's name. Symfony keeps commit authors on merge. **Unsigned (user rule, 2026-11-23):** cloud sessions sign with a key GitHub can't match to the user, so the commit shows "Unverified"; commit with `--no-gpg-sign` (or `git -c commit.gpgsign=false`). No badge is the goal. All open PR heads were re-committed unsigned on 2026-11-23 (same trees; fix #5 message now names `DateTimeValueResolverTest::provideInvalidDates`).
6. **Commit trailers on fix branches (user rule, 2026-11-21):** end fix-branch commits with `Co-Authored-By: Claude …` only. **Do not add `Claude-Session:`**, even if the harness attribution reminder asks for it; the user's rule takes precedence. Reviewers can't open that link. `flaky-kb` commits omit it too: this branch is public and the session history lives on the board (section 0). A PR session checks `git log upstream/<base>..<branch> --format=%B | grep Claude-Session`. If it finds one, it asks the user before amending and force-pushing (`--force-with-lease`).
7. **Push** the branch `fix-<slug>` to `origin` (clfer/symfony) and draft the PR text (section 5). The user opens the upstream PR.
8. **Update** this file (fix log, new patterns) and record the outcome with `python3 flaky-scan/flaky_scan.py annotate '<key>' --status fix-proposed --notes '...'`, then run `report`. Commit and push `flaky-kb`.

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
  - **Docker Hub 429:** `docker build` can fail with `429 Too Many Requests` on `php:8.4-cli` (rate limit, not a proxy block). Retry with backoff (30 s, 60 s, ...); it succeeded on the 3rd try (2026-11-21).
  - The first `./phpunit` call installs PHPUnit into `.phpunit/` and takes ~15 min: GitHub dist downloads time out ("Could not authenticate against github.com") and Composer falls back to git clones. Later calls are instant.
  - **HttpClient tests in Docker:** `php84.sh` passes the proxy variables, and an empty `HTTP_PROXY`/`http_proxy` makes every HttpClient test error with `Invalid HTTP proxy: host is missing`. Run tests as `php84.sh env -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy -u NO_PROXY php phpunit …` (keep them for `composer`).
  - Pre-existing local failure on 8.2 in Docker: `XmlUtilsTest::testLoadFile` ("is not readable" expected), because the container runs as root and `chmod 0` doesn't stop root. Ignore it.
- **Docker:** the daemon isn't running at start. Start it with `dockerd &`. Pulling images from Docker Hub works (tested with `redis:6.2.8`).
- **`gh` with the user's token (tested 2026-11-22):** the user saved a GitHub token as a Network secret for `api.github.com`. Run `gh` with a dummy `GH_TOKEN=ghp_0000…`; the proxy injects the real token.
  - Works: `gh api user` → `clfer`. It can't tell whether the token or the proxy's own GitHub credential answered.
  - Blocked: the proxy's per-session repo gate runs before the token. `gh api repos/symfony/symfony/pulls/68320` and `repos/amphp/sql-common/pulls/10` both return HTTP 403 "GitHub access to this repository is not enabled for this session. Use add_repo to request access…". The token does NOT bypass it, so only repos attached with `add_repo` can be reached.
  - `gh pr checks` (GraphQL) is always 403: "GitHub GraphQL is not available from Claude Code sessions; use the REST API". Use `gh api repos/{o}/{r}/commits/{sha}/check-runs` instead.
  - **REST retest (2026-11-22):** `repos/{o}/{r}/commits/{sha}/check-runs`, `/status`, `/issues/{n}` on amphp/sql-common and symfony/symfony → same 403 repo gate. **Working method for reads:** `gh api -X GET search/issues -f q='repo:symfony/symfony is:pr 68320'` returns title, state, author and full `body` of PRs/issues in any public repo. Head SHA: `git ls-remote https://github.com/<o>/<r> refs/pull/<n>/head`. CI checks and writes on non-attached repos: no working method.
  - `docs.github.com` is now allowed.
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
- **Own copy per fix:** branch `ci-repro-<slug>` from `origin/ci-windows-repro`. Add your branch name to `on.push.branches`, or a push won't trigger the run. Push the fix on top as a second commit for the "after" run; both runs go in parallel.
- **Timing:** install ~1 min, Phase A ~5 min for 200 iterations of a 1 s test, Phase B ~25 min (CPU contention slows phpunit start-up a lot). `get_job_logs` returns 404 until the job has finished. The log is one long line: grep the saved file for `PHASE . RESULT`.
- **Before/after an upstream fix in one run:** add a matrix `variant: [current, pre-fix]` and a step `if: matrix.variant == 'pre-fix'` that runs `git apply -R .github/repro/<fix>.patch` (the patch is `git diff <upmerge>^1 <upmerge> -- <files>` on the repro base). The fix's own regression tests failing in every pre-fix iteration proves the revert took effect (fix #4, run 44956288177).
- **Prove the tests ran:** a filter that matches nothing exits 0. Print the `Tests:`/`OK (` line on success too (strip ANSI first: lines start with escape codes, so `^OK` misses).
- **A 0/N result does not clear a race**: the upstream job runs the whole `src\Symfony` suite in parallel, so its timing differs. Back a fix with a deterministic simulation of the mechanism.

## 3. Symfony conventions for flaky tests
- **Windows group:** `#[Group('transient-on-windows')]` (PHPUnit attribute, 7.4+; `@group` docblock on 6.4) is excluded by `.github/workflows/windows.yml` (all 3 phpunit invocations).
  - Precedent: symfony/symfony#66077, commit d6e861ab69a (7.4 only; its PR table said "Bug fix? no"; the short hash only resolves with enough history, e.g. `--shallow-since=2025-06-01`) "[HttpClient] Skip the timing-sensitive timeout tests on Windows" (Alexandre Daubois, 2026-09-15), which covered AmpHttpClientTest and AsyncDecoratorTraitTest.
  - Rationale used upstream: *"The Windows workflow runs the HttpClient suite concurrently with the rest of src\Symfony on a 2-core runner, and these tests have margins too tight to survive that contention."*
- **Transient on all platforms:** `#[Group('transient')]`.
- **Time-dependent tests:** prefer `ClockMock` with `#[Group('time-sensitive')]` (PhpUnitBridge) or `MockClock` over real `sleep`/`microtime`.
- **Inline skip:** `markTestSkipped('Too transient on Windows')` under `if ('\\' === \DIRECTORY_SEPARATOR)` is an older pattern (see `CurlHttpClientTest::testTimeoutIsNotAFatalError`). Prefer the group attribute.
- **Test servers:** the HttpClient tests use the `TestHttpServer` PHP built-in server on ports 8057/8067/8077 + offset (`self::port()`). This server is single-threaded, so concurrent requests to the same port are serialized.

## 4. Running tests locally
- **Pitfall — IPv6:** the container has no IPv6. `CurlHttpClientTest::testIPv6Resolve` and `testBindToPortV6` **hang**, and so does `AmpHttpClientTest` (same inherited tests). Exclude them:
  `./phpunit src/Symfony/Component/HttpClient/Tests/CurlHttpClientTest.php --exclude-group network,tty,benchmark --filter '/^(?!.*(IPv6|V6)).*$/'`
- **Pitfall — proxy:** the container's `http(s)_proxy`/`no_proxy` variables make `HttpClient` `testProxy*` fail. Unset them, or expect those 2 failures.
- **6.4 vs 7.4 Windows:** 6.4 runs HttpClient sequentially (windows.yml:119-121, 133-135); 7.4 runs it concurrently, which explains why timing flakes there are 7.4+ only.
- **Pitfall — `AmpHttpClientTest::testUnixSocket`** errors locally (`Connection to 'docker:80' failed`), also on unmodified 7.4. Ignore it.
- **Pitfall — shared ports:** never run a second HttpClient test process while a loop runs: both use `TestHttpServer` on 8057/8067 and kill each other's server (`Connection to tcp://localhost:8057 refused`). Discard such a loop.
- **Timeouts:** always wrap long runs in `timeout <secs>` and use `--debug` to find the test that hangs.

## 5. PR rules and text
See `CONTRIBUTING-CHECKLIST.md` (rules with sources and a last-verified date; re-check each one before every PR). Body files and compare URLs are produced by the PR session.

## 6. Classification patterns learned
| Signature | Cause | Usual fix |
|---|---|---|
| `Idle timeout reached for "http://localhost:80x7/timeout-header"` and `TransportExceptionInterface expected`, Windows only | 50 ms margin (server sleeps 300 ms, client timeout 250 ms) on a 2-core Windows runner | `transient-on-windows` |
| `Idle timeout reached for "http://localhost:8057/timeout-body"` in `AmpHttpClientTest::testPauseDuringBodyLongerThanTimeout`, Windows only | server sends `<1>`, sleeps 0.5 s, sends `<2>`; client timeout 0.3 s, pause 0.8 s. Idle time counts from pause expiry, so the fiber has 0.3 s after the pause to deliver buffered data/EOF | `transient-on-windows` (same family as #66077) |
| Amp `handlesActivity`/`openHandles` not empty after cancel/close (`two arrays are identical`, `actual size 1 matches expected size 0`), before 2026-11-16 | response fiber appended `[null, $e]` after the canary removed the handle (leak on cancel) | fixed upstream by #68198; check the failing SHA against the upmerge that brought it |
| `Remaining direct deprecation notices (N)` in high-deps | a new dependency release | REAL, not flaky |
| `cannot extend final class` in lowest-php | a dependency release | REAL |
| `Failed asserting that false is true.` on `$cache->isFresh()` after a rebuild (Config, test with real `sleep()`) | test mixes the real `time()` and filesystem mtimes. `ResourceCheckerConfigCache::write()` backdates the meta by `time() - buildTime`, read *after* `dumpFile()` set the mtime. A second boundary in between over-backdates by 1 s | test-only: `#[Group('time-sensitive')]` + ClockMock (product behaviour is the safe direction) |
| `Expectation failed for method name is "handle"` + `… is accepted by specified callback` in `InlineFragmentRendererTest` (any PHP) | `getKernelExpectingRequest()` compares sub-requests with `==` incl. `REQUEST_TIME` (set by `Request::create()` from `time()`). ClockMock doesn't freeze it in component runs (see 6b), so a second boundary between the two `create()` calls fails | test-only: ignore `REQUEST_TIME` like `REQUEST_TIME_FLOAT` (fix #5) |
| `Fatal error: Out of memory (allocated ~20 MB) (tried to allocate >1 GB) … on line N` where line N is a `class … extends …` declaration, or `Cannot instantiate abstract class ` with an empty name, or `X must be of type X, X given`; 7.4 Windows `x86 / all extensions / lowest-php` only, random test, before 2026-11-19 | opcache class-table corruption on PHP 8.2.0 x86 with `opcache.enable_cli=1` (~8% of runs). The size is garbage (fix #6: 0x5D3FC900). Small "allocated" because that job filters each component with `--requires-php-extension` | environmental, fixed in CI by #68270 (c912ac79, 2026-11-19: drops `opcache.enable_cli=1`). Check `git merge-base --is-ancestor c912ac79 <failing sha>`; reject if older |

## 6b. Investigation lessons
- Always get the **full exception chain** ("Caused by") from the CI log before guessing. The outer Symfony message hides the cause.
- Repro scripts for SQLite races are in the session scratchpad; the pattern is 3 background `php` processes on the same fresh file, ×300.
- **Check the clock hypothesis before building on it.** For fix #3 the obvious guess ("NTFS mtimes lag PHP `time()`") was only marginally true. A probe on windows-2022 (`.github/clock-probe.php` on `ci-repro-config`) showed 0/20000 on one runner, and 3/20000 with ≤ 0.2 ms lag on another. The main window is between two operations, not between two clocks: `time()` is read after `dumpFile()` (temp file + rename) has set the mtime. Fix #3 Windows repro: before 0/400, after 0/400 (runs 44952497179, 44953962727). `repro/config-cache-clock-skew.php` simulates it by shifting `time()` in the class namespace.
- **ClockMock is registered per namespace for the whole process.** Once any `time-sensitive` test in `Tests\Resource` has run, `time()` in `Symfony\Component\Config\Resource` is mocked too. A test can then pass alone and fail in the component run (FileResource's same-second hash decision uses `time()`). Always run the whole component, not just the class, after adding ClockMock. Moving the mocked clock *forward* broke it; starting it in the past (`ClockMock::withClockMock(time() - 10)` then `sleep(10)`) keeps it aligned with the real clock.
- **ClockMock can silently miss a namespace in a component run (PHPUnit 9, fix #5).** The listener's `time-sensitive` namespaces in `phpunit.xml.dist` (e.g. `Symfony\Component\HttpFoundation` for HttpKernel) are registered when the listener is built, *after* PHPUnit 9 has run every data provider while building the suite. PHP binds an unqualified `time()` call site to the function it finds on first execution, so a provider that calls `Request::create()` (HttpKernel: the first one to run is `Controller/ArgumentResolver/DateTimeValueResolverTest::provideInvalidDates`; removing `ProfilerListenerTest` and `QueryParameterValueResolverTest` still leaves the failure, per the fix #5 review) binds it to the global `\time()` for the whole process. `HttpFoundation\time()` called directly is still mocked, which hides the problem. The class or directory alone passes, the component fails. Probe: compare `Request::create()->server->get('REQUEST_TIME')` with `\Symfony\Component\HttpFoundation\time()` and `\time()` inside the test, after forcing a real second boundary with fully qualified `\usleep()` (unqualified `usleep` is mocked). The same gap can affect other HttpKernel tests that rely on a frozen `REQUEST_TIME` (e.g. SessionListenerTest, cf. #68244). Review of fix #5: no other HttpKernel test compares `Request::create()`'s REQUEST_TIME, but `Fragment/FragmentHandlerTest::testRender` (not time-sensitive) is a latent sibling: it compares two Request::create() calls ignoring only REQUEST_TIME_FLOAT. 7.4+ (PHPUnit 12, SymfonyExtension `clock-mock-namespaces`) registers the mock early, so the gap is 6.4-only.
- **A failed `$this->callback()` matcher prints only the actual object**, not a diff. To find the differing property, reason about which fields are nondeterministic, then reproduce.
- **Date the failure against the upmerge, not the PR merge.** An upstream fix reaches 7.4/8.x hours later through `Merge branch 'X' into 'Y'`. Fix #4: #68198 merged on 6.4/7.4 at 10:51, reached 8.2 at 13:56 (4107c1eb622); the 8.2 failure was on ee61eda818 (13:35), so it predates the fix. Check with `git merge-base --is-ancestor <fix> <failing-sha>`.
- **Push the commit message right the first time, or use a new branch name.** On a branch with an open PR, force-push only with the user's OK. Fix #3's first branch (`fix-config-cache-corrective-build-clock`) had a wrong explanation and a `Claude-Session` trailer, so it was superseded by a new branch instead of amended.

- **Amp version per branch (review of fix #4):** on 7.4 with PHP 8.3, `composer install` resolves amphp/http-client **4.x** (AmpResponseV4), while 8.2 on PHP 8.4 uses 5.x (AmpResponseV5). A Linux 7.4 run therefore doesn't exercise the V5 code that failed upstream on 8.2. Say which Amp major you tested.
- **Deterministic timing repro (reviews of fix #3 and #4):** inject the delay at the suspected point instead of looping for a natural failure. Fix #3: `usleep` to the next second between `dumpFile()` and `time()` in `write()`, which makes the old test fail 5/5. Fix #4: a Revolt timer that blocks the loop around the pause expiry. It fails with the CI message from 0.32 s stalls upward and never at 0.29 s or less, so the threshold is the 0.3 s idle timeout.
- **Upmerge conflicts:** a test override placed next to methods that later branches changed conflicts on merge-up (fix #4: 7.4 → 8.2 in AmpHttpClientTest). Placing it after a stable method avoids that. It isn't a blocker, since maintainers resolve these.

### CI-only failures (not test bugs), from the fix #7 scan on 2026-11-22
- **Windows 7.4 "x86 / all extensions / lowest-php" job:**
  - Symptoms: "Cannot instantiate abstract class " (empty name), "cannot extend final class " (empty name), "X must be of type X, X given", single allocations over 1 GB.
  - Cause: PHP 8.2.0 opcache class-table corruption.
  - Fixed in CI by upstream #68270 (merged 2026-11-19 10:02Z: opcache off in the CLI).
  - Treat failures before that date as FLAKY and already fixed, not as REAL or new.
- **Composer infrastructure failures before phpunit runs:** "is a corrupted zip archive (0 bytes)" or a packagist port 443 timeout. Not a test problem: wontfix. flaky_scan.py classifies these as `INFRA …`.
- **Scan coverage:** only push events. Since 2026-11-20 16:46Z there were no new push runs.
- **A fatal on a `class` line is the engine, not the code.** `Out of memory … on line N` where N is the class declaration means the allocation happened while PHP linked the class (inheritance tables). No Symfony code runs there, so look at the runtime (PHP version, opcache) and at CI-config changes on the branch (`git log upstream/<b> -- .github/workflows/windows.yml`). Fix #6.
- **"allocated N bytes" in an OOM is the process's Zend MM reserve, so it hints at which process died, but check the log.** Fix #6: 20 MB looked like a fresh child (an insulated `KernelBrowser` child is ~6 MB, a full FrameworkBundle suite ~190 MB). The real log showed the FrameworkBundle PHPUnit process itself after 23 tests: the all-extensions job passes `--requires-php-extension …`, so each component runs only a handful of tests and stays small. Trace probe that is still useful: a temporary `file_put_contents((new \Exception)->getTraceAsString())` at the top of the file. Insulated children read `php.ini`, not the parent's `-d` flags.
- **32-bit PHP on Linux:** `docker pull i386/php:8.2.0-cli` works (`PHP_INT_SIZE` 4). On 7.4 with PHP 8.2, install PHPUnit with `SYMFONY_PHPUNIT_VERSION=11.5 ./phpunit install` (12 needs 8.3) and set `composer config platform.php 8.2.0` locally before `composer update`.
- **Search merged CI-config PRs before reproducing an environmental flake.** Fix #6 was already handled upstream by #68270 three days after the failure; the scan session found it, not the fix session.

## 7. Fix log
| # | Test | Branch | Fix branch | Status | Notes |
|---|---|---|---|---|---|
| 6 | FATAL `FrameworkBundle/Routing/AttributeRouteControllerLoader.php` line 24: `Out of memory (allocated 20971520) (tried to allocate 1564461312)` | 7.4 | - | rejected (environmental, already fixed in CI by #68270) | Run 43863071988 (sha ee58092627, 2026-11-16), `x86 / all extensions / lowest-php`, PHP 8.2.0 x86. Line 24 is the `class … extends AttributeClassLoader` declaration, so the allocation is in engine class linking, and the 0x5D3FC900 size is garbage. The fatal killed the FrameworkBundle PHPUnit process after 23 tests, with no test name (CI log session). #68270 (c912ac79, 2026-11-19) removed `opcache.enable_cli=1` from that job for exactly this corruption (~8% of runs, ">1 GB" allocations); ee58092627 doesn't contain it. Linux i386 PHP 8.2.0 without opcache CLI: FrameworkBundle suite 2/2 OK, insulated Functional tests looped without failures. Fork Windows runs 44997931062 (after) and 44997996575 (before, enable_cli=1) were not awaited. |
| 4 | `HttpClient\Tests\AmpHttpClientTest::testPauseDuringBodyLongerThanTimeout` (+ `testBackpressureWhileOtherFibersRun` #2, `testHandleIsRemovedOnException`) | 7.4 | `fix-amp-pause-during-body-transient-windows` (98006dc0a77) | fix-proposed (pause test); other two: no change, likely fixed by #68198 | Pause: timing margin, tagged like #66077. Backpressure (8.2, ee61eda818) and HandleIsRemoved (7.4, 51d218e1cf) both failed before #68198 reached their branch. Repro, all 0 failures: Windows filtered 400 runs (run 44952763217, current + #68198 reverted, A+B), Windows whole class 100 runs (run 44956288177, 161 tests each); Linux 7.4 taskset+3 burners 300 runs; Linux 8.2 Docker `--cpus=0.5` 300 runs. Not reproducible anywhere, so the tag rests on the mechanism and precedent, not on a repro. |
| 2 | `Messenger\Bridge\AmpSql\…\AmpSqlTransportTest::testSetupIsIdempotent` and `AmpSqlTransportFactoryTest::testTheTransportCanBeUsedAgainAfterBeingClosed` | 8.2 | - | waiting for driver session | CI causes (PHP 8.4.1, Windows, fabpot/amphp-sqlite3 v1.1.0): (a) `Could not start the SQLite child process: Unable to prepare statement: database is locked` (2 concurrent connections opening a fresh file); (b) `…: Starting the process failed` (Windows process spawn). Plain SQLite reproduces a lock race on Linux: 3 concurrent opens of a fresh file, `PRAGMA journal_mode=WAL` gives SQLITE_BUSY (code 5) without the busy handler, ~1-5% of opens. The driver already retries this in `enableWal()`, and the real driver on PHP 8.4.28 Linux showed 0 failures in 400 opens. The Windows failure is at *prepare* time, which is likely an earlier startup statement that is not retried. Can't reproduce on Linux. | Windows fork repro (run 44952026059, same setup as upstream): **0/100 alone, 0/100 under CPU load**. The tests only fail inside the full parallel suite.
| 1 | `HttpClient\Tests\CurlHttpClientTest::testTimeoutOnDestruct` | 7.4 | `fix-curl-timeout-on-destruct-windows` | draft PR symfony/symfony#68316 (2026-11-22) | Adds `transient-on-windows`, like the AmpHttpClientTest override (#66077). CI runs 43843829136 and 43872663191. Review: timing contention (50 ms margin), base 7.4 (not seen on 6.4), no duplicates. Full class run: 174 tests, only `testProxy*` fail (proxy variables). |

## 8. Prior contributions
- symfony/symfony#67636, #67637 (user's earlier flaky-test fixes).
