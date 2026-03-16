# Database Investigation Runbook

## Goal
Investigate issues involving application data — rides, bookings, payments, drivers, riders, or any entity referenced by a UUID or phone number. Also covers RDS performance issues where direct SQL diagnostics are needed.

## Prerequisites
- The `database` toolset must be enabled with at least one connection configured
- Read the **database learnings** first: `learnings_read(database)` — this contains your deployment's specific schema, table names, ID resolution patterns, query templates, and which connection to use for what

## When to Use This
- User mentions a ride ID, booking ID, or any UUID that looks like application data
- User asks about a specific person (rider/driver) by phone number or ID
- User asks "why did this ride fail", "what happened to this booking", "debug this payment"
- RDS CPU alert where you need to check `pg_stat_activity` for stuck queries
- Any investigation that needs to look at actual database rows

## Investigation Steps

### Step 1: Load Schema Knowledge
```
learnings_read(database)
```
This gives you the full table schema, ID resolution chains, query templates, and connection routing rules. **Do this first before any db_query calls.**

### Step 2: Identify What You're Looking For
- If given a UUID → use the ID resolution chain from learnings to determine which table it belongs to and resolve to the core entity (usually BPP ride)
- If given a phone number → look up person table first, then find their recent rides/bookings
- If investigating an RDS alert → use PostgreSQL diagnostic queries from learnings (`pg_stat_activity`, `pg_stat_user_tables`, etc.)

### Step 3: Query the Database
Use `db_query(connection, query)` with the appropriate connection:
- Application data queries → use the primary data connection (e.g., ClickHouse)
- PostgreSQL system diagnostics → use the PostgreSQL connections
- Tables missing in primary store → use PostgreSQL fallback

Use `db_list_tables(connection, database)` to discover available tables.
Use `db_describe_table(connection, table)` to discover column names and types.

### Step 4: Follow the Chain
Most application data is linked across tables. Follow the foreign key chain:
- ride → booking → search_request (core chain)
- Cross-database links documented in learnings
- Fetch additional tables based on the issue context (payments, cancellations, fare details, etc.)

## Safety Rules
- **SELECT only** — all write queries are blocked by the toolset
- **Always add LIMIT** — never run unlimited queries
- **Respect timeouts** — if a query times out, the table likely has no index on your filter column. Try a different approach or check learnings for known unindexed tables
- **Add date filters** on large tables for performance (e.g., `AND created_at >= '2026-03-01'`)
