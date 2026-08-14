#!/usr/bin/env python3
"""
import_b2c_images.py — Sustituye las fotos de baja resolución del ERP por las
fotos reales (alta resolución) de la tienda B2C (masscob.com), cruzando por
CÓDIGO DE REFERENCIA (no por slug/nombre: el nombre en B2C puede estar en
otro orden de palabras — "Coat Maomi" en mayorista vs "Maomi Coat" en
masscob.com — pero el código va siempre en las tags de Shopify, ej. "W27/104A").

Fuente: el endpoint público de Shopify /products.json (paginado), que ya
trae título, tags, variantes (color/talla) y todas las imágenes del producto
en una sola llamada — no hace falta scrapear cada ficha.

Uso:
    python import_b2c_images.py [products.js]

Escribe products.js en el sitio, sustituyendo 'image'/'images' de cada
referencia que encuentre match; las que no matchean conservan la foto local
(banco de fotos del ERP) que ya tuvieran.
"""
import sys
import os
import re
import json
import time
import urllib.request

B2C_BASE = 'https://masscob.com'
CACHE_PATH = os.path.join(os.path.dirname(__file__), '.b2c_products_cache.json')
CODIGO_RE = re.compile(r'\b([A-Z]\d{2}/\d{3}[A-Z]{1,2})\b')


def _get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (masscob-b2b sync)'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))


def fetch_all_b2c_products(use_cache=True):
    """Descarga todo /products.json paginado. Devuelve la lista cruda de productos."""
    if use_cache and os.path.exists(CACHE_PATH):
        age = time.time() - os.path.getmtime(CACHE_PATH)
        if age < 3600:  # cache de 1h para no golpear la tienda en cada prueba
            return json.load(open(CACHE_PATH, encoding='utf-8'))

    productos = []
    page = 1
    while True:
        data = _get(f'{B2C_BASE}/products.json?limit=250&page={page}')
        batch = data.get('products', [])
        if not batch:
            break
        productos.extend(batch)
        page += 1
    json.dump(productos, open(CACHE_PATH, 'w', encoding='utf-8'), ensure_ascii=False)
    return productos


def _codigo_de_tags(tags):
    for t in tags:
        m = CODIGO_RE.search(t.upper())
        if m:
            return m.group(1)
    return None


def index_por_codigo(b2c_productos):
    """{codigo: {'handle', 'title', 'images': [src,...], 'colores_b2c': set(...)}}"""
    idx = {}
    for prod in b2c_productos:
        codigo = _codigo_de_tags(prod.get('tags', []))
        if not codigo:
            continue
        images = [im['src'] for im in sorted(prod.get('images', []), key=lambda i: i['position'])]
        colores = {v['option1'].upper() for v in prod.get('variants', []) if v.get('option1')}
        idx[codigo] = {'handle': prod['handle'], 'title': prod['title'], 'images': images, 'colores_b2c': colores}
    return idx


def _tokens(texto):
    return set(re.findall(r'[a-z]+', texto.lower()))


def _filename_tokens(src):
    base = src.split('/')[-1].split('?')[0]
    base = re.sub(r'\.(jpg|jpeg|png|webp)$', '', base, flags=re.IGNORECASE)
    return _tokens(base)


def asignar_fotos_por_color(colores_locales, imagenes):
    """
    Reparte las imágenes del producto B2C entre los colores locales según
    coincidencia de palabras en el nombre de archivo (ej. '..._red_2.jpg'
    -> color 'RED'). Las imágenes sin ninguna palabra de color reconocible
    van al color "por defecto" (el primero del producto local), que es el
    criterio que ya usa el banco de fotos del ERP para su '_default'.
    Devuelve {color: [src,...]}.
    """
    color_tokens = {c: _tokens(c) - {'de', 'la'} for c in colores_locales}
    reparto = {c: [] for c in colores_locales}
    sin_color = []
    for src in imagenes:
        ftoks = _filename_tokens(src)
        match = [c for c, toks in color_tokens.items() if toks and (toks & ftoks)]
        if len(match) == 1:
            reparto[match[0]].append(src)
        else:
            sin_color.append(src)  # 0 o >1 coincidencias: ambiguo, va al color por defecto
    if colores_locales:
        reparto[colores_locales[0]] = sin_color + reparto[colores_locales[0]]
    return reparto


def cruzar(catalogo, b2c_idx):
    """
    Muta 'catalogo' en sitio: sustituye 'image'/'images' cuando hay match B2C
    (usadas por la tienda) y deja 'imagesLocal' SIEMPRE con solo las fotos
    del banco propio (usadas por el panel admin al imprimir/exportar los
    informes de stock, que deben ir con nuestra foto de plana, no la de la
    web). Conserva las fotos locales (import_images.py) cuando no hay match.
    Devuelve resumen {con_b2c, sin_b2c: [codigos]}.
    """
    con_b2c = 0
    sin_b2c = []
    for p in catalogo:
        match = b2c_idx.get(p['codigo'])
        if not match or not match['images']:
            p['imagesLocal'] = dict(p.get('images') or {})
            sin_b2c.append(p['codigo'])
            continue
        colores_locales = list(p.get('colores', {}).keys())
        reparto = asignar_fotos_por_color(colores_locales, match['images'])
        # banco de fotos del ERP (rutas locales, nunca URL): filtra por si
        # products.js ya traía una pasada anterior de este mismo script, para
        # que correrlo varias veces sea seguro y no "adopte" una foto B2C
        # previa como si fuera la foto de plana local
        fotos_locales = {c: v for c, v in (p.get('images') or {}).items() if not str(v).startswith('http')}
        p['imagesLocal'] = dict(fotos_locales)

        # galería para la ficha: solo fotos B2C (mejor calidad que el banco
        # local, y ya sin la de plana repetida — la última foto B2C "limpia"
        # de fondo suele ser la misma toma que la de plana, pero en mejor resolución)
        gallery = {}
        for color in colores_locales:
            serie = list(reparto.get(color, []))
            if serie:
                gallery[color] = serie
        if gallery:
            p['gallery'] = gallery

        images = {}
        for color, srcs in reparto.items():
            if srcs:
                images[color] = srcs[0]
        if colores_locales and colores_locales[0] in images:
            images['_default'] = images[colores_locales[0]]
        elif match['images']:
            images['_default'] = match['images'][0]
        # conserva las fotos locales para colores que B2C no cubre (ej. color
        # descatalogado en la web pero aún vivo en el mayorista)
        for color, ruta in fotos_locales.items():
            images.setdefault(color, ruta)
        p['images'] = images
        p['image'] = images.get('_default') or next(iter(images.values()), p.get('image'))
        p['b2cHandle'] = match['handle']
        con_b2c += 1
    return {'con_b2c': con_b2c, 'sin_b2c': sin_b2c}


def main():
    products_js_path = sys.argv[1] if len(sys.argv) > 1 else 'products.js'
    src = open(products_js_path, encoding='utf-8').read()
    m = re.search(r'window\.MASSCOB_PRODUCTS\s*=\s*(\[.*?\]);\s*window\.MASSCOB_META\s*=\s*(\{.*?\});', src, re.S)
    if not m:
        sys.exit(f'No se pudo leer {products_js_path} (¿corriste build_products.py antes?)')
    catalogo = json.loads(m.group(1))
    meta = json.loads(m.group(2))

    print('Descargando catálogo público de masscob.com...')
    b2c_productos = fetch_all_b2c_products()
    print(f'  {len(b2c_productos)} productos leídos de {B2C_BASE}/products.json')
    b2c_idx = index_por_codigo(b2c_productos)
    print(f'  {len(b2c_idx)} con código de referencia reconocible')

    resumen = cruzar(catalogo, b2c_idx)

    js = (
        '// Generado por build_products.py + import_b2c_images.py — NO editar a mano.\n'
        'window.MASSCOB_PRODUCTS = '
        + json.dumps(catalogo, ensure_ascii=False, indent=2) + ';\n'
        'window.MASSCOB_META = '
        + json.dumps(meta, ensure_ascii=False, indent=2) + ';\n'
    )
    with open(products_js_path, 'w', encoding='utf-8') as f:
        f.write(js)

    print(f'\nFotos B2C cruzadas : {resumen["con_b2c"]} / {len(catalogo)}')
    print(f'Sin match en B2C   : {len(resumen["sin_b2c"])}  (conservan foto local del ERP)')
    for cod in resumen['sin_b2c']:
        print(f'   sin match -> {cod}')


if __name__ == '__main__':
    main()
