"""Conservative account matching by verified usernames and explicit admin links."""
import re
import time
import unicodedata

def key(username):return re.sub(r'[^a-z0-9]','',unicodedata.normalize('NFKC',str(username)).casefold())

def reconcile(book):
    added=[]
    with book.store.lock,book.store.db:
        groups={}
        for row in book.store.db.execute('SELECT * FROM community_usernames WHERE at>=?',(int(time.time())-30*86400,)):
            value=key(row['username'])
            if len(value)>=5:groups.setdefault(value,{'telegram':[],'twitch':[]})[row['platform']].append(row['user_id'])
        pairs={(g['twitch'][0],g['telegram'][0]) for g in groups.values() if len(g['twitch'])==len(g['telegram'])==1}
        for row in book.store.db.execute("SELECT * FROM community_links WHERE method='auto'").fetchall():
            if (row['twitch_id'],row['telegram_id']) not in pairs:
                book.store.db.execute("DELETE FROM community_links WHERE twitch_id=? AND method='auto'",(row['twitch_id'],))
        for twitch,telegram in sorted(pairs):
            if book.store.db.execute('SELECT 1 FROM community_links WHERE twitch_id=? OR telegram_id=?',(twitch,telegram)).fetchone():continue
            if book.store.db.execute('SELECT 1 FROM community_link_blocks WHERE twitch_id=? AND telegram_id=?',(twitch,telegram)).fetchone():continue
            book.store.db.execute("INSERT INTO community_links VALUES (?,?,?,'auto')",(twitch,telegram,int(time.time())))
            added.append((twitch,telegram))
    return added

def manual(book,twitch,telegram):
    twitch,telegram=str(twitch),str(telegram)
    if not twitch.isdigit() or not telegram.isdigit():raise ValueError('Выберите два существующих аккаунта по ID.')
    with book.store.lock,book.store.db:
        for platform,uid in (('twitch',twitch),('telegram',telegram)):
            if not book.store.db.execute('SELECT 1 FROM community_participants WHERE platform=? AND user_id=?',(platform,uid)).fetchone():raise ValueError('Аккаунт ещё не встречался в чате.')
        current=book.store.db.execute('SELECT * FROM community_links WHERE twitch_id=? OR telegram_id=?',(twitch,telegram)).fetchall()
        if any(row['method']!='auto' and (row['twitch_id'],row['telegram_id'])!=(twitch,telegram) for row in current):raise ValueError('Один из аккаунтов уже связан вручную. Сначала отмените прежнюю связь.')
        book.store.db.execute("DELETE FROM community_links WHERE method='auto' AND (twitch_id=? OR telegram_id=?)",(twitch,telegram))
        book.store.db.execute("INSERT INTO community_links VALUES (?,?,?,'manual') ON CONFLICT(twitch_id) DO UPDATE SET telegram_id=excluded.telegram_id,at=excluded.at,method='manual'",(twitch,telegram,int(time.time())))
        book.store.db.execute('DELETE FROM community_link_blocks WHERE twitch_id=? AND telegram_id=?',(twitch,telegram))
    return {'ok':True}
