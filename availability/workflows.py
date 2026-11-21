"""Durable orchestration. All network and database work runs in activities."""
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

RETRY=RetryPolicy(initial_interval=timedelta(seconds=1),maximum_interval=timedelta(seconds=30))


@workflow.defn
class SubagentWatch:
    def __init__(self):self.event=None

    @workflow.run
    async def run(self,request:dict):
        await workflow.execute_activity('availability_register',request,start_to_close_timeout=timedelta(seconds=20),retry_policy=RETRY)
        remaining=max(0,request['expires']-workflow.now().timestamp())
        try:await workflow.wait_condition(lambda:self.event is not None,timeout=timedelta(seconds=remaining))
        except TimeoutError:return
        await workflow.execute_activity('availability_forward',{'request':request,'event':self.event},start_to_close_timeout=timedelta(seconds=20),retry_policy=RETRY)

    @workflow.signal
    def opened(self,event:dict):
        if self.event is None:self.event=event


@workflow.defn
class MainAvailability:
    def __init__(self):self.events=[]

    @workflow.run
    async def run(self,request:dict):
        pending=set(request.get('query_ids',[]))
        self.events=request.get('events',[])+self.events
        while workflow.now().timestamp()<request['expires']:
            try:await workflow.wait_condition(lambda:bool(self.events),timeout=timedelta(seconds=max(0,request['expires']-workflow.now().timestamp())))
            except TimeoutError:return
            event=self.events.pop(0)
            parent=event.get('parent_workflow_id')
            if parent and parent!=workflow.info().workflow_id:continue
            dynamic_child=request.get('recurring') and parent==workflow.info().workflow_id
            if request.get('query_ids') and event.get('query_id') not in request['query_ids'] and not dynamic_child:continue
            await workflow.execute_activity('availability_publish',event,start_to_close_timeout=timedelta(seconds=20),retry_policy=RETRY)
            pending.discard(event['query_id'])
            if request.get('query_ids') and not pending and not request.get('recurring'):return
            if workflow.info().is_continue_as_new_suggested():workflow.continue_as_new({**request,'query_ids':request['query_ids'] if request.get('recurring') else sorted(pending),'events':self.events})

    @workflow.signal
    def opened(self,event:dict):self.events.append(event)


@workflow.defn
class AvailabilityCheck:
    @workflow.run
    async def run(self,request:dict):
        await workflow.execute_activity('availability_poll',request,start_to_close_timeout=timedelta(seconds=110),schedule_to_close_timeout=timedelta(seconds=115),retry_policy=RetryPolicy(maximum_attempts=1))
