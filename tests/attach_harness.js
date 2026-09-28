// The Diagnose box's attachments under a DOM stub: upload on add, Send held while a
// description is being made, ids sent with the message, and the sent turn showing them.
const fs=require('fs'), vm=require('vm'), path=require('path');
const mk=()=>({innerHTML:'',textContent:'',value:'',hidden:false,disabled:false,dataset:{},style:{},
  classList:{add(){},remove(){},contains(){return false}},
  setAttribute(k,v){this['_'+k]=v},getAttribute(k){return this['_'+k]},
  scrollIntoView(){},focus(){},click(){this.clicked=true},contains(){return false},
  insertAdjacentHTML(_,h){this.innerHTML=h+this.innerHTML},querySelectorAll:()=>[]});
const els={}; const el=sel=>els[sel]||(els[sel]=mk());
const sent=[]; let release=null;
const REC={id:'a1b2c3d4e5f6',name:'shot.png',kind:'image',media_type:'image/png',bytes:9,
  text:"Error banner: 'ASU backend unreachable'",truncated:false,usage:{input:1,output:1,cache_read:0}};
const sandbox={console,setTimeout,clearInterval,setInterval,Promise,JSON,Math,Number,Object,Date,String,
  alert:()=>{},confirm:()=>true,
  FileReader:class{readAsDataURL(f){this.result='data:x;base64,'+f.b64;setTimeout(()=>this.onload(),0)}},
  fetch:(p,o={})=>{
    if(/\/attachments$/.test(p)){ sent.push(['upload',JSON.parse(o.body)]);
      return new Promise(res=>{release=()=>res({ok:true,json:async()=>REC})}); }
    if(o.method==='POST') sent.push([p,o.body?JSON.parse(o.body):null]);
    return Promise.resolve({ok:true,json:async()=>
      /\/conversation$/.test(p)?{conversation_id:'c1'}:{ok:true,sessions:[],pending:0,working:0,turns:[]}});},
  document:{querySelector:sel=>el(sel),querySelectorAll:()=>[],getElementById:id=>el('#'+id),
            addEventListener(){}}};
sandbox.window=sandbox;
vm.createContext(sandbox);
const html=fs.readFileSync(path.join(__dirname,'..','console','index.html'),'utf8');
vm.runInContext(html.split('</div><script>')[1].split('</script>')[0],sandbox);
vm.runInContext("MODE='live'; CONV=null; poll=()=>{};",sandbox);

const cases=[];
async function check(name,f){try{await f();cases.push(['ok',name])}
  catch(e){cases.push(['FAIL',name,e.message])}}
const want=(c,m)=>{if(!c)throw new Error(m)};
const tick=()=>new Promise(r=>setTimeout(r,5));

(async()=>{
await check('the button opens the file picker',async()=>{
  el('#attach').onclick(); want(el('#attfile').clicked,'picker not opened');});
await check('a pasted screenshot is uploaded under a readable name, Send held meanwhile',async()=>{
  let prevented=false;
  el('#q').onpaste({preventDefault(){prevented=true},
    clipboardData:{files:[{name:'image.png',type:'image/png',b64:'AAAA'}]}});
  await tick();
  want(prevented,'paste not taken over');
  const up=sent.find(s=>s[0]==='upload');
  want(up&&/^screenshot-\d{6}\.png$/.test(up[1].name),'name '+(up&&up[1].name));
  want(up[1].data==='AAAA'&&up[1].media_type==='image/png','payload '+JSON.stringify(up&&up[1]));
  want(/describing…/.test(el('#attbar').innerHTML),'no progress shown');
  want(el('#go').disabled===true,'Send allowed while describing');
  sandbox.send(); await tick();
  want(!sent.some(s=>/\/message$/.test(s[0])),'sent before the description was ready');});
await check('once described, the text the agents will read is shown and Send is back',async()=>{
  release(); await tick();
  const h=el('#attbar').innerHTML;
  want(/what the agents will read/.test(h)&&/ASU backend unreachable/.test(h),'description hidden');
  want(el('#go').disabled===false,'Send still held');});
await check('Send carries the ids, even with nothing typed, then clears the box',async()=>{
  el('#q').value='';
  await sandbox.send(); await tick();
  const m=sent.find(s=>/\/conversation\/c1\/message$/.test(s[0]));
  want(m,'message never sent');
  want(JSON.stringify(m[1].attachments)===JSON.stringify([REC.id]),'ids '+JSON.stringify(m[1]));
  want(el('#attbar').innerHTML==='','attachments left in the box');});
await check('removing one before sending drops it',async()=>{
  sandbox.addFiles([{name:'a.log',type:'text/plain',b64:'eA=='}]); await tick(); release(); await tick();
  want(/a\.log|shot\.png/.test(el('#attbar').innerHTML),'not listed');
  const key=vm.runInContext('ATT[0].key',sandbox);
  sandbox.rmAtt(key);
  want(el('#attbar').innerHTML==='','still listed');});
await check('a failed upload says why and does not block Send',async()=>{
  const real=sandbox.fetch;
  sandbox.fetch=(p,o)=>/\/attachments$/.test(p)
    ?Promise.resolve({ok:false,status:400,json:async()=>({detail:'a.exe: not supported'})}):real(p,o);
  await sandbox.addFiles([{name:'a.exe',type:'application/x-msdownload',b64:'TVo='}]);
  sandbox.fetch=real;
  want(/not supported/.test(el('#attbar').innerHTML),'no reason shown');
  want(el('#go').disabled===false,'Send blocked by a failed upload');
  sandbox.clearAtt();});
await check('a sent turn links the original and shows the description',async()=>{
  const h=sandbox.turnCard({kind:'run',question:'no tracks',attachments:[REC],report:{},findings:[],
    transcript:[],usage:{}},0);
  want(h.includes(`/api/attachments/${REC.id}/file`),'no link to the original');
  want(/<img class="attimg"/.test(h),'no preview for an image');
  want(/ASU backend unreachable/.test(h),'no description');});

let bad=0;
for(const c of cases){if(c[0]!=='ok')bad++;console.log(c[0].padEnd(5),c[1],c[2]?'→ '+c[2]:'');}
console.log(bad?`\n${bad} FAILED`:`\nall ${cases.length} attachment checks ok`);
process.exit(bad?1:0);
})();
