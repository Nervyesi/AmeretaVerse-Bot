"""
Twitter scraping via twscrape — multi-account pool with active/backup slots.

Env vars (slots 1–5):
  Cookie auth (preferred):
    TWITTER_ACCOUNT_1_USERNAME, TWITTER_ACCOUNT_1_CT0, TWITTER_ACCOUNT_1_AUTH_TOKEN
  Password auth (fallback):
    TWITTER_ACCOUNT_1_USERNAME, TWITTER_ACCOUNT_1_EMAIL, TWITTER_ACCOUNT_1_PASSWORD

Active status is stored in the twitter_accounts DB table (admin-managed).
Only slots with active=1 in DB are added to the twscrape pool.

Backward compat: legacy TWITTER_USERNAME/CT0/AUTH_TOKEN or TWITTER_EMAIL/PASSWORD
env vars are treated as slot 1 on first run.
"""
import os
import asyncio
import re
import traceback as _tb

API_INSTANCE = None
INITIALIZED  = False
_init_lock   = asyncio.Lock()

TWSCRAPE_DB_PATH = os.getenv('TWSCRAPE_DB_PATH', '/data/twscrape.db')

# Scraping health — tracks consecutive failures to detect auth/rate-limit outages
SCRAPING_HEALTHY      = True
_consecutive_failures = 0
_FAILURE_THRESHOLD    = 3
# Reasons that indicate the scraper itself is broken (not just "not found")
_SCRAPER_ERROR_PREFIXES = ('scrape_error', 'verification_disabled', 'no_active_account', 'rate_limited')


def _update_scraping_health(result: dict) -> None:
    global SCRAPING_HEALTHY, _consecutive_failures
    verified = result.get('verified')
    reason   = result.get('reason', '')
    if verified is not None:  # True or False = real result, scraper is working
        if not SCRAPING_HEALTHY or _consecutive_failures > 0:
            print(f'[twitter] health: real result received — resetting failure counter')
            SCRAPING_HEALTHY = True
        _consecutive_failures = 0
    elif any(reason.startswith(p) for p in _SCRAPER_ERROR_PREFIXES):
        _consecutive_failures += 1
        print(f'[twitter] health: scraper failure #{_consecutive_failures} (reason={reason!r})')
        if _consecutive_failures >= _FAILURE_THRESHOLD and SCRAPING_HEALTHY:
            SCRAPING_HEALTHY = False
            print(f'[twitter] SCRAPING_HEALTHY=False — {_consecutive_failures} consecutive scraper failures')


def get_scraping_health() -> dict:
    return {'healthy': SCRAPING_HEALTHY, 'consecutive_failures': _consecutive_failures}


def _get_slot_credentials(slot: int) -> dict | None:
    """Read credentials for a numbered slot.

    Cookie auth (preferred): USERNAME + CT0 + AUTH_TOKEN
    Password auth (fallback): USERNAME + EMAIL + PASSWORD
    Returns None if slot is not configured.
    """
    prefix   = f'TWITTER_ACCOUNT_{slot}_'
    username = (os.getenv(f'{prefix}USERNAME') or '').strip()
    if not username:
        return None

    ct0        = (os.getenv(f'{prefix}CT0')        or '').strip()
    auth_token = (os.getenv(f'{prefix}AUTH_TOKEN') or '').strip()
    email      = (os.getenv(f'{prefix}EMAIL')      or '').strip()
    password   = (os.getenv(f'{prefix}PASSWORD')   or '').strip()

    if ct0 and auth_token:
        creds = {'slot': slot, 'username': username, 'auth_mode': 'cookies',
                 'ct0': ct0, 'auth_token': auth_token}
        if email:    creds['email']    = email
        if password: creds['password'] = password
        return creds

    if email and password:
        return {'slot': slot, 'username': username, 'auth_mode': 'password',
                'email': email, 'password': password}

    return None


def _get_legacy_credentials() -> dict | None:
    """Backward compat: old TWITTER_* env vars without slot numbering."""
    username = (os.getenv('TWITTER_USERNAME') or '').strip()
    if not username:
        return None
    ct0        = (os.getenv('TWITTER_CT0')        or '').strip()
    auth_token = (os.getenv('TWITTER_AUTH_TOKEN') or '').strip()
    if ct0 and auth_token:
        return {'slot': 1, 'username': username, 'auth_mode': 'cookies',
                'ct0': ct0, 'auth_token': auth_token}
    email    = (os.getenv('TWITTER_EMAIL')    or '').strip()
    password = (os.getenv('TWITTER_PASSWORD') or '').strip()
    if email and password:
        return {'slot': 1, 'username': username, 'auth_mode': 'password',
                'email': email, 'password': password}
    return None


def _discover_configured_accounts() -> list:
    accounts = []
    for slot in range(1, 6):
        creds = _get_slot_credentials(slot)
        if creds:
            accounts.append(creds)
    if not accounts:
        legacy = _get_legacy_credentials()
        if legacy:
            accounts.append(legacy)
    return accounts


async def get_api():
    global API_INSTANCE, INITIALIZED
    print(f'[twitter] get_api called (INITIALIZED={INITIALIZED})')
    async with _init_lock:
        if INITIALIZED and API_INSTANCE is not None:
            print('[twitter] get_api: returning cached API_INSTANCE')
            return API_INSTANCE

        try:
            from twscrape import API
        except ImportError:
            print('[twitter] twscrape not installed — verification disabled')
            INITIALIZED = True
            return None

        api        = API(TWSCRAPE_DB_PATH)
        configured = _discover_configured_accounts()
        print(f'[twitter] get_api: discovered {len(configured)} configured account(s)')

        if not configured:
            print('[twitter] No credentials configured — verification disabled')
            INITIALIZED  = True
            API_INSTANCE = api
            return api

        from database import (
            list_twitter_accounts,
            upsert_twitter_account_slot,
            set_twitter_account_active,
        )

        for acc in configured:
            print(f'[twitter] get_api: slot {acc["slot"]} username={acc["username"]} mode={acc["auth_mode"]}')
            upsert_twitter_account_slot(acc['slot'], acc['username'])

        db_rows    = list_twitter_accounts()
        any_active = any(r['active'] for r in db_rows)
        if not any_active and configured:
            set_twitter_account_active(configured[0]['slot'], 1)
            print(f"[twitter] Auto-activated slot {configured[0]['slot']} ({configured[0]['username']}) — first run")
            db_rows = list_twitter_accounts()

        active_slots = {r['slot'] for r in db_rows if r['active']}
        print(f'[twitter] get_api: active slots = {active_slots}')

        for acc in configured:
            if acc['slot'] not in active_slots:
                print(f'[twitter] slot {acc["slot"]} ({acc["username"]}) not active — skipping')
                continue

            auth_mode = acc.get('auth_mode', 'password')

            if auth_mode == 'cookies':
                cookie_str = f"ct0={acc['ct0']}; auth_token={acc['auth_token']}"

                # Remove any stale pool row so re-add with fresh cookies is accepted
                try:
                    await api.pool.delete_accounts(acc['username'])
                    print(f'[twitter] deleted stale pool row for {acc["username"]}')
                except Exception as e:
                    print(f'[twitter] delete_accounts note (slot {acc["slot"]}): {type(e).__name__}: {e}')

                # add_account with cookies kwarg — the correct twscrape 0.17 API
                try:
                    placeholder_email = acc.get('email') or f"{acc['username']}@placeholder.local"
                    placeholder_pw    = acc.get('password') or 'placeholder_pw_unused'
                    await api.pool.add_account(
                        acc['username'], placeholder_pw,
                        placeholder_email, placeholder_pw,
                        cookies=cookie_str,
                    )
                    print(f'[twitter] add_account(cookies=...) OK: slot {acc["slot"]} ({acc["username"]})')
                except Exception as e:
                    print(f'[twitter] add_account(cookies=...) FAILED slot {acc["slot"]}: {type(e).__name__}: {e}')
                    _tb.print_exc()

            else:
                # Password mode — add account then login
                try:
                    await api.pool.add_account(
                        acc['username'], acc['password'],
                        acc['email'],    acc['password'],
                    )
                    print(f'[twitter] add_account OK: slot {acc["slot"]} ({acc["username"]})')
                except Exception as e:
                    print(f'[twitter] add_account note (slot {acc["slot"]}): {type(e).__name__}: {e}')

                print(f'[twitter] password-mode slot {acc["slot"]} ({acc["username"]}) — calling login_all()...')
                try:
                    await api.pool.login_all()
                    print(f'[twitter] login_all returned for slot {acc["slot"]}')
                except Exception as e:
                    print(f'[twitter] login_all FAILED slot {acc["slot"]}: {type(e).__name__}: {e}')
                    _tb.print_exc()

        # Verify auth actually works with a known public account
        try:
            print('[twitter] testing API — looking up @twitter...')
            test_user = await api.user_by_login('twitter')
            if test_user:
                print(f'[twitter] API TEST OK: got @{test_user.username} (id={test_user.id})')
            else:
                print('[twitter] API TEST: returned None — auth may not be working')
        except Exception as e:
            print(f'[twitter] API TEST FAILED: {type(e).__name__}: {e}')
            _tb.print_exc()

        INITIALIZED  = True
        API_INSTANCE = api
        print('[twitter] get_api: initialization complete, API_INSTANCE set')
        return api


async def reload_api():
    """Force re-initialization of the twscrape pool (call after admin changes active slots)."""
    global API_INSTANCE, INITIALIZED
    print('[twitter] reload_api: resetting state')
    async with _init_lock:
        API_INSTANCE = None
        INITIALIZED  = False
    return await get_api()


def normalize_username(u: str) -> str:
    return (u or '').lstrip('@').strip().lower()


async def check_comment(tweet_id: str, target_username: str) -> dict:
    """Check if target_username commented on tweet_id.

    Returns verified=True (found), verified=False (conclusively not found, ≥5 replies fetched),
    or verified=None (inconclusive — any error, rate limit, or too few replies).
    """
    print(f'[twitter] check_comment called: tweet_id={tweet_id} target={target_username}')
    target = normalize_username(target_username)
    if not target:
        print('[twitter] check_comment: empty target — inconclusive')
        r = {'verified': None, 'reason': 'no_username'}
        _update_scraping_health(r)
        return r
    try:
        api = await get_api()
        if api is None:
            print('[twitter] check_comment: api is None — verification disabled')
            r = {'verified': None, 'reason': 'verification_disabled'}
            _update_scraping_health(r)
            return r
        print(f'[twitter] check_comment: fetching replies for tweet {tweet_id}...')
        from twscrape import gather
        replies = await gather(api.tweet_replies(int(tweet_id), limit=200))
        print(f'[twitter] check_comment: got {len(replies)} replies')
        for reply in replies:
            reply_user = normalize_username(getattr(reply.user, 'username', ''))
            if reply_user == target:
                print(f'[twitter] check_comment: MATCH found for {target}')
                r = {'verified': True, 'reason': 'found_comment'}
                _update_scraping_health(r)
                return r
        if len(replies) < 5:
            # Very few replies is suspicious — likely rate-limited or auth issue
            print(f'[twitter] check_comment: only {len(replies)} replies — treating as inconclusive')
            r = {'verified': None, 'reason': f'insufficient_replies:{len(replies)}'}
            _update_scraping_health(r)
            return r
        print(f'[twitter] check_comment: no match for {target} in {len(replies)} replies — confirmed absent')
        r = {'verified': False, 'reason': 'no_comment_found'}
        _update_scraping_health(r)
        return r
    except Exception as e:
        print(f'[twitter] check_comment EXCEPTION: {type(e).__name__}: {e}')
        _tb.print_exc()
        r = {'verified': None, 'reason': f'scrape_error:{type(e).__name__}'}
        _update_scraping_health(r)
        return r


async def check_retweet(tweet_id: str, target_username: str) -> dict:
    """Check if target_username retweeted tweet_id.

    Returns verified=True (found), verified=False (timeline non-empty, no retweet found),
    or verified=None (inconclusive — any error, empty timeline, user not found, etc.).
    """
    print(f'[twitter] check_retweet called: tweet_id={tweet_id} target={target_username}')
    target = normalize_username(target_username)
    if not target:
        print('[twitter] check_retweet: empty target — inconclusive')
        r = {'verified': None, 'reason': 'no_username'}
        _update_scraping_health(r)
        return r
    try:
        api = await get_api()
        if api is None:
            print('[twitter] check_retweet: api is None — verification disabled')
            r = {'verified': None, 'reason': 'verification_disabled'}
            _update_scraping_health(r)
            return r
        print(f'[twitter] check_retweet: looking up user {target}...')
        user = await api.user_by_login(target)
        if not user:
            # Could be auth failure returning null, not definitive "account doesn't exist"
            print(f'[twitter] check_retweet: user {target} lookup returned None — inconclusive')
            r = {'verified': None, 'reason': 'user_not_found'}
            _update_scraping_health(r)
            return r
        print(f'[twitter] check_retweet: user found id={user.id}, fetching timeline...')
        from twscrape import gather
        tweets = await gather(api.user_tweets(user.id, limit=200))
        print(f'[twitter] check_retweet: got {len(tweets)} tweets in timeline')
        for tw in tweets:
            rt = getattr(tw, 'retweetedTweet', None)
            if rt and str(rt.id) == str(tweet_id):
                print(f'[twitter] check_retweet: MATCH found — tweet {tweet_id} in timeline')
                r = {'verified': True, 'reason': 'found_retweet'}
                _update_scraping_health(r)
                return r
        if len(tweets) == 0:
            # Empty timeline is suspicious — likely auth/rate-limit issue
            print(f'[twitter] check_retweet: empty timeline — treating as inconclusive')
            r = {'verified': None, 'reason': 'empty_timeline'}
            _update_scraping_health(r)
            return r
        print(f'[twitter] check_retweet: tweet {tweet_id} not in {len(tweets)}-tweet timeline — confirmed absent')
        r = {'verified': False, 'reason': 'no_retweet_found'}
        _update_scraping_health(r)
        return r
    except Exception as e:
        print(f'[twitter] check_retweet EXCEPTION: {type(e).__name__}: {e}')
        _tb.print_exc()
        r = {'verified': None, 'reason': f'scrape_error:{type(e).__name__}'}
        _update_scraping_health(r)
        return r


async def lookup_twitter_user_by_login(username: str) -> dict | None:
    """Get a Twitter user's basic info by username. Returns None if not found or error."""
    print(f'[twitter] lookup_by_login: username={username}')
    try:
        api = await get_api()
        if api is None:
            print('[twitter] lookup_by_login: api is None')
            return None
        cleaned = normalize_username(username)
        print(f'[twitter] lookup_by_login: calling api.user_by_login({cleaned!r})...')
        user = await api.user_by_login(cleaned)
        if user:
            print(f'[twitter] lookup_by_login: found id={user.id} username={user.username}')
            return {
                'id':           str(user.id),
                'username':     user.username,
                'display_name': getattr(user, 'displayname', ''),
            }
        print(f'[twitter] lookup_by_login: no user returned for {cleaned!r}')
        return None
    except Exception as e:
        print(f'[twitter] lookup_by_login EXCEPTION: {type(e).__name__}: {e}')
        _tb.print_exc()
        return None


async def lookup_twitter_user_by_id(user_id: str) -> dict | None:
    """Get a Twitter user's basic info by numeric user ID. Returns None if not found or error."""
    print(f'[twitter] lookup_by_id: user_id={user_id}')
    try:
        api = await get_api()
        if api is None:
            print('[twitter] lookup_by_id: api is None')
            return None
        user = await api.user_by_id(int(user_id))
        if user:
            print(f'[twitter] lookup_by_id: found username={user.username}')
            return {
                'id':           str(user.id),
                'username':     user.username,
                'display_name': getattr(user, 'displayname', ''),
            }
        print(f'[twitter] lookup_by_id: no user returned for id={user_id}')
        return None
    except Exception as e:
        print(f'[twitter] lookup_by_id EXCEPTION: {type(e).__name__}: {e}')
        _tb.print_exc()
        return None


def extract_tweet_id(url: str) -> str | None:
    m = re.search(r'/status/(\d+)', url or '')
    return m.group(1) if m else None
