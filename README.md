# Guidewire login history

Answers "who is using which environment" from the `User Login` events in
Loki: a daily collector copies them into SQL Server, and a Grafana dashboard
reads SQL Server — [`logins/`](logins/).

## Why

Loki retains about 30 days. Anything querying it directly inherits that
ceiling — "logins over the last year" is not answerable from it at all.

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

## A note on the data

`logins/` stores usernames per day, because daily distinct-user counts cannot be
summed into a monthly or quarterly figure. That makes the tables subject to
whatever policy covers user identifiers — see the access notes in
[`logins/sql/grants.sql`](logins/sql/grants.sql).

Once a day ages out of Loki, these tables are the only copy of it. Read the two
safeguards in `logins/README.md` before changing how the collector writes.
