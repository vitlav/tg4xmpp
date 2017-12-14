from sleekxmpp.plugins.xep_0054 import XEP_0054
from sleekxmpp import Iq
from sleekxmpp.exceptions import XMPPError


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

# Грязно патчим баг в библиотеке
XEP_0054._handle_get_vcard = patched_handle_get_vcard
