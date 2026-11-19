import pytest
import httpx2
from openai import AsyncOpenAI, AuthenticationError, RateLimitError, APIConnectionError

from personal_agent import config


def response_body():
    return {'id':'resp_test','object':'response','created_at':0,'status':'completed',
            'model':'gpt-5.4-mini','output':[]}


@pytest.mark.parametrize('transient_status',[429,503,'connection'])
async def test_transient_model_failures_retry_with_a_bound(monkeypatch,transient_status):
    attempts=[]
    async def handler(request):
        attempts.append(request.url.path)
        if len(attempts)<3:
            if transient_status=='connection':raise httpx2.ConnectError('Synthetic connection failure',request=request)
            return httpx2.Response(transient_status,json={'error':{'message':'Transient failure','type':'rate_limit_error'}})
        return httpx2.Response(200,json=response_body())
    monkeypatch.setattr(AsyncOpenAI,'_calculate_retry_timeout',lambda *a,**kw:0)
    async with AsyncOpenAI(api_key='sk-test',max_retries=config.MODEL_MAX_RETRIES,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))) as client:
        result=await client.responses.create(model=config.MODEL,input='Synthetic inference only')
    assert result.status=='completed' and attempts==['/v1/responses']*3


async def test_authentication_errors_fail_without_retries():
    attempts=[]
    async def handler(request):
        attempts.append(request.url.path)
        return httpx2.Response(401,json={'error':{'message':'Invalid key','type':'invalid_request_error'}})
    async with AsyncOpenAI(api_key='sk-test',max_retries=config.MODEL_MAX_RETRIES,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))) as client:
        with pytest.raises(AuthenticationError):await client.responses.create(model=config.MODEL,input='Synthetic')
    assert len(attempts)==1


async def test_exhausted_model_retries_stop(monkeypatch):
    attempts=[]
    async def handler(request):
        attempts.append(request.url.path)
        return httpx2.Response(429,json={'error':{'message':'Still busy','type':'rate_limit_error'}})
    monkeypatch.setattr(AsyncOpenAI,'_calculate_retry_timeout',lambda *a,**kw:0)
    async with AsyncOpenAI(api_key='sk-test',max_retries=config.MODEL_MAX_RETRIES,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))) as client:
        with pytest.raises(RateLimitError):await client.responses.create(model=config.MODEL,input='Synthetic')
    assert len(attempts)==3


async def test_partial_stream_is_not_replayed():
    attempts=[]
    class BrokenStream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"Hello","item_id":"msg_test","output_index":0,"content_index":0,"sequence_number":0,"logprobs":[]}\n\n'
            raise httpx2.ReadError('Synthetic stream disconnect')
    async def handler(request):
        attempts.append(request.url.path)
        return httpx2.Response(200,headers={'content-type':'text/event-stream'},stream=BrokenStream())
    received=[]
    async with AsyncOpenAI(api_key='sk-test',max_retries=config.MODEL_MAX_RETRIES,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))) as client:
        stream=await client.responses.create(model=config.MODEL,input='Synthetic',stream=True)
        with pytest.raises(APIConnectionError):
            async for event in stream:received.append(event.type)
    assert received==['response.output_text.delta'] and len(attempts)==1
