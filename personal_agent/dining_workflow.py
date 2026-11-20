"""Temporal orchestration: no I/O or wall-clock timers inside workflow code."""
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class DiningWorkflow:
    """Compatibility worker for workflows started before agent-led dining lookup."""
    def __init__(self):
        self.initial=None;self.opening=None;self.request=None

    @workflow.run
    async def run(self,request:dict)->dict:
        self.request=request
        self.initial=await workflow.execute_activity('company_search',request,
            start_to_close_timeout=timedelta(seconds=20),retry_policy=RetryPolicy(non_retryable_error_types=['ValueError']))
        if self.initial['available']:return self.initial
        # Signal history, rather than an Aster timer, is the source of truth.
        await workflow.wait_condition(lambda:self.opening is not None)
        await workflow.execute_activity('publish_dining_opening',{'request':request,'event':self.opening},
            start_to_close_timeout=timedelta(seconds=20))
        return self.opening

    @workflow.query
    def initial_result(self)->dict|None:return self.initial

    @workflow.signal
    def availability_opened(self,event:dict):
        if self.request is None:return
        expected=self.request['company_query']
        if (event.get('available') is True and event.get('slots') and
            all(event.get(key)==expected[key] for key in ('request_id','venue','location','date','party_size'))):
            if self.opening is None:self.opening=event


@workflow.defn
class DiningWatchWorkflow:
    """The agent has already looked up availability; only manage its follow-up."""
    def __init__(self):self.request=None;self.opening=None

    @workflow.run
    async def run(self,request:dict)->dict:
        self.request=request
        registration=await workflow.execute_activity('register_dining_watch',request,
            start_to_close_timeout=timedelta(seconds=20),retry_policy=RetryPolicy(non_retryable_error_types=['ValueError']))
        if not registration.get('watching'):return registration
        await workflow.wait_condition(lambda:self.opening is not None)
        await workflow.execute_activity('publish_dining_opening',{'request':request,'event':self.opening},
            start_to_close_timeout=timedelta(seconds=20))
        return self.opening

    @workflow.signal
    def availability_opened(self,event:dict):
        if self.request is None:return
        expected=self.request['company_query']
        if (event.get('available') is True and event.get('slots') and
            all(event.get(key)==expected[key] for key in ('request_id','venue','location','date','party_size'))):
            if self.opening is None:self.opening=event
