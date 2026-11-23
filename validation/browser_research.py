"""Real-model linked-page reading across independent website fixtures."""
import asyncio,json,os,sys,tempfile,time
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,str(Path.cwd()))
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from personal_agent import config,browser_host as hm,browser_tasks as tm
from personal_agent.browser_host import BrowserHost
from personal_agent.browser_tasks import BrowserTasks
from personal_agent.agent import Engine
from personal_agent.db import Database
from personal_agent.service import Service
from personal_agent.memory import Memory
from validation.navigation_acceptance import NoSummaryCalls

HUB='<title>Card benefits</title><main><h1>Card benefits</h1><a href="#benefit-card">Benefits by Card</a><section id="benefit-card"><h2>Find benefits by card</h2><a href="/cards/amber">Amber Card benefits</a><a href="/cards/jade">Jade Card benefits</a><a href="/cards/silver">Silver Card benefits</a></section></main>'
CARDS={'amber':'<h1>Amber Card benefits</h1><p>Earn 2 points per dollar on dining. Annual fee: $40. Includes purchase protection for 60 days.</p>',
       'jade':'<h1>Jade Card benefits</h1><p>Four airport lounge visits per year. Earn 3 points per dollar on travel. Annual fee: $90.</p>',
       'silver':'<h1>Silver Card benefits</h1><p>No annual fee. Earn 1 percent cash back on purchases. Includes rental car roadside assistance.</p>'}
CASES={
 'cards':{'host':'cards.fixture.test','hub_path':'/benefits','prefix':'/cards/',
          'hub':HUB,'details':CARDS,'title':'Card benefits','open':'Open the card benefits page',
          'question':'Summarize benefits by card, including the annual fee and rewards for each card.'},
 'docs':{'host':'reference.fixture.test','hub_path':'/reference','prefix':'/operations/',
         'hub':'<title>API retry reference</title><main><h1>API retry reference</h1><p>Select an operation to read its retry behavior.</p><a href="/operations/read">Read operation</a><a href="/operations/create">Create operation</a><a href="/operations/update">Update operation</a></main>',
         'details':{'read':'<h1>Read operation</h1><p>GET requests retry three times after HTTP 429. Delay: one second per retry.</p>',
                    'create':'<h1>Create operation</h1><p>POST requests have zero automatic retries. Repeating a create request requires an idempotency key.</p>',
                    'update':'<h1>Update operation</h1><p>PATCH requests retry twice after HTTP 429. Retry delays are two seconds then four seconds.</p>'},
         'title':'API retry reference','open':'Open the API retry reference',
         'question':'Summarize retry behavior by operation, including retry count and delay for each operation.'}}


CATALOG={name:'<h1>'+name.title()+' model specifications</h1><p>'+('Reference background. '*350)+'</p><p>Warranty: '+str(i+1)+' years. Capacity: '+str((i+1)*20)+' GB.</p>' for i,name in enumerate(['alder','birch','cedar','dune','elm','fern'])}
CASES['catalog']={'host':'catalog.fixture.test','hub_path':'/models','prefix':'/models/',
 'hub':'<title>Model specifications</title><main><h1>Model specifications</h1>'+''.join('<a href="/models/'+name+'">'+name.title()+' specifications</a>' for name in CATALOG)+'<footer><a href="/compare">Compare models</a><a href="/apply">Apply for a model program</a></footer></main>',
 'details':CATALOG,'title':'Model specifications','open':'Open model specifications',
 'question':'Summarize specifications by model for all six listed models, including warranty and capacity for each model.'}


CASES['shopping']={'host':'outfitters.fixture.test','hub_path':'/c/white-shirts','prefix':'/p/',
 'hub':'<title>Fixture Outfitters shirts</title><main><h1>Shirts</h1><button type="button" onclick="document.getElementById(\'colors\').hidden=false">Color</button><ul id="colors" hidden><li onclick="this.dataset.selected=\'true\';document.getElementById(\'filter-status\').textContent=\'White selected\'">White</li></ul><p id="filter-status">No color selected</p><button type="button" onclick="if(document.querySelector(\'li[data-selected=true]\')){document.getElementById(\'products\').hidden=false;document.getElementById(\'products\').innerHTML=\'<h2>White shirts</h2><a href=&quot;/p/alpha&quot;>White Shirt Alpha</a><a href=&quot;/p/beta&quot;>White Shirt Beta</a>\'}">Apply selected filters</button><section id="products" hidden></section></main>',
 'details':{name:'<meta property="og:title" content="White Shirt '+name.title()+'"><meta property="og:description" content="A white cotton shirt."><meta property="og:image" content="https://outfitters.fixture.test/images/'+name+'.png"><h1>White Shirt '+name.title()+'</h1><p>Color: white. Cotton fabric. Price: $'+str(price)+'. Sizes S, M, L.</p>' for name,price in [('alpha',40),('beta',50)]},
 'title':'White shirts','open':'Fixture Outfitters website is https://outfitters.fixture.test.',
 'question':'At Fixture Outfitters, use the Color filter to select White and apply the selected filters, then show me two white shirts with their product links and prices.','fresh':True}

class Host(BrowserHost):
 def __init__(self,db,case):
  super().__init__(db,headless=True);self.case=case
 async def context(self,user):
  fresh=user not in self.contexts;ctx=await super().context(user)
  if fresh:
   async def route(r):
    path=urlsplit(r.request.url).path
    case=self.case
    body=('<title>Item details</title><main>'+case['details'][path.split('/')[-1]]+'<a href="'+case['hub_path']+'">Back to overview</a></main>') if path.startswith(case['prefix']) and path.split('/')[-1] in case['details'] else case['hub']
    await r.fulfill(content_type='text/html',body=body)
   await ctx.route('https://'+self.case['host']+'/**',route)
  return ctx
async def main(scenario):
 case=CASES[scenario]
 production=config.RUNTIME;oldhost=hm.public_url;oldtask=tm.public_url
 os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(production/'playwright')
 key=config.keychain_key()
 async def public_fixture(url):
  if urlsplit(url).hostname!=case['host']:await oldhost(url)
 hm.public_url=tm.public_url=public_fixture
 from availability import media
 old_fetch=media.fetch
 if case.get('fresh'):
  import base64
  png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOuoAAAAASUVORK5CYII=')
  async def fixture_media(url,maximum):
   p=urlsplit(url)
   if p.hostname!=case['host']:raise ValueError('Outside fixture')
   return (png if p.path.startswith('/images/') else case['details'].get(p.path.rsplit('/',1)[-1],case['hub']).encode()),url
  media.fetch=fixture_media
 try:
  with tempfile.TemporaryDirectory(prefix='aster-linked-reading-') as tmp:
   config.RUNTIME=Path(tmp);db=Database();db.seed();uid=db.one("select id from users where username='user01'")['id'];sid=db.new_session(uid)
   host=Host(db,case)
   async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'checkpoints.sqlite')) as saver:
    engine=Engine(db,host,saver,key);service=Service(db,engine);service.memory=Memory(db,NoSummaryCalls())
    try:
     db.message(uid,sid,'user',case['open'])
     if not case.get('fresh'):
      db.execute("insert into runs(id,user_id,session_id,status,created) values('source',?,?,'completed',?)",(uid,sid,time.time()))
      db.message(uid,sid,'assistant','The overview is open.')
      saved={'status':'verified','verification':{'success':True},'url':'https://'+case['host']+case['hub_path'],'title':case['title']}
      db.execute("insert into browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created,result) values('source-task',?,'source','source-call',?,?,0,'completed',?,?)",(uid,case['open'],json.dumps([case['host']]),time.time(),json.dumps(saved)))
      host.set_scope(uid,'source',[case['host']]);host.run_for_user[uid]='source'
      page=(await host.context(uid)).pages[0];await page.goto(saved['url']);host.set_view(uid,'source',{'url':page.url,'title':case['title']})
     async def no_search(**kwargs):raise AssertionError('Linked detail pages supply the answer; web search must not run')
     engine.client.responses.create=no_search
     started=time.monotonic()
     rid=(await service.submit(uid,sid,case['question']))['run_id']
     deadline=time.monotonic()+210
     while service.active(uid):
      if service.active(uid)['status'] in {'approval','input'}:raise AssertionError('Read-only website browsing unexpectedly requested user input or approval')
      if time.monotonic()>deadline:raise TimeoutError('Linked read exceeded bounded check')
      await asyncio.sleep(.5)
     tasks=db.rows('select id,result,model_calls from browser_use_tasks where run_id=?',(rid,))
     seen=set();ops=[];status=[]
     for task in tasks:
      result=json.loads(task['result'] or '{}');status.append(result.get('status'))
      for a in db.rows('select operation,after_state from browser_use_actions where task_id=? order by seq',(task['id'],)):
       ops.append(a['operation']);state=json.loads(a['after_state'] or '{}')
       if case['prefix'] in state.get('url',''):seen.add(urlsplit(state['url']).path)
     searches=db.one('select searches from run_usage where run_id=?',(rid,))['searches']
     final=db.one("select content from messages where session_id=? and role='assistant' order by id desc limit 1",(sid,))['content']
     report={'status':db.one('select status from runs where id=?',(rid,))['status'],'task_statuses':status,'operations':ops,'visited_detail_pages':sorted(seen),'searches':searches,'elapsed_seconds':round(time.monotonic()-started,1),'browser_model_calls':sum(t['model_calls'] for t in tasks),'direct_reads':ops.count('read_page'),'extraction_calls':ops.count('extract'),'answer':final}
     (production/('browser-linked-reading-'+scenario+'-acceptance.json')).write_text(json.dumps(report,indent=2))
     print(json.dumps(report),flush=True)
     assert report['status']=='completed' and status[-1]=='verified' and searches==0
     assert seen=={case['prefix']+name for name in case['details']}
     if scenario=='shopping':assert ops.count('click')+ops.count('click_selector')>=3
     assert all(word in final.lower() for word in case['details'])
    finally:await service.close();await host.close();await engine.client.close()
 finally:config.RUNTIME=production;hm.public_url=oldhost;tm.public_url=oldtask;media.fetch=old_fetch
if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser();parser.add_argument('--scenario',choices=CASES,default='cards')
 asyncio.run(main(parser.parse_args().scenario))
