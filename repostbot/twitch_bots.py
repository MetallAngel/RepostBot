"""Explicit automation logins, never a heuristic based on someone's display name."""
import re

DEFAULTS=('nightbot','streamelements','streamlabs','moobot','fossabot','wizebot','botisimo',
          'pretzelrocks','commanderroot','soundalerts','blerp','sery_bot','stay_hydrated_bot',
          'ankhbot','deepbot','coebot','phantombot','supibot','logviewer','pokemoncommunitygame')

def logins(extra=''):
    values=extra if isinstance(extra,(list,tuple,set)) else re.split(r'[,;\s]+',str(extra))
    return set(DEFAULTS)|{str(v).lstrip('@').casefold() for v in values if re.fullmatch(r'@?[A-Za-z0-9_]{3,25}',str(v))}

def ignored(login,extra='',own=()):
    name=str(login or '').lstrip('@').casefold()
    return bool(name and name in (logins(extra)|{str(v).lstrip('@').casefold() for v in own if v}))
