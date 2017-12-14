"""
Различные полезные функции
"""

import types
from datetime import datetime


def display_tg_name(first_name, last_name):
    if first_name and last_name:
        return '{} {}'.format(first_name, last_name)
    elif first_name:
        return first_name
    elif last_name:
        return last_name
    else:
        return '[No name]'


def make_gate_jid():
    pass


def parse_gate_jid():
    pass


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