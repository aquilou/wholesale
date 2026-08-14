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
-- Se siembra una vez desde products.js (ver backend/seed_stock.py) y a
-- partir de ahí el backend es la única fuente de verdad: se descuenta al
-- aceptar un pedido (PATCH /admin/pedidos/{id}/estado) y se restituye si
-- el pedido deja de estar ACEPTADO. Reimportar el Excel del ERP y
-- regenerar products.js NO toca esta tabla — ver aviso en seed_stock.py.
create table if not exists stock (
  codigo text not null,
  color  text not null,
  talla  text not null,
  cantidad int not null check (cantidad >= 0),
  primary key (codigo, color, talla)
);

-- Todas las tablas bloqueadas para acceso directo (anon/authenticated desde
-- el navegador); solo el backend (FastAPI, con DATABASE_URL) puede leer y
-- escribir. Si algún día se llama a Supabase directo desde el navegador,
-- hacen falta políticas explícitas aquí — hoy no hay ninguna a propósito.
alter table clientes enable row level security;
alter table pedidos enable row level security;
alter table pedido_items enable row level security;
alter table stock enable row level security;
