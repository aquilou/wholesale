#!/usr/bin/env python3
"""
Cruza el banco de fotos con el catálogo por código de referencia + color.

Estructura de carpetas esperada (una por colección, así se puede elegir
campaña sin tocar código — alcanza con agregar la carpeta):
    <fotos_root>/Fotos <CODIGO_COLECCION>/   ej. Fotos/Fotos W27, Fotos/Fotos S26

Convención de nombre de archivo dentro de cada carpeta:
    <REFERENCIA SIN BARRA><COLOR opcional>.<ext>
      W27104A.jpeg          -> foto "por defecto" de W27/104A
      W27104AOLIVE.jpeg     -> foto del color OLIVE de W27/104A

Uso como diagnóstico (no escribe nada, solo informa cruces y huérfanos):
    python import_images.py [fotos_root] [catalog.json]
"""
import sys
import os
import re
import json

from categorias import derivar_coleccion

_IMG_EXT = ('.jpg', '.jpeg', '.png', '.webp')


def _clean_stem(stem):
    """Normaliza basura típica del banco de fotos: '-copia', doble extensión, puntos/espacios de más."""
    stem = re.sub(r'-copia\d*$', '', stem, flags=re.IGNORECASE)
    for ext in _IMG_EXT:
        if stem.lower().endswith(ext):
            stem = stem[:-len(ext)]
    return stem.strip(' .')


def _relpath(*parts):
    """Ruta relativa con '/' (para usar como src de <img>, sin importar el SO)."""
    return '/'.join(parts)


def escanear_carpeta(carpeta, codigos_conocidos):
    """
    Lee una carpeta de fotos de UNA colección.
    En vez de adivinar dónde termina el código y empieza el color con una
    regex (el sufijo del código puede tener 1 o 2 letras — '503G' vs
    '312KN' — así que una regex genérica confunde 'G' + 'RED' con 'GR' + 'ED'),
    ancla cada archivo contra los códigos REALES del catálogo para esa
    colección: el más largo que sea prefijo del nombre de archivo gana, y el
    resto del nombre es el color.
    Devuelve {codigo: {'_default': ruta, 'COLOR': ruta, ...}}.
    """
    mapa = {}
    if not os.path.isdir(carpeta):
        return mapa
    # más largos primero: evita que un código corto capture de más si otro
    # código de la misma colección lo tiene como prefijo
    candidatos = sorted(codigos_conocidos, key=len, reverse=True)
    for nombre in sorted(os.listdir(carpeta)):
        base, ext = os.path.splitext(nombre)
        if ext.lower() not in _IMG_EXT:
            continue
        stem = _clean_stem(base).upper()
        cod_sin_barra = next((c for c in candidatos if stem.startswith(c)), None)
        if cod_sin_barra is None:
            continue
        codigo = cod_sin_barra[:3] + '/' + cod_sin_barra[3:]
        color = stem[len(cod_sin_barra):].strip(' .') or '_default'
        ruta = _relpath(carpeta.replace(os.sep, '/'), nombre)
        mapa.setdefault(codigo, {})
        mapa[codigo].setdefault(color, ruta)  # el primero gana si hay duplicados/typos
    return mapa


def elegir_imagen(producto, fotos_codigo):
    """
    Elige LA foto principal de la ficha entre las disponibles para un código:
    prioriza el color que tiene stock; si no, la foto "por defecto" del
    banco; si no, la primera que haya.
    """
    if not fotos_codigo:
        return None
    for color in producto.get('colores', {}):
        if color.upper() in fotos_codigo:
            return fotos_codigo[color.upper()]
    if '_default' in fotos_codigo:
        return fotos_codigo['_default']
    return next(iter(fotos_codigo.values()))


def importar(catalogo, fotos_root):
    """
    Muta 'catalogo' en sitio: agrega a cada producto
      image  -> foto principal (string) o None
      images -> {color: ruta} con TODAS las fotos encontradas para ese código
                (queda disponible aunque hoy la ficha solo muestre 'image')
    La colección de cada producto se resuelve por su propio código
    (derivar_coleccion), así que conviven varias campañas sin configurar nada.
    Devuelve un resumen {con_foto, sin_foto: [codigos]}.
    """
    codigos_por_coleccion = {}
    for p in catalogo:
        col_codigo, _ = derivar_coleccion(p['codigo'])
        if col_codigo:
            codigos_por_coleccion.setdefault(col_codigo, set()).add(p['codigo'].replace('/', '').upper())

    cache_por_coleccion = {}
    con_foto = 0
    sin_foto = []
    for p in catalogo:
        col_codigo, _ = derivar_coleccion(p['codigo'])
        if col_codigo is None:
            sin_foto.append(p['codigo'])
            continue
        if col_codigo not in cache_por_coleccion:
            carpeta = os.path.join(fotos_root, f'Fotos {col_codigo}')
            cache_por_coleccion[col_codigo] = escanear_carpeta(carpeta, codigos_por_coleccion[col_codigo])
        fotos = cache_por_coleccion[col_codigo].get(p['codigo'])
        imagen = elegir_imagen(p, fotos)
        p['image'] = imagen
        p['images'] = fotos or {}
        if imagen:
            con_foto += 1
        else:
            sin_foto.append(p['codigo'])
    return {'con_foto': con_foto, 'sin_foto': sin_foto}


if __name__ == '__main__':
    fotos_root = sys.argv[1] if len(sys.argv) > 1 else os.path.join('..', 'Fotos')
    catalog_path = sys.argv[2] if len(sys.argv) > 2 else 'catalog.json'

    prods = json.load(open(catalog_path, encoding='utf-8'))['products']
    resumen = importar(prods, fotos_root)

    print(f'Fotos root: {os.path.abspath(fotos_root)}')
    print(f'Productos con foto : {resumen["con_foto"]} / {len(prods)}')
    print(f'Productos sin foto : {len(resumen["sin_foto"])}')
    for cod in resumen['sin_foto']:
        print(f'   sin foto -> {cod}')
