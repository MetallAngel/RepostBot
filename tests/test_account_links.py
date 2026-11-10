import unittest
from tests import test_reply_context as fixture
from repostbot.account_links import manual,reconcile

class AccountLinkTests(unittest.TestCase):
    setUp=fixture.ReplyContextTests.setUp
    tearDown=fixture.ReplyContextTests.tearDown
    def see(self,platform,uid,nick):self.worker.community.see_user(platform,uid,nick,nick)

    def test_separator_difference_links_existing_ids_and_combines_roles(self):
        self.see('telegram','17','SampleUser');self.see('twitch','27','sample_user_')
        self.assertEqual(set(self.worker.community._accounts('telegram','17')),{('telegram','17'),('twitch','27')})
        self.worker.social.save_permanent_role({'chat':'-10077','person':'17','role_key':'regular_member'})
        self.assertTrue(self.worker.participants.profile('twitch','27')['roles'])

    def test_ambiguous_normalized_names_are_not_merged_and_manual_choice_is_preserved(self):
        self.see('telegram','17','sample_name');self.see('twitch','27','samplename');self.see('telegram','18','samplename_')
        self.assertEqual(len(self.worker.community._accounts('telegram','17')),1)
        manual(self.worker.community,'27','18');reconcile(self.worker.community)
        self.assertEqual(set(self.worker.community._accounts('twitch','27')),{('twitch','27'),('telegram','18')})

    def test_unlink_blocks_automatic_reconnection_and_display_name_is_not_an_identity(self):
        self.see('telegram','17','other_login');self.see('twitch','27','otherlogin_')
        self.worker.community.unlink('27');reconcile(self.worker.community)
        self.assertEqual(len(self.worker.community._accounts('twitch','27')),1)
        self.worker.community.see_user('telegram','18','otherlogin','')
        self.assertEqual(len(self.worker.community._accounts('telegram','18')),1)
