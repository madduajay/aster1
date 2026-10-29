"""One capped structured-output model call; no user data or website action."""
import asyncio,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


async def main():
    from pydantic import BaseModel
    from browser_use import ChatOpenAI
    from browser_use.llm.messages import UserMessage
    from personal_agent import config
    from personal_agent.browser_model import browser_model_options
    class Check(BaseModel):
        ok: bool
    report={'passed':False,'model':config.BROWSER_MODEL,'generation_call_limit':1}
    try:
        model=ChatOpenAI(model=config.BROWSER_MODEL,api_key=config.keychain_key(),
            base_url='https://api.openai.com/v1',timeout=30,max_retries=0,max_completion_tokens=512,
            **browser_model_options(config.BROWSER_MODEL))
        result=await model.ainvoke([UserMessage(content='Return ok=true in the required schema. This is a compatibility check.')],output_format=Check)
        report['passed']=result.completion.ok
        report['generation_calls']=1
    except Exception as exc:
        report['error_class']=type(exc).__name__
        report['status_code']=getattr(exc,'status_code',None)
    (config.RUNTIME/'terra-probe.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
