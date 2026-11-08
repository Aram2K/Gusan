import base64,math
s=open('mock2.tpl').read()
logo='data:image/png;base64,'+base64.b64encode(open('icon160.png','rb').read()).decode()
mic='<svg viewBox="0 0 24 24" fill="#fff"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0" stroke="#fff" stroke-width="2" fill="none"/><path d="M12 17v4" stroke="#fff" stroke-width="2"/></svg>'
scrico='<svg viewBox="0 0 24 24" fill="none" stroke="#0a84ff" stroke-width="1.9" stroke-linecap="round"><rect x="6.5" y="2" width="11" height="20" rx="3"/><path d="M10 4.6h4"/></svg>'
ctxscr=scrico.replace('#0a84ff','#0b3d91')
ctxsel='<svg viewBox="0 0 24 24" fill="none" stroke="#0b3d91" stroke-width="2" stroke-linecap="round"><path d="M5 7V5h14v2M12 5v14M9 19h6"/></svg>'
xsvg='<svg viewBox="0 0 24 24" fill="none" stroke="#374151" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>'
scr='<svg viewBox="0 0 24 24" fill="none" stroke="#1f2937" stroke-width="1.8"><rect x="4" y="3" width="16" height="18" rx="3"/><path d="M8 7h8M8 11h5"/></svg>'
scrw=scrico.replace('#0a84ff','#fff').replace('<svg ','<svg width="17" height="17" ')
globe='<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="#111" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18"/></svg>'
amic='<svg viewBox="0 0 24 24" width="18" height="18" fill="#111"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0" stroke="#111" stroke-width="1.8" fill="none"/><path d="M12 17v4" stroke="#111" stroke-width="1.8"/></svg>'
def row(ch): return '<div class="kr">'+''.join(f'<div class="k">{c}</div>' for c in ch)+'</div>'
bottom=('<div class="kr"><div class="k f">⇧</div>'+''.join(f'<div class="k">{c}</div>' for c in 'ZXCVBNM')+'<div class="k f">⌫</div></div>'
 '<div class="kr"><div class="k sm">123</div><div class="k sm">'+globe+'</div><div class="k sp">space</div><div class="k ret">return</div></div></div>')
keys='<div class="keys">'+row('QWERTYUIOP')+row('ASDFGHJKL')+bottom; keys2='<div class="keys">'+bottom
hs=[8,16,26,14,22,28,12,20,26,10,18,24,8,16,26,14,22,28,12,20]
full=''.join(f'<i style="--h:{6+int(26*abs(math.sin(i*0.55))*(0.55+0.45*abs(math.sin(i*0.17))))}px"></i>' for i in range(38))
wave=''.join(f'<i style="--h:{h}px"></i>' for h in hs[:14]); wavebig=''.join(f'<i style="--h:{h*2.3:.0f}px"></i>' for h in hs[:14])
ic={'Home':'<path d="M3 11l9-8 9 8v10a1 1 0 0 1-1 1h-5v-7h-6v7H4a1 1 0 0 1-1-1z"/>','Sessions':'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M7 9h10M7 13h6"/>','Vault':'<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>','Settings':'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/>'}
def tabs(on): return '<div class="tabs">'+''.join(f'<div class="{"on" if n==on else ""}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">{ic[n]}</svg>{n}</div>' for n in ic)+'</div>'
s=(s.replace('CTXSEL',ctxsel).replace('CTXSCR',ctxscr).replace('SCRSVG',scrico).replace('XSVG',xsvg).replace('MICSVG',mic).replace('SCREENSVGW',scrw).replace('SCREENSVG',scr).replace('WAVEFULL',full).replace('WAVEBIG',wavebig).replace('WAVE',wave)
   .replace('KEYS2',keys2).replace('KEYS',keys).replace('TABS1',tabs('Home')).replace('TABS2',tabs('Sessions')).replace('TABS3',tabs('Vault')).replace('TABS4',tabs('Settings')).replace('LOGO',logo))
open('mock2.html','w').write(s); print('html ok')
