import io
import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from repostbot.bot_profile_api import BotProfileAPI,ProfileError
from repostbot.glitch_avatar_assets import avatar,ASSETS


class BotProfileAPITests(unittest.TestCase):
    def setUp(self):self.api=BotProfileAPI('12345:private-token-for-test')

    def test_upload_uses_typed_static_photo_and_multipart_attachment(self):
        result=io.BytesIO(b'{"ok":true,"result":true}')
        with patch('repostbot.bot_profile_api.urlopen',return_value=result) as send:
            self.api.set_photo(avatar('casual_style'))
        request=send.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/setMyProfilePhoto'))
        self.assertIn('multipart/form-data',request.get_header('Content-type'))
        self.assertIn(b'"type": "static"',request.data)
        self.assertIn(b'"photo": "attach://avatar"',request.data)
        self.assertIn(b'name="avatar"; filename="assistant.jpg"',request.data)
        self.assertIn(b'Content-Type: image/jpeg',request.data)
        self.assertNotIn(b'chat_id',request.data)

    def test_empty_original_removes_only_own_profile_photo(self):
        with patch.object(self.api,'call',return_value=True) as call:
            self.api.set_photo(b'')
        call.assert_called_once_with('removeMyProfilePhoto')

    def test_rate_limit_is_preserved_without_exposing_token(self):
        error=HTTPError('private-url',429,'Too Many Requests',{},io.BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":37}}'))
        with patch('repostbot.bot_profile_api.urlopen',side_effect=error):
            with self.assertRaises(ProfileError) as caught:self.api.set_name('Assistant.exe')
        self.assertEqual(caught.exception.retry_after,37)
        self.assertNotIn('private-token',str(caught.exception))

    def test_snapshot_downloads_largest_own_photo_before_any_mutation(self):
        photos={'photos':[[{'file_id':'small','width':160,'height':160},{'file_id':'large','width':640,'height':640}]]}
        with patch.object(self.api,'call',side_effect=[{'name':'Assistant'},photos]) as call, \
             patch('repostbot.core.telegram_file_bytes',return_value=avatar('custom')) as download:
            result=self.api.snapshot('999')
        self.assertEqual(result['photo'],avatar('custom'))
        self.assertEqual(download.call_args.args[1]['file_id'],'large')
        self.assertEqual(call.call_args_list[1].args[1]['user_id'],999)
        self.assertEqual([item.args[0] for item in call.call_args_list],['getMyName','getUserProfilePhotos'])

    def test_generated_assets_are_static_square_telegram_jpegs(self):
        from PIL import Image
        self.assertEqual(len(ASSETS),8)
        for style in ASSETS:
            picture=Image.open(io.BytesIO(avatar(style)))
            self.assertEqual(picture.format,'JPEG')
            self.assertEqual(picture.size,(512,512))
