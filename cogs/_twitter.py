"""
Twitter scraping via twscrape.
Uses credentials from env vars; session persists in /data/twscrape.db.
"""
import os
import asyncio
import re

API_INSTANCE = None
INITIALIZED = False
_init_lock = asyncio.Lock()

TWITTER_USERNAME     = os.getenv('TWITTER_USERNAME', '')
TWITTER_EMAIL        = os.getenv('TWITTER_EMAIL', '')
TWITTER_PASSWORD     = os.getenv('TWITTER_PASSWORD', '')
TWITTER_EMAIL_PASSWD = os.getenv('TWITTER_EMAIL_PASSWORD', TWITTER_PASSWORD)
TWSCRAPE_DB_PATH     = os.getenv('TWSCRAPE_DB_PATH', '/data/twscrape.db')


async def get_api():
    global API_INSTANCE, INITIALIZED
    async with _init_lock:
        if INITIALIZED and API_INSTANCE is not None:
            return API_INSTANCE
        try:
            from twscrape import API
            api = API(TWSCRAPE_DB_PATH)
            if not (TWITTER_USERNAME and TWITTER_EMAIL and TWITTER_PASSWORD):
                print('[twitter] No credentials configured — verification disabled')
                INITIALIZED = True
                API_INSTANCE = api
                return api
            try:
                await api.pool.add_account(
                    TWITTER_USERNAME, TWITTER_PASSWORD,
                    TWITTER_EMAIL, TWITTER_EMAIL_PASSWD,
                )
                print(f'[twitter] Added account {TWITTER_USERNAME}, logging in...')
                await api.pool.login_all()
            except Exception as e:
                print(f'[twitter] Account setup note: {e}')
            INITIALIZED = True
            API_INSTANCE = api
            return api
        except ImportError:
            print('[twitter] twscrape not installed — verification disabled')
            INITIALIZED = True
            return None


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


def extract_tweet_id(url: str) -> str | None:
    m = re.search(r'/status/(\d+)', url or '')
    return m.group(1) if m else None
