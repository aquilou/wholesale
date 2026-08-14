"""
Valida el JWT que emite Supabase Auth al hacer login desde tienda/index.html.
No hay contraseñas ni sesiones propias aquí: Supabase ya hizo el login, este
módulo solo confirma que el token es genuino y de quién dice ser.

Supabase firma los tokens con claves asimétricas (no un secreto compartido):
el backend descubre la clave pública vigente sola, consultando el endpoint
JWKS del proyecto. PyJWKClient cachea esa clave, no hay que gestionarla a mano.
"""
import secrets

import jwt
from jwt import PyJWKClient
from fastapi import Header, HTTPException

from config import ADMIN_API_KEY, SUPABASE_URL

_jwk_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")


def get_current_client(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Falta el token de autenticación")
    token = authorization[len("Bearer "):]
    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token, signing_key.key, algorithms=["ES256", "RS256"], audience="authenticated"
        )
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Token inválido o vencido: {e}")
    return {"user_id": payload["sub"], "email": payload.get("email")}


# Clave compartida para el panel admin, que no tiene login propio (se abre
# como archivo local). No es autenticación por usuario — es deliberadamente
# simple, acorde a que hoy el panel admin ya no tiene ningún control de
# acceso. Ver tienda/session.js para el caso de clientes reales (JWT).
def require_admin(x_admin_key: str = Header(...)):
    if not secrets.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(401, "Clave de administrador incorrecta")
