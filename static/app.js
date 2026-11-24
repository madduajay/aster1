'use strict';
const $ = id => document.getElementById(id);
let account = null, session = null, feed = null, answer = null, liveText = '', mode = 'login', navigation = 0, pendingApproval = null, pendingInput = null;
let browserTimer=null,browserWatch=0;
let takeoverTail=Promise.resolve();
let scrollFrame=null,scrollIntent=0;
let draftFiles=[];
const atBottom=()=>document.documentElement.scrollHeight-innerHeight-scrollY<40;
function scrollLatest(){
  if(document.body.classList.contains('takeover'))return;
  if(scrollFrame!==null)return;
  const intent=scrollIntent;
  scrollFrame=requestAnimationFrame(()=>{scrollFrame=null;if(intent===scrollIntent)window.scrollTo({top:document.documentElement.scrollHeight,behavior:'instant'});});
}
for(const event of ['wheel','touchmove'])window.addEventListener(event,()=>{scrollIntent++;},{passive:true});
window.addEventListener('keydown',e=>{if(['PageUp','PageDown','Home','End','ArrowUp','ArrowDown'].includes(e.key)&&!e.target.closest('input,textarea'))scrollIntent++;});

function clearBrowser(){stopBrowserControl();pendingInput=null;document.body.classList.remove('takeover');$('browser-hint').textContent='Live view · read-only';browserWatch++;clearTimeout(browserTimer);browserTimer=null;$('browser-view').hidden=true;$('browser-frame').removeAttribute('src');delete $('browser-frame').dataset.url;$('browser-url').removeAttribute('href');$('browser-url').textContent='';document.body.classList.remove('browser-open');}
function updateTakeoverButtons(waiting){
  if(pendingInput!==waiting)return;
  $('browser-take-control').dataset.run=waiting.run;
  $('browser-cancel-task').dataset.run=waiting.run;
  $('browser-take-control').hidden=!!(waiting.takeover||waiting.starting||waiting.busy||waiting.returned);
  if(waiting.releaseButton)waiting.releaseButton.hidden=!(waiting.takeover&&!waiting.starting&&!waiting.returned);
}
function watchBrowser(run,repeat=true,fallbackRun=null){
  const watch=++browserWatch,generation=navigation;clearTimeout(browserTimer);
  async function refresh(){
    try{
      let displayRun=run,view=await api('/runs/'+encodeURIComponent(run)+'/browser');
      if(!view.available&&!view.can_take_control&&!view.returned&&fallbackRun&&fallbackRun!==run){
        view=await api('/runs/'+encodeURIComponent(fallbackRun)+'/browser');displayRun=fallbackRun;
      }
      if(watch!==browserWatch||generation!==navigation)return;
      if(view.returned){$('browser-view').hidden=true;document.body.classList.remove('browser-open','takeover');return;}
      if(pendingInput)updateTakeoverButtons(pendingInput);
      else $('browser-take-control').hidden=displayRun!==run||!view.can_take_control;
      $('browser-take-control').dataset.run=displayRun;
      $('browser-cancel-task').dataset.run=run;
      if(view.available||view.can_take_control){
        $('browser-view').hidden=false;document.body.classList.add('browser-open');$('browser-state').textContent=view.live?'Live':'Last view';
        if(pendingInput?.takeover&&view.dialog)$('browser-hint').textContent='Website dialog: '+view.dialog.message+'. Focus the browser view and press Enter to accept or Escape to dismiss.';
        try{const url=new URL(view.url);if(!['https:','http:'].includes(url.protocol))throw new Error();$('browser-url').href=url.href;$('browser-url').textContent=url.hostname+url.pathname;}catch{$('browser-url').removeAttribute('href');$('browser-url').textContent='Opening page…';}
        $('browser-frame').dataset.url='/api/runs/'+encodeURIComponent(displayRun)+'/browser/frame';
        $('browser-frame').hidden=!view.available;
        if(!view.available)$('browser-hint').textContent='Saved page · choose Take control to open it';
        if(view.available&&$('browser-view').open&&!pendingInput?.control)$('browser-frame').src=$('browser-frame').dataset.url+'?t='+Date.now();
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
function venuePhoto(path,alt){
  const image=document.createElement('img');image.className='venue-photo';image.src=path;image.alt=alt||'Venue photo';image.loading='lazy';image.decoding='async';
  image.onerror=()=>{const placeholder=document.createElement('span');placeholder.className='venue-photo-placeholder';placeholder.textContent='Photo unavailable';placeholder.setAttribute('role','img');placeholder.setAttribute('aria-label','Photo unavailable');image.replaceWith(placeholder);};
  return image;
}
function normalizeVenuePhotos(text){
  // Accept escaped Markdown delimiters only for the authenticated photo route.
  return text.replace(/!\[([^\]\n]{0,250})\]\\\((\/api\/venue-photos\/[a-f0-9]{64})\\\)/g,'![$1]($2)');
}
function inlineText(node, text) {
  text=normalizeVenuePhotos(text);
  // Only server-cached venue photos can become images; model HTML stays text.
  const pattern=/!\[(?<illustrationAlt>[^\]\n]{0,250})\]\((?<illustration>\/api\/artifacts\/[a-f0-9]{32})\)|!\[(?<alt>[^\]\n]{0,250})\]\((?<photo>\/api\/venue-photos\/[a-f0-9]{64})\)|\[(?<label>[^\]\n]{1,300})\]\((?<url>https?:\/\/[^\s)]+)\)|\*\*(?<bold>[^*\n]+)\*\*|`(?<code>[^`\n]+)`/g;
  let last=0, match;
  while ((match=pattern.exec(text))) {
    node.append(document.createTextNode(text.slice(last, match.index)));
    const part=match.groups;
    if(part.illustration){const image=document.createElement('img');image.src=part.illustration;image.alt=part.illustrationAlt||'Illustration';image.className='generated-illustration';image.loading='lazy';image.decoding='async';node.append(image);}
    else if(part.photo)node.append(venuePhoto(part.photo,part.alt));
    else if(part.label){const a=document.createElement('a');inlineText(a,part.label);a.href=part.url;a.target='_blank';a.rel='noopener noreferrer';node.append(a);}
    else{const span=document.createElement(part.bold?'strong':'code');if(part.bold)inlineText(span,part.bold);else span.textContent=part.code;node.append(span);}
    last=pattern.lastIndex;
  }
  node.append(document.createTextNode(text.slice(last)));
}
function venueCell(node,text){
  text=normalizeVenuePhotos(text);
  const photo=text.match(/^!\[([^\]]*)\]\((\/api\/venue-photos\/[a-f0-9]{64})\)\s*/);
  if(!photo){inlineText(node,text);return;}
  const preview=document.createElement('div');preview.className='venue-preview';preview.append(venuePhoto(photo[2],photo[1]));
  const copy=document.createElement('div');copy.className='venue-copy';const rest=text.slice(photo[0].length),leadLink=rest.match(/^\[[^\]\n]+\]\(https?:\/\/[^\s)]+\)/),divider=rest.indexOf(' — ',leadLink?leadLink[0].length:0);
  const name=document.createElement('div');name.className='venue-name';inlineText(name,divider<0?rest:rest.slice(0,divider));copy.append(name);
  if(divider>=0){const description=document.createElement('p');description.className='venue-description';description.textContent=rest.slice(divider+3);copy.append(description);}
  preview.append(copy);node.append(preview);
}
function links(node, text) {
  node._messageText=text;
  node.replaceChildren();
  if(!node.classList.contains('assistant')){inlineText(node,text);return;}
  let paragraph=null,list=null,code=null;
  const lines=text.replace(/\r\n?/g,'\n').split('\n');
  const cells=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split(/(?<!\\)\|/).map(value=>value.trim().replace(/\\\|/g,'|'));
  for(let index=0;index<lines.length;index++) {
    const line=lines[index];
    if(/^\s*```/.test(line)) {
      if(code){code=null;}else{const pre=document.createElement('pre');code=document.createElement('code');pre.append(code);node.append(pre);}
      paragraph=null;list=null;continue;
    }
    if(code){code.append(document.createTextNode((code.childNodes.length?'\n':'')+line));continue;}
    // Blank lines make a loose Markdown list; they do not split each venue
    // into a separate white response card.
    if(!line.trim()){paragraph=null;continue;}
    if(line.trim().startsWith('|')&&index+1<lines.length&&/^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$/.test(lines[index+1].trim())) {
      const wrap=document.createElement('div');wrap.className='response-table';
      const table=document.createElement('table'),head=document.createElement('thead'),body=document.createElement('tbody');
      const headers=cells(line),row=document.createElement('tr');
      for(const label of headers){const th=document.createElement('th');th.scope='col';inlineText(th,label);row.append(th);}
      head.append(row);table.append(head,body);index++;
      while(index+1<lines.length&&lines[index+1].trim().startsWith('|')) {
        const values=cells(lines[++index]),tr=document.createElement('tr');
        for(let column=0;column<headers.length;column++){const td=document.createElement('td');if(column===0&&['Hotel','Venue','Item','Product','Option'].includes(headers[0]))venueCell(td,values[column]||'');else inlineText(td,values[column]||'');tr.append(td);}
        body.append(tr);
      }
      wrap.append(table);node.append(wrap);paragraph=null;list=null;continue;
    }
    const bullet=line.match(/^\s*(?:[-*+•]\s+|#{1,6}\s+)(.+)$/);
    const ordered=line.match(/^\s*(\d+)[.)]\s+(.+)$/);
    if(bullet||ordered) {
      const tag=ordered?'ol':'ul';
      if(!list||list.tagName.toLowerCase()!==tag){list=document.createElement(tag);if(ordered)list.start=Number(ordered[1]);node.append(list);}
      const item=document.createElement('li');const itemText=bullet?bullet[1]:ordered[2];if(itemText.startsWith('![')){item.className='venue-item';list.classList.add('venue-list');venueCell(item,itemText);}else inlineText(item,itemText);list.append(item);paragraph=null;
    } else {
      list=null;
      if(!paragraph){paragraph=document.createElement('p');node.append(paragraph);}else{paragraph.append(document.createTextNode('\n'));}
      inlineText(paragraph,line);
    }
  }
}
function message(role, text, follow=true) {
  const stick=follow&&(role==='user'||atBottom());
  $('welcome').hidden=true;
  const node=document.createElement('div'); node.className='message '+role; links(node,text); $('messages').append(node);
  if(stick)scrollLatest();return node;
}
function updateAnswer(node,text){const stick=atBottom();links(node,text);if(stick)scrollLatest();}
function renderAttachments(node,attachments=[]){
  if(node.classList.contains('assistant'))attachments=attachments.filter(file=>!node._messageText.includes('/api/artifacts/'+file.id));
  if(!attachments.length)return;
  node.replaceChildren();node.classList.add('upload-message');
  if(!node._messageText.startsWith('Uploaded file:')){const question=document.createElement('div');question.className='attachment-question';question.textContent=node._messageText;node.append(question);}
  for(const file of attachments){
    // URLs are constructed from authenticated artifact IDs, never model text.
    if(!/^[a-f0-9]{32}$/.test(file.id))continue;
    const link=document.createElement('a');link.href='/api/artifacts/'+file.id;link.target='_blank';link.rel='noopener';
    if(file.image){const image=document.createElement('img');image.src=link.href;image.alt=file.filename;image.loading='lazy';link.append(image);}
    const name=document.createElement('span');name.className='attachment-name';name.textContent=file.filename;link.append(name);node.append(link);
  }
}
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
    renderAttachments(node,row.attachments);
  }
  for(const node of existing)if(!used.has(node)&&!node.dataset.artifact)node.remove();
  return known;
}
function signedOut() {
  $('browser-mode-setting').hidden=true;
  window.stopBackgroundUI?.();
  clearDraftFiles();
  clearBrowser();
  navigation++;feed?.close(); feed=null; account=null;session=null;mode='login';pendingApproval=null;
  $('messages').replaceChildren();$('chat').hidden=true;$('composer').hidden=true;$('login').hidden=false;
  $('login-form').hidden=false;$('password-form').hidden=true;$('new').hidden=true;$('auth').textContent='Login';$('who').textContent='';
  $('password').value='';$('current-password').value='';$('new-password').value='';
  $('input').value='';$('status').textContent='';error(null);
  $('login-note').textContent='Sign in to your personal space.';
}
async function entered(me) {
  await loadBrowserMode();
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
  if(!refresh&&id!==session)clearDraftFiles();
  if(!refresh)clearBrowser();
  const generation=++navigation; feed?.close();feed=null;pendingApproval=null;stopBrowserControl();pendingInput=null;if(!refresh)document.body.classList.remove('takeover');if(!refresh)$('input').disabled=true;
  let result;
  try{result=await api('/sessions/'+encodeURIComponent(id));}
  catch(e){if(generation===navigation)$('input').disabled=false;throw e;}
  if(generation!==navigation)return;
  if(result.active_run?.status!=='input')document.body.classList.remove('takeover');
  const stick=refresh&&atBottom();
  feed?.close();feed=null;session=id;liveText='';$('welcome').hidden=result.messages.length>0;$('status').textContent='';
  renderMessages(result.messages,refresh);answer=null;
  window.refreshBackground?.();
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
    links(node,active.input_text.replace(/Continue hands control back to the agent; Cancel stops this task\./g,'Release control continues the task. Return to chat closes this panel; Cancel task stops the task.').replace(/\bContinue\b|\bDone\b/g,'Release control'));
    const waiting={run:active.id,id:active.approval_id,kind:active.input.kind,purpose:active.input.purpose,returned:active.browser_returned,session:id,generation,busy:false};pendingInput=waiting;takeoverTail=Promise.resolve();
    const controls=document.createElement('div');controls.className='approval-actions';node.classList.add('approval-card');
    if(waiting.returned){
      node.append(controls);
      const open=document.createElement('button');open.type='button';open.textContent='Open browser';open.onclick=()=>reopenBrowser(waiting.run,waiting);controls.append(open);
      $('browser-view').hidden=true;document.body.classList.remove('browser-open','takeover');
    }else{
      controls.classList.add('browser-controls');$('browser-toolbar').insertBefore(controls,$('browser-toolbar').firstChild);
      const next=document.createElement('button');next.textContent='Return to chat';next.type='button';next.onclick=()=>returnToChat(waiting);controls.append(next);
      waiting.startControl=async()=>{
        if(waiting.starting||waiting.takeover||waiting.busy||pendingInput!==waiting)return;
        waiting.starting=true;$('browser-take-control').hidden=true;
        try{
        await api('/runs/'+encodeURIComponent(waiting.run)+'/takeover','POST',{input_id:waiting.id,operation:'open'});
        if(generation!==navigation)return;
        // Expand first: Chromium needs the actual available panel dimensions,
        // rather than scaling its desktop viewport into the old small preview.
        $('browser-view').hidden=false;$('browser-view').open=true;document.body.classList.add('browser-open','takeover');
        waiting.control=new BrowserControlView(waiting.run,waiting.id,$('browser-frame'),e=>{if(pendingInput===waiting){waiting.takeover=false;updateTakeoverButtons(waiting);error(e);}},size=>takeover('resize',size).catch(error));
        await waiting.control.connect();
        if(generation!==navigation){waiting.control.close();return;}
        waiting.takeover=true;$('browser-view').open=true;
        document.body.classList.add('takeover');$('browser-hint').textContent=waiting.purpose==='direct_control'?'Enter details directly. Release control ends manual browsing.':'Enter details directly. Release control continues the task; Return to chat closes this panel.';
        $('browser-frame').setAttribute('aria-label','Interactive website. Click a field, then type or paste.');
        }catch(e){
          waiting.control?.close();waiting.control=null;waiting.takeover=false;
          if(generation===navigation){document.body.classList.remove('takeover');error(e);}
        }
        finally{waiting.starting=false;updateTakeoverButtons(waiting);}
      };
      if(waiting.purpose==='direct_control')await waiting.startControl();
      else{
        $('browser-view').hidden=false;$('browser-view').open=true;document.body.classList.add('browser-open');
        $('browser-hint').textContent='Choose Take control to enter information in the browser panel.';
      }
    }
    if(!waiting.returned){
      const release=document.createElement('button');release.type='button';release.textContent='Release control';
      waiting.releaseButton=release;release.hidden=!waiting.takeover;
      release.title=waiting.purpose==='direct_control'?'End manual browser control':'Continue this task with Browser Use';
      release.onclick=async()=>{
        release.disabled=true;
        try{await respondToInput(waiting,'continue',true);}
        finally{if(pendingInput===waiting){release.disabled=false;updateTakeoverButtons(waiting);}}
      };
      controls.append(release);
      updateTakeoverButtons(waiting);
    }
    if(waiting.returned){const cancel=document.createElement('button');cancel.textContent='Cancel task';cancel.type='button';cancel.onclick=()=>cancelInput(waiting);controls.append(cancel);}
    $('status').textContent=waiting.returned?'Task paused. Open browser to finish website steps or release control.':waiting.kind==='manual'?'Waiting for you to complete the website step.':'Waiting for your reply. Enter information in the browser panel or reply here.';
    watchBrowser(active.id);
  } else if(result.active_run && result.active_run.status!=='approval') listen(result.active_run.id,result.active_run.after||0,result.browser_run);
  else if(result.active_run?.status==='approval')watchBrowser(result.active_run.id);
  else if(result.browser_run){
    watchBrowser(result.browser_run,false);
    if(result.browser_returned){
      const node=message('assistant','You’re back in chat.',false);node.classList.add('return-card');
      const open=document.createElement('button');open.type='button';open.textContent='Open browser';open.onclick=()=>reopenBrowser(result.browser_run);node.append(open);
    }
  } else clearBrowser();
  $('input').disabled=false;
  if(!refresh&&!pendingInput?.takeover){$('input').focus({preventScroll:true});scrollLatest();}else if(stick)scrollLatest();
}
async function reopenBrowser(run,waiting=null){
  error(null);
  try{
    if(waiting)await api('/runs/'+encodeURIComponent(run)+'/takeover','POST',{input_id:waiting.id,operation:'open'});
    else await api('/runs/'+encodeURIComponent(run)+'/browser/take-control','POST',{});
    await openSession(session,{refresh:true});
  }catch(e){error(e);}
}
async function returnToChat(waiting){
  if(pendingInput!==waiting||waiting.busy)return;waiting.busy=true;error(null);
  try{
    await takeoverTail;
    await api('/runs/'+encodeURIComponent(waiting.run)+'/browser/return','POST',{input_id:waiting.id,text:'return to chat'});
    stopBrowserControl();pendingInput=null;document.body.classList.remove('takeover','browser-open');$('browser-view').hidden=true;
    await openSession(session,{refresh:true});$('input').focus({preventScroll:true});scrollLatest();
  }catch(e){waiting.busy=false;error(e);}
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
  if(!waiting||pendingInput!==waiting||waiting.busy)return;
  waiting.busy=true;error(null);
  try{
    await takeoverTail; // Finish accepted clicks/keystrokes before resuming the agent.
    if(pendingInput!==waiting||waiting.generation!==navigation||waiting.cancelled)return;
    const result=await api('/runs/'+encodeURIComponent(waiting.run)+'/input','POST',{input_id:waiting.id,text,completed_in_browser:completedInBrowser});
    if(waiting.generation!==navigation)return;
    if(result.completed){stopBrowserControl();pendingInput=null;document.body.classList.remove('takeover');await openSession(session,{refresh:true});return;}
    stopBrowserControl();pendingInput=null;document.body.classList.remove('takeover');$('browser-hint').textContent='Live view · read-only';$('browser-frame').removeAttribute('aria-label');
    for(const controls of $('messages').querySelectorAll('.approval-actions'))controls.remove();
    message('user',waiting.kind==='manual'?'Completed manual website step.':completedInBrowser?'Completed website information step.':text);listen(result.run_id,result.after||0);
  }catch(e){waiting.busy=false;error(e);}
}
async function cancelInput(waiting){
  if(pendingInput!==waiting||waiting.cancelled)return;
  return cancelBrowserTask(waiting.run);
}
async function cancelBrowserTask(run){
  const button=$('browser-cancel-task');if(!run||button.disabled)return;
  button.disabled=true;error(null);const currentSession=session;
  ++navigation;feed?.close();feed=null;
  if(pendingInput){pendingInput.cancelled=true;pendingInput.busy=true;}
  clearBrowser();
  try{await api('/runs/'+encodeURIComponent(run)+'/cancel','POST',{});if(session===currentSession)await openSession(currentSession,{refresh:true});}
  catch(e){if(session===currentSession){await openSession(currentSession,{refresh:true});error(e);}}
  finally{button.disabled=false;}
}
async function takeover(operation,details={}){
  const waiting=pendingInput;
  if(!waiting?.takeover||waiting.busy)return;
  waiting.queue??=new BrowserInputQueue(async(operation,details)=>{
    if(pendingInput!==waiting||waiting.cancelled)return;
    const focus=await waiting.control.send(operation,details);
    if(pendingInput===waiting&&!focus.cancelled){
      error(null);
      const keyboard=$('website-keyboard');keyboard.inputMode=focus.input_mode==='number'?'numeric':focus.input_mode||'text';
      if(focus.editable&&['click','pointerup','key'].includes(operation))keyboard.focus({preventScroll:true});
      else if(!focus.editable&&document.activeElement===keyboard)$('browser-frame').focus({preventScroll:true});
    }
  });
  const action=waiting.queue.send(operation,details);
  // Drain input in order, but do not make Continue rethrow an old failed key.
  // Its server-side readiness check must inspect the actual website instead.
  takeoverTail=action.catch(()=>{});return action;
}
$('browser-frame').tabIndex=0;
let heldPointer=null;
$('browser-frame').onpointerdown=e=>{
  if(!pendingInput?.takeover||e.button!==0||heldPointer)return;
  e.preventDefault();
  try{
    const point=pendingInput.control.point(e);if(!point)return;
    heldPointer={id:e.pointerId,point,touch:e.pointerType==='touch',startX:e.clientX,startY:e.clientY,lastX:e.clientX,lastY:e.clientY,moved:false};e.currentTarget.setPointerCapture(e.pointerId);
    if(!heldPointer.touch){$('website-keyboard').focus({preventScroll:true});takeover('pointerdown',point).catch(error);}
  }catch(e){error(e);}
};
function scrollWebsite(x,y,point){
  return takeover('scroll',{x,y,pointer_x:point.x,pointer_y:point.y,width:point.width,height:point.height});
}
$('browser-frame').onpointermove=e=>{
  const held=heldPointer;if(!held?.touch||held.id!==e.pointerId)return;
  e.preventDefault();
  if(!held.moved&&Math.hypot(e.clientX-held.startX,e.clientY-held.startY)<8)return;
  const rect=$('browser-frame').getBoundingClientRect();
  const scale=1/Math.min(rect.width/pendingInput.control.width,rect.height/pendingInput.control.height);
  scrollWebsite((held.lastX-e.clientX)*scale,(held.lastY-e.clientY)*scale,held.point).catch(error);
  held.lastX=e.clientX;held.lastY=e.clientY;held.moved=true;
};
function releaseWebsitePointer(e,cancel=false){
  if(!heldPointer||e&&e.pointerId!==heldPointer.id)return;
  const held=heldPointer;heldPointer=null;
  try{if($('browser-frame').hasPointerCapture(held.id))$('browser-frame').releasePointerCapture(held.id);}catch{}
  if(held.touch){
    if(!cancel&&!held.moved){
      // Mobile browsers require keyboard focus inside the actual tap gesture,
      // before the network acknowledgement. Swipes never open the keyboard.
      $('website-keyboard').focus({preventScroll:true});takeover('click',held.point).catch(error);
    }
    return;
  }
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
$('browser-frame').addEventListener('wheel',e=>{
  if(!pendingInput?.takeover)return;e.preventDefault();e.stopPropagation();
  try{
    const point=pendingInput.control.point(e);if(!point)return;
    const unit=e.deltaMode===1?16:e.deltaMode===2?pendingInput.control.height:1;
    scrollWebsite(e.deltaX*unit,e.deltaY*unit,point).catch(error);
  }catch(e){error(e);}
},{passive:false});
// The invisible keyboard sink supports mobile keyboards and IME. It clears
// immediately; users see and edit only the actual website textbox in the stream.
let composing=false,ignoreCompositionInput=false,compositionText='';
const websiteKeyboard=$('website-keyboard');
// A website may autofocus its login field while the local document still owns
// keyboard focus (the visible cursor belongs to the remote browser). Forward
// page-level typing during takeover, never keys from chat or other controls.
document.addEventListener('keydown',e=>{
  if(!pendingInput?.takeover||![document.body,document.documentElement].includes(e.target))return;
  if(e.isComposing)return;
  websiteKeyboard.focus({preventScroll:true});browserKey(e);
});
document.addEventListener('paste',e=>{
  if(!pendingInput?.takeover||![document.body,document.documentElement].includes(e.target))return;
  const text=e.clipboardData?.getData('text/plain');if(!text)return;
  e.preventDefault();websiteKeyboard.focus({preventScroll:true});takeover('type',{text}).catch(error);
});
websiteKeyboard.addEventListener('compositionstart',()=>{composing=true;});
websiteKeyboard.addEventListener('compositionend',e=>{composing=false;ignoreCompositionInput=true;compositionText=e.data;websiteKeyboard.value='';if(compositionText)takeover('type',{text:compositionText}).catch(error);setTimeout(()=>{ignoreCompositionInput=false;compositionText='';},0);});
websiteKeyboard.addEventListener('input',e=>{if(composing||e.isComposing)return;const text=websiteKeyboard.value;websiteKeyboard.value='';if(text&&!(ignoreCompositionInput&&text===compositionText))takeover('type',{text}).catch(error);});
websiteKeyboard.addEventListener('beforeinput',e=>{
  if(e.inputType==='insertLineBreak'){e.preventDefault();takeover('key',{key:'Enter'}).catch(error);}
  else if(e.inputType==='deleteContentBackward'||e.inputType==='deleteContentForward'){e.preventDefault();takeover('key',{key:e.inputType==='deleteContentBackward'?'Backspace':'Delete'}).catch(error);}
});
websiteKeyboard.addEventListener('paste',e=>{e.preventDefault();const text=e.clipboardData?.getData('text/plain');if(text)takeover('type',{text}).catch(error);});
function stopBrowserControl(){heldPointer=null;pendingInput?.queue?.close();pendingInput?.control?.close();websiteKeyboard.value='';for(const controls of $('browser-view').querySelectorAll('.browser-controls'))controls.remove();}
$('browser-take-control').onclick=async()=>{
  const button=$('browser-take-control');button.disabled=true;error(null);
  try{
    if(pendingInput?.run===button.dataset.run&&pendingInput.startControl)await pendingInput.startControl();
    else{await api('/runs/'+encodeURIComponent(button.dataset.run)+'/browser/take-control','POST',{});await openSession(session,{refresh:true});}
  }
  catch(e){error(e);}finally{button.disabled=false;}
};
$('browser-cancel-task').onclick=()=>cancelBrowserTask($('browser-cancel-task').dataset.run);
async function newChat() {
  const generation=++navigation;
  clearBrowser();pendingApproval=null;
  feed?.close();feed=null;$('input').disabled=true;$('new').disabled=true;
  try{const row=await api('/sessions','POST',{});if(generation!==navigation)return;await openSession(row.id);}
  finally{$('input').disabled=false;$('new').disabled=false;}
}
function listen(run, after=0,previewRun=null) {
  if($('browser-frame').dataset.url&&$('browser-frame').dataset.url!=='/api/runs/'+encodeURIComponent(run)+'/browser/frame'&&!previewRun)clearBrowser();
  watchBrowser(run,true,previewRun);
  feed?.close();answer=null;liveText='';$('status').textContent='Thinking…';
  const source=new EventSource('/api/runs/'+encodeURIComponent(run)+'/events?after='+after);feed=source;
  const tripSections=new Map();
  source.addEventListener('trip_section',e=>{if(feed!==source)return;const section=JSON.parse(e.data);tripSections.set(section.key,section);liveText=[...tripSections.values()].sort((a,b)=>a.order-b.order).map(s=>s.text).join('\n\n');if(!answer)answer=message('assistant','');updateAnswer(answer,liveText);});
  source.addEventListener('token',e=>{if(feed!==source||tripSections.size)return;liveText+=JSON.parse(e.data);if(!answer)answer=message('assistant','');updateAnswer(answer,liveText);});
  source.addEventListener('progress',e=>{if(feed===source)$('status').textContent=JSON.parse(e.data);});
  source.addEventListener('response_reset',()=>{if(feed!==source)return;tripSections.clear();liveText='';if(answer){answer.remove();answer=null;}});
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
$('browser-view').ontoggle=()=>{if(pendingInput?.takeover&&!$('browser-view').open){$('browser-view').open=true;return;}if($('browser-view').open&&$('browser-frame').dataset.url&&!pendingInput?.control)$('browser-frame').src=$('browser-frame').dataset.url+'?t='+Date.now();};
$('auth').onclick=async()=>{try{if(account){await api('/logout','POST',{});signedOut();}else $('login-form').requestSubmit();}catch(e){error(e);}};
$('login-form').onsubmit=async e=>{e.preventDefault();try{const me=await api('/login','POST',{username:$('username').value,password:$('password').value});$('current-password').value=$('password').value;$('password').value='';await entered(me);}catch(e){error(e);}};
$('password-form').onsubmit=async e=>{e.preventDefault();try{await api('/password','POST',{current_password:$('current-password').value,new_password:$('new-password').value});$('current-password').value='';$('new-password').value='';await entered({...account,must_change:false});}catch(e){error(e);}};
for(const id of ['username','password','current-password','new-password'])$(id).addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();e.target.form.requestSubmit();}});
$('input').oninput=()=>{$('input').style.height='auto';$('input').style.height=Math.min($('input').scrollHeight,170)+'px';};
$('input').addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files'))e.preventDefault();});
let uploadBusy=false;
function clearDraftFiles(){for(const item of draftFiles)if(item.preview)URL.revokeObjectURL(item.preview);draftFiles=[];renderDraftFiles();}
function renderDraftFiles(){
  const tray=$('draft-attachments');tray.replaceChildren();tray.hidden=!draftFiles.length;
  for(const item of draftFiles){
    const card=document.createElement('div');card.className='draft-file';
    if(item.preview){const image=document.createElement('img');image.src=item.preview;image.alt=item.file.name;card.append(image);}
    else{const icon=document.createElement('span');icon.className='draft-file-icon';icon.textContent='File';card.append(icon);}
    const name=document.createElement('span');name.textContent=item.file.name;name.title=item.file.name;card.append(name);
    const remove=document.createElement('button');remove.type='button';remove.textContent='×';remove.setAttribute('aria-label','Remove '+item.file.name);remove.disabled=uploadBusy;
    remove.onclick=()=>{if(item.preview)URL.revokeObjectURL(item.preview);draftFiles=draftFiles.filter(f=>f!==item);renderDraftFiles();$('input').focus();};card.append(remove);tray.append(card);
  }
}
function stageFiles(files){
  if(uploadBusy)return;
  error(null);
  try{
    if(!account||!session)throw new Error('Log in and open a chat first.');
    if(draftFiles.length+files.length>10)throw new Error('Attach up to ten files at a time.');
    for(const file of files){
      const image=['image/png','image/jpeg','image/webp','image/gif'].includes(file.type);
      if(file.size>(image?20000000:50000000))throw new Error(image?'Images must be at most 20 MB.':'Each file must be at most 50 MB.');
      draftFiles.push({file,preview:image?URL.createObjectURL(file):null,result:null});
    }
  }catch(e){error(e);}
  renderDraftFiles();$('input').focus();
}
async function uploadDraftFiles(destination){
  for(const item of draftFiles){
    if(item.result)continue;
    $('status').textContent='Uploading '+item.file.name+'…';
    const response=await fetch('/api/uploads?draft=true&session_id='+encodeURIComponent(destination),{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Filename':encodeURIComponent(item.file.name)},body:item.file});
    if(!response.ok)throw new Error('Upload failed: '+await response.text());
    item.result=await response.json();
  }
  return draftFiles.map(item=>item.result.id);
}
$('attach').onclick=()=>{if(!uploadBusy&&!$('input').disabled)$('file-picker').click();};
$('file-picker').onchange=()=>{
  const files=Array.from($('file-picker').files||[]);
  $('file-picker').value='';
  stageFiles(files);
};
$('input').addEventListener('drop',e=>{
  const files=Array.from(e.dataTransfer?.files||[]);if(!files.length)return;e.preventDefault();
  stageFiles(files);
});
$('input').onkeydown=async e=>{
  if(e.key!=='Enter'||e.shiftKey||e.isComposing)return;e.preventDefault();const text=$('input').value.trim();if(!text||uploadBusy)return;
  $('input').value='';$('input').style.height='auto';error(null);
  try{
    if(!draftFiles.length&&text.startsWith('/open ')){await openSession(text.slice(6).trim());return;}
    if(!draftFiles.length&&pendingApproval&&/^(approve|cancel|reject)$/i.test(text)){await respondToApproval(pendingApproval,text.toLowerCase()==='approve'?'approve':'reject');return;}
    if(!draftFiles.length&&pendingInput&&/^(?:cancel|stop|\/stop)$/i.test(text)){await cancelInput(pendingInput);return;}
    const inputId=draftFiles.length?null:pendingInput?.id;
    // Hide the prior page immediately. The server restores it only when this
    // prompt is explicitly related to that browser context.
    clearBrowser();
    // Reconnect after submission: same-chat running tasks keep their run ID,
    // while explicit cancellation or switching chats can retire the old stream.
    ++navigation;feed?.close();feed=null;uploadBusy=true;$('attach').disabled=true;$('input').disabled=true;$('new').disabled=true;renderDraftFiles();
    try{
      const attachmentIds=await uploadDraftFiles(session);
      await api('/runs','POST',{session_id:session,text,pending_input_id:inputId||null,attachment_ids:attachmentIds});
      clearDraftFiles();
      pendingApproval=null;await openSession(session,{refresh:true});
    }catch(e){await openSession(session,{refresh:true});throw e;}
    finally{uploadBusy=false;$('attach').disabled=false;$('input').disabled=false;$('new').disabled=false;renderDraftFiles();}

  }catch(e){error(e);if(account)$('input').value=text;}
};
async function loadBrowserMode(){
  const setting=$('browser-mode-setting'),select=$('browser-mode');
  setting.hidden=true;
  try{
    const current=await api('/browser/mode');
    select.value=current.mode;select.dataset.saved=current.mode;setting.hidden=false;
  }catch{}
}
$('browser-mode').addEventListener('change',async event=>{
  const select=event.currentTarget;select.disabled=true;error(null);
  try{
    const current=await api('/browser/mode','POST',{mode:select.value});
    select.value=current.mode;select.dataset.saved=current.mode;clearBrowser();
    $('status').textContent=current.mode==='visible'?'Visible selected. Your next browser task opens Chrome.':'Headless selected for your next browser task.';
  }catch(e){select.value=select.dataset.saved;error(e);}
  finally{select.disabled=false;}
});
api('/me').then(entered).catch(()=>signedOut());
new ResizeObserver(()=>{document.documentElement.style.setProperty('--composer-space',($('composer').hidden?170:$('composer').offsetHeight+32)+'px');}).observe($('composer'));
