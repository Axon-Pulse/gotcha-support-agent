// Runs the console's inline JS against a minimal DOM stub and calls every render path.
// The UI is otherwise untested JavaScript; this catches a runtime error in a template
// before it blanks a tab in a browser. Driven by tests/test_console_render.py.
const fs=require('fs'), vm=require('vm'), path=require('path');
const mk=()=>({innerHTML:'',textContent:'',value:'',type:'text',hidden:false,dataset:{},
  style:{},classList:{add(){},remove(){},contains(){return false}},
  scrollIntoView(){},focus(){},getBoundingClientRect:()=>({left:0,width:100}),
  setAttribute(k,v){this[k==='aria-expanded'?'_expanded':k]=v},
  contains(){return false},querySelectorAll:()=>[]});
const els={};                     // stable per selector, so rendered HTML is inspectable
const el=sel=>els[sel]||(els[sel]=mk());
// A real tab bar, because tab visibility is now logic worth testing: the Run and
// Live-with-chat tabs appear only in their own mode.
const TAB_NAMES=['run','chat','approvals','traces','kb','graph','perms','inv'];
const tabs=TAB_NAMES.map(t=>({dataset:{t},hidden:false,_attrs:{'aria-selected':t==='run'?'true':'false'},
  setAttribute(k,v){this._attrs[k]=v}, getAttribute(k){return this._attrs[k]},
  get textContent(){return t}, set onclick(f){this._click=f}}));
const qsa=sel=>/#tabs button/.test(sel||'')?tabs:[];
const sandbox={console,setTimeout,clearInterval,setInterval,Promise,JSON,Math,Number,Object,Date,
  alert:()=>{},confirm:()=>sandbox.__confirm,
  fetch:()=>Promise.resolve({ok:true,json:async()=>({sessions:[],pending:0,working:0})}),
  document:{querySelector:sel=>el(sel),querySelectorAll:qsa,getElementById:id=>el('#'+id),
            addEventListener(){}}};
sandbox.__tabs=tabs;
sandbox.window=sandbox;
sandbox.location={hash:''};      // the open tab lives here now, so it must be modelled
sandbox.addEventListener=()=>{};
sandbox.__confirm=true;          // what confirm() answers; flipped per check
vm.createContext(sandbox);
const html=fs.readFileSync(path.join(__dirname,'..','console','index.html'),'utf8');
const js=html.split('</div><script>')[1].split('</script>')[0];
vm.runInContext(js,sandbox);

// Sections come from the operator-editable type catalogue now, so the harness has to
// supply one exactly as loadInv() does.
sandbox.INVTYPES={
  radar:{label:'Radar',role:'sensor',domain:'radar',scheme:'tcp',
         fields:[{key:'azimuth',label:'Azimuth °',default:''},
                 {key:'elevation',label:'Elevation °',default:'0'}]},
  camera:{label:'Camera',role:'sensor',domain:'camera',scheme:'http',fields:[]},
  acoustic:{label:'Acoustic',role:'sensor',domain:'acoustic',scheme:'http',fields:[]},
  computers:{label:'Computers',role:'compute',domain:'',scheme:'tcp',fields:[]},
  other:{label:'Other',role:'other',domain:'',scheme:'tcp',fields:[]}};
sandbox.rebuildSections();

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

// --- component type editor ---
sandbox.TYPEMETA={roles:['sensor','compute','laptop','network','power','other'],
  defaults:{},overridden:false,always_shown:['name','type'],
  builtin_fields:[{key:'address',label:'IP address'},{key:'port',label:'Port'},
    {key:'hardware',label:'Hardware model'},{key:'software_version',label:'Version'},
    {key:'web',label:'Web address'},{key:'location',label:'Latitude / longitude'},
    {key:'ssh',label:'Username & password'}]};
check('the summary lists the types and their fields',()=>{
  sandbox.TYPEDRAFT=null; sandbox.INV={systems:[]};
  sandbox.renderTypes();
  const h=el('#typeed').innerHTML;
  if(!/Radar/.test(h)||!/Acoustic/.test(h))throw new Error('types not listed');
  if(!/azimuth/.test(h))throw new Error('fields not summarised');
  if(!/startTypes\(\)/.test(h))throw new Error('no way into the editor');});
check('the editor exposes every type and its fields',()=>{
  sandbox.startTypes();
  const h=el('#typeed').innerHTML;
  if(!/TYPEDRAFT\['radar'\].label/.test(h))throw new Error('the label is not editable');
  if(!/TYPEDRAFT\['radar'\].fields\[0\].key/.test(h))throw new Error('fields not editable');
  if(!/default \(optional\)/.test(h))throw new Error('no default column');
  if(!/addType\(\)/.test(h))throw new Error('cannot add a type');});
check('the type editor offers every standard field, not just the extras',()=>{
  // The complaint this answers: the type editor showed four boxes while a component
  // card showed eleven.
  const h=el('#typeed').innerHTML;
  // The ampersand arrives escaped, which is the point of esc() — assert the escaped
  // form rather than loosening the check.
  for(const lbl of ['IP address','Port','Hardware model','Version','Web address',
                    'Latitude / longitude','Username &amp; password'])
    if(!h.includes(lbl))throw new Error('standard field missing from the editor: '+lbl);
  if(!/always\s+shown/i.test(h.replace(/<[^>]+>/g,' ')))
    throw new Error('does not say which fields cannot be turned off');});
check('unticking a standard field is recorded on the draft',()=>{
  sandbox.setBuiltin('radar','ssh',false);
  if(sandbox.TYPEDRAFT.radar.builtin.ssh!==false)throw new Error('not recorded');
  sandbox.setBuiltin('radar','ssh',true);});
check('a field can be added and removed in the draft',()=>{
  const before=sandbox.TYPEDRAFT.radar.fields.length;
  sandbox.addField('radar');
  if(sandbox.TYPEDRAFT.radar.fields.length!==before+1)throw new Error('add failed');
  sandbox.rmField('radar',before);
  if(sandbox.TYPEDRAFT.radar.fields.length!==before)throw new Error('remove failed');});
check('editing is a draft — cancel throws it away',()=>{
  sandbox.TYPEDRAFT.radar.label='WRECKED';
  sandbox.cancelTypes();
  if(sandbox.INVTYPES.radar.label==='WRECKED')
    throw new Error('the draft was applied to the live catalogue before saving');});
check('removing a type warns when something uses it',()=>{
  sandbox.startTypes();
  sandbox.INV={systems:[{name:'t',components:[{name:'r1',type:'radar',domain:'radar',role:'sensor'}]}]};
  sandbox.__confirm=false;
  sandbox.rmType('radar');
  if(!sandbox.TYPEDRAFT.radar)throw new Error('removed a type despite the operator declining');
  sandbox.__confirm=true; sandbox.rmType('radar');
  if(sandbox.TYPEDRAFT.radar)throw new Error('did not remove it when confirmed');
  sandbox.cancelTypes(); sandbox.INV={systems:[]};});
check('a save error is shown rather than swallowed',()=>{
  sandbox.startTypes();
  sandbox.TYPEDRAFT.__err="type 'radar': 'password' is a credential name";
  sandbox.renderTypes();
  if(!/credential name/.test(el('#typeed').innerHTML))
    throw new Error('the server refusal is not shown next to the form');
  sandbox.cancelTypes();});

// --- the execution graph, now at the foot of Agents & permissions ---
sandbox.GRAPHDATA={g:{agents:[
    {name:'triage',in_order:true,tools:['a','b'],needs_context:[],scope_context:[]},
    {name:'network',in_order:true,tools:['c'],needs_context:['probe_targets'],scope_context:[]},
    {name:'radar_deep_dive',in_order:false,tools:['d'],needs_context:[],scope_context:['radar_targets']}],
  post_agents:[{name:'customer_communicator'}],supervisor_picks:true,
  order:['triage','network'],max_steps:10},
  eff:{requires:{network:['triage']}}};
check('the diagram paints into the permissions page',()=>{
  el('#graphpane').innerHTML='';
  sandbox.paintGraph();
  const h=el('#graphpane').innerHTML;
  if(!/<svg/.test(h))throw new Error('no diagram rendered');
  if(!/triage/.test(h)||!/network/.test(h))throw new Error('agents missing from the diagram');
  if(!/Ceiling of 10 agent steps/.test(h))throw new Error('the legend is gone');});
check('an agent gated on context explains itself in the legend',()=>{
  const h=el('#graphpane').innerHTML;
  if(!/probe_targets/.test(h))throw new Error('needs_context not explained');
  if(!/radar_targets/.test(h))throw new Error('scope_context not explained');
  if(!/not applicable/.test(h))
    throw new Error('an inapplicable expert reads as a check we failed to run');});
check('painting without data is a no-op, not a crash',()=>{
  const keep=sandbox.GRAPHDATA;
  sandbox.GRAPHDATA=null; el('#graphpane').innerHTML='kept';
  sandbox.paintGraph();
  if(el('#graphpane').innerHTML!=='kept')throw new Error('wiped the pane with nothing to draw');
  sandbox.GRAPHDATA=keep;});

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
  // The CATALOGUE decides now, not a hardcoded map. Only `computers` declares
  // role=compute, so a laptop lands in Other until somebody defines a Laptop type —
  // which is the whole point of the type editor.
  const want={radar:['r1'],camera:['c1'],acoustic:['a1'],computers:['e1'],
              other:['l1','s1','x1']};
  if(JSON.stringify(got)!==JSON.stringify(want))
    throw new Error('bucketing: '+JSON.stringify(got));});
check('defining a type gives it its own section',()=>{
  sandbox.INVTYPES={...sandbox.INVTYPES,
    laptop:{label:'Laptops',role:'laptop',domain:'',scheme:'tcp',fields:[]}};
  sandbox.rebuildSections();
  const g=sandbox.bucket([{name:'l1',role:'laptop'}]);
  if(!(g.laptop||[]).length)throw new Error('a new type did not claim its components');
  if(!sandbox.SECTIONS.some(s=>s.key==='laptop'))throw new Error('no section for it');
  delete sandbox.INVTYPES.laptop; sandbox.rebuildSections();});
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
check('the Run tab shows THIS session and nothing else',()=>{
  sandbox.LIFETIME={lifetime:{input:1234567,output:96000,cache_read:0},sessions:47};
  sandbox.paintUsage({input:42118,output:3204,cache_read:38900});
  const h=el('#usage').innerHTML;
  if(!/this session/.test(h))throw new Error('no session total');
  if(!/42K/.test(h))throw new Error('not abbreviated: '+h);
  // Install-level bookkeeping does not belong in front of somebody diagnosing.
  if(/lifetime|Recorded usage|47 session/.test(h))
    throw new Error('install totals leaked back onto the Run tab');});
check('with no session yet the Run tab shows nothing',()=>{
  sandbox.paintUsage(null);
  if(el('#usage').innerHTML)throw new Error('showed a total for a session that has not run');});
check('install totals live in the credentials panel',()=>{
  sandbox.paintLifetime();
  const h=el('#lifetime').innerHTML;
  if(!/1\.2M/.test(h)||!/96K/.test(h))throw new Error('not abbreviated: '+h);
  if(!/47 sessions/.test(h))throw new Error('session count missing');
  if(!/not an account balance/.test(h))
    throw new Error('presented recorded usage as if it were a bill');});
check('one session is not called "1 sessions"',()=>{
  sandbox.LIFETIME={lifetime:{input:10,output:2,cache_read:0},sessions:1};
  sandbox.paintLifetime();
  if(!/1 session[^s]/.test(el('#lifetime').innerHTML))throw new Error('said "1 sessions"');});
check('usage survives missing numbers',()=>{
  sandbox.LIFETIME=null; sandbox.paintUsage({}); sandbox.paintLifetime();
  if(/NaN|undefined/.test(el('#usage').innerHTML+el('#lifetime').innerHTML))
    throw new Error('printed NaN/undefined');
  if(el('#lifetime').innerHTML)throw new Error('showed a total it does not have');});
check('no credentials points at the key field further down the page',()=>{
  sandbox.renderCreds({ready:false,source:null,warning:'',stored:false,masked:null,
                       localhost_only:'x'});
  const h=el('#credhint');
  if(h.hidden!==false)throw new Error('the hint is hidden while Run is disabled');
  if(!/Set a key/.test(h.innerHTML))throw new Error('no pointer to the key field');});
check('the credentials panel starts closed and toggles from the pill',()=>{
  const box=el('#creds'), pill=el('#keystate');
  box.hidden=true;
  sandbox.toggleCreds();
  if(box.hidden)throw new Error('the pill did not open the panel');
  if(pill._expanded!=='true')throw new Error('aria-expanded not set for screen readers');
  sandbox.toggleCreds();
  if(!box.hidden)throw new Error('a second click did not close it');
  if(pill._expanded!=='false')throw new Error('aria-expanded left stale');});
check('the hint beside a disabled Run opens the panel rather than scrolling',()=>{
  el('#creds').hidden=true;
  sandbox.jumpToCreds();
  if(el('#creds').hidden)throw new Error('the Set-a-key link did not open the panel');
  el('#creds').hidden=true;});
check('the hint disappears once a key resolves',()=>{
  sandbox.renderCreds({ready:true,source:'ANTHROPIC_API_KEY',warning:'',stored:false,
                       masked:null,localhost_only:'x'});
  if(el('#credhint').hidden!==true)throw new Error('still nagging with a working key');});

// --- conversation thread ---
const RUN_TURN={kind:'run',question:'no tracks',why:'first message',duration_s:38.2,
  usage:{input:41000,output:1100,cache_read:38900},agents:['triage','topology'],
  report:{bottom_line:'Two launcher sessions are running the same config.',
    root_cause:'Two launcher sessions',confidence:'high',escalate:false,
    evidence:['uptime_s 30 and 26690'],unknowns:['camera not checked'],
    suggested_actions:['stop the older session']},
  findings:[{agent:'triage',tool:'get_system_health',ok:true},
            {agent:'network',tool:'probe_endpoint',ok:false}],
  transcript:[{agent:'triage',text:'Two PID groups.'}]};
const FU_TURN={kind:'follow_up',question:'which one?',why:'asks about a conclusion',
  answer:'The older one, PID 291846.',duration_s:2.1,usage:{input:3000,output:120}};

check('a run turn leads with the bottom line',()=>{
  sandbox.META={order:['triage','topology'],credentials:{ready:true}};
  sandbox.renderThread({turns:[RUN_TURN],totals:{turns:1,runs:2,input:41000,output:1100,duration_s:38.2}});
  const h=el('#thread').innerHTML;
  if(!/Two launcher sessions are running the same config/.test(h))
    throw new Error('the bottom line is not shown');
  if(!/38\.2s/.test(h))throw new Error('no duration on the turn');
  if(!/2 agents/.test(h))throw new Error('no agent count');
  sandbox.renderThread({turns:[{...RUN_TURN,agents:['triage']}],totals:{turns:1,runs:1}});
  if(!/1 agent[^s]/.test(el('#thread').innerHTML))throw new Error('said "1 agents"');
  sandbox.renderThread({turns:[RUN_TURN],totals:{turns:1,runs:2,input:41000,output:1100,duration_s:38.2}});
  if(!/42K tok/.test(h))throw new Error('tokens not summarised: '+h.slice(0,400));});
check('the detail is collapsed, not dropped',()=>{
  const h=el('#thread').innerHTML;
  for(const s of ['Evidence','What could not be checked','Next steps','Tools called','Agent detail'])
    if(!h.includes(s))throw new Error('missing section: '+s);
  if(/<details class="more" open/.test(h))throw new Error('sections start open');
  if(!/uptime_s 30 and 26690/.test(h))throw new Error('evidence not rendered inside');});
check('an opened section stays open across a re-render',()=>{
  sandbox.TURN_OPEN.add('0:ev');
  sandbox.renderThread({turns:[RUN_TURN],totals:{turns:1,runs:1}});
  if(!/<details class="more" open[\s\S]*?Evidence/.test(el('#thread').innerHTML))
    throw new Error('a section the operator opened snapped shut on the next poll');
  sandbox.TURN_OPEN.delete('0:ev');});
check('an empty section is not offered',()=>{
  const bare={...RUN_TURN,report:{...RUN_TURN.report,unknowns:[]},findings:[],transcript:[]};
  sandbox.renderThread({turns:[bare],totals:{turns:1,runs:1}});
  const h=el('#thread').innerHTML;
  if(/What could not be checked/.test(h))throw new Error('offered an empty section');
  if(/Tools called/.test(h))throw new Error('offered a tool list with no tools');});
check('a follow-up turn renders its answer and is labelled',()=>{
  sandbox.renderThread({turns:[FU_TURN],totals:{turns:1,runs:0,follow_ups:1}});
  const h=el('#thread').innerHTML;
  if(!/The older one, PID 291846\./.test(h))throw new Error('answer missing');
  if(!/kind follow_up/.test(h))throw new Error('not labelled as a follow-up');
  if(/<details/.test(h))throw new Error('a follow-up was given a run\'s sections');});
check('the newest turn is rendered first',()=>{
  // The input is pinned above the thread, so chronological order would push each new
  // answer further from the box.
  sandbox.renderThread({turns:[RUN_TURN,FU_TURN],totals:{turns:2,runs:1}});
  const h=el('#thread').innerHTML;
  if(h.indexOf('PID 291846')>h.indexOf('Two launcher sessions'))
    throw new Error('oldest turn came first');});
check('a turn in flight sits above the finished ones',()=>{
  sandbox.renderThread({turns:[RUN_TURN],status:'working:follow_up',
    current:{question:'and the camera?'},live:{},totals:{turns:1,runs:1}});
  const h=el('#thread').innerHTML;
  if(h.indexOf('and the camera?')>h.indexOf('Two launcher sessions'))
    throw new Error('the live turn rendered below the history');});
check('a failed turn says so rather than vanishing',()=>{
  sandbox.renderThread({turns:[{kind:'error',question:'x',error:'model unreachable'}],totals:{turns:1,runs:0}});
  if(!/model unreachable/.test(el('#thread').innerHTML))throw new Error('error turn dropped');});
check('work in progress shows which agents have fired',()=>{
  sandbox.renderThread({turns:[],status:'working:run',current:{question:'no tracks'},
    live:{visited:['triage'],next:['topology']},totals:{}});
  const h=el('#thread').innerHTML;
  if(!/gathering evidence/.test(h))throw new Error('no progress line');
  if(!/triage/.test(h)||!/topology/.test(h))throw new Error('agent steps missing');});
check('a follow-up in progress does not claim to be gathering evidence',()=>{
  sandbox.renderThread({turns:[],status:'working:follow_up',current:{question:'which?'},live:{},totals:{}});
  if(/gathering evidence/.test(el('#thread').innerHTML))
    throw new Error('said it was running tools when it was not');});
check('the conversation header totals up',()=>{
  sandbox.renderThread({turns:[RUN_TURN,FU_TURN],
    totals:{turns:2,runs:1,follow_ups:1,input:44000,output:1220,duration_s:71}});
  const m=el('#convmeta').textContent;
  if(!/2 turns/.test(m)||!/1 run/.test(m))throw new Error('header wrong: '+m);
  if(!/1m 11s/.test(m))throw new Error('duration not humanised: '+m);});
check('an empty conversation renders nothing rather than zeros',()=>{
  sandbox.renderThread({turns:[],totals:{}});
  if(el('#convmeta').textContent)throw new Error('header shown with no turns');
  if(el('#thread').innerHTML)throw new Error('thread not empty');});

// --- bidi isolation ---
// Tickets arrive in Hebrew about a system named in English. Every interpolated value
// must be isolated or the two reorder each other's punctuation in one LTR template.
const HEB='החיישן האקוסטי לא מציג מסלולים';
check('a Hebrew question is isolated and direction-detected',()=>{
  sandbox.renderThread({turns:[{...RUN_TURN,question:HEB}],totals:{turns:1,runs:1}});
  const h=el('#thread').innerHTML;
  // dir="auto" on the element itself, NOT a <bdi> wrapper: dir="auto" skips isolated
  // descendants, so wrapping would make the paragraph fall back to LTR and left-align
  // the Hebrew.
  if(!h.includes('<span dir="auto">'+HEB+'</span>'))
    throw new Error('the question is not a dir="auto" element holding its own text');});
check('a Hebrew answer and its English detail are each isolated',()=>{
  sandbox.renderThread({turns:[{...FU_TURN,answer:HEB+' PID 291846.'}],totals:{turns:1}});
  const h=el('#thread').innerHTML;
  if(!/class="say" dir="auto">[^<]/.test(h))
    throw new Error('the answer is not a dir="auto" element holding its own text');});
check('every list item carries its own direction',()=>{
  sandbox.TURN_OPEN.add('0:ev');
  // Only evidence carries items here: the other sections render their <li>s too, even
  // while collapsed, so a global count would measure the wrong thing.
  sandbox.renderThread({turns:[{...RUN_TURN,report:{...RUN_TURN.report,
    evidence:[HEB,'uptime_s 30 and 26690'],unknowns:[],suggested_actions:[]}}],totals:{turns:1}});
  const h=el('#thread').innerHTML;
  const items=h.match(/<li dir="auto">/g)||[];
  if(items.length!==2)throw new Error('expected both items isolated, got '+items.length);
  if(!h.includes('<li dir="auto">'+HEB+'</li>'))
    throw new Error('the Hebrew item did not get its own direction');
  sandbox.TURN_OPEN.delete('0:ev');});
check('markup in a ticket cannot escape the isolation',()=>{
  sandbox.renderThread({turns:[{...RUN_TURN,question:'<img src=x onerror=1>'}],totals:{turns:1}});
  const h=el('#thread').innerHTML;
  if(/<img/.test(h))throw new Error('raw markup reached the DOM');
  if(!/&lt;img/.test(h))throw new Error('the text was lost instead of escaped');});
check('a multi-line answer keeps its breaks inside the isolate',()=>{
  sandbox.renderThread({turns:[{...FU_TURN,answer:'one\ntwo'}],totals:{turns:1}});
  if(!/class="say" dir="auto">one<br>two</.test(el('#thread').innerHTML))
    throw new Error('line breaks lost, or the paragraph lost its direction');});
check('a value embedded beside other text IS wrapped in <bdi>',()=>{
  // The opposite case: here the value has neighbours it could reorder.
  sandbox.renderThread({turns:[{...RUN_TURN,report:{...RUN_TURN.report,
    needs_permission:[{what:'radar_status',why:HEB}]}}],totals:{turns:1}});
  const h=el('#thread').innerHTML;
  if(!h.includes('<bdi>'+HEB+'</bdi>'))
    throw new Error('an embedded value was left to reorder the text around it');});

// --- asking for permission ---
const PERM={...RUN_TURN,report:{...RUN_TURN.report,needs_permission:[
  {what:'radar_status',why:'would confirm whether the radar is publishing'},
  {what:'dumbo9',why:'the node is not in the inventory'}]}};
check('a blocked run asks for what it was refused',()=>{
  sandbox.renderThread({turns:[PERM],totals:{turns:1,runs:1}});
  const h=el('#thread').innerHTML;
  if(!/Blocked on permissions/.test(h))throw new Error('no ask rendered');
  if(!/radar_status/.test(h)||!/dumbo9/.test(h))throw new Error('not every item listed');
  if(!/would confirm whether the radar is publishing/.test(h))throw new Error('reason dropped');});
check('the ask is visible, not hidden behind a disclosure',()=>{
  // A request for permission nobody expands is a request nobody answers.
  const h=el('#thread').innerHTML;
  const ask=h.indexOf('Blocked on permissions');
  const firstDetails=h.indexOf('<details');
  if(firstDetails!==-1&&ask>firstDetails)throw new Error('the ask is below the collapsibles');
  if(/<details[^>]*>[^]*Blocked on permissions/.test(h.slice(firstDetails)))
    throw new Error('the ask is inside a collapsed section');});
check('the ask says where to grant it',()=>{
  const h=el('#thread').innerHTML;
  if(!/transport.ALLOWED/.test(h)||!/Inventory/.test(h))
    throw new Error('asks for permission without saying where to change it');});
check('a clean run shows no ask at all',()=>{
  sandbox.renderThread({turns:[RUN_TURN],totals:{turns:1,runs:1}});
  if(/Blocked on permissions/.test(el('#thread').innerHTML))
    throw new Error('nagged about permissions on a run that was never blocked');});
check('a malformed permission entry is skipped rather than rendered blank',()=>{
  sandbox.renderThread({turns:[{...RUN_TURN,report:{...RUN_TURN.report,
    needs_permission:[{why:'no what'},null,{what:'radar_status',why:'ok'}]}}],totals:{turns:1}});
  const h=el('#thread').innerHTML;
  const ask=h.slice(h.indexOf('askperm'),h.indexOf('</div>',h.indexOf('askperm')));
  if((ask.match(/<li dir="auto">/g)||[]).length!==1)
    throw new Error('rendered an entry with nothing to ask for');});

// --- the open tab survives a refresh ---
check('opening a tab records it in the URL',()=>{
  sandbox.location.hash='';
  sandbox.selectTab('traces');
  if(sandbox.location.hash!=='traces'&&sandbox.location.hash!=='#traces')
    throw new Error('hash is '+JSON.stringify(sandbox.location.hash));});
check('a reload reopens the tab named in the URL',()=>{
  sandbox.location.hash='#kb';
  if(sandbox.tabFromHash()!=='kb')throw new Error('got '+sandbox.tabFromHash());
  sandbox.selectTab(sandbox.tabFromHash());
  if(el('#kb').hidden)throw new Error('the section was not reopened');
  if(!el('#run').hidden)throw new Error('Run was left open alongside it');});
check('no hash lands on the tab the mode offers',()=>{
  sandbox.location.hash='';
  sandbox.MODE='token'; if(sandbox.tabFromHash()!=='run')throw new Error('token default wrong');
  sandbox.MODE='chat'; if(sandbox.tabFromHash()!=='chat')throw new Error('chat default wrong');});
check('a hash naming nothing real is ignored',()=>{
  sandbox.location.hash='#../etc/passwd';
  sandbox.MODE='token';
  if(sandbox.tabFromHash()!=='run')throw new Error('followed a bogus hash');
  sandbox.location.hash='#';
  if(sandbox.tabFromHash()!=='run')throw new Error('an empty hash broke the default');});
check('a stale hash for a tab this mode hides is not followed',()=>{
  // #run bookmarked in token mode, reopened in chat mode: following it would strand
  // the operator on a section whose tab is not even in the bar.
  sandbox.MODE='chat'; sandbox.renderMode();
  sandbox.location.hash='#run';
  if(sandbox.tabFromHash()!=='chat')
    throw new Error('followed a hash to a hidden tab: '+sandbox.tabFromHash());
  sandbox.MODE='token'; sandbox.renderMode(); sandbox.location.hash='';});
check('a hash for a tab both modes share is honoured',()=>{
  sandbox.MODE='chat'; sandbox.renderMode();
  sandbox.location.hash='#inv';
  if(sandbox.tabFromHash()!=='inv')throw new Error('refused a tab that is always offered');
  sandbox.MODE='token'; sandbox.renderMode(); sandbox.location.hash='';});

// --- model menu ---
check('the pill names the model in force',()=>{
  sandbox.MODELS={models:[{id:'claude-opus-5',display_name:'Claude Opus 5',max_input_tokens:1000000},
                          {id:'claude-sonnet-5',display_name:'Claude Sonnet 5'}],
                  source:'api',current:'claude-opus-5',default:'claude-opus-5'};
  sandbox.renderModels();
  if(el('#model').textContent!=='claude-opus-5')
    throw new Error('pill reads '+el('#model').textContent);});
check('every available model is offered, with the current one marked',()=>{
  const h=el('#modelmenu').innerHTML;
  if(!/Claude Opus 5/.test(h)||!/Claude Sonnet 5/.test(h))throw new Error('a model is missing');
  const on=h.match(/class="modeopt on"/g)||[];
  if(on.length!==1)throw new Error('expected one marked, got '+on.length);
  if(!/class="modeopt on"[^]*?Claude Opus 5/.test(h))throw new Error('marked the wrong one');
  if(!/1\.0M context/.test(h))throw new Error('context window not shown where known');});
check('switching is flagged as a cache reset, not a free action',()=>{
  if(!/invalidates the prompt cache/.test(el('#modelmenu').innerHTML))
    throw new Error('no warning that the next run pays full price');});
check('a fallback list says so rather than looking authoritative',()=>{
  sandbox.MODELS={models:[{id:'claude-opus-5',display_name:'Claude Opus 5'}],
    source:'fallback',why:'no credentials',current:'claude-opus-5',default:'claude-opus-5'};
  sandbox.renderModels();
  const h=el('#modelmenu').innerHTML;
  if(!/built-in list/.test(h))throw new Error('a fallback list is shown as if complete');
  if(!/no credentials/.test(h))throw new Error('did not say why');
  if(/invalidates the prompt cache/.test(h))
    throw new Error('buried the more important warning under the routine one');});
check('a non-default model offers a way back',()=>{
  sandbox.MODELS={models:[{id:'claude-haiku-4-5',display_name:'Haiku'}],source:'api',
    current:'claude-haiku-4-5',default:'claude-opus-5'};
  sandbox.renderModels();
  if(!/clearModel\(\)/.test(el('#modelmenu').innerHTML))
    throw new Error('no reset once the model has been changed');
  if(!/claude-opus-5/.test(el('#modelmenu').innerHTML))
    throw new Error('reset does not say what it reverts to');});
check('the default model offers no pointless reset',()=>{
  sandbox.MODELS={models:[{id:'claude-opus-5',display_name:'Opus'}],source:'api',
    current:'claude-opus-5',default:'claude-opus-5'};
  sandbox.renderModels();
  if(/clearModel\(\)/.test(el('#modelmenu').innerHTML))
    throw new Error('offered a reset to the value already in force');});
check('the model menu toggles from its pill',()=>{
  el('#modelmenu').hidden=true;
  sandbox.toggleModel();
  if(el('#modelmenu').hidden)throw new Error('did not open');
  if(el('#model')._expanded!=='true')throw new Error('aria-expanded not set');
  sandbox.toggleModel();
  if(!el('#modelmenu').hidden)throw new Error('did not close');});
check('an empty catalogue still renders',()=>{
  sandbox.MODELS={models:[],source:'api',current:'claude-opus-5',default:'claude-opus-5'};
  sandbox.renderModels();
  if(/undefined|NaN/.test(el('#modelmenu').innerHTML))throw new Error('printed undefined');});

// --- answering mode ---
check('the pill shows the answering mode, not AGENT_MODE',()=>{
  sandbox.MODE='token'; sandbox.renderMode();
  if(el('#mode').textContent!=='token mode')throw new Error('pill reads '+el('#mode').textContent);
  sandbox.MODE='chat'; sandbox.renderMode();
  if(el('#mode').textContent!=='chat mode')throw new Error('pill did not follow the mode');});
check('each option states what it can reach',()=>{
  // The word "mock" left the pill; the fact it carried must not leave with it.
  sandbox.MODES.token.blurb='The graph pipeline answers. Spends API tokens. Tools read '
    +'recorded fixtures (AGENT_MODE=mock) — never the real bench.';
  sandbox.renderMode();
  const h=el('#modemenu').innerHTML;
  if(!/recorded fixtures/.test(h))throw new Error('token option does not say it uses fixtures');
  if(!/REAL hardware/.test(h))throw new Error('chat option does not say it touches the bench');
  if(!/Token mode/.test(h)||!/Chat mode/.test(h))throw new Error('an option is missing');});
const tabOf=n=>sandbox.__tabs.find(t=>t.dataset.t===n);
check('only the tab belonging to the mode is offered',()=>{
  sandbox.MODE='token'; sandbox.renderMode();
  if(tabOf('run').hidden)throw new Error('Run hidden in token mode');
  if(!tabOf('chat').hidden)throw new Error('Live-with-chat still offered in token mode');
  sandbox.MODE='chat'; sandbox.renderMode();
  if(tabOf('chat').hidden)throw new Error('Live-with-chat hidden in chat mode');
  if(!tabOf('run').hidden)throw new Error('Run still offered in chat mode');});
check('the other tabs are never touched',()=>{
  // Only Run and Live-with-chat are mode-specific; hiding Traces or Approvals with
  // them would strand the queue and the history.
  for(const n of ['approvals','traces','kb','graph','perms','inv'])
    if(tabOf(n).hidden)throw new Error(n+' was hidden by a mode switch');});
check('switching away from the tab you are on moves you, not blanks the page',()=>{
  sandbox.MODE='token'; sandbox.renderMode();
  tabs.forEach(t=>t.setAttribute('aria-selected',t.dataset.t==='run'?'true':'false'));
  sandbox.MODE='chat'; sandbox.renderMode();
  if(tabOf('chat').getAttribute('aria-selected')!=='true')
    throw new Error('hid the open tab without selecting its counterpart');
  if(el('#chat').hidden)throw new Error('the chat section was left hidden');});
check('a mode switch leaves an unrelated tab alone',()=>{
  sandbox.MODE='chat'; sandbox.renderMode();
  tabs.forEach(t=>t.setAttribute('aria-selected',t.dataset.t==='traces'?'true':'false'));
  sandbox.MODE='token'; sandbox.renderMode();
  if(tabOf('traces').getAttribute('aria-selected')!=='true')
    throw new Error('yanked the operator off Traces for an unrelated mode change');});
check('the reassurance under the box follows the mode',()=>{
  // "no device touched" is FALSE in chat mode, and a stale reassurance is worse than
  // none at all.
  sandbox.MODE='token'; sandbox.renderMode();
  if(!/no device touched/.test(el('#modenote').innerHTML))
    throw new Error('token mode lost its fixtures note');
  sandbox.MODE='chat'; sandbox.renderMode();
  const h=el('#modenote').innerHTML;
  if(/no device touched/.test(h))
    throw new Error('chat mode still claims nothing is touched');
  if(!/real hardware/.test(h))throw new Error('chat mode does not warn about the bench');});
check('the selected option is marked',()=>{
  sandbox.MODE='chat'; sandbox.renderMode();
  const h=el('#modemenu').innerHTML;
  const on=h.match(/class="modeopt on"/g)||[];
  if(on.length!==1)throw new Error('expected exactly one selected, got '+on.length);
  if(!/class="modeopt on"[^]*?Chat mode/.test(h))throw new Error('marked the wrong one');});
check('the menu toggles from the pill',()=>{
  el('#modemenu').hidden=true;
  sandbox.toggleMode();
  if(el('#modemenu').hidden)throw new Error('did not open');
  if(el('#mode')._expanded!=='true')throw new Error('aria-expanded not set');
  sandbox.toggleMode();
  if(!el('#modemenu').hidden)throw new Error('did not close');});
check('switching mode swaps the thread, it does not merge them',()=>{
  sandbox.LAST_CONV={turns:[RUN_TURN],totals:{turns:1,runs:1}};
  sandbox.CHAT_IDS=['abc']; sandbox.CHAT_CACHE={abc:{kind:'chat',question:'radar down',
    id:'abc',status:'pending',pending:true}};
  sandbox.MODE='chat'; sandbox.renderThread(sandbox.threadFor());
  let h=el('#thread').innerHTML;
  if(!/radar down/.test(h))throw new Error('chat ticket missing');
  if(/Two launcher sessions/.test(h))throw new Error('a token turn leaked into chat mode');
  sandbox.MODE='token'; sandbox.renderThread(sandbox.threadFor());
  h=el('#thread').innerHTML;
  if(!/Two launcher sessions/.test(h))throw new Error('token thread lost');
  if(/radar down/.test(h))throw new Error('a chat ticket leaked into token mode');});
check('an unanswered ticket says who drains the queue',()=>{
  sandbox.MODE='chat'; sandbox.renderThread(sandbox.threadFor());
  const h=el('#thread').innerHTML;
  if(!/type <code>go<\/code>/.test(h))
    throw new Error('a queued ticket looks like a failure rather than one waiting');
  if(/<details/.test(h))throw new Error('offered sections for a ticket with no report');});
check('an answered ticket renders its report and bench commands',()=>{
  sandbox.CHAT_CACHE.abc={kind:'chat',question:'radar down',id:'abc',status:'done',
    pending:false,report:{bottom_line:'The radar service was stopped.',root_cause:'rc',
      confidence:'high',evidence:['systemctl reports inactive'],unknowns:[],
      suggested_actions:['start it'],escalate:false},
    commands:[{cmd:'systemctl status radar',readonly:true,output:'inactive'}],
    kb_gaps:['no case covers a stopped radar service']};
  sandbox.renderThread(sandbox.threadFor());
  const h=el('#thread').innerHTML;
  if(!/The radar service was stopped\./.test(h))throw new Error('bottom line missing');
  if(!/Commands run on the bench/.test(h))throw new Error('bench commands not offered');
  if(!/Documentation gaps/.test(h))throw new Error('kb gaps dropped');
  sandbox.MODE='token'; sandbox.CHAT_IDS=[]; sandbox.CHAT_CACHE={};});

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

// --- per-system sections ---
check('a system only shows the sections it uses',()=>{
  // renderInv reads domains and the type catalogue too, so the fixture is a whole
  // /api/inventory payload rather than just the systems.
  sandbox.INV={systems:[{name:'t',site:'s',components:[
      {name:'r1',type:'radar',domain:'radar',role:'sensor',ssh:{},fields:{}}]}],
    domains:['radar','acoustic','camera'],
    roles:['sensor','compute','laptop','network','power','other'],type_catalog:{}};
  sandbox.renderInv(); sandbox.editSys('t');
  const h=el('#sysinline').innerHTML;
  if(!/Radar/.test(h))throw new Error('the section in use is missing');
  if(/secg[^"]*"[^]*?Acoustic/.test(h.split('Add a section:')[0]||''))
    throw new Error('an unused section is still shown as a heading');
  if(!/Add a section:/.test(h))throw new Error('no way to add one back');
  if(!/addSection\('acoustic'\)/.test(h))throw new Error('acoustic is not offered');});
check('an empty section can be dropped, a populated one cannot',()=>{
  sandbox.addSection('acoustic');
  if(!sandbox.EDIT.sections.has('acoustic'))throw new Error('add failed');
  sandbox.dropSection('acoustic');
  if(sandbox.EDIT.sections.has('acoustic'))throw new Error('empty section would not drop');
  sandbox.dropSection('radar');
  if(!sandbox.EDIT.sections.has('radar'))
    throw new Error('dropped a section with hardware in it');});
check('a new type gives the system a section to add into',()=>{
  const h=el('#sysinline').innerHTML;
  if(!/addSection\('computers'\)/.test(h))throw new Error('computers not offered');});
check('removing a component asks first',()=>{
  sandbox.__confirm=false;
  const n=sandbox.EDIT.components.length;
  sandbox.rmComp(0);
  if(sandbox.EDIT.components.length!==n)throw new Error('removed despite a declined confirm');
  sandbox.__confirm=true; sandbox.rmComp(0);
  if(sandbox.EDIT.components.length!==n-1)throw new Error('did not remove when confirmed');});
check('adding a component fills in its type defaults',()=>{
  sandbox.EDIT={name:'t',orig:'t',components:[],isNew:true,sections:new Set()};
  sandbox.addComp('radar');
  const c=sandbox.EDIT.components[0];
  if(c.domain!=='radar'||c.scheme!=='tcp')throw new Error('section defaults not applied');
  if((c.fields||{}).elevation!=='0')
    throw new Error('a field default was not prefilled: '+JSON.stringify(c.fields));});
check('a component only shows the standard fields its type has',()=>{
  sandbox.INVTYPES={...sandbox.INVTYPES,
    acoustic:{...sandbox.INVTYPES.acoustic,builtin:{address:false,ssh:false,port:true,
      hardware:true,software_version:true,web:true,location:true}}};
  sandbox.EDIT={name:'t',orig:'t',components:[],isNew:true,sections:new Set()};
  sandbox.addComp('acoustic'); sandbox.renderEdit();
  const h=el('#sysnew').innerHTML;
  if(/IP address/.test(h))throw new Error('showed an address on a local Docker service');
  if(/Username/.test(h)||/Password/.test(h))throw new Error('showed shell credentials');
  if(/secrets file/.test(h))throw new Error('left the password note behind');
  if(!/Port/.test(h))throw new Error('hid a field the type still has');
  delete sandbox.INVTYPES.acoustic.builtin;});
check('an unknown type shows everything rather than nothing',()=>{
  const sec=sandbox.sectionOf({type:'from_the_future',role:'sensor'});
  sandbox.EDIT={name:'t',orig:'t',isNew:true,sections:new Set([sec]),
    components:[{name:'x',type:'from_the_future',role:'sensor',ssh:{},fields:{}}]};
  sandbox.SEC_OPEN.add(sec);            // a collapsed section renders no fields at all
  sandbox.renderEdit();
  const h=el('#sysnew').innerHTML;
  if(!/IP address/.test(h))throw new Error('an unrecognised type rendered with no fields');
  sandbox.SEC_OPEN.delete(sec);});
check('a save keeps values whose field the catalogue no longer declares',()=>{
  // Reverting the catalogue, or renaming a field, must not turn the next save of an
  // unrelated system into a silent delete of values nobody can see to rescue.
  sandbox.INV={systems:[],domains:[],roles:[],type_catalog:{}};
  sandbox.EDIT={name:'t',orig:'t',isNew:false,sections:new Set(['radar']),
    components:[{name:'r1',type:'radar',domain:'radar',role:'sensor',ssh:{},
                 fields:{azimuth_offset:'137',serial:'AR-300-7714'}}]};
  let sent=null;
  const realFetch=sandbox.fetch;
  sandbox.fetch=(p,o)=>{ sent=JSON.parse(o.body);
    return Promise.resolve({ok:true,json:async()=>({name:'t',systems:[]})}); };
  sandbox.saveSys();
  sandbox.fetch=realFetch;
  if(!sent)throw new Error('saveSys sent nothing');
  const f=sent.components[0].fields;
  if(f.azimuth_offset!=='137')throw new Error('dropped a declared value');
  if(f.serial!=='AR-300-7714')
    throw new Error('dropped a value the catalogue no longer declares: '+JSON.stringify(f));});
check('a component renders the fields its type declares',()=>{
  // Self-contained: the checks above rebuild EDIT for their own purposes.
  sandbox.EDIT={name:'t',orig:'t',components:[],isNew:true,sections:new Set()};
  sandbox.addComp('radar');
  sandbox.renderEdit();
  const h=el('#sysnew').innerHTML;
  if(!/Azimuth/.test(h)||!/Elevation/.test(h))throw new Error('type fields not rendered');
  if(!/setF\(0,'azimuth'/.test(h))throw new Error('the field does not write back');
  if(!/Latitude/.test(h))throw new Error('no per-component coordinates');});


let bad=0;
for(const c of cases){ if(c[0]!=='ok')bad++; console.log(c[0].padEnd(5),c[1],c[2]?'→ '+c[2]:''); }
console.log(bad?`\n${bad} FAILED`:`\nall ${cases.length} render paths ok`);
process.exit(bad?1:0);
