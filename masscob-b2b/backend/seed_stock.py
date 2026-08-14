#!/usr/bin/env python3
"""
seed_stock.py — siembra inicial de la tabla `stock` a partir de products.js.

Uso (una sola vez, desde backend/):
    python seed_stock.py

Lee ../products.js (stock "físico" del último Excel importado), le resta
las unidades de los pedidos que ya estén ACEPTADO en la base de datos, e
inserta el resultado en `stock` — así el invariante "stock en vivo = stock
ERP menos lo ya aceptado" se cumple desde el primer día.

AVISO — reposición física no resuelta todavía: a partir de aquí `stock` es
la única fuente de verdad y build_products.py/products.js ya NO la
alimentan. Si el almacén recibe mercancía nueva y se reimporta el Excel del
ERP, esta tabla NO sube sola — hace falta un mecanismo aparte (pendiente de
diseñar) para aplicar esa reposición aquí. Re-ejecutar este script tal cual
NO sirve para eso: recalcula desde cero (stock ERP - aceptados) y pisaría
cualquier descuento ya aplicado por pedidos aceptados después del seed
inicial.
"""
import json
import os
import re

from db import get_conn

PRODUCTS_JS = os.path.join(os.path.dirname(__file__), "..", "products.js")


def cargar_stock_bruto(path=PRODUCTS_JS):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"window\.MASSCOB_PRODUCTS = (\[.*?\]);", text, re.S)
    if not m:
        raise RuntimeError(f"No se encontró window.MASSCOB_PRODUCTS en {path}")
    productos = json.loads(m.group(1))
    raw = {}
    for p in productos:
        codigo = p["codigo"]
        for color, tallas in (p.get("colores") or {}).items():
            for talla, cantidad in tallas.items():
                raw[(codigo, color, talla)] = cantidad
    return raw


def cargar_aceptados(cur):
    cur.execute(
        "select pi.codigo, pi.color, pi.talla, sum(pi.cantidad) "
        "from pedido_items pi join pedidos p on p.id = pi.pedido_id "
        "where p.estado = 'ACEPTADO' "
        "group by pi.codigo, pi.color, pi.talla"
    )
    return {(codigo, color, talla): cantidad for codigo, color, talla, cantidad in cur.fetchall()}


def main():
    raw_stock = cargar_stock_bruto()
    print(f"products.js: {len(raw_stock)} combinaciones codigo/color/talla")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            aceptados = cargar_aceptados(cur)
            print(f"pedidos ACEPTADO: {len(aceptados)} combinaciones a descontar")

            filas = []
            ajustadas = 0
            for key, cantidad in raw_stock.items():
                descuento = aceptados.get(key, 0)
                seed_qty = cantidad - descuento
                if seed_qty < 0:
                    print(f"  aviso: {key} quedaría en {seed_qty}, se deja en 0 (revisar a mano)")
                    seed_qty = 0
                if descuento:
                    ajustadas += 1
                filas.append((*key, seed_qty))

            cur.executemany(
                "insert into stock (codigo, color, talla, cantidad) values (%s, %s, %s, %s) "
                "on conflict (codigo, color, talla) do update set cantidad = excluded.cantidad",
                filas,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"stock sembrado: {len(filas)} filas ({ajustadas} ajustadas por pedidos ya aceptados)")


if __name__ == "__main__":
    main()
