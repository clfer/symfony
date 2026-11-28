# Symfony PR checklist (for flaky-test fixes)

Each rule links to its source. **Re-verify before each PR**, and edit this file only when something changed (update the date).
Last verified: 2026-11-27 (orchestrator, fix #10 PR prep).

| Rule | Source |
|---|---|
| Body = upstream template table (Branch, Bug fix?, New feature?, Deprecations?, Issues `Fix #…` or `-`, License MIT), and replace the HTML comment with the explanation. | `.github/PULL_REQUEST_TEMPLATE.md` on the base branch |
| Target the lowest branch in the PR template (since 2026-11-27: 7.4, 8.1, 8.2), **test-only fixes included** (6.4 security-only after v6.4.51; upstream 0c59c9423e "Bye bye 6.4"). When the change also applies to 6.4/5.4, end the body with one line offering a backport (e.g. "I can send a backport to 6.4 if wanted."); no question to the maintainer. #68494 (asked the question, 2026-11-28) was merged into 7.4 without retarget. A fix that only applies to 6.4/5.4 is not proposed. | template on 7.4; #68494; user 2026-11-28 |
| Commit and PR title: `[Component] Capitalized imperative sentence`, no trailing period. | same page, "Make your Change" |
| Change only the files the fix needs. | same page |
| No CHANGELOG/UPGRADE entry for test-only changes (only features, behaviour changes, BC breaks, deprecations). | https://symfony.com/doc/current/contributing/code/conventions.html#writing-a-changelog-entry |
| **No draft PRs on symfony/symfony.** The bot answers a draft with: "we don't allow \"Draft\" pull requests. Could you please click the \"ready for review\" button or close this PR and open a new one when you are done?" So open a PR only once it is ready, as a normal PR. No need to squash (squashed on merge). | bot comment on #68316, reported by the user 2026-11-22; code.html "Reworking your Pull Request" |
| Test-only "skip/transient" tagging: precedent #66077 used "Bug fix? no". | https://github.com/symfony/symfony/pull/66077 |
| AI assistance: no written policy found (code.html, conventions, standards, reviews, code of conduct). Practice: `Co-authored-by: Claude` appears on 16 upstream 7.4 commits (e.g. 0d01ca03a9). `Claude-Session` links to a private page reviewers can't open. | searched 2026-11-21 |
| **User rule:** fix-branch commits keep `Co-Authored-By: Claude …` but must NOT carry `Claude-Session:`. Check before the PR; if one is present, ask the user before amending and force-pushing. | user decision 2026-11-21 |
| **User rule:** commit author/committer = `Clément FERRIER <6459155+clfer@users.noreply.github.com>`. Commits unsigned (`--no-gpg-sign`). Check with `git log upstream/<base>..<branch> --format='%an <%ae> | %cn <%ce> | %G?'` (expect `N`; the cloud key shows "Unverified" on GitHub) before the PR. Force-pushing a PR branch is allowed by Symfony's rework flow; no amphp contribution guide exists. | user decisions 2026-11-22/23 |

Docs moved in 2026: `contributing/code/pull_requests.html`, `patches.html` and `tests.html` redirect to `contributing/code.html`.

## How a PR gets opened from here
- (Before the 2026-11-27 rename to clfer/symfony-fork; may no longer hold.) A session with source clfer/symfony couldn't call `create_pull_request` on symfony/symfony ("not configured"), and `add_repo symfony/symfony` is refused (same-name checkout limit, KNOWLEDGE.md section 2).
- A session with source symfony/symfony reaches `create_pull_request` but GitHub answers **403 "Resource not accessible by integration"**: the Claude GitHub App isn't installed on symfony/symfony (2026-11-21). Don't retry.
- Path used: a prefilled compare URL `https://github.com/symfony/symfony/compare/<base>...clfer:<branch>?expand=1&title=…&body=…`. The user opens it with "Create pull request" (not draft; see above). Store that URL **on the status board** in the fix record's `draft_link` field (column "Open draft PR"), never in a scratchpad. Once the user has opened the PR, set `pr_url`, delete `draft_link`, and set stage `created` (KNOWLEDGE.md, "PR stages").
