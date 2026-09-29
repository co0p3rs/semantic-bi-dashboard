# Architecture

The code is the source of truth; this page is the reasoning.

## Problem

Several teams read the same business numbers: the owner on a landing
page, marketing on spend efficiency, finance through an export, an agency
through its own reporting tool. When each of them computes "net revenue"
their own way, the numbers disagree by a few percent, and after that
nobody trusts any of them.

The service exists so there is exactly one definition of each number,
one code path that computes it, and one place that decides who may see it.

## Request path

```text
Dashboard request                    Partner request
  session cookie → resolve()           X-API-Key → sha256 → scope
  CSRF header on mutations             sliding-window rate limit
  resources from role                  resources from key
          │                                     │
          └──────── require_metrics() ──────────┘
                           │
                           ▼
                    Engine.execute(query)
                           │
     key = sha256(canonical query, generations of its sources)
                           │
              hit ◄────────┴────────► miss
                                       │
                          semantic.run(): one SQL per fact
                          merge → derive → null past sync
                          freshness_for(): lag per source
                                       │
                                  cache.put()
```

Authorization is checked **before** the cache. The cache key has no user
in it because the result does not depend on who asked; it depends only on
whether they may ask.

## The semantic layer

**Closed vocabulary.** Callers send metric names, a date range, a grain,
up to two dimensions, and filter values. Every identifier in the SQL comes
from the catalog, every value is a bound parameter, and filter values are
checked against the known set before compilation. There is no field for
SQL, column names, or join hints, so there is nothing to sanitise.

**Facts are aggregated separately.** Orders, refunds, ad spend, and
sessions live at different grains. A single `JOIN ... ON day` repeats each
order once per spend row. Instead, each fact is aggregated to
`(bucket, dimensions)` on its own and the results are merged in Python.
The test for this is simple: revenue queried alone equals revenue queried
next to spend and sessions.

**Derived metrics are computed after aggregation.** `mer` is
`(gross - refunds) / spend` over the bucket, never an average of daily
values. The totals row uses the same function over summed inputs, so a
total is never a sum of ratios. A zero denominator is `null`, not zero or
infinity, and the UI shows "n/a".

**Unsupported splits are errors.** Each fact declares the dimensions it
can answer. A metric supports the intersection across its inputs.
Conversion rate needs sessions; sessions have no product; so conversion
by product is a 400 that says so.

## Data correctness

**Refund ledger vs. snapshot.** Commerce platforms often expose a
`refunded` total on the order, captured when the order was last synced.
Refunds issued weeks later never reach it. The warehouse keeps both on
purpose. Net revenue reads the ledger and attributes each refund to the
original order date, so last month's net keeps getting *more accurate*
as late refunds land, instead of this week absorbing them. One visible
consequence: refund rate for the most recent days is low because those
orders have not had time to be refunded yet. That is true, and the
freshness metadata is the place to say so, not a smoothing formula.

**Per-source freshness.** Every source records how far it has synced.
A query's result is *complete* only if every source it reads has reached
the end of the range. Incomplete results are still returned, with the
lagging source and the number of missing days. At daily grain, values from
a fact past its sync are `null`, so the chart lifts the pen instead of
drawing a collapse on the last day.

## Caching

Each source has a **generation** that a successful sync increments. The
cache key is a hash of the canonical query plus the generations of only
the sources that query reads. After a sync:

* entries that read that source become unreachable immediately;
* entries that do not read it are still valid (a spend sync does not evict
  revenue-only queries);
* nothing scans keys; old entries age out through TTL and an LRU bound.

Freshness metadata is cached with the result. It changes only when
`synced_through` changes, which only happens together with a generation bump.

## Authorization

Grants are on **resources** (`overview`, `marketing`, `traffic`), not on
metric names or prefixes. Each base metric maps to the resources that show
it. A derived metric requires access to *every* input. Otherwise a viewer
who can see net revenue and MER could compute ad spend by division.

The catalog is filtered with the same function, so the UI never offers a
metric that the query endpoint would refuse.

## Authentication

* Passwords: scrypt with a per-user salt. Unknown emails still pay for a
  hash, and every failure returns the same message.
* Lockout after repeated failures, applied even if the next attempt has
  the right password.
* Sessions: an opaque random token in an HttpOnly, SameSite=Lax cookie. The
  server stores only its SHA-256. Sessions end on idle timeout, on an
  absolute limit, on logout (server-side), and when an admin revokes the user.
* CSRF: a per-session token, readable by the page and required in a header
  on every mutation.
* Invites: one-time, hashed, 72-hour expiry. The admin receives the token
  once, to hand over out of band.

## Partner API

`/v1` is a contract with code this team does not control. It has fixed
field names, daily grain, a 92-day window, hashed and scoped keys, and a
per-key rate limit. It goes through the same engine and cache as the
dashboard, and a test asserts the two agree to the cent. Changing a field
means shipping `/v2`.

## Front end

Plain HTML, CSS, and JavaScript, served from the same origin under a
strict Content-Security-Policy (`script-src 'self'`, no inline script).
Data reaches the DOM only through `textContent` and SVG attributes, never
`innerHTML`, so the policy is not undermined by the page itself. Charts
are hand-built SVG, so there is no third-party script to trust.

## Production mapping

| Demo | Production-shaped replacement |
|---|---|
| SQLite, seeded at boot | PostgreSQL read replica; a read-only role that can `SELECT` approved views only |
| In-process `GenerationCache` | Redis; generation counters as keys, results with TTL |
| `sync_state` table updated by `/api/admin/sync` | Sync jobs write `synced_through` and bump the generation in one transaction, then prewarm common ranges |
| In-memory users and sessions | Users table; sessions in Redis with TTL |
| Demo API keys | Keys generated once, shown once, stored as hashes, rotatable |

None of those replacements change the vocabulary, the permission model,
or the rule that an unknown value is `null`.
