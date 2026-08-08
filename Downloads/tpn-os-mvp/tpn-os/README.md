# TPN OS

Production-ready MVP of the Tyo Paeng Nyo three-surface web system. All data lives in Supabase; no more demo state, no fake accounts.

- **`index.html`** — public site + staff portal
- **`tpn-table-menu.html`** — dine-in customer menu (opens via table QR)
- **`tpn-dine-in-floor.html`** — live floor panel for staff
- **`tpn-supabase.js`** — shared Supabase data layer

## First-time setup

### 1. Run the SQL migrations (in order)
In Supabase SQL Editor:

1. `sql/01-schema.sql` — tables, RLS, triggers
2. `sql/02-seed.sql` — branches, menu, tables
3. `sql/03-security-fixes.sql` — hardens the schema
4. `sql/04-signals.sql` — call-staff / bill-request signals
5. `sql/05-anon-orders.sql` — lets anonymous customers place pickup/delivery orders

### 2. Paste your Supabase credentials
Open `tpn-supabase.js` and replace the two placeholder values at the top:

```js
const SUPABASE_URL      = 'https://YOUR-PROJECT-REF.supabase.co';
const SUPABASE_ANON_KEY = 'PASTE-YOUR-ANON-KEY-HERE';
```

Find these in Supabase → Project Settings → API.

### 3. Create your first admin user
Supabase → Authentication → Users → Add user (email + password).

Then in SQL Editor:
```sql
update public.staff
set role = 'ceo',
    full_name = 'Your Name Here',
    branch_id = (select id from public.branches where code = 'laspinas')
where id = (select id from auth.users where email = 'YOUR-EMAIL');
```

That user is now CEO and can see everything.

## What's wired up (fully functional)

**Customer flows:**
- Public site menu, cart, checkout → creates real order in DB
- Table QR menu (dine-in) → creates order, notifies floor panel
- "Call staff" and "Request bill" from any table → live signal
- B2B concession + event inquiry forms → land in `inquiries` table

**Staff portal:**
- Real Supabase auth login (email + password)
- No fake accounts, no demo data — starts empty, fills as real data lands
- Live Orders kanban — realtime updates as orders come in
- Inquiry Inbox — reads from DB
- Accounts panel — reads real staff from DB (manager+ can see all)
- Table QR generator — creates deep links for physical tables
- Dashboard revenue is computed from actual orders

**Realtime sync:**
- Orders across all 3 surfaces update within ~1 second
- Table signals (call-staff, bill-request) push instantly to floor panel + portal

## What's still simplified for MVP

- **Staff Board, Recognition Panel, Audit Log** — the panels render (empty for a fresh system), but writing to them isn't wired to the DB yet
- **2FA** — the fake SMS flow is bypassed. Manager+ users log in with just password. Real SMS gateway (Semaphore) hookup is a follow-up.
- **Menu items on customer surfaces are still hardcoded in the HTML** — not yet reading from the `menu_items` table. Editing prices means editing the HTML for now. DB-driven menu is a follow-up pass.
- **Direct staff account creation from the portal** isn't wired — for now, create staff in Supabase Auth dashboard, then update their `staff.role` and `staff.branch_id` via SQL.

## Test locally

```
python -m http.server 8000
```

Then visit:
- Public site + staff portal: <http://localhost:8000/>
- Table menu: <http://localhost:8000/tpn-table-menu.html?table=3&branch=Las%20Pi%C3%B1as&branchId=laspinas>
- Floor panel: <http://localhost:8000/tpn-dine-in-floor.html>

Place an order from the table menu → should appear in the floor panel and staff portal within ~1 second.

## Push updates to GitHub

```
git add .
git commit -m "your message"
git push
```

GitHub Pages auto-redeploys in ~30 seconds.

## Production checklist before real launch

- [ ] Move Supabase to Pro tier ($25/mo) — free tier pauses after 1 week idle
- [ ] Set custom domain on GitHub Pages
- [ ] Wire real payment webhook (GCash Business API) — QRs are display-only right now
- [ ] Add real SMS 2FA (Semaphore or Twilio) for manager+ accounts
- [ ] BIR-compliant receipts still handled by StoreHub side
- [ ] Set up a nightly backup of the Supabase DB
