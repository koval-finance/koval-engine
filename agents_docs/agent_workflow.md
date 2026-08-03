# Agent workflow

Purpose: the loop an agent follows for any change, and the rules that do not
bend.

## The loop

1. **Frame.** Restate the task in one or two sentences and name what is out
   of scope. This is what stops "improving" adjacent code nobody asked about.
2. **Test first.** Write the failing test and run it to see it fail.
   Calculation or state-transition logic without a test that was observed
   failing is not accepted.
3. **Implement** the minimum that makes the test pass.
4. **Verify.** `./scripts/verify.sh` must exit 0. Its exit code is the entire
   definition of done; no prose judgement substitutes.
5. **Self-review** the working-tree diff: in scope? No stray files? No new
   dependencies? Comments only where the why is non-obvious?
6. **Report and stop.** Summarize what changed, show the verification output,
   suggest a commit message. Do not run any git write command.

## Hard rules

- **Never commit, push, tag, rebase, merge, or rewrite history.** All git
  actions belong to a human. Suggest; do not act.
- **When a guard test fails, fix the cause — never the guard.** The guards
  are listed in [invariants.md](invariants.md).
- **Never install `backtrader`** into this repository's virtualenv.
- **Never add a runtime dependency.** If a task seems to need one, stop and
  say so.
- Stay inside the task. If you notice an unrelated problem, report it instead
  of fixing it in the same change.

## Suggested commit style

`feat:`, `fix:`, `docs:`, `test:`, `chore:` prefixes, imperative mood, one
concern per commit. Contributions are signed off by the human committer
(`git commit -s`) under the DCO — see [CONTRIBUTING.md](../CONTRIBUTING.md).

Update this file when: the verification gate changes, the git policy changes,
or a new hard rule is added.
