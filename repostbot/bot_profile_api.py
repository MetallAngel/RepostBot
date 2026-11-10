"""Bot API profile operations; no user account session or chat mutations."""
import json
import secrets
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request,urlopen


class ProfileError(Exception):
    def __init__(self,message,retry_after=0):
        super().__init__(message)
        self.retry_after=max(0,int(retry_after or 0))


class BotProfileAPI:
    def __init__(self,token):self.token=token

    def call(self,method,data=None,photo=None):
        from .core import _https_context
        data=data or {}
        if photo is None:
            body=json.dumps(data).encode();mime='application/json'
        else:
            boundary='assistant-profile-'+secrets.token_hex(16)
            parts=[]
            for key,value in data.items():
                parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+key+'"\r\n\r\n'+str(value)+'\r\n').encode())
            parts.extend([('--'+boundary+'\r\nContent-Disposition: form-data; name="avatar"; filename="assistant.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode(),
                          photo,('\r\n--'+boundary+'--\r\n').encode()])
            body=b''.join(parts);mime='multipart/form-data; boundary='+boundary
        request=Request('https://api.telegram.org/bot'+quote(self.token,safe=':')+'/'+method,
                        data=body,headers={'Content-Type':mime,'User-Agent':'RepostBot'})
        try:
            with urlopen(request,timeout=25,context=_https_context(True)) as response:
                result=json.loads(response.read(2_000_000))
        except HTTPError as exc:
            try:result=json.loads(exc.read(8192))
            except (ValueError,TypeError):result={'ok':False,'error_code':exc.code}
        except Exception:
            raise ProfileError('Подтверждение изменения профиля Telegram не получено.') from None
        if not isinstance(result,dict) or not result.get('ok'):
            result=result if isinstance(result,dict) else {}
            delay=(result.get('parameters') or {}).get('retry_after',0)
            code=result.get('error_code','?')
            description=str(result.get('description','')).casefold()
            message=('Telegram назначил паузу смены профиля.' if delay else
                     'Bot API не поддерживает смену фото профиля.' if code==404 or 'method not found' in description else
                     'Telegram отклонил запрос профиля (код '+str(code)+').')
            if description and not delay:message+=' '+description.replace(self.token,'[скрыто]')[:160]
            raise ProfileError(message,delay or (3600 if code==404 else 0))
        return result.get('result')

    def identity(self):
        me=self.call('getMe')
        if not isinstance(me,dict) or not me.get('is_bot') or not str(me.get('id','')).isdigit():
            raise ProfileError('Не удалось подтвердить ID основной помощник.')
        return me

    def snapshot(self,bot):
        name=self.call('getMyName')
        photos=self.call('getUserProfilePhotos',{'user_id':int(bot),'offset':0,'limit':1})
        if not isinstance(name,dict) or not name.get('name') or not isinstance(photos,dict):
            raise ProfileError('Не удалось сохранить исходный профиль.')
        sizes=(photos.get('photos') or [[]])[0]
        raw=b''
        if sizes:
            largest=max(sizes,key=lambda item:int(item.get('width',0))*int(item.get('height',0)))
            from .core import telegram_file_bytes
            raw=telegram_file_bytes(self.token,largest,10_000_000)
            if not raw.startswith(b'\xff\xd8'):
                raise ProfileError('Telegram не предоставил статичный JPEG исходного аватара.')
        return {'name':name['name'],'photo':raw}

    def set_photo(self,raw):
        if raw:
            result=self.call('setMyProfilePhoto',{'photo':json.dumps({'type':'static','photo':'attach://avatar'})},photo=raw)
        else:result=self.call('removeMyProfilePhoto')
        if result is not True:raise ProfileError('Telegram не подтвердил установку аватара.')

    def set_name(self,name):
        if not name.startswith('Assistant') or len(name)>64:raise ValueError('Имя должно начинаться с Assistant.')
        if self.call('setMyName',{'name':name}) is not True:
            raise ProfileError('Telegram не подтвердил смену имени.')
