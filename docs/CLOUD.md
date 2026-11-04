# Cloud mode: INV-POS as the inventory engine behind the CFC website

This is the same INV-POS code as the restaurant LAN install, configured differently:

| | LAN install | Cloud mode |
|---|---|---|
| Runs on | one PC at the restaurant | Render (web service) |
| Database | SQLite file `data/cfc.db` | Postgres on Supabase, schema `inventory` |
| Till / checkout | on | **off** (`POS_MODULE=off`); MICROS keeps selling |
| Logins | till PIN | till PIN **or** the website's Supabase login |
| Address | `http://<server-ip>:8000` | `https://api.cityfoodcourt.co.ke` |

Everything else (stock movements, purchasing, suppliers, day open/close and counts, reports,
roles, audit log, imports) is identical, and the same 63 tests pass on both databases.

```
cityfoodcourt.co.ke/dashboard  (Vercel, Next.js, Supabase login)
        │  fetch("https://api.cityfoodcourt.co.ke/api/...", { headers: { Authorization: "Bearer <supabase access token>" } })
        ▼
api.cityfoodcourt.co.ke        (Render: this repo, POS_MODULE=off)
        │
        ▼
Supabase Postgres, schema "inventory"   (same Supabase project as the website, separate schema)
```

Sales from MICROS are not linked yet. That is phase 4 (a small bridge on CFC's MICROS server),
after the meeting with the MICROS people. Until then stock is kept by deliveries, waste,
adjustments and the daily count, and sales can be brought in with the stock workbook import.

---

## 1. Supabase (database + logins)

INV-POS uses the **same Supabase project as the website**: the website's logins are issued
by that project, so INV-POS must trust it anyway. The data goes in its own schema,
`inventory`, owned by its own database user, `invpos`. That user can't see the website's
tables, and the website's public key can't see INV-POS's.

**Run once, in Supabase → SQL Editor** (pick a long random password and keep it):

```sql
do $$ begin
  if not exists (select from pg_roles where rolname = 'invpos') then
    create role invpos with login password 'CHANGE-ME-long-random-password';
  end if;
end $$;
grant invpos to current_user;   -- lets Supabase's "postgres" user hand the schema to invpos
create schema if not exists inventory authorization invpos;
create extension if not exists citext with schema extensions;
grant usage on schema extensions to invpos;
revoke all on schema inventory from anon, authenticated;
```

Safe to run more than once. (Without the `grant` line Supabase answers
`must be able to SET ROLE "invpos"`: its `postgres` user isn't a superuser.)
To change the password later: `alter role invpos with password '...';`

* **Don't expose it.** Leave `inventory` *off* the list under *Project Settings → Data API →
  Exposed schemas*. Then the website's anon/authenticated keys can never reach stock, costs or staff.
* **Connection string:** Supabase → **Connect** → *Session pooler*. Copy the URI and swap in
  the new user: `postgresql://invpos.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`.
  Use the session pooler (port 5432), not the transaction pooler (6543). INV-POS sets the time
  zone and schema once per connection. The direct connection is IPv6-only on the free plan,
  which Render can't reach. Note the pooler host's region (e.g. `eu-central-1`): Render should run in the same area.
* **Free plan pausing:** a free project pauses after about a week with no activity. Daily use keeps it awake.
* **Logins:** in *Authentication → Sign In / Providers* keep **Confirm email** on. If the
  website dashboard is for staff only, turn off *Allow new users to sign up* and invite staff
  instead. INV-POS only trusts the e-mail in a confirmed, signed-in Supabase session.
* **Signing keys:** *Project Settings → JWT Keys*. If it shows the new signing keys, nothing
  more is needed. If it still says *Legacy JWT secret*, copy that secret for `SUPABASE_JWT_SECRET`.

## 2. Render (the API)

1. Commit and push this repo (as E-Gilbert).
2. Render → **New → Blueprint** → pick `E-Gilbert/inv-pos`. It reads `render.yaml`.
3. Fill in the values Render asks for:

| Variable | Value |
|---|---|
| `DATABASE_URL` | the Supabase session-pooler URI from step 1 |
| `INVPOS_SECRET` | 64 hex characters. Generate once: `python -c "import secrets; print(secrets.token_hex(32))"`. **Keep a copy offline.** If it changes, every PIN stops working (website logins still work). |
| `SUPABASE_URL` | `https://<project-ref>.supabase.co` |

   Already set by `render.yaml`: `POS_MODULE=off`, `TZ=Africa/Nairobi`, `INVPOS_PG_SCHEMA=inventory`,
   `CORS_ORIGINS=https://cityfoodcourt.co.ke,https://www.cityfoodcourt.co.ke`, `INVPOS_SERVER_ID`.
   Only set `SUPABASE_JWT_SECRET` if the Supabase project still signs with the legacy shared secret
   (*Project Settings → JWT Keys* says "Legacy JWT secret"); projects on the new signing keys need nothing.
4. Pick the Render region **closest to the Supabase project's region** (each query crosses that link).
   CFC's Supabase is in `us-east-1`, so `render.yaml` uses `virginia`.
5. Deploy. Open `https://<service>.onrender.com/api/health`: you should see `"pos": false`.

**Free plan behaviour:** the service sleeps after about 15 minutes with no requests; the first
request after that takes about 30–60 seconds. Fine for the test period. For live use move to a
paid instance (no sleep) and charge it to CFC.

`autoDeploy` is off: deploy deliberately from the Render dashboard after you've tested a commit.

## 3. Domain: api.cityfoodcourt.co.ke

1. Render → the service → **Settings → Custom domains → Add** `api.cityfoodcourt.co.ke`.
2. At the DNS host for cityfoodcourt.co.ke add the record Render shows (a `CNAME` from `api` to
   `<service>.onrender.com`).
3. Wait for Render to show the certificate as issued. HTTPS is automatic and free.

The website itself is not touched by this.

## 4. First login and linking website accounts

1. Open `https://api.cityfoodcourt.co.ke` and log in with PIN **1234**. You must choose a new admin PIN.
2. Load CFC's menu: run once from your PC with the cloud settings, e.g. (PowerShell)
   ```
   pip install -r requirements-cloud.txt
   $env:DATABASE_URL="<session pooler URI>"; $env:INVPOS_SECRET="<same secret>"; $env:TZ="Africa/Nairobi"
   python scripts\load_menu.py menus\cfc_menu.csv --hide-missing --cfc
   ```
   (or Setup → Import / export in the browser).
3. **Setup → Staff:** for each person who will use the website dashboard, fill in
   *Website login e-mail* with the address they sign in to the website with. Their role here
   (stock controller, manager, admin) decides what they can do; the website can't grant more.
   An address can belong to one staff member only. Disabling the staff member cuts website access too.

## 5. Calling the API from /dashboard (Next.js)

```ts
// lib/inventory.ts
import { createClient } from "@/utils/supabase/client"; // the site's existing Supabase browser client

const API = process.env.NEXT_PUBLIC_INVENTORY_API ?? "https://api.cityfoodcourt.co.ke";

export async function inv<T>(path: string, init: RequestInit = {}): Promise<T> {
  const { data: { session } } = await createClient().auth.getSession();
  if (!session) throw new Error("Not signed in");
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { ...init.headers, Authorization: `Bearer ${session.access_token}`, "Content-Type": "application/json" },
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? res.statusText);
  return res.json();
}

// e.g. const me = await inv("/api/state");  const stock = await inv("/api/stock/overview");
```

Useful endpoints for the dashboard pages: `/api/state` (who am I, permissions, open day),
`/api/dashboard`, `/api/stock/overview`, `/api/stock/movement` (waste / adjustment),
`/api/suppliers`, `/api/purchase-orders` (+ `/order`, `/receive`), `/api/day/opening-preview`,
`/api/day/open`, `/api/day/close`, `/api/reports/catalog`, `/api/reports/run/{key}`,
`/api/reports/valuation`, `/api/export/{kind}.csv`. Errors come back as `{"detail": "..."}` in
plain language, ready to show to the user.

Status codes worth handling: `401` not signed in / session expired → send to the website login;
`403` signed in but not linked to staff, or not allowed; `404` with "inventory-only" → a till
feature that is switched off here.

## 6. Bringing in this month's records and daily routine

* **Import records**: cityfoodcourt.co.ke/admin/inventory → *Import records* (or *Open / Close day* in the INV-POS screen, which offers it while there are no closed days). Excel or CSV, one sheet per day or one table with a date column. Units are taken from the file, matching stock items or the quantities; only items that can't be told apart are asked about. Today then opens with the last closing count.
* **Automatic opening**: set `INVPOS_AUTO_OPEN_DAY=1` on Render (already in `render.yaml`) or switch it on under Setup → Settings → *Opening the day*. The first stock entry of a new day opens it with last night's closing carried over. Closing still needs the night count (website: *Inventory → Day*).
* **Analytics**: `GET /api/analytics?days=7|30|90` feeds both dashboards. Values in KES are only returned to roles that may see costs.

## Backups

Supabase's free plan has no downloadable backups. Take your own, at least weekly and before
every deploy that changes the schema:

```
pg_dump "<session pooler URI>" --schema=inventory -Fc -f inv-pos-2026-11-04.dump
```

Restore into an empty database with `pg_restore -d "<URI>" inv-pos-2026-11-04.dump`.
Keep `INVPOS_SECRET` with the backups (separately). Without it the PINs in a restored copy can't be used.

## Running the tests against Postgres

```
DATABASE_URL=postgresql://postgres@127.0.0.1/invpos_test python -m pytest -q
```

Use a scratch database: the tests wipe it. Never point this at Supabase.

## Limits to know

* Run **one** Render instance. The login throttle is in memory, and the advisory write lock
  already serialises writes the same way SQLite does.
* No MICROS sales yet (phase 4). The till is off, so `/api/orders`, `/api/tabs`, `/api/shifts` return 404.
* The engine's own browser UI is still served at `/`. It's a usable interim dashboard until the
  Next.js pages exist, and the place for admin setup.
