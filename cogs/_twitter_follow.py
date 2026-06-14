"""
_twitter_follow.py — follow-relationship check for the Giveaway task-gating
feature.

cogs/_twitter.py is off limits and never implemented a follow check, so this
sibling helper adds exactly one capability: "does user A follow user B". It
reuses the same TwitterAPI.io client config the main module already loads
(API_BASE, API_KEY, the X-API-Key header, httpx) by importing those values,
and mirrors _twitter.py's request + logging style. It does not modify
cogs/_twitter.py in any way.

Endpoint (direct single call, no pagination):
    GET https://api.twitterapi.io/twitter/user/check_follow_relationship
        ?source_user_name=<follower>&target_user_name=<account>
    -> { "status": "success"|"error",
         "data": { "following": bool, "followed_by": bool }, ... }

`following` is true when source_user_name follows target_user_name, which is
exactly the giveaway follow task: the entrant (source) must follow the target
account. Cost is a single ~$0.001 call per check.
"""
import httpx

# Reuse the existing module's configuration. Importing these values is read
# only; cogs/_twitter.py is not touched.
from cogs._twitter import API_BASE, API_KEY, normalize_username

_FOLLOW_PATH = '/twitter/user/check_follow_relationship'


async def check_user_follows(
    target_username: str, follower_username: str, timeout: float = 20.0,
) -> dict:
    """Does @follower_username follow @target_username?

    Returns a dict shaped like _twitter.py's verification helpers:
        {'verified': True|False|None, 'reason': str}
    where verified is:
        True  — the follow relationship is confirmed
        False — confirmed NOT following, or a transient/API error (so the
                caller treats it as "not done" and the user can retry; we never
                grant entry on an unconfirmed follow)
        None  — only used if we cannot reach a verdict in a way that should not
                count against the user; for follow we never return None because
                a missing API key or error is safest treated as not verified.

    The argument order mirrors the engage/raid convention of (target, actor):
    target_username is the account to be followed, follower_username is the
    entrant's linked X handle.
    """
    target   = normalize_username(target_username)
    follower = normalize_username(follower_username)
    if not target or not follower:
        return {'verified': False, 'reason': 'missing_handle'}

    if not API_KEY:
        print('[twitter_follow] no API key configured')
        return {'verified': False, 'reason': 'no_api_key'}

    url     = f'{API_BASE}{_FOLLOW_PATH}'
    headers = {'X-API-Key': API_KEY}
    params  = {'source_user_name': follower, 'target_user_name': target}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            print(f'[twitter_follow] GET {_FOLLOW_PATH} source={follower} target={target}')
            resp = await client.get(url, params=params, headers=headers)

        if resp.status_code != 200:
            print(f'[twitter_follow] HTTP {resp.status_code}: {resp.text[:200]}')
            return {'verified': False, 'reason': f'http_{resp.status_code}'}

        data = resp.json()
        if not isinstance(data, dict):
            return {'verified': False, 'reason': 'bad_payload'}

        if data.get('status') == 'error':
            print(f'[twitter_follow] api error: {str(data.get("message"))[:200]}')
            return {'verified': False, 'reason': 'api_error'}

        inner = data.get('data') if isinstance(data.get('data'), dict) else data
        following = inner.get('following')
        if following is True or (isinstance(following, str) and following.strip().lower() == 'true'):
            return {'verified': True, 'reason': 'following'}
        return {'verified': False, 'reason': 'not_following'}

    except httpx.TimeoutException:
        print(f'[twitter_follow] timeout after {timeout}s')
        return {'verified': False, 'reason': 'timeout'}
    except Exception as e:
        print(f'[twitter_follow] exception: {type(e).__name__}: {e}')
        return {'verified': False, 'reason': 'exception'}
