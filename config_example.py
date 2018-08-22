# Rename to config.py

CONFIG = {
    'title': 'XMPP <-> Telegram Gate',

    'debug': True,
    'logfile': '/dev/null', 

    'jid': 'tlgrm.localhost',
    'secret': 'secret',
    'server': 'localhost',
    'port': '8889',

    'xmpp_use_roster_exchange': True, # use XEP-0144 to import roster from Telegram

    'tg_api_id': '17349',  # Telegram Desktop (GitHub)
    'tg_api_hash': '344583e45741c457fe1862106095a5eb',

    #'tg_server_ip': '149.154.167.50', 
    #'tg_server_port': 443,
    #'tg_server_dc': 2,

    'db_connect': 'db.sqlite',

    'media_external_formats': 'png|jpg|jpeg|gif|mp3|mp4|ogg',

    'media_web_link_prefix': 'http://tlgrm.localhost/media/',
    'media_store_path': '/var/tg4xmpp/media/',
    'media_max_download_size': 1024 * 1024 * 100,  # in bytes

    'messages_max_max_cache_size': 300,  # for quotes
}
