"""One exclusive Twitch IRC-over-WebSocket transport using the owner's session."""
import json
import errno
import queue
import re
import socket
import ssl
import threading
import time
from collections import deque
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen

IRC_URL='wss://irc-ws.chat.twitch.tv:443'
VALIDATE_URL='https://id.twitch.tv/oauth2/validate'
PING_INTERVAL=30
IDLE_TIMEOUT=90
AUTH_TIMEOUT=20
JOIN_TIMEOUT=20


class InvalidSession(RuntimeError):pass
class SessionCheckError(RuntimeError):pass


class IRCFailure(RuntimeError):
    def __init__(self,code,message):self.code,self.message=code,message;super().__init__(message)


def safe_failure(exc,stage):
    """Never reflect exception bodies, headers, URLs, or credentials."""
    from websocket import (WebSocketException,WebSocketBadStatusException,WebSocketProxyException,WebSocketAddressException,
        WebSocketTimeoutException,WebSocketConnectionClosedException,WebSocketProtocolException)
    if isinstance(exc,IRCFailure):return exc.code,exc.message
    if isinstance(exc,InvalidSession):return 'SESSION_REJECTED',str(exc)
    if isinstance(exc,SessionCheckError):return 'SESSION_CHECK',str(exc)
    if isinstance(exc,WebSocketBadStatusException):
        status=exc.status_code
        if isinstance(status,int) and 100<=status<=599:
            return 'WS_HTTP_'+str(status),'Сервер WebSocket ответил HTTP '+str(status)+' вместо подключения чата.'
        return 'WS_HTTP_ERROR','Сервер не разрешил подключение WebSocket.'
    if isinstance(exc,WebSocketProxyException):return 'PROXY_ERROR','Прокси не установил соединение с сервером чата.'
    if isinstance(exc,(socket.gaierror,WebSocketAddressException)):return 'DNS_ERROR','Не удалось определить адрес irc-ws.chat.twitch.tv.'
    if isinstance(exc,ssl.SSLCertVerificationError):return 'TLS_CERTIFICATE','Не удалось проверить сертификат сервера чата. Проверьте время и доверенные сертификаты контейнера.'
    if isinstance(exc,ssl.SSLError):return 'TLS_ERROR','Не удалось установить защищённое соединение с сервером чата.'
    if isinstance(exc,(TimeoutError,WebSocketTimeoutException)):return 'CONNECT_TIMEOUT','Истекло время ожидания соединения с сервером чата.'
    if isinstance(exc,WebSocketProtocolException):return 'WS_PROTOCOL','Сервер вернул некорректный ответ протокола WebSocket.'
    if isinstance(exc,ConnectionRefusedError):return 'CONNECTION_REFUSED','Сервер чата отказал в соединении.'
    if isinstance(exc,(ConnectionError,WebSocketConnectionClosedException)):return 'CONNECTION_CLOSED','Соединение с сервером чата прервано.'
    if isinstance(exc,WebSocketException) and stage=='websocket':return 'WS_HANDSHAKE','Сервер не подтвердил подключение WebSocket с протоколом irc.'
    if isinstance(exc,OSError):
        codes={errno.ECONNREFUSED:('CONNECTION_REFUSED','Сервер чата отказал в соединении.'),
               errno.ENETUNREACH:('NETWORK_UNREACHABLE','Сеть сервера чата недоступна из контейнера.'),
               errno.EHOSTUNREACH:('HOST_UNREACHABLE','Сервер чата недоступен из контейнера.')}
        if exc.errno in codes:return codes[exc.errno]
    if stage=='profile':return 'PROFILE_ERROR','Не удалось прочитать сохранённую сессию из профиля Chrome.'
    return 'CONNECTION_ERROR','Не удалось подключить чат. Подробности этапа сохранены в диагностике.'


def validate_session(token,login):
    from .core import _https_context
    try:
        request=Request(VALIDATE_URL,headers={'Authorization':'OAuth '+token,'User-Agent':'RepostBot'})
        with urlopen(request,timeout=15,context=_https_context(True)) as response:data=json.load(response)
    except HTTPError as exc:
        if exc.code in (400,401):raise InvalidSession('Twitch не принял сессию. Нужен действующий токен своего вошедшего аккаунта.') from None
        raise SessionCheckError('Проверка сессии Twitch: HTTP '+str(exc.code)) from None
    except (URLError,OSError,ValueError):raise SessionCheckError('Не удалось связаться с сервером проверки сессии Twitch') from None
    if not isinstance(data,dict):raise InvalidSession('Twitch не подтвердил пользователя этой сессии')
    actual=str(data.get('login') or '').casefold();user_id=str(data.get('user_id') or '')
    if not re.fullmatch(r'[a-z0-9_]{3,25}',actual) or not user_id.isdigit():raise InvalidSession('Twitch не подтвердил пользователя этой сессии')
    if actual!=login.casefold():raise InvalidSession('Сессия принадлежит @'+actual+', а в настройках указан @'+login)
    return {'id':user_id,'login':actual}


class ScoutIRC:
    def __init__(self,owner):
        self.owner=owner;self.stop=threading.Event();self.thread=None;self.socket=None
        self.lock=threading.Lock();self.jobs=queue.Queue(maxsize=32)
        self.stage='stopped';self.stage_at=0;self.attempt=0;self.retry_at=0
        self.verified_login='';self.failure={};self.events=deque(maxlen=40)

    def snapshot(self):
        with self.lock:
            return {'stage':self.stage,'elapsed':max(0,int(time.time()-self.stage_at)) if self.stage_at else 0,
                    'attempt':self.attempt,'retry_at':self.retry_at,'session_verified':bool(self.verified_login),
                    'verified_login':self.verified_login,'failure':dict(self.failure),'events':[dict(row) for row in self.events]}

    def record(self,code,message):
        with self.lock:self.events.append({'at':int(time.time()),'code':code,'message':message})

    def progress(self,stage,phase,message):
        with self.lock:self.stage=stage;self.stage_at=time.time()
        self.owner.phase=phase;self.record(stage.upper(),message)

    def failed(self,exc,retry=0):
        code,message=safe_failure(exc,self.stage)
        with self.lock:
            self.failure={'at':int(time.time()),'stage':self.stage,'code':code,'message':message}
            self.retry_at=time.time()+retry if retry else 0
        self.record(code,message);self.owner.error='['+code+'] '+message
        self.owner.phase='retry_wait' if retry else 'error'

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                if self.stop.is_set():raise RuntimeError('Предыдущее соединение ещё закрывается; повторите подключение')
                return
            if self.owner.closed:return
            self.stop.clear();self.thread=threading.Thread(target=self.run,daemon=True,name='twitch_scout_irc');self.thread.start()

    def request_send(self,key):
        try:self.jobs.put_nowait(key)
        except queue.Full:self.owner.finish_job(key,'cancelled',error='Очередь прямого чата Twitch занята')

    def live(self):
        return bool(not self.stop.is_set() and not self.owner.closed and not self.owner.worker.stop.is_set()
                    and self.owner.settings().get('transport','browser')=='irc')

    def disconnected(self):
        if self.owner.settings().get('transport','browser')=='irc':
            self.owner.authenticated=False;self.owner.reading=False
        with self.owner.lock:keys=[key for key,job in self.owner.pending.items() if job.get('transport')=='irc' and not job['future'].done()]
        for key in keys:self.owner.finish_job(key,'uncertain',error='Связь чата прервалась; отправка не повторяется')
        while not self.jobs.empty():
            try:self.jobs.get_nowait()
            except queue.Empty:break

    def credentials(self):
        # Chrome is used only as the existing protected persistent cookie store.
        # It is closed before IRC starts; the two transports cannot write together.
        self.owner.browser.start()
        data=self.owner.browser.request('get_session')
        self.owner.browser.request('pause')
        token=data.get('token','')
        if not token:raise InvalidSession('В профиле нет сессии. Вставьте токен своей сессии в разделе «Вход и браузер».')
        return token

    def write_job(self,sock,key):
        text=self.owner.prepare_send(key)
        if text is False:return False
        if text is None:return True
        try:
            with self.owner.lock:
                job=self.owner.pending.get(key)
                if not job or not self.owner.can_send(job['generation']) or job['channel']!=self.owner.channel:
                    if job:self.owner.finish_job(key,'cancelled',error='Отправка отменена после изменения настроек')
                    return True
                frame='PRIVMSG #'+job['channel']+' :'+text
                sock.send(frame);self.owner.wire(frame,True)
        except Exception:
            self.owner.finish_job(key,'uncertain',error='Нет подтверждения отправки; автоматического повтора нет')
            raise
        return True

    def run(self):
        failures=0;token=''
        try:
            with self.lock:self.attempt=0;self.retry_at=0;self.verified_login='';self.failure={}
            self.owner.error='';self.disconnected()
            self.progress('profile','starting','Чтение сохранённой сессии из профиля Chrome.')
            token=self.credentials()
            while self.live():
                try:
                    from websocket import create_connection,WebSocketTimeoutException
                    with self.lock:self.attempt+=1;self.retry_at=0;self.verified_login=''
                    self.owner.error='';self.progress('validate','validating','Проверка сессии на id.twitch.tv.')
                    expected=validate_session(token,self.owner.login)
                    if not self.live():break
                    with self.lock:self.verified_login=expected['login']
                    self.record('SESSION_VERIFIED','Сессия подтверждена для @'+expected['login']+'. Вход в чат ещё не подтверждён.')
                    self.progress('websocket','connecting','Подключение к irc-ws.chat.twitch.tv:443 через защищённый WebSocket.')
                    from .core import _https_context
                    sock=create_connection(IRC_URL,timeout=15,sslopt={'context':_https_context(True)},subprotocols=['irc'],suppress_origin=True)
                    self.socket=sock;sock.settimeout(.2)
                    try:
                        if not self.live():break
                        self.progress('authenticate','authenticating','Соединение WebSocket установлено; ожидается подтверждение аккаунта чатом.')
                        commands=['CAP REQ :twitch.tv/tags twitch.tv/commands','PASS oauth:'+token,'NICK '+self.owner.login]
                        for command in commands:sock.send(command)
                        self.owner.wire('NICK '+self.owner.login,True)
                        deadline=time.monotonic()+AUTH_TIMEOUT;joined=False;connected=False
                        waiting=None;last_ping=last_received=time.monotonic()
                        while self.live():
                            if self.owner.authenticated and self.owner.user_id!=expected['id']:
                                raise InvalidSession('Подключение чата подтвердило другой аккаунт')
                            if self.owner.authenticated and not joined:
                                sock.send('JOIN #'+self.owner.channel);joined=True;deadline=time.monotonic()+JOIN_TIMEOUT
                                self.progress('join','joining','Аккаунт подтверждён чатом; ожидается подключение выбранного канала.')
                            if self.owner.authenticated and self.owner.reading:
                                failures=0
                                if not connected:
                                    connected=True;self.owner.error=''
                                    with self.lock:self.failure={};self.retry_at=0
                                    self.progress('connected','connected','Аккаунт и канал подтверждены; чат подключён.')
                            elif time.monotonic()>deadline:
                                if joined:raise IRCFailure('JOIN_TIMEOUT','Twitch подтвердил аккаунт, но не подтвердил вход в канал. Проверьте логин канала.')
                                raise IRCFailure('AUTH_TIMEOUT','WebSocket открыт, но Twitch не подтвердил ID аккаунта чата за '+str(AUTH_TIMEOUT)+' с.')
                            if waiting is None:
                                try:waiting=self.jobs.get_nowait()
                                except queue.Empty:pass
                            if waiting is not None and self.write_job(sock,waiting):waiting=None
                            try:payload=sock.recv()
                            except WebSocketTimeoutException:
                                if time.monotonic()-last_received>IDLE_TIMEOUT:raise IRCFailure('HEARTBEAT_TIMEOUT','Чат не отвечает на проверку связи.')
                                if time.monotonic()-last_ping>PING_INTERVAL:sock.send('PING :repostbot');last_ping=time.monotonic()
                                continue
                            if not payload:raise ConnectionError('Чат закрыл соединение')
                            if not isinstance(payload,str) or len(payload)>100000:continue
                            last_received=time.monotonic()
                            for line in payload.split('\r\n'):
                                if not line:continue
                                body=line.partition(' ')[2] if line.startswith('@') else line
                                fields=body.split(' ',3);command=fields[1] if body.startswith(':') and len(fields)>1 else fields[0]
                                if line.startswith('PING '):sock.send('PONG '+line[5:])
                                elif command=='NOTICE' and any(value in body.casefold() for value in ('login authentication failed','improperly formatted auth','login unsuccessful','invalid nick')):
                                    raise InvalidSession('Twitch не принял вход в чат. Проверьте сессию и логин аккаунта.')
                                elif command=='CAP' and re.search(r' CAP \S+ NAK(?: |$)',body):raise IRCFailure('CAP_REJECTED','Сервер чата не разрешил запрошенные возможности IRC.')
                                elif command=='RECONNECT':raise IRCFailure('SERVER_RECONNECT','Twitch запросил переподключение чата.')
                                else:
                                    if command=='GLOBALUSERSTATE':
                                        identity=re.search(r'(?:^@|;)user-id=(\d+)(?:;| )',line)
                                        if not identity or identity[1]!=expected['id']:raise InvalidSession('Twitch подтвердил другой аккаунт чата')
                                    self.owner.wire(line)
                    finally:
                        self.socket=None
                        try:sock.close()
                        except Exception:pass
                        finally:
                            try:sock.shutdown()
                            except Exception:pass
                        self.disconnected()
                except InvalidSession:raise
                except Exception as exc:
                    if not self.live():break
                    failures+=1;delay=min(30,2*failures)
                    self.failed(exc,delay)
                    if self.stop.wait(delay):break
        except InvalidSession as exc:
            if self.live():self.failed(exc)
        except Exception as exc:
            if self.live():self.failed(exc)
        finally:
            token='';self.disconnected()
            if self.owner.settings().get('transport','browser')=='irc' and self.owner.phase!='error':
                self.progress('stopped','stopped','Прямой чат остановлен; профиль сессии сохранён.')
            with self.lock:
                if self.thread is threading.current_thread():self.thread=None

    def close(self):
        self.stop.set();sock=self.socket
        if sock:
            try:sock.close()
            except Exception:pass
            finally:
                try:sock.shutdown()
                except Exception:pass
        thread=self.thread
        if thread and thread is not threading.current_thread():thread.join(timeout=20)
