import sqlite3
import re

from sleekxmpp.componentxmpp import ComponentXMPP

from telethon.tl.functions.messages import GetDialogsRequest, SendMessageRequest
from telethon.tl.functions.account import UpdateStatusRequest, GetAuthorizationsRequest
from telethon.tl.types import InputPeerEmpty, InputPeerUser, InputPeerChat, InputPeerChannel
from telethon.tl.types import PeerChannel, PeerChat, PeerUser, Chat, ChatForbidden, Channel, ChannelForbidden
from telethon.tl.types import UserStatusOnline, UserStatusRecently, UserStatusOffline
from telethon.tl.types import UpdatesTg, UpdateShortSentMessage, UpdateMessageID
from telethon.tl.types.messages import Dialogs, DialogsSlice

from telethon.helpers import generate_random_long
from telethon.errors import SessionPasswordNeededError

from xmpp_tg.mtproto import TelegramGateClient
from xmpp_tg.utils import var_dump, display_tg_name
import xmpp_tg.monkey  # Патчим баги в библиотеках


class XMPPTelegram(ComponentXMPP):
    """
    Класс XMPP компонента транспорта между Telegram и Jabber
    """

    def __init__(self, config_dict):
        """
        Инициализация, подключение плагинов и регистрация событий
        :param config_dict:
        """

        ComponentXMPP.__init__(self, config_dict['jid'], config_dict['secret'], config_dict['server'],
                               config_dict['port'])

        self.config = config_dict
        self.tg_connections = dict()
        self.tg_phones = dict()
        self.tg_dialogs = dict()

        self.db_connection = self.init_database()

        self.register_plugin('xep_0030')  # Service discovery
        self.register_plugin('xep_0054')  # VCard-temp
        self.register_plugin('xep_0172')  # NickNames

        self.add_event_handler('message', self.message)
        self.add_event_handler('presence', self.event_presence)
        self.add_event_handler('got_online', self.handle_online)
        self.add_event_handler('got_offline', self.handle_offline)
        self.add_event_handler('session_start', self.handle_start)

        self.plugin['xep_0030'].add_identity(
            category='gateway',
            itype='telegram',
            name=self.config['title'],
            node=self.boundjid.node,
            jid=self.boundjid.bare,
            lang='no'
        )

        vcard = self.plugin['xep_0054'].make_vcard()
        vcard['FN'] = self.config['title']
        vcard['DESC'] = 'Send /help for information'
        self.plugin['xep_0054'].publish_vcard(jid=self.boundjid.bare, vcard=vcard)

    def __del__(self):
        """
        Деструктор. Теоретически.
        :return:
        """
        self.db_connection.close()

    def handle_start(self, arg):
        """
        Обработчик события успешного подключения компонента к Jabber серверу
        :param arg:
        :return:
        """
        users = self.db_connection.execute("SELECT * FROM accounts").fetchall()
        for usr in users:
            self.send_presence(pto=usr['jid'], pfrom=self.boundjid.bare)

    def message(self, iq):
        """
        Обработчик входящих сообщений из XMPP
        :param iq:
        :return:
        """
        jid = iq['from'].bare

        if iq['to'] == self.config['jid'] and iq['type'] == 'chat':  # Пишут транспорту
            if iq['body'].startswith('!'):
                self.process_command(iq)
            else:
                self.gate_reply_message(iq, 'Only commands accepted. Try !help for more info.')
        else:  # Пишут в Telegram
            if jid in self.tg_connections and self.tg_connections[jid].is_user_authorized():
                if iq['body'].startswith('!'):  # Команда из чата
                    print('command received')
                    if iq['to'].bare.startswith('u'):
                        self.process_chat_user_command(iq)
                    elif iq['to'].bare.startswith('g') or iq['to'].bare.startswith('s'):
                        self.process_chat_group_command(iq)
                    else:
                        self.gate_reply_message(iq, 'Error.')
                else:  # Обычное сообщение
                    print('sent message')
                    tg_id = int(iq['to'].node[1:])
                    tg_peer = None
                    msg = iq['body']
                    reply_mid = None

                    if msg.startswith('>'):  # Проверка на цитирование
                        msg_lines = msg.split('\n')
                        matched = re.match(r'>[ ]*(?P<mid>[\d]+)[ ]*', msg_lines[0]).groupdict()

                        if 'mid' in matched:  # Если нашли ID сообщения, то указываем ответ
                            reply_mid = int(matched['mid'])
                            msg = '\n'.join(msg_lines[1:])

                    if iq['to'].bare.startswith('u'):  # Обычный пользователь
                        tg_peer = InputPeerUser(tg_id, self.tg_dialogs[jid]['users'][tg_id].access_hash)
                    elif iq['to'].bare.startswith('g'):  # Обычная группа
                        tg_peer = InputPeerChat(tg_id)
                    elif iq['to'].bare.startswith('s') or iq['to'].bare.startswith('c'):  # Супергруппа
                        tg_peer = InputPeerChannel(tg_id, self.tg_dialogs[jid]['supergroups'][tg_id].access_hash)

                    if tg_peer:
                        # Отправляем сообщение и получаем новый апдейт
                        result = self.tg_connections[jid].invoke(
                            SendMessageRequest(tg_peer, msg, generate_random_long(), reply_to_msg_id=reply_mid)
                        )
                        msg_id = None

                        # Ищем ID отправленного сообщения
                        if type(result) is UpdatesTg:  # Супегруппа / канал
                            for upd in result.updates:
                                if type(upd) is UpdateMessageID:
                                    msg_id = upd.id
                        elif type(result) is UpdateShortSentMessage:  # ЛС / Группа
                            msg_id = result.id

                        # if msg_id:
                        #    # Отправляем ответ с ID отправленного сообщения
                        #    self.send_message(mto=iq['from'], mfrom=iq['to'], mtype='chat',
                        #                      mbody='[Your MID:{}]'.format(msg_id))

    def event_presence(self, presence):
        """
        Обработчик события subscribe
        :param presence:
        :return:
        """

        ptype = presence['type']

        if ptype == 'subscribe':
            self.send_presence(pto=presence['from'].bare, pfrom=presence['to'].bare, ptype='subscribed')
        elif ptype == 'subscribed':
            #self.send_presence(pto=presence['from'].bare, pfrom=presence['to'].bare, ptype='subscribed')
            pass
        elif ptype == 'unsubscribe':
            pass
        elif ptype == 'unsubscribed':
            pass
        elif ptype == 'probe':
            pass
        elif ptype == 'unavailable':
            pass
        else:
            # self.send_presence(pto=presence['from'], pfrom=presence['to'])
            pass

    def handle_online(self, event):
        """
        Обработчик события online. Подключается к Telegram при наличии авторизации.
        :param event:
        :return:
        """
        jid = event['from'].bare

        if jid not in self.tg_connections:
            result = self.db_connection.execute("SELECT * FROM accounts WHERE jid = ?", (jid,)).fetchone()

            if result is not None:
                self.spawn_tg_client(jid, result['tg_phone'])
        else:
            if not (self.tg_connections[jid].sender and self.tg_connections[jid].is_connected()):
                self.tg_connections[jid].connect()
                self.tg_connections[jid].invoke(UpdateStatusRequest(offline=False))
                self.tg_process_dialogs(jid)


    def handle_offline(self, event):
        """
        Обработчик события offline. Отключается от Telegram, если было создано подключение.
        :param event:
        :return:
        """
        jid = event['from'].bare

        if jid in self.tg_connections:
            self.tg_connections[jid].invoke(UpdateStatusRequest(offline=True))
            self.tg_connections[jid].disconnect()

    def process_command(self, iq):
        """
        Обработчик общих команд транспорта
        :param iq:
        :return:
        """
        parced = iq['body'].split(' ')
        jid = iq['from'].bare

        if parced[0] == '!help':
            self.gate_reply_message(iq, 'Available command:\n\n'
                                        '!help - Displays this text\n'
                                        '!login +123456789 - Initiates Telegram session\n'
                                        '!code 12345 - Entering one-time code during auth\n'
                                        '!password abc123 - Entering password during two-factor auth\n'
                                        '!list_sessions - List all created sessions at Telegram servers\n'
                                        '!delete_session 123 - Delete session\n'
                                        '!logout - Deletes current Telegram session at gate\n'
                                        '!reload_dialogs - Reloads dialogs list from Telegram\n\n'
                                        '!create_group - Initiates group creation\n'
                                        '!create_channel - Initiates channel creation\n\n'
                                        '!change_name first last - Changes your name in Telegram\n'
                                        '!change_username username - Changes your @username in Telegram\n'
                                        # '!blocked_users_list\n'
                                        # '!blocked_users_add\n'
                                        # '!blocked_users_remove\n'
                                        # '!last_seen_privacy_status\n'
                                        # '!last_seen_privacy_set\n'
                                        # '!last_seen_privacy_never_add\n'
                                        # '!last_seen_privacy_never_remove\n'
                                        # '!last_seen_privacy_always_add\n'
                                        # '!last_seen_privacy_always_remove\n'
                                        # '!group_invite_settings_status\n'
                                        # '!group_invite_settings_set\n'
                                        # '!group_invite_settings_add\n'
                                        # '!group_invite_settings_remove\n'
                                        # '!group_invite_settings_add\n'
                                        # '!group_invite_settings_remove\n'
                                        # '!account_selfdestruct_setting_status\n'
                                        # '!account_selfdestruct_setting_set\n'
                                    )
        elif parced[0] == '!login':  # --------------------------------------------------
            self.gate_reply_message(iq, 'Please wait...')
            self.spawn_tg_client(jid, parced[1])

            if self.tg_connections[jid].is_user_authorized():
                self.gate_reply_message(iq, 'You are already authenticated in Telegram.')
            else:
                self.tg_connections[jid].send_code_request(parced[1])
                self.gate_reply_message(iq, 'Gate is connected. Telegram should send SMS message to you.')
                self.gate_reply_message(iq, 'Please enter one-time code via !code 12345.')
        elif parced[0] in ['!code', '!password']:  # --------------------------------------------------
            if not self.tg_connections[jid].is_user_authorized():
                if parced[0] == '!code':
                    try:
                        self.gate_reply_message(iq, 'Trying authenticate...')
                        self.tg_connections[jid].sign_in(self.tg_phones[jid], parced[1])
                    except SessionPasswordNeededError:
                        self.gate_reply_message(iq, 'Two-factor authentication detected.')
                        self.gate_reply_message(iq, 'Please enter your password via !password abc123.')
                        return

                if parced[0] == '!password':
                    self.gate_reply_message(iq, 'Checking password...')
                    self.tg_connections[jid].sign_in(password=parced[1])

                if self.tg_connections[jid].is_user_authorized():
                    self.gate_reply_message(iq, 'Authentication successful. Initiating Telegram...')
                    self.init_tg(jid)
                    self.db_connection.execute("INSERT INTO accounts VALUES(?, ?)", (jid, self.tg_phones[jid],))
                else:
                    self.gate_reply_message(iq, 'Authentication failed.')
            else:
                self.gate_reply_message(iq, 'You are already authenticated. Please use !logout before new login.')
        elif parced[0] == '!list_sessions':  # --------------------------------------------------
            if not self.tg_connections[jid].is_user_authorized():
                self.gate_reply_message(iq, 'Error.')
                return

            sessions = self.tg_connections[jid].invoke(GetAuthorizationsRequest())
            print(sessions.__dict__)
        elif parced[0] == '!reload_dialogs':
            if not self.tg_connections[jid].is_user_authorized():
                self.gate_reply_message(iq, 'Error.')
                return
            self.tg_process_dialogs(jid)
            self.gate_reply_message(iq, 'Dialogs reloaded.')
        elif parced[0] == '!logout':  # --------------------------------------------------
            self.tg_connections[jid].log_out()
            self.db_connection.execute("DELETE FROM accounts WHERE jid = ?", (jid,))
            self.gate_reply_message(iq, 'Your Telegram session was deleted')
        else:  # --------------------------------------------------
            self.gate_reply_message(iq, 'Unknown command. Try !help for list all commands.')

    def process_chat_user_command(self, iq):
        parced = []

        if parced[0] == '!search':
            pass
        elif parced[0] == '!get_history':
            pass
        elif parced[0] == '!forward_messages':
            pass
        elif parced[0] == '!delete_messages':
            pass
        elif parced[0] == '!block_status':
            pass
        elif parced[0] == '!block_set':
            pass
        elif parced[0] == '!block_unser':
            pass
        elif parced[0] == '!clear_history':
            pass
        elif parced[0] == '!delete_conversation':
            pass
        elif parced[0] == '!help':
            pass

    def process_chat_group_command(self, iq):
        parced = []

        if parced[0] == '!search':
            pass
        elif parced[0] == '!get_history':
            pass
        elif parced[0] == '!forward_messages':
            pass
        elif parced[0] == '!delete_messages':
            pass
        elif parced[0] == '!pin_message':
            pass
        elif parced[0] == '!unpin_message':
            pass
        elif parced[0] == '!leave_group':
            pass
        elif parced[0] == '!add_members':
            pass
        elif parced[0] == '!bans_list':
            pass
        elif parced[0] == '!ban_user':
            pass
        elif parced[0] == '!unban_user':
            pass
        elif parced[0] == '!restrict_user':
            pass
        elif parced[0] == '!unrestrict_user':
            pass
        elif parced[0] == '!get_recent_actions':
            pass
        elif parced[0] == '!get_recent_actions':
            pass

    def spawn_tg_client(self, jid, phone):
        """
        Создает и инициализирует подключение к Telegram
        :param jid:
        :param phone:
        :return:
        """
        client = TelegramGateClient('a_'+phone, int(self.config['tg_api_id']), self.config['tg_api_hash'],
                                    self, jid, phone)
        client.connect()

        self.tg_connections[jid] = client
        self.tg_phones[jid] = phone

        if client.is_user_authorized():
            self.init_tg(jid)
            self.send_presence(pto=jid, pfrom=self.boundjid.bare, ptype='online', pstatus='connected')

    def init_tg(self, jid):
        """
        Инициализация транспорта для конкретного пользователя после подключения к Telegram
        :param jid: 
        :return: 
        """
        # Устанавливаем, что пользователь онлайн
        self.tg_connections[jid].invoke(UpdateStatusRequest(offline=False))

        # Получаем и обрабатываем список диалогов
        self.tg_process_dialogs(jid)

        # Регистрируем обработчик обновлений в Telegram
        self.tg_connections[jid].add_update_handler(self.tg_connections[jid].xmpp_update_handler)

    def tg_process_dialogs(self, jid):
        # Инициализируем словари для диалогов
        self.tg_dialogs[jid] = dict()
        self.tg_dialogs[jid]['raw'] = list()
        self.tg_dialogs[jid]['users'] = dict()
        self.tg_dialogs[jid]['groups'] = dict()
        self.tg_dialogs[jid]['supergroups'] = dict()

        # Оффсеты для получения диалогов
        last_peer = InputPeerEmpty()
        last_msg_id = 0
        last_date = None

        while True:  # В цикле по кускам получаем все диалоги
            dlgs = self.tg_connections[jid].invoke(GetDialogsRequest(offset_date=last_date, offset_id=last_msg_id,
                                                                     offset_peer=last_peer, limit=100))

            self.tg_dialogs[jid]['raw'].append(dlgs)

            for usr in dlgs.users:
                self.tg_dialogs[jid]['users'][usr.id] = usr
            for cht in dlgs.chats:
                if type(cht) in [Chat, ChatForbidden]:  # Старая группа
                    self.tg_dialogs[jid]['groups'][cht.id] = cht
                elif type(cht) in [Channel, ChannelForbidden]:  # Супергруппа
                    self.tg_dialogs[jid]['supergroups'][cht.id] = cht

            for dlg in dlgs.dialogs:
                if type(dlg.peer) is PeerUser:
                    usr = self.tg_dialogs[jid]['users'][dlg.peer.user_id]
                    vcard = self.plugin['xep_0054'].make_vcard()
                    u_jid = 'u' + str(usr.id) + '@' + self.boundjid.bare

                    if usr.deleted:
                        vcard['FN'] = 'Deleted account'
                        vcard['DESC'] = 'This user no longer exists in Telegram'
                    else:
                        vcard['FN'] = display_tg_name(usr.first_name, usr.last_name)
                        if usr.first_name:
                            vcard['N']['GIVEN'] = usr.first_name
                        if usr.last_name:
                            vcard['N']['FAMILY'] = usr.last_name
                        if usr.username:
                            vcard['DESC'] = 'Telegram Username: @' + usr.username

                            if usr.bot:
                                vcard['DESC'] += ' [Bot]'

                        vcard['NICKNAME'] = vcard['FN']

                    vcard['JABBERID'] = u_jid
                    self.plugin['xep_0054'].publish_vcard(jid=u_jid, vcard=vcard)
                    self.plugin['xep_0172'].publish_nick(nick=vcard['FN'], ifrom=u_jid)

                    self.send_presence(pto=jid, pfrom=u_jid, ptype='subscribe')

                    if usr.bot:
                        self.send_presence(pto=jid, pfrom=u_jid, pstatus='Bot')
                    else:
                        if type(usr.status) is UserStatusOnline:
                            self.send_presence(pto=jid, pfrom=u_jid)
                        elif type(usr.status) is UserStatusRecently:
                            self.send_presence(pto=jid, pfrom=u_jid, pshow='away', pstatus='Last seen recently')
                        elif type(usr.status) is UserStatusOffline:
                            self.send_presence(
                                pto=jid,
                                pfrom=u_jid,
                                ptype='xa',
                                pstatus=usr.status.was_online.strftime('Last seen at %H:%M %d/%m/%Y')
                            )
                        else:
                            self.send_presence(pto=jid, pfrom=u_jid, ptype='unavailable',
                                               pstatus='Last seen a long time ago')

                if type(dlg.peer) in [PeerChat, PeerChannel]:
                    g_type = ''
                    cht = None

                    if type(dlg.peer) is PeerChat:  # Старая группа
                        cht = self.tg_dialogs[jid]['groups'][dlg.peer.chat_id]
                        c_jid = 'g' + str(cht.id) + '@' + self.boundjid.bare
                        g_type = 'G'
                    elif type(dlg.peer) is PeerChannel:  # Супергруппа
                        cht = self.tg_dialogs[jid]['supergroups'][dlg.peer.channel_id]

                        if cht.broadcast:
                            g_type = 'C'
                            c_jid = 'c' + str(cht.id) + '@' + self.boundjid.bare
                        else:
                            g_type = 'SG'
                            c_jid = 's' + str(cht.id) + '@' + self.boundjid.bare

                    vcard = self.plugin['xep_0054'].make_vcard()
                    vcard['FN'] = '[{}] {}'.format(g_type, cht.title)
                    vcard['NICKNAME'] = vcard['FN']
                    vcard['JABBERID'] = c_jid
                    self.plugin['xep_0054'].publish_vcard(jid=c_jid, vcard=vcard)
                    self.plugin['xep_0172'].publish_nick(nick=vcard['FN'], ifrom=c_jid)

                    self.send_presence(pto=jid, pfrom=c_jid, ptype='subscribe')
                    self.send_presence(pto=jid, pfrom=c_jid)

            if len(dlgs.dialogs) == 0:  # Если все диалоги получены - прерываем цикл
                break
            else:  # Иначе строим оффсеты
                last_msg_id = dlgs.dialogs[-1].top_message  # Нужен последний id сообщения. Наркоманы.
                last_peer = dlgs.dialogs[-1].peer

                last_date = next(msg for msg in dlgs.messages  # Ищем дату среди сообщений
                                 if type(msg.to_id) is type(last_peer) and msg.id == last_msg_id).date

                if type(last_peer) is PeerUser:  # Пользователь
                    access_hash = self.tg_dialogs[jid]['users'][last_peer.user_id].access_hash
                    last_peer = InputPeerUser(last_peer.user_id, access_hash)
                elif type(last_peer) in [Chat, ChatForbidden]:  # Группа
                    last_peer = InputPeerChat(last_peer.chat_id)
                elif type(last_peer) in [Channel, ChannelForbidden]:  # Супергруппа
                    access_hash = self.tg_dialogs[jid]['supergroups'][last_peer.channel_id].access_hash
                    last_peer = InputPeerChannel(last_peer.channel_id, access_hash)

    def tg_process_unread_messages(self):
        pass

    def gate_reply_message(self, iq, msg):
        """
        Отправляет ответное сообщение от имени транспорта
        :param iq:
        :param msg:
        :return:
        """
        self.send_message(mto=iq['from'], mfrom=self.config['jid'], mtype='chat', mbody=msg)

    def init_database(self):
        """
        Инициализация БД
        :return:
        """
        def dict_factory(cursor, row):
            d = {}
            for idx, col in enumerate(cursor.description):
                d[col[0]] = row[idx]
            return d

        conn = sqlite3.connect(self.config['db_connect'], isolation_level=None, check_same_thread=False)
        conn.row_factory = dict_factory

        conn.execute("CREATE TABLE IF NOT EXISTS accounts("
                     "jid VARCHAR(255),"
                     "tg_phone VARCHAR(25)"
                     ")")

        # conn.execute("CREATE TABLE IF NOT EXISTS roster("
        #              "")

        return conn
