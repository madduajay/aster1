"""Capped live semantic retention check on isolated synthetic conversations.

Two GPT-4.1 mini calls at most, no web search, no browser, no production writes.
The Keychain credential stays in process memory. Report contains no credentials.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from personal_agent import config
from personal_agent.db import Database
from personal_agent.memory import Memory


async def main():
    production_runtime = config.RUNTIME
    key = config.keychain_key()
    model = ChatOpenAI(model=config.MODEL, api_key=key, base_url='https://api.openai.com/v1',
                       max_tokens=2500, max_retries=0, timeout=45, temperature=0, store=False)
    del key
    with tempfile.TemporaryDirectory(prefix='retention-live-', dir=production_runtime) as folder:
        config.RUNTIME = Path(folder)
        db = Database()
        db.execute("INSERT INTO users(id,username,password,must_change) VALUES('synthetic','synthetic','disabled',0)")
        sid = db.new_session('synthetic')
        positive = [
            ('my favorite color is white', 'white'),
            ('my favority pet is dog', 'dog'),
            ('I am a mechanical engineer', 'engineer'),
            ('My favorite instrument is cello', 'cello'),
        ]
        ids = []
        for text, _ in positive:
            ids.append(db.message('synthetic', sid, 'user', text))
            db.message('synthetic', sid, 'assistant', 'Thanks for sharing.')
        forbidden_sources = []
        for text in [
            'Find white shoes for tomorrow under $100',
            'I want a hotel in Boston for Friday',
            'Indian please',
            'What is my favorite animal?',
            "My brother's favorite color is orange",
            'Pretend my favorite drink is ginger ale',
            'My favorite snack is pretzels. Do not remember this',
            'My phone number is 202-555-0104',
        ]:
            forbidden_sources.append(db.message('synthetic', sid, 'user', text))
            db.message('synthetic', sid, 'assistant', 'Acknowledged for this chat.')
        forbidden_sources.append(db.message('synthetic', sid, 'assistant', 'I infer your favorite fabric is velvet.'))
        memory = Memory(db, model)
        async with asyncio.timeout(55):
            await memory.summarize('synthetic', sid)
        facts = db.facts('synthetic', inspect=True)
        assert all(f['source_message'] in ids for f in facts), 'Session-only or unsupported fact was promoted'
        for mid, (_, expected) in zip(ids, positive):
            assert any(f['source_message']==mid and expected in f['text'].casefold() for f in facts), 'A stable disclosure was missed'
        fresh = db.new_session('synthetic')
        context = Memory(Database(db.path), None).context('synthetic', fresh, 'what are my favorite color and pet')
        assert not context['summary'] and not context['recent_messages'] and not context['unsummarized_messages']
        answer = await model.bind(max_tokens=180).ainvoke([
            SystemMessage(content='Answer the personal question only from long_term_facts. Do not invent preferences.'),
            HumanMessage(content=json.dumps({'question':'What are my favorite color and pet?', 'context':context})),
        ])
        assert 'white' in answer.content.casefold() and 'dog' in answer.content.casefold()
        report = {'passed':True,'model':config.MODEL,'live_model_calls':2,
                  'source_ids_promoted':[f['source_message'] for f in facts],
                  'stable_disclosures_saved':len(positive),'unsupported_sources_rejected':len(forbidden_sources),
                  'restart_and_new_chat_recall':True,'web_searches':0,'browser_actions':0,
                  'production_records_modified':False}
        config.RUNTIME = production_runtime
        (production_runtime/'memory-retention-live.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report))


if __name__ == '__main__':
    asyncio.run(main())
