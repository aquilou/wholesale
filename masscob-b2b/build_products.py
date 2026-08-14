#!/usr/bin/env python3
"""
build_products.py — Puente entre el motor de datos (Python) y el panel (Design).

Lee los dos .xls del ERP, cruza stock + precios, deriva categorías y filtra
insumos, y escribe products.js con:
    window.MASSCOB_PRODUCTS = [...]   // catálogo para el panel
    window.MASSCOB_META      = {...}  // totales de validación

Uso:
    python build_products.py stockmasscob.xls precioserp.xls
    -> genera products.js  (colócalo junto al Panel_Admin_MASSCOB_dc.html)
"""
import sys
import os
import json
import re
from collections import Counter
import pandas as pd

from import_stock import parse as parse_stock, select_sheet as select_stock_sheet
from merge_sources import cargar_precios, find_default_report
from categorias import derivar_categoria, derivar_coleccion
from import_images import importar as importar_fotos


def build(stock_path, price_path, fotos_root=None):
    # 1. stock (jerárquico) -> productos con colores/tallas
    productos, err_stock, warn_stock = parse_stock(stock_path)

    # 2. precios del ERP -> {codigo: {price, category(familia), pvp}}
    precios = cargar_precios(price_path)

    # 3. fecha de stock y familia (para la cabecera del panel)
    raw = pd.read_excel(stock_path, sheet_name=select_stock_sheet(stock_path), header=None, dtype=str)
    fecha_stock = ''
    for _, r in raw.head(6).iterrows():
        for cell in r:
            s = str(cell)
            m = re.search(r'\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}', s)
            if m:
                fecha_stock = m.group(0)
                break
        if fecha_stock:
            break

    # 4. construir catálogo final
    catalogo = []
    insumos = 0
    sin_categoria = []
    colecciones_count = Counter()
    for p in productos:
        cod = p['codigo']
        categoria, es_prenda = derivar_categoria(p['nombre'])
        if not es_prenda:
            insumos += 1
            continue  # insumo: no llega a la tienda
        pr = precios.get(cod, {})
        price_erp = pr.get('price')
        tarifas = pr.get('tarifas') or []
        if categoria is None:
            sin_categoria.append(p['nombre'])
        col_codigo, col_label = derivar_coleccion(cod)
        if col_codigo:
            colecciones_count[(col_codigo, col_label)] += 1
        catalogo.append({
            'codigo': cod,
            'name': p['nombre'].title(),
            'category': categoria or 'Sin categoría',
            'coleccion': col_codigo,       # ej. 'W27' — para poder filtrar por campaña
            'coleccionLabel': col_label,   # ej. 'WINTER 26-27'
            'colores': p['colores'],
            'stock': p['stock_calculado'],
            'tallaje': p['tallaje'] or 'alfanumerico',
            'price_erp': price_erp,      # neto del ERP; None si no hay precio
            'tarifas': tarifas,          # [{nombre, pct, precio}, ...] — todas las columnas WHOLESALE* de esta referencia
        })

    # 5. fotos: cruce por código+color, una carpeta por colección (ver import_images.py)
    fotos_root = fotos_root or os.path.join('..', 'Fotos')
    fotos_resumen = importar_fotos(catalogo, fotos_root)

    # 6. diagnóstico
    codigos_stock = {p['codigo'] for p in productos}
    huerfanos = [c for c in precios if c not in codigos_stock]
    colecciones = [
        {'codigo': c, 'label': l, 'count': n}
        for (c, l), n in sorted(colecciones_count.items(), key=lambda kv: -kv[1])
    ]

    # nombres de tarifa (columnas WHOLESALE* del informe de precios), para
    # las cabeceras de la tabla del panel > Tarifas — union de todas las
    # referencias por si alguna fila no trajera alguna columna
    tarifas_por_nombre = {}
    for pr in precios.values():
        for t in (pr.get('tarifas') or []):
            tarifas_por_nombre[t['nombre']] = t['pct']
    tarifas_meta = [
        {'nombre': n, 'pct': pct}
        for n, pct in sorted(tarifas_por_nombre.items(), key=lambda kv: kv[1])
    ]

    meta = {
        'recibidos': len(catalogo),
        'sinPrecio': sum(1 for c in catalogo if c['price_erp'] is None),
        'huerfanos': len(huerfanos),
        'insumos': insumos,
        'fechaStock': fecha_stock,
        'colecciones': colecciones,
        'conFoto': fotos_resumen['con_foto'],
        'sinFoto': len(fotos_resumen['sin_foto']),
        'tarifas': tarifas_meta,
    }
    return catalogo, meta, sin_categoria


def main():
    stock_path = sys.argv[1] if len(sys.argv) > 1 else find_default_report('stockmasscob')
    price_path = sys.argv[2] if len(sys.argv) > 2 else find_default_report('precioserp')
    fotos_root = sys.argv[3] if len(sys.argv) > 3 else None

    catalogo, meta, sin_cat = build(stock_path, price_path, fotos_root)

    js = (
        '// Generado por build_products.py — NO editar a mano.\n'
        '// Fuente: ' + stock_path + ' + ' + price_path + '\n'
        'window.MASSCOB_PRODUCTS = '
        + json.dumps(catalogo, ensure_ascii=False, indent=2) + ';\n'
        'window.MASSCOB_META = '
        + json.dumps(meta, ensure_ascii=False, indent=2) + ';\n'
    )
    with open('products.js', 'w', encoding='utf-8') as f:
        f.write(js)

    print(f'products.js generado: {len(catalogo)} productos')
    print(f'  con precio ERP : {sum(1 for c in catalogo if c["price_erp"] is not None)}')
    print(f'  sin precio     : {meta["sinPrecio"]}')
    print(f'  huérfanos      : {meta["huerfanos"]}')
    print(f'  insumos filtr. : {meta["insumos"]}')
    print(f'  con foto       : {meta["conFoto"]}')
    print(f'  sin foto       : {meta["sinFoto"]}')
    colecciones_txt = ' · '.join(f'{c["label"]} ({c["count"]})' for c in meta['colecciones'])
    print(f'  colecciones    : {colecciones_txt}  (stock {meta["fechaStock"]})')
    if sin_cat:
        print(f'  ⚠ sin categoría reconocida: {sin_cat}')


if __name__ == '__main__':
    main()
