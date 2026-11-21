import json
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from .contracts import COMPANY_PORTS


async def call_company(security,domain,tool,arguments):
    async with httpx.AsyncClient(headers=security.headers(),timeout=15) as http:
        async with streamable_http_client(f'http://127.0.0.1:{COMPANY_PORTS[domain]}/mcp/',http_client=http) as (read,write,_):
            async with ClientSession(read,write) as session:
                await session.initialize();result=await session.call_tool(tool,arguments)
                if result.isError:raise RuntimeError('Company request failed')
                return result.structuredContent or json.loads(next(c.text for c in result.content if c.type=='text'))
