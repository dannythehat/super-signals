"""Day 34 restart-safe Web Push delivery.

Web Push is a visibility channel only. It consumes persisted notification_events,
records one receipt per notification/device subscription, and never calls a trading or
broker mutation gateway. Push failures are isolated from signal execution.
"""
from __future__ import annotations
import asyncio,json,logging
from dataclasses import dataclass
from uuid import UUID
from pywebpush import WebPushException,webpush
from sqlalchemy import text
from sqlalchemy.orm import Session,sessionmaker
logger=logging.getLogger(__name__);_MAX_ATTEMPTS=5;_STALE_SENDING_SECONDS=120
@dataclass(frozen=True,slots=True)
class PushDeliveryResult: seeded:int;sent:int;failed:int;suppressed:int;broker_trade_action_created:bool=False
class Day34PushNotificationManager:
    def __init__(self,*,session_factory:sessionmaker[Session],vapid_private_key:str,vapid_subject:str,poll_seconds:int=3):
        if not vapid_private_key.strip():raise ValueError('day34_vapid_private_key_missing')
        if not vapid_subject.strip():raise ValueError('day34_vapid_subject_missing')
        if poll_seconds<=0:raise ValueError('day34_push_poll_seconds_invalid')
        self._session_factory=session_factory;self._vapid_private_key=vapid_private_key;self._vapid_subject=vapid_subject;self._poll_seconds=poll_seconds;self._task=None;self._stop_event=asyncio.Event()
    async def start(self):
        if self._task is not None:return
        self._stop_event.clear();self._recover_stale_sending();self._task=asyncio.create_task(self._run(),name='day34-web-push')
    async def stop(self):
        self._stop_event.set()
        if self._task is not None:await self._task;self._task=None
    async def _run(self):
        while not self._stop_event.is_set():
            try:await self.deliver_once()
            except Exception:logger.exception('Day 34 push loop failed safely')
            try:await asyncio.wait_for(self._stop_event.wait(),timeout=self._poll_seconds)
            except TimeoutError:pass
    async def deliver_once(self):
        self._recover_stale_sending();seeded=self._seed_deliveries();sent=failed=suppressed=0
        while True:
            r=self._claim_one()
            if r is None:break
            o=await asyncio.to_thread(self._send_claimed,r)
            if o=='sent':sent+=1
            elif o=='suppressed':suppressed+=1
            else:failed+=1
        return PushDeliveryResult(seeded,sent,failed,suppressed)
    def _recover_stale_sending(self):
        with self._session_factory() as s:
            r=s.execute(text("UPDATE push_notification_deliveries SET status='failed',failure_code='push_delivery_recovered_after_restart',failure_reason='Recovered stale sending state after worker restart.',updated_at=now() WHERE status='sending' AND sent_at IS NULL AND attempted_at IS NOT NULL AND attempted_at<now()-make_interval(secs=>:v) RETURNING id"),{'v':_STALE_SENDING_SECONDS}).all();s.commit();return len(r)
    def _seed_deliveries(self):
        with self._session_factory() as s:
            s.execute(text("""INSERT INTO notification_events(event_key,signal_id,lifecycle_event_id,user_id,audience,kind,title,body,payload) SELECT 'push-enabled:'||ps.id::text||':'||to_char(ps.active_since AT TIME ZONE 'UTC','YYYYMMDDHH24MISSUS'),NULL,NULL,ps.user_id,'user','push_test','Super Signals alerts are on','This device is ready to receive live trade alerts.',jsonb_build_object('subscription_confirmation',true,'provider_identity_exposed',false,'private_balance_exposed',false,'trade_action_created',false) FROM push_subscriptions ps WHERE ps.enabled=true AND NOT EXISTS(SELECT 1 FROM notification_events n WHERE n.event_key='push-enabled:'||ps.id::text||':'||to_char(ps.active_since AT TIME ZONE 'UTC','YYYYMMDDHH24MISSUS')) ON CONFLICT(event_key) DO NOTHING"""));r=s.execute(text("""INSERT INTO push_notification_deliveries(notification_id,subscription_id,status) SELECT n.id,ps.id,'pending' FROM notification_events n JOIN push_subscriptions ps ON ps.enabled=true AND n.created_at>=ps.active_since AND(n.audience='shared' OR(n.audience='user' AND n.user_id=ps.user_id)) WHERE NOT EXISTS(SELECT 1 FROM push_notification_deliveries d WHERE d.notification_id=n.id AND d.subscription_id=ps.id) ON CONFLICT(notification_id,subscription_id) DO NOTHING RETURNING id""")).all();s.commit();return len(r)
    def _claim_one(self):
        with self._session_factory() as s:
            r=s.execute(text("""WITH c AS(SELECT d.id FROM push_notification_deliveries d JOIN push_subscriptions ps ON ps.id=d.subscription_id WHERE d.status IN('pending','failed') AND d.attempt_count<:m AND ps.enabled=true ORDER BY d.created_at,d.id FOR UPDATE OF d SKIP LOCKED LIMIT 1) UPDATE push_notification_deliveries d SET status='sending',attempt_count=d.attempt_count+1,attempted_at=now(),failure_code=NULL,failure_reason=NULL,updated_at=now() FROM c WHERE d.id=c.id RETURNING d.id"""),{'m':_MAX_ATTEMPTS}).mappings().first()
            if r is None:s.commit();return None
            p=s.execute(text("SELECT d.id delivery_id,d.subscription_id,n.id notification_id,n.kind,n.title,n.body,ps.endpoint,ps.p256dh,ps.auth FROM push_notification_deliveries d JOIN notification_events n ON n.id=d.notification_id JOIN push_subscriptions ps ON ps.id=d.subscription_id WHERE d.id=:id"),{'id':r['id']}).mappings().one();s.commit();return dict(p)
    def _send_claimed(self,r):
        did=UUID(str(r['delivery_id']));sid=UUID(str(r['subscription_id']));data=json.dumps({'notification_id':str(r['notification_id']),'kind':str(r['kind']),'title':str(r['title']),'body':str(r['body']),'url':'/','private_account_data_included':False},separators=(',',':'));sub={'endpoint':str(r['endpoint']),'keys':{'p256dh':str(r['p256dh']),'auth':str(r['auth'])}}
        try:webpush(subscription_info=sub,data=data,vapid_private_key=self._vapid_private_key,vapid_claims={'sub':self._vapid_subject},ttl=300,timeout=10)
        except WebPushException as e:
            c=getattr(getattr(e,'response',None),'status_code',None)
            if c in{404,410}:self._suppress_expired(did,sid,c);return'suppressed'
            self._record_failure(did,sid,code=f"web_push_{c or 'error'}",reason=str(e)[:500]);return'failed'
        except Exception as e:self._record_failure(did,sid,code='web_push_unexpected',reason=str(e)[:500]);return'failed'
        self._record_success(did,sid);return'sent'
    def _record_success(self,did,sid):
        with self._session_factory() as s:s.execute(text("UPDATE push_notification_deliveries SET status='sent',sent_at=now(),failure_code=NULL,failure_reason=NULL,updated_at=now() WHERE id=:id"),{'id':did});s.execute(text("UPDATE push_subscriptions SET failure_count=0,last_success_at=now(),updated_at=now() WHERE id=:id"),{'id':sid});s.commit()
    def _record_failure(self,did,sid,*,code,reason):
        with self._session_factory() as s:s.execute(text("UPDATE push_notification_deliveries SET status='failed',failure_code=:c,failure_reason=:r,updated_at=now() WHERE id=:id"),{'id':did,'c':code[:80],'r':reason[:500]});s.execute(text("UPDATE push_subscriptions SET failure_count=failure_count+1,last_failure_at=now(),updated_at=now() WHERE id=:id"),{'id':sid});s.commit()
    def _suppress_expired(self,did,sid,c):
        with self._session_factory() as s:s.execute(text("UPDATE push_notification_deliveries SET status='suppressed',failure_code=:c,failure_reason='Browser push subscription expired or was removed.',updated_at=now() WHERE id=:id"),{'id':did,'c':f'web_push_{c}'});s.execute(text("UPDATE push_subscriptions SET enabled=false,failure_count=failure_count+1,last_failure_at=now(),updated_at=now() WHERE id=:id"),{'id':sid});s.commit()
__all__=['Day34PushNotificationManager','PushDeliveryResult']
