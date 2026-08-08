-- ═══════════════════════════════════════════════════════════════
-- TPN OS — Security Warning Fixes (Migration 03)
-- Addresses all 8 Supabase advisor warnings from the initial schema.
-- Safe to run multiple times.
-- ═══════════════════════════════════════════════════════════════

-- ── 1. Fix mutable search_path on public functions ──────────────
-- With empty search_path, all references must be schema-qualified.

create or replace function public.set_order_number()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  branch_code text;
  seq_num int;
begin
  if new.order_number is null then
    select code into branch_code from public.branches where id = new.branch_id;
    seq_num := nextval('public.order_number_seq');
    new.order_number := format('TPN-%s-%s-%s',
      upper(branch_code),
      to_char(now(), 'YYYYMMDD'),
      lpad(seq_num::text, 4, '0')
    );
  end if;
  return new;
end $$;

create or replace function public.tg_touch_updated()
returns trigger
language plpgsql
set search_path = ''
as $$
begin new.updated_at := now(); return new; end $$;

-- ── 2. Revoke public execute on handle_new_user ─────────────────
-- Trigger still fires normally (triggers bypass grants),
-- but nobody can call it via /rest/v1/rpc/handle_new_user.

revoke execute on function public.handle_new_user() from public;
revoke execute on function public.handle_new_user() from anon;
revoke execute on function public.handle_new_user() from authenticated;

-- Also lock down search_path on this one while we're at it
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.staff (id, full_name, phone, role)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'full_name', new.email),
    new.raw_user_meta_data->>'phone',
    'dining'
  );
  return new;
end $$;

-- ── 3. Tighten permissive RLS policies ──────────────────────────

-- 3a. Audit log: user can only insert rows AS themselves
drop policy if exists audit_write on public.audit_log;
create policy audit_write on public.audit_log
  for insert to authenticated
  with check (actor_id = auth.uid());

-- 3b. Orders (auth): must be user's branch, unless admin+
drop policy if exists orders_auth_create on public.orders;
create policy orders_auth_create on public.orders
  for insert to authenticated
  with check (
    branch_id = private.my_branch() or private.has_role('admin')
  );

-- 3c. Order items (auth): parent order must be visible to the user
drop policy if exists order_items_auth_insert on public.order_items;
create policy order_items_auth_insert on public.order_items
  for insert to authenticated
  with check (
    exists (
      select 1 from public.orders o
      where o.id = order_id
        and (o.branch_id = private.my_branch() or private.has_role('admin'))
    )
  );

-- 3d. Inquiries: enforce required fields to block junk submissions
drop policy if exists inquiries_create on public.inquiries;
create policy inquiries_create on public.inquiries
  for insert to anon, authenticated
  with check (
    contact_name is not null and length(trim(contact_name)) > 0
    and message is not null and length(trim(message)) > 0
  );

-- Done. Re-run the Advisor to confirm all warnings are cleared.
