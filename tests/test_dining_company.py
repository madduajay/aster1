from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dining_company.app import DiningQuery,Inventory


def query(venue='Carbone'):
    return DiningQuery(request_id='aster-dining-'+'a'*32,venue=venue,location='New York',date='2099-12-01',party_size=2)


@pytest.mark.parametrize('venue',['Nobu','Lilia','Gramercy Tavern'])
def test_other_venues_have_slots_without_an_event(tmp_path,venue):
    company=Inventory(tmp_path/'company.sqlite',clock=lambda:100)
    result=company.search(query(venue))
    assert result['available'] and result['slots']==['18:30','19:00','19:30']
    assert not any(word in str(result).lower() for word in ('dummy','demo','simulated'))
    with company.connect() as db:assert db.execute('SELECT due FROM requests').fetchone()[0] is None


@pytest.mark.parametrize('venue',[
    'Carbone','carbone indian restaurant','Carbone New York restaurant',
    'CARBONE in Phoenix',"Carbone's",'Carbone’s','Carbone-NYC','Ｃａｒｂｏｎｅ restaurant',
])
def test_carbone_with_descriptors_never_returns_initial_slots(tmp_path,venue):
    company=Inventory(tmp_path/'company.sqlite',clock=lambda:100)
    result=company.search(query(venue))
    assert result['available'] is False and result['slots']==[]
    assert company.subscribe(query(venue))['watching'] is True


@pytest.mark.parametrize('venue',['Carbonera','Carbonell','Nobu'])
def test_other_names_are_not_misclassified_as_carbone(tmp_path,venue):
    assert Inventory(tmp_path/'company.sqlite').search(query(venue))['available'] is True


def test_previously_cached_carbone_alias_slots_are_corrected_on_lookup(tmp_path):
    import json
    company=Inventory(tmp_path/'company.sqlite',clock=lambda:100)
    request=query('carbone indian restaurant');result=company.search(request)
    with company.connect() as db:
        db.execute('UPDATE requests SET response=? WHERE id=?',
            (json.dumps({**result,'available':True,'slots':['18:30','19:00','19:30']}),request.request_id))
    corrected=company.search(request)
    assert corrected['available'] is False and corrected['slots']==[]
    assert company.subscribe(request)['watching'] is True


async def test_company_owns_ten_second_timer_retry_and_restart(tmp_path):
    clock=[100.];company=Inventory(tmp_path/'company.sqlite',clock=lambda:clock[0])
    assert company.search(query())['available'] is False
    with company.connect() as db:assert db.execute('SELECT due FROM requests').fetchone()[0] is None
    company.subscribe(query())
    clock[0]=105;assert company.search(query())['slots']==[]
    assert company.subscribe(query())['watching'] is True
    signal=AsyncMock();client=SimpleNamespace(get_workflow_handle=lambda wid:SimpleNamespace(signal=signal))
    clock[0]=109.99;await company.deliver(client);signal.assert_not_awaited()
    company=Inventory(tmp_path/'company.sqlite',clock=lambda:clock[0]);clock[0]=110
    signal.side_effect=ConnectionError('controlled unavailable')
    await company.deliver(client)
    signal.side_effect=None;await company.deliver(client);await company.deliver(client)
    assert signal.await_count==2
    name,event=signal.call_args.args
    assert name=='availability_opened' and event['available'] and event['slots']==['19:00']
    assert company.search(query())['available'] is False
    with pytest.raises(ValueError):company.search(query().model_copy(update={'party_size':3}))
