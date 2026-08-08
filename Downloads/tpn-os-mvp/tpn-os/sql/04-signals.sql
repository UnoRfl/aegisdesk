-- ═══════════════════════════════════════════════════════════════
-- TPN OS — Migration 04: Table Signals
-- Adds call-staff and bill-request signals to restaurant_tables,
-- with SECURITY DEFINER RPCs so anon customers can trigger them
-- WITHOUT full UPDATE rights on the tables table.
-- ═══════════════════════════════════════════════════════════════

-- ── Columns ───────────────────────────────────────────────────
alter table public.restaurant_tables
  add column if not exists call_staff_at    timestamptz,
  add column if not exists bill_requested_at timestamptz,
  add column if not exists bill_payment_method text,
  add column if not exists bill_total numeric(10,2);

-- ── RPCs ──────────────────────────────────────────────────────
-- Customer taps "Call staff" → sets timestamp
create or replace function public.signal_call_staff(p_table_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.restaurant_tables
  set call_staff_at = now()
  where id = p_table_id and is_active = true;
end $$;

-- Customer taps "Request bill" → sets timestamp + payment intent
create or replace function public.signal_bill_request(
  p_table_id uuid,
  p_payment_method text,
  p_total numeric
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.restaurant_tables
  set bill_requested_at = now(),
      bill_payment_method = p_payment_method,
      bill_total = p_total
  where id = p_table_id and is_active = true;
end $$;

-- Staff acknowledges (clears the signal)
create or replace function public.ack_call_staff(p_table_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not private.has_role('dining') then
    raise exception 'insufficient privileges';
  end if;
  update public.restaurant_tables
  set call_staff_at = null
  where id = p_table_id;
end $$;

create or replace function public.ack_bill_request(p_table_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not private.has_role('dining') then
    raise exception 'insufficient privileges';
  end if;
  update public.restaurant_tables
  set bill_requested_at = null,
      bill_payment_method = null,
      bill_total = null
  where id = p_table_id;
end $$;

-- ── Grants: anon can call signals, authenticated can ack ──────
grant execute on function public.signal_call_staff(uuid) to anon, authenticated;
grant execute on function public.signal_bill_request(uuid, text, numeric) to anon, authenticated;
grant execute on function public.ack_call_staff(uuid) to authenticated;
grant execute on function public.ack_bill_request(uuid) to authenticated;

-- ── Realtime for tables (so floor panel sees signal changes) ──
alter publication supabase_realtime add table public.restaurant_tables;
