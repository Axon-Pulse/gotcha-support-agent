// The Trace tab's JS under a DOM stub: open/close toggling, the summary, and search.
// Separate from console_render_harness.js so the two can change independently.
const fs=require('fs'), vm=require('vm'), path=require('path');
const mk=()=>({innerHTML:'',textContent:'',value:'',hidden:false,dataset:{},style:{},
  classList:{add(){},remove(){},contains(){return false}},
  setAttribute(k,v){this['_'+k]=v},getAttribute(k){return this['_'+k]},
  scrollIntoView(){},focus(){},contains(){return false},querySelectorAll:()=>[]});
const els={}; const el=sel=>els[sel]||(els[sel]=mk());
const TRACES=[
  {id:'aaa',tag:'0002',session:2,started:200,system:'gotcha3',system_serial:1,date:'2026-09-24',
   location:'lebanon',tokens:41000,asker:'gal',via:'chat',problem:'radar shows no tracks',
   diagnosis:'magos node down',found:true,bytes:10},
  {id:'bbb',tag:'0001',session:1,started:100,system:null,system_serial:null,date:'2026-09-16',
   location:null,tokens:null,asker:null,via:'bridge',problem:'no signal',diagnosis:'',
   found:false,bytes:5},
  {id:'ccc',tag:'0003',session:3,started:300,system:'gotcha5',system_serial:1,date:'2026-09-28',
   location:null,tokens:900,asker:'gal',via:'run',problem:'',diagnosis:'',found:false,bytes:7},
  {id:'ddd',tag:'0004',session:4,started:400,system:'gotcha3',system_serial:2,date:'2026-09-28',
   location:'lebanon',tokens:120,asker:'gal',via:'run',problem:'crashed',diagnosis:'',
   found:false,bytes:3,status:'error',status_label:'failed',finished:false},
  {id:'eee',tag:'0005',session:5,started:500,system:null,system_serial:null,date:'2026-09-28',
   location:null,tokens:null,asker:null,via:'bridge',problem:'waiting',diagnosis:'',
   found:false,bytes:2,status:'pending',status_label:'not picked up',finished:false}];
for(const t of TRACES) if(!('finished' in t)) Object.assign(t,{status:'done',status_label:'finished',finished:true});
const DETAIL={id:'aaa',tag:'0002',summary:{via:'chat',problem:'radar shows no tracks',
  follow_ups:[],symptoms:['magos_node: not running'],diagnosis:'magos node down',
  root_cause:'magos node down',confidence:'high',solution:'',next_steps:['check pid'],
  escalate:false,unknowns:[],needs_permission:[],found:true,turns:1},
  sections:[{key:'findings',title:'Tool results',why:'What each tool returned.',value:[1]}],
  entries:[{}]};
let fetches=0;
const sandbox={console,setTimeout,clearInterval,setInterval,Promise,JSON,Math,Number,Object,Date,
  alert:()=>{},confirm:()=>true,
  fetch:p=>{fetches++;return Promise.resolve({ok:true,json:async()=>
    /\/traces\/aaa$/.test(p)?DETAIL:/\/traces$/.test(p)?{traces:TRACES,systems:['gotcha9','gotcha3']}
    :{sessions:[],pending:0,working:0}})},
  document:{querySelector:sel=>el(sel),querySelectorAll:()=>[],getElementById:id=>el('#'+id),
            addEventListener(){}}};
sandbox.window=sandbox;
vm.createContext(sandbox);
const html=fs.readFileSync(path.join(__dirname,'..','console','index.html'),'utf8');
vm.runInContext(html.split('</div><script>')[1].split('</script>')[0],sandbox);

const cases=[];
async function check(name,f){try{await f();cases.push(['ok',name])}
  catch(e){cases.push(['FAIL',name,e.message])}}
const want=(c,m)=>{if(!c)throw new Error(m)};
// The ids in the order the table body lists them.
const order=()=>[...el('#tbody').innerHTML.matchAll(/id="tr-(\w+)"/g)].map(m=>m[1]).join(',');
const filt=(o={})=>{el('#tfrom').value=o.from||'';el('#tto').value=o.to||'';
  for(const k of ['system','location','asker'])el('#tf-'+k).value=o[k]||'';
  sandbox.filterTraces();return order();};
(async()=>{
await check('the list renders filters and sort, one row per trace, newest first',async()=>{
  await sandbox.loadTraces();
  const h=el('#traces').innerHTML;
  for(const id of ['tfrom','tto','tf-system','tf-location','tf-asker','tsort','tdir'])
    want(h.includes(`id="${id}"`),'no control '+id);
  want(order()==='ccc,aaa,bbb','default order '+order());
  // The problem belongs to the opened summary, not the collapsed row.
  want(!/radar shows no tracks/.test(el('#tbody').innerHTML),'problem shown on a collapsed row');});
await check('system dropdown is the inventory, plus - for unknown',async()=>{
  const h=el('#traces').innerHTML;
  const sys=h.slice(h.indexOf('id="tf-system"'),h.indexOf('</select>',h.indexOf('id="tf-system"')));
  want(/value="gotcha9"/.test(sys),'inventory system with no sessions not offered');
  want(/value="gotcha3"/.test(sys),'gotcha3 missing');
  want(!/value="gotcha5"/.test(sys),'offered a system no longer in the inventory');
  want(/- \(unknown\)/.test(sys),'no unknown option');
  want(sys.indexOf('gotcha3')<sys.indexOf('gotcha9'),'not sorted');});
await check('open turns into close and shows the summary',async()=>{
  await sandbox.toggleTrace('aaa');
  want(el('#tb-aaa').textContent==='close','button still says '+el('#tb-aaa').textContent);
  want(el('#tdr-aaa').hidden===false,'detail row hidden');
  const h=el('#tdc-aaa').innerHTML;
  for(const w of ['Problem','Symptoms','Diagnosis','Solution','Tool results','What each tool returned',
                  'Raw trace file','magos_node: not running','check pid'])
    want(h.includes(w),'detail lacks '+w);});
await check('close hides it again and says open',async()=>{
  await sandbox.toggleTrace('aaa');
  want(el('#tb-aaa').textContent==='open','button says '+el('#tb-aaa').textContent);
  want(el('#tdr-aaa').hidden===true,'detail row still shown');});
await check('reopening reuses the loaded detail',async()=>{
  const before=fetches; await sandbox.toggleTrace('aaa');
  want(fetches===before,'fetched the same trace twice');
  await sandbox.toggleTrace('aaa');});
await check('filter by system, location, asker, including unknown',async()=>{
  want(filt({system:'gotcha3'})==='aaa','system');
  want(filt({system:'-'})==='bbb','unknown system');
  want(filt({location:'-'})==='ccc,bbb','unknown location');
  want(filt({asker:'gal'})==='ccc,aaa','asker');
  want(filt({asker:'gal',system:'gotcha5'})==='ccc','filters combine');
  want(filt()==='ccc,aaa,bbb','cleared');});
await check('filter by date range, inclusive',async()=>{
  want(filt({from:'2026-09-24'})==='ccc,aaa','from');
  want(filt({to:'2026-09-24'})==='aaa,bbb','to');
  want(filt({from:'2026-09-17',to:'2026-09-27'})==='aaa','between');
  filt();});
await check('sort by time and tokens, both directions; unknown tokens last',async()=>{
  sandbox.sortTraces(null,true);
  want(order()==='bbb,aaa,ccc','time ascending '+order());
  want(el('#tdir').textContent.includes('ascending'),'direction label');
  sandbox.sortTraces('tokens');
  want(order()==='ccc,aaa,bbb','tokens ascending '+order());
  sandbox.sortTraces(null,true);
  want(order()==='aaa,ccc,bbb','tokens descending '+order());
  sandbox.sortTraces('time');});
await check('an open trace stays open through a sort and a filter',async()=>{
  await sandbox.toggleTrace('aaa');
  sandbox.sortTraces('tokens'); filt({system:'gotcha3'});
  want(/magos node down/.test(el('#tbody').innerHTML),'detail lost on re-render');
  want(/>close</.test(el('#tbody').innerHTML),'button reset to open');
  filt(); sandbox.sortTraces('time');});
await check('nothing matching says so',async()=>{
  want(filt({system:'gotcha3',asker:'-'})==='','rows shown');
  want(/No finished session matches/.test(el('#tbody').innerHTML),'no empty message');
  filt();});

await check('clear resets the filters and the sort to newest first',async()=>{
  filt({system:'gotcha3'}); sandbox.sortTraces('tokens',true);
  el('#tsort').value='tokens';
  sandbox.clearTraceFilters();
  want(order()==='ccc,aaa,bbb','order after clear '+order());
  want(el('#tsort').value==='time','sort dropdown not reset');
  want(el('#tdir').textContent.includes('descending'),'direction not reset');
  want(el('#tf-system').value==='','system filter kept');});

await check('unfinished sessions are hidden by default and counted on the button',async()=>{
  sandbox.clearTraceFilters();
  want(order()==='ccc,aaa,bbb','default shows unfinished: '+order());
  want(/2 unfinished/.test(el('#tall').textContent),'button: '+el('#tall').textContent);
  want(el('#tall').getAttribute('aria-pressed')==='false','pressed by default');});
await check('show all includes them, labelled, and filters still apply',async()=>{
  sandbox.toggleAllTraces();
  want(order()==='eee,ddd,ccc,aaa,bbb','all: '+order());
  want(el('#tall').getAttribute('aria-pressed')==='true','not pressed');
  const h=el('#tbody').innerHTML;
  want(/not picked up/.test(h)&&/pill bad">failed/.test(h),'status labels missing');
  want(!/>finished</.test(h),'finished ones should carry no label');
  want(filt({system:'gotcha3'})==='ddd,aaa','filter with all on: '+order());
  filt();});
await check('clear turns show all back off',async()=>{
  sandbox.clearTraceFilters();
  want(order()==='ccc,aaa,bbb','after clear: '+order());
  want(el('#tall').getAttribute('aria-pressed')==='false','still pressed');});

let bad=0;
for(const c of cases){if(c[0]!=='ok')bad++;console.log(c[0].padEnd(5),c[1],c[2]?'→ '+c[2]:'');}
console.log(bad?`\n${bad} FAILED`:`\nall ${cases.length} trace page checks ok`);
process.exit(bad?1:0);
})();
