#!/usr/bin/env python3
"""
Importador de stock MASSCOB (informe ERP .xls) -> JSON catálogo.

Estructura del informe (jerárquica):
  [Código, Nombre, Almacén, Nombre almacén, Stock total]   <- producto
  [COLOR - TALLAJE {ALFANUMERICO|NUMERICO}, "Stock", ...]   <- cabecera
  [COLOR - TALLA, stock, ...]                                <- variante (1..n)

Salida: lista de productos con variantes agrupadas por color y desglose por talla.
"""
import sys, json, re
import pandas as pd

def select_sheet(path):
    """
    El ERP a veces exporta el informe con una pestaña 'Instrucciones' delante
    de los datos, desplazando la hoja de stock de la posición 0 a la 1.
    Prioriza la hoja 'Stock Masscob' si existe; si no, la primera hoja que
    no se llame 'Instrucciones'; si no, la hoja 0 (formato antiguo, una sola hoja).
    """
    xl = pd.ExcelFile(path)
    if 'Stock Masscob' in xl.sheet_names:
        return 'Stock Masscob'
    candidatas = [s for s in xl.sheet_names if s.strip().lower() != 'instrucciones']
    return candidatas[0] if candidatas else xl.sheet_names[0]


def parse(path):
    df = pd.read_excel(path, sheet_name=select_sheet(path), header=None)
    products, errors, warnings = [], [], []
    cur = None              # producto actual
    sizing = None           # tipo de tallaje vigente

    def val(x):
        return str(x).strip() if pd.notna(x) else ''

    for idx, row in df.iterrows():
        excel_row = idx + 1
        c0, c1 = val(row[0]), val(row[1])
        if not c0:
            continue
        # cabecera de informe / títulos
        if c0 in ('Código',) or c0.startswith('Informe de stock') or re.match(r'^\d{4}-\d{2}-\d{2}', c0):
            continue
        # cabecera de tallaje
        if c0.startswith('COLOR - TALLAJE'):
            sizing = 'numerico' if 'NUMERICO' in c0 else 'alfanumerico'
            continue
        # fila de producto: código tipo S26/405W o W27/104A + nombre + almacén numérico
        if re.match(r'^[SW]\d{2}/\d+', c0) and c1:
            stock_total = row[4] if pd.notna(row[4]) else None
            cur = {
                'codigo': c0,
                'nombre': c1,
                'almacen': int(row[2]) if pd.notna(row[2]) else None,
                'nombre_almacen': val(row[3]),
                'stock_total_erp': int(stock_total) if stock_total is not None else None,
                'tallaje': None,
                'colores': {},       # color -> {talla: stock}
            }
            products.append(cur)
            continue
        # fila de variante: "COLOR - TALLA" con stock en col 1
        if ' - ' in c0 and cur is not None:
            color, talla = [p.strip() for p in c0.rsplit(' - ', 1)]
            try:
                stock = int(float(c1)) if c1 else 0
            except ValueError:
                errors.append(f'Fila {excel_row}: stock no numérico "{c1}" en {cur["codigo"]} / {c0}')
                continue
            cur['tallaje'] = cur['tallaje'] or sizing
            cur['colores'].setdefault(color, {})[talla] = stock
            continue
        warnings.append(f'Fila {excel_row}: no reconocida -> "{c0}" | "{c1}"')

    # validación: suma de variantes vs stock total ERP
    for p in products:
        suma = sum(s for tallas in p['colores'].values() for s in tallas.values())
        p['stock_calculado'] = suma
        if p['stock_total_erp'] is not None and suma != p['stock_total_erp']:
            warnings.append(
                f'{p["codigo"]} {p["nombre"]}: suma variantes ({suma}) != stock ERP ({p["stock_total_erp"]})')

    return products, errors, warnings


def to_catalog(products):
    """Forma plana, una entrada por color, lista para el componente <x-dc>."""
    out = []
    for p in products:
        for color, tallas in p['colores'].items():
            sid = (p['codigo'].replace('/', '-') + '-' + color.replace(' ', '_')).lower()
            out.append({
                'id': sid,
                'codigo': p['codigo'],
                'name': p['nombre'].title(),
                'color': color.title(),
                'tallaje': p['tallaje'],
                'sizes': {t: s for t, s in tallas.items()},
                'stock': sum(tallas.values()),
                # campos que NO vienen del ERP de stock; rellenar aparte:
                'pvp': None,
                'price': None,
                'category': None,
            })
    return out


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else 'stockmasscob.xls'
    products, errors, warnings = parse(path)
    catalog = to_catalog(products)

    print(f'Productos: {len(products)}  |  Entradas catálogo (color): {len(catalog)}')
    print(f'Errores: {len(errors)}  |  Avisos: {len(warnings)}')
    if errors:
        print('\n== ERRORES ==')
        for e in errors: print('  ✗', e)
    if warnings:
        print('\n== AVISOS ==')
        for w in warnings: print('  ⚠', w)

    json.dump({'products': products, 'catalog': catalog},
              open('catalog.json', 'w'), ensure_ascii=False, indent=2)
    print('\n-> catalog.json escrito')
