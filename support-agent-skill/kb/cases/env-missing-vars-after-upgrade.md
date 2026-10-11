---
topics:
- deploy
- config
symptoms:
- we changed the setting and nothing happened
- it works on the new machine but not the old one
- this option doesn't seem to exist on their system
- the documentation mentions a setting we don't have
---

# A setting has no effect on a machine that was installed a while ago

Usually nothing dramatic — a setting that the docs describe, and that works on a machine
installed last month, simply does nothing on a machine installed last year. Or a feature
behaves as though it were never configured, because from its point of view it wasn't.

This one is worth suspecting **whenever two machines behave differently and their software
versions match**.

## Evidence

- `ERROR: machine not initialized — run: make init`

## Root cause

`scripts/init.sh:20` is:

```sh
[ -f .env ] || cp .env.example .env
```

The `.env` file is created **once**, on first install, and is never touched again — not by
`make pull`, not by `make update`, not by re-running `make init`. `.env` is gitignored, so
git never reconciles it either.

That is deliberate and correct: `.env` holds the machine's own settings and an upgrade must
not stamp on them. But the consequence is that **every variable added to `.env.example` after
a machine was provisioned is permanently absent from that machine**, and nothing ever says so.
The code reads the variable, finds nothing, and uses its default.

This is why a new variable is a genuinely expensive thing to add to this system, and why
anything added has to be designed so that *missing means the safe default*.

## Checks (read-only)

Compare what the machine has against what the current release expects:

```
cd <the gotcha directory>
diff <(grep -oP '^[A-Z_]+(?==)' .env.example | sort) \
     <(grep -oP '^[A-Z_]+(?==)' .env         | sort)
```

Anything in the left column that is missing from the right is a setting this machine has
never had. `check: env_completeness` does exactly this and is part of the standard triage
bundle.

## Fix

Append only what is missing — never overwrite the file, because the customer's real settings
live in it:

```
cd <the gotcha directory>
cp .env .env.backup.$(date +%F)
comm -23 <(grep -oP '^[A-Z_]+(?==)' .env.example | sort) \
         <(grep -oP '^[A-Z_]+(?==)' .env         | sort) \
  | while read -r k; do grep "^$k=" .env.example >> .env; done
```

Then read the result before restarting anything — the appended lines carry the *example*
values, and some of them will be wrong for this site:

```
tail -20 .env
```

Pay particular attention to anything naming a path, a port, or a coordinate. When it looks
right:

```
make up
```

## Verify

Re-run the triage diff; it should print nothing. Then confirm the setting that started this
whole conversation now actually takes effect.

## If that didn't work

- If `make` refuses with `ERROR: machine not initialized — run: make init`, the `MODE` line
  is missing or empty rather than the whole file. `make init` is safe here: it will not
  overwrite an existing `.env`, it only fixes `MODE`.
- If the variable exists and is still ignored, it may be one that nothing reads.
  `DATA_PROVIDER_MODE` in `.env.example` is the known example — nothing in the codebase reads
  it, and the provider mode actually comes from `data_provider.mode` in the gateway config
  file. Changing it has no effect by design, which is worse than it not existing.
- A machine provisioned before the one-directory-per-system config layout may also be missing
  `configs/default/` entirely. `DEPLOYMENT.md` covers extracting it from the image by hand.
