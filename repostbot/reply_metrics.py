"""Bounded timing log. Stores durations and identifiers, never prompts or credentials."""
import functools
import sqlite3
import threading
import time


def timed(stage):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(worker,*args,**kwargs):
            metrics=getattr(worker,'reply_metrics',None)
            if metrics is None:return fn(worker,*args,**kwargs)
            frame=getattr(metrics.local,'frame',None);root=frame is None and stage in ('total','answer')
            if root:
                if stage=='total':
                    msg=args[1] if len(args)>1 else kwargs.get('msg',{})
                    if fn.__name__=='on_chat':
                        event=args[0] if args else kwargs.get('event',{})
                        msg={'chat':{'id':'twitch:'+worker.store.setting('twitch_channel')},'from':{'id':event.get('chatter_user_id','')}}
                    chat=str((msg.get('chat') or {}).get('id',''));person=str((msg.get('from') or {}).get('id',''))
                    queued=float(msg.get('_reply_queue_wait_ms',0))
                else:
                    chat=str(args[1] if len(args)>1 else kwargs.get('chat_id',''));person=str(args[2] if len(args)>2 else kwargs.get('person',''));queued=0
                frame={'chat':chat,'person':person,'queue_ms':max(0,queued),'answer':0,'context':0,'precheck':0,'used':False,'error':''}
                metrics.local.frame=frame
            if frame is None:return fn(worker,*args,**kwargs)
            started=time.perf_counter()
            if stage=='answer':frame['used']=True
            try:return fn(worker,*args,**kwargs)
            except Exception as exc:
                frame['error']=type(exc).__name__;raise
            finally:
                elapsed=max(0,(time.perf_counter()-started)*1000)
                if stage!='total':frame[stage]=frame.get(stage,0)+elapsed
                if root:
                    metrics.local.frame=None
                    if frame['used']:
                        try:metrics.record(frame,elapsed)
                        except sqlite3.Error:pass
        return wrapped
    return decorate


class ReplyMetrics:
    def __init__(self,store):
        self.store=store;self.local=threading.local()
        with store.lock,store.db:
            store.db.execute('''CREATE TABLE IF NOT EXISTS reply_metrics (
                id INTEGER PRIMARY KEY,at INTEGER NOT NULL,chat TEXT NOT NULL,person TEXT NOT NULL,
                total_ms REAL NOT NULL,queue_ms REAL NOT NULL,precheck_ms REAL NOT NULL,
                answer_ms REAL NOT NULL,context_ms REAL NOT NULL,error TEXT NOT NULL)''')

    def record(self,frame,total):
        with self.store.lock,self.store.db:
            self.store.db.execute('INSERT INTO reply_metrics(at,chat,person,total_ms,queue_ms,precheck_ms,answer_ms,context_ms,error) VALUES (?,?,?,?,?,?,?,?,?)',
                (int(time.time()),frame['chat'],frame['person'],round(total,1),round(frame['queue_ms'],1),round(frame['precheck'],1),round(frame['answer'],1),round(frame['context'],1),frame['error']))
            self.store.db.execute('DELETE FROM reply_metrics WHERE id<=(SELECT MAX(id)-100 FROM reply_metrics)')

    def recent(self):
        with self.store.lock:return [dict(r) for r in self.store.db.execute('SELECT * FROM reply_metrics ORDER BY id DESC LIMIT 30')]
