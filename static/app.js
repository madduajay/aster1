'use strict';
const $ = id => document.getElementById(id);
let account = null, session = null, feed = null, answer = null, liveText = '', mode = 'login', navigation = 0, pendingApproval = null, pendingInput = null;
let browserTimer=null,browserWatch=0;
let takeoverTail=Promise.resolve();
let scrollFrame=null,scrollIntent=0;
const atBottom=()=>document.documentElement.scrollHeight-innerHeight-scrollY<40;
function scrollLatest(){
  if(scrollFrame!==null)return;
  const intent=scrollIntent;
  scrollFrame=requestAnimationFrame(()=>{scrollFrame=null;if(intent===scrollIntent)window.scrollTo({top:document.documentElement.scrollHeight,behavior:'instant'});});
}
for(const event of ['wheel','touchmove'])window.addEventListener(event,()=>{scrollIntent++;},{passive:true});
window.addEventListener('keydown',e=>{if(['PageUp','PageDown','Home','End','ArrowUp','ArrowDown'].includes(e.key)&&!e.target.closest('input,textarea'))scrollIntent++;});

function clearBrowser(){stopBrowserControl();pendingInput=null;document.body.classList.remove('takeover');$('browser-hint').textContent='Live view · read-only';browserWatch++;clearTimeout(browserTimer);browserTimer=null;$('browser-view').hidden=true;$('browser-frame').removeAttribute('src');delete $('browser-frame').dataset.url;$('browser-url').removeAttribute('href');$('browser-url').textContent='';document.body.classList.remove('browser-open');}
function watchBrowser(run,repeat=true){
  const watch=++browserWatch,generation=navigation;clearTimeout(browserTimer);
  async function refresh(){
    try{
      const view=await api('/runs/'+encodeURIComponent(run)+'/browser');
      if(watch!==browserWatch||generation!==navigation)return;
      if(view.available){
        $('browser-view').hidden=false;document.body.classList.add('browser-open');$('browser-state').textContent=view.live?'Live':'Last view';
        if(pendingInput?.takeover&&view.dialog)$('browser-hint').textContent='Website dialog: '+view.dialog.message+'. Focus the browser view and press Enter to accept or Escape to dismiss.';
        try{const url=new URL(view.url);if(!['https:','http:'].includes(url.protocol))throw new Error();$('browser-url').href=url.href;$('browser-url').textContent=url.hostname+url.pathname;}catch{$('browser-url').removeAttribute('href');$('browser-url').textContent='Opening page…';}
        $('browser-frame').dataset.url='/api/runs/'+encodeURIComponent(run)+'/browser/frame';
        if($('browser-view').open&&!pendingInput?.control)$('browser-frame').src=$('browser-frame').dataset.url+'?t='+Date.now();
      }
    }catch(e){if(watch!==browserWatch)return;}
    if(repeat&&watch===browserWatch&&generation===navigation)browserTimer=setTimeout(refresh,1500);
  }
  refresh();
}

async function api(path, method='GET', body) {
  const response = await fetch('/api'+path, {method, headers:body === undefined?{}:{'Content-Type':'application/json'}, body:body===undefined?undefined:JSON.stringify(body)});
  if (!response.ok) {
    const raw = await response.text(); let detail = raw;
    try { detail = JSON.parse(raw).detail || raw; } catch {}
    if (response.status === 401 && path !== '/login') signedOut();
    throw new Error(typeof detail === 'string'?detail:'Please check the form fields.');
  }
  return response.json();
}
function error(e) {if(e?.silent)return;$('error').textContent=e?.message||'';}
function links(node, text) {
  node._messageText=text;
  node.replaceChildren();
  // Render only text and HTTP(S) links. Never execute model-provided HTML.
  const pattern=/\[([^\]\n]{1,300})\]\((https?:\/\/[^\s)]+)\)/g;
  let last=0, match;
  while ((match=pattern.exec(text))) {
    node.append(document.createTextNode(text.slice(last, match.index)));
    const a=document.createElement('a'); a.textContent=match[1]; a.href=match[2]; a.target='_blank'; a.rel='noopener noreferrer'; node.append(a);
    last=pattern.lastIndex;
  }
  node.append(document.createTextNode(text.slice(last)));
}
function message(role, text, follow=true) {
  const stick=follow&&(role==='user'||atBottom());
  $('welcome').hidden=true;
  const node=document.createElement('div'); node.className='message '+role; links(node,text); $('messages').append(node);
  if(stick)scrollLatest();return node;
}
function updateAnswer(node,text){const stick=atBottom();links(node,text);if(stick)scrollLatest();}
function renderMessages(rows,refresh){
  if(!refresh)$('messages').replaceChildren();
  const existing=Array.from($('messages').children),used=new Set();
  const known=new Map(existing.filter(n=>n.dataset.messageId).map(n=>[n.dataset.messageId,n]));
  for(const row of rows){
    let node=known.get(String(row.id));
    if(!node)node=existing.find(n=>!used.has(n)&&!n.dataset.messageId&&!n.dataset.artifact&&n.classList.contains(row.role)&&(row.role!=='user'||n._messageText===row.content));
    if(!node)node=message(row.role,row.content,false);
    else if(node._messageText!==row.content)links(node,row.content);
    node.dataset.messageId=row.id;used.add(node);
  }
  for(const node of existing)if(!used.has(node)&&!node.dataset.artifact)node.remove();
  return known;
}
function signedOut() {
  clearBrowser();
  navigation++;feed?.close(); feed=null; account=null;session=null;mode='login';pendingApproval=null;
  $('messages').replaceChildren();$('chat').hidden=true;$('composer').hidden=true;$('login').hidden=false;
  $('login-form').hidden=false;$('password-form').hidden=true;$('new').hidden=true;$('auth').textContent='Login';$('who').textContent='';
  $('password').value='';$('current-password').value='';$('new-password').value='';
  $('input').value='';$('status').textContent='';error(null);
  $('login-note').textContent='Sign in to your personal space.';
}
async function entered(me) {
  account=me; error(null);
  if (me.must_change) {
    mode='password';$('auth').textContent='Logout';$('login-form').hidden=true;$('password-form').hidden=false;
    $('login-note').textContent='Set your own password before your first chat.';$('new-password').focus();return;
  }
  mode='chat';$('login').hidden=true;$('chat').hidden=false;$('composer').hidden=false;$('new').hidden=false;$('auth').textContent='Logout';$('who').textContent=me.username;
  const rows=await api('/sessions');
  if(rows.length) await openSession(rows[0].id); else await newChat();
}
async function openSession(id,{refresh=false}={}) {
  refresh=refresh&&id===session;
  if(!refresh)clearBrowser();
  const generation=++navigation; feed?.close();feed=null;pendingApproval=null;stopBrowserControl();pendingInput=null;document.body.classList.remove('takeover');if(!refresh)$('input').disabled=true;
  let result;
  try{result=await api('/sessions/'+encodeURIComponent(id));}
  catch(e){if(generation===navigation)$('input').disabled=false;throw e;}
  if(generation!==navigation)return;
  const stick=refresh&&atBottom();
  feed?.close();feed=null;session=id;liveText='';$('welcome').hidden=result.messages.length>0;$('status').textContent='';
  renderMessages(result.messages,refresh);answer=null;
  for(const controls of $('messages').querySelectorAll('.approval-actions'))controls.remove();
  let approvalNode=null;
  if(result.active_run?.approval_message_id)approvalNode=Array.from($('messages').children).find(n=>n.dataset.messageId===String(result.active_run.approval_message_id));
  if(result.active_run?.status==='approval') {
    approvalNode=approvalNode||message('assistant',result.active_run.approval_text||'Approval needed',false);
    links(approvalNode,result.active_run.approval_text||'Approval needed');
    const controls=document.createElement('div');controls.className='approval-actions';controls.setAttribute('role','group');controls.setAttribute('aria-label','Approval actions');
    const approval={run:result.active_run.id,id:result.active_run.approval_id,session:id,generation,busy:false,controls};pendingApproval=approval;
    for(const [label,decision] of [['Approve','approve'],['Cancel','reject']]){
      const button=document.createElement('button');button.type='button';button.textContent=label;button.className=decision==='approve'?'approve':'cancel';
      button.onclick=()=>respondToApproval(approval,decision);controls.append(button);
    }
    approvalNode.classList.add('approval-card');approvalNode.append(controls);$('status').textContent='Waiting for your approval.';
  }
  if(result.active_run?.status==='input') {
    const active=result.active_run;
    let node=Array.from($('messages').children).find(n=>n.dataset.messageId===String(active.input_message_id));
    if(!node)node=message('assistant',active.input_text,false);
    if(active.input.kind==='manual')links(node,'The website needs your input. Enter information directly in the browser panel. Continue hands control back to the agent; Cancel stops this task. Keep passwords and verification codes out of chat.');
    const waiting={run:active.id,id:active.approval_id,kind:active.input.kind,session:id,generation,busy:false};pendingInput=waiting;takeoverTail=Promise.resolve();
    const controls=document.createElement('div');controls.className='approval-actions';node.append(controls);node.classList.add('approval-card');
    {
      const next=document.createElement('button');next.textContent='Continue';next.type='button';next.onclick=()=>respondToInput(waiting,'continue',true);controls.append(next);
      try{
        await api('/runs/'+encodeURIComponent(waiting.run)+'/takeover','POST',{input_id:waiting.id,operation:'open'});
        if(generation!==navigation)return;
        // Expand first: Chromium needs the actual available panel dimensions,
        // rather than scaling its desktop viewport into the old small preview.
        $('browser-view').hidden=false;$('browser-view').open=true;document.body.classList.add('browser-open','takeover');
        waiting.control=new BrowserControlView(waiting.run,waiting.id,$('browser-frame'),e=>{if(pendingInput===waiting){waiting.takeover=false;error(e);}},size=>takeover('resize',size).catch(error));
        await waiting.control.connect();
        if(generation!==navigation){waiting.control.close();return;}
        waiting.takeover=true;$('browser-view').open=true;
        document.body.classList.add('takeover');$('browser-hint').textContent='Your browser · click the website textbox and type directly. Choose Continue when finished.';
        $('browser-frame').setAttribute('aria-label','Interactive website. Click a field, then type or paste.');
      }catch(e){if(generation===navigation){next.disabled=true;error(e);}}
    }
    const cancel=document.createElement('button');cancel.textContent='Cancel';cancel.type='button';cancel.onclick=()=>cancelInput(waiting);controls.append(cancel);
    $('status').textContent=waiting.kind==='manual'?'Waiting for you to complete the website step.':'Waiting for your reply. Enter information in the browser panel or reply here.';
    watchBrowser(active.id);
  } else if(result.active_run && result.active_run.status!=='approval') listen(result.active_run.id,result.active_run.after||0);
  else if(result.active_run?.status==='approval')watchBrowser(result.active_run.id);
  else if(result.browser_run)watchBrowser(result.browser_run,false);
  $('input').disabled=false;
  if(!refresh){$('input').focus({preventScroll:true});scrollLatest();}else if(stick)scrollLatest();
}
async function respondToApproval(approval,decision) {
  if(pendingApproval!==approval||approval.busy)return;
  approval.busy=true;error(null);for(const button of approval.controls.children)button.disabled=true;
  try{
    const result=await api('/runs/'+encodeURIComponent(approval.run)+'/approval','POST',{approval_id:approval.id,decision});
    if(approval.generation!==navigation||approval.session!==session)return;
    pendingApproval=null;approval.controls.replaceChildren();message('user',decision==='approve'?'Approve':'Cancel');
    listen(result.run_id,result.after||0);
  }catch(e){
    if(approval.generation===navigation){try{await openSession(approval.session,{refresh:true});}catch{}error(e);}
  }
}
async function respondToInput(waiting,text,completedInBrowser=false){
  if(pendingInput!==waiting||waiting.busy)return;
  waiting.busy=true;error(null);
  try{
    await takeoverTail; // Finish accepted clicks/keystrokes before resuming the agent.
    if(pendingInput!==waiting||waiting.generation!==navigation||waiting.cancelled)return;
    const result=await api('/runs/'+encodeURIComponent(waiting.run)+'/input','POST',{input_id:waiting.id,text,completed_in_browser:completedInBrowser});
    if(waiting.generation!==navigation)return;
    stopBrowserControl();pendingInput=null;document.body.classList.remove('takeover');$('browser-hint').textContent='Live view · read-only';$('browser-frame').removeAttribute('aria-label');
    for(const controls of $('messages').querySelectorAll('.approval-actions'))controls.remove();
    message('user',waiting.kind==='manual'?'Completed manual website step.':completedInBrowser?'Completed website information step.':text);listen(result.run_id,result.after||0);
  }catch(e){waiting.busy=false;error(e);}
}
async function cancelInput(waiting){
  if(pendingInput!==waiting||waiting.busy)return;
  waiting.busy=true;waiting.cancelled=true;
  try{await api('/runs/'+encodeURIComponent(waiting.run)+'/cancel','POST',{});if(waiting.generation===navigation){clearBrowser();await openSession(session,{refresh:true});}}
  catch(e){waiting.busy=false;waiting.cancelled=false;error(e);}
}
async function takeover(operation,details={}){
  const waiting=pendingInput;
  if(!waiting?.takeover||waiting.busy)return;
  const action=takeoverTail.catch(()=>{}).then(async()=>{
    if(pendingInput!==waiting||waiting.cancelled)return;
    const focus=await waiting.control.send(operation,details);
    if(pendingInput===waiting&&!focus.cancelled){
      error(null);
      const keyboard=$('website-keyboard');keyboard.inputMode=focus.input_mode==='number'?'numeric':focus.input_mode||'text';
      if(focus.editable&&['click','pointerup','key'].includes(operation))keyboard.focus({preventScroll:true});
      else if(!focus.editable&&document.activeElement===keyboard)$('browser-frame').focus({preventScroll:true});
    }
  });
  // Drain input in order, but do not make Continue rethrow an old failed key.
  // Its server-side readiness check must inspect the actual website instead.
  takeoverTail=action.catch(()=>{});return action;
}
$('browser-frame').tabIndex=0;
let heldPointer=null;
$('browser-frame').onpointerdown=e=>{
  if(!pendingInput?.takeover||e.button!==0||heldPointer)return;
  e.preventDefault();
  $('website-keyboard').focus({preventScroll:true});
  try{
    const point=pendingInput.control.point(e);if(!point)return;
    heldPointer={id:e.pointerId,point};e.currentTarget.setPointerCapture(e.pointerId);
    takeover('pointerdown',point).catch(error);
  }catch(e){error(e);}
};
function releaseWebsitePointer(e,cancel=false){
  if(!heldPointer||e&&e.pointerId!==heldPointer.id)return;
  const held=heldPointer;heldPointer=null;
  try{if($('browser-frame').hasPointerCapture(held.id))$('browser-frame').releasePointerCapture(held.id);}catch{}
  let point=held.point;
  if(cancel)point={...point,x:0,y:0};
  else if(e){try{point=pendingInput?.control?.point(e)||{...point,x:0,y:0};}catch{point={...point,x:0,y:0};}}
  takeover('pointerup',point).catch(error);
}
$('browser-frame').onpointerup=e=>releaseWebsitePointer(e);
$('browser-frame').onpointercancel=e=>releaseWebsitePointer(e,true);
window.addEventListener('blur',()=>releaseWebsitePointer(null,true));
function browserKey(e){
  if(!pendingInput?.takeover||e.isComposing)return;
  if(['Enter','Tab','Backspace','Delete','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)){
    e.preventDefault();takeover('key',{key:e.shiftKey&&e.key==='Tab'?'Shift+Tab':e.key}).catch(error);
  }else if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='a'){e.preventDefault();takeover('key',{key:'ControlOrMeta+A'}).catch(error);}
  else if(e.key.length===1&&!e.ctrlKey&&!e.metaKey&&!e.altKey){e.preventDefault();takeover('type',{text:e.key}).catch(error);}
}
$('browser-frame').onkeydown=browserKey;
$('website-keyboard').onkeydown=e=>{if(e.key.length!==1||e.ctrlKey||e.metaKey||e.altKey)browserKey(e);};
$('browser-frame').addEventListener('paste',e=>{if(pendingInput?.takeover){e.preventDefault();const text=e.clipboardData?.getData('text/plain');if(text)takeover('type',{text}).catch(error);}});
$('browser-frame').addEventListener('wheel',e=>{if(pendingInput?.takeover){e.preventDefault();takeover('scroll',{x:e.deltaX,y:e.deltaY}).catch(error);}},{passive:false});
// The invisible keyboard sink supports mobile keyboards and IME. It clears
// immediately; users see and edit only the actual website textbox in the stream.
let composing=false,ignoreCompositionInput=false,compositionText='';
const websiteKeyboard=$('website-keyboard');
websiteKeyboard.addEventListener('compositionstart',()=>{composing=true;});
websiteKeyboard.addEventListener('compositionend',e=>{composing=false;ignoreCompositionInput=true;compositionText=e.data;websiteKeyboard.value='';if(compositionText)takeover('type',{text:compositionText}).catch(error);setTimeout(()=>{ignoreCompositionInput=false;compositionText='';},0);});
websiteKeyboard.addEventListener('input',e=>{if(composing||e.isComposing)return;const text=websiteKeyboard.value;websiteKeyboard.value='';if(text&&!(ignoreCompositionInput&&text===compositionText))takeover('type',{text}).catch(error);});
websiteKeyboard.addEventListener('beforeinput',e=>{
  if(e.inputType==='insertLineBreak'){e.preventDefault();takeover('key',{key:'Enter'}).catch(error);}
  else if(e.inputType==='deleteContentBackward'||e.inputType==='deleteContentForward'){e.preventDefault();takeover('key',{key:e.inputType==='deleteContentBackward'?'Backspace':'Delete'}).catch(error);}
});
websiteKeyboard.addEventListener('paste',e=>{e.preventDefault();const text=e.clipboardData?.getData('text/plain');if(text)takeover('type',{text}).catch(error);});
function stopBrowserControl(){heldPointer=null;pendingInput?.control?.close();websiteKeyboard.value='';}
async function newChat() {
  feed?.close();feed=null;$('input').disabled=true;$('new').disabled=true;
  try{const row=await api('/sessions','POST',{});await openSession(row.id);}
  finally{$('input').disabled=false;$('new').disabled=false;}
}
function listen(run, after=0) {
  if($('browser-frame').dataset.url&&$('browser-frame').dataset.url!=='/api/runs/'+encodeURIComponent(run)+'/browser/frame')clearBrowser();
  watchBrowser(run);
  feed?.close();answer=null;liveText='';$('status').textContent='Thinking…';
  const source=new EventSource('/api/runs/'+encodeURIComponent(run)+'/events?after='+after);feed=source;
  source.addEventListener('token',e=>{if(feed!==source)return;liveText+=JSON.parse(e.data);if(!answer)answer=message('assistant','');updateAnswer(answer,liveText);});
  source.addEventListener('progress',e=>{if(feed===source)$('status').textContent=JSON.parse(e.data);});
  source.addEventListener('diagnostic',e=>{if(feed===source)$('status').textContent=JSON.parse(e.data).text;});
  source.addEventListener('answer',e=>{if(feed!==source)return;liveText=JSON.parse(e.data);if(!answer)answer=message('assistant','');updateAnswer(answer,liveText);});
  source.addEventListener('artifact',e=>{if(feed!==source)return;const a=JSON.parse(e.data); const node=message('assistant','');node.dataset.artifact='true';const link=document.createElement('a');link.href=a.url;link.textContent=a.name;link.target='_blank';link.rel='noopener';node.append(link);if(a.image){const img=document.createElement('img');img.src=a.url;img.alt='Browser screenshot';node.append(img);}});
  source.addEventListener('approval',async()=>{if(feed!==source)return;source.close();feed=null;try{await openSession(session,{refresh:true});$('status').textContent='Waiting for your approval.';}catch(e){error(e);}});
  source.addEventListener('input',async()=>{if(feed!==source)return;source.close();feed=null;try{await openSession(session,{refresh:true});}catch(e){error(e);}});
  source.addEventListener('done',async()=>{if(feed!==source)return;source.close();feed=null;try{await openSession(session,{refresh:true});$('status').textContent='';}catch(e){error(e);}});
  source.onerror=()=>{if(feed!==source)return;source.close();feed=null;$('status').textContent='Connection paused. Reload to reconnect; your conversation is saved.';};
}
$('new').onclick=()=>newChat().catch(error);
$('browser-frame').onload=()=>{$('browser-frame').style.visibility='visible';};
$('browser-frame').onerror=()=>{$('browser-frame').style.visibility='hidden';};
$('browser-view').ontoggle=()=>{if($('browser-view').open&&$('browser-frame').dataset.url&&!pendingInput?.control)$('browser-frame').src=$('browser-frame').dataset.url+'?t='+Date.now();};
$('auth').onclick=async()=>{try{if(account){await api('/logout','POST',{});signedOut();}else $('login-form').requestSubmit();}catch(e){error(e);}};
$('login-form').onsubmit=async e=>{e.preventDefault();try{const me=await api('/login','POST',{username:$('username').value,password:$('password').value});$('current-password').value=$('password').value;$('password').value='';await entered(me);}catch(e){error(e);}};
$('password-form').onsubmit=async e=>{e.preventDefault();try{await api('/password','POST',{current_password:$('current-password').value,new_password:$('new-password').value});$('current-password').value='';$('new-password').value='';await entered({...account,must_change:false});}catch(e){error(e);}};
for(const id of ['username','password','current-password','new-password'])$(id).addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();e.target.form.requestSubmit();}});
$('input').oninput=()=>{$('input').style.height='auto';$('input').style.height=Math.min($('input').scrollHeight,170)+'px';};
$('input').addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files'))e.preventDefault();});
$('input').addEventListener('drop',async e=>{
  const files=Array.from(e.dataTransfer?.files||[]);if(!files.length)return;e.preventDefault();
  const destination=session,generation=navigation;
  $('input').disabled=true;error(null);
  try{
    if(!account||!destination)throw new Error('Log in and open a chat first.');
    for(const file of files){
      if(file.size>50000000)throw new Error('Each file must be at most 50 MB.');
      $('status').textContent='Uploading '+file.name+'…';
      const response=await fetch('/api/uploads?session_id='+encodeURIComponent(destination),{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Filename':encodeURIComponent(file.name)},body:file});
      if(!response.ok)throw new Error('Upload failed: '+await response.text());
      const result=await response.json();
      if(generation===navigation)message('user',result.text);
    }
  }catch(e){error(e);}finally{if(generation===navigation){$('status').textContent='';$('input').disabled=false;$('input').focus();}}
});
$('input').onkeydown=async e=>{
  if(e.key!=='Enter'||e.shiftKey||e.isComposing)return;e.preventDefault();const text=$('input').value.trim();if(!text)return;
  $('input').value='';$('input').style.height='auto';error(null);
  try{
    if(text.startsWith('/open ')){await openSession(text.slice(6).trim());return;}
    if(pendingApproval&&/^(approve|cancel|reject)$/i.test(text)){await respondToApproval(pendingApproval,text.toLowerCase()==='approve'?'approve':'reject');return;}
    if(pendingInput&&/^(?:cancel|stop|\/stop)$/i.test(text)){await cancelInput(pendingInput);return;}
    const inputId=pendingInput?.id;
    // Detach the old event stream before cancellation can emit its final event.
    // Otherwise that event can reload the old run over the replacement stream.
    ++navigation;feed?.close();feed=null;$('input').disabled=true;$('new').disabled=true;
    try{
      await api('/runs','POST',{session_id:session,text,pending_input_id:inputId||null});
      clearBrowser();pendingApproval=null;await openSession(session,{refresh:true});
    }catch(e){await openSession(session,{refresh:true});throw e;}
    finally{$('input').disabled=false;$('new').disabled=false;}

  }catch(e){error(e);if(account)$('input').value=text;}
};
api('/me').then(entered).catch(()=>signedOut());
