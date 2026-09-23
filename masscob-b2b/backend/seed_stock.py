#!/usr/bin/env python3
"""
seed_stock.py — sobrescribe la tabla `stock` con lo que dice el Excel del ERP.

Uso (desde backend/, normalmente vía ../actualizar_catalogo.py):
    python seed_stock.py

Lee ../products.js (stock del último Excel importado) y deja la tabla
`stock` EXACTAMENTE igual: el Excel manda. No se restan pedidos PENDIENTE ni
ACEPTADO — si el Excel dice 3, en la web hay 3. Las combinaciones
codigo/color/talla que ya no aparecen en el Excel se ponen a 0.

A partir de aquí los pedidos siguen moviendo `stock` como siempre (reservan
al crearse, ANULADO devuelve) hasta la siguiente importación del Excel.
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
                raw[(codigo, color, talla)] = max(0, cantidad)
    return raw


def main():
    raw_stock = cargar_stock_bruto()
    print(f"products.js: {len(raw_stock)} combinaciones codigo/color/talla")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("select codigo, color, talla, cantidad from stock")
            actual = {(codigo, color, talla): cantidad for codigo, color, talla, cantidad in cur.fetchall()}

            filas = [(*key, cantidad) for key, cantidad in raw_stock.items()]
            a_cero = [key for key, cantidad in actual.items() if key not in raw_stock and cantidad != 0]
            cambiadas = sum(1 for key, cantidad in raw_stock.items() if actual.get(key) != cantidad)

            cur.executemany(
                "insert into stock (codigo, color, talla, cantidad) values (%s, %s, %s, %s) "
                "on conflict (codigo, color, talla) do update set cantidad = excluded.cantidad",
                filas,
            )
            cur.executemany(
                "update stock set cantidad = 0 where codigo = %s and color = %s and talla = %s",
                a_cero,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"stock sobrescrito desde el Excel: {len(filas)} filas ({cambiadas} cambiadas, "
          f"{len(a_cero)} que ya no están en el Excel puestas a 0)")


if __name__ == "__main__":
    main()
