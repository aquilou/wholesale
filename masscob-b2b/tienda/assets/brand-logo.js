// Muestra el logo subido en Ajustes (panel admin) en vez del wordmark de texto,
// si hay uno guardado. Lee el mismo localStorage que usa el panel admin, así que
// solo funciona cuando ambas apps se sirven desde el mismo origen (no en file://).
(function(){
  var STORAGE_KEY = 'masscob_admin_v2';

  function readLogo(){
    try{
      var raw = localStorage.getItem(STORAGE_KEY);
      if(!raw) return null;
      var data = JSON.parse(raw);
      return (data && data.logo) || null;
    }catch(e){ return null; }
  }

  function apply(){
    var logo = readLogo();
    document.querySelectorAll('[data-brand]').forEach(function(el){
      var img = el.querySelector('.brandLogoImg');
      var text = el.querySelector('.brandText');
      if(logo){
        if(!img){
          img = document.createElement('img');
          img.className = 'brandLogoImg';
          var h = el.getAttribute('data-logo-height') || '30';
          img.style.cssText = 'display:block;max-height:'+h+'px;max-width:180px;object-fit:contain';
          el.insertBefore(img, el.firstChild);
        }
        img.src = logo;
        if(text) text.style.display = 'none';
      } else if(img){
        img.remove();
        if(text) text.style.display = '';
      }
    });
  }

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', apply);
  } else {
    apply();
  }
  window.addEventListener('storage', function(e){
    if(e.key === STORAGE_KEY) apply();
  });
})();
