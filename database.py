import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'ameretaverse.db')

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id          INTEGER PRIMARY KEY,
                username         TEXT NOT NULL,
                total_points     INTEGER NOT NULL DEFAULT 0,
                x_username       TEXT,
                x_username_set_at TIMESTAMP,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS raids (
                raid_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                tweet_link    TEXT NOT NULL,
                tweet_content TEXT,
                tweet_image   TEXT,
                tasks         TEXT,
                total_points  INTEGER NOT NULL DEFAULT 0,
                mode          TEXT NOT NULL DEFAULT 'all',
                created_by    INTEGER NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active        INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS raid_participation (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                raid_id         INTEGER NOT NULL,
                user_id         INTEGER NOT NULL,
                tasks_completed TEXT,
                points_earned   INTEGER NOT NULL DEFAULT 0,
                confirmed_at    TIMESTAMP,
                flagged         INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (raid_id) REFERENCES raids(raid_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            -- ── Engage-for-Engage tables (regular users) ─────────────
            CREATE TABLE IF NOT EXISTS engage_links (
                link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                tweet_link      TEXT NOT NULL,
                source          TEXT NOT NULL DEFAULT 'submit',
                submitted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at      TIMESTAMP NOT NULL,
                active          INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS engage_participation (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id         INTEGER NOT NULL,
                user_id         INTEGER NOT NULL,
                tasks_completed TEXT,
                points_earned   INTEGER NOT NULL DEFAULT 0,
                confirmed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (link_id) REFERENCES engage_links(link_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            -- ── Engage-for-Engage tables (creators) ──────────────────
            CREATE TABLE IF NOT EXISTS creator_engage_links (
                link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                tweet_link      TEXT NOT NULL,
                source          TEXT NOT NULL DEFAULT 'submit',
                submitted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at      TIMESTAMP NOT NULL,
                active          INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS creator_engage_participation (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id         INTEGER NOT NULL,
                user_id         INTEGER NOT NULL,
                tasks_completed TEXT,
                points_earned   INTEGER NOT NULL DEFAULT 0,
                confirmed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (link_id) REFERENCES creator_engage_links(link_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            -- ── Config table (central settings) ─────────────────────
            CREATE TABLE IF NOT EXISTS config (
                key             TEXT PRIMARY KEY,
                value           TEXT NOT NULL
            );
        """)

        # Default config values (only inserted if not already set)
        defaults = {
            # regular user pool
            "engage_link_lifetime_hours": "24",
            "engage_links_per_request": "10",
            "engage_daily_limit": "0",
            "engage_submit_cost": "0",
            "engage_weight_like": "12.5",
            "engage_weight_comment": "40.0",
            "engage_weight_retweet": "47.5",
            "engage_points_per_link": "10",
            # creator pool
            "creator_engage_link_lifetime_hours": "24",
            "creator_engage_links_per_request": "10",
            "creator_engage_points_per_link": "10",
            "creator_engage_weight_like": "12.5",
            "creator_engage_weight_comment": "40.0",
            "creator_engage_weight_retweet": "47.5",
        }
        for k, v in defaults.items():
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v)
                )
            except sqlite3.OperationalError:
                pass

        # Migrations for existing databases
        for migration in [
            "ALTER TABLE raids ADD COLUMN mode TEXT NOT NULL DEFAULT 'all'",
            "ALTER TABLE users ADD COLUMN x_username TEXT",
            "ALTER TABLE users ADD COLUMN x_username_set_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN engage_points INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN creator_engage_points INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists

if __name__ == '__main__':
    init_db()
    print(f'Database initialized at {DB_PATH}')
