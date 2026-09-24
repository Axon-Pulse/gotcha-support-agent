// Runs the console's inline JS against a minimal DOM stub and calls every render path.
// The UI is otherwise untested JavaScript; this catches a runtime error in a template
// before it blanks a tab in a browser. Driven by tests/test_console_render.py.
const fs=require('fs'), vm=require('vm'), path=require('path');
const mk=()=>({innerHTML:'',textContent:'',value:'',type:'text',hidden:false,dataset:{},
  style:{},classList:{add(){},remove(){},contains(){return false}},
  scrollIntoView(){},focus(){},getBoundingClientRect:()=>({left:0,width:100}),
  setAttribute(){},querySelectorAll:()=>[]});
const els={};                     // stable per selector, so rendered HTML is inspectable
const el=sel=>els[sel]||(els[sel]=mk());
const sandbox={console,setTimeout,clearInterval,setInterval,Promise,JSON,Math,Number,Object,Date,
  alert:()=>{},confirm:()=>sandbox.__confirm,
  fetch:()=>Promise.resolve({ok:true,json:async()=>({})}),
  document:{querySelector:sel=>el(sel),querySelectorAll:()=>[],getElementById:id=>el('#'+id)}};
sandbox.window=sandbox;
sandbox.__confirm=true;          // what confirm() answers; flipped per check
vm.createContext(sandbox);
const html=fs.readFileSync(path.join(__dirname,'..','console','index.html'),'utf8');
const js=html.split('</div><script>')[1].split('</script>')[0];
vm.runInContext(js,sandbox);

const cases=[];
const check=(label,fn)=>{try{fn();cases.push(['ok',label])}catch(e){cases.push(['FAIL',label,e.message])}};

// --- flow editor ---
sandbox.FLOW={order:['triage','knowledge','topology','network'],
  requires:{topology:['triage'],network:['triage']},picks:true,
  all:[{name:'triage',tools:['a','b']},{name:'knowledge',tools:['c']},
       {name:'topology',tools:['d']},{name:'network',tools:['e']},
       {name:'radar_deep_dive',tools:['f','g']}],sel:null};
check('renderFlow',()=>sandbox.renderFlow());
check('renderFlow with a selection',()=>{sandbox.FLOW.sel='network';sandbox.renderFlow()});
check('pick() adds a prerequisite',()=>{
  sandbox.FLOW.sel='network'; sandbox.pick('knowledge');
  if(!sandbox.FLOW.requires.network.includes('knowledge'))throw new Error('edge not added');});
check('pick() toggles it off',()=>{
  sandbox.FLOW.sel='network'; sandbox.pick('knowledge');
  if((sandbox.FLOW.requires.network||[]).includes('knowledge'))throw new Error('edge not removed');});
check('unlink()',()=>{sandbox.unlink('topology','triage');
  if(sandbox.FLOW.requires.topology)throw new Error('not unlinked');});
check('empty pipeline renders',()=>{const o=sandbox.FLOW.order;sandbox.FLOW.order=[];
  sandbox.renderFlow();sandbox.FLOW.order=o});
check('a backwards prerequisite is reported',()=>{
  sandbox.FLOW.order=['network','triage','knowledge','topology'];
  sandbox.FLOW.requires={network:['triage']};
  const p=sandbox.flowProblems();
  if(p.length!==1||!/runs before it/.test(p[0].why))throw new Error('not flagged: '+JSON.stringify(p));
  sandbox.renderFlow();});
check('a prerequisite off the pipeline is reported',()=>{
  sandbox.FLOW.order=['triage','knowledge']; sandbox.FLOW.requires={knowledge:['network']};
  const p=sandbox.flowProblems();
  if(p.length!==1||!/not in the pipeline/.test(p[0].why))throw new Error('not flagged');
  sandbox.FLOW.order=['triage','knowledge','topology','network']; sandbox.FLOW.requires={};});

// --- knowledge editor ---
sandbox.TOPICS_ALL=['acoustic','camera','general','network','radar'];
sandbox.TOPICS_SEL={kbnew:[],kbdoc:['acoustic']};
const SEC={title:'ASU backend never reachable',intro:'Captured in a fixture.',
  root_cause:'The container was not running.',checks:'    docker ps -a',
  fix:'Start it.',extra:''};
sandbox.KBDOC={name:'asu-dumbo',topics:['acoustic'],symptoms:['no tracks','asu CRITICAL'],
  sections:{...SEC},body:''};
check('renderKb',()=>sandbox.renderKb());
check('each section gets its own field',()=>{
  sandbox.KBDOC={name:'x',topics:[],symptoms:[],sections:{...SEC},body:''};
  sandbox.renderKb();
  const html=sandbox.document.querySelector('#kbdetail').innerHTML;
  for(const id of ['kb-title','kb-intro','kb-rc','kb-checks','kb-fix'])
    if(!html.includes('id="'+id+'"'))throw new Error('missing '+id);
  if(html.includes('id="kb-body"'))throw new Error('the single body box is still there');
  if(html.includes('id="kb-extra"'))throw new Error('empty extra should stay hidden');});
check('an unknown section is surfaced, not dropped',()=>{
  sandbox.KBDOC={name:'x',topics:[],symptoms:[],
    sections:{...SEC,extra:'## Evidence\n\nsomething'},body:''};
  sandbox.renderKb();
  const html=sandbox.document.querySelector('#kbdetail').innerHTML;
  if(!html.includes('id="kb-extra"'))throw new Error('extra section hidden');
  if(!html.includes('Evidence'))throw new Error('extra content lost');});
check('renderKb on a case with no sections yet',()=>{
  sandbox.KBDOC={name:'x',topics:[],symptoms:[],body:''};
  sandbox.TOPICS_SEL.kbdoc=[]; sandbox.renderKb();
  if(!sandbox.KBDOC.sections)throw new Error('sections not defaulted');});
check('closeKb clears the editor in place',()=>{
  sandbox.KBDOC={name:'x',topics:[],symptoms:[],body:'# Old'};
  sandbox.renderKb(); sandbox.closeKb();
  if(sandbox.KBDOC!==null)throw new Error('doc not cleared');
  if(sandbox.document.querySelector('#kbdetail').innerHTML!=='')throw new Error('panel not cleared');});
check('toggleKb closes the case already open',()=>{
  sandbox.KBDOC={name:'same',topics:[],symptoms:[],body:'# S'};
  sandbox.toggleKb('same');
  if(sandbox.KBDOC!==null)throw new Error('second click did not close it');});
check('the editor offers a Close button',()=>{
  sandbox.KBDOC={name:'x',topics:['radar'],symptoms:[],body:'# B'};
  sandbox.TOPICS_SEL.kbdoc=['radar']; sandbox.renderKb();
  const html=sandbox.document.querySelector('#kbdetail').innerHTML;
  if(!html.includes('closeKb()'))throw new Error('no Close button');});
check('a topic toggles on and off',()=>{
  sandbox.TOPICS_SEL.kbdoc=[]; sandbox.toggleTopic('kbdoc','radar');
  if(!sandbox.TOPICS_SEL.kbdoc.includes('radar'))throw new Error('not selected');
  sandbox.toggleTopic('kbdoc','radar');
  if(sandbox.TOPICS_SEL.kbdoc.includes('radar'))throw new Error('not deselected');});
check('several topics can be selected at once',()=>{
  sandbox.TOPICS_SEL.kbdoc=[];
  sandbox.toggleTopic('kbdoc','network'); sandbox.toggleTopic('kbdoc','radar');
  if(sandbox.TOPICS_SEL.kbdoc.length!==2)throw new Error('multi-select failed');});
check('renderRemovable lists only unused topics',()=>{
  sandbox.TOPICS_REMOVABLE=['hydraulics','tower'];
  sandbox.renderRemovable();
  const html=sandbox.document.querySelector('#kbnew-removable').innerHTML;
  if(!html.includes('hydraulics ×')||!html.includes('tower ×'))throw new Error('missing');
  if(html.includes('radar'))throw new Error('listed a topic in use');
  sandbox.TOPICS_REMOVABLE=[]; sandbox.renderRemovable();
  if(sandbox.document.querySelector('#kbnew-removable').innerHTML!=='')
    throw new Error('should be empty when nothing is removable');});
check('renderTopics marks only the selected ones',()=>{
  sandbox.TOPICS_SEL.kbdoc=['radar']; sandbox.renderTopics('kbdoc');
  const html=sandbox.document.querySelector('#kbdoc-topics').innerHTML;
  if(!/class="tchip on"[^>]*radar/.test(html.replace(/\n/g,'')))throw new Error('radar not marked');
  const onCount=(html.match(/class="tchip on"/g)||[]).length;
  if(onCount!==1)throw new Error('expected exactly one selected, got '+onCount);});
check('addSym/rmSym',()=>{sandbox.addSym();
  if(sandbox.KBDOC.symptoms.length!==1)throw new Error('add failed');
  sandbox.rmSym(0);
  if(sandbox.KBDOC.symptoms.length!==0)throw new Error('remove failed');});

// --- agents & permissions ---
sandbox.META={tools:[
  {name:'get_system_health',side_effect:'none',params:[],
   description:'Per-node health. Status, uptime, salient metrics. Cannot see a node that never started.',
   default_description:'d'},
  {name:'search_runbook',side_effect:'none',params:['query','top_k'],
   description:'Search RECORDED CASES by symptom. No hits means the pattern is new.',
   default_description:'d'},
  {name:'get_radar_status',side_effect:'none',params:['node'],
   description:'PLACEHOLDER — the radar domain check is NOT implemented yet.',default_description:'d'}]};
sandbox.CFG={
  supervisor:{name:'supervisor',read_only:true,picks:true,max_steps:10,
    eligible_pool:['triage','knowledge'],
    role:'Routing engine, not a diagnostic agent. It picks the next eligible agent.'},
  effective:{
    agents:{
      triage:{prompt:'Get the overall picture: which nodes are unhealthy. Report what you see.',
              tools:['get_system_health']},
      radar_deep_dive:{prompt:'The radar is implicated. Read its link counters from the digest.',
              tools:['get_radar_status','search_runbook'],
              scope_context:['radar_targets'],requires:['triage']},
      lonely:{prompt:'An agent with no tools at all.',tools:[]}},
    order:['triage'],
    requires:{radar_deep_dive:['triage']},
    supervisor_picks:true,
    post_agents:{customer_communicator:{prompt:'Rewrite the report for the client. No jargon.',tools:[]}},
    allowed_commands:{health:['ecal_mon_cli','-l']},
    tool_descriptions:{get_radar_status:'overridden text'}},
  overridden_keys:['tool_descriptions']};

check('renderPerms (all collapsed)',()=>sandbox.renderPerms());
check('collapsed shows a summary, not the whole prompt',()=>{
  sandbox.OPEN.agent.clear(); sandbox.renderPerms();
  const html=sandbox.document.querySelector('#perms').innerHTML;
  if(html.includes('Report what you see'))throw new Error('full prompt rendered while collapsed');
  if(!html.includes('Get the overall picture'))throw new Error('summary missing');
  if(!html.includes('get_system_health'))throw new Error('tool chips missing');
  // Not "<textarea" — the allowed-commands box is always present and is not an editor.
  if(html.includes('id="ap-'))throw new Error('agent editor rendered while collapsed');
  if(html.includes('id="td-'))throw new Error('tool editor rendered while collapsed');});
check('expanded reveals the editable prompt',()=>{
  sandbox.OPEN.agent.add('triage'); sandbox.renderPerms();
  const html=sandbox.document.querySelector('#perms').innerHTML;
  if(!html.includes('Report what you see'))throw new Error('full prompt missing');
  if(!html.includes('id="ap-triage"'))throw new Error('prompt textarea missing');
  if(!html.includes('data-a="triage"'))throw new Error('tool checkboxes missing');
  sandbox.OPEN.agent.clear();});
check('a tool shows a summary collapsed and its full text expanded',()=>{
  sandbox.OPEN.tool.clear(); sandbox.renderPerms();
  let html=sandbox.document.querySelector('#perms').innerHTML;
  if(html.includes('id="td-search_runbook"'))throw new Error('tool editor open while collapsed');
  sandbox.OPEN.tool.add('search_runbook'); sandbox.renderPerms();
  html=sandbox.document.querySelector('#perms').innerHTML;
  if(!html.includes('id="td-search_runbook"'))throw new Error('tool editor missing');
  if(!html.includes('No hits means the pattern is new'))throw new Error('full description missing');
  sandbox.OPEN.tool.clear();});
check('the supervisor has no edit affordance',()=>{
  sandbox.renderPerms();
  const html=sandbox.document.querySelector('#perms').innerHTML;
  const card=html.slice(html.indexOf('supervisor')-260, html.indexOf('supervisor')+260);
  if(/togglePerm\('agent','supervisor'\)/.test(card))throw new Error('supervisor is toggleable');});
check('togglePerm expands an agent',()=>{
  sandbox.togglePerm('agent','triage');
  if(!sandbox.OPEN.agent.has('triage'))throw new Error('not opened');
  sandbox.renderPerms();});
check('togglePerm collapses it again',()=>{
  sandbox.togglePerm('agent','triage');
  if(sandbox.OPEN.agent.has('triage'))throw new Error('not closed');});
check('tool entry expands',()=>{sandbox.togglePerm('tool','search_runbook');sandbox.renderPerms();});
check('post agent entry expands',()=>{sandbox.togglePerm('post','customer_communicator');
  sandbox.renderPerms();});
check('an agent with no tools renders',()=>{sandbox.togglePerm('agent','lonely');
  sandbox.renderPerms();});
check('firstSentence truncates and keeps one sentence',()=>{
  const f=sandbox.firstSentence;
  if(f('One. Two.',80)!=='One.')throw new Error('sentence split: '+f('One. Two.',80));
  if(!f('x'.repeat(300),40).endsWith('…'))throw new Error('no ellipsis');
  if(f('',40)!=='')throw new Error('empty');});

// --- inventory ---
const comp=(o)=>Object.assign({name:'x',type:'t',role:'sensor',domain:'',address:'',port:null,
  scheme:'tcp',hardware:'',software_version:'',ssh:null},o);
sandbox.INV={path:'/p/systems_inventory.yaml',exists:true,secrets_path:'/p/secrets.local.env',
  domains:['radar','acoustic','camera'],roles:['sensor','compute','laptop','network','power','other'],
  type_catalog:{sensor:['magos_radar','meduza_optic'],compute:['compute_box'],laptop:['laptop'],
                network:['poe_switch'],power:['ups'],other:[]},
  agent_prompt:'- magos (magos_radar)',agent_view:[],defaults:{},
  systems:[{name:'tower1',site:'beit-yanai',description:'d',flat:false,components:[
    comp({name:'magos',type:'magos_radar',role:'sensor',domain:'radar',address:'192.168.40.60',
      port:8080,hardware:'AR-300',software_version:'2.4.1',
      ssh:{user:'magos',port:22,key_file:'~/.ssh/k',password_env:'MAGOS_SSH_PW',
           password:'pw',password_in_environment:false}}),
    comp({name:'cam1',type:'meduza_optic',role:'sensor',domain:'camera',address:'192.168.40.71',port:80}),
    comp({name:'edge1',type:'compute_box',role:'compute',address:'192.168.40.10',
      hardware:'Advantech ARK',ssh:{user:'ops',key_file:'~/.ssh/ops'}}),
    comp({name:'sw1',type:'poe_switch',role:'network',address:'192.168.40.2'}),
    comp({name:'field1',type:'rugged_laptop',role:'laptop',address:'192.168.40.120'})]}]};
sandbox.systems=sandbox.INV.systems;
check('renderInv (list)',()=>sandbox.renderInv());
check('viewSys → read-only summary',()=>{sandbox.viewSys('tower1');
  if(sandbox.EDIT)throw new Error('view mode must not open the editor')});
check('the detail expands in place, not in a card below',()=>{
  sandbox.viewSys('tower1');
  const inline=sandbox.document.querySelector('#sysinline').innerHTML;
  if(!inline.includes('magos'))throw new Error('detail did not render into the row');
  const list=sandbox.document.querySelector('#inv').innerHTML;
  if(list.includes('id="sysdetail"'))throw new Error('the separate card is still there');
  if(!list.includes('id="sysinline"'))throw new Error('no inline row was emitted');});
check('toggleSys closes the system already open',()=>{
  sandbox.viewSys('tower1'); sandbox.toggleSys('tower1');
  if(sandbox.VIEW!==null)throw new Error('second click did not collapse it');});
check('a new system edits under the Add box, not in a row',()=>{
  sandbox.document.querySelector('#newsys').value='tower2';
  sandbox.newSys();
  if(!sandbox.EDIT||!sandbox.EDIT.isNew)throw new Error('new editor not opened');
  if(!sandbox.document.querySelector('#sysnew').innerHTML.includes('New system'))
    throw new Error('new-system form did not render into #sysnew');
  sandbox.EDIT=null; sandbox.VIEW=null;});
check('editSys → dense editor',()=>{sandbox.editSys('tower1');
  if(!sandbox.EDIT)throw new Error('edit mode not entered');
  if(sandbox.EDIT.components.length!==5)throw new Error('components not copied')});
check('every role renders',()=>{sandbox.renderEdit()});
check('components bucket into their categories',()=>{
  const g=sandbox.bucket([
    {name:'r1',role:'sensor',domain:'radar'},
    {name:'c1',role:'sensor',domain:'camera'},
    {name:'a1',role:'sensor',domain:'acoustic'},
    {name:'e1',role:'compute'},{name:'l1',role:'laptop'},
    {name:'s1',role:'network'},{name:'x1',role:'sensor'}]);
  const got=Object.fromEntries(Object.entries(g).map(([k,v])=>[k,v.map(o=>o.c.name)]));
  const want={radar:['r1'],camera:['c1'],acoustic:['a1'],computers:['e1','l1'],
              other:['s1','x1']};
  if(JSON.stringify(got)!==JSON.stringify(want))
    throw new Error('bucketing: '+JSON.stringify(got));});
check('nothing can fall outside a section',()=>{
  const odd=[{name:'q',role:'power'},{name:'z',role:'other',domain:'hydraulics'}];
  const total=Object.values(sandbox.bucket(odd)).reduce((n,v)=>n+v.length,0);
  if(total!==odd.length)throw new Error('a component was hidden');});
check('sections render in view mode',()=>{
  sandbox.viewSys('tower1');
  const html=sandbox.document.querySelector('#sysinline').innerHTML;
  for(const label of ['Radar','Camera','Acoustic','Computers'])
    if(!html.includes('>'+label+'<'))throw new Error('missing section '+label);});
check('a section with nothing in it still offers to add',()=>{
  sandbox.editSys('tower1'); sandbox.renderEdit();
  const html=sandbox.document.querySelector('#sysinline').innerHTML;
  if(!html.includes("addComp('camera')"))throw new Error('no add button for camera');});
check('adding lands in the section clicked, with its defaults',()=>{
  sandbox.editSys('tower1');
  const before=sandbox.EDIT.components.length;
  sandbox.addComp('camera');
  const c=sandbox.EDIT.components[sandbox.EDIT.components.length-1];
  if(sandbox.EDIT.components.length!==before+1)throw new Error('not added');
  if(c.domain!=='camera'||c.role!=='sensor')throw new Error('wrong defaults: '+JSON.stringify(c));
  if(sandbox.sectionOf(c)!=='camera')throw new Error('landed in the wrong section');});
check('adding a computer defaults to the compute role',()=>{
  sandbox.editSys('tower1'); sandbox.addComp('computers');
  const c=sandbox.EDIT.components[sandbox.EDIT.components.length-1];
  if(c.role!=='compute'||c.domain)throw new Error('wrong defaults: '+JSON.stringify(c));});
check('toggleSec opens and closes a section',()=>{
  sandbox.editSys('tower1'); sandbox.SEC_OPEN.clear();
  sandbox.toggleSec('radar');
  if(!sandbox.SEC_OPEN.has('radar'))throw new Error('not opened');
  sandbox.toggleSec('radar');
  if(sandbox.SEC_OPEN.has('radar'))throw new Error('not closed');});
check('the form has no role, domain, scheme or ssh plumbing',()=>{
  sandbox.editSys('tower1'); sandbox.SEC_OPEN=new Set(['radar','camera','acoustic','computers','other']);
  sandbox.renderEdit();
  const html=sandbox.document.querySelector('#sysinline').innerHTML;
  for(const gone of ['SSH user','SSH port','Key file','>Scheme<','>Role<','>Domain<'])
    if(html.includes(gone))throw new Error('still showing '+gone);
  for(const want of ['>Username<','>Web address<','>Password<','>Name<'])
    if(!html.includes(want))throw new Error('missing '+want);});
check('empty fields read as "-", not as a worked example',()=>{
  const html=sandbox.document.querySelector('#sysinline').innerHTML;
  // as PLACEHOLDERS — the same strings are legitimate as stored values
  for(const example of ['2.4.1','Magos AR-300','192.168.40.60','e.g. magos'])
    if(html.includes('placeholder="'+example+'"'))
      throw new Error('placeholder still suggests '+example);
  if(!html.includes('placeholder="-"'))throw new Error('no "-" placeholders');});
check('a new component is numbered, and the number is free inventory-wide',()=>{
  sandbox.editSys('tower1');
  const before=new Set(sandbox.INV.systems.flatMap(s=>(s.components||s.sensors||[]).map(c=>String(c.name))));
  sandbox.addComp('radar');
  const n=sandbox.EDIT.components[sandbox.EDIT.components.length-1].name;
  if(!/^\d+$/.test(n))throw new Error('not a number: '+n);
  if(before.has(n))throw new Error('collides with an existing component: '+n);});
check('numbers keep counting up within one edit',()=>{
  sandbox.editSys('tower1');
  sandbox.addComp('radar'); sandbox.addComp('camera');
  const names=sandbox.EDIT.components.slice(-2).map(c=>c.name);
  if(names[0]===names[1])throw new Error('duplicate number: '+names);});
check('the number is still editable',()=>{
  sandbox.editSys('tower1'); sandbox.addComp('radar');
  const i=sandbox.EDIT.components.length-1;
  sandbox.setS(i,'name','front-radar');
  if(sandbox.EDIT.components[i].name!=='front-radar')throw new Error('not editable');});
check('addComp/rmComp',()=>{
  // Relative, not absolute: these checks share one sandbox, so a fixed expected length
  // breaks whenever an earlier check adds a component.
  sandbox.editSys('tower1');
  const n=sandbox.EDIT.components.length;
  sandbox.addComp('radar');
  if(sandbox.EDIT.components.length!==n+1)throw new Error('add failed');
  sandbox.rmComp(n);
  if(sandbox.EDIT.components.length!==n)throw new Error('remove failed')});
check('component with no ssh renders',()=>{sandbox.editSys('tower1');
  sandbox.EDIT.components[1].ssh={};sandbox.renderEdit()});
check('clearPw flags deletion',()=>{sandbox.clearPw(0);
  if(!sandbox.EDIT.components[0].ssh.clear_password)throw new Error('flag not set')});
check('cancel returns to the view',()=>{sandbox.viewSys('tower1');
  if(sandbox.EDIT)throw new Error('editor still open')});
check('a failed save keeps the editor open and says why',()=>{
  sandbox.editSys('tower1');
  sandbox.EDIT.error='component \'b\': SSH port -1 is out of range';
  sandbox.renderEdit();
  const html=sandbox.document.querySelector('#sysinline').innerHTML;
  if(!html.includes('Not saved.'))throw new Error('no failure banner');
  if(!html.includes('SSH port -1'))throw new Error('the reason is not shown');});
check('collapsing with unsaved changes asks first',()=>{
  sandbox.editSys('tower1');
  sandbox.__confirm=false; sandbox.closeView();
  if(!sandbox.EDIT)throw new Error('discarded without asking');
  sandbox.__confirm=true; sandbox.closeView();
  if(sandbox.EDIT)throw new Error('kept the editor after a confirmed discard');});
check('opening another system with unsaved changes asks first',()=>{
  sandbox.editSys('tower1');
  sandbox.__confirm=false; sandbox.viewSys('rack1');
  if(!sandbox.EDIT)throw new Error('discarded without asking');
  sandbox.__confirm=true; sandbox.viewSys('tower1');});
check('the payload omits ssh settings the form cannot edit',()=>{
  sandbox.editSys('tower1');
  sandbox.EDIT.components[0].ssh={user:'ops',port:2222,key_file:'~/.ssh/k',password:'pw'};
  // Capture at fetch, not at api(): `api` is a top-level const, so it resolves
  // lexically and replacing the sandbox property would not affect it.
  let sent=null;
  const realFetch=sandbox.fetch;
  sandbox.fetch=(p,o)=>{ sent=JSON.parse(o.body);
    return Promise.resolve({ok:true,json:async()=>({name:'tower1',systems:[]})}); };
  sandbox.saveSys();
  sandbox.fetch=realFetch;
  if(!sent)throw new Error('saveSys never sent anything');
  const ssh=sent.components[0].ssh;
  if('port' in ssh||'key_file' in ssh)
    throw new Error('round-tripped a field the form does not edit: '+JSON.stringify(ssh));
  if(ssh.user!=='ops'||ssh.password!=='pw')throw new Error('dropped what it does edit');});
check('legacy sensors key still renders',()=>{
  const sy=sandbox.INV.systems[0]; const keep=sy.components;
  delete sy.components; sy.sensors=keep; sandbox.renderInv(); sandbox.viewSys('tower1');
  sy.components=keep;});
check('empty inventory renders',()=>{const s2=sandbox.INV.systems;sandbox.INV.systems=[];
  sandbox.VIEW=null;sandbox.EDIT=null;sandbox.renderInv();sandbox.INV.systems=s2});
check('newSys refuses an empty name',()=>{
  sandbox.document.querySelector('#newsys').value='';
  sandbox.EDIT=null; sandbox.newSys();
  if(sandbox.EDIT)throw new Error('opened an editor for an unnamed system');});
check('closeView clears both modes',()=>{sandbox.closeView();
  if(sandbox.VIEW||sandbox.EDIT)throw new Error('not cleared')});

// --- credentials & usage ---
sandbox.META={credentials:{},order:[]};
check('no credentials renders the key form',()=>{
  sandbox.renderCreds({ready:false,source:null,warning:'',stored:false,masked:null,
                       localhost_only:'no authentication'});
  const h=el('#creds').innerHTML;
  if(!/id="apikey"/.test(h))throw new Error('no way to enter a key');
  if(!/no authentication/.test(h))throw new Error('the localhost warning is not shown');
  if(el('#go').disabled!==true)throw new Error('Run is enabled without credentials');});
check('a stored key renders masked, with Clear',()=>{
  sandbox.renderCreds({ready:true,source:'secrets.local.env',warning:'',stored:true,
                       masked:'sk-ant-api03-••••••••ZQ4A',localhost_only:'x'});
  const h=el('#creds').innerHTML;
  if(!/ZQ4A/.test(h))throw new Error('the mask is not shown');
  if(!/id="clearkey"/.test(h))throw new Error('no way to clear a stored key');
  if(el('#go').disabled!==false)throw new Error('Run stayed disabled with a key');});
check('the key itself is never rendered back',()=>{
  // The server sends a mask, never the value — but assert the UI would not print one
  // even if a future change started returning it.
  sandbox.renderCreds({ready:true,source:'ANTHROPIC_API_KEY',warning:'',stored:true,
                       masked:'sk-ant-api03-••••••••ZQ4A',localhost_only:'x',
                       key:'sk-ant-api03-SECRETVALUE'});
  if(/SECRETVALUE/.test(el('#creds').innerHTML))throw new Error('leaked the key into the DOM');});
check('a credential warning is surfaced',()=>{
  sandbox.renderCreds({ready:true,source:'ANTHROPIC_API_KEY',stored:false,masked:null,
                       warning:'Both a key and a token are set.',localhost_only:'x'});
  if(!/Both a key and a token/.test(el('#creds').innerHTML))throw new Error('warning dropped');});
check('usage renders lifetime alone',()=>{
  sandbox.LIFETIME={lifetime:{input:1234567,output:96000,cache_read:0},sessions:47};
  sandbox.paintUsage(null);
  const h=el('#usage').innerHTML;
  if(!/1\.2M/.test(h)||!/96K/.test(h))throw new Error('not abbreviated: '+h);
  if(!/47 sessions/.test(h))throw new Error('session count missing');
  if(/this session/.test(h))throw new Error('claimed a session total with none given');});
check('usage renders session and lifetime together',()=>{
  sandbox.paintUsage({input:42118,output:3204,cache_read:38900});
  const h=el('#usage').innerHTML;
  if(!/this session/.test(h)||!/lifetime/.test(h))throw new Error('missing a half: '+h);});
check('usage survives missing numbers',()=>{
  sandbox.LIFETIME=null; sandbox.paintUsage({});
  if(/NaN|undefined/.test(el('#usage').innerHTML))throw new Error('printed NaN/undefined');});
check('no credentials points at the key field further down the page',()=>{
  sandbox.renderCreds({ready:false,source:null,warning:'',stored:false,masked:null,
                       localhost_only:'x'});
  const h=el('#credhint');
  if(h.hidden!==false)throw new Error('the hint is hidden while Run is disabled');
  if(!/Set a key/.test(h.innerHTML))throw new Error('no pointer to the key field');});
check('the hint disappears once a key resolves',()=>{
  sandbox.renderCreds({ready:true,source:'ANTHROPIC_API_KEY',warning:'',stored:false,
                       masked:null,localhost_only:'x'});
  if(el('#credhint').hidden!==true)throw new Error('still nagging with a working key');});

// --- inventory: the required hardware type ---
check('adding a sensor prefills the hardware type its section implies',()=>{
  sandbox.INV={systems:[],domains:[],type_catalog:{}};
  sandbox.EDIT={name:'tower1',orig:'tower1',components:[],isNew:true};
  sandbox.addComp('acoustic');
  const c=sandbox.EDIT.components[0];
  if(c.type!=='acoustic')throw new Error('type left blank: '+JSON.stringify(c.type));
  if(sandbox.missingTypes().length)throw new Error('reported as missing despite a default');});
check('a section with no obvious type is reported before any request',()=>{
  sandbox.EDIT={name:'tower1',orig:'tower1',components:[],isNew:true};
  sandbox.addComp('computers');
  const bad=sandbox.missingTypes();
  if(bad.length!==1)throw new Error('should be exactly one missing: '+JSON.stringify(bad));
  let sent=false;
  const realFetch=sandbox.fetch;
  sandbox.fetch=()=>{sent=true;return Promise.resolve({ok:true,json:async()=>({})})};
  sandbox.saveSys();
  sandbox.fetch=realFetch;
  if(sent)throw new Error('sent a request that could only be refused');
  if(!/hardware type/.test(sandbox.EDIT.error||''))
    throw new Error('no explanation recorded: '+sandbox.EDIT.error);});
check('filling the type in clears the complaint',()=>{
  sandbox.EDIT.components[0].type='dell-optiplex';
  if(sandbox.missingTypes().length)throw new Error('still reported after being filled');});

let bad=0;
for(const c of cases){ if(c[0]!=='ok')bad++; console.log(c[0].padEnd(5),c[1],c[2]?'→ '+c[2]:''); }
console.log(bad?`\n${bad} FAILED`:`\nall ${cases.length} render paths ok`);
process.exit(bad?1:0);
