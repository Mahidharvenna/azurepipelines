# Guidewire login reporting

Two ways of answering "who is using which environment", both reading the same
`User Login` events out of Loki.

| | What it does | Output |
|---|---|---|
| [`logins/`](logins/) | Daily collector → SQL Server → Grafana | A dashboard with unbounded history |
| [`reports/`](reports/) | Monthly query → Excel → email | A spreadsheet in your inbox |

They share their Loki selector, `LOGIN_USER_REGEX` and `REPORT_TIMEZONE`
settings, so their numbers reconcile.

## Why there are two

Loki retains about 30 days. Anything querying it directly inherits that ceiling —
the monthly report needs ≥32 days of retention just to see its own reporting
period, and "logins over the last year" is not answerable from it at all.

`logins/` fixes that by decoupling collection from presentation:

```
                  runs daily, well inside retention
                              │
   Loki  ───────────────────► collect_logins.py ──────► SQL Server
  (~30d rolling)                                     (permanent)
                                                            │
                                                            ▼
                                                        Grafana
                                                  (unbounded history)
```

Loki only has to remember the last few days. Everything older lives in tables
nobody purges, so history grows indefinitely.

## Start here

- **Deploying the dashboard** → [`logins/DEPLOY.md`](logins/DEPLOY.md) — blocking
  checks, then a four-run test ladder.
- **How the collector works** → [`logins/README.md`](logins/README.md) — tables,
  configuration, and the safeguards that protect the stored history.
- **The emailed report** → [`reports/README.md`](reports/README.md).

## A note on the data

`logins/` stores usernames per day, because daily distinct-user counts cannot be
summed into a monthly or quarterly figure. That makes the tables subject to
whatever policy covers user identifiers — see the access notes in
[`logins/sql/grants.sql`](logins/sql/grants.sql).

Once a day ages out of Loki, these tables are the only copy of it. Read the two
safeguards in `logins/README.md` before changing how the collector writes.
