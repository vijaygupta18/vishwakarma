# Database / Domain Investigation Runbook

## Goal
Investigate application-level issues — ride failures, payment problems, booking anomalies, driver/rider issues, or any entity referenced by UUID/ID. This is the primary runbook for **domain debugging** (not infra alerts).

## Prerequisites
- `database` toolset must be enabled
- **FIRST STEP always:** `learnings_read(database)` — this has the full schema, ID resolution chains, connection routing, and known gotchas

## When This Runbook Applies
- User asks about a specific ride, booking, payment, driver, or rider by ID
- User asks to trace/debug why something happened (ride cancelled, payment failed, driver not matched)
- User asks to check data patterns (bookings with condition X, rides in city Y)
- RDS performance diagnostics (pg_stat_activity, slow queries, locks)

---

## Phase 1 — Understand What You're Looking For

Parse the user's question to determine:
1. **Entity type**: ride, booking, payment, driver, rider, search, quote, fare
2. **Identifier**: UUID, phone number, ride short ID, transaction ID
3. **What happened**: failure, cancellation, wrong fare, missing data, payment issue
4. **Time context**: when did it happen (needed for date filters)

## Phase 2 — Load Schema Knowledge

```
learnings_read(database)
```

This gives you:
- Connection routing (ClickHouse vs PostgreSQL)
- ID resolution chains (any UUID → full ride data)
- Table relationships and foreign keys
- Known missing tables, missing indexes, performance gotchas
- Query templates for common investigations

**DO NOT skip this step.** Without it you'll query wrong tables, wrong connections, or miss the FK chain.

---

## Phase 3 — Resolve the Entity

### If given a UUID
Follow the ID resolution chain from learnings. Try in order:
1. BPP ride (`atlas_driver_offer_bpp.ride`)
2. BPP booking → ride
3. BAP booking → BPP booking → ride
4. BAP ride → BPP ride
5. Search request → quote → booking → ride
6. Driver/rider person table

**Always fetch the full row** — don't just check existence. The column values tell the story.

### If given a phone number
```sql
-- Rider
SELECT id, first_name, created_at FROM atlas_app.person WHERE mobile_number_hash = sha256('<phone>') LIMIT 5
-- Driver
SELECT id, first_name, created_at FROM atlas_driver_offer_bpp.person WHERE mobile_number_hash = sha256('<phone>') LIMIT 5
```

### If given a ride short ID
```sql
SELECT * FROM atlas_driver_offer_bpp.ride WHERE short_id = '<short_id>' LIMIT 1
```

---

## Phase 4 — Trace the Full Chain

Once you have the core entity, fetch ALL related records to build the complete picture:

### For a ride issue, fetch (in order):
1. **BPP ride** — status, trip_start/end, fare, driver_id, booking_id, cancellation info
2. **BPP booking** — status, from/to locations, vehicle_variant, provider_id, quote_id
3. **BAP booking** — rider-side status, payment_method, bpp_ride_booking_id
4. **BAP ride** — rider-side ride status, bpp_ride_id
5. **Payment** — `SELECT * FROM atlas_app.payment_order WHERE order_id = '<bap_booking_id>' LIMIT 5`
6. **Fare details** — `SELECT * FROM atlas_driver_offer_bpp.fare_parameters WHERE id = '<fare_parameters_id>' LIMIT 1`
7. **Driver** — `SELECT id, first_name, active, enabled, mode FROM atlas_driver_offer_bpp.person WHERE id = '<driver_id>' LIMIT 1`
8. **Rider** — `SELECT id, first_name FROM atlas_app.person WHERE id = '<rider_id>' LIMIT 1`

### For a payment issue:
1. Start from payment_order or payment_transaction
2. Trace to booking → ride
3. Check payment status, gateway response, refund status

### For a driver not getting rides:
1. Check driver person record — `active`, `enabled`, `mode`
2. Check driver_information — vehicle details, subscription status
3. Check recent rides — are they completing normally?
4. Check driver_location — is location being updated?

---

## Phase 5 — Analyze What Went Wrong

Once you have all the data, look for:

### Ride cancellation
- `ride.status` = CANCELLED → check `booking_cancellation_reason` table
- Who cancelled? `ride.ride_ended_by` or cancellation reason
- When? Compare `created_at` vs `cancelled_at` timestamps

### Payment failure
- `payment_order.status` = FAILED → check `payment_transaction` for gateway response
- Was it auto-refunded? Check refund records
- Is the gateway returning errors? Check the response body

### Fare mismatch
- Compare `ride.fare` with `fare_parameters` breakdown
- Check if distance was calculated correctly (`ride.chargeableDistance` vs `ride.traveledDistance`)
- Check `fare_breakup` for individual components

### Driver not matched
- Is driver active? (`person.active = true, person.enabled = true`)
- Is driver in correct mode? (`person.mode` should be ONLINE or SILENT)
- Is driver location recent? Check location_mapping or driver location data
- Did driver receive the request? Check search_request on BPP side

---

## Phase 6 — Compare with Normal Cases (if issue is unclear)

If the data looks normal but user reports a problem, compare with a **working example**:

```sql
-- Find a recent successful ride in the same city for comparison
SELECT id, status, fare, created_at FROM atlas_driver_offer_bpp.ride
WHERE status = 'COMPLETED' AND merchant_operating_city_id = '<same_city_id>'
AND created_at >= '<today>' ORDER BY created_at DESC LIMIT 1
```

Then fetch the same chain for the working ride and diff the two.

---

## Phase 7 — Aggregate Queries (for pattern analysis)

If user asks about patterns, trends, or counts:

```sql
-- Rides by status in last hour
SELECT status, count(*) FROM atlas_driver_offer_bpp.ride
WHERE created_at >= '<1h_ago>' GROUP BY status ORDER BY count DESC

-- Cancellation reasons
SELECT cancellation_reason, count(*) FROM atlas_driver_offer_bpp.booking_cancellation_reason
WHERE created_at >= '<1h_ago>' GROUP BY cancellation_reason ORDER BY count DESC LIMIT 10

-- Payment failures by gateway
SELECT status, count(*) FROM atlas_app.payment_order
WHERE created_at >= '<1h_ago>' GROUP BY status ORDER BY count DESC
```

Always add date filters on large tables for performance.

---

## Phase 8 — PostgreSQL Diagnostics (for RDS issues)

Use `bpp_pg` or `bap_pg` connections for system queries:

```sql
-- Active queries
db_query(bpp_pg, "SELECT pid, now()-query_start AS duration, state, wait_event_type, left(query,200) FROM pg_stat_activity WHERE state='active' AND query NOT LIKE '%pg_stat%' ORDER BY duration DESC LIMIT 20")

-- Connection count by app
db_query(bpp_pg, "SELECT application_name, state, count(*) FROM pg_stat_activity GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20")

-- Table with high seq scans (missing index)
db_query(bpp_pg, "SELECT relname, seq_scan, idx_scan, seq_tup_read FROM pg_stat_user_tables WHERE seq_scan > 100 ORDER BY seq_tup_read DESC LIMIT 20")

-- Index usage on a specific table
db_query(bpp_pg, "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = '<table>' ORDER BY indexname")

-- EXPLAIN ANALYZE a slow query
db_query(bpp_pg, "EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) <query>")
```

---

## Report Requirements

### For entity debugging (ride/booking/payment):
1. **What the data shows** — exact status, timestamps, amounts from the DB
2. **The chain** — how entities link (booking → ride → payment → fare)
3. **What went wrong** — specific field/status that indicates the failure
4. **Why** — if determinable from the data (e.g., "driver cancelled because pickup was 15 min away")
5. **What was NOT found** — if data is missing or inconclusive

### For pattern analysis:
1. **Counts with context** — "X rides cancelled in last hour" is meaningless without "vs Y total rides"
2. **Comparison** — always compare with yesterday or a normal period
3. **Drill down** — don't just report counts, show the breakdown (by reason, by city, by status)

### For RDS diagnostics:
1. **Which query** — exact SQL from pg_stat_activity or Performance Insights
2. **Why it's slow** — EXPLAIN ANALYZE results, missing index, table bloat
3. **Impact** — is it blocking other queries? How many connections is it holding?
