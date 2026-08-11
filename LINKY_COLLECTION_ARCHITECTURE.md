# Linky collection publication architecture

## Project blueprint

Goal: separate a fast guild total from a publishable streamer snapshot and a settled closed-day ledger. A current-day pagination race must never erase the last complete streamer snapshot or block the guild total.

Non-goals: infer missing streamer income, convert a missing `total_item` to zero, or mark current-day data settled.

Publication contract:

- Guild summary: page one only, every 15 minutes, `PROVISIONAL`. A failed read retains the prior value as `STALE`; no prior value becomes `UNAVAILABLE`.
- Current streamer detail: complete pagination plus exact `total_item` reconciliation. Useful rows from an incomplete scan are accumulated in a protected day cache. Database consumers run only after an exact snapshot exists, so they keep serving the last complete snapshot on failure.
- Closed-day detail: strict complete pagination and exact reconciliation, followed by the existing `settled=true` ledger and closure receipt.

Acceptance: malformed summaries fail closed; partial detail never reaches PostgreSQL; a later pass may complete an earlier partial pass; a decrease resets the accumulated seed; all state artifacts are atomic mode `0600` files; each lane has an independent lock and rollback boundary.

## Read-only audit

- `linke_live_refresh.sh` was scheduled hourly at minute 15 and ran one full bundle for live views, daily ledger, display-time rebuild and projections.
- `linky_fetch.py` and `linky_api_pagination.py` coupled current streamer and room pagination. Database writes were already transactionally gated on a complete bundle, but useful incomplete rows survived only inside one invocation.
- `linke_streamer_daily.settled` already represents closed-day finality. No separate guild-summary table or publication existed.
- `POST /api/guild/export_streamer_stat` returns a temporary CSV URL, but the documented local summary does not define the body. Bounded production probes confirmed the response shape and CSV delivery; the attempted date fields did not select the requested data, so the exporter is not accepted as a snapshot source until its official request contract is obtained.

## Change-package roadmap

1. Collector package: lightweight page-one guild summary, durable fail-closed current-day accumulation, strict closed-day behavior, dedicated tests. Rollback is a release symlink switch; state files are additive and ignored by the old release.
2. Schedule package: install the summary lane at minutes `2,17,32,47`; retain the hourly detail lane and daily closure lane. Rollback removes only the summary cron line.
3. Export adapter (deferred gate): implement only after an official body/schema contract and file consistency test. It must remain behind a feature flag and fall back to the paginated accumulation path.

Explicit exclusions: no secret output, no production database schema change, no fabricated totals, and no backend restart.
