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
_REPLIES_MAX_PAGES    = max(1, int(os.getenv('TWITTER_REPLIES_MAX_PAGES', '20') or 20))
_RETWEETERS_MAX_PAGES = max(1, int(os.getenv('TWITTER_RETWEETERS_MAX_PAGES', '20') or 20))
_PAGINATION_BUDGET_S  = max(5.0, float(os.getenv('TWITTER_PAGINATION_BUDGET_S', '120') or 120))

print(f'[twitter] replies cap={_REPLIES_MAX_PAGES} '
      f'retweeters cap={_RETWEETERS_MAX_PAGES} '
      f'time budget={_PAGINATION_BUDGET_S}s')


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

        print(f'[twitter] check_comment p{page_idx}: '
              f'sending cursor={_cursor_preview(sent_cursor)} elapsed={elapsed:.1f}s')

        params: dict = {'tweetId': str(tweet_id)}
        if sent_cursor:
            params['cursor'] = sent_cursor

        data = await _api_get('/twitter/tweet/replies', params, timeout=30.0)
        if data is None:
            if not api_call_succeeded:
                return {'verified': None, 'reason': 'api_error'}
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
            f'[twitter] check_comment p{page_idx}: '
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
                print(f'[twitter] check_comment: MATCH for @{target} on page {page_idx} '
                      f'(scanned {seen + len(tweets)} replies)')
                return {'verified': True, 'reason': 'found_comment'}

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
    print(f'[twitter] check_comment: stop reason={break_reason} '
          f'after {pages} pages, {seen} replies, {elapsed:.1f}s')
    if scanned_usernames:
        # Diagnostic: did the target ever appear in the data, or did matching
        # miss them? Log only usernames — first/last few + unique count.
        print(
            f'[twitter] check_comment: scanned usernames '
            f'first={scanned_usernames[:5]} last={scanned_usernames[-5:]} '
            f'total={len(scanned_usernames)} unique={len(seen_handles)}'
        )
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

        print(f'[twitter] check_retweet p{page_idx}: '
              f'sending cursor={_cursor_preview(sent_cursor)} elapsed={elapsed:.1f}s')

        params: dict = {'tweetId': str(tweet_id)}
        if sent_cursor:
            params['cursor'] = sent_cursor

        data = await _api_get('/twitter/tweet/retweeters', params, timeout=30.0)
        if data is None:
            if not api_call_succeeded:
                return {'verified': None, 'reason': 'api_error'}
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
            f'[twitter] check_retweet p{page_idx}: '
            f'users={len(users)} unique_handles={len(page_handle_set)} '
            f'new={new_in_page} overlap_with_prev={overlap_in_page} '
            f'has_next_raw={raw_has_next!r}({type(raw_has_next).__name__}) '
            f'has_next_parsed={parsed_has_next} '
            f'next_cursor={_cursor_preview(new_cursor)} src={cursor_src!r} '
            f'same_as_sent={same_as_sent}'
        )

        for u in users:
            if _matches_target(u, target):
                print(f'[twitter] check_retweet: MATCH for @{target} on page {page_idx} '
                      f'(scanned {seen + len(users)} retweeters)')
                return {'verified': True, 'reason': 'found_retweet'}

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
    print(f'[twitter] check_retweet: stop reason={break_reason} '
          f'after {pages} pages, {seen} retweeters, {elapsed:.1f}s')
    if scanned_usernames:
        print(
            f'[twitter] check_retweet: scanned usernames '
            f'first={scanned_usernames[:5]} last={scanned_usernames[-5:]} '
            f'total={len(scanned_usernames)} unique={len(seen_handles)}'
        )
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
