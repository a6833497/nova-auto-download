# Linky collection publication architecture

## Project blueprint

Goal: separate a fast guild total from a publishable streamer snapshot and a settled closed-day ledger. A current-day pagination race must never erase the last complete streamer snapshot or block the guild total.

Non-goals: infer missing streamer income, convert a missing `total_item` to zero, or mark current-day data settled.

Publication contract:

- Guild summary: page one only, every 15 minutes, `PROVISIONAL`. A failed read retains the prior value as `STALE`; no prior value becomes `UNAVAILABLE`.
- Current streamer detail: complete pagination plus exact `total_item` reconciliation. A failed amount reconciliation gets one endpoint-local pass with a different page boundary (`5000` then `4096` by default), not another full bundle scan. Useful rows from an incomplete scan are accumulated in a protected day cache. Database consumers run only after an exact snapshot exists, so they keep serving the last complete snapshot on failure.
- Closed-day detail: strict complete pagination and exact reconciliation, followed by the existing `settled=true` ledger and closure receipt.

Acceptance: malformed summaries fail closed; partial detail never reaches PostgreSQL; a later pass may complete an earlier partial pass; a decrease resets the accumulated seed; all state artifacts are atomic mode `0600` files; each lane has an independent lock and rollback boundary. The 15-minute summary lane stops at 720 seconds and retains stale values, while detail work is bounded to 240 seconds per endpoint, 480 seconds per guild and 2,700 seconds per batch so it cannot consume the next hourly window.

## Read-only audit

- `linke_live_refresh.sh` was scheduled hourly at minute 15 and ran one full bundle for live views, daily ledger, display-time rebuild and projections.
- `linky_fetch.py` and `linky_api_pagination.py` coupled current streamer and room pagination. Database writes were already transactionally gated on a complete bundle, but useful incomplete rows survived only inside one invocation.
- `linke_streamer_daily.settled` already represents closed-day finality. No separate guild-summary table or publication existed.
- The current official UI sends `POST /api/guild/export_streamer_stat` with numeric `begin`, `end`, `req_type` and nullable `sid`. A bounded settled-day production probe selected the requested date but returned exactly 5,000 detail rows for an API-reported 14,079 rows. Its `Total` row matched the API summary while its detail sum did not, proving that this export is capped rather than a complete snapshot source.
- Recent production hourly runs normally spent about 1,180-1,336 aggregate API seconds for roughly 135-138 requests; one degraded run reached 1,586 seconds and a 439-second endpoint. This rules out a ten-minute detail-batch budget and supports the bounded 45-minute hourly envelope above. The independent summary lane remains the only 15-minute freshness contract.

## Change-package roadmap

1. Collector package: lightweight page-one guild summary, durable fail-closed current-day accumulation, strict closed-day behavior, dedicated tests. Rollback is a release symlink switch; state files are additive and ignored by the old release.
2. Schedule package: install the summary lane at minutes `2,17,32,47`; retain the hourly detail lane and daily closure lane. Rollback removes only the summary cron line.
3. Export adapter: use the official request body only for an explicit read-only probe. Validate the business date, row count, unique SID count, detail sum and `Total` row against the same-day API summary before returning any rows. A capped or inconsistent file is rejected and the production publisher continues using the paginated accumulation path. The adapter is not scheduled and cannot write business data.

Explicit exclusions: no secret output, no production database schema change, no fabricated totals, and no backend restart.
