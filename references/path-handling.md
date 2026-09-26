# Path Handling Reference

This skill stores two pieces of state on disk:

1. **SQLite database** (`zepp.db`) — your measurements, raw records, etc.
2. **OAuth token** (`token.json` inside `.secrets/`) — your login session.

Both have historically supported different environment variables, which caused
the "401 with valid-looking token" bug that affected several users on
v4.0.0 → v4.0.1. **v4.0.2 unifies both behind `ZEPP_DATA_DIR`.**

---

## Precedence chain (v4.0.2+)

### SQLite database path

Resolved by every CLI script's `--db` argument default. Order of precedence:

1. **`--db <path>`** on the command line (explicit override).
2. **`ZEPP_DATA_DIR` env var** + `/zepp.db`.
3. **`~/.zepp-data/zepp.db`** (default; on Windows that's
   `%USERPROFILE%\.zepp-data\zepp.db`).

### OAuth token path

Resolved by `scripts/zepp_client.py::_resolve_secrets_path()`. Order:

1. **`ZEPP_SECRETS_PATH` env var** (most specific — for cron jobs that want
   the token in a custom absolute location, independent of the data dir).
2. **`ZEPP_DATA_DIR` env var** + `/.secrets/token.json` (**new in v4.0.2**).
3. **`~/.zepp-data/.secrets/token.json`** (default home dir).
4. Legacy hardcoded path (only as a final hint; the script will error out if
   it doesn't exist).

---

## Why two env vars exist

`ZEPP_SECRETS_PATH` was the original (v1.0 → v4.0.1) way to override the
token location. It is kept for backward compatibility — cron jobs that set
it continue to work.

`ZEPP_DATA_DIR` was added later (M5 / M6) to relocate the whole data tree
(DB + logs + cache) under one root. Until v4.0.2, however, the token was
*not* automatically relocated with it, which is the source of the
**most-reported v4.0.0 bug**:

> *User sets `ZEPP_DATA_DIR=/custom/path` so they can write to a
> non-default directory. `pull_to_sqlite.py` correctly opens
> `/custom/path/zepp.db`, but `ZeppClient()` keeps reading
> `~/.zepp-data/.secrets/token.json` — which on the user's system happens
> to hold the stale token from their very first (failed) login attempt.
> Every sync then returns 401 because the token is for an account that no
> longer exists at this user.*

After v4.0.2, if you set `ZEPP_DATA_DIR=/custom/path`, you must also run
`python3 scripts/zepp_oauth.py login` once so the token is written to
`/custom/path/.secrets/token.json`. From then on, both DB and token live
under the same root, and the 401 loop is broken.

---

## Recommended layout

For most users, the default `~/.zepp-data/` is fine — just leave both
variables unset.

For users on read-only home dirs (corporate laptops, PRoot sandboxes where
the root's home is mounted read-only, etc.), the recommended pattern is:

```bash
export ZEPP_DATA_DIR=/path/to/writable/dir
python3 scripts/zepp_oauth.py login     # writes token under $ZEPP_DATA_DIR
python3 scripts/pull_to_sqlite.py sync  # uses both DB and token under $ZEPP_DATA_DIR
```

For users who need the token in a *different* location from the DB
(uncommon), the pattern is:

```bash
export ZEPP_DATA_DIR=/path/to/db
export ZEPP_SECRETS_PATH=/some/other/path/token.json   # takes precedence
python3 scripts/zepp_oauth.py login --phone ...
```

---

## Migration from v4.0.0 / v4.0.1

If you upgrade an existing install that uses `ZEPP_DATA_DIR` and you were
hitting 401s, **delete the stale token** so the next login writes a fresh
one in the new (correct) location:

```bash
rm $ZEPP_DATA_DIR/.secrets/token.json   # if it exists
rm ~/.zepp-data/.secrets/token.json     # the stale one
python3 scripts/zepp_oauth.py login --phone ...
```

If you don't use `ZEPP_DATA_DIR`, no migration is needed — the default
home-dir path is unchanged.