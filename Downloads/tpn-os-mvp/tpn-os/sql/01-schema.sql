-- ═══════════════════════════════════════════════════════════════
-- TPN OS — Database Schema v1.0
-- Migration from localStorage prototype to Supabase Postgres
-- Run this ENTIRE file in Supabase SQL Editor as one migration.
-- ═══════════════════════════════════════════════════════════════

-- ── ENUMS ──────────────────────────────────────────────────────
create type staff_role as enum (
  'dining', 'kitchen', 'supervisor', 'manager', 'admin', 'director', 'ceo'
);
create type order_type_enum as enum ('dine_in', 'pickup', 'delivery');
create type order_status as enum (
  'pending', 'confirmed', 'preparing', 'ready', 'served', 'completed', 'cancelled'
);
create type payment_method as enum ('cash', 'gcash', 'paymaya', 'card');
create type payment_status as enum ('pending', 'paid', 'refunded', 'failed');
create type station_enum as enum ('kitchen', 'bar', 'cold_prep', 'service');
create type inquiry_type as enum ('events', 'catering', 'concession', 'general');
create type inquiry_status as enum ('pending', 'in_progress', 'resolved', 'closed');

-- ── PRIVATE SCHEMA for security-definer helpers ────────────────
create schema if not exists private;

-- ═══════════════════════════════════════════════════════════════
-- TABLES
-- ═══════════════════════════════════════════════════════════════

-- Branches
create table public.branches (
  id uuid primary key default gen_random_uuid(),
  code text unique not null,
  name text not null,
  address text,
  is_active boolean default true,
  created_at timestamptz default now()
);

-- Staff (extends auth.users)
create table public.staff (
  id uuid primary key references auth.users(id) on delete cascade,
  full_name text not null,
  phone text,
  role staff_role not null default 'dining',
  branch_id uuid references public.branches(id),
  employment_status text default 'active',
  hired_at date,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index idx_staff_role on public.staff(role);
create index idx_staff_branch on public.staff(branch_id);

-- Menu categories
create table public.menu_categories (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  name_tagalog text,
  display_order int default 0,
  icon text,
  is_active boolean default true,
  created_at timestamptz default now()
);

-- Menu items
create table public.menu_items (
  id uuid primary key default gen_random_uuid(),
  category_id uuid references public.menu_categories(id),
  name text not null,
  name_tagalog text,
  description text,
  price numeric(10,2) not null,
  image_url text,
  is_available boolean default true,
  is_shareable boolean default false,
  pax_options jsonb,
  station station_enum default 'kitchen',
  display_order int default 0,
  branch_availability jsonb,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index idx_menu_items_category on public.menu_items(category_id);
create index idx_menu_items_available on public.menu_items(is_available) where is_available = true;

-- Restaurant tables (physical)
create table public.restaurant_tables (
  id uuid primary key default gen_random_uuid(),
  branch_id uuid references public.branches(id) not null,
  table_number int not null,
  capacity int default 4,
  is_active boolean default true,
  created_at timestamptz default now(),
  unique(branch_id, table_number)
);

-- Orders
create sequence order_number_seq;

create table public.orders (
  id uuid primary key default gen_random_uuid(),
  order_number text unique not null,
  branch_id uuid references public.branches(id) not null,
  table_id uuid references public.restaurant_tables(id),
  order_type order_type_enum not null,
  status order_status default 'pending',
  customer_name text,
  customer_phone text,
  subtotal numeric(10,2) not null default 0,
  service_charge numeric(10,2) default 0,
  discount numeric(10,2) default 0,
  total numeric(10,2) not null default 0,
  payment_method payment_method,
  payment_status payment_status default 'pending',
  payment_reference text,
  notes text,
  placed_at timestamptz default now(),
  confirmed_at timestamptz,
  ready_at timestamptz,
  served_at timestamptz,
  completed_at timestamptz,
  cancelled_at timestamptz,
  cancel_reason text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index idx_orders_branch on public.orders(branch_id);
create index idx_orders_status on public.orders(status);
create index idx_orders_active on public.orders(branch_id, status)
  where status in ('pending','confirmed','preparing','ready');

-- Order items
create table public.order_items (
  id uuid primary key default gen_random_uuid(),
  order_id uuid references public.orders(id) on delete cascade not null,
  menu_item_id uuid references public.menu_items(id),
  name_snapshot text not null,
  price_snapshot numeric(10,2) not null,
  quantity int not null default 1,
  pax_size text,
  unit_price numeric(10,2) not null,
  total_price numeric(10,2) not null,
  notes text,
  station station_enum default 'kitchen',
  status order_status default 'pending',
  created_at timestamptz default now()
);
create index idx_order_items_order on public.order_items(order_id);

-- Inquiries (B2B, events, catering)
create table public.inquiries (
  id uuid primary key default gen_random_uuid(),
  inquiry_type inquiry_type not null,
  status inquiry_status default 'pending',
  contact_name text not null,
  contact_phone text,
  contact_email text,
  organization text,
  message text not null,
  event_date date,
  expected_pax int,
  assigned_to uuid references public.staff(id),
  branch_id uuid references public.branches(id),
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  resolved_at timestamptz,
  resolution_notes text
);
create index idx_inquiries_status on public.inquiries(status);

-- Audit log
create table public.audit_log (
  id uuid primary key default gen_random_uuid(),
  actor_id uuid references public.staff(id),
  actor_role staff_role,
  action text not null,
  entity_type text,
  entity_id uuid,
  before_state jsonb,
  after_state jsonb,
  metadata jsonb,
  ip_address inet,
  user_agent text,
  created_at timestamptz default now()
);
create index idx_audit_created on public.audit_log(created_at desc);
create index idx_audit_actor on public.audit_log(actor_id);
create index idx_audit_entity on public.audit_log(entity_type, entity_id);

-- ═══════════════════════════════════════════════════════════════
-- FUNCTIONS (SECURITY DEFINER, in private schema)
-- ═══════════════════════════════════════════════════════════════

-- Get current user's staff role
create or replace function private.my_role()
returns staff_role
language sql security definer stable
set search_path = ''
as $$
  select role from public.staff where id = auth.uid()
$$;

-- Get current user's branch
create or replace function private.my_branch()
returns uuid
language sql security definer stable
set search_path = ''
as $$
  select branch_id from public.staff where id = auth.uid()
$$;

-- Role hierarchy check
create or replace function private.has_role(min_role staff_role)
returns boolean
language sql security definer stable
set search_path = ''
as $$
  select coalesce(
    array_position(enum_range(null::public.staff_role), private.my_role())
    >= array_position(enum_range(null::public.staff_role), min_role),
    false
  )
$$;

-- Generate order number: TPN-{BRANCH}-{YYYYMMDD}-{4-digit}
create or replace function public.set_order_number()
returns trigger language plpgsql as $$
declare
  branch_code text;
  seq_num int;
begin
  if new.order_number is null then
    select code into branch_code from public.branches where id = new.branch_id;
    seq_num := nextval('order_number_seq');
    new.order_number := format('TPN-%s-%s-%s',
      upper(branch_code),
      to_char(now(), 'YYYYMMDD'),
      lpad(seq_num::text, 4, '0')
    );
  end if;
  return new;
end $$;

create trigger trg_orders_number
  before insert on public.orders
  for each row execute function public.set_order_number();

-- updated_at trigger
create or replace function public.tg_touch_updated()
returns trigger language plpgsql as $$
begin new.updated_at := now(); return new; end $$;

create trigger trg_staff_touch before update on public.staff
  for each row execute function public.tg_touch_updated();
create trigger trg_menu_items_touch before update on public.menu_items
  for each row execute function public.tg_touch_updated();
create trigger trg_orders_touch before update on public.orders
  for each row execute function public.tg_touch_updated();
create trigger trg_inquiries_touch before update on public.inquiries
  for each row execute function public.tg_touch_updated();

-- Auto-create staff row when new user signs up
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer
set search_path = ''
as $$
begin
  insert into public.staff (id, full_name, phone, role)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'full_name', new.email),
    new.raw_user_meta_data->>'phone',
    'dining'  -- default lowest role
  );
  return new;
end $$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ═══════════════════════════════════════════════════════════════
-- ROW LEVEL SECURITY
-- ═══════════════════════════════════════════════════════════════

alter table public.branches enable row level security;
alter table public.staff enable row level security;
alter table public.menu_categories enable row level security;
alter table public.menu_items enable row level security;
alter table public.restaurant_tables enable row level security;
alter table public.orders enable row level security;
alter table public.order_items enable row level security;
alter table public.inquiries enable row level security;
alter table public.audit_log enable row level security;

-- BRANCHES: public read, admin+ write
create policy branches_read on public.branches
  for select to anon, authenticated using (is_active = true);
create policy branches_write on public.branches
  for all to authenticated using (private.has_role('admin')) with check (private.has_role('admin'));

-- STAFF: self read, manager+ reads branch, admin+ reads all, admin writes
create policy staff_self_read on public.staff
  for select to authenticated using (id = auth.uid());
create policy staff_branch_read on public.staff
  for select to authenticated using (
    private.has_role('manager') and branch_id = private.my_branch()
  );
create policy staff_admin_read on public.staff
  for select to authenticated using (private.has_role('admin'));
create policy staff_admin_write on public.staff
  for insert to authenticated with check (private.has_role('admin'));
create policy staff_admin_update on public.staff
  for update to authenticated using (private.has_role('admin')) with check (private.has_role('admin'));
create policy staff_director_delete on public.staff
  for delete to authenticated using (private.has_role('director'));

-- MENU: public read active, manager+ writes
create policy menu_cat_read on public.menu_categories
  for select to anon, authenticated using (is_active = true);
create policy menu_cat_write on public.menu_categories
  for all to authenticated using (private.has_role('manager')) with check (private.has_role('manager'));

create policy menu_items_read on public.menu_items
  for select to anon, authenticated using (is_available = true);
create policy menu_items_write on public.menu_items
  for all to authenticated using (private.has_role('manager')) with check (private.has_role('manager'));

-- TABLES: public read active, admin+ writes
create policy tables_read on public.restaurant_tables
  for select to anon, authenticated using (is_active = true);
create policy tables_write on public.restaurant_tables
  for all to authenticated using (private.has_role('admin')) with check (private.has_role('admin'));

-- ORDERS: 
--   anon can create dine-in orders (from table QR)
--   auth can create any orders
--   staff read their branch, admin reads all
--   staff update their branch
create policy orders_anon_create on public.orders
  for insert to anon with check (order_type = 'dine_in');
create policy orders_auth_create on public.orders
  for insert to authenticated with check (true);
create policy orders_staff_read on public.orders
  for select to authenticated using (
    branch_id = private.my_branch() or private.has_role('admin')
  );
create policy orders_anon_read on public.orders
  for select to anon using (true);
create policy orders_staff_update on public.orders
  for update to authenticated using (
    branch_id = private.my_branch() or private.has_role('admin')
  );

-- ORDER_ITEMS: inherit from parent order
create policy order_items_read on public.order_items
  for select to anon, authenticated using (
    exists (select 1 from public.orders o where o.id = order_id)
  );
create policy order_items_anon_insert on public.order_items
  for insert to anon with check (
    exists (select 1 from public.orders o where o.id = order_id and o.order_type = 'dine_in')
  );
create policy order_items_auth_insert on public.order_items
  for insert to authenticated with check (true);
create policy order_items_update on public.order_items
  for update to authenticated using (
    exists (
      select 1 from public.orders o
      where o.id = order_id
        and (o.branch_id = private.my_branch() or private.has_role('admin'))
    )
  );

-- INQUIRIES: public create, supervisor+ read, manager+ update
create policy inquiries_create on public.inquiries
  for insert to anon, authenticated with check (true);
create policy inquiries_read on public.inquiries
  for select to authenticated using (private.has_role('supervisor'));
create policy inquiries_update on public.inquiries
  for update to authenticated using (private.has_role('manager')) with check (private.has_role('manager'));

-- AUDIT LOG: director+ reads, authenticated writes
create policy audit_read on public.audit_log
  for select to authenticated using (private.has_role('director'));
create policy audit_write on public.audit_log
  for insert to authenticated with check (true);

-- ═══════════════════════════════════════════════════════════════
-- REALTIME publications
-- ═══════════════════════════════════════════════════════════════

alter publication supabase_realtime add table public.orders;
alter publication supabase_realtime add table public.order_items;
alter publication supabase_realtime add table public.inquiries;

-- Done. Now run tpn-02-seed.sql to insert your branches, menu, and tables.
