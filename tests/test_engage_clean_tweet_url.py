"""
Tests for _clean_tweet_url in cogs/engage.py, the defensive sanitizer that
keeps a stored tweet_url with extra text in front of the URL from breaking the
Discord markdown link in the /engage list.

Run from the repo root with:
  python tests/test_engage_clean_tweet_url.py

Stdlib unittest only, no pytest dependency.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cogs.engage import _clean_tweet_url


class CleanTweetUrlTests(unittest.TestCase):

    def test_datetime_prefix_is_stripped_to_canonical(self):
        """The production symptom: a copied timestamp in front of the URL."""
        bad = ('Saturday, June 13, 2026 12:29 '
               'https://x.com/BAANIIzz/status/2065720328320434630')
        out = _clean_tweet_url(bad)
        self.assertEqual(
            out, 'https://x.com/baaniizz/status/2065720328320434630')
        # The whole point: the result has no spaces, so markdown is well formed.
        self.assertNotIn(' ', out)

    def test_clean_url_is_untouched(self):
        """A well formed single token URL keeps its original casing."""
        good = 'https://x.com/Tom_Degen68/status/2065802002937119003'
        self.assertEqual(_clean_tweet_url(good), good)

    def test_clean_lowercase_url_is_untouched(self):
        good = 'https://x.com/0xaghdd/status/2065710406195200116'
        self.assertEqual(_clean_tweet_url(good), good)

    def test_trailing_junk_after_url_is_rebuilt(self):
        bad = 'https://x.com/someone/status/123456789 via the app'
        self.assertEqual(
            _clean_tweet_url(bad), 'https://x.com/someone/status/123456789')

    def test_surrounding_whitespace_only_is_stripped(self):
        good = '  https://x.com/user/status/987654321  '
        self.assertEqual(
            _clean_tweet_url(good), 'https://x.com/user/status/987654321')

    def test_unparseable_value_falls_back_to_first_url_token(self):
        bad = 'check this out https://example.com/page and more text'
        self.assertEqual(_clean_tweet_url(bad), 'https://example.com/page')

    def test_no_url_returns_stripped_raw(self):
        self.assertEqual(_clean_tweet_url('  just text  '), 'just text')

    def test_none_returns_empty_string(self):
        self.assertEqual(_clean_tweet_url(None), '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
