"""A separate desktop browser with a private persistent profile and local CDP."""
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import Future
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


WS_HOST='irc-ws.chat.twitch.tv'


class ScoutBrowser:
    def __init__(self,owner):
        self.owner=owner;self.profile=owner.store.data_dir/'twitch-scout-browser'
        self.commands=queue.Queue(maxsize=100);self.thread=None;self.stop=threading.Event()
        self.running=False;self.page=None;self.context=None;self.display_process=None;self.start_lock=threading.Lock()
        self.active_socket=None;self.browser_process=None;self.browser_connection=None
        self.browser_version='';self.login_warning=''

    def dependencies(self):
        import importlib.util
        return {'controller':bool(importlib.util.find_spec('websocket')),
                'chromium':bool(self.executable()),'chrome':bool(self.chrome_executable()),
                'display':bool(os.environ.get('DISPLAY') or shutil.which('Xvfb'))}

    @staticmethod
    def chrome_executable():return shutil.which('google-chrome-stable') or shutil.which('google-chrome')

    @classmethod
    def executable(cls):return cls.chrome_executable() or shutil.which('chromium') or shutil.which('chromium-browser')

    def start(self):
        with self.start_lock:
            if self.owner.closed:raise RuntimeError('наблюдатель остановлена')
            if self.thread and self.thread.is_alive():return
            self.stop.clear();self.thread=threading.Thread(target=self.run,daemon=True,name='twitch_scout_browser');self.thread.start()

    def request(self,action,data=None,wait=True):
        if action not in ('login','chat','frame','input','send','pause','forget','emotes','session','get_session','rewards'):raise ValueError('Неизвестное действие браузера')
        if not self.thread or not self.thread.is_alive():
            if action in ('frame','input','send'):raise RuntimeError('Откройте браузер Twitch в разделе наблюдателя')
            self.start()
        future=Future()
        try:self.commands.put_nowait((action,data or {},future))
        except queue.Full:raise RuntimeError('Браузер занят; дождитесь обработки предыдущего действия') from None
        if not wait:return future
        try:return future.result(timeout=15)
        except TimeoutError:
            future.cancel()
            raise RuntimeError('Браузер занят. Проверьте экран перед повторным вводом.') from None

    def chat_url(self):return 'https://www.twitch.tv/popout/'+self.owner.channel+'/chat?popout='

    def attach(self,page):
        self.page=page
        def websocket(ws):
            if urlsplit(ws.url).hostname!=WS_HOST:return
            self.active_socket=ws;self.disconnected()
            ws.on('framesent',lambda payload:self.owner.wire(payload,True) if ws is self.active_socket and not self.monitoring() else None)
            ws.on('framereceived',lambda payload:self.owner.wire(payload) if ws is self.active_socket and not self.monitoring() else None)
            ws.on('close',lambda _socket=None:self.disconnected() if ws is self.active_socket else None)
        page.on('websocket',websocket)
        page.set_default_timeout(8000)

    def monitoring(self):return self.owner.settings().get('transport','browser')=='irc'

    def disconnected(self):
        if self.monitoring():return
        self.owner.reading=False;self.owner.authenticated=False
        with self.owner.lock:keys=[key for key,job in self.owner.pending.items() if job.get('wired') and not job['future'].done()]
        for key in keys:self.owner.finish_job(key,'uncertain',error='Связь Twitch прервалась; автоматического повтора нет')
        if self.owner.phase not in ('stopped','error'):self.owner.phase='waiting'

    def launch(self):
        executable=self.executable()
        if not executable:raise RuntimeError('Браузер Chrome/Chromium ещё не установлен в контейнере')
        self.profile.mkdir(parents=True,exist_ok=True);os.chmod(self.profile,0o700)
        env=dict(os.environ)
        if not env.get('DISPLAY'):
            xvfb=shutil.which('Xvfb')
            if not xvfb:raise RuntimeError('Для браузера в Docker нужен Xvfb; перезапустите контейнер после обновления')
            # A private X socket, no TCP display or VNC port.
            display=':'+str(100+os.getpid()%10000)
            self.display_process=subprocess.Popen([xvfb,display,'-screen','0','1440x1200x24','-nolisten','tcp'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            env['DISPLAY']=display
            for _ in range(30):
                if os.path.exists('/tmp/.X11-unix/X'+display[1:]):break
                if self.display_process.poll() is not None:raise RuntimeError('Виртуальный экран не запустился')
                self.stop.wait(.1)
        # Start the installed desktop browser itself. Do not use Playwright's
        # test-browser launcher, Runtime domain or fingerprint modifications.
        # Only the controller in this container can reach the loopback CDP port;
        # Compose never publishes it. A fresh port is selected on every launch.
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        self.browser_process=subprocess.Popen([executable,'--user-data-dir='+str(self.profile.resolve()),
            '--remote-debugging-address=127.0.0.1','--remote-debugging-port='+str(port),
            '--no-first-run','--no-default-browser-check','--disable-session-crashed-bubble',
            '--no-sandbox','--disable-dev-shm-usage','--window-size=1280,1000','about:blank'],
            env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        endpoint='http://127.0.0.1:'+str(port)
        # Explicitly local: never send the debugging endpoint through an HTTP proxy.
        opener=build_opener(ProxyHandler({}));deadline=time.monotonic()+12
        while time.monotonic()<deadline and not self.stop.is_set():
            if self.browser_process.poll() is not None:
                raise RuntimeError('Браузер Chrome/Chromium завершился при запуске; проверьте память и доступ к профилю')
            try:
                with opener.open(endpoint+'/json/version',timeout=.5) as response:
                    details=json.loads(response.read(16000))
                if details.get('webSocketDebuggerUrl'):break
            except (OSError,ValueError):pass
            self.stop.wait(.1)
        else:raise RuntimeError('Браузер Chrome/Chromium не успел запуститься; повторите перезапуск браузера')
        from .scout_cdp import Browser
        self.browser_connection=Browser(endpoint,details)
        context=self.browser_connection
        self.browser_version=('Google Chrome' if self.chrome_executable()==executable else 'Chromium')+' '+self.browser_connection.version
        page=context.pages[0] if context.pages else context.new_page()
        page.set_viewport_size({'width':1280,'height':900})
        return context

    def shutdown_browser(self):
        """Flush the persistent profile before stopping only our own processes."""
        self.owner.authenticated=False;self.owner.reading=False
        if self.browser_connection:
            try:self.browser_connection.close_browser()
            except Exception:pass  # The target may already be closed.
        if self.browser_process:
            try:self.browser_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.browser_process.terminate()
                try:self.browser_process.wait(timeout=2)
                except subprocess.TimeoutExpired:self.browser_process.kill();self.browser_process.wait()
            self.browser_process=None
        if self.browser_connection:
            try:self.browser_connection.close()
            except Exception:pass
            self.browser_connection=None
        if self.display_process:
            self.display_process.terminate()
            try:self.display_process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.display_process.kill();self.display_process.wait()
            self.display_process=None

    def check_login_warning(self):
        self.login_warning=''
        url=urlsplit(self.page.url)
        if url.hostname not in ('www.twitch.tv','twitch.tv','passport.twitch.tv','id.twitch.tv'):return
        # Read only the presence of a fixed public error, never input values.
        alerts=self.page.get_by_text(re.compile(r'Your browser is not currently supported|Ваш браузер[^.]*не поддерживается',re.I))
        if any(alert.is_visible() for alert in alerts.all()[:5]):
            self.login_warning=('Twitch отклонил вход в '+(self.browser_version or 'браузере')+'. '
                                'Причина по этому сообщению не определена; посмотрите диагностику входа ниже. Сессия не удалена.')

    def perform(self,action,data):
        page=self.page
        if action=='rewards':
            if self.monitoring() and not page.url.startswith('https://www.twitch.tv/popout/'+self.owner.channel+'/chat'):page.goto(self.chat_url(),wait_until='domcontentloaded',timeout=15000)
            if not page.url.startswith('https://www.twitch.tv/popout/'+self.owner.channel+'/chat'):return {'ready':False,'rows':[],'page':page.url}
            ready=bool(page.locator('[data-a-target="chat-scroller"],.chat-scrollable-area__message-container,[data-test-selector="chat-scrollable-area__message-container"]').count())
            channel=urlsplit(page.url).path.split('/')[2]
            return {'ready':ready,'rows':page.reward_rows() if hasattr(page,'reward_rows') else [],'page':str(id(page))+':'+str(getattr(page,'navigation',0)),'channel':channel}
        if action=='get_session':
            rows=self.context.cookies(['https://www.twitch.tv/'])
            return {'token':next((row['value'] for row in rows if row.get('name')=='auth-token'),'')}
        if action=='session':
            self.disconnected();self.active_socket=None
            self.context.add_cookies([{'name':'auth-token','value':data['token'],'domain':'.twitch.tv','path':'/',
                                      'secure':True,'httpOnly':False,'sameSite':'Lax','expires':time.time()+365*86400},
                                     {'name':'login','value':self.owner.login,'domain':'.twitch.tv','path':'/',
                                      'secure':True,'sameSite':'Lax','expires':time.time()+365*86400}])
            self.login_warning=''
            return {'transferred':True}
        if action=='frame':
            for attempt in range(2):
                try:
                    if hasattr(page,'inspect_login_results'):page.inspect_login_results()
                    self.check_login_warning();page.bring_to_front()
                    return page.screenshot(type='jpeg',quality=65)
                except Exception:
                    if attempt:raise
                    page.wait_for_timeout(200)
        if action in ('login','chat'):
            self.login_warning='';self.disconnected();page.goto('https://www.twitch.tv/login' if action=='login' else self.chat_url(),wait_until='domcontentloaded',timeout=15000)
            return {'opened':True}
        if action=='input':
            kind=data.get('kind')
            if kind=='click':
                x,y=float(data.get('x',-1)),float(data.get('y',-1))
                if not 0<=x<1280 or not 0<=y<900:raise ValueError('Точка вне экрана')
                page.mouse.click(x,y)
            elif kind=='text':
                text=str(data.get('text') or '')
                if len(text)>2000:raise ValueError('Слишком длинный ввод')
                page.keyboard.insert_text(text)
            elif kind=='key':
                key=data.get('key')
                if key not in ('Enter','Tab','Shift+Tab','Backspace','Delete','Control+A','Escape','ArrowDown','ArrowUp','ArrowLeft','ArrowRight','Space'):raise ValueError('Недопустимая клавиша')
                page.keyboard.press(key)
            elif kind=='scroll':page.mouse.wheel(0,max(-900,min(900,int(data.get('delta',0)))))
            else:raise ValueError('Неизвестное действие ввода')
            return {'ok':True}
        if action=='emotes':
            if not self.owner.authenticated:raise RuntimeError('Сначала войдите указанным аккаунтом и откройте чат')
            picker=page.locator('[data-a-target="emote-picker-button"]').first
            picker.wait_for(state='visible');picker.click();page.wait_for_timeout(1000)
            rows=page.emote_images() if hasattr(page,'emote_images') else page.locator('img[src*="/emoticons/"][alt]').evaluate_all("nodes => nodes.map(n => ({code:n.alt,url:n.src}))")
            picker.click()
            count=0
            for item in rows:
                match=re.search(r'/emoticons/(?:v2/)?([^/]+)/',item['url'])
                if (not match or not re.fullmatch(r'[^\s<>|]{1,100}',item['code'])
                        or urlsplit(item['url']).hostname!='static-cdn.jtvnw.net'):continue
                with self.owner.store.lock,self.owner.store.db:
                    self.owner.store.db.execute("""INSERT INTO twitch_emote_library(provider,code,source,provider_id,url) VALUES ('twitch',?,?,?,?)
                        ON CONFLICT(provider,code,source) DO UPDATE SET provider_id=excluded.provider_id,url=excluded.url""",
                        (item['code'],self.owner.channel,match[1],item['url']))
                count+=1
            if count and self.owner.channel=='example_channel':self.owner.store.set_state('twitch_emotes_seed_example_channel','1')
            if data.get('learn',True):self.owner.emotes.learn([r['id'] for r in self.owner.emotes.library() if r['source']==self.owner.channel and not r['analyzed']])
            return {'imported':count}
        if action=='send':
            text=self.owner.prepare_send(data['id'])
            if text is False:return {'retry':True}
            if text is None:return {'cancelled':True}
            try:
                field=page.locator('[data-a-target="chat-input"]').first
                field.wait_for(state='visible');field.fill(text)
                # Recheck identity/settings after a potentially slow DOM wait.
                job=self.owner.pending.get(data['id'])
                if not job or not self.owner.can_send(job['generation']):
                    self.owner.finish_job(data['id'],'cancelled',error='Отправка отменена');return {'cancelled':True}
                field.press('Enter')
            except Exception:
                self.owner.finish_job(data['id'],'uncertain',error='Отправка не подтверждена; автоматического повтора нет')
            return {'submitted':True}
        return {}

    def run(self):
        self.running=True
        if not self.monitoring():self.owner.phase='starting';self.owner.error=''
        forget=False;finish_future=None
        try:
            try:
                self.context=self.launch()
                self.attach(self.context.pages[0] if self.context.pages else self.context.new_page())
                if not self.monitoring():self.owner.phase='waiting'
                with self.commands.mutex:
                    navigating=any(action in ('login','chat','session') for action,_,_ in self.commands.queue)
                if self.owner.settings()['enabled'] and self.owner.settings().get('transport','browser')=='browser' and not navigating:
                    try:self.perform('chat',{})
                    except Exception:
                        self.owner.error='Чат Twitch не открылся. Сессия сохранена; повторите «Открыть чат» после восстановления связи.'
                while not self.stop.is_set() and not self.owner.worker.stop.is_set():
                    try:action,data,future=self.commands.get_nowait()
                    except queue.Empty:
                        if hasattr(self.page,'inspect_login_results'):self.page.inspect_login_results()
                        self.page.wait_for_timeout(100);continue
                    if future.cancelled():continue
                    if not future.running() and not future.set_running_or_notify_cancel():continue
                    try:
                        if action in ('pause','forget'):
                            forget=action=='forget';finish_future=future;break
                        result=self.perform(action,data)
                        if isinstance(result,dict) and result.get('retry'):
                            self.commands.put((action,data,future));self.page.wait_for_timeout(100)
                        elif not future.done():future.set_result(result)
                    except Exception as exc:
                        if not future.done():future.set_exception(RuntimeError('Браузер Twitch: '+type(exc).__name__))
            finally:
                self.shutdown_browser();self.context=None
        except Exception as exc:
            if not self.monitoring():
                self.owner.phase='error'
                self.owner.error=str(exc) if isinstance(exc,ImportError) or str(exc).startswith(('Браузер Chrome/Chromium','Для браузера','Виртуальный экран')) else 'Браузер остановился: '+type(exc).__name__
                self.owner.store.event('info','Twitch-наблюдатель: '+self.owner.error[:200])
        finally:
            self.page=None;self.context=None;self.login_warning=''
            self.shutdown_browser();self.active_socket=None
            if forget:shutil.rmtree(self.profile,ignore_errors=True)
            self.running=False
            if not self.monitoring():
                self.owner.authenticated=False;self.owner.reading=False
                if self.owner.phase!='error':self.owner.phase='stopped'
            while not self.commands.empty():
                try:_,_,future=self.commands.get_nowait()
                except queue.Empty:break
                if not future.done():future.set_exception(RuntimeError('Браузер Twitch закрыт'))
            with self.start_lock:
                if self.thread is threading.current_thread():self.thread=None
            if finish_future and not finish_future.done():finish_future.set_result({'stopped':True})

    def forget(self):
        if self.thread and self.thread.is_alive():return self.request('forget')
        shutil.rmtree(self.profile,ignore_errors=True)
        return {'stopped':True}

    def close(self):
        self.stop.set()
        thread=self.thread
        if thread and thread is not threading.current_thread():thread.join(timeout=25)
