"""
Twitter data via TwitterAPI.io — paid-per-call, no plan restrictions.

Env var:
  TWITTER_API_IO_KEY — your TwitterAPI.io API key

Functions return:
  {'verified': True|False|None, 'reason': str}
None = inconclusive (network error, auth failure, etc.) — caller must NOT flag.
False = conclusively absent (API returned data but task was not done).
"""
import os
import re
import time
import asyncio
import collections
import httpx
from typing import Optional

API_KEY  = (os.getenv('TWITTER_API_IO_KEY') or '').strip()
API_BASE = 'https://api.twitterapi.io'

# ── Global budget guard (TwitterAPI.io is paid per call) ─────────────────────
# A single choke point for ALL outbound Twitter calls regardless of which
# feature triggered them (raid verify, engage verify, manual checks, lookups).
# Caps the per-minute and per-day call rate so abuse/spam cannot run up the
# bill. When the cap is hit we return None (inconclusive) so callers degrade
# gracefully — no crash, no wrongly-awarded points; the user can retry later.
_MAX_CALLS_PER_MIN = max(1, int(os.getenv('TWITTER_MAX_CALLS_PER_MIN', '120') or 120))
_MAX_CALLS_PER_DAY = max(1, int(os.getenv('TWITTER_MAX_CALLS_PER_DAY', '20000') or 20000))
_call_times: collections.deque = collections.deque()  # monotonic ts within last 60s
_day_count   = 0
_day_start   = 0.0
_budget_lock = asyncio.Lock()


async def _budget_allows() -> bool:
    """Return True if another outbound Twitter call is within budget."""
    global _day_count, _day_start
    async with _budget_lock:
        now = time.monotonic()
        if _day_start == 0.0 or (now - _day_start) >= 86400:
            _day_start, _day_count = now, 0
        if _day_count >= _MAX_CALLS_PER_DAY:
            print('[twitter] budget guard: DAILY cap reached')
            return False
        cutoff = now - 60.0
        while _call_times and _call_times[0] < cutoff:
            _call_times.popleft()
        if len(_call_times) >= _MAX_CALLS_PER_MIN:
            print('[twitter] budget guard: per-minute cap reached')
            return False
        _call_times.append(now)
        _day_count += 1
        return True


# Minimum spacing between consecutive outbound Twitter calls. Spreads deep
# pagination out so it cannot spike the per-minute rate or the bill. Default
# 0.25s; override with TWITTER_API_MIN_GAP (seconds, 0 disables).
_MIN_GAP      = max(0.0, float(os.getenv('TWITTER_API_MIN_GAP', '0.25') or 0.25))
_last_call_ts = 0.0
_gap_lock     = asyncio.Lock()


async def _respect_min_gap() -> None:
    """Sleep just enough to keep at least _MIN_GAP seconds between calls."""
    global _last_call_ts
    if _MIN_GAP <= 0:
        return
    async with _gap_lock:
        wait = _MIN_GAP - (time.monotonic() - _last_call_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_ts = time.monotonic()


SCRAPING_HEALTHY      = True
_consecutive_failures = 0
_FAILURE_THRESHOLD    = 3
_health_lock          = asyncio.Lock()

if API_KEY:
    print(f'[twitter] TwitterAPI.io configured (key length={len(API_KEY)})')
else:
    print('[twitter] TWITTER_API_IO_KEY not set — verification disabled')


# ── Pagination depth + wall-clock budget ────────────────────────────────────
# Popular tweets need many pages of replies/retweeters. A shallow page cap OR a
# too-tight time budget silently truncates the scan and causes verification
# false negatives (a real commenter further down the list is missed). The page
# caps default to a REAL 20 (not 5). The wall-clock budget defaults to 120s:
# 20 pages × real API latency (several seconds each, plus the min-gap spacing)
# easily exceeds the old 25s budget, which was the actual reason pagination
# stopped at ~5 pages despite the 20-page cap. All three are env-overridable and
# are printed at load so the ACTIVE values are visible in Railway even when an
# env var overrides a default.
_REPLIES_MAX_PAGES      = max(1, int(os.getenv('TWITTER_REPLIES_MAX_PAGES',     '20') or 20))
_RETWEETERS_MAX_PAGES   = max(1, int(os.getenv('TWITTER_RETWEETERS_MAX_PAGES', '20') or 20))
_USER_TWEETS_MAX_PAGES  = max(1, int(os.getenv('TWITTER_USER_TWEETS_MAX_PAGES', '12') or 12))
# The retweet user-side path uses a low cap because a recent retweet is at the
# top of the user's timeline; scanning deep is wasted spend. Default 3 pages
# (~60 tweets), enough for any recent retweet. Comment fallback (if a working
# replies endpoint is discovered) uses the full _USER_TWEETS_MAX_PAGES cap.
_USER_TWEETS_RETWEET_MAX_PAGES = max(
    1, int(os.getenv('TWITTER_USER_TWEETS_RETWEET_MAX_PAGES', '3') or 3)
)
_PAGINATION_BUDGET_S    = max(5.0, float(os.getenv('TWITTER_PAGINATION_BUDGET_S', '120') or 120))
# Minimum replies tweet-side must have scanned before we trust its "no match"
# as conclusive (and skip the user-side fallback). When tweet-side returns
# 0-few replies, that's the "broken endpoint" signature and we fall back.
_TWEETSIDE_CONFIDENT_FALSE_MIN = max(
    1, int(os.getenv('TWITTER_TWEETSIDE_CONFIDENT_FALSE_MIN', '5') or 5)
)
# Short-TTL process-level cache for a user's recent tweets. One engage finalize
# verifies many submissions for the same user — we'd hit /user/last_tweets
# once per submission without this. 60s is plenty for one finalize batch and
# short enough that brand-new replies show up before the next attempt.
_USER_TWEETS_CACHE_TTL  = max(1.0, float(os.getenv('TWITTER_USER_TWEETS_CACHE_TTL', '60') or 60))
# After scanning at least this many user tweets with no match, treat a
# tweet-side inconclusive (None) as a soft "no" rather than a full inconclusive.
# Keeps False-negatives during intermittent /tweet/replies outages.
_USERSIDE_CONFIDENT_FALSE_MIN = max(
    1, int(os.getenv('TWITTER_USERSIDE_CONFIDENT_FALSE_MIN', '20') or 20)
)

print(f'[twitter] replies cap={_REPLIES_MAX_PAGES} '
      f'retweeters cap={_RETWEETERS_MAX_PAGES} '
      f'user_tweets cap={_USER_TWEETS_MAX_PAGES} '
      f'rt_userside cap={_USER_TWEETS_RETWEET_MAX_PAGES} '
      f'cache_ttl={_USER_TWEETS_CACHE_TTL}s '
      f'time budget={_PAGINATION_BUDGET_S}s '
      f'tweetside_confident_min={_TWEETSIDE_CONFIDENT_FALSE_MIN}')


# ── User-tweets cache (single fetch reused across one finalize batch) ────────
# Keyed by normalized username. Value is (epoch_monotonic, list_of_tweet_dicts).
# Read+write inside an asyncio.Lock so concurrent verifies share the same
# in-flight result and a slow API call isn't billed twice.
_user_tweets_cache: dict = {}
_user_tweets_lock          = asyncio.Lock()
_logged_response_shapes: set = set()   # one-shot keys for "raw shape" diagnostics


async def _record_health(success: bool) -> None:
    global SCRAPING_HEALTHY, _consecutive_failures
    async with _health_lock:
        if success:
            if _consecutive_failures > 0 or not SCRAPING_HEALTHY:
                print(f'[twitter] health restored after {_consecutive_failures} failures')
            _consecutive_failures = 0
            SCRAPING_HEALTHY      = True
        else:
            _consecutive_failures += 1
            print(f'[twitter] health: API failure #{_consecutive_failures}')
            if _consecutive_failures >= _FAILURE_THRESHOLD and SCRAPING_HEALTHY:
                SCRAPING_HEALTHY = False
                print(f'[twitter] SCRAPING_HEALTHY=False — {_consecutive_failures} consecutive failures')


def get_scraping_health() -> dict:
    return {'healthy': SCRAPING_HEALTHY, 'consecutive_failures': _consecutive_failures}


def normalize_username(name: str) -> str:
    return (name or '').strip().lstrip('@').strip().lower()


# Every handle-bearing field the TwitterAPI.io reply/retweeter objects may use.
# The author can live directly on the object OR inside a nested container, and
# different endpoints/versions name the handle differently. Missing any one of
# these variants means a real comment/retweet is skipped — a false negative.
_HANDLE_FIELDS  = ('userName', 'username', 'screen_name', 'screenName', 'handle')
_NESTED_USER_KEYS = ('author', 'user', 'core', 'legacy', 'user_results',
                     'result', 'userInfo', 'user_info', 'tweet')


def _candidate_handles(obj, _depth: int = 0) -> list:
    """Collect every plausible handle string from a reply/retweeter object,
    including common nested user containers. Bounded depth guards against
    unexpectedly deep payloads."""
    out: list = []
    if not isinstance(obj, dict) or _depth > 4:
        return out
    for f in _HANDLE_FIELDS:
        v = obj.get(f)
        if isinstance(v, str) and v.strip():
            out.append(v)
    for key in _NESTED_USER_KEYS:
        nested = obj.get(key)
        if isinstance(nested, dict):
            out.extend(_candidate_handles(nested, _depth + 1))
    return out


def _matches_target(obj, target: str) -> bool:
    """True if any handle field on the object (or a nested user) equals target
    after normalization (case-insensitive, @/whitespace trimmed on both sides)."""
    return any(normalize_username(h) == target for h in _candidate_handles(obj))


def _primary_handle(obj) -> str:
    """The first normalized handle found on the object — for diagnostics only."""
    cands = _candidate_handles(obj)
    return normalize_username(cands[0]) if cands else ''


# ── Pagination signal parsing ────────────────────────────────────────────────
# TwitterAPI.io's pagination shape varies by endpoint and we have seen issues
# upstream where `has_next_page` arrives as a STRING ("true"/"false") rather
# than a bool — strict `is False` checks miss that. And `next_cursor` can be
# absent or empty, in which case a naive `data.get('next_cursor') or
# data.get('cursor')` chain falls back to the response's `cursor` field, which
# many APIs ECHO back the same value we sent. Sending the same cursor again
# refetches the same page, producing the duplicate handles we observed.

_CURSOR_KEYS_PRIORITY = (
    'next_cursor', 'nextCursor',
    'pagination_token', 'paginationToken',
    'next_token',       'nextToken',
)


def _parse_has_next(value) -> Optional[bool]:
    """Coerce a has_next_page value to True / False / None (unknown).
    Accepts bool, int, and common string forms; anything else → None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ('true',  '1', 'yes', 'on'):
            return True
        if s in ('false', '0', 'no',  'off', ''):
            return False
    return None


def _read_next_cursor(data: dict, sent_cursor: Optional[str]) -> tuple[Optional[str], str]:
    """Return (next_cursor, source_field) from a paginated response.

    Tries known explicit next-page fields first. Only consults a bare `cursor`
    field as a last resort AND only if it differs from what we sent — many
    APIs echo the input cursor under `cursor`, which would silently make us
    refetch the same page indefinitely."""
    if not isinstance(data, dict):
        return None, ''
    for key in _CURSOR_KEYS_PRIORITY:
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip(), key
    v = data.get('cursor')
    if isinstance(v, str) and v.strip():
        if v.strip() != (sent_cursor or ''):
            return v.strip(), 'cursor'
        return None, 'cursor(echo_ignored)'
    return None, ''


def _cursor_preview(c: Optional[str]) -> str:
    """Short, safe-to-log cursor string: '(none)' or '<first 12>…(len=N)'."""
    if not c:
        return '(none)'
    return f'{c[:12]}…(len={len(c)})'


# ── In-reply-to / retweet-of field detection ─────────────────────────────────
# TwitterAPI.io's user-tweets response uses several field names depending on
# endpoint version. We probe every known variant; the first scan also logs the
# observed shape ONCE so we can spot a new variant in production logs.

_REPLY_TO_ID_FIELDS = (
    'inReplyToId', 'in_reply_to_status_id_str', 'in_reply_to_status_id',
    'inReplyToStatusId', 'in_reply_to_tweet_id', 'replyToId',
    'inReplyTo', 'reply_to_id',
    # TwitterAPI.io variants observed in user_tweets payloads:
    'replied_to_status_id', 'repliedToStatusId', 'replied_to_status_id_str',
)
# Conversation-id fields. We use these ONLY as a last-resort signal — a
# conversation_id matching the target tweet means the tweet is in the target's
# thread, which covers most reply scenarios but can also match the root tweet
# itself. We guard against false positives below (own-id != target + a reply
# hint).
_CONVERSATION_ID_FIELDS = (
    'conversation_id', 'conversationId', 'conversationID',
)
# Boolean-ish "this is a reply" flags. Used together with conversation_id to
# distinguish a real reply from the root tweet of the conversation.
_REPLY_HINT_FIELDS = (
    'isReply', 'is_reply',
    'inReplyToUserId', 'in_reply_to_user_id', 'in_reply_to_user_id_str',
    'in_reply_to_screen_name', 'inReplyToScreenName',
)
_RETWEET_REF_ID_FIELDS = (
    'retweetedStatusId', 'retweeted_status_id', 'retweeted_status_id_str',
    'retweetedTweetId', 'retweeted_tweet_id',
)
_RETWEET_NESTED_KEYS = (
    'retweeted_status', 'retweeted_tweet', 'retweetedStatus', 'retweetedTweet',
)


def _id_str(v) -> str:
    """Coerce a tweet id (int or string) to a clean string for comparison."""
    if v is None:
        return ''
    return str(v).strip()


def _tweet_replies_to(tw: dict, target_id: str) -> bool:
    """True if `tw` is a reply whose in-reply-to id equals target_id.

    Priority order:
      1. Any of the known direct in-reply-to id fields (snake_case + camelCase).
      2. The Twitter-v2 `referenced_tweets` array (type='replied_to').
      3. conversation_id == target_id AS LAST RESORT, gated by the tweet being
         not the root of that conversation AND carrying a reply hint (so we
         don't false-positive on the original target tweet itself).
    """
    if not isinstance(tw, dict) or not target_id:
        return False

    # 1. Direct in-reply-to fields.
    for f in _REPLY_TO_ID_FIELDS:
        if _id_str(tw.get(f)) == target_id:
            return True

    # 2. Twitter-v2 referenced_tweets array.
    refs = tw.get('referenced_tweets')
    if isinstance(refs, list):
        for r in refs:
            if isinstance(r, dict) and str(r.get('type', '')) == 'replied_to':
                if _id_str(r.get('id')) == target_id:
                    return True

    # 3. conversation_id last-resort. Only counts when:
    #    - the tweet's own id isn't the target (don't match the root tweet)
    #    - at least one reply-hint field exists (rules out a top-level tweet
    #      that happens to be the conversation root)
    own_id = _id_str(tw.get('id') or tw.get('id_str'))
    if own_id == target_id:
        return False
    for cf in _CONVERSATION_ID_FIELDS:
        if _id_str(tw.get(cf)) == target_id:
            if any(tw.get(h) for h in _REPLY_HINT_FIELDS):
                return True
            break  # conversation matched but no reply-hint → not enough to claim
    return False


def _tweet_retweets(tw: dict, target_id: str) -> bool:
    """True if `tw` is a retweet whose source tweet id equals target_id.

    Quote-tweets are intentionally NOT counted — that's a different action."""
    if not isinstance(tw, dict) or not target_id:
        return False
    for f in _RETWEET_REF_ID_FIELDS:
        if _id_str(tw.get(f)) == target_id:
            return True
    for key in _RETWEET_NESTED_KEYS:
        nested = tw.get(key)
        if isinstance(nested, dict):
            if (_id_str(nested.get('id'))     == target_id
                    or _id_str(nested.get('id_str')) == target_id):
                return True
    refs = tw.get('referenced_tweets')
    if isinstance(refs, list):
        for r in refs:
            if isinstance(r, dict) and str(r.get('type', '')) == 'retweeted':
                if _id_str(r.get('id')) == target_id:
                    return True
    return False


def _log_shape_once(tag: str, data: dict, sample: Optional[dict]) -> None:
    """First time we see a given response shape, log keys for one tweet
    object. Subsequent identical shapes are silent. Helps us notice if
    TwitterAPI.io adds/renames fields without spamming the log."""
    try:
        top_keys    = sorted(data.keys()) if isinstance(data, dict) else []
        sample_keys = sorted(sample.keys())[:30] if isinstance(sample, dict) else []
    except Exception:
        return
    shape_key = f'{tag}|top={",".join(top_keys)}|sample={",".join(sample_keys)}'
    if shape_key in _logged_response_shapes:
        return
    _logged_response_shapes.add(shape_key)
    print(f'[twitter] {tag} shape: top_keys={top_keys}')
    if sample_keys:
        print(f'[twitter] {tag} sample tweet keys: {sample_keys}')


# ── user_tweets payload shape & tweet extraction ────────────────────────────
# TwitterAPI.io's /twitter/user/last_tweets wraps the payload in a
# {code, msg, status, data, has_next_page, next_cursor} envelope. The
# `data` field itself can be either a list of tweet dicts OR a dict whose
# `tweets` key holds the list (and sometimes `pin_tweet` next to it). The
# previous parser short-circuited with an `or` truthiness chain that fell
# through when `data` was an empty list AND when `data['tweets']` was empty
# next to a non-empty `pin_tweet`, giving got=0 even though the cursor said
# more pages exist. This helper tries every documented path in priority
# order and returns the path string so we can confirm in logs which shape
# this endpoint version actually uses.

def _extract_tweets_from_payload(data) -> tuple[list, str]:
    """Return (tweet_list, path_used) for a /user/last_tweets response.

    path_used is one of: 'data[]', 'data.tweets', 'data.data[]',
    'data.items', 'data.results', 'tweets', 'items', 'results', 'none',
    'not-a-dict'. Logged on every page so we can confirm field shape."""
    if not isinstance(data, dict):
        return [], 'not-a-dict'

    d = data.get('data')

    # 1. data is itself a list of tweets.
    if isinstance(d, list):
        return d, 'data[]'

    # 2. data is a dict wrapping the tweets list. Probe known carrier keys.
    if isinstance(d, dict):
        for sub in ('tweets', 'data', 'items', 'results', 'statuses'):
            v = d.get(sub)
            if isinstance(v, list):
                return v, f'data.{sub}{"[]" if sub == "data" else ""}'

    # 3. Top-level carrier (older / alternate shapes).
    for k in ('tweets', 'items', 'results', 'statuses'):
        v = data.get(k)
        if isinstance(v, list):
            return v, k

    return [], 'none'


def _log_user_tweets_data_shape_once(data) -> None:
    """ONCE per process, log the deep structure of the user_tweets `data`
    field so the path our extractor uses is verifiable against production
    payloads. Subsequent calls are silent."""
    key = 'user_tweets:data-shape'
    if key in _logged_response_shapes:
        return
    _logged_response_shapes.add(key)
    if not isinstance(data, dict):
        print(f'[twitter] user_tweets data: top-level type={type(data).__name__}')
        return
    d = data.get('data')
    if isinstance(d, list):
        first = d[0] if d else None
        first_keys = sorted(first.keys())[:30] if isinstance(first, dict) else None
        print(f'[twitter] user_tweets data: list len={len(d)} '
              f'first_tweet_keys={first_keys}')
        return
    if isinstance(d, dict):
        sub_keys = sorted(d.keys())
        print(f'[twitter] user_tweets data: dict keys={sub_keys}')
        for sk in ('tweets', 'data', 'items', 'results', 'statuses'):
            sv = d.get(sk)
            if isinstance(sv, list):
                first = sv[0] if sv else None
                first_keys = sorted(first.keys())[:30] if isinstance(first, dict) else None
                print(f'[twitter] user_tweets data.{sk}: list len={len(sv)} '
                      f'first_tweet_keys={first_keys}')
                break
        return
    print(f'[twitter] user_tweets data: type={type(d).__name__} '
          f'preview={str(d)[:200]}')


def _log_first_tweet_keys_once(tw) -> None:
    """ONCE per process, log the keys of the first observed user tweet
    object and any reply / conversation / retweet related fields with their
    values. Makes new field-name variants instantly visible in Railway."""
    key = 'user_tweets:first-tweet-keys'
    if key in _logged_response_shapes:
        return
    if not isinstance(tw, dict):
        return
    _logged_response_shapes.add(key)
    print(f'[twitter] user_tweets first tweet keys: {sorted(tw.keys())[:40]}')
    reply_like = {
        k: tw[k] for k in tw.keys()
        if isinstance(k, str) and (
            'reply' in k.lower() or 'conversation' in k.lower() or 'retweet' in k.lower()
        )
    }
    if reply_like:
        # Trim very long string values so the log line stays readable.
        compact = {
            k: (v if not isinstance(v, str) or len(v) <= 80 else v[:77] + '…')
            for k, v in reply_like.items()
        }
        print(f'[twitter] user_tweets first tweet reply/retweet fields: {compact}')


# ── "Is this a reply?" / "Is this a retweet?" tests for diagnostics ─────────
# Used to count replies_in_timeline / retweets_in_timeline so we can SEE,
# directly in the log line, whether the endpoint in use actually returns
# user replies. Production proved /user/last_tweets does NOT — every scanned
# tweet shows isReply=False, inReplyToId=None — which is why the user-side
# comment scan was a long wasted spend.

def _is_reply_tweet(tw: dict) -> bool:
    if not isinstance(tw, dict):
        return False
    if tw.get('isReply') is True or tw.get('is_reply') is True:
        return True
    for f in _REPLY_TO_ID_FIELDS:
        v = tw.get(f)
        if v not in (None, '', 0, '0'):
            return True
    refs = tw.get('referenced_tweets')
    if isinstance(refs, list):
        for r in refs:
            if isinstance(r, dict) and str(r.get('type', '')) == 'replied_to':
                return True
    return False


def _is_retweet_tweet(tw: dict) -> bool:
    if not isinstance(tw, dict):
        return False
    if tw.get('isRetweet') is True or tw.get('is_retweet') is True:
        return True
    for f in _RETWEET_REF_ID_FIELDS:
        v = tw.get(f)
        if v not in (None, '', 0, '0'):
            return True
    for key in _RETWEET_NESTED_KEYS:
        nested = tw.get(key)
        if isinstance(nested, dict) and nested:
            return True
    refs = tw.get('referenced_tweets')
    if isinstance(refs, list):
        for r in refs:
            if isinstance(r, dict) and str(r.get('type', '')) == 'retweeted':
                return True
    return False


# ── Candidate endpoints that MIGHT return user replies ─────────────────────
# /user/last_tweets is confirmed in production to exclude replies. These are
# probed once per process, lazily, the first time check_comment needs a
# fallback (i.e. tweet-side returned 0 replies — the "broken endpoint" path).
# The probe makes one call per candidate against the user being verified, so
# the spend is bounded and the first response can also double-serve as the
# first scan page.

_REPLIES_ENDPOINT_CANDIDATES = (
    # (path, extra_params_dict, short_name)
    ('/twitter/user/last_tweets',           {'includeReplies':    'true'}, 'last_tweets+includeReplies'),
    ('/twitter/user/last_tweets',           {'include_replies':   'true'}, 'last_tweets+include_replies'),
    ('/twitter/user/last_tweets',           {'withReplies':       'true'}, 'last_tweets+withReplies'),
    ('/twitter/user/last_tweets',           {'include_rts_and_replies': 'true'}, 'last_tweets+include_rts_and_replies'),
    ('/twitter/user/last_tweets_and_replies', {},                          'last_tweets_and_replies'),
    ('/twitter/user/replies',               {},                            'user/replies'),
    ('/twitter/user/with_replies',          {},                            'user/with_replies'),
    ('/twitter/user/tweets_and_replies',    {},                            'tweets_and_replies'),
)

# None = not probed yet. Dict = probe found a working endpoint. 'unavailable'
# = probe ran, nothing returned replies. Cached for the process lifetime so
# the spend (up to len(_REPLIES_ENDPOINT_CANDIDATES) calls) happens at most
# once.
_replies_endpoint_probe_result = None
_replies_endpoint_probe_lock   = asyncio.Lock()


async def _probe_replies_endpoint(test_username: str) -> Optional[dict]:
    """Try each candidate ONCE per process. Returns the winning endpoint
    dict {'path', 'extra_params', 'name'} or None if no candidate exposed
    replies. Subsequent calls return the cached result without spending."""
    global _replies_endpoint_probe_result
    async with _replies_endpoint_probe_lock:
        if isinstance(_replies_endpoint_probe_result, dict):
            return _replies_endpoint_probe_result
        if _replies_endpoint_probe_result == 'unavailable':
            return None

        print(f'[twitter] probe_replies: starting one-shot probe with @{test_username} '
              f'({len(_REPLIES_ENDPOINT_CANDIDATES)} candidates)')

        for path, extra, name in _REPLIES_ENDPOINT_CANDIDATES:
            params = {'userName': test_username, **extra}
            data = await _api_get(path, params, timeout=20.0)
            if data is None:
                print(f'[twitter] probe_replies {name}: api_error/4xx/5xx — skipping')
                continue
            tweets, extract_path = _extract_tweets_from_payload(data)
            reply_count = sum(1 for tw in tweets if _is_reply_tweet(tw))
            print(f'[twitter] probe_replies {name}: tweets={len(tweets)} '
                  f'replies={reply_count} extract_path={extract_path!r}')
            if reply_count > 0:
                winner = {'path': path, 'extra_params': extra, 'name': name}
                _replies_endpoint_probe_result = winner
                print(f'[twitter] probe_replies: WINNER={name} '
                      f'(this will be used for comment user-side from now on)')
                return winner

        _replies_endpoint_probe_result = 'unavailable'
        print('[twitter] probe_replies: NO candidate returned replies — '
              'comment verification will rely on tweet-side only')
        return None


async def _fetch_user_recent_tweets(
    username: str,
    *,
    endpoint: str = '/twitter/user/last_tweets',
    extra_params: Optional[dict] = None,
    max_pages: Optional[int] = None,
) -> list:
    """Return the user's recent tweets, paginated.

    endpoint / extra_params let the caller route to a replies-bearing endpoint
    discovered via _probe_replies_endpoint. The cache is keyed by
    (endpoint, normalized_username, frozen(extra_params)) so different
    endpoint requests for the same user don't poison each other.
    max_pages overrides the module default for cheap user-side retweet checks.
    """
    target = normalize_username(username)
    if not target:
        return []
    if extra_params is None:
        extra_params = {}
    cache_key = (endpoint, target, frozenset((k, str(v)) for k, v in extra_params.items()))
    max_pages = max_pages if max_pages is not None else _USER_TWEETS_MAX_PAGES

    # Cache check — under the lock so two concurrent verifies share the result.
    async with _user_tweets_lock:
        cached = _user_tweets_cache.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < _USER_TWEETS_CACHE_TTL:
            age = time.monotonic() - cached[0]
            print(f'[twitter] user_tweets cache HIT key=({endpoint},@{target},{dict(extra_params) or "{}"}) '
                  f'age={age:.1f}s tweets={len(cached[1])}')
            return cached[1]

    all_tweets: list = []
    sent_cursor: Optional[str] = None
    started_at = time.monotonic()
    deadline   = started_at + _PAGINATION_BUDGET_S
    pages = 0
    prev_empty = False   # tolerate one empty page when the cursor is advancing
    break_reason = f'page_cap({max_pages})'
    replies_seen   = 0   # diagnostic: how many tweets in the timeline are replies
    retweets_seen  = 0   # diagnostic: how many are retweets

    for page_idx0 in range(max_pages):
        page_idx = page_idx0 + 1
        if time.monotonic() > deadline:
            break_reason = f'time_budget({_PAGINATION_BUDGET_S}s)'
            break

        params: dict = {'userName': target, **extra_params}
        if sent_cursor:
            params['cursor'] = sent_cursor

        data = await _api_get(endpoint, params, timeout=30.0)
        if data is None:
            break_reason = 'api_error_first_page' if pages == 0 else 'api_error_midstream'
            break

        pages += 1

        # Extract tweets via the path-probing helper. The previous chain
        # short-circuited on truthiness when `data['data']` was an empty list
        # or had an empty `tweets` array alongside a populated `pin_tweet`,
        # which silently returned [] even when later pages had real data.
        tweets, extract_path = _extract_tweets_from_payload(data)

        _log_user_tweets_data_shape_once(data)
        _log_shape_once('user_tweets', data, tweets[0] if tweets else None)
        if tweets:
            _log_first_tweet_keys_once(tweets[0])

        # Count reply / retweet tweets in this page (cumulative on all_tweets).
        for tw in tweets:
            if _is_reply_tweet(tw):
                replies_seen += 1
            if _is_retweet_tweet(tw):
                retweets_seen += 1

        all_tweets.extend(tweets)

        raw_has_next = data.get('has_next_page')
        if raw_has_next is None:
            raw_has_next = data.get('hasNextPage')
        parsed_has_next = _parse_has_next(raw_has_next)
        new_cursor, cursor_src = _read_next_cursor(data, sent_cursor)
        same_as_sent = (
            new_cursor is not None and sent_cursor is not None
            and new_cursor == sent_cursor
        )

        print(
            f'[twitter] user_tweets p{page_idx} @{target} via {endpoint}: '
            f'got={len(tweets)} total={len(all_tweets)} '
            f'replies_so_far={replies_seen} retweets_so_far={retweets_seen} '
            f'path={extract_path!r} '
            f'has_next_raw={raw_has_next!r} has_next_parsed={parsed_has_next} '
            f'next_cursor={_cursor_preview(new_cursor)} src={cursor_src!r} '
            f'same_as_sent={same_as_sent}'
        )

        # Stop conditions in priority order. The cursor is the source of
        # truth: production logs show /user/last_tweets returns
        # has_next_page=False on page 1 WITH a real next_cursor that DOES
        # yield more data. Trust the cursor; treat has_next=False alone as
        # advisory.
        if not new_cursor:
            break_reason = f'no_next_cursor(src={cursor_src!r})'
            break
        if same_as_sent:
            break_reason = 'cursor_not_advancing(api_returned_same_cursor)'
            break

        # Tolerate ONE empty page if the cursor is still advancing — the
        # API occasionally returns a metadata-only first page. A second
        # empty page in a row means we're really out of data.
        if len(tweets) == 0:
            if prev_empty:
                break_reason = 'two_empty_pages_in_a_row'
                break
            prev_empty = True
            print(f'[twitter] user_tweets p{page_idx} @{target}: '
                  f'empty page but cursor advances — continuing')
        else:
            prev_empty = False

        # has_next_page=False with NO next cursor is handled above. If it's
        # False but a cursor exists, the API is being inconsistent — keep
        # paging and let the cursor / empty-page logic stop us.
        if parsed_has_next is False and not tweets and prev_empty:
            # The API really wants us to stop and there's nothing in hand.
            break_reason = f'has_next_page=False(raw={raw_has_next!r})_after_empty'
            break

        sent_cursor = new_cursor

    elapsed = time.monotonic() - started_at
    print(f'[twitter] user_tweets done @{target} via {endpoint}: '
          f'tweets={len(all_tweets)} pages={pages} '
          f'replies_in_timeline={replies_seen} retweets_in_timeline={retweets_seen} '
          f'reason={break_reason} {elapsed:.1f}s')

    async with _user_tweets_lock:
        _user_tweets_cache[cache_key] = (time.monotonic(), all_tweets)
        # Opportunistic prune so the cache never grows without bound under
        # heavy churn — drop entries older than 2× TTL.
        if len(_user_tweets_cache) > 256:
            stale_cutoff = time.monotonic() - (_USER_TWEETS_CACHE_TTL * 2)
            for k in [k for k, v in list(_user_tweets_cache.items())
                      if v[0] < stale_cutoff][:64]:
                _user_tweets_cache.pop(k, None)

    return all_tweets


def extract_tweet_id(url: str) -> Optional[str]:
    m = re.search(r'/status/(\d+)', url or '')
    return m.group(1) if m else None


def extract_author_from_tweet_url(url: str) -> Optional[str]:
    """Extract the author handle from https://x.com/Author/status/123"""
    if not url:
        return None
    m = re.search(r'(?:twitter|x)\.com/([^/?#]+)/status/\d+', url, re.IGNORECASE)
    return m.group(1).lower() if m else None


async def _api_get(path: str, params: dict, timeout: float = 30.0) -> Optional[dict]:
    """GET a TwitterAPI.io endpoint. Returns parsed JSON dict or None on any failure."""
    if not API_KEY:
        print('[twitter] _api_get: no API key configured')
        return None

    if not await _budget_allows():
        # Over budget — degrade to inconclusive rather than spend more money.
        return None

    # Space consecutive calls so deep pagination cannot spike rate/cost.
    await _respect_min_gap()

    url     = f'{API_BASE}{path}'
    headers = {'X-API-Key': API_KEY}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            print(f'[twitter] GET {path} params={params}')
            resp = await client.get(url, params=params, headers=headers)

        if resp.status_code in (401, 403):
            print(f'[twitter] auth error {resp.status_code}: {resp.text[:200]}')
            await _record_health(success=False)
            return None

        if resp.status_code != 200:
            print(f'[twitter] HTTP {resp.status_code}: {resp.text[:200]}')
            await _record_health(success=False)
            return None

        data = resp.json()
        keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
        print(f'[twitter] OK: keys={keys}')
        await _record_health(success=True)
        return data

    except httpx.TimeoutException:
        print(f'[twitter] timeout after {timeout}s')
        await _record_health(success=False)
        return None
    except Exception as e:
        print(f'[twitter] exception: {type(e).__name__}: {e}')
        import traceback
        traceback.print_exc()
        await _record_health(success=False)
        return None


async def check_comment(tweet_id: str, target_username: str) -> dict:
    """Verify @target_username replied to tweet_id.

    Order: TWEET-SIDE FIRST, USER-SIDE FALLBACK (only if a working
    replies-bearing user endpoint exists). Production proved
    /twitter/user/last_tweets EXCLUDES replies — scanning 239 tweets for a
    reply via that endpoint found nothing while tweet-side hit on page 1 in
    1 second. So the cheap and reliable path is tweet-side first; user-side
    is only worth running when tweet-side returns 0 replies AND a probe has
    discovered an endpoint that does carry user replies.

    Result rules (signature unchanged: dict with 'verified' and 'reason'):
      tweet_side True                                                   -> True
      tweet_side False, scanned ≥ _TWEETSIDE_CONFIDENT_FALSE_MIN        -> False (no fallback — trust)
      tweet_side False/None, scanned < confident_min, replies endpoint AVAILABLE:
          probe_match  True                                              -> True
          probe_match  False                                              -> False
      tweet_side False/None, scanned < confident_min, NO replies endpoint:
          tweet_side False                                                -> False (best we have)
          tweet_side None                                                 -> None  (inconclusive)
    """
    print(f'[twitter] check_comment: tweet={tweet_id} target={target_username} '
          f'order=tweetside_first')
    target = normalize_username(target_username)
    if not target or not tweet_id:
        return {'verified': None, 'reason': 'missing_input'}
    target_id_s = _id_str(tweet_id)

    # ── PRIMARY: tweet-side replies scan ─────────────────────────────────────
    ts = await _check_comment_tweetside(tweet_id, target)
    ts_verified = ts.get('verified')
    ts_seen     = int(ts.get('seen')      or 0)
    ts_pages    = int(ts.get('pages')     or 0)
    ts_calls    = int(ts.get('api_calls') or ts_pages)

    if ts_verified is True:
        print(f'[twitter] check_comment: matched_side=tweet_side '
              f'tweetside_seen={ts_seen} pages={ts_pages} api_calls={ts_calls}')
        return {'verified': True, 'reason': 'found_comment_tweetside'}

    # Tweet-side ran healthy (lots of replies seen) AND user wasn't there.
    # Trust the False — the user genuinely did not reply from this handle.
    # No user-side fallback in this branch: it would cost ~12 API calls and
    # ~20 seconds for nothing on every legitimate "didn't comment" case.
    if ts_verified is False and ts_seen >= _TWEETSIDE_CONFIDENT_FALSE_MIN:
        print(f'[twitter] check_comment: matched_side=none '
              f'tweetside_seen={ts_seen} (confident no-match — '
              f'skipping userside fallback) api_calls={ts_calls}')
        return {'verified': False,
                'reason': f'no_comment_tweetside({ts_seen}_replies_scanned)'}

    # Tweet-side returned 0 / very-few replies or hit an api_error. This is
    # the "broken /tweet/replies endpoint" signature. Try user-side IFF a
    # replies-bearing endpoint can be found.
    print(f'[twitter] check_comment: tweetside={ts_verified} seen={ts_seen} '
          f'pages={ts_pages} — running probe + userside fallback')

    fallback = await _comment_userside_fallback(target, target_id_s)
    fb_match    = fallback.get('matched', False)
    fb_scanned  = int(fallback.get('scanned')   or 0)
    fb_endpoint = fallback.get('endpoint')
    fb_calls    = int(fallback.get('api_calls') or 0)

    total_calls = ts_calls + fb_calls

    if fb_match:
        print(f'[twitter] check_comment: matched_side=user_side via {fb_endpoint!r} '
              f'tweetside_seen={ts_seen} userside_scanned={fb_scanned} '
              f'api_calls={total_calls}')
        return {'verified': True,
                'reason': f'found_comment_userside_via_{fb_endpoint or "unknown"}'}

    if fb_endpoint is None:
        # No replies endpoint available. We're back to whatever tweet-side
        # said. If it said False (even with 0 replies seen), surface that;
        # if it said None (api_error), surface inconclusive.
        if ts_verified is False:
            print(f'[twitter] check_comment: matched_side=none '
                  f'tweetside_seen={ts_seen} userside=unavailable api_calls={total_calls}')
            return {'verified': False,
                    'reason': f'no_comment_tweetside_only(seen={ts_seen})_no_userside_endpoint'}
        print(f'[twitter] check_comment: matched_side=none '
              f'tweetside=inconclusive userside=unavailable api_calls={total_calls}')
        return {'verified': None,
                'reason': 'tweetside_inconclusive_no_userside_endpoint'}

    # Replies endpoint exists, user-side ran, no match.
    confident_userside_no = fb_scanned >= _USERSIDE_CONFIDENT_FALSE_MIN
    if confident_userside_no or ts_verified is False:
        print(f'[twitter] check_comment: matched_side=none '
              f'tweetside_seen={ts_seen} userside_scanned={fb_scanned} '
              f'api_calls={total_calls}')
        return {'verified': False,
                'reason': f'no_comment_both_sides(ts={ts_seen},us={fb_scanned})'}
    print(f'[twitter] check_comment: matched_side=none '
          f'tweetside_seen={ts_seen} userside_scanned={fb_scanned} '
          f'(both shallow) api_calls={total_calls}')
    return {'verified': None,
            'reason': f'both_sides_shallow(ts={ts_seen},us={fb_scanned})'}


async def _comment_userside_fallback(target: str, target_id_s: str) -> dict:
    """Probe (once per process) for a replies-bearing user endpoint and, if
    one exists, scan the target's recent tweets via that endpoint for a
    reply to target_id. Returns {'matched', 'endpoint', 'scanned', 'api_calls'}.
    'endpoint' is None when no candidate returned replies."""
    probe = await _probe_replies_endpoint(target)
    if probe is None:
        # The probe itself may have made up to len(candidates) calls.
        # We don't track them here precisely; the probe logs each.
        return {'matched': False, 'endpoint': None, 'scanned': 0, 'api_calls': 0}

    tweets = await _fetch_user_recent_tweets(
        target,
        endpoint=probe['path'],
        extra_params=probe['extra_params'],
        max_pages=_USER_TWEETS_MAX_PAGES,
    )
    scanned = len(tweets)
    for tw in tweets:
        if _tweet_replies_to(tw, target_id_s):
            return {'matched': True, 'endpoint': probe['name'],
                    'scanned': scanned, 'api_calls': scanned // 20 + 1}
    return {'matched': False, 'endpoint': probe['name'],
            'scanned': scanned, 'api_calls': scanned // 20 + 1}


async def _check_comment_tweetside(tweet_id: str, target: str) -> dict:
    """Tweet-side replies pagination (the original /twitter/tweet/replies
    scan, kept as the secondary check). Expects target already normalized."""
    sent_cursor: Optional[str] = None
    seen               = 0
    pages              = 0
    max_pages          = _REPLIES_MAX_PAGES
    started_at         = time.monotonic()
    deadline           = started_at + _PAGINATION_BUDGET_S
    api_call_succeeded = False
    scanned_usernames: list = []
    seen_handles: set  = set()   # all handles observed so far (dedupe analysis)
    break_reason       = f'page_cap({max_pages})'

    for page_idx0 in range(max_pages):
        page_idx = page_idx0 + 1
        elapsed  = time.monotonic() - started_at
        if time.monotonic() > deadline:
            break_reason = f'time_budget({_PAGINATION_BUDGET_S}s)'
            break

        print(f'[twitter] check_comment tweetside p{page_idx}: '
              f'sending cursor={_cursor_preview(sent_cursor)} elapsed={elapsed:.1f}s')

        params: dict = {'tweetId': str(tweet_id)}
        if sent_cursor:
            params['cursor'] = sent_cursor

        data = await _api_get('/twitter/tweet/replies', params, timeout=30.0)
        if data is None:
            if not api_call_succeeded:
                return {'verified': None, 'reason': 'api_error',
                        'seen': 0, 'pages': 0, 'api_calls': 1}
            break_reason = 'api_error_midstream'
            break

        api_call_succeeded = True
        pages += 1
        tweets = data.get('tweets') or data.get('replies') or data.get('data') or []
        if not isinstance(tweets, list):
            tweets = []

        # Per-page dedupe analysis: how many handles on this page are new?
        page_handles: list = []
        for t in tweets:
            h = _primary_handle(t)
            if h:
                page_handles.append(h)
        page_handle_set = set(page_handles)
        new_in_page = len(page_handle_set - seen_handles)
        overlap_in_page = len(page_handle_set & seen_handles)

        # Pagination signals (raw + parsed for log clarity)
        raw_has_next = data.get('has_next_page')
        if raw_has_next is None:
            raw_has_next = data.get('hasNextPage')
        parsed_has_next = _parse_has_next(raw_has_next)

        new_cursor, cursor_src = _read_next_cursor(data, sent_cursor)
        same_as_sent = (
            new_cursor is not None and sent_cursor is not None
            and new_cursor == sent_cursor
        )

        print(
            f'[twitter] check_comment tweetside p{page_idx}: '
            f'replies={len(tweets)} unique_handles={len(page_handle_set)} '
            f'new={new_in_page} overlap_with_prev={overlap_in_page} '
            f'has_next_raw={raw_has_next!r}({type(raw_has_next).__name__}) '
            f'has_next_parsed={parsed_has_next} '
            f'next_cursor={_cursor_preview(new_cursor)} src={cursor_src!r} '
            f'same_as_sent={same_as_sent}'
        )

        # Match against the target — short-circuits the whole verify.
        for t in tweets:
            if _matches_target(t, target):
                print(f'[twitter] check_comment tweetside: MATCH for @{target} on page {page_idx} '
                      f'(scanned {seen + len(tweets)} replies)')
                return {'verified': True, 'reason': 'found_comment',
                        'seen': seen + len(tweets), 'pages': page_idx,
                        'api_calls': page_idx}

        scanned_usernames.extend(page_handles)
        seen_handles |= page_handle_set
        seen += len(tweets)

        # Break conditions in priority order — log exactly which fired.
        if len(tweets) == 0:
            break_reason = 'empty_page'
            break
        if parsed_has_next is False:
            break_reason = f'has_next_page=False(raw={raw_has_next!r})'
            break
        if not new_cursor:
            break_reason = f'no_next_cursor(src={cursor_src!r})'
            break
        if same_as_sent:
            # API echoed back what we sent — sending it again refetches the same
            # page. Stop cleanly and report so the symptom is visible upstream.
            break_reason = 'cursor_not_advancing(api_returned_same_cursor)'
            break

        sent_cursor = new_cursor

    elapsed = time.monotonic() - started_at
    print(f'[twitter] check_comment tweetside: stop reason={break_reason} '
          f'after {pages} pages, {seen} replies, {elapsed:.1f}s')
    if scanned_usernames:
        # Diagnostic: did the target ever appear in the data, or did matching
        # miss them? Log only usernames — first/last few + unique count.
        print(
            f'[twitter] check_comment tweetside: scanned usernames '
            f'first={scanned_usernames[:5]} last={scanned_usernames[-5:]} '
            f'total={len(scanned_usernames)} unique={len(seen_handles)}'
        )
    return {'verified': False, 'reason': 'no_comment_found',
            'seen': seen, 'pages': pages, 'api_calls': pages}


async def check_retweet(tweet_id: str, target_username: str) -> dict:
    """Verify @target_username retweeted tweet_id.

    Order: USER-SIDE FIRST with a LOW page cap, TWEET-SIDE FALLBACK. Unlike
    replies, retweets ARE in /twitter/user/last_tweets (the production log
    confirms: retweeted_tweet.id appears on tweet objects). A recent retweet
    sits at the top of the timeline, so scanning ~3 pages (~60 tweets)
    finds it cheaply. If user-side misses, fall back to the slower
    /twitter/tweet/retweeters scan."""
    print(f'[twitter] check_retweet: tweet={tweet_id} target={target_username} '
          f'order=userside_first cap={_USER_TWEETS_RETWEET_MAX_PAGES}')
    target = normalize_username(target_username)
    if not target or not tweet_id:
        return {'verified': None, 'reason': 'missing_input'}
    target_id_s = _id_str(tweet_id)

    # ── PRIMARY: user-side (cheap, low cap) ──────────────────────────────────
    user_tweets = await _fetch_user_recent_tweets(
        target, max_pages=_USER_TWEETS_RETWEET_MAX_PAGES,
    )
    us_count = len(user_tweets)
    if us_count:
        for tw in user_tweets:
            if _tweet_retweets(tw, target_id_s):
                rt_id = _id_str(tw.get('id') or tw.get('id_str'))
                print(f'[twitter] check_retweet: matched_side=user_side @{target} '
                      f'retweeted {target_id_s} (rt_tweet={rt_id}, '
                      f'scanned={us_count}) api_calls≈{us_count // 20 + 1}')
                return {'verified': True, 'reason': 'found_retweet_userside'}
        print(f'[twitter] check_retweet: userside no match in {us_count} tweets '
              f'@{target} — running tweet-side retweeters fallback')
    else:
        print(f'[twitter] check_retweet: userside returned 0 tweets @{target} '
              f'— running tweet-side retweeters fallback')

    # ── SECONDARY: tweet-side retweeters scan ────────────────────────────────
    ts = await _check_retweet_tweetside(tweet_id, target)
    ts_verified = ts.get('verified')
    ts_seen     = int(ts.get('seen')      or 0)
    ts_calls    = int(ts.get('api_calls') or ts.get('pages') or 0)
    total_calls = (us_count // 20 + 1) + ts_calls

    if ts_verified is True:
        print(f'[twitter] check_retweet: matched_side=tweet_side '
              f'tweetside_seen={ts_seen} api_calls={total_calls}')
        return {'verified': True, 'reason': 'found_retweet_tweetside'}

    if ts_verified is False:
        print(f'[twitter] check_retweet: matched_side=none '
              f'userside_scanned={us_count} tweetside_seen={ts_seen} '
              f'api_calls={total_calls}')
        reason = ('no_retweet_found_both_sides' if us_count
                  else 'no_retweet_found_tweetside_only')
        return {'verified': False, 'reason': reason}

    # ts_verified is None
    if us_count >= _USERSIDE_CONFIDENT_FALSE_MIN:
        print(f'[twitter] check_retweet: matched_side=none '
              f'userside_scanned={us_count}(confident) tweetside=inconclusive '
              f'api_calls={total_calls}')
        return {
            'verified': False,
            'reason':  f'no_retweet_userside({us_count}_scanned)_tweetside_inconclusive',
        }
    if us_count:
        return {
            'verified': None,
            'reason':  f'userside_shallow({us_count})_tweetside_inconclusive',
        }
    return {'verified': None, 'reason': 'both_sides_inconclusive'}


async def _check_retweet_tweetside(tweet_id: str, target: str) -> dict:
    """Tweet-side retweeters pagination (the original /twitter/tweet/retweeters
    scan, kept as the secondary check). Expects target already normalized."""
    sent_cursor: Optional[str] = None
    seen               = 0
    pages              = 0
    max_pages          = _RETWEETERS_MAX_PAGES
    started_at         = time.monotonic()
    deadline           = started_at + _PAGINATION_BUDGET_S
    api_call_succeeded = False
    scanned_usernames: list = []
    seen_handles: set  = set()
    break_reason       = f'page_cap({max_pages})'

    for page_idx0 in range(max_pages):
        page_idx = page_idx0 + 1
        elapsed  = time.monotonic() - started_at
        if time.monotonic() > deadline:
            break_reason = f'time_budget({_PAGINATION_BUDGET_S}s)'
            break

        print(f'[twitter] check_retweet tweetside p{page_idx}: '
              f'sending cursor={_cursor_preview(sent_cursor)} elapsed={elapsed:.1f}s')

        params: dict = {'tweetId': str(tweet_id)}
        if sent_cursor:
            params['cursor'] = sent_cursor

        data = await _api_get('/twitter/tweet/retweeters', params, timeout=30.0)
        if data is None:
            if not api_call_succeeded:
                return {'verified': None, 'reason': 'api_error',
                        'seen': 0, 'pages': 0, 'api_calls': 1}
            break_reason = 'api_error_midstream'
            break

        api_call_succeeded = True
        pages += 1
        users = data.get('users') or data.get('retweeters') or data.get('data') or []
        if not isinstance(users, list):
            users = []

        page_handles: list = []
        for u in users:
            h = _primary_handle(u)
            if h:
                page_handles.append(h)
        page_handle_set = set(page_handles)
        new_in_page = len(page_handle_set - seen_handles)
        overlap_in_page = len(page_handle_set & seen_handles)

        raw_has_next = data.get('has_next_page')
        if raw_has_next is None:
            raw_has_next = data.get('hasNextPage')
        parsed_has_next = _parse_has_next(raw_has_next)

        new_cursor, cursor_src = _read_next_cursor(data, sent_cursor)
        same_as_sent = (
            new_cursor is not None and sent_cursor is not None
            and new_cursor == sent_cursor
        )

        print(
            f'[twitter] check_retweet tweetside p{page_idx}: '
            f'users={len(users)} unique_handles={len(page_handle_set)} '
            f'new={new_in_page} overlap_with_prev={overlap_in_page} '
            f'has_next_raw={raw_has_next!r}({type(raw_has_next).__name__}) '
            f'has_next_parsed={parsed_has_next} '
            f'next_cursor={_cursor_preview(new_cursor)} src={cursor_src!r} '
            f'same_as_sent={same_as_sent}'
        )

        for u in users:
            if _matches_target(u, target):
                print(f'[twitter] check_retweet tweetside: MATCH for @{target} on page {page_idx} '
                      f'(scanned {seen + len(users)} retweeters)')
                return {'verified': True, 'reason': 'found_retweet',
                        'seen': seen + len(users), 'pages': page_idx,
                        'api_calls': page_idx}

        scanned_usernames.extend(page_handles)
        seen_handles |= page_handle_set
        seen += len(users)

        if len(users) == 0:
            break_reason = 'empty_page'
            break
        if parsed_has_next is False:
            break_reason = f'has_next_page=False(raw={raw_has_next!r})'
            break
        if not new_cursor:
            break_reason = f'no_next_cursor(src={cursor_src!r})'
            break
        if same_as_sent:
            break_reason = 'cursor_not_advancing(api_returned_same_cursor)'
            break

        sent_cursor = new_cursor

    elapsed = time.monotonic() - started_at
    print(f'[twitter] check_retweet tweetside: stop reason={break_reason} '
          f'after {pages} pages, {seen} retweeters, {elapsed:.1f}s')
    if scanned_usernames:
        print(
            f'[twitter] check_retweet tweetside: scanned usernames '
            f'first={scanned_usernames[:5]} last={scanned_usernames[-5:]} '
            f'total={len(scanned_usernames)} unique={len(seen_handles)}'
        )
    return {'verified': False, 'reason': 'no_retweet_found',
            'seen': seen, 'pages': pages, 'api_calls': pages}


async def lookup_twitter_user_by_login(username: str) -> Optional[dict]:
    """Look up a Twitter user by handle. Returns {'id', 'username', 'display_name'} or None."""
    target = normalize_username(username)
    if not target:
        return None
    print(f'[twitter] lookup_by_login: @{target}')

    data = await _api_get('/twitter/user/info', {'userName': target}, timeout=20.0)
    if not data:
        return None

    user = data.get('user') or data.get('data') or data
    if not isinstance(user, dict):
        print(f'[twitter] lookup_by_login: unexpected user shape type={type(user).__name__}')
        return None

    print(f'[twitter] user_info raw keys: {list(user.keys())}')
    follower_fields = {k: v for k, v in user.items() if 'follow' in k.lower()}
    print(
        f'[twitter] user_info sample: '
        f'id={user.get("id")} userName={user.get("userName")} '
        f'follower_fields={follower_fields}'
    )

    public_metrics = user.get('public_metrics') if isinstance(user.get('public_metrics'), dict) else {}
    followers_count = int(
        user.get('followers')
        or user.get('followers_count')
        or user.get('followersCount')
        or user.get('followerCount')
        or public_metrics.get('followers_count')
        or 0
    )

    return {
        'id':                str(user.get('id') or user.get('userId') or ''),
        'username':          user.get('userName') or user.get('username') or user.get('screen_name') or target,
        'display_name':      user.get('name') or user.get('displayName') or '',
        'followers_count':   followers_count,
        'profile_image_url': (user.get('profilePicture') or user.get('profile_image_url') or user.get('profileImageUrl') or ''),
    }


async def lookup_twitter_user_by_id(user_id: str) -> Optional[dict]:
    """Not implemented — left for interface compat."""
    print(f'[twitter] lookup_by_id: {user_id} — not implemented')
    return None


# ── Legacy compatibility stubs ─────────────────────────────────────────────────

async def get_api():
    return None

async def reload_api():
    return None
