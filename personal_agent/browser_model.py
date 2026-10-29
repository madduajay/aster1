"""Model-specific Browser Use request options (no credentials or SDK imports)."""


def browser_model_options(model):
    if model.startswith('gpt-6'):
        # Browser Use 0.13.14's default reasoning-model list predates GPT-6.
        # Its planner returns structured JSON actions, not OpenAI tool calls.
        return {'temperature':None,'frequency_penalty':None,
                'reasoning_models':['gpt-6'],'reasoning_effort':'low'}
    return {}
