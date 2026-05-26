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
    """Check if target_username replied to tweet_id.

    Paginates replies via next_cursor / has_next_page until the user is found,
    the API says there are no more pages, or a page/time cap is hit (popular
    tweets need many pages — a shallow cap caused legitimate replies to be
    missed). Early-stops on match. Zero data → inconclusive; data + no match
    → False.
    """
    print(f'[twitter] check_comment: tweet={tweet_id} target={target_username}')
    target = normalize_username(target_username)
    if not target or not tweet_id:
        return {'verified': None, 'reason': 'missing_input'}

    cursor              = None
    seen                = 0
    pages               = 0
    max_pages           = max(1, int(os.getenv('TWITTER_REPLIES_MAX_PAGES', '20') or 20))
    deadline            = time.monotonic() + 25.0   # overall wall-clock budget
    api_call_succeeded  = False

    for _page in range(max_pages):
        if time.monotonic() > deadline:
            print(f'[twitter] check_comment: time budget reached after {pages} pages')
            break

        params: dict = {'tweetId': str(tweet_id)}
        if cursor:
            params['cursor'] = cursor

        data = await _api_get('/twitter/tweet/replies', params, timeout=30.0)
        if data is None:
            if not api_call_succeeded:
                # First call failed — we have no data at all, inconclusive
                return {'verified': None, 'reason': 'api_error'}
            # Later page failed but we already scanned some replies — treat as done
            break

        api_call_succeeded = True
        pages += 1
        tweets = data.get('tweets') or data.get('replies') or data.get('data') or []
        if not isinstance(tweets, list):
            tweets = []

        for t in tweets:
            author      = t.get('author') or t.get('user') or {}
            author_name = normalize_username(
                author.get('userName') or author.get('username') or author.get('screen_name') or ''
            )
            if author_name == target:
                print(f'[twitter] check_comment: MATCH for @{target} on page {pages} '
                      f'(scanned {seen + len(tweets)} replies)')
                return {'verified': True, 'reason': 'found_comment'}

        seen += len(tweets)
        has_next = data.get('has_next_page')
        if has_next is None:
            has_next = data.get('hasNextPage')
        cursor = data.get('next_cursor') or data.get('nextCursor') or data.get('cursor')
        # Stop when API says no more pages, no cursor, or an empty page.
        if has_next is False or not cursor or len(tweets) == 0:
            break

    # API succeeded — conclusive result: user did not comment
    print(f'[twitter] check_comment: no match for @{target} in {seen} replies across {pages} pages')
    return {'verified': False, 'reason': 'no_comment_found'}


async def check_retweet(tweet_id: str, target_username: str) -> dict:
    """Check if target_username retweeted tweet_id via /twitter/tweet/retweeters.

    API success + user absent from retweeters list = verified=False (conclusive).
    Only genuine API/network failure returns verified=None.
    """
    print(f'[twitter] check_retweet: tweet={tweet_id} target={target_username}')
    target = normalize_username(target_username)
    if not target or not tweet_id:
        return {'verified': None, 'reason': 'missing_input'}

    cursor             = None
    seen               = 0
    pages              = 0
    max_pages          = max(1, int(os.getenv('TWITTER_RETWEETERS_MAX_PAGES', '20') or 20))
    deadline           = time.monotonic() + 25.0   # overall wall-clock budget
    api_call_succeeded = False

    for _page in range(max_pages):
        if time.monotonic() > deadline:
            print(f'[twitter] check_retweet: time budget reached after {pages} pages')
            break

        params: dict = {'tweetId': str(tweet_id)}
        if cursor:
            params['cursor'] = cursor

        data = await _api_get('/twitter/tweet/retweeters', params, timeout=30.0)
        if data is None:
            if not api_call_succeeded:
                return {'verified': None, 'reason': 'api_error'}
            break  # later-page failure but we already scanned some retweeters

        api_call_succeeded = True
        pages += 1
        users = data.get('users') or data.get('retweeters') or data.get('data') or []
        if not isinstance(users, list):
            users = []

        for u in users:
            uname = normalize_username(
                u.get('userName') or u.get('username') or u.get('screen_name') or ''
            )
            if uname == target:
                print(f'[twitter] check_retweet: MATCH for @{target} on page {pages} '
                      f'(scanned {seen + len(users)} retweeters)')
                return {'verified': True, 'reason': 'found_retweet'}

        seen += len(users)
        has_next = data.get('has_next_page')
        if has_next is None:
            has_next = data.get('hasNextPage')
        cursor = data.get('next_cursor') or data.get('nextCursor') or data.get('cursor')
        if has_next is False or not cursor or len(users) == 0:
            break

    print(f'[twitter] check_retweet: @{target} not in {seen} retweeters of tweet {tweet_id} '
          f'across {pages} pages')
    return {'verified': False, 'reason': 'no_retweet_found'}


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
