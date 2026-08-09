# Pi go-live runbook — v1.0 first, then migrate to `rework`

The concrete, ordered procedure to take Ledger from an unused Raspberry Pi
to a live app running the current `rework` code on real household data. It
follows CORE-DESIGN's sequence: **deploy pristine v1.0, tag it at the
deployed state, then apply the migrations to the live database under the
balance gate.**

Generic mechanics (apt, `.env`, systemd, Tailscale, SimpleFIN) live in the
repo `README.md`; this runbook references those sections and adds the parts
specific to *this* bring-up — deploying by `git clone` (so the gate can run
on the Pi), the `v1.0` tag, and the gated migration walk via `deploy.sh`.

Steps marked **[you]** touch a secret (SSH key, SECRET_KEY, SimpleFIN token,
account passwords) and are yours to run — I never handle those.

---

## Phase 0 — Dev dress rehearsal — DONE ✅

Rehearsed on the dev machine before any Pi step (2026-07-25):

- A pure v1.0 seed DB (`seed_db.py`, 225 txns / 176 shared / 8 months) —
  the schema shape a used v1.0 Pi database has.
- `migrate.py apply` bootstraps from a `schema_version`-absent DB straight
  through #001→#005 (176 shared → 352 splits, members preserved).
- The real-data gate (`verify_live_migration.py`, v1.0 → `rework`) is
  **GATE PASS**: balance and every monthly total unchanged to the cent; the
  only differences are the expected structural row counts.
- Full suite green (165 tests). The money tripwire was watched to fire on a
  fabricated balance change.

This is the go/no-go gate on the whole idea; it passed.

---

## Phase A — Pi prerequisites

On the Pi (see README §2):

```bash
sudo apt update && sudo apt install -y python3-venv git
```

**[you]** Install Tailscale and sign in (README §6) — this is how both
phones reach the app; nothing is exposed to the open internet:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Note the Pi's tailnet address: `tailscale ip -4` (a `100.x.y.z`).

---

## Phase B — Get the code on the Pi via `git clone`

We clone (not `scp`) so the Pi can check out any ref and run the balance
gate locally — `gate.py` compares two refs via `git worktree`.

**[you]** Add a **read-only deploy key** so the private repo can be cloned:

```bash
ssh-keygen -t ed25519 -C "pi-ledger-deploy" -f ~/.ssh/ledger_deploy -N ""
cat ~/.ssh/ledger_deploy.pub
```

Paste that public key into GitHub → the `evenkeel` repo → Settings → Deploy
keys → Add deploy key (leave "Allow write access" **unchecked**). Then:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-ledger
  HostName github.com
  User git
  IdentityFile ~/.ssh/ledger_deploy
EOF
git clone git@github-ledger:altamashmomin/evenkeel.git /home/pi/pifinance
cd /home/pi/pifinance
git checkout main            # pristine v1.0 to start
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

---

## Phase C — v1.0 live, real accounts

**[you]** Create `.env` (README §3): a `SECRET_KEY` from
`python3 -c "import secrets; print(secrets.token_hex(32))"`, then
`chmod 600 .env`. On `main`, the app creates its own v1.0 schema on first
run — there is **no** `migrate.py` here yet, so just start it:

```bash
venv/bin/python app.py
```

Open `http://<pi-tailnet-ip>:8080`. The one-time setup screen appears —
**[you] create both real accounts** (names, usernames, 8+ char passwords).
Confirm login works, then Ctrl-C.

Install the service so it survives reboots (README §5):

```bash
sudo cp deploy/pifinance.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pifinance
systemctl status pifinance        # active (running)
```

---

## Phase D — Real data, then tag `v1.0`

**[you]** Connect the bank and pull the first real transactions (README §7):

```bash
venv/bin/python simplefin_sync.py --claim "PASTE_SETUP_TOKEN"
venv/bin/python simplefin_sync.py        # first sync — real rows land
```

(Or add a handful of manual entries in the app.) Take an off-Pi backup —
SD cards die:

```bash
sqlite3 finance.db ".backup finance-golden-v1.0.db"
# then copy finance-golden-v1.0.db to your laptop / cloud
```

**Tag the deployed state.** This freezes what "v1.0" means for the gate:

```bash
git tag v1.0 main
git push github-ledger v1.0        # (push is optional; the tag must exist locally on the Pi)
```

Note down, from the running v1.0 app, the **who-owes-whom balance** and each
month's **Spent** — you'll confirm these are identical after migrating.

---

## Phase E — Migrate the live DB to `rework` (gated)

One command does it safely — it backs up, proves on a copy that no money
moves, and only then stops the service, checks out `rework`, applies
migrations `--live`, and restarts:

```bash
cd /home/pi/pifinance
deploy/deploy.sh rework v1.0
```

Expect: `GATE PASS — balance and every monthly total are unchanged to the
cent`, a structural row-count list (users→members, shared→splits, new empty
tables, `schema_version` → 5), then `applied 001…005` and the service coming
back up. If the gate does **not** pass, the script stops before touching the
live DB and keeps the backup — nothing is lost; investigate before retrying.

Then confirm the app shows the **same balance and monthly Spent** you noted
in Phase D. (The gate already proved this numerically; this is the human
cross-check.)

---

## Phase F — Turn on daily sync, finish up

Enable the daily SimpleFIN timer (README §7). On `rework`, sync now also
imports **income** (money in), classified by any rules you set up later:

```bash
sudo cp deploy/pifinance-sync.service deploy/pifinance-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pifinance-sync.timer
systemctl list-timers pifinance-sync.timer
```

Update CLAUDE.md "Current position in the sequence": Pi deployed, `v1.0`
tagged, live at `rework` HEAD; merging increments to `main` can now proceed.

---

## Rollback (any time)

Every `deploy.sh` run leaves a dated `finance.db.bak-<timestamp>`, plus the
off-Pi `finance-golden-v1.0.db` from Phase D. To revert a deploy:

```bash
sudo systemctl stop pifinance
cp finance.db.bak-<timestamp> finance.db
git checkout <previous-ref>          # e.g. v1.0
sudo systemctl start pifinance
```

---

## Future increments (after this go-live)

Once `rework` work merges to `main` one increment at a time, each Pi update
is the same single, gated command (canonical form — `origin/<branch>`, which
always resolves to the freshly-fetched remote tip; pin the gate baseline by
naming the currently-deployed commit as `<old_ref>`):

```bash
cd /home/altamash/pifinance && git fetch origin && deploy/deploy.sh origin/main <deployed-sha>
```

`<old_ref>` defaults to the currently-deployed HEAD; pass it explicitly so the
gate compares against the right baseline. The dry-run gate runs every time; a
migration that would move a cent never reaches the live database. If the target
resolves to the same commit that's already deployed (e.g. a fetch that raced a
just-pushed ref), the deploy stops with a clear message rather than shipping a
no-op under a misleading PASS. On success the local `main` branch is
fast-forwarded to the deployed commit, so a later `git checkout main` can't
revert the tree to stale code.
