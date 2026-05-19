"""AVbot-wide brand defaults and constants.

These values are the final fallbacks used by every embed builder when no
module-specific, guild-specific, or premium-override value is configured.
"""

# AV logo uploaded to R2; final fallback for every embed thumbnail.
# Run scripts/upload_default_logo.py once after first deploy to populate the asset.
DEFAULT_BOT_THUMBNAIL_URL = 'https://cdn.avbot.app/1199707792706117642/2e6734d8c9fc47fab6b8525a57374de3.png'
DEFAULT_BOT_FOOTER_TEXT   = 'Powered by AVbot'
DEFAULT_BOT_EMBED_COLOR   = 0x94730D
