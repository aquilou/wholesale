"""
MASSCOB Wholesale API — login real + ficha de cliente + pedidos.
Login en sí lo hace Supabase Auth (desde tienda/index.html); esta API solo
valida el token resultante y sirve los datos que dependen de negocio.

Correr en local:
    uvicorn main:app --reload
"""
import json
import secrets
import time
import urllib.error
import urllib.request
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
def _enviar_email(destinatarios: List[str], asunto: str, html: str) -> bool:
    if not RESEND_API_KEY or not destinatarios:
        return False
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps({
            "from": RESEND_FROM_EMAIL, "to": destinatarios, "subject": asunto, "html": html,
        }).encode(),
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as res:
            res.read()
        return True
    except Exception as e:
        print(f"[email] no se pudo enviar {asunto!r} a {destinatarios}: {e!r}")
        return False


def _notif_emails_equipo(cur) -> List[str]:
    cur.execute("select email from notif_emails order by email")
    return [row[0] for row in cur.fetchall()]


def _email_cliente(cur, cliente_id: str) -> Optional[str]:
    cur.execute("select email from auth.users where id = %s", (cliente_id,))
    row = cur.fetchone()
    return row[0] if row else None


def _items_html(items) -> str:
    filas = "".join(
        f"<li>{i.cantidad} x {i.nombre} — {i.color}, talla {i.talla} "
        f"({i.precio_unit:.2f} €/ud)</li>"
        for i in items
    )
    return f"<ul>{filas}</ul>"


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

    # El pedido ya está guardado en este punto (commit hecho arriba). Un
    # fallo desde aquí en adelante (Resend caído, notif_emails inexistente,
    # etc.) es best-effort y NUNCA debe convertirse en un 500 de cara al
    # cliente — si no, el pedido "falla" en la pantalla pero ya existe en
    # la base de datos, y cada reintento crea uno duplicado.
    try:
        resumen = (
            f"<p>Referencia: <strong>{referencia}</strong></p>"
            f"{_items_html(pedido.items)}"
            f"<p>Total: <strong>{float(total):.2f} €</strong></p>"
        )
        if client.get("email"):
            _enviar_email(
                [client["email"]],
                f"Hemos recibido tu pedido {referencia}",
                f"<p>Hola,</p><p>Hemos recibido tu pedido. Te avisaremos en cuanto lo revisemos.</p>{resumen}",
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
                f"<p>Nuevo pedido de <strong>{client.get('email','—')}</strong>.</p>{resumen}",
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


# ---- clientes ----
#
# La contraseña de acceso ya no la escribe el admin a mano: se genera aquí
# (alta de cliente, botón "Generar nueva contraseña" del panel, o el cron de
# los 30 días) y se manda por email al cliente — el admin nunca llega a
# verla, solo puede disparar que se genere una nueva.


def _generar_password() -> str:
    return secrets.token_urlsafe(9)  # 12 caracteres, aleatorio-seguro


def _html_credenciales(usuario: str, password: str, es_regeneracion: bool) -> str:
    intro = (
        "Se ha generado una nueva contraseña para tu cuenta de acceso a la "
        "tienda mayorista de MASSCOB. La anterior ha dejado de funcionar."
        if es_regeneracion else
        "Ya tienes acceso a la tienda mayorista de MASSCOB."
    )
    enlace = f'<p><a href="{STORE_LOGIN_URL}">Entrar en la tienda</a></p>' if STORE_LOGIN_URL else ""
    return (
        f"<p>Hola,</p><p>{intro}</p>"
        f"<p>Usuario: <strong>{usuario}</strong><br>"
        f"Contraseña: <strong>{password}</strong></p>"
        f"{enlace}"
        "<p>Por seguridad, no compartas esta contraseña con nadie.</p>"
    )


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
    """Reseteo automático de contraseñas cada 30 días — llamado a diario
    por el cron de Vercel (ver vercel.json). Idempotente: cada día solo
    toca a quien ya lleve 30 días o más desde su último reseteo, así que da
    igual si un día el cron no llega a ejecutarse."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select c.id, u.email from clientes c join auth.users u on u.id = c.id "
                "where c.password_updated_at is null "
                "or c.password_updated_at < now() - interval '30 days'"
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
