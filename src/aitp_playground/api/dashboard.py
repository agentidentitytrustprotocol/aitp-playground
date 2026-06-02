"""Self-contained web dashboard for the playground.

A single buildless HTML page (inline CSS + vanilla JS) served at ``/dashboard``.
It is pure presentation over the existing JSON/SSE APIs — no AITP protocol
logic — honoring the playground invariant. Aesthetic: a dark "signal console"
for watching trust get established between agents in real time.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["dashboard"])

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AITP · trust console</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0a0c10; --panel:#0f131a; --panel2:#11161f; --line:#1d2532;
    --ink:#c9d4e3; --dim:#6b7787; --faint:#3a4452;
    --signal:#d4ff3f; --trust:#4fd6ff; --deny:#ff5c5c; --amber:#ffc04d; --deleg:#b48cff;
    --mono:'IBM Plex Mono',ui-monospace,monospace; --disp:'Syne',sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{
    background:
      linear-gradient(transparent 0 31px,rgba(120,140,170,.03) 31px 32px) 0 0/100% 32px,
      linear-gradient(90deg,transparent 0 31px,rgba(120,140,170,.03) 31px 32px) 0 0/32px 100%,
      radial-gradient(900px 500px at 85% -10%,rgba(79,214,255,.06),transparent 70%),
      var(--bg);
    color:var(--ink); font-family:var(--mono); font-size:13px; line-height:1.5;
    display:flex; flex-direction:column; min-height:100%;
  }
  a{color:var(--trust);text-decoration:none}
  header{
    display:flex;align-items:baseline;gap:18px;padding:18px 24px;border-bottom:1px solid var(--line);
    position:sticky;top:0;background:rgba(10,12,16,.86);backdrop-filter:blur(6px);z-index:5;
  }
  .brand{font-family:var(--disp);font-weight:800;font-size:20px;letter-spacing:-.5px;color:#fff}
  .brand b{color:var(--signal)}
  .tag{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.18em}
  #caps{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;align-items:center;justify-content:flex-end}
  .chip{font-size:10.5px;padding:3px 8px;border:1px solid var(--line);border-radius:999px;color:var(--dim);letter-spacing:.04em}
  .chip.on{color:var(--signal);border-color:rgba(212,255,63,.4);background:rgba(212,255,63,.06)}
  .chip.sdk{color:var(--trust);border-color:rgba(79,214,255,.35)}
  main{display:grid;grid-template-columns:300px 1fr 320px;gap:1px;background:var(--line);flex:1;min-height:0}
  .col{background:var(--bg);overflow:auto;min-height:0}
  .col::-webkit-scrollbar{width:8px}.col::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
  .sec{padding:16px 18px;border-bottom:1px solid var(--line)}
  .sec h2{font-family:var(--disp);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.2em;color:var(--dim);margin:0 0 12px}
  select,textarea,button{font-family:var(--mono);font-size:12.5px;width:100%;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;padding:9px 10px}
  select:focus,textarea:focus{outline:none;border-color:var(--trust)}
  textarea{resize:vertical;min-height:62px;margin-top:8px}
  button.go{margin-top:10px;background:var(--signal);color:#0a0c10;border:none;font-weight:600;cursor:pointer;letter-spacing:.04em;transition:filter .15s,transform .05s}
  button.go:hover{filter:brightness(1.08)} button.go:active{transform:translateY(1px)}
  .run{padding:9px 10px;border:1px solid var(--line);border-radius:6px;margin-bottom:7px;cursor:pointer;transition:border-color .15s,background .15s}
  .run:hover{border-color:var(--faint)} .run.active{border-color:var(--trust);background:rgba(79,214,255,.05)}
  .run .ref{color:var(--ink);font-size:11.5px;word-break:break-all}
  .run .meta{display:flex;justify-content:space-between;margin-top:4px;font-size:10.5px;color:var(--dim)}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:middle}
  .s-pending{color:var(--amber)} .s-pending .dot,.dot.pending{background:var(--amber);box-shadow:0 0 8px var(--amber)}
  .s-running{color:var(--trust)} .dot.running{background:var(--trust);box-shadow:0 0 8px var(--trust);animation:pulse 1s infinite}
  .s-complete{color:var(--signal)} .dot.complete{background:var(--signal)}
  .s-failed{color:var(--deny)} .dot.failed{background:var(--deny)}
  @keyframes pulse{50%{opacity:.35}}
  /* stream */
  #streamhead{display:flex;align-items:center;gap:10px;padding:14px 22px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:2}
  #streamhead .ref{font-family:var(--disp);font-weight:600;color:#fff;font-size:14px}
  #streamhead .live{margin-left:auto;font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.16em}
  .stream{padding:6px 0 40px}
  .ev{display:grid;grid-template-columns:64px 1fr;gap:14px;padding:5px 22px;position:relative;animation:slidein .25s ease both}
  @keyframes slidein{from{opacity:0;transform:translateX(-8px)}}
  .ev .t{color:var(--faint);font-size:10.5px;text-align:right;padding-top:2px;font-variant-numeric:tabular-nums}
  .ev .body{border-left:1px solid var(--line);padding:0 0 8px 16px;position:relative}
  .ev .body::before{content:"";position:absolute;left:-4.5px;top:6px;width:8px;height:8px;border-radius:50%;background:var(--faint)}
  .ev .ty{font-weight:600;font-size:12px;letter-spacing:.02em}
  .ev .det{color:var(--dim);font-size:11px;margin-top:1px;word-break:break-word}
  .ev[data-k=trust] .body::before{background:var(--trust);box-shadow:0 0 7px var(--trust)} .ev[data-k=trust] .ty{color:var(--trust)}
  .ev[data-k=deny] .body::before{background:var(--deny);box-shadow:0 0 7px var(--deny)} .ev[data-k=deny] .ty{color:var(--deny)}
  .ev[data-k=deleg] .body::before{background:var(--deleg);box-shadow:0 0 7px var(--deleg)} .ev[data-k=deleg] .ty{color:var(--deleg)}
  .ev[data-k=ok] .body::before{background:var(--signal);box-shadow:0 0 7px var(--signal)} .ev[data-k=ok] .ty{color:var(--signal)}
  .ev[data-k=warn] .body::before{background:var(--amber)} .ev[data-k=warn] .ty{color:var(--amber)}
  .empty{color:var(--dim);padding:50px 22px;text-align:center;font-size:12px}
  /* right rail */
  .met{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed var(--line);font-size:12px}
  .met b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
  .met span{color:var(--dim)}
  .cpstat{font-size:11px;color:var(--dim)} .cpstat.on{color:var(--signal)}
  footer{padding:8px 24px;border-top:1px solid var(--line);color:var(--faint);font-size:10.5px;display:flex;gap:16px}
</style>
</head>
<body>
<header>
  <span class="brand">AITP<b>·</b>console</span>
  <span class="tag">agent identity &amp; trust</span>
  <div id="caps"><span class="chip">probing…</span></div>
</header>
<main>
  <div class="col" id="left">
    <div class="sec">
      <h2>Launch scenario</h2>
      <select id="scenario"></select>
      <textarea id="inputs" spellcheck="false">{}</textarea>
      <button class="go" id="launch">▸ run</button>
    </div>
    <div class="sec">
      <h2>Runs</h2>
      <div id="runs"></div>
    </div>
  </div>
  <div class="col" id="center">
    <div id="streamhead"><span class="ref" id="cref">no run selected</span><span class="live" id="live"></span></div>
    <div class="stream" id="stream"><div class="empty">select a run to watch trust get established</div></div>
  </div>
  <div class="col" id="right">
    <div class="sec"><h2>Metrics</h2><div id="metrics"><span class="cpstat">loading…</span></div></div>
    <div class="sec"><h2>Control plane</h2><div id="cp"><span class="cpstat">checking…</span></div></div>
  </div>
</main>
<footer><span>SSE live</span><span>buildless · vanilla</span><span id="clock"></span></footer>
<script>
const $=s=>document.querySelector(s), api=p=>fetch(p).then(r=>r.json());
let activeRun=null, es=null;

function kind(t){
  if(/denied|failed|rejected|revoked|fault/.test(t)) return 'deny';
  if(/trust|handshake|established/.test(t)) return 'trust';
  if(/deleg/.test(t)) return 'deleg';
  if(/complete|established|issued|verified|provisioned|cache/.test(t)) return 'ok';
  if(/skip|renew|warn/.test(t)) return 'warn';
  return '';
}
function clock(){const d=new Date();$('#clock').textContent=d.toTimeString().slice(0,8)}
setInterval(clock,1000);clock();

async function caps(){
  try{const c=await api('/capabilities');const el=$('#caps');el.innerHTML='';
    const sdk=document.createElement('span');sdk.className='chip sdk';
    sdk.textContent=c.sdk_available?('aitp '+(c.sdk_version||'?')):'no wheel';el.appendChild(sdk);
    for(const[k,v]of Object.entries(c.features||{})){const s=document.createElement('span');
      s.className='chip'+(v?' on':'');s.textContent=k.replace(/_/g,' ');el.appendChild(s);}
  }catch(e){}
}
async function scenarios(){
  try{const d=await api('/scenarios');const sel=$('#scenario');
    sel.innerHTML=d.scenarios.map(s=>`<option value="${s.ref}">${s.ref}</option>`).join('');
  }catch(e){$('#scenario').innerHTML='<option>none</option>'}
}
async function runs(){
  try{const d=await api('/runs');const box=$('#runs');
    if(!d.runs.length){box.innerHTML='<span class="cpstat">no runs yet</span>';return}
    box.innerHTML=d.runs.slice().reverse().map(r=>{
      const st=r.status||'?';
      return `<div class="run ${activeRun===r.run_id?'active':''}" data-id="${r.run_id}">
        <div class="ref">${r.scenario_ref||r.run_id}</div>
        <div class="meta s-${st}"><span><i class="dot ${st}"></i>${st}</span><span>${r.event_count||0} ev</span></div></div>`;
    }).join('');
    box.querySelectorAll('.run').forEach(el=>el.onclick=()=>watch(el.dataset.id));
  }catch(e){}
}
function addEv(ev){
  const s=$('#stream');if(s.querySelector('.empty'))s.innerHTML='';
  if(ev.type==='stream.end'){$('#live').textContent='● ended';return}
  const t=ev.ts?new Date(ev.ts*1000).toTimeString().slice(0,8):'';
  const det=[ev.step_id,ev.capability,ev.agent||ev.agent_id,ev.target,ev.notes,ev.peer_aid]
    .filter(Boolean).join(' · ');
  const row=document.createElement('div');row.className='ev';row.dataset.k=kind(ev.type||'');
  row.innerHTML=`<div class="t">${t}</div><div class="body"><div class="ty">${ev.type||'event'}</div>${det?`<div class="det">${det}</div>`:''}</div>`;
  s.appendChild(row);s.parentElement.scrollTop=s.parentElement.scrollHeight;
}
function watch(id){
  activeRun=id;runs();
  $('#cref').textContent=id;$('#live').textContent='● live';
  $('#stream').innerHTML='';
  if(es)es.close();
  es=new EventSource('/runs/'+id+'/events');
  es.onmessage=m=>{try{addEv(JSON.parse(m.data))}catch(e){}};
  es.onerror=()=>{$('#live').textContent='● closed'};
}
async function launch(){
  let inputs={};try{inputs=JSON.parse($('#inputs').value||'{}')}catch(e){alert('inputs must be JSON');return}
  const ref=$('#scenario').value;
  const r=await fetch('/runs',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({scenario_ref:ref,inputs})}).then(r=>r.json());
  if(r.run_id){await runs();watch(r.run_id)}
}
$('#launch').onclick=launch;
async function metrics(){
  try{const txt=await fetch('/metrics').then(r=>r.text());const box=$('#metrics');const rows=[];
    txt.split('\n').forEach(l=>{if(l.startsWith('#')||!l.trim())return;
      const m=l.match(/^(aitp_playground_[a-z_]+)(\{[^}]*\})?\s+([\d.]+)$/);
      if(m&&+m[3]>0){const lbl=(m[2]||'').replace(/[{}"]/g,'').replace(/=/g,':');
        rows.push(`<div class="met"><span>${m[1].replace('aitp_playground_','')} ${lbl}</span><b>${(+m[3])}</b></div>`);}});
    box.innerHTML=rows.length?rows.join(''):'<span class="cpstat">no activity yet</span>';
  }catch(e){}
}
async function cp(){
  try{const d=await api('/cp/dashboard');const box=$('#cp');
    if(!d.cp_enabled){box.innerHTML='<span class="cpstat">not configured — set CP_BASE_URL</span>';return}
    const o=d.data||{};
    // The CP overview nests its counters under `kpis`; fall back to any
    // scalar top-level fields if that shape ever changes.
    const kpis=(o.kpis&&typeof o.kpis==='object')?o.kpis
      :Object.fromEntries(Object.entries(o).filter(([,v])=>typeof v!=='object'));
    const rows=Object.entries(kpis).map(([k,v])=>
      `<div class="met"><span>${k.replace(/([A-Z])/g,' $1').toLowerCase()}</span><b>${v}</b></div>`).join('');
    box.innerHTML='<span class="cpstat on">● connected</span>'+(rows||'<div class="met"><span>no data</span></div>');
  }catch(e){$('#cp').innerHTML='<span class="cpstat">unreachable</span>'}
}
caps();scenarios();runs();metrics();cp();
setInterval(()=>{runs();metrics();cp()},3000);
</script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Serve the single-file trust console."""
    return HTMLResponse(_PAGE)
