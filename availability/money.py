"""Keep user budget units separate from provider price units."""
import re
import xml.etree.ElementTree as ET
import httpx

ECB='https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
SYMBOLS={'$':'USD','€':'EUR','£':'GBP'}


def budget(text):
    amount=r'(?P<currency>USD|EUR|GBP|AUD|CAD|JPY|SGD|AED|ZAR|BRL|[$€£])?\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?P<scale>k)?(?:\s*(?P<suffix>USD|EUR|GBP|AUD|CAD|JPY|SGD|AED|ZAR|BRL))?'
    patterns=[r'\bbudget\s*(?:of|is|:)?\s*'+amount,amount+r'\s*(?:total\s+)?budget\b']
    found=[m for pattern in patterns for m in re.finditer(pattern,text,re.I)]
    if not found:return None
    m=max(found,key=lambda m:m.start());code=m['currency'] or m['suffix'] or 'USD'
    return {'limit':float(m['amount'].replace(',',''))*(1000 if m['scale'] else 1),'currency':SYMBOLS.get(code,code.upper())}


async def exchange_rates():
    async with httpx.AsyncClient(timeout=10) as client:
        response=await client.get(ECB);response.raise_for_status()
    root=ET.fromstring(response.text);rates={'EUR':1.0};day=''
    for node in root.iter():
        if node.get('time'):day=node.get('time')
        if node.get('currency'):rates[node.get('currency')]=float(node.get('rate'))
    if not day or 'USD' not in rates:raise ValueError('No exchange rates')
    return {'date':day,'rates':rates,'source_url':ECB}


def convert(amount,source,target,quotes):
    if source==target:return amount
    rates=quotes.get('rates',{})
    if source not in rates or target not in rates:raise ValueError('Currency conversion unavailable')
    return round(amount/rates[source]*rates[target],2)


def display(amount,currency):
    symbol={'USD':'$','EUR':'€','GBP':'£'}.get(currency,currency+' ')
    return symbol+f'{amount:,.0f}'
