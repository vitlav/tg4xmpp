from sleekxmpp.plugins.xep_0054 import XEP_0054
from sleekxmpp.plugins.xep_0030 import XEP_0030
from sleekxmpp import Iq
from sleekxmpp.xmlstream import JID
from sleekxmpp.exceptions import XMPPError

from telethon.update_state import UpdateState

def patched_handle_get_vcard(self, iq):
    if iq['type'] == 'result':
        self.api['set_vcard'](jid=iq['from'], args=iq['vcard_temp'])
        return
    elif iq['type'] == 'get':
        vcard = self.api['get_vcard'](iq['to'].bare)
        if isinstance(vcard, Iq):
            vcard.send()
        else:
            iq = iq.reply()
            iq.append(vcard)
            iq.send()
    elif iq['type'] == 'set':
        raise XMPPError('service-unavailable')

def patched_stop_workers(self):
    """
    Waits for all the worker threads to stop.
    """
    # Put dummy ``None`` objects so that they don't need to timeout.
    n = self._workers
    self._workers = None
    if n:
        with self._updates_lock:
            for _ in range(n):
                self._updates.put(None)

    for t in self._worker_threads:
        t.join()

    self._worker_threads.clear()
    self._workers = n 

def patched_get_info(self, jid=None, node=None, local=None, cached=None, **kwargs):

     if local is None:
         if jid is not None and not isinstance(jid, JID):
             jid = JID(jid)
             if self.xmpp.is_component:
                 if jid.domain == self.xmpp.boundjid.domain:
                     local = True
             else:
                 if str(jid) == str(self.xmpp.boundjid):
                     local = True
             jid = jid.full
         elif jid in (None, ''):
             local = True

     if local:
         log.debug("Looking up local disco#info data " + \
                   "for %s, node %s.", jid, node)
         info = self.api['get_info'](jid, node,
                 kwargs.get('ifrom', None),
                 kwargs)
         info = self._fix_default_info(info)
         return self._wrap(kwargs.get('ifrom', None), jid, info)

     if cached:
         info = self.api['get_cached_info'](jid, node,
                 kwargs.get('ifrom', None),
                 kwargs)
         if info is not None:
             return self._wrap(kwargs.get('ifrom', None), jid, info)

     iq = self.xmpp.Iq()
     # Check dfrom parameter for backwards compatibility
     iq['from'] = kwargs.get('ifrom', kwargs.get('dfrom', ''))
     iq['from'] = self.xmpp.boundjid.bare if (not iq['from'] or iq['from'] == '') else iq['from']
     iq['to'] = jid
     iq['type'] = 'get'
     iq['disco_info']['node'] = node if node else ''
     return iq.send(timeout=kwargs.get('timeout', None),
                    block=kwargs.get('block', True),
                    callback=kwargs.get('callback', None),
                    timeout_callback=kwargs.get('timeout_callback', None))


# hey i'm baboon
XEP_0054._handle_get_vcard = patched_handle_get_vcard
XEP_0030.get_info = patched_get_info
UpdateState.stop_workers = patched_stop_workers
