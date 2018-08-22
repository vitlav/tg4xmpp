import re, sys, os, io, sqlite3, hashlib, time, datetime
import xml.etree.ElementTree as ET

from sleekxmpp.componentxmpp import ComponentXMPP
from sleekxmpp import Presence, Message

from telethon.tl.functions.messages import GetDialogsRequest, SendMessageRequest, SendMediaRequest, EditMessageRequest, DeleteMessagesRequest, ImportChatInviteRequest, GetFullChatRequest, AddChatUserRequest, DeleteChatUserRequest, CreateChatRequest, DeleteHistoryRequest
from telethon.tl.functions.account import UpdateStatusRequest, GetAuthorizationsRequest, UpdateProfileRequest, UpdateUsernameRequest
from telethon.tl.functions.contacts import DeleteContactRequest, BlockRequest, UnblockRequest, ImportContactsRequest
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest, InviteToChannelRequest, EditBannedRequest, CreateChannelRequest, DeleteMessagesRequest as DeleteMessagesChannel

from telethon.tl.types import InputPeerEmpty, InputPeerUser, InputPeerChat, InputPeerChannel, InputPhoneContact, InputMediaPhotoExternal
from telethon.tl.types import User, Chat, Channel
from telethon.tl.types import PeerChannel, PeerChat, PeerUser, Chat, ChatForbidden, Channel, ChannelForbidden, ChannelBannedRights
from telethon.tl.types import UserStatusOnline, UserStatusRecently, UserStatusOffline
from telethon.tl.types import Updates, UpdateShortSentMessage, UpdateMessageID

from telethon.tl.types.messages import Dialogs, DialogsSlice

from telethon.helpers import generate_random_long
from telethon.errors import SessionPasswordNeededError

from xmpp_tg.mtproto import TelegramGateClient
from xmpp_tg.utils import var_dump, display_tg_name, get_contact_jid, localtime
import xmpp_tg.monkey  # monkeypatch

class XMPPTelegram(ComponentXMPP):
    """
    Main XMPPTelegram class.
    """

    def __init__(self, config_dict):
        """
        Transport initialization
        :param config_dict:
        """

        ComponentXMPP.__init__(self, config_dict['jid'], config_dict['secret'], config_dict['server'],
                               config_dict['port'])
                               
        self.auto_authorize = True
        # self.auto_subscribe = True
        
        self.config = config_dict
        self.accounts = dict() # personal configuration per JID
        self.tg_connections = dict()
        self.tg_phones = dict()
        self.tg_dialogs = dict()
        self.contact_list = dict()

        self.db_connection = self.init_database()

        self.register_plugin('xep_0030')  # Service discovery
        self.register_plugin('xep_0054')  # VCard-temp
        self.register_plugin('xep_0172')  # NickNames

        self.add_event_handler('message', self.message)
        self.add_event_handler('presence_unsubscribe', self.event_presence_unsub)
        self.add_event_handler('presence_unsubscribed', self.event_presence_unsub)
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
        vcard['DESC'] = 'Send !help for information'
        self.plugin['xep_0054'].publish_vcard(jid=self.boundjid.bare, vcard=vcard)

    def __del__(self):
        """
        Destructor
        :return:
        """
        self.db_connection.close()

    def handle_start(self, arg):
        """
        Successful connection to Jabber server
        :param arg:
        :return:
        """
        users = self.db_connection.execute("SELECT * FROM accounts").fetchall()
        for usr in users:
            self.accounts[usr['jid']] = usr
            self.send_presence(pto=usr['jid'], pfrom=self.boundjid.bare, ptype='probe')

    def message(self, iq):
        """
         Message from XMPP
        :param iq:
        :return:
        """
        jid = iq['from'].bare

        if iq['to'] == self.config['jid'] and iq['type'] == 'chat':  # message to gateway
            if iq['body'].startswith('!'):
                self.process_command(iq)
            else:
                self.gate_reply_message(iq, 'Only commands accepted. Try !help for more info.')
        else:  # --- outgoing message ---
            if jid in self.tg_connections and self.tg_connections[jid].is_user_authorized():
                if iq['body'].startswith('!'):  # it is command!
                    if iq['to'].bare.startswith( ('u', 'b') ):
                        self.process_chat_user_command(iq)
                    elif iq['to'].bare.startswith('g') or iq['to'].bare.startswith('s') or iq['to'].bare.startswith('c'):
                        self.process_chat_group_command(iq)
                    else:
                        self.gate_reply_message(iq, 'Error.')
                else:  # -- normal message --
                    tg_id = int(iq['to'].node[1:])
                    tg_peer = None
                    msg = iq['body']
                    reply_mid = None

                    if msg.startswith('>'):  # quoting check
                        msg_lines = msg.split('\n')
                        matched = re.match(r'>[ ]*(?P<mid>[\d]+)[ ]*', msg_lines[0])
                        matched = matched.groupdict() if matched else {}

                        if 'mid' in matched:  # citation
                            reply_mid = int(matched['mid'])
                            msg = '\n'.join(msg_lines[1:])

                    if iq['to'].bare.startswith( ('u', 'b') ):  # normal user
                        tg_peer = InputPeerUser(tg_id, self.tg_dialogs[jid]['users'][tg_id].access_hash)
                    elif iq['to'].bare.startswith('g'):  # generic group
                        tg_peer = InputPeerChat(tg_id)
                    elif iq['to'].bare.startswith( ('s', 'c') ):  # supergroup
                        tg_peer = InputPeerChannel(tg_id, self.tg_dialogs[jid]['supergroups'][tg_id].access_hash)
                        
                    # peer OK. 
                    if tg_peer:
                        result = None 
                        
                        # detect media
                        if msg.startswith('http') and re.match(r'(?:http\:|https\:)?\/\/.*\.(?:' + self.config['media_external_formats'] + ')', msg):
                            urls = re.findall(r'(?:http\:|https\:)?\/\/.*\.(?:' + self.config['media_external_formats'] + ')', msg)
                            message = msg.replace(urls[0], '')
                            media = InputMediaPhotoExternal(urls[0])
                            try:
                                result = self.tg_connections[jid].invoke(SendMediaRequest(tg_peer, media, message, random_id = generate_random_long(), reply_to_msg_id = reply_mid))
                            except Exception:
                                print('Media upload failed.')
                                
                        # media send failed. #
                        if not result:
                            result = self.tg_connections[jid].invoke(SendMessageRequest(tg_peer, msg, generate_random_long(), reply_to_msg_id=reply_mid))

                        # find sent message id and save it
                        if result and hasattr(result, 'id'):  # update id 
                            msg_id = result.id
                            self.tg_dialogs[jid]['messages'][tg_id] = {'id': msg_id, 'body': msg}
                            #self.send_message(mto=iq['from'], mfrom=iq['to'], mtype='chat', mbody='[Your MID:{}]'.format(msg_id))


    def event_presence_unsub(self, presence):
       return

    def event_presence(self, presence):
        """
        Presence handler
        :param presence:
        :return:
        """
        ptype = presence['type']
        

        # handle "online" to transport:
        if ptype == 'available' and presence['to'].bare == self.boundjid.bare:
            self.handle_online(presence, False) # handle online 
        elif ptype == 'subscribe':
            self.send_presence(pto=presence['from'].bare, pfrom=presence['to'].bare, ptype='subscribed')
        elif ptype == 'subscribed':
            pass
        elif ptype == 'unsubscribe':
            pass            
        elif ptype == 'unsubscribed':
            pass
        elif ptype == 'probe':
            self.send_presence(pto=presence['from'], pfrom=presence['to'], ptype='available')
        elif ptype == 'unavailable':
            pass
        else:
            # self.send_presence(pto=presence['from'], pfrom=presence['to'])
            pass

    def handle_online(self, event, sync_roster = True):
        """
        Gateway's subscriber comes online
        :param event:
        :return:
        """
        jid = event['from'].bare
        to = event['to'].bare
                        
        # maybe if i'll ignore it — it will go ahead
        if to != self.boundjid.bare:
            return

        if jid not in self.tg_connections:
            result = self.db_connection.execute("SELECT * FROM accounts WHERE jid = ?", (jid,)).fetchone()

            if result is not None:
                self.spawn_tg_client(jid, result['tg_phone'])
        else:
            if not (self.tg_connections[jid].is_connected()):
                self.tg_connections[jid].connect()
                self.tg_connections[jid].invoke(UpdateStatusRequest(offline=False))

            self.send_presence(pto=jid, pfrom=self.boundjid.bare, ptype='online', pstatus='connected')
            self.tg_process_dialogs(jid, sync_roster) # do not sync roster if we already have connection!


    def handle_offline(self, event):
        """
        Gateway's subscriber comes offline.
        :param event:
        :return:
        """
        jid = event['from'].bare
        
        # keep telegram online ?
        if self.accounts[jid]['keep_online']:
            return

        if jid in self.tg_connections:
            self.tg_connections[jid].invoke(UpdateStatusRequest(offline=True))
            self.tg_connections[jid].disconnect()
            
    def handle_interrupt(self, signal, frame):
        """
        Interrupted (Ctrl+C).
        :param event:
        :return:
        """
        
        for jid in self.tg_connections:
            print('Disconnecting: %s' % jid)
            self.tg_connections[jid].invoke(UpdateStatusRequest(offline=True))
            self.tg_connections[jid].disconnect()
            for contact_jid, contact_nickname in self.contact_list[jid].items():
                self.send_presence(pto=jid, pfrom=contact_jid, ptype='unavailable')                
            self.send_presence(pto=jid, pfrom=self.boundjid.bare, ptype='unavailable')
        sys.exit(0)

    def process_command(self, iq):
        """
        Commands to gateway, users or chats (starts with !)
        :param iq:
        :return:
        """
        parsed = iq['body'].split(' ')
        jid = iq['from'].bare

        if parsed[0] == '!help':
            self.gate_reply_message(iq, '=== Available gateway commands ===:\n\n'
                                        '!help - Displays this text\n'
                                        '!login +123456789 - Initiates Telegram session\n'
                                        '!code 12345 - Entering one-time code during auth\n'
                                        '!password abc123 - Entering password during two-factor auth\n'
                                        '!configure - Configure transport settings\n'
                                        #'!list_sessions - List all created sessions at Telegram servers\n'
                                        #'!delete_session 123 - Delete session\n'
                                        '!logout - Deletes current Telegram session at gate\n'
                                        '!reload_dialogs - Reloads dialogs list from Telegram\n\n'
                                        '!add - Find and add Telegram contact. Any formats accepted (nickname or t.me link)\n\n'
                                        '!join - Join Telegram conference via invite link \n\n'
                                        '!import phone firstname lastname - Add Telegram contact with phone number \n\n'
                                        '!group GroupName @InviteContact - Create a normal group\n'
                                        '!supergroup SupergroupName - Create a supergroup\n'
                                        '!channel ChannelName - Create a channel\n\n'
                                        '!name first last - Change your name in Telegram\n'
                                        '!about text - Change about text in Telegram\n'
                                        '!username - Changes your @username in Telegram\n'
                                    )
        elif parsed[0] == '!configure': 
            config_exclude = ['jid', 'tg_phone']
            if len(parsed) > 2 and parsed[1] not in config_exclude:
                self.db_connection.execute("update accounts set {} = ? where jid = ?".format(parsed[1]), (parsed[2],jid,) )
                self.accounts[jid] = self.db_connection.execute("SELECT * FROM accounts where jid = ?", (jid,) ).fetchone()

            message = "=== Your current configuration ===\n\n"
            for param, value in self.accounts[jid].items():
                message = message + "<%s>: %s" %  (param, value) + "\n"
            message = message + "\nTo modify some option, please, send !configure param value"
            self.gate_reply_message(iq, message)

            
        elif parsed[0] == '!login':  # --------------------------------------------------
            self.gate_reply_message(iq, 'Please wait...')
            self.spawn_tg_client(jid, parsed[1])
            
            if self.tg_connections[jid].is_user_authorized():
                self.send_presence(pto=jid, pfrom=self.boundjid.bare, ptype='online', pstatus='connected')
                self.gate_reply_message(iq, 'You are already authenticated in Telegram.')
            else:
                # remove old sessions for this JID #
                self.db_connection.execute("DELETE from accounts where jid = ?", (jid, ) )
                self.tg_connections[jid].send_code_request(parsed[1])
                self.gate_reply_message(iq, 'Gate is connected. Telegram should send SMS message to you.')
                self.gate_reply_message(iq, 'Please enter one-time code via !code 12345.')
        elif parsed[0] in ['!code', '!password']:  # --------------------------------------------------
            if not self.tg_connections[jid].is_user_authorized():
                if parsed[0] == '!code':
                    try:
                        self.gate_reply_message(iq, 'Trying authenticate...')
                        self.tg_connections[jid].sign_in(self.tg_phones[jid], parsed[1])
                    except SessionPasswordNeededError:
                        self.gate_reply_message(iq, 'Two-factor authentication detected.')
                        self.gate_reply_message(iq, 'Please enter your password via !password abc123.')
                        return

                if parsed[0] == '!password':
                    self.gate_reply_message(iq, 'Checking password...')
                    self.tg_connections[jid].sign_in(password=parsed[1])

                if self.tg_connections[jid].is_user_authorized():
                    self.send_presence(pto=jid, pfrom=self.boundjid.bare, ptype='online', pstatus='connected')
                    self.gate_reply_message(iq, 'Authentication successful. Initiating Telegram...')
                    self.db_connection.execute("INSERT INTO accounts(jid, tg_phone) VALUES(?, ?)", (jid, self.tg_phones[jid],))
                    self.accounts[jid] = self.db_connection.execute("SELECT * FROM accounts where jid = ?", (jid,) ).fetchone()
                    self.init_tg(jid)

                else:
                    self.gate_reply_message(iq, 'Authentication failed.')
            else:
                self.gate_reply_message(iq, 'You are already authenticated. Please use !logout before new login.')
        elif parsed[0] == '!list_sessions':  # --------------------------------------------------
            if not self.tg_connections[jid].is_user_authorized():
                self.gate_reply_message(iq, 'Error.')
                return

            sessions = self.tg_connections[jid].invoke(GetAuthorizationsRequest())
        elif parsed[0] == '!reload_dialogs':
            if not self.tg_connections[jid].is_user_authorized():
                self.gate_reply_message(iq, 'Error.')
                return
            self.tg_process_dialogs(jid)
            self.gate_reply_message(iq, 'Dialogs reloaded.')
        elif parsed[0] == '!logout':  # --------------------------------------------------
            self.tg_connections[jid].log_out()
            self.db_connection.execute("DELETE FROM accounts WHERE jid = ?", (jid,))
            self.gate_reply_message(iq, 'Your Telegram session was deleted')
        elif parsed[0] == '!add': # add user
            result = self.tg_connections[jid].get_entity(parsed[1])
            if type(result) == User: 
                tg_peer = InputPeerUser( result.id, result.access_hash )
                result = self.tg_connections[jid].invoke( SendMessageRequest(tg_peer, 'Hello! I just want to add you in my contact list.', generate_random_long() ) )
            elif type(result) == Channel: 
                tg_peer = InputPeerChannel( result.id, result.access_hash )
                self.tg_connections[jid].invoke(JoinChannelRequest( InputPeerChannel(result.id, result.access_hash) ) )
            else:
                self.gate_reply_message(iq, 'Sorry, nothing found.')
                return
                
            self.tg_process_dialogs(jid)
            
        elif parsed[0] == '!join': # join chat by link
            link = parsed[1].split('/') # https://t.me/joinchat/HrCmckx_SkMbSGFLhXCvSg
            self.tg_connections[jid].invoke(ImportChatInviteRequest(link[4]))
            time.sleep(1)
            self.tg_process_dialogs(jid)
                
        elif parsed[0] == '!group' and len(parsed) >= 3: # create new group 
            # group name? #
            groupname = parsed[1]
            
            # group users? #
            groupuser = self.tg_connections[jid].get_entity(parsed[2]) 

            # we re ready to make group 
            self.tg_connections[jid].invoke(CreateChatRequest([groupuser], groupname))
            self.tg_process_dialogs(jid)        

        elif parsed[0] == '!channel' and len(parsed) >= 2: # create new channel 
            groupname = parsed[1]
            self.tg_connections[jid].invoke(CreateChannelRequest(groupname, groupname, broadcast = True))
            self.tg_process_dialogs(jid)        

        elif parsed[0] == '!supergroup' and len(parsed) >= 2: # create new channel 
            groupname = parsed[1]
            self.tg_connections[jid].invoke(CreateChannelRequest(groupname, groupname, megagroup = True))
            self.tg_process_dialogs(jid)        

        elif parsed[0] == '!username' and len(parsed) >= 2: # create new channel 
            username = parsed[1]
            self.tg_connections[jid].invoke(UpdateUsernameRequest(username))

        elif parsed[0] == '!name' and len(parsed) >= 2: # create new channel 
            firstname = parsed[1]
            lastname = parsed[2] if len(parsed) > 2 else None
            self.tg_connections[jid].invoke(UpdateProfileRequest(first_name = firstname, last_name = lastname))            

        elif parsed[0] == '!about' and len(parsed) >= 2: # create new channel 
            about = iq['body'][7:]
            self.tg_connections[jid].invoke(UpdateProfileRequest(about = about))            

        elif parsed[0] == '!import' and len(parsed) >= 3: # create new channel 
            phone = parsed[1]
            firstname = parsed[2]
            lastname = parsed[3] if len(parsed) > 3 else None

            contact = InputPhoneContact(client_id=generate_random_long(), phone=phone, first_name=firstname, last_name=lastname) 
            self.tg_connections[jid].invoke(ImportContactsRequest([contact]))            
            self.tg_process_dialogs(jid)        
            
        else:  # --------------------------------------------------
            self.gate_reply_message(iq, 'Unknown command. Try !help for list all commands.')

    def process_chat_user_command(self, iq):
        parsed = iq['body'].split(' ')
        jid = iq['from'].bare

        if parsed[0] == '!help':
            self.gate_reply_message(iq, '=== Available dialog commands ===:\n\n'
                                        '!help - Displays this text\n'
                                        '!s/find/replace - Edit last message. Use empty `find` to edit whole message and empty `replace` to delete it.\n'
                                        '!block - Blacklists current user\n' 
                                        '!unblock - Unblacklists current user\n' 
                                        '!remove - Removes history and contact from your contact list\n' 
                                   )
        elif parsed[0] == '!block':
            tg_id = int(iq['to'].node[1:])
            nickname = display_tg_name(self.tg_dialogs[jid]['users'][tg_id])
            self.tg_connections[jid].invoke(BlockRequest( InputPeerUser(tg_id, self.tg_dialogs[jid]['users'][tg_id].access_hash) ) )
            self.gate_reply_message(iq, 'User %s blacklisted!' % nickname)

        elif parsed[0] == '!unblock':
            tg_id = int(iq['to'].node[1:])
            nickname = display_tg_name(self.tg_dialogs[jid]['users'][tg_id])
            self.tg_connections[jid].invoke(UnblockRequest( InputPeerUser(tg_id, self.tg_dialogs[jid]['users'][tg_id].access_hash) ) )
            self.gate_reply_message(iq, 'User %s unblacklisted!' % nickname)
            
        elif parsed[0] == '!remove':
            tg_id = int(iq['to'].node[1:])
            peer = InputPeerUser(tg_id, self.tg_dialogs[jid]['users'][tg_id].access_hash)
            c_jid = get_contact_jid(self.tg_dialogs[jid]['users'][tg_id], self.boundjid.bare)
            self.tg_connections[jid].invoke( DeleteContactRequest(peer) )
            self.tg_connections[jid].invoke( DeleteHistoryRequest( peer, max_id = 0, just_clear = None ) )
            self.send_presence(pto = jid, pfrom = c_jid, ptype = 'unavailable')
            self.send_presence(pto = jid, pfrom = c_jid, ptype = 'unsubscribed')
            self.send_presence(pto = jid, pfrom = c_jid, ptype = 'unsubscribe')
            
        elif iq['body'].startswith('!s/'):
            tg_id = int(iq['to'].node[1:])
            peer = InputPeerUser(tg_id, self.tg_dialogs[jid]['users'][tg_id].access_hash)
            
            msg_id, edited = self.edit_message(jid, tg_id, iq['body'])
            if not edited: return

            # and send it 
            if edited != '' and edited != ' ':
                self.tg_dialogs[jid]['messages'][tg_id]["body"] = edited
                self.tg_connections[jid].invoke( EditMessageRequest(peer, msg_id, message = edited) )
            else:
                del(self.tg_dialogs[jid]['messages'][tg_id])
                self.tg_connections[jid].invoke( DeleteMessagesRequest([msg_id], revoke = True) )
                    

    def process_chat_group_command(self, iq):
        parsed = iq['body'].split(' ')
        jid = iq['from'].bare

        if parsed[0] == '!help':
            self.gate_reply_message(iq, '=== Available chat commands ===:\n\n'
                                        '!help - Displays this text\n'
                                        '!s/find/replace - Edit last message. Use empty `find` to edit whole message and empty `replace` to delete it.\n'
                                        '!leave - Leaves current group or supergroup\n' 
                                        '!invite - Invites user to group\n' 
                                        '!kick - Kicks user to group\n' 
                                    )
        elif parsed[0] == '!leave':
            tg_id = int(iq['to'].node[1:])
            if tg_id in self.tg_dialogs[jid]['supergroups']:
                peer = InputPeerChannel(tg_id, self.tg_dialogs[jid]['supergroups'][tg_id].access_hash)
                self.tg_connections[jid].invoke( LeaveChannelRequest(peer) )
                self.tg_connections[jid].invoke( DeleteHistoryRequest( peer, max_id = 0, just_clear = None ) )
                c_jid = get_contact_jid(self.tg_dialogs[jid]['supergroups'][tg_id], self.boundjid.bare)
                self.send_presence(pto = jid, pfrom = c_jid, ptype = 'unavailable')
                self.send_presence(pto = jid, pfrom = c_jid, ptype = 'unsubscribed')
                self.send_presence(pto = jid, pfrom = c_jid, ptype = 'unsubscribe')
            if tg_id in self.tg_dialogs[jid]['groups']:
                self.tg_connections[jid].invoke( DeleteChatUserRequest(tg_id, self.tg_connections[jid].me) )
                self.tg_connections[jid].invoke( DeleteHistoryRequest( InputPeerChat(tg_id), max_id = 0, just_clear = None ) )
                c_jid = get_contact_jid(self.tg_dialogs[jid]['groups'][tg_id], self.boundjid.bare)
                self.send_presence(pto = jid, pfrom = c_jid, ptype = 'unavailable')
                self.send_presence(pto = jid, pfrom = c_jid, ptype = 'unsubscribed')
                self.send_presence(pto = jid, pfrom = c_jid, ptype = 'unsubscribe')
                
        elif parsed[0] == '!invite':
            tg_id = int(iq['to'].node[1:])
            if tg_id in self.tg_dialogs[jid]['supergroups']:
                invited_user = self.tg_connections[jid].get_entity(parsed[1])
                if type(invited_user) == User:
                    self.tg_connections[jid].invoke(EditBannedRequest( InputPeerChannel(tg_id, self.tg_dialogs[jid]['supergroups'][tg_id].access_hash), invited_user, ChannelBannedRights(until_date=None,view_messages=False) ) )
                    self.tg_connections[jid].invoke(InviteToChannelRequest( InputPeerChannel(tg_id, self.tg_dialogs[jid]['supergroups'][tg_id].access_hash), [invited_user] ) )
            if tg_id in self.tg_dialogs[jid]['groups']:
                invited_user = self.tg_connections[jid].get_entity(parsed[1])
                if type(invited_user) == User:
                    self.tg_connections[jid].invoke( AddChatUserRequest(tg_id, invited_user, 0) )
             
        elif parsed[0] == '!kick':
            tg_id = int(iq['to'].node[1:])
            if tg_id in self.tg_dialogs[jid]['supergroups']:
                kicked_user = self.tg_connections[jid].get_entity(parsed[1])
                if type(kicked_user) == User:
                    self.tg_connections[jid].invoke(EditBannedRequest( InputPeerChannel(tg_id, self.tg_dialogs[jid]['supergroups'][tg_id].access_hash), kicked_user, ChannelBannedRights(until_date=None,view_messages=True) ) )
            if tg_id in self.tg_dialogs[jid]['groups']:
                kicked_user = self.tg_connections[jid].get_entity(parsed[1])
                if type(kicked_user) == User:
                    self.tg_connections[jid].invoke( DeleteChatUserRequest(tg_id, kicked_user) )
            
        elif iq['body'].startswith('!s/'):
            tg_id = int(iq['to'].node[1:])
            peer = InputPeerChannel(tg_id, self.tg_dialogs[jid]['supergroups'][tg_id].access_hash) if tg_id in self.tg_dialogs[jid]['supergroups'] else InputPeerChat(tg_id)
            
            msg_id, edited = self.edit_message(jid, tg_id, iq['body'])
            if not edited: return

            # and send it 
            if edited != '' and edited != ' ':
                self.tg_dialogs[jid]['messages'][tg_id]["body"] = edited
                self.tg_connections[jid].invoke( EditMessageRequest(peer, msg_id, message = edited) )
            else:
                del(self.tg_dialogs[jid]['messages'][tg_id])
                if isinstance(peer, InputPeerChannel):
                    self.tg_connections[jid].invoke( DeleteMessagesChannel(peer, [msg_id]) )
                else:
                    self.tg_connections[jid].invoke( DeleteMessagesRequest([msg_id], revoke = True) )
                    
                    
           

    def spawn_tg_client(self, jid, phone):
        """
        Spawns Telegram client
        :param jid:
        :param phone:
        :return:
        """
        client = TelegramGateClient('a_'+phone, int(self.config['tg_api_id']), self.config['tg_api_hash'], self, jid, phone)
        if 'tg_server_ip' in self.config and 'tg_server_dc' in self.config and 'tg_server_port' in self.config:
            client.session.set_dc(self.config['tg_server_dc'], self.config['tg_server_ip'], self.config['tg_server_port'])
        client.connect()

        self.tg_connections[jid] = client
        self.tg_phones[jid] = phone

        if client.is_user_authorized():
            self.init_tg(jid)
            self.send_presence(pto=jid, pfrom=self.boundjid.bare, ptype='online', pstatus='connected')

    def init_tg(self, jid):
        """
        Initialize 
        :param jid: 
        :return: 
        """
        # Set status = Online
        self.tg_connections[jid].invoke(UpdateStatusRequest(offline=False))

        # Process Telegram contact list
        self.tg_process_dialogs(jid, sync_roster = False)

        # Register Telegrap updates handler  
        self.tg_connections[jid].add_update_handler(self.tg_connections[jid].xmpp_update_handler)
        
    def roster_exchange(self, tojid, contacts):
        
        message = Message()
        message['from'] = self.boundjid.bare
        message['to'] = tojid
        rawxml = "<x xmlns='http://jabber.org/protocol/rosterx'>"
        for jid, nick in contacts.items():
               c = "<item action='add' jid='%s' name='%s'><group>Telegram</group></item>" % (jid, nick)
               rawxml = rawxml + c
               
        rawxml = rawxml + "</x>"
        message.appendxml(ET.fromstring(rawxml))
        
        self.send(message)
        
    def roster_fill(self, tojid, contacts):
       
       for jid, nick in contacts.items():
               presence = Presence()
               presence['from'] = jid
               presence['to'] = tojid
               presence['type'] = 'subscribe'
               presence.appendxml(ET.fromstring("<nick xmlns='http://jabber.org/protocol/nick'>%s</nick>" % nick))
               self.send(presence)
          

    def tg_process_dialogs(self, jid, sync_roster = True):
       
        print('Processing dialogs...')
       
        # dialogs dictonaries
        self.tg_dialogs[jid] = dict()
        self.tg_dialogs[jid]['raw'] = list()
        self.tg_dialogs[jid]['users'] = dict()
        self.tg_dialogs[jid]['groups'] = dict()
        self.tg_dialogs[jid]['supergroups'] = dict()
        self.tg_dialogs[jid]['messages'] = dict()

        # offsets
        last_peer = InputPeerEmpty()
        last_msg_id = 0
        last_date = None
        
        # roster exchange #
        self.contact_list[jid] = dict()
        
        while True:  
            dlgs = self.tg_connections[jid].invoke(GetDialogsRequest(offset_date=last_date, offset_id=last_msg_id,
                                                                     offset_peer=last_peer, limit=100))

            self.tg_dialogs[jid]['raw'].append(dlgs)

            for usr in dlgs.users:
                self.tg_dialogs[jid]['users'][usr.id] = usr
            for cht in dlgs.chats:
                if type(cht) in [Chat, ChatForbidden]:  # normal group
                    self.tg_dialogs[jid]['groups'][cht.id] = cht
                elif type(cht) in [Channel, ChannelForbidden]:  # supergroup
                    self.tg_dialogs[jid]['supergroups'][cht.id] = cht

            for dlg in dlgs.dialogs:
                if type(dlg.peer) is PeerUser:
                    usr = self.tg_dialogs[jid]['users'][dlg.peer.user_id]
                    vcard = self.plugin['xep_0054'].make_vcard()
                    u_jid = get_contact_jid(usr, self.boundjid.bare)

                    # make vcard #
                    vcard['JABBERID'] = u_jid

                    if usr.deleted:
                        rostername = "Deleted Account"
                        vcard['FN'] = 'Deleted account'
                        vcard['DESC'] = 'This user no longer exists in Telegram'
                    else:
                        rostername = display_tg_name(usr)
                        rostername = '[B] ' + rostername if usr.bot else rostername

                        vcard['FN'] = display_tg_name(usr)
                        vcard['DESC'] = ''
                        if usr.first_name:
                            vcard['N']['GIVEN'] = usr.first_name
                        if usr.last_name:
                            vcard['N']['FAMILY'] = usr.last_name
                        if usr.username:
                            vcard['DESC'] = 'Telegram Username: @' + usr.username
                        if usr.phone:
                            vcard['DESC'] += "\n" + 'Phone number: ' + usr.phone

                        vcard['NICKNAME'] = vcard['FN'] 
                    
                    # add photo to VCard #
                    photo, photosha1hash = self.get_peer_photo(jid, usr) if sync_roster else (None, None)
                    if photo:
                        vcard['PHOTO']['TYPE'] = 'image/jpeg'
                        vcard['PHOTO']['BINVAL'] = photo

                    self.plugin['xep_0054'].publish_vcard(jid=u_jid, vcard=vcard)
                    self.plugin['xep_0172'].publish_nick(nick=vcard['FN'], ifrom=u_jid)
                    self.publish_photo(jid, u_jid, photosha1hash) if photosha1hash else None

                    # add it to contect list & avatar download queue #
                    self.contact_list[jid][u_jid] = rostername

                    if usr.bot:
                        self.send_presence(pto=jid, pfrom=u_jid, pshow = 'chat', pstatus='Bot')
                    else:
                        if type(usr.status) is UserStatusOnline:
                            self.send_presence(pto=jid, pfrom=u_jid, pstatus = 'Online' )
                        elif type(usr.status) is UserStatusRecently:
                            self.send_presence(pto=jid, pfrom=u_jid, pshow='dnd', pstatus='Last seen recently')
                        elif type(usr.status) is UserStatusOffline:
                            phow = 'away' if datetime.datetime.utcnow() - usr.status.was_online < datetime.timedelta(hours = self.accounts[jid]['status_xa_interval'] ) else 'xa' 
                            self.send_presence(pto=jid, pfrom=u_jid, pshow=phow, pstatus=localtime(usr.status.was_online).strftime('Last seen at %H:%M %d/%m/%Y') )
                        else:
                            self.send_presence(pto=jid, pfrom=u_jid, ptype='unavailable', pstatus='Last seen a long time ago')

                if type(dlg.peer) in [PeerChat, PeerChannel]:
                    cht = None

                    if type(dlg.peer) is PeerChat:  # old group
                        cht = self.tg_connections[jid].invoke(GetFullChatRequest(dlg.peer.chat_id))
                        cht = cht.chats[0]
                        if cht.deactivated or cht.left:
                            cht = None
                    elif type(dlg.peer) is PeerChannel:  # supergroup
                        cht = self.tg_dialogs[jid]['supergroups'][dlg.peer.channel_id]


                    if cht and cht.id:
                        rostername = display_tg_name(cht)
                        u_jid = get_contact_jid(cht, self.boundjid.bare)

                        vcard = self.plugin['xep_0054'].make_vcard()
                        vcard['FN'] = rostername
                        vcard['NICKNAME'] = rostername
                        vcard['JABBERID'] = u_jid
                        
                        # add photo to VCard #
                        photo, photosha1hash = self.get_peer_photo(jid, cht) if sync_roster else (None, None)
                        if photo:
                            vcard['PHOTO']['TYPE'] = 'image/jpeg'
                            vcard['PHOTO']['BINVAL'] = photo
                        self.plugin['xep_0054'].publish_vcard(jid=u_jid, vcard=vcard)
                        self.plugin['xep_0172'].publish_nick(nick=vcard['FN'], ifrom=u_jid)
                        self.publish_photo(jid, u_jid, photosha1hash) if photosha1hash else None

                        self.contact_list[jid][u_jid] = rostername
                        self.send_presence(pto=jid, pfrom=u_jid, pshow = 'chat', pstatus = cht.title)
                    

            if len(dlgs.dialogs) == 0:  # all dialogs was received.
                if sync_roster and 'use_roster_exchange' in self.accounts[jid] and self.accounts[jid]['use_roster_exchange'] == 'true':
                    self.roster_exchange(jid, self.contact_list[jid])
                elif sync_roster:
                    self.roster_fill(jid, self.contact_list[jid])
                break
            else:  # get next part of dialogs.
                last_msg_id = dlgs.dialogs[-1].top_message  # we fucking need last msg id! 
                last_peer = dlgs.dialogs[-1].peer

                last_date = next(msg for msg in dlgs.messages  # find date
                                 if type(msg.to_id) is type(last_peer) and msg.id == last_msg_id).date

                if type(last_peer) is PeerUser:  # user/bot
                    access_hash = self.tg_dialogs[jid]['users'][last_peer.user_id].access_hash
                    last_peer = InputPeerUser(last_peer.user_id, access_hash)
                elif type(last_peer) in [Chat, ChatForbidden]:  # normal group
                    last_peer = InputPeerChat(last_peer.chat_id)
                elif type(last_peer) in [Channel, ChannelForbidden]:  # supergroup/channel
                    access_hash = self.tg_dialogs[jid]['supergroups'][last_peer.channel_id].access_hash
                    last_peer = InputPeerChannel(last_peer.channel_id, access_hash)
        
    def tg_process_unread_messages(self):
        pass

    def gate_reply_message(self, iq, msg):
        """
        Reply to message to gate.
        :param iq:
        :param msg:
        :return:
        """
        self.send_message(mto=iq['from'], mfrom=self.config['jid'], mtype='chat', mbody=msg)

    def get_peer_photo(self, jid, peer):
       
        # we are able to disable this shit #
        if not 'enable_avatars' in self.accounts[jid] or self.accounts[jid]['enable_avatars'] != 'true':
            return (None, None)
       
        data = io.BytesIO()
        self.tg_connections[jid].download_profile_photo(peer, file = data)
        data.flush()
        if isinstance(data, io.BytesIO) and data.getbuffer().nbytes > 0:
            image = data.getvalue()
            image_sha1 = hashlib.sha1(image).hexdigest()
            return (image, image_sha1)
        else:
            return (None, None)
            
    def edit_message(self, jid, tg_id, message):

        # get last message to this peer 
        if not tg_id in self.tg_dialogs[jid]['messages']:
            return (None, None)

        msg_id = self.tg_dialogs[jid]['messages'][tg_id]["id"]
        msg_body = self.tg_dialogs[jid]['messages'][tg_id]["body"]

        # edit this message 
        pattern = message.split('/')
        replace = ' ' if pattern[2] == '' else '/'.join(pattern[2:]) # no empty regexp — replace with whitespace
        edited = re.sub(r'%s' % pattern[1], replace, msg_body, re.I) if pattern[1] != '' else replace # if no pattern specified — edit whole message
        
        return (msg_id, edited)

    def publish_photo(self, jid, fromjid, photo):
       presence = Presence()
       presence['to'] = jid
       presence['from'] = fromjid
       presence.appendxml(ET.fromstring("<x xmlns='vcard-temp:x:update'><photo>%s</photo></x>" % photo))
       self.send(presence)
    

    def init_database(self):
        """
        Database initialization
        :return:
        """
        def dict_factory(cursor, row):
            d = {}
            for idx, col in enumerate(cursor.description):
                d[col[0]] = row[idx]
            return d

        conn = sqlite3.connect(self.config['db_connect'], isolation_level=None, check_same_thread=False)
        conn.row_factory = dict_factory

        conn.execute("CREATE TABLE IF NOT EXISTS accounts(jid VARCHAR(255), tg_phone VARCHAR(25), use_roster_exchange BOOLEAN default false, keep_online BOOLEAN default false, status_update_interval INTEGER default 30, status_xa_interval INTEGER default 24, enable_avatars BOOLEAN default false)")

        return conn
