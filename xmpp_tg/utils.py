import types
import time
import pytz
from datetime import datetime



def display_tg_name(peer):
    if hasattr(peer,'title') and hasattr(peer,'broadcast') and peer.broadcast: # channel
        return '[C] ' + peer.title
    elif hasattr(peer,'title') and hasattr(peer,'broadcast') and not peer.broadcast: # supergroup
        return '[SG] ' + peer.title
    elif hasattr(peer,'title'): # normal group
        return '[G] ' + peer.title
    elif peer.first_name and peer.last_name: # user with first and last name
        return '{} {}'.format(peer.first_name, peer.last_name)
    elif peer.first_name: # user with firstname only
        return peer.first_name
    elif peer.last_name: # user with lastname only 
        return peer.last_name
    elif peer.username: # user with username only
        return peer.username
    else: # no match, unknown contact
        return '[Unknown]'

def get_contact_jid(peer, gatejid):
    if peer.id and hasattr(peer,'title') and hasattr(peer, 'broadcast') and peer.broadcast: # channel
        return 'c' + str(peer.id) + '@' + gatejid
    elif peer.id and hasattr(peer,'title') and hasattr(peer,'broadcast') and not peer.broadcast: # supergroup
        return 's' + str(peer.id) + '@' + gatejid
    elif peer.id and hasattr(peer,'title'): # normal group
        return 'g' + str(peer.id) + '@' + gatejid
    elif peer.id and not peer.bot: # it is... user?
        return 'u' + str(peer.id) + '@' + gatejid
    elif peer.id and peer.bot:
        return 'b' + str(peer.id) + '@' + gatejid
    else: # what a fuck is this? 
        return None

def localtime(utc_dt):
    if time.daylight:
        offsetHour = time.altzone / 3600
    else:
        offsetHour = time.timezone / 3600
    local_tz = pytz.timezone('Etc/GMT%+d' % offsetHour)
    local_dt = utc_dt.replace(tzinfo = pytz.utc).astimezone(local_tz)
    return local_tz.normalize(local_dt)

def var_dump(obj, depth=7, l=""):
    # fall back to repr
    if depth < 0 or type(obj) is datetime:
        return repr(obj)

    # expand/recurse dict
    if isinstance(obj, dict):
        name = ""
        objdict = obj
    else:
        # if basic type, or list thereof, just print
        canprint = lambda o: isinstance(o, (int, float, str, bool, type(None), types.LambdaType))

        try:
            if canprint(obj) or sum(not canprint(o) for o in obj) == 0:
                return repr(obj)
        except TypeError:
            pass

        # try to iterate as if obj were a list
        try:
            return "[\n" + "\n".join(l + var_dump(k, depth=depth - 1, l=l + "    ") + "," for k in obj) + "\n" + l + "]"
        except TypeError as e:
            # else, expand/recurse object attribs
            name = (hasattr(obj, '__class__') and obj.__class__.__name__ or type(obj).__name__)
            objdict = {}

            for a in dir(obj):
                if a[:2] != "__" and (not hasattr(obj, a) or not hasattr(getattr(obj, a), '__call__')):
                    try:
                        objdict[a] = getattr(obj, a)
                    except Exception as e:
                        objdict[a] = str(e)

    return name + "{\n" + "\n".join(l + repr(k) + ": " + var_dump(v, depth=depth - 1, l=l + "    ") + "," for k, v in
                                    objdict.items()) + "\n" + l + "}"
