"""Local topic index. Raw history remains authoritative and controls access scope."""
import re
import sqlite3
import time

STOP = {'помощник','помощник','тебя','меня','только','когда','привет','можешь','расскажи','сегодня','завтра','хочу','какой','какая','какие','будет'}
SOURCES = {'telegram': ('chat_memory','person'), 'twitch': ('twitch_messages','user_id')}


def terms(text):
    return {w[:5] for w in re.findall(r'[а-яёa-z0-9]{5,}', text.casefold())} - {w[:5] for w in STOP}


def expression(question):
    words = sorted(terms(question))[:16]
    if re.search(r'погод|температур|дожд|снег|прогноз|жарк|холодн|ветер',question,re.I):
        words += ['живу','живём','живем','переехал','нахожусь','город','из','погод','темпе','дожд','снег','прогн','жарк','холод','ветер']
        extra = ' OR ("у" AND "нас" AND "в")'
    else:extra=''
    return ' OR '.join('"'+w+'"*' for w in dict.fromkeys(words))+extra if words else ''


class MemorySearch:
    def __init__(self, store):
        self.store=store;self.available=False
        try:
            with store.lock,store.db:
                for platform,(table,field) in SOURCES.items():
                    name='memory_search_'+platform
                    existed=store.db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(name,)).fetchone()
                    if existed and 'person' not in {r[1] for r in store.db.execute('PRAGMA table_info('+name+')')}:
                        for suffix in ('insert','delete','update'):store.db.execute('DROP TRIGGER IF EXISTS '+name+'_'+suffix)
                        store.db.execute('DROP TABLE '+name);existed=None
                    store.db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS '+name+' USING fts5(text,person,prefix=5)')
                    store.db.executescript(f'''
                        CREATE TRIGGER IF NOT EXISTS {name}_insert AFTER INSERT ON {table} BEGIN
                            INSERT INTO {name}(rowid,text,person) VALUES (new.rowid,new.text,new.{field});
                        END;
                        CREATE TRIGGER IF NOT EXISTS {name}_delete AFTER DELETE ON {table} BEGIN
                            DELETE FROM {name} WHERE rowid=old.rowid;
                        END;
                        CREATE TRIGGER IF NOT EXISTS {name}_update AFTER UPDATE ON {table} BEGIN
                            DELETE FROM {name} WHERE rowid=old.rowid;
                            INSERT INTO {name}(rowid,text,person) VALUES (new.rowid,new.text,new.{field});
                        END;
                    ''')
                    if not existed:
                        store.set_state(name+'_cursor','0')
                        empty=not store.db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone()
                        store.set_state(name+'_ready','1' if empty else '0')
            self.available=True
        except sqlite3.OperationalError:
            # Some Python builds omit FTS5. The paged local search still works.
            self.available=False

    def index_batch(self, limit=500):
        if not self.available:return False
        pending=False
        for platform,(table,field) in SOURCES.items():
            name='memory_search_'+platform
            with self.store.lock,self.store.db:
                if self.store.state(name+'_ready','0')=='1':continue
                cursor=int(self.store.state(name+'_cursor','0'))
                rows=self.store.db.execute('SELECT rowid,text,'+field+' AS person FROM '+table+' WHERE rowid>? ORDER BY rowid LIMIT ?',(cursor,limit)).fetchall()
                self.store.db.executemany('INSERT OR REPLACE INTO '+name+'(rowid,text,person) VALUES (?,?,?)',[(r['rowid'],r['text'],r['person']) for r in rows])
                if rows:self.store.set_state(name+'_cursor',str(rows[-1]['rowid']))
                if len(rows)<limit:self.store.set_state(name+'_ready','1')
                else:pending=True
        return pending

    def candidates(self, platform, person, question, cutoff=-1, scope=(), before=None):
        if not self.available:return None
        table,field=SOURCES[platform];name='memory_search_'+platform;query=expression(question)
        with self.store.lock:
            if self.store.state(name+'_ready','0')!='1':return None
            if not query:return []
            where=' WHERE '+name+' MATCH ? AND m.'+field+'=? AND m.at>=?'
            match='person : \"'+str(person).replace('\"','\"\"')+'\" AND text : ('+query+')'
            args=[match,str(person),cutoff]
            if scope:
                where+=' AND m.chat IN ('+','.join('?' for _ in scope)+')';args+=list(scope)
            if before is not None:where+=' AND m.at<?';args.append(before)
            columns='m.chat,m.text,m.at' if platform=='telegram' else 'm.text,m.at'
            try:
                return [dict(r) for r in self.store.db.execute('SELECT '+columns+' FROM '+name+' f CROSS JOIN '+table+' m ON m.rowid=f.rowid'+where+' ORDER BY f.rank,m.at DESC LIMIT 400',args)]
            except sqlite3.OperationalError:
                return None

    def status(self):
        with self.store.lock:
            return {'available':self.available,'ready':self.available and all(self.store.state('memory_search_'+p+'_ready','0')=='1' for p in SOURCES)}

    def loop(self, stop):
        while not stop.is_set():
            try:pending=self.index_batch()
            except sqlite3.Error:pending=False
            stop.wait(.2 if pending else 5)
