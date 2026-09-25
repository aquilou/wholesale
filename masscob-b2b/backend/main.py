"""
MASSCOB Wholesale API — login real + ficha de cliente + pedidos.
Login en sí lo hace Supabase Auth (desde tienda/index.html); esta API solo
valida el token resultante y sirve los datos que dependen de negocio.

Correr en local:
    uvicorn main:app --reload
"""
import base64
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from html import escape as html_escape
from io import BytesIO
from typing import List, Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fpdf import FPDF
from PIL import Image
from pydantic import BaseModel

from auth import get_current_client, require_admin, require_cron
from config import (
    RESEND_API_KEY,
    RESEND_FROM_EMAIL,
    STORE_LOGIN_URL,
    SUPABASE_SERVICE_ROLE_KEY,
    SUPABASE_URL,
)
from db import get_conn

app = FastAPI(title="MASSCOB Wholesale API")

app.add_middleware(
    CORSMiddleware,
    # En producción (Vercel) frontend y backend son el mismo origen, así que
    # el navegador ni aplica CORS ahí. Esto es solo para poder seguir
    # probando en local con la tienda servida aparte del backend (:5500 vs
    # :8001, orígenes distintos).
    allow_origins=["http://127.0.0.1:5500", "http://localhost:5500"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_CLIENTE_CAMPOS = ["nombre_comercial", "razon_social", "cif", "direccion", "pais", "agente"]

# Facturación y envío van por el ERP, no por aquí — solo estos 3 estados.
ESTADOS_PEDIDO = {"PENDIENTE", "ACEPTADO", "ANULADO"}


# ---- notificaciones de pedido por email (resend.com) ----
# Best-effort a propósito: un fallo aquí (Resend caído, dominio sin
# verificar, clave que falta...) no debe tumbar la creación del pedido ni
# el cambio de estado, que ya se guardaron en la base de datos.
def _enviar_email(
    destinatarios: List[str], asunto: str, html: str, adjuntos: Optional[List[dict]] = None
) -> bool:
    return _enviar_email_detalle(destinatarios, asunto, html, adjuntos)[0]


def _enviar_email_detalle(
    destinatarios: List[str], asunto: str, html: str, adjuntos: Optional[List[dict]] = None
) -> tuple:
    """Igual que _enviar_email pero devuelve (ok, motivo_del_fallo) — para
    cuando el panel tiene que enseñarle al admin por qué no salió un email."""
    if not RESEND_API_KEY:
        return False, "Falta RESEND_API_KEY en el backend"
    if not destinatarios:
        return False, "Sin destinatarios"
    payload = {"from": RESEND_FROM_EMAIL, "to": destinatarios, "subject": asunto, "html": html}
    if adjuntos:
        payload["attachments"] = adjuntos
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            # Sin esto, Cloudflare (delante de api.resend.com) bloquea la
            # petición con un 403 (error 1010): el User-Agent por defecto de
            # urllib ("Python-urllib/3.x") coincide con firmas de bot.
            "User-Agent": "masscob-b2b-backend/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as res:
            res.read()
        return True, None
    except urllib.error.HTTPError as e:
        # El motivo real (dominio no verificado, cuenta restringida, etc.)
        # va en el cuerpo de la respuesta, no en el código de estado — sin
        # esto un 403 no dice nada útil en los logs.
        body = e.read().decode(errors="replace")
        print(f"[email] no se pudo enviar {asunto!r} a {destinatarios}: HTTP {e.code} {body}")
        try:
            motivo = json.loads(body).get("message") or body
        except Exception:
            motivo = body
        return False, f"HTTP {e.code}: {motivo}"
    except Exception as e:
        print(f"[email] no se pudo enviar {asunto!r} a {destinatarios}: {e!r}")
        return False, repr(e)


def _notif_emails_equipo(cur) -> List[str]:
    cur.execute("select email from notif_emails order by email")
    return [row[0] for row in cur.fetchall()]


def _email_cliente(cur, cliente_id: str) -> Optional[str]:
    cur.execute("select email from auth.users where id = %s", (cliente_id,))
    row = cur.fetchone()
    return row[0] if row else None


# ---- fotos del pedido en el PDF: mismo banco de fotos del ERP (no la de la
# web) que ya usa el botón "Descargar PDF" del panel admin, para que el PDF
# del email tenga la misma calidad — ver buildOrderItemRows() en
# "Panel Admin MASSCOB.dc.html".
_catalogo_fotos_cache: dict = {}


def _catalogo_fotos(base_url: str) -> dict:
    """codigo -> producto (con imagesLocal/image), leído de products.js tal
    cual lo sirve el sitio — no vive en la base de datos, lo genera
    build_products.py. Se cachea en memoria del proceso: si falla, un
    pedido no debe quedarse sin avisar por un problema de fotos."""
    if base_url in _catalogo_fotos_cache:
        return _catalogo_fotos_cache[base_url]
    catalogo = {}
    try:
        req = urllib.request.Request(
            base_url + "/masscob-b2b/products.js",
            headers={"User-Agent": "masscob-b2b-backend/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            raw = res.read().decode("utf-8")
        inicio = raw.index("[")
        fin = raw.index("];") + 1
        catalogo = {p["codigo"]: p for p in json.loads(raw[inicio:fin])}
    except Exception as e:
        print(f"[pdf] no se pudo cargar products.js para las fotos: {e!r}")
    _catalogo_fotos_cache[base_url] = catalogo
    return catalogo


def _imagen_item(base_url: str, codigo: str, color: str) -> Optional[str]:
    prod = _catalogo_fotos(base_url).get(codigo)
    if not prod:
        return None
    local = prod.get("imagesLocal") or {}
    rel = local.get(color) or local.get(str(color).upper()) or local.get("_default")
    if rel:
        # rutas tipo "../Fotos/Fotos W27/W27100T.jpeg", relativas a
        # masscob-b2b/ (una carpeta por encima de Fotos/, que se sirve como
        # estático desde la raíz del sitio) — de ahí en adelante hay que
        # url-encodearla (los nombres de fichero llevan espacios).
        idx = rel.find("Fotos/")
        if idx != -1:
            return base_url + "/" + quote(rel[idx:], safe="/")
    return prod.get("image")


_ESTADO_PDF_LABEL = {
    "PENDIENTE": "PENDIENTE DE CONFIRMACION",
    "ACEPTADO": "ACEPTADO",
    "ANULADO": "ANULADO",
}


def _pedido_pdf(
    referencia: str, cliente_email: str, fecha_iso: str, items: List[dict], total: float,
    estado: str = "PENDIENTE",
) -> bytes:
    # Fuentes core de FPDF (Helvetica) van en latin-1, no soportan "€" —
    # se usa "EUR" en el PDF en vez del símbolo para no arriesgar un fallo
    # de codificación con nombres/colores con acentos.
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, "MASSCOB", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Pedido {referencia}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Cliente: {cliente_email}", new_x="LMARGIN", new_y="NEXT")
    estado_label = _ESTADO_PDF_LABEL.get(estado, estado)
    pdf.cell(0, 5, f"Fecha {fecha_iso} - Estado {estado_label}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 6, "RESUMEN DE PEDIDO", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    left = pdf.l_margin
    col_foto, col_prod, col_color, col_talla, col_cant, col_precio, col_subtotal = (
        20, 55, 25, 18, 15, 27, 27,
    )
    cols = [col_foto, col_prod, col_color, col_talla, col_cant, col_precio, col_subtotal]

    headers = ["", "PRODUCTO", "COLOR", "TALLA", "CANT.", "PRECIO", "SUBTOTAL"]
    header_aligns = ["L", "L", "L", "L", "C", "C", "C"]
    pdf.set_font("Helvetica", "B", 8)
    x, y = left, pdf.get_y()
    for w, h, al in zip(cols, headers, header_aligns):
        pdf.set_xy(x, y)
        pdf.cell(w, 6, h, border="B", align=al)
        x += w
    pdf.set_y(y + 7)

    row_h = 18
    for it in items:
        if pdf.get_y() + row_h > pdf.page_break_trigger:
            pdf.add_page()
        y = pdf.get_y()
        x = left

        pdf.set_fill_color(247, 247, 244)
        pdf.rect(x, y, col_foto, row_h, style="F")
        if it.get("imagen_url"):
            try:
                pad = 2
                box_w, box_h = col_foto - 2 * pad, row_h - 2 * pad
                req_img = urllib.request.Request(
                    it["imagen_url"], headers={"User-Agent": "masscob-b2b-backend/1.0"}
                )
                with urllib.request.urlopen(req_img, timeout=8) as res_img:
                    img = Image.open(BytesIO(res_img.read()))
                # "contain": encajar sin deformar (antes se forzaba w y h
                # fijos y la foto salía achatada) y centrar en la caja.
                escala = min(box_w / img.width, box_h / img.height)
                w_img, h_img = img.width * escala, img.height * escala
                pdf.image(
                    img, x=x + pad + (box_w - w_img) / 2, y=y + pad + (box_h - h_img) / 2,
                    w=w_img, h=h_img,
                )
            except Exception as e:
                print(f"[pdf] no se pudo cargar la foto de {it['codigo']}: {e!r}")
        x += col_foto

        pdf.set_xy(x, y + 2)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(col_prod, 4.5, it["nombre"], align="L")
        pdf.set_xy(x, pdf.get_y())
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(150, 150, 150)
        pdf.cell(col_prod, 4, it["codigo"])
        pdf.set_text_color(0, 0, 0)
        x += col_prod

        pdf.set_font("Helvetica", "", 9)
        vals = [
            it["color"], it["talla"], str(it["cantidad"]),
            f"{it['precio_unit']:.2f} EUR", f"{it['cantidad'] * it['precio_unit']:.2f} EUR",
        ]
        for w, val, al in zip(
            [col_color, col_talla, col_cant, col_precio, col_subtotal],
            vals, ["L", "L", "C", "C", "C"],
        ):
            pdf.set_xy(x, y + row_h / 2 - 2.5)
            pdf.cell(w, 5, val, align=al)
            x += w

        pdf.set_draw_color(230, 230, 225)
        pdf.line(left, y + row_h, left + sum(cols), y + row_h)
        pdf.set_y(y + row_h)

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(sum(cols[:-1]), 8, "Total", border="T")
    pdf.cell(cols[-1], 8, f"{total:.2f} EUR", border="T", align="R")

    return bytes(pdf.output())


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/me")
def me(client: dict = Depends(get_current_client)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"select {', '.join(_CLIENTE_CAMPOS)} from clientes where id = %s",
                (client["user_id"],),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(
            404, "No hay ficha de cliente asociada a esta cuenta. Contacta con tu agente."
        )
    return dict(zip(_CLIENTE_CAMPOS, row))


# ---- pedidos ----

class PedidoItemIn(BaseModel):
    codigo: str
    nombre: str
    color: str
    talla: str
    cantidad: int
    precio_unit: float


class PedidoIn(BaseModel):
    items: List[PedidoItemIn]
    total: float
    nota: Optional[str] = None


class EstadoIn(BaseModel):
    estado: str


def _fetch_items(cur, pedido_id):
    cur.execute(
        "select codigo, nombre, color, talla, cantidad, precio_unit "
        "from pedido_items where pedido_id = %s",
        (pedido_id,),
    )
    return [
        {"codigo": codigo, "nombre": nombre, "color": color, "talla": talla,
         "cantidad": cantidad, "precio_unit": float(precio_unit)}
        for codigo, nombre, color, talla, cantidad, precio_unit in cur.fetchall()
    ]


@app.post("/pedidos")
def crear_pedido(pedido: PedidoIn, request: Request, client: dict = Depends(get_current_client)):
    if not pedido.items:
        raise HTTPException(400, "El pedido no tiene items")
    referencia = "PED-" + str(int(time.time() * 1000))[-8:]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "insert into pedidos (cliente_id, referencia, nota, total, estado) "
                "values (%s, %s, %s, %s, 'PENDIENTE') "
                "returning id, referencia, estado, total, nota, created_at",
                (client["user_id"], referencia, pedido.nota, pedido.total),
            )
            pedido_id, referencia, estado, total, nota, created_at = cur.fetchone()
            for item in pedido.items:
                cur.execute(
                    "insert into pedido_items "
                    "(pedido_id, codigo, nombre, color, talla, cantidad, precio_unit) "
                    "values (%s, %s, %s, %s, %s, %s, %s)",
                    (pedido_id, item.codigo, item.nombre, item.color, item.talla,
                     item.cantidad, item.precio_unit),
                )
                # el stock se reserva ya en PENDIENTE (no al ACEPTAR) para que
                # dos pedidos no puedan pisarse la misma unidad mientras el
                # primero espera revisión
                cur.execute(
                    "update stock set cantidad = cantidad - %s "
                    "where codigo=%s and color=%s and talla=%s and cantidad >= %s "
                    "returning cantidad",
                    (item.cantidad, item.codigo, item.color, item.talla, item.cantidad),
                )
                if cur.fetchone() is None:
                    raise HTTPException(
                        409,
                        f"Stock insuficiente para {item.codigo} / {item.color} / {item.talla}",
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # El pedido ya está guardado en este punto (commit hecho arriba). Un
    # fallo desde aquí en adelante (Resend caído, notif_emails inexistente,
    # etc.) es best-effort y NUNCA debe convertirse en un 500 de cara al
    # cliente — si no, el pedido "falla" en la pantalla pero ya existe en
    # la base de datos, y cada reintento crea uno duplicado.
    try:
        adjuntos = None
        base_url = str(request.base_url).rstrip("/")
        items_pdf = []
        for item in pedido.items:
            try:
                imagen_url = _imagen_item(base_url, item.codigo, item.color)
            except Exception:
                imagen_url = None
            items_pdf.append({
                "codigo": item.codigo, "nombre": item.nombre, "color": item.color,
                "talla": item.talla, "cantidad": item.cantidad, "precio_unit": item.precio_unit,
                "imagen_url": imagen_url,
            })
        try:
            pdf_bytes = _pedido_pdf(
                referencia, client.get("email", "—"), created_at.isoformat(),
                items_pdf, float(total), estado=estado,
            )
            adjuntos = [{
                "filename": f"pedido-{referencia}.pdf",
                "content": base64.b64encode(pdf_bytes).decode(),
            }]
        except Exception as e:
            # El PDF es un extra sobre el aviso por email — si falla al
            # generarlo, mejor mandar el correo sin adjunto que no mandar
            # ningún aviso.
            print(f"[pdf] no se pudo generar el PDF del pedido {referencia}: {e!r}")

        cliente_email = client.get("email") or "—"
        if client.get("email"):
            _enviar_email(
                [client["email"]],
                f"Hemos recibido tu pedido {referencia}",
                _html_pedido(referencia, cliente_email, items_pdf, float(total), para_equipo=False),
                adjuntos=adjuntos,
            )
        conn2 = get_conn()
        try:
            with conn2.cursor() as cur:
                equipo = _notif_emails_equipo(cur)
        finally:
            conn2.close()
        if equipo:
            _enviar_email(
                equipo,
                f"Nuevo pedido {referencia}",
                _html_pedido(referencia, cliente_email, items_pdf, float(total), para_equipo=True),
                adjuntos=adjuntos,
            )
    except Exception as e:
        print(f"[email] aviso de pedido {referencia} no enviado: {e!r}")

    return {
        "id": pedido_id, "referencia": referencia, "estado": estado,
        "total": float(total), "nota": nota, "fecha": created_at.isoformat(),
        "items": [item.model_dump() for item in pedido.items],
    }


@app.get("/pedidos")
def listar_pedidos(client: dict = Depends(get_current_client)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select id, referencia, estado, total, nota, created_at "
                "from pedidos where cliente_id = %s order by created_at desc",
                (client["user_id"],),
            )
            pedidos = cur.fetchall()
            result = []
            for pedido_id, referencia, estado, total, nota, created_at in pedidos:
                result.append({
                    "id": pedido_id, "referencia": referencia, "estado": estado,
                    "total": float(total), "nota": nota, "fecha": created_at.isoformat(),
                    "items": _fetch_items(cur, pedido_id),
                })
    finally:
        conn.close()
    return result


@app.get("/pedidos/{pedido_id}/pdf")
def descargar_pedido_pdf(
    pedido_id: int, request: Request, client: dict = Depends(get_current_client)
):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select referencia, estado, total, created_at, cliente_id "
                "from pedidos where id = %s",
                (pedido_id,),
            )
            row = cur.fetchone()
            # 404 también si el pedido es de otro cliente — no distinguir
            # "no existe" de "no es tuyo" para no filtrar qué ids existen.
            if not row or str(row[4]) != str(client["user_id"]):
                raise HTTPException(404, "Pedido no encontrado")
            referencia, estado, total, created_at, _cliente_id = row
            items = _fetch_items(cur, pedido_id)
    finally:
        conn.close()

    base_url = str(request.base_url).rstrip("/")
    items_pdf = [
        dict(item, imagen_url=_imagen_item(base_url, item["codigo"], item["color"]))
        for item in items
    ]
    pdf_bytes = _pedido_pdf(
        referencia, client.get("email", "—"), created_at.isoformat(),
        items_pdf, float(total), estado=estado,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="pedido-{referencia}.pdf"'},
    )


@app.get("/admin/pedidos")
def listar_pedidos_admin(_: None = Depends(require_admin)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select p.id, p.referencia, p.estado, p.total, p.nota, p.created_at, "
                "c.nombre_comercial "
                "from pedidos p join clientes c on c.id = p.cliente_id "
                "order by p.created_at desc"
            )
            pedidos = cur.fetchall()
            result = []
            for pedido_id, referencia, estado, total, nota, created_at, cliente_nombre in pedidos:
                result.append({
                    "id": pedido_id, "referencia": referencia, "estado": estado,
                    "total": float(total), "nota": nota, "fecha": created_at.isoformat(),
                    "cliente_nombre": cliente_nombre,
                    "items": _fetch_items(cur, pedido_id),
                })
    finally:
        conn.close()
    return result


@app.patch("/admin/pedidos/{pedido_id}/estado")
def actualizar_estado_pedido(pedido_id: int, body: EstadoIn, _: None = Depends(require_admin)):
    if body.estado not in ESTADOS_PEDIDO:
        raise HTTPException(400, f"Estado inválido: {body.estado}")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # bloquea la fila del pedido para toda la transacción: evita que
            # dos PATCH concurrentes sobre el mismo pedido descuenten stock
            # dos veces (o restituyan dos veces)
            cur.execute(
                "select estado, cliente_id, referencia from pedidos where id = %s for update",
                (pedido_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Pedido no encontrado")
            estado_actual, cliente_id, referencia = row

            if estado_actual != body.estado:
                # el stock ya se reservó al crear el pedido (PENDIENTE), así
                # que Pendiente<->Aceptado no lo vuelve a tocar. Solo entrar
                # o salir de ANULADO mueve stock: anular lo devuelve, y
                # reabrir un pedido anulado (hacia Pendiente o Aceptado) lo
                # vuelve a reservar (puede fallar si ya no hay unidades).
                entra_en_anulado = body.estado == "ANULADO"
                sale_de_anulado = estado_actual == "ANULADO"
                if entra_en_anulado or sale_de_anulado:
                    items = _fetch_items(cur, pedido_id)
                    for item in items:
                        if entra_en_anulado:
                            cur.execute(
                                "update stock set cantidad = cantidad + %s "
                                "where codigo=%s and color=%s and talla=%s",
                                (item["cantidad"], item["codigo"], item["color"], item["talla"]),
                            )
                        else:
                            cur.execute(
                                "update stock set cantidad = cantidad - %s "
                                "where codigo=%s and color=%s and talla=%s and cantidad >= %s "
                                "returning cantidad",
                                (item["cantidad"], item["codigo"], item["color"], item["talla"], item["cantidad"]),
                            )
                            if cur.fetchone() is None:
                                raise HTTPException(
                                    409,
                                    f"Stock insuficiente para {item['codigo']} / {item['color']} / {item['talla']}",
                                )

            cur.execute(
                "update pedidos set estado = %s where id = %s",
                (body.estado, pedido_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if body.estado != estado_actual and body.estado in ("ACEPTADO", "ANULADO"):
        conn2 = get_conn()
        try:
            with conn2.cursor() as cur:
                email_cliente = _email_cliente(cur, cliente_id)
        finally:
            conn2.close()
        if email_cliente:
            if body.estado == "ACEPTADO":
                asunto, mensaje = f"Pedido {referencia} aceptado", "Tu pedido ha sido aceptado."
            else:
                asunto, mensaje = f"Pedido {referencia} anulado", "Tu pedido ha sido anulado."
            _enviar_email(
                [email_cliente], asunto,
                f"<p>Hola,</p><p>{mensaje}</p><p>Referencia: <strong>{referencia}</strong></p>",
            )

    return {"id": pedido_id, "estado": body.estado}


# ---- stock ----

def _fetch_stock(cur):
    cur.execute("select codigo, color, talla, cantidad from stock")
    out = {}
    for codigo, color, talla, cantidad in cur.fetchall():
        out.setdefault(codigo, {}).setdefault(color, {})[talla] = cantidad
    return out


@app.get("/admin/stock")
def listar_stock_admin(_: None = Depends(require_admin)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            result = _fetch_stock(cur)
    finally:
        conn.close()
    return result


@app.get("/stock")
def listar_stock(client: dict = Depends(get_current_client)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            result = _fetch_stock(cur)
    finally:
        conn.close()
    return result


# ---- stocklist por email ----
#
# El panel manda las filas TAL CUAL las pinta en la vista Tabla (mismos
# filtros de colección/categoría/búsqueda y mismo formato "Con unidades" /
# "Sin unidades") y aquí se maqueta el PDF imitando el de "Exportar PDF"
# (que es el diálogo de impresión del navegador, imposible de reutilizar en
# el servidor) y se envía por Resend, un email por destinatario para que
# ningún cliente vea a quién más se le ha mandado.

class StocklistTallaIn(BaseModel):
    size: str
    qty: int
    display: str = ""  # "" (sin stock), "3" (con unidades) o "✓" (sin unidades)


class StocklistRowIn(BaseModel):
    codigo: str
    name: str
    color: str
    dot: str = "#b8b3a8"
    stock: int
    estado: Optional[str] = None
    imagen: Optional[str] = None
    tallas: List[StocklistTallaIn] = []


class StocklistEnvioIn(BaseModel):
    emails: List[str]
    asunto: Optional[str] = None
    mensaje: Optional[str] = None
    titulo: str
    subtitulo: str = ""
    fecha: str = ""
    formato: str = "detallado"  # "detallado" (con unidades) | "cliente" (sin unidades)
    logo: Optional[str] = None
    rows: List[StocklistRowIn]


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Hosts de los que el backend acepta descargar fotos/logo para el PDF: el
# propio sitio (Fotos/ y el logo), el CDN de masscob.com y local para
# pruebas — nada más, para que el endpoint no sirva de proxy a cualquier URL.
_HOSTS_IMAGEN_OK = {"cdn.shopify.com", "localhost", "127.0.0.1"}


def _latin1(s: str) -> str:
    """Las fuentes core de FPDF (Helvetica/Courier) solo cubren latin-1."""
    s = str(s or "").replace("—", "-").replace("–", "-").replace("’", "'")
    return s.encode("latin-1", "replace").decode("latin-1")


def _hex_rgb(h: str) -> tuple:
    h = str(h or "").lstrip("#")
    if len(h) != 6:
        return (184, 179, 168)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (184, 179, 168)


def _cargar_imagen(src: Optional[str], host_propio: str, lado_max: int = 240):
    """URL (o data: URL) -> PIL.Image en RGB, reducida. None si no se puede."""
    if not src:
        return None
    try:
        if src.startswith("data:"):
            raw = base64.b64decode(src.split(",", 1)[1])
        else:
            host = urllib.parse.urlparse(src).hostname or ""
            if host != host_propio and host not in _HOSTS_IMAGEN_OK:
                return None
            req = urllib.request.Request(src, headers={"User-Agent": "masscob-b2b-backend/1.0"})
            with urllib.request.urlopen(req, timeout=8) as res:
                raw = res.read()
        img = Image.open(BytesIO(raw))
        if img.mode in ("RGBA", "LA", "P"):
            fondo = Image.new("RGB", img.size, (255, 255, 255))
            img = img.convert("RGBA")
            fondo.paste(img, mask=img.split()[-1])
            img = fondo
        else:
            img = img.convert("RGB")
        img.thumbnail((lado_max, lado_max))
        return img
    except Exception as e:
        print(f"[stocklist] no se pudo cargar la imagen {src[:80]!r}: {e!r}")
        return None


def _recorte_cover(img, ratio: float):
    """Recorta al centro para llenar una caja de proporción ancho/alto =
    ratio sin deformar (object-fit: cover, como la miniatura del panel)."""
    w, h = img.size
    if w / h > ratio:
        nw = int(h * ratio)
        return img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    nh = int(w / ratio)
    return img.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))


class _StocklistPDF(FPDF):
    INK = (22, 22, 22)
    GRIS = (107, 107, 100)
    LINEA = (237, 237, 234)
    COLS = (16, 46, 56, 125, 30)  # foto, item, nombre, tallas, stock/estado = 273mm

    def __init__(self, datos: StocklistEnvioIn, logo_img):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.datos = datos
        self.logo_img = logo_img
        self.set_margins(12, 12, 12)
        self.set_auto_page_break(auto=False)

    def header(self):
        d = self.datos
        x0, y0, ancho = self.l_margin, 12, self.w - self.l_margin - self.r_margin
        alto = 20
        self.set_draw_color(*self.LINEA)
        self.set_line_width(0.3)
        self.rect(x0, y0, ancho, alto)
        # logo
        logo_w = 52
        self.line(x0 + logo_w, y0, x0 + logo_w, y0 + alto)
        if self.logo_img is not None:
            iw, ih = self.logo_img.size
            caja_w, caja_h = logo_w - 10, alto - 8
            esc = min(caja_w / iw, caja_h / ih)
            w, h = iw * esc, ih * esc
            self.image(self.logo_img, x=x0 + (logo_w - w) / 2, y=y0 + (alto - h) / 2, w=w, h=h)
        else:
            self.set_xy(x0, y0 + 7)
            self.set_font("Helvetica", "B", 15)
            self.set_text_color(*self.INK)
            self.cell(logo_w, 6, "MASSCOB", align="C")
        # título con barra negra
        self.set_fill_color(*self.INK)
        self.rect(x0 + logo_w + 8, y0 + 6.5, 1.2, 7, style="F")
        self.set_xy(x0 + logo_w + 12, y0 + 6.5)
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(*self.INK)
        self.cell(120, 7, _latin1(d.titulo))
        # FECHA | valor
        fw1, fw2, fh = 18, 30, 9
        fx = x0 + ancho - 8 - fw1 - fw2
        fy = y0 + (alto - fh) / 2
        self.set_fill_color(250, 250, 248)
        self.rect(fx, fy, fw1, fh, style="DF")
        self.rect(fx + fw1, fy, fw2, fh)
        self.set_xy(fx, fy)
        self.set_font("Helvetica", "B", 7)
        self.cell(fw1, fh, "FECHA", align="C")
        self.set_font("Helvetica", "", 8.5)
        self.cell(fw2, fh, _latin1(d.fecha), align="C")
        # subtítulo
        self.set_xy(x0, y0 + alto + 2.5)
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(*self.GRIS)
        self.cell(ancho, 5, _latin1(d.subtitulo), align="C")
        # cabecera de columnas
        y = y0 + alto + 10
        self.set_font("Helvetica", "B", 6.5)
        self.set_text_color(*self.INK)
        heads = ["", "ITEM", "NOMBRE", "TALLA / SIZE", "STOCK" if d.formato != "cliente" else "ESTADO"]
        x = x0
        for w, t in zip(self.COLS, heads):
            self.set_xy(x + (3 if t else 0), y)
            self.cell(w - 6 if t else w, 7, t, align="R" if t in ("STOCK", "ESTADO") else "L")
            x += w
        self.line(x0, y + 7, x0 + ancho, y + 7)
        self.set_y(y + 7)

    def footer(self):
        self.set_y(-9)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(167, 167, 160)
        self.cell(0, 4, f"{self.page_no()} / {{nb}}", align="R")

    def fila(self, r: StocklistRowIn, img):
        alto = 15.5
        if self.get_y() + alto > self.h - 12:
            self.add_page()
        x0, y = self.l_margin, self.get_y()
        c_foto, c_item, c_nombre, c_tallas, c_stock = self.COLS

        # foto (cover) sobre fondo gris claro
        fw, fh = 9.5, 12
        fx, fy = x0 + (c_foto - fw) / 2 + 1, y + (alto - fh) / 2
        self.set_fill_color(242, 242, 239)
        self.rect(fx, fy, fw, fh, style="F")
        if img is not None:
            self.image(_recorte_cover(img, fw / fh), x=fx, y=fy, w=fw, h=fh)

        # item: código + punto de color + nombre de color
        x = x0 + c_foto
        self.set_xy(x + 3, y + alto / 2 - 4)
        self.set_font("Courier", "B", 8.5)
        self.set_text_color(*self.INK)
        self.cell(c_item - 6, 4, _latin1(r.codigo))
        self.set_fill_color(*_hex_rgb(r.dot))
        self.set_draw_color(210, 210, 205)
        self.ellipse(x + 3, y + alto / 2 + 1.1, 2, 2, style="DF")
        self.set_xy(x + 6, y + alto / 2 + 0.3)
        self.set_font("Helvetica", "B", 7)
        self.set_text_color(*self.GRIS)
        self.cell(c_item - 9, 3.6, _latin1(r.color))

        # nombre
        x += c_item
        self.set_xy(x + 3, y + alto / 2 - 2)
        self.set_font("Helvetica", "", 8.5)
        self.set_text_color(*self.INK)
        self.cell(c_nombre - 6, 4, _latin1(r.name))

        # rejilla de tallas: etiqueta arriba, casilla abajo
        x += c_nombre
        bw, bh, gap = 7.5, 5.2, 3.2
        bx = x + 3
        for t in r.tallas:
            self.set_xy(bx - 1, y + 2.6)
            self.set_font("Helvetica", "B", 6.3)
            self.set_text_color(*self.INK)
            self.cell(bw + 2, 3, _latin1(t.size), align="C")
            by = y + 6.6
            con = t.qty > 0
            self.set_draw_color(*((221, 216, 207) if con else self.LINEA))
            self.set_fill_color(*((250, 250, 248) if con else (255, 255, 255)))
            self.rect(bx, by, bw, bh, style="DF")
            if con and t.display and t.display != "✓":
                self.set_xy(bx, by)
                self.set_font("Helvetica", "B", 7.5)
                self.cell(bw, bh, _latin1(t.display), align="C")
            elif con and t.display == "✓":
                self.set_draw_color(*self.INK)
                self.set_line_width(0.35)
                cx, cy = bx + bw / 2, by + bh / 2
                self.line(cx - 1.4, cy, cx - 0.4, cy + 1)
                self.line(cx - 0.4, cy + 1, cx + 1.5, cy - 1.1)
                self.set_line_width(0.3)
            bx += bw + gap
            if bx + bw > x + c_tallas:
                break

        # stock / estado
        x += c_tallas
        if self.datos.formato == "cliente":
            estado = _latin1(r.estado or "DISPONIBLE")
            poco = "POCAS" in estado.upper()
            self.set_font("Helvetica", "B", 6.5)
            pw = self.get_string_width(estado) + 5
            px, py = x + c_stock - 3 - pw, y + alto / 2 - 2.5
            self.set_fill_color(*((253, 236, 235) if poco else (240, 242, 236)))
            self.set_draw_color(*((246, 211, 207) if poco else (221, 227, 212)))
            self.rect(px, py, pw, 5, style="DF")
            self.set_xy(px, py)
            self.set_text_color(*((165, 68, 59) if poco else (110, 113, 80)))
            self.cell(pw, 5, estado, align="C")
        else:
            self.set_xy(x, y + alto / 2 - 2.5)
            self.set_font("Helvetica", "B", 10)
            self.set_text_color(*self.INK)
            self.cell(c_stock - 3, 5, str(r.stock), align="R")

        self.set_draw_color(242, 242, 239)
        self.line(x0, y + alto, x0 + sum(self.COLS), y + alto)
        self.set_y(y + alto)


def _stocklist_pdf(datos: StocklistEnvioIn, host_propio: str) -> bytes:
    from concurrent.futures import ThreadPoolExecutor

    srcs = [r.imagen for r in datos.rows]
    with ThreadPoolExecutor(max_workers=16) as pool:
        imgs = list(pool.map(lambda s: _cargar_imagen(s, host_propio), srcs))
        logo = _cargar_imagen(datos.logo, host_propio, lado_max=600)
    pdf = _StocklistPDF(datos, logo)
    pdf.alias_nb_pages()
    pdf.add_page()
    for r, img in zip(datos.rows, imgs):
        pdf.fila(r, img)
    return bytes(pdf.output())


def _html_stocklist(mensaje: Optional[str], titulo: str) -> str:
    cuerpo = html_escape(mensaje or "").strip().replace("\n", "<br>")
    if not cuerpo:
        cuerpo = f"Hola,<br><br>Te adjuntamos el {html_escape(titulo.strip())} actualizado de MASSCOB."
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#161616">'
        f"<p>{cuerpo}</p>"
        '<p style="color:#6b6b64;font-size:12px">Adjunto: stocklist en PDF.</p>'
        "</div>"
    )


@app.post("/admin/stocklist/enviar")
def enviar_stocklist(datos: StocklistEnvioIn, request: Request, _: None = Depends(require_admin)):
    emails = []
    for e in datos.emails:
        e = e.strip().lower()
        if e and e not in emails:
            emails.append(e)
    if not emails:
        raise HTTPException(400, "Indica al menos un email")
    invalidos = [e for e in emails if not _EMAIL_RE.match(e)]
    if invalidos:
        raise HTTPException(400, f"Email no válido: {', '.join(invalidos)}")
    if len(emails) > 50:
        raise HTTPException(400, "Máximo 50 destinatarios por envío")
    if not datos.rows:
        raise HTTPException(400, "El stocklist está vacío con los filtros actuales")

    pdf_bytes = _stocklist_pdf(datos, request.url.hostname or "")
    titulo = datos.titulo.strip() or "Stocklist"
    nombre_pdf = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{titulo} {datos.fecha}").strip("-") + ".pdf"
    adjuntos = [{"filename": nombre_pdf, "content": base64.b64encode(pdf_bytes).decode()}]
    asunto = (datos.asunto or "").strip() or f"{titulo} - MASSCOB"
    html = _html_stocklist(datos.mensaje, titulo)

    enviados, fallidos = [], []
    for email in emails:
        ok, motivo = _enviar_email_detalle([email], asunto, html, adjuntos)
        if ok:
            enviados.append(email)
        else:
            fallidos.append({"email": email, "error": motivo})
    return {"enviados": enviados, "fallidos": fallidos, "pdf_kb": round(len(pdf_bytes) / 1024)}


# ---- clientes ----
#
# La contraseña de acceso ya no la escribe el admin a mano: se genera aquí
# (alta de cliente, botón "Generar nueva contraseña" del panel, el propio
# cliente desde "¿Has olvidado tu contraseña?" en el login, o el cron de las
# 48h) y se manda por email al cliente — el admin nunca llega a verla, solo
# puede disparar que se genere una nueva.


def _generar_password() -> str:
    return secrets.token_urlsafe(9)  # 12 caracteres, aleatorio-seguro


# Plantilla corporativa de los emails de credenciales. Todo con tablas y
# estilos inline porque Gmail/Outlook ignoran casi todo lo demás. La
# tipografía es Archivo, como en la web: Apple Mail e iOS la cargan desde
# Google Fonts; Gmail y Outlook descartan webfonts y caen en Helvetica/Arial.
# El logo va en JPG (no el SVG de la web) porque Gmail no muestra SVG.
_EMAIL_FONT = "'Archivo',Helvetica,Arial,sans-serif"
_EMAIL_LOGO_URL = (
    STORE_LOGIN_URL.rsplit("/", 1)[0] + "/assets/masscob-logo-email.jpg"
    if STORE_LOGIN_URL else ""
)


def _html_email_corporativo(titulo: str, cuerpo_html: str, preheader: str = "") -> str:
    logo = (
        f'<img src="{_EMAIL_LOGO_URL}" width="150" alt="MASSCOB" '
        'style="display:block;width:150px;height:auto;border:0">'
        if _EMAIL_LOGO_URL else
        f'<span style="font-family:{_EMAIL_FONT};font-size:22px;font-weight:700;letter-spacing:.18em;color:#161616">MASSCOB</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_escape(titulo)}</title>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&display=swap');</style>
</head>
<body style="margin:0;padding:0;background:#f2f2ef;-webkit-font-smoothing:antialiased">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{html_escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f2f2ef">
<tr><td align="center" style="padding:40px 16px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:520px;background:#ffffff;border:1px solid #e2e2dd">
    <tr><td style="padding:36px 40px 28px;border-bottom:1px solid #e2e2dd">
      {logo}
      <div style="font-family:{_EMAIL_FONT};font-size:9px;font-weight:500;letter-spacing:.34em;color:#6b6b64;margin-top:10px">WHOLESALE</div>
    </td></tr>
    <tr><td style="padding:36px 40px 40px;font-family:{_EMAIL_FONT};font-size:14px;line-height:1.6;color:#161616">
      {cuerpo_html}
    </td></tr>
  </table>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:520px">
    <tr><td style="padding:22px 40px 0;font-family:{_EMAIL_FONT};font-size:11px;line-height:1.6;color:#a7a7a0;text-align:center">
      MASSCOB Wholesale · <a href="mailto:sales@masscob.com" style="color:#a7a7a0">sales@masscob.com</a><br>
      Este es un mensaje automático, por favor no respondas a este email.
    </td></tr>
  </table>
</td></tr>
</table>
</body></html>"""


def _html_credenciales(usuario: str, password: str, es_regeneracion: bool) -> str:
    if es_regeneracion:
        eyebrow, titulo = "NUEVA CONTRASEÑA", "Tu nueva contraseña"
        intro = (
            "Se ha generado una nueva contraseña para tu cuenta de acceso a la "
            "tienda mayorista de MASSCOB. La anterior ha dejado de funcionar."
        )
    else:
        eyebrow, titulo = "BIENVENIDO", "Ya tienes acceso"
        intro = (
            "Tu cuenta en la tienda mayorista de MASSCOB está lista. "
            "Estas son tus credenciales de acceso:"
        )
    usuario_h, password_h = html_escape(usuario), html_escape(password)
    label = "font-size:10px;font-weight:600;letter-spacing:.16em;color:#a7a7a0;padding-bottom:4px"
    valor = "font-size:15px;font-weight:600;color:#161616;word-break:break-all"
    boton = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-top:28px"><tr>'
        f'<td style="background:#161616"><a href="{STORE_LOGIN_URL}" '
        f'style="display:inline-block;padding:14px 28px;font-family:{_EMAIL_FONT};font-size:13px;'
        'font-weight:600;letter-spacing:.04em;color:#ffffff;text-decoration:none">Entrar en la tienda</a></td>'
        "</tr></table>"
        if STORE_LOGIN_URL else ""
    )
    cuerpo = (
        f'<div style="font-size:10px;font-weight:600;letter-spacing:.24em;color:#a7a7a0;margin-bottom:12px">{eyebrow}</div>'
        f'<h1 style="margin:0 0 16px;font-family:{_EMAIL_FONT};font-size:24px;font-weight:600;letter-spacing:-.01em;color:#161616">{titulo}</h1>'
        f'<p style="margin:0 0 24px;color:#6b6b64">Hola,<br>{intro}</p>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#fafaf8;border:1px solid #e2e2dd">'
        f'<tr><td style="padding:18px 20px 8px;font-family:{_EMAIL_FONT}"><div style="{label}">USUARIO</div><div style="{valor}">{usuario_h}</div></td></tr>'
        f'<tr><td style="padding:10px 20px 18px;font-family:{_EMAIL_FONT}"><div style="{label}">CONTRASEÑA</div>'
        f'<div style="{valor};font-family:ui-monospace,Menlo,Consolas,monospace;letter-spacing:.04em">{password_h}</div></td></tr>'
        "</table>"
        f"{boton}"
        '<p style="margin:28px 0 0;font-size:12px;color:#a7a7a0">Por seguridad, no compartas esta contraseña con nadie.</p>'
    )
    return _html_email_corporativo(titulo, cuerpo, preheader=intro)


def _html_pedido(
    referencia: str, cliente_email: str, items: List[dict], total: float, para_equipo: bool
) -> str:
    """Email de pedido nuevo (cliente y equipo): el detalle va en el cuerpo
    con fotos para verlo sin abrir nada, y el PDF sigue adjunto aparte —
    ningún cliente de correo pinta un PDF dentro del mensaje."""
    if para_equipo:
        eyebrow, titulo = "NUEVO PEDIDO", f"Pedido {referencia}"
        intro = f"Nuevo pedido de <strong>{html_escape(cliente_email)}</strong>."
        preheader = f"Nuevo pedido de {cliente_email} — {total:.2f} €"
    else:
        eyebrow, titulo = "PEDIDO RECIBIDO", "Hemos recibido tu pedido"
        intro = "Gracias por tu pedido. Te avisaremos en cuanto lo revisemos."
        preheader = f"Pedido {referencia} — {total:.2f} €"

    label = "font-size:10px;font-weight:600;letter-spacing:.16em;color:#a7a7a0;padding-bottom:4px"
    valor = "font-size:14px;font-weight:600;color:#161616"
    ficha = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#fafaf8;border:1px solid #e2e2dd">'
        "<tr>"
        f'<td style="padding:16px 20px;font-family:{_EMAIL_FONT}"><div style="{label}">REFERENCIA</div><div style="{valor}">{html_escape(referencia)}</div></td>'
        f'<td style="padding:16px 20px;font-family:{_EMAIL_FONT}"><div style="{label}">ESTADO</div><div style="{valor}">Pendiente de confirmación</div></td>'
        "</tr></table>"
    )

    filas = []
    for it in items:
        img = (
            f'<img src="{html_escape(it["imagen_url"])}" width="56" alt="" '
            'style="display:block;width:56px;height:auto;max-height:72px;border:0">'
            if it.get("imagen_url") else ""
        )
        subtotal = it["cantidad"] * it["precio_unit"]
        filas.append(
            "<tr>"
            f'<td width="64" valign="top" style="padding:14px 0;border-bottom:1px solid #e2e2dd">'
            f'<div style="width:56px;background:#f7f7f4">{img}</div></td>'
            f'<td valign="top" style="padding:14px 8px 14px 12px;border-bottom:1px solid #e2e2dd;font-family:{_EMAIL_FONT};font-size:13px;line-height:1.45;color:#161616">'
            f'<div style="font-weight:600">{html_escape(it["nombre"])}</div>'
            f'<div style="font-size:11px;color:#a7a7a0">{html_escape(it["codigo"])}</div>'
            f'<div style="font-size:12px;color:#6b6b64;margin-top:4px">{html_escape(str(it["color"]))} · Talla {html_escape(str(it["talla"]))}'
            f' · {it["cantidad"]} × {it["precio_unit"]:.2f} €</div></td>'
            f'<td valign="top" align="right" style="padding:14px 0;border-bottom:1px solid #e2e2dd;font-family:{_EMAIL_FONT};font-size:13px;font-weight:600;color:#161616;white-space:nowrap">{subtotal:.2f} €</td>'
            "</tr>"
        )
    tabla = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:28px">'
        f'<tr><td colspan="3" style="{label};border-bottom:1px solid #161616;padding-bottom:8px;font-family:{_EMAIL_FONT}">RESUMEN DE PEDIDO</td></tr>'
        + "".join(filas)
        + f'<tr><td colspan="2" style="padding:16px 0 0;font-family:{_EMAIL_FONT};font-size:14px;font-weight:600">Total</td>'
        f'<td align="right" style="padding:16px 0 0;font-family:{_EMAIL_FONT};font-size:16px;font-weight:700;white-space:nowrap">{total:.2f} €</td></tr>'
        "</table>"
    )

    cuerpo = (
        f'<div style="font-size:10px;font-weight:600;letter-spacing:.24em;color:#a7a7a0;margin-bottom:12px">{eyebrow}</div>'
        f'<h1 style="margin:0 0 16px;font-family:{_EMAIL_FONT};font-size:24px;font-weight:600;letter-spacing:-.01em;color:#161616">{html_escape(titulo)}</h1>'
        f'<p style="margin:0 0 24px;color:#6b6b64">Hola,<br>{intro}</p>'
        f"{ficha}{tabla}"
        '<p style="margin:28px 0 0;font-size:12px;color:#a7a7a0">Adjuntamos el PDF con el detalle completo del pedido.</p>'
    )
    return _html_email_corporativo(titulo, cuerpo, preheader=preheader)


class ClienteIn(BaseModel):
    nombre_comercial: str
    razon_social: Optional[str] = None
    cif: Optional[str] = None
    direccion: Optional[str] = None
    pais: Optional[str] = None
    agente: Optional[str] = None
    usuario: str  # email de acceso a la tienda


def _crear_auth_user(email: str, password: str) -> str:
    """Crea el usuario en Supabase Auth (Admin API) y devuelve su id (uuid).

    Usa la service_role key porque solo la Admin API puede crear cuentas ya
    confirmadas (email_confirm=True) sin pasar por el flujo de verificación
    por correo, que no tiene sentido para clientes dados de alta a mano.
    """
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            500,
            "Falta SUPABASE_SERVICE_ROLE_KEY en el backend (.env) — "
            "sin ella no se pueden crear accesos nuevos.",
        )
    req = urllib.request.Request(
        f"{SUPABASE_URL}/auth/v1/admin/users",
        data=json.dumps({
            "email": email, "password": password, "email_confirm": True,
        }).encode(),
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read())["id"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            msg = json.loads(body).get("msg") or json.loads(body).get("message") or body
        except Exception:
            msg = body
        raise HTTPException(400, f"No se pudo crear el acceso a la tienda: {msg}")
    except HTTPException:
        raise
    except Exception as e:
        # Cualquier otro fallo (red, SSL, respuesta inesperada de Supabase...)
        # también como HTTPException con el motivo real, para no devolver un
        # 500 mudo que solo se puede diagnosticar mirando logs de Vercel.
        raise HTTPException(502, f"No se pudo contactar con Supabase Auth: {e!r}")


class ClienteAccesoIn(BaseModel):
    usuario: Optional[str] = None


def _actualizar_auth_user(user_id: str, email: Optional[str], password: Optional[str]):
    """Cambia el email y/o la contraseña de un cliente que ya tiene cuenta.

    Antes esto no existía: el panel dejaba editar el email/contraseña de un
    cliente ya sincronizado pero solo lo guardaba en local, sin tocar
    Supabase Auth, así que el acceso nunca cambiaba de verdad (bug).
    """
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            500,
            "Falta SUPABASE_SERVICE_ROLE_KEY en el backend (.env) — "
            "sin ella no se puede modificar el acceso.",
        )
    body = {}
    if email:
        body["email"] = email
    if password:
        body["password"] = password
    if not body:
        return
    req = urllib.request.Request(
        f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
        data=json.dumps(body).encode(),
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req) as res:
            res.read()
    except urllib.error.HTTPError as e:
        resp_body = e.read().decode()
        try:
            msg = json.loads(resp_body).get("msg") or json.loads(resp_body).get("message") or resp_body
        except Exception:
            msg = resp_body
        raise HTTPException(400, f"No se pudo actualizar el acceso a la tienda: {msg}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"No se pudo contactar con Supabase Auth: {e!r}")


@app.put("/admin/clientes/{cliente_id}/acceso")
def actualizar_acceso_cliente(
    cliente_id: str, datos: ClienteAccesoIn, _: None = Depends(require_admin)
):
    usuario = datos.usuario.strip().lower() if datos.usuario else None
    _actualizar_auth_user(cliente_id, usuario, None)
    return {"ok": True}


@app.post("/admin/clientes/{cliente_id}/regenerar-password")
def regenerar_password_cliente(cliente_id: str, _: None = Depends(require_admin)):
    """Genera una contraseña nueva para un cliente ya existente y se la
    manda por email. El admin dispara la acción pero nunca ve la
    contraseña — solo llega al cliente."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            email = _email_cliente(cur, cliente_id)
    finally:
        conn.close()
    if not email:
        raise HTTPException(404, "Cliente no encontrado")

    password = _generar_password()
    _actualizar_auth_user(cliente_id, None, password)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "update clientes set password_updated_at = now() where id = %s",
                (cliente_id,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    enviado = _enviar_email(
        [email],
        "Tu nueva contraseña de acceso a MASSCOB Wholesale",
        _html_credenciales(email, password, es_regeneracion=True),
    )
    return {"ok": True, "usuario": email, "email_enviado": enviado}


@app.get("/admin/cron/reset-passwords")
def cron_reset_passwords(_: None = Depends(require_cron)):
    """Reseteo automático de contraseñas cada 48h — llamado a diario por el
    cron de Vercel (ver vercel.json). Idempotente: cada día solo toca a
    quien ya lleve 48h o más desde su último reseteo, así que da igual si
    un día el cron no llega a ejecutarse. Nota: con el cron corriendo una
    vez al día (límite del plan Hobby de Vercel), una contraseña puede
    llegar a durar algo más de 48h en el peor caso — no exactamente 48h al
    segundo."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select c.id, u.email from clientes c join auth.users u on u.id = c.id "
                "where c.password_updated_at is null "
                "or c.password_updated_at < now() - interval '48 hours'"
            )
            pendientes = cur.fetchall()
    finally:
        conn.close()

    resultados = []
    for cliente_id, email in pendientes:
        try:
            password = _generar_password()
            _actualizar_auth_user(cliente_id, None, password)
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "update clientes set password_updated_at = now() where id = %s",
                        (cliente_id,),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            enviado = _enviar_email(
                [email],
                "Tu contraseña de acceso a MASSCOB Wholesale se ha renovado",
                _html_credenciales(email, password, es_regeneracion=True),
            )
            resultados.append({"usuario": email, "email_enviado": enviado})
        except Exception as e:
            resultados.append({"usuario": email, "error": str(e)})
    return {"procesados": len(resultados), "resultados": resultados}


class OlvidePasswordIn(BaseModel):
    usuario: str


@app.post("/auth/olvide-password")
def olvide_password(datos: OlvidePasswordIn):
    """Autoservicio del propio cliente desde "¿Has olvidado tu contraseña?"
    en el login — a diferencia de regenerar_password_cliente() no hay clave
    de admin de por medio, así que esto es público. Por eso: (1) identifica
    solo por email y SIEMPRE responde igual, exista ese email o no, para que
    no sirva para averiguar qué clientes tenemos dados de alta; (2) respeta
    un cooldown de 5 min (reutilizando password_updated_at) para que no
    sirva para tirar a un cliente de su cuenta a base de pedir resets sin
    parar."""
    usuario = datos.usuario.strip().lower()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select c.id, u.email, "
                "coalesce(c.password_updated_at > now() - interval '5 minutes', false) as en_cooldown "
                "from clientes c join auth.users u on u.id = c.id "
                "where lower(u.email) = %s",
                (usuario,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row and not row[2]:
        cliente_id, email, _ = row
        password = _generar_password()
        _actualizar_auth_user(cliente_id, None, password)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "update clientes set password_updated_at = now() where id = %s",
                    (cliente_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        _enviar_email(
            [email],
            "Tu nueva contraseña de acceso a MASSCOB Wholesale",
            _html_credenciales(email, password, es_regeneracion=True),
        )

    return {"ok": True}


@app.post("/admin/clientes")
def crear_cliente(cliente: ClienteIn, _: None = Depends(require_admin)):
    usuario = cliente.usuario.strip().lower()
    password = _generar_password()
    user_id = _crear_auth_user(usuario, password)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"insert into clientes (id, {', '.join(_CLIENTE_CAMPOS)}, password_updated_at) "
                f"values (%s, %s, %s, %s, %s, %s, %s, now()) "
                f"returning id, {', '.join(_CLIENTE_CAMPOS)}",
                (user_id, cliente.nombre_comercial, cliente.razon_social, cliente.cif,
                 cliente.direccion, cliente.pais, cliente.agente),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Igual que el resto de emails: best-effort, un fallo aquí no debe
    # tumbar el alta del cliente, que ya se guardó.
    _enviar_email(
        [usuario],
        "Tus credenciales de acceso a MASSCOB Wholesale",
        _html_credenciales(usuario, password, es_regeneracion=False),
    )
    return dict(zip(["id"] + _CLIENTE_CAMPOS, row))


@app.get("/admin/clientes")
def listar_clientes_admin(_: None = Depends(require_admin)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"select c.id, {', '.join('c.' + f for f in _CLIENTE_CAMPOS)}, "
                "u.email, c.password_updated_at "
                "from clientes c join auth.users u on u.id = c.id "
                "order by c.created_at desc"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    keys = ["id"] + _CLIENTE_CAMPOS + ["usuario", "password_updated_at"]
    result = []
    for row in rows:
        d = dict(zip(keys, row))
        d["password_updated_at"] = (
            d["password_updated_at"].isoformat() if d["password_updated_at"] else None
        )
        result.append(d)
    return result


# ---- notificaciones: destinatarios del equipo (además del cliente) ----

class NotifEmailsIn(BaseModel):
    emails: List[str]


@app.get("/admin/notif-emails")
def listar_notif_emails(_: None = Depends(require_admin)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            emails = _notif_emails_equipo(cur)
    finally:
        conn.close()
    return emails


@app.put("/admin/notif-emails")
def actualizar_notif_emails(body: NotifEmailsIn, _: None = Depends(require_admin)):
    emails = sorted({e.strip().lower() for e in body.emails if e.strip()})
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("delete from notif_emails")
            for email in emails:
                cur.execute("insert into notif_emails (email) values (%s)", (email,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return emails
