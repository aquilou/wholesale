#!/usr/bin/env python3
"""
Utilidades de plataforma para MASSCOB B2B:

1. derivar_categoria(nombre) -> categoría de tipo de prenda, o None si el
   artículo no es una prenda vendible (patrones, telas, etiquetas, botones...).
   Esto filtra los insumos de fabricación que vienen mezclados en el ERP.

2. derivar_coleccion(codigo) -> colección (temporada) a partir del prefijo
   de la referencia: S+año = Summer (temporada de un solo año calendario),
   W+año = Winter (temporada a caballo entre año-1 y año).

3. aplicar_ajuste(neto_erp, pct) -> precio de plataforma tras aplicar un
   ±% sobre el neto del ERP, SIN perder el dato original.
"""
import re

_COL_RE = re.compile(r'^([SW])(\d{2})/')

# Mapa palabra-del-nombre -> categoría de tienda. Incluye typos del ERP
# (JAKECT) y variantes en español (FALDA) para normalizarlos.
_PRENDAS = {
    'COAT': 'Abrigos',
    'JACKET': 'Chaquetas', 'JAKECT': 'Chaquetas',      # typo en el ERP
    'CARDIGAN': 'Chaquetas',
    'SWEATER': 'Punto', 'POLO': 'Punto', 'TANKTOP': 'Punto',
    'SHIRT': 'Camisas',
    'TOP': 'Tops',
    'DRESS': 'Vestidos',
    'SKIRT': 'Faldas', 'FALDA': 'Faldas',              # variante ES
    'PANTS': 'Pantalones',
    'SCARF': 'Complementos', 'HAT': 'Complementos', 'BAG': 'Complementos',
    'BELT': 'Complementos', 'NECKLACE': 'Complementos', 'NECESER': 'Complementos',
}

# Palabras que indican insumo de fabricación, NO prenda vendible.
_INSUMOS = {
    'PT', 'TELA', 'ETI', 'ETIQ', 'ETIQUETA', 'BOTON', 'PIEL', 'FORRO',
    'VIVO', 'CENEFA', 'HEBILLA', 'ASA', 'METALICA', 'COLLAR', 'COLLARIN',
}


def derivar_categoria(nombre):
    """
    Devuelve (categoria, es_prenda).
    - es_prenda=False para insumos -> no deben publicarse en la tienda.
    - categoria=None si la palabra no se reconoce (revisar a mano).
    """
    if not nombre:
        return None, False
    palabra = nombre.strip().upper().split()[0]
    if palabra in _INSUMOS:
        return None, False
    if palabra in _PRENDAS:
        return _PRENDAS[palabra], True
    return None, True   # prenda desconocida: la dejamos pasar pero sin categoría


def derivar_coleccion(codigo):
    """
    Devuelve (codigo_coleccion, etiqueta) a partir del prefijo de la referencia.
      'W27/...' -> ('W27', 'WINTER 26-27')   temporada a caballo de dos años
      'S26/...' -> ('S26', 'SUMMER 2026')    temporada de un solo año
    Devuelve (None, None) si el código no matchea el patrón esperado.
    """
    m = _COL_RE.match(codigo or '')
    if not m:
        return None, None
    letra, yy = m.group(1), m.group(2)
    if letra == 'W':
        yy_prev = str((int(yy) - 1) % 100).zfill(2)
        return f'W{yy}', f'WINTER {yy_prev}-{yy}'
    return f'S{yy}', f'SUMMER 20{yy}'


def aplicar_ajuste(neto_erp, pct):
    """
    Aplica un ajuste porcentual sobre el neto del ERP.
      pct = 0    -> sin cambio
      pct = 10   -> +10%  (subida)
      pct = -15  -> -15%  (descuento)
    Devuelve el precio de plataforma redondeado a 2 decimales.
    El neto del ERP original NO se modifica (se conserva aparte).
    """
    if neto_erp is None:
        return None
    return round(float(neto_erp) * (1 + pct / 100.0), 2)


if __name__ == '__main__':
    import json
    prods = json.load(open('catalog.json'))['products']
    print('Derivación de categoría sobre el stock real:\n')
    sin_cat = []
    for p in prods:
        cat, es_prenda = derivar_categoria(p['nombre'])
        marca = cat if cat else ('[INSUMO]' if not es_prenda else '[? revisar]')
        print(f'  {p["codigo"]:<12} {p["nombre"][:28]:<28} -> {marca}')
        if cat is None and es_prenda:
            sin_cat.append(p['nombre'])
    if sin_cat:
        print('\nPrendas sin categoría reconocida (revisar mapa):', sin_cat)

    print('\nEjemplo de ajuste de precio sobre un neto de 430.58:')
    for pct in (0, 10, -15):
        print(f'  {pct:+d}%  ->  {aplicar_ajuste(430.58, pct)}')
