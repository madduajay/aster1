import json
from personal_agent.browser_verification import research_pages,relevant_read_links


def test_research_evidence_contains_only_visited_successful_scoped_pages():
    def action(url,text,status='completed'):
        return {'status':status,'before_state':json.dumps({'url':url,'text':text}),'after_state':None}
    actions=[action('https://example.com/cards/amber','Amber earns two points.'),
             action('https://example.com/cards/jade','Jade includes lounge access.'),
             action('https://unrelated.example/','Not in scope'),
             action('https://example.com/error','Forbidden'),
             action('https://example.com/blocked','Never read','blocked')]
    observed={'url':'https://example.com/cards/silver','title':'Silver','text':'Silver has no annual fee.',
              'links':['https://example.com/cards/unvisited']}
    pages=research_pages(observed,actions,['example.com'],lambda u:403 if u.endswith('/error') else 200)
    assert [p['url'] for p in pages]==['https://example.com/cards/amber','https://example.com/cards/jade','https://example.com/cards/silver']
    assert all('unvisited' not in p['url'] for p in pages)


def test_relevant_read_links_select_observed_card_details_without_login_or_mutations():
    observed={'links':['https://example.com/benefits/#benefit-card','https://example.com/cards/amber',
                       'https://example.com/cards/jade','https://unrelated.example/cards/other',
                       'https://example.com/login/cards','https://example.com/cart/add/card',
                       'https://example.com/privacy','https://example.com/cards/amber']}
    assert relevant_read_links(observed,'summarize benefits by card',['example.com'])==observed['links'][:3]


def test_research_preserves_distinct_tab_states_at_the_same_url():
    url='https://example.com/plans'
    basic={'url':url,'text':'Basic plan includes 10 GB.','selections':[{'name':'Basic','value':'basic'}]}
    pro={'url':url,'text':'Pro plan includes 100 GB.','selections':[{'name':'Pro','value':'pro'}]}
    actions=[{'status':'completed','before_state':json.dumps(basic),'after_state':json.dumps(pro)}]
    pages=research_pages(pro,actions,['example.com'],lambda _:200)
    assert [p['text'] for p in pages]==[basic['text'],pro['text']]
    assert [p['selections'] for p in pages]==[basic['selections'],pro['selections']]


def test_navigation_candidates_work_for_documentation_on_another_domain():
    observed={'url':'https://reference.example/guide','links':['https://reference.example/privacy',
              'https://reference.example/operations/read','https://reference.example/operations/update']}
    assert relevant_read_links(observed,'Explain retry behavior by operation',['reference.example'])==observed['links'][1:]


def test_later_ui_snapshots_cannot_evict_an_earlier_detail_page():
    states=[{'url':'https://example.com/products/first','text':'First model has a three-year warranty.'}]
    states += [{'url':'https://example.com/products/last','text':'Last model specifications. '+str(i),
                'selections':[{'name':'Tab','value':str(i)}]} for i in range(20)]
    actions=[{'status':'completed','before_state':json.dumps(s),'after_state':None} for s in states]
    pages=research_pages(states[-1],actions,['example.com'],lambda _:200)
    assert pages[0]['url']==states[0]['url'] and 'three-year warranty' in pages[0]['text']
    assert len([p for p in pages if p['url']==states[-1]['url']])<=3
