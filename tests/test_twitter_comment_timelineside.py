"""
Tests for the timelineside only comment verify chain in cogs/_twitter.py.

Run from the repo root with:
  python tests/test_twitter_comment_timelineside.py

Stdlib unittest only, no pytest dependency. All network touching functions in
the module are replaced with fakes in setUp; any path that would reach the real
TwitterAPI.io raises immediately so a test can never spend money or depend on
the network.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cogs import _twitter

TARGET_TWEET_ID   = '111000111'
TARGET_AUTHOR_ID  = '999000999'
EXPECTED_USER_ID  = '555000555'
EXPECTED_USERNAME = 'replyguy'

# createdAt values in the Twitter string format _parse_tweet_time understands.
TARGET_CREATED = 'Tue Jun 09 12:00:00 +0000 2026'
AFTER_TARGET   = 'Wed Jun 10 12:00:00 +0000 2026'
BEFORE_TARGET  = 'Mon Jun 08 12:00:00 +0000 2026'


def make_target_tweet():
    return {
        'id': TARGET_TWEET_ID,
        'author': {'id': TARGET_AUTHOR_ID, 'userName': 'projectacct'},
        'createdAt': TARGET_CREATED,
    }


def make_reply_tweet(tweet_id='777000777'):
    """A tweet by the expected user that replies to the target tweet."""
    return {
        'id': tweet_id,
        'author': {'id': EXPECTED_USER_ID, 'userName': EXPECTED_USERNAME},
        'inReplyToId': TARGET_TWEET_ID,
        'createdAt': AFTER_TARGET,
    }


def make_unrelated_tweet(tweet_id, created=AFTER_TARGET):
    """A tweet by the expected user that does NOT reply to the target."""
    return {
        'id': tweet_id,
        'author': {'id': EXPECTED_USER_ID, 'userName': EXPECTED_USERNAME},
        'createdAt': created,
    }


class TimelinesideOnlyChainTests(unittest.IsolatedAsyncioTestCase):
    """check_comment with TWITTER_USE_ONLY_TIMELINESIDE=true plus the
    check_comment_timelineside scanner it delegates to."""

    PATCHED = (
        'TWITTER_USE_ONLY_TIMELINESIDE',
        '_api_get',
        'fetch_user_tweet_timeline',
        'fetch_tweets_by_ids',
        'lookup_twitter_user_by_login',
        '_probe_replies_endpoint',
        '_fetch_user_recent_tweets',
        '_check_comment_tweetside',
        '_check_comment_conversation',
        'check_comment_mentionside',
    )

    def setUp(self):
        self._originals = {n: getattr(_twitter, n) for n in self.PATCHED}
        _twitter.TWITTER_USE_ONLY_TIMELINESIDE = True
        self.legacy_calls = []

        async def no_real_api(*args, **kwargs):
            raise AssertionError('unexpected real API call')

        def record_legacy(name):
            async def stub(*args, **kwargs):
                self.legacy_calls.append(name)
                return {'verified': False}
            return stub

        _twitter._api_get = no_real_api
        _twitter.fetch_user_tweet_timeline = no_real_api
        _twitter.fetch_tweets_by_ids = no_real_api
        _twitter.lookup_twitter_user_by_login = no_real_api
        _twitter._probe_replies_endpoint = record_legacy('probe_replies_endpoint')
        _twitter._fetch_user_recent_tweets = record_legacy('fetch_user_recent_tweets')
        _twitter._check_comment_tweetside = record_legacy('check_comment_tweetside')
        _twitter._check_comment_conversation = record_legacy('check_comment_conversation')
        _twitter.check_comment_mentionside = record_legacy('check_comment_mentionside')

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(_twitter, name, value)

    async def test_match_on_page_one(self):
        """The common case: the reply is on page 1 of the user's timeline."""
        pages_fetched = []

        async def fake_timeline(user_id, include_replies=True,
                                include_parent_tweet=False, cursor=None):
            pages_fetched.append(cursor)
            return {
                'tweets': [make_unrelated_tweet('700'), make_reply_tweet()],
                'has_next_page': True,
                'next_cursor': 'cursor_p2',
            }

        async def fake_by_ids(ids):
            return [make_reply_tweet()]

        _twitter.fetch_user_tweet_timeline = fake_timeline
        _twitter.fetch_tweets_by_ids = fake_by_ids

        res = await _twitter.check_comment_timelineside(
            make_target_tweet(), EXPECTED_USER_ID, EXPECTED_USERNAME)

        self.assertIs(res['verified'], True)
        self.assertEqual(res['method'], 'timelineside')
        self.assertEqual(res['matched_tweet_id'], '777000777')
        self.assertEqual(len(pages_fetched), 1)

    async def test_no_match_exhausts_timeline_and_skips_legacy_paths(self):
        """Pagination runs until has_next_page=false with no match. The
        verdict is no_match and no legacy comment path is ever called."""
        pages_fetched = []
        total_pages = 3

        async def fake_timeline(user_id, include_replies=True,
                                include_parent_tweet=False, cursor=None):
            pages_fetched.append(cursor)
            idx = len(pages_fetched)
            last = idx >= total_pages
            return {
                'tweets': [make_unrelated_tweet(str(800 + idx))],
                'has_next_page': not last,
                'next_cursor': None if last else f'cursor_p{idx + 1}',
            }

        async def fake_by_ids(ids):
            # _resolve_idside_context fetching the target tweet by id.
            self.assertEqual(list(ids), [TARGET_TWEET_ID])
            return [make_target_tweet()]

        async def fake_lookup(username):
            return {'id': EXPECTED_USER_ID, 'username': EXPECTED_USERNAME,
                    'display_name': '', 'followers_count': 0,
                    'profile_image_url': ''}

        _twitter.fetch_user_tweet_timeline = fake_timeline
        _twitter.fetch_tweets_by_ids = fake_by_ids
        _twitter.lookup_twitter_user_by_login = fake_lookup

        res = await _twitter.check_comment(TARGET_TWEET_ID, EXPECTED_USERNAME)

        self.assertIs(res['verified'], False)
        self.assertEqual(res['reason'], 'no_comment_timelineside')
        self.assertEqual(pages_fetched, [None, 'cursor_p2', 'cursor_p3'])
        self.assertEqual(self.legacy_calls, [])

    async def test_early_stop_when_page_predates_target(self):
        """Once a page's oldest createdAt predates the target tweet, the scan
        stops even though the API still advertises more pages."""
        pages_fetched = []

        async def fake_timeline(user_id, include_replies=True,
                                include_parent_tweet=False, cursor=None):
            pages_fetched.append(cursor)
            idx = len(pages_fetched)
            created = AFTER_TARGET if idx == 1 else BEFORE_TARGET
            return {
                'tweets': [make_unrelated_tweet(str(900 + idx), created=created)],
                'has_next_page': True,
                'next_cursor': f'cursor_p{idx + 1}',
            }

        _twitter.fetch_user_tweet_timeline = fake_timeline

        res = await _twitter.check_comment_timelineside(
            make_target_tweet(), EXPECTED_USER_ID, EXPECTED_USERNAME)

        self.assertIs(res['verified'], False)
        self.assertEqual(res['pages'], 2)
        self.assertLess(len(pages_fetched), _twitter._TIMELINE_MAX_PAGES)
        self.assertEqual(self.legacy_calls, [])

    async def test_unresolved_context_is_inconclusive(self):
        """When the target tweet fetch and the user id lookup both fail, the
        verdict is inconclusive (None), never a conclusive no."""
        async def fake_by_ids(ids):
            return []

        async def fake_lookup(username):
            return None

        _twitter.fetch_tweets_by_ids = fake_by_ids
        _twitter.lookup_twitter_user_by_login = fake_lookup

        res = await _twitter.check_comment(TARGET_TWEET_ID, EXPECTED_USERNAME)

        self.assertIsNone(res['verified'])
        self.assertEqual(res['reason'], 'timelineside_context_unresolved')
        self.assertEqual(self.legacy_calls, [])

    async def test_flag_false_reenables_legacy_chain(self):
        """The rollback path: with TWITTER_USE_ONLY_TIMELINESIDE=false the
        legacy probe and tweetside paths run again."""
        _twitter.TWITTER_USE_ONLY_TIMELINESIDE = False

        async def fake_probe(test_username):
            self.legacy_calls.append('probe_replies_endpoint')
            return None  # no replies capable endpoint found

        async def fake_tweetside(tweet_id, target):
            self.legacy_calls.append('check_comment_tweetside')
            return {'verified': False, 'reason': 'no_comment_found',
                    'seen': 0, 'pages': 1, 'api_calls': 1}

        _twitter._probe_replies_endpoint = fake_probe
        _twitter._check_comment_tweetside = fake_tweetside

        res = await _twitter.check_comment(TARGET_TWEET_ID, EXPECTED_USERNAME)

        self.assertIs(res['verified'], False)
        self.assertIn('probe_replies_endpoint', self.legacy_calls)
        self.assertIn('check_comment_tweetside', self.legacy_calls)


if __name__ == '__main__':
    unittest.main(verbosity=2)
