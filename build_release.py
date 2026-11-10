"""Build only distributable application files; runtime data is never included."""
import hashlib
import json
from pathlib import Path
import zipfile
from repostbot.update import allowed_file
from repostbot import update
root=Path(__file__).resolve().parent
version=json.loads((root/'version.json').read_text())['version']
archive=root.parent/('RepostBot-public-'+version+'.zip')
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as output:
    for path in sorted(root.rglob('*')):
        if not path.is_file():continue
        name=path.relative_to(root).as_posix()
        if allowed_file(name):output.write(path,'RepostBot/'+name)
raw=archive.read_bytes()
current=update.CURRENT_VERSION
try:
    update.CURRENT_VERSION='0.0.0'
    assert update.inspect_package(raw)==version
finally:
    update.CURRENT_VERSION=current
with zipfile.ZipFile(archive) as package:assert package.testzip() is None
print(json.dumps({'archive':str(archive),'version':version,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}))
