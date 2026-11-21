"""Explicitly prevent DeepAgents from constructing a general-purpose subagent."""
from deepagents import GeneralPurposeSubagentProfile,HarnessProfile,register_harness_profile
_disabled=set()


def no_general_agent(model=None):
    profile=HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))
    from deepagents._models import get_model_provider
    names={'openai','openai:gpt-5.4-mini'}
    if model is not None and get_model_provider(model):names.add(get_model_provider(model))
    for name in names:
        if name not in _disabled:
            register_harness_profile(name,profile);_disabled.add(name)
    if model is not None:
        from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model
        selected=_harness_profile_for_model(model,None)
        if selected.general_purpose_subagent.enabled is not False:raise ValueError('Model harness must explicitly disable general-purpose subagents')
