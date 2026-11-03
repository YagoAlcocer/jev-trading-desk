# Builds the `const D = {...};` block for the paper-trading canvas from dashboard_data.py JSON.
import json,sys,bisect
d=json.load(open(sys.argv[1]))
c=d['curves']['calibration']
hm=lambda t:t[11:16]
lab=[x['t'] for x in c['hold_btc']]
def samp(curve):
    ts=[x['t'] for x in curve]; out=[]
    for t in lab:
        i=bisect.bisect_right(ts,t)-1
        out.append(round(curve[i]['v'],2) if i>=0 else 1000.0)
    return out
def comp(v):
    n=0
    while n<len(v) and v[n]==1000.0: n+=1
    rest=json.dumps(v[n:]).strip('[]')
    return f"[...Array({n}).fill(1000), {rest}] as number[]" if n>3 else json.dumps(v)+" as number[]"
rbm=dict(d['rejections_by_minute'])
rej=[rbm.get(t,0) for t in lab]
fs=dict(d['funnel_stages']['hoy'])
otro=fs.get('otro',0)+fs.get('7 edge_est',0)
stages=[["Evaluados (incl. re-escaneos)",d['scanned_today']],["Caen en liquidez",fs['1 liquidez']],["Caen en volumen 24h",fs['2 volumen 24h']],["Caen en LP quemado",fs['3 LP quemado']],["Caen en on-chain",fs['4 on-chain']],["Caen en copycat",fs['5 copycat']],["Caen en límites de riesgo",fs['6 límites de riesgo']],["Caen en otro (bonding curve, etc.)",otro],["Llegan a Jev",fs['7 Jev']],["Llegan a ejecución",fs['8 ejecución']]]
cat={'liquidez':'liquidez','volumen 24h':'volumen','volumen':'volumen','copycat':'copycat'}
feed=[[f['t'],f['symbol'] or '—',f['rule'],cat.get(f['first_stage'] or '', 'otro')] for f in d['feed'][:15]]
def money(x):
    s='+' if x>=0 else '−'; return f"{s}${abs(x):.2f}"
def px(x): return f"{x:.6g}" if x is not None else "sin marca aún"
pos=[]
for p in d['positions']:
    if p['status']!='open': continue
    st=p['strategy']+(' (añadido)' if p['leg']=='add_on' else '')
    up=p['unrealized_pnl']
    pos.append([p['symbol'],st,p['opened'][11:16],f"${p['cost_usd']:.2f}",px(p['entry_price']),px(p['current_price']),money(up) if up is not None else "—","spot, sin apalancamiento"])
L=[]
L.append('const D = {')
L.append(f'  generated: "{d["generated"]}",')
L.append(f'  lastCycle: "{hm(d["last_cycle"])}",')
L.append(f'  lastScan: "{hm(d["last_scan"])}",')
L.append(f'  pid: {d["loop_pid"]},')
L.append(f'  chainRecords: {d["chain_records"]},')
L.append(f'  scannedToday: {d["scanned_today"]},')
L.append(f'  jevQuestions: {d["jev"]["questions_answered"]},')
L.append(f'  evalStart: "{d["eval_start"]}",')
L.append(f'  labels: {json.dumps([hm(t) for t in lab],ensure_ascii=False)},')
L.append(f'  rejByCycle: {json.dumps(rej)},')
L.append(f'  jev: {comp(samp(c["jev"]))},')
L.append(f'  simple: {comp(samp(c["simple"]))},')
L.append(f'  btc: {json.dumps([round(x["v"],2) for x in c["hold_btc"]])},')
L.append(f'  sol: {json.dumps([round(x["v"],2) for x in c["hold_sol"]])},')
L.append(f'  stages: {json.dumps(stages,ensure_ascii=False)} as [string, number][],')
L.append(f'  rules: {json.dumps(d["rule_counts"]["hoy"],ensure_ascii=False)} as [string, number][],')
L.append(f'  feed: {json.dumps(feed,ensure_ascii=False)} as [string, string, string, string][],')
L.append(f'  positions: {json.dumps(pos,ensure_ascii=False)} as string[][],')
L.append('};')
print("\n".join(L))
