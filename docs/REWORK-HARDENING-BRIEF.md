# Ledger rework hardening — briefing

## Purpose

Ledger is a live household-finance app whose balance is trusted by two
people. The rework goal was not a rewrite for its own sake. It was to make
the existing app safer to extend through a small grammar:

- normalized nouns (`members`, `splits`, typed `links`);
- named, validated, audited write verbs;
- one derivation for every displayed number;
- numbered migrations with a verified rollback path;
- later income and agent features built on those foundations.

The central constraint was continuity: preserve the deployed behavior and
financial history after every increment, proving balance and monthly totals
before anything reaches `main`.

## Where the first implementation fell short

Claude's `rework` branch built a promising runner, synthetic seed, gate, and
migrations `#001–#003`. The migration math itself preserved the tested
balance. The surrounding safety logic, however, had several holes:

1. **Two schema authorities.** `app.py` still executed schema DDL at import.
   Starting new code before migrating an old database created `members` and
   `splits`, after which migration `#002` refused the mixed state. This made
   deployment order capable of poisoning the database and contradicted the
   migration-only invariant.
2. **A gate that did not fully test application behavior.** Balance used each
   version's code, but monthly totals came from SQL duplicated inside the
   gate. A dashboard regression could therefore pass. The convenience command
   also ran old and new code against one database, which is invalid when the
   schema itself changes, and importing the legacy app could mutate that input.
3. **Claims without durable proofs.** Manual demonstrations existed, but no
   automated suite continuously proved rollback, ordering, immutable startup,
   pre/post numerical identity, or that a one-cent error fails the gate.
4. **Target and transition were conflated.** The design says member count is
   data, while setup, sync, split writing, and balance presentation still
   assume two active people. That may be an acceptable transition, but it must
   be named and bounded rather than described as completed N-member support.
5. **Unresolved invariant conflicts.** Float-based API presentation remains
   despite “no floats ever.” The proposed `bill_payment` link does not clearly
   identify a bill because both link endpoints reference transactions. Audit
   insertion also needs to be atomic with the edit it records.

## Hardening argument and changes

The argument is that safety infrastructure must be more deterministic than
the feature code it governs. A gate that can mutate its fixture or bypass the
dashboard's calculation is not yet a merge gate; startup DDL beside migrations
is not yet a migration system.

On `codex/rework-hardening`:

- migration `#001` is the idempotent v1 baseline for both existing databases
  and explicit fresh initialization;
- app and sync startup only open an existing database and verify migration
  history read-only;
- migration history must be a contiguous, description-matching prefix;
- balance and spending live in a Flask-independent derivation module consumed
  by both the app and gate;
- the gate evaluates old code on an untouched pre-migration copy and new code
  on a separate migrated copy;
- twelve synthetic regression tests prove initialization, no-op reapply,
  rollback, ordering, immutable startup failure, numerical identity, exact
  expected diffs, and deliberate failure.

The full synthetic v1-to-`#003` rehearsal now passes with only five declared
structural changes. Balance and every monthly total remain identical.

## The separate branch as a use case

The repository arrangement demonstrates the same principle the product rework
is trying to establish:

```text
main                       preserved deployed baseline
rework                     preserved Claude implementation
codex/rework-hardening     reviewable corrections built from rework
```

`codex/rework-hardening` also has its own Git worktree, so the original folder
stays on clean `main` while the alternative can be run, tested, criticized, or
discarded independently. Nothing is overwritten and no correction is accepted
merely because it was implemented. The branch is therefore a concrete use case
for additive, reversible change: retain the trusted state, create an isolated
candidate, enumerate differences, prove invariants, and merge only after human
review.

## Decision before further work

Do not begin `settle_up` verb extraction yet. First review the three hardening
commits and explicitly decide how the transition handles N-member behavior,
float-free money boundaries, `bill_payment` link meaning, and atomic auditing.
Once those foundations are accepted, verb extraction can resume one governed
write path at a time.
