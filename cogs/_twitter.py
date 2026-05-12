"""
Twitter scraping via twscrape — multi-account pool with active/backup slots.

Env vars (slots 1–5):
  TWITTER_ACCOUNT_1_USERNAME, TWITTER_ACCOUNT_1_EMAIL, TWITTER_ACCOUNT_1_PASSWORD
  TWITTER_ACCOUNT_2_USERNAME, TWITTER_ACCOUNT_2_EMAIL, TWITTER_ACCOUNT_2_PASSWORD
  ... up to TWITTER_ACCOUNT_5_*

Active status is stored in the twitter_accounts DB table (admin-managed).
Only slots with active=1 in DB are added to the twscrape pool.

Backward compat: if no numbered accounts are set but old TWITTER_USERNAME/EMAIL/PASSWORD
env vars exist, they are treated as slot 1 and auto-activated on first run.
"""
import os
import asyncio
import re

API_INSTANCE = None
INITIALIZED  = False
_init_lock   = asyncio.Lock()

TWSCRAPE_DB_PATH = os.getenv('TWSCRAPE_DB_PATH', '/data/twscrape.db')


def _get_slot_credentials(slot: int) -> dict | None:
    prefix   = f'TWITTER_ACCOUNT_{slot}_'
    username = (os.getenv(f'{prefix}USERNAME') or '').strip()
    email    = (os.getenv(f'{prefix}EMAIL')    or '').strip()
    password = (os.getenv(f'{prefix}PASSWORD') or '').strip()
    if not (username and email and password):
        return None
    return {'slot': slot, 'username': username, 'email': email, 'password': password}


def _get_legacy_credentials() -> dict | None:
    username = (os.getenv('TWITTER_USERNAME') or '').strip()
    email    = (os.getenv('TWITTER_EMAIL')    or '').strip()
    password = (os.getenv('TWITTER_PASSWORD') or '').strip()
    if not (username and email and password):
        return None
    return {'slot': 1, 'username': username, 'email': email, 'password': password}


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
    async with _init_lock:
        if INITIALIZED and API_INSTANCE is not None:
            return API_INSTANCE

        try:
            from twscrape import API
        except ImportError:
            print('[twitter] twscrape not installed — verification disabled')
            INITIALIZED = True
            return None

        api         = API(TWSCRAPE_DB_PATH)
        configured  = _discover_configured_accounts()

        if not configured:
            print('[twitter] No credentials configured — verification disabled')
            INITIALIZED = True
            API_INSTANCE = api
            return api

        from database import (
            list_twitter_accounts,
            upsert_twitter_account_slot,
            set_twitter_account_active,
        )

        # Sync DB rows for every configured slot so the admin panel shows them
        for acc in configured:
            upsert_twitter_account_slot(acc['slot'], acc['username'])

        # Auto-activate slot 1 on first run if nothing is active yet
        db_rows    = list_twitter_accounts()
        any_active = any(r['active'] for r in db_rows)
        if not any_active and configured:
            set_twitter_account_active(configured[0]['slot'], 1)
            print(f"[twitter] Auto-activated slot {configured[0]['slot']} ({configured[0]['username']}) — first run")
            db_rows = list_twitter_accounts()

        active_slots = {r['slot'] for r in db_rows if r['active']}

        # Find which usernames are already registered with twscrape
        try:
            pool_accounts      = await api.pool.accounts_info()
            existing_usernames = {a.username.lower() for a in pool_accounts}
        except Exception:
            existing_usernames = set()

        added = 0
        for acc in configured:
            if acc['slot'] not in active_slots:
                continue
            if acc['username'].lower() in existing_usernames:
                print(f"[twitter] Slot {acc['slot']} ({acc['username']}) already in pool")
                continue
            try:
                await api.pool.add_account(
                    acc['username'], acc['password'],
                    acc['email'],    acc['password'],
                )
                added += 1
                print(f"[twitter] Added slot {acc['slot']} ({acc['username']}) to pool")
            except Exception as e:
                print(f"[twitter] Slot {acc['slot']} add error: {type(e).__name__}: {e}")

        if added > 0:
            print('[twitter] Logging in newly added accounts...')
            try:
                await api.pool.login_all()
            except Exception as e:
                print(f'[twitter] login_all warning: {type(e).__name__}: {e}')

        INITIALIZED  = True
        API_INSTANCE = api
        return api


async def reload_api():
    """Force re-initialization of the twscrape pool (call after admin changes active slots)."""
    global API_INSTANCE, INITIALIZED
    async with _init_lock:
        API_INSTANCE = None
        INITIALIZED  = False
    return await get_api()


def normalize_username(u: str) -> str:
    return (u or '').lstrip('@').strip().lower()


async def check_comment(tweet_id: str, target_username: str) -> dict:
    """Check if target_username commented on tweet_id."""
    target = normalize_username(target_username)
    if not target:
        return {'verified': False, 'reason': 'no_username'}
    try:
        api = await get_api()
        if api is None:
            return {'verified': None, 'reason': 'verification_disabled'}
        from twscrape import gather
        replies = await gather(api.tweet_replies(int(tweet_id), limit=200))
        for reply in replies:
            if normalize_username(getattr(reply.user, 'username', '')) == target:
                return {'verified': True, 'reason': 'found_comment'}
        return {'verified': False, 'reason': 'no_comment_found'}
    except Exception as e:
        print(f'[twitter] check_comment error: {type(e).__name__}: {e}')
        return {'verified': None, 'reason': f'scrape_error:{type(e).__name__}'}


async def check_retweet(tweet_id: str, target_username: str) -> dict:
    """Check if target_username retweeted tweet_id."""
    target = normalize_username(target_username)
    if not target:
        return {'verified': False, 'reason': 'no_username'}
    try:
        api = await get_api()
        if api is None:
            return {'verified': None, 'reason': 'verification_disabled'}
        from twscrape import gather
        user = await api.user_by_login(target)
        if not user:
            return {'verified': False, 'reason': 'user_not_found'}
        tweets = await gather(api.user_tweets(user.id, limit=200))
        for tw in tweets:
            rt = getattr(tw, 'retweetedTweet', None)
            if rt and str(rt.id) == str(tweet_id):
                return {'verified': True, 'reason': 'found_retweet'}
        return {'verified': False, 'reason': 'no_retweet_found'}
    except Exception as e:
        print(f'[twitter] check_retweet error: {type(e).__name__}: {e}')
        return {'verified': None, 'reason': f'scrape_error:{type(e).__name__}'}


async def lookup_twitter_user_by_login(username: str) -> dict | None:
    """Get a Twitter user's basic info by username. Returns None if not found or error."""
    try:
        api = await get_api()
        if api is None:
            return None
        user = await api.user_by_login(normalize_username(username))
        if user:
            return {
                'id': str(user.id),
                'username': user.username,
                'display_name': getattr(user, 'displayname', ''),
            }
        return None
    except Exception as e:
        print(f'[twitter] lookup_by_login error: {type(e).__name__}: {e}')
        return None


async def lookup_twitter_user_by_id(user_id: str) -> dict | None:
    """Get a Twitter user's basic info by numeric user ID. Returns None if not found or error."""
    try:
        api = await get_api()
        if api is None:
            return None
        user = await api.user_by_id(int(user_id))
        if user:
            return {
                'id': str(user.id),
                'username': user.username,
                'display_name': getattr(user, 'displayname', ''),
            }
        return None
    except Exception as e:
        print(f'[twitter] lookup_by_id error: {type(e).__name__}: {e}')
        return None


def extract_tweet_id(url: str) -> str | None:
    m = re.search(r'/status/(\d+)', url or '')
    return m.group(1) if m else None
