-- ═══════════════════════════════════════════════════════════════
-- TPN OS — Seed Data
-- Run this AFTER tpn-01-schema.sql
-- Contains: 3 branches, 10 menu categories, sample menu items, 8 tables
-- Extend the menu items as needed from your v5 HTML file.
-- ═══════════════════════════════════════════════════════════════

-- ── BRANCHES ───────────────────────────────────────────────────
insert into public.branches (code, name, address) values
  ('laspinas', 'Las Piñas Main', '316 Real Street, Alabang-Zapote Road, Talon Uno, Las Piñas City'),
  ('bacoor',   'Bacoor Stall',   'Bacoor City Hall Food Court, Bacoor, Cavite'),
  ('tgt',      'TGT Concession', 'TGT Building — address TBD');

-- ── MENU CATEGORIES ────────────────────────────────────────────
insert into public.menu_categories (name, name_tagalog, display_order, icon) values
  ('Signature Dishes',    'Espesyal',    1, '⭐'),
  ('Silog Meals',         'Silog',       2, '🍳'),
  ('Rice Bowls',          'Kanin',       3, '🍚'),
  ('Grilled Favorites',   'Ihaw-Ihaw',   4, '🔥'),
  ('Soups & Stews',       'Sabaw',       5, '🍲'),
  ('Vegetables & Sides',  'Gulay',       6, '🥬'),
  ('Sizzling Plates',     'Sisig',       7, '🥘'),
  ('Snacks & Merienda',   'Meryenda',    8, '🥟'),
  ('Beverages',           'Inumin',      9, '🥤'),
  ('Desserts',            'Panghimagas', 10, '🍮');

-- ── SAMPLE MENU ITEMS + TABLES ─────────────────────────────────
do $$
declare
  laspinas_id uuid;
  bacoor_id uuid;
  tgt_id uuid;
  sig_id uuid;
  silog_id uuid;
  rice_id uuid;
  soup_id uuid;
  all_branches jsonb := '{"laspinas":true,"bacoor":true,"tgt":true}'::jsonb;
begin
  select id into laspinas_id from public.branches where code = 'laspinas';
  select id into bacoor_id   from public.branches where code = 'bacoor';
  select id into tgt_id      from public.branches where code = 'tgt';
  select id into sig_id      from public.menu_categories where name = 'Signature Dishes';
  select id into silog_id    from public.menu_categories where name = 'Silog Meals';
  select id into rice_id     from public.menu_categories where name = 'Rice Bowls';
  select id into soup_id     from public.menu_categories where name = 'Soups & Stews';

  -- Featured signature dishes (shareable, with pax pricing)
  insert into public.menu_items (category_id, name, name_tagalog, description, price, is_shareable, pax_options, branch_availability, display_order) values
    (sig_id, 'Kare-Kareng Lechon Kawali', 'Kare-Kare',      'Crispy pork belly in rich peanut-based stew with vegetables', 495, true, '{"2-3":495,"4-6":795}'::jsonb, all_branches, 1),
    (sig_id, 'Laing',                     'Laing',          'Taro leaves in coconut milk with chili and shrimp paste',      285, true, '{"2-3":285,"4-6":485}'::jsonb, all_branches, 2),
    (sig_id, 'Beef Caldereta',            'Kalderetang Baka','Slow-braised beef in tomato-based stew with vegetables',      495, true, '{"2-3":495,"4-6":795}'::jsonb, all_branches, 3);

  -- Silog meals (solo plates)
  insert into public.menu_items (category_id, name, name_tagalog, description, price, is_shareable, branch_availability, display_order) values
    (silog_id, 'Tapsilog',   'Tapsilog',   'Marinated beef tapa with garlic rice and sunny-side egg',    165, false, all_branches, 1),
    (silog_id, 'Longsilog',  'Longsilog',  'Sweet longganisa with garlic rice and sunny-side egg',        155, false, all_branches, 2),
    (silog_id, 'Bangsilog',  'Bangsilog',  'Marinated milkfish with garlic rice and sunny-side egg',      175, false, all_branches, 3),
    (silog_id, 'Cornsilog',  'Cornsilog',  'Corned beef with garlic rice and sunny-side egg',             145, false, all_branches, 4);

  -- Rice bowls / soups (add more from your v5 menu file)
  insert into public.menu_items (category_id, name, name_tagalog, description, price, is_shareable, branch_availability, display_order) values
    (rice_id, 'Unli Rice',     'Unli Rice',    'Unlimited garlic rice add-on',        45, false, all_branches, 1),
    (soup_id, 'Sinigang na Baboy', 'Sinigang', 'Pork in tamarind-based sour broth',  345, true,  '{"2-3":345,"4-6":545}'::jsonb, 1);

  -- Physical tables for Las Piñas (8 per your existing setup)
  for i in 1..8 loop
    insert into public.restaurant_tables (branch_id, table_number, capacity)
    values (laspinas_id, i, 4);
  end loop;

  -- Bacoor has fewer tables (stall)
  for i in 1..4 loop
    insert into public.restaurant_tables (branch_id, table_number, capacity)
    values (bacoor_id, i, 4);
  end loop;
end $$;

-- Done. Next: create your first admin user via Supabase Dashboard > Authentication.
