// Carrito de pedido — MASSCOB Wholesale.
// VISTA PREVIA: se guarda solo en localStorage de este navegador, no llega
// todavía al backend ni a la pestaña "Pedidos" del panel admin (eso necesita
// el servidor real — ver Ajustes en el panel admin para el mínimo de reorder
// y el % de exchanges, que hoy no se pueden leer desde aquí).
const CART_KEY = 'masscob_cart_v1';
// Mínimo de unidades del PEDIDO COMPLETO (sumando todas las tallas/colores/
// referencias), no por línea — se puede pedir 1 unidad de tres productos
// distintos y ya cumple. Fijo aquí hasta que el backend sirva el valor real
// de Ajustes (panel admin).
const MIN_ORDER_UNITS = 3;

function fmtEUR(n){ return (n==null)?'—': n.toLocaleString('es-ES',{minimumFractionDigits:2,maximumFractionDigits:2})+' €'; }

// Resuelve una ruta de foto de producto: local (banco de fotos del ERP,
// relativa a la raíz de masscob-b2b) o absoluta (CDN de masscob.com, cuando
// la referencia ya se cruzó con la ficha real de la tienda B2C).
function imgSrc(path, width){
  if(!path) return '';
  if(/^https?:\/\//.test(path)){
    if(width && path.includes('cdn.shopify.com')){
      return path + (path.includes('?') ? '&' : '?') + 'width=' + width;
    }
    return path;
  }
  return '../' + path;
}
function cartMeetsMinimum(){ return cartCount() >= MIN_ORDER_UNITS; }

function cartLoad(){
  try{ return JSON.parse(localStorage.getItem(CART_KEY)) || []; }catch(e){ return []; }
}
function cartSave(items){
  try{ localStorage.setItem(CART_KEY, JSON.stringify(items)); }catch(e){}
}
function cartLineKey(codigo,color,talla){ return codigo+'__'+color+'__'+talla; }

function cartAddLine(line){
  // line: {codigo, name, color, talla, cantidad, precioUnit, image}
  const items = cartLoad();
  const key = cartLineKey(line.codigo, line.color, line.talla);
  const existing = items.find(i => cartLineKey(i.codigo,i.color,i.talla)===key);
  if(existing){ existing.cantidad += line.cantidad; }
  else { items.push(Object.assign({}, line)); }
  cartSave(items);
  return items;
}
function cartSetQty(codigo,color,talla,cantidad){
  let items = cartLoad();
  const key = cartLineKey(codigo,color,talla);
  if(cantidad<=0){
    items = items.filter(i => cartLineKey(i.codigo,i.color,i.talla)!==key);
  } else {
    const ex = items.find(i => cartLineKey(i.codigo,i.color,i.talla)===key);
    if(ex) ex.cantidad = cantidad;
  }
  cartSave(items);
  return items;
}
function cartRemove(codigo,color,talla){ return cartSetQty(codigo,color,talla,0); }
function cartClear(){ cartSave([]); }
function cartCount(){ return cartLoad().reduce((t,i)=>t+i.cantidad,0); }
function cartTotal(){ return cartLoad().reduce((t,i)=>t+i.cantidad*i.precioUnit,0); }

function updateCartBadge(){
  const el = document.getElementById('cartCount');
  if(el) el.textContent = cartCount();
}

function showToast(msg){
  let t = document.getElementById('toast');
  if(!t){
    t = document.createElement('div');
    t.id = 'toast';
    t.style.cssText = 'position:fixed;bottom:26px;left:50%;transform:translateX(-50%) translateY(10px);background:#161616;color:#fff;padding:13px 22px;font-size:13px;font-weight:500;z-index:100;opacity:0;transition:opacity .25s,transform .25s;pointer-events:none';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  requestAnimationFrame(()=>{ t.style.opacity='1'; t.style.transform='translateX(-50%) translateY(0)'; });
  clearTimeout(t._hideTimer);
  t._hideTimer = setTimeout(()=>{ t.style.opacity='0'; t.style.transform='translateX(-50%) translateY(10px)'; }, 2600);
}

document.addEventListener('DOMContentLoaded', updateCartBadge);
