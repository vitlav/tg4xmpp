from telethon import TelegramClient
from telethon.utils import get_extension
from telethon.tl.types import UpdateShortMessage, UpdateShortChatMessage, UpdateEditMessage, UpdateDeleteMessages, \
                              UpdateNewMessage, UpdateUserStatus, UpdateShort, Updates, UpdateNewChannelMessage,\
                              UpdateChannelTooLong, UpdateDeleteChannelMessages, UpdateEditChannelMessage,\
                              UpdateUserName
from telethon.tl.types import InputPeerChat, InputPeerUser, InputPeerChannel, InputUser
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto, MessageMediaUnsupported, MessageMediaContact,\
                              MessageMediaGeo, MessageMediaEmpty, MessageMediaVenue
from telethon.tl.types import DocumentAttributeAnimated, DocumentAttributeAudio, DocumentAttributeFilename,\
                              DocumentAttributeSticker, DocumentAttributeVideo, DocumentAttributeHasStickers
from telethon.tl.types import Message, MessageService, MessageActionChannelCreate, MessageActionChannelMigrateFrom,\
                              MessageActionChatCreate, MessageActionChatAddUser, MessageActionChatDeleteUser,\
                              MessageActionChatEditTitle, MessageActionChatJoinedByLink, MessageActionChatMigrateTo,\
                              MessageActionPinMessage
from telethon.tl.types import UserStatusOnline, UserStatusOffline, UserStatusRecently
from telethon.tl.types import User, Chat, Channel
from telethon.tl.types import PeerUser, PeerChat, PeerChannel
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.messages import ReadHistoryRequest, GetFullChatRequest, GetMessagesRequest
from telethon.tl.functions.channels import ReadHistoryRequest as ReadHistoryChannel, GetParticipantRequest, GetMessagesRequest
from telethon.tl.functions.updates import GetDifferenceRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest

import os, threading, queue, hashlib, time, datetime
from xmpp_tg.utils import localtime, display_tg_name

import xmpp_tg.monkey 
import traceback


class TelegramGateClient(TelegramClient):
    def __init__(self, session, api_id, api_hash, xmpp_gate, jid, phone, proxy=None):
        super().__init__(session, api_id, api_hash, proxy=proxy, update_workers = 4)
        
        self.me = None

        self.xmpp_gate = xmpp_gate
        self.jid = jid
        self.phone = phone

        self._media_queue = queue.Queue()
        self._media_thread = threading.Thread(name='MediaDownloaderThread', target=self.media_thread_downloader)
        
        self._status_updates = dict()
        self._status_update_thread = threading.Thread(name = 'StatusUpdateThread', target = self.status_updater_thread)

        self._groups_users = dict()
        self._message_cache_users = dict()
        self._message_cache_groups = dict()
        self._message_cache_supergroups = dict()
        
        self._del_pts = 0
        

    def xmpp_update_handler(self, obj):
       
        """
        Main function: Telegram update handler.
        :param media:
        :return:
        """
        # print("We have received update for <%s>" % self.jid)
        # print(obj)

        # we have received some updates, so we're logined and can get <me> object and start mtd / upd threads #
        if not self.me:
           me = self.get_me()
           self.me = InputPeerUser(me.id, me.access_hash)
           self._media_thread.start()
           self._status_update_thread.start()

        nl = '\n' 

        try:
           
            # message from normal chat # 
            if type(obj) in [UpdateShortMessage] and not obj.out:

               fwd_from = self._process_forward_msg(obj) if obj.fwd_from else '' # process forward messages 
               self.gate_send_message( mfrom='u' + str(obj.user_id), mbody = '{}{}'.format(fwd_from, obj.message) )
               usr = self._get_user_information(obj.user_id) # get peer information
               self.invoke(ReadHistoryRequest( InputPeerUser(usr.id, usr.access_hash), obj.id )) # delivery report
               
            # message from normal group # 
            if type(obj) in [UpdateShortChatMessage] and not obj.out:
               fwd_from = self._process_forward_msg(obj) if obj.fwd_from else '' # process forward messages 
               usr = self._get_user_information(obj.from_id)
               nickname = display_tg_name(usr)
               
               # send message 
               self.gate_send_message(mfrom='g' + str(obj.chat_id), mbody ='[User: {}] {}{}'.format(nickname, fwd_from, obj.message) )
               self.invoke(ReadHistoryRequest(InputPeerChat(obj.chat_id), obj.id))
               
             
            # message from supergroup or media message #
            if type(obj) in [UpdateNewMessage, UpdateNewChannelMessage, UpdateEditMessage, UpdateEditChannelMessage] and not obj.message.out:
               
               cid = None
               msg = ''
               fwd_from = ''
               mid = obj.message.id

               
               # detect message type
               is_user = type(obj.message.to_id) is PeerUser
               is_group = type(obj.message.to_id) is PeerChat
               is_supergroup = type(obj.message.to_id) is PeerChannel
               
               
               # detect from id  
               if is_user:
                  cid = obj.message.from_id
                  user = self._get_user_information(cid)
                  peer = InputPeerUser(user.id, user.access_hash) 
                  prefix = 'u'  
                  prefix = 'b' if user.bot else prefix  
               elif is_group:
                  cid = obj.message.to_id.chat_id
                  peer = InputPeerChat(cid) 
                  prefix = 'g'
               elif is_supergroup:
                  cid = obj.message.to_id.channel_id
                  peer = InputPeerChannel(cid, self.xmpp_gate.tg_dialogs[self.jid]['supergroups'][cid].access_hash) if cid in self.xmpp_gate.tg_dialogs[self.jid]['supergroups'] else None
                  prefix = 's'
                  
               # our message #
               if type(obj.message) == MessageService:
                  obj.message.fwd_from, obj.message.post, obj.message.edit_date, obj.message.media = None, None, None, None
                  msg = self._process_info_msg(obj.message, peer)
               elif type(obj.message) == Message:
                  msg = obj.message.message
                                    

               # is forwarded?
               if obj.message.fwd_from:
                  fwd_from = self._process_forward_msg(obj.message)

               # maybe its channel? #
               if obj.message.post: 
                  prefix = 'c'

               # get sender information from chat info #
               if not is_user and not obj.message.post: 
                  usr = self._get_user_information(obj.message.from_id)
                  nickname = display_tg_name(usr)
                  msg = '[User: {}] {}'.format(nickname, msg) 

                  
               # message media #
               if obj.message.media:
                  msg = '{} {}'.format( msg, self._process_media_msg(obj.message.media) ) 
                  
               # edited #
               if obj.message.edit_date:
                  msg = '[Edited] {}'.format(msg)
               
               # send message #   
               self.gate_send_message(prefix + str(cid), mbody = '[MSG {}] {}{}'.format(mid, fwd_from, msg) )
               
               # delivery report
               if is_supergroup:
                  self.invoke(ReadHistoryChannel(peer, mid))
               else:
                  self.invoke(ReadHistoryRequest(peer, mid))

            # Status Updates #
            if type(obj) is UpdateUserStatus: 
               # process status update #
               if type(obj.status) is UserStatusOnline:
                  self._status_updates[str(obj.user_id)] = { 'status': None, 'message':  'Online' }
               elif type(obj.status) is UserStatusOffline:
                  status = 'away' if datetime.datetime.utcnow() - obj.status.was_online < datetime.timedelta(hours = self.xmpp_gate.accounts[self.jid]['status_xa_interval'] ) else 'xa'
                  self._status_updates[str(obj.user_id)] = { 'status': status, 'message':  localtime(obj.status.was_online).strftime('Last seen at %H:%M %d/%m/%Y') }
               elif type(obj.status) is UserStatusRecently:
                  self._status_updates[str(obj.user_id)] = { 'status': 'dnd', 'message':  'Last seen recently' }
               else:
                  pass

               
        except Exception:
            print('Exception occurs!')
            print(traceback.format_exc())

    def gate_send_message(self, mfrom, mbody):
        tg_from = int(mfrom[1:]) 
        if not tg_from in self.xmpp_gate.tg_dialogs[self.jid]['users'] and not tg_from in self.xmpp_gate.tg_dialogs[self.jid]['groups'] and not tg_from in self.xmpp_gate.tg_dialogs[self.jid]['supergroups']: # new contact appeared
           self.xmpp_gate.tg_process_dialogs( self.jid )

        self.xmpp_gate.send_message( mto=self.jid, mfrom=mfrom + '@' + self.xmpp_gate.config['jid'], mtype='chat', mbody=mbody)

    def generate_media_link(self, media):
        """
        Generates download link from media object
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
        Get document attribute.
        :param attributes:
        :param match:
        :return:
        """
        for attrib in attributes:
            if type(attrib) == match:
                return attrib
        return None
        
    def _get_user_information(self, uid):
       
       if uid in self.xmpp_gate.tg_dialogs[self.jid]['users']:
          return self.xmpp_gate.tg_dialogs[self.jid]['users'][uid]
          
       entity = self.get_entity(uid)
       if entity.access_hash:
          self.xmpp_gate.tg_dialogs[self.jid]['users'][uid] = entity 
          return entity 
       else:
          return {'first_name': 'Unknown', 'last_name': 'user', 'access_hash': -1, 'id': 0, 'bot': False}
           

    def _process_forward_msg(self, message):
        """
        Process forward message to find out from what user message is forwarded.
        :param message:
        :param users:
        :param channels:
        :return:
        """
        if message.fwd_from.from_id:  # from user
            
            usr = self._get_user_information(message.fwd_from.from_id)
            fwd_from = display_tg_name(usr)

        if message.fwd_from.channel_id:  # from channel
            fwd_from = 'Channel {}'.format(message.fwd_from.channel_id)

        # let's construct
        fwd_reply = '|Forwarded from [{}]|'.format(fwd_from)
        return fwd_reply

    def _process_media_msg(self, media):
        """
        Process message with media.
        :param media:
        :return:
        """
        msg = ''

        if type(media) is MessageMediaDocument:  # document
            attributes = media.document.attributes
            attributes_types = [type(a) for a in attributes]  

            size_text = '|Size: {:.2f} Mb'.format(media.document.size / 1024 / 1024)

            if media.document.size > self.xmpp_gate.config['media_max_download_size']:  # too big file
                g_link = {'link': 'File is too big to be downloaded via this gateway. Sorry.'}
            else: # add it to download queue if everything is ok
                g_link = self.generate_media_link(media)  
                self._media_queue.put({'media': media, 'file': g_link['name']})

            attr_fn = self.get_document_attribute(attributes, DocumentAttributeFilename)
            if attr_fn:  # file has filename attrib
                msg = '[FileName:{}{}] {}'.format(attr_fn.file_name, size_text, g_link['link'])
            else:
                msg = g_link['link']

            if DocumentAttributeSticker in attributes_types:  # sticker
                smile = self.get_document_attribute(attributes, DocumentAttributeSticker).alt
                msg = '[Sticker {}] {}'.format(smile, g_link['link'])  
            elif DocumentAttributeAudio in attributes_types:  # audio file
                attr_a = self.get_document_attribute(attributes, DocumentAttributeAudio)

                if attr_a.voice:  # voicemessage
                    msg = '[VoiceMessage|{} sec] {}'.format(attr_a.duration, g_link['link'])  
                else:  # other audio
                    attr_f = self.get_document_attribute(attributes, DocumentAttributeFilename)
                    msg = '[Audio|File:{}{}|Performer:{}|Title:{}|Duration:{} sec] {}' \
                        .format(attr_f.file_name, size_text, attr_a.performer, attr_a.title,
                                attr_a.duration, g_link['link'])
            elif DocumentAttributeVideo in attributes_types:  # video
                video_type = 'Video'
                video_file = ''
                caption = ''

                if DocumentAttributeAnimated in attributes_types:  # it is "gif"
                    video_type = 'AnimatedVideo'

                if DocumentAttributeFilename in attributes_types:  # file has filename attrib
                    attr_v = self.get_document_attribute(attributes, DocumentAttributeFilename)
                    video_file = '|File:{}'.format(attr_v.file_name)

                if hasattr(media, 'caption'):
                    caption = media.caption + ' '

                msg = '[{}{}{}] {}{}'.format(video_type, video_file, size_text, caption, g_link['link'])
        elif type(media) is MessageMediaPhoto:  # photo (jpg)
            g_link = self.generate_media_link(media)
            msg = g_link['link']

            self._media_queue.put({'media': media, 'file': g_link['name']})

            if hasattr(media, 'caption'):  # caption
                msg = '{} {}'.format(media.caption, msg)

        elif type(media) is MessageMediaContact:  # contact
            msg = 'First name: {} / Last name: {} / Phone: {}'\
                .format(media.first_name, media.last_name, media.phone_number)
        elif type(media) in [MessageMediaGeo, MessageMediaVenue]:  # address
            map_link_template = 'https://maps.google.com/maps?q={0:.4f},{1:.4f}&ll={0:.4f},{1:.4f}&z=16'
            map_link = map_link_template.format(media.geo.lat, media.geo.long)
            msg = map_link

            if type(media) is MessageMediaVenue:
                msg = '[Title: {}|Address: {}|Provider: {}] {}'.format(media.title, media.address, media.provider, msg)

        return msg

    def _process_info_msg(self, message, peer):
        """
        Information messages.
        :param message:
        :param users:
        :return:
        """
        
        msg = ''
        usr = self._get_user_information(message.from_id)
        nickname = display_tg_name(usr)

        # supergroup created #
        if type(message.action) is MessageActionChannelCreate:
            pass

        # group created # 
        elif type(message.action) is MessageActionChatCreate:
            pass

        # user added #
        elif type(message.action) is MessageActionChatAddUser:
            added_users = []
            for user_id in message.action.users:
               usr = self._get_user_information(user_id)
               added_users.append(display_tg_name(usr))
            
            msg = 'User [{}] has just invited [{}]'.format(nickname, ','.join(added_users))   

        # user exit #
        elif type(message.action) is MessageActionChatDeleteUser:            
            usr = self._get_user_information(message.action.user_id)
            msg = 'User [{}] has just left the room'.format(display_tg_name(usr))

        # user joined #
        elif type(message.action) is MessageActionChatJoinedByLink:
            usr = self._get_user_information(message.action.user_id)
            msg = 'User [{}] joined the room'.format(display_tg_name(usr))

        # chat name modified #
        elif type(message.action) is MessageActionChatEditTitle:
            msg = 'User [{}] changed title to [{}]'.format(nickname, message.action.title)

        # pinned message
        elif type(message.action) is MessageActionPinMessage:
            pinned_mid = message.reply_to_msg_id  # target message
            message_req = self.invoke(GetMessagesRequest(peer, [pinned_mid]))
            if len(message_req.messages) > 0:
                pinned_message = message_req.messages[0].message
                pinned_from = self._get_user_information(message_req.messages[0].from_id)
                msg = 'User [{}] pinned message: [{}]: {}'.format(nickname, display_tg_name(pinned_from), pinned_message)

        # group converted to supergroup
        elif type(message.action) in [MessageActionChatMigrateTo, MessageActionChannelMigrateFrom]:
            pass

        return msg

    def media_thread_downloader(self):
        """
        Media downloader thread
        :return:
        """
        while True:
            try:
                if self._media_queue.empty():  # queue is empty
                    time.sleep(0.1)
                else:  # queue is not empty
                    print('MTD ::: Queue is not empty. Downloading...')
                    media = self._media_queue.get()
                    file_path = self.xmpp_gate.config['media_store_path'] + media['file']
                    if os.path.isfile(file_path):
                        print('MTD ::: File already exists')
                    else:
                        self.download_media(media['media'], file_path, False)
                        print('MTD ::: Media downloaded')
            except Exception:
                print(traceback.format_exc())
                
    def status_updater_thread(self):
       
        while True:
            try:
                if len(self._status_updates) > 0:
                    for uid, status in self._status_updates.items():
                        self.xmpp_gate.send_presence( pto=self.jid, pfrom='u'+str(uid)+'@'+self.xmpp_gate.config['jid'], pshow = status['status'], pstatus = status['message'] )
            except Exception:
                print(traceback.format_exc())

            self._status_updates = dict()
            time.sleep( self.xmpp_gate.accounts[self.jid]['status_update_interval'])
