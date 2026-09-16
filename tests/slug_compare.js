// Runs the console's slugify() over inputs given as JSON on argv, printing JSON results.
// Driven by tests/test_kb_api.py, which compares them against the Python _slugify.
// The page's start-up chain calls endpoints the stub answers with {}, which rejects.
// That is an artifact of the stub, not of the code under test, so it must not decide
// this script's exit status.
process.on('unhandledRejection',()=>{});
const fs=require('fs'), vm=require('vm'), path=require('path');
const html=fs.readFileSync(path.join(__dirname,'..','console','index.html'),'utf8');
const js=html.split('</div><script>')[1].split('</script>')[0];
const mk=()=>({innerHTML:'',textContent:'',value:'',classList:{add(){},remove(){}},
  style:{},dataset:{},setAttribute(){},querySelectorAll:()=>[]});
const sandbox={console,setTimeout,clearInterval,setInterval,Promise,JSON,Math,Number,Object,Date,
  alert:()=>{},confirm:()=>true,fetch:()=>Promise.resolve({ok:true,json:async()=>({})}),
  document:{querySelector:()=>mk(),querySelectorAll:()=>[],getElementById:()=>mk()}};
sandbox.window=sandbox;
vm.createContext(sandbox);
vm.runInContext(js,sandbox);
process.stdout.write(JSON.stringify(JSON.parse(process.argv[2]).map(sandbox.slugify)));
process.exit(0);
