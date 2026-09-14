import os
from dotenv import load_dotenv

load_dotenv()

# .strip(): pegar estas variables a mano en el panel de Vercel ha colado más
# de una vez un salto de línea o espacio de sobra al final del valor (p.ej.
# SUPABASE_SERVICE_ROLE_KEY con un "\n" pegado rompía las llamadas a
# Supabase con "Invalid header value"). Mejor tolerarlo aquí que depender de
# pegar siempre perfecto.
SUPABASE_URL = os.environ["SUPABASE_URL"].strip()
DATABASE_URL = os.environ["DATABASE_URL"].strip()
ADMIN_API_KEY = os.environ["ADMIN_API_KEY"].strip()
# Solo hace falta para crear clientes desde el panel admin (POST /admin/clientes).
# Settings > API > Project API keys > service_role, en el panel de Supabase.
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

# Notificaciones de pedido por email (resend.com). Sin ella el backend
# simplemente no manda avisos — no bloquea crear ni gestionar pedidos.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
# Remitente de esos emails. Con el plan gratuito de Resend, mientras el
# dominio de "para" no esté verificado en Resend, solo se entregan a la
# cuenta con la que se creó la API key — verificar el dominio en
# resend.com/domains para poder mandarlos a clientes reales.
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "MASSCOB Wholesale <pedidos@masscob.com>").strip()

# Reseteo automático de contraseñas de clientes cada 30 días
# (GET /admin/cron/reset-passwords, llamado por el cron de Vercel — ver
# vercel.json). Vercel manda solo "Authorization: Bearer $CRON_SECRET" en
# cada invocación de cron cuando esta env var está configurada en el
# proyecto; sin ella el endpoint rechaza cualquier llamada.
CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()

# Enlace a la tienda que se incluye en los emails de contraseña generada o
# regenerada. Opcional: si no está, el email simplemente no lleva enlace.
STORE_LOGIN_URL = os.environ.get("STORE_LOGIN_URL", "").strip()
