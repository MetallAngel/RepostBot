"""Local browser control without enabling the JavaScript Runtime domain.

Network events are used only for the scout's existing IRC accounting and fixed
HTTP status diagnostics. No request bodies, headers or response bodies are kept.
"""
import base64
from concurrent.futures import Future
import json
import threading
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


def login_result(raw,integrity=False):
    """Extract fixed, non-secret result fields; never retain the response body."""
    if not isinstance(raw,str) or len(raw)>65536:return {}
    try:body=json.loads(raw)
    except ValueError:return {}
    if not isinstance(body,dict):return {}
    result={}
    if integrity:
        result['integrity_token_received']=bool(isinstance(body.get('token'),str) and body['token'])
        if isinstance(body.get('is_bad_bot'),bool):result['rejected_client']=body['is_bad_bot']
    for field in ('error_code','error_number'):
        if isinstance(body.get(field),int) and not isinstance(body[field],bool):result['error_code']=body[field];break
    return result


class CDPError(RuntimeError):pass


class Connection:
    def __init__(self,url,handler=None):
        import websocket
        # The endpoint is discovered on our private loopback browser, never supplied by a panel user.
        parts=urlsplit(url)
        if parts.scheme!='ws' or parts.hostname!='127.0.0.1':raise CDPError('Недопустимый локальный адрес браузера')
        self.socket=websocket.create_connection(url,timeout=5,suppress_origin=True,http_no_proxy=['127.0.0.1'])
        self.socket.settimeout(1)
        self.handler=handler;self.pending={};self.lock=threading.RLock();self.serial=0;self.closed=False
        self.thread=threading.Thread(target=self.read,daemon=True,name='scout_cdp_events');self.thread.start()

    def call(self,method,params=None,timeout=8):
        if method.startswith('Runtime.'):raise CDPError('JavaScript Runtime не используется контроллером Twitch')
        future=Future()
        with self.lock:
            if self.closed:raise CDPError('Связь с браузером закрыта')
            self.serial+=1;key=self.serial;self.pending[key]=future
            try:self.socket.send(json.dumps({'id':key,'method':method,'params':params or {}}))
            except Exception:
                self.pending.pop(key,None);raise CDPError('Не удалось передать команду браузеру') from None
        try:return future.result(timeout=timeout)
        except TimeoutError:raise CDPError('Браузер не ответил на '+method) from None
        finally:
            with self.lock:self.pending.pop(key,None)

    def read(self):
        from websocket import WebSocketTimeoutException
        try:
            while not self.closed:
                try:raw=self.socket.recv()
                except WebSocketTimeoutException:continue
                if not raw:break
                event=json.loads(raw)
                if 'id' in event:
                    with self.lock:future=self.pending.get(event['id'])
                    if future and not future.done():
                        # Browser messages may contain user data; retain only a protocol code on error.
                        if 'error' in event:future.set_exception(CDPError('Команда браузера отклонена ('+str(event['error'].get('code',''))+')'))
                        else:future.set_result(event.get('result') or {})
                elif self.handler:
                    try:self.handler(event.get('method',''),event.get('params') or {})
                    except Exception:pass
        except Exception:pass
        finally:
            with self.lock:
                self.closed=True
                for future in self.pending.values():
                    if not future.done():future.set_exception(CDPError('Связь с браузером прервалась'))
            if self.handler:
                try:self.handler('connectionClosed',{})
                except Exception:pass

    def close(self):
        self.closed=True
        try:self.socket.close()
        except Exception:pass
        if self.thread is not threading.current_thread():self.thread.join(timeout=2)


class Browser:
    def __init__(self,endpoint,details):
        self.connection=Connection(details['webSocketDebuggerUrl']);self.version=details.get('Browser','').split('/')[-1]
        try:
            opener=build_opener(ProxyHandler({}))
            with opener.open(endpoint+'/json/list',timeout=3) as response:targets=json.load(response)
            target=next((item for item in targets if item.get('type')=='page' and item.get('webSocketDebuggerUrl')),None)
            if not target:raise CDPError('Браузер не открыл страницу')
            self.page=Page(target['webSocketDebuggerUrl'],target.get('url','about:blank'));self.pages=[self.page]
        except Exception:
            self.connection.close();raise

    def close_browser(self):
        try:self.connection.call('Browser.close',timeout=3)
        except CDPError:pass

    def close(self):
        self.page.connection.close();self.connection.close()

    def add_cookies(self,rows):
        for row in rows:
            result=self.page.call('Network.setCookie',row)
            if result.get('success') is False:raise CDPError('Chrome не принял сессию')

    def cookies(self,urls=None):
        return self.page.call('Network.getCookies',{'urls':[urls] if isinstance(urls,str) else urls} if urls else {}).get('cookies',[])


class Socket:
    def __init__(self,url):self.url=url;self.handlers={}
    def on(self,name,callback):self.handlers[name]=callback
    def emit(self,name,value=None):
        callback=self.handlers.get(name)
        if callback:callback(value)


KEYS={'Enter':(13,'Enter','\r'),'Tab':(9,'Tab','\t'),'Backspace':(8,'Backspace',''),
      'Delete':(46,'Delete',''),'Escape':(27,'Escape',''),'ArrowDown':(40,'ArrowDown',''),
      'ArrowUp':(38,'ArrowUp',''),'ArrowLeft':(37,'ArrowLeft',''),'ArrowRight':(39,'ArrowRight',''),
      'Space':(32,'Space',' '),'A':(65,'KeyA','a')}


class Keyboard:
    def __init__(self,page):self.page=page
    def insert_text(self,text):self.page.call('Input.insertText',{'text':text})
    def press(self,chord):
        parts=chord.split('+');name=parts[-1];mods=(2 if 'Control' in parts else 0)|(8 if 'Shift' in parts else 0)
        key,code,text=KEYS[name]
        params={'key':' ' if name=='Space' else 'a' if name=='A' and not mods&8 else name,'code':code,'windowsVirtualKeyCode':key,'nativeVirtualKeyCode':key,'modifiers':mods}
        if name in ('Enter','Space') and not mods:params.update(text=text,unmodifiedText=text)
        down=dict(params,type='keyDown')
        if name=='A' and mods&2:down['commands']=['selectAll']
        self.page.call('Input.dispatchKeyEvent',down)
        release={key:value for key,value in params.items() if key not in ('text','unmodifiedText')}
        self.page.call('Input.dispatchKeyEvent',dict(release,type='keyUp'))


class Mouse:
    def __init__(self,page):self.page=page;self.position=(0,0)
    def click(self,x,y):
        self.position=(x,y)
        self.page.call('Input.dispatchMouseEvent',{'type':'mouseMoved','x':x,'y':y})
        for kind in ('mousePressed','mouseReleased'):
            self.page.call('Input.dispatchMouseEvent',{'type':kind,'x':x,'y':y,'button':'left','clickCount':1})
    def wheel(self,x,y):
        px,py=self.position
        self.page.call('Input.dispatchMouseEvent',{'type':'mouseWheel','x':px,'y':py,'deltaX':x,'deltaY':y})


class Locator:
    def __init__(self,page,selector=None,node=None):self.page=page;self.selector=selector;self.node=node
    @property
    def first(self):return self
    def nodes(self):
        if self.node:return [self.node]
        root=self.page.call('DOM.getDocument',{'depth':0})['root']['nodeId']
        return self.page.call('DOM.querySelectorAll',{'nodeId':root,'selector':self.selector}).get('nodeIds',[])
    def count(self):return len(self.nodes())
    def box(self):
        nodes=self.nodes()
        if not nodes:raise CDPError('Элемент страницы не найден')
        quad=self.page.call('DOM.getBoxModel',{'nodeId':nodes[0]})['model']['border']
        return {'x':min(quad[::2]),'y':min(quad[1::2]),'width':max(quad[::2])-min(quad[::2]),'height':max(quad[1::2])-min(quad[1::2])}
    def is_visible(self):
        try:box=self.box();return box['width']>0 and box['height']>0
        except CDPError:return False
    def wait_for(self,state='visible'):
        deadline=time.monotonic()+self.page.timeout/1000
        while time.monotonic()<deadline:
            if self.is_visible():return
            time.sleep(.1)
        raise CDPError('Элемент страницы не появился')
    def click(self):
        self.wait_for();node=self.nodes()[0]
        self.page.call('DOM.scrollIntoViewIfNeeded',{'nodeId':node});box=self.box()
        self.page.mouse.click(box['x']+box['width']/2,box['y']+box['height']/2)
    def fill(self,text):
        self.wait_for();self.page.bring_to_front();self.page.call('DOM.focus',{'nodeId':self.nodes()[0]})
        self.page.keyboard.press('Control+A');self.page.keyboard.insert_text(text)
    def press(self,key):
        self.page.call('DOM.focus',{'nodeId':self.nodes()[0]});self.page.keyboard.press(key)


class TextMatches:
    def __init__(self,page):self.page=page
    def all(self):
        self.page.call('DOM.getDocument',{'depth':0})
        query='//*[not(self::script) and not(self::style) and (contains(text(),"Your browser is not currently supported") or (contains(text(),"Ваш браузер") and contains(text(),"не поддерживается")))]'
        result=self.page.call('DOM.performSearch',{'query':query});search=result['searchId']
        try:
            count=min(5,result.get('resultCount',0))
            nodes=self.page.call('DOM.getSearchResults',{'searchId':search,'fromIndex':0,'toIndex':count}).get('nodeIds',[]) if count else []
            return [Locator(self.page,node=node) for node in nodes if node]
        finally:self.page.call('DOM.discardSearchResults',{'searchId':search})


class Page:
    def reward_rows(self):
        root=self.call('DOM.getDocument',{'depth':0})['root']['nodeId']
        nodes=self.call('DOM.querySelectorAll',{'nodeId':root,'selector':'.user-notice-line,[data-test-selector="user-notice-line"],[data-test-selector="community-points-redemption-message"],[data-a-target="channel-points-reward-line"]'}).get('nodeIds',[])
        rows=[]
        for node in nodes[-100:]:
            try:
                info=self.call('DOM.describeNode',{'nodeId':node})['node']
                rows.append({'key':info['backendNodeId'],'html':self.call('DOM.getOuterHTML',{'nodeId':node}).get('outerHTML','')[:10000]})
            except CDPError:continue # A notice can scroll out while it is being read.
        return rows

    def __init__(self,url,initial_url='about:blank'):
        self.handlers={};self.sockets={};self.loaded=threading.Event();self.timeout=8000
        self.current_url=initial_url;self.main_frame=None
        self.diagnostic_lock=threading.Lock();self.login_diagnostics=[]
        self.response_rows={};self.ready_responses=[]
        self.keyboard=Keyboard(self);self.mouse=Mouse(self)
        self.connection=Connection(url,self.event)
        # Deliberately no Runtime.enable, Runtime.evaluate or injected scripts.
        self.call('Page.enable');self.call('DOM.enable');self.call('Network.enable',{'maxPostDataSize':0})

    def call(self,method,params=None,timeout=8):return self.connection.call(method,params,timeout)
    def on(self,name,handler):self.handlers[name]=handler
    def set_default_timeout(self,value):self.timeout=value
    @property
    def url(self):
        return self.current_url
    def event(self,method,data):
        key=data.get('requestId');socket=self.sockets.get(key)
        if method=='Network.webSocketCreated':
            socket=Socket(data.get('url',''));self.sockets[key]=socket
            if self.handlers.get('websocket'):self.handlers['websocket'](socket)
        elif method in ('Network.webSocketFrameSent','Network.webSocketFrameReceived') and socket:
            frame=data.get('response') or {}
            if frame.get('opcode')==1:socket.emit('framesent' if method.endswith('Sent') else 'framereceived',frame.get('payloadData',''))
        elif method=='Network.webSocketClosed' and socket:
            socket.emit('close',socket);self.sockets.pop(key,None)
        elif method=='connectionClosed':
            for socket in list(self.sockets.values()):socket.emit('close',socket)
            self.sockets.clear()
        elif method=='Page.frameNavigated' and not (data.get('frame') or {}).get('parentId'):
            frame=data['frame'];self.main_frame=frame.get('id');self.current_url=frame.get('url','about:blank');self.navigation=getattr(self,'navigation',0)+1
        elif method=='Page.navigatedWithinDocument' and data.get('frameId')==self.main_frame:
            self.current_url=data.get('url',self.current_url)
        elif method=='Page.domContentEventFired':self.loaded.set()
        elif method=='Network.responseReceived':
            response=data.get('response') or {};parts=urlsplit(response.get('url',''))
            # Only fixed authentication endpoints. Query strings, headers, body,
            # account name, cookies and request IDs never enter the diagnostics.
            if ((parts.hostname in ('passport.twitch.tv','id.twitch.tv') and parts.path in ('/login','/integrity','/api/login'))
                    or (parts.hostname=='gql.twitch.tv' and parts.path=='/integrity')):
                row={'endpoint':parts.hostname+parts.path,'status':int(response.get('status',0)),'at':int(time.time())}
                with self.diagnostic_lock:
                    self.login_diagnostics=(self.login_diagnostics+[row])[-8:]
                    if key:
                        self.response_rows[key]=row
                        while len(self.response_rows)>16:self.response_rows.pop(next(iter(self.response_rows)))
        elif method=='Network.loadingFinished':
            with self.diagnostic_lock:
                if key in self.response_rows:self.ready_responses=(self.ready_responses+[key])[-16:]
    def inspect_login_results(self):
        with self.diagnostic_lock:
            ready=self.ready_responses[:4];self.ready_responses=self.ready_responses[4:]
        for key in ready:
            with self.diagnostic_lock:row=self.response_rows.pop(key,None)
            if not row:continue
            try:
                data=self.call('Network.getResponseBody',{'requestId':key},timeout=2)
                raw=data.get('body','')
                if data.get('base64Encoded'):raw=base64.b64decode(raw).decode('utf-8')
                result=login_result(raw,row['endpoint'].endswith('/integrity'))
                with self.diagnostic_lock:row.update(result)
            except (CDPError,ValueError,UnicodeError):pass

    def diagnostics(self):
        with self.diagnostic_lock:return list(self.login_diagnostics)
    def goto(self,url,wait_until='domcontentloaded',timeout=15000):
        self.loaded.clear();result=self.call('Page.navigate',{'url':url},timeout=min(timeout/1000,8))
        if result.get('errorText'):raise CDPError('Страница не открылась ('+str(result['errorText'])[:80]+')')
        if result.get('loaderId') and not self.loaded.wait(timeout/1000):raise CDPError('Страница не успела загрузиться')
    def bring_to_front(self):self.call('Page.bringToFront')
    def wait_for_timeout(self,ms):
        if self.connection.closed:raise CDPError('Связь с браузером прервалась')
        time.sleep(ms/1000)
        if self.connection.closed:raise CDPError('Связь с браузером прервалась')
    def screenshot(self,type='jpeg',quality=65):
        return base64.b64decode(self.call('Page.captureScreenshot',{'format':type,'quality':quality,'fromSurface':True,'captureBeyondViewport':False})['data'])
    def set_viewport_size(self,size):
        self.call('Emulation.setDeviceMetricsOverride',{'width':size['width'],'height':size['height'],'deviceScaleFactor':1,'mobile':False})
    def locator(self,selector):return Locator(self,selector)
    def get_by_text(self,pattern):return TextMatches(self)
    def emote_images(self):
        rows=[]
        for node in self.locator('img[src*="/emoticons/"][alt]').nodes():
            attrs=self.call('DOM.getAttributes',{'nodeId':node}).get('attributes',[]);attrs=dict(zip(attrs[::2],attrs[1::2]))
            rows.append({'code':attrs.get('alt',''),'url':attrs.get('src','')})
        return rows
