from personal_agent.browser_model import browser_model_options


def test_terra_options_preserve_low_effort_and_remove_sampling_parameters():
    options=browser_model_options('gpt-6.1-terra')
    assert options['temperature'] is None and options['frequency_penalty'] is None
    assert options['reasoning_models']==['gpt-6'] and options['reasoning_effort']=='low'
    assert browser_model_options('gpt-5.4-mini')=={}
