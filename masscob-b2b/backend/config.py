import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_API_KEY = os.environ["ADMIN_API_KEY"]
# Solo hace falta para crear clientes desde el panel admin (POST /admin/clientes).
# Settings > API > Project API keys > service_role, en el panel de Supabase.
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
