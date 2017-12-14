from telethon import TelegramClient
from telethon.utils import get_extension
from telethon.tl.types import UpdateShortMessage, UpdateShortChatMessage, UpdateEditMessage, UpdateDeleteMessages, \
                              UpdateNewMessage, UpdateUserStatus, UpdateShort, UpdatesTg, UpdateNewChannelMessage,\
                              UpdateChannelTooLong, UpdateDeleteChannelMessages, UpdateEditChannelMessage,\
                              UpdateUserName
from telethon.tl.types import InputPeerChat, InputPeerUser, InputPeerChannel, InputUser
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto, MessageMediaUnsupported, MessageMediaContact,\
                              MessageMediaGeo, MessageMediaEmpty, MessageMediaVenue
from telethon.tl.types import DocumentAttributeAnimated, DocumentAttributeAudio, DocumentAttributeFilename,\
                              DocumentAttributeSticker, DocumentAttributeVideo, DocumentAttributeHasStickers
from telethon.tl.types import MessageService, MessageActionChannelCreate, MessageActionChannelMigrateFrom,\
                              MessageActionChatCreate, MessageActionChatAddUser, MessageActionChatDeleteUser,\
                              MessageActionChatEditTitle, MessageActionChatJoinedByLink, MessageActionChatMigrateTo,\
                              MessageActionPinMessage
from telethon.tl.types import UserStatusOnline, UserStatusOffline, UserStatusRecently
from telethon.tl.types import User, Chat, Channel
from telethon.tl.types import PeerUser, PeerChat, PeerChannel
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.messages import ReadHistoryRequest, GetFullChatRequest
from telethon.tl.functions.channels import ReadHistoryRequest as ReadHistoryChannel
from telethon.tl.functions.updates import GetDifferenceRequest

import hashlib
import os
import queue
import threading
from time import sleep
from xmpp_tg.utils import display_tg_name

from .utils import var_dump
import traceback


class TelegramGateClient(TelegramClient):
    def __init__(self, session, api_id, api_hash, xmpp_gate, jid, phone, proxy=None):
        super().__init__(session, api_id, api_hash, proxy)

        self.xmpp_gate = xmpp_gate
        self.jid = jid
        self.phone = phone
        self.user_options = {'nl_after_info': True}

        self._media_queue = queue.Queue()
        self._media_thread = threading.Thread(name='MediaDownloaderThread', target=self.media_thread_downloader)
        self._media_thread.start()

        self._groups_users = dict()
        self._message_cache_users = dict()
        self._message_cache_groups = dict()
        self._message_cache_supergroups = dict()

        self._del_pts = 0

    def xmpp_update_handler(self, obj):
        print('new update for ' + self.jid)
        print(type(obj), obj.__dict__)

        '''
        Боты
        Сделать запоминание ростера в бд
        Сделать лучше хендлинг ошибок
        Доделать все типы информационных сообщений
        Сделать джойны по линкам в чаты/каналы
        Сделать поиск и добавление пользователей
        Сделать листание истории
        Сделать отправку всех непрочтенных сообщений при входе
        '''

        # Здесь будет очень длинный пиздец ^__^

        nl = '\n' if self.user_options['nl_after_info'] else ''

        try:
            if type(obj) is UpdatesTg:  # Какой-то общий тип обновления (всегда большое со списками)
                print('UpdatesTg')

                # Делаем разбор пользователей/чатов, которые учавствуют в апдейте
                updates_users = {usr.id: usr for usr in obj.users}
                updates_groups = {}
                updates_channels = {}
                updates_supergroups = {}
                updates_type_channels = {}  # Супегруппы и каналы вместе

                for chat in obj.chats:
                    if type(chat) is Chat:  # Обычная группа
                        updates_groups[chat.id] = chat
                    elif type(chat) is Channel:
                        if chat.broadcast:  # Канал
                            updates_channels[chat.id] = chat
                        else:  # Супегруппа
                            updates_supergroups[chat.id] = chat
                        updates_type_channels[chat.id] = chat

                # -------------------------------------------------------------------------------------------

                for update in obj.updates:  # Апдейт содержит список с апдейтами
                    # Новое сообщение или отредактированное в супегруппе или канале
                    # А так же отредактированные и новые сообщения с медиа в ЛС и группах
                    if type(update) in [UpdateNewChannelMessage, UpdateEditChannelMessage, UpdateNewMessage,
                                        UpdateEditMessage]:
                        if update.message.out:  # Игнорируем исходящее сообщение
                            return

                        uid = update.message.from_id  # Будет None, если канал, а так же post=True
                        mid = update.message.id
                        cid = None
                        is_post = update.message.post
                        usr = updates_users[uid] if uid else None
                        nickname = display_tg_name(usr.first_name, usr.last_name) if usr else None

                        from_type = 'c' if is_post else 's'
                        msg = ''
                        alt_msg = None
                        edited = ''
                        fwd_reply = ''
                        orig_msg = None

                        is_user = type(update.message.to_id) is PeerUser
                        is_group = type(update.message.to_id) is PeerChat
                        is_supergroup = type(update.message.to_id) is PeerChannel

                        if is_user:
                            cid = update.message.from_id
                        elif is_group:
                            cid = update.message.to_id.chat_id
                        elif is_supergroup:
                            cid = update.message.to_id.channel_id

                        if type(update.message) is MessageService:  # Сервисные уведомления в чате
                            print('messageService detected')
                            with open('/home/sofia/tgdebug/' + str(mid) + '.txt', 'w') as f:
                                f.write(var_dump(obj))
                            ##################################################################
                            alt_msg = self._process_info_msg(update.message, obj.users)
                        else:  # Обычное сообщение в чате
                            msg = update.message.message

                            if update.message.media:
                                print('media detected')
                                #######################################################
                                msg = '[{}] {}'.format(
                                    update.message.media.__class__.__name__,
                                    '{} {}'.format(self._process_media_msg(update.message.media), msg)
                                )

                            if update.message.reply_to_msg_id:
                                fwd_reply = '|Reply to MID: {}'.format(update.message.reply_to_msg_id)
                                reply_mid = update.message.reply_to_msg_id
                                orig_msg = self.get_cached_message(cid, reply_mid, is_user, is_group, is_supergroup)

                            if update.message.fwd_from:  # Пересланное сообщение
                                fwd_reply = self._process_forward_msg(update.message, updates_users, updates_channels)

                        if update.message.edit_date:  # Если новое - отмечаем прочитанным
                            edited = '|Edited'

                        if alt_msg is None:
                            if is_post or type(update.message.to_id) is PeerUser:
                                header = '[MID:{}{}{}] '.format(mid, fwd_reply, edited)
                            else:
                                header = '[User: {}|UID:{}|MID:{}{}{}] {}'\
                                    .format(nickname, uid, mid, fwd_reply, edited, nl)

                            alt_msg = '{}{}'.format(header, msg)

                            self.set_cached_message(  # Кэшируем без цитаты
                                cid, mid, alt_msg,
                                user=is_user, group=is_group, supergroup=is_supergroup
                            )

                            if orig_msg is not None:  # Перестраиваем сообщение уже с цитатой
                                alt_msg = '{}> {}\n{}'.format(header, orig_msg.replace('\n', '\n> '), msg)

                        if is_user:
                            self.gate_send_message(
                                mfrom='u' + str(cid),
                                mbody=alt_msg
                            )

                            if False:  # Зарезервируем рекурсивные цитаты под опцию
                                self.set_cached_message(cid, mid, alt_msg, user=True)

                            if not update.message.edit_date:
                                self.invoke(ReadHistoryRequest(  # Отмечаем прочитанным
                                   InputPeerUser(usr.id, usr.access_hash),
                                   mid
                                ))
                        elif is_group:
                            self.gate_send_message(
                                mfrom='g' + str(update.message.to_id.chat_id),
                                mbody=alt_msg
                            )

                            if False: # ...
                                self.set_cached_message(cid, mid, alt_msg, group=True)

                            if not update.message.edit_date:
                                self.invoke(ReadHistoryRequest(InputPeerChat(cid), mid))
                        elif is_supergroup:
                            self.gate_send_message(
                                mfrom=from_type + str(cid),
                                mbody=alt_msg
                            )

                            if False: # ...
                                self.set_cached_message(cid, mid, alt_msg, supergroup=True)

                            if not update.message.edit_date:
                                access_hash = updates_type_channels[cid].access_hash
                                self.invoke(ReadHistoryChannel(InputPeerChannel(cid, access_hash), mid))
                    elif type(update) is UpdateDeleteChannelMessages:  # Удаленные сообщения в супергруппе/канале
                        channel_id = update.channel_id
                        channel_type = 's'

                        if self.xmpp_gate.tg_dialogs[self.jid]['supergroups'][channel_id].broadcast:  # А может канал?
                            channel_type = 'c'

                        self.gate_send_message(
                            mfrom=channel_type + str(channel_id),
                            mbody='[Deleted messages IDs: {}]'.format(', '.join([str(mid) for mid in update.messages]))
                        )
                    elif type(update) is UpdateDeleteMessages:
                        # Этот ивент обновления присылается при удалении сообщения в личном сообщении или группе.
                        # Только id сообщения. Нет информации об диалоге/пользователе/группе.
                        pass

                if type(obj.updates) is list and type(obj.updates[0]) is UpdateChannelTooLong:
                    print('too long')

            # ***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***

            if type(obj) is UpdateShort:  # Тоже какой-то общий тип обновления (всегда маленькое)
                if type(obj.update) is UpdateUserStatus:  # Обновление статуса пользователя в сети Tg
                    print('UpdateUserStatus')

                    if type(obj.update.status) is UserStatusOnline:
                        self.xmpp_gate.send_presence(
                            pto=self.jid,
                            pfrom='u'+str(obj.update.user_id)+'@'+self.xmpp_gate.config['jid']
                        )
                    elif type(obj.update.status) is UserStatusOffline:
                        self.xmpp_gate.send_presence(
                            pto=self.jid,
                            pfrom='u'+str(obj.update.user_id)+'@'+self.xmpp_gate.config['jid'],
                            ptype='xa',
                            pstatus=obj.update.status.was_online.strftime('Last seen at %H:%M %d/%m/%Y')
                        )
                    elif type(obj.update.status) is UserStatusRecently:
                        self.xmpp_gate.send_presence(
                            pto=self.jid,
                            pfrom='u' + str(obj.update.user_id) + '@' + self.xmpp_gate.config['jid'],
                            pstatus='Last seen recently'
                        )
                    else:
                        print(type(obj.update.status))
                        print(obj.update.status.__dict__)

                elif type(obj.update) is UpdateDeleteChannelMessages:  # Удаленное сообщение в супергруппе
                    if obj.update.pts > self._del_pts:  # Фильтруем дубли обновлений
                        channel_id = obj.update.channel_id
                        channel_type = 's'

                        if self.xmpp_gate.tg_dialogs[self.jid]['supergroups'][channel_id].broadcast:
                            channel_type = 'c'

                        msg = '[Deleted messages IDs: {}]'.format(', '.join([str(mid) for mid in obj.update.messages]))

                        self.gate_send_message(
                            mfrom=channel_type + str(channel_id),
                            mbody=msg
                        )

                    self._del_pts = obj.update.pts

            # ***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***^__^***

            # Входящее сообщение в ЛС или группу (без медиа вложений)
            if type(obj) in [UpdateShortMessage, UpdateShortChatMessage] and not obj.out:
                fwd_reply = ''
                nickname = ''

                if type(obj) is UpdateShortChatMessage:
                    # Так как в апдейте есть только ID пользователя, то запрашиваем с сервера информацию о группе
                    if obj.from_id not in self._groups_users:
                        chat_info = self.invoke(GetFullChatRequest(obj.chat_id))

                        for usr in chat_info.users:
                            self._groups_users[usr.id] = usr

                    nickname = display_tg_name(self._groups_users[obj.from_id].first_name,
                                               self._groups_users[obj.from_id].last_name)

                if obj.reply_to_msg_id:
                    fwd_reply = '|Reply to MID: {}'.format(obj.reply_to_msg_id)

                if obj.fwd_from:
                    full_update = self.invoke(GetDifferenceRequest(obj.pts - 1, obj.date, -1, 1))

                    fwd_reply = self._process_forward_msg(
                        full_update.new_messages[0],
                        {usr.id: usr for usr in full_update.users},
                        {}
                    )

                if type(obj) is UpdateShortMessage:
                    self.gate_send_message(
                        mfrom='u' + str(obj.user_id),
                        mbody='[MID:{}{}] {}'.format(obj.id, fwd_reply, obj.message)
                    )

                    if obj.user_id in self.xmpp_gate.tg_dialogs[self.jid]['users']:
                        usr = self.xmpp_gate.tg_dialogs[self.jid]['users'][obj.user_id]
                        self.invoke(ReadHistoryRequest(  # Отмечаем прочитанным
                           InputPeerUser(usr.id, usr.access_hash),
                           obj.id
                        ))
                elif type(obj) is UpdateShortChatMessage:
                    self.gate_send_message(
                        mfrom='g' + str(obj.chat_id),
                        mbody='[User: {}|UID:{}|MID:{}{}] {}'.format(nickname, obj.from_id, obj.id, fwd_reply,
                                                                     obj.message)
                    )

                    self.invoke(ReadHistoryRequest(InputPeerChat(obj.chat_id), obj.id))

        except Exception:
            print('Exception occurs!')
            print(traceback.format_exc())

        print(' ')

    def gate_send_message(self, mfrom, mbody):
        self.xmpp_gate.send_message(
            mto=self.jid,
            mfrom=mfrom + '@' + self.xmpp_gate.config['jid'],
            mtype='chat',
            mbody=mbody
        )

    def generate_media_link(self, media):
        """
        Генерирует будующее имя и ссылку на скачиваемое медиа-вложения из сообщения
        :param media:
        :return:
        """
        if type(media) is MessageMediaPhoto:
            media_id = media.photo.id
        elif type(media) is MessageMediaDocument:
            media_id = media.document.id
        else:
            return None

        ext = get_extension(media)
        if ext == '.oga':
            ext = '.ogg'

        file_name = hashlib.new('sha256')
        file_name.update(str(media_id).encode('ascii'))
        file_name.update(str(os.urandom(2)).encode('ascii'))
        file_name = file_name.hexdigest() + ext

        link = self.xmpp_gate.config['media_web_link_prefix'] + file_name

        return {'name': file_name, 'link': link}

    @staticmethod
    def get_document_attribute(attributes, match):
        """
        Находит заданных аттрибут в списке. Используется при разборе медиа-вложений типа Документ.
        :param attributes:
        :param match:
        :return:
        """
        for attrib in attributes:
            if type(attrib) == match:
                return attrib
        return None

    @staticmethod
    def _process_forward_msg(message, users, channels):
        """
        Обрабатывает информацию в пересланном сообщении (от кого оно и/или из какого канала). Требует дополнительно
        предоставление информации об пользователях/каналах.
        :param message:
        :param users:
        :param channels:
        :return:
        """
        if message.fwd_from.from_id:  # От пользователя
            fwd_user_id = message.fwd_from.from_id
            fwd_user = users[fwd_user_id]
            fwd_user_nickname = display_tg_name(fwd_user.first_name, fwd_user.last_name)

        if message.fwd_from.channel_id:  # От канала
            fwd_channel_id = message.fwd_from.channel_id
            fwd_channel_title = channels[fwd_channel_id].title

        # Теперь строим
        if message.fwd_from.from_id and message.fwd_from.channel_id:  # Неанонимное сообщение в канале
            fwd_reply = '|Forwarded from [{}] (UID:{}) at [{}] (CID:{})|Views: {}'\
                .format(fwd_user_nickname, fwd_user_id, fwd_channel_title, fwd_channel_id, message.views)
        elif message.fwd_from.from_id:  # Сообщение пользователя
            fwd_reply = '|Forwarded from [{}] (UID:{})'.format(fwd_user_nickname, fwd_user_id)
        elif message.fwd_from.channel_id:  # Анонимное сообщение в канале
            fwd_reply = '|Forwarded from [{}] (CID:{})|Views: {}' \
                .format(fwd_channel_title, fwd_channel_id, message.views)

        return fwd_reply

    def _process_media_msg(self, media):
        """
        Обрабатывает медиа-вложения в сообщениях. Добавляет их в очередь на загрузку. Производит разбор с генерацию
        готового для вывода сообщения с информацией о медиа и сгенерированной ссылкой на него.
        :param media:
        :return:
        """
        msg = ''
        print(var_dump(media))

        if type(media) is MessageMediaDocument:  # Документ или замаскированная сущность
            attributes = media.document.attributes
            attributes_types = [type(a) for a in attributes]  # Документами могут быть разные вещи и иметь аттрибуты

            size_text = '|Size: {:.2f} Mb'.format(media.document.size / 1024 / 1024)

            if media.document.size > self.xmpp_gate.config['media_max_download_size']:  # Не загружаем большие файлы
                g_link = {'link': 'File is too big to be downloaded via Telegram <---> XMPP Gateway. Sorry.'}
            else:
                g_link = self.generate_media_link(media)  # Добавляем файл в очередь на загрузку в отдельном потоке
                self._media_queue.put({'media': media, 'file': g_link['name']})

            attr_fn = self.get_document_attribute(attributes, DocumentAttributeFilename)
            if attr_fn:  # Если есть оригинальное имя файла, то выводим
                msg = '[FileName:{}{}] {}'.format(attr_fn.file_name, size_text, g_link['link'])
            else:
                msg = g_link['link']

            if DocumentAttributeSticker in attributes_types:  # Стикер
                smile = self.get_document_attribute(attributes, DocumentAttributeSticker).alt
                msg = '[Sticker {}] {}'.format(smile, g_link['link'])  # У стикеров свой формат вывода
            elif DocumentAttributeAudio in attributes_types:  # Аудио файл / Голосовое сообщение
                attr_a = self.get_document_attribute(attributes, DocumentAttributeAudio)

                if attr_a.voice:  # Голосовое сообщение
                    msg = '[VoiceMessage|{} sec] {}'.format(attr_a.duration, g_link['link'])  # Тоже свой формат
                else:  # Приложенный аудиофайл, добавляем возможную информацию из его тегов
                    attr_f = self.get_document_attribute(attributes, DocumentAttributeFilename)
                    msg = '[Audio|File:{}{}|Performer:{}|Title:{}|Duration:{} sec] {}' \
                        .format(attr_f.file_name, size_text, attr_a.performer, attr_a.title,
                                attr_a.duration, g_link['link'])
            elif DocumentAttributeVideo in attributes_types:  # Видео
                video_type = 'Video'
                video_file = ''
                caption = ''

                if DocumentAttributeAnimated in attributes_types:  # Проверка на "gif"
                    video_type = 'AnimatedVideo'

                if DocumentAttributeFilename in attributes_types:  # Если есть оригинальное имя файла - указываем
                    attr_v = self.get_document_attribute(attributes, DocumentAttributeFilename)
                    video_file = '|File:{}'.format(attr_v.file_name)

                if media.caption:
                    caption = media.caption + ' '

                # Тоже свой формат
                msg = '[{}{}{}] {}{}'.format(video_type, video_file, size_text, caption, g_link['link'])
        elif type(media) is MessageMediaPhoto:  # Фотография (сжатая, jpeg)
            g_link = self.generate_media_link(media)
            msg = g_link['link']

            self._media_queue.put({'media': media, 'file': g_link['name']})

            if media.caption:  # Если есть описание - указываем
                msg = '{} {}'.format(media.caption, msg)

        elif type(media) is MessageMediaContact:  # Контакт (с номером)
            msg = 'First name: {} / Last name: {} / Phone: {}'\
                .format(media.first_name, media.last_name, media.phone_number)
        elif type(media) in [MessageMediaGeo, MessageMediaVenue]:  # Адрес на карте
            map_link_template = 'https://maps.google.com/maps?q={0:.4f},{1:.4f}&ll={0:.4f},{1:.4f}&z=16'
            map_link = map_link_template.format(media.geo.lat, media.geo.long)
            msg = map_link

            if type(media) is MessageMediaVenue:
                msg = '[Title: {}|Address: {}|Provider: {}] {}'.format(media.title, media.address, media.provider, msg)

        return msg

    @staticmethod
    def _process_info_msg(message, users):
        """
        Обрабатывает информационные сообщения в групповых чатах. Возвращает готовое для вывода сообщение.
        :param message:
        :param users:
        :return:
        """
        alt_msg = None
        nickname = display_tg_name(users[0].first_name, users[0].last_name)
        uid = users[0].id

        # MessageActionChatEditPhoto

        # Создана супергруппа
        if type(message.action) is MessageActionChannelCreate:
            # Пока нет смысла - поддержка каналов не реализована
            pass
        # Создана группа
        elif type(message.action) is MessageActionChatCreate:
            pass
        # Добавлен пользователь в чат
        elif type(message.action) is MessageActionChatAddUser:
            if len(users) == 2:  # Кто-то добавил другого пользователя
                j_name = display_tg_name(users[1].first_name, users[1].last_name)
                j_uid = users[1].id
                alt_msg = 'User [{}] (UID:{}) added [{}] (UID:{})'.format(nickname, uid,
                                                                          j_name, j_uid)
            else:  # Пользователь вошел сам
                alt_msg = 'User [{}] (UID:{}) joined'.format(nickname, uid)
        # Пользователь удален/вышел/забанен
        elif type(message.action) is MessageActionChatDeleteUser:
            pass
        # Пользователь вошел по инвайт ссылке
        elif type(message.action) is MessageActionChatJoinedByLink:
            alt_msg = 'User [{}] (UID:{}) joined via invite link'.format(nickname, uid)
        # Изменено название чата
        elif type(message.action) is MessageActionChatEditTitle:
            g_title = message.action.title
            alt_msg = 'User [{}] (UID:{}) changed title to [{}]'.format(nickname, uid, g_title)
        # Прикреплено сообщение в чате
        elif type(message.action) is MessageActionPinMessage:
            # Notify all members реализовано путем указания, что пользователя упомянули,
            # то есть флаг mentioned=True. Но для транспорта он не имеет смысла.
            p_mid = message.reply_to_msg_id  # Наркоманы
            alt_msg = 'User [{}] (UID:{}) pinned message with MID:{}'.format(nickname, uid, p_mid)
        # Группа была преобразована в супергруппу
        elif type(message.action) is MessageActionChatMigrateTo:
            # Это сложный ивент, который ломает текущую реализацию хендлинга
            # (ибо в доках, которых нет, не сказано, что так можно было)
            # Пусть полежит до рефакторинга
            pass
        # Супергруппа была технически создана из группы
        elif type(message.action) is MessageActionChannelMigrateFrom:
            # ---...---...---
            # ---...---...---
            # ---...---...---
            pass

        return alt_msg

    def get_cached_message(self, dlg_id, msg_id, user=False, group=False, supergroup=False):
        """
        Получает из кэша сообщение диалога указанной группы (для работы цитат в последних сообщениях)
        :param dlg_id:
        :param msg_id:
        :param user:
        :param group:
        :param supergroup:
        :return:
        """
        if user:
            obj = self._message_cache_users
        elif group:
            obj = self._message_cache_groups
        elif supergroup:
            obj = self._message_cache_supergroups
        else:
            return None

        if dlg_id in obj:
            if msg_id in obj[dlg_id]:
                return obj[dlg_id][msg_id]

        return None

    def set_cached_message(self, dlg_id, msg_id, msg, user=False, group=False, supergroup=False):
        """
        Кэширует сообщение из диалога указанной группы (для работы цитат в последних сообщениях)
        :param dlg_id:
        :param msg_id:
        :param msg:
        :param user:
        :param group:
        :param supergroup:
        :return:
        """
        if user:
            obj = self._message_cache_users
        elif group:
            obj = self._message_cache_groups
        elif supergroup:
            obj = self._message_cache_supergroups
        else:
            return

        if dlg_id not in obj:
            obj[dlg_id] = dict()

        obj[dlg_id][msg_id] = msg

        # Удаляем старые сообщения из кэша
        if len(obj[dlg_id]) > self.xmpp_gate.config['messages_max_max_cache_size']:
            del obj[dlg_id][sorted(obj[dlg_id].keys())[0]]

    def media_thread_downloader(self):
        """
        Этот метод запускается в отдельном потоке и скачивает по очереди все медиа вложения из сообщений
        :return:
        """
        while True:
            try:
                if self._media_queue.empty():  # Нет медиа в очереди - спим
                    sleep(0.1)
                else:  # Иначе скачиваем медиа
                    print('MTD ::: Queue is not empty. Downloading...')
                    media = self._media_queue.get()
                    file_path = self.xmpp_gate.config['media_store_path'] + media['file']
                    if os.path.isfile(file_path):
                        print('MTD ::: File already exists')
                    else:
                        self.download_msg_media(media['media'], file_path, False)
                        print('MTD ::: Media downloaded')
            except Exception:
                print(traceback.format_exc())
