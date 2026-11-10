"""Counts come from delivered reposts; genre labels are explicit, editable facts."""
import json
import re
import time
from collections import Counter

GENRES={'hsr':('Гача / RPG',1),'uma':('Гача / симулятор',1),'hearthstone':('Карточная игра',0),
        'xcom':('Тактическая стратегия',0),'xcom2':('Тактическая стратегия',0),
        'lok_defiance':('Приключенческий экшен',0),'lok_soul_reaver':('Приключенческий экшен',0),
        'lok_soul_reaver_2':('Приключенческий экшен',0)}

class RepostStats:
    def __init__(self,worker):
        self.worker,self.store=worker,worker.store
        with self.store.lock,self.store.db:
            self.store.db.execute('CREATE TABLE IF NOT EXISTS repost_genres(game_key TEXT PRIMARY KEY,genre TEXT NOT NULL,is_gacha INTEGER)')
            self.store.db.executemany('INSERT OR IGNORE INTO repost_genres VALUES(?,?,?)',[(key,*value) for key,value in GENRES.items()])

    def chats(self):
        with self.store.lock:return [row[0] for row in self.store.db.execute('SELECT DISTINCT chat FROM route_post_memory ORDER BY chat')]

    def resolve(self,chat,main=False):
        available=self.chats();chat=str(chat or '')
        if main:chat=self.store.setting('repost_stats_main_chat') or self.worker.scout.destination().get('chat') or chat
        chat=str(self.store.canonical_chat_id(chat))
        if chat in available:return chat
        with self.store.lock:
            row=self.store.db.execute('SELECT username FROM chats WHERE id=?',(chat,)).fetchone()
        if row and row[0] and '@'+row[0] in available:return '@'+row[0]
        for target in self.worker.scout.targets():
            if target.get('chat')==chat:
                candidates=['-100'+str(target.get('channel_id','')),'@'+str(target.get('username',''))]
                if any(value in available for value in candidates):return next(value for value in candidates if value in available)
        return available[0] if len(available)==1 and main else ''

    def genres(self):
        with self.store.lock:return [dict(row) for row in self.store.db.execute('SELECT g.game_key,g.title,COALESCE(r.genre,\'\') AS genre,r.is_gacha FROM stream_games g LEFT JOIN repost_genres r ON r.game_key=g.game_key ORDER BY g.title')]

    def save(self,data):
        key=str(data.get('game_key',''));genre=str(data.get('genre','')).strip()[:100];kind=data.get('is_gacha')
        if kind not in (None,'unknown',0,1,'0','1'):raise ValueError('Гача: да, нет или неизвестно.')
        kind=None if kind in (None,'unknown') else int(kind)
        with self.store.lock,self.store.db:
            if not self.store.db.execute('SELECT 1 FROM stream_games WHERE game_key=?',(key,)).fetchone():raise ValueError('Рубрика не найдена.')
            self.store.db.execute('INSERT INTO repost_genres VALUES(?,?,?) ON CONFLICT(game_key) DO UPDATE SET genre=excluded.genre,is_gacha=excluded.is_gacha',(key,genre,kind))
        return {'ok':True}

    def report(self,chat,period=None):
        count=Counter();genres=Counter();examples=[];seen=set()
        with self.store.lock:
            rows=self.store.db.execute('SELECT * FROM route_post_memory WHERE chat=? ORDER BY at',(str(chat),)).fetchall()
            labels={row['game_key']:dict(row) for row in self.store.db.execute('SELECT * FROM repost_genres')}
            games=[dict(row) for row in self.store.db.execute('SELECT * FROM stream_series_posts WHERE chat=?',(str(chat),))]
        by_post={}
        for game in games:by_post.setdefault((game['route_id'],game['source_key']),[]).append(game)
        for row in rows:
            key=('message',row['message_id']) if row['message_id'] else (row['route_id'],row['source_key'])
            if key in seen:continue
            at=row['at']
            if period and not period[1]<=at<period[2]:continue
            seen.add(key);count['reposts']+=1
            try:context=json.loads(row['context'])
            except (ValueError,TypeError):context={}
            text=str(context.get('text') or context.get('title') or '')
            post_games=by_post.get((row['route_id'],row['source_key']),[])
            if not post_games and not re.search(r'(?:анонс|сегодня|завтра|дебют|выпуск|начало|в\s+\d{1,2}[:.]\d{2}).{0,180}(?:стрим|эфир|трансляц)|(?:стрим|эфир|трансляц).{0,180}(?:сегодня|завтра|начало|в\s+\d{1,2}[:.]\d{2})',text,re.I|re.S):continue
            count['announcements']+=1
            kinds=[];names=set()
            for game in post_games:
                label=labels.get(game['game_key'],{})
                kinds.append(label.get('is_gacha'));names.add(label.get('genre') or 'Жанр не указан')
            if 1 in kinds:bucket='mixed' if 0 in kinds else 'gacha'
            elif kinds and all(value==0 for value in kinds):bucket='non_gacha'
            else:bucket='unknown'
            count[bucket]+=1
            for name in names or {'Жанр не указан'}:genres[name]+=1
            if len(examples)<10:examples.append({'message_id':row['message_id'],'source_key':row['source_key'],'category':bucket})
        return {'chat':chat,'period':period[0] if period else 'всё время сохранённого журнала',
                'counts':dict(count),'genres':[{'genre':name,'posts':amount} for name,amount in genres.most_common()],
                'examples':examples,'note':'Один опубликованный пост считается один раз. Анонс с несколькими рубриками может входить в несколько жанров. Без подтверждённой разметки жанр остаётся неизвестным.'}

    def answer(self,question,chat):
        text=str(question).casefold()
        if not re.search(r'анонс|репост|рубри[кч]|жанр',text) or not re.search(r'сколько|сравн|количеств|статист|дол[яию]|процент|соотношен',text):return ''
        target=self.resolve(chat,main=bool(re.search(r'основн|главн',text)))
        if not target:return 'В журнале есть несколько каналов. Укажи канал или выбери основной в «Маршруты → Статистика репостов».' if self.chats() else 'В локальном журнале пока нет доставленных репостов для подсчёта.'
        period=self.store.route_post_requested_range(question);report=self.report(target,period);counts=report['counts'];total=counts.get('announcements',0)
        lines=['По сохранённому журналу канала '+target+' ('+report['period']+'): репостов '+str(counts.get('reposts',0))+', анонсов '+str(total)+'.']
        for key,label in (('non_gacha','Подтверждённо без гачи'),('gacha','Гача'),('mixed','Смешанные анонсы: гача и другие игры'),('unknown','Жанр пока не определён')):
            amount=counts.get(key,0);lines.append(label+': '+str(amount)+((' ('+str(round(amount*100/total,1))+'%)') if total else '')+'.')
        if report['genres']:lines.append('По жанрам: '+ '; '.join(row['genre']+' — '+str(row['posts']) for row in report['genres'])+'.')
        lines.append('Смешанные и неразмеченные анонсы не выдаю за подтверждённые «не гача». Это локальная история доставленных публикаций; жанры можно уточнить в панели.')
        return '\n'.join(lines)
