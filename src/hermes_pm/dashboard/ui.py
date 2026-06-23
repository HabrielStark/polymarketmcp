"""Embedded single-page dashboard UI (Section 17). Vanilla JS + WebSocket; no
build step. Money views are explicitly PAPER-labelled and stale/lock states are
shown prominently (17.2)."""

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Hermes-PM · PAPER Trading Lab</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--mut:#8b949e;
--grn:#3fb950;--red:#f85149;--amb:#d29922;--blu:#58a6ff;--pap:#bb8009;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--line);
background:var(--panel);position:sticky;top:0;z-index:5;flex-wrap:wrap}
h1{font-size:15px;margin:0;letter-spacing:.5px}
.badge{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700;border:1px solid}
.paper{background:#3a2d05;color:#f0c43a;border-color:var(--pap)}
.locked{background:#3a0d0d;color:#ff9b95;border-color:var(--red)}
.ok{background:#0d2818;color:var(--grn);border-color:var(--grn)}
.warn{background:#3a2d05;color:var(--amb);border-color:var(--amb)}
.spacer{flex:1}
button{background:#21262d;color:var(--fg);border:1px solid var(--line);border-radius:6px;
padding:6px 10px;cursor:pointer;font-family:inherit}
button:hover{border-color:var(--blu)}
button.danger{border-color:var(--red);color:#ff9b95}
select{background:#21262d;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px}
nav{display:flex;gap:4px;padding:8px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
nav button.active{border-color:var(--blu);color:var(--blu)}
main{padding:16px;display:grid;gap:16px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}
.card h3{margin:0 0 8px;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px}
.kpi{font-size:22px;font-weight:700}
.pos{color:var(--grn)}.neg{color:var(--red)}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:600}
.tag{font-size:10px;padding:1px 6px;border-radius:8px;border:1px solid var(--line);color:var(--mut)}
.stale{color:var(--amb)}.fresh{color:var(--grn)}
#timeline{max-height:60vh;overflow:auto;font-size:12px}
.evt{padding:4px 8px;border-bottom:1px solid var(--line);display:flex;gap:8px}
.evt .t{color:var(--mut);min-width:78px}
.evt .ty{color:var(--blu);min-width:150px}
.hide{display:none}
pre{white-space:pre-wrap;word-break:break-word;background:#0b0f14;padding:10px;border-radius:6px;
border:1px solid var(--line);max-height:50vh;overflow:auto}
.muted{color:var(--mut)}
</style></head>
<body>
<header>
  <h1>HERMES-PM</h1>
  <span class="badge paper">PAPER MODE</span>
  <span id="liveBadge" class="badge locked">LIVE LOCKED</span>
  <span id="connBadge" class="badge ok">WS …</span>
  <span id="staleBadge" class="badge ok">data fresh</span>
  <span class="spacer"></span>
  <select id="campSel" onchange="selectCampaign(this.value)"></select>
  <button onclick="refresh()">↻ refresh</button>
  <button onclick="exportAudit()">⬇ export audit</button>
  <button class="danger" onclick="emergency()">■ EMERGENCY STOP</button>
</header>
<nav id="tabs"></nav>
<main id="view"></main>
<script>
const $=(s,r=document)=>r.querySelector(s);
let CID=null, MARKETS=[], TAB="overview", EVENTS=[];
const TABS=["overview","watchlist","trades","timeline","sources","risk","learning","promotion"];
const fmt=(n)=>n==null?"—":(typeof n==="number"?n.toLocaleString(undefined,{maximumFractionDigits:4}):n);
const cls=(n)=>n>0?"pos":(n<0?"neg":"");
const DASH_TOKEN=localStorage.getItem("hpm_dashboard_token")||"";
function escapeHtml(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));}
const h=(v)=>escapeHtml(v);
const jsq=(v)=>escapeHtml(String(v??"").replace(/\\/g,"\\\\").replace(/'/g,"\\'"));
const path=(v)=>encodeURIComponent(String(v??""));
async function api(p,opt={}){opt.headers={...(opt.headers||{})};
 if(DASH_TOKEN)opt.headers.Authorization=`Bearer ${DASH_TOKEN}`;
 const r=await fetch(p,opt);if(!r.ok)throw new Error(p+" "+r.status);return r.json();}

function renderTabs(){const n=$("#tabs");n.innerHTML="";TABS.forEach(t=>{const b=document.createElement("button");
 b.textContent=t.toUpperCase();b.className=t===TAB?"active":"";b.onclick=()=>{TAB=t;renderTabs();render();};n.appendChild(b);});}

async function loadCampaigns(){const cs=await api("/api/campaigns");const sel=$("#campSel");sel.innerHTML="";
 if(!cs.length){sel.innerHTML="<option>no campaigns</option>";return;}
 cs.forEach(c=>{const o=document.createElement("option");o.value=c.campaign_id;
  o.textContent=`${c.name} · ${c.status}`;sel.appendChild(o);});
 if(!CID)CID=cs[0].campaign_id;sel.value=CID;}
function selectCampaign(v){CID=v;render();}

async function loadStatus(){try{const s=await api("/api/status");
 $("#liveBadge").className="badge "+(s.live_adapter_enabled?"warn":"locked");
 $("#liveBadge").textContent=s.live_adapter_enabled?"LIVE ENABLED":"LIVE LOCKED";
 const stale=s.stale_tokens>0||s.connectivity_lost;
 $("#staleBadge").className="badge "+(stale?"warn":"ok");
 $("#staleBadge").textContent=stale?`STALE ${s.stale_tokens}`:"data fresh";
 if(s.emergency_stop){$("#liveBadge").textContent="EMERGENCY STOP";$("#liveBadge").className="badge locked";}
 }catch(e){}}

async function render(){await loadStatus();const v=$("#view");v.innerHTML="<div class='muted'>loading…</div>";
 try{ if(TAB==="overview")await vOverview(v);
  else if(TAB==="watchlist")await vWatch(v);
  else if(TAB==="trades")await vTrades(v);
  else if(TAB==="timeline")vTimeline(v);
  else if(TAB==="sources")await vSources(v);
  else if(TAB==="risk")await vRisk(v);
  else if(TAB==="learning")await vLearning(v);
  else if(TAB==="promotion")await vPromotion(v);
 }catch(e){v.innerHTML=`<div class='card'>error: ${h(e.message)}</div>`;}}

async function vOverview(v){if(!CID){v.innerHTML="<div class='card'>No campaign.</div>";return;}
 const r=await api(`/api/campaign/${CID}/report`);const p=r.portfolio,m=r.metrics;
 v.innerHTML=`<div class="grid">
  <div class="card"><h3>Equity <span class="badge paper">PAPER</span></h3><div class="kpi">$${fmt(p.equity)}</div></div>
  <div class="card"><h3>Net P&L <span class="badge paper">PAPER</span></h3><div class="kpi ${cls(p.net_pnl)}">$${fmt(p.net_pnl)}</div></div>
  <div class="card"><h3>Max Drawdown</h3><div class="kpi neg">$${fmt(p.max_drawdown)}</div></div>
  <div class="card"><h3>Cash</h3><div class="kpi">$${fmt(p.cash)}</div></div>
  <div class="card"><h3>Realized</h3><div class="kpi ${cls(p.realized_pnl)}">$${fmt(p.realized_pnl)}</div></div>
  <div class="card"><h3>Unrealized</h3><div class="kpi ${cls(p.unrealized_pnl)}">$${fmt(p.unrealized_pnl)}</div></div>
  <div class="card"><h3>Ledger</h3><div class="kpi">${p.ledger_balanced?"✓ balanced":"✗ UNBALANCED"}</div></div>
  <div class="card"><h3>Sample size</h3><div class="kpi">${fmt(m.decision_sample_size)} <span class="muted" style="font-size:12px">intents</span></div></div>
 </div>
 <div class="card"><h3>Open Positions <span class="badge paper">PAPER</span></h3>
  <table><tr><th>Token</th><th>Shares</th><th>Avg</th><th>Mark</th><th>Unrealized</th></tr>
  ${(p.open_positions||[]).map(x=>`<tr><td>${h(x.token_id)}</td><td>${fmt(x.shares)}</td><td>${fmt(x.avg_price)}</td><td>${fmt(x.mark_price)}</td><td class="${cls(x.unrealized_pnl)}">${fmt(x.unrealized_pnl)}</td></tr>`).join("")||"<tr><td colspan=5 class=muted>none</td></tr>"}
  </table></div>
 <div class="card"><h3>Quality metrics</h3><table>
  <tr><th>Hit rate</th><td>${fmt(m.hit_rate)}</td><th>Profit factor</th><td>${fmt(m.profit_factor)}</td></tr>
  <tr><th>Brier</th><td>${fmt(m.brier_score)}</td><th>Baseline edge</th><td>${fmt(m.market_baseline_edge)}</td></tr>
  <tr><th>Slippage err</th><td>${fmt(m.slippage_model_error)}</td><th>Risk rejections</th><td>${fmt(m.risk_rejections)}</td></tr>
 </table></div>`;}

async function vWatch(v){MARKETS=await api("/api/markets");
 v.innerHTML=`<div class="card"><h3>Market Watchlist</h3><table>
  <tr><th>Market</th><th>Cat</th><th>Tradable</th><th>Resolution</th><th>Order book</th></tr>
  ${MARKETS.map(m=>`<tr><td>${h(m.market_id)}</td><td>${h(m.category)}</td>
   <td>${m.tradable?"<span class=tag style='color:var(--grn)'>yes</span>":"<span class=tag style='color:var(--red)'>"+h((m.tradable_reasons||[]).join(","))+"</span>"}</td>
   <td>${m.has_clear_resolution?"clear":"<span class=stale>ambiguous</span>"}</td>
   <td>${m.enable_order_book?"on":"off"}</td></tr>`).join("")}
 </table></div>`;}

async function vTrades(v){if(!CID){v.innerHTML="<div class='card'>No campaign.</div>";return;}
 const orders=await api(`/api/campaign/${path(CID)}/orders`);
 v.innerHTML=`<div class="card"><h3>Trades <span class="badge paper">PAPER</span> — click a row for "why did this happen?"</h3>
  <table><tr><th>Order</th><th>Side</th><th>Status</th><th>Size $</th><th>Filled $</th><th>Fills</th></tr>
  ${orders.map(o=>`<tr style="cursor:pointer" onclick="tradeDetail('${jsq(o.intent_id)}')">
   <td>${h(String(o.order_id||"").slice(0,12))}</td><td>${h(o.side)}</td><td>${h(o.status)}</td>
   <td>${fmt(o.size_usd)}</td><td>${fmt(o.filled_size_usd)}</td><td>${(o.fills||[]).length}</td></tr>`).join("")
   ||"<tr><td colspan=6 class=muted>no orders yet</td></tr>"}
  </table></div><div id="tradeDetail"></div>`;}
async function tradeDetail(iid){const d=await api(`/api/campaign/${path(CID)}/trade/${path(iid)}`);
 const rd=(d.risk_decisions[0]||{});
 $("#tradeDetail").innerHTML=`<div class="card"><h3>Why did this happen? <span class="badge paper">PAPER</span></h3>
  <table>
   <tr><th>Thesis</th><td>${escapeHtml(d.thesis||"")}</td></tr>
   <tr><th>Counter-thesis</th><td>${escapeHtml(d.counter_thesis||"")}</td></tr>
   <tr><th>Invalidation</th><td>${escapeHtml(d.invalidation_criteria||"")}</td></tr>
   <tr><th>Resolution rules</th><td>${escapeHtml(d.resolution_rules||"")}</td></tr>
   <tr><th>EV / break-even</th><td>${fmt(d.intent.normalized_ev)} / ${fmt(d.intent.break_even_probability)}</td></tr>
   <tr><th>Risk decision</th><td>${h(rd.result||"—")} ${rd.violated_rules&&rd.violated_rules.length?"· "+h(rd.violated_rules.join(", ")):""} <span class=muted>(${h(rd.policy_version||"")})</span></td></tr>
   <tr><th>Position</th><td>${d.position?("shares "+fmt(d.position.shares)+" @ "+fmt(d.position.avg_price)+" · realized "+fmt(d.position.realized_pnl)):"—"}</td></tr>
  </table>
  <h3 style="margin-top:10px">Evidence (sanitized · untrusted)</h3>
  <table><tr><th>Adapter</th><th>Class</th><th>Stance</th><th>Source ref</th></tr>
   ${(d.evidence||[]).map(e=>`<tr><td>${h(e.adapter)}</td><td>${h(e.source_type)}</td><td>${h(e.stance)}</td><td class=muted>${h(e.source_ref)}</td></tr>`).join("")||"<tr><td colspan=4 class=muted>none</td></tr>"}</table>
  <h3 style="margin-top:10px">Entry order-book snapshot + fills (replay source)</h3>
  <pre>${escapeHtml(JSON.stringify({entry_order_book:d.entry_order_book,fills:d.fills},null,2))}</pre></div>`;}

function vTimeline(v){v.innerHTML=`<div class="card"><h3>Agent / System Timeline (live)</h3>
 <input id="tlSearch" placeholder="filter events… (type to search)" oninput="filterTimeline(this.value)"
  style="width:100%;margin-bottom:8px;background:#0b0f14;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px"/>
 <div id="timeline">${renderEvents(EVENTS)}</div></div>`;}
function renderEvents(list){return list.map(evHtml).join("")||"<div class=muted style='padding:8px'>waiting for events…</div>";}
function filterTimeline(q){q=(q||"").toLowerCase();
 const f=q?EVENTS.filter(e=>(e.type+JSON.stringify(e.data)).toLowerCase().includes(q)):EVENTS;
 const tl=$("#timeline");if(tl)tl.innerHTML=renderEvents(f);}
function evHtml(e){const ts=new Date(e.ts).toLocaleTimeString();
 return `<div class="evt"><span class="t">${h(ts)}</span><span class="ty">${h(e.type)}</span><span>${h(JSON.stringify(e.data))}</span></div>`;}

async function vSources(v){MARKETS=MARKETS.length?MARKETS:await api("/api/markets");
 const opts=MARKETS.map(m=>`<option value="${h(m.market_id)}">${h(m.market_id)} · ${h(m.category)}</option>`).join("");
 v.innerHTML=`<div class="card"><h3>Source Intelligence</h3>
  <select id="mSel" onchange="loadSig(this.value)">${opts}</select>
  <div id="sigOut" class="muted" style="margin-top:10px">select a market…</div></div>`;
 if(MARKETS[0])loadSig(MARKETS[0].market_id);}
async function loadSig(mid){const d=await api(`/api/market/${path(mid)}/signals`);const s=d.summary;
 $("#sigOut").innerHTML=`<table>
  <tr><th>Net stance</th><td>${h(s.stance)} (${fmt(s.net_stance_score)})</td><th>Disagreement</th><td>${fmt(s.disagreement)}</td></tr>
  <tr><th>Avg trust</th><td>${fmt(s.avg_trust)}</td><th>Tainted</th><td>${fmt(s.suspected_injection_count)}</td></tr>
  <tr><th>By class</th><td colspan=3>${h(JSON.stringify(s.by_source_class||{}))}</td></tr></table>
  <h3 style="margin-top:10px">Evidence (sanitized · untrusted)</h3>
  <table><tr><th>Adapter</th><th>Class</th><th>Stance</th><th>Trust</th><th>Source ref</th></tr>
  ${(d.evidence||[]).map(e=>`<tr><td>${h(e.adapter)}</td><td>${h(e.source_type)}</td><td>${h(e.stance)}</td><td>${fmt(e.trust_score)}</td><td class="muted">${h(e.source_ref)}${e.suspected_injection?" <span class=stale>⚠inj</span>":""}</td></tr>`).join("")}
  </table>`;}

async function vRisk(v){const a=await api(`/api/audit?campaign_id=${path(CID)}&limit=200`);
 const rd=a.events.filter(e=>e.type==="risk_decision");
 v.innerHTML=`<div class="card"><h3>Risk Console · chain ${a.chain.ok?"<span style='color:var(--grn)'>verified ✓</span>":"<span style='color:var(--red)'>BROKEN</span>"}</h3>
  <table><tr><th>Decision</th><th>Result</th><th>Violations / reasons</th><th>Policy</th></tr>
  ${rd.map(e=>{const o=(e.payload&&e.payload.outputs)||{};return `<tr><td>${h(String(o.decision_id||'').slice(0,12))}</td>
   <td>${h(o.result)}</td><td class="muted">${h((o.violated_rules&&o.violated_rules.join(", "))||(o.reasons&&o.reasons.join("; "))||"")}</td>
   <td class="muted">${h(o.policy_version||"")}</td></tr>`;}).join("")||"<tr><td colspan=4 class=muted>no risk decisions yet</td></tr>"}
  </table></div>`;}

async function vLearning(v){const ls=await api(`/api/audit?campaign_id=${path(CID)}&limit=200`);
 const lessons=ls.events.filter(e=>e.type==="lesson_written").map(e=>e.payload.outputs);
 const pm=ls.events.filter(e=>e.type==="postmortem").map(e=>e.payload.outputs);
 v.innerHTML=`<div class="card"><h3>Lessons (compact)</h3><table>
  <tr><th>Trigger</th><th>Rule</th><th>Memory</th><th>Support</th></tr>
  ${lessons.map(l=>`<tr><td>${h(l.trigger)}</td><td>${h(l.rule)}</td><td>${h(l.memory_target)}</td><td>${fmt(l.supporting_evidence_count)}</td></tr>`).join("")||"<tr><td colspan=4 class=muted>none</td></tr>"}
  </table></div>
  <div class="card"><h3>Postmortems</h3><table><tr><th>Outcome</th><th>Failure mode</th><th>Drivers</th></tr>
  ${pm.map(p=>`<tr><td>${h(p.outcome)}</td><td>${h(p.failure_mode)}</td><td class=muted>${h((p.drivers||[]).join(", "))}</td></tr>`).join("")||"<tr><td colspan=3 class=muted>none</td></tr>"}
  </table></div>`;}

async function vPromotion(v){if(!CID){v.innerHTML="<div class='card'>No campaign.</div>";return;}
 const r=await api(`/api/campaign/${path(CID)}/promotion`);const ve=r.verdicts;
 v.innerHTML=`<div class="grid">
  <div class="card"><h3>Statistically weak</h3><div class="kpi ${ve.statistically_weak?'neg':'pos'}">${ve.statistically_weak}</div></div>
  <div class="card"><h3>Operationally safe</h3><div class="kpi ${ve.operationally_safe?'pos':'neg'}">${ve.operationally_safe}</div></div>
  <div class="card"><h3>Compliance eligible</h3><div class="kpi ${ve.compliance_eligible?'pos':'neg'}">${ve.compliance_eligible}</div></div>
 </div>
 <div class="card"><h3>Recommendation</h3><pre>${escapeHtml(r["8_recommendation"])}</pre>
  ${r.sample_size_warning?`<p class="stale">${escapeHtml(r.sample_size_warning)}</p>`:""}</div>
 <div class="card"><h3>Full report</h3><pre>${escapeHtml(JSON.stringify(r,null,2))}</pre></div>`;}

async function emergency(){if(!confirm("Engage EMERGENCY STOP? Cancels open paper orders and freezes campaigns."))return;
 await api("/api/emergency_stop"+(CID?`?campaign_id=${path(CID)}`:""),{method:"POST"});await refresh();}
async function refresh(){await loadCampaigns();await render();}
async function exportAudit(){const url=CID?`/api/campaign/${path(CID)}/audit/export`:`/api/audit`;
 const data=await api(url);const blob=new Blob([JSON.stringify(data,null,2)],{type:"application/json"});
 const a=document.createElement("a");a.href=URL.createObjectURL(blob);
 a.download=`audit_${CID||"all"}.json`;a.click();URL.revokeObjectURL(a.href);}

function connectWS(){const proto=location.protocol==="https:"?"wss":"ws";
 const ws=DASH_TOKEN?new WebSocket(`${proto}://${location.host}/ws`,[`hpm-token-${DASH_TOKEN}`]):new WebSocket(`${proto}://${location.host}/ws`);
 ws.onopen=()=>{$("#connBadge").className="badge ok";$("#connBadge").textContent="WS live";};
 ws.onclose=()=>{$("#connBadge").className="badge warn";$("#connBadge").textContent="WS down";setTimeout(connectWS,1500);};
 ws.onmessage=(m)=>{const e=JSON.parse(m.data);EVENTS.unshift(e);if(EVENTS.length>400)EVENTS.pop();
  if(["emergency_stop","book_stale","connectivity","risk_decision","fill"].includes(e.type))loadStatus();
  if(TAB==="timeline"){const tl=$("#timeline");if(tl)tl.insertAdjacentHTML("afterbegin",evHtml(e));}};}

renderTabs();connectWS();refresh();setInterval(loadStatus,4000);
</script></body></html>
"""
