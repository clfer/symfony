# Symfony flaky-test knowledge base

The source of truth for fixing flaky tests in `symfony/symfony` from the fork `clfer/symfony`.
This branch (`flaky-kb`) is an **orphan branch**: it shares no history with Symfony, so it can never end up in an upstream PR.

To load it in a new session:
```bash
git fetch origin flaky-kb && git worktree add ../flaky-kb origin/flaky-kb
```
Then read this file and `flaky-scan/BACKLOG.md`.

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
- **`gh` access:** in the fork session, `gh api` works for `search/*` only; the Actions API returns 403 for upstream.
- **PHP version:** the container has PHP 8.3. Branches 8.x need PHP ≥ 8.4.1 (`composer install` fails there); 6.4 and 7.4 work.
- **Docker:** the daemon isn't running at start. Start it with `dockerd &`. Pulling images from Docker Hub works (tested with `redis:6.2.8`).
- **Upstream remote:** `git remote add upstream https://github.com/symfony/symfony && git fetch upstream 6.4 7.4 8.0`.

## 3. Symfony conventions for flaky tests
- **Windows group:** `#[Group('transient-on-windows')]` (PHPUnit attribute, 7.4+; `@group` docblock on 6.4) is excluded by `.github/workflows/windows.yml` (all 3 phpunit invocations).
  - Precedent: d6e861ab6 "[HttpClient] Skip the timing-sensitive timeout tests on Windows" (Alexandre Daubois, 2026-09-15), which covered AmpHttpClientTest and AsyncDecoratorTraitTest.
  - Rationale used upstream: *"The Windows workflow runs the HttpClient suite concurrently with the rest of src\Symfony on a 2-core runner, and these tests have margins too tight to survive that contention."*
- **Transient on all platforms:** `#[Group('transient')]`.
- **Time-dependent tests:** prefer `ClockMock` with `#[Group('time-sensitive')]` (PhpUnitBridge) or `MockClock` over real `sleep`/`microtime`.
- **Inline skip:** `markTestSkipped('Too transient on Windows')` under `if ('\\' === \DIRECTORY_SEPARATOR)` is an older pattern (see `CurlHttpClientTest::testTimeoutIsNotAFatalError`). Prefer the group attribute.
- **Test servers:** the HttpClient tests use the `TestHttpServer` PHP built-in server on ports 8057/8067/8077 + offset (`self::port()`). This server is single-threaded, so concurrent requests to the same port are serialized.

## 4. Running tests locally
- **Pitfall — IPv6:** the container has no IPv6. `CurlHttpClientTest::testIPv6Resolve` and `testBindToPortV6` **hang**. Exclude them:
  `./phpunit src/Symfony/Component/HttpClient/Tests/CurlHttpClientTest.php --exclude-group network,tty,benchmark --filter '/^(?!.*(IPv6|V6)).*$/'`
- **Timeouts:** always wrap long runs in `timeout <secs>` and use `--debug` to find the test that hangs.

## 5. PR text template (upstream `.github/PULL_REQUEST_TEMPLATE.md`)
```
| Q             | A
| ------------- | ---
| Branch?       | 7.4
| Bug fix?      | yes
| New feature?  | no
| Deprecations? | no
| Issues        | -
| License       | MIT

<why the test is flaky, CI run links, precedent commit>
```

## 6. Classification patterns learned
| Signature | Cause | Usual fix |
|---|---|---|
| `Idle timeout reached for "http://localhost:80x7/timeout-header"` and `TransportExceptionInterface expected`, Windows only | 50 ms margin (server sleeps 300 ms, client timeout 250 ms) on a 2-core Windows runner | `transient-on-windows` |
| `Remaining direct deprecation notices (N)` in high-deps | a new dependency release | REAL, not flaky |
| `cannot extend final class` in lowest-php | a dependency release | REAL |

## 7. Fix log
| # | Test | Branch | Fix branch | Status | Notes |
|---|---|---|---|---|---|
| 1 | `HttpClient\Tests\CurlHttpClientTest::testTimeoutOnDestruct` | 7.4 | `fix-curl-timeout-on-destruct-windows` | fix-proposed | Adds `transient-on-windows`, like the AmpHttpClientTest override (d6e861ab6). CI runs 43843829136 and 43872663191. Full class run not done locally (IPv6 hang). |

## 8. Prior contributions
- symfony/symfony#67636, #67637 (user's earlier flaky-test fixes).
