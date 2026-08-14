#!/usr/bin/env python3
"""
Cruce de las tres fuentes MASSCOB por 'codigo':
  - stock   (del ERP, ya parseado por import_stock.py -> catalog.json)
  - precios (informe de precios del ERP: Código, Nombre, + columna de precio)
  - imagenes(mapa codigo -> ruta/nombre de fichero)

El precio del ERP es el NETO MAYORISTA definitivo (no se aplica descuento
a este nivel; los descuentos por cliente/zona se aplican después).
La 'Familia' se usa como categoría, si el informe la trae.

Regla central del merge: el stock se actualiza SIN pisar precios ni imágenes
ya asignados. Devuelve catálogo + diagnóstico (incompletos, huérfanos).
"""
import glob
import json
import os
import re
import pandas as pd

_COD_RE = re.compile(r'^[SW]\d{2}/')


def find_default_report(prefix):
    """
    Busca <prefix>.xlsx o <prefix>.xls en el directorio actual (el ERP a
    veces exporta un formato, a veces el otro) y devuelve el más reciente.
    """
    candidatos = [f for ext in ('xlsx', 'xls') for f in glob.glob(f'{prefix}.{ext}')]
    if not candidatos:
        raise FileNotFoundError(
            f"No se encontró '{prefix}.xlsx' ni '{prefix}.xls' en el directorio actual"
        )
    return max(candidatos, key=os.path.getmtime)

# El ERP exporta el precio bajo distintos nombres según el informe usado
# ("Informe de artículos" -> Precio; "Informe de precios" -> WHOLESALE EUROPA,
# WHOLESALE EUROPA + 10%, WHOLESALE EUROPA + 15%... una columna por tarifa).
# 'WHOLESALE EUROPA' a secas (sin '+ N%') es la tarifa base, y es la que
# sigue alimentando el precio del catálogo/carrito; el resto de columnas
# WHOLESALE* se guardan aparte como tarifas adicionales (panel > Tarifas).
_PRICE_COLS = ['Precio', 'WHOLESALE EUROPA']
_TARIFA_PCT_RE = re.compile(r'\+\s*(\d+)\s*%')


def _tarifa_pct(nombre_col):
    """% de recargo de una columna de tarifa a partir de su nombre (0 si es la base)."""
    m = _TARIFA_PCT_RE.search(nombre_col)
    return int(m.group(1)) if m else 0


def cargar_precios(path_xlsx):
    """
    Lee el informe de precios del ERP. Estructura:
      filas de cabecera (fecha, título), luego una fila con
      Código | Nombre | [Familia] | <col. de precio> [+ más columnas WHOLESALE...],
      y debajo los datos.
    Devuelve {codigo: {price, category, tarifas}}, con 'tarifas' la lista
    de TODAS las columnas WHOLESALE* de esa fila (incluida la base),
    ordenadas por % ascendente: [{nombre, pct, precio}, ...].
    """
    raw = pd.read_excel(path_xlsx, sheet_name=0, header=None, dtype=str)
    hdr = None
    for i, r in raw.iterrows():
        if str(r[0]).strip() == 'Código':
            hdr = i
            break
    if hdr is None:
        raise ValueError("No se encontró la fila de cabecera 'Código' en el Excel de precios")

    df = pd.read_excel(path_xlsx, sheet_name=0, header=hdr, dtype={'Código': str})
    df.columns = [str(c).strip() for c in df.columns]

    price_col = next((c for c in _PRICE_COLS if c in df.columns), None)
    if price_col is None:
        raise ValueError(
            "No se encontró ninguna columna de precio reconocida "
            f"({', '.join(_PRICE_COLS)}) en el Excel. Columnas presentes: {list(df.columns)}"
        )
    # todas las columnas de tarifa (WHOLESALE...), ordenadas por su % de recargo
    tarifa_cols = sorted(
        (c for c in df.columns if c.upper().startswith('WHOLESALE')),
        key=_tarifa_pct,
    )

    precios = {}
    for _, r in df.iterrows():
        cod = str(r.get('Código', '')).strip()
        if not _COD_RE.match(cod):
            continue
        p = pd.to_numeric(r.get(price_col), errors='coerce')
        if pd.isna(p):
            continue
        fam = str(r.get('Familia', '')).strip()
        categoria = fam.split('/')[-1] if '/' in fam else fam
        tarifas = []
        for col in tarifa_cols:
            tp = pd.to_numeric(r.get(col), errors='coerce')
            if pd.isna(tp):
                continue
            tarifas.append({'nombre': col, 'pct': _tarifa_pct(col), 'precio': round(float(tp), 2)})
        precios[cod] = {
            'price': round(float(p), 2),   # neto mayorista definitivo (tarifa base)
            'pvp': None,                    # el ERP no da PVP separado
            'category': categoria or None,
            'tarifas': tarifas,
        }
    return precios


def merge(stock_products, precios, imagenes=None, previo=None):
    """
    stock_products: lista de productos del ERP (catalog.json -> 'products')
    precios:  {codigo: {pvp, price}}
    imagenes: {codigo: ruta}            (opcional)
    previo:   catálogo anterior {codigo: {pvp, price, image}}  para NO pisar
    """
    imagenes = imagenes or {}
    previo = previo or {}
    catalogo, incompletos = [], []
    codigos_stock = set()

    for p in stock_products:
        cod = p['codigo']
        codigos_stock.add(cod)
        ant = previo.get(cod, {})
        pr = precios.get(cod, {})

        # precio/categoría nuevos si existen; si no, conserva lo anterior (no pisar)
        pvp = pr.get('pvp') if pr.get('pvp') is not None else ant.get('pvp')
        price = pr.get('price') if pr.get('price') is not None else ant.get('price')
        category = pr.get('category') if pr.get('category') is not None else ant.get('category')
        img = imagenes.get(cod) or ant.get('image')

        # B2B: publicable si tiene neto mayorista e imagen. El PVP es opcional
        # (el ERP no lo da); los descuentos por cliente se aplican sobre el neto.
        publicable = price is not None and img is not None
        if not publicable:
            faltan = [n for n, v in (('precio', price), ('imagen', img)) if v is None]
            incompletos.append({'codigo': cod, 'nombre': p['nombre'], 'faltan': faltan})

        catalogo.append({
            'codigo': cod, 'nombre': p['nombre'],
            'colores': p['colores'], 'stock': p['stock_calculado'],
            'category': category, 'pvp': pvp, 'price': price, 'image': img,
            'publicable': publicable,
        })

    huerfanos = [c for c in precios if c not in codigos_stock]
    return {
        'catalogo': catalogo,
        'incompletos': incompletos,
        'huerfanos_precios': huerfanos,
        'resumen': {
            'total': len(catalogo),
            'publicables': sum(1 for c in catalogo if c['publicable']),
            'incompletos': len(incompletos),
            'huerfanos': len(huerfanos),
        },
    }