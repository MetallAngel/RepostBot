"""Lightweight Silero voice styles, shared by the panel and CPU child."""
from html import escape


SPEAKERS = ('aidar', 'baya', 'kseniya', 'xenia', 'eugene')
PROSODY = ('x-low', 'low', 'medium', 'high', 'x-high')
RATES = ('x-slow', 'slow', 'medium', 'fast', 'x-fast')


def _preset(key, label, group, speaker, rate='medium', pitch='medium', description=''):
    return {'id': key, 'label': label, 'group': group, 'speaker': speaker,
            'rate': rate, 'pitch': pitch, 'description': description}


PRESETS = (
    _preset('xenia', 'Xenia · обычный голос', 'Женские · без изменения', 'xenia',
            description='Базовый женский голос Xenia, обычный темп и тон.'),
    _preset('baya', 'Baya · обычный голос', 'Женские · без изменения', 'baya',
            description='Базовый женский голос Baya, обычный темп и тон.'),
    _preset('kseniya', 'Kseniya · обычный голос', 'Женские · без изменения', 'kseniya',
            description='Базовый женский голос Kseniya, обычный темп и тон.'),
    _preset('fairy', 'Лесная фея', 'Женские · сказочные', 'xenia', pitch='high',
            description='Xenia с повышенным тоном и обычным темпом, для лёгких сказочных реплик.'),
    _preset('moon_fairy', 'Лунная фея', 'Женские · сказочные', 'xenia', 'slow', 'high',
            'Повышенный тон Xenia и неспешная речь, для сказки у костра.'),
    _preset('elf', 'Эльфийская сказительница', 'Женские · сказочные', 'kseniya', 'slow', 'high',
            'Kseniya говорит медленнее и выше обычного, для легенд и преданий.'),
    _preset('princess', 'Принцесса', 'Женские · сказочные', 'kseniya', pitch='high',
            description='Kseniya с повышенным тоном, без замедления речи.'),
    _preset('queen', 'Королева', 'Женские · сказочные', 'kseniya', pitch='low',
            description='Пониженный тон Kseniya и обычный темп, для торжественных обращений.'),
    _preset('witch', 'Ведьма из старой сказки', 'Женские · сказочные', 'baya', 'slow', 'low',
            'Baya с пониженным тоном и медленным темпом, для загадочных историй.'),
    _preset('forest_witch', 'Лесная ведьма', 'Женские · сказочные', 'baya', pitch='low',
            description='Пониженный тон Baya, обычный темп: короткие загадочные реплики.'),
    _preset('battle_witch', 'Боевая чародейка', 'Женские · сказочные', 'kseniya', 'fast', 'low',
            'Kseniya с пониженным тоном и быстрым темпом, для решительных ответов.'),
    _preset('story', 'Хранительница летописи', 'Женские · спокойные', 'baya', 'slow',
            description='Baya читает медленнее, сохраняя обычный тон. Для записей летописи.'),
    _preset('campfire', 'Рассказчица у костра', 'Женские · спокойные', 'xenia', 'slow',
            description='Xenia в медленном темпе, без изменения высоты голоса.'),
    _preset('oracle', 'Древняя прорицательница', 'Женские · спокойные', 'xenia', 'slow', 'low',
            'Неспешная Xenia с пониженным тоном, для пророчеств и мудрых советов.'),
    _preset('mischief', 'Озорная фея', 'Женские · оживлённые', 'xenia', 'fast', 'high',
            'Xenia с высоким тоном и быстрым темпом, для шуток и коротких реплик.'),
    _preset('bard', 'Бардесса', 'Женские · оживлённые', 'baya', 'fast',
            description='Baya в быстром темпе, для бодрых рассказов; это речь, а не пение.'),
    _preset('cheerful', 'Весёлая спутница', 'Женские · оживлённые', 'baya', pitch='high',
            description='Baya с повышенным тоном и обычным темпом.'),
    _preset('sprite', 'Искорка', 'Женские · оживлённые', 'baya', 'fast', 'high',
            'Быстрый темп и повышенный тон Baya, для коротких весёлых реплик.'),
    _preset('aidar', 'Aidar · обычный голос', 'Мужские', 'aidar',
            description='Базовый мужской голос Aidar, обычный темп и тон.'),
    _preset('eugene', 'Eugene · обычный голос', 'Мужские', 'eugene',
            description='Базовый мужской голос Eugene, обычный темп и тон.'),
    _preset('dwarf', 'Гном-летописец', 'Мужские', 'aidar', 'slow', 'low',
            'Aidar с пониженным тоном и медленным темпом, для суровой летописи.'),
    _preset('narrative_styler', 'Странствующий сказитель', 'Мужские', 'eugene', 'slow',
            description='Eugene в неспешном темпе, для длинных историй.'),
)
PRESET_MAP = {item['id']: item for item in PRESETS}
VOICE_KEYS = ('preset', 'speaker', 'rate', 'pitch')
RATE_LABELS = {'x-slow': 'очень медленный', 'slow': 'медленный', 'medium': 'обычный',
               'fast': 'быстрый', 'x-fast': 'очень быстрый'}
PITCH_LABELS = {'x-low': 'очень низкий', 'low': 'пониженный', 'medium': 'обычный',
                'high': 'повышенный', 'x-high': 'очень высокий'}


def voice_settings(data, previous=None):
    """Old speaker-only settings keep their voice; explicit presets own their values."""
    current = dict(previous or {'preset': 'xenia', 'speaker': 'xenia', 'rate': 'medium', 'pitch': 'medium'})
    if 'preset' in data:
        preset = data['preset']
        if not isinstance(preset, str) or preset not in (*PRESET_MAP, 'custom'):
            raise ValueError('Выберите голосовой пресет из списка.')
    elif any(key in data for key in ('speaker', 'rate', 'pitch')):
        # Earlier panels supplied only speaker. Do not let a saved style override it.
        preset = 'custom'
    else:
        preset = current.get('preset', 'custom')
    if preset != 'custom':
        return {key: preset if key == 'preset' else PRESET_MAP[preset][key] for key in VOICE_KEYS}
    current.update({key: data[key] for key in VOICE_KEYS if key in data})
    if current['speaker'] not in SPEAKERS:
        raise ValueError('Выберите один из базовых голосов Silero.')
    if current.get('rate', 'medium') not in RATES or current.get('pitch', 'medium') not in PROSODY:
        raise ValueError('Выберите темп и высоту тона из списка.')
    current.setdefault('rate', 'medium')
    current.setdefault('pitch', 'medium')
    current['preset'] = current['speaker'] if current['rate'] == current['pitch'] == 'medium' else 'custom'
    return {key: current[key] for key in VOICE_KEYS}


def tts_arguments(text, config):
    """SSML comes only from validated local controls, never from a user or LLM."""
    voice = voice_settings(config)
    args = {'speaker': voice['speaker'], 'sample_rate': 24000, 'put_accent': True, 'put_yo': True}
    if voice['rate'] == voice['pitch'] == 'medium':
        args['text'] = text
    else:
        args['ssml_text'] = (f'<speak><prosody rate="{voice["rate"]}" pitch="{voice["pitch"]}">'
                             + escape(str(text), quote=False) + '</prosody></speak>')
    return args


def voice_test_caption(config):
    voice = voice_settings(config)
    preset = PRESET_MAP.get(voice['preset'])
    label = preset['label'] if preset else 'Свой вариант'
    return (f'Тест озвучки: «{label}»\n'
            f'Голос: {voice["speaker"].capitalize()} · темп: {RATE_LABELS[voice["rate"]]} · тон: {PITCH_LABELS[voice["pitch"]]}.')
