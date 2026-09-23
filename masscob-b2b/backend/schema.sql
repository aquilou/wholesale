-- Esquema inicial MASSCOB Wholesale (Supabase / Postgres).
-- Correr esto una vez en el SQL Editor del panel de Supabase.

-- Un registro por cuenta de cliente (1:1 con auth.users, que ya gestiona
-- Supabase Auth). Mismos campos que la pestaña "Clientes" del panel admin.
create table if not exists clientes (
  id uuid primary key references auth.users(id) on delete cascade,
  nombre_comercial text not null,
  razon_social text,
  cif text,
  direccion text,
  pais text,
  agente text,
  created_at timestamptz not null default now()
);

-- Última vez que se generó o regeneró la contraseña de este cliente (alta
-- inicial, botón "Generar nueva contraseña" del panel, o el cron diario de
-- reseteo — ver GET /admin/cron/reset-passwords). Null = cliente creado
-- antes de este cambio, se resetea en el primer paso del cron.
alter table clientes add column if not exists password_updated_at timestamptz;

create table if not exists pedidos (
  id bigserial primary key,
  cliente_id uuid not null references clientes(id),
  referencia text not null unique,
  nota text,
  total numeric(10,2) not null,
  estado text not null default 'pendiente', -- pendiente | confirmado | enviado
  created_at timestamptz not null default now()
);

create table if not exists pedido_items (
  id bigserial primary key,
  pedido_id bigint not null references pedidos(id) on delete cascade,
  codigo text not null,
  nombre text not null,
  color text not null,
  talla text not null,
  cantidad int not null check (cantidad > 0),
  precio_unit numeric(10,2) not null
);

-- Stock en vivo por (codigo, color, talla) — mismo grano que pedido_items.
-- Cada importación del Excel del ERP (actualizar_catalogo.py ->
-- backend/seed_stock.py) la sobrescribe entera: el Excel manda. Entre
-- importaciones la mueven los pedidos: se reserva al crear el pedido
-- (POST /pedidos, ya en PENDIENTE) y se restituye si el pedido pasa a
-- ANULADO (y se vuelve a reservar si un ANULADO se reabre).
create table if not exists stock (
  codigo text not null,
  color  text not null,
  talla  text not null,
  cantidad int not null check (cantidad >= 0),
  primary key (codigo, color, talla)
);

-- Direcciones del equipo MASSCOB que reciben aviso por email de cada
-- pedido nuevo (además del propio cliente que lo hizo). Editable desde
-- Admin > Ajustes > Notificaciones por email.
create table if not exists notif_emails (
  email text primary key
);

-- Todas las tablas bloqueadas para acceso directo (anon/authenticated desde
-- el navegador); solo el backend (FastAPI, con DATABASE_URL) puede leer y
-- escribir. Si algún día se llama a Supabase directo desde el navegador,
-- hacen falta políticas explícitas aquí — hoy no hay ninguna a propósito.
alter table clientes enable row level security;
alter table pedidos enable row level security;
alter table pedido_items enable row level security;
alter table stock enable row level security;
alter table notif_emails enable row level security;
