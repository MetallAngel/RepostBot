"""Optional community scene extension. No built-in characters or scenarios."""

class RoleScenes:

    def __init__(self, social):
        self.social, self.store = (social, social.store)
        with self.store.lock, self.store.db:
            self.store.db.execute('CREATE TABLE IF NOT EXISTS social_role_scenes (\n                chat TEXT NOT NULL, person TEXT NOT NULL, role_key TEXT NOT NULL,\n                status TEXT NOT NULL, task_key TEXT NOT NULL, task TEXT NOT NULL,\n                notice TEXT NOT NULL, at INTEGER NOT NULL, until_at INTEGER NOT NULL,\n                announced INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,\n                PRIMARY KEY(chat,person))')
            for column in ('location', 'escape_text'):
                if column not in {row[1] for row in self.store.db.execute('PRAGMA table_info(social_role_scenes)')}:
                    self.store.db.execute('ALTER TABLE social_role_scenes ADD COLUMN ' + column + " TEXT NOT NULL DEFAULT ''")
            self.store.db.execute("CREATE TABLE IF NOT EXISTS social_scene_effects (\n                chat TEXT NOT NULL,person TEXT NOT NULL,revision INTEGER NOT NULL,episode TEXT NOT NULL,\n                status TEXT NOT NULL DEFAULT 'pending',PRIMARY KEY(chat,person,revision))")

    def context(self, *args, **kwargs):
        return ''

    def notice(self, *args, **kwargs):
        return ('', 0)

    def effect(self, *args, **kwargs):
        return None

    def after_reply(self, *args, **kwargs):
        return None

    def commit(self, *args, **kwargs):
        return None

    def current_role_key(self, *args, **kwargs):
        return ''

    def media_instruction(self, *args, **kwargs):
        return ''

    def media_result(self, chat, person, text, allowed=False):
        return text

    def chat_context(self, *args, **kwargs):
        return ''
