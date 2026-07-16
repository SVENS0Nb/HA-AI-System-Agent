'use strict';
const $ = id => document.getElementById(id);
const api = path => `${window.location.pathname.replace(/\/?$/, '/')}${path}`;
const fields = [
  'openai_api_key','openai_model','reasoning_effort','openai_timeout_seconds',
  'max_output_tokens','max_tool_rounds','max_parallel_agent_runs','signal_api_url',
  'signal_api_token','signal_account','timezone','conversation_messages',
  'message_retention_days','max_messages_per_sender','max_monitors_per_sender',
  'reconcile_interval_seconds','default_log_lines','max_config_file_kb',
  'startup_message','allow_sensitive_config','clear_openai_api_key',
  'clear_signal_api_token'
];
function toast(message,error=false) {
  const el=$('toast'); el.textContent=message; el.className=`show${error?' error':''}`;
  clearTimeout(window.toastTimer); window.toastTimer=setTimeout(()=>{el.className='';},4500);
}
async function request(path,options={}) {
  const response=await fetch(api(path),options);
  const body=await response.json().catch(()=>({error:response.statusText}));
  if(!response.ok||body.ok===false) throw new Error(body.error||response.statusText);
  return body;
}
async function loadSettings() {
  try {
    const {settings:s}=await request('api/settings');
    fields.forEach(id=>{
      if(!(id in s)||id.startsWith('clear_')||id==='openai_api_key'||id==='signal_api_token') return;
      const el=$(id); if(el.type==='checkbox') el.checked=Boolean(s[id]); else el.value=s[id];
    });
    $('allowed_senders').value=(s.allowed_senders||[]).join('\n');
    $('openaiKeyHint').textContent=s.openai_api_key_set?'Ein Key ist gespeichert. Leer lassen, um ihn beizubehalten.':'Noch kein Key gespeichert.';
    $('signalTokenHint').textContent=s.signal_api_token_set?'Ein Token ist gespeichert. Leer lassen, um ihn beizubehalten.':'Optional; aktuell ist kein Token gespeichert.';
  } catch(e) { toast(e.message,true); }
}
async function loadStatus() {
  try {
    const {status}=await request('api/status');
    $('statusDot').classList.toggle('ok',status.agent_running);
    $('statusTitle').textContent=status.agent_running?'Agentprozess aktiv':'Konfiguration oder Laufzeit prüfen';
    $('statusMessages').replaceChildren(...status.messages.map(m=>{const li=document.createElement('li');li.textContent=m;return li;}));
  } catch(e) { toast(e.message,true); }
}
function payload() {
  const result={};
  fields.forEach(id=>{const el=$(id);result[id]=el.type==='checkbox'?el.checked:(el.type==='number'?Number(el.value):el.value.trim());});
  result.allowed_senders=$('allowed_senders').value.split(/\r?\n|,/).map(x=>x.trim()).filter(Boolean);
  return result;
}
$('save').addEventListener('click',async()=>{
  const button=$('save'); button.disabled=true;
  try {
    const result=await request('api/settings',{method:'PUT',headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},body:JSON.stringify(payload())});
    toast(result.message); $('openai_api_key').value=''; $('signal_api_token').value='';
    $('clear_openai_api_key').checked=false; $('clear_signal_api_token').checked=false;
    await loadSettings(); setTimeout(loadStatus,700);
  } catch(e) { toast(e.message,true); } finally { button.disabled=false; }
});
$('reset').addEventListener('click',async()=>{
  const button=$('reset'); button.disabled=true;
  try {
    const result=await request('api/settings',{method:'DELETE',headers:{'X-Requested-With':'XMLHttpRequest'}});
    toast(result.message); await loadSettings(); setTimeout(loadStatus,700);
  } catch(e) { toast(e.message,true); } finally { button.disabled=false; }
});
document.querySelectorAll('.test').forEach(button=>button.addEventListener('click',async()=>{
  button.disabled=true;
  try { const result=await request(`api/test/${button.dataset.target}`,{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'}}); toast(result.message); }
  catch(e) { toast(e.message,true); } finally { button.disabled=false; }
}));
loadSettings(); loadStatus(); setInterval(loadStatus,5000);
