'use strict';
const $ = id => document.getElementById(id);
const api = path => `${window.location.pathname.replace(/\/?$/, '/')}${path}`;
const fields = [
  'openai_api_key','openai_model','reasoning_mode','reasoning_effort','openai_timeout_seconds',
  'max_output_tokens','max_tool_rounds','max_parallel_agent_runs','signal_mode',
  'signal_api_url','signal_api_token','signal_account','signal_self_chat_enabled','timezone',
  'conversation_messages','message_retention_days','max_messages_per_sender',
  'max_monitors_per_sender','reconcile_interval_seconds','default_log_lines',
  'max_config_file_kb','startup_message','learning_enabled','anomaly_sensitivity',
  'memory_retention_days','max_memories_per_sender','entity_control_enabled','allow_sensitive_config',
  'clear_openai_api_key','clear_signal_api_token'
];
let linkPolling=false;
let pairingStarting=false;
let lastPairStatus='idle';

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
function setSignalMode() {
  const integrated=$('signal_mode').value==='integrated';
  $('integratedSignal').hidden=!integrated;
  $('externalSignal').hidden=integrated;
  $('signal_account').readOnly=integrated;
  $('signalAccountHint').textContent=integrated?'Im integrierten Modus automatisch erkannt.':'Muss zum Konto der externen Bridge passen.';
  if(integrated) loadSignalStatus();
}
function setReasoningMode() {
  const fixed=$('reasoning_mode').value==='fixed';
  $('fixedReasoning').hidden=!fixed;
  $('reasoning_effort').disabled=!fixed;
}
function setLearningMode() {
  const enabled=$('learning_enabled').checked;
  ['anomaly_sensitivity','memory_retention_days','max_memories_per_sender'].forEach(id=>{$(id).disabled=!enabled;});
}
function setEntityControlMode() {
  $('controllable_entities').disabled=!$('entity_control_enabled').checked;
}
async function loadTimezones(selected) {
  let zones=[];
  try { ({timezones:zones}=await request('api/timezones')); }
  catch(_) { zones=['Europe/Berlin','UTC']; }
  if(selected&&!zones.includes(selected)) zones.unshift(selected);
  const select=$('timezone');
  select.replaceChildren(...zones.map(name=>{
    const option=document.createElement('option'); option.value=name; option.textContent=name; return option;
  }));
  select.value=selected||'Europe/Berlin';
}
async function loadSettings() {
  try {
    const {settings:s}=await request('api/settings');
    await loadTimezones(s.timezone);
    fields.forEach(id=>{
      if(!(id in s)||id.startsWith('clear_')||id==='openai_api_key'||id==='signal_api_token') return;
      const el=$(id); if(el.type==='checkbox') el.checked=Boolean(s[id]); else el.value=s[id];
    });
    $('allowed_senders').value=(s.allowed_senders||[]).join('\n');
    $('controllable_entities').value=(s.controllable_entities||[]).join('\n');
    $('openaiKeyHint').textContent=s.openai_api_key_set?'Ein Key ist gespeichert. Leer lassen, um ihn beizubehalten.':'Noch kein Key gespeichert.';
    $('signalTokenHint').textContent=s.signal_api_token_set?'Ein Token ist gespeichert. Leer lassen, um ihn beizubehalten.':'Optional; aktuell ist kein Token gespeichert.';
    setReasoningMode();
    setLearningMode();
    setEntityControlMode();
    setSignalMode();
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
async function loadSignalStatus() {
  if($('signal_mode').value!=='integrated') return;
  try {
    const {status}=await request('api/signal/status');
    const accounts=status.accounts||[];
    $('signalBridgeDot').classList.toggle('ok',status.ready);
    $('signalConnect').disabled=accounts.length>0||linkPolling;
    $('signalPair').disabled=accounts.length!==1||status.pairing.status==='waiting';
    $('signalUnlink').disabled=accounts.length!==1;
    if(!status.ready) {
      $('signalBridgeTitle').textContent='Integrierte Signal-Bridge startet …';
      $('signalBridgeMessage').textContent=status.error||'Der erste Start kann etwas länger dauern.';
      return;
    }
    if(accounts.length===0) {
      $('signalBridgeTitle').textContent='Signal-Bridge bereit';
      $('signalBridgeMessage').textContent='Jetzt das Bot-Konto per QR-Code verbinden.';
    } else if(accounts.length===1) {
      $('signalBridgeTitle').textContent=`Signal-Konto verbunden: ${accounts[0]}`;
      const senderCount=(status.allowed_senders||[]).length;
      const selfChat=Boolean(status.signal_self_chat_enabled);
      $('signalBridgeMessage').textContent=selfChat&&senderCount
        ?`„Notiz an mich“ und ${senderCount} weitere Absender sind aktiv.`
        :(selfChat?'„Notiz an mich“ ist aktiv.':(senderCount?'Mindestens ein Absender ist freigegeben.':'„Notiz an mich“ aktivieren oder einen persönlichen Absender koppeln.'));
      $('signal_account').value=accounts[0];
      if(linkPolling) {
        linkPolling=false;
        $('signalQrPanel').hidden=true;
        if($('signal_self_chat_enabled').checked) {
          await request('api/settings',{method:'PUT',headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},body:JSON.stringify({signal_mode:'integrated',signal_account:accounts[0],signal_self_chat_enabled:true})});
          toast('„Notiz an mich“ wurde für das verbundene Konto aktiviert.');
          await loadSettings();
          setTimeout(loadStatus,700);
        } else if(senderCount===0) await startPairing();
      }
    } else {
      $('signalBridgeTitle').textContent='Mehrere Signal-Konten gefunden';
      $('signalBridgeMessage').textContent='Für die automatische Kopplung darf nur ein Bot-Konto hinterlegt sein.';
    }
    const pair=status.pairing||{status:'idle'};
    const pairChanged=pair.status!==lastPairStatus;
    lastPairStatus=pair.status;
    if(pair.status==='paired'&&pairChanged) {
      toast(`Signal-Absender ${pair.paired_sender} wurde gekoppelt.`);
      $('signalPairPanel').hidden=true;
      await loadSettings();
      setTimeout(loadStatus,700);
    } else if(pair.status==='expired'&&pairChanged) {
      toast('Der Signal-Kopplungscode ist abgelaufen.',true);
    } else if(pair.status==='error'&&pairChanged) {
      toast(pair.error||'Signal-Kopplung fehlgeschlagen.',true);
    }
  } catch(e) {
    $('signalBridgeDot').classList.remove('ok');
    $('signalBridgeTitle').textContent='Signal-Bridge nicht erreichbar';
    $('signalBridgeMessage').textContent=e.message;
  }
}
async function startPairing() {
  if(pairingStarting) return;
  pairingStarting=true;
  $('signalPair').disabled=true;
  try {
    const result=await request('api/signal/pair',{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'}});
    $('signalPairCode').textContent=`KOPPELN ${result.code}`;
    $('signalPairPanel').hidden=false;
    lastPairStatus='waiting';
    toast('Kopplungscode erstellt. Jetzt über Signal an den Bot senden.');
  } catch(e) { toast(e.message,true); }
  finally { pairingStarting=false; setTimeout(loadSignalStatus,500); }
}
function payload() {
  const result={};
  fields.forEach(id=>{const el=$(id);result[id]=el.type==='checkbox'?el.checked:(el.type==='number'?Number(el.value):el.value.trim());});
  result.allowed_senders=$('allowed_senders').value.split(/\r?\n|,/).map(x=>x.trim()).filter(Boolean);
  result.controllable_entities=$('controllable_entities').value.split(/\r?\n|,/).map(x=>x.trim()).filter(Boolean);
  return result;
}

$('signal_mode').addEventListener('change',setSignalMode);
$('reasoning_mode').addEventListener('change',setReasoningMode);
$('learning_enabled').addEventListener('change',setLearningMode);
$('entity_control_enabled').addEventListener('change',setEntityControlMode);
$('signalConnect').addEventListener('click',async()=>{
  const button=$('signalConnect'); button.disabled=true;
  try {
    const result=await request('api/signal/link',{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'}});
    $('signalQr').src=result.qr_code;
    $('signalQrPanel').hidden=false;
    linkPolling=true;
    toast(result.message);
    window.setTimeout(()=>{
      if(!linkPolling) return;
      linkPolling=false;
      $('signalQrPanel').hidden=true;
      $('signalConnect').disabled=false;
      toast('Der QR-Code ist abgelaufen. Bitte einen neuen erzeugen.',true);
    },90000);
  } catch(e) { toast(e.message,true); button.disabled=false; }
});
$('signalPair').addEventListener('click',startPairing);
$('signalUnlink').addEventListener('click',async()=>{
  if(!window.confirm('Lokale Signal-Verknüpfung und alle erlaubten Absender wirklich entfernen? Das Signal-Konto selbst wird nicht gelöscht.')) return;
  const button=$('signalUnlink'); button.disabled=true;
  try {
    const result=await request('api/signal/unlink',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},body:JSON.stringify({confirmation:'TRENNEN'})});
    $('signalQrPanel').hidden=true; $('signalPairPanel').hidden=true;
    $('signal_account').value=''; $('signal_self_chat_enabled').checked=false; $('allowed_senders').value='';
    toast(result.message); await loadSettings(); setTimeout(loadStatus,700);
  } catch(e) { toast(e.message,true); } finally { setTimeout(loadSignalStatus,500); }
});
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

loadSettings(); loadStatus();
setInterval(loadStatus,5000);
setInterval(loadSignalStatus,3000);
