"""
One off cleanup for engage_submissions rows whose tweet_url was stored with
extra text in front of the URL, for example a copied timestamp like
'Saturday, June 13, 2026 12:29 https://x.com/.../status/...'. Those values
break the Discord markdown link in the /engage list because of the embedded
spaces.

This rewrites each affected tweet_url to the canonical
https://x.com/<author>/status/<id> form using the same extractors the bot
uses, leaving already clean rows untouched.

Usage:
  python scripts/fix_engage_tweet_urls.py            # dry run, prints changes
  python scripts/fix_engage_tweet_urls.py --apply    # write the changes

DB_PATH env var selects the database file, same as the bot (default
ameretaverse.db). Point it at the production database before running --apply.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.engage import _clean_tweet_url

DB_PATH = os.getenv('DB_PATH', 'ameretaverse.db')


def main():
    apply = '--apply' in sys.argv[1:]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute(
        'SELECT submission_id, tweet_url FROM engage_submissions'
    ).fetchall()

    changed = 0
    for row in rows:
        old = row['tweet_url'] or ''
        new = _clean_tweet_url(old)
        if new != old:
            changed += 1
            print(f'submission_id={row["submission_id"]}')
            print(f'  old: {old!r}')
            print(f'  new: {new!r}')
            if apply:
                cur.execute(
                    'UPDATE engage_submissions SET tweet_url = ? WHERE submission_id = ?',
                    (new, row['submission_id']),
                )

    if apply:
        conn.commit()
        print(f'Applied {changed} update(s) to {DB_PATH}.')
    else:
        print(f'Dry run over {DB_PATH}: {changed} row(s) would change. '
              f'Re run with --apply to write them.')

    conn.close()


if __name__ == '__main__':
    main()
