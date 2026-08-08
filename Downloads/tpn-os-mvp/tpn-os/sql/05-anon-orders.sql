-- ═══════════════════════════════════════════════════════════════
-- TPN OS — Migration 05: Fix online-order RLS
-- The public website's checkout creates pickup/delivery orders from
-- unauthenticated visitors. The initial policy only permitted dine_in.
-- ═══════════════════════════════════════════════════════════════

drop policy if exists orders_anon_create on public.orders;
create policy orders_anon_create on public.orders
  for insert to anon
  with check (order_type in ('dine_in', 'pickup', 'delivery'));

drop policy if exists order_items_anon_insert on public.order_items;
create policy order_items_anon_insert on public.order_items
  for insert to anon
  with check (
    exists (
      select 1 from public.orders o
      where o.id = order_id
        and o.order_type in ('dine_in', 'pickup', 'delivery')
    )
  );
