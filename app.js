"use strict";
const $=id=>document.getElementById(id),TZ="America/Mexico_City";
// Constructor DOM seguro: todo texto va por textContent; estilos por CSSOM (compatible con CSP).
function h(tag,cls,text,kids,style){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=String(text);
 (kids||[]).forEach(k=>k!=null&&e.appendChild(k instanceof Node?k:document.createTextNode(String(k))));if(style)Object.assign(e.style,style);return e}
const put=(id,...kids)=>$(id).replaceChildren(...kids.flat(Infinity).filter(k=>k!=null));
const dim=t=>h("span","dim",t),sp=()=>" ";
const RC={liquidity:"#FF3040",volume_24h:"#FF8A00",lp_lock:"#FFEA00",copycat:"#9D4DFF",rpc:"#00B7FF","[rpc]":"#00B7FF",top_holder:"#FF00E6",top10:"#C77DFF",pass:"#00FF41",other:"#888"};
const RN={liquidity:"liquidez",volume_24h:"volumen",lp_lock:"LP lock",copycat:"copycat",rpc:"rpc",top_holder:"top holder",top10:"top10"};
const col=r=>RC[r]||RC.other,rn=r=>RN[r]||r;
let chart,src="";
setInterval(()=>{$("clock").textContent=new Date().toLocaleTimeString("es-MX",{timeZone:TZ,hour12:false})},1000);
const LOCAL=["localhost","127.0.0.1"].includes(location.hostname);
const GH_API="https://api.github.com/repos/YagoAlcocer/jev-trading-desk/contents/data.json?ref=data";
// Sondeo con ETag: If-None-Match manual (cache:no-store); 304 = sin cambios, no se vuelve a dibujar.
let etag=null,hasData=false,lastCheck=null,genMs=null,genTxt="",netNote="",quota=null,nextAt=null;
// Ritmo adaptativo: GitHub cuenta también los 304 sin autenticar (60/h por IP), así que el intervalo
// es max(15 s, segundos hasta el reinicio / peticiones restantes).
function pace(r){const rem=+r.headers.get("x-ratelimit-remaining"),rst=+r.headers.get("x-ratelimit-reset");
 if(!r.headers.get("x-ratelimit-remaining")||!rst)return 15000;quota=rem;const left=Math.max(0,rst*1000-Date.now());
 return rem>0?Math.max(15000,Math.ceil(left/rem)):left+2000}
let delay=15000;
async function getJSON(u,o){const r=await fetch(u,Object.assign({cache:"no-store"},o||{}));if(!r.ok)throw new Error("HTTP "+r.status);return r.json()}
function showErr(msg){netNote=msg;if(!hasData)put("metrics",h("span","badge b-err","SIN DATOS")," ",h("span","rd",msg));tick()}
async function load(){let d;
 try{
  if(LOCAL){d=await getJSON("/api/data");src="EN VIVO /api/data"}
  else{const hd={Accept:"application/vnd.github.raw"};if(etag&&hasData)hd["If-None-Match"]=etag;
   const r=await fetch(GH_API,{cache:"no-store",headers:hd});lastCheck=Date.now();delay=pace(r);
   if(r.status===304){netNote="";tick();return}
   if(!r.ok){const rem=r.headers.get("x-ratelimit-remaining"),rst=r.headers.get("x-ratelimit-reset");
    const lim=(r.status===403||r.status===429)&&(rem==="0"||r.status===429);
    throw new Error("HTTP "+r.status+(lim?" · LÍMITE DE TASA"+(rst?" HASTA "+new Date(rst*1000).toLocaleTimeString("es-MX",{timeZone:TZ,hour12:false})+" CDMX":""):""))}
   const et=r.headers.get("ETag")||null;
   if(et&&et===etag&&hasData){netNote="";tick();return}   // algunos navegadores entregan 200 con el mismo ETag: tampoco se redibuja
   d=await r.json();etag=et;src="CASI EN VIVO · API GITHUB RAMA data · ETAG"}
  lastCheck=Date.now();netNote="";
 }catch(e){const why=String(e&&e.message||e);
  try{d=await getJSON("data.json");lastCheck=Date.now();src=`RESPALDO ./data.json (${LOCAL?"/api/data":"API GITHUB"} FALLÓ: ${why})`;netNote=`${LOCAL?"/api/data":"API GITHUB"}: ${why} · USANDO ./data.json`}
  catch(e2){showErr(`${LOCAL?"/api/data":"API GITHUB"}: ${why} · ./data.json: ${String(e2&&e2.message||e2)}${hasData?" · SE MANTIENEN LOS ÚLTIMOS DATOS":""}`);return}}
 if(d===null||typeof d!=="object"||Array.isArray(d)){showErr("DATOS INVÁLIDOS");return}
 if(d.error){showErr("ERROR: "+String(d.error));return}
 hasData=true;genTxt=String(d.generated||"");genMs=parseCdmx(genTxt);
 render(d);tick()}
// Hora CDMX (UTC-6 fijo desde 2022, sin horario de verano) -> epoch ms
function parseCdmx(t){const m=String(t).match(/(\d+)-(\d+)-(\d+)[ T](\d+):(\d+)(?::(\d+))?/);return m?Date.UTC(+m[1],m[2]-1,+m[3],+m[4]+6,+m[5],+(m[6]||0)):null}
function tick(){const el=$("live");if(!el)return;const now=Date.now(),kids=[];
 if(genMs!=null){const age=Math.max(0,Math.round((now-genMs)/1000));
  kids.push(h("span","m","DATOS DE",[h("b",age>600?"rd":age>120?"ye":"gr",`${genTxt.slice(11,19)} CDMX · HACE ${age} S`)]))}
 else kids.push(h("span","m","DATOS DE",[h("b","dim",hasData?"SIN HORA":"ESPERANDO")]));
 kids.push(h("span","m","ÚLTIMA REVISIÓN",[h("b",null,(lastCheck?`HACE ${Math.max(0,Math.round((now-lastCheck)/1000))} S`:"—")+(nextAt?` · PRÓXIMA EN ${Math.max(0,Math.round((nextAt-now)/1000))} S`:"")+(quota!=null?` · CUOTA ${quota}/60`:""))]));
 if(netNote)kids.push(h("span","badge b-err",netNote));
 el.replaceChildren(...kids)}
setInterval(tick,1000);
const lastv=a=>a&&a.length?a[a.length-1].v:null,fusd=v=>v==null?"SIN DATO":"$"+Number(v).toFixed(2);
const m=(k,v)=>h("span","m",k,[typeof v==="string"?h("b",null,v):h("b",null,null,[v])]);
const firstRule=f=>f.result==="reject"?(String(f.rule||"").split(",")[0]||"other"):"pass";
function prob(f){const a=f.jev_answers;if(a==null)return null;if(typeof a==="number")return a;
 if(typeof a==="object"){for(const k of["prob","probability","p","conf","confidence"])if(a[k]!=null)return a[k]}return null}
const kv=(k,v,cls)=>h("div","kv",null,[h("span",null,k),h("b",cls||null,v)]);
const cdmxNow=()=>{const p=Object.fromEntries(new Intl.DateTimeFormat("en-CA",{timeZone:"America/Mexico_City",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).formatToParts(new Date()).map(x=>[x.type,x.value]));return Date.UTC(+p.year,p.month-1,+p.day,+p.hour,+p.minute,+p.second)};
const agoMin=t=>{if(!t)return null;const m=String(t).match(/(\d+)-(\d+)-(\d+)[ T](\d+):(\d+)(?::(\d+))?/);if(!m)return null;return (cdmxNow()-Date.UTC(+m[1],m[2]-1,+m[3],+m[4],+m[5],+(m[6]||0)))/60000};
function render(d){
 const c=(d.curves||{})[d.phase]||Object.values(d.curves||{})[0]||{};
 put("metrics",m("LOOP",d.loop_running?h("span","badge b-run","VIVO"):h("span","badge b-err","CAÍDO")),
  m("ÚLTIMO CICLO",String(d.last_cycle??"SIN DATO")),m("ESCANEADOS HOY",String(d.scanned_today??0)),m("PREGUNTAS A JEV",String(d.jev?.questions_answered??0)),
  m("EQUITY JEV",fusd(lastv(c.jev))),m("REGLA SIMPLE",fusd(lastv(c.simple))),m("HOLD BTC",fusd(lastv(c.hold_btc))),m("HOLD SOL",fusd(lastv(c.hold_sol))),
  d.kill?m("KILL",h("span","badge b-err","ACTIVO")):null,m("CADENA",d.chain_ok===true?h("span","gr","OK"):(typeof d.chain==="string"&&d.chain.includes("ok=False"))?h("span","rd","ROTA"):h("span","ye","SIN VERIFICAR")),m("FASE",(d.jev?.questions_answered??0)>0?`${d.phase??""} D${d.day??""}`:"ESPERANDO PRIMER CANDIDATO"));
 const names={escaner:"Escáner",filtros:"Filtros",jev:"Jev",riesgo:"Riesgo",ejecucion:"Ejecución papel",reporte:"Reporte"};
 const DL=decLast(d),othersOn=["escaner","filtros","jev","riesgo"].every(x=>{const a=agoMin(DL[x]||(d.operators||{})[x]?.last);return a!=null&&a<=10});
 put("ops",Object.entries(names).map(([k,n])=>{const o0=(d.operators||{})[k]||{},o=DL[k]?{last:DL[k]}:o0;let st,cl,cc;
  const am=agoMin(o.last),stale=agoMin(d.generated)>10;
  if(!d.loop_running||stale){st=stale?"SIN DATOS":"ERR";cl="err";cc="#FF3040"}else if(am==null){st="IDLE";cl="idle";cc="#555"}
  else if(am<=3){const r=k==="reporte";st=r?"SYNC":"RUN";cl=r?"sync":"run";cc=r?"#9D4DFF":"#00FF41"}
  else if(am<=10){st="ACTIVE";cl="active";cc="#FFEA00"}else if(k==="ejecucion"&&othersOn){st="SIN TRADES";cl="idle";cc="#555"}else{st="STALE";cl="err";cc="#FF3040"}
  const on=cl!=="idle"&&cl!=="err",bars=h("div","bars"+(on?" on":""),null,[h("i")]);bars.style.setProperty("--c",cc);
  return h("div","op",null,[h("div","row",null,[h("span","n",n),h("span",null,null,[DL[k]?h("small","dim","DECISIÓN "):null,dim(o.last?String(o.last).slice(11)+" CDMX"+(am!=null?" · HACE "+Math.max(0,Math.round(am))+" MIN":""):"SIN ACCIÓN")," ",h("span","badge b-"+cl,st)])]),bars])}));
 const F=d.feed||[],l30=F.slice(0,30);
 const cells=l30.map(f=>{const r=firstRule(f),e=h("div",null,null,null,{background:col(r),boxShadow:"0 0 4px "+col(r)});e.title=`${f.t} CDMX · ${f.token} · ${f.result} · ${f.rule}\n${f.reason}`;return e});
 while(cells.length<30){const e=h("div",null,null,null,{background:"#1a141a"});e.title="sin decisión";cells.push(e)}
 put("matrix",cells);
 put("mlegend",[...new Set(l30.map(firstRule))].map(r=>h("span",null,null,[h("i",null,null,null,{background:col(r)}),r==="pass"?"pasó":rn(r)])),dim("COLOR = PRIMERA REGLA QUE RECHAZÓ"));
 const E=d.loop_errors||[];put("errs",E.length?E.slice().reverse().map(e=>h("div","ln",null,[dim(e.t)," ",h("span","rd","ERR")," ",String(e.error??"")])):[h("span","gr","SIN ERRORES REGISTRADOS")]);
 const L=d.last_token;
 if(L){$("lt").textContent=`${L.token} · ${L.t} CDMX`;const st=L.stages||[],fi=st.findIndex(s=>String(s).toLowerCase()===String(L.first_stage||"").toLowerCase()),out=[];
  st.forEach((s,i)=>{let k="";if(L.result==="reject"){if(i<fi||i===0)k="ok";if(i===fi)k="fail"}else k="ok";if(i)out.push(h("b","dim","→"));out.push(h("span",k,s))});
  put("track",out);put("ltr","RESULTADO ",h("b",L.result==="reject"?"rd":"gr",L.result)," · ",String(L.reason??""))}else{put("track",dim("SIN DATO"));put("ltr")}
 const fu=d.funnel||{},stg=(d.funnel_stages||{}).hoy||[],mx=Math.max(1,fu.scanned||1);
 put("funnel",kv("ESCANEADOS",String(fu.scanned??0)),kv("PASARON",String(fu.passed??0),fu.passed?"gr":"dim"),kv("RECHAZADOS",String(fu.rejected??0),"rd"),
  h("div","dim small sub","ETAPA DONDE CAYERON (HOY)"),stg.map(([n,v])=>h("div","fr",null,[h("span",null,n),h("b",null,v),h("div","bar",null,[h("i",null,null,null,{width:(100*v/mx)+"%"})])])),
  h("div","dim small sub","REGLAS INCUMPLIDAS (UN TOKEN PUEDE FALLAR VARIAS)"),(fu.by_rule||[]).map(([r,v])=>h("div","kv",null,[h("span",null,rn(r),null,{color:col(r)}),h("b",null,v)])));
 renderPos(d);renderHist(d);
 const C=d.controls||{};
 put("ctl",Object.entries(C.lanes||{}).map(([n,l])=>[h("div","kv",null,[h("span",null,"CARRIL "+String(n).toUpperCase()),h("span","badge "+(l.state==="active"?"b-active":"b-err"),String(l.state).toUpperCase())]),l.reason?h("div","dim small",l.reason):null]),
  kv("FACTOR DE RIESGO",String(C.risk_factor??"SIN DATO")),kv("CONTROLES VÁLIDOS",C.valid?"SÍ":"NO",C.valid?"gr":"rd"),
  kv("ENTRADAS / META",`${d.trade_progress?.entries_logged??0} / ${d.trade_progress?.target??"?"}`),kv("MODELO JEV",String(d.jev?.model??"SIN DATO"),"small"));
 const H=[...(d.control_history||[]),...(d.adjustments||[])];
 put("hist",H.length?H.map(x=>h("div","ln small",null,[dim(x.t)," ",h("span","mg",x.kind||x.cmd||x.command||"")," ",x.detail?.decided_by?"por "+x.detail.decided_by:""])):[dim("SIN COMANDOS")]);
 renderLog2(d);
 GX.update(d);
 $("vxlabel").textContent=`GALAXIA DECORATIVA · CUERPOS = DATOS REALES · ÚLTIMO ESCANEO ${d.last_scan??"SIN DATO"} CDMX`;
 drawChart(d,c);
 $("foot").textContent=`FUENTE: ${src} · GENERADO ${d.generated??"?"} CDMX · ${d.chain??""} · REFRESCO ${LOCAL?"10 S":"≥15 S CON ETAG (SEGÚN CUOTA)"} · SOLO LECTURA`}
// Bitácora en 2 columnas: izquierda rechazos del filtro (símbolo + regla); derecha monedas que pasaron el filtro con resultado por estrategia.
const LOG_SEEN={rej:null,pass:null};
const sStr=v=>v==null||v===""?ND:String(v);
function logCol(id,rows,keyf,kind,mk,empty){const prev=LOG_SEEN[kind],ks=rows.map(keyf);LOG_SEEN[kind]=new Set(ks);
 put(id,rows.length?rows.map((r,i)=>{const e=mk(r);if(prev&&!prev.has(ks[i]))e.classList.add("new");return e}):[dim(empty)])}
// labels1: limit labels (display only). Raw reason goes to the title attribute via setAttribute (never innerHTML).
function limitLabel(o){const k=o&&o.limit_kind,r=String((o&&o.reason)||""),ru=o&&o.rule;
 if(k==="global_cap"){const m=/max (\d+) concurrent/.exec(r);return "LÍMITE DE POSICIONES"+(m?" ("+m[1]+")":"")}
 if(k==="per_mint_max"){const m=/max (\d+)\)/.exec(r);return m?"MÁX. "+m[1]+" ENTRADAS POR MONEDA":"MÁX. DE ENTRADAS POR MONEDA"}
 if(k==="rebuy_used")return "ADD-ON NO PERMITIDO";
 if(k==="pyramid_loss"||k==="pyramid_no_mark")return "RECOMPRA SOLO EN GANANCIA";
 if(k==="exposure"||ru==="exposure")return "TOPE DE EXPOSICIÓN";
 if(ru==="position_limit")return "LÍMITE: MOTIVO NO RECONOCIDO";
 return sStr(ru)}
function withTitle(e,o){const t=o&&(o.reason!=null&&o.reason!==""?o.reason:o.rule);if(t!=null)e.setAttribute("title",String(t));return e}
function simpleTag(o){if(!o||!o.outcome)return h("span","otag o-nd","SIMPLE: "+ND);
 if(o.outcome==="ENTRO")return h("span","otag o-in","SIMPLE: ENTRÓ");
 if(o.outcome==="BLOQUEADA")return withTitle(h("span","otag o-blk","SIMPLE: BLOQUEADA ("+limitLabel(o)+")"),o);
 return withTitle(h("span","otag o-out","SIMPLE: RECHAZÓ · "+sStr(o.rule)),o)}
function jevTag(o,t){if(!o||!o.outcome)return h("span","otag o-nd","JEV: "+ND);
 if(o.outcome==="ENTRO")return h("span","otag o-in","JEV: ENTRÓ");
 // antes: question=null con rule jev_Q4b_below caía en "PREGUNTA SIN DATO"
 if(/^jev_Q4b_/i.test(String(o.rule||""))||o.question==="Q4b"){const n=q4bScore(o);
  return h("span","otag o-out","JEV: RECHAZÓ · Q4b BAJA ("+(n!=null?n+"/10":"PUNTAJE "+ND)+")")}
 if(o.rule==="cycle_entry_limit")return withTitle(h("span","otag o-blk","JEV: BLOQUEADA (LÍMITE DE ENTRADAS POR CICLO)"),o);
 if(o.rule==="jev_unavailable")return withTitle(h("span","otag o-nd","JEV: SIN RESPUESTA (SERVICIO NO DISPONIBLE)"),o);
 if(o.rule==="position_limit"||o.rule==="exposure")return withTitle(h("span","otag o-blk","JEV: BLOQUEADA ("+limitLabel(o)+")"),o);
 // "PREGUNTA SIN DATO" solo para rechazos reales jev_Qn sin pregunta identificable
 if(/^jev_Q[1-9]/.test(String(o.rule||""))){const q=typeof o.question==="string"&&/^Q[1-9]$/.test(o.question)?o.question:null;
  return withTitle(h("span","otag o-out","JEV: RECHAZÓ · "+(q?"PREGUNTA "+q:"PREGUNTA "+ND)+" ("+sStr(o.rule)+")"),o)}
 return withTitle(h("span","otag o-out","JEV: RECHAZÓ · "+sStr(o.rule)),o)}
// Hora de la última decisión por estrategia: máximo t de log2.passed con resultado para esa estrategia, o feed con strategy. Si no hay, operators.
function decLast(d){const P=d.log2&&Array.isArray(d.log2.passed)?d.log2.passed:[],F=Array.isArray(d.feed)?d.feed:[],out={};
 const mx=(a,b)=>typeof b==="string"&&/^\d{4}-\d\d-\d\d[ T]\d\d:\d\d/.test(b)&&(!a||b>a)?b:a;
 let j=null,s=null;P.forEach(r=>{if(r&&r.jev&&r.jev.outcome)j=mx(j,r.t);if(r&&r.simple&&r.simple.outcome)s=mx(s,r.t)});
 F.forEach(f=>{if(f&&f.strategy==="jev")j=mx(j,f.t);if(f&&f.strategy==="simple")s=mx(s,f.t)});
 if(j)out.jev=j.replace("T"," ");if(s)out.simple=s.replace("T"," ");return out}
// Q4b: puntaje SOLO si el registro de esa moneda lo trae directo (jev.q4b_score / score / q4b{score}). Sin emparejamiento por hora.
function q4bScore(o){for(const k of["q4b_score","score","q4b"]){const v=o&&o[k];const n=v&&typeof v==="object"?v.score:v;if(typeof n==="number"&&Number.isFinite(n))return n}return null}
function renderLog2(d){const L=d.log2;
 if(!L||typeof L!=="object"){put("logRej",dim(ND));put("logPass",dim(ND));return}
 const R=Array.isArray(L.rejections)?L.rejections:null,P=Array.isArray(L.passed)?L.passed:null;
 if(R)logCol("logRej",R,r=>r.t+"|"+r.mint+"|"+r.rule,"rej",r=>{const fr=String(r.first_rule||"other");
   const e=h("div","ln l1",null,[dim(String(r.t??ND).slice(11,19))," ",h("b",null,sStr(r.symbol))," ",h("span",null,String(r.rule||ND).split(",").map(rn).join(" · "),null,{color:col(fr)})]);
   e.setAttribute("title",sStr(r.reason!=null&&r.reason!==""?r.reason:r.rule));return e},"SIN RECHAZOS");else put("logRej",dim(ND));
 if(P)logCol("logPass",P,r=>r.t+"|"+r.mint,"pass",r=>h("div","ln l2",null,[h("div",null,null,[dim(String(r.t??ND).slice(11,19))," ",h("b",null,sStr(r.symbol))]),h("div","otags",null,[simpleTag(r.simple),jevTag(r.jev,r.t)])]),"NINGUNA MONEDA PASÓ LOS FILTROS");else put("logPass",dim(ND))}
const SN={jev:"JEV",simple:"REGLA SIMPLE"},SB={jev:"b-jev",simple:"b-simple"},ND="SIN DATO";
const pct=v=>v==null||!isFinite(v)?ND:v.toFixed(2)+"%";
function lastEq(d,k){const cv=(d.curves||{})[d.phase]||{},s=(cv[k]||[]).filter(p=>p.v!=null);return s.length?s[s.length-1].v:null}
function ago(ts){const t=Date.parse(ts);if(!isFinite(t))return ND;const m=Math.max(0,Math.floor((Date.now()-t)/60000));return m<60?m+" MIN":Math.floor(m/60)+" H "+(m%60)+" MIN"}
function cell(l,v,cls){return h("div","pc",null,[h("small","dim",l),h("b",cls||(v===ND?"dim":null),v)])}
const B58=/^[1-9A-HJ-NP-Za-km-z]{32,44}$/,okCA=v=>typeof v==="string"&&B58.test(v),shortCA=v=>v.slice(0,4)+"…"+v.slice(-4);
function lnk(txt,url){const a=h("a","plink",txt);a.href=url;a.target="_blank";a.rel="noopener noreferrer";return a}
function caBox(mint,pool){if(!okCA(mint))return h("b","dim",ND);
 const cp=h("button","pcopy","COPIAR");cp.type="button";cp.addEventListener("click",()=>{const done=()=>{cp.textContent="COPIADO";setTimeout(()=>cp.textContent="COPIAR",1500)};
  if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(mint).then(done,()=>{cp.textContent="NO SE PUDO"});else cp.textContent="NO SE PUDO"});
 return h("span","ca",null,[h("b",null,shortCA(mint)),okCA(pool)?lnk("DEXSCREENER","https://dexscreener.com/solana/"+encodeURIComponent(pool)):null,lnk("SOLSCAN","https://solscan.io/token/"+encodeURIComponent(mint)),cp])}
// Precio de entrada de la pierna exacta: campo propio de la entrada > posición con mismo id > carril+mint+hora exacta (seg, CDMX UTC-6). Nunca promedio ni precio actual.
const pxOK=v=>typeof v==="number"&&Number.isFinite(v)&&v>0?v:null;
function entPx(d,e){let v=pxOK(e.entry_price);const P=Array.isArray(d.positions)?d.positions:[];
 if(v==null&&e.id!=null){const m=P.filter(p=>p&&p.id!=null&&String(p.id)===String(e.id));if(m.length===1)v=pxOK(m[0].entry_price)}
 if(v==null&&e.lane!=null&&okCA(e.mint)){const t=Date.parse(String(e.t??"").replace(" ","T")+"-06:00");
  if(isFinite(t)){const m=P.filter(p=>p&&p.lane===e.lane&&p.mint===e.mint&&Math.floor((tsMs(p.opened_ts)??NaN)/1000)===t/1000);if(m.length===1)v=pxOK(m[0].entry_price)}}
 return v!=null&&v>0?" · ENTRADA "+fpxs(v):""}
function renderEntries(d){const E=d.entries;if(!Array.isArray(E)){put("entries",dim("SIN DATO"));return}
 put("entries",E.length?E.map(e=>h("div","ln ent",null,[h("span","dim",String(e.t??ND).slice(11)+" "),h("span","badge "+(SB[e.strategy]||"b-idle"),SN[e.strategy]||String(e.strategy??ND).toUpperCase())," ",
  h("b",e.side==="buy"?"gr":"rd",e.side==="buy"?"ENTRADA":"SALIDA")," ",h("b",null,String(e.symbol??ND))," ",caBox(e.mint,e.pool)," MONTO ",fusd(e.usd),entPx(d,e),e.side!=="buy"&&e.reason?dim(" · "+e.reason):null])):[dim("SIN ENTRADAS")])}
// Posiciones expandidas: en memoria (por id) y en sessionStorage; sobreviven a cada sondeo/redibujo.
const POS_OPEN=new Set((()=>{try{const v=JSON.parse(sessionStorage.getItem("jevPosOpen")||"[]");return Array.isArray(v)?v.filter(x=>(typeof x==="number"&&Number.isInteger(x))||(typeof x==="string"&&/^g?[0-9]{1,12}$/.test(x))).slice(0,200).map(String):[]}catch(_){return[]}})());
const num=v=>v==null||v===""||!isFinite(+v)?null:+v;
function fpxs(v){v=num(v);if(v==null)return ND;const a=Math.abs(v);if(a===0)return "$0";if(a>=1)return "$"+v.toFixed(a>=1000?2:4);
 const z=Math.floor(-Math.log10(a));return "$"+v.toFixed(Math.min(20,z+4)).replace(/0+$/,"")}
const fpx=v=>{const t=fpxs(v);return h("b",t===ND?"dim":null,t)};
// sin precio actual (null/no finito) o marca sin ruta: el export valúa a -100% (regla conservadora del auditor); solo se etiqueta
const noPx=p=>!!p&&p.status==="open"&&(!pxOK(p.current_price)||/^(no[_ ]?route|sin[_ ]?ruta)$/i.test(String(p.mark_status??"")));
const NOPX=()=>h("span","dim","SIN RUTA");
const fsig=v=>(v<0?"-$":"+$")+Math.abs(v).toFixed(Math.abs(v)<0.01&&v!==0?4:2);
// Agrupación de adiciones (piramidación): clave = group_id > pyramid_of > mint (ver groupLegs);
// si no, estrategia + carril + mint base58 validado (nunca símbolo). Sólo se muestran grupos con >=1 pierna abierta;
// reentry1: las piernas cerradas no se unen a tarjetas abiertas; sólo aparecen en el historial de cerradas.
const tsMs=v=>{const t=Date.parse(v);return isFinite(t)?t:null};
const nnInt=v=>{if(typeof v==="string"&&!/^[0-9]{1,12}$/.test(v))return null;const n=typeof v==="number"||typeof v==="string"?+v:NaN;return Number.isInteger(n)&&n>=0&&n<1e12?n:null};
function groupLegs(d,k){const A=(d.positions||[]).filter(p=>p&&p.strategy===k),hasPyr=A.some(p=>Object.prototype.hasOwnProperty.call(p,"pyramid_of"));
 // prioridad: group_id válido > pyramid_of válido (base = pyramid_of o id propio) > carril + mint base58 (regla de ciclo). Valores no numéricos se ignoran.
 const key=p=>{const g=nnInt(p.group_id);if(g!=null)return "g"+g;
  if(hasPyr){const b=nnInt(p.pyramid_of)??(p.pyramid_of==null?nnInt(p.id):null);if(b!=null)return "g"+b}
  return okCA(p.mint)?"m"+String(p.lane??"")+"|"+p.mint:"x"+String(p.id)};
 const G=new Map();A.filter(p=>p.status==="open").forEach(p=>{const kk=key(p);if(!G.has(kk))G.set(kk,{legs:[]});G.get(kk).legs.push(p)});
 for(const [kk,g] of G){
  g.legs.sort((x,y)=>(tsMs(x.opened_ts)??0)-(tsMs(y.opened_ts)??0)||(+x.id||0)-(+y.id||0))}
 return [...G.values()]}
function groupCard(g,k,eq,n){const O=g.legs.filter(p=>p.status==="open"),C=g.legs.filter(p=>p.status!=="open"),f=g.legs[0];
 const ids=g.legs.map(p=>String(p.id)).filter(x=>/^[0-9]{1,12}$/.test(x)),key=ids.length?"g"+ids[0]:null,did="pg-"+(key??("x"+n)),isOpen=key!==null&&POS_OPEN.has(key);
 let cost=0,wq=0,uS=0,cS=0,uOK=O.length>0,cur=null;
 O.forEach(p=>{const c0=num(p.cost_usd),ep=num(p.entry_price),u=num(p.unrealized_pnl),ql=num(p.qty_left_raw),qr=num(p.qty_raw);
  if(c0!=null){cost+=c0;if(ep!=null&&ep>0)wq+=c0/ep}if(num(p.current_price)!=null)cur=num(p.current_price);
  if(u==null||c0==null||ql==null||qr==null||qr<=0){uOK=false;return}uS+=u;cS+=c0*ql/qr});
 const avg=wq>0?cost/wq:null,uP=uOK&&cS>0?100*uS/cS:null,uC=!uOK?"dim":uS>=0?"gr":"rd",eqp=eq?100*cost/eq:null;
 let rS=0,rOK=C.length>0;C.forEach(p=>{const v=num(p.net_pnl);if(v==null)rOK=false;else rS+=v});const rC=!rOK?"dim":rS>=0?"gr":"rd";
 const btn=h("button","prow",null,[h("span","pchev","▸"),h("span","badge "+SB[k],SN[k]),h("b","psym",String(f.symbol??ND)),h("span","badge b-spot","SPOT · SIN APALANCAMIENTO"),
  h("span","badge b-ntr",`${g.legs.length} ${g.legs.length===1?"trade":"trades"}`+(C.length?` · ${O.length} ABIERTAS`:"")),
  h("span","pfx",null,[h("small","dim","ENTRADA PROM. "),fpx(avg)]),h("span","pfx",null,[h("small","dim","ACTUAL "),O.length&&O.every(noPx)?NOPX():fpx(cur)]),
  h("span","ppnl",null,[h("small","dim","uPNL ABIERTAS "),h("b",uC,uOK?fsig(uS):ND),h("span",uC,uOK?" ("+(uP!=null?(uP>=0?"+":"")+uP.toFixed(2)+"%":ND)+")":""),O.some(noPx)?h("small","dim nopx"," · SIN RUTA · valuación conservadora"):null]),
  C.length?h("span","ppnl",null,[h("small","dim","REALIZADO "),h("b",rC,rOK?fsig(rS):ND)]):null,
  h("span","psz",`${fusd(cost)} · ${pct(eqp)}`)]);
 const leg=p=>{const op=p.status==="open",u=num(p.unrealized_pnl),cb=+p.cost_usd*((+p.qty_left_raw&&+p.qty_raw)?p.qty_left_raw/p.qty_raw:1),up=u!=null&&cb>0?100*u/cb:null,np=num(p.net_pnl);
  const c=v=>v==null?"dim":v>=0?"gr":"rd";
  return h("div","pleg",null,[h("div","phead",null,[h("span","dim",`#${p.id}`),h("span","badge "+(op?"b-run":"b-idle"),op?"ABIERTA":"CERRADA")]),
   h("div","pgrid",null,[cell("ENTRADA (HORA CDMX)",String(p.opened??ND)),cell("TIEMPO ABIERTA",op?ago(p.opened_ts):ND),cell("PRECIO ENTRADA",fpxs(p.entry_price)),op&&noPx(p)?cell("PRECIO ACTUAL","SIN RUTA","dim"):null,
    cell("TAMAÑO",fusd(+p.cost_usd)),cell("STOP",num(p.stop_price)!=null?fpxs(p.stop_price)+(num(p.stop_pct)!=null?` (${num(p.stop_pct)}%)`:""):ND),
    op?cell("PNL NO REAL."+(noPx(p)?" · SIN RUTA (VALUACIÓN CONSERVADORA)":""),u!=null?fsig(u)+(up!=null?` (${(up>=0?"+":"")+up.toFixed(2)}%)`:""):ND,c(u)):cell("CIERRE (HORA CDMX)",String(p.closed??ND)),
    op?cell("ESTADO MARCA",p.mark_status?String(p.mark_status).toUpperCase().replace("_"," "):ND):cell("PNL NETO",np!=null?fsig(np):ND,c(np)),
    op?null:cell("MOTIVO SALIDA",String(p.exit_reason??ND))])])};
 const det=h("div","pdet",null,[h("div","phead",null,[h("span","dim",`CARRIL ${String(f.lane??ND).toUpperCase()} · ADICIONES A LA MISMA MONEDA · CADA TRADE CONSERVA SU STOP Y SALIDA 24H`)]),
  h("div","pca",null,[h("small","dim","CA "),caBox(f.mint,f.pool)]),...g.legs.map(leg)]);
 det.id=did;det.hidden=!isOpen;const card=h("div","pcard pgroup "+SB[k]+(isOpen?" open":""),null,[btn,det]);
 btn.type="button";btn.setAttribute("aria-expanded",String(isOpen));btn.setAttribute("aria-controls",did);
 btn.addEventListener("click",()=>{const o=btn.getAttribute("aria-expanded")!=="true";btn.setAttribute("aria-expanded",String(o));det.hidden=!o;card.classList.toggle("open",o);
  if(key!==null){o?POS_OPEN.add(key):POS_OPEN.delete(key);try{sessionStorage.setItem("jevPosOpen",JSON.stringify([...POS_OPEN]))}catch(_){}}});
 return card}
function renderPos(d){renderEntries(d);const P=(d.positions||[]).filter(p=>p.status==="open"),R=d.risk||{};
 const out=[];
 ["jev","simple"].forEach(k=>{const L=P.filter(p=>p.strategy===k),eq=lastEq(d,k),tot=L.reduce((s,p)=>s+(+p.cost_usd||0),0);
  // uPnL total: Σ unrealized_pnl / Σ costo restante (cost_usd*qty_left_raw/qty_raw); si falta cualquier dato -> SIN DATO
  let uS=0,cS=0,uOK=L.length>0;L.forEach(p=>{const u=num(p.unrealized_pnl),c0=num(p.cost_usd),ql=num(p.qty_left_raw),qr=num(p.qty_raw);
   if(u==null||c0==null||ql==null||qr==null||qr<=0){uOK=false;return}uS+=u;cS+=c0*ql/qr});
  const tP=uOK&&cS>0?100*uS/cS:null,tC=!uOK?"dim":uS>=0?"gr":"rd";
  out.push(h("div","ptot",null,[h("span","badge "+SB[k],SN[k]),h("span",null,`${L.length} ABIERTAS · ${fusd(tot)}`),h("span","dim",`${eq?pct(100*tot/eq):ND} DEL EQUITY · EQUITY ${fusd(eq)}`),
   h("span","pup",null,[h("small","dim","uPNL TOTAL "),h("b",tC,uOK?fsig(uS):ND),h("span",tC,uOK?" ("+(tP!=null?(tP>=0?"+":"")+tP.toFixed(2)+"%":ND)+")":""),L.some(noPx)?h("small","dim nopx",` · incluye ${L.filter(noPx).length} sin ruta a -100%`):null])]));
  const one=p=>{const eqp=eq?100*(+p.cost_usd)/eq:null;
   const key=/^[0-9]{1,12}$/.test(String(p.id))?String(p.id):null,did="pd-"+(key??("x"+out.length)),isOpen=key!==null&&POS_OPEN.has(key);
   const upU=num(p.unrealized_pnl),cb=+p.cost_usd*((+p.qty_left_raw&&+p.qty_raw)?p.qty_left_raw/p.qty_raw:1),upP=upU!=null&&cb>0?100*upU/cb:null,ms=p.mark_status;
   const pc=v=>v==null?"dim":v>=0?"gr":"rd",uC=upU!=null?pc(upU):"dim";
   const sb=ms==="stale"?h("span","badge b-stale","STALE"):ms==="no_route"?h("span","badge b-err","SIN RUTA"):null;
   const btn=h("button","prow",null,[h("span","pchev","▸"),h("span","badge "+SB[k],SN[k]),h("b","psym",String(p.symbol??ND)),h("span","badge b-spot","SPOT · SIN APALANCAMIENTO"),sb,
    h("span","pfx",null,[h("small","dim","ENTRADA "),fpx(p.entry_price)]),h("span","pfx",null,[h("small","dim","ACTUAL "),noPx(p)?NOPX():fpx(p.current_price)]),
    h("span","ppnl",null,[h("small","dim","uPNL "),h("b",uC,upU!=null?fsig(upU):ND),h("span",uC,upU!=null?" ("+(upP!=null?(upP>=0?"+":"")+upP.toFixed(2)+"%":ND)+")":""),noPx(p)?h("small","dim nopx"," · SIN RUTA · valuación conservadora"):null]),
    h("span","psz",`${fusd(+p.cost_usd)} · ${pct(eqp)}`)]);
   btn.type="button";btn.setAttribute("aria-expanded",String(isOpen));btn.setAttribute("aria-controls",did);
   btn.addEventListener("click",()=>{const o=btn.getAttribute("aria-expanded")!=="true";btn.setAttribute("aria-expanded",String(o));det.hidden=!o;card.classList.toggle("open",o);
    if(key!==null){o?POS_OPEN.add(key):POS_OPEN.delete(key);try{sessionStorage.setItem("jevPosOpen",JSON.stringify([...POS_OPEN]))}catch(_){}}});
   const det=h("div","pdet",null,[
    h("div","phead",null,[h("span","dim",`#${p.id} · CARRIL ${String(p.lane??ND).toUpperCase()}`)]),
    h("div","pca",null,[h("small","dim","CA "),caBox(p.mint,p.pool)]),
    h("div","pgrid",null,[
     cell("LADO",p.side?String(p.side).toUpperCase():"LONG (COMPRA)"),
     cell("TAMAÑO",fusd(+p.cost_usd)),cell("% EQUITY",pct(eqp)),
     cell("ENTRADA (HORA CDMX)",String(p.opened??ND)),cell("TIEMPO ABIERTA",ago(p.opened_ts)),
     cell("PRECIO ENTRADA",fpxs(p.entry_price)),
     cell("PRECIO ACTUAL",noPx(p)?"SIN RUTA":fpxs(p.current_price),noPx(p)?"dim":null),noPx(p)?cell("VALUACIÓN","SIN RUTA · CONSERVADORA (-100%)","dim"):null,
     cell("PNL NO REAL. $",upU!=null?fsig(upU):ND,upU!=null?uC:null),
     cell("PNL NO REAL. %",upP!=null?pct(upP):ND,upP!=null?uC:null),
     cell("ESTADO MARCA",ms?String(ms).toUpperCase().replace("_"," ")+(num(p.minutes_since_valid_mark)!=null?` · ${num(p.minutes_since_valid_mark)} MIN`:""):ND),
     cell("TIPO","SPOT · SIN APALANCAMIENTO"),
     cell("STOP",R.stop_loss_pct!=null?`${R.stop_loss_pct}% (REGLA GLOBAL)`:ND),
     cell("CANTIDAD (RAW)",p.qty_left_raw!=null?String(p.qty_left_raw):ND),
     cell("TOPE TAMAÑO",R.max_ticket_pct_bank!=null?`${R.max_ticket_pct_bank}% BANCA (GLOBAL)`:ND),
     cell("PROB. JEV",p.jev_prob!=null?pct(100*p.jev_prob):(k==="simple"?"N/A (SIN JEV)":ND)),
     cell("COMISIÓN / IMPACTO",`${fusd(+p.entry_fee_usd)} · ${pct(+p.entry_impact_pct)}`)])]);
   det.id=did;det.hidden=!isOpen;const card=h("div","pcard "+SB[k]+(isOpen?" open":""),null,[btn,det]);return card};
  groupLegs(d,k).forEach(g=>out.push(g.legs.length===1?one(g.legs[0]):groupCard(g,k,eq,out.length)))});
 if(!P.length)out.push(dim("SIN POSICIONES ABIERTAS"));
 out.push(h("div","dim small sub","SIN DATO = el campo no viene en los datos. STALE = última marca válida reciente pero fallando; SIN RUTA = sin precio de salida (valor 0). Todas las posiciones son spot, sin apalancamiento."));
 put("pos",out)}
function drawChart(d,c){if(!window.Chart){$("eqnote").textContent="CHART.JS NO CARGÓ";return}
 const K=["jev","simple","hold_btc","hold_sol"],mn=t=>{const m=/(\d\d):(\d\d)$/.exec(t);return m?+m[1]*60+ +m[2]:NaN};
 const ts=[...new Set(K.flatMap(k=>(c[k]||[]).map(p=>p.t)))].sort();
 const real={};K.forEach(k=>{const mp=new Map();(c[k]||[]).forEach(p=>{if(p.v!=null)mp.set(p.t,p.v)});real[k]=[...mp].sort((a,b)=>a[0]<b[0]?-1:1)});
 const APX={};
 const ser=k=>{const r=real[k],ap=APX[k]=[];let j=0;return ts.map((t,i)=>{while(j<r.length&&r[j][0]<t)j++;
   if(j<r.length&&r[j][0]===t){ap[i]=null;return r[j][1]}
   const L=r[j-1],R=r[j];if(!L||!R){ap[i]=null;return null}
   const x=(mn(t)-mn(L[0]))/((mn(R[0])-mn(L[0]))||1),v=L[1]+(R[1]-L[1])*x;ap[i]=[L[0].slice(11),R[0].slice(11)];return Math.round(v*100)/100})};
 const rbm={};(d.rejections_by_minute||[]).forEach(([t,n])=>rbm[t]=n);
 const SP=[["EQUITY JEV","jev","#FF00E6"],["REGLA SIMPLE","simple","#FFEA00"],["HOLD BTC","hold_btc","#00C8FF"],["HOLD SOL","hold_sol","#00FF88"]];
 const ds=SP.map(([l,k,cc])=>{const data=ser(k),ap=APX[k];return{label:l,_k:k,data,yAxisID:"y",borderColor:cc,backgroundColor:cc,borderWidth:k==="jev"?2.5:k==="simple"?2.2:1.8,tension:.2,order:k==="simple"?0:1,
  borderDash:k==="simple"?[6,4]:undefined,pointRadius:data.map((v,i)=>v!=null&&!ap[i]&&k.startsWith("hold")?2:0),pointHoverRadius:data.map((v,i)=>v==null?0:ap[i]?3:5),
  pointStyle:data.map((v,i)=>ap[i]?"crossRot":"circle")}});
 ds.push({label:"MINUTOS CON RECHAZOS",_k:"rej",type:"bar",yAxisID:"yR",data:ts.map(t=>rbm[t]??null),backgroundColor:"rgba(255,48,64,.75)",borderColor:"#FF3040",borderWidth:0,barThickness:3,order:2});
 const fnt={family:"JetBrains Mono",size:10};
 const cfg={type:"line",data:{labels:ts.map(t=>t.slice(11)),datasets:ds},options:{animation:false,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
  plugins:{legend:{labels:{color:"#E0E0E0",font:fnt,boxWidth:10}},
   tooltip:{backgroundColor:"rgba(10,7,10,.94)",borderColor:"#FF00E6",borderWidth:1,titleFont:fnt,bodyFont:fnt,footerFont:fnt,caretSize:0,padding:6,boxWidth:8,boxHeight:8,
    position:"nearest",yAlign:"top",
    filter:it=>it.dataset._k!=="rej",itemSort:(a,b)=>a.datasetIndex-b.datasetIndex,
    callbacks:{title:it=>it.length?`${ts[it[0].dataIndex]} CDMX`:"",
     label:it=>{const k=it.dataset._k,ap=APX[k]&&APX[k][it.dataIndex];return `${it.dataset.label}: ${ap?"≈":""}$${Number(it.raw).toFixed(2)}${ap?` (INTERP. ${ap[0]}–${ap[1]})`:""}`},
     footer:it=>{if(!it.length)return"";const t=ts[it[0].dataIndex],shown=new Set(it.map(x=>x.dataset._k)),miss=SP.filter(s=>!shown.has(s[1])).map(s=>s[0]+": SIN DATO");
      const n=rbm[t];return [...miss,`RECHAZOS ESE MINUTO: ${n??0}`,it.some(x=>APX[x.dataset._k]&&APX[x.dataset._k][x.dataIndex])?"≈ = APROXIMADO (SIN PUNTO REAL)":""].filter(Boolean)}}}},
  scales:{x:{ticks:{color:"#665A60",font:fnt,maxTicksLimit:10},grid:{color:"#1d161d"}},
   yR:{stack:"eq",stackWeight:1,offset:true,beginAtZero:true,position:"left",ticks:{color:"#FF3040",font:fnt,maxTicksLimit:2,precision:0},grid:{color:"#2a1218"},title:{display:false}},
   y:{stack:"eq",stackWeight:5,offset:true,ticks:{color:"#665A60",font:fnt,callback:v=>"$"+v},grid:{color:"#1d161d"}}}}};
 if(chart){chart.data=cfg.data;chart.options=cfg.options;chart.update()}else chart=new Chart($("eq"),cfg);
 const flat=ds.slice(0,4).every(s=>{const v=s.data.filter(x=>x!=null);return v.every(x=>x===v[0])});
 $("eqnote").textContent=`HORAS CDMX · ${ts.length} MINUTOS · PUNTOS REALES: JEV ${real.jev.length}, REGLA ${real.simple.length}, BTC ${real.hold_btc.length}, SOL ${real.hold_sol.length}${flat?" · CURVAS PLANAS: SIN VARIACIÓN REGISTRADA":""} · ≈ EN EL TOOLTIP = INTERPOLADO ENTRE PUNTOS REALES · BARRAS ROJAS ABAJO = RECHAZOS POR MINUTO`}
// Galaxia con agujero negro (propuesta blackhole). ROJO = moneda analizada y rechazada (d.log2.rejections / d.feed): pequeña, rápida, cae en espiral y es absorbida.
// VERDE = posición abierta (d.positions, por id): grande, lenta. Al cerrar: net_pnl>0 sale disparada; net_pnl<0 cae al agujero; sin net_pnl numérico (o =0) se desvanece (resultado desconocido, no se inventa).
const GX=(function(){const cv=$("vx"),x=cv.getContext("2d"),RM=matchMedia("(prefers-reduced-motion: reduce)");
 const TAU=Math.PI*2,MAXR=24,MAXG=32,MAXQ=40,NB=150,NG=(navigator.maxTouchPoints>1?620:820),RH=.075;// RH = horizonte (radio normalizado)
 let W=1,H=1,D=1,cx=0,cy=0,Rx=1,Ry=1,t=0,last=0,raf=0,nextShoot=3,spawnGap=0,hasData=false,flash=0;
 const BX=new Float32Array(NB),BY=new Float32Array(NB),BZ=new Float32Array(NB),BP=new Float32Array(NB);
 for(let i=0;i<NB;i++){BX[i]=Math.random();BY[i]=Math.random();BZ[i]=.2+Math.random()*.8;BP[i]=Math.random()*TAU}
 const GR=new Float32Array(NG),GT=new Float32Array(NG),GS=new Float32Array(NG),GC=new Uint8Array(NG);
 const gauss=()=>(Math.random()+Math.random()+Math.random()-1.5)/1.5;
 for(let i=0;i<NG;i++){const r=.16+.84*Math.pow(Math.random(),.75),disk=Math.random()<.22,arm=(i&1)*Math.PI;
  GR[i]=r;GT[i]=disk?Math.random()*TAU:arm+r*4.6+gauss()*(.28+.25*r);GS[i]=Math.random()<.08?2:1;
  const u=Math.random();GC[i]=r<.3?(u<.6?0:1):(u<.45?2:u<.8?1:0)}
 const GCOL=["rgba(255,236,250,","rgba(190,215,255,","rgba(224,48,224,"];
 const wStar=r=>.045*(1+1.4*(1-r));// estrellas decorativas: rotación suave
 // Kepler: ω ∝ r^-1.5, con r acotado abajo y ω acotada arriba
 const wK=r=>Math.min(2.6,.07*Math.pow(Math.max(r,.1),-1.5));
 function sprite(sz,stops){const c=document.createElement("canvas");c.width=c.height=sz;const g=c.getContext("2d"),gr=g.createRadialGradient(sz/2,sz/2,0,sz/2,sz/2,sz/2);
  stops.forEach(s=>gr.addColorStop(s[0],s[1]));g.fillStyle=gr;g.fillRect(0,0,sz,sz);return c}
 const SR=sprite(64,[[0,"rgba(255,235,235,1)"],[.15,"rgba(255,60,70,.95)"],[.45,"rgba(255,30,50,.28)"],[1,"rgba(255,0,40,0)"]]);
 const SG=sprite(64,[[0,"rgba(235,255,240,1)"],[.15,"rgba(0,255,90,.95)"],[.45,"rgba(0,255,65,.28)"],[1,"rgba(0,255,65,0)"]]);
 let NEB=null,DISK=null,HALO=null;
 function mkNeb(){const w=Math.max(2,Math.round(Rx*2.4)),h=Math.max(2,Math.round(Ry*2.4)),c=document.createElement("canvas");c.width=w;c.height=h;const g=c.getContext("2d");
  g.globalCompositeOperation="lighter";const blobs=[[.5,.5,.5,"rgba(157,77,255,.16)"],[.32,.4,.3,"rgba(224,48,224,.13)"],[.68,.6,.3,"rgba(224,48,224,.13)"],[.25,.65,.22,"rgba(60,90,255,.08)"],[.75,.35,.22,"rgba(60,90,255,.08)"]];
  for(const b of blobs){const gr=g.createRadialGradient(b[0]*w,b[1]*h,0,b[0]*w,b[1]*h,b[2]*w);gr.addColorStop(0,b[3]);gr.addColorStop(1,"rgba(0,0,0,0)");g.fillStyle=gr;g.fillRect(0,0,w,h)}NEB=c}
 // disco de acreción prerenderizado (cara, circular); se dibuja aplastado por Ry/Rx para la inclinación. Hot spots giran con ctx.rotate (barato).
 function mkBH(){const rs=RH*Rx,sz=Math.max(8,Math.ceil(rs*7.2)),c=document.createElement("canvas");c.width=c.height=sz;const g=c.getContext("2d"),m=sz/2;
  const gr=g.createRadialGradient(m,m,rs*1.05,m,m,m);gr.addColorStop(0,"rgba(255,235,255,.95)");gr.addColorStop(.08,"rgba(236,150,255,.85)");gr.addColorStop(.3,"rgba(170,70,255,.45)");gr.addColorStop(.6,"rgba(110,40,220,.16)");gr.addColorStop(1,"rgba(60,20,160,0)");
  g.fillStyle=gr;g.beginPath();g.arc(m,m,m,0,TAU);g.arc(m,m,rs*1.05,0,TAU,true);g.fill();
  g.globalCompositeOperation="lighter";for(let i=0;i<46;i++){const a=Math.random()*TAU,rr=rs*(1.3+Math.random()*1.8);g.strokeStyle=`rgba(${200+Math.random()*55|0},${120+Math.random()*80|0},255,${.08+Math.random()*.18})`;g.lineWidth=(.6+Math.random()*1.6)*D;g.beginPath();g.arc(m,m,rr,a,a+.4+Math.random()*1.3);g.stroke()}
  DISK=c;const hs=Math.ceil(rs*4.4),h=document.createElement("canvas");h.width=h.height=hs;const k=h.getContext("2d"),hm=hs/2,hg=k.createRadialGradient(hm,hm,rs*.9,hm,hm,hm);
  hg.addColorStop(0,"rgba(255,230,255,.9)");hg.addColorStop(.08,"rgba(220,130,255,.55)");hg.addColorStop(.35,"rgba(140,60,255,.18)");hg.addColorStop(1,"rgba(80,30,200,0)");k.fillStyle=hg;k.fillRect(0,0,hs,hs);HALO=h}
 function rs(){D=Math.min(2,devicePixelRatio||1);W=Math.max(1,cv.clientWidth*D|0);H=Math.max(1,cv.clientHeight*D|0);cv.width=W;cv.height=H;cx=W/2;cy=H*.54;
  Rx=Math.min(W*.46,H*.5/.42);Ry=Rx*.42;mkNeb();mkBH();if(typeof vxl==="function"){vxl();bands()}if(!raf)frame(performance.now())}
 // st: 1 orbita/espiral (rojo) · 0 entrada, 1 órbita (verde) · 2 eyección · 3 absorción · 4 caída en espiral (verde con pérdida) · 5 desvanecido neutro
 const TL=14;const mkB=()=>({rt:.6,key:"",full:"",pnl:null,pr:0,on:false,st:0,r:0,th:0,w:0,dr:0,x:0,y:0,vx:0,vy:0,a:1,age:0,lab:"",tx:new Float32Array(TL),ty:new Float32Array(TL),ti:0,tn:0,r0:0,id:null,sc:1});
 const RED=[];for(let i=0;i<MAXR;i++)RED.push(mkB());const Q=[],SEEN=new Set(),SEENQ=[],GREEN=new Map(),SH={on:false,x:0,y:0,vx:0,vy:0,l:0};
 const clean=s=>String(s==null?"":s).replace(/[\u0000-\u001f]/g,"").slice(0,12);
 const full=s=>String(s==null?"":s).replace(/[\u0000-\u001f]/g,"").slice(0,40);
 function push(b){b.tx[b.ti]=b.x;b.ty[b.ti]=b.y;b.ti=(b.ti+1)%TL;if(b.tn<TL)b.tn++}
 function polar(b){b.x=cx+Math.cos(b.th)*b.r*Rx;b.y=cy+Math.sin(b.th)*b.r*Ry}
 function eject(b,s0){const ox=Math.cos(b.th),oy=Math.sin(b.th),tx=-Math.sin(b.th)*Rx,ty=Math.cos(b.th)*Ry;const tl=Math.hypot(tx,ty)||1,s=(s0+Math.random()*120)*D;
  b.vx=(tx/tl)*s*.5+ox*s;b.vy=(ty/tl)*s*.5+oy*s*.6;b.st=2}
 function spawnRed(sym){const b=RED.find(o=>!o.on);if(!b)return false;b.on=true;b.st=1;b.r=1.05;b.th=Math.random()*TAU;b.dr=.9+Math.random()*.3;
  b.age=0;b.a=1;b.sc=1;b.lab=clean(sym);b.tn=0;b.ti=0;polar(b);return true}
 // caída: dr/dt crece al acercarse (≈ r^-0.5), con tope
 const fall=(b,dt,k)=>{b.r-=Math.min(.5,k*(.06+.02/Math.sqrt(Math.max(b.r,.05))))*dt};
 function absorb(b){b.st=3;b.age2=0;flash=Math.min(1,flash+.5)}
 function step(dt,slow){t+=dt;flash=Math.max(0,flash-dt*1.5);
  for(const b of RED){if(!b.on)continue;b.age+=dt;
   if(b.st===1){b.th+=wK(b.r)*1.9*dt;fall(b,dt,b.dr);if(b.r<=RH*1.15)absorb(b);polar(b)}
   else if(b.st===3){b.age2+=dt;b.th+=wK(b.r)*1.9*dt;b.r=Math.max(0,b.r-.08*dt);b.sc=Math.max(0,1-b.age2/.35);b.a=b.sc;polar(b);if(b.sc<=0){b.on=false;continue}}
   push(b)}
  for(const [k,g] of GREEN){g.age+=dt;
   if(g.st===0){const e=Math.min(1,g.age/2.2),k2=1-Math.pow(1-e,3);g.th+=wK(g.r)*.55*dt;g.r=1.5+(g.r0-1.5)*k2;polar(g);g.r0+=(g.rt-g.r0)*Math.min(1,dt*1.2);if(e>=1)g.st=1}
   else if(g.st===1){g.r0+=(g.rt-g.r0)*Math.min(1,dt*1.2);g.th+=wK(g.r0)*.55*dt;g.r=g.r0;polar(g)}
   else if(g.st===2){g.vx*=1+.8*dt;g.vy*=1+.8*dt;g.x+=g.vx*dt;g.y+=g.vy*dt;g.a-=.3*dt;if(g.a<=0||g.x<-80*D||g.x>W+80*D||g.y<-80*D||g.y>H+80*D){GREEN.delete(k);continue}}
   else if(g.st===4){g.th+=wK(g.r)*.9*dt;fall(g,dt,1.6);if(g.r<=RH*1.15){g.st=3;g.age2=0;flash=1}polar(g)}
   else if(g.st===3){g.age2+=dt;g.th+=wK(g.r)*.9*dt;g.r=Math.max(0,g.r-.08*dt);g.sc=Math.max(0,1-g.age2/.45);g.a=g.sc;polar(g);if(g.sc<=0){GREEN.delete(k);continue}}
   else{g.th+=wK(g.r0)*.55*dt;polar(g);g.a-=.45*dt;if(g.a<=0){GREEN.delete(k);continue}}
   push(g)}
  spawnGap-=dt;if(Q.length&&spawnGap<=0){if(spawnRed(Q[0]))Q.shift();spawnGap=1.1}
  if(!slow){nextShoot-=dt;if(!SH.on&&nextShoot<=0){SH.on=true;SH.x=Math.random()*W;SH.y=Math.random()*H*.4;const a=.25+Math.random()*.5,s=(380+Math.random()*260)*D,dir=Math.random()<.5?1:-1;SH.vx=Math.cos(a)*s*dir;SH.vy=Math.sin(a)*s;SH.l=1;nextShoot=4+Math.random()*7}
   if(SH.on){SH.x+=SH.vx*dt;SH.y+=SH.vy*dt;SH.l-=.9*dt;if(SH.l<=0)SH.on=false}}}
 function trail(b,rgb,a,wd){if(b.tn<2)return;x.strokeStyle=rgb;x.lineWidth=wd*D;let j=(b.ti-b.tn+TL)%TL;let px=b.tx[j],py=b.ty[j];
  for(let i=1;i<b.tn;i++){j=(j+1)%TL;x.globalAlpha=a*i/b.tn*.8;x.beginPath();x.moveTo(px,py);px=b.tx[j];py=b.ty[j];x.lineTo(px,py);x.stroke()}}
 const behind=b=>b.y<cy;// mitad trasera del disco (arriba en pantalla) queda detrás del agujero
 function bodies(back){for(const b of RED){if(!b.on||behind(b)!==back)continue;trail(b,"#ff3040",b.a,1);x.globalAlpha=b.a;const s=6*D*b.sc;x.drawImage(SR,b.x-s,b.y-s,s*2,s*2)}
  for(const g of GREEN.values()){if(behind(g)!==back)continue;trail(g,"#00ff41",g.a*.6,1.6);x.globalAlpha=g.a;const s=16*D*g.sc*(1+.1*Math.sin(t*1.5+g.r0*9));x.drawImage(SG,g.x-s,g.y-s,s*2,s*2)}}
 function disk(half){const rs=RH*Rx,sz=DISK.width;x.save();x.translate(cx,cy);x.beginPath();if(half<0)x.rect(-sz,-sz,sz*2,sz);else x.rect(-sz,0,sz*2,sz);x.clip();
  x.scale(1,Ry/Rx);x.rotate(t*.35);x.globalAlpha=half<0?.75:.95;x.drawImage(DISK,-sz/2,-sz/2);x.restore()}
 function hole(){const rs=RH*Rx;x.globalCompositeOperation="lighter";
  x.globalAlpha=.85+.15*flash;const hs=HALO.width;x.drawImage(HALO,cx-hs/2,cy-hs/2);// anillo de fotones / lente
  x.globalCompositeOperation="source-over";x.globalAlpha=1;x.fillStyle="#000";x.beginPath();x.arc(cx,cy,rs,0,TAU);x.fill();// sombra
  x.globalCompositeOperation="lighter";x.strokeStyle="rgba(245,200,255,"+(.75+.25*flash)+")";x.lineWidth=1.4*D;x.beginPath();x.arc(cx,cy,rs*1.06,0,TAU);x.stroke();
  // disco trasero lensado: arco sobre la sombra
  x.strokeStyle="rgba(210,120,255,.55)";x.lineWidth=3*D;x.beginPath();x.ellipse(cx,cy-rs*.15,rs*1.55,rs*1.25,0,Math.PI*1.08,Math.PI*1.92);x.stroke();
  x.strokeStyle="rgba(255,220,255,.35)";x.lineWidth=1*D;x.beginPath();x.ellipse(cx,cy-rs*.1,rs*1.3,rs*1.12,0,Math.PI*1.05,Math.PI*1.95);x.stroke()}
 function draw(){x.globalCompositeOperation="source-over";x.globalAlpha=1;x.fillStyle="#05030a";x.fillRect(0,0,W,H);
  x.globalCompositeOperation="lighter";
  x.fillStyle="#dfe6ff";for(let i=0;i<NB;i++){const z=BZ[i];let px=(BX[i]+t*.004*z)%1;x.globalAlpha=(.25+.55*z)*(.6+.4*Math.sin(t*(.8+z*1.6)+BP[i]));const s=(z>.85?1.6:1)*D;x.fillRect(px*W,BY[i]*H,s,s)}
  x.globalAlpha=.9;x.drawImage(NEB,cx-NEB.width/2,cy-NEB.height/2);
  for(let c=0;c<3;c++){x.fillStyle=GCOL[c]+"1)";for(let i=0;i<NG;i++){if(GC[i]!==c)continue;const r=GR[i],th=GT[i]+wStar(r)*t;
    x.globalAlpha=.35+.6*(1-r);const s=GS[i]*D;x.fillRect(cx+Math.cos(th)*r*Rx,cy+Math.sin(th)*r*Ry,s,s)}}
  if(SH.on){x.globalAlpha=Math.max(0,SH.l);x.strokeStyle="#ffffff";x.lineWidth=1.2*D;x.beginPath();x.moveTo(SH.x,SH.y);x.lineTo(SH.x-SH.vx*.12,SH.y-SH.vy*.12);x.stroke()}
  disk(-1);bodies(true);hole();x.globalCompositeOperation="lighter";disk(1);bodies(false);
  x.globalCompositeOperation="source-over";x.font=(9*D)+"px JetBrains Mono, monospace";
  labels();
  if(!hasData){x.globalAlpha=.8;x.fillStyle="#665A60";x.textAlign="center";x.fillText("SIN DATOS DE RECHAZOS NI POSICIONES ABIERTAS",cx,H-10*D);x.textAlign="start"}
  x.globalAlpha=1}

 // Clusters: agrupa cuerpos verdes casi superpuestos y dibuja sólo un anillo + número pequeño (sin nombre). Los nombres van en el tooltip.
 const MAXL=60,TW=new Map(),BOX=[],GL=[];let CL=[];
 const tw=s=>{let w=TW.get(s);if(w===undefined){if(TW.size>400)TW.clear();w=x.measureText(s).width;TW.set(s,w)}return w};
 const hit=(a,b,c,d)=>{for(let i=0;i<BOX.length;i+=4){if(a<BOX[i+2]&&a+c>BOX[i]&&b<BOX[i+3]&&b+d>BOX[i+1])return true}return false};
 function place(px,py,r,s,h){const w=tw(s),m=2*D,O=[[r,-r-h],[r,r],[-r-w,-r-h],[-r-w,r],[-w/2,-r-h-m],[-w/2,r+m],[r+m,-h/2],[-r-w-m,-h/2]];
  for(const o of O){let lx=px+o[0],ly=py+o[1];lx=Math.max(m,Math.min(W-w-m,lx));ly=Math.max(m,Math.min(H-h-m,ly));
   if(!hit(lx-1,ly-1,w+2,h+2)){BOX.push(lx-1,ly-1,lx+w+1,ly+h+1);return[lx,ly]}}return null}
 let VXL=null;const vxl=()=>{const e=$("vxlabel");VXL=e&&e.offsetWidth?[e.offsetLeft*D,e.offsetTop*D,(e.offsetLeft+e.offsetWidth)*D,(e.offsetTop+e.offsetHeight)*D]:null};
 function labels(){BOX.length=0;GL.length=0;if(VXL)BOX.push(VXL[0]-2,VXL[1]-2,VXL[2]+2,VXL[3]+2);const fh=9*D,h=fh*1.15;x.textBaseline="top";
  for(const g of GREEN.values())if(g.lab&&g.a>.05)GL.push(g);GL.sort((a,b)=>b.pr-a.pr);
  const cl=[],rr=(20*D)*(20*D);
  for(const g of GL){let c=null;for(const k of cl){const dx=k.x-g.x,dy=k.y-g.y;if(dx*dx+dy*dy<rr){c=k;break}}
   if(c)c.m.push(g);else cl.push({x:g.x,y:g.y,a:g.a,m:[g]})}
  CL=cl;let n=0;
  for(const c of cl){if(c.m.length<2)continue;x.globalAlpha=c.a*.9;x.strokeStyle="#7dffa0";x.lineWidth=1*D;x.beginPath();x.arc(c.x,c.y,13*D,0,TAU);x.stroke();
   const bx=c.x+10*D,by=c.y-10*D;x.fillStyle="#05030a";x.beginPath();x.arc(bx,by,6*D,0,TAU);x.fill();x.stroke();x.fillStyle="#7dffa0";x.textAlign="center";x.fillText(String(c.m.length),bx,by-fh/2+D*.5);x.textAlign="start";
   BOX.push(bx-6*D,by-6*D,bx+6*D,by+6*D)}
  // sin etiquetas de nombre siempre visibles: el nombre sólo aparece en el tooltip .vxtip (hover/toque). Se conserva el número del cluster (sin nombre).
  for(const b of RED){if(n>=MAXL)break;if(!b.on||!b.lab||b.age>3.5)continue;const pl=place(b.x,b.y,5*D,b.lab,h);if(!pl)continue;n++;
   x.globalAlpha=Math.max(0,1-b.age/3.5)*b.a;x.fillStyle="#ff8a94";x.fillText(b.lab,pl[0],pl[1])}
  x.textBaseline="alphabetic"}
 // tooltip (solo textContent) al pasar el puntero / tocar un cuerpo verde o cluster
 const TIP=document.createElement("div");TIP.className="vxtip";TIP.hidden=true;TIP.setAttribute("role","tooltip");cv.parentNode.appendChild(TIP);
 function pick(e){const r=cv.getBoundingClientRect(),px=(e.clientX-r.left)*D,py=(e.clientY-r.top)*D;let best=null,bd=(16*D)*(16*D);
  for(const c of CL){const dx=c.x-px,dy=c.y-py,dd=dx*dx+dy*dy;if(dd<bd){bd=dd;best=c}}
  if(!best){TIP.hidden=true;return}TIP.textContent="";
  for(const g of best.m.slice(0,12)){const ln=document.createElement("div");ln.textContent=g.full+(typeof g.pnl==="number"&&Number.isFinite(g.pnl)?` · uPnL ${g.pnl<0?"-":"+"}$${Math.abs(g.pnl).toFixed(2)}`:"");TIP.appendChild(ln)}
  if(best.m.length>12){const ln=document.createElement("div");ln.textContent=`… +${best.m.length-12}`;TIP.appendChild(ln)}
  TIP.hidden=false;const lx=e.clientX-r.left,ly=e.clientY-r.top,tw2=TIP.offsetWidth,th=TIP.offsetHeight;
  TIP.style.left=Math.max(2,Math.min(r.width-tw2-2,lx+12))+"px";TIP.style.top=Math.max(2,Math.min(r.height-th-2,ly-th-8))+"px"}
 cv.addEventListener("pointermove",pick);cv.addEventListener("pointerdown",pick);cv.addEventListener("pointerleave",()=>{TIP.hidden=true});
 // Bandas orbitales: cada cuerpo verde vivo recibe un radio distinto y determinista (orden por hash estable de group_id/mint|id),
 // repartido uniformemente en [R0,R1]; separación mínima ≈ alto de etiqueta (12px). Si no caben todas, se reutilizan bandas
 // con desfase angular (y el cluster sigue como respaldo). Velocidad: Kepler por radio (wK), sin cambios para rojos.
 const R0=.3,R1=.97,hsh=s=>{let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619)>>>0}return h};
 function bands(){const L=[];for(const g of GREEN.values())if(g.st<=1)L.push(g);const n=L.length;if(!n)return;
  for(const g of L)g.hk=hsh(g.key||String(g.id));L.sort((a,b)=>a.hk-b.hk||(a.key<b.key?-1:1));
  const cap=Math.max(1,Math.floor((R1-R0)*Ry/(12*D))+1),nb=Math.min(n,cap);
  for(let i=0;i<n;i++){const b=i%nb;L[i].rt=nb>1?R0+(R1-R0)*b/(nb-1):(R0+R1)/2;
   if(i>=nb&&!L[i].shifted){L[i].th+=Math.PI*(i/nb|0)/ (n/nb|0||1);L[i].shifted=true}}}
 const PERF={n:0,sum:0,max:0};
 function frame(now){raf=0;if(document.hidden)return;const slow=RM.matches;let dt=Math.min(.05,Math.max(0,(now-(last||now))/1000));last=now;
  const t0=performance.now();step(slow?dt*.15:dt,slow);draw();const ms=performance.now()-t0;PERF.n++;PERF.sum+=ms;if(ms>PERF.max)PERF.max=ms;
  raf=requestAnimationFrame(frame)}
 document.addEventListener("visibilitychange",()=>{if(!document.hidden&&!raf){last=0;raf=requestAnimationFrame(frame)}});
 addEventListener("resize",rs);rs();
 function note(k){if(SEEN.has(k))return false;SEEN.add(k);SEENQ.push(k);if(SEENQ.length>400)SEEN.delete(SEENQ.shift());return true}
 const pid=p=>String(p.id??p.mint??p.symbol);
 return{perf:()=>({frames:PERF.n,avg_ms:PERF.n?PERF.sum/PERF.n:0,max_ms:PERF.max,red:RED.filter(b=>b.on).length,green:GREEN.size,queue:Q.length}),
  update(d){const L=d.log2&&Array.isArray(d.log2.rejections)?d.log2.rejections:null,first=SEEN.size===0;
   const rows=L?L.map(r=>[r.t+"|"+(r.mint||r.symbol)+"|"+r.rule,r.symbol]):(d.feed||[]).filter(f=>f.result==="reject").map(f=>[f.t+"|"+f.token+"|"+f.rule,f.token]);
   const fresh=rows.filter(r=>note(r[0]));(first?fresh.slice(0,8):fresh).reverse().forEach(r=>Q.push(r[1]));if(Q.length>MAXQ)Q.splice(0,Q.length-MAXQ);
   const all=(d.positions||[]).filter(p=>p),open=all.filter(p=>p.status==="open").slice(0,MAXG),ids=new Set(),byId=new Map();for(const p of all)byId.set(pid(p),p);
   for(const p of open){const id=pid(p);ids.add(id);if(!GREEN.has(id)&&GREEN.size<MAXG){const b=mkB();b.on=true;b.id=id;b.lab=clean(p.symbol||p.token);b.full=full(p.symbol||p.token);
     let hs=0;for(const ch of id)hs=(hs*31+ch.charCodeAt(0))>>>0;const gi=GREEN.size+hs%7;b.r0=.45+((gi*.618034)%1)*.42;b.rt=b.r0;b.th=(gi*2.39996)%TAU;b.r=1.5;polar(b);GREEN.set(id,b)}}
   for(const p of open){const id=pid(p),g=GREEN.get(id);if(!g)continue;const u=p.unrealized_pnl;g.pnl=typeof u==="number"&&Number.isFinite(u)?u:null;g.pr=1e3+(g.pnl!==null?Math.abs(g.pnl):0);g.key=String(p.group_id??p.mint??id)+"|"+id}
   bands();
   // cierre: resultado SOLO desde net_pnl real de la posición ya no abierta; si no hay dato numérico → desvanecido neutro
   for(const [id,g] of GREEN){if(ids.has(id)||g.st>=2)continue;const p=byId.get(id),n=p&&p.status!=="open"&&typeof p.net_pnl==="number"&&isFinite(p.net_pnl)?p.net_pnl:null;
    if(n!==null&&n>0)eject(g,200);else if(n!==null&&n<0)g.st=4;else g.st=5;}
   hasData=rows.length>0||open.length>0;requestAnimationFrame(vxl)}}})();
async function loop(){try{await load()}finally{const ms=LOCAL?10000:delay;nextAt=Date.now()+ms;setTimeout(loop,ms)}}
loop();

// Historial de posiciones cerradas: PnL realizado = Σ net_pnl tal cual viene (no se recalcula). Cierres sin ruta / -100% se muestran.
const finN=v=>typeof v==="number"&&Number.isFinite(v)?v:null;
const toCdmx=v=>{const t=tsMs(v);if(t==null)return null;return new Date(t-6*3600e3).toISOString().slice(0,16).replace("T"," ")};
function dur(a,b){const x=tsMs(a),y=tsMs(b);if(x==null||y==null||y<x)return ND;const m=Math.round((y-x)/60000);return m<60?m+" MIN":Math.floor(m/60)+" H "+(m%60)+" MIN"}
function renderHist(d){const C=(Array.isArray(d.positions)?d.positions:[]).filter(p=>p&&p.status==="closed");
 const out=[];
 ["jev","simple"].forEach(k=>{const L=C.filter(p=>p.strategy===k),V=L.map(p=>finN(p.net_pnl)),ok=V.filter(v=>v!=null),sum=ok.reduce((a,b)=>a+b,0),w=ok.filter(v=>v>0).length;
  out.push(h("div","ptot",null,[h("span","badge "+SB[k],SN[k]),h("span",null,`${L.length} TRADES CERRADOS`),
   h("span",null,null,[h("small","dim","PNL NETO REALIZADO "),h("b",!ok.length?"dim":sum>=0?"gr":"rd",ok.length?fsig(sum):ND),ok.length<L.length?h("small","dim",` (${L.length-ok.length} sin net_pnl)`):null]),
   h("span",null,null,[h("small","dim","GANADORAS "),h("b",null,ok.length?`${w}/${ok.length} · ${(100*w/ok.length).toFixed(1)}%`:ND)])]))});
 const oth=C.filter(p=>p.strategy!=="jev"&&p.strategy!=="simple").length;if(oth)out.push(dim(`${oth} cierres de otra estrategia (incluidos en la tabla)`));
 if(!C.length){out.push(dim("SIN POSICIONES CERRADAS EN EL EXPORT"));put("closed",out);return}
 const th=["ESTRATEGIA / CARRIL","TOKEN · CA","APERTURA CDMX","CIERRE CDMX","DURACIÓN","COSTO","PRECIO ENTRADA","PRECIO SALIDA","PNL NETO $","PNL %","MOTIVO SALIDA"];
 const rows=C.slice().sort((a,b)=>(tsMs(b.closed_ts)??0)-(tsMs(a.closed_ts)??0)).map(p=>{const n=finN(p.net_pnl),c=finN(p.cost_usd),pc=n!=null&&c!=null&&c>0?100*n/c:null,cls=n==null?"dim":n>=0?"gr":"rd";
  const ca=okCA(p.mint)?p.mint:okCA(p.ca)?p.ca:null,xp=pxOK(p.exit_price)??pxOK(p.close_price)??pxOK(p.exit_px);
  const ap=toCdmx(p.opened_ts)??(p.opened_at!=null?String(p.opened_at):p.opened!=null?String(p.opened):ND),cp=toCdmx(p.closed_ts)??(p.closed!=null?String(p.closed):ND);
  const td=(t,cl)=>h("td",cl||(t===ND?"dim":null),t);
  return h("tr",null,null,[h("td",null,null,[h("span","badge "+(SB[p.strategy]||"b-idle"),SN[p.strategy]||String(p.strategy??ND).toUpperCase())," ",dim(String(p.lane??ND).toUpperCase())]),
   h("td",null,null,[h("b",null,String(p.symbol??ND))," ",ca?dim(shortCA(ca)):dim(ND)]),td(ap),td(cp),td(dur(p.opened_ts,p.closed_ts)),td(c!=null?"$"+c.toFixed(2):ND),
   td(pxOK(p.entry_price)!=null?fpxs(p.entry_price):ND),td(xp!=null?fpxs(xp):ND),td(n!=null?fsig(n):ND,cls),td(pc!=null?(pc>=0?"+":"")+pc.toFixed(2)+"%":ND,pc==null?"dim":cls),
   td(p.exit_reason!=null&&p.exit_reason!==""?String(p.exit_reason):ND)])});
 out.push(h("div","htw",null,[h("table","htab",null,[h("thead",null,null,[h("tr",null,null,th.map(t=>h("th",null,t)))]),h("tbody",null,null,rows)])]));
 out.push(h("div","dim small sub","PNL = net_pnl del export (sin recalcular). % = net_pnl / costo. PRECIO SALIDA: el export aún no trae ese campo → SIN DATO."));
 put("closed",out)}
