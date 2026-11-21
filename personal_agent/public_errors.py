"""Customer wording is separate from internal exception diagnostics."""
import re

REQUEST_FAILURE='I couldn’t process that request. Please try again.'
SAFE_INPUT_MESSAGES={
    'Attach up to ten different files.',
    'Send attachments with a new question, rather than a command or paused-task reply.',
    'An attachment is unavailable or already sent. Select it again.',
    'Open the conversation with the pending approval first',
    'Use the current approval buttons or its exact approval ID',
    'A run is already active','Choose Approve or Cancel','Approval already handled',
    'Enter a short response','Input already handled',
    'Enter passwords and verification codes in the browser, then choose Continue',
    'Complete the required website fields and any private login/verification step before continuing. The agent is still paused.',
    'Private login information must be entered in the browser',
    'Provide a venue name from the user request.','Provide the venue location/city.',
    'Party size must be from 1 to 20.','Choose a current or future date.',
    'The website changed or disconnected. Inspect the browser before continuing.',
}


def request_error(exc):
    message=str(exc)
    return message if message in SAFE_INPUT_MESSAGES else REQUEST_FAILURE


def customer_text(text):
    """Project known historical system-error templates without editing history.

    Leave explanations of code/errors alone; only generated failure templates
    are matched, not arbitrary mentions of Python or other technical subjects.
    """
    if re.match(r'^Browser task (?:blocked|unverified|interrupted|failed|error|cancelled):',text):
        return 'I couldn’t confirm that the website completed your request. Please check the current page before trying again.'
    text=re.sub(r'(?m)^(?:[-*] )?Please provide (?:Invalid isoformat string:|Invalid flight connections)[^\n]*$',
        'I couldn’t complete that availability check. Please try again.',text)
    return re.sub(r'The run stopped \([A-Za-z_][A-Za-z0-9_.]*\)\.',
        'I couldn’t complete this request.',text)
