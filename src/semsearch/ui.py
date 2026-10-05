"""The settings page served at /ui: status, a search box, the indexed folders, the exclusion
patterns (editable) and the Windows Search scope suggestion. Plain HTML + fetch(), no
build step, no external resources (the page must work offline and must not reach out).
Folder add/remove needs a filesystem grant that only the operator can make, so the page
sends the user to the tray menu / `semsearch roots` for that and edits only exclusions.
"""
from __future__ import annotations

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>SemSearch</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f6f7f9;--fg:#1d2330;--mut:#5c6673;--line:#d9dee5;--acc:#2a6df4;--ok:#1a8f4a;--warn:#b85c00;--card:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#141820;--fg:#e7eaf0;--mut:#9aa4b2;--line:#2b3340;--acc:#6ea0ff;--ok:#4cc27a;--warn:#f0a040;--card:#1b2029}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 8px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:8px 0}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.mut{color:var(--mut)}.ok{color:var(--ok)}.warn{color:var(--warn)}
input[type=text],textarea{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
textarea{min-height:200px;font-family:ui-monospace,Consolas,monospace;font-size:13px}
button{padding:8px 14px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:inherit;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
ul{margin:6px 0;padding-left:20px}li{margin:3px 0}code{font-family:ui-monospace,Consolas,monospace;font-size:13px}
.hit{padding:8px 0;border-top:1px solid var(--line)}.hit:first-child{border-top:0}.hit .p{font-weight:600;word-break:break-all}.hit .x{color:var(--mut);font-size:13px;white-space:pre-wrap}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px}#msg{min-height:1.4em}
</style></head><body><main>
<h1>SemSearch</h1><div class="mut" id="sub">local semantic file search</div>
<div class="card"><div class="row"><input type="text" id="q" placeholder="search your files by meaning..." autofocus><button class="primary" id="go">Search</button>
<select id="mode"><option value="hybrid">hybrid</option><option value="semantic">semantic</option><option value="literal">literal</option></select></div>
<div id="results"></div></div>
<h2>Status</h2><div class="card"><div class="kv" id="status"></div></div>
<h2>Indexed folders</h2><div class="card"><ul id="roots"></ul>
<div class="mut">Add or remove folders from the SemSearch tray icon (right-click &rsaquo; Folders) or with <code>semsearch roots add &lt;folder&gt;</code>: the service must be granted read access on a folder first, and only you can do that.</div></div>
<h2>Exclusions</h2><div class="card"><div class="mut">One glob per line. A pattern with <code>/</code> matches the full path (forward slashes, case-insensitive); a bare pattern such as <code>*.pem</code> matches file names only.</div>
<textarea id="excl" spellcheck="false"></textarea><div class="row" style="margin-top:8px"><button class="primary" id="saveExcl">Save exclusions</button><button id="winScope">Show Windows Search scope</button><span id="msg"></span></div>
<div id="scope"></div></div>
<div class="card mut" id="auth"></div>
</main><script>
const $=s=>document.querySelector(s);
let SESS=null;try{SESS=sessionStorage.getItem('semsearch.session')}catch(e){}
async function signIn(){const m=location.hash.match(/n=([A-Za-z0-9_-]+)/);history.replaceState(null,'',location.pathname);if(!m)return;try{const r=await fetch('/ui/redeem',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({nonce:m[1]})});if(r.ok){SESS=(await r.json()).session;try{sessionStorage.setItem('semsearch.session',SESS)}catch(e){}}}catch(e){}}
const H=()=>SESS?{'x-semsearch-session':SESS}:{};
async function j(u,o){const r=await fetch(u,Object.assign({headers:Object.assign({'content-type':'application/json'},H())},o||{}));const t=await r.text();let b;try{b=JSON.parse(t)}catch(e){b={detail:t}}if(!r.ok)throw new Error(b.detail||b.error||r.status);return b}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function status(){try{const h=await j('/health');const s=await j('/status');const ix=s.indexer||{};const q=ix.queue||{};
$('#sub').textContent='version '+h.version+' - '+h.documents.toLocaleString()+' documents';
$('#status').innerHTML=[['indexer',ix.running?(ix.paused?'paused':'running'):'stopped'],['queue',(q.pending||0)+' pending, '+(q.running||0)+' running, '+(q.failed||0)+' failed'],['current',ix.current_path||'-'],['watcher',ix.watcher?'active':'off'],['embedding',(ix.embedding||{}).device||''],['Windows Search',(s.windows_search&&s.windows_search.available)?('reachable, '+(s.windows_search.items||0).toLocaleString()+' items'):'unavailable'],['config',s.config_source||'']].map(([k,v])=>'<div class="mut">'+esc(k)+'</div><div>'+esc(v)+'</div>').join('')}catch(e){$('#status').innerHTML='<div class="warn">'+esc(e.message)+'</div>'}}
async function config(){try{const c=await j('/config');$('#roots').innerHTML=c.roots.length?c.roots.map(r=>'<li><code>'+esc(r)+'</code></li>').join(''):'<li class="warn">no folders configured: nothing is indexed</li>';$('#excl').value=c.excludes.join('\n')}catch(e){$('#roots').innerHTML='<li class="warn">'+esc(e.message)+'</li>'}}
$('#saveExcl').onclick=async()=>{$('#msg').textContent='saving...';try{const v=$('#excl').value.split('\n').map(s=>s.trim()).filter(Boolean);const r=await j('/config/excludes',{method:'POST',body:JSON.stringify({excludes:v})});$('#msg').innerHTML='<span class="ok">saved '+esc(r.excludes)+' patterns; documents now excluded are removed in the background</span>'}catch(e){$('#msg').innerHTML='<span class="warn">'+esc(e.message)+'</span>'}};
$('#winScope').onclick=async()=>{$('#scope').textContent='reading the Windows Search scope...';try{const s=await j('/config/windows-scope');$('#scope').innerHTML='<div style="margin-top:10px"><b>Folders Windows indexes for content</b> (your profile only):<ul>'+(s.roots.map(r=>'<li><code>'+esc(r)+'</code></li>').join('')||'<li class="mut">none</li>')+'</ul><b>'+s.excludes.length+' exclusion rules</b> from Windows (first 30):<ul>'+s.excludes.slice(0,30).map(r=>'<li><code>'+esc(r)+'</code></li>').join('')+'</ul><div class="mut">Apply them from the tray (Folders &rsaquo; Use the Windows Search scope) or with <code>semsearch roots import-windows</code>.</div></div>'}catch(e){$('#scope').innerHTML='<span class="warn">'+esc(e.message)+'</span>'}};
async function search(){const q=$('#q').value.trim();if(!q)return;$('#results').innerHTML='<div class="mut">searching...</div>';try{const r=await j('/search',{method:'POST',body:JSON.stringify({query:q,mode:$('#mode').value,limit:10})});$('#results').innerHTML=r.results.length?r.results.map(h=>'<div class="hit"><div class="p">'+esc(h.path)+' <span class="mut">'+h.score.toFixed(2)+' '+esc(h.match_type)+'</span></div><div class="x">'+esc(h.excerpt||'')+'</div></div>').join(''):'<div class="mut">no results</div>'}catch(e){$('#results').innerHTML='<div class="warn">'+esc(e.message)+'</div>'}}
$('#go').onclick=search;$('#q').addEventListener('keydown',e=>{if(e.key==='Enter')search()});
signIn().then(()=>{if(!SESS){$('#auth').textContent='Not signed in: open this page from the SemSearch tray icon (Open settings page).'}else{$('#auth').textContent='Signed in for this tab (session expires after 12 hours).'}status();config();setInterval(status,10000)});
</script></body></html>
"""
