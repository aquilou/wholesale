#!/usr/bin/env python3
import json
from merge_sources import cargar_precios, merge, find_default_report

prods = json.load(open('catalog.json'))['products']
precios = cargar_precios(find_default_report('precioserp'))   # fichero real del ERP

print(f'Precios cargados del ERP: {len(precios)}')

# --- 1ª importación: stock + precios reales, sin imágenes todavía ---
r1 = merge(prods, precios)
print('1ª importación:', r1['resumen'])
print('Incompletos (faltan datos):')
for c in r1['incompletos']:
    print(f'   {c["codigo"]} {c["nombre"]} -> faltan {c["faltan"]}')

# --- Regla 1: reimportar stock NO pisa precio/imagen ya guardados ---
cod0 = prods[0]['codigo']
previo = {cod0: {'price': 555.0, 'category': 'GUARDADA', 'image': 'foto_guardada.jpg'}}
r2 = merge(prods, precios={}, previo=previo)
g = next(c for c in r2['catalogo'] if c['codigo'] == cod0)
assert g['price'] == 555.0, 'FALLO: el merge piso el precio previo'
assert g['image'] == 'foto_guardada.jpg', 'FALLO: el merge piso la imagen previa'
print('\nOK  regla 1: reimportar stock NO pisa precio/imagen ya guardados')

# --- Regla 2: producto sin precio en el ERP => incompleto ---
sin_precio = [c['codigo'] for c in r1['catalogo'] if c['price'] is None]
assert 'S26/405W' in sin_precio, 'FALLO: S26/405W deberia quedar sin precio'
print(f'OK  regla 2: {len(sin_precio)} producto(s) sin precio marcados incompletos -> {sin_precio}')

# --- Regla 3: precios sin stock => huerfanos reportados ---
assert r1['resumen']['huerfanos'] > 0, 'FALLO: deberian existir precios huerfanos'
print(f'OK  regla 3: {r1["resumen"]["huerfanos"]} precios huerfanos reportados (con precio pero sin stock)')

# --- Regla 4: la categoria (Familia del ERP) se propaga, cuando el informe la trae ---
# Se prueba con datos sintéticos: el informe de precios actualmente cargado
# (find_default_report) puede no incluir columna 'Familia' (ej. el "Informe
# de precios" solo trae WHOLESALE EUROPA), y eso es válido, no un fallo.
precios_con_familia = {cod0: {'price': 100.0, 'pvp': None, 'category': 'Abrigos'}}
r4 = merge(prods, precios_con_familia)
g4 = next(c for c in r4['catalogo'] if c['codigo'] == cod0)
assert g4['category'] == 'Abrigos', 'FALLO: la categoria no se propago desde Familia'
print(f'OK  regla 4: categoria propagada desde Familia cuando el informe la trae (ej. {cod0} -> {g4["category"]})')

print('\nTODOS LOS TESTS PASAN')