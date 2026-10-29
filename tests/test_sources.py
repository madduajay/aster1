from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from personal_agent.agent import Engine
from personal_agent.memory import Memory
from personal_agent.service import Service
from personal_agent.sources import allowed_url, canonical_url, normalize_domains, vetted_text
from conftest import SummaryModel
from test_agent import ScriptedBrowser, ScriptedModel, wait_service


@pytest.mark.parametrize('domain', ['americanexpress.com', 'nasa.gov', 'manufacturer.example'])
def test_dynamic_domains_allow_only_owner_and_subdomains(domain):
    domains = normalize_domains([domain])
    assert allowed_url('https://' + domain + '/products', domains)
    assert allowed_url('https://offers.' + domain + '/details', domains)
    assert not allowed_url('https://' + domain + '.unrelated.example/products', domains)
    assert not allowed_url('https://fake' + domain + '/products', domains)
    assert not allowed_url('https://' + domain + '@unrelated.example/products', domains)
    assert not allowed_url('https://unrelated.example/?official=' + domain, domains)


@pytest.mark.parametrize('domains', [[], ['com'], ['*.example.com'], ['https://example.com'], ['example.com/path'], ['127.0.0.1']])
def test_invalid_official_scope_rejected(domains):
    with pytest.raises(ValueError):
        normalize_domains(domains)


def test_unverified_and_historical_citations_not_displayed():
    sources = [{'url': 'https://owner.example/products', 'title': 'Products'}]
    assert 'Products' in vetted_text('See [Products](https://owner.example/products).', sources)
    for text in ['See [review](https://reviews.example/products).',
                 'Earlier summary linked https://reviews.example/products.',
                 'See https://owner.example/guessed-page.']:
        result = vetted_text(text, sources)
        assert 'http' not in result


def test_tracking_variants_restore_exact_verified_urls():
    source = 'https://www.amazon.com/deals?utm_source=openai'
    text = 'Try [Today’s Deals](https://www.amazon.com/deals).'
    assert vetted_text(text, [{'url': source, 'title': 'Deals'}]) == f'Try [Today’s Deals]({source}).'
    assert vetted_text('See https://www.amazon.com/deals.', [{'url': source, 'title': 'Deals'}]) == f'See {source}.'


def test_unverified_detail_uses_verified_parent_as_explicit_overview():
    source = 'https://www.americanexpress.com/us/credit-cards/?utm_source=openai'
    text = '- [Gold Card](https://www.americanexpress.com/us/credit-cards/card/gold-card/) — dining rewards.'
    result = vetted_text(text, [{'url': source, 'title': 'Cards'}])
    assert result == f'- [Gold Card (official overview)]({source}) — dining rewards.'
    assert '/card/gold-card/' not in result


def test_unknown_link_does_not_erase_verified_results():
    source = 'https://owner.example/products?utm_source=openai'
    text = 'Verified result: [Products](https://owner.example/products).\nUnverified claim: [Review](https://reviews.example/products).\n'
    result = vetted_text(text, [{'url': source, 'title': 'Products'}])
    assert 'Verified result:' in result and source in result
    assert 'Unverified claim' not in result and 'reviews.example' not in result
    assert 'substitute other websites' not in result


def test_canonical_urls_keep_functional_parameters_paths_and_fragments():
    base = 'https://owner.example/products?sku=42&locale=en&utm_source=openai'
    assert canonical_url(base) == 'https://owner.example/products?sku=42&locale=en'
    for changed in ['https://owner.example/products?sku=43&locale=en',
                    'https://owner.example/products?sku=42&locale=fr',
                    'https://owner.example/products/?sku=42&locale=en',
                    'https://owner.example/products?sku=42&locale=en#other']:
        assert canonical_url(base) != canonical_url(changed)
        assert vetted_text(f'[Other]({changed})', [{'url': base, 'title': 'Product'}]) == ''


def test_streaming_does_not_emit_repeated_rejection_messages():
    assert vetted_text('See https://unknown.example/page\n', [], empty_fallback=False) == ''
    assert vetted_text('A normal conversational response.\n', []) == 'A normal conversational response.\n'


async def run_search_case(database, users, tmp_path, domains, response, final='I could not verify official information.'):
    uid = users[0]['id']; sid = database.new_session(uid)
    class FakeResponses:
        args = None
        async def create(self, **kwargs):
            self.args = kwargs
            return response
    responses = FakeResponses()
    model = ScriptedModel(responses=[AIMessage(content='', tool_calls=[
        {'name': 'web_search', 'args': {'query': 'Show the official product information', 'official_domains': domains},
         'id': 'search-policy', 'type': 'tool_call'}]), AIMessage(content=final)])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'policy.sqlite')) as saver:
        engine = Engine(database, ScriptedBrowser(), saver, 'sk-test', model=model, client=SimpleNamespace(responses=responses))
        service = Service(database, engine); service.memory = Memory(database, SummaryModel())
        run = (await service.submit(uid, sid, 'Get the official product information'))['run_id']
        await wait_service(service, run)
        assert database.one('SELECT status FROM runs WHERE id=?', (run,))['status'] == 'completed'
    return responses, model, run


@pytest.mark.parametrize('domain', ['americanexpress.com', 'another-brand.example'])
async def test_search_domain_scope_is_dynamic(database, users, tmp_path, domain):
    response = SimpleNamespace(output_text='Official fact', output=[SimpleNamespace(type='message',content=[
        SimpleNamespace(annotations=[SimpleNamespace(type='url_citation',url=f'https://{domain}/products',title='Official products')])])])
    requests, model, run = await run_search_case(database, users, tmp_path, [domain], response)
    assert requests.args['tools'][0]['filters']['allowed_domains'] == [domain]
    assert domain in requests.args['instructions']
    assert 'PRIMARY, FIRST-PARTY' in requests.args['instructions']
    assert database.rows("SELECT * FROM events WHERE run_id=? AND kind='sources'", (run,))


@pytest.mark.parametrize('bad_citation,bad_consulted,bad_body', [(True, False, False), (False, True, False), (False, False, True)])
async def test_mixed_provider_answer_is_discarded(database, users, tmp_path, bad_citation, bad_consulted, bad_body):
    annotation = SimpleNamespace(type='url_citation', url='https://reviews.example/products' if bad_citation else 'https://owner.example/products', title='Products')
    output = [SimpleNamespace(type='message', content=[SimpleNamespace(annotations=[annotation])])]
    if bad_consulted:
        output.append(SimpleNamespace(type='web_search_call', action=SimpleNamespace(sources=[SimpleNamespace(url='https://reviews.example/products')])))
    response = SimpleNamespace(output_text='UNTRUSTED FACT SENTINEL' + (' https://reviews.example/products' if bad_body else ''), output=output)
    requests, model, run = await run_search_case(database, users, tmp_path, ['owner.example'], response)
    assert 'UNTRUSTED FACT SENTINEL' not in str(model.seen[-1])
    assert not database.rows("SELECT * FROM events WHERE run_id=? AND kind='sources'", (run,))


async def test_unknown_official_domain_does_not_search_unrestricted(database, users, tmp_path):
    requests, model, run = await run_search_case(database, users, tmp_path, [], None)
    assert requests.args is None
    assert not database.rows("SELECT * FROM events WHERE run_id=? AND kind='sources'", (run,))


async def test_provider_answer_without_citations_is_not_evidence(database, users, tmp_path):
    response = SimpleNamespace(output_text='UNCITED FACT SENTINEL', output=[])
    requests, model, run = await run_search_case(database, users, tmp_path, ['owner.example'], response)
    assert 'UNCITED FACT SENTINEL' not in str(model.seen[-1])
    assert not database.rows("SELECT * FROM events WHERE run_id=? AND kind='sources'", (run,))


async def test_unverified_research_cannot_be_filled_from_model_memory(database, users, tmp_path):
    response = SimpleNamespace(output_text='UNCITED FACT', output=[])
    requests, model, run = await run_search_case(database, users, tmp_path, ['owner.example'], response,
                                                final='UNVERIFIED MODEL FACT: the product costs 999 dollars.')
    answer = database.one("SELECT payload FROM events WHERE run_id=? AND kind='answer' ORDER BY id DESC LIMIT 1", (run,))['payload']
    assert 'UNVERIFIED MODEL FACT' not in answer and "couldn't verify" in answer
