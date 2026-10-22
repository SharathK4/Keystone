# Working notes for this repository

Resume from `docs/briefs/RUNBOOK.md` (the brief, verbatim) and
`reports/STATUS.md` (where the work stands), then `git log`.

## Commit rules

- Work only on branch `paypal`. Never push to `origin`; its push URL is disabled.
  No force-push. No history rewrite outside the runbook's two sanctioned cases.
- Plain imperative messages, optional scope prefix: `paypal:`, `structure:`,
  `frontend:`, `docs:`, `chore:`.
- No attribution lines of any kind in messages, tags, branch names, file headers
  or code comments. `scripts/git-hooks/commit-msg` strips them; run
  `scripts/install_hooks.sh` once per clone.
- After every commit run `sh scripts/ci/check_commit_trailers.sh`. It must print
  "clean". Avoid the phrase "generated with" in prose: the check flags it.
- One logical change per commit, tests in the same commit as the code, tree
  green at every commit (`ruff check src tests` plus the targeted tests).
- Never read, print or commit `.env`. PayPal live tests run only when the owner
  says "run live tests".
- The regression anchors must stay byte-identical: snapshot
  `snap-a4c99d5a49a64ffd` (`668c4a7b6d6fb71c`) and model
  `discrete_hazard-9d1ffe99e9` (`d99a0b97feb42050`).
