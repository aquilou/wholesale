// Sesión de cliente + pedidos — MASSCOB Wholesale.
// Login y pedidos reales contra Supabase Auth + backend (FastAPI). Requiere
// que cada página cargue el SDK de Supabase (CDN) antes que este script.
const SUPABASE_URL = 'https://eqhlxeainbvzfkifszap.supabase.co';
const SUPABASE_ANON_KEY = 'sb_publishable_gLbaQk49__Z1PMYFeGNFkA_ojyiCuAF';
const API_BASE = 'http://127.0.0.1:8001'; // backend local; actualizar cuando haya despliegue real

const supabaseClient = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

const ESTADOS_PEDIDO = ['PENDIENTE', 'ACEPTADO', 'ANULADO'];
const ESTADO_STYLE = {
  'PENDIENTE': 'color:#b07a3f;background:#f7f0e6;border:1px solid #ecdcc4',
  'ACEPTADO': 'color:#6E7150;background:#f0f2ec;border:1px solid #dde3d4',
  'ANULADO': 'color:#8a4a42;background:#f5eae8;border:1px solid #e6cfc9',
};

// ---- sesión (Supabase Auth real) ----
async function _authHeader(){
  const { data } = await supabaseClient.auth.getSession();
  const session = data && data.session;
  if(!session) return null;
  return { Authorization: 'Bearer ' + session.access_token };
}

// Devuelve la ficha de cliente (o null si no hay sesión, si /me falla,
// o si la cuenta de Auth no tiene fila en `clientes` todavía).
async function currentClient(){
  const { data } = await supabaseClient.auth.getSession();
  const session = data && data.session;
  if(!session) return null;
  let res;
  try{
    res = await fetch(API_BASE + '/me', { headers: await _authHeader() });
  }catch(e){
    return null;
  }
  if(!res.ok) return null;
  const perfil = await res.json();
  return {
    id: session.user.id,
    usuario: session.user.email,
    nombreComercial: perfil.nombre_comercial,
    razonSocial: perfil.razon_social,
    cif: perfil.cif,
    direccion: perfil.direccion,
    pais: perfil.pais,
    agente: perfil.agente,
  };
}
// Llamar al principio de cada página protegida: si no hay sesión válida,
// redirige al login.
async function requireSession(){
  const client = await currentClient();
  if(!client){ await supabaseClient.auth.signOut(); location.href = 'index.html'; return null; }
  return client;
}

async function loginClient(usuario, password){
  const { error } = await supabaseClient.auth.signInWithPassword({
    email: String(usuario||'').trim(),
    password: String(password||''),
  });
  if(error) return null;
  return currentClient();
}

// ---- pedidos (backend real) ----
function _pedidoFromApi(p){
  return {
    ref: p.referencia, fecha: p.fecha, estado: p.estado, total: p.total, nota: p.nota,
    items: p.items.map(i => ({
      codigo: i.codigo, name: i.nombre, color: i.color, talla: i.talla,
      cantidad: i.cantidad, precioUnit: i.precio_unit,
    })),
  };
}
async function pedidosDeCliente(){
  const headers = await _authHeader();
  if(!headers) return [];
  const res = await fetch(API_BASE + '/pedidos', { headers });
  if(!res.ok) return [];
  const pedidos = await res.json();
  return pedidos.map(_pedidoFromApi);
}
async function crearPedido({items, total, nota}){
  const headers = await _authHeader();
  if(!headers) return null;
  const body = {
    total, nota: nota || '',
    items: items.map(i => ({
      codigo: i.codigo, nombre: i.name, color: i.color, talla: i.talla,
      cantidad: i.cantidad, precio_unit: i.precioUnit,
    })),
  };
  const res = await fetch(API_BASE + '/pedidos', {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json' }, headers),
    body: JSON.stringify(body),
  });
  if(!res.ok) return null;
  return _pedidoFromApi(await res.json());
}
async function stockActual(){
  const headers = await _authHeader();
  if(!headers) return {};
  const res = await fetch(API_BASE + '/stock', { headers });
  if(!res.ok) return {};
  return res.json();
}
function fmtFechaPedido(iso){
  try{ return new Date(iso).toLocaleDateString('es-ES', {day:'2-digit',month:'2-digit',year:'numeric'}); }
  catch(e){ return iso; }
}

// ---- header: menú "Mi perfil" (misma estructura en catalogo/carrito/checkout/pedidos/mi-cuenta) ----
function initProfileMenu(client, active){
  const btn = document.getElementById('profileBtn');
  const dd = document.getElementById('profileDropdown');
  if(btn && client && client.nombreComercial) btn.textContent = client.nombreComercial + ' ▾';
  document.querySelectorAll('[data-brand]').forEach(el => {
    el.style.cursor = 'pointer';
    el.addEventListener('click', () => { location.href = 'catalogo.html'; });
  });
  if(!btn || !dd) return;
  btn.addEventListener('click', e => { e.stopPropagation(); dd.classList.toggle('open'); });
  document.addEventListener('click', () => dd.classList.remove('open'));
  dd.addEventListener('click', e => e.stopPropagation());
  const logoutBtn = document.getElementById('logoutBtn');
  if(logoutBtn) logoutBtn.addEventListener('click', async () => { await supabaseClient.auth.signOut(); location.href = 'index.html'; });
  if(active){
    const link = dd.querySelector('[data-nav="' + active + '"]');
    if(link) link.classList.add('activeItem');
  }
}
