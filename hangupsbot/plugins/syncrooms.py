import asyncio, re, time, os, aiohttp

import hangups

from urllib.parse import urlparse
from hangups.ui.utils import get_conv_name

class __registers(object):
    def __init__(self):
        self.last_event_id = '' # recorded last event to avoid re-syncing
        self.last_user_id = '' # recorded last user to allow message compression
        self.last_chatroom_id = '' # recorded last chat room to prevent room crossover
        self.last_time_id = 0 # recorded timestamp of last chat to 'expire' chats

_registers=__registers()


def _initialise(Handlers, bot=None):
    _migrate_syncroom_v1(bot)
    Handlers.register_admin_command(["syncusers"])
    Handlers.register_handler(_handle_syncrooms_broadcast, type="sending")
    Handlers.register_handler(_handle_incoming_message, type="message")
    Handlers.register_handler(_handle_syncrooms_membership_change, type="membership")
    return [] # implements no commands


def _migrate_syncroom_v1(bot):
    if bot.config.exists(["conversations"]):
        write_config = False
        _config2 = []
        _newdict = {}
        _oldlist = bot.config.get_by_path(["conversations"])
        for conv_id in _oldlist:
            parameters = _oldlist[conv_id]
            if "sync_rooms" in parameters:
                old_sync_rooms = parameters["sync_rooms"]
                old_sync_rooms.append(conv_id)
                old_sync_rooms = list(set(old_sync_rooms))
                old_sync_rooms.sort()
                ref_key = "-".join(old_sync_rooms)
                _newdict[ref_key] = old_sync_rooms # prevent duplicates

                del parameters["sync_rooms"] # remove old config
                bot.config.set_by_path(["conversations", conv_id], parameters)
                write_config = True

        if write_config:
            _config2 = list(_newdict.values())
            bot.config.set_by_path(["sync_rooms"], _config2) # write new config
            bot.config.save()
            print(_("_migrate_syncroom_v1(): config-v2 = {}").format(_config2))


def _handle_syncrooms_broadcast(bot, broadcast_list, context):
    """
    handles non-syncroom messages, i.e. messages from other plugins

    for messages explicitly relayed by _handle_syncrooms_broadcast(), this
    handler actually doesn't run
    """
    if not bot.get_config_option('syncing_enabled'):
        return

    if context and "explicit_relay" in context:
        print(_("SYNCROOMS: handler disabled by context"))
        return

    origin_conversation_id = broadcast_list[0][0]
    response = broadcast_list[0][1]

    syncouts = bot.get_config_option('sync_rooms')
    if syncouts:
        for sync_room_list in syncouts:
            if origin_conversation_id in sync_room_list:
                for other_room_id in sync_room_list:
                    if origin_conversation_id != other_room_id:
                        broadcast_list.append((other_room_id, response))

                print(_("SYNCROOMS: broadcasting to {} room(s)").format(
                    len(broadcast_list)))
            else:
                print(_("SYNCROOMS: not a sync room").format(origin_conversation_id))


@asyncio.coroutine
def _handle_incoming_message(bot, event, command):
    """Handle message syncing"""
    if not bot.get_config_option('syncing_enabled'):
        return

    syncouts = bot.get_config_option('sync_rooms')

    if not syncouts:
        return # Sync rooms not configured, returning

    if _registers.last_event_id == event.conv_event.id_:
        return # This event has already been synced

    _registers.last_event_id = event.conv_event.id_

    for sync_room_list in syncouts:
        if event.conv_id in sync_room_list:
            print(_('SYNCROOMS: incoming message'));
            link = 'https://plus.google.com/u/0/{}/about'.format(event.user_id.chat_id)

            ### Deciding how to relay the name across

            # Checking that it hasn't timed out since last message
            timeout_threshold = 30.0 # Number of seconds to allow the timeout
            if time.time() - _registers.last_time_id > timeout_threshold:
                timeout = True
            else:
                timeout = False

            # Checking if the user is the same as the one who sent the previous message
            if _registers.last_user_id in event.user_id.chat_id:
                sameuser = True
            else:
                sameuser = False

            # Checking if the room is the same as the room where the last message was sent
            if _registers.last_chatroom_id in event.conv_id:
                sameroom = True
            else:
                sameroom = False

            if (not sameroom or timeout or not sameuser) and \
                (bot.memory.exists(['user_data', event.user_id.chat_id, "nickname"])):
                # Now check if there is a nickname set

                try:
                    fullname = '{0} ({1})'.format(event.user.full_name.split(' ', 1)[0]
                        , bot.get_memory_suboption(event.user_id.chat_id, 'nickname'))
                except TypeError:
                    fullname = event.user.full_name
            elif sameroom and sameuser and not timeout:
                fullname = '>>'
            else:
                fullname = event.user.full_name

            ### Name decided and put into variable 'fullname'

            segments = [hangups.ChatMessageSegment('{0}'.format(fullname), hangups.SegmentType.LINK,
                                                   link_target=link, is_bold=True),
                        hangups.ChatMessageSegment(': ', is_bold=True)]

            # Make links hyperlinks and send message
            URL_RE = re.compile(r'https?://\S+')
            for segment in event.conv_event.segments:
                last = 0
                for match in URL_RE.finditer(segment.text):
                    if match.start() > last:
                        segments.append(hangups.ChatMessageSegment(segment.text[last:match.start()]))
                    segments.append(hangups.ChatMessageSegment(match.group(), link_target=match.group()))
                    last = match.end()
                if last != len(segment.text):
                    if segment.type_ is hangups.SegmentType.LINE_BREAK:
                        segments.append(hangups.ChatMessageSegment('\n', hangups.SegmentType.LINE_BREAK))
                    else:
                        segments.append(hangups.ChatMessageSegment(segment.text[last:]))

            for _conv_id in sync_room_list:
                if not _conv_id == event.conv_id:

                    _context = {}
                    _context["explicit_relay"] = True

                    if not event.text.startswith(("/bot ", "/me ")):
                        _context["autotranslate"] = {
                            "conv_id" : event.conv_id,
                            "event_text" : event.text }

                    if not event.conv_event.attachments:
                        bot.send_message_segments(_conv_id, list(segments), context=_context)

                    for link in event.conv_event.attachments:
                        # Attempt to upload the photo separately
                        # We need to download the photo first before we can upload it
                        downloadURL = link
                        fileName = os.path.basename(urlparse(downloadURL).path)
                        r = yield from aiohttp.request('get',downloadURL)
                        raw = yield from r.read()
                        newFile = open(fileName,'wb')
                        newFile.write(raw)

                        try:
                            photoID = yield from bot._client.upload_image(fileName)
                            segments.append(hangups.ChatMessageSegment('\n', hangups.SegmentType.LINE_BREAK))
                            segments.append(hangups.ChatMessageSegment('incoming image:', is_italic=True))
                            bot.send_message_segments(_conv_id, list(segments), context=_context)
                            yield from bot._client.sendchatmessage(_conv_id, None, imageID=photoID)
                            # Remove the image after use
                            os.remove(fileName)
                        except AttributeError:
                            segments.append(hangups.ChatMessageSegment('\n', hangups.SegmentType.LINE_BREAK))
                            segments.extend([hangups.ChatMessageSegment(link, hangups.SegmentType.LINK, link_target=link)])
                            bot.send_message_segments(_conv_id, list(segments), context=_context)

            _registers.last_user_id = event.user_id.chat_id
            _registers.last_time_id = time.time()
            _registers.last_chatroom_id = event.conv_id


@asyncio.coroutine
def _handle_syncrooms_membership_change(bot, event, command):
    if not bot.get_config_option('syncing_enabled'):
        return

    # Don't handle events caused by the bot himself
    if event.user.is_self:
        return

    syncouts = bot.get_config_option('sync_rooms')

    if not syncouts:
        return # Sync rooms not configured, returning

    # are we in a sync room?
    sync_room_list = None
    for _rooms in syncouts:
        if event.conv_id in _rooms:
            sync_room_list = _rooms
            break
    if sync_room_list is None:
        return

    # Generate list of added or removed users for current ROOM (NOT SYNCROOMS!)
    event_users = [event.conv.get_user(user_id) for user_id
                   in event.conv_event.participant_ids]
    names = ', '.join([user.full_name for user in event_users])

    syncroom_name = '<b>' + get_conv_name(event.conv) + '</b>'

    # JOIN a specific room
    if event.conv_event.type_ == hangups.MembershipChangeType.JOIN:
        print(_("SYNCROOMS: {} user(s) added to {}").format(len(event_users), event.conv_id))
        if syncroom_name:
            bot.send_message_parsed(event.conv, '<i>{} has added {} to {}</i>'.format(
                event.user.full_name,
                names,
                syncroom_name))
    # LEAVE a specific room
    else:
        print(_("SYNCROOMS: {} user(s) left {}").format(len(event_users), event.conv_id))
        if syncroom_name:
            bot.send_message_parsed(event.conv, '<i>{} has left {}</i>'.format(
                names,
                syncroom_name))


def syncusers(bot, event, conversation_id=None, *args):
    """syncroom-aware users list.
    optional parameter conversation_id to get a list of users in other rooms. will include users
    in linked syncrooms. append "rooms" to segment user list by individual rooms.
    """
    combined = True

    if not conversation_id:
        conversation_id = event.conv_id
    elif conversation_id == "rooms":
        # user specified /bot syncusers rooms
        conversation_id = event.conv_id
        combined = False

    if "rooms" in args:
        # user specified /bot syncusers [roomid] rooms
        combined = False

    syncouts = bot.get_config_option('sync_rooms')

    if not syncouts:
        return # Sync rooms not configured, returning

    _lines = []

    # are we in a sync room?
    sync_room_list = None
    for _rooms in syncouts:
        if conversation_id in _rooms:
            sync_room_list = _rooms
            _lines.append(_("<b>Sync Rooms: {}</b>").format(len(sync_room_list)))
            break
    if sync_room_list is None:
        sync_room_list = [conversation_id]
        _lines.append(_("<b>Standard Room</b>"))

    all_users = {}
    if combined or len(sync_room_list) == 1:
        all_users["_ALL_"] = bot.get_users_in_conversation(sync_room_list)
    else:
        for room_id in sync_room_list:
            all_users[room_id] = bot.get_users_in_conversation(room_id)

    unique_users = []

    for room_id in all_users:
        if room_id is not "_ALL_":
            _line_room = '<i>{}</i>'.format(room_id)
            """XXX: written in this way in case we need to try: get_conv_name in future"""
            _line_room = '<b>{}</b> {}'.format(
                get_conv_name(bot._conv_list.get(room_id)),
                _line_room)
            _lines.append(_line_room)
        list_users = all_users[room_id]
        for User in list_users:
            _line_user = '{}'.format(User.full_name);
            if User.emails:
                _line_user = _line_user + ' ({})'.format(User.emails[0])
            _lines.append(_line_user)
            unique_users.append(User)

    unique_users = list(set(unique_users))
    _lines.append(_("<b>Total Unique: {}</b>").format(len(unique_users)))

    bot.send_message_parsed(event.conv, '<br />'.join(_lines))
