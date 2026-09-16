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
  alert:()=>{},confirm:()=>true,
  fetch:()=>Promise.resolve({ok:true,json:async()=>({})}),
  document:{querySelector:sel=>el(sel),querySelectorAll:()=>[],getElementById:id=>el('#'+id)}};
sandbox.window=sandbox;
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
check('editSys → dense editor',()=>{sandbox.editSys('tower1');
  if(!sandbox.EDIT)throw new Error('edit mode not entered');
  if(sandbox.EDIT.components.length!==5)throw new Error('components not copied')});
check('every role renders',()=>{sandbox.renderEdit()});
check('setRole refreshes the type list',()=>{sandbox.setRole(3,'network');
  if(sandbox.EDIT.components[3].role!=='network')throw new Error('role not set')});
check('addComp/rmComp',()=>{sandbox.addComp();
  if(sandbox.EDIT.components.length!==6)throw new Error('add failed');
  sandbox.rmComp(5);
  if(sandbox.EDIT.components.length!==5)throw new Error('remove failed')});
check('component with no ssh renders',()=>{sandbox.EDIT.components[3].ssh={};sandbox.renderEdit()});
check('clearPw flags deletion',()=>{sandbox.clearPw(0);
  if(!sandbox.EDIT.components[0].ssh.clear_password)throw new Error('flag not set')});
check('cancel returns to the view',()=>{sandbox.viewSys('tower1');
  if(sandbox.EDIT)throw new Error('editor still open')});
check('legacy sensors key still renders',()=>{
  const sy=sandbox.INV.systems[0]; const keep=sy.components;
  delete sy.components; sy.sensors=keep; sandbox.renderInv(); sandbox.viewSys('tower1');
  sy.components=keep;});
check('empty inventory renders',()=>{const s2=sandbox.INV.systems;sandbox.INV.systems=[];
  sandbox.VIEW=null;sandbox.EDIT=null;sandbox.renderInv();sandbox.INV.systems=s2});
check('newSys opens a blank editor',()=>{sandbox.newSys()});
check('closeView clears both modes',()=>{sandbox.closeView();
  if(sandbox.VIEW||sandbox.EDIT)throw new Error('not cleared')});

let bad=0;
for(const c of cases){ if(c[0]!=='ok')bad++; console.log(c[0].padEnd(5),c[1],c[2]?'→ '+c[2]:''); }
console.log(bad?`\n${bad} FAILED`:`\nall ${cases.length} render paths ok`);
process.exit(bad?1:0);
