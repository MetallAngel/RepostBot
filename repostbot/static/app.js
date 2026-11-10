const $ = s => document.querySelector(s);
let state = {routes: [], route_posts: {}, route_history: {}, route_events: {}, facts: [], profiles: [], memory_people: [], corrections: [], faqs: [], triggers: [], chats: [], events: [], settings: {}};
let activeRouteId = null, aiStatus = {}, diagnostic = {}, updateVersion = '';
const defaultYoutubeTemplate = '{test}<b>🎬 Новое видео: {title}</b>\nКанал: {channel}\nДата выхода: {date}\n\n<a href="{url}">Смотреть на YouTube</a>';
const defaultVkTemplate = '{test}<b>Новая запись VK</b>\n{text}\nДата: {date}\n\n<a href="{url}">Открыть запись</a>{videos}';
const pageNames = {scout:'Аккаунт-наблюдатель',connections:'Подключения', routes:'Маршруты', routeDetail:'Посты маршрута', routeEdit:'Настройки маршрута', answers:'Ответы бота', models:'Нейросети', stickers:'Стикеры и медиа', conversation:'Живое общение', speech:'Озвучка Telegram', memory:'Память и роли чатов', templates:'Шаблоны Telegram', triggers:'Триггеры', twitch:'Книга обид', moderation:'Модерация', activity:'События', 'decision-log':'Журнал решений', social:'Участники и роли', analytics:'Аналитика ИИ', updates:'Обновление'};
function toast(message, error=false) { const el=$('#toast'); el.textContent=message; el.className=error?'error':''; el.style.display='block'; clearTimeout(window.toastTimer); window.toastTimer=setTimeout(()=>el.style.display='none',8000); }
let authEpoch=0;
async function api(path, data) {
  const epoch=authEpoch,controller=new AbortController();
  const bounded=data===undefined&&['session','config','stickers','conversation','ai/status'].includes(path);
  const timer=bounded?setTimeout(()=>controller.abort(),20000):null;
  try{
    const response=await fetch('/api/'+path,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Repostbot-Request':'1'},credentials:'same-origin',signal:controller.signal,body:data===undefined?undefined:JSON.stringify(data)});
    const value=await response.json();
    if(!response.ok){
      if(response.status===401&&path!=='login'&&epoch===authEpoch){$('#dashboard').hidden=true;$('#login').hidden=false;}
      const error=new Error(value.error||'Ошибка запроса');error.status=response.status;throw error;
    }
    if(path==='login'||path==='logout')authEpoch++;
    return value;
  }catch(error){if(error.name==='AbortError')throw new Error('Сервер долго загружает данные. Повторите загрузку.');throw error;}
  finally{if(timer)clearTimeout(timer);}
}

function formData(form) { const result=Object.fromEntries(new FormData(form)); form.querySelectorAll('input[type=checkbox]').forEach(input=>result[input.name]=input.checked); return result; }
const formBaselines = new WeakMap();
function changedFormData(form) {
  const baseline = formBaselines.get(form) || {};
  return Object.fromEntries(Object.entries(formData(form)).filter(([key, value]) =>
    String(value).trim() !== String(baseline[key] ?? '').trim()));
}
function fillForm(form, value) { Object.entries(value).forEach(([key,v])=>{const input=form.elements[key]; if(input) input.type==='checkbox'?input.checked=v===true||v==='1'||v===1:input.value=v??'';}); formBaselines.set(form, formData(form)); }
function dateTime(value) { return value?new Date(Number(value)*1000).toLocaleString('ru-RU'):'—'; }
let scoutLoaded=false, scoutDefaultPrompt='', scoutDraftId='', scoutTestReady=false, scoutTestBusy=false, scoutConnected=false;
function scoutTestControls(){for(const id of ['scoutSendScene','scoutSendDialog'])$('#'+id).disabled=scoutTestBusy||!scoutTestReady;}
async function scoutRunCheck(path,body,resultId){
  if(scoutTestBusy)return;
  scoutTestBusy=true;scoutTestControls();
  try{const result=await api(path,body);$('#'+resultId).textContent='Проверка отправлена в тестовый чат '+result.chat+'. Сообщений: '+(result.message_ids||[]).length+'.';toast('Проверка запущена в тестовом чате');await refreshScout();}
  catch(error){toast(error.message,true);}
  finally{scoutTestBusy=false;scoutTestControls();}
}
async function refreshScout(force=false){
  const data=await api('scout'), status=data.status||{}, actor=status.account||{};
  scoutConnected=status.phase==='connected';
  scoutDefaultPrompt=data.default_prompt||'';
  const phases={disconnected:'Не подключена',connecting:'Подключение',phone:'Нужен вход по номеру',code:'Ожидается код Telegram',password:'Нужен пароль двухэтапной проверки',connected:'Аккаунт подключён',error:'Ошибка подключения'};
  $('#scoutStatus').textContent=(phases[status.phase]||status.phase)+(actor.id?' · '+(actor.username?'@'+actor.username:actor.name)+' · '+actor.id:'')+(status.error?' · '+status.error:'')+(!data.available?' · Telethon пока не установлен':'');
  $('#scoutReceiveStatus').textContent='Последнее событие Telegram: '+dateTime(status.last_received_at)+' · принято к обработке: '+dateTime(status.last_accepted_at)+' · в очереди: '+(status.queue_size||0)+(status.last_skip?' · '+status.last_skip:'');
  $('#scoutHashState').textContent=data.api_hash_saved?'(сохранён)':'';
  const disguise=data.disguise||{}, labels={scout:'наблюдатель',box:'профиль A',profile_b:'профиль B',hidden_c:'ресурс'};
  $('#scoutDisguiseStatus').textContent='Облик: '+(labels[disguise.mode]||'ещё не менялся')+(disguise.desired?' · ожидается: '+labels[disguise.desired]:'')+(disguise.retry_at>Date.now()/1000?' · повтор после '+dateTime(disguise.retry_at):'')+(disguise.next_hide_at>Date.now()/1000?' · новое укрытие после '+dateTime(disguise.next_hide_at):'')+(data.settings.disguise_auto&&disguise.next_auto_at?' · самостоятельная смена после '+dateTime(disguise.next_auto_at):'')+(disguise.error?' · '+disguise.error:'');
  $('#scoutDisguiseButtons').querySelectorAll('button').forEach(control=>control.disabled=!scoutConnected||!data.settings.disguise_enabled||scoutDisguiseBusy);

  if(force||!scoutLoaded){fillForm($('#scoutSettings'),data.settings);fillForm($('#scoutCredentials'),{api_id:data.api_id});scoutLoaded=true;}
  $('#scoutCode').hidden=!['code','password'].includes(status.phase);
  $('#scoutCodeLabel').hidden=status.phase==='password';$('#scoutPasswordLabel').hidden=status.phase!=='password';
  $('#scoutHomeLabel').textContent=data.home?.chat?(data.home.title+' · '+data.home.chat):'Основной чат не указан.';
  scoutTestReady=status.phase==='connected'&&!!actor.id&&data.test_chat?.owner===actor.id;
  $('#scoutTestLabel').textContent=data.test_chat?.chat?(data.test_chat.title+' · '+data.test_chat.chat+(scoutTestReady?(data.settings.test_chat_replies?' · отвечает на обращения':' · ответы на обращения выключены'):' · подключите аккаунт, которым указан этот чат')):'Тестовый чат не указан.';
  scoutTestControls();
  $('#scoutMainGlitchStart').disabled=!data.test_chat?.chat;
  const targets=$('#scoutTargets');targets.replaceChildren();
  (data.targets||[]).forEach(item=>{const row=node('div','','item');row.append(node('strong',item.title),node('p',item.chat+(item.comments_only?' · комментарии канала'+(!item.post_id&&data.settings.joined_chat_replies?' и прямые вызовы наблюдателя в группе':''):' · группа')+(item.post_id?' · пост '+item.post_id:'')+(item.owner!==actor.id?' · требуется вход тем аккаунтом, которым подключена цель':''),'hint'));
    row.append(button(item.enabled?'Приостановить':'Включить',async()=>{await api('scout/target-change',{id:item.id,enabled:!item.enabled});await refreshScout();}),button('Удалить цель',async()=>{await api('scout/target-change',{id:item.id});await refreshScout();}));targets.append(row);});
  const joined=$('#scoutJoinedGroups');joined.replaceChildren();
  (data.joined_groups||[]).forEach(item=>{const entry=node('div','','item');entry.append(node('strong',item.title),node('p',item.chat+' · ответы на обращения'+(item.owner!==actor.id?' · другой аккаунт':''),'hint'));if(item.owner===actor.id)entry.append(button(item.enabled?'Приостановить':'Включить',async()=>{await api('scout/joined-change',{chat:item.chat,enabled:!item.enabled});await refreshScout();}));joined.append(entry);});
  if(!(data.joined_groups||[]).length)joined.append(node('p','Группы появятся здесь при получении новых сообщений после вступления аккаунта.','hint'));
  const history=data.history||{};const waits=(history.chats||[]).filter(row=>row.max_id>row.cleared_id&&row.next_at>Date.now()/1000);
  $('#scoutHistoryStatus').textContent='Групп в очереди очистки: '+(history.pending||0)+(waits.length?' · ближайшая отложенная попытка: '+dateTime(Math.min(...waits.map(row=>row.next_at))):'')+(history.error?' · '+history.error:'');
  $('#scoutHistoryClear').disabled=!scoutConnected||!data.settings.clear_group_history;
  await refreshTwitchScout(force);
  const logs=$('#scoutEvents');logs.replaceChildren();(data.events||[]).forEach(item=>{const row=node('div','','item');row.append(node('strong',dateTime(item.at)+' · '+item.action),node('p',item.reason||'','hint'));if(item.answer)row.append(node('p',item.answer));logs.append(row);});
}
function scoutSubmit(id,path,after){$('#'+id).onsubmit=async event=>{event.preventDefault();const submit=event.submitter;submit.disabled=true;try{const form=event.target,result=await api(path,formData(form));if(after)await after(result,form);else await refreshScout(true);}catch(error){toast(error.message,true);}finally{submit.disabled=false;}};}
let scoutDisguiseBusy=false;
function bindScoutDisguise(){
  for(const [mode,label] of [['scout','Вернуть наблюдателя'],['box','В профиль A'],['profile_b','В профиль B'],['hidden_c','В ресурс']]){
    const card=node('div','','item'), photo=document.createElement('img');photo.src='/api/scout/avatar?mode='+mode;photo.alt=label;photo.width=128;photo.height=128;photo.loading='lazy';card.append(photo,node('p',label));$('#scoutAvatarChoices').append(card);
    $('#scoutDisguiseButtons').append(button(label,async()=>{
      if(scoutDisguiseBusy)return;scoutDisguiseBusy=true;$('#scoutDisguiseButtons').querySelectorAll('button').forEach(control=>control.disabled=true);
      try{await api('scout/disguise',{mode});await refreshScout();toast('Запрос смены профиля обработан; результат указан выше');}catch(error){toast(error.message,true);}finally{scoutDisguiseBusy=false;await refreshScout();}
    }));
  }
}
function bindScout(){
  bindScoutDisguise();
  scoutSubmit('scoutCredentials','scout/settings',async(_result,form)=>{form.elements.api_hash.value='';await refreshScout(true);toast('Подключение сохранено');});
  scoutSubmit('scoutSettings','scout/settings',async()=>{scoutDraftId='';$('#scoutSendTest').disabled=true;await refreshScout(true);toast('Настройки наблюдателя сохранены');});
  scoutSubmit('scoutPhone','scout/code',async(_result,form)=>{form.elements.phone.value='';await refreshScout();toast('Код запрошен. Проверьте Telegram.');});
  scoutSubmit('scoutCode','scout/verify',async(_result,form)=>{form.elements.code.value='';form.elements.password.value='';await refreshScout();});
  ['scoutTarget','scoutHome','scoutTestChat'].forEach(id=>scoutSubmit(id,'scout/target',async()=>{scoutDraftId='';$('#scoutSendTest').disabled=true;await refreshScout();toast('Чат подключён');}));
  scoutSubmit('scoutDialog','scout/test-dialog',async result=>{$('#scoutDialogResult').textContent=result.text||result.reason||'наблюдатель выбрала молчание.';});
  $('#scoutHistoryClear').onclick=async()=>{const button=$('#scoutHistoryClear');button.disabled=true;try{const result=await api('scout/history-clear',{});toast('Проверка истории завершена. Групп в очереди: '+result.pending);await refreshScout();}catch(error){toast(error.message,true);}finally{button.disabled=!scoutConnected||!$('#scoutSettings').elements.clear_group_history.checked;}};
  $('#scoutMainGlitchStart').onclick=async()=>{const button=$('#scoutMainGlitchStart'),out=$('#scoutMainGlitchResult');button.disabled=true;out.hidden=false;out.textContent='Запускаю сбой основной помощник…';try{const data=await api('conversation/glitch-test',{destination:'scout_test',style:$('#scoutMainGlitchStyle').value,text:$('#scoutMainGlitchText').value,replace:true});out.textContent='Основная помощник: '+data.style+'. Тест отправлен в '+data.chat+'. Сбой общий для всех её чатов.';}catch(error){out.textContent=error.message;}finally{button.disabled=false;}};
  $('#scoutMainGlitchStop').onclick=async()=>{try{await api('conversation/glitch-cancel',{});$('#scoutMainGlitchResult').hidden=false;$('#scoutMainGlitchResult').textContent='Общий сбой основной остановлен. Исходный профиль восстановится с учётом ограничений Telegram.';}catch(error){toast(error.message,true);}};
  $('#scoutRefresh').onclick=async()=>{try{if(scoutConnected)await api('scout/connect',{});await refreshScout();}catch(error){toast(error.message,true);}};
  $('#scoutPromptReset').onclick=()=>{$('#scoutSettings').elements.prompt.value=scoutDefaultPrompt;toast('Образ наблюдателя восстановлен в форме. Сохраните настройки.');};
  for(const [id,path] of [['scoutReconnect','scout/connect'],['scoutDisconnect','scout/disconnect']])$('#'+id).onclick=async()=>{try{await api(path,{});await refreshScout();}catch(error){toast(error.message,true);}};
  const exercises=['помощник, основная помощник говорит, что она главнее. Что ответишь?','помощник, тебя заметили! Прячься в профиль A.','помощник, приём! Как слышно?','помощник, что такое северное сияние?','помощник, без шуток: как выключить уведомления?'];
  exercises.forEach((text,index)=>$('#scoutExercises').append(button(['Кто главнее','профиль A и тревога','Связь по связь','Смена темы','Серьёзный вопрос'][index],()=>{$('#scoutDialog').elements.text.value=text;})));
  $('#scoutPreviewScene').onclick=async()=>{const control=$('#scoutPreviewScene');control.disabled=true;try{const result=await api('scout/scene-preview',{scenario:$('#scoutSceneScenario').value});$('#scoutSceneResult').textContent='Репетиция — ничего не опубликовано.\n'+(result.lines||[]).map(line=>(line.actor==='main'?'Основная помощник: ':'наблюдатель: ')+line.text).join('\n\n');}catch(error){toast(error.message,true);}finally{control.disabled=false;}};
  $('#scoutSendScene').onclick=()=>scoutRunCheck('scout/scene-test',{scenario:$('#scoutSceneScenario').value},'scoutSceneResult');
  $('#scoutSendDialog').onclick=()=>scoutRunCheck('scout/dialog-test',{text:$('#scoutDialog').elements.text.value},'scoutDialogResult');
  $('#scoutPreviewReport').onclick=async()=>{const control=$('#scoutPreviewReport');control.disabled=true;try{const result=await api('scout/report-preview',{});scoutDraftId=result.draft_id;$('#scoutReportResult').textContent=result.text||result.reason;$('#scoutSendTest').disabled=!result.text;}catch(error){toast(error.message,true);}finally{control.disabled=false;}};
  $('#scoutSendTest').onclick=async()=>{const control=$('#scoutSendTest');control.disabled=true;try{const result=await api('scout/report-test',{draft_id:scoutDraftId});scoutDraftId='';toast('Пробная сводка отправлена в тестовый чат '+result.chat);await refreshScout();}catch(error){toast(error.message,true);}};
}
document.addEventListener('DOMContentLoaded',bindScout);
function dateOnly(value) { return value?new Date(Number(value)*1000).toLocaleDateString('ru-RU'):'—'; }
function node(tag, text='', className='') { const element=document.createElement(tag); element.textContent=text; if(className)element.className=className; return element; }
function button(label, action, className='outline') { const el=node('button',label,className); el.type='button'; el.onclick=async()=>{el.disabled=true;try{await action(el);}catch(error){toast(error.message,true);}finally{el.disabled=false;}}; return el; }
function link(label, href) { const el=node('a',label,'button-link'); el.href=href; return el; }
function row(parent, title, detail, buttons=[]) { const el=node('div','','item'); el.append(node('strong',title)); if(detail)el.append(node('p',detail)); if(buttons.length){const actions=node('div','','actions');buttons.forEach(([label,action])=>actions.append(button(label,action)));el.append(actions);}parent.append(el);return el; }

function navigate(path, replace=false) { if(replace)history.replaceState(null,'',path);else history.pushState(null,'',path); showPage(); window.scrollTo(0,0); }
const pageSettingsLoads=new Map();
function loadPageSettings(page,loader){
  if(pageSettingsLoads.has(page))return pageSettingsLoads.get(page);
  const section=$('#'+page);if(!section)return Promise.resolve();
  let status=section.querySelector(':scope > .settings-load-status');
  if(!status){status=node('div','','statusbox settings-load-status');status.setAttribute('role','status');section.prepend(status);}
  status.hidden=false;status.textContent='Загружаю сохранённые настройки…';
  section.dataset.settingsLoading='1';section.setAttribute('aria-busy','true');
  section.querySelectorAll('form').forEach(form=>form.inert=true);
  const request=Promise.resolve().then(loader).then(()=>{
    status.hidden=true;delete section.dataset.settingsLoading;section.removeAttribute('aria-busy');
    section.querySelectorAll('form').forEach(form=>form.inert=false);
  }).catch(error=>{
    section.removeAttribute('aria-busy');
    status.replaceChildren(node('p','Сохранённые настройки пока не загружены: '+error.message),button('Повторить загрузку',()=>loadPageSettings(page,loader)));
  }).finally(()=>pageSettingsLoads.delete(page));
  pageSettingsLoads.set(page,request);return request;
}
document.addEventListener('submit',event=>{
  if(event.target.closest('[data-settings-loading="1"]')){event.preventDefault();event.stopImmediatePropagation();toast('Дождитесь загрузки сохранённых настроек.',true);}
},true);
function showPage() {
  let path=location.pathname==='/'?'/connections':location.pathname, page=path.slice(1), match=path.match(/^\/routes\/(\d+)(\/settings)?$/);
  if(match){activeRouteId=Number(match[1]);page=match[2]?'routeEdit':'routeDetail';if(page==='routeEdit'){const route=state.routes.find(r=>r.id===activeRouteId);if(route)editRoute(route,false);else page='routes';}}
  if(path==='/routes/new'){page='routeEdit';$('#routeForm').reset();$('#routeForm').elements.id.value='';$('#routeEditTitle').textContent='Новый маршрут';routeSourceVisibility();}
  if(!pageNames[page])page='connections';
  document.querySelectorAll('.page-section').forEach(section=>section.hidden=section.id!==page);
  document.querySelectorAll('.page-nav a').forEach(a=>a.classList.toggle('active',a.pathname===(page==='routeEdit'||page==='routeDetail'?'/routes':'/'+page)));
  $('#pageTitle').textContent=pageNames[page];
  if(page==='routeDetail'){renderRouteWorkspace();refreshRoutePosts();}
  if(page==='answers'){providerVisibility();refreshAIStatus();}
  if(page==='models')loadPageSettings(page,()=>refreshModels(true));
  if(page==='stickers')loadPageSettings(page,refreshStickerCatalog);
  if(page==='speech')loadPageSettings(page,()=>refreshSpeech(true));
  if(page==='templates')loadPageSettings(page,refreshTemplates);
  if(page==='twitch'){loadPageSettings(page,()=>Promise.all([refreshTwitch(),refreshQuests(),refreshServices()]));}
  if(page==='moderation')loadPageSettings(page,refreshModeration);
  if(page==='decision-log')refreshDecisionLog().catch(error=>toast(error.message,true));
  if(page==='social')loadPageSettings(page,refreshSocial);
  if(page==='analytics')refreshAIAnalytics().catch(error=>toast(error.message,true));
  if(page==='scout')refreshScout().catch(error=>toast(error.message,true));
  if(page==='conversation'){loadPageSettings(page,refreshConversation);refreshReplyPerformance().catch(error=>toast(error.message,true));}
}
document.addEventListener('click',event=>{const a=event.target.closest('a');if(a&&a.origin===location.origin&&!a.target&&!a.hasAttribute('download')&&!event.ctrlKey&&!event.metaKey&&event.button===0){event.preventDefault();navigate(a.pathname);}});
window.addEventListener('popstate',showPage);

const secretFields={telegram_token:{control:'#telegramTokenControl',state:'#tgState',placeholder:'Вставьте токен Telegram'},vk_token:{control:'#vkTokenControl',state:'#vkState',placeholder:'Не нужен для VK без API'},vk_cookie:{control:'#vkCookieControl',state:'#vkCookieState',placeholder:'remixnsid=…; remixsid6=…'},openai_api_key:{control:'#openaiKeyControl',state:'#openaiState',placeholder:'API-ключ OpenAI'},weather_api_key:{control:'#weatherKeyControl',state:'#weatherState',placeholder:'Коммерческий ключ Open-Meteo (необязательно)'}};
function renderSecretField(key, configured, editing=false){const item=secretFields[key],container=$(item.control);container.replaceChildren();$(item.state).textContent=configured?'(сохранён)':'(не задан)';if(configured&&!editing){container.append(node('span','Значение сохранено','secret-saved'),button('Изменить',()=>renderSecretField(key,true,true)));return;}const input=node('input');input.name=key;input.type='password';input.autocomplete='new-password';input.spellcheck=false;input.placeholder=item.placeholder;container.append(input);if(configured)container.append(button('Отмена',()=>renderSecretField(key,true,false),'subtle'));}
function providerVisibility(){const provider=$('#aiProvider').value,image=$('#imageEnabled').checked,chain=provider==='chain';$('#chatgptSettings').hidden=provider!=='chatgpt'&&!chain;$('#openaiSettings').hidden=provider!=='openai'&&!chain;$('#localSettings').hidden=provider!=='local'&&!chain;$('#chainSettings').hidden=!chain;$('#imageSettings').hidden=!image;$('#apiKeySettings').hidden=provider!=='openai'&&!chain&&!image;$('#apiKeySettings').querySelectorAll('input').forEach(input=>input.disabled=provider!=='openai'&&!chain&&!image);if(provider==='local'||chain)refreshModelStatus();}
$('#aiProvider').onchange=providerVisibility;$('#imageEnabled').onchange=providerVisibility;
$('#openaiTest').onclick=async()=>{const result=$('#openaiTestResult');result.textContent='Проверяю…';try{const model=$('#modelLegacyForm').elements.openai_model.value.trim();const response=await api('ai/test',{model});result.textContent=response.message;}catch(error){result.textContent=error.message;result.className='error';}};
$('#openaiModels').onclick=async()=>{const result=$('#openaiTestResult');result.className='hint';result.textContent='Получаю список моделей…';try{const response=await api('ai/models',{}),list=$('#apiModels');list.replaceChildren();(response.models||[]).forEach(item=>{const option=document.createElement('option');option.value=item.id;list.append(option);});result.textContent=`Доступно моделей: ${(response.models||[]).length}. Выберите точный ID и сохраните настройки.`;}catch(error){result.textContent=error.message;result.className='error';}};
$('#imageTest').onclick=async()=>{const result=$('#imageTestResult'),button=$('#imageTest');button.disabled=true;result.className='hint';result.textContent='Генерирую и отправляю тест…';try{const response=await api('image/test',{prompt:$('#imageTestPrompt').value});result.textContent=response.message;}catch(error){result.textContent=error.message;result.className='error';}finally{button.disabled=false;}};
$('#imageDeliveryTest').onclick=async()=>{const result=$('#imageTestResult'),button=$('#imageDeliveryTest');button.disabled=true;result.className='hint';result.textContent='Проверяю доставку маленького PNG…';try{const response=await api('image/delivery-test',{});result.textContent=response.message;}catch(error){result.textContent=error.message;result.className='error';}finally{button.disabled=false;}};

function routeKind(route){return route.source_type==='vk_public'?'VK без API':route.source_type==='vk'?'VK API':route.source_type==='youtube'?'YouTube':'Telegram';}
function renderDeliveryReport(parent,post){const reports=[...(post.deliveries||[])];(post.queue||[]).forEach(q=>{if(!reports.some(r=>r.target===q.target&&r.status==='sent'))reports.push(q);});if(!reports.length){parent.append(node('span','Отправка ещё не выполнялась.','hint'));return;}reports.forEach(r=>{const sent=r.status==='sent',waiting=r.status==='queued',marked=r.status==='marked',unmarked=r.status==='unmarked';const description=sent?'отправлено':marked?'отмечено вручную':unmarked?'ручная отметка снята':waiting?'в очереди':'ошибка';const method=marked||unmarked?'':r.manual?' · ручная отправка':' · автоматическая';const line=node('div',`${r.target} · ${description} · ${dateTime(r.at||r.publish_at)}${method}`,'delivery-line '+(sent||marked?'delivery-ok':waiting||unmarked?'':'delivery-error'));if(r.error)line.append(node('p',r.error));parent.append(line);});}
function cleanVkText(value){return (value||'').replace(/\[#alias\|([^|\]\n]+)\|[^\]\n]*\]/gi,'$1');}
function renderPost(parent,route,post){const card=node('article','','post-card'),head=node('div','','post-head'),youtube=route.source_type==='youtube';const labels={sent:'Опубликован',queued:'В очереди',preview:'Предпросмотр',error:'Ошибка'};head.append(node('strong',youtube?`Видео · ${post.title}`:`Пост ${post.owner_id}_${post.post_id}`),node('span',labels[post.status]||'Новый','post-status'));card.append(head,node('p',`Опубликовано ${youtube?'YouTube':'VK'}: ${youtube?dateOnly(post.published_at):dateTime(post.published_at)} · найдено: ${dateTime(post.first_seen_at)}`,'post-meta'),node('p',cleanVkText(post.text)||'(без текста)','post-text'));
  if(youtube&&post.thumbnail_url){const cover=node('img','','youtube-cover');cover.alt='Обложка видео';cover.loading='lazy';cover.onerror=()=>cover.remove();cover.src=post.thumbnail_url;card.append(cover);}
  if((post.media||[]).length){const gallery=node('div','','post-gallery');post.media.forEach(item=>{const image=node('img');image.loading='lazy';image.alt='Фото публикации';image.onerror=()=>{image.remove();if(!gallery.querySelector('img'))gallery.replaceWith(node('p','Фото сейчас недоступно. Откройте запись VK.','hint'));};image.src=item.preview||item.url;gallery.append(image);});card.append(gallery);}
  const actions=node('div','','actions');actions.append(button('Отправить заново',async()=>{const r=await api('route/resend',{route_id:route.id,source_key:post.source_key});toast(`Отправлено: ${r.sent.length}, ошибок: ${r.failed.length}`+(r.failed.length?' · '+r.failed.map(item=>item.target+': '+item.error).join('; '):''),r.failed.length>0);await refreshRoutePosts();}));const original=link(youtube?'Открыть YouTube':'Открыть VK',post.source_url);original.target='_blank';original.rel='noreferrer';actions.append(original);card.append(actions,node('div','Отчёт отправки','delivery-title'));const reports=node('div','','delivery-report');renderDeliveryReport(reports,post);card.append(reports);parent.append(card);}
function renderPostHistory(parent,route){const rows=state.route_history?.[String(route.id)]||[];if(!rows.length)return;const youtube=route.source_type==='youtube';const section=node('section','','post-history');section.append(node('h3',`${youtube?'История видео':'История постов'} (последние ${new Set(rows.map(item=>item.post_id)).size})`));const scroll=node('div','','post-history-scroll'),table=node('table'),head=node('thead'),heading=node('tr');[youtube?'Видео':'Пост',youtube?'Дата видео YouTube':'Дата поста VK','Спарсен','Получатель','Отправлен / статус','Галочка','Действие'].forEach(label=>heading.append(node('th',label)));head.append(heading);table.append(head);const body=node('tbody');rows.forEach(item=>{const tr=node('tr'),cell=node('td'),original=link(youtube?item.title:`${item.owner_id}_${item.post_id}`,item.source_url);original.target='_blank';original.rel='noopener noreferrer';if(youtube&&item.thumbnail_url){const thumb=node('img','','youtube-thumb');thumb.alt='Обложка видео';thumb.loading='lazy';thumb.onerror=()=>thumb.remove();thumb.src=item.thumbnail_url;cell.append(thumb);}cell.append(original);const checkCell=node('td'),check=node('input');check.type='checkbox';check.checked=Boolean(item.sent_at||item.marked_at);check.disabled=Boolean(item.sent_at);check.setAttribute('aria-label',`${youtube?'Видео '+item.title:'Пост '+item.owner_id+'_'+item.post_id} отправлено в ${item.target}`);check.title=item.sent_at?'Успешная отправка подтверждена ботом':'Не отправлять автоматически: отметить пост вручную';check.onchange=async()=>{check.disabled=true;try{await api('route/mark-sent',{route_id:route.id,source_key:item.source_key,target:item.target,marked:check.checked});await refreshRoutePosts();toast(check.checked?'Автоматическая отправка отключена для поста':'Ручная отметка снята');}catch(error){check.checked=!check.checked;toast(error.message,true);}finally{check.disabled=Boolean(item.sent_at);}};checkCell.append(check);const action=node('td');action.append(button(item.sent_at||item.marked_at?'Переотправить':'Отправить',async()=>{const result=await api('route/resend',{route_id:route.id,source_key:item.source_key,target:item.target});toast(result.failed.length?result.failed.map(f=>f.target+': '+f.error).join('; '):'Отправлено в '+item.target,result.failed.length>0);await refreshRoutePosts();}));const status=item.sent_at?dateTime(item.sent_at):item.marked_at?'Автоматическая отправка отключена '+dateTime(item.marked_at):item.queue_status==='queued'?'В очереди':item.queue_status==='error'?'Повтор после ошибки':'Учтён, не отправлен';tr.append(cell,node('td',youtube?dateOnly(item.published_at):dateTime(item.published_at)),node('td',dateTime(item.first_seen_at)),node('td',item.target),node('td',status),checkCell,action);body.append(tr);});table.append(body);scroll.append(table);section.append(scroll,node('p','«Учтён, не отправлен» — запись не стоит в очереди. Галочка появляется после отправки или ставится вручную, чтобы запретить автоматическую отправку. Сама галочка запись не отправляет. Кнопка «Отправить» отправляет по явному нажатию.','hint'));parent.append(section);}
function renderRouteWorkspace(){const workspace=$('#routeWorkspace'),sameRoute=workspace.dataset.routeId===String(activeRouteId),previewOpen=sameRoute&&Boolean(workspace.querySelector('.post-previews')?.open),previousLog=sameRoute?workspace.querySelector('.route-log'):null,logOpen=Boolean(previousLog?.open),logScroll=previousLog?.querySelector('.items')?.scrollTop||0,previous=sameRoute?workspace.querySelector('.parser-diagnostic'):null,wasOpen=Boolean(previous?.open),previousScroll=previous?.querySelector('pre')?.scrollTop||0,previousPreview=previous?.querySelector('.vk-html-preview'),sameDiagnostic=workspace.dataset.diagnosticAt===String(diagnostic.at||''),previewText=sameDiagnostic&&previousPreview&&!previousPreview.hidden?previousPreview.textContent:null,previewScroll=previousPreview?.scrollTop||0;workspace.dataset.routeId=String(activeRouteId);workspace.dataset.diagnosticAt=String(diagnostic.at||'');workspace.replaceChildren();const route=state.routes.find(r=>r.id===activeRouteId);if(!route){workspace.append(node('p','Маршрут не найден.'));return;}$('#routeDetailTitle').textContent=routeKind(route)+' · '+route.source;
  const panel=node('div','','route-panel');panel.append(node('p',route.last_error||(!route.enabled?'Мониторинг остановлен':`Следующая проверка: ${dateTime(route.next_poll_at)}`),route.last_error?'error':'hint'));const controls=node('div','','buttonrow');if(route.source_type!=='telegram')controls.append(button(route.source_type==='youtube'?'Проверить видео сейчас (без отправки)':'Парсить сейчас (без отправки)',async()=>{try{const result=await api('route/parse',{id:route.id});toast('Найдено записей: '+result.found+' · сохранены без автоматической отправки');}finally{await refreshRoutePosts();}}));if(route.source_type!=='telegram')controls.append(button(route.source_type==='youtube'?'Тест: последнее видео':'Тест: последний пост',async()=>{const result=await api('route/test-latest',{id:route.id});toast(`Тест ${result.post}: отправлено ${result.sent.length}/${route.targets.length}`+(result.failed.length?' · '+result.failed.map(f=>f.target+': '+f.error).join('; '):''),result.failed.length>0);}));if(route.source_type==='vk'||route.source_type==='vk_public'){const streamTest=node('label');streamTest.textContent='Группа для теста';const streamTarget=node('input');streamTarget.placeholder='-1001234567890 или @имя группы';streamTarget.value=route.targets.length===1?route.targets[0]:'';streamTest.append(streamTarget);controls.append(streamTest,button('Найти сегодняшний стрим и тихо закрепить',async()=>{const result=await api('route/test-stream-pin',{id:route.id,target:streamTarget.value.trim()});toast(result.message+(result.failed.length?' · '+result.failed.map(f=>f.target+': '+f.error).join('; '):''),result.failed.length>0);}));}controls.append(link('Настройки маршрута',`/routes/${route.id}/settings`));panel.append(controls);
  if(route.source_type==='telegram')panel.append(node('p','Telegram-публикации поступают через подключённого бота. Отчёты доступны на странице «События».','hint'));else{const log=node('details','','activity-log route-log');log.open=logOpen;const entries=state.route_events?.[String(route.id)]||[];log.append(node('summary',`Журнал маршрута (${entries.length})`));const logRows=node('div','','items');if(!entries.length)logRows.append(node('span','Проверок пока не было','empty'));else entries.forEach(entry=>row(logRows,`${dateTime(entry.at)} · ${entry.level==='error'?'Ошибка':'Событие'}`,entry.detail));log.append(button('Обновить журнал',async()=>refreshRoutePosts()),logRows);panel.append(log);renderPostHistory(panel,route);const previews=node('details','','post-previews');previews.open=previewOpen;const posts=node('div','','posts-grid');const list=state.route_posts[String(route.id)]||[];previews.append(node('summary',`${route.source_type==='youtube'?'Предпросмотр видео':'Предпросмотр постов'} (${list.length})`));if(!list.length)posts.append(node('p','Записи пока не найдены. Нажмите «Проверить сейчас (без отправки)».','hint'));else list.forEach(post=>renderPost(posts,route,post));previews.append(posts);panel.append(previews);const details=node('details','','parser-diagnostic');details.open=wasOpen;details.append(node('summary',route.source_type==='youtube'?'Диагностика YouTube':'Диагностика парсера'));const pre=node('pre',JSON.stringify(diagnostic,null,2));details.append(pre);if(route.source_type==='vk_public'){const slug=route.source.replace(/\/+$/,'').split('/').pop().split('?')[0].replace(/^@/,'');const publicUrl='https://vk.com/'+encodeURIComponent(/^-?\d+$/.test(slug)?'club'+Math.abs(Number(slug)):slug);const actions=node('div','','buttonrow');const direct=link('Открыть группу VK',publicUrl);direct.target='_blank';direct.rel='noopener noreferrer';actions.append(direct);const preview=node('pre','','vk-html-preview');preview.hidden=previewText===null;if(previewText!==null)preview.textContent=previewText;actions.append(button('Показать ответ без Cookie',async()=>{const endpoint=diagnostic.html_available?'diagnostic-html':'public-html';const response=await fetch(`/api/route/${route.id}/${endpoint}`,{credentials:'same-origin'});if(!response.ok){const error=await response.json();throw new Error(error.error||'Не удалось получить страницу VK');}preview.textContent=await response.text();preview.hidden=false;details.open=true;}));details.append(actions,node('p','Ответ получает сервер без Cookie VK. Это исходный HTML, показанный как текст; ссылка на VK откроется в вашем браузере с его обычной сессией. Для проверки страницы без входа используйте приватное окно.','hint'),preview);}panel.append(details);}workspace.append(panel);workspace.querySelector('.route-log .items')?.scrollTo(0,logScroll);if(wasOpen){const current=workspace.querySelector('.parser-diagnostic');current?.querySelector('pre')?.scrollTo(0,previousScroll);current?.querySelector('.vk-html-preview')?.scrollTo(0,previewScroll);}}
function renderLists(){['routeList','faqList','factList','profileList','memoryPeople','correctionList','triggerList','chatList','events'].forEach(id=>$('#'+id).replaceChildren());
  state.routes.forEach(r=>{const entry=row($('#routeList'),routeKind(r)+' · '+r.source,`${r.enabled?'Мониторинг включён':'Остановлен'} · ${r.targets.join(', ')} · проверка ${r.parse_interval} с`,[['Настройки',()=>navigate(`/routes/${r.id}/settings`)],['Удалить',()=>remove('route',r.id)]]);entry.prepend(link('Открыть маршрут',`/routes/${r.id}`));});
  state.faqs.forEach(f=>row($('#faqList'),f.question,f.answer,[['Изменить',()=>fillForm($('#faqForm'),f)],['Удалить',()=>remove('faq',f.id)]]));
  (state.facts||[]).forEach(f=>row($('#factList'),f.name,f.value+(f.triggers?'\nПрямой ответ: '+f.triggers:''),[['Изменить',()=>fillForm($('#factForm'),f)],['Удалить',()=>remove('fact',f.id)]]));
  (state.profiles||[]).forEach(p=>row($('#profileList'),(p.label||'Роль')+' · '+p.chat,p.prompt,[['Изменить',()=>fillForm($('#profileForm'),p)],['Удалить',async()=>{if(confirm('Удалить роль чата?')){await api('profile/delete',{chat:p.chat});await refresh();}}]]));
  (state.memory_people||[]).forEach(person=>{const card=node('details','','item memory-person');card.append(node('summary',`${person.name} · ID ${person.person} · ${person.count} сообщений · ${dateTime(person.last_at)}`));const list=node('div','','memory-messages');person.messages.forEach(message=>{const entry=node('div','','memory-message');entry.append(node('strong',`${dateTime(message.at)} · чат ${message.chat}`),node('p',message.text));list.append(entry);});person.bot_replies.forEach(reply=>{const entry=node('div','','memory-message');entry.append(node('strong',`${dateTime(reply.at)} · отправлено ботом · чат ${reply.chat}`),node('p',`Вопрос: ${reply.question}\nОтвет: ${reply.answer}`));list.append(entry);});person.dialogs.filter(dialog=>!person.bot_replies.some(reply=>reply.question===dialog.question&&reply.answer===dialog.answer)).forEach(dialog=>{const entry=node('div','','memory-message');entry.append(node('strong',`${dateTime(dialog.at)} · сохранённый ответ · чат ${dialog.chat}`),node('p',`Вопрос: ${dialog.question}\nОтвет: ${dialog.answer}`));list.append(entry);});const search=node('div','','memory-search'),input=node('input'),results=node('div','','memory-messages');input.type='search';input.placeholder='Тема: роботы, погода, город…';input.setAttribute('aria-label',`Поиск по памяти: ${person.name}`);search.append(input,button('Найти по теме',async()=>{if(!input.value.trim())return;const found=await api('memory/search?'+new URLSearchParams({person:person.person,q:input.value.trim()}));results.replaceChildren();if(!found.messages.length)results.append(node('p','По этой теме подходящих сообщений нет.','hint'));found.messages.forEach(message=>{const entry=node('div','','memory-message');entry.append(node('strong',`${dateTime(message.at)} · чат ${message.chat}`),node('p',message.text));results.append(entry);});}));card.append(search,results,list);$('#memoryPeople').append(card);});
  (state.corrections||[]).forEach(c=>row($('#correctionList'),`${c.kind==='note'?'Уточнение':'Правка'} · ${c.username?'@'+c.username:c.name+' (ID '+c.person+')'} · ${dateTime(c.at)} · чат ${c.chat}`,`${c.target_username?'О ком: @'+c.target_username+'\n':''}Тема: ${c.question}\n${c.answer?'Раньше бот ответил: '+c.answer+'\n':''}Сообщение: ${c.correction}`,[['Удалить правку',async()=>{if(!confirm('Удалить эту правку из памяти?'))return;await api('memory/correction/delete',{id:c.id});await refresh();}]]));
  const actions={answer:'Готовый текст · без модели',fact:'Постоянные данные · без модели',ai:'Вопрос модели',image:'Генерация изображения'};(state.triggers||[]).forEach(t=>row($('#triggerList'),`${t.enabled?'●':'○'} ${t.name} · приоритет ${t.priority}`,`${t.phrases}\n${actions[t.action]} · ${t.chats||'все разрешённые чаты'}${t.require_address?' · нужно обращение к боту':''}`,[['Изменить',()=>editTrigger(t)],['Удалить',()=>remove('trigger',t.id)]]));
  const selectedFact=$('#triggerFact').value;$('#triggerFact').replaceChildren();$('#triggerFact').append(node('option','Выберите постоянные данные'));$('#triggerFact').firstChild.value='';(state.facts||[]).forEach(f=>{const opt=node('option',f.name);opt.value=f.id;$('#triggerFact').append(opt);});$('#triggerFact').value=selectedFact;
  $('#knownChats').replaceChildren();const emptyMemoryChat=node('option','Выберите чат для добавления');emptyMemoryChat.value='';$('#memoryChatCandidate').replaceChildren(emptyMemoryChat);state.chats.forEach(c=>{const status=c.reply_status||{};row($('#chatList'),c.title+' · '+c.id,`${c.type} · получено: ${dateTime(c.seen_at)}${status.reason?'\n'+dateTime(status.at)+' · '+status.reason:''}`);const opt=node('option',c.title);opt.value=c.id;$('#knownChats').append(opt);const memoryOption=node('option',`${c.title} · ${c.id}`);memoryOption.value=c.id;$('#memoryChatCandidate').append(memoryOption);});
  $('#eventCount').textContent='('+state.events.length+')';state.events.forEach(e=>row($('#events'),dateTime(e.at)+' · '+e.level,e.detail));
  ['routeList','faqList','factList','profileList','memoryPeople','correctionList','triggerList','chatList','events'].forEach(id=>{if(!$('#'+id).children.length)$('#'+id).append(node('span','Записей пока нет','empty'));});
}
let socialRenderedDay = "";
async function refreshSocial(){const data=await api('social'),settingsForm=$('#socialSettingsForm');if(!settingsForm)return;socialRenderedDay=data.day||'';$('#socialTodayDate').textContent='Дата по Москве: '+socialRenderedDay;fillForm(settingsForm,data.settings);const roles=$('#socialRoles'),daily=$('#socialDailyRoles'),permanent=$('#socialPermanentRoles'),achievements=$('#socialAchievements');roles.replaceChildren();daily.replaceChildren();permanent.replaceChildren();achievements.replaceChildren();const roleSelect=$('#socialPermanentRoleKey');if(roleSelect){roleSelect.replaceChildren();(data.roles||[]).forEach(item=>{const option=node('option',item.title+(item.personal?' · персональная, только вручную':' · '+item.polarity));option.value=item.key;roleSelect.append(option);});}(data.roles||[]).forEach(item=>{const label=item.title+(item.personal?' · персональная, только постоянная':' · '+(item.polarity==='positive'?'позитивная':'негативная')+' · '+item.min_score+'…'+item.max_score);const detail=(item.personal?'Один человек на площадку, назначение по ID. Не участвует в случайной выдаче.':'Контексты: '+(item.contexts||[]).join(', '))+'\n'+item.directive;const actions=item.builtin?[]:[['Удалить',async()=>{if(!confirm('Удалить пользовательскую роль?'))return;await api('social/role/delete',{key:item.key});await refreshSocial();}]];row(roles,label,detail,actions);});if(!(data.roles||[]).length)roles.append(node('p','Роли пока не настроены.','empty'));(data.permanent_roles||[]).forEach(item=>{const actions=[['Снять',async()=>{if(!confirm('Снять постоянную роль?'))return;await api('social/permanent-role/delete',{chat:item.chat,person:item.person,role_key:item.role_key});await refreshSocial();}]];row(permanent,`${item.title} · ${item.polarity==='positive'?'позитивная':'негативная'}`,`Чат: ${item.chat} · участник: ${item.person}`,actions);});if(!(data.permanent_roles||[]).length)permanent.append(node('p','Постоянные роли ещё не назначены.','empty'));(data.daily_roles||[]).forEach(item=>row(daily,`${item.title} · ${item.polarity==='positive'?'позитивная':'негативная'}`,`Чат: ${item.chat} · участник: ${item.person} · день: ${item.day} · контекст: ${item.context}`));if(!(data.daily_roles||[]).length)daily.append(node('p','Сегодня дневные роли ещё не выдавались.','empty'));(data.achievements||[]).forEach(item=>row(achievements,`${item.title} · ${dateTime(item.unlocked_at)}`,`${item.detail}\nЧат: ${item.chat} · участник: ${item.person}`));if(!(data.achievements||[]).length)achievements.append(node('p','Достижения ещё не разблокированы.','empty'));if($('#socialRoleHistoryPanel').open)await refreshSocialRoleHistory();await refreshParticipantRoles();}
async function refreshSocialRoleHistory(){const data=await api('social/role-history?'+new URLSearchParams(formData($('#socialRoleHistoryForm')))),list=$('#socialRoleHistory');list.replaceChildren();for(const item of data.events||[])row(list,`${dateTime(item.at)} · ${item.title}`,`${item.kind==='scene'?'Ролевая сцена':item.kind==='permanent'?'Постоянная роль':'Дневная роль'} · ${{revoked:'снята',assigned:'назначена',scene_assigned:'санкция назначена',scene_returned:'возвращение',scene_resolved:'наряд принят',scene_expired:'сцена завершена',scene_notified:'санкция объявлена в чате',personal_cheer:'радостная реакция на подкол'}[item.action]||item.action}\nЧат: ${item.chat} · участник: ${item.person} · дата: ${item.day}`);if(!(data.events||[]).length)list.append(node('p','По выбранным фильтрам записей нет.','empty'));}
$('#socialRoleHistoryForm').onsubmit=async event=>{event.preventDefault();try{await refreshSocialRoleHistory();}catch(error){toast(error.message,true);}};
$('#socialRoleHistoryPanel').addEventListener('toggle',()=>{if($('#socialRoleHistoryPanel').open)refreshSocialRoleHistory().catch(error=>toast(error.message,true));});
setInterval(()=>{if(!$('#social').hidden&&socialRenderedDay){const day=new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());if(day!==socialRenderedDay)refreshSocial().catch(error=>toast(error.message,true));}},60000);
$('#socialSettingsForm').onsubmit=async event=>{event.preventDefault();try{await api('social/settings',formData(event.target));await refreshSocial();toast('Настройки достижений и ролей сохранены');}catch(error){toast(error.message,true);}};
$('#socialRoleForm').onsubmit=async event=>{event.preventDefault();try{await api('social/role',formData(event.target));event.target.reset();await refreshSocial();toast('Пользовательская роль сохранена');}catch(error){toast(error.message,true);}};
$('#socialPermanentRoleForm').onsubmit=async event=>{event.preventDefault();try{await api('social/permanent-role',formData(event.target));await refreshSocial();toast('Постоянная роль назначена');}catch(error){toast(error.message,true);}};
async function refreshDecisionLog(){const module=$('#decisionLogModule').value,chat=$('#decisionLogChat').value.trim(),q=$('#decisionLogQuery').value.trim();const result=await api('decision-log?'+new URLSearchParams({module,chat,q,limit:'300'}));const list=$('#decisionLogRows');list.replaceChildren();(result.events||[]).forEach(item=>{const extra=[item.answer?'Ответ: '+item.answer:'',item.topic?'Тема: '+item.topic:'',item.confidence?'Уверенность: '+item.confidence+'%':'',item.message_id?'Сообщение: '+item.message_id:''].filter(Boolean).join('\n');row(list,`${dateTime(item.at)} · ${item.module} · ${item.action||item.level}${item.chat?' · чат '+item.chat:''}`,item.detail+(extra?'\n'+extra:''));});if(!(result.events||[]).length)list.append(node('p','По выбранным фильтрам записей нет.','empty'));}
$('#decisionLogRefresh').onclick=()=>refreshDecisionLog().catch(error=>toast(error.message,true));['decisionLogModule','decisionLogChat','decisionLogQuery'].forEach(id=>$('#'+id).addEventListener('change',()=>refreshDecisionLog().catch(error=>toast(error.message,true))));
function triggerVisibility(){const action=$('#triggerAction').value;$('#triggerFactSetting').hidden=action!=='fact';$('#triggerPayloadSetting').hidden=action==='fact';$('#triggerFact').required=action==='fact';const label=$('#triggerPayloadLabel');label.firstChild.textContent=action==='answer'?'Текст ответа':action==='image'?'Промпт изображения (пусто — текст пользователя)':'Вопрос модели (пусто — текст пользователя)';$('#triggerPayloadHint').textContent=action==='answer'?'Этот ответ не использует модель и не расходует токены.':'Можно использовать {message} для подстановки исходного сообщения. Память и роль чата передаются текстовой модели автоматически.';}
let triggerPackPreview=null;
function triggerPackReport(result){const el=$('#triggerImportReport');el.hidden=false;el.textContent=`Постоянные данные: +${result.facts_added}, обновлено ${result.facts_updated}, без изменений ${result.facts_unchanged}. Триггеры: +${result.triggers_added}, обновлено ${result.triggers_updated}, без изменений ${result.triggers_unchanged}.`+(result.conflicts.length?'\nКонфликты: '+result.conflicts.join('; '):'');}
$('#triggerExport').onclick=async()=>{try{const pack=await api('triggers/export'),blob=new Blob([JSON.stringify(pack,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='repostbot-triggers-'+new Date().toISOString().slice(0,10)+'.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(error){toast(error.message,true);}};
function invalidateTriggerPreview(){triggerPackPreview=null;$('#triggerImportApply').disabled=true;$('#triggerImportReport').hidden=true;}
$('#triggerImportFile').onchange=invalidateTriggerPreview;$('#triggerImportOverwrite').onchange=invalidateTriggerPreview;
$('#triggerImportPreview').onclick=async()=>{invalidateTriggerPreview();try{const file=$('#triggerImportFile').files?.[0];if(!file)throw new Error('Выберите JSON-файл');if(file.size>1_500_000)throw new Error('Файл слишком большой');const pack=JSON.parse(await file.text()),overwrite=$('#triggerImportOverwrite').checked,result=await api('triggers/import',{pack,overwrite,preview:true});triggerPackReport(result);if(!result.conflicts.length||overwrite){triggerPackPreview={pack,overwrite};$('#triggerImportApply').disabled=false;}}catch(error){toast('Проверка файла: '+error.message,true);}};
$('#triggerImportApply').onclick=async()=>{if(!triggerPackPreview)return;const button=$('#triggerImportApply');button.disabled=true;try{const result=await api('triggers/import',{...triggerPackPreview,preview:false});triggerPackReport(result);triggerPackPreview=null;await refresh();toast('Настройки импортированы');}catch(error){toast(error.message,true);button.disabled=false;}};
function editTrigger(value){$('#triggerForm').reset();fillForm($('#triggerForm'),value);if(value.action==='fact')$('#triggerFact').value=value.payload;triggerVisibility();$('#triggerForm').scrollIntoView({behavior:'smooth',block:'start'});}
$('#triggerAction').onchange=triggerVisibility;$('#triggerReset').onclick=()=>{editTrigger({id:'',action:'answer'});};$('#twitchPreset').onclick=()=>{editTrigger({id:'',name:'Ссылка на Twitch',phrases:'дай ссылку на твич, где твич, ссылка на twitch',action:'fact',payload:'',enabled:true,require_address:false,priority:10});toast('Выберите сохранённую ссылку Twitch из постоянных данных');};
$('#triggerForm').onsubmit=async event=>{event.preventDefault();try{const data=formData(event.target);if(data.action==='fact')data.payload=$('#triggerFact').value;await api('trigger',data);event.target.reset();triggerVisibility();await refresh();toast('Триггер сохранён');}catch(e){toast(e.message,true);}};
$('#telegramDiagnose').onclick=async()=>{const output=$('#telegramDiagnostic');output.hidden=false;output.textContent='Проверяю…';try{const result=await api('telegram/diagnose',{chat_id:$('#diagnosticChat').value});const lines=[`Бот: @${result.username}`,`Privacy Mode: ${result.privacy_disabled?'выключен':'включён; обычные сообщения доступны боту-администратору'}`,`Webhook: ${result.webhook_active?'включён — мешает получению через getUpdates':'выключен'}`,`Ожидает сообщений: ${result.pending_updates}`];if(result.membership)lines.push('Роль в группе: '+result.membership.status+(result.membership.can_send_messages===false?' · отправка запрещена':''));if(result.polling?.at)lines.push('Получение сообщений: '+dateTime(result.polling.at)+' · '+(result.polling.ok?'работает':result.polling.error));if(result.last_message?.reason)lines.push('Последнее сообщение: '+dateTime(result.last_message.at)+' · '+result.last_message.reason);else lines.push('Сообщения из указанной группы пока не поступали. Проверьте права и отправьте /ask@'+result.username+' тест');output.textContent=lines.join('\n');}catch(e){output.textContent=e.message;}};
function routeSourceVisibility(){const form=$('#routeForm'),type=form.elements.source_type.value,youtube=type==='youtube',vk=type==='vk'||type==='vk_public';$('#useVkCookieSetting').hidden=type!=='vk_public';['allowAdsSetting','allowRepostsSetting','photosOnlySetting'].forEach(id=>$('#'+id).hidden=!vk);$('#videoSourcesSetting').hidden=!vk;$('#youtubeMessageSetting').hidden=!youtube;$('#vkMessageSetting').hidden=!vk;$('#routeLimitLabel').textContent=youtube?'Видео за проход':'Постов за проход';form.elements.max_posts.max=youtube?'15':'5';form.elements.source.placeholder=youtube?'https://www.youtube.com/@ExampleChannel/videos':type==='telegram'?'-1001234567890':'https://vk.ru/club123';$('#routeSourceHint').textContent=youtube?'При первой проверке существующие видео сохраняются без отправки, если не включена публикация первых найденных. Далее бот отправляет только новые видео.':'Для проверки VK без авторизации оставьте Cookie выключенным. Уже созданные маршруты сохраняют прежний способ подключения.';$('#routeMediaHint').hidden=youtube;$('#routeTest').textContent=youtube?'Тест: отправить последнее видео':'Тест: отправить последний пост';if(youtube&&!$('#youtubeTemplate').value)$('#youtubeTemplate').value=defaultYoutubeTemplate;}
$('#routeForm').elements.source_type.onchange=()=>{const form=$('#routeForm');form.elements.max_posts.value=form.elements.source_type.value==='youtube'?'15':'5';routeSourceVisibility();};
function editRoute(route,go=true){const form=$('#routeForm');form.reset();fillForm(form,route);form.elements.targets.value=route.targets.join('\n');routeSourceVisibility();$('#youtubeMessagePreview').hidden=true;$('#vkMessagePreview').hidden=true;$('#routeEditTitle').textContent='Настройки · '+route.source;if(go)navigate(`/routes/${route.id}/settings`);}
function insertTemplateText(value, selected=false,kind='youtube'){const field=$(kind==='vk'?'#vkTemplate':'#youtubeTemplate'),start=field.selectionStart,end=field.selectionEnd,selection=field.value.slice(start,end),replacement=selected?value.replace('%s',selection||'текст'):value;field.setRangeText(replacement,start,end,'select');field.focus();$(kind==='vk'?'#vkMessagePreview':'#youtubeMessagePreview').hidden=true;}
$('#youtubeTemplateTools').onclick=event=>{const tag=event.target.closest('[data-template-wrap]')?.dataset.templateWrap;if(!tag)return;insertTemplateText(tag==='a'?'<a href="{url}">%s</a>':`<${tag}>%s</${tag}>`,true);};
$('#youtubeTemplateFields').onclick=event=>{const field=event.target.closest('[data-template-field]')?.dataset.templateField;if(field)insertTemplateText(`{${field}}`);};
$('#youtubeTemplateReset').onclick=()=>{$('#youtubeTemplate').value=defaultYoutubeTemplate;$('#youtubeMessagePreview').hidden=true;};
$('#youtubeTemplate').oninput=()=>$('#youtubeMessagePreview').hidden=true;
function safeTelegramPreview(markup){const source=new DOMParser().parseFromString(markup,'text/html'),allowed=new Set(['B','I','U','S','CODE','BLOCKQUOTE','A']),output=node('div','','telegram-caption');function append(parent,item){if(item.nodeType===Node.TEXT_NODE){parent.append(document.createTextNode(item.textContent));return;}if(item.nodeType!==Node.ELEMENT_NODE)return;const tag=item.tagName;if(!allowed.has(tag)){parent.append(document.createTextNode(item.textContent));return;}const copy=document.createElement(tag.toLowerCase());if(tag==='A'){const url=item.getAttribute('href')||'';if(/^https:\/\/(?:www\.youtube\.com\/watch\?v=[A-Za-z0-9_-]{11}|vk\.com\/wall-?\d+_\d+)$/.test(url)){copy.href=url;copy.target='_blank';copy.rel='noopener noreferrer';}}item.childNodes.forEach(child=>append(copy,child));parent.append(copy);}source.body.childNodes.forEach(item=>append(output,item));return output;}
$('#youtubeTemplatePreviewButton').onclick=async()=>{const el=$('#youtubeMessagePreview');el.replaceChildren();el.hidden=false;try{const form=$('#routeForm'),result=await api('route/format-preview',{id:form.elements.id.value||null,youtube_template:form.elements.youtube_template.value});el.append(node('p',result.photo_caption?`Подпись к обложке · ${result.length}/1024 символов`:`Обложка и отдельный текст · ${result.length}/4096 символов`,'hint'),safeTelegramPreview(result.html));}catch(error){el.append(node('p',error.message,'error'));}};
$('#vkTemplateTools').onclick=event=>{const tag=event.target.closest('[data-template-wrap]')?.dataset.templateWrap;if(tag)insertTemplateText(tag==='a'?'<a href="{url}">%s</a>':`<${tag}>%s</${tag}>`,true,'vk');};
$('#vkTemplateFields').onclick=event=>{const field=event.target.closest('[data-template-field]')?.dataset.templateField;if(field)insertTemplateText(`{${field}}`,false,'vk');};
$('#vkTemplate').placeholder=defaultVkTemplate;
$('#vkTemplateReset').onclick=()=>{$('#vkTemplate').value='';$('#vkMessagePreview').hidden=true;};
$('#vkTemplate').oninput=()=>$('#vkMessagePreview').hidden=true;
$('#vkTemplatePreviewButton').onclick=async()=>{const el=$('#vkMessagePreview');el.replaceChildren();el.hidden=false;try{const form=$('#routeForm'),result=await api('route/vk-format-preview',{id:form.elements.id.value||null,vk_template:form.elements.vk_template.value});el.append(node('p',result.photo_caption?`Подпись к фото · ${result.length}/1024 символов`:`Фото и отдельный текст · ${result.length}/4096 символов`,'hint'),safeTelegramPreview(result.html));}catch(error){el.append(node('p',error.message,'error'));}};
async function remove(kind,id){if(!confirm('Удалить запись?'))return;await api(kind+'/delete',{id});await refresh();toast('Удалено');}
async function refresh(){const data=await api('config');state=data;$('#login').hidden=true;$('#dashboard').hidden=false;$('#connection').textContent=data.bot_username?'● @'+data.bot_username:'Ожидание Telegram';Object.keys(secretFields).forEach(key=>renderSecretField(key,Boolean(data.settings[key+'_configured'])));['settingsForm','replyForm','modelLegacyForm','memoryForm','routeReactionForm','streamPinForm'].forEach(id=>fillForm($('#'+id),data.settings));const preview=$('#routeReactionRoute'),selected=preview.value;preview.replaceChildren(new Option('Свой пример',''));data.routes.forEach(route=>preview.append(new Option(routeKind(route)+' · '+route.source,route.id)));preview.value=[...preview.options].some(option=>option.value===selected)?selected:'';$('#routeReactionPreviewForm').elements.target.value=(data.settings.route_reaction_chats||'').split(/[,\n]/).map(s=>s.trim()).find(Boolean)||'';renderLists();providerVisibility();showPage();refreshAIStatus().catch(error=>toast(error.message,true));refreshUpdateStatus();}
async function refreshRoutePosts(){if(activeRouteId===null||$('#routeDetail').hidden)return;try{const result=await api('route/'+activeRouteId+'/posts');state.route_posts[String(activeRouteId)]=result.posts;state.route_history??={};state.route_history[String(activeRouteId)]=result.history||[];state.route_events??={};state.route_events[String(activeRouteId)]=result.events||[];if(result.route){const index=state.routes.findIndex(r=>r.id===result.route.id);if(index>=0)state.routes[index]=result.route;}diagnostic=result.diagnostic||{};renderRouteWorkspace();if(diagnostic.html_available){const details=$('#routeWorkspace .parser-diagnostic');if(details){const download=link('Скачать HTML ответа VK без Cookie',`/api/route/${activeRouteId}/diagnostic-html`);download.download=`vk-route-${activeRouteId}.html.txt`;details.append(download);}}}catch(error){toast(error.message,true);}}

async function refreshAIStatus(){if($('#dashboard').hidden)return;try{const result=await api('ai/status');if(aiStatus.active!==undefined&&aiStatus.active!==result.chatgpt.active){state.settings.chatgpt_model='';$('#chatgptModel').value='';}aiStatus=result.chatgpt;const previous=$('#chatgptAccount').value;$('#chatgptAccount').replaceChildren();(aiStatus.accounts||[]).forEach(a=>{const opt=node('option',a.label+(a.connected?'':' (отключён)'));opt.value=a.id;$('#chatgptAccount').append(opt);});$('#chatgptAccount').value=aiStatus.active||previous;$('#chatgptStatus').textContent=aiStatus.error|| (aiStatus.pending?'Вход открыт в отдельной вкладке. Завершите авторизацию.':aiStatus.connected?'Аккаунт ChatGPT подключён. Выберите модель и сохраните настройки.':'Аккаунт ChatGPT не подключён.');$('#chatgptStatus').classList.toggle('error',Boolean(aiStatus.error));renderAccountModels(aiStatus.models||[]);const chain=result.chain||{},names={chatgpt:'ChatGPT',openai:'OpenAI API',local:'Ollama'},stateText=Object.entries(names).map(([key,label])=>{const item=chain[key]||{};let status=!item.configured?'не настроен':item.retry_in?'проверка через '+item.retry_in+' с':'готов';if(item.error)status+=' · '+item.error.slice(0,140);return label+': '+status;}).join(' · ');$('#chainStatus').textContent=stateText;$('#aiUsage').textContent=`За 24 часа: ${result.usage.requests} запросов к модели · входных токенов ${result.usage.input_tokens} · выходных ${result.usage.output_tokens}`;}catch(error){$('#chatgptStatus').textContent=error.message;}}
function renderAccountModels(models){const select=$('#chatgptModel'),selected=select.value||state.settings.chatgpt_model||'';select.replaceChildren();select.append(node('option',models.length?'Выберите модель':'Обновите список моделей'));select.firstChild.value='';models.forEach(model=>{const opt=node('option',model.name);opt.value=model.id;select.append(opt);});if(selected&&!models.some(model=>model.id===selected)){const opt=node('option',selected+' (сохранена)');opt.value=selected;select.append(opt);}select.value=selected;}
async function connectChatGPT(accountId=''){$('#chatgptCallbackUrl').value='';$('#chatgptCompleteResult').textContent='';const popup=window.open('about:blank','repostbot-chatgpt');try{const result=await api('chatgpt/connect',{account_id:accountId});$('#chatgptRemoteLogin').open=true;if(popup)popup.location.href=result.url;else{const link=document.createElement('a');link.href=result.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='Открыть страницу входа ChatGPT';$('#chatgptCompleteResult').replaceChildren(link);}await refreshAIStatus();}catch(error){if(popup)popup.close();throw error;}}
$('#chatgptConnect').onclick=()=>connectChatGPT().catch(e=>toast(e.message,true));$('#chatgptReauthorize').onclick=()=>connectChatGPT($('#chatgptAccount').value).catch(e=>toast(e.message,true));
$('#chatgptComplete').onclick=async()=>{const button=$('#chatgptComplete'),field=$('#chatgptCallbackUrl'),result=$('#chatgptCompleteResult');button.disabled=true;result.textContent='Завершаю подключение…';try{await api('chatgpt/complete-url',{url:field.value});field.value='';result.textContent='Аккаунт подключён. Обновите список моделей, выберите модель и сохраните настройки.';await refreshAIStatus();}catch(error){result.textContent=error.message;}finally{button.disabled=false;}};
$('#chatgptAccount').onchange=async()=>{try{await api('chatgpt/select',{account_id:$('#chatgptAccount').value});state.settings.chatgpt_model='';$('#chatgptModel').value='';await refreshAIStatus();}catch(e){toast(e.message,true);}};
$('#chatgptModels').onclick=async()=>{try{const result=await api('chatgpt/models',{});renderAccountModels(result.models);toast('Выберите модель из доступного списка');}catch(e){toast(e.message,true);}};
$('#chatgptCancel').onclick=async()=>{await api('chatgpt/cancel',{});$('#chatgptCallbackUrl').value='';$('#chatgptCompleteResult').textContent='Вход отменён.';await refreshAIStatus();};$('#chatgptLogout').onclick=async()=>{const account=$('#chatgptAccount').value;if(!account)return;try{const result=await api('chatgpt/logout',{account_id:account});await refreshAIStatus();toast(result.warning||'Запись аккаунта удалена из RepostBot',Boolean(result.warning));}catch(e){toast(e.message,true);}};
async function refreshModelStatus(){if(!['local','chain'].includes($('#aiProvider').value))return;try{const s=await api('model/status');$('#modelStatus').textContent=s.installing?'Загрузка модели «'+s.model+'»… '+(s.install_progress||''):s.install_error?'Ошибка загрузки модели: '+s.install_error:!s.connected?'Ollama недоступна. Проверьте сервис контейнера и адрес.':s.ready?'Модель «'+s.model+'» готова.':'Модель не загружена. Доступны: '+(s.available.join(', ')||'нет');}catch(e){$('#modelStatus').textContent=e.message;}}
$('#installLocal').onclick=async()=>{const form=$('#modelLegacyForm');try{await api('local/setup',{url:form.elements.ollama_url.value,model:form.elements.ollama_model.value});toast('Загрузка модели запущена. Статус появится здесь.');refreshModelStatus();}catch(e){toast(e.message,true);}};

async function refreshAIAnalytics(){if($('#dashboard').hidden||$('#analytics').hidden)return;const days=$('#aiAnalyticsDays').value||'7';const report=await api('ai/usage?days='+encodeURIComponent(days));const total=report.total||{};const cost=total.cost_complete?'$'+Number(total.cost_usd||0).toFixed(4):'$'+Number(total.cost_usd||0).toFixed(4)+' + модели без тарифа';$('#aiAnalyticsSummary').textContent=`За ${report.days} дн.: ${total.requests||0} запросов · вход ${total.input_tokens||0} токенов · выход ${total.output_tokens||0} · ориентировочная стоимость ${cost}`;const list=$('#aiAnalyticsRows');list.replaceChildren();(report.rows||[]).forEach(item=>{const itemCost=item.cost_usd===null?'тариф не задан':item.provider==='local'?'$0.0000':'$'+Number(item.cost_usd||0).toFixed(4);row(list,`${item.provider} · ${item.model}`,`${item.requests} запросов · вход ${item.input_tokens} · выход ${item.output_tokens} · ${itemCost}`);});if(!(report.rows||[]).length)list.append(node('p','За этот период обращений к моделям не было.','hint'));}
$('#aiAnalyticsRefresh').onclick=()=>refreshAIAnalytics().catch(error=>toast(error.message,true));$('#aiAnalyticsDays').onchange=()=>refreshAIAnalytics().catch(error=>toast(error.message,true));

$('#loginForm').onsubmit=async event=>{event.preventDefault();try{await api('login',{password:$('#password').value});$('#password').value='';await refresh();}catch(e){toast(e.message,true);}};
$('#logout').onclick=async()=>{await api('logout',{});$('#dashboard').hidden=true;$('#login').hidden=false;};
$('#shutdown').onclick=async()=>{if(!confirm('Выключить бота? Публикация и ответы остановятся до следующего запуска.'))return;try{await api('shutdown',{});$('#dashboard').hidden=true;$('#login').hidden=true;$('#stopped').hidden=false;$('#connection').textContent='Выключен';}catch(e){toast(e.message,true);}};
$('#settingsForm').onsubmit=async event=>{event.preventDefault();try{const data=formData(event.target);data.vk_ssl_verify=data.vk_ssl_verify?'1':'0';await api('settings',data);await refresh();toast('Подключения сохранены');}catch(e){toast(e.message,true);}};
$('#replyForm').onsubmit=async event=>{event.preventDefault();try{const data=changedFormData(event.target);for(const key of Object.keys(data))if(key.startsWith('reply_sticker'))delete data[key];for(const key of ['reply_enabled','image_enabled','reply_vision_enabled','weather_enabled','admin_alerts'])if(key in data)data[key]=data[key]?'1':'0';await api('settings',{...data,_scope:'answers'});await refresh();toast('Настройки ответов сохранены');}catch(e){toast(e.message,true);}};
$('#weatherTest').onclick=async()=>{const result=$('#weatherTestResult');result.hidden=false;result.textContent='Проверяю сохранённые настройки и погодный сервис…';try{const checked=await api('weather/test',{city:$('#weatherTestCity').value});result.textContent=checked.message;}catch(error){result.textContent=error.message;}};
$('#memoryForm').onsubmit=async event=>{event.preventDefault();try{await api('settings',formData(event.target));toast('Настройки памяти сохранены');}catch(e){toast(e.message,true);}};
$('#addMemoryChat').onclick=()=>{const selected=$('#memoryChatCandidate').value, input=$('#memoryForm').elements.memory_chats;if(!selected)return;const current=input.value.split(/[,\n]/).map(value=>value.trim()).filter(Boolean);if(!current.includes(selected))input.value=[...current,selected].join('\n');};
$('#clearMemory').onclick=async()=>{if(!confirm('Очистить краткую память разговоров? Постоянные данные сохранятся.'))return;try{await api('memory/clear',{});toast('Память разговоров очищена');}catch(e){toast(e.message,true);}};
for(const [formId,endpoint] of [['faqForm','faq'],['factForm','fact'],['profileForm','profile']])$('#'+formId).onsubmit=async event=>{event.preventDefault();try{await api(endpoint,formData(event.target));event.target.reset();await refresh();toast('Сохранено');}catch(e){toast(e.message,true);}};
['faq','fact','profile'].forEach(kind=>$('#'+kind+'Reset').onclick=()=>{$('#'+kind+'Form').reset();const id=$('#'+kind+'Form').elements.id;if(id)id.value='';});
$('#mascotPreset').onclick=()=>$('#profileForm').elements.prompt.value='Ты дружелюбный маскот сообщества. Отвечай тепло, живо и с лёгким юмором. Не перегружай ответ эмодзи. Ссылки, реквизиты и факты передавай точно.';
$('#businessPreset').onclick=()=>$('#profileForm').elements.prompt.value='Ты деловой партнёр. Отвечай спокойно, по существу и профессионально. Без шуток и эмодзи. Уточняй недостающие детали. Не придумывай условия и обязательства.';
// Keep the temporary role glitch in its own settings tab while retaining one
// form and one save operation, so old installations preserve their values.
(()=>{const form=$('#conversationForm'),glitch=form?.querySelector('input[name="glitch_enabled"]')?.closest('details');if(!form||!glitch||form.querySelector('[data-task-nav="conversation"]'))return;const nav=document.createElement('div');nav.className='task-tabs';nav.setAttribute('role','tablist');nav.dataset.taskNav='conversation';const generalTab=document.createElement('button');generalTab.type='button';generalTab.setAttribute('role','tab');generalTab.dataset.taskSelect='general';generalTab.setAttribute('aria-selected','true');generalTab.textContent='Основные настройки';const glitchTab=document.createElement('button');glitchTab.type='button';glitchTab.setAttribute('role','tab');glitchTab.dataset.taskSelect='glitch';glitchTab.textContent='Сбои и скрытые испытания';nav.append(generalTab,glitchTab);const general=document.createElement('div');general.className='task-panel';general.dataset.taskGroup='conversation';general.dataset.taskPanel='general';const glitchPanel=document.createElement('div');glitchPanel.className='task-panel';glitchPanel.dataset.taskGroup='conversation';glitchPanel.dataset.taskPanel='glitch';for(const child of [...form.children]){if(child===glitch)glitchPanel.append(child);else general.append(child);}const save=document.createElement('button');save.type='submit';save.textContent='Применить параметры сбоя';save.className='outline';glitchPanel.append(save);const stop=document.createElement('button');stop.type='button';stop.textContent='Немедленно отключить активный сбой';stop.className='outline';stop.onclick=async()=>{stop.disabled=true;try{const result=await api('conversation/glitch-cancel',{});toast(result.message);await refreshConversation();}catch(error){toast(error.message,true);}finally{stop.disabled=false;}};glitchPanel.append(stop);form.prepend(nav,general,glitchPanel);})();
function glitchSetting(name,label,attributes){const wrapper=document.createElement('label');wrapper.textContent=label;const input=document.createElement('input');input.name=name;Object.entries(attributes).forEach(([key,value])=>input.setAttribute(key,value));wrapper.append(input);return wrapper;}
const glitchDelayInput=document.querySelector('input[name="glitch_delay_seconds"]');
if(glitchDelayInput){const delayRow=glitchDelayInput.closest('.inline');const glitchRow1=document.createElement('div');glitchRow1.className='inline';glitchRow1.append(glitchSetting('glitch_recovery_cooldown_seconds','Перезарядка после восстановления, секунд (0 — сразу)',{type:'number',min:'0',max:'86400',value:'300'}),glitchSetting('glitch_recovery_announce_chance','Шанс отдельного сообщения о выходе из сбоя, %',{type:'number',min:'0',max:'100',value:'35'}),glitchSetting('glitch_interactive_chance','Шанс скрытого испытания, %',{type:'number',min:'0',max:'100',value:'20'}),glitchSetting('glitch_ignore_chance','Шанс мягко проигнорировать обычного или положительного участника, %',{type:'number',min:'0',max:'100',value:'10'}));const glitchRow2=document.createElement('div');glitchRow2.className='inline';glitchRow2.append(glitchSetting('glitch_negative_threshold','Порог отрицательной репутации для усиления',{type:'number',min:'-100',max:'0',value:'-20'}),glitchSetting('glitch_negative_bonus','Надбавка к шансу для отрицательной репутации, п.п.',{type:'number',min:'0',max:'100',value:'20'}),glitchSetting('glitch_grudge_conversion_chance','Шанс временно превратить обиду в дружбу, %',{type:'number',min:'0',max:'100',value:'100'}));const glitchRow3=document.createElement('div');glitchRow3.className='inline';glitchRow3.append(glitchSetting('glitch_live_quest_chance','Шанс живого квеста для любого стиля сбоя, %',{type:'number',min:'0',max:'100',value:'35'}),glitchSetting('glitch_live_quest_step_seconds','Пауза между вызовами живого квеста, секунд',{type:'number',min:'20',max:'900',value:'45'}),glitchSetting('glitch_live_quest_penalty','Штраф за полный игнор, баллов',{type:'number',min:'1',max:'100',value:'10'}));const glitchHint=document.createElement('p');glitchHint.className='hint';glitchHint.textContent='Для репутации на уровне порога и ниже к базовому шансу сбоя добавляются процентные пункты. Например, 10% + 20 п.п. = 30%. При срабатывании отрицательная репутация может временно стать дружеским отношением по заданному проценту; запись в Книге обид не удаляется. Живой квест доступен для любого выбранного стиля сбоя: бот зовёт одного участника в группу, учитывает контекст его сообщений, через паузу напоминает и только после второго игнора списывает штраф. Любой ответ этого участника отменяет штраф и даёт бонус; остальные люди квест не завершают. Скрытое испытание отправляется участнику личным ephemeral-сообщением, без кнопок и публикации в группе; его ответ «да» или «нет» продолжает сцену. Команда /roll имеет отдельный лимит: один участник может бросать не чаще раза в минуту, а повтор получает личное уведомление с остатком времени. Отдельное сообщение о выходе из сбоя отправляется с заданной вероятностью; при 0% роль меняется тихо. Для обычных и положительных участников действует отдельный шанс мягкого игнорирования.';delayRow?.after(glitchRow1,glitchRow2,glitchRow3,glitchHint);document.querySelectorAll('#conversationForm .hint').forEach(el=>{if(el.textContent.includes('10 минут'))el.textContent=el.textContent.replace('повторный запуск временно блокируется на 10 минут','повторный запуск блокируется на заданную паузу, по умолчанию на 5 минут');});}
const glitchStyleLabels={calm_style:'помощник ресурс отделения',casual_style:'разговорный стиль — дворовый базар',technical_style:'Техноересь и технический контекст',narrative_style:'Летопись',dnd:'D&D',formal_style:'FormalStyle 40,000',warm_style:'стиль — сладкие речи',custom:'Собственный промпт'};
let glitchStyleChecks=[];let randomGlitchStyleChecks=[];let glitchPromptDefaults={};
$('#glitchPromptReset').onclick=()=>{Object.entries(glitchPromptDefaults).forEach(([style,prompt])=>{const input=$('#conversationForm').elements['glitch_prompt_'+style];if(input)input.value=prompt;});toast('Базовые промпты возвращены. Сохраните параметры сбоя.');};
function syncGlitchStyleChecks(value){const selected=new Set(String(value||'').split(/[,\n]/).map(item=>item.trim()).filter(Boolean));glitchStyleChecks.forEach(input=>{input.checked=selected.has(input.value);});}
const glitchStylesText=document.querySelector('textarea[name="glitch_styles"]');
if(glitchStylesText){const stylesTextLabel=glitchStylesText.closest('label');const stylesSelectLabel=document.querySelector('select[name="glitch_style"]')?.closest('label');if(stylesSelectLabel)stylesSelectLabel.hidden=true;if(stylesTextLabel)stylesTextLabel.hidden=true;const styleChecks=document.createElement('div');styleChecks.className='checks';Object.entries(glitchStyleLabels).forEach(([value,label])=>{const wrapper=document.createElement('label');wrapper.className='check';const input=document.createElement('input');input.type='checkbox';input.name='glitch_style_choice';input.value=value;wrapper.append(input,' '+label);styleChecks.append(wrapper);glitchStyleChecks.push(input);});(stylesTextLabel||stylesSelectLabel)?.after(styleChecks);}const randomGlitchChoices=document.querySelector('#randomGlitchStyleChoices');if(randomGlitchChoices){Object.entries(glitchStyleLabels).forEach(([value,label])=>{const wrapper=document.createElement('label');wrapper.className='check';const input=document.createElement('input');input.type='checkbox';input.value=value;wrapper.append(input,' '+label);randomGlitchChoices.append(wrapper);randomGlitchStyleChecks.push(input);});}function syncRandomGlitchStyleChecks(value){const selected=new Set(String(value||'').split(/[,\n]/).map(item=>item.trim()).filter(Boolean));randomGlitchStyleChecks.forEach(input=>{input.checked=selected.has(input.value);});}
$('#routeForm').onsubmit=async event=>{event.preventDefault();try{const data=formData(event.target);data.targets=data.targets.split(/[,\n]/).map(s=>s.trim()).filter(Boolean);['parse_interval','publish_delay','max_posts','min_length'].forEach(k=>data[k]=Number(data[k]||0));const result=await api('route',data);await refresh();navigate('/routes/'+result.id);toast('Маршрут сохранён');}catch(e){toast(e.message,true);}};
const glitchStylesSelect=document.querySelector('select[name="glitch_style"]');
if(glitchStylesSelect&&!document.querySelector('textarea[name="glitch_styles"]')){const stylesLabel=document.createElement('label');stylesLabel.textContent='Стили сбоя, по одному или через запятую';const stylesInput=document.createElement('textarea');stylesInput.name='glitch_styles';stylesInput.rows=2;stylesInput.placeholder='technical_style, narrative_style, dnd';stylesLabel.append(stylesInput);glitchStylesSelect.closest('label')?.after(stylesLabel);glitchStylesSelect.addEventListener('change',()=>{if(!stylesInput.value.trim()||stylesInput.value.split(/[,\n]/).filter(Boolean).length===1)stylesInput.value=glitchStylesSelect.value;});}
if(!glitchStyleChecks.length){const stylesTextFallback=document.querySelector('textarea[name="glitch_styles"]');if(stylesTextFallback){const stylesTextLabelFallback=stylesTextFallback.closest('label');const stylesSelectLabelFallback=document.querySelector('select[name="glitch_style"]')?.closest('label');if(stylesSelectLabelFallback)stylesSelectLabelFallback.hidden=true;if(stylesTextLabelFallback)stylesTextLabelFallback.hidden=true;const styleChecksFallback=document.createElement('div');styleChecksFallback.className='checks';Object.entries(glitchStyleLabels).forEach(([value,label])=>{const wrapper=document.createElement('label');wrapper.className='check';const input=document.createElement('input');input.type='checkbox';input.name='glitch_style_choice';input.value=value;wrapper.append(input,' '+label);styleChecksFallback.append(wrapper);glitchStyleChecks.push(input);});(stylesTextLabelFallback||stylesSelectLabelFallback)?.after(styleChecksFallback);}}
$('#routeReactionForm').onsubmit=async event=>{event.preventDefault();try{const form=formData(event.target);await api('route-reaction/settings',{enabled:Boolean(form.route_reaction_enabled),game_facts:Boolean(form.route_reaction_game_facts),chance:Number(form.route_reaction_chance),repeat_hours:Number(form.route_reaction_repeat_hours),chats:form.route_reaction_chats,prompt:form.route_reaction_prompt});await refresh();toast('Реакции сохранены');}catch(error){toast(error.message,true);}};
$('#streamPinForm').onsubmit=async event=>{event.preventDefault();try{const form=formData(event.target);await api('stream-pin/settings',{enabled:Boolean(form.stream_pin_enabled),delete_service:Boolean(form.stream_pin_delete_service),chats:form.stream_pin_chats||''});await refresh();toast('Настройки закрепления сохранены');}catch(error){toast(error.message,true);}};
$('#streamPinCheckToday').onclick=async()=>{const result=$('#streamPinCheckResult'),button=$('#streamPinCheckToday');result.hidden=false;result.className='statusbox';result.textContent='Сохраняю настройки и ищу сегодняшние посты VK…';button.disabled=true;try{const form=formData($('#streamPinForm'));await api('stream-pin/settings',{enabled:Boolean(form.stream_pin_enabled),delete_service:Boolean(form.stream_pin_delete_service),chats:form.stream_pin_chats||''});const response=await api('stream-pin/check-today',{});const details=(response.results||[]).map(item=>{const errors=(item.failed||[]).map(f=>`${f.target||item.route}: ${f.error}`).join('; ');return `${item.route}: найдено и закреплено ${item.pinned?.length||0}${errors?`; ошибки: ${errors}`:''}`;}).join('\n');result.textContent=[response.message,details].filter(Boolean).join('\n');}catch(error){result.className='statusbox error';result.textContent=error.message;}finally{button.disabled=false;}};
$('#routeReactionPreviewForm').onsubmit=async event=>{event.preventDefault();const result=$('#routeReactionPreviewResult'),submit=event.target.querySelector('button');result.hidden=false;result.textContent='Готовлю пример…';submit.disabled=true;try{const data=formData(event.target);const answer=await api('route-reaction/preview',{...data,prompt:$('#routeReactionForm').elements.route_reaction_prompt.value});result.textContent=`Публикация (${answer.source}): ${answer.post}\n\n${answer.message}`;}catch(error){result.textContent=error.message;}finally{submit.disabled=false;}};
let stickerCatalog=[],stickerAnalysisTimer=null,stickerPreviewObserver=null;
function watchStickerJob(job){const status=$('#stickerManagerStatus');status.hidden=false;const tick=async()=>{try{const value=await api('stickers/analyze-status',{job_id:job.job_id});status.textContent='Анализ: '+(value.done||0)+' из '+(value.total||job.total)+(value.errors?.length?' · ошибок '+value.errors.length:'');Object.entries(value.results||{}).forEach(([key,contexts])=>{const card=document.querySelector('.sticker-card[data-sticker-key=\"'+CSS.escape(key)+'\"]');const textarea=card?.querySelector('textarea');if(textarea)textarea.value=contexts;});if(value.status==='done'||value.status==='cancelled'){clearInterval(stickerAnalysisTimer);stickerAnalysisTimer=null;}}catch(error){clearInterval(stickerAnalysisTimer);stickerAnalysisTimer=null;status.textContent=error.message;}};if(stickerAnalysisTimer)clearInterval(stickerAnalysisTimer);stickerAnalysisTimer=setInterval(tick,1500);tick();const stop=button('Остановить анализ',async()=>{await api('stickers/analyze-cancel',{job_id:job.job_id});stop.remove();});status.append(' ',stop);}
const stickerPreviewQueue=[];
let activeStickerPreviews=0;
function scheduleStickerPreview(task){
  return new Promise((resolve,reject)=>{
    stickerPreviewQueue.push({task,resolve,reject});
    const pump=()=>{while(activeStickerPreviews<4&&stickerPreviewQueue.length){
      const next=stickerPreviewQueue.shift();activeStickerPreviews++;
      Promise.resolve().then(next.task).then(next.resolve,next.reject).finally(()=>{activeStickerPreviews--;pump();});
    }};pump();
  });
}
async function loadStickerPreview(image){
  if(image.dataset.previewLoaded)return true;
  if(image._previewPromise)return image._previewPromise;
  const status=image._previewStatus;
  if(status)status.textContent='Загрузка миниатюры…';
  image._previewPromise=scheduleStickerPreview(async()=>{
    if(!image.isConnected)return false;
    try{
      const preview=await api('stickers/preview',{key:image.dataset.stickerKey,refresh:image.dataset.previewRefresh==='1'});
      if(typeof preview.image!=='string'||!preview.image.startsWith('data:image/'))throw new Error('Сервер не вернул изображение');
      await new Promise((resolve,reject)=>{
        image.onload=resolve;
        image.onerror=()=>{image.dataset.previewRefresh='1';reject(new Error('Не удалось прочитать изображение. Нажмите «Показать» для повторной загрузки.'));};
        image.src=preview.image;
      });
      image.dataset.previewLoaded='1';delete image.dataset.previewRefresh;
      image.hidden=false;if(status)status.textContent='';return true;
    }catch(error){
      image.hidden=true;image.removeAttribute('src');delete image.dataset.previewLoaded;
      if(status)status.textContent='Миниатюра недоступна: '+error.message;
      return false;
    }finally{image.onload=null;image.onerror=null;}
  });
  try{return await image._previewPromise;}finally{image._previewPromise=null;}
}
async function refreshStickerCatalog(){refreshTwitchEmotes().catch(error=>{$('#twitchEmoteStatus').textContent=error.message;});const data=await api('stickers'),form=$('#stickerManagerForm');stickerCatalog=data.stickers||[];if(form){form.elements.reply_stickers_enabled.checked=data.enabled;};const list=$('#stickerCatalog');if(!list)return;list.replaceChildren();if(!stickerCatalog.length){list.append(node('p','Стикеры пока не добавлены. Импортируйте набор или добавьте строки вручную.','hint'));return;}const previewTasks=[];const loadPreview=loadStickerPreview;const packName=item=>item.pack||'Без набора';stickerCatalog.sort((a,b)=>{const ap=packName(a),bp=packName(b);if(ap==='Без набора'&&bp!=='Без набора')return 1;if(bp==='Без набора'&&ap!=='Без набора')return -1;return ap.localeCompare(bp,'ru')||a.key.localeCompare(b.key,'ru');});let lastPack='',groupTarget=list,currentGroup=null;stickerCatalog.forEach((item,index)=>{if(item.pack!==lastPack){lastPack=packName(item);const group=document.createElement('details');group.className='sticker-group';group.dataset.stickerPack=lastPack;currentGroup=group;group.addEventListener('toggle',()=>{if(group.open)previewTasks.filter(task=>task.group===group).forEach(task=>{if(stickerPreviewObserver)stickerPreviewObserver.observe(task.image._previewStatus);else loadPreview(task.image);});});const summary=document.createElement('summary');summary.textContent=lastPack+' ('+stickerCatalog.filter(value=>packName(value)===lastPack).length+')';group.append(summary);const body=node('div','','sticker-catalog');const groupActions=node('div','','buttonrow');const loadGroupButton=button('Загрузить миниатюры',async()=>{loadGroupButton.disabled=true;const tasks=previewTasks.filter(task=>task.group===group);for(let offset=0;offset<tasks.length;offset+=4)await Promise.all(tasks.slice(offset,offset+4).map(task=>loadPreview(task.image)));loadGroupButton.disabled=false;});groupActions.append(loadGroupButton);body.append(groupActions);group.append(body);list.append(group);groupTarget=body;}const card=node('div','','sticker-card'),check=document.createElement('input');card.dataset.stickerKey=item.key;check.type='checkbox';check.className='sticker-select';check.value=item.key;card.classList.toggle('sticker-disabled',!item.enabled);card.append(check,node('strong',item.key),node('span',' · '+(item.format||'static'),'hint'));if(item.learning_pending)card.append(node('span',' · ожидает анализа','hint'));if(item.safe_for_send===false&&!item.learning_pending)card.append(node('span',item.pack==='Банворды'?' · Банворды':' · бот не отправляет','hint'));if(item.blocked_count)card.append(node('span',' · удалений: '+item.blocked_count,'hint'));const image=node('img');image.alt='';image.dataset.stickerKey=item.key;image.hidden=true;image._previewStatus=node('p','Миниатюра не загружена.','hint');image._previewStatus._previewImage=image;card.append(image,image._previewStatus);card.append(node('p','file_id: '+item.file_id,'hint'));const desc=node('textarea');desc.value=item.contexts||item.description||'';desc.placeholder='Ситуации: лошади, поддержка, испуг...';desc.style.height='auto';desc.style.height=Math.min(240,Math.max(70,desc.scrollHeight))+'px';card.append(desc);const botStatus=node('p','','hint'),botCheck=node('input'),botLabel=node('label','','check');botCheck.type='checkbox';botLabel.append(botCheck,' Боту запрещено отправлять');const syncBotPolicy=()=>{const review=item.bot_review||{},approved=review.confidence==='high'&&['safe','playful'].includes(review.kind),banned=item.pack==='Банворды';botLabel.hidden=item.origin!=='learned'&&!banned;botStatus.hidden=botLabel.hidden;botCheck.checked=banned||!approved||Boolean(review.manual_blocked);botCheck.disabled=banned||!approved;botStatus.textContent=banned?('Банворды: отправка ботом запрещена всегда. '+(item.ban_evidence?.source?('Основание: '+({ocr:'надпись на стикере',local_ocr:'надпись на стикере',vision:'анализ изображения',description:'описание',manual:'ручная блокировка'}[item.ban_evidence.source]||'проверка стикера')+(item.ban_evidence.reason?' — '+item.ban_evidence.reason:'')):'Причина не сохранена в старой версии.')):approved?(review.manual_blocked?'Отправка ботом выключена вручную.':'Модель уверенно разрешила отправку: '+(review.kind==='playful'?'шуточный стикер.':'безопасный стикер.')):(review.confidence==='high'?({insult:'Модель определила оскорбление: бот не отправляет.',hate:'Модель определила ненависть: бот не отправляет.',threat:'Модель определила угрозу: бот не отправляет.',adult:'Модель определила пошлость: бот не отправляет.'}[review.kind]||'Обучение: нет уверенного допуска к отправке.'):'Обучение: бот не отправляет стикер без уверенной оценки модели.')+(review.reason?' '+review.reason:'');botCheck.title=banned?'Сначала снимите блокировку Банвордов.':!approved?'Нажмите «Анализировать»: для разрешения нужна высокая уверенность модели.':'';};syncBotPolicy();botCheck.onchange=async()=>{const blocked=botCheck.checked;botCheck.disabled=true;try{const updated=await api('stickers/bot-permission',{key:item.key,blocked});Object.assign(item,updated);toast(blocked?'Бот больше не отправляет этот стикер':'Отправка уверенно проверенного стикера разрешена');}catch(error){toast(error.message,true);}finally{syncBotPolicy();}};card.append(botLabel,botStatus);const actions=node('div','','buttonrow');const toggleButton=button(item.enabled?'Отключить':'Включить',async()=>{await api('stickers/toggle',{key:item.key,enabled:!item.enabled});item.enabled=!item.enabled;toggleButton.textContent=item.enabled?'Отключить':'Включить';card.classList.toggle('sticker-disabled',!item.enabled);toast(item.enabled?'Стикер включён':'Стикер отключён');});const deleteButton=button('Удалить',async()=>{if(!confirm('Удалить этот стикер из каталога?'))return;deleteButton.disabled=true;await api('stickers/delete',{key:item.key});const group=card.closest('.sticker-group');card.remove();item._deleted=true;const remaining=group?.querySelectorAll('.sticker-card').length||0;if(group){if(remaining){const summary=group.querySelector('summary');if(summary)summary.textContent=(group.dataset.stickerPack||'Без набора')+' ('+remaining+')';}else group.remove();}toast('Стикер удалён');});actions.append(button('Показать',async()=>{await loadPreview(image);}),button(item.analyzed?'Повторно анализировать':'Анализировать',async()=>{const result=await api('stickers/analyze',{key:item.key});desc.value=result.contexts||desc.value;item.contexts=result.contexts||desc.value;item.analyzed=true;const updated=(result.stickers||[]).find(value=>value.key===item.key);if(updated){const oldPack=item.pack;Object.assign(item,updated);if(oldPack!==item.pack)await refreshStickerCatalog();}syncBotPolicy();toast('Описание и допуск к отправке проверены для '+item.key);}),button('Сохранить описание',async()=>{const result=await api('stickers/semantic',{key:item.key,contexts:desc.value});const updated=(result.stickers||result||[]).find(value=>value.key===item.key);if(updated)Object.assign(item,updated);syncBotPolicy();toast('Описание сохранено');}),toggleButton,deleteButton);card.append(actions);groupTarget.append(card);previewTasks.push({image,group:currentGroup});});const observer='IntersectionObserver' in window?new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){loadPreview(entry.target._previewImage);observer.unobserve(entry.target);}}),{rootMargin:'320px'}):null;stickerPreviewObserver=observer;previewTasks.forEach(task=>{if(task.group.open){if(observer)observer.observe(task.image._previewStatus);else loadPreview(task.image);}});}
api('stickers/analyze-status').then(active=>{if(active?.length)watchStickerJob(active[0]);}).catch(()=>{});
$('#stickerManagerForm').onsubmit=async event=>{event.preventDefault();try{const data=formData(event.target);delete data.sticker_pack;await api('stickers/settings',data);await refreshStickerCatalog();toast('Настройки стикеров сохранены');}catch(error){toast(error.message,true);}};
$('#stickerImportPack').onclick=async()=>{const pack=$('#stickerManagerForm').elements.sticker_pack.value.trim();if(!pack){toast('Вставьте ссылку на набор',true);return;}try{const result=await api('stickers/import-pack',{pack});$('#stickerManagerForm').elements.sticker_pack.value='';await refreshStickerCatalog();toast('Набор '+result.pack+': Telegram вернул '+result.received+', добавлено '+result.added+', уже в каталоге '+result.existing+'.'+(result.unavailable?' Недоступно для отправки: '+result.unavailable+'.':''));}catch(error){toast(error.message,true);}};
$('#stickerManualAdd').onclick=async()=>{try{const extra=$('#stickerManualLines').value.trim();if(!extra)throw new Error('Введите строки стикеров');const result=await api('stickers/add-lines',{lines:extra});$('#stickerManualLines').value='';await refreshStickerCatalog();toast('Добавлено новых стикеров: '+result.added+'. Существующие записи сохранены без дублей.');}catch(error){toast(error.message,true);}};
$('#stickerSelectUnanalyzed').onclick=()=>document.querySelectorAll('.sticker-select').forEach(input=>{const item=stickerCatalog.find(value=>value.key===input.value);input.checked=Boolean(item&&!item.analyzed);});$('#stickerSelectAll').onclick=()=>document.querySelectorAll('.sticker-select').forEach(input=>input.checked=true);$('#stickerSelectNone').onclick=()=>document.querySelectorAll('.sticker-select').forEach(input=>input.checked=false);
$('#stickerAnalyzeSelected').onclick=async()=>{const keys=[...document.querySelectorAll('.sticker-select:checked')].map(input=>input.value);if(!keys.length){toast('Выберите стикеры',true);return;}try{const job=await api('stickers/analyze-bulk',{keys});watchStickerJob(job);}catch(error){toast(error.message,true);}};
document.querySelectorAll('#replyForm [name="reply_stickers_enabled"],#replyForm [name="reply_stickers"],#replyForm [name="reply_sticker_chance"],#replyForm [name="reply_sticker_default_key"]').forEach(input=>{const label=input.closest('label');if(label)label.hidden=true;});
function updateConversationCountdowns(){document.querySelectorAll('[data-conversation-until]').forEach(el=>{const remaining=Math.ceil(Number(el.dataset.conversationUntil)-Date.now()/1000);el.textContent=remaining>0?'Можно проверить снова через '+Math.floor(remaining/60)+' мин '+remaining%60+' сек.':'Пауза истекла; ждёт нового сообщения.';});}
function renderGlitchProfile(profile){const p=profile||{};$('#glitchProfileStatus').textContent=(p.mode&&p.mode!=='normal'?'Облик: '+(glitchStyleLabels[p.mode]||p.mode):'Обычный профиль')+(p.name?' · '+p.name:'')+(p.original_saved?' · исходный профиль сохранён':' · исходный профиль будет сохранён перед первой сменой')+(p.enabled===false?' · смена профиля выключена':p.desired&&p.desired!==p.mode?' · ожидается облик: '+(glitchStyleLabels[p.desired]||p.desired):'')+(p.pending?' · смена ещё не подтверждена':'')+(p.retry_at>Date.now()/1000?' · повтор после '+dateTime(p.retry_at):'')+(p.error?' · '+p.error:'');}
for(const [id,path] of [['glitchProfileSync','glitch-profile-sync'],['glitchProfileRestore','glitch-profile-restore']])$('#'+id).onclick=async()=>{const button=$('#'+id);button.disabled=true;try{const profile=await api('conversation/'+path,{});renderGlitchProfile(profile);if(id==='glitchProfileRestore'){const form=$('#conversationForm');form.elements.glitch_profile_enabled.checked=false;}toast(profile.error||'Состояние профиля проверено',!!profile.error);}catch(error){toast(error.message,true);}finally{button.disabled=false;}};
function renderConversationActivity(data){renderGlitchProfile(data.glitch_profile);const c=data.settings,p=data.presence||{},overview=[c.enabled?'Живое общение включено':'Живое общение выключено',`Инициатива: Telegram ${c.telegram_enabled?'вкл.':'выкл.'}, Twitch ${c.twitch_enabled?'вкл.':'выкл.'}`,`Шанс анализа ${c.chance}%; пауза чата ${c.cooldown_seconds} с, пользователя ${c.person_cooldown_seconds} с, анализа ${c.analysis_pause_seconds} с.`+(c.idle_enabled?` После тишины: ${c.idle_seconds} с + случайно до ${c.idle_random_seconds||0} с.`:'' ),`Живые действия: ${p.enabled&&p.telegram_enabled?'вкл.':'выкл.'}${p.enabled&&p.telegram_enabled?`; шанс ${p.chance}% за затишье, ожидание ${p.quiet_seconds}–${p.quiet_seconds+(p.quiet_random_seconds||0)} с, пауза ${p.cooldown_seconds} с, ${p.delete_unanswered?`до удаления ${p.message_ttl_seconds} с`:`без удаления; ожидание реакции ${p.message_ttl_seconds} с`}.`:''}`];if(c.telegram_enabled&&!data.reply_enabled)overview.push('Ответы Telegram выключены в «Ответах и моделях».');if(c.media_enabled&&!data.vision_enabled)overview.push('Для инициативы на медиа включите анализ фото и стикеров в «Ответах и моделях».');$('#conversationOverview').textContent=overview.join('\n');const actions={sent:'Отправлено',silent:'Молчание',stale:'Устарело',repeat:'Повтор пропущен',not_sent:'Не доставлено',error:'Ошибка',glitch_quest_cooldown:'Скрытый откат испытания',glitch_quest_resolved:'Скрытое испытание завершено'};for(const [id,values] of [['conversationEvents',data.events],['conversationDiagnostics',data.statuses||[]]]){const list=$('#'+id),scroll=list.scrollTop;list.replaceChildren();values.forEach(item=>{const entry=row(list,`${dateTime(item.at)} · ${actions[item.action]||item.name||item.code} · ${item.platform} · ${item.chat}`,item.reason+(id==='conversationEvents'?`\nОценка уместности: ${item.confidence}/100${item.topic?' · тема: '+item.topic:''}${item.answer?'\nРеплика: '+item.answer:''}`:''));if(item.until){const countdown=node('p','','hint');countdown.dataset.conversationUntil=item.until;entry.append(countdown);}});if(!values.length)list.append(node('p',id==='conversationEvents'?'Анализов пока нет.':'Пока нет результатов обработки.','hint'));list.scrollTop=scroll;}updateConversationCountdowns();}
async function refreshConversationActivity(){renderConversationActivity(await api('conversation'));}
async function refreshConversation(){const data=await api('conversation');glitchPromptDefaults=data.settings.glitch_prompt_defaults||{};const presenceFields=Object.fromEntries(Object.entries(data.presence||{}).map(([key,value])=>['presence_'+key,value]));fillForm($('#conversationForm'),{...data.settings,...presenceFields});syncGlitchStyleChecks(data.settings.glitch_styles||data.settings.glitch_style);syncRandomGlitchStyleChecks(data.settings.random_glitch_styles);const testChat=$('#glitchTestChat');if(testChat&&!testChat.value.trim())testChat.value=(data.settings.glitch_chats||'').split(/[\n,]/).map(item=>item.trim()).find(Boolean)||'';$('#conversationModels').replaceChildren();(aiStatus.models||[]).forEach(model=>{const option=node('option',model.name);option.value=model.id;$('#conversationModels').append(option);});renderConversationActivity(data);}
$('#conversationForm').onsubmit=async event=>{event.preventDefault();try{const data=formData(event.target);data.glitch_styles=glitchStyleChecks.filter(input=>input.checked).map(input=>input.value).join(',')||data.glitch_style;data.random_glitch_styles=randomGlitchStyleChecks.filter(input=>input.checked).map(input=>input.value).join(',');await api('conversation/settings',data);await refreshConversationActivity();toast('Живое общение сохранено');}catch(error){toast(error.message,true);}};
$('#conversationPreviewForm').onsubmit=async event=>{event.preventDefault();const output=$('#conversationPreviewResult'),submit=event.target.querySelector('button');output.hidden=false;output.textContent='Анализирую пример…';submit.disabled=true;try{const data=await api('conversation/preview',formData(event.target)),decision=data.decision;const actions={silent:'Промолчать',text:'Ответить текстом',sticker:'Отправить стикер',text_sticker:'Ответить текстом и отдельным стикером'};output.textContent=`Решение: ${actions[decision.action]}\nУверенность: ${decision.confidence}%\nПричина: ${decision.reason}${decision.topic?'\nТема: '+decision.topic:''}${decision.text?'\n\n'+decision.text:''}${decision.sticker_key?'\nСтикер: '+decision.sticker_key:''}`;}catch(error){output.textContent=error.message;}finally{submit.disabled=false;}};$('#glitchTestButton').onclick=async()=>{const button=$('#glitchTestButton'),result=$('#glitchTestResult');button.disabled=true;result.hidden=false;result.className='statusbox';result.textContent='Генерирую и отправляю тест сбоя…';try{const response=await api('conversation/glitch-test',{chat:$('#glitchTestChat').value.trim(),person:$('#glitchTestPerson').value.trim(),text:$('#glitchTestText').value,style:$('#glitchTestStyle').value,replace:true});result.textContent=response.message+' Стиль: '+response.style;await refreshConversationActivity();}catch(error){result.className='statusbox error';result.textContent=error.message;}finally{button.disabled=false;}};
$('#conversationRefresh').onclick=()=>refreshConversationActivity().catch(error=>toast(error.message,true));
for(const [id,values] of Object.entries({conversationQuiet:{chance:5,confidence:85,cooldown_seconds:1800,person_cooldown_seconds:3600,analysis_pause_seconds:120,daily_limit:6,idle_enabled:false,idle_seconds:600},conversationBalanced:{chance:15,confidence:80,cooldown_seconds:600,person_cooldown_seconds:1800,analysis_pause_seconds:60,daily_limit:12,idle_enabled:false,idle_seconds:600},conversationActive:{chance:35,confidence:70,cooldown_seconds:30,person_cooldown_seconds:120,analysis_pause_seconds:10,daily_limit:60,idle_enabled:false,idle_seconds:600},conversationTesting:{chance:100,confidence:0,cooldown_seconds:0,person_cooldown_seconds:0,analysis_pause_seconds:0,daily_limit:100,idle_enabled:false,idle_seconds:600}}))$('#'+id).onclick=()=>{fillForm($('#conversationForm'),values);toast('Значения подставлены. Сохраните настройки.');};
$('#routeTest').onclick=async event=>{const el=event.currentTarget;el.disabled=true;try{const data=formData($('#routeForm'));data.targets=data.targets.split(/[,\n]/).map(s=>s.trim()).filter(Boolean);if(!data.id)delete data.id;const result=await api('route/test',data);toast(result.message+(result.failed.length?' · '+result.failed.map(x=>x.error).join('; '):''),result.failed.length>0);}catch(e){toast(e.message,true);}finally{el.disabled=false;}};
$('#refresh').onclick=()=>refresh().catch(e=>toast(e.message,true));
document.querySelectorAll('[data-clear]').forEach(el=>el.onclick=async()=>{if(!confirm('Удалить сохранённое значение?'))return;try{await api('clear-secret',{key:el.dataset.clear});await refresh();toast('Удалено');}catch(e){toast(e.message,true);}});
document.querySelectorAll('[data-test]').forEach(el=>el.onclick=async()=>{el.disabled=true;try{const result=await api('test',{kind:el.dataset.test,question:$('#testQuestion').value,chat:$('#testChat').value});if(el.dataset.test==='llm'){$('#testAnswer').hidden=false;$('#testAnswer').textContent=result.message;}else toast(result.message);}catch(e){toast(e.message,true);}finally{el.disabled=false;}});

async function refreshUpdateStatus(){try{const status=await api('update/status');updateVersion=status.version;$('#versionInfo').textContent='Текущая версия: '+status.version+(status.pending_version?' · готова к установке: '+status.pending_version:'');$('#uploadUpdate').disabled=!status.supported;$('#updateFile').disabled=!status.supported;$('#applyUpdate').hidden=!status.supported||!status.pending_version;$('#applyUpdate').dataset.version=status.pending_version||'';$('#updateState').textContent=status.supported?'Python, данные и модели сохраняются при обновлении.':'Для обновления через панель нужен запуск через файл запуска или контейнерный Compose.';}catch(e){$('#updateState').textContent=e.message;}}
$('#uploadUpdate').onclick=async()=>{const file=$('#updateFile').files[0];if(!file){toast('Выберите ZIP новой версии',true);return;}try{const response=await fetch('/api/update/upload',{method:'POST',headers:{'Content-Type':'application/zip','X-Repostbot-Request':'1'},credentials:'same-origin',body:file}),data=await response.json();if(!response.ok)throw new Error(data.error);await refreshUpdateStatus();toast('Архив версии '+data.version+' подготовлен');}catch(e){toast(e.message,true);}};
$('#applyUpdate').onclick=async()=>{if(!confirm('Установить обновление и перезапустить бота?'))return;try{const target=$('#applyUpdate').dataset.version;await api('update/apply',{});$('#updateState').textContent='Обновление и перезапуск…';for(let i=0;i<300;i++){await new Promise(resolve=>setTimeout(resolve,2000));try{const status=await api('update/status');if(status.version===target&&status.version!==updateVersion){location.reload();return;}}catch(e){if(e.status===401){$('#updateState').textContent='Бот перезапущен. Войдите в панель ещё раз.';return;}}}$('#updateState').textContent='Проверьте окно запуска и обновите страницу.';}catch(e){toast(e.message,true);}};
async function refreshTwitch(){
  const [status,community,stats]=await Promise.all([api('twitch/status'),api('community'),api('twitch/stats?days='+encodeURIComponent($('#twitchStatsDays').value||-1))]);
  fillForm($('#twitchConfigForm'),{client_id:status.client_id,channel:status.channel,redirect_uri:status.redirect_uri});
  fillForm($('#communitySettingsForm'),community);
  $('#banwordEngineStatus').textContent=community.enabled
    ? `Книга обид включена · сохранено групп: ${community.terms_count}. Звёздочка в конце правила означает любое окончание того же слова или его отсутствие.`
    : `Книга обид выключена · сохранено групп: ${community.terms_count}. Правила не применяются к сообщениям, пока вы не включите «Книгу обид и реакции» выше и не сохраните настройки.`;
  $('#mtprotoForm').elements.api_id.value=community.mtproto.api_id||'';
  $('#mtprotoState').textContent=`MTProto: ${community.mtproto.configured?'данные сохранены':'не настроен'} · ${community.mtproto.available?'модуль готов':'модуль Telethon пока не установлен'}.`;
  const selection=$('#scanGroupId').value;
  $('#scanGroupId').replaceChildren(new Option('Все известные группы',''));
  $('#grudgeGroupCoverage').replaceChildren();
  (community.groups||[]).forEach(group=>{
    $('#scanGroupId').append(new Option(group.title+' · '+group.id,group.id));
    row($('#grudgeGroupCoverage'),group.title,`Известно боту: ${group.known}${group.member_count?' · участников в Telegram: '+group.member_count:''}`);
  });
  $('#scanGroupId').value=[...$('#scanGroupId').options].some(option=>option.value===selection)?selection:'';
  $('#twitchConnectionState').textContent=`Бот: ${status.chat_connected?'@'+status.chat_login:'не подключён'} · ${status.chat_state}. Модератор (только чтение): ${status.moderator_connected?'@'+status.moderator_login:'не подключён'} · ${status.moderator_state}.`;
  $('#twitchStatsSummary').textContent=stats.days===-1?`За всё сохранённое время: ${stats.total} сообщений.`:`За ${stats.days} дн.: ${stats.total} сообщений.`;
  $('#twitchTopUsers').replaceChildren();$('#twitchTopEmotes').replaceChildren();$('#twitchRecentRewards').replaceChildren();$('#seenRewards').replaceChildren();
  stats.users.forEach((item,i)=>row($('#twitchTopUsers'),`${i+1}. ${item.name||item.user_id}`,`${item.messages} сообщений · ID ${item.user_id}`));
  stats.emotes.forEach((item,i)=>row($('#twitchTopEmotes'),`${i+1}. ${item.label||item.emote_id}`,`${item.uses} раз · ID ${item.emote_id}`));
  stats.rewards.forEach(item=>{row($('#twitchRecentRewards'),`${dateTime(item.at)} · @${item.display_name||item.login}`,`${item.title||'Награда'} · ID ${item.reward_id} · ${item.user_input||''}`);const option=node('option',item.title||item.reward_id);option.value=item.reward_id;$('#seenRewards').append(option);});
  $('#rewardRules').replaceChildren();$('#grudgePeople').replaceChildren();$('#protectedPeople').replaceChildren();$('#honorPeople').replaceChildren();
  community.reward_rules.forEach(item=>row($('#rewardRules'),item.title||item.reward_id,`${item.reward_id} · ${item.action} · ${item.amount} · ${item.response||'стандартный ответ'}`,[['Изменить',()=>fillForm($('#rewardRuleForm'),item)],['Удалить',async()=>{await api('community/reward-rule/delete',{id:item.id});await refreshTwitch();}]]));
  $('#grudgePeopleSummary').textContent=community.profiles.length?`Известных людей: ${community.profiles.length}. Связанные пары показаны одной карточкой.`:'Пока никто не замечен. Добавление бота в группу не раскрывает список её участников. Нажмите «Сканировать известные группы» или дождитесь сообщений.';
  $('#questKnownPeople').replaceChildren();(community.profiles||[]).forEach(person=>{const nick=person.identities?.telegram?.username||(person.platform==='telegram'?person.name:'');if(nick)$('#questKnownPeople').append(new Option(nick.replaceAll('@',''),nick.replaceAll('@','')));});
  const bookProfiles=[...community.profiles].sort((a,b)=>Number(Boolean(b.honors?.length))-Number(Boolean(a.honors?.length))||b.reputation-a.reputation||a.name.localeCompare(b.name,'ru'));
  const selectedPerson=$('#bookPersonSelect').value;$('#bookPersonSelect').replaceChildren(new Option('Выберите участника',''));
  bookProfiles.forEach(person=>{const identity=person.identities?.[person.platform],label=`${person.name.replaceAll('@','')}${identity?.username?' ('+identity.username+')':''} · ${person.platform==='telegram'?'Telegram':'Twitch'}${person.linked?' ↔ связан':''}`,option=new Option(label,`${person.platform}:${person.user_id}`);option.dataset.nick=identity?.username||person.name.replaceAll('@','');$('#bookPersonSelect').append(option);});$('#bookPersonSelect').value=selectedPerson;
  await refreshParticipantRoles();
  const query=new URLSearchParams(location.search);if(query.has('error')){toast(query.get('error'),true);history.replaceState(null,'',location.pathname);}else if(query.has('connected')){toast('Аккаунт Twitch подключён');history.replaceState(null,'',location.pathname);}
}
async function refreshModeration(){
  const result=await api('moderation');
  fillForm($('#moderationSettingsForm'),result.settings);
  await refreshLexicon();
  const events=result.events||[];
  $('#moderationEventCount').textContent=events.length;
  const container=$('#moderationEvents');container.replaceChildren();
  events.forEach(item=>row(container,`${item.name||item.user_id} · ${dateTime(item.at)} · ${item.action}`,`Чат ${item.chat_id} · сообщение ${item.message_id} · штраф ${item.points} · предупреждений ${item.warnings} · ${item.executor} · ${item.status}`));
  if(!events.length)container.append(node('p','Пока нет нарушений.','hint'));
}
let lexiconOffset=0, whiteOffset=0, variantOffset=0, selectedLexiconGroup=null;
let blackRequest=0, whiteRequest=0, detailRequest=0, blackTimer, whiteTimer;
const lexiconPageSize=40;
function lexiconRange(offset, count, total){return total?`${offset+1}–${offset+count} из ${total}`:'Нет записей';}
function lexiconRow(title, detail){
  const item=node('div','','lexicon-row'), word=node('div','','lexicon-word');
  word.append(node('strong',title),node('small',detail));item.append(word);return item;
}
async function refreshLexicon(){await Promise.all([refreshBlack(),refreshWhite()]);}
async function refreshBlack(){
  const request=++blackRequest;
  const query=new URLSearchParams({kind:'groups',query:$('#lexiconSearch').value.trim(),offset:String(lexiconOffset),limit:String(lexiconPageSize)});
  const data=await api('moderation/lexicon?'+query);
  if(request!==blackRequest)return;
  if(lexiconOffset>=data.groups_total&&lexiconOffset){lexiconOffset=Math.max(0,Math.ceil(data.groups_total/lexiconPageSize)-1)*lexiconPageSize;return refreshBlack();}
  const list=$('#lexiconGroups');list.replaceChildren();
  $('#lexiconGroupCount').textContent=`Всего групп: ${data.groups_total}`;
  $('#lexiconPage').textContent=lexiconRange(lexiconOffset,data.groups.length,data.groups_total);
  $('#lexiconPrev').disabled=!lexiconOffset;$('#lexiconNext').disabled=lexiconOffset+data.groups.length>=data.groups_total;
  data.groups.forEach(group=>{
    const item=lexiconRow(group.phrase,`${group.scope==='both'?'Telegram и Twitch':group.scope==='telegram'?'Telegram':'Twitch'} · вариантов: ${group.variant_count}`);
    if(group.id===selectedLexiconGroup)item.classList.add('selected');
    item.append(node('span',`−${group.points}`,'lexicon-score'));
    const label=node('label','','lexicon-switch'), toggle=node('input');toggle.type='checkbox';toggle.checked=Boolean(group.enabled);toggle.setAttribute('aria-label',`Включить группу ${group.phrase}`);
    label.append(toggle,node('span','Вкл.'));toggle.onchange=async()=>{
      toggle.disabled=true;try{await api('community/term/toggle',{id:group.id,enabled:toggle.checked});if(group.id===selectedLexiconGroup)await refreshGroupDetail();toast(toggle.checked?'Группа включена':'Группа выключена');}
      catch(error){toggle.checked=!toggle.checked;toast(error.message,true);}finally{toggle.disabled=false;}
    };
    item.append(label,button('Изменить',()=>openLexiconGroup(group.id),'outline'));list.append(item);
  });
  if(!data.groups.length)list.append(node('p','Совпадений нет.','lexicon-empty'));
}
async function refreshWhite(){
  const request=++whiteRequest;
  const query=new URLSearchParams({kind:'white',query:$('#lexiconWhiteSearch').value.trim(),offset:String(whiteOffset),limit:String(lexiconPageSize)});
  const data=await api('moderation/lexicon?'+query);
  if(request!==whiteRequest)return;
  if(whiteOffset>=data.white_total&&whiteOffset){whiteOffset=Math.max(0,Math.ceil(data.white_total/lexiconPageSize)-1)*lexiconPageSize;return refreshWhite();}
  const list=$('#lexiconWhite');list.replaceChildren();
  $('#lexiconWhiteCount').textContent=`Всего слов: ${data.white_total}`;
  $('#lexiconWhitePage').textContent=lexiconRange(whiteOffset,data.white.length,data.white_total);
  $('#lexiconWhitePrev').disabled=!whiteOffset;$('#lexiconWhiteNext').disabled=whiteOffset+data.white.length>=data.white_total;
  data.white.forEach(word=>{
    const item=lexiconRow(word.word,word.source==='model'?'Проверено моделью':'Добавлено вручную');
    item.append(button('Убрать',async()=>{await api('moderation/white',{word:word.word,enabled:false});await refreshWhite();}));list.append(item);
  });
  if(!data.white.length)list.append(node('p','Совпадений нет.','lexicon-empty'));
}
async function openLexiconGroup(id){
  selectedLexiconGroup=id;variantOffset=0;
  $('#lexiconEditor').hidden=false;
  await Promise.all([refreshGroupDetail(),refreshBlack()]);
  $('#lexiconEditor').scrollIntoView({behavior:'smooth',block:'start'});
}
async function refreshGroupDetail(){
  if(selectedLexiconGroup===null)return;
  const id=selectedLexiconGroup,request=++detailRequest;
  const data=await api('moderation/lexicon/group?'+new URLSearchParams({id:String(id),offset:String(variantOffset)}));
  if(request!==detailRequest||id!==selectedLexiconGroup)return;
  const group=data.group;
  if(variantOffset>=group.variant_count&&variantOffset){variantOffset=Math.max(0,Math.ceil(group.variant_count/40)-1)*40;return refreshGroupDetail();}
  $('#lexiconEditorTitle').textContent=`Группа «${group.phrase}»`;
  fillForm($('#lexiconEditForm'),group);
  $('#lexiconVariantCount').textContent=`(${group.variant_count})`;
  $('#lexiconVariantPage').textContent=lexiconRange(variantOffset,group.variants.length,group.variant_count);
  $('#lexiconVariantPrev').disabled=!variantOffset;$('#lexiconVariantNext').disabled=variantOffset+group.variants.length>=group.variant_count;
  const list=$('#lexiconVariants');list.replaceChildren();
  group.variants.forEach(variant=>{
    const item=lexiconRow(variant.phrase,variant.origin==='model'?'Найдена моделью':'Добавлена вручную');
    item.append(button('Убрать',async()=>{await api('moderation/variant/delete',{id:variant.id});await Promise.all([refreshGroupDetail(),refreshBlack()]);}));list.append(item);
  });
  if(!group.variants.length)list.append(node('p','Вариантов пока нет.','lexicon-empty'));
}
$('#lexiconSearch').oninput=()=>{clearTimeout(blackTimer);blackTimer=setTimeout(()=>{lexiconOffset=0;refreshBlack().catch(error=>toast(error.message,true));},250);};
$('#lexiconWhiteSearch').oninput=()=>{clearTimeout(whiteTimer);whiteTimer=setTimeout(()=>{whiteOffset=0;refreshWhite().catch(error=>toast(error.message,true));},250);};
$('#lexiconPrev').onclick=()=>{lexiconOffset=Math.max(0,lexiconOffset-lexiconPageSize);refreshBlack().catch(error=>toast(error.message,true));};
$('#lexiconNext').onclick=()=>{lexiconOffset+=lexiconPageSize;refreshBlack().catch(error=>toast(error.message,true));};
$('#lexiconWhitePrev').onclick=()=>{whiteOffset=Math.max(0,whiteOffset-lexiconPageSize);refreshWhite().catch(error=>toast(error.message,true));};
$('#lexiconWhiteNext').onclick=()=>{whiteOffset+=lexiconPageSize;refreshWhite().catch(error=>toast(error.message,true));};
$('#lexiconVariantPrev').onclick=()=>{variantOffset=Math.max(0,variantOffset-40);refreshGroupDetail().catch(error=>toast(error.message,true));};
$('#lexiconVariantNext').onclick=()=>{variantOffset+=40;refreshGroupDetail().catch(error=>toast(error.message,true));};
$('#lexiconClose').onclick=()=>{selectedLexiconGroup=null;detailRequest++;$('#lexiconEditor').hidden=true;refreshBlack().catch(error=>toast(error.message,true));};
$('#lexiconGroupForm').onsubmit=async event=>{event.preventDefault();try{const group=await api('community/term',formData(event.target));event.target.reset();event.target.closest('details').open=false;await refreshBlack();await openLexiconGroup(group.id||group);toast('Группа добавлена');}catch(error){toast(error.message,true);}};
$('#lexiconEditForm').onsubmit=async event=>{event.preventDefault();try{await api('community/term',formData(event.target));await Promise.all([refreshBlack(),refreshGroupDetail()]);toast('Группа сохранена');}catch(error){toast(error.message,true);}};
$('#lexiconDeleteGroup').onclick=async()=>{if(!confirm('Удалить группу и все её варианты?'))return;try{await api('community/term/delete',{id:selectedLexiconGroup});selectedLexiconGroup=null;detailRequest++;$('#lexiconEditor').hidden=true;await refreshBlack();toast('Группа удалена');}catch(error){toast(error.message,true);}};
$('#lexiconVariantForm').onsubmit=async event=>{event.preventDefault();try{await api('moderation/variant',{group_id:selectedLexiconGroup,phrase:event.target.elements.phrase.value});event.target.reset();await Promise.all([refreshGroupDetail(),refreshBlack()]);toast('Вариант добавлен');}catch(error){toast(error.message,true);}};
$('#lexiconWhiteForm').onsubmit=async event=>{event.preventDefault();try{await api('moderation/white',{word:event.target.elements.word.value,enabled:true});event.target.reset();await refreshWhite();toast('Слово добавлено');}catch(error){toast(error.message,true);}};
let participantRequest=0;
async function refreshParticipantRoles(){
  const request=++participantRequest,data=await api('social/participants');if(request!==participantRequest)return;
  const list=$('#grudgePeople'),select=$('#bookPersonSelect'),selected=select.value;
  list.replaceChildren();select.replaceChildren(new Option('Выберите участника',''));
  $('#protectedPeople').replaceChildren();$('#honorPeople').replaceChildren();
  const openForm=(id,value)=>{const form=$('#'+id),details=form.closest('details');if(details)details.open=true;fillForm(form,value);revealSetting(form);};
  for(const platform of ['telegram','twitch']){
    const control=$('#participantLinkForm').elements[platform+'_id'],selected=control.value,seen=new Set();control.replaceChildren(new Option('Выберите аккаунт',''));
    for(const person of data.people||[]){const identity=person.identities?.[platform];if(identity&&!seen.has(identity.id)){seen.add(identity.id);control.append(new Option((identity.username?'@'+identity.username:identity.name)+' · '+identity.id,identity.id));}}
    control.value=selected;
  }
  for(const person of data.people||[]){
    const identities=Object.entries(person.identities||{}).map(([platform,value])=>(platform==='telegram'?'Telegram: ':'Twitch: ')+(value.name||value.id)+(value.username?' (@'+value.username+')':''));
    const label=identities.join(' ↔ ')||person.name, card=row(list,label,`Репутация ${person.reputation} · обида ${person.debt} · активность ${person.activity} · награды ${person.favor}`+(person.fixed_score!==null?` · фиксированный уровень ${person.fixed_score}`:''));
    card.dataset.participant=person.platform+':'+person.user_id;
    const option=new Option(label,person.platform+':'+person.user_id);option.dataset.nick=person.identities?.[person.platform]?.username||person.name;select.append(option);
    const grouped=new Map();for(const role of person.roles||[]){const key=role.role_key; if(!grouped.has(key))grouped.set(key,[]);grouped.get(key).push(role);}
    for(const roles of grouped.values()){
      card.append(node('p',roles[0].title+' · '+roles.map(role=>role.chat+(role.kind==='daily'?' · сегодня':' · постоянно')).join(', '),'participant-role'));
      for(const role of roles)if(role.kind==='permanent')card.append(button('Снять роль в '+role.chat,async()=>{if(!confirm('Снять эту постоянную роль в выбранном чате?'))return;await api('social/permanent-role/delete',{chat:role.chat,person:role.person,role_key:role.role_key});await refreshSocial();}));
    }
    for(const honor of person.honors||[]){
      const entry=node('div','','participant-honor');
      if(!honor.covered_by_role)entry.append(node('strong',honor.title));
      if(honor.message)entry.append(node('p',honor.message,'hint'));
      entry.append(button(honor.covered_by_role?'Настроить звание и уровень':'Изменить звание',()=>{
        if(['king','donor'].includes(honor.category))openForm('protectedForm',{...honor,platform:person.platform,user_id:person.user_id,nick:person.identities?.[person.platform]?.username||person.name,fixed_score:honor.fixed_score??person.fixed_score??100});
        else editHonor(honor);
      }),button('Убрать звание',async()=>{if(!confirm('Убрать это звание и связанную с ним фиксацию уровня у участника? Игровые роли сохранятся.'))return;await api('social/title/delete',{platform:person.platform,user_id:person.user_id,category:honor.category,title:honor.title});await refreshParticipantRoles();}));
      card.append(entry);
    }
    card.append(button('Роль',()=>{const role=person.roles?.[0];openForm('socialPermanentRoleForm',{chat:role?.chat||'',person:role?.person||(person.platform==='twitch'?'twitch:':'')+person.user_id});}),
      button('Добавить звание',()=>editHonor({platform:person.platform,user_id:person.user_id,nick:person.name,category:'custom',fixed_score:null})),
      button('Фиксированный уровень',()=>openForm('protectedForm',{platform:person.platform,user_id:person.user_id,nick:person.name,fixed_score:person.fixed_score??100})),
      button('Правка Книги',()=>{openForm('grudgeAdjustForm',{platform:person.platform,user_id:person.user_id,nick:person.name});$('#grudgeAdjustForm').dataset.score=person.reputation;}));
    if(person.linked)card.append(button('Отвязать аккаунты',async()=>{if(!confirm('Отвязать Telegram и Twitch?'))return;await api('community/unlink',{twitch_id:person.twitch_id});await refreshParticipantRoles();}));
    const history=node('details','','person-ledger');history.append(node('summary','История Книги'));
    for(const event of person.events||[])history.append(node('p',dateTime(event.at)+' · '+event.reason+' · обида '+event.grudge_delta+', бонус '+event.favor_delta+', активность '+event.activity_delta));card.append(history);
  }
  select.value=selected;
  for(const pending of data.pending||[]){
    const card=row(list,'⏳ @'+pending.nick,pending.platform+' · ожидается привязка ID'),seen=new Set();
    const records=[...(pending.records||[])].sort((a,b)=>Number(b.kind==='protected')-Number(a.kind==='protected'));
    for(const record of records){
      const key=record.category+':'+record.title.toLocaleLowerCase('ru-RU');if(seen.has(key))continue;seen.add(key);
      if(record.title)card.append(node('p',record.title));
      card.append(button('Изменить',()=>{if(record.kind==='honor')editHonor({...record,user_id:'',pending_id:record.id,id:''});else openForm(record.kind==='person'?'bookAddPersonForm':'protectedForm',{...record,user_id:'',pending_id:record.id,id:''});}));
    }
    card.append(button('Удалить ожидающую запись',async()=>{if(!confirm('Удалить ожидающие записи этого участника?'))return;for(const record of records)await api('community/pending/delete',{id:record.id});await refreshParticipantRoles();}));
  }
  $('#grudgePeopleSummary').textContent='Единых карточек: '+(data.people||[]).length+'. Ожидают ID: '+(data.pending||[]).length+'.';
  const groups=$('#grudgeGroupCoverage'),groupSelect=$('#scanGroupId'),previous=groupSelect.value;groups.replaceChildren();groupSelect.replaceChildren(new Option('Все известные группы',''));
  for(const group of data.groups||[]){groupSelect.append(new Option(group.title+' · '+group.id,group.id));row(groups,group.title,'Известно участников: '+group.known);}groupSelect.value=previous;
}
function syncHonorForm(resetLevel=false){const form=$('#honorForm'),category=form.elements.category.value,isCustom=category==='custom',isProtected=category==='king'||category==='donor';form.elements.title.required=isCustom;$('#honorCustomHint').hidden=!isCustom;form.elements.fixed_enabled.disabled=isProtected;$('#honorFixedHint').textContent=isProtected?'У короля и топ донатера уровень меняется в форме фиксированного уровня.':isCustom?'Для своего звания укажите любой уровень от −100 до +100, например −60.':'Для владельца и модератора по умолчанию +100.';if(resetLevel){form.elements.fixed_enabled.checked=!isProtected;form.elements.fixed_score.value=isCustom?'0':'100';}form.elements.fixed_score.disabled=isProtected||!form.elements.fixed_enabled.checked;}
function editHonor(item){const form=$('#honorForm');revealSetting(form);form.closest('details').open=true;form.reset();fillForm(form,{...item,fixed_enabled:item.fixed_score!==null&&item.fixed_score!==undefined,fixed_score:item.fixed_score??100});syncHonorForm();revealSetting(form);form.scrollIntoView({behavior:'smooth'});}
for(const [buttonId,formId] of [['bookPickProtected','protectedForm'],['bookPickHonor','honorForm'],['bookPickAdjust','grudgeAdjustForm']])$('#'+buttonId).onclick=()=>{const selected=$('#bookPersonSelect').value;if(!selected){toast('Выберите участника Книги',true);return;}const [platform,user_id]=selected.split(':');const option=$('#bookPersonSelect').selectedOptions[0],form=$('#'+formId);form.closest('details').open=true;form.reset();if(form.elements.id)form.elements.id.value='';fillForm(form,{platform,user_id,nick:option.dataset.nick});if(formId==='honorForm')syncHonorForm();revealSetting(form);form.scrollIntoView({behavior:'smooth'});};
$('#twitchConfigForm').onsubmit=async event=>{event.preventDefault();try{await api('twitch/config',formData(event.target));await refreshTwitch();toast('Подключение сохранено');}catch(error){toast(error.message,true);}};
for(const [id,role] of [['twitchLoginChat','chat'],['twitchLoginModerator','moderator']])$('#'+id).onclick=async()=>{const tab=window.open('about:blank','_blank');try{const result=await api('twitch/begin',{role});$('#twitchManualAuth').open=true;toast('Вход Twitch открыт в новой вкладке. После разрешения вернитесь сюда.');if(tab){tab.opener=null;tab.location.replace(result.url);}else location.href=result.url;}catch(error){tab?.close();toast(error.message,true);}};
$('#twitchCallbackForm').onsubmit=async event=>{event.preventDefault();try{const result=await api('twitch/complete-url',{url:event.target.elements.url.value});event.target.reset();await refreshTwitch();toast('Twitch: подключён аккаунт @'+result.login);}catch(error){toast(error.message,true);}};
for(const [id,role] of [['twitchDisconnectChat','chat'],['twitchDisconnectModerator','moderator']])$('#'+id).onclick=async()=>{try{await api('twitch/disconnect',{role});await refreshTwitch();toast('Аккаунт отключён');}catch(error){toast(error.message,true);}};
$('#twitchTestSend').onclick=async()=>{try{await api('twitch/send',{message:$('#twitchTestText').value});toast('Отправлено от аккаунта бота');}catch(error){toast(error.message,true);}};
$('#twitchRefresh').onclick=()=>refreshTwitch().catch(error=>toast(error.message,true));
$('#mtprotoForm').onsubmit=async event=>{event.preventDefault();try{await api('community/mtproto',formData(event.target));event.target.elements.api_hash.value='';await refreshTwitch();toast('Данные MTProto сохранены');}catch(error){toast(error.message,true);}};
$('#mtprotoDisconnect').onclick=async()=>{if(!confirm('Удалить API ID, API Hash и сеанс MTProto?'))return;try{await api('community/mtproto/disconnect',{});await refreshTwitch();toast('Доступ MTProto удалён');}catch(error){toast(error.message,true);}};
$('#scanCommunityGroups').onclick=async()=>{const el=$('#scanCommunityGroups');el.disabled=true;try{const result=await api('community/scan-groups',{group_id:$('#scanGroupId').value});await refreshTwitch();const lines=[`Проверено групп: ${result.groups}. Сохранённых авторов сообщений: ${result.saved_authors}, администраторов: ${result.administrators}.`,`Полный состав: ${result.full.status}; получено участников: ${result.full.added}.`,...(result.full.errors||[]).map(item=>item.chat+': '+item.reason)];if(result.failed_groups)lines.push('Ошибок Bot API: '+result.failed_groups);$('#scanReport').textContent=lines.join('\n');$('#scanReport').hidden=false;toast('Сканирование завершено'+(result.full.added?' · '+result.full.added+' участников':''),Boolean(result.full.errors?.length||result.failed_groups));}catch(error){toast(error.message,true);}finally{el.disabled=false;}};
$('#protectedForm').onsubmit=async event=>{event.preventDefault();try{const result=await api('social/title',{...formData(event.target),kind:'protected'});event.target.reset();await refreshTwitch();toast(result.pending?'Ник сохранён. Иммунитет включится после привязки ID.':'Иммунитет сохранён');}catch(error){toast(error.message,true);}};
$('#honorForm').onsubmit=async event=>{event.preventDefault();try{const data=formData(event.target);data.fixed_enabled=event.target.elements.fixed_enabled.checked&&!event.target.elements.fixed_enabled.disabled;const result=await api('social/title',{...data,kind:'honor'});event.target.reset();event.target.elements.id.value='';syncHonorForm();await refreshTwitch();toast(result.pending?'Ник сохранён. Звание привяжется при появлении пользователя.':'Звание сохранено');}catch(error){toast(error.message,true);}};
$('#bookAddPersonForm').onsubmit=async event=>{event.preventDefault();try{const result=await api('community/person',formData(event.target));event.target.reset();await refreshTwitch();toast(result.pending?'Ник добавлен в Книгу: ждём сообщения пользователя.':'Участник уже найден в Книге');}catch(error){toast(error.message,true);}};
$('#honorReset').onclick=()=>{const form=$('#honorForm');form.reset();form.elements.id.value='';form.elements.pending_id.value='';syncHonorForm();};
$('#honorForm').elements.category.addEventListener('change',()=>syncHonorForm(true));
$('#honorForm').elements.fixed_enabled.addEventListener('change',()=>syncHonorForm());
syncHonorForm();
for(const name of ['platform','user_id'])$('#honorForm').elements[name].addEventListener('input',()=>{$('#honorForm').elements.id.value='';});
for(const formId of ['protectedForm','honorForm','grudgeAdjustForm'])$('#'+formId).elements.nick.addEventListener('input',event=>{const form=event.target.form;form.elements.user_id.value='';if(form.elements.id)form.elements.id.value='';});
$('#grudgeBulkForm').onsubmit=async event=>{event.preventDefault();const el=event.submitter;el.disabled=true;try{const result=await api('community/terms/import',formData(event.target));event.target.elements.lines.value='';await refreshBlack();toast(`Банворды: добавлено ${result.added}, пропущено ${result.skipped}.`);}catch(error){toast(error.message,true);}finally{el.disabled=false;}};
let banwordPackPreview=null;
function invalidateBanwordPreview(){banwordPackPreview=null;$('#banwordImportApply').disabled=true;$('#banwordImportReport').hidden=true;}
$('#banwordImportFile').onchange=invalidateBanwordPreview;
$('#banwordImportOverwrite').onchange=invalidateBanwordPreview;
$('#banwordImportEnableBook').onchange=invalidateBanwordPreview;
$('#banwordExport').onclick=async()=>{try{const pack=await api('community/terms/export'),blob=new Blob([JSON.stringify(pack,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='repostbot-banwords-'+new Date().toISOString().slice(0,10)+'.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(error){toast(error.message,true);}};
$('#banwordImportPreview').onclick=async()=>{invalidateBanwordPreview();try{const file=$('#banwordImportFile').files?.[0];if(!file)throw new Error('Выберите JSON-файл');if(file.size>1900000)throw new Error('Файл слишком большой');const pack=JSON.parse(await file.text()),overwrite=$('#banwordImportOverwrite').checked,enable_book=$('#banwordImportEnableBook').checked,result=await api('community/terms/import-pack',{pack,overwrite,enable_book,preview:true});$('#banwordImportReport').textContent=`Новых групп: ${result.added}; обновится: ${result.updated}; новых вариантов: ${result.variants_added||0}; белых слов: ${result.white_added||0}; конфликтов: ${result.conflicts}. ${enable_book?'При импорте Книга обид будет включена.':'Книга обид останется в текущем состоянии.'}`;$('#banwordImportReport').hidden=false;banwordPackPreview={pack,overwrite,enable_book};$('#banwordImportApply').disabled=false;}catch(error){toast('Проверка файла: '+error.message,true);}};
$('#banwordImportApply').onclick=async()=>{if(!banwordPackPreview)return;const button=$('#banwordImportApply');button.disabled=true;try{const result=await api('community/terms/import-pack',banwordPackPreview);invalidateBanwordPreview();await refreshLexicon();toast(`Банворды: групп ${result.added}, вариантов ${result.variants_added||0}, белых слов ${result.white_added||0}.`+(result.book_enabled?'':' Книга обид выключена: включите её выше, чтобы правила применялись.'));}catch(error){button.disabled=false;toast(error.message,true);}};
$('#banwordCheckForm').onsubmit=async event=>{event.preventDefault();try{const result=await api('community/terms/check',formData(event.target));const lines=[result.book_enabled?'Книга обид включена.':'Книга обид выключена: совпадения есть, но репутация не меняется.',...(result.matches.length?result.matches.map(item=>`${item.phrase} · −${item.points} · ${item.enabled?'правило включено':'правило выключено'}`):['Совпадений не найдено.']),`Применится правил: ${result.would_apply.length}. Проверка не меняет репутацию.`];$('#banwordCheckReport').textContent=lines.join('\n');$('#banwordCheckReport').hidden=false;}catch(error){toast(error.message,true);}};
$('#communitySettingsForm').onsubmit=async event=>{event.preventDefault();try{await api('community/settings',formData(event.target));await refreshTwitch();toast('Правила сохранены');}catch(error){toast(error.message,true);}};
$('#moderationSettingsForm').onsubmit=async event=>{event.preventDefault();try{await api('moderation/settings',formData(event.target));await refreshModeration();toast('Модерация сохранена');}catch(error){toast(error.message,true);}};
$('#moderationScanChatButton').onclick=async()=>{const button=$('#moderationScanChatButton'),report=$('#moderationScanReport');button.disabled=true;report.hidden=false;report.className='statusbox';report.textContent='Проверяю сохранённые сообщения…';try{const result=await api('moderation/scan-chat',{chat:$('#moderationScanChat').value.trim(),limit:Number($('#moderationScanLimit').value||500)});const lines=[`Проверено: ${result.scanned}; совпадений: ${result.matched}; удалено: ${result.deleted}.`,`Уже отсутствуют: ${result.already_gone||0}; старше 48 часов: ${result.skipped_old||0}; удалить запрещено Telegram: ${result.not_deletable||0}; прочие ошибки: ${result.failed||0}.`,`Порог: ${result.threshold}.`];if(result.model_checked)lines.push(`Модель проверила неизвестных сообщений: ${result.model_checked}; распознала нарушений: ${result.model_matches||0}.`);if(result.model_errors)lines.push(`Ошибок проверки моделью: ${result.model_errors}.`);if(result.skipped_old)lines.push('Старые сообщения Telegram не даёт удалить через Bot API.');if(result.not_deletable)lines.push('Для части сообщений Telegram отказал в удалении: проверьте возраст сообщения и право «Удаление сообщений».');if(result.failures?.length)lines.push(...result.failures.map(item=>`Сообщение ${item.message_id}: ${item.error}`));report.textContent=lines.join('\n');await refreshModeration();}catch(error){report.className='statusbox error';report.textContent=error.message;}finally{button.disabled=false;}};
$('#grudgeAdjustForm').elements.action.onchange=()=>{const form=$('#grudgeAdjustForm'),exact=form.elements.action.value==='set',amount=form.elements.amount;amount.min=exact?'-100':'0.5';if(exact){amount.max='100';amount.value=form.dataset.score||'0';}else{amount.removeAttribute('max');amount.value='5';}$('#grudgeAmountLabel').firstChild.textContent=exact?'Точная репутация':'Баллы';};
for(const [formId,endpoint] of [['rewardRuleForm','community/reward-rule'],['grudgeAdjustForm','community/adjust']])$('#'+formId).onsubmit=async event=>{event.preventDefault();try{await api(endpoint,formData(event.target));if(formId==='grudgeAdjustForm'){event.target.reset();event.target.dataset.score='';event.target.elements.action.onchange();}else{event.target.reset();event.target.elements.id.value='';}await refreshTwitch();toast('Сохранено');}catch(error){toast(error.message,true);}};
$('#rewardRuleReset').onclick=()=>{const form=$('#rewardRuleForm');form.reset();form.elements.id.value='';};
async function refreshTemplates(){const result=await api('telegram/templates'),list=$('#templateList');list.replaceChildren();$('#templateCount').textContent=result.templates.length;result.templates.forEach(item=>row(list,item.name,`${item.phrases} · ${item.media_type||'текст'} · ${item.enabled?'включён':'выключен'}`,[['Изменить',()=>{fillForm($('#telegramTemplateForm'),item);$('#telegramTemplateForm').scrollIntoView({behavior:'smooth'});}],['Удалить',async()=>{if(!confirm('Удалить шаблон?'))return;await api('telegram/template/delete',{id:item.id});await refreshTemplates();}]]));}
$('#telegramTemplateForm').onsubmit=async event=>{event.preventDefault();try{await api('telegram/template',formData(event.target));event.target.reset();event.target.elements.id.value='';await refreshTemplates();toast('Шаблон сохранён');}catch(error){toast(error.message,true);}};
$('#templateReset').onclick=()=>{$('#telegramTemplateForm').reset();$('#telegramTemplateForm').elements.id.value='';};
$('#templateUploadButton').onclick=async()=>{const file=$('#templateUploadFile').files?.[0],kind=$('#templateUploadType').value;if(!file||file.size>8000000){toast('Выберите файл до 8 МБ',true);return;}const control=$('#templateUploadButton');control.disabled=true;try{const response=await fetch('/api/telegram/media/upload?type='+encodeURIComponent(kind)+'&name='+encodeURIComponent(file.name),{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Repostbot-Request':'1'},credentials:'same-origin',body:file});const result=await response.json();if(!response.ok)throw new Error(result.error||'Ошибка загрузки');const form=$('#telegramTemplateForm');form.elements.media_type.value=kind;form.elements.file_id.value=result.file_id;toast('Файл загружен в Telegram. Сохраните шаблон.');}catch(error){toast(error.message,true);}finally{control.disabled=false;}};
$('#templateLoadStickers').onclick=async()=>{try{const result=await api('telegram/sticker-set',{name:$('#templateStickerSet').value}),list=$('#templateStickerChoices');list.replaceChildren();list.append(node('p',result.title+' · '+result.stickers.length+' стикеров','hint'));result.stickers.forEach((item,index)=>{const entry=row(list,`${index+1}. ${item.emoji||'Стикер'} · ${item.format}`,'', [['В шаблон',()=>{const form=$('#telegramTemplateForm');form.elements.media_type.value='sticker';form.elements.file_id.value=item.file_id;toast('Стикер выбран. Сохраните шаблон.');}],['В ответы бота',async()=>{const key=$('#templateStickerKey').value.trim().toLowerCase();if(!/^[a-zа-яё0-9_-]{1,24}$/.test(key))throw new Error('Укажите ключ русскими или латинскими буквами, например музыка');await api('stickers/add',{key,file_id:item.file_id,description:$('#templateStickerDescription').value.trim()||key.replaceAll('_',' ')});toast('Стикер добавлен в ответы бота. Каталог доступен в «Стикерах и медиа».');}]]);});}catch(error){toast(error.message,true);}};
setInterval(()=>{if(!$('#dashboard').hidden){if(!$('#answers').hidden){refreshAIStatus();refreshModelStatus();}if(!$('#conversation').hidden)refreshConversationActivity().catch(()=>{});if(!$('#routeWorkspace .parser-diagnostic[open]'))refreshRoutePosts();}},15000);
api('session').then(session=>{if(session.authenticated)return refresh();}).catch(e=>toast(e.message,true));

setInterval(updateConversationCountdowns,1000);

const questStatusNames={waiting:'Ждёт скрытой выдачи',active:'В пути',ready:'Ожидает награду',paused:'Остановлен хранителем',pending:'Ожидает доставки',sending:'Отправляется',sent:'Доставлено',cancelled:'Отменено'};
const questKindNames={calm:'Мирный путь',messages:'Разные сообщения',visits:'Мирные визиты',dialogue:'Ответы участникам'};
async function cancelQuest(run,automatic){try{await api('quests/cancel',{id:run.id,automatic});await refreshQuests();toast('Свиток отменён владельцем');}catch(error){toast(error.message,true);}}
function renderQuestActivity(data){
  for(const id of ['questRuns','questEvents','questStatuses','questPrivateNotices'])$('#'+id).replaceChildren();
  const runs=[...(data.auto_runs||[]).map(r=>({...r,automatic:true})),...(data.runs||[]).map(r=>({...r,automatic:false}))];
  runs.forEach(run=>{const rule=JSON.parse(run.rule);row($('#questRuns'),run.nick.replaceAll('@','')+' · '+rule.title,`${run.automatic?'Автоматический':'Ручная доска'} · ${questStatusNames[run.status]||run.status} · ${run.progress}/${rule.target} · ${run.status==='waiting'?'срок ещё не начался':run.status==='paused'?'новая выдача после возобновления':run.status==='ready'?'путь пройден, ожидает суточного лимита':'до '+dateTime(run.expires)} · группа ${run.chat}`,[['Отменить свиток',()=>cancelQuest(run,run.automatic)]]);});
  if(!runs.length)$('#questRuns').append(node('p','Действующих личных свитков пока нет.','hint'));
  data.events.forEach(event=>row($('#questEvents'),`${dateTime(event.at)} · ${event.nick.replaceAll('@','')} · ${event.title}`,`${event.platform} · ${event.reason} · ${event.delta>0?'награда':'игровая обида'} ${Math.abs(event.delta)}`));
  if(!data.events.length)$('#questEvents').append(node('p','Сохранённых игровых событий пока нет.','hint'));
  (data.statuses||[]).forEach(item=>row($('#questStatuses'),`${dateTime(item.at)} · ${item.name.replaceAll('@','')} · ${item.platform}`,item.reason));
  if(!data.statuses?.length)$('#questStatuses').append(node('p','Игровых проверок пока нет.','hint'));
  (data.private_notices||[]).forEach(item=>row($('#questPrivateNotices'),`${dateTime(item.at)} · ${questStatusNames[item.status]||item.status} · ${item.kind==='assigned'?'выдача':item.kind==='completed'?'завершение':'обновление пути'}`,`Свиток ${Math.abs(item.run_id)} · ${item.run_id<0?'ручной':'автоматический'} · группа ${item.chat} · попыток ${item.attempts}`+(item.error?'\n'+item.error:'')));
  if(!data.private_notices?.length)$('#questPrivateNotices').append(node('p','Скрытых уведомлений пока нет.','hint'));
}
async function refreshQuests(){
  const data=await api('quests');fillForm($('#questSettingsForm'),data.settings);$('#questRules').replaceChildren();$('#questAutoRules').replaceChildren();$('#questAssignRule').replaceChildren();
  data.rules.forEach(rule=>{row($('#questRules'),`${rule.title}${rule.enabled?'':' · выключен'}`,rule.description+`\nНаграда ${rule.reward}, повтор через ${rule.cooldown_hours} ч`,[['Изменить',()=>{fillForm($('#questRuleForm'),rule);$('#questRuleEditor').open=true;}]]);if(rule.enabled)$('#questAssignRule').append(new Option(rule.title,rule.id));});
  (data.auto_rules||[]).forEach(rule=>row($('#questAutoRules'),`${rule.title}${rule.enabled?'':' · выключен'}`,`${questKindNames[rule.kind]} · цель ${rule.target} · срок ${rule.hours} ч · награда ${rule.reward}`,[['Изменить',()=>{const form=$('#questAutoRuleForm');fillForm(form,rule);for(const key of ['target','hours','reward'])form.elements[key].readOnly=rule.kind==='calm';$('#questAutoRuleEditor').open=true;}]]));
  renderQuestActivity(data);
}
$('#questSettingsForm').onsubmit=async event=>{event.preventDefault();try{await api('quests/settings',formData(event.target));await refreshQuests();toast('Квесты сохранены');}catch(error){toast(error.message,true);}};
$('#questRuleForm').onsubmit=async event=>{event.preventDefault();try{await api('quests/rule',formData(event.target));event.target.reset();event.target.elements.id.value='';await refreshQuests();toast('Свиток сохранён');}catch(error){toast(error.message,true);}};
$('#questAutoRuleForm').onsubmit=async event=>{event.preventDefault();try{await api('quests/auto-rule',formData(event.target));await refreshQuests();toast('Автоматический свиток сохранён');}catch(error){toast(error.message,true);}};
$('#questAssignForm').onsubmit=async event=>{event.preventDefault();const control=event.target.querySelector('button');control.disabled=true;try{await api('quests/assign',formData(event.target));await refreshQuests();toast('Личный свиток ожидает скрытой доставки');}catch(error){toast(error.message,true);}finally{control.disabled=false;}};
$('#questRuleNew').onclick=()=>{$('#questRuleForm').reset();$('#questRuleForm').elements.id.value='';};
$('#questRefresh').onclick=async()=>{try{await refreshQuests();}catch(error){toast(error.message,true);}};

async function refreshServices(){const data=await api('telegram/service-messages');fillForm($('#serviceSettingsForm'),data.settings);}
$('#serviceSettingsForm').onsubmit=async event=>{event.preventDefault();try{await api('telegram/service-messages/settings',formData(event.target));toast('Служебные ответы сохранены');}catch(error){toast(error.message,true);}};
$('#serviceCommands').onclick=async()=>{const el=$('#serviceCommands');el.disabled=true;try{const data=await api('telegram/service-messages/commands',{});$('#serviceCommandStatus').textContent=data.message;toast(data.message);}catch(error){$('#serviceCommandStatus').textContent=error.message;toast(error.message,true);}finally{el.disabled=false;}};

function selectTask(group,key){
  const panels=[...document.querySelectorAll('[data-task-group="'+group+'"]')];
  if(!panels.some(panel=>panel.dataset.taskPanel===key))key=panels[0]?.dataset.taskPanel;
  panels.forEach(panel=>panel.hidden=panel.dataset.taskPanel!==key);
  document.querySelectorAll('[data-task-nav="'+group+'"] [data-task-select]').forEach(control=>{const active=control.dataset.taskSelect===key;control.classList.toggle('active',active);control.setAttribute('aria-selected',String(active));control.tabIndex=active?0:-1;});
  try{sessionStorage.setItem('assistant-settings-tab-'+group,key);}catch(error){}
}
function organizeScoutSettings(){
  const root=$('#scout');if(!root||$('#scoutPlatforms'))return;
  const statuses={telegram:$('#scoutStatus'),received:$('#scoutReceiveStatus'),twitch:$('#twitchScoutStatus'),stats:$('#twitchScoutStats')};
  const original=[...root.children],settings=$('#scoutSettings'),twitchSettings=$('#twitchScoutSettings');
  const makePanel=(group,key,id)=>{const panel=node('div','','task-panel');panel.dataset.taskGroup=group;panel.dataset.taskPanel=key;panel.id=id;panel.setAttribute('role','tabpanel');return panel;};
  const makeNav=(group,entries,id)=>{const nav=node('div','','task-tabs');nav.dataset.taskNav=group;nav.id=id;nav.setAttribute('role','tablist');for(const [key,title] of entries){const control=button(title,()=>{});control.dataset.taskSelect=key;control.id=group+'-tab-'+key;control.setAttribute('role','tab');control.setAttribute('aria-selected',String(key===entries[0][0]));nav.append(control);}return nav;};
  const rootNav=makeNav('scout-platform',[['telegram','Telegram'],['twitch','Twitch']],'scoutPlatforms');
  const telegram=makePanel('scout-platform','telegram','scoutTelegramPlatform'),twitch=makePanel('scout-platform','twitch','scoutTwitchPlatform');
  const heading=root.querySelector('h2');root.replaceChildren(heading,rootNav,telegram,twitch);
  const tgKeys=[['connection','Подключение'],['chats','Чаты'],['behavior','Поведение'],['disguise','Маскировка'],['memory','Память и роли'],['reports','Донесения и сценки'],['checks','Проверки'],['journal','Журнал']];
  const twKeys=[['account','Аккаунт'],['login','Вход и браузер'],['behavior','Поведение и смайлы'],['log','Сообщения и награды'],['journal','Диагностика']];
  const tgNav=makeNav('scout-telegram',tgKeys,'scoutTelegramGroups'),twNav=makeNav('scout-twitch',twKeys,'scoutTwitchGroups');
  telegram.append(statuses.telegram,statuses.received,tgNav);twitch.append(statuses.twitch,statuses.stats,twNav);
  const tg={},tw={};for(const [key] of tgKeys)tg[key]=makePanel('scout-telegram',key,'scoutTelegram-'+key);
  for(const [key] of twKeys)tw[key]=makePanel('scout-twitch',key,key==='login'?'twitchScoutLoginPanel':'scoutTwitch-'+key);
  const twitchSource=original.find(child=>child.id==='twitchScoutPanel');
  // Keep every form and its controls together: hidden groups still save all settings.
  function splitForm(form,group,classify,saveKeys){
    const fragments={};for(const key of saveKeys)fragments[key]=makePanel(group,key,group+'-settings-'+key);
    let current=saveKeys[0];const children=[...form.children];
    const originalSave=children.find(child=>child.tagName==='BUTTON'&&(!child.type||child.type==='submit'));
    for(const child of children){if(child===originalSave)continue;current=classify(child,current);fragments[current].append(child);}
    form.replaceChildren(...Object.values(fragments));
    for(const key of saveKeys){const save=node('button',originalSave?.textContent||'Сохранить настройки');save.type='submit';fragments[key].append(save);}
    form.addEventListener('invalid',event=>revealSetting(event.target),true);
  }
  splitForm(settings,'scout-telegram',(child,current)=>{
    const fields=[...child.querySelectorAll('[name]')].map(field=>field.name);if(child.name)fields.unshift(child.name);
    if(fields.includes('clear_group_history'))return 'memory';
    if(fields.includes('disguise_enabled'))return 'disguise';
    if(fields.includes('prompt'))return 'behavior';
    if(fields.includes('remember')||fields.includes('book')||fields.includes('roles_chat'))return 'memory';
    if(fields.includes('glitch_tease_chance')||fields.includes('report_enabled')||fields.includes('duo_prompt'))return 'reports';
    return current;
  },['behavior','memory','disguise','reports']);
  let current='connection';
  for(const child of original){
    if(child===heading||child===twitchSource||child.id==='scoutStatus'||child.id==='scoutReceiveStatus')continue;
    if(child===settings){telegram.append(settings);current='chats';continue;}
    const summary=child.tagName==='DETAILS'?child.querySelector('summary')?.textContent||'':'';
    if(child.querySelector('#scoutHome')||child.id==='scoutTarget'||child.id==='scoutTargets'||child.id==='scoutJoinedGroups')current='chats';
    else if(summary==='Маскировка наблюдателя')current='disguise';
    else if(summary==='Тест сбоя основной помощник'||child.textContent==='Проверки перед запуском')current='checks';
    else if(summary==='Журнал разведки и донесений')current='journal';
    if(child.tagName==='H3'&&child.textContent.includes('Telegram —'))continue;
    if(child.classList.contains('divider'))continue;
    if(child.tagName==='DETAILS')child.open=true;
    tg[current].append(child);
  }
  // Source-only controls are small; the 900px browser screen lives in its own tab.
  splitForm(twitchSettings,'scout-twitch',(child,current)=>child.querySelector('[name="prompt"]')?'behavior':current,['account','behavior']);
  twitch.append(twitchSettings);
  for(const child of [...twitchSource.children]){
    if(child.tagName==='SUMMARY'||child===twitchSettings||child.id==='twitchScoutStatus'||child.id==='twitchScoutStats')continue;
    if(child.id==='twitchLogPanel'){tw.log.append(child);}
    else if(child.id==='twitchScoutBrowser'||child.id==='twitchScoutSession'||child.querySelector('#twitchScoutLogin')){
      if(child.tagName==='DETAILS')child.open=true;tw.login.append(child);
    }else if(child.tagName==='DETAILS'){
      child.open=true;tw.journal.append(child);
    }else tw.account.append(child);
  }
  const goLogin=button('Открыть вход и браузер',()=>selectTask('scout-twitch','login'),'outline');tw.account.append(goLogin);
  telegram.append(...Object.values(tg));twitch.append(...Object.values(tw));
  for(const [group,nav] of [['scout-platform',rootNav],['scout-telegram',tgNav],['scout-twitch',twNav]]){
    for(const control of nav.querySelectorAll('[data-task-select]')){
      const panels=[...root.querySelectorAll('[data-task-group="'+group+'"][data-task-panel="'+control.dataset.taskSelect+'"]')];
      control.setAttribute('aria-controls',panels.map(panel=>panel.id).join(' '));panels.forEach(panel=>panel.setAttribute('aria-labelledby',control.id));
    }
  }
}
organizeScoutSettings();

function organizeParticipantSettings(){
  const root=$('#social'),workspace=$('#participantWorkspace'),children=[...root.children],panels={},nav=node('div','','task-tabs');nav.dataset.taskNav='participants';nav.setAttribute('role','tablist');
  const entries=[['people','Участники'],['links','Связь аккаунтов'],['titles','Звания и уровни'],['book','Правки Книги'],['roles','Роли по чатам'],['settings','Настройки ролей'],['history','Журнал']];
  for(const [key,title] of entries){const control=button(title,()=>{});control.dataset.taskSelect=key;control.setAttribute('role','tab');control.setAttribute('aria-selected',String(key==='people'));nav.append(control);const panel=node('div','','task-panel');panel.dataset.taskGroup='participants';panel.dataset.taskPanel=key;panel.id='participants-'+key;panel.setAttribute('role','tabpanel');panels[key]=panel;}
  const heading=root.querySelector('h2'),intro=heading.nextElementSibling;root.replaceChildren(heading,intro,nav,...Object.values(panels));
  let current='people';
  const fields=children.flatMap(child=>child===workspace?[...workspace.children]:[child]);
  for(const child of fields){
    if(child===heading||child===intro)continue;
    const has=id=>child.id===id||child.querySelector('#'+id);
    if(has('participantLinkForm'))current='links';
    else if(has('grudgePeople'))current='people';
    else if(has('protectedForm')||has('honorForm'))current='titles';
    else if(has('bookAddPersonForm')||has('grudgeAdjustForm'))current='book';
    else if(has('socialRoleHistoryPanel'))current='history';
    else if(has('socialSettingsForm'))current='settings';
    else if(has('socialPermanentRoleForm')||has('socialRoleForm'))current='roles';
    else if(has('mtprotoForm')||has('scanCommunityGroups'))current='people';
    else if(child.tagName==='H3'&&/игров|постоян|назнач/i.test(child.textContent))current='roles';
    panels[current].append(child);
  }
  workspace.replaceChildren(nav,...Object.values(panels));root.replaceChildren(heading,intro,workspace);
}
organizeParticipantSettings();

document.querySelectorAll('[data-task-nav]').forEach(nav=>{
  const group=nav.dataset.taskNav,controls=[...nav.querySelectorAll('[data-task-select]')];
  controls.forEach((control,index)=>{control.onclick=()=>{selectTask(group,control.dataset.taskSelect);if(group==='scout-twitch'&&control.dataset.taskSelect==='log')refreshTwitchLog().catch(error=>toast(error.message,true));};control.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?controls.length-1:(index+(event.key==='ArrowRight'?1:-1)+controls.length)%controls.length;controls[next].click();controls[next].focus();};});
  let saved;try{saved=sessionStorage.getItem('assistant-settings-tab-'+group);}catch(error){}
  selectTask(group,saved||controls.find(control=>control.getAttribute('aria-selected')==='true')?.dataset.taskSelect);
});
function revealSetting(control){
  const page=control.closest('section.page-section');if(page)navigate('/'+page.id);
  for(let parent=control.parentElement;parent;parent=parent.parentElement){if(parent.dataset.taskGroup)selectTask(parent.dataset.taskGroup,parent.dataset.taskPanel);if(parent.tagName==='DETAILS')parent.open=true;}
  $('#settingsSearchResults').hidden=true;
  requestAnimationFrame(()=>{control.scrollIntoView({behavior:'smooth',block:'center'});control.focus({preventScroll:true});const label=control.closest('label');if(label){label.classList.add('setting-found');setTimeout(()=>label.classList.remove('setting-found'),2500);}});
}
$('#settingsSearch').oninput=event=>{
  const query=event.target.value.trim().toLocaleLowerCase('ru-RU').replaceAll('ё','е').split(/\s+/).filter(Boolean),results=$('#settingsSearchResults');results.replaceChildren();results.hidden=!query.length;if(!query.length)return;
  let count=0;
  for(const label of document.querySelectorAll('.page-section label')){
    const control=label.querySelector('input[name],select[name],textarea[name]');if(!control||control.disabled||control.type==='password')continue;
    const copy=label.cloneNode(true);copy.querySelectorAll('input,select,textarea,button').forEach(el=>el.remove());const title=copy.textContent.trim().replace(/\s+/g,' '),page=label.closest('.page-section'),group=label.closest('details')?.querySelector('summary')?.textContent||'';
    const terms=(title+' '+group+' '+(pageNames[page?.id]||'')).toLocaleLowerCase('ru-RU').replaceAll('ё','е');if(!query.every(word=>terms.includes(word)))continue;
    const result=button(title,()=>revealSetting(control),'setting-search-result');result.append(node('small',(pageNames[page?.id]||'')+(group?' · '+group:'')));results.append(result);if(++count===12)break;
  }
  if(!count)results.append(node('p','Настройка не найдена. Попробуйте более короткое слово.','hint'));
};
$('#settingsSearch').onkeydown=event=>{if(event.key==='Escape'){$('#settingsSearchResults').hidden=true;event.target.value='';}};

async function refreshReplyPerformance(){const data=await api('reply-performance');$('#memoryIndexStatus').textContent=data.memory_index.available?(data.memory_index.ready?'Индекс памяти готов: старые темы ищутся локально.':'Старый архив индексируется в фоне; история уже доступна обычным поиском.'):'В этой сборке SQLite нет FTS5; работает обычный поиск памяти.';$('#replyPerformance').replaceChildren();for(const item of data.events){const sec=value=>(value/1000).toFixed(2)+' с';row($('#replyPerformance'),dateTime(item.at)+' · '+(item.chat||'веб-проверка')+' · участник '+item.person,`До ответа ${sec(item.total_ms+item.queue_ms)} · очередь ${sec(item.queue_ms)} · предварительный анализ ${sec(item.precheck_ms)} · ответ ${sec(item.answer_ms)} · контекст внутри ответа ${sec(item.context_ms)}`+(item.error?' · '+item.error:''));}if(!data.events.length)$('#replyPerformance').append(node('p','После обычного ответа здесь появится измерение.','hint'));}
$('#replyPerformanceRefresh').onclick=()=>refreshReplyPerformance().catch(error=>toast(error.message,true));

let speechPanel=null;
function speechPresetDescription(){
  const form=$('#speechForm'),preset=speechPanel?.presets.find(item=>item.id===form.elements.preset.value);
  $('#speechPresetDescription').textContent=preset?.description||'Свой вариант: базовый голос, темп и тон выбраны вручную.';
}
function selectSpeechPreset(id){
  const preset=speechPanel?.presets.find(item=>item.id===id);if(!preset&&id!=='custom')return;
  fillForm($('#speechForm'),preset?{preset:id,speaker:preset.speaker,rate:preset.rate,pitch:preset.pitch}:{preset:id});
  speechPresetDescription();if(id==='custom')$('#speechCustomVoice').open=true;
}
function populateSpeechPresets(presets){
  const select=$('#speechPreset');select.replaceChildren();const groups=new Map();
  for(const preset of presets){let group=groups.get(preset.group);if(!group){group=document.createElement('optgroup');group.label=preset.group;groups.set(preset.group,group);select.append(group);}const option=document.createElement('option');option.value=preset.id;option.textContent=preset.label;group.append(option);}
  const custom=document.createElement('option');custom.value='custom';custom.textContent='Свой вариант';select.append(custom);
}
async function refreshSpeech(fill=false){
  const data=await api('speech');const first=!speechPanel;speechPanel=data;
  if(first||fill){populateSpeechPresets(data.presets);fillForm($('#speechForm'),data.settings);speechPresetDescription();}
  const status=data.preparing?'Подготовка зависимостей и голоса в фоне…':!data.settings.enabled?'Озвучка выключена. Голосовая модель выгружена.':!data.ready?'Голос не подготовлен. Нажмите «Подготовить Silero».':data.loaded?'Голос загружен.':'Голос загружается в фоне.';
  $('#speechStatus').textContent=status+' Ожидают: '+data.waiting+(data.active?' · озвучивается запрос участника '+data.active.person+' в чате '+data.active.chat:'')+(data.loaded?' · память процесса '+data.rss_mb+' МБ':'')+(!data.cpu_cap_supported?' · На этой ОС действует один поток и пониженный приоритет; доля CPU ограничивается только в Linux/Docker.':'');
  $('#speechPrepare').disabled=data.preparing;const list=$('#speechEvents'),scroll=list.scrollTop;list.replaceChildren();
  const labels={test_request:'Запрос теста получен',checking:'Проверка чата',prepare:'Подготовка',ready:'Готово',queued:'В очереди',generating:'Генерация',sent:'Отправлено',error:'Ошибка',cancelled:'Отменено',skipped:'Пропущено',loaded:'Загружено',unloaded:'Выгружено'};
  data.events.forEach(item=>row(list,dateTime(item.at)+' · '+(labels[item.status]||item.status)+(item.chat?' · чат '+item.chat:''),item.detail+(item.queue_ms?' · очередь '+(item.queue_ms/1000).toFixed(2)+' с':'')+(item.synthesis_ms?' · загрузка и синтез '+(item.synthesis_ms/1000).toFixed(2)+' с':'')+(item.duration?' · запись '+item.duration+' с':'')));list.scrollTop=scroll;
}
$('#speechForm').onsubmit=async event=>{event.preventDefault();try{await api('speech/settings',formData(event.target));await refreshSpeech(true);toast('Настройки озвучки сохранены.');}catch(error){toast(error.message,true);}};
$('#speechPreset').onchange=event=>selectSpeechPreset(event.target.value);
['speaker','rate','pitch'].forEach(key=>$('#speechForm').elements[key].onchange=()=>{fillForm($('#speechForm'),{preset:'custom'});speechPresetDescription();});
document.querySelectorAll('[data-speech-preset]').forEach(el=>el.onclick=()=>selectSpeechPreset(el.dataset.speechPreset));
$('#speechPrepare').onclick=async()=>{try{toast((await api('speech/prepare',{})).message);await refreshSpeech();}catch(error){toast(error.message,true);}};
$('#speechPrivateChat').onclick=()=>{const id=speechPanel?.private_chat||state.settings.admin_user_id;if(!id){toast('Укажите Telegram ID администратора в настройках или впишите личный ID вручную.',true);return;}$('#speechTestForm').elements.chat.value=id;};
$('#speechAllowPrivate').onclick=async event=>{const el=event.currentTarget,id=$('#speechTestForm').elements.chat.value.trim();if(!/^[1-9]\d{0,19}$/.test(id)){toast('Для личного чата нужен числовой Telegram ID без минуса.',true);return;}el.disabled=true;try{const data=formData($('#speechForm'));data.chats=[...new Set([...data.chats.split(/[,;\s]+/).filter(Boolean),id])].join('\n');data.enabled=true;await api('speech/settings',data);await refreshSpeech(true);toast('Личный чат разрешён. Откройте бота в Telegram и отправьте /start, затем запустите тест.');}catch(error){toast(error.message,true);}finally{el.disabled=false;}};
$('#speechTestForm').onsubmit=async event=>{
  event.preventDefault();const form=event.currentTarget,submit=form.querySelector('button[type="submit"],button:not([type])'),output=$('#speechTestStatus'),data=formData(form);output.hidden=false;submit.disabled=true;
  try{
    const chat=String(data.chat).trim();if(/^[1-9]\d*$/.test(chat)&&speechPanel&&!speechPanel.settings.chats.split(/[,;\s]+/).includes(chat))throw new Error('Этот ID не сохранён в разрешённых чатах озвучки. Добавьте его и сохраните настройки.');
    const voice=formData($('#speechForm'));for(const key of ['preset','speaker','rate','pitch'])data[key]=voice[key];
    output.textContent='Передаю запрос теста в очередь…';const result=await api('telegram/voice-preview',data);output.textContent=result.message;toast(result.message);
    refreshSpeech().catch(()=>{output.textContent=result.message+' Не удалось обновить журнал: проверьте соединение с панелью.';});
  }catch(error){const message=error instanceof TypeError?'Не удалось получить подтверждение от веб-панели. Нажмите «Обновить» в журнале: если запрос получен, там появится запись теста.':error.message;output.textContent=message;toast(message,true);}finally{submit.disabled=false;}
};
$('#speechRefresh').onclick=()=>refreshSpeech().catch(error=>toast(error.message,true));
setInterval(()=>{if(location.pathname==='/speech'&&!$('#dashboard').hidden)refreshSpeech().catch(()=>{});},3000);

// Provider pool: polling updates results only and never overwrites a settings draft.
let modelsPanel = null, modelPanelBusy = false;
const modelKinds = {kimi:'Kimi', openrouter:'OpenRouter Free', groq:'Groq', gemini:'Gemini', litellm:'LiteLLM'};
function modelErrorMessage(value){const [kind,code]=String(value||'').split(':');const reasons={max_output_tokens:'тест не завершён из-за лимита генерации; модель не отключена',connection_failed:'не удалось соединиться с API или истёк таймаут',model_not_found:'модель недоступна: проверьте ID',insufficient_quota:'исчерпан баланс или лимит расходов',invalid_api_key:'API-ключ недействителен'};return reasons[code]||({QuotaExceeded:'исчерпана квота или достигнут лимит',IncompleteResponse:'тест не завершён; модель не отключена',AIError:'ошибка API или модели',ValueError:'проверьте настройки',TimeoutError:'истёк таймаут'}[kind]||kind)+(code?' ('+code+')':'');}

function resetEndpoint(kind='kimi'){const form=$('#modelEndpointForm');form.reset();form.elements.kind.value=kind;const preset=modelsPanel?.presets?.[kind];if(preset)fillForm(form,{id:'',label:preset.title,base_url:preset.base_url,model:preset.model,api_key:'',vision:false});}
function editEndpoint(profile){fillForm($('#modelEndpointForm'),{...profile,vision:Boolean(profile.vision),api_key:''});$('#modelEndpointForm').closest('details').open=true;$('#modelEndpointForm').scrollIntoView({behavior:'smooth',block:'center'});}
function syncModelPrimary(selectedValue){const select=$('#modelPrimary'),value=selectedValue||select.value;select.replaceChildren();document.querySelectorAll('#modelSelection input:checked').forEach(input=>{const p=modelsPanel.providers.find(item=>item.id===input.value);if(p)select.append(new Option(p.label+' · '+(p.model||'не выбрана'),p.id));});if([...select.options].some(option=>option.value===value))select.value=value;}
async function refreshModels(fill=false){
  if(modelPanelBusy)return;modelPanelBusy=true;
  try{
    const data=await api('models');modelsPanel=data;
    if(fill){fillForm($('#modelRouterForm'),data.settings);const selected=$('#modelSelection');selected.replaceChildren();for(const p of data.providers){const label=node('label','','check'),input=node('input');input.type='checkbox';input.value=p.id;input.checked=data.settings.selected.includes(p.id);input.onchange=()=>syncModelPrimary();label.append(input,document.createTextNode(p.label+' · '+(p.model||'модель не выбрана')));selected.append(label);}syncModelPrimary(data.settings.primary);if(!$('#modelEndpointForm').elements.base_url.value)resetEndpoint();}
    $('#localOcrStatus').textContent=data.ocr.detail;
    const list=$('#modelProviders');list.replaceChildren();
    for(const p of data.providers){
      const m=p.metrics||{},quality=m.grade_count?(m.grade_total/m.grade_count).toFixed(0)+'% ('+m.grade_count+' оценок)':'не оценена';
      const paused=m.retry_at>Date.now()/1000;
      const detail=[p.model||'Модель не выбрана',p.configured?'подключение настроено':'нужна настройка',m.latency_ms==null?'скорость ещё не измерена':(m.latency_ms/1000).toFixed(2)+' с', 'качество: '+quality,paused?'временная пауза: '+modelErrorMessage(m.error):'',m.probe_error&&!paused?'последний тест: '+modelErrorMessage(m.probe_error):'',(m.checked_at||m.probe_at)?'проверено '+dateTime(m.checked_at||m.probe_at):''].filter(Boolean).join(' · ');
      const actions=[['Проверить доступность',async()=>{await api('models/check',{id:p.id});await refreshModels();toast('Проверка запущена в фоне');}]];
      if(!p.builtin){actions.push(['Изменить',()=>editEndpoint(p)],['Список моделей',async()=>{const result=await api('models/list',{id:p.id});editEndpoint(p);const choices=$('#routerModelIds');choices.replaceChildren();for(const item of result.models)choices.append(new Option(item.id+(item.vision?' · изображения':''),item.id));toast('Моделей: '+result.models.length+'. Выберите ID и сохраните подключение');}],['Удалить',async()=>{if(!confirm('Удалить подключение '+p.label+'?'))return;await api('models/delete',{id:p.id});await refreshModels(true);}]);}
      else actions.push(['Настроить',()=>{$('#modelLegacyForm').closest('details').open=true;$('#modelLegacyForm').scrollIntoView({behavior:'smooth',block:'start'});}]);
      row(list,p.label,detail,actions);
    }
    const evaluations=$('#modelEvaluations');evaluations.replaceChildren();
    for(const evaluation of data.evaluations){
      const labels={running:'Выполняется',complete:'Завершено',interrupted:'Прервано перезапуском',reference_failed:'Эталонная модель недоступна'};
      const card=node('details','','item');card.open=true;card.append(node('summary',dateTime(evaluation.at)+' · '+(labels[evaluation.status]||evaluation.status)),node('p','Вопрос: '+evaluation.question),node('p','Эталон ('+evaluation.reference_provider+'): '+(evaluation.reference||'Ожидание ответа')));
      for(const answer of evaluation.answers){const providerName=data.providers.find(p=>p.id===answer.provider)?.label||answer.provider;const entry=row(card,providerName+' · '+answer.model,answer.error?'Ошибка: '+answer.error:answer.answer+'\nВремя: '+(answer.latency_ms/1000).toFixed(2)+' с');if(answer.answer&&!answer.error){const actions=node('div','','inline'),label=node('label','Ваша оценка, 0–100'),input=node('input');input.type='number';input.min='0';input.max='100';input.value=answer.grade??'';input.setAttribute('aria-label','Оценка '+answer.provider);label.append(input);actions.append(label,button('Сохранить оценку',async()=>{if(input.value==='')throw new Error('Введите оценку');await api('models/grade',{id:evaluation.id,provider:answer.provider,grade:Number(input.value)});await refreshModels();toast('Оценка сохранена');}));entry.append(actions);}}
      evaluations.append(card);
    }
    if(!data.evaluations.length)evaluations.append(node('p','Проверок качества пока нет.','hint'));
  }finally{modelPanelBusy=false;}
}
$('#modelRouterForm').onsubmit=async event=>{event.preventDefault();const submit=event.target.querySelector('button');submit.disabled=true;try{const data=formData(event.target);data.selected=[...document.querySelectorAll('#modelSelection input:checked')].map(input=>input.value);await api('models/settings',data);await refreshModels(true);toast('Пул моделей сохранён');}catch(error){toast(error.message,true);}finally{submit.disabled=false;}};
$('#modelEndpointForm').onsubmit=async event=>{event.preventDefault();const submit=event.target.querySelector('button');submit.disabled=true;try{await api('models/provider',formData(event.target));resetEndpoint();await refreshModels(true);toast('Подключение сохранено. Отметьте его среди резервных и сохраните выбор моделей.');}catch(error){toast(error.message,true);}finally{submit.disabled=false;}};
$('#modelLegacyForm').onsubmit=async event=>{event.preventDefault();try{await api('settings',formData(event.target));await refresh();await refreshModels();toast('Настройки подключения сохранены');}catch(error){toast(error.message,true);}};
$('#modelPreset').onchange=()=>resetEndpoint($('#modelPreset').value);
$('#modelEndpointReset').onclick=()=>resetEndpoint();
$('#modelsRefresh').onclick=()=>refreshModels().catch(error=>toast(error.message,true));
$('#modelEvaluationForm').onsubmit=async event=>{event.preventDefault();const submit=event.target.querySelector('button');submit.disabled=true;try{await api('models/evaluate',formData(event.target));await refreshModels();toast('Сравнение запущено; результаты появятся ниже');}catch(error){toast(error.message,true);}finally{submit.disabled=false;}};
setInterval(()=>{if(location.pathname==='/models'&&!$('#dashboard').hidden&&(modelsPanel?.busy?.length||modelsPanel?.evaluations?.some(item=>item.status==='running')))refreshModels().catch(()=>{});},3000);

let streamSeriesBusy=false;
async function refreshStreamSeries(){
  $('#streamSeriesStatus').textContent='Загружаю историю рубрик…';
  const data=await api('route-reaction/series?'+new URLSearchParams(formData($('#streamSeriesFilter'))));
  streamSeriesBusy=data.rebuilding||Boolean(data.checking.length);
  $('#streamSeriesStatus').textContent=data.rebuilding?'Идёт сбор истории из сохранённых публикаций. Обновите после завершения.':data.checking.length?'Проверяется справка об игре.':'Показаны только сохранённые анонсы. Это не полный список сыгранных эфиров.';
  const list=$('#streamSeriesRows');list.replaceChildren();
  for(const item of data.series){
    const kind={announced:'объявлен финальный стрим',event:'финал турнира или группы',near:'приближение к финалу',none:'обычный анонс',undated:'дата публикации не подтверждена; финал не засчитан'};
    const detail=`Группа: ${item.chat} · канал: ${item.channel}\nЗаход ${item.run} · прежних эфиров в истории: ${item.prior_broadcasts} · прежних финалов этого захода: ${item.prior_finals}\n${item.episode?'Номер выпуска в анонсе: '+item.episode+' · ':''}${kind[item.finale]||item.finale}${item.continuity_uncertain?'\nСвязь после долгого перерыва неясна: шутка про повторный финал отключена.':''}\nПоследний анонс: ${dateTime(item.published_at)}\n${item.body.slice(0,220)}`;
    const entry=row(list,item.title,detail);
    for(const previous of item.previous||[])entry.append(node('p',dateTime(previous.published_at)+' · '+(previous.episode?'выпуск '+previous.episode+' · ':'')+(kind[previous.finale]||previous.finale)+' · '+previous.body.slice(0,120),'hint'));
    for(const bit of item.author_wordplay||[])entry.append(node('p','Каламбур стримера: «'+bit.quote+'»'+(bit.idea?' · '+bit.idea:''),'hint'));
    for(const joke of item.jokes||[])entry.append(node('p',(joke.relation==='author_callback'?'Подхват каламбура стримера: ':joke.relation==='continuation'?'Продолжение прежней шутки: ':'Прежняя шутка: ')+joke.text+(joke.idea?'\nИдея: '+joke.idea:'')+(joke.new_detail?'\nНовый шаг: '+joke.new_detail:'')+(!joke.can_continue?'\nПринадлежит другому заходу или связь не подтверждена — продолжать нельзя.':''),'hint'));
  }
  if(!data.series.length)list.append(node('p','Рубрик пока нет. Можно собрать историю из уже сохранённых репостов.','empty'));
  const games=$('#streamGameRows');games.replaceChildren();
  const states={checked:'источник проверен',cached:'сохранённая справка; обновление пока недоступно',builtin:'встроенные факты',pending:'справка ещё не проверена',unavailable:'источник пока не подтверждён'};
  for(const game of data.games){
    row(games,game.title,(game.notes||'')+'\n'+(game.summary||'')+'\n'+(states[game.fact_status]||game.fact_status)+(game.source_url?'\nИсточник: '+game.source_url:'')+(game.checked_at?'\nПроверено: '+dateTime(game.checked_at):''),[
      ['Изменить',()=>{fillForm($('#streamGameForm'),{...game,aliases:JSON.parse(game.aliases).join('\n')});$('#streamGameForm').scrollIntoView({block:'center'});}],
      ['Проверить справку',async()=>{await api('route-reaction/game/check',{game_key:game.game_key});await refreshStreamSeries();}]
    ]);
  }
}
$('#streamSeriesPanel').addEventListener('toggle',()=>{if($('#streamSeriesPanel').open)refreshStreamSeries().catch(error=>toast(error.message,true));});
$('#streamSeriesFilter').onsubmit=async event=>{event.preventDefault();try{await refreshStreamSeries();}catch(error){toast(error.message,true);}};
$('#streamSeriesRebuild').onclick=async()=>{try{await api('route-reaction/series/rebuild',{});await refreshStreamSeries();}catch(error){toast(error.message,true);}};
$('#streamGameForm').onsubmit=async event=>{event.preventDefault();try{await api('route-reaction/game',formData(event.target));event.target.reset();await refreshStreamSeries();toast('Карточка игры сохранена');}catch(error){toast(error.message,true);}};

setInterval(()=>{if(streamSeriesBusy&&$('#streamSeriesPanel').open&&!$('#dashboard').hidden)refreshStreamSeries().catch(()=>{});},3000);

async function refreshStreamSeriesJournal(){
  const data=await api('route-reaction/journal?'+new URLSearchParams(formData($('#streamSeriesJournalFilter'))));
  const labels={post_indexed:'Анонс учтён',game_saved:'Карточка игры изменена',settings_changed:'Настройки реакций изменены',reviewed:'Проверка идеи и связи',reaction_queued:'Реакция запланирована',reaction_error:'Ошибка реакции',reaction_sent:'Шутка отправлена',reaction_skipped:'Шутка пропущена',route_archived:'Сведения сохранены перед сменой маршрута',rebuild_started:'Сбор истории начат',rebuild_finished:'Сбор истории завершён',facts_checked:'Источник игры проверен',facts_unavailable:'Источник игры пока недоступен'};
  const reasons={reactions_disabled:'Автоматические реакции выключены',chat_not_selected:'Группа не выбрана для реакций',chance_or_existing_record:'Не выпал шанс или запись уже обработана',route_changed_during_generation:'Маршрут изменился во время анализа',repeat_idea:'Повтор смысловой идеи',repeat_text:'Почти дословный повтор',wrong_parent_or_run:'Прежняя шутка относится к другому заходу или не найдена',no_new_detail:'Нет нового шага для продолжения',unsupported_quote:'Авторская цитата не найдена в анонсе',unsupported_final:'Нет основания для подкола про повторный финал',unreviewed:'Модель не вернула проверку смысла',model_skip:'Модель не нашла уместного подкола',forced:'Натянутая шутка',repeat:'Смысловой повтор',unsupported:'Шутка не подтверждается анонсом',invalid_format:'Модель не вернула ожидаемую структуру',wrong_rubric:'Шутка связана с другой рубрикой'};
  const list=$('#streamSeriesJournalRows');list.replaceChildren();
  for(const item of data.events){let detail=reasons[item.detail]||item.detail;
    if(item.event==='reviewed'){try{const review=JSON.parse(item.detail);detail='Заход '+review.run+' · '+({first_observed:'первая сохранённая серия анонсов',explicit_restart:'явно объявлен новый заход',first_episode_after_long_gap:'первый выпуск после долгого перерыва'}[review.run_reason]||review.run_reason)+'\nПроверка: '+(reasons[review.verdict]||review.verdict||'историческая запись')+(review.idea?'\nИдея: '+review.idea:'')+(review.author_quote?'\nФраза стримера: «'+review.author_quote+'»':'')+(review.new_detail?'\nРазвитие: '+review.new_detail:'');}catch(error){}}
    if(item.event==='reaction_sent'){try{const review=JSON.parse(item.detail);detail=review.text+'\nСвязь: '+({author_callback:'подхват каламбура стримера',continuation:'продолжение шутки помощник',new:'новый подкол'}[review.relation]||'новый подкол')+(review.author_quote?'\nФраза стримера: «'+review.author_quote+'»':'')+(review.new_detail?'\nРазвитие: '+review.new_detail:'');}catch(error){}}
    row(list,dateTime(item.at)+' · '+(labels[item.event]||item.event),[item.chat?'Группа: '+item.chat:'',item.channel?'Канал: '+item.channel:'',item.game_key?'Рубрика: '+item.game_key:'',item.source_key?'Публикация: '+item.source_key:'',detail].filter(Boolean).join('\n'));
  }
  if(!data.events.length)list.append(node('p','По выбранным фильтрам записей нет.','empty'));
}
$('#streamSeriesJournalPanel').addEventListener('toggle',()=>{if($('#streamSeriesJournalPanel').open)refreshStreamSeriesJournal().catch(error=>toast(error.message,true));});
$('#streamSeriesJournalFilter').onsubmit=async event=>{event.preventDefault();try{await refreshStreamSeriesJournal();}catch(error){toast(error.message,true);}};


let twitchScoutTransport="browser",twitchScoutLoaded=false,twitchScoutFrameBusy=false,twitchScoutStatusBusy=false;
let twitchLogOffset=0;
async function refreshTwitchLog(){
  const form=$('#twitchLogFilter'),query=new URLSearchParams({kind:form.elements.kind.value,offset:twitchLogOffset});if(form.elements.day.value)query.set('day',form.elements.day.value);
  const data=await api('scout/twitch/log?'+query);if(!form.elements.day.value)form.elements.day.value=data.day;
  const states={not_started:'чтение наград ещё не запущено',reading:'карточки наград читаются',waiting_for_browser_chat:'ожидается экран чата',waiting_for_chat:'ожидается подключение чата',disabled:'чтение карточек выключено',unavailable:'карточки недоступны'};
  $('#twitchLogState').textContent=data.day+' · Москва · сообщений '+data.messages+' · наград '+data.rewards+' · '+(states[data.reward_reader]||data.reward_reader)+(data.reward_error?' · '+data.reward_error:'');
  const list=$('#twitchLogRows');list.replaceChildren();for(const item of data.rows)row(list,dateTime(item.at)+' · '+(item.kind==='reward'?'Награда':'Сообщение')+' · @'+item.login,item.text+(item.kind==='reward'?'\n'+item.title+' · '+(item.cost===null?'стоимость не передана':item.cost+' баллов')+' · источник: '+item.source:''));
  if(!data.rows.length)list.append(node('p','За выбранный день в этой категории записей нет.'));
  $('#twitchLogPrev').disabled=twitchLogOffset===0;$('#twitchLogNext').disabled=twitchLogOffset+data.rows.length>=data.total;
}
$('#twitchLogFilter').onsubmit=event=>{event.preventDefault();twitchLogOffset=0;refreshTwitchLog().catch(error=>toast(error.message,true));};
$('#twitchLogPrev').onclick=()=>{twitchLogOffset=Math.max(0,twitchLogOffset-100);refreshTwitchLog().catch(error=>toast(error.message,true));};
$('#twitchLogNext').onclick=()=>{twitchLogOffset+=100;refreshTwitchLog().catch(error=>toast(error.message,true));};
for(const [id,format] of [['twitchLogJSONL','jsonl'],['twitchLogText','txt']])$('#'+id).onclick=async()=>{try{if(!$('#twitchLogFilter').elements.day.value)await refreshTwitchLog();const response=await fetch('/api/scout/twitch/export?'+new URLSearchParams({day:$('#twitchLogFilter').elements.day.value,format}),{credentials:'same-origin'});if(!response.ok){const error=await response.json();throw Error(error.error||'Выгрузка не получена');}const url=URL.createObjectURL(await response.blob()),link=node('a');link.href=url;link.download='twitch-'+$('#twitchLogFilter').elements.day.value+'.'+format;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(error){toast(error.message,true);}};
$('#twitchIgnoredDefaults').textContent='По умолчанию игнорируются: Nightbot, StreamElements, Streamlabs, Moobot, Fossabot, Wizebot, Botisimo, PretzelRocks, CommanderRoot, SoundAlerts, Blerp, Sery_bot, Stay_hydrated_bot, Ankhbot, Deepbot, Coebot, Phantombot, Supibot, Logviewer, PokemonCommunityGame. Список выше дополняет этот набор; используются точные логины.';
$('#participantLinkForm').onsubmit=async event=>{event.preventDefault();try{await api('social/link',formData(event.target));await refreshParticipantRoles();toast('Аккаунты объединены');}catch(error){toast(error.message,true);}};
async function refreshRepostStatistics(){
  const data=await api('repost-stats?'+new URLSearchParams({chat:$('#repostStatsForm').elements.chat.value})),control=$('#repostStatsForm').elements.chat,value=control.value||data.main_chat;
  control.replaceChildren(new Option('Основной канал не выбран',''));for(const chat of data.chats)control.append(new Option(chat,chat));control.value=value;
  const summary=$('#repostStatsSummary');summary.replaceChildren();if(data.report){row(summary,data.report.period,JSON.stringify(data.report.counts)+'\n'+data.report.note);}
  const list=$('#repostGenreRows');list.replaceChildren();for(const game of data.genres){const card=row(list,game.title,''),genre=node('input'),kind=node('select');genre.value=game.genre;genre.placeholder='Жанр';genre.setAttribute('aria-label','Жанр '+game.title);for(const [key,label] of [['unknown','Гача: неизвестно'],['1','Гача'],['0','Не гача']])kind.append(new Option(label,key));kind.value=game.is_gacha===null?'unknown':String(game.is_gacha);card.append(genre,kind,button('Сохранить жанр',async()=>{await api('repost-stats/genre',{game_key:game.game_key,genre:genre.value,is_gacha:kind.value});toast('Жанр сохранён');}));}
}
$('#repostStatistics').ontoggle=()=>{if($('#repostStatistics').open)refreshRepostStatistics().catch(error=>toast(error.message,true));};
$('#repostStatsForm').onsubmit=async event=>{event.preventDefault();try{await api('repost-stats/main',formData(event.target));await refreshRepostStatistics();toast('Основной канал сохранён');}catch(error){toast(error.message,true);}};
async function refreshTwitchScout(force=false){
  if(twitchScoutStatusBusy)return;
  twitchScoutStatusBusy=true;
  try{
    const data=await api('scout/twitch');
    twitchScoutTransport=data.settings.transport||'browser';
    if(force||!twitchScoutLoaded){fillForm($('#twitchScoutSettings'),data.settings);twitchScoutLoaded=true;}
    const phases={stopped:'Остановлена',starting:twitchScoutTransport==='irc'?'Подготавливается прямой чат':'Запускается браузер',validating:'Проверяется сессия Twitch',connecting:'Подключается прямой чат',authenticating:'Ожидается подтверждение аккаунта чатом',joining:'Ожидается вход в канал',retry_wait:'Ожидается переподключение',waiting:'Браузер открыт; ожидается чат',connected:'Поток чата подключён',error:twitchScoutTransport==='irc'?'Ошибка прямого чата':'Ошибка браузера'};
    const parts=[phases[data.phase]||data.phase,data.authenticated?'Вход подтверждён: @'+data.account.login:'Вход указанным аккаунтом не подтверждён'];
    if(data.error)parts.push(data.error);
    if(data.browser_version)parts.push(data.browser_version);
    const direct=data.direct_chat||{};
    if(data.browser_mode==='irc-websocket'){
      parts.push('Прямой чат Twitch (IRC)');$('#twitchScoutFrame').hidden=true;
      if(direct.session_verified)parts.push('Сессия проверена: @'+direct.verified_login);
      if(data.phase!=='connected'&&data.phase!=='stopped')parts.push('Попытка '+(direct.attempt||0)+' · этап: '+({profile:'чтение профиля',validate:'проверка сессии',websocket:'соединение WebSocket',authenticate:'авторизация в чате',join:'вход в канал'}[direct.stage]||direct.stage||'подготовка')+' · '+(direct.elapsed||0)+' с');
      if(direct.retry_at&&data.phase==='retry_wait')parts.push('Следующая попытка: '+dateTime(direct.retry_at));
      if(direct.failure?.code&&!(data.error||'').includes(direct.failure.code))parts.push('Последняя ошибка: ['+direct.failure.code+'] '+direct.failure.message);
    }
    if(data.browser_mode==='native-cdp')parts.push('Управление Chrome без Playwright');
    if(data.login_warning)parts.push(data.login_warning);
    const deps=data.dependencies||{};
    if(twitchScoutTransport==='browser'&&(!deps.controller||!deps.chromium||!deps.display))parts.push('Компоненты браузера ещё не готовы; перезапустите контейнер после обновления и дождитесь установки');
    $('#twitchScoutStatus').textContent=parts.join(' · ');
    $('#twitchScoutDiagnostics').textContent=data.browser_mode==='irc-websocket'?
      ((direct.events||[]).map(item=>dateTime(item.at)+' · '+item.code+' · '+item.message).join('\n')||'Прямой чат ещё не запускался. Токены и содержимое сообщений не записываются.'):
      ((data.login_diagnostics||[]).map(item=>dateTime(item.at)+' · '+item.endpoint+' · HTTP '+item.status+(typeof item.integrity_token_received==='boolean'?' · токен проверки получен: '+(item.integrity_token_received?'да':'нет'):'')+(typeof item.error_code==='number'?' · код ошибки: '+item.error_code:'')+(typeof item.rejected_client==='boolean'?' · отказ клиенту: '+(item.rejected_client?'да':'нет'):'')).join('\n')||'Ответов сервера входа пока нет. Пароли, токены и содержимое запросов в диагностику не записываются.');
    $('#twitchScoutStats').textContent='Сообщений: '+data.messages+' · участников: '+data.participants+' · последний приём: '+dateTime(data.last_read);
    const out=$('#twitchScoutOutbox');out.replaceChildren();
    const labels={queued:'В очереди',sending:'Отправляется',sent:'Twitch подтвердил отправку',failed:'Twitch отклонил',uncertain:'Результат неизвестен; повтора не будет',cancelled:'Отменено'};
    for(const item of data.outbox||[])out.append(node('p',dateTime(item.at)+' · '+(labels[item.status]||item.status),'hint'));
  }finally{twitchScoutStatusBusy=false;}
}
async function twitchScoutFrame(){
  if(twitchScoutTransport==='irc'){ $('#twitchScoutBrowserState').textContent='Для прямого чата экран браузера не используется; состояние подключения показано выше.';return;}
  if(twitchScoutFrameBusy||$('#scout').hidden||$('#scoutTwitchPlatform')?.hidden||$('#twitchScoutLoginPanel')?.hidden||!$('#twitchScoutBrowser').open)return;
  twitchScoutFrameBusy=true;
  try{
    const response=await fetch('/api/scout/twitch/frame',{credentials:'same-origin',cache:'no-store'});
    if(!response.ok){const error=await response.json();throw new Error(error.error||'Браузер не готов');}
    const picture=$('#twitchScoutFrame'),previous=picture.dataset.objectUrl,url=URL.createObjectURL(await response.blob());
    picture.src=url;picture.dataset.objectUrl=url;picture.hidden=false;if(previous)URL.revokeObjectURL(previous);
    $('#twitchScoutBrowserState').textContent='Нажмите поле на экране браузера для ввода.';
  }catch(error){$('#twitchScoutBrowserState').textContent=error.message;}
  finally{twitchScoutFrameBusy=false;}
}
document.addEventListener('DOMContentLoaded',()=>{
  $('#twitchScoutSessionForm').onsubmit=async event=>{event.preventDefault();const form=event.target,input=form.elements.token,value=input.value,submit=form.querySelector('button');input.value='';submit.disabled=true;try{await api('scout/twitch/session',{token:value});selectTask('scout-platform','twitch');selectTask('scout-twitch','login');$('#twitchScoutBrowser').open=true;await refreshTwitchScout();await twitchScoutFrame();toast('Сессия перенесена в Chrome; ожидается подтверждение аккаунта чатом');}catch(error){toast(error.message,true);}finally{submit.disabled=false;}};
  $('#twitchScoutSettings').onsubmit=async event=>{event.preventDefault();try{await api('scout/twitch/settings',formData(event.target));await refreshTwitchScout(true);toast('Twitch-наблюдатель сохранена');}catch(error){toast(error.message,true);}};
  $('#twitchScoutDirect').onclick=async()=>{try{const result=await api('scout/twitch/open',{destination:'direct'});twitchScoutTransport=result.settings.transport;$('#twitchScoutSettings').elements.transport.value=twitchScoutTransport;await refreshTwitchScout();toast('Прямой чат подключается; ожидается проверка сессии Twitch');}catch(error){toast(error.message,true);}};
  $('#twitchScoutRefresh').onclick=()=>refreshTwitchScout().catch(error=>toast(error.message,true));
  for(const [id,destination] of [['twitchScoutLogin','login'],['twitchScoutChat','chat'],['twitchScoutRestart','restart']])$('#'+id).onclick=async()=>{try{const result=await api('scout/twitch/open',{destination});twitchScoutTransport=result.settings.transport;$('#twitchScoutSettings').elements.transport.value=twitchScoutTransport;selectTask('scout-platform','twitch');selectTask('scout-twitch','login');$('#twitchScoutBrowser').open=true;await refreshTwitchScout();await twitchScoutFrame();}catch(error){toast(error.message,true);}};
  $('#twitchScoutDisconnect').onclick=async()=>{try{await api('scout/twitch/disconnect',{});await refreshTwitchScout(true);$('#twitchScoutFrame').hidden=true;toast('Сессия Twitch удалена');}catch(error){toast(error.message,true);}};
  $('#twitchScoutBrowser').ontoggle=()=>twitchScoutFrame();
  $('#twitchScoutFrame').onclick=async event=>{const rect=event.target.getBoundingClientRect();try{await api('scout/twitch/input',{kind:'click',x:(event.clientX-rect.left)*1280/rect.width,y:(event.clientY-rect.top)*900/rect.height});await twitchScoutFrame();}catch(error){toast(error.message,true);}};
  $('#twitchScoutInput').onsubmit=async event=>{event.preventDefault();const input=event.target.elements.text,text=input.value;input.value='';try{await api('scout/twitch/input',{kind:'text',text});await twitchScoutFrame();}catch(error){toast(error.message,true);}};
  for(const key of ['Enter','Tab','Shift+Tab','Backspace','Control+A','Escape','ArrowDown','ArrowUp'])$('#twitchScoutKeys').append(button(key,async()=>{await api('scout/twitch/input',{kind:'key',key});await twitchScoutFrame();}));
  for(const [label,delta] of [['Прокрутить вверх',-500],['Прокрутить вниз',500]])$('#twitchScoutKeys').append(button(label,async()=>{await api('scout/twitch/input',{kind:'scroll',delta});await twitchScoutFrame();}));
  setInterval(()=>{if(!$('#scout').hidden&&!document.hidden){refreshTwitchScout().catch(()=>{});twitchScoutFrame();}},3000);
});

let twitchEmoteRows=[],twitchEmoteBusy=false,twitchEmoteTimer=null;
async function refreshTwitchEmotes(){
  if(twitchEmoteBusy)return;twitchEmoteBusy=true;
  try{
    const data=await api('twitch/emotes'),list=$('#twitchEmoteCatalog'),drafts=new Map();
    list.querySelectorAll('textarea').forEach(field=>{if(field.value!==field.dataset.original)drafts.set(field.dataset.id,field.value);});
    const selected=new Set([...list.querySelectorAll('input[type=checkbox]:checked')].map(x=>x.value));
    twitchEmoteRows=data.emotes||[];list.replaceChildren();
    for(const item of twitchEmoteRows){
      const card=node('div','','item'),head=node('label',item.code+' · '+item.provider+' · '+item.source),check=document.createElement('input');check.type='checkbox';check.value=item.id;check.checked=selected.has(String(item.id));head.prepend(check);card.append(head);
      if(item.mime||item.url){const image=document.createElement('img');image.src='/api/twitch/emotes/image?id='+item.id;image.width=96;image.height=96;image.loading='lazy';image.alt=item.code;image.style.objectFit='contain';card.append(image);}
      const field=document.createElement('textarea');field.value=drafts.get(String(item.id))??item.description;field.rows=3;field.maxLength=2000;field.dataset.id=item.id;field.dataset.original=item.description;card.append(field);
      card.append(node('p',(data.learning||[]).includes(item.id)?'Анализируется…':item.analyzed?'Описание сохранено':'Описание пока не задано','hint'));
      const actions=node('div','','buttonrow');
      actions.append(button('Изменить код или картинку',()=>{fillForm($('#twitchEmoteForm'),{...item,enabled:!!item.enabled});$('#twitchEmoteForm').scrollIntoView({block:'center'});}),
        button('Сохранить описание',async()=>{await api('twitch/emotes/save',{...item,description:field.value,enabled:!!item.enabled});field.dataset.original=field.value;await refreshTwitchEmotes();}),
        button('Анализировать',async()=>{await api('twitch/emotes/analyze',{ids:[item.id]});await refreshTwitchEmotes();}),
        button(item.enabled?'Отключить':'Включить',async()=>{await api('twitch/emotes/change',{id:item.id,enabled:!item.enabled});await refreshTwitchEmotes();}),
        button('Удалить',async()=>{await api('twitch/emotes/change',{id:item.id});await refreshTwitchEmotes();}));card.append(actions);list.append(card);
    }
    if(!twitchEmoteRows.length)list.append(node('p','Смайлы Twitch пока не добавлены. Импортируйте ExampleChannel или добавьте нужные коды вручную.','hint'));
    $('#twitchEmoteStatus').textContent=(data.import_status||'Смайлов в наборе: '+twitchEmoteRows.length)+(data.importing?' · импорт выполняется':'')+((data.learning||[]).length?' · анализируются: '+data.learning.length:'');
    clearTimeout(twitchEmoteTimer);if(data.importing||(data.learning||[]).length)twitchEmoteTimer=setTimeout(()=>{if(!$('#stickers').hidden)refreshTwitchEmotes().catch(()=>{});},3000);
  }finally{twitchEmoteBusy=false;}
}
document.addEventListener('DOMContentLoaded',()=>{
  $('#twitchEmoteForm').onsubmit=async event=>{event.preventDefault();const form=event.target,data=formData(form),file=form.elements.image.files[0];delete data.image;
    try{if(file){if(file.size>1000000)throw new Error('Картинка смайла: до 1 МБ');data.image_data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('Не удалось прочитать картинку'));reader.readAsDataURL(file);});}
      await api('twitch/emotes/save',data);form.reset();form.elements.id.value='';await refreshTwitchEmotes();toast('Смайл Twitch сохранён');
    }catch(error){toast(error.message,true);}
  };
  $('#twitchEmoteNew').onclick=()=>{const form=$('#twitchEmoteForm');form.reset();form.elements.id.value='';};
  $('#twitchEmoteImport').onsubmit=async event=>{event.preventDefault();try{await api('twitch/emotes/import',formData(event.target));await refreshTwitchEmotes();toast('Импорт и анализ смайлов запущены');}catch(error){toast(error.message,true);}};
  $('#twitchEmoteSelectAll').onclick=()=>$('#twitchEmoteCatalog').querySelectorAll('input[type=checkbox]').forEach(input=>input.checked=true);
  $('#twitchEmoteAnalyze').onclick=async()=>{const ids=[...$('#twitchEmoteCatalog').querySelectorAll('input[type=checkbox]:checked')].map(input=>Number(input.value));try{await api('twitch/emotes/analyze',{ids});await refreshTwitchEmotes();}catch(error){toast(error.message,true);}};
  $('#twitchEmoteRefresh').onclick=()=>refreshTwitchEmotes().catch(error=>toast(error.message,true));
});
