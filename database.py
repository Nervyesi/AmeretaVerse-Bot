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

            -- ── Protection action log ─────────────────────────────────
            CREATE TABLE IF NOT EXISTS protection_actions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                user_id     INTEGER NOT NULL DEFAULT 0,
                detail      TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- ── OAuth sessions (dashboard login) ─────────────────────
            CREATE TABLE IF NOT EXISTS oauth_sessions (
                user_id       INTEGER PRIMARY KEY,
                access_token  TEXT NOT NULL,
                refresh_token TEXT NOT NULL DEFAULT '',
                expires_at    TEXT NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- ── Analytics snapshots (daily rolled-up stats) ───────────
            CREATE TABLE IF NOT EXISTS analytics_snapshots (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id          INTEGER NOT NULL,
                snapshot_date     DATE NOT NULL,
                member_count      INTEGER DEFAULT 0,
                online_count      INTEGER DEFAULT 0,
                verified_count    INTEGER DEFAULT 0,
                message_count_24h INTEGER DEFAULT 0,
                voice_minutes_24h INTEGER DEFAULT 0,
                joins_24h         INTEGER DEFAULT 0,
                leaves_24h        INTEGER DEFAULT 0,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, snapshot_date)
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_guild_date
                ON analytics_snapshots(guild_id, snapshot_date DESC);

            -- ── Intraday counters (auto-cleaned after 3 days) ─────────
            CREATE TABLE IF NOT EXISTS message_counters (
                guild_id      INTEGER NOT NULL,
                date          DATE NOT NULL,
                message_count INTEGER DEFAULT 0,
                joins         INTEGER DEFAULT 0,
                leaves        INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, date)
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
            # ── Protection module defaults ──────────────────────────
            "protection_link_detection":          "1",
            "protection_link_whitelist":           "twitter.com,x.com,discord.gg,youtube.com",
            "protection_spam_detection":           "1",
            "protection_spam_threshold":           "5",
            "protection_spam_window":              "10",
            "protection_suspicious_users":         "1",
            "protection_suspicious_action":        "flag",
            "protection_suspicious_account_age":   "7",
            "protection_phishing_detection":       "1",
            "protection_anti_raid":                "1",
            "protection_anti_raid_threshold":      "10",
            "protection_anti_raid_window":         "60",
            "protection_banned_words":             "0",
            "protection_banned_words_list":        "",
            "protection_log_channel":              "mod-log",
            "protection_mute_role":                "Muted",
            # ── Protection action config ────────────────────────────────
            "protection_link_action":              "delete",
            "protection_spam_action":              "mute",
            "protection_spam_mute_duration":       "600",
            "protection_banned_words_action":      "delete",
            "protection_phishing_action":          "delete",
            "protection_phishing_list": (
                "discorcl.com,discordc.com,dlscord.com,discrod.com,disc0rd.com,"
                "discordd.com,discordapp.co,discord-gift.com,discord-nitro.com,"
                "discordnitro.gift,free-nitro.com,steamcommunity.ru,steampowered.ru,"
                "csgo-skins.com,nft-free-mint.com,free-nft.io,opensea-drop.com,"
                "metamask-airdrop.com,airdrop-claim.io,walletconnect.services,"
                "claimrewards.xyz,claim-nft.site,free-airdrop.net"
            ),
            "protection_suspicious_no_avatar":     "1",
            "protection_suspicious_username_keywords": "1",
            "protection_suspicious_bio_keywords":  "0",
            "protection_suspicious_keywords_list": (
                "admin,mod,moderator,support,assistance,helpdesk,help-desk,"
                "official,giveaway,airdrop,free mint,freemint,nft drop,nftdrop,staff,team"
            ),
            "protection_anti_raid_action":         "lockdown",
            "protection_dm_on_action":             "0",
            "protection_dm_link_message":          "Your link was removed because it's not whitelisted on this server.",
            "protection_dm_spam_message":          "You were muted for spamming. Duration: {duration}s.",
            "protection_dm_banned_word_message":   "Your message contained a banned word and was removed.",
            "protection_dm_phishing_message":      "Your message was removed — it contained a phishing link.",
            "protection_dm_suspicious_message":    "Your account was flagged due to suspicious characteristics.",
            "protection_main_embed_title":         "\U0001f6e1\ufe0f Server Protection",
            "protection_main_embed_description": (
                "This server is protected by AVbot. Attempting spam, phishing, "
                "raids, or abuse will result in automated action."
            ),
            "protection_main_embed_channel":       "",
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
