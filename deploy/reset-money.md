# Fresh-start money reset — Pi runbook

`actions.reset_money` clears every money-movement row (transactions, splits,
links, bill_payments, goal_contributions, pending_actions; income_rules
hit_count → 0) while keeping the configured structure — members, bills, goals,
budgets, income rules, pantry, api tokens — untouched. `audit_log` is kept and
the reset writes its own audit row. **CLI-only by design: no route reaches it.**

Run ON THE PI as `altamash`, from `/home/altamash/pifinance`, with the deploy
that carries the verb already applied.

## 1. Stop writers + fresh backup (rule 6)

```bash
sudo systemctl stop pifinance
venv/bin/python -c "import sqlite3,sys; sqlite3.connect('finance.db').execute('VACUUM INTO ?', [sys.argv[1]])" finance.db.bak-pre-reset-$(date +%Y-%m-%d-%H%M%S)
```

## 2. Rehearse on a COPY first (proves the effect before the live run)

```bash
cp finance.db /tmp/reset-rehearsal.db
venv/bin/python -c "
import sqlite3, actions
db = sqlite3.connect('/tmp/reset-rehearsal.db'); db.row_factory = sqlite3.Row
print(actions.reset_money(db, 'ui:altamash', actions.RESET_CONFIRM_PHRASE))
print('members', db.execute('SELECT COUNT(*) c FROM members').fetchone()['c'],
      'bills', db.execute('SELECT COUNT(*) c FROM bills').fetchone()['c'],
      'goals', db.execute('SELECT COUNT(*) c FROM goals').fetchone()['c'],
      'items', db.execute('SELECT COUNT(*) c FROM items').fetchone()['c'])
"
```

Eyeball: the printed dict shows what would clear; members/bills/goals/items
counts match what you expect to KEEP. If anything looks wrong, stop here —
nothing live has changed.

## 3. The live run

```bash
venv/bin/python -c "
import sqlite3, actions
db = sqlite3.connect('finance.db'); db.row_factory = sqlite3.Row
print(actions.reset_money(db, 'ui:altamash', actions.RESET_CONFIRM_PHRASE))
"
sudo systemctl start pifinance
sudo systemctl restart ledger-mcp
```

Open the app: balance settled at $0, Spent empty, bills/goals/budgets/pantry
all still there (goal "saved" reads $0 — contributions cleared, nothing stored).

## 4. Fresh bank connections

```bash
mv simplefin_access.url simplefin_access.url.old   # retire the old claim
venv/bin/python simplefin_sync.py --claim '<NEW_SETUP_TOKEN>'
venv/bin/python simplefin_sync.py                  # first fresh pull
```

Repeat the claim step per bank as you add them. Expectation: SimpleFIN serves
the recent window only (~lookback days) — history does not replay; the ledger
fills forward from here. The daily 06:30 timer takes over after the first
manual sync.

## Rollback (until you're satisfied)

```bash
sudo systemctl stop pifinance
cp finance.db.bak-pre-reset-<ts> finance.db
sudo systemctl start pifinance
```
