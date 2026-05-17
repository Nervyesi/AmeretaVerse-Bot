import sqlite3
import os
import shutil

DB_PATH = os.getenv("DB_PATH", "ameretaverse.db")

DEFAULT_CONFIG = {
    # regular user pool
    "engage_link_lifetime_hours":        "24",
    "engage_links_per_request":          "10",
    "engage_daily_limit":                "0",
    "engage_submit_cost":                "0",
    "engage_weight_like":                "12.5",
    "engage_weight_comment":             "40.0",
    "engage_weight_retweet":             "47.5",
    "engage_points_per_link":            "10",
    # creator pool
    "creator_engage_link_lifetime_hours": "24",
    "creator_engage_links_per_request":   "10",
    "creator_engage_points_per_link":     "10",
    "creator_engage_weight_like":         "12.5",
    "creator_engage_weight_comment":      "40.0",
    "creator_engage_weight_retweet":      "47.5",
    # ── Protection module defaults ──────────────────────────────────────────
    "protection_link_detection":          "1",
    "protection_link_whitelist":          "twitter.com,x.com,discord.gg,youtube.com",
    "protection_spam_detection":          "1",
    "protection_spam_threshold":          "5",
    "protection_spam_window":             "10",
    "protection_suspicious_users":        "1",
    "protection_suspicious_action":       "flag",
    "protection_suspicious_account_age":  "7",
    "protection_phishing_detection":      "1",
    "protection_anti_raid":               "1",
    "protection_anti_raid_threshold":     "10",
    "protection_anti_raid_window":        "60",
    "protection_banned_words":            "0",
    "protection_banned_words_list":       "",
    "protection_log_channel":             "mod-log",
    "protection_mute_role":               "Muted",
    "protection_link_action":             "delete",
    "protection_spam_action":             "mute",
    "protection_spam_mute_duration":      "600",
    "protection_banned_words_action":     "delete",
    "protection_phishing_action":         "delete",
    "protection_phishing_list": (
        "discorcl.com,discordc.com,dlscord.com,discrod.com,disc0rd.com,"
        "discordd.com,discordapp.co,discord-gift.com,discord-nitro.com,"
        "discordnitro.gift,free-nitro.com,steamcommunity.ru,steampowered.ru,"
        "csgo-skins.com,nft-free-mint.com,free-nft.io,opensea-drop.com,"
        "metamask-airdrop.com,airdrop-claim.io,walletconnect.services,"
        "claimrewards.xyz,claim-nft.site,free-airdrop.net"
    ),
    "protection_suspicious_no_avatar":        "1",
    "protection_suspicious_username_keywords": "1",
    "protection_suspicious_bio_keywords":     "0",
    "protection_suspicious_keywords_list": (
        "admin,mod,moderator,support,assistance,helpdesk,help-desk,"
        "official,giveaway,airdrop,free mint,freemint,nft drop,nftdrop,staff,team"
    ),
    "protection_anti_raid_action":            "lockdown",
    "protection_dm_on_action":                "0",
    "protection_dm_link_message":             "Your link was removed because it's not whitelisted on this server.",
    "protection_dm_spam_message":             "You were muted for spamming. Duration: {duration}s.",
    "protection_dm_banned_word_message":      "Your message contained a banned word and was removed.",
    "protection_dm_phishing_message":         "Your message was removed — it contained a phishing link.",
    "protection_dm_suspicious_message":       "Your account was flagged due to suspicious characteristics.",
    "protection_main_embed_title":            "\U0001f6e1\ufe0f Server Protection",
    "protection_main_embed_description": (
        "This server is protected by AVbot. Attempting spam, phishing, "
        "raids, or abuse will result in automated action."
    ),
    "protection_main_embed_channel":          "",
    # ── Analytics tracking markers ─────────────────────────────────────────
    "analytics_leaves_tracking_started":      "",
    # ── Tickets module defaults ────────────────────────────────────────────
    "tickets_enabled":                        "0",
    "tickets_panel_channel":                  "",
    "tickets_panel_title":                    "Support Tickets",
    "tickets_panel_description":              (
        "Need help? Click the button below to open a support ticket. "
        "A staff member will assist you shortly."
    ),
    "tickets_panel_button_label":             "Open Ticket",
    "tickets_category":                       "",
    "tickets_staff_roles":                    "",
    "tickets_ping_role":                      "",
    "tickets_welcome_message":                (
        "Hi {user}, thanks for opening a ticket. A staff member will be "
        "with you shortly. Please describe your issue in detail."
    ),
    "tickets_archive_channel":                "",
    "tickets_auto_close_enabled":             "1",
    "tickets_auto_close_warning_hours":       "48",
    "tickets_auto_close_final_hours":         "72",
    "tickets_auto_close_warning_message":     (
        "⏰ This ticket has been inactive for 48 hours. "
        "It will be auto-closed in 24 hours unless someone responds."
    ),
    "tickets_dm_on_open_enabled":             "1",
    "tickets_dm_on_open_message":             (
        "Your support ticket has been opened in {server}. "
        "We'll be in touch soon."
    ),
    "tickets_dm_on_close_enabled":            "1",
    "tickets_dm_on_close_message":            (
        "Your support ticket in {server} has been closed. "
        "If you need further help, feel free to open a new one."
    ),
    # ── Verification module defaults ───────────────────────────────────────────
    "verify_enabled":               "1",
    "verify_channel":               "",
    "verify_success_role":          "Verified",
    "verify_max_attempts":          "3",
    "verify_embed_title":           "\U0001f512 Verify to Enter",
    "verify_embed_description":     "Click the button below and solve the CAPTCHA to access the server.",
    "verify_embed_button_label":    "Verify",
    "verify_wrong_attempt_message": "❌ Wrong! You have {remaining} attempts left.",
    "verify_last_chance_message":   "⚠️ Last chance! Get this one wrong and you'll be kicked.",
    "verify_kicked_message":        "You've been kicked for failing verification. You can rejoin and try again.",
    "verify_success_message":       "✅ Verified! Welcome to the server.",
    "verify_dm_on_success_enabled": "1",
    "verify_dm_on_success_message": "Welcome! You've been verified in {server}.",
    "verify_dm_on_kick_enabled":    "1",
    "verify_dm_on_kick_message":    "You were kicked from {server} for failing CAPTCHA. Feel free to try again.",
    # ── Brand / visual customization (plan-gated) ──────────────────────────────
    "server_plan":              "free",          # 'free' | 'premium' | 'premium_plus'
    "brand_color":              "",              # hex like "#94730D" — empty = AVbot default
    "brand_thumbnail_url":      "",
    "brand_image_url":          "",
    "brand_footer_text":        "",
    "brand_footer_icon_url":    "",
    "brand_author_name":        "",
    "brand_author_icon_url":    "",
    # per-cog overrides (fall back to brand_* then to BRAND_DEFAULTS)
    "verify_color":             "",
    "verify_thumbnail_url":     "",
    "verify_image_url":         "",
    "verify_footer_text":       "",
    "verify_footer_icon_url":   "",
    "tickets_color":            "",
    "tickets_thumbnail_url":    "",
    "tickets_image_url":        "",
    "tickets_footer_text":      "",
    "roleselect_color":         "",
    "roleselect_thumbnail_url": "",
    "roleselect_image_url":     "",
    "roleselect_footer_text":   "",
}


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_to_volume():
    target = os.getenv("DB_PATH", "ameretaverse.db")
    legacy = "ameretaverse.db"
    if target != legacy and not os.path.exists(target) and os.path.exists(legacy):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(legacy, target)
        print(f"Migrated database from {legacy} to {target}")


def init_db():
    _migrate_to_volume()
    with get_connection() as conn:
        # ── Migrate old global config table to per-guild if needed ─────────
        cols = [r[1] for r in conn.execute("PRAGMA table_info(config)").fetchall()]
        if cols and 'guild_id' not in cols:
            conn.execute("ALTER TABLE config RENAME TO config_old_backup")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id           INTEGER PRIMARY KEY,
                username          TEXT NOT NULL,
                total_points      INTEGER NOT NULL DEFAULT 0,
                x_username        TEXT,
                x_username_set_at TIMESTAMP,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

            CREATE TABLE IF NOT EXISTS engage_links (
                link_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                tweet_link   TEXT NOT NULL,
                source       TEXT NOT NULL DEFAULT 'submit',
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at   TIMESTAMP NOT NULL,
                active       INTEGER NOT NULL DEFAULT 1,
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

            CREATE TABLE IF NOT EXISTS creator_engage_links (
                link_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                tweet_link   TEXT NOT NULL,
                source       TEXT NOT NULL DEFAULT 'submit',
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at   TIMESTAMP NOT NULL,
                active       INTEGER NOT NULL DEFAULT 1,
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

            CREATE TABLE IF NOT EXISTS config (
                guild_id   INTEGER NOT NULL,
                key        TEXT NOT NULL,
                value      TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, key)
            );
            CREATE INDEX IF NOT EXISTS idx_config_guild ON config(guild_id);

            CREATE TABLE IF NOT EXISTS protection_actions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                user_id     INTEGER NOT NULL DEFAULT 0,
                detail      TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS oauth_sessions (
                user_id       INTEGER PRIMARY KEY,
                access_token  TEXT NOT NULL,
                refresh_token TEXT NOT NULL DEFAULT '',
                expires_at    TEXT NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

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

            CREATE TABLE IF NOT EXISTS message_counters (
                guild_id      INTEGER NOT NULL,
                date          DATE NOT NULL,
                message_count INTEGER DEFAULT 0,
                joins         INTEGER DEFAULT 0,
                leaves        INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, date)
            );

            CREATE TABLE IF NOT EXISTS message_hourly (
                guild_id INTEGER NOT NULL,
                date     DATE NOT NULL,
                hour     INTEGER NOT NULL,
                count    INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, date, hour)
            );

            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id         INTEGER NOT NULL,
                channel_id       INTEGER NOT NULL,
                user_id          INTEGER NOT NULL,
                username         TEXT,
                opened_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                warned_at        TIMESTAMP,
                closed_at        TIMESTAMP,
                closed_by        INTEGER,
                close_reason     TEXT,
                status           TEXT NOT NULL DEFAULT 'open'
            );
            CREATE INDEX IF NOT EXISTS idx_tickets_guild_status ON tickets(guild_id, status);
            CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets(channel_id);

            CREATE TABLE IF NOT EXISTS roleselect_panels (
                panel_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                channel_id  INTEGER,
                message_id  INTEGER,
                title       TEXT NOT NULL DEFAULT '🎯 Role Selection',
                description TEXT DEFAULT '',
                style       TEXT NOT NULL DEFAULT 'buttons',
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_rs_panels_guild ON roleselect_panels(guild_id);

            CREATE TABLE IF NOT EXISTS roleselect_buttons (
                button_id              INTEGER PRIMARY KEY AUTOINCREMENT,
                panel_id               INTEGER NOT NULL,
                position               INTEGER NOT NULL DEFAULT 0,
                label                  TEXT NOT NULL DEFAULT 'Click me',
                emoji                  TEXT DEFAULT '',
                role                   TEXT NOT NULL,
                mode                   TEXT NOT NULL DEFAULT 'toggle',
                confirm_give_enabled   INTEGER NOT NULL DEFAULT 0,
                confirm_give_message   TEXT DEFAULT 'Are you sure you want this role?',
                confirm_take_enabled   INTEGER NOT NULL DEFAULT 0,
                confirm_take_message   TEXT DEFAULT 'Are you sure you want to remove this role?',
                dm_give_enabled        INTEGER NOT NULL DEFAULT 0,
                dm_give_message        TEXT DEFAULT 'You received the {role} role in {server}.',
                dm_take_enabled        INTEGER NOT NULL DEFAULT 0,
                dm_take_message        TEXT DEFAULT 'You no longer have the {role} role in {server}.',
                FOREIGN KEY (panel_id) REFERENCES roleselect_panels(panel_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_rs_buttons_panel ON roleselect_buttons(panel_id);

            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id TEXT PRIMARY KEY,
                default_embed_color TEXT DEFAULT '#94730D',
                default_thumbnail_url TEXT,
                default_footer_text TEXT,
                default_footer_icon_url TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS guild_module_access (
                guild_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                module TEXT NOT NULL,
                granted INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (guild_id, role_id, module)
            );
            CREATE INDEX IF NOT EXISTS idx_gma_guild ON guild_module_access(guild_id, role_id);
        """)

        for migration in [
            "ALTER TABLE raids ADD COLUMN mode TEXT NOT NULL DEFAULT 'all'",
            "ALTER TABLE users ADD COLUMN x_username TEXT",
            "ALTER TABLE users ADD COLUMN x_username_set_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN engage_points INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN creator_engage_points INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tickets ADD COLUMN display_number INTEGER DEFAULT NULL",
            "ALTER TABLE roleselect_panels ADD COLUMN thumbnail_url TEXT DEFAULT ''",
            "ALTER TABLE roleselect_panels ADD COLUMN image_url TEXT DEFAULT ''",
            "ALTER TABLE roleselect_panels ADD COLUMN color TEXT DEFAULT ''",
            "ALTER TABLE roleselect_panels ADD COLUMN footer_text TEXT DEFAULT ''",
            "ALTER TABLE form_submissions ADD COLUMN display_number INTEGER DEFAULT NULL",
            "ALTER TABLE forms ADD COLUMN auto_close_on_decision INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE raid_settings ADD COLUMN raid_channel_id TEXT DEFAULT ''",
            "ALTER TABLE raid_settings ADD COLUMN raid_ping_role_id TEXT DEFAULT ''",
            "ALTER TABLE raids ADD COLUMN ended_reason TEXT DEFAULT NULL",
            "ALTER TABLE raid_settings ADD COLUMN raid_guide_channel_id TEXT DEFAULT ''",
            "ALTER TABLE raid_settings ADD COLUMN raid_guide_title TEXT DEFAULT ''",
            "ALTER TABLE raid_settings ADD COLUMN raid_guide_description TEXT DEFAULT ''",
            "ALTER TABLE raid_settings ADD COLUMN raid_guide_thumbnail_url TEXT DEFAULT ''",
            "ALTER TABLE raid_settings ADD COLUMN raid_guide_image_url TEXT DEFAULT ''",
            "ALTER TABLE raid_settings ADD COLUMN raid_guide_color TEXT DEFAULT ''",
            "ALTER TABLE raid_settings ADD COLUMN raid_guide_footer_text TEXT DEFAULT ''",
            # engage_pools new columns
            "ALTER TABLE engage_pools ADD COLUMN allowed_role_ids TEXT DEFAULT '[]'",
            "ALTER TABLE engage_pools ADD COLUMN auto_reset_daily INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE engage_pools ADD COLUMN embed_footer_icon_url TEXT",
            # bot profile columns
            "ALTER TABLE guild_settings ADD COLUMN bot_display_name TEXT",
            "ALTER TABLE guild_settings ADD COLUMN bot_avatar_url TEXT",
        ]:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass

        # assets_library table for R2 uploads
        conn.execute("""
            CREATE TABLE IF NOT EXISTS assets_library (
                asset_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id      INTEGER NOT NULL,
                file_id       TEXT NOT NULL,
                key           TEXT NOT NULL,
                url           TEXT NOT NULL,
                original_name TEXT,
                size          INTEGER NOT NULL,
                content_type  TEXT,
                extension     TEXT,
                uploaded_by   INTEGER,
                uploaded_at   TEXT DEFAULT (datetime('now')),
                deleted       INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assets_library_guild "
            "ON assets_library(guild_id, deleted)"
        )

        # ── Forms tables ──────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS forms (
                form_id              INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id             INTEGER NOT NULL,
                name                 TEXT NOT NULL,
                title                TEXT NOT NULL DEFAULT '',
                description          TEXT NOT NULL DEFAULT '',
                button_label         TEXT NOT NULL DEFAULT 'Apply',
                thumbnail_url        TEXT NOT NULL DEFAULT '',
                image_url            TEXT NOT NULL DEFAULT '',
                color                TEXT NOT NULL DEFAULT '',
                footer_text          TEXT NOT NULL DEFAULT '',
                channel_id           TEXT NOT NULL DEFAULT '',
                message_id           TEXT NOT NULL DEFAULT '',
                ticket_category      TEXT NOT NULL DEFAULT '',
                staff_roles          TEXT NOT NULL DEFAULT '',
                ping_role            TEXT NOT NULL DEFAULT '',
                approve_role         TEXT NOT NULL DEFAULT '',
                approve_dm_enabled   INTEGER NOT NULL DEFAULT 0,
                approve_dm_message   TEXT NOT NULL DEFAULT '',
                reject_dm_enabled    INTEGER NOT NULL DEFAULT 0,
                reject_dm_message    TEXT NOT NULL DEFAULT '',
                enabled              INTEGER NOT NULL DEFAULT 1,
                created_at           TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_forms_guild ON forms(guild_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS form_fields (
                field_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                form_id         INTEGER NOT NULL,
                position        INTEGER NOT NULL DEFAULT 0,
                label           TEXT NOT NULL,
                field_type      TEXT NOT NULL DEFAULT 'short_text',
                placeholder     TEXT NOT NULL DEFAULT '',
                required        INTEGER NOT NULL DEFAULT 1,
                options         TEXT NOT NULL DEFAULT '',
                max_length      INTEGER DEFAULT NULL,
                FOREIGN KEY (form_id) REFERENCES forms(form_id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_form_fields_form ON form_fields(form_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS form_submissions (
                submission_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                form_id         INTEGER NOT NULL,
                guild_id        INTEGER NOT NULL,
                user_id         INTEGER NOT NULL,
                username        TEXT NOT NULL DEFAULT '',
                channel_id      TEXT NOT NULL DEFAULT '',
                answers         TEXT NOT NULL DEFAULT '{}',
                status          TEXT NOT NULL DEFAULT 'pending',
                decided_by      INTEGER DEFAULT NULL,
                decided_at      TEXT DEFAULT NULL,
                created_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_form_submissions_lookup "
            "ON form_submissions(form_id, user_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_form_submissions_guild "
            "ON form_submissions(guild_id)"
        )

        # One-time backfill: assign per-guild display numbers to existing tickets
        rows_to_fill = conn.execute(
            "SELECT ticket_id, guild_id FROM tickets "
            "WHERE display_number IS NULL ORDER BY guild_id, ticket_id"
        ).fetchall()
        if rows_to_fill:
            existing_maxes = {
                r['guild_id']: r['max_num'] or 0
                for r in conn.execute(
                    "SELECT guild_id, MAX(display_number) AS max_num FROM tickets "
                    "WHERE display_number IS NOT NULL GROUP BY guild_id"
                ).fetchall()
            }
            counters = dict(existing_maxes)
            updates = []
            for row in rows_to_fill:
                gid = row['guild_id'] or 0
                counters[gid] = counters.get(gid, 0) + 1
                updates.append((counters[gid], row['ticket_id']))
            conn.executemany(
                "UPDATE tickets SET display_number=? WHERE ticket_id=?", updates
            )
            print(f'[migration] backfilled display_number for {len(updates)} tickets')

        # One-time backfill: per-guild display numbers for existing form submissions
        sub_rows_to_fill = conn.execute(
            "SELECT submission_id, guild_id FROM form_submissions "
            "WHERE display_number IS NULL ORDER BY guild_id, submission_id"
        ).fetchall()
        if sub_rows_to_fill:
            existing_sub_maxes = {
                r['guild_id']: r['max_num'] or 0
                for r in conn.execute(
                    "SELECT guild_id, MAX(display_number) AS max_num FROM form_submissions "
                    "WHERE display_number IS NOT NULL GROUP BY guild_id"
                ).fetchall()
            }
            sub_counters = dict(existing_sub_maxes)
            sub_updates = []
            for row in sub_rows_to_fill:
                gid = row['guild_id'] or 0
                sub_counters[gid] = sub_counters.get(gid, 0) + 1
                sub_updates.append((sub_counters[gid], row['submission_id']))
            conn.executemany(
                "UPDATE form_submissions SET display_number=? WHERE submission_id=?",
                sub_updates,
            )
            print(f'[migration] backfilled display_number for {len(sub_updates)} form submissions')

        # One-time migration: convert legacy dropdown/number form_fields to short_text
        result = conn.execute(
            "UPDATE form_fields SET field_type='short_text', options='' "
            "WHERE field_type IN ('number', 'dropdown')"
        )
        if result.rowcount > 0:
            print(f'[migration] Converted {result.rowcount} legacy form_fields to short_text')

        # Idempotent: rename legacy raids/raid_participation (no guild_id) to *_legacy
        try:
            raid_cols = [r[1] for r in conn.execute("PRAGMA table_info(raids)").fetchall()]
            if raid_cols and 'guild_id' not in raid_cols:
                print('[migration] Renaming legacy raids -> raids_legacy')
                conn.execute("ALTER TABLE raids RENAME TO raids_legacy")
        except sqlite3.OperationalError:
            pass
        try:
            rp_cols = [r[1] for r in conn.execute("PRAGMA table_info(raid_participation)").fetchall()]
            if rp_cols and 'guild_id' not in rp_cols:
                print('[migration] Renaming legacy raid_participation -> raid_participation_legacy')
                conn.execute("ALTER TABLE raid_participation RENAME TO raid_participation_legacy")
        except sqlite3.OperationalError:
            pass

        # New per-guild raid tables
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS raids (
                raid_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id        INTEGER NOT NULL,
                display_number  INTEGER DEFAULT NULL,
                tweet_url       TEXT NOT NULL,
                tweet_id        TEXT NOT NULL DEFAULT '',
                total_points    INTEGER NOT NULL DEFAULT 100,
                mode            TEXT NOT NULL DEFAULT 'partial',
                tasks_json      TEXT NOT NULL DEFAULT '{"like":true,"comment":true,"retweet":true}',
                channel_id      TEXT NOT NULL DEFAULT '',
                message_id      TEXT NOT NULL DEFAULT '',
                posted_at       TEXT DEFAULT (datetime('now')),
                ends_at         TEXT DEFAULT NULL,
                status          TEXT NOT NULL DEFAULT 'active',
                created_by      INTEGER DEFAULT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_raids_guild ON raids(guild_id, status);

            CREATE TABLE IF NOT EXISTS raid_participation (
                participation_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id            INTEGER NOT NULL,
                raid_id             INTEGER NOT NULL,
                user_id             INTEGER NOT NULL,
                tasks_claimed       TEXT NOT NULL DEFAULT '[]',
                points_earned       INTEGER NOT NULL DEFAULT 0,
                verified_at         TEXT DEFAULT NULL,
                verification_status TEXT NOT NULL DEFAULT 'pending',
                flag_reason         TEXT DEFAULT NULL,
                created_at          TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_raidpart_guild_user
                ON raid_participation(guild_id, user_id, raid_id);
            CREATE INDEX IF NOT EXISTS idx_raidpart_pending
                ON raid_participation(verification_status, created_at);

            CREATE TABLE IF NOT EXISTS raid_user_points (
                guild_id        INTEGER NOT NULL,
                user_id         INTEGER NOT NULL,
                total_points    INTEGER NOT NULL DEFAULT 0,
                raids_completed INTEGER NOT NULL DEFAULT 0,
                last_active     TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_raidpts_lb
                ON raid_user_points(guild_id, total_points DESC);

            CREATE TABLE IF NOT EXISTS raid_settings (
                guild_id                    INTEGER PRIMARY KEY,
                enabled                     INTEGER NOT NULL DEFAULT 0,
                point_ratio_like            INTEGER NOT NULL DEFAULT 12,
                point_ratio_comment         INTEGER NOT NULL DEFAULT 40,
                point_ratio_retweet         INTEGER NOT NULL DEFAULT 48,
                adaptive_verification       INTEGER NOT NULL DEFAULT 1,
                max_manual_checks_per_day   INTEGER NOT NULL DEFAULT 3,
                manual_check_count_today    INTEGER NOT NULL DEFAULT 0,
                manual_check_date           TEXT DEFAULT NULL,
                guide_channel_id            TEXT DEFAULT '',
                guide_message               TEXT DEFAULT '',
                raid_role_ids               TEXT DEFAULT '',
                ping_role_id                TEXT DEFAULT '',
                embed_thumbnail_url         TEXT DEFAULT '',
                embed_footer_text           TEXT DEFAULT '',
                embed_color                 TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS raid_verification_log (
                log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                raid_id     INTEGER,
                user_id     INTEGER NOT NULL,
                task        TEXT NOT NULL,
                claimed     INTEGER NOT NULL,
                verified    INTEGER NOT NULL,
                source      TEXT NOT NULL,
                checked_at  TEXT DEFAULT (datetime('now')),
                error_text  TEXT DEFAULT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_raidlog_guild
                ON raid_verification_log(guild_id, checked_at DESC);
        """)

        # One-time: copy ping_role_id -> raid_ping_role_id for existing rows
        conn.execute(
            "UPDATE raid_settings SET raid_ping_role_id=ping_role_id "
            "WHERE raid_ping_role_id='' AND ping_role_id IS NOT NULL AND ping_role_id != ''"
        )

        # One-time: backfill default guide title and description for existing rows.
        # OLD default (from previous phase) is replaced with NEW default (added FIX4 line +
        # updated button references). Rows with custom text are left untouched.
        _DEFAULT_GUIDE_TITLE = "Raid System - How It Works"
        _OLD_GUIDE_DESC_SENTINEL = "Repeat offenders can be banned from raiding."
        _NEW_DEFAULT_GUIDE_DESC = (
            "Welcome! This is your community's raid system. Here's everything you need to know to participate.\n\n"
            "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
            "**1️⃣ Link your X (Twitter) account**\n\n"
            "Use the `/setx` command followed by your X username (without the @).\n\n"
            "Example: `/setx myusername`\n\n"
            "You only need to do this once. If you ever need to change it, you can update it again after a 7-day cooldown.\n\n"
            "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
            "**2️⃣ Wait for a raid to drop**\n\n"
            "When a new raid is posted, you'll see an embed in the raid channel containing:\n"
            "- The tweet to raid\n"
            "- Which tasks count (Like, Comment, Retweet)\n"
            "- A 🎯 **Join Raid** button below the embed\n\n"
            "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
            "**3️⃣ Complete the tasks on X**\n\n"
            "Open the tweet on X and do the tasks you want to claim. Be genuine — write thoughtful comments, don't just spam.\n\n"
            "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
            "**4️⃣ Claim your tasks**\n\n"
            "Back on Discord, click 🎯 **Join Raid** under the raid embed. "
            "This opens a private panel just for you:\n\n"
            "❤️ **Like** — click to toggle if you liked the tweet\n"
            "💬 **Comment** — click to toggle if you commented\n"
            "🔁 **Retweet** — click to toggle if you retweeted\n\n"
            "Each click silently updates your selection — nothing is recorded until you confirm.\n\n"
            "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
            "**5️⃣ Confirm your submission**\n\n"
            "When you're ready, click ✅ **Confirm**. The bot records what you claimed and shows the points you earned. "
            "Your submission is final — you can only confirm once per raid.\n\n"
            "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
            "**🔍 How we verify**\n\n"
            "A random sample of submissions is automatically checked against X every day. "
            "Admins can also manually verify any submission. "
            "If a task was claimed but not done, it gets flagged and points are deducted. "
            "Admins can also take extra actions (ban, mute, etc.) at their discretion.\n\n"
            "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
            "**📊 Track your progress**\n\n"
            "- `/raid leaderboard` — see the top raiders in this server\n"
            "- `/raid profile` — see your own stats\n\n"
            "Happy raiding! 🚀"
        )
        conn.execute(
            "UPDATE raid_settings SET raid_guide_title=? WHERE raid_guide_title=''",
            (_DEFAULT_GUIDE_TITLE,),
        )
        # Update empty rows AND rows that still have the old default (identified by sentinel phrase)
        conn.execute(
            "UPDATE raid_settings SET raid_guide_description=? "
            "WHERE raid_guide_description='' OR raid_guide_description LIKE ?",
            (_NEW_DEFAULT_GUIDE_DESC, f'%{_OLD_GUIDE_DESC_SENTINEL}%'),
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS twitter_accounts (
                slot      INTEGER PRIMARY KEY,
                username  TEXT NOT NULL DEFAULT '',
                active    INTEGER NOT NULL DEFAULT 0,
                last_used TEXT DEFAULT NULL,
                notes     TEXT DEFAULT ''
            )
        """)

    # ── Engage module: rename legacy tables then create new schema ────────────
    def _safe_rename(conn, old, new):
        if conn.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (old,)).fetchone():
            if not conn.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (new,)).fetchone():
                conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
                print(f'[db] renamed legacy table {old} -> {new}')

    with get_connection() as conn:
        _safe_rename(conn, 'engage_links', 'engage_links_legacy')
        _safe_rename(conn, 'engage_participation', 'engage_participation_legacy')
        _safe_rename(conn, 'creator_engage_links', 'creator_engage_links_legacy')
        _safe_rename(conn, 'creator_engage_participation', 'creator_engage_participation_legacy')

    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS engage_pools (
                pool_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                name TEXT NOT NULL,
                display_name TEXT,
                pool_type TEXT NOT NULL DEFAULT 'default',
                enabled INTEGER NOT NULL DEFAULT 0,
                channel_id TEXT,
                submit_cost INTEGER NOT NULL DEFAULT 50,
                ttl_hours INTEGER NOT NULL DEFAULT 24,
                min_followers INTEGER NOT NULL DEFAULT 100,
                daily_submission_limit INTEGER NOT NULL DEFAULT 3,
                point_ratio_like INTEGER NOT NULL DEFAULT 12,
                point_ratio_comment INTEGER NOT NULL DEFAULT 40,
                point_ratio_retweet INTEGER NOT NULL DEFAULT 48,
                total_points_per_engage INTEGER NOT NULL DEFAULT 10,
                allow_like INTEGER NOT NULL DEFAULT 1,
                allow_comment INTEGER NOT NULL DEFAULT 1,
                allow_retweet INTEGER NOT NULL DEFAULT 1,
                embed_color TEXT,
                embed_thumbnail_url TEXT,
                embed_footer_text TEXT,
                embed_footer_icon_url TEXT,
                guide_title TEXT,
                guide_description TEXT,
                guide_image_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, name)
            );

            CREATE TABLE IF NOT EXISTS engage_submissions (
                submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                pool_id INTEGER NOT NULL,
                submitter_user_id TEXT NOT NULL,
                tweet_url TEXT NOT NULL,
                tweet_id TEXT NOT NULL,
                submitter_x_username TEXT,
                cost_paid INTEGER NOT NULL DEFAULT 0,
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'active',
                display_number INTEGER,
                FOREIGN KEY (pool_id) REFERENCES engage_pools(pool_id)
            );
            CREATE INDEX IF NOT EXISTS idx_engage_sub_pool_status ON engage_submissions(pool_id, status);

            CREATE TABLE IF NOT EXISTS engage_actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                pool_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                engager_user_id TEXT NOT NULL,
                engager_x_username TEXT,
                like_claimed INTEGER NOT NULL DEFAULT 0,
                comment_claimed INTEGER NOT NULL DEFAULT 0,
                retweet_claimed INTEGER NOT NULL DEFAULT 0,
                like_verified INTEGER,
                comment_verified INTEGER,
                retweet_verified INTEGER,
                points_earned INTEGER NOT NULL DEFAULT 0,
                verification_source TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                flagged INTEGER NOT NULL DEFAULT 0,
                flag_reason TEXT,
                UNIQUE(submission_id, engager_user_id),
                FOREIGN KEY (submission_id) REFERENCES engage_submissions(submission_id)
            );
            CREATE INDEX IF NOT EXISTS idx_engage_actions_pool ON engage_actions(pool_id);
            CREATE INDEX IF NOT EXISTS idx_engage_actions_engager ON engage_actions(engager_user_id);

            CREATE TABLE IF NOT EXISTS engage_user_points (
                guild_id TEXT NOT NULL,
                pool_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                total_engaged INTEGER NOT NULL DEFAULT 0,
                total_submitted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (pool_id, user_id),
                FOREIGN KEY (pool_id) REFERENCES engage_pools(pool_id)
            );

            CREATE TABLE IF NOT EXISTS engage_verification_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                pool_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                engager_user_id TEXT NOT NULL,
                task TEXT NOT NULL,
                claimed INTEGER NOT NULL,
                verified INTEGER NOT NULL,
                source TEXT,
                error_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_engage_log ON engage_verification_log(submission_id, engager_user_id);
        """)

    # Seed AmeretaVerse pools
    AMERETAVERSE_GUILD_ID = '1199707792706117642'
    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO engage_pools (guild_id, name, display_name, pool_type, min_followers)
            VALUES (?, 'community', 'Community Engage', 'community', 500)
        """, (AMERETAVERSE_GUILD_ID,))
        conn.execute("""
            INSERT OR IGNORE INTO engage_pools (guild_id, name, display_name, pool_type, min_followers)
            VALUES (?, 'creator', 'Creator Engage', 'creator', 500)
        """, (AMERETAVERSE_GUILD_ID,))

    # One-time cleanup: close orphaned tickets (NULL/0 guild_id or stub channel_id=0
    # left behind by interrupted ticket creation before the channel was made).
    with get_connection() as conn:
        r1 = conn.execute(
            "UPDATE tickets SET status='closed' "
            "WHERE (guild_id IS NULL OR guild_id=0) AND status='open'"
        )
        if r1.rowcount > 0:
            print(f'[migration] Closed {r1.rowcount} orphaned tickets with no guild_id')
        r2 = conn.execute(
            "UPDATE tickets SET status='closed' "
            "WHERE channel_id=0 AND status='open'"
        )
        if r2.rowcount > 0:
            print(f'[migration] Closed {r2.rowcount} stub tickets with channel_id=0')


    # Informational audit: warn if any guild's max display_number doesn't match its raid count.
    # This can happen if display_number was assigned without a guild_id filter in older code.
    # We do NOT renumber historical data — just log so admins are aware of gaps.
    with get_connection() as conn:
        gap_rows = conn.execute(
            "SELECT guild_id, COUNT(*) AS cnt, MAX(display_number) AS maxd "
            "FROM raids GROUP BY guild_id HAVING maxd IS NOT NULL AND maxd != cnt"
        ).fetchall()
        for r in gap_rows:
            print(
                f'[raid] guild {r["guild_id"]} has {r["cnt"]} raids but '
                f'max display_number={r["maxd"]} — gap detected '
                f'(historical only; new raids use guild-scoped numbering)'
            )


def ensure_guild_defaults(guild_id: int):
    """Insert default config keys for a guild if they don't already exist."""
    with get_connection() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            [(guild_id, k, v) for k, v in DEFAULT_CONFIG.items()],
        )


def get_config(guild_id: int, key: str, default: str = None) -> str:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE guild_id=? AND key=?",
            (guild_id, key),
        ).fetchone()
    if row is not None:
        return row["value"]
    return DEFAULT_CONFIG.get(key, default)


def set_config(guild_id: int, key: str, value: str):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value, updated_at) VALUES (?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (guild_id, key, str(value)),
        )


def get_all_config(guild_id: int) -> dict:
    """Return merged config: defaults overridden by guild-specific rows."""
    result = dict(DEFAULT_CONFIG)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT key, value FROM config WHERE guild_id=?", (guild_id,)
        ).fetchall()
    for r in rows:
        result[r["key"]] = r["value"]
    return result


# ── RoleSelect CRUD helpers ────────────────────────────────────────────────────

def get_panels(guild_id: int) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM roleselect_panels WHERE guild_id=? ORDER BY panel_id",
            (guild_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_panel(panel_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM roleselect_panels WHERE panel_id=?",
            (panel_id,),
        ).fetchone()
    return dict(row) if row else None


def create_panel(guild_id: int, title: str, description: str, style: str) -> int:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO roleselect_panels (guild_id, title, description, style) "
            "VALUES (?,?,?,?)",
            (guild_id, title, description, style),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_panel(panel_id: int, **fields) -> bool:
    allowed = {'channel_id', 'message_id', 'title', 'description', 'style',
               'thumbnail_url', 'image_url', 'color', 'footer_text'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = (
        ', '.join(f"{k}=?" for k in updates)
        + ', updated_at=CURRENT_TIMESTAMP'
    )
    with get_connection() as conn:
        c = conn.execute(
            f"UPDATE roleselect_panels SET {set_clause} WHERE panel_id=?",
            list(updates.values()) + [panel_id],
        )
    return c.rowcount > 0


def delete_panel(panel_id: int) -> bool:
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM roleselect_buttons WHERE panel_id=?", (panel_id,)
        )
        c = conn.execute(
            "DELETE FROM roleselect_panels WHERE panel_id=?", (panel_id,)
        )
    return c.rowcount > 0


def get_buttons(panel_id: int) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM roleselect_buttons "
            "WHERE panel_id=? ORDER BY position, button_id",
            (panel_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_button(button_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM roleselect_buttons WHERE button_id=?",
            (button_id,),
        ).fetchone()
    return dict(row) if row else None


def create_button(panel_id: int, **fields) -> int:
    allowed = {
        'position', 'label', 'emoji', 'role', 'mode',
        'confirm_give_enabled', 'confirm_give_message',
        'confirm_take_enabled', 'confirm_take_message',
        'dm_give_enabled', 'dm_give_message',
        'dm_take_enabled', 'dm_take_message',
    }
    cols = {'panel_id': panel_id}
    for k, v in fields.items():
        if k in allowed:
            cols[k] = v
    col_list = ', '.join(cols.keys())
    placeholders = ', '.join('?' for _ in cols)
    with get_connection() as conn:
        conn.execute(
            f"INSERT INTO roleselect_buttons ({col_list}) VALUES ({placeholders})",
            list(cols.values()),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_button(button_id: int, **fields) -> bool:
    allowed = {
        'position', 'label', 'emoji', 'role', 'mode',
        'confirm_give_enabled', 'confirm_give_message',
        'confirm_take_enabled', 'confirm_take_message',
        'dm_give_enabled', 'dm_give_message',
        'dm_take_enabled', 'dm_take_message',
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = ', '.join(f"{k}=?" for k in updates)
    with get_connection() as conn:
        c = conn.execute(
            f"UPDATE roleselect_buttons SET {set_clause} WHERE button_id=?",
            list(updates.values()) + [button_id],
        )
    return c.rowcount > 0


def delete_button(button_id: int) -> bool:
    with get_connection() as conn:
        c = conn.execute(
            "DELETE FROM roleselect_buttons WHERE button_id=?", (button_id,)
        )
    return c.rowcount > 0


# ── Assets library helpers ─────────────────────────────────────────────────────

def list_guild_assets(guild_id: int) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM assets_library WHERE guild_id=? AND deleted=0 "
            "ORDER BY uploaded_at DESC",
            (guild_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def create_asset_record(
    guild_id: int, file_id: str, key: str, url: str,
    original_name: str, size: int, content_type: str,
    extension: str, uploaded_by: int,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO assets_library
               (guild_id, file_id, key, url, original_name, size,
                content_type, extension, uploaded_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, file_id, key, url, original_name, size,
             content_type, extension, uploaded_by),
        )
        return cur.lastrowid


def soft_delete_asset(guild_id: int, asset_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM assets_library "
            "WHERE asset_id=? AND guild_id=? AND deleted=0",
            (asset_id, guild_id),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE assets_library SET deleted=1 WHERE asset_id=?",
            (asset_id,),
        )
    return dict(row)


def get_asset_by_id(guild_id: int, asset_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM assets_library "
            "WHERE asset_id=? AND guild_id=? AND deleted=0",
            (asset_id, guild_id),
        ).fetchone()
    return dict(row) if row else None


# ── Forms CRUD helpers ─────────────────────────────────────────────────────────

def list_forms(guild_id: int) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM forms WHERE guild_id=? ORDER BY form_id DESC",
            (guild_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_form(form_id: int, guild_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM forms WHERE form_id=? AND guild_id=?",
            (form_id, guild_id),
        ).fetchone()
    return dict(row) if row else None


def create_form(guild_id: int, name: str) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO forms (guild_id, name, title, description) VALUES (?, ?, ?, '')",
            (guild_id, name, name),
        )
        return cur.lastrowid


def update_form(form_id: int, guild_id: int, **fields) -> bool:
    allowed = {
        'name', 'title', 'description', 'button_label', 'thumbnail_url', 'image_url',
        'color', 'footer_text', 'channel_id', 'message_id', 'ticket_category',
        'staff_roles', 'ping_role', 'approve_role', 'approve_dm_enabled',
        'approve_dm_message', 'reject_dm_enabled', 'reject_dm_message', 'enabled',
        'auto_close_on_decision',
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    cols = ', '.join(f'{k}=?' for k in sets)
    vals = list(sets.values()) + [form_id, guild_id]
    with get_connection() as conn:
        c = conn.execute(f"UPDATE forms SET {cols} WHERE form_id=? AND guild_id=?", vals)
        return c.rowcount > 0


def delete_form(form_id: int, guild_id: int) -> bool:
    with get_connection() as conn:
        c = conn.execute(
            "DELETE FROM forms WHERE form_id=? AND guild_id=?",
            (form_id, guild_id),
        )
        return c.rowcount > 0


def list_form_fields(form_id: int) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM form_fields WHERE form_id=? ORDER BY position ASC, field_id ASC",
            (form_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def create_form_field(
    form_id: int, position: int, label: str, field_type: str,
    placeholder: str = '', required: int = 1,
    options: str = '', max_length: int = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO form_fields
               (form_id, position, label, field_type, placeholder, required, options, max_length)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (form_id, position, label, field_type, placeholder, required, options, max_length),
        )
        return cur.lastrowid


def update_form_field(field_id: int, form_id: int, **fields) -> bool:
    allowed = {'position', 'label', 'field_type', 'placeholder', 'required', 'options', 'max_length'}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    cols = ', '.join(f'{k}=?' for k in sets)
    vals = list(sets.values()) + [field_id, form_id]
    with get_connection() as conn:
        c = conn.execute(f"UPDATE form_fields SET {cols} WHERE field_id=? AND form_id=?", vals)
        return c.rowcount > 0


def delete_form_field(field_id: int, form_id: int) -> bool:
    with get_connection() as conn:
        c = conn.execute(
            "DELETE FROM form_fields WHERE field_id=? AND form_id=?",
            (field_id, form_id),
        )
        return c.rowcount > 0


def create_form_submission(
    form_id: int, guild_id: int, user_id: int,
    username: str, channel_id: str, answers: str,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO form_submissions
               (form_id, guild_id, user_id, username, channel_id, answers, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (form_id, guild_id, user_id, username, channel_id, answers),
        )
        return cur.lastrowid


def get_submission(submission_id: int, guild_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM form_submissions WHERE submission_id=? AND guild_id=?",
            (submission_id, guild_id),
        ).fetchone()
    return dict(row) if row else None


def update_submission_status(
    submission_id: int, guild_id: int, status: str, decided_by: int,
) -> bool:
    with get_connection() as conn:
        c = conn.execute(
            """UPDATE form_submissions
               SET status=?, decided_by=?, decided_at=datetime('now')
               WHERE submission_id=? AND guild_id=?""",
            (status, decided_by, submission_id, guild_id),
        )
        return c.rowcount > 0


# ── Twitter account helpers ────────────────────────────────────────────────────

def list_twitter_accounts() -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM twitter_accounts ORDER BY slot"
        ).fetchall()
    return [dict(r) for r in rows]


def set_twitter_account_active(slot: int, active: int) -> bool:
    with get_connection() as conn:
        c = conn.execute(
            "UPDATE twitter_accounts SET active=? WHERE slot=?", (active, slot)
        )
    return c.rowcount > 0


def upsert_twitter_account_slot(slot: int, username: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO twitter_accounts (slot, username, active) VALUES (?, ?, 0) "
            "ON CONFLICT(slot) DO UPDATE SET username=excluded.username",
            (slot, username),
        )


def find_user_by_x_username(x_username: str) -> dict | None:
    """Find a users row by X/Twitter username (case-insensitive, strips leading @)."""
    cleaned = (x_username or '').lstrip('@').strip().lower()
    if not cleaned:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(x_username) = ?", (cleaned,)
        ).fetchone()
    return dict(row) if row else None


def get_user_x_username(user_id: int) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT x_username FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    return row['x_username'] if row else None


def has_pending_submission(form_id: int, user_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM form_submissions "
            "WHERE form_id=? AND user_id=? AND status='pending' LIMIT 1",
            (form_id, user_id),
        ).fetchone()
    return row is not None


# ── Raid CRUD helpers ──────────────────────────────────────────────────────────

_RAID_SETTINGS_DEFAULTS = {
    'guild_id': 0, 'enabled': 0,
    'point_ratio_like': 12, 'point_ratio_comment': 40, 'point_ratio_retweet': 48,
    'adaptive_verification': 1, 'max_manual_checks_per_day': 3,
    'manual_check_count_today': 0, 'manual_check_date': None,
    'guide_channel_id': '', 'guide_message': '', 'raid_role_ids': '',
    'ping_role_id': '', 'embed_thumbnail_url': '', 'embed_footer_text': '', 'embed_color': '',
    'raid_channel_id': '', 'raid_ping_role_id': '',
    'raid_guide_channel_id': '', 'raid_guide_title': '', 'raid_guide_description': '',
    'raid_guide_thumbnail_url': '', 'raid_guide_image_url': '',
    'raid_guide_color': '', 'raid_guide_footer_text': '',
}


def get_raid_settings(guild_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM raid_settings WHERE guild_id=?", (guild_id,)
        ).fetchone()
    if row:
        return dict(row)
    d = dict(_RAID_SETTINGS_DEFAULTS)
    d['guild_id'] = guild_id
    return d


def upsert_raid_settings(guild_id: int, **fields) -> bool:
    allowed = {
        'enabled', 'point_ratio_like', 'point_ratio_comment', 'point_ratio_retweet',
        'adaptive_verification', 'max_manual_checks_per_day', 'manual_check_count_today',
        'manual_check_date', 'guide_channel_id', 'guide_message', 'raid_role_ids',
        'ping_role_id', 'embed_thumbnail_url', 'embed_footer_text', 'embed_color',
        'raid_channel_id', 'raid_ping_role_id',
        'raid_guide_channel_id', 'raid_guide_title', 'raid_guide_description',
        'raid_guide_thumbnail_url', 'raid_guide_image_url',
        'raid_guide_color', 'raid_guide_footer_text',
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    cols = ['guild_id'] + list(sets.keys())
    vals = [guild_id] + list(sets.values())
    placeholders = ', '.join('?' for _ in vals)
    update_clause = ', '.join(f'{k}=excluded.{k}' for k in sets.keys())
    with get_connection() as conn:
        conn.execute(
            f"INSERT INTO raid_settings ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {update_clause}",
            vals,
        )
    return True


def create_guild_raid(
    guild_id: int, tweet_url: str, tweet_id: str, total_points: int,
    mode: str, tasks_json: str, created_by: int,
) -> int:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO raids (guild_id, tweet_url, tweet_id, total_points, mode, tasks_json, created_by, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
            (guild_id, tweet_url, tweet_id, total_points, mode, tasks_json, created_by),
        )
        raid_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        row = conn.execute(
            "SELECT COALESCE(MAX(display_number), 0) + 1 AS next_num FROM raids WHERE guild_id=?",
            (guild_id,),
        ).fetchone()
        conn.execute(
            "UPDATE raids SET display_number=? WHERE raid_id=?",
            (row['next_num'], raid_id),
        )
    return raid_id


def end_raid(raid_id: int, guild_id: int, ended_reason: str = 'admin') -> bool:
    with get_connection() as conn:
        c = conn.execute(
            "UPDATE raids SET status='ended', ended_reason=? "
            "WHERE raid_id=? AND guild_id=? AND status='active'",
            (ended_reason, raid_id, guild_id),
        )
    return c.rowcount > 0


def get_guild_raid(raid_id: int, guild_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM raids WHERE raid_id=? AND guild_id=?",
            (raid_id, guild_id),
        ).fetchone()
    return dict(row) if row else None


def list_guild_raids(guild_id: int, status: str = 'active', limit: int = 50) -> list:
    with get_connection() as conn:
        if status == 'all':
            rows = conn.execute(
                "SELECT * FROM raids WHERE guild_id=? ORDER BY posted_at DESC LIMIT ?",
                (guild_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM raids WHERE guild_id=? AND status=? ORDER BY posted_at DESC LIMIT ?",
                (guild_id, status, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def update_guild_raid(raid_id: int, guild_id: int, **fields) -> bool:
    allowed = {'tweet_url', 'tweet_id', 'total_points', 'mode', 'tasks_json',
               'channel_id', 'message_id', 'ends_at', 'status'}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    cols = ', '.join(f'{k}=?' for k in sets)
    with get_connection() as conn:
        c = conn.execute(
            f"UPDATE raids SET {cols} WHERE raid_id=? AND guild_id=?",
            list(sets.values()) + [raid_id, guild_id],
        )
    return c.rowcount > 0


def get_raid_participation(guild_id: int, raid_id: int, user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM raid_participation WHERE guild_id=? AND raid_id=? AND user_id=?",
            (guild_id, raid_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def get_participation_by_id(participation_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM raid_participation WHERE participation_id=?",
            (participation_id,),
        ).fetchone()
    return dict(row) if row else None


def create_raid_participation(
    guild_id: int, raid_id: int, user_id: int,
    tasks_claimed: str, points_earned: int,
) -> int:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO raid_participation (guild_id, raid_id, user_id, tasks_claimed, points_earned) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, raid_id, user_id, tasks_claimed, points_earned),
        )
        return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def update_raid_participation(participation_id: int, **fields) -> bool:
    allowed = {'tasks_claimed', 'points_earned', 'verified_at', 'verification_status', 'flag_reason'}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    cols = ', '.join(f'{k}=?' for k in sets)
    with get_connection() as conn:
        c = conn.execute(
            f"UPDATE raid_participation SET {cols} WHERE participation_id=?",
            list(sets.values()) + [participation_id],
        )
    return c.rowcount > 0


def list_raid_participations(
    guild_id: int, raid_id: int = None, user_id: int = None,
    v_status: str = None, limit: int = 50,
) -> list:
    conditions = ["guild_id=?"]
    params: list = [guild_id]
    if raid_id is not None:
        conditions.append("raid_id=?"); params.append(raid_id)
    if user_id is not None:
        conditions.append("user_id=?"); params.append(user_id)
    if v_status is not None:
        conditions.append("verification_status=?"); params.append(v_status)
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM raid_participation WHERE {' AND '.join(conditions)} "
            "ORDER BY created_at DESC LIMIT ?", params,
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_raid_user_points(guild_id: int, user_id: int, delta_points: int, delta_raids: int = 0):
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO raid_user_points (guild_id, user_id, total_points, raids_completed, last_active)
               VALUES (?, ?, MAX(0,?), MAX(0,?), datetime('now'))
               ON CONFLICT(guild_id, user_id) DO UPDATE SET
                   total_points=MAX(0, total_points + ?),
                   raids_completed=raids_completed + ?,
                   last_active=datetime('now')""",
            (guild_id, user_id, max(0, delta_points), max(0, delta_raids),
             delta_points, delta_raids),
        )


def get_raid_user_points(guild_id: int, user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM raid_user_points WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def get_raid_leaderboard(guild_id: int, limit: int = 10) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT rup.user_id, u.username, rup.total_points, rup.raids_completed "
            "FROM raid_user_points rup LEFT JOIN users u ON u.user_id=rup.user_id "
            "WHERE rup.guild_id=? ORDER BY rup.total_points DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def add_raid_verification_log(
    guild_id: int, raid_id: int, user_id: int,
    task: str, claimed: bool, verified,
    source: str, error_text: str = None,
):
    # verified: 1=passed, 0=failed, -1=inconclusive; bool True/False also accepted
    v_int = int(verified) if isinstance(verified, (int, bool)) else -1
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO raid_verification_log "
            "(guild_id, raid_id, user_id, task, claimed, verified, source, error_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, raid_id, user_id, task,
             1 if claimed else 0, v_int, source, error_text),
        )


def get_raid_verification_log(
    guild_id: int, raid_id: int = None, user_id: int = None, limit: int = 50,
) -> list:
    conditions = ["guild_id=?"]
    params: list = [guild_id]
    if raid_id is not None:
        conditions.append("raid_id=?"); params.append(raid_id)
    if user_id is not None:
        conditions.append("user_id=?"); params.append(user_id)
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM raid_verification_log WHERE {' AND '.join(conditions)} "
            "ORDER BY checked_at DESC LIMIT ?", params,
        ).fetchall()
    return [dict(r) for r in rows]


def count_pending_participations_24h() -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM raid_participation WHERE verification_status='pending' "
            "AND created_at > datetime('now', '-24 hours')"
        ).fetchone()
    return row[0] if row else 0


def sample_pending_participations(limit: int) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM raid_participation WHERE verification_status='pending' "
            "AND created_at > datetime('now', '-24 hours') ORDER BY RANDOM() LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def check_reset_manual_count(guild_id: int) -> dict:
    import datetime as _dt
    today = _dt.date.today().isoformat()
    settings = get_raid_settings(guild_id)
    if settings.get('manual_check_date') != today:
        upsert_raid_settings(guild_id, manual_check_count_today=0, manual_check_date=today)
        settings['manual_check_count_today'] = 0
        settings['manual_check_date'] = today
    return settings


# ── Engage helpers ──────────────────────────────────────────────────────────

def get_engage_pool_by_id(pool_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM engage_pools WHERE pool_id=?", (pool_id,)).fetchone()
    return dict(row) if row else None


def get_engage_pool_by_channel(guild_id: str, channel_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM engage_pools WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id)
        ).fetchone()
    return dict(row) if row else None


def list_engage_pools(guild_id: str) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM engage_pools WHERE guild_id=? ORDER BY pool_id",
            (guild_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_engage_pool(pool_id: int, **kwargs) -> bool:
    allowed = {
        'enabled', 'channel_id', 'allowed_role_ids', 'submit_cost', 'ttl_hours',
        'auto_reset_daily', 'min_followers', 'daily_submission_limit',
        'point_ratio_like', 'point_ratio_comment', 'point_ratio_retweet',
        'total_points_per_engage', 'allow_like', 'allow_comment', 'allow_retweet',
        'embed_color', 'embed_thumbnail_url', 'embed_footer_text', 'embed_footer_icon_url',
        'guide_title', 'guide_description', 'guide_image_url', 'display_name',
    }
    sets = {k: v for k, v in kwargs.items() if k in allowed}
    if not sets:
        return False
    cols = ', '.join(f'{k}=?' for k in sets)
    with get_connection() as conn:
        c = conn.execute(f"UPDATE engage_pools SET {cols} WHERE pool_id=?",
                         list(sets.values()) + [pool_id])
    return c.rowcount > 0


def create_engage_submission(
    guild_id: str, pool_id: int, submitter_user_id: str,
    tweet_url: str, tweet_id: str, submitter_x_username: str,
    cost_paid: int, ttl_hours,
) -> dict:
    import datetime as _dt
    expires_at = None
    if ttl_hours is not None:
        expires_at = (_dt.datetime.utcnow() + _dt.timedelta(hours=int(ttl_hours))).isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO engage_submissions
               (guild_id, pool_id, submitter_user_id, tweet_url, tweet_id,
                submitter_x_username, cost_paid, expires_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (str(guild_id), pool_id, str(submitter_user_id), tweet_url, tweet_id,
             submitter_x_username, cost_paid, expires_at),
        )
        submission_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        row = conn.execute(
            "SELECT COALESCE(MAX(display_number), 0) + 1 FROM engage_submissions WHERE pool_id=?",
            (pool_id,)
        ).fetchone()
        conn.execute("UPDATE engage_submissions SET display_number=? WHERE submission_id=?",
                     (row[0], submission_id))
        result = conn.execute("SELECT * FROM engage_submissions WHERE submission_id=?",
                              (submission_id,)).fetchone()
    return dict(result)


def list_active_submissions(pool_id: int, limit: int = 10, exclude_user_id: str = None) -> list:
    params = [pool_id]
    exclude_clause = ''
    if exclude_user_id is not None:
        exclude_clause = "AND submitter_user_id != ?"
        params.append(str(exclude_user_id))
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT * FROM engage_submissions
                WHERE pool_id=? AND status='active'
                AND (expires_at IS NULL OR expires_at > datetime('now'))
                {exclude_clause}
                ORDER BY RANDOM() LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def expire_old_submissions() -> int:
    with get_connection() as conn:
        c = conn.execute(
            """UPDATE engage_submissions SET status='expired'
               WHERE status='active' AND expires_at IS NOT NULL
               AND expires_at < datetime('now')"""
        )
    return c.rowcount


def get_user_daily_submission_count(pool_id: int, user_id: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            """SELECT COUNT(*) FROM engage_submissions
               WHERE pool_id=? AND submitter_user_id=?
               AND submitted_at > datetime('now', '-24 hours')""",
            (pool_id, str(user_id))
        ).fetchone()
    return row[0] if row else 0


def get_engage_action(submission_id: int, engager_user_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM engage_actions WHERE submission_id=? AND engager_user_id=?",
            (submission_id, str(engager_user_id))
        ).fetchone()
    return dict(row) if row else None


def upsert_engage_action(
    guild_id: str, pool_id: int, submission_id: int, engager_user_id: str,
    engager_x_username: str,
    like_claimed: int, comment_claimed: int, retweet_claimed: int,
    like_verified, comment_verified, retweet_verified,
    points_earned: int, verification_source: str,
    flagged: int = 0, flag_reason: str = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO engage_actions
               (guild_id, pool_id, submission_id, engager_user_id, engager_x_username,
                like_claimed, comment_claimed, retweet_claimed,
                like_verified, comment_verified, retweet_verified,
                points_earned, verification_source, flagged, flag_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(submission_id, engager_user_id) DO UPDATE SET
                 like_claimed=excluded.like_claimed,
                 comment_claimed=excluded.comment_claimed,
                 retweet_claimed=excluded.retweet_claimed,
                 like_verified=excluded.like_verified,
                 comment_verified=excluded.comment_verified,
                 retweet_verified=excluded.retweet_verified,
                 points_earned=excluded.points_earned,
                 verification_source=excluded.verification_source,
                 flagged=excluded.flagged,
                 flag_reason=excluded.flag_reason""",
            (str(guild_id), pool_id, submission_id, str(engager_user_id), engager_x_username,
             like_claimed, comment_claimed, retweet_claimed,
             like_verified, comment_verified, retweet_verified,
             points_earned, verification_source, flagged, flag_reason),
        )


def add_engage_verification_log(
    guild_id: str, pool_id: int, submission_id: int, engager_user_id: str,
    task: str, claimed: int, verified: int, source: str, error_text: str = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO engage_verification_log
               (guild_id, pool_id, submission_id, engager_user_id, task, claimed, verified, source, error_text)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(guild_id), pool_id, submission_id, str(engager_user_id),
             task, claimed, verified, source, error_text),
        )


def upsert_engage_user_points(
    guild_id: str, pool_id: int, user_id: str,
    delta_points: int, delta_engaged: int = 0, delta_submitted: int = 0,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO engage_user_points (guild_id, pool_id, user_id, points, total_engaged, total_submitted)
               VALUES (?,?,?,MAX(0,?),MAX(0,?),MAX(0,?))
               ON CONFLICT(pool_id, user_id) DO UPDATE SET
                 points=MAX(0, points + ?),
                 total_engaged=total_engaged + ?,
                 total_submitted=total_submitted + ?""",
            (str(guild_id), pool_id, str(user_id),
             max(0, delta_points), max(0, delta_engaged), max(0, delta_submitted),
             delta_points, delta_engaged, delta_submitted),
        )


def get_engage_user_points(pool_id: int, user_id: str) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM engage_user_points WHERE pool_id=? AND user_id=?",
            (pool_id, str(user_id))
        ).fetchone()
    return dict(row) if row else {'points': 0, 'total_engaged': 0, 'total_submitted': 0}


def get_engage_leaderboard(pool_id: int, limit: int = 10) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT eup.user_id, u.username, eup.points, eup.total_engaged, eup.total_submitted
               FROM engage_user_points eup
               LEFT JOIN users u ON u.user_id = eup.user_id
               WHERE eup.pool_id=? ORDER BY eup.points DESC LIMIT ?""",
            (pool_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def sample_pending_engage_actions(pool_id: int, limit: int) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT ea.*, es.tweet_id FROM engage_actions ea
               JOIN engage_submissions es ON es.submission_id = ea.submission_id
               WHERE ea.pool_id=? AND ea.verification_source IS NULL
               ORDER BY RANDOM() LIMIT ?""",
            (pool_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def count_pending_engage_actions(pool_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM engage_actions WHERE pool_id=? AND verification_source IS NULL",
            (pool_id,)
        ).fetchone()
    return row[0] if row else 0


# ── Engage module extra helpers ────────────────────────────────────────────────

_ENGAGE_AMERETAVERSE_GID = '1199707792706117642'


def ensure_default_pool(guild_id) -> dict:
    """For non-AmeretaVerse guilds: ensure exactly one 'default' pool row exists. Returns it."""
    gid = str(guild_id)
    if gid == _ENGAGE_AMERETAVERSE_GID:
        raise ValueError(
            'AmeretaVerse main uses community/creator pools — '
            'do not call ensure_default_pool for it.'
        )
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM engage_pools WHERE guild_id=? AND name='default'", (gid,)
        ).fetchone()
        if row:
            return dict(row)
        conn.execute(
            "INSERT INTO engage_pools "
            "(guild_id, name, display_name, pool_type, min_followers) "
            "VALUES (?,'default','Engage','default',100)",
            (gid,),
        )
        row = conn.execute(
            "SELECT * FROM engage_pools WHERE guild_id=? AND name='default'", (gid,)
        ).fetchone()
        return dict(row)


# Alias names used in cogs/engage.py (spec-required names)

def list_active_engage_submissions(pool_id: int, limit: int = 10, exclude_user_id=None) -> list:
    """Alias for list_active_submissions with spec-required name."""
    return list_active_submissions(pool_id, limit=limit, exclude_user_id=exclude_user_id)


def expire_old_engage_submissions() -> int:
    """Alias for expire_old_submissions with spec-required name."""
    return expire_old_submissions()


def get_user_daily_engage_submissions(pool_id: int, user_id) -> int:
    """Alias for get_user_daily_submission_count with spec-required name."""
    return get_user_daily_submission_count(pool_id, str(user_id))


def reset_engage_pool_daily(pool_id: int) -> int:
    """Expire all active submissions in a pool (daily reset)."""
    with get_connection() as conn:
        c = conn.execute(
            "UPDATE engage_submissions SET status='expired' "
            "WHERE pool_id=? AND status='active'",
            (pool_id,),
        )
    return c.rowcount


# ── Guild settings + module access ──────────────────────────────────────────

MODULES = ('verify', 'roleselect', 'forms', 'tickets', 'raid', 'engage',
           'protection', 'flagged', 'mod_log', 'audit_log', 'analytics', 'settings')


def get_guild_settings(guild_id) -> dict:
    gid = str(guild_id)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM guild_settings WHERE guild_id=?", (gid,)).fetchone()
        if row:
            return dict(row)
        conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (gid,))
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM guild_settings WHERE guild_id=?", (gid,)).fetchone()
        return dict(row) if row else {
            'guild_id': gid, 'default_embed_color': '#94730D',
            'default_thumbnail_url': None, 'default_footer_text': None,
            'default_footer_icon_url': None,
        }


def update_guild_settings(guild_id, **kwargs) -> dict:
    allowed = {'default_embed_color', 'default_thumbnail_url',
               'default_footer_text', 'default_footer_icon_url',
               'bot_display_name', 'bot_avatar_url'}
    payload = {k: v for k, v in kwargs.items() if k in allowed}
    if payload:
        get_guild_settings(guild_id)  # ensure row exists
        set_clause = ', '.join(f'{k}=?' for k in payload) + ', updated_at=CURRENT_TIMESTAMP'
        with get_connection() as conn:
            conn.execute(f"UPDATE guild_settings SET {set_clause} WHERE guild_id=?",
                         list(payload.values()) + [str(guild_id)])
    return get_guild_settings(guild_id)


def list_module_access(guild_id) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT role_id, module, granted FROM guild_module_access WHERE guild_id=?",
            (str(guild_id),)
        ).fetchall()
    return [dict(r) for r in rows]


def set_module_access(guild_id, role_id: str, module: str, granted: bool) -> None:
    gid, rid = str(guild_id), str(role_id)
    with get_connection() as conn:
        if granted:
            conn.execute(
                "INSERT INTO guild_module_access (guild_id, role_id, module, granted) VALUES (?,?,?,1) "
                "ON CONFLICT(guild_id, role_id, module) DO UPDATE SET granted=1",
                (gid, rid, module),
            )
        else:
            conn.execute(
                "DELETE FROM guild_module_access WHERE guild_id=? AND role_id=? AND module=?",
                (gid, rid, module),
            )


def user_can_access_module(guild_id, user_id, module: str, bot) -> bool:
    """Owner → always. Admin → yes if no restrictions, else needs a granted role."""
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return False
    if str(guild.owner_id) == str(user_id):
        return True
    member = guild.get_member(int(user_id))
    if not member or not member.guild_permissions.administrator:
        return False
    user_role_ids = {str(r.id) for r in member.roles}
    with get_connection() as conn:
        grants = conn.execute(
            "SELECT role_id FROM guild_module_access WHERE guild_id=? AND module=? AND granted=1",
            (str(guild_id), module)
        ).fetchall()
    granted_ids = {str(g['role_id']) for g in grants}
    if not granted_ids:
        return True  # no restrictions → all admins can access
    return bool(user_role_ids & granted_ids)


if __name__ == '__main__':
    init_db()
    print(f'Database initialized at {DB_PATH}')
