// The Knowledge tab's case list and topic picker under a DOM stub: the small "+ topic"
// chip, filtering by topic and created time, sorting, and deleting from the editor.
const fs=require('fs'), vm=require('vm'), path=require('path');
const mk=()=>({innerHTML:'',textContent:'',value:'',hidden:false,dataset:{},style:{},
  classList:{add(){},remove(){},contains(){return false}},
  setAttribute(k,v){this['_'+k]=v},getAttribute(k){return this['_'+k]},
  scrollIntoView(){},focus(){this.focused=true},contains(){return false},querySelectorAll:()=>[]});
const els={}; const el=sel=>els[sel]||(els[sel]=mk());
const DOCS=[
  {name:'old-radar',topics:['radar','network'],symptom_count:2,bytes:10,
   created:'2026-09-01T10:00:00',created_source:'git',sessions:0,search_hits:3},
  {name:'new-launcher',topics:['config'],symptom_count:1,bytes:10,
   created:'2026-09-20T09:00:00',created_source:'recorded',sessions:2,search_hits:0},
  {name:'untagged',topics:[],symptom_count:0,bytes:10,
   created:'2026-09-10T09:00:00',created_source:'file',sessions:0,search_hits:0}];
const calls=[];
const sandbox={console,setTimeout,clearInterval,setInterval,Promise,JSON,Math,Number,Object,Date,
  alert:()=>{},confirm:()=>true,
  fetch:(p,o={})=>{calls.push([o.method||'GET',p]);return Promise.resolve({ok:true,json:async()=>
    /\/kb$/.test(p)?{docs:DOCS,all_topics:['config','network','radar'],removable_topics:[]}
    :/\/system-model$/.test(p)?{text:'',path:'kb/system-model.md'}
    :/\/kb\/new-launcher$/.test(p)&&!o.method?{name:'new-launcher',topics:['config'],symptoms:['x'],
       sections:{title:'t',intro:'',root_cause:'',checks:'',fix:'',extra:''},all_topics:[]}
    :/\/topics$/.test(p)?{all_topics:['config','hydraulics','network','radar'],removable_topics:['hydraulics']}
    :{ok:true,sessions:[],pending:0,working:0}})},
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
const order=()=>[...el('#kb-rows').innerHTML.matchAll(/toggleKb\('([\w-]+)'\)/g)].map(m=>m[1]).join(',');

(async()=>{
await check('the list shows created time and sessions, newest first',async()=>{
  await sandbox.loadKb();
  want(order()==='new-launcher,untagged,old-radar','order '+order());
  const h=el('#kb-rows').innerHTML;
  want(h.includes('2026-09-20 09:00'),'created time missing');
  want(/~2026-09-01 10:00/.test(h),'estimated time not marked');
  want(/· 3\s+in search/.test(h),'search hits not shown');
  want(!/\d+ B</.test(h),'size still shown');});
await check('adding a topic is a small chip that opens in place',async()=>{
  const h=el('#kbnew-topics').innerHTML;
  want(/\+ topic/.test(h),'no + topic chip');
  want(!/newtopic/.test(h),'input shown before asked for');
  sandbox.startAddTopic('kbnew');
  want(/id="kbnew-newtopic"/.test(el('#kbnew-topics').innerHTML),'input not opened');
  want(el('#kbnew-newtopic').focused,'input not focused');
  el('#kbnew-newtopic').value='hydraulics';
  await sandbox.addTopic('kbnew');
  const after=el('#kbnew-topics').innerHTML;
  want(/✓ hydraulics/.test(after),'new topic not selected');
  want(/\+ topic/.test(after)&&!/newtopic/.test(after),'chip did not come back');
  sandbox.startAddTopic('kbnew'); sandbox.cancelAddTopic('kbnew');
  want(!/newtopic/.test(el('#kbnew-topics').innerHTML),'Esc did not close it');});
await check('filter by topic: any selected topic matches',async()=>{
  sandbox.kbFilterTopic('radar');
  want(order()==='old-radar','radar '+order());
  sandbox.kbFilterTopic('config');
  want(order()==='new-launcher,old-radar','radar or config '+order());
  want(el('#kbcount').textContent==='2 of 3','count '+el('#kbcount').textContent);
  sandbox.kbClear();});
await check('filter by created date, inclusive',async()=>{
  el('#kbf-from').value='2026-09-10'; el('#kbf-to').value='2026-09-20'; sandbox.kbFilterDates();
  want(order()==='new-launcher,untagged','range '+order());
  sandbox.kbClear();});
await check('sort by time and topics, both ways; untagged last',async()=>{
  sandbox.kbSort(null,true);
  want(order()==='old-radar,untagged,new-launcher','time asc '+order());
  sandbox.kbSort('topics');
  want(order()==='new-launcher,old-radar,untagged','topics asc '+order());
  sandbox.kbSort(null,true);
  want(order()==='old-radar,new-launcher,untagged','topics desc '+order());});
await check('clear resets filters and sort',async()=>{
  sandbox.kbFilterTopic('radar'); sandbox.kbClear();
  want(order()==='new-launcher,untagged,old-radar','after clear '+order());
  want(el('#kbsort').value==='time','sort dropdown');
  want(el('#kbdir').textContent.includes('descending'),'direction');});
await check('the editor deletes without saving first',async()=>{
  await sandbox.showKb('new-launcher');
  want(/Delete case/.test(el('#kbdetail').innerHTML),'no delete in the editor');
  sandbox.KBDOC.symptoms.push('unsaved edit');
  calls.length=0;
  await sandbox.delKb('new-launcher');
  want(calls.some(c=>c[0]==='DELETE'&&/\/kb\/new-launcher$/.test(c[1])),'no DELETE sent');
  want(!calls.some(c=>c[0]==='PUT'),'saved before deleting');
  want(sandbox.KBDOC===null,'editor left open on a deleted case');});

let bad=0;
for(const c of cases){if(c[0]!=='ok')bad++;console.log(c[0].padEnd(5),c[1],c[2]?'→ '+c[2]:'');}
console.log(bad?`\n${bad} FAILED`:`\nall ${cases.length} knowledge page checks ok`);
process.exit(bad?1:0);
})();
