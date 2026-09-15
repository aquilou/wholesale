#!/usr/bin/env python3
"""
actualizar_catalogo.py — un solo comando para reimportar el Excel del ERP y
dejar todo en su sitio de una vez, en vez de acordarte de tres pasos sueltos.

Encadena, en este orden:
  1. build_products.py    -> products.js (stock + precios + fotos del banco ERP)
  2. import_b2c_images.py -> sustituye fotos por las de masscob.com donde haya match
  3. backend/seed_stock.py -> sincroniza la tabla `stock` (BD, la que de verdad
                              bloquea/permite pedidos) con el products.js nuevo

Uso (desde cualquier sitio, no hace falta estar en masscob-b2b/):
    python actualizar_catalogo.py

Busca stockmasscob.xlsx/.xls y precioserp.xlsx/.xls tanto en esta carpeta
(masscob-b2b/) como en la carpeta padre (B2B/) — da igual en cuál de las dos
dejes el Excel nuevo del ERP, coge el más reciente de los que encuentre.

Lo que este script NO hace (sigue siendo manual, a propósito): revisar el
resultado y subirlo a producción con git commit + push. products.js sí va
en el repo; catalog.json y los .xlsx/.xls están en .gitignore.
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)


def _find_report(prefix):
    candidatos = []
    for carpeta in (HERE, PARENT):
        for ext in ("xlsx", "xls"):
            candidatos += glob.glob(os.path.join(carpeta, f"{prefix}.{ext}"))
    if not candidatos:
        sys.exit(f"No se encontró '{prefix}.xlsx' ni '{prefix}.xls' en {HERE} ni en {PARENT}")
    return max(candidatos, key=os.path.getmtime)


def _run(cmd, cwd):
    print(f"\n$ {' '.join(cmd)}   (en {cwd})")
    subprocess.run(cmd, cwd=cwd, check=True)


def main():
    stock_path = _find_report("stockmasscob")
    price_path = _find_report("precioserp")
    print(f"Informe de stock  : {stock_path}")
    print(f"Informe de precios: {price_path}")

    _run([sys.executable, "build_products.py", stock_path, price_path], cwd=HERE)
    _run([sys.executable, "import_b2c_images.py"], cwd=HERE)
    _run([sys.executable, "seed_stock.py"], cwd=os.path.join(HERE, "backend"))

    print(
        "\nListo. products.js y el stock en vivo ya están sincronizados con el Excel.\n"
        "Falta el paso manual de siempre: revisar el resultado y hacer commit + push "
        "de products.js para que llegue a producción."
    )


if __name__ == "__main__":
    main()
