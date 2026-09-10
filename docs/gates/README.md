# Sign-off records

One file per branch. It says which roles reviewed the branch, what they
decided, and — critically — **which exact commit they decided it about**.

`tools/pushgate.py` reads these. A branch does not reach GitHub without one.

---

## Where the file lives

```
docs/gates/<branch name, slashes replaced by dashes>.yaml
```

| Branch | File |
|---|---|
| `feat/explorer-traits` | `docs/gates/feat-explorer-traits.yaml` |
| `fix/BUG-20260909-041` | `docs/gates/fix-BUG-20260909-041.yaml` |

**These files are gitignored, on purpose.** A record names the exact HEAD sha
it applies to. Committing the record would itself move HEAD, so a *tracked*
record could never satisfy its own binding — it would be stale the instant it
existed. So the record is a working-tree artifact: written on the machine the
reviews happened on, read by the gate, and discarded with the branch. This
`README.md` is tracked; the `.yaml` files are not.

The durable copy of who approved what is the pull request description, which
is what Spencer reads before he merges.

---

## The shape, in full

```yaml
branch: feat/explorer-traits
commit: 9932f9ff37af0c1e5a2b8d3e4f5061728394a5b6
signoffs:
  - role: tech-lead
    verdict: APPROVE
    at: "2026-09-10T09:12:00-05:00"
    note: "Traceable to REQ-D-31 and REQ-F-12. Both validator findings fixed
           with tests that fail against the old code."
  - role: pm
    verdict: APPROVE
    at: "2026-09-10T09:40:00-05:00"
    note: "Asked for: 'load every Argonaut's traits without burning the REST
           budget'. That is what this does. Nothing extra rode along."
  - role: validator
    verdict: APPROVE
    at: "2026-09-10T10:05:00-05:00"
    note: "Reverted each fix and confirmed the new test fails. 3 S2 findings
           raised, 3 fixed, 0 deferred."
```

### Every field

| Field | Rule |
|---|---|
| `branch` | Must equal the branch you are on. |
| `commit` | The **full 40-character** sha the sign-offs apply to. Must equal HEAD exactly. |
| `signoffs` | A list. One entry per role. A role may appear only once. |
| `role` | Must have a charter at `.claude/agents/<role>.md`. A sign-off from a role the repo does not define is not a sign-off. |
| `verdict` | `APPROVE`, `APPROVE-WITH-FIXES`, or `REJECT`. **Only `APPROVE` passes.** |
| `at` | ISO-8601 **with a timezone offset** — `2026-09-10T09:12:00-05:00`, or `...Z`. May not be in the future. |
| `note` | Plain language. What was checked, and what the reviewer is staking the approval on. |

### Which roles are required

`config/base.yaml` → `gates.required_roles`. It currently reads:

```yaml
gates:
  required_roles:
    - tech-lead
    - pm
    - validator
```

Three signatures because they are three different questions:

- **`pm`** — is this what was asked for, all of it, and nothing else?
- **`tech-lead`** — does it cohere with the architecture, and does it trace?
- **`validator`** — does it actually work, and were the defects really fixed?

Extra roles may sign; they are recorded but not required. Adding a required
role is a one-line change to that config block, and the role needs a charter
before the gate will accept it.

---

## The two rules people try to bend

**1. The sha binding is the whole mechanism.**

The `commit:` field is why this file means anything. Without it a sign-off is
a claim that "the branch" was reviewed, and a branch is a moving target — one
more commit after the review and the approval covers code nobody read.

So: **a new commit invalidates every sign-off on the branch.** Not "unless it
was small". Not "unless it was only a comment". Every one. Re-run the reviews
and write a new record. That is annoying exactly in proportion to how often
people commit after being approved, which is the behaviour it is meant to
discourage.

**2. `APPROVE-WITH-FIXES` is not approval.**

It means: this is right in shape and wrong in detail, and the detail has not
been fixed yet. The gate rejects it. The path forward is: apply the fixes,
commit, get the role to sign the new sha. There is no version of this where
the fixes are "trusted to happen later" — this project has a bug class made
entirely of things everyone agreed would be fixed.

---

## Recording a verdict

Only the role itself, or the `push-steward` transcribing on its behalf, writes
an entry — and the steward writes only verdicts that were actually given, with
a pointer to where they were given. Nobody signs for anybody, ever. A fabricated
entry is invisible to every downstream check: the gate would read green, the
push would succeed, and the only thing standing between that and a merge would
be Spencer reading the PR.

---

## Checking a record without pushing anything

```
python3 tools/pushgate.py            # numbered PASS/FAIL report
python3 tools/pushgate.py --json     # the same, for an agent to read
```

Both are read-only and neither can push. `push.command` is the only thing in
this repo that pushes, it runs this gate first, and it requires the Operator
to type `PUSH`.
