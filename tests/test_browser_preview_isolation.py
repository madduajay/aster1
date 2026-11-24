from types import SimpleNamespace
from unittest.mock import AsyncMock
import shutil
import subprocess
import asyncio
import os

import pytest

from personal_agent.browser_host import BrowserHost
from personal_agent import config


def setup(database,users,same_chat=False,related=False,retired=False):
    user=users[0]['id'];old=database.new_session(user);new=old if same_chat else database.new_session(user)
    for run,chat in [('old-run',old),('new-run',new)]:
        database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'completed',0)",(run,user,chat))
    host=BrowserHost(database);host.contexts[user]=SimpleNamespace(pages=[])
    host.set_scope(user,'old-run',['example.com']);host.set_scope(user,'new-run',['example.com'])
    host.run_for_user[user]='old-run'
    host.set_view(user,'old-run',{'url':'https://example.com/old','target_id':'old-tab'})
    host.views[(user,'old-run')]['frame']=b'old-chat-frame'
    if related:database.event('new-run','browser_context',{'source_run':'old-run','target_id':'old-tab'})
    if retired:host.retire_view(user,'old-run')
    host.prepare_view(user,'new-run');host.run_for_user[user]='new-run'
    return host,user


async def test_new_chat_hides_old_page_even_on_same_domain_until_navigation(database,users):
    host,user=setup(database,users)
    host.set_view(user,'new-run',{'url':'https://example.com/old','target_id':'old-tab'})
    assert not host.view_info(user,'new-run')['available']
    assert await host.preview(user,'new-run') is None
    assert host.views[(user,'new-run')]['url']==''
    host.activate_view(user,'new-run',{'url':'https://other.example/','target_id':'wrong-tab'})
    assert not host.view_info(user,'new-run')['available']
    host.activate_view(user,'new-run',{'url':'https://example.com/new','target_id':'new-tab'})
    assert host.view_info(user,'new-run')['available']
    assert host.view_info(user,'new-run')['target_id']=='new-tab'
    assert host.views[(user,'old-run')]['frame']==b'old-chat-frame'


async def test_unbound_run_never_captures_arbitrary_previous_tab(database,users):
    host,user=setup(database,users,same_chat=True)
    host.views[(user,'new-run')].pop('waiting_for_navigation',None)
    screenshot=AsyncMock(side_effect=AssertionError('Unbound page must not be captured'))
    host.contexts[user].pages=[SimpleNamespace(url='https://example.com/old',is_closed=lambda:False,screenshot=screenshot)]
    assert not host.view_info(user,'new-run')['available']
    assert await host.preview(user,'new-run') is None
    screenshot.assert_not_awaited()


def test_same_chat_followup_can_keep_its_relevant_page(database,users):
    host,user=setup(database,users,same_chat=True,related=True)
    host.set_view(user,'new-run',{'url':'https://example.com/old','target_id':'old-tab'})
    assert host.view_info(user,'new-run')['available']


@pytest.mark.parametrize('retired',[False,True])
async def test_unrelated_task_in_same_chat_starts_on_fresh_tab(database,users,retired):
    host,user=setup(database,users,same_chat=True,retired=retired)
    old=SimpleNamespace(url='https://example.com/account',name='old')
    new=SimpleNamespace(url='about:blank',name='new')
    client=SimpleNamespace(send=AsyncMock(return_value={'targetInfo':{'targetId':'fresh-tab'}}),detach=AsyncMock())
    host.contexts[user]=SimpleNamespace(pages=[old],new_page=AsyncMock(return_value=new),new_cdp_session=AsyncMock(return_value=client))
    assert await host.prepare_task_page(user,'new-run')=='fresh-tab'
    host.contexts[user].new_page.assert_awaited_once()
    host.contexts[user].new_cdp_session.assert_awaited_once_with(new)
    host.set_view(user,'new-run',{'url':old.url,'target_id':'old-tab'})
    assert not host.view_info(user,'new-run')['available']


async def test_followup_and_resumption_bind_current_target_not_original_tab(database,users):
    host,user=setup(database,users,same_chat=True,related=True)
    assert await host.prepare_task_page(user,'new-run')=='old-tab'
    host.views[(user,'new-run')]['initial_target_id']='outdated-start-tab'
    host.set_view(user,'new-run',{'url':'https://example.com/new','target_id':'current-tab'})
    assert await host.prepare_task_page(user,'new-run')=='current-tab'


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real Browser Use tab binding in isolated profile')
async def test_browser_use_attaches_to_new_task_tab_before_observing_old_login(database,users,monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    host,user=setup(database,users,same_chat=True,retired=True)
    host.contexts.clear()
    try:
        context=await host.context(user)
        old=context.pages[0]
        await old.set_content('<h1>Previous account</h1><input type="password">')
        target=await host.prepare_task_page(user,'new-run')
        assert len(context.pages)==2
        endpoint=await host.cdp_endpoint(user)
        code='''import asyncio,sys
from browser_use import Browser
async def main():
 b=Browser(cdp_url=sys.argv[1],keep_alive=True,enable_default_extensions=False,allowed_domains=['*'],highlight_elements=False)
 try:
  await b.start()
  await b.get_or_create_cdp_session(target_id=sys.argv[2],focus=True)
  await b.start()
  assert b.agent_focus_target_id==sys.argv[2], 'Browser Use selected the old task tab'
  print('TASK_TARGET_OK')
 finally:await b.stop()
asyncio.run(main())'''
        env={k:v for k,v in os.environ.items() if not k.endswith('API_KEY') and k not in {'OPENAI_BASE_URL','OPENAI_API_BASE','OPENAI_PROXY','PERSONAL_AGENT_KEY_FD'}}
        env.update(ANONYMIZED_TELEMETRY='false',BROWSER_USE_CLOUD_SYNC='false',BROWSER_USE_LOGGING_LEVEL='critical')
        proc=await asyncio.create_subprocess_exec(str(config.ROOT/'.runtime/browser-use-venv/bin/python'),'-c',code,endpoint,target,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env)
        try:out,err=await asyncio.wait_for(proc.communicate(),45)
        except BaseException:
            if proc.returncode is None:proc.kill()
            await proc.wait();raise
        assert proc.returncode==0,err.decode()[-1600:]
        assert b'TASK_TARGET_OK' in out
    finally:await host.close()


@pytest.mark.skipif(shutil.which('node') is None,reason='Node executes the browser event handlers')
def test_new_chat_clears_preview_before_request_and_ignores_late_old_frames():
    script=r'''
    const fs=require('fs'),vm=require('vm'),assert=require('assert');
    const source=fs.readFileSync('static/app.js','utf8');
    const watch=source.slice(source.indexOf('function watchBrowser('),source.indexOf('async function api('));
    const create=source.slice(source.indexOf('async function newChat('),source.indexOf('function listen('));
    let finishOld,finishNew,shown=true,opened=null;
    const old=new Promise(r=>finishOld=r),fresh=new Promise(r=>finishNew=r);
    const nodes={input:{},new:{}};
    const context={browserWatch:0,navigation:0,browserTimer:null,pendingApproval:null,pendingInput:null,
      feed:{close(){}},clearTimeout,setTimeout,
      $:id=>nodes[id],clearBrowser(){shown=false;context.browserWatch++;},
      api:path=>path==='/sessions'?fresh:old,
      openSession:async id=>{opened=id;},
    };
    vm.createContext(context);vm.runInContext(watch+create,context);
    context.watchBrowser('old-run',false);
    const creating=context.newChat();
    assert.equal(shown,false);assert.equal(opened,null);
    assert.equal(nodes.input.disabled,true);
    finishOld({available:true,can_take_control:true,url:'https://old.example/'});
    (async()=>{
      await Promise.resolve();await Promise.resolve();
      assert.equal(shown,false);assert.equal(opened,null);
      finishNew({id:'new-chat'});await creating;
      assert.equal(opened,'new-chat');assert.equal(shown,false);
    })().catch(e=>{console.error(e);process.exitCode=1;});
    '''
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


@pytest.mark.skipif(shutil.which('node') is None,reason='Node executes the browser event handlers')
def test_new_prompt_hides_preview_before_post_and_preserves_pending_reply():
    script=r'''
    const fs=require('fs'),vm=require('vm'),assert=require('assert');
    const source=fs.readFileSync('static/app.js','utf8');
    const handler=source.slice(source.indexOf("$('input').onkeydown=async e=>{"),source.indexOf('async function loadBrowserMode'));
    (async()=>{
      for(const pending of [null,{id:'pending-input'}]){
        let shown=true,posted=null,release;
        const blocked=new Promise(resolve=>{release=resolve;});
        const elements={input:{value:'Open a different hotel',style:{}},attach:{},new:{}};
        const context={draftFiles:[],uploadBusy:false,pendingInput:pending,pendingApproval:null,
          navigation:0,feed:null,session:'same-chat',account:{},$:id=>elements[id],error:e=>{if(e)throw e;},
          clearBrowser(){shown=false;context.pendingInput=null;},renderDraftFiles(){},clearDraftFiles(){},
          uploadDraftFiles:async()=>[],openSession:async()=>{},
          api:async(path,method,body)=>{assert.equal(shown,false);posted={path,method,body};await blocked;},
        };
        vm.createContext(context);vm.runInContext(handler,context);
        const running=elements.input.onkeydown({key:'Enter',preventDefault(){}});
        assert.equal(shown,false);assert.equal(elements.input.disabled,true);
        await new Promise(resolve=>setImmediate(resolve));
        assert.equal(posted.path,'/runs');assert.equal(posted.body.session_id,'same-chat');
        assert.equal(posted.body.pending_input_id,pending?.id||null);
        release();await running;assert.equal(elements.input.disabled,false);
      }
    })().catch(e=>{console.error(e);process.exitCode=1;});
    '''
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
