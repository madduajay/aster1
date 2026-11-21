'use strict';
let backgroundTimer=null,backgroundGeneration=0,backgroundSignature='',backgroundSeen=new Set();
const backgroundDrafts=new Map();
function backgroundText(tag,text,parent,className=''){
  const node=document.createElement(tag);node.textContent=text;if(className)node.className=className;parent.append(node);return node;
}
function backgroundButton(label,parent,action){
  const button=backgroundText('button',label,parent);button.type='button';button.onclick=()=>action().catch(error);return button;
}
async function dismissBackgroundAlert(id){
  await api('/background/notifications/'+id+'/read','POST',{});
  if($('background-toast').dataset.notice===String(id))$('background-toast').hidden=true;
  await refreshBackground();
}
function appendBackgroundChat(rows=[]){
  const container=$('messages');
  const known=new Set([...container.children].map(node=>node.dataset.messageId));
  const stick=atBottom();let appended=false;
  for(const row of [...rows].sort((a,b)=>a.id-b.id)){
    if(row.session_id!==session||known.has(String(row.id)))continue;
    const node=message('assistant',row.content,false);node.dataset.messageId=row.id;
    const next=[...container.children].find(other=>other!==node&&(
      Number(other.dataset.messageId)>row.id||other===answer&&!other.dataset.messageId));
    if(next)container.insertBefore(node,next);
    known.add(String(row.id));appended=true;
  }
  if(appended&&stick)scrollLatest();
}
function renderBackground(data){
  appendBackgroundChat(data.chat_updates);
  const unread=data.notifications.filter(n=>!n.read_at);
  $('alerts').hidden=!unread.length&&!data.watches.some(w=>['needs_details','active'].includes(w.status))&&!(data.monitors||[]).length;
  $('alerts').dataset.unread=String(unread.length);
  $('alerts').textContent=unread.length?'Alerts · '+unread.length:'Background tasks';
  const fresh=unread.find(n=>!backgroundSeen.has(n.id));
  if(fresh){$('background-toast').textContent=fresh.title+': '+fresh.body;$('background-toast').dataset.notice=String(fresh.id);$('background-toast').hidden=false;}
  if(!unread.some(n=>String(n.id)===$('background-toast').dataset.notice))$('background-toast').hidden=true;
  for(const notice of data.notifications)backgroundSeen.add(notice.id);
  const signature=JSON.stringify(data);if(signature===backgroundSignature)return;
  const panel=$('background-panel');
  for(const form of panel.querySelectorAll('form'))backgroundDrafts.set(form.dataset.watch,Object.fromEntries(new FormData(form)));
  const focus=document.activeElement?.closest('form')?{watch:document.activeElement.form.dataset.watch,name:document.activeElement.name}:null;
  panel.replaceChildren();backgroundSignature=signature;
  backgroundText('h2','Background tasks',panel);
  const memory=backgroundText('div','',panel,'background-card');backgroundText('h3','Memory consolidation',memory);
  backgroundText('p',data.profile?data.profile.summary:'No current consolidated profile yet. Existing long-term facts remain available.',memory);
  backgroundText('p',data.memory_review?'Last review: '+data.memory_review.status+' · '+new Date(data.memory_review.created*1000).toLocaleString():'Reviews run while you are idle and repeat only when memory changes.',memory,'background-note');
  backgroundButton('Review memory',memory,async()=>{await api('/background/memory','POST',{});await refreshBackground();});
  for(const monitor of data.monitors||[]){
    const card=backgroundText('div','',panel,'background-card');backgroundText('h3','Availability monitor · '+monitor.state,card);
    const interval=Math.max(60,monitor.interval_seconds||300);backgroundText('p','Every '+(interval%60===0?interval/60+' minute'+(interval===60?'':'s'):interval+' seconds'),card);
    backgroundText('p',(monitor.queries||[]).map(q=>{const c=q.criteria;return (c.venue||c.hotel||c.carrier||c.destination||q.domain)+' · '+(c.date||c.check_in||c.departure_date||'')+(q.state==='expired'?' (expired)':'');}).join('; '),card);
    backgroundText('p','Last check: '+(monitor.last_check?new Date(monitor.last_check*1000).toLocaleString():'Pending')+' · Next check: '+(monitor.next_check?new Date(monitor.next_check*1000).toLocaleString():'—'),card);
    if(['active','pending'].includes(monitor.state))backgroundButton('Cancel monitor',card,async()=>{await api('/background/monitors/'+monitor.id+'/cancel','POST',{});await refreshBackground();});
  }
  for(const watch of data.watches){
    const card=backgroundText('div','',panel,'background-card');backgroundText('h3',(watch.criteria.venue||watch.criteria.hotel||watch.criteria.carrier||watch.criteria.domain||'Dining')+' watch · '+watch.status,card);
    const c=watch.criteria;backgroundText('p',[c.location||c.destination,c.date||c.check_in||c.departure_date,c.party_size?c.party_size+' guests':''].filter(Boolean).join(' · '),card);
    if(watch.status==='needs_details'){
      const form=document.createElement('form');form.dataset.watch=watch.id;const draft=backgroundDrafts.get(watch.id)||{};
      for(const [name,label,type,value] of [['location','Location / city','text',c.location||''],['date','Date','date',c.date||''],['party_size','Party size','number',c.party_size||2]]){
        const wrap=backgroundText('label',label,form);const input=document.createElement('input');input.name=name;input.type=type;input.required=true;input.value=draft[name]??value;
        if(type==='number'){input.min='1';input.max='20';}else if(name==='location')input.maxLength=80;
        wrap.append(input);
      }
      const submit=backgroundText('button','Notify me when available',form);submit.type='submit';
      form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;
        try{const values=Object.fromEntries(new FormData(form));values.party_size=Number(values.party_size);
          const result=await api('/background/watches/'+watch.id,'POST',values);backgroundDrafts.delete(watch.id);
          if(result.session_id===session)await openSession(session,{refresh:true});await refreshBackground();
        }catch(e){error(e);submit.disabled=false;}};
      card.append(form);
    }
    if(['needs_details','active'].includes(watch.status))backgroundButton('Cancel '+(c.domain||'dining')+' watch',card,async()=>{await api('/background/watches/'+watch.id+'/cancel','POST',{});await refreshBackground();});
  }
  backgroundText('h3','Notifications',panel);
  if(!unread.length)backgroundText('p','No unread alerts.',panel,'background-note');
  for(const notice of unread){
    const card=backgroundText('div','',panel,'background-card unread');backgroundText('strong',notice.title,card);backgroundText('p',notice.body,card);
    backgroundText('p',new Date(notice.created*1000).toLocaleString(),card,'background-note');
    card.setAttribute('role','button');card.tabIndex=0;card.setAttribute('aria-label','Dismiss alert: '+notice.title);
    card.onclick=()=>dismissBackgroundAlert(notice.id).catch(error);
    card.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();card.click();}};
  }
  if(focus){const form=[...panel.querySelectorAll('form')].find(f=>f.dataset.watch===focus.watch);form?.elements.namedItem(focus.name)?.focus();}
}
async function refreshBackground(){
  clearTimeout(backgroundTimer);const generation=++backgroundGeneration,user=account?.username;
  if(!user)return;
  let delay=15000;
  try{const data=await api('/background');if(generation!==backgroundGeneration||account?.username!==user)return;
    renderBackground(data);if(data.watches.some(w=>w.status==='active')||(data.monitors||[]).some(m=>m.state==='active'))delay=1000;
  }catch{if(generation!==backgroundGeneration||account?.username!==user)return;}
  backgroundTimer=setTimeout(refreshBackground,delay);
}
window.refreshBackground=refreshBackground;
window.stopBackgroundUI=()=>{backgroundGeneration++;clearTimeout(backgroundTimer);backgroundTimer=null;backgroundSignature='';backgroundSeen.clear();backgroundDrafts.clear();$('alerts').hidden=true;$('background-panel').hidden=true;$('background-toast').hidden=true;};
$('alerts').onclick=async()=>{
  const button=$('alerts');button.disabled=true;
  try{
    if(Number(button.dataset.unread)>0){
      await api('/background/notifications/read','POST',{});
      $('background-toast').hidden=true;$('background-panel').hidden=true;
      button.setAttribute('aria-expanded','false');
    }else{
      $('background-panel').hidden=!$('background-panel').hidden;
      button.setAttribute('aria-expanded',String(!$('background-panel').hidden));
    }
    await refreshBackground();
  }catch(e){error(e);}finally{button.disabled=false;}
};
$('background-toast').setAttribute('role','button');$('background-toast').setAttribute('aria-live','polite');$('background-toast').tabIndex=0;
$('background-toast').onclick=()=>dismissBackgroundAlert($('background-toast').dataset.notice).catch(error);
$('background-toast').onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();$('background-toast').click();}};
if(account)refreshBackground();
