"""
MASSCOB Wholesale API — login real + ficha de cliente + pedidos.
Login en sí lo hace Supabase Auth (desde tienda/index.html); esta API solo
valida el token resultante y sirve los datos que dependen de negocio.

Correr en local:
    uvicorn main:app --reload
"""
import json
import time
import urllib.error
import urllib.request
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from auth import get_current_client, require_admin
from config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
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
def crear_pedido(pedido: PedidoIn, client: dict = Depends(get_current_client)):
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
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
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
            cur.execute("select estado from pedidos where id = %s for update", (pedido_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Pedido no encontrado")
            estado_actual = row[0]

            if estado_actual != body.estado:
                entra_en_aceptado = body.estado == "ACEPTADO"
                sale_de_aceptado = estado_actual == "ACEPTADO"
                if entra_en_aceptado or sale_de_aceptado:
                    items = _fetch_items(cur, pedido_id)
                    for item in items:
                        if entra_en_aceptado:
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
                        else:
                            cur.execute(
                                "update stock set cantidad = cantidad + %s "
                                "where codigo=%s and color=%s and talla=%s",
                                (item["cantidad"], item["codigo"], item["color"], item["talla"]),
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


# ---- clientes ----

class ClienteIn(BaseModel):
    nombre_comercial: str
    razon_social: Optional[str] = None
    cif: Optional[str] = None
    direccion: Optional[str] = None
    pais: Optional[str] = None
    agente: Optional[str] = None
    usuario: str  # email de acceso a la tienda
    password: str


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


@app.post("/admin/clientes")
def crear_cliente(cliente: ClienteIn, _: None = Depends(require_admin)):
    if len(cliente.password) < 6:
        raise HTTPException(400, "La contraseña debe tener al menos 6 caracteres")
    user_id = _crear_auth_user(cliente.usuario.strip().lower(), cliente.password)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"insert into clientes (id, {', '.join(_CLIENTE_CAMPOS)}) "
                f"values (%s, %s, %s, %s, %s, %s, %s) returning id, {', '.join(_CLIENTE_CAMPOS)}",
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
    return dict(zip(["id"] + _CLIENTE_CAMPOS, row))


@app.get("/admin/clientes")
def listar_clientes_admin(_: None = Depends(require_admin)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"select c.id, {', '.join('c.' + f for f in _CLIENTE_CAMPOS)}, u.email "
                "from clientes c join auth.users u on u.id = c.id "
                "order by c.created_at desc"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    keys = ["id"] + _CLIENTE_CAMPOS + ["usuario"]
    return [dict(zip(keys, row)) for row in rows]
