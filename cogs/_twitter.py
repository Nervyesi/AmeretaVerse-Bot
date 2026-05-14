"""
Twitter scraping via Apify API — stateless HTTP, no account management.

Env vars:
  APIFY_TOKEN  — required: your Apify API key
  APIFY_ACTOR  — optional: defaults to 'apidojo~twitter-scraper-lite'

Function return shapes are identical to the previous implementation
so raidbot.py callers need no changes:
  {'verified': True|False|None, 'reason': str}

verified=None means inconclusive — caller MUST NOT flag the user.
verified=False means conclusively NOT done (scraping succeeded, task absent).
verified=True  means conclusively done.
"""
import os
import re
import asyncio
import traceback
from typing import Optional

import httpx

APIFY_TOKEN = (os.getenv('APIFY_TOKEN') or '').strip()
APIFY_ACTOR = (os.getenv('APIFY_ACTOR') or 'apidojo~twitter-scraper-lite').strip()
_APIFY_BASE = 'https://api.apify.com/v2'

# Scraping health — tracks consecutive Apify call failures
SCRAPING_HEALTHY      = True
_consecutive_failures = 0
_FAILURE_THRESHOLD    = 3
_health_lock          = asyncio.Lock()

if APIFY_TOKEN:
    print(f'[twitter] Apify configured (actor={APIFY_ACTOR})')
else:
    print('[twitter] APIFY_TOKEN not set — verification will return inconclusive')


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
            print(f'[twitter] health: Apify failure #{_consecutive_failures}')
            if _consecutive_failures >= _FAILURE_THRESHOLD and SCRAPING_HEALTHY:
                SCRAPING_HEALTHY = False
                print(f'[twitter] SCRAPING_HEALTHY=False — {_consecutive_failures} consecutive Apify failures')


def get_scraping_health() -> dict:
    return {'healthy': SCRAPING_HEALTHY, 'consecutive_failures': _consecutive_failures}


def normalize_username(name: str) -> str:
    return (name or '').strip().lstrip('@').strip().lower()


def extract_tweet_id(url: str) -> Optional[str]:
    m = re.search(r'/status/(\d+)', url or '')
    return m.group(1) if m else None


async def _run_apify(input_data: dict, timeout: float = 90.0) -> Optional[list]:
    """POST to Apify run-sync-get-dataset-items. Returns list of items or None on any failure."""
    if not APIFY_TOKEN:
        print('[twitter] _run_apify: APIFY_TOKEN not set — returning None')
        await _record_health(success=False)
        return None

    url = f'{_APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?token={APIFY_TOKEN}'
    print(f'[twitter] Apify POST actor={APIFY_ACTOR} input={input_data}')

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=input_data)

        if resp.status_code not in (200, 201):
            print(f'[twitter] Apify HTTP {resp.status_code}: {resp.text[:300]}')
            await _record_health(success=False)
            return None

        items = resp.json()
        if not isinstance(items, list):
            print(f'[twitter] Apify response not a list: {type(items).__name__}')
            await _record_health(success=False)
            return None

        # Detect Apify demo mode — returned when plan doesn't cover this actor
        # Demo items look like: [{"demo": true}, {"demo": true}, ...]
        if items:
            all_demo = all(
                isinstance(item, dict) and set(item.keys()) <= {'demo'} and item.get('demo') is True
                for item in items
            )
            if all_demo:
                print(f'[twitter] APIFY DEMO MODE — {len(items)} demo items, no real data returned')
                print('[twitter] Fix: upgrade Apify plan, or check actor pricing/permissions at console.apify.com')
                await _record_health(success=False)
                return None

            # Filter out any stray demo items mixed with real data
            real_items = [item for item in items if not (isinstance(item, dict) and item.get('demo') is True)]
            if len(real_items) < len(items):
                print(f'[twitter] filtered {len(items) - len(real_items)} demo items, {len(real_items)} real items remain')
            items = real_items

        print(f'[twitter] Apify OK: {len(items)} items (HTTP {resp.status_code})')
        await _record_health(success=True)
        return items

    except httpx.TimeoutException:
        print(f'[twitter] Apify timeout after {timeout}s')
        await _record_health(success=False)
        return None
    except Exception as e:
        print(f'[twitter] Apify exception: {type(e).__name__}: {e}')
        traceback.print_exc()
        await _record_health(success=False)
        return None


async def check_comment(tweet_id: str, target_username: str) -> dict:
    """Check if target_username replied to tweet_id.

    Uses Twitter search operator 'from:USER conversation_id:TWEET' which directly
    returns only that user's replies in that conversation.
    """
    print(f'[twitter] check_comment: tweet={tweet_id} target={target_username}')
    target = normalize_username(target_username)
    if not target:
        return {'verified': None, 'reason': 'no_username'}
    if not tweet_id:
        return {'verified': None, 'reason': 'no_tweet_id'}

    items = await _run_apify({
        'searchTerms': [f'from:{target} conversation_id:{tweet_id}'],
        'sort':        'Latest',
        'maxItems':    10,
    }, timeout=120.0)

    if items is None:
        return {'verified': None, 'reason': 'apify_failed'}

    if len(items) == 0:
        print(f'[twitter] check_comment: no reply from @{target} in conversation {tweet_id}')
        return {'verified': False, 'reason': 'no_comment_found'}

    # Verify returned items actually belong to this user and conversation
    for item in items:
        author      = item.get('author') or {}
        author_name = normalize_username(author.get('userName') or author.get('username') or '')
        conv_id     = str(item.get('conversationId') or item.get('conversation_id') or '')
        if author_name == target and conv_id == str(tweet_id):
            print(f'[twitter] check_comment: MATCH for @{target}')
            return {'verified': True, 'reason': 'found_comment'}

    # Items returned but none matched — treat as no comment
    print(f'[twitter] check_comment: {len(items)} results but no verified match for @{target}')
    return {'verified': False, 'reason': 'no_comment_found'}


async def check_retweet(tweet_id: str, target_username: str) -> dict:
    """Check if target_username retweeted tweet_id.

    Uses 'from:USER filter:nativeretweets' to fetch that user's retweets only.
    Empty results are inconclusive (user may have no retweets or search is limited).
    """
    print(f'[twitter] check_retweet: tweet={tweet_id} target={target_username}')
    target = normalize_username(target_username)
    if not target:
        return {'verified': None, 'reason': 'no_username'}
    if not tweet_id:
        return {'verified': None, 'reason': 'no_tweet_id'}

    items = await _run_apify({
        'searchTerms': [f'from:{target} filter:nativeretweets'],
        'sort':        'Latest',
        'maxItems':    100,
    }, timeout=120.0)

    if items is None:
        return {'verified': None, 'reason': 'apify_failed'}

    if len(items) == 0:
        print(f'[twitter] check_retweet: empty results for @{target} retweets — inconclusive')
        return {'verified': None, 'reason': 'empty_search_results'}

    for item in items:
        rt_id = (
            item.get('retweetedStatusId')
            or (item.get('retweetedStatus') or {}).get('id')
            or (item.get('retweetedTweet')  or {}).get('id')
            or (item.get('referencedTweet') or {}).get('id')
            or item.get('quotedTweetId')
        )
        if rt_id and str(rt_id) == str(tweet_id):
            print(f'[twitter] check_retweet: MATCH for @{target}')
            return {'verified': True, 'reason': 'found_retweet'}

    print(f'[twitter] check_retweet: tweet {tweet_id} not in @{target} retweets ({len(items)} items)')
    return {'verified': False, 'reason': 'no_retweet_found'}


async def lookup_twitter_user_by_login(username: str) -> Optional[dict]:
    """Look up basic Twitter user info via 'from:USER' search.
    Returns {'id', 'username', 'display_name'} or None if not found or on error."""
    target = normalize_username(username)
    if not target:
        return None
    print(f'[twitter] lookup_by_login: @{target}')

    items = await _run_apify({
        'searchTerms': [f'from:{target}'],
        'sort':        'Latest',
        'maxItems':    1,
    }, timeout=60.0)

    if not items:
        return None

    item   = items[0]
    author = item.get('author') or {}
    return {
        'id':           str(author.get('id') or item.get('authorId') or ''),
        'username':     author.get('userName') or author.get('username') or target,
        'display_name': author.get('name') or '',
    }


async def lookup_twitter_user_by_id(user_id: str) -> Optional[dict]:
    """Apify doesn't support clean by-ID lookup — always returns None."""
    print(f'[twitter] lookup_by_id: {user_id} — not supported via Apify')
    return None


# ── Backward-compatibility stubs ──────────────────────────────────────────────

async def get_api():
    """Legacy compat stub — Apify needs no persistent API object."""
    return None


async def reload_api():
    """Legacy compat stub — Apify is stateless, nothing to reload."""
    return None
