# Rename to config.py

CONFIG = {
    'title': 'XMPP <-> Telegram Gate',

    'debug': True,

    'jid': 'telegram.yourserver',
    'secret': 'key',
    'server': '0.0.0.0',
    'port': '1488',

    'tg_api_id': '17349',  # Telegram Desktop (GitHub)
    'tg_api_hash': '344583e45741c457fe1862106095a5eb',

    'db_connect': 'db.sqlite',

    'media_web_link_prefix': 'http://example.org/tg_xmpp_media/',
    'media_store_path': '/var/www/tg_xmpp_media/',
    'media_max_download_size': 1024 * 1024 * 5,  # in bytes

    'messages_max_max_cache_size': 300,  # for quotes
}
