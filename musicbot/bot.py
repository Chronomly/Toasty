import sys
import time
import shlex
import shutil
import random
import inspect
import logging
import asyncio
import pathlib
import traceback
import os
import json
import random
import aiohttp
allow_requests = True
import requests
from cleverbot import Cleverbot
import giphypop
import discord
import twitter
import urllib.request
from random import randint
from bs4 import BeautifulSoup
from io import BytesIO, StringIO
from functools import wraps
from textwrap import dedent
from datetime import timedelta
from collections import defaultdict
from discord.enums import ChannelType
from discord.ext.commands.bot import _get_variable
from discord.http import _func_
from imp import reload
from urllib.parse import parse_qs
from lxml import etree
from . import exceptions
from . import downloader

from .playlist import Playlist
from .player import MusicPlayer
from .entry import StreamPlaylistEntry
from .opus_loader import load_opus_lib
from .config import Config, ConfigDefaults
from .permissions import Permissions, PermissionsDefaults
from .constructs import SkipState, Response, VoiceStateUpdate
from .utils import load_file, write_file, sane_round_int, fixg, ftimedelta
import musicbot.misc
import musicbot.genre
import logmein
from .constants import VERSION as BOTVERSION
from .constants import DISCORD_MSG_CHAR_LIMIT, AUDIO_CACHE_PATH


load_opus_lib()

log = logging.getLogger(__name__)


class MusicBot(discord.Client):
    def __init__(self, config_file=None, perms_file=None):
        if config_file is None:
            config_file = ConfigDefaults.options_file

        if perms_file is None:
            perms_file = PermissionsDefaults.perms_file
        self.shard_id = 1
        self.shard_count = 2
        self.players = {}
        self.exit_signal = None
        self.init_ok = False
        self.cached_app_info = None
        self.last_status = None

        self.config = Config(config_file)
        self.permissions = Permissions(perms_file, grant_all=[self.config.owner_id])

        self.blacklist = set(load_file(self.config.blacklist_file))
        self.autoplaylist = load_file(self.config.auto_playlist_file)

        self.aiolocks = defaultdict(asyncio.Lock)
        self.downloader = downloader.Downloader(download_folder='audio_cache')

        self._setup_logging()

        if not self.autoplaylist:
            log.warning("Autoplaylist is empty, disabling.")
            self.config.auto_playlist = False
        else:
            log.info("Loaded autoplaylist with {} entries".format(len(self.autoplaylist)))

        if self.blacklist:
            log.debug("Loaded blacklist with {} entries".format(len(self.blacklist)))

        # TODO: Do these properly
        ssd_defaults = {
            'last_np_msg': None,
            'auto_paused': False,
            'availability_paused': False
        }
        self.server_specific_data = defaultdict(ssd_defaults.copy)

        super().__init__()
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += ' MusicBot/%s' % BOTVERSION

    def __del__(self):
        # These functions return futures but it doesn't matter
        try:    self.http.session.close()
        except: pass

        try:    self.aiosession.close()
        except: pass

    # TODO: Add some sort of `denied` argument for a message to send when someone else tries to use it
    def owner_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Only allow the owner to use these commands
            orig_msg = _get_variable('message')

            if not orig_msg or orig_msg.author.id == self.config.owner_id:
                # noinspection PyCallingNonCallable
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("only the owner can use this command", expire_in=30)

        return wrapper

    def dev_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            orig_msg = _get_variable('message')

            if orig_msg.author.id in self.config.dev_ids:
                # noinspection PyCallingNonCallable
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("only dev users can use this command", expire_in=30)

        wrapper.dev_cmd = True
        return wrapper

    def ensure_appinfo(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            await self._cache_app_info()
            # noinspection PyCallingNonCallable
            return await func(self, *args, **kwargs)

        return wrapper

    def _get_owner(self, *, server=None, voice=False):
            return discord.utils.find(
                lambda m: m.id == self.config.owner_id and (m.voice_channel if voice else True),
                server.members if server else self.get_all_members()
            )

    def _delete_old_audiocache(self, path=AUDIO_CACHE_PATH):
        try:
            shutil.rmtree(path)
            return True
        except:
            try:
                os.rename(path, path + '__')
            except:
                return False
            try:
                shutil.rmtree(path)
            except:
                os.rename(path + '__', path)
                return False

        return True

    def _setup_logging(self):
        if len(logging.getLogger(__package__).handlers) > 1:
            log.debug("Skipping logger setup, already set up")
            return
        import colorlog
        shandler = logging.StreamHandler(stream=sys.stdout)
        shandler.setFormatter(colorlog.LevelFormatter(
            fmt = {
                'DEBUG': '{log_color}[{levelname}:{module}] {message}',
                'INFO': '{log_color}{message}',
                'WARNING': '{log_color}{levelname}: {message}',
                'ERROR': '{log_color}[{levelname}:{module}] {message}',
                'CRITICAL': '{log_color}[{levelname}:{module}] {message}',

                'EVERYTHING': '{log_color}[{levelname}:{module}] {message}',
                'NOISY': '{log_color}[{levelname}:{module}] {message}',
                'VOICEDEBUG': '{log_color}[{levelname}:{module}][{relativeCreated:.9f}] {message}',
                'FFMPEG': '{log_color}[{levelname}:{module}][{relativeCreated:.9f}] {message}'
            },
            log_colors = {
                'DEBUG':    'cyan',
                'INFO':     'white',
                'WARNING':  'yellow',
                'ERROR':    'red',
                'CRITICAL': 'bold_red',

                'EVERYTHING': 'white',
                'NOISY':      'white',
                'FFMPEG':     'bold_purple',
                'VOICEDEBUG': 'purple',
        },
            style = '{',
            datefmt = ''
        ))
        shandler.setLevel(self.config.debug_level)
        logging.getLogger(__package__).addHandler(shandler)

        log.debug("Set logging level to {}".format(self.config.debug_level_str))

        if self.config.debug_mode:
            dlogger = logging.getLogger('discord')
            dlogger.setLevel(logging.DEBUG)
            dhandler = logging.FileHandler(filename='logs/discord.log', encoding='utf-8', mode='w')
            dhandler.setFormatter(logging.Formatter('{asctime}:{levelname}:{name}: {message}', style='{'))
            dlogger.addHandler(dhandler)

    @staticmethod
    def _check_if_empty(vchannel: discord.Channel, *, excluding_me=True, excluding_deaf=False):
        def check(member):
            if excluding_me and member == vchannel.server.me:
                return False

            if excluding_deaf and any([member.deaf, member.self_deaf]):
                return False

            return True

        return not sum(1 for m in vchannel.voice_members if check(m))


    async def _join_startup_channels(self, channels, *, autosummon=True):
        joined_servers = set()
        channel_map = {c.server: c for c in channels}

        def _autopause(player):
            if self._check_if_empty(player.voice_client.channel):
                log.info("Initial autopause in empty channel")

                player.pause()
                self.server_specific_data[player.voice_client.channel.server]['auto_paused'] = True

        for server in self.servers:
            if server.unavailable or server in channel_map:
                continue

            if server.me.voice_channel:
                log.info("Found resumable voice channel {0.server.name}/{0.name}".format(server.me.voice_channel))
                channel_map[server] = server.me.voice_channel

            if autosummon:
                owner = self._get_owner(server=server, voice=True)
                if owner:
                    log.info("Found owner in \"{}\"".format(owner.voice_channel.name))
                    channel_map[server] = owner.voice_channel

        for server, channel in channel_map.items():
            if server in joined_servers:
                log.info("Already joined a channel in \"{}\", skipping".format(server.name))
                continue

            if channel and channel.type == discord.ChannelType.voice:
                log.info("Attempting to join {0.server.name}/{0.name}".format(channel))

                chperms = channel.permissions_for(server.me)

                if not chperms.connect:
                    log.info("Cannot join channel \"{}\", no permission.".format(channel.name))
                    continue

                elif not chperms.speak:
                    log.info("Will not join channel \"{}\", no permission to speak.".format(channel.name))
                    continue

                try:
                    player = await self.get_player(channel, create=True, deserialize=self.config.persistent_queue)
                    joined_servers.add(server)

                    log.info("Joined {0.server.name}/{0.name}".format(channel))

                    if player.is_stopped:
                        player.play()

                    if self.config.auto_playlist and not player.playlist.entries:
                        await self.on_player_finished_playing(player)
                        if self.config.auto_pause:
                            player.once('play', lambda player, **_: _autopause(player))

                except Exception:
                    log.debug("Error joining {0.server.name}/{0.name}".format(channel), exc_info=True)
                    log.error("Failed to join {0.server.name}/{0.name}".format(channel))

            elif channel:
                log.warning("Not joining {0.server.name}/{0.name}, that's a text channel.".format(channel))

            else:
                log.warning("Invalid channel thing: {}".format(channel))

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message, quiet=True)

    # TODO: Check to see if I can just move this to on_message after the response check
    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def _check_ignore_non_voice(self, msg):
        vc = msg.server.me.voice_channel

        # If we've connected to a voice chat and we're in the same voice channel
        if not vc or vc == msg.author.voice_channel:
            return True
        else:
            raise exceptions.PermissionsError(
                "you cannot use this command when not in the voice channel (%s)" % vc.name, expire_in=30)

    async def _cache_app_info(self, *, update=False):
        if not self.cached_app_info and not update and self.user.bot:
            log.debug("Caching app info")
            self.cached_app_info = await self.application_info()

        return self.cached_app_info


    async def remove_from_autoplaylist(self, song_url:str, *, ex:Exception=None, delete_from_ap=False):
        if song_url not in self.autoplaylist:
            log.debug("URL \"{}\" not in autoplaylist, ignoring".format(song_url))
            return

        async with self.aiolocks[_func_()]:
            self.autoplaylist.remove(song_url)
            log.info("Removing unplayable song from autoplaylist: %s" % song_url)

            with open(self.config.auto_playlist_removed_file, 'a', encoding='utf8') as f:
                f.write(
                    '# Entry removed {ctime}\n'
                    '# Reason: {ex}\n'
                    '{url}\n\n{sep}\n\n'.format(
                        ctime=time.ctime(),
                        ex=str(ex).replace('\n', '\n#' + ' ' * 10), # 10 spaces to line up with # Reason:
                        url=song_url,
                        sep='#' * 32
                ))

            if delete_from_ap:
                log.info("Updating autoplaylist")
                write_file(self.config.auto_playlist_file, self.autoplaylist)

    @ensure_appinfo
    async def generate_invite_link(self, *, permissions=discord.Permissions(70380544), server=None):
        return discord.utils.oauth_url(self.cached_app_info.id, permissions=permissions, server=server)


    async def join_voice_channel(self, channel):
        if isinstance(channel, discord.Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise discord.InvalidArgument('That isnt a voice channel :confused:')

        server = channel.server

        if self.is_voice_connected(server):
            raise discord.ClientException('Im already there XD')

        def session_id_found(data):
            user_id = data.get('user_id')
            guild_id = data.get('guild_id')
            return user_id == self.user.id and guild_id == server.id

        log.voicedebug("(%s) creating futures", _func_())
        # register the futures for waiting
        session_id_future = self.ws.wait_for('VOICE_STATE_UPDATE', session_id_found)
        voice_data_future = self.ws.wait_for('VOICE_SERVER_UPDATE', lambda d: d.get('guild_id') == server.id)

        # "join" the voice channel
        log.voicedebug("(%s) setting voice state", _func_())
        await self.ws.voice_state(server.id, channel.id)

        log.voicedebug("(%s) waiting for session id", _func_())
        session_id_data = await asyncio.wait_for(session_id_future, timeout=15, loop=self.loop)

        # sometimes it gets stuck on this step.  Jake said to wait indefinitely.  To hell with that.
        log.voicedebug("(%s) waiting for voice data", _func_())
        data = await asyncio.wait_for(voice_data_future, timeout=15, loop=self.loop)

        kwargs = {
            'user': self.user,
            'channel': channel,
            'data': data,
            'loop': self.loop,
            'session_id': session_id_data.get('session_id'),
            'main_ws': self.ws
        }

        voice = discord.VoiceClient(**kwargs)
        try:
            log.voicedebug("(%s) connecting...", _func_())
            with aiohttp.Timeout(15):
                await voice.connect()

        except asyncio.TimeoutError as e:
            log.voicedebug("(%s) connection failed, disconnecting", _func_())
            try:
                await voice.disconnect()
            except:
                pass
            raise e

        log.voicedebug("(%s) connection successful", _func_())

        self.connection._add_voice_client(server.id, voice)
        return voice


    async def get_voice_client(self, channel: discord.Channel):
        if isinstance(channel, discord.Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        async with self.aiolocks[_func_() + ':' + channel.server.id]:
            if self.is_voice_connected(channel.server):
                return self.voice_client_in(channel.server)

            vc = None
            t0 = t1 = 0
            tries = 5

            for attempt in range(1, tries+1):
                log.debug("Connection attempt {} to {}".format(attempt, channel.name))
                t0 = time.time()

                try:
                    vc = await self.join_voice_channel(channel)
                    t1 = time.time()
                    break

                except asyncio.TimeoutError:
                    log.warning("Failed to connect, retrying ({}/{})".format(attempt, tries))

                    # TODO: figure out if I need this or not
                    # try:
                    #     await self.ws.voice_state(channel.server.id, None)
                    # except:
                    #     pass

                except:
                    log.exception("Unknown error attempting to connect to voice")

                await asyncio.sleep(0.5)

            if not vc:
                log.critical("Voice client is unable to connect, restarting...")
                await self.restart()

            log.debug("Connected in {:0.1f}s".format(t1-t0))
            log.info("Connected to {}/{}".format(channel.server, channel))

            vc.ws._keep_alive.name = 'VoiceClient Keepalive'

            return vc

    async def reconnect_voice_client(self, server, *, sleep=0.1, channel=None):
        log.debug("Reconnecting voice client on \"{}\"{}".format(
            server, ' to "{}"'.format(channel.name) if channel else ''))

        async with self.aiolocks[_func_() + ':' + server.id]:
            vc = self.voice_client_in(server)

            if not (vc or channel):
                return

            _paused = False
            player = self.get_player_in(server)

            if player and player.is_playing:
                log.voicedebug("(%s) Pausing", _func_())

                player.pause()
                _paused = True

            log.voicedebug("(%s) Disconnecting", _func_())

            try:
                await vc.disconnect()
            except:
                pass

            if sleep:
                log.voicedebug("(%s) Sleeping for %s", _func_(), sleep)
                await asyncio.sleep(sleep)

            if player:
                log.voicedebug("(%s) Getting voice client", _func_())

                if not channel:
                    new_vc = await self.get_voice_client(vc.channel)
                else:
                    new_vc = await self.get_voice_client(channel)

                log.voicedebug("(%s) Swapping voice client", _func_())
                await player.reload_voice(new_vc)

                if player.is_paused and _paused:
                    log.voicedebug("Resuming")
                    player.resume()

        log.debug("Reconnected voice client on \"{}\"{}".format(
            server, ' to "{}"'.format(channel.name) if channel else ''))

    async def disconnect_voice_client(self, server):
        vc = self.voice_client_in(server)
        if not vc:
            return

        if server.id in self.players:
            self.players.pop(server.id).kill()

        await vc.disconnect()

    async def disconnect_all_voice_clients(self):
        for vc in list(self.voice_clients).copy():
            await self.disconnect_voice_client(vc.channel.server)

    async def set_voice_state(self, vchannel, *, mute=False, deaf=False):
        if isinstance(vchannel, discord.Object):
            vchannel = self.get_channel(vchannel.id)

        if getattr(vchannel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        await self.ws.voice_state(vchannel.server.id, vchannel.id, mute, deaf)
        # I hope I don't have to set the channel here
        # instead of waiting for the event to update it

    def get_player_in(self, server: discord.Server) -> MusicPlayer:
        return self.players.get(server.id)

    async def get_player(self, channel, create=False, *, deserialize=False) -> MusicPlayer:
        server = channel.server

        async with self.aiolocks[_func_() + ':' + server.id]:
            if deserialize:
                voice_client = await self.get_voice_client(channel)
                player = await self.deserialize_queue(server, voice_client)

                if player:
                    log.debug("Created player via deserialization for server %s with %s entries", server.id, len(player.playlist))
                    # Since deserializing only happens when the bot starts, I should never need to reconnect
                    return self._init_player(player, server=server)

            if server.id not in self.players:
                if not create:
                    raise exceptions.CommandError(
                        'Im not in a voice channel.  '
                        'Use %sspawn to spawn me to your voice channel.' % self.config.command_prefix)

                voice_client = await self.get_voice_client(channel)

                playlist = Playlist(self)
                player = MusicPlayer(self, voice_client, playlist)
                self._init_player(player, server=server)

            async with self.aiolocks[self.reconnect_voice_client.__name__ + ':' + server.id]:
                if self.players[server.id].voice_client not in self.voice_clients:
                    log.debug("Reconnect required for voice client in {}".format(server.name))
                    await self.reconnect_voice_client(server, channel=channel)

        return self.players[server.id]

    def _init_player(self, player, *, server=None):
        player = player.on('play', self.on_player_play) \
                       .on('resume', self.on_player_resume) \
                       .on('pause', self.on_player_pause) \
                       .on('stop', self.on_player_stop) \
                       .on('finished-playing', self.on_player_finished_playing) \
                       .on('entry-added', self.on_player_entry_added) \
                       .on('error', self.on_player_error)

        player.skip_state = SkipState()

        if server:
            self.players[server.id] = player

        return player

    async def on_player_play(self, player, entry):
        await self.update_now_playing_status(entry)
        player.skip_state.reset()

        # This is the one event where its ok to serialize autoplaylist entries
        await self.serialize_queue(player.voice_client.channel.server)

        channel = entry.meta.get('channel', None)
        author = entry.meta.get('author', None)

        if channel and author:
            last_np_msg = self.server_specific_data[channel.server]['last_np_msg']
            if last_np_msg and last_np_msg.channel == channel:

                async for lmsg in self.logs_from(channel, limit=1):
                    if lmsg != last_np_msg and last_np_msg:
                        await self.safe_delete_message(last_np_msg)
                        self.server_specific_data[channel.server]['last_np_msg'] = None
                    break  # This is probably redundant

            if self.config.now_playing_mentions:
                newmsg = '%s - your song **%s** is now playing in %s!' % (
                    entry.meta['author'].mention, entry.title, player.voice_client.channel.name)
            else:
                newmsg = 'Now playing in %s: **%s**' % (
                    player.voice_client.channel.name, entry.title)

            if self.server_specific_data[channel.server]['last_np_msg']:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_edit_message(last_np_msg, newmsg, send_if_fail=True)
            else:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_send_message(channel, newmsg)

        # TODO: Check channel voice state?

    async def on_player_resume(self, player, entry, **_):
        await self.update_now_playing_status(entry)

    async def on_player_pause(self, player, entry, **_):
        await self.update_now_playing_status(entry, True)
        # await self.serialize_queue(player.voice_client.channel.server)

    async def on_player_stop(self, player, **_):
        await self.update_now_playing_status()

    async def on_player_finished_playing(self, player, **_):
        if not player.playlist.entries and not player.current_entry and self.config.auto_playlist:
            while self.autoplaylist:
                random.shuffle(self.autoplaylist)
                song_url = random.choice(self.autoplaylist)

                info = {}

                try:
                    info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                except downloader.youtube_dl.utils.DownloadError as e:
                    if 'YouTube said:' in e.args[0]:
                        # url is bork, remove from list and put in removed list
                        log.error("Error processing youtube url:\n{}".format(e.args[0]))

                    else:
                        # Probably an error from a different extractor, but I've only seen youtube's
                        log.error("Error processing \"{url}\": {ex}".format(url=song_url, ex=e))

                    await self.remove_from_autoplaylist(song_url, ex=e, delete_from_ap=True)
                    continue

                except Exception as e:
                    log.error("Error processing \"{url}\": {ex}".format(url=song_url, ex=e))
                    log.exception()

                    self.autoplaylist.remove(song_url)
                    continue

                if info.get('entries', None):  # or .get('_type', '') == 'playlist'
                    log.debug("Playlist found but is unsupported at this time, skipping.")
                    # TODO: Playlist expansion

                # Do I check the initial conditions again?
                # not (not player.playlist.entries and not player.current_entry and self.config.auto_playlist)

                try:
                    await player.playlist.add_entry(song_url, channel=None, author=None)
                except exceptions.ExtractionError as e:
                    log.error("Error adding song from autoplaylist: {}".format(e))
                    log.debug('', exc_info=True)
                    continue

                break

            if not self.autoplaylist:
                # TODO: When I add playlist expansion, make sure that's not happening during this check
                log.warning("No playable songs in the autoplaylist, disabling.")
                self.config.auto_playlist = False

        else: # Don't serialize for autoplaylist events
            await self.serialize_queue(player.voice_client.channel.server)

    async def on_player_entry_added(self, player, playlist, entry, **_):
        if entry.meta.get('author') and entry.meta.get('channel'):
            await self.serialize_queue(player.voice_client.channel.server)

    async def on_player_error(self, player, entry, ex, **_):
        if 'channel' in entry.meta:
            await self.safe_send_message(
                entry.meta['channel'],
                "```\nError from FFmpeg:\n{}\n```".format(ex)
            )
        else:
            log.exception("Player error", exc_info=ex)

    async def update_now_playing_status(self, entry=None, is_paused=False):
        game = None

        if self.user.bot:
            activeplayers = sum(1 for p in self.players.values() if p.is_playing)
            if activeplayers > 1:
                game = discord.Game(name=" with toast in %s servers" % activeplayers)
                entry = None

            elif activeplayers == 1:
                player = discord.utils.get(self.players.values(), is_playing=True)
                entry = player.current_entry

        if entry:
            prefix = u'\u275A\u275A ' if is_paused else ''

            name = u'{}{}'.format(prefix, entry.title)[:128]
            game = discord.Game(name=name)
        await self.change_presence(game = game)

                

    async def update_now_playing_message(self, server, message, *, channel=None):
        lnp = self.server_specific_data[server]['last_np_msg']
        m = None

        if message is None and lnp:
            await self.safe_delete_message(lnp, quiet=True)

        elif lnp: # If there was a previous lp message
            oldchannel = lnp.channel

            if lnp.channel == oldchannel: # If we have a channel to update it in
                async for lmsg in self.logs_from(channel, limit=1):
                    if lmsg != lnp and lnp: # If we need to resend it
                        await self.safe_delete_message(lnp, quiet=True)
                        m = await self.safe_send_message(channel, message, quiet=True)
                    else:
                        m = await self.safe_edit_message(lnp, message, send_if_fail=True, quiet=False)

            elif channel: # If we have a new channel to send it to
                await self.safe_delete_message(lnp, quiet=True)
                m = await self.safe_send_message(channel, message, quiet=True)

            else: # we just resend it in the old channel
                await self.safe_delete_message(lnp, quiet=True)
                m = await self.safe_send_message(oldchannel, message, quiet=True)

        elif channel: # No previous message
            m = await self.safe_send_message(channel, message, quiet=True)

        self.server_specific_data[server]['last_np_msg'] = m


    async def serialize_queue(self, server, *, dir=None):
        """
        Serialize the current queue for a server's player to json.
        """

        player = self.get_player_in(server)
        if not player:
            return

        if dir is None:
            dir = 'data/%s/queue.json' % server.id

        async with self.aiolocks['queue_serialization'+':'+server.id]:
            log.debug("Serializing queue for %s", server.id)

            with open(dir, 'w', encoding='utf8') as f:
                f.write(player.serialize(sort_keys=True))

    async def serialize_all_queues(self, *, dir=None):
        coros = [self.serialize_queue(s, dir=dir) for s in self.servers]
        await asyncio.gather(*coros, return_exceptions=True)

    async def deserialize_queue(self, server, voice_client, playlist=None, *, dir=None) -> MusicPlayer:
        """
        Deserialize a saved queue for a server into a MusicPlayer.  If no queue is saved, returns None.
        """

        if playlist is None:
            playlist = Playlist(self)

        if dir is None:
            dir = 'data/%s/queue.json' % server.id

        async with self.aiolocks['queue_serialization' + ':' + server.id]:
            if not os.path.isfile(dir):
                return None

            log.debug("Deserializing queue for %s", server.id)

            with open(dir, 'r', encoding='utf8') as f:
                data = f.read()

        return MusicPlayer.from_json(data, self, voice_client, playlist)

    @ensure_appinfo
    async def _on_ready_sanity_checks(self):
        # Ensure folders exist
        await self._scheck_ensure_env()

        # Server permissions check
        await self._scheck_server_permissions()

        # playlists in autoplaylist
        await self._scheck_autoplaylist()

        # config/permissions async validate?
        await self._scheck_configs()


    async def _scheck_ensure_env(self):
        log.debug("Ensuring data folders exist")
        for server in self.servers:
            pathlib.Path('data/%s/' % server.id).mkdir(exist_ok=True)

        with open('data/server_names.txt', 'w', encoding='utf8') as f:
            for server in sorted(self.servers, key=lambda s:int(s.id)):
                f.write('{:<22} {}\n'.format(server.id, server.name))

        if not self.config.save_videos and os.path.isdir(AUDIO_CACHE_PATH):
            if self._delete_old_audiocache():
                log.debug("Deleted old audio cache")
            else:
                log.debug("Could not delete old audio cache, moving on.")


    async def _scheck_server_permissions(self):
        log.debug("Checking server permissions")
        pass # TODO

    async def _scheck_autoplaylist(self):
        log.debug("Auditing autoplaylist")
        pass # TODO

    async def _scheck_configs(self):
        log.debug("Validating config")
        await self.config.async_validate(self)

        log.debug("Validating permissions config")
        await self.permissions.async_validate(self)



#######################################################################################################################



    async def safe_send_message(self, dest, content, *, tts=False, expire_in=0, also_delete=None, quiet=False):
        msg = None
        try:
            msg = await self.send_message(dest, content, tts=tts)
        except discord.Forbidden:
            if not quiet:
                await self.safe_send_message((discord.Object(id='228835542417014784')),"Warning: Cannot send message to %s, no permission" % dest.name)
        except discord.NotFound:
            if not quiet:
                await self.safe_send_message((discord.Object(id='228835542417014784')),"Warning: Cannot send message to %s, invalid channel?" % dest.name)
        return msg


    async def safe_delete_message(self, message, *, quiet=False):
        lfunc = log.debug if quiet else log.warning

        try:
            if "/toast" in str(message):
                return
            return await self.delete_message(message)

        except discord.Forbidden:
            lfunc("Cannot delete message \"{}\", no permission".format(message.clean_content))

        except discord.NotFound:
            lfunc("Cannot delete message \"{}\", message not found".format(message.clean_content))

    async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
        lfunc = log.debug if quiet else log.warning

        try:
            return await self.edit_message(message, new)

        except discord.NotFound:
            lfunc("Cannot edit message \"{}\", message not found".format(message.clean_content))
            if send_if_fail:
                lfunc("Sending message instead")
                return await self.safe_send_message(message.channel, new)

    async def send_typing(self, destination):
        try:
            return await super().send_typing(destination)
        except discord.Forbidden:
            log.warning("Could not send typing to {}, no permission".format(destination))

    async def edit_profile(self, **fields):
        if self.user.bot:
            return await super().edit_profile(**fields)
        else:
            return await super().edit_profile(self.config._password,**fields)


    async def restart(self):
        self.exit_signal = exceptions.RestartSignal()
        await self.logout()

    def restart_threadsafe(self):
        asyncio.run_coroutine_threadsafe(self.restart(), self.loop)

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.logout())
        except: pass

        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except: pass

    # noinspection PyMethodOverriding
    def run(self):
        try:
            self.loop.run_until_complete(self.start(logmein.token()))

        except discord.errors.LoginFailure:
            # Add if token, else
            raise exceptions.HelpfulError(
                "Bot cannot login, bad credentials.",
                "Fix your %s in the options file.  "
                "Remember that each field should be on their own line."
                % ['shit', 'Token', 'Email/Password', 'Credentials'][len(self.config.auth)]
            ) #     ^^^^ In theory self.config.auth should never have no items

        finally:
            try:
                self._cleanup()
            except Exception:
                log.error("Error in cleanup", exc_info=True)

            self.loop.close()
            if self.exit_signal:
                raise self.exit_signal

    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            log.error("Exception in {}:\n{}".format(event, ex.message))

            await asyncio.sleep(2)  # don't ask
            await self.logout()

        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.logout()

        else:
            log.error("Exception in {}".format(event), exc_info=True)

    async def on_resumed(self):
        log.info("\nReconnected to discord.\n")

    async def on_ready(self):
        dlogger = logging.getLogger('discord')
        for h in dlogger.handlers:
            if getattr(h, 'terminator', None) == '':
                dlogger.removeHandler(h)
                print()

        log.debug("Connection established, ready to go.")

        self.ws._keep_alive.name = 'Gateway Keepalive'

        if self.init_ok:
            log.debug("Received additional READY event, may have failed to resume")
            return

        await self._on_ready_sanity_checks()
        print()

        log.info('Connected!  Musicbot v{}\n'.format(BOTVERSION))

        self.init_ok = True

        ################################

        log.info("Bot:   {0}/{1}#{2}{3}".format(
            self.user.id,
            self.user.name,
            self.user.discriminator,
            ' [BOT]' if self.user.bot else ' [Userbot]'
        ))

        owner = self._get_owner(voice=True) or self._get_owner()
        if owner and self.servers:
            log.info("Owner: {0}/{1}#{2}\n".format(
                owner.id,
                owner.name,
                owner.discriminator
            ))

            log.info('Server List:')
            [log.info(' - ' + s.name) for s in self.servers]

        elif self.servers:
            log.warning("Owner could not be found on any server (id: %s)\n" % self.config.owner_id)

            log.info('Server List:')
            [log.info(' - ' + s.name) for s in self.servers]

        else:
            log.warning("Owner unknown, bot is not on any servers.")
            if self.user.bot:
                log.warning(
                    "To make the bot join a server, paste this link in your browser. \n"
                    "Note: You should be logged into your main account and have \n"
                    "manage server permissions on the server you want the bot to join.\n"
                    "  " + await self.generate_invite_link()
                )

        print(flush=True)

        if self.config.bound_channels:
            chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if c.type == discord.ChannelType.voice)

            chlist.difference_update(invalids)
            self.config.bound_channels.difference_update(invalids)

            if chlist:
                log.info("Bound to text channels:")
                [log.info(' - {}/{}'.format(ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]
            else:
                print("Not bound to any text channels")

            if invalids and self.config.debug_mode:
                print(flush=True)
                log.info("Not binding to voice channels:")
                [log.info(' - {}/{}'.format(ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

            print(flush=True)

        else:
            log.info("Not bound to any text channels")

        if self.config.autojoin_channels:
            chlist = set(self.get_channel(i) for i in self.config.autojoin_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if c.type == discord.ChannelType.text)

            chlist.difference_update(invalids)
            self.config.autojoin_channels.difference_update(invalids)

            if chlist:
                log.info("Autojoining voice chanels:")
                [log.info(' - {}/{}'.format(ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]
            else:
                log.info("Not autojoining any voice channels")

            if invalids and self.config.debug_mode:
                print(flush=True)
                log.info("Cannot autojoin text channels:")
                [log.info(' - {}/{}'.format(ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

            autojoin_channels = chlist

        else:
            log.info("Not autojoining any voice channels")
            autojoin_channels = set()

        print(flush=True)
        log.info("Options:")

        log.info("  Command prefix: " + self.config.command_prefix)
        log.info("  Default volume: {}%".format(int(self.config.default_volume * 100)))
        log.info("  Skip threshold: {} votes or {}%".format(
            self.config.skips_required, fixg(self.config.skip_ratio_required * 100)))
        log.info("  Now Playing @mentions: " + ['Disabled', 'Enabled'][self.config.now_playing_mentions])
        log.info("  Auto-Summon: " + ['Disabled', 'Enabled'][self.config.auto_summon])
        log.info("  Auto-Playlist: " + ['Disabled', 'Enabled'][self.config.auto_playlist])
        log.info("  Auto-Pause: " + ['Disabled', 'Enabled'][self.config.auto_pause])
        log.info("  Delete Messages: " + ['Disabled', 'Enabled'][self.config.delete_messages])
        if self.config.delete_messages:
            log.info("    Delete Invoking: " + ['Disabled', 'Enabled'][self.config.delete_invoking])
        log.info("  Debug Mode: " + ['Disabled', 'Enabled'][self.config.debug_mode])
        log.info("  Downloaded songs will be " + ['deleted', 'saved'][self.config.save_videos])
        print(flush=True)

        # maybe option to leave the ownerid blank and generate a random command for the owner to use
        # wait_for_message is pretty neato

        await self._join_startup_channels(autojoin_channels, autosummon=self.config.auto_summon)

        # t-t-th-th-that's all folks!

    async def cmd_help(self, author, channel, command=None):
        """
        Usage:
            {command_prefix}help

        a help message.
        """
        await self.safe_send_message(author,"**Commands**\n")
        helpmsg1 = musicbot.misc.helpmusic()
        helpmsg2 = musicbot.misc.helputility()
        helpmsg3 = musicbot.misc.helpadmin()
        helpmsg4 = musicbot.misc.helpchat()
        #if user_permissions.name == "Donators": #checking if the user is a donator
        #    helpmsg5 = musicbot.misc.helpdonate()
        await self.safe_send_message(author, helpmsg1)
        await self.safe_send_message(author, helpmsg2)
        await self.safe_send_message(author, helpmsg3)
        await self.safe_send_message(author, helpmsg4)
        #if user_permissions.name == "Donators":
        #    await self.safe_send_message(author, helpmsg5) #extra help msg for donators
        #    pass
       # else:
            #donators dont get asked to donate
        await self.safe_send_message(author, "Rememer to donate (**/donate**) it really helps us out")
        await self.safe_send_message(channel, "Ive sent my commands to your dm :smile:")
        

    async def cmd_blacklist(self, message, user_mentions, option, something):
        """
        Usage:
            {command_prefix}blacklist [ + | - | add | remove ] @UserName [@UserName2 ...]

        Add or remove users to the blacklist.
        Blacklisted users are forbidden from using bot commands.
        """

        if not user_mentions:
            raise exceptions.CommandError("No users listed.", expire_in=20)

        if option not in ['+', '-', 'add', 'remove']:
            raise exceptions.CommandError(
                'Invalid option "%s" specified, use +, -, add, or remove' % option, expire_in=20
            )

        for user in user_mentions.copy():
            if user.id == self.config.owner_id:
                print("[Commands:Blacklist] The owner cannot be blacklisted.")
                user_mentions.remove(user)

        old_len = len(self.blacklist)

        if option in ['+', 'add']:
            self.blacklist.update(user.id for user in user_mentions)

            write_file(self.config.blacklist_file, self.blacklist)

            return Response(
                '%s users have been added to the blacklist' % (len(self.blacklist) - old_len),
                reply=True, delete_after=10
            )

        else:
            if self.blacklist.isdisjoint(user.id for user in user_mentions):
                return Response('none of those users are in the blacklist.', reply=True, delete_after=10)

            else:
                self.blacklist.difference_update(user.id for user in user_mentions)
                write_file(self.config.blacklist_file, self.blacklist)

                return Response(
                    '%s users have been removed from the blacklist' % (old_len - len(self.blacklist)),
                    reply=True, delete_after=10
                )

    async def cmd_id(self, author, user_mentions):
        """
        Usage:
            {command_prefix}id [@user]

        Tells the user their id or the id of another user.
        """
        if not user_mentions:
            return Response('your id is `%s`' % author.id, reply=True, delete_after=35)
        else:
            usr = user_mentions[0]
            return Response("%s's id is `%s`" % (usr.name, usr.id), reply=True, delete_after=35)

    async def cmd_play(self, player, channel, author, permissions, leftover_args, song_url):
        """
        Usage:
            {command_prefix}play song_link
            {command_prefix}play text to search for

        Adds the song to the playlist.  If a link is not provided, the first
        result from a youtube search is added to the queue.
        """

        song_url = song_url.strip('<>')

        if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
            raise exceptions.PermissionsError(
                "You have reached your enqueued song limit (%s)" % permissions.max_songs, expire_in=30
            )

        await self.send_typing(channel)

        if leftover_args:
            song_url = ' '.join([song_url, *leftover_args])

        try:
            info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=30)

        if not info:
            raise exceptions.CommandError(
                "I....I cant play that.  Try using {}stream.".format(self.config.command_prefix),
                expire_in=30
            )

        # abstract the search handling away from the user
        # our ytdl options allow us to use search strings as input urls
        if info.get('url', '').startswith('ytsearch'):
            # print("[Command:play] Searching for \"%s\"" % song_url)
            info = await self.downloader.extract_info(
                player.playlist.loop,
                song_url,
                download=False,
                process=True,    # ASYNC LAMBDAS WHEN
                on_error=lambda e: asyncio.ensure_future(
                    self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
                retry_on_error=True
            )

            if not info:
                raise exceptions.CommandError(
                    "Error extracting info from search string, youtubedl returned no data.  "
                    "Type /bug", expire_in=30
                )

            if not all(info.get('entries', [])):
                # empty list, no data
                log.debug("Got empty list, no data")
                return

            # TODO: handle 'webpage_url' being 'ytsearch:...' or extractor type
            song_url = info['entries'][0]['webpage_url']
            info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
            # Now I could just do: return await self.cmd_play(player, channel, author, song_url)
            # But this is probably fine

        # TODO: Possibly add another check here to see about things like the bandcamp issue
        # TODO: Where ytdl gets the generic extractor version with no processing, but finds two different urls

        if 'entries' in info:
            # I have to do exe extra checks anyways because you can request an arbitrary number of search results
            if not permissions.allow_playlists and ':search' in info['extractor'] and len(info['entries']) > 1:
                raise exceptions.PermissionsError("You are not allowed to request playlists", expire_in=30)

            # The only reason we would use this over `len(info['entries'])` is if we add `if _` to this one
            num_songs = sum(1 for _ in info['entries'])

            if permissions.max_playlist_length and num_songs > permissions.max_playlist_length:
                raise exceptions.PermissionsError(
                    "Playlist has too many entries (%s > %s)" % (num_songs, permissions.max_playlist_length),
                    expire_in=30
                )

            # This is a little bit weird when it says (x + 0 > y), I might add the other check back in
            if permissions.max_songs and player.playlist.count_for_user(author) + num_songs > permissions.max_songs:
                raise exceptions.PermissionsError(
                    "Playlist entries + your already queued songs reached limit (%s + %s > %s)" % (
                        num_songs, player.playlist.count_for_user(author), permissions.max_songs),
                    expire_in=30
                )

            if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
                try:
                    return await self._cmd_play_playlist_async(player, channel, author, permissions, song_url, info['extractor'])
                except exceptions.CommandError:
                    raise
                except Exception as e:
                    log.error("Error queuing playlist", exc_info=True)
                    raise exceptions.CommandError("Error queuing playlist:\n%s" % e, expire_in=30)

            t0 = time.time()

            # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
            # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
            # I don't think we can hook into it anyways, so this will have to do.
            # It would probably be a thread to check a few playlists and get the speed from that
            # Different playlists might download at different speeds though
            wait_per_song = 1.2

            procmesg = await self.safe_send_message(
                channel,
                'Gathering playlist information for {} songs{}'.format(
                    num_songs,
                    ', ETA: {} seconds'.format(fixg(
                        num_songs * wait_per_song)) if num_songs >= 10 else '.'))

            # We don't have a pretty way of doing this yet.  We need either a loop
            # that sends these every 10 seconds or a nice context manager.
            await self.send_typing(channel)

            # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
            #       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

            entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

            tnow = time.time()
            ttime = tnow - t0
            listlen = len(entry_list)
            drop_count = 0

            if permissions.max_song_length:
                for e in entry_list.copy():
                    if e.duration > permissions.max_song_length:
                        player.playlist.entries.remove(e)
                        entry_list.remove(e)
                        drop_count += 1
                        # Im pretty sure there's no situation where this would ever break
                        # Unless the first entry starts being played, which would make this a race condition
                if drop_count:
                    print("Dropped %s songs" % drop_count)

            log.info("Ready to play".format(
                listlen,
                fixg(ttime),
                ttime / listlen if listlen else 0,
                ttime / listlen - wait_per_song if listlen - wait_per_song else 0,
                fixg(wait_per_song * num_songs))
            )

            await self.safe_delete_message(procmesg)

            if not listlen - drop_count:
                raise exceptions.CommandError(
                    "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length,
                    expire_in=30
                )

            reply_text = "Enqueued **%s** songs to be played. Position in queue: %s"
            btext = str(listlen - drop_count)

        else:
            if permissions.max_song_length and info.get('duration', 0) > permissions.max_song_length:
                raise exceptions.PermissionsError(
                    "Song duration exceeds limit (%s > %s)" % (info['duration'], permissions.max_song_length),
                    expire_in=30
                )

            try:
                entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

            except exceptions.WrongEntryTypeError as e:
                if e.use_url == song_url:
                    log.warning("Determined incorrect entry type, but suggested url is the same.  Help.")

                log.debug("Assumed url \"%s\" was a single entry, was actually a playlist" % song_url)
                log.debug("Using \"%s\" instead" % e.use_url)

                return await self.cmd_play(player, channel, author, permissions, leftover_args, e.use_url)

            reply_text = "Enqueued **%s** to be played. Position in queue: %s"
            btext = entry.title

        if position == 1 and player.is_stopped:
            position = 'Up next!'
            reply_text %= (btext, position)

        else:
            try:
                time_until = await player.playlist.estimate_time_until(position, player)
                reply_text += ' - Gunna play in %s'
            except:
                traceback.print_exc()
                time_until = ''

            reply_text %= (btext, position, ftimedelta(time_until))

        return Response(reply_text, delete_after=30)

    async def _cmd_play_playlist_async(self, player, channel, author, permissions, playlist_url, extractor_type):
        """
        Secret handler to use the async wizardry to make playlist queuing non-"blocking"
        """

        await self.send_typing(channel)
        info = await self.downloader.extract_info(player.playlist.loop, playlist_url, download=False, process=False)

        if not info:
            raise exceptions.CommandError("That playlist cannot be played.")

        num_songs = sum(1 for _ in info['entries'])
        t0 = time.time()

        busymsg = await self.safe_send_message(
            channel, "Processing %s songs..." % num_songs)  # TODO: From playlist_title
        await self.send_typing(channel)

        entries_added = 0
        if extractor_type == 'youtube:playlist':
            try:
                entries_added = await player.playlist.async_process_youtube_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                log.error("Error processing playlist", exc_info=True)
                raise exceptions.CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)

        elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
            try:
                entries_added = await player.playlist.async_process_sc_bc_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                log.error("Error processing playlist", exc_info=True)
                raise exceptions.CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)


        songs_processed = len(entries_added)
        drop_count = 0
        skipped = False

        if permissions.max_song_length:
            for e in entries_added.copy():
                if e.duration > permissions.max_song_length:
                    try:
                        player.playlist.entries.remove(e)
                        entries_added.remove(e)
                        drop_count += 1
                    except:
                        pass

            if drop_count:
                log.debug("Dropped %s songs" % drop_count)

            if player.current_entry and player.current_entry.duration > permissions.max_song_length:
                await self.safe_delete_message(self.server_specific_data[channel.server]['last_np_msg'])
                self.server_specific_data[channel.server]['last_np_msg'] = None
                skipped = True
                player.skip()
                entries_added.pop()

        await self.safe_delete_message(busymsg)

        songs_added = len(entries_added)
        tnow = time.time()
        ttime = tnow - t0
        wait_per_song = 0.66
        # TODO: actually calculate wait per song in the process function and return that too

        # This is technically inaccurate since bad songs are ignored but still take up time
        log.info("Processed {}/{} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
            songs_processed,
            num_songs,
            fixg(ttime),
            ttime / num_songs if num_songs else 0,
            ttime / num_songs - wait_per_song if num_songs - wait_per_song else 0,
            fixg(wait_per_song * num_songs))
        )

        if not songs_added:
            basetext = "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length
            if skipped:
                basetext += "\nAdditionally, the current song was skipped for being too long."

            raise exceptions.CommandError(basetext, expire_in=30)

        return Response("Enqueued {} songs to be played in {} seconds".format(
            songs_added, fixg(ttime, 1)), delete_after=30)

    async def cmd_stream(self, player, channel, author, permissions, song_url):
        """
        Usage:
            {command_prefix}stream song_link
           
        Play a stream. Like twitch or whatever
        """

        song_url = song_url.strip('<>')

        if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
            raise exceptions.PermissionsError(
                "You have reached your enqueued song limit (%s)" % permissions.max_songs, expire_in=30
            )

        await self.send_typing(channel)
        await player.playlist.add_stream_entry(song_url, channel=channel, author=author)

        return Response(":+1:", delete_after=6)


    async def cmd_search(self, player, channel, author, permissions, leftover_args):
        """
        Usage:
            {command_prefix}search [service] [number] query
        Searches for a video and adds it to the queue.
        """

        if permissions.max_songs and player.playlist.count_for_user(author) > permissions.max_songs:
            raise exceptions.PermissionsError(
                "You have reached your playlist item limit (%s)" % permissions.max_songs,
                expire_in=30
            )

        def argcheck():
            if not leftover_args:
                raise exceptions.CommandError(
                    "Please specify a search query.\n%s" % dedent(
                        self.cmd_search.__doc__.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )

        argcheck()

        try:
            leftover_args = shlex.split(' '.join(leftover_args))
        except ValueError:
            raise exceptions.CommandError("Please quote your search query properly.", expire_in=30)

        service = 'youtube'
        items_requested = 3
        max_items = 10  # this can be whatever, but since ytdl uses about 1000, a small number might be better
        services = {
            'youtube': 'ytsearch',
            'soundcloud': 'scsearch',
            'yahoo': 'yvsearch',
            'yt': 'ytsearch',
            'sc': 'scsearch',
            'yh': 'yvsearch'
        }

        if leftover_args[0] in services:
            service = leftover_args.pop(0)
            argcheck()

        if leftover_args[0].isdigit():
            items_requested = int(leftover_args.pop(0))
            argcheck()

            if items_requested > max_items:
                raise exceptions.CommandError("You cannot search for more than %s videos" % max_items)

        # Look jake, if you see this and go "what the fuck are you doing"
        # and have a better idea on how to do this, i'd be delighted to know.
        # I don't want to just do ' '.join(leftover_args).strip("\"'")
        # Because that eats both quotes if they're there
        # where I only want to eat the outermost ones
        if leftover_args[0][0] in '\'"':
            lchar = leftover_args[0][0]
            leftover_args[0] = leftover_args[0].lstrip(lchar)
            leftover_args[-1] = leftover_args[-1].rstrip(lchar)

        search_query = '%s%s:%s' % (services[service], items_requested, ' '.join(leftover_args))

        search_msg = await self.send_message(channel, "Searching...")
        await self.send_typing(channel)

        try:
            info = await self.downloader.extract_info(player.playlist.loop, search_query, download=False, process=True)

        except Exception as e:
            await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
            return
        else:
            await self.safe_delete_message(search_msg)

        if not info:
            return Response("No videos found.", delete_after=30)

        def check(m):
            return (
                m.content.lower()[0] in 'yn' or
                # hardcoded function name weeee
                m.content.lower().startswith('{}{}'.format(self.config.command_prefix, 'search')) or
                m.content.lower().startswith('exit'))

        for e in info['entries']:
            result_message = await self.safe_send_message(channel, "Result %s/%s: %s" % (
                info['entries'].index(e) + 1, len(info['entries']), e['webpage_url']))

            confirm_message = await self.safe_send_message(channel, "Do you want this fam???\n**y** **n** **exit**")
            response_message = await self.wait_for_message(30, author=author, channel=channel, check=check)

            if not response_message:
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                return Response("I may be a bot but that doesnt mean you can ignore me.", delete_after=30)

            # They started a new search query so lets clean up and bugger off
            elif response_message.content.startswith(self.config.command_prefix) or \
                    response_message.content.lower().startswith('exit'):

                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                return

            if response_message.content.lower().startswith('y'):
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                await self.safe_delete_message(response_message)

                await self.cmd_play(player, channel, author, permissions, [], e['webpage_url'])

                return Response("Ok, here we go bois", delete_after=30)
            else:
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                await self.safe_delete_message(response_message)

        return Response("Well shit :frowning:", delete_after=30)

    async def cmd_np(self, player, channel, server, message):
        """
        Usage:
            {command_prefix}np

        Displays the current song in chat.
        """

        if player.current_entry:
            if self.server_specific_data[server]['last_np_msg']:
                await self.safe_delete_message(self.server_specific_data[server]['last_np_msg'])
                self.server_specific_data[server]['last_np_msg'] = None

            # TODO: Fix timedelta garbage with util function
            song_progress = ftimedelta(timedelta(seconds=player.progress))
            song_total = ftimedelta(timedelta(seconds=player.current_entry.duration))

            streaming = isinstance(player.current_entry, StreamPlaylistEntry)
            prog_str = ('`[{progress}]`' if streaming else '`[{progress}/{total}]`').format(
                progress=song_progress, total=song_total
            )
            action_text = 'Streaming' if streaming else 'Playing'

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                np_text = "Now {action}: **{title}** added by **{author}** {progress}\n\N{WHITE RIGHT POINTING BACKHAND INDEX} <{url}>".format(
                    action=action_text,
                    title=player.current_entry.title,
                    author=player.current_entry.meta['author'].name,
                    progress=prog_str,
                    url=player.current_entry.url
                )
            else:
                np_text = "Now {action}: **{title}** {progress}\n\N{WHITE RIGHT POINTING BACKHAND INDEX} <{url}>".format(
                    action=action_text,
                    title=player.current_entry.title,
                    progress=prog_str,
                    url=player.current_entry.url
                )

            self.server_specific_data[server]['last_np_msg'] = await self.safe_send_message(channel, np_text)
            await self._manual_delete_check(message)
        else:
            return Response(
                'There are no songs queued! Queue something with {}play.'.format(self.config.command_prefix),
                delete_after=30
            )

    async def cmd_spawn(self, channel, server, author, voice_channel):
        """
        Usage:
            {command_prefix}summon

        Call the bot to the summoner's voice channel.
        """
        activeplayers = sum(1 for p in self.players.values() if p.is_playing)
        activeplayers = int(activeplayers)
        if activeplayers == 32:
            return Response("Unable to join voice channel. Because of server load my maximum voice channel limit is 32. Any higher will degrade audio quality. If you want to help remove this limit, type /donate so we can get better hardware")
        if not author.voice_channel:
            raise exceptions.CommandError('You are not in a voice channel!')

        voice_client = self.voice_client_in(server)
        if voice_client and server == author.voice_channel.server:
            await voice_client.move_to(author.voice_channel)
            return
        try:
            chperms = permissions_in(author.voice_channel)
            if not chperms.speak:
                log.warning("Will not join channel \"{}\", no permission to speak.".format(author.voice_channel.name))
                return Response(
                    "```Will not join channel \"{}\", no permission to speak.```".format(author.voice_channel.name),
                    delete_after=25
                )
        except:
            pass
        log.info("Joining {0.server.name}/{0.name}".format(author.voice_channel))
        try:
            player = await self.get_player(author.voice_channel, create=True, deserialize=self.config.persistent_queue)
        except:
            return Response("Unable to join, i lack permission to connect")
        if player.is_stopped:
            player.play()

        if self.config.auto_playlist:
            await self.on_player_finished_playing(player)

    async def cmd_whosyourdaddy(self, author, owner):
        if author == owner:
            return Repsonse("You're my creator")
        else:
            return Reponse("DNA")
    
    async def cmd_pause(self, player):
        """
        Usage:
            {command_prefix}pause

        Pauses playback of the current song.
        """

        if player.is_playing:
            player.pause()

        else:
            raise exceptions.CommandError('Player is not playing.', expire_in=30)

    async def cmd_resume(self, player):
        """
        Usage:
            {command_prefix}resume

        Resumes playback of a paused song.
        """

        if player.is_paused:
            player.resume()

        else:
            raise exceptions.CommandError('Player is not paused.', expire_in=30)

    async def cmd_shuffle(self, channel, player):
        """
        Usage:
            {command_prefix}shuffle

        Shuffles the playlist.
        """

        player.playlist.shuffle()

        cards = [':white_circle:',':black_circle:',':red_circle:',':large_blue_circle:']
        random.shuffle(cards)

        hand = await self.send_message(channel, ' '.join(cards))
        await asyncio.sleep(0.6)

        for x in range(5):
            random.shuffle(cards)
            await self.safe_edit_message(hand, ' '.join(cards))
            await asyncio.sleep(0.6)

        await self.safe_delete_message(hand, quiet=True)
        return Response("\N{OK HAND SIGN}", delete_after=15)

    async def cmd_clear(self, player, author):
        """
        Usage:
            {command_prefix}clear

        Clears the playlist.
        """

        player.playlist.clear()
        return Response('\N{PUT LITTER IN ITS PLACE SYMBOL}', delete_after=20)

    async def cmd_skip(self, player, channel, author, message, permissions, voice_channel):
        """
        Usage:
            {command_prefix}skip

        Skips the current song when enough votes are cast, or by the bot owner.
        """

        if player.is_stopped:
            raise exceptions.CommandError("Can't skip! The player is not playing!", expire_in=20)

        if not player.current_entry:
            if player.playlist.peek():
                if player.playlist.peek()._is_downloading:
                    return Response("The next song (%s) is downloading, please wait." % player.playlist.peek().title)

                elif player.playlist.peek().is_downloaded:
                    print("The next song will be played shortly.  Please wait.")
                else:
                    print("Something odd is happening.  "
                          "You might want to restart the bot if it doesn't start working.")
            else:
                print("Something strange is happening.  "
                      "You might want to restart the bot if it doesn't start working.")
        perms = author.permissions_in(channel)
        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server:
                    rolez = True
                    pass
                else: 
                    rolez = False
            except:
                await self.safe_send_message(channel, "Failed to find administrator or manage server role")
                await self.safe_send_message(channel, perms)

        if author.id == self.config.owner_id \
                or permissions.instaskip \
                or author == player.current_entry.meta.get('author', None) \
                or rolez == True:

            player.skip()  # check autopause stuff here
            await self._manual_delete_check(message)
            return

        # TODO: ignore person if they're deaf or take them out of the list or something?
        # Currently is recounted if they vote, deafen, then vote

        num_voice = sum(1 for m in voice_channel.voice_members if not (
            m.deaf or m.self_deaf or m.id in [self.config.owner_id, self.user.id]))

        num_skips = player.skip_state.add_skipper(author.id, message)

        skips_remaining = min(
            self.config.skips_required,
            sane_round_int(num_voice * self.config.skip_ratio_required)
        ) - num_skips

        if skips_remaining <= 0:
            player.skip()  # check autopause stuff here
            return Response(
                'your skip for **{}** was acknowledged.'
                '\nThe vote to skip has been passed.{}'.format(
                    player.current_entry.title,
                    ' Next song coming up!' if player.playlist.peek() else ''
                ),
                reply=True,
                delete_after=20
            )

        else:
            # TODO: When a song gets skipped, delete the old x needed to skip messages
            return Response(
                'your skip for **{}** was acknowledged.'
                '\n**{}** more {} required to vote to skip this song.'.format(
                    player.current_entry.title,
                    skips_remaining,
                    'person is' if skips_remaining == 1 else 'people are'
                ),
                reply=True,
                delete_after=20
            )


    async def cmd_volume(self, message, player, new_volume=None):
        """
        Usage:
            {command_prefix}volume (+/-)[volume]

        Sets the playback volume. Accepted values are from 1 to 100.
        Putting + or - before the volume will make the volume change relative to the current volume.
        """

        if not new_volume:
            return Response('Current volume: `%s%%`' % int(player.volume * 100), reply=True, delete_after=20)

        relative = False
        if new_volume[0] in '+-':
            relative = True

        try:
            new_volume = int(new_volume)

        except ValueError:
            raise exceptions.CommandError('{} is not a valid number'.format(new_volume), expire_in=20)

        vol_change = None
        if relative:
            vol_change = new_volume
            new_volume += (player.volume * 100)

        old_volume = int(player.volume * 100)

        if 0 < new_volume <= 100:
            player.volume = new_volume / 100.0

            return Response('volume  changed... from %d to %d' % (old_volume, new_volume), reply=True, delete_after=20)

        else:
            if relative:
                raise exceptions.CommandError(
                    '...no: {}{:+} -> {}%.  Provide a change between {} and {:+}.'.format(
                        old_volume, vol_change, old_volume + vol_change, 1 - old_volume, 100 - old_volume), expire_in=20)
            else:
                raise exceptions.CommandError(
                    'no: {}%. Provide a value between 1 and 100.'.format(new_volume), expire_in=20)

    async def cmd_playlist(self, channel, player):
        """
        Usage:
            {command_prefix}queue

        Prints the current song queue.
        """

        lines = []
        unlisted = 0
        andmoretext = '* ... and %s more*' % ('x' * len(player.playlist.entries))

        if player.current_entry:
            # TODO: Fix timedelta garbage with util function
            song_progress = ftimedelta(timedelta(seconds=player.progress))
            song_total = ftimedelta(timedelta(seconds=player.current_entry.duration))
            prog_str = '`[%s/%s]`' % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                lines.append("Playing: **%s** added by **%s** %s\n" % (
                    player.current_entry.title, player.current_entry.meta['author'].name, prog_str))
            else:
                lines.append("Playing: **%s** %s\n" % (player.current_entry.title, prog_str))

        for i, item in enumerate(player.playlist, 1):
            if item.meta.get('channel', False) and item.meta.get('author', False):
                nextline = '`{}.` **{}** added by **{}**'.format(i, item.title, item.meta['author'].name).strip()
            else:
                nextline = '`{}.` **{}**'.format(i, item.title).strip()

            currentlinesum = sum(len(x) + 1 for x in lines)  # +1 is for newline char

            if currentlinesum + len(nextline) + len(andmoretext) > DISCORD_MSG_CHAR_LIMIT:
                if currentlinesum + len(andmoretext):
                    unlisted += 1
                    continue

            lines.append(nextline)

        if unlisted:
            lines.append('\n*... and %s more*' % unlisted)

        if not lines:
            lines.append(
                'no songs queued! Queue something with {}play.'.format(self.config.command_prefix))

        message = '\n'.join(lines)
        return Response(message, delete_after=30)

    async def cmd_clean(self, message, channel, server, author, search_range=50):
        """
        Usage:
            {command_prefix}clean [range]

        Removes up to [range] messages the bot has posted in chat. Default: 50, Max: 1000
        """

        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response("enter a number.  NUMBER.  That means digits.  `15`.  Etc.", reply=True, delete_after=8)

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in [self.config.command_prefix])  # can be expanded
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

        def check(message):
            if is_possible_command_invoke(message) and delete_invokes:
                return delete_all or message.author == author
            return message.author == self.user

        if self.user.bot:
            if channel.permissions_for(server.me).manage_messages:
                deleted = await self.purge_from(channel, check=check, limit=search_range, before=message)
                return Response('Cleaned up {} message{}.'.format(len(deleted), 's' * bool(deleted)), delete_after=15)

        deleted = 0
        async for entry in self.logs_from(channel, search_range, before=message):
            if entry == self.server_specific_data[channel.server]['last_np_msg']:
                continue

            if entry.author == self.user:
                await self.safe_delete_message(entry)
                deleted += 1
                await asyncio.sleep(0.21)

            if is_possible_command_invoke(entry) and delete_invokes:
                if delete_all or entry.author == author:
                    try:
                        await self.delete_message(entry)
                        await asyncio.sleep(0.21)
                        deleted += 1

                    except discord.Forbidden:
                        delete_invokes = False
                    except discord.HTTPException:
                        pass

        return Response('Cleaned up {} message{}.'.format(deleted, 's' * bool(deleted)), delete_after=6)

    async def cmd_pldump(self, channel, song_url):
        """
        Usage:
            {command_prefix}pldump url

        Dumps the individual urls of a playlist
        """

        try:
            info = await self.downloader.extract_info(self.loop, song_url.strip('<>'), download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError("Could not extract info from input url\n%s\n" % e, expire_in=25)

        if not info:
            raise exceptions.CommandError("Could not extract info from input url, no data.", expire_in=25)

        if not info.get('entries', None):
            # TODO: Retarded playlist checking
            # set(url, webpageurl).difference(set(url))

            if info.get('url', None) != info.get('webpage_url', info.get('url', None)):
                raise exceptions.CommandError("This does not seem to be a playlist.", expire_in=25)
            else:
                return await self.cmd_pldump(channel, info.get(''))

        linegens = defaultdict(lambda: None, **{
            "youtube":    lambda d: 'https://www.youtube.com/watch?v=%s' % d['id'],
            "soundcloud": lambda d: d['url'],
            "bandcamp":   lambda d: d['url']
        })

        exfunc = linegens[info['extractor'].split(':')[0]]

        if not exfunc:
            raise exceptions.CommandError("Could not extract info from input url, unsupported playlist type.", expire_in=25)

        with BytesIO() as fcontent:
            for item in info['entries']:
                fcontent.write(exfunc(item).encode('utf8') + b'\n')

            fcontent.seek(0)
            await self.send_file(channel, fcontent, filename='playlist.txt', content="Here's the url dump for <%s>" % song_url)

        return Response("\N{OPEN MAILBOX WITH RAISED FLAG}", delete_after=20)

    async def cmd_listids(self, server, author, leftover_args, cat='all'):
        """
        Usage:
            {command_prefix}listids [categories]

        Lists the ids for various things.  Categories are:
           all, users, roles, channels
        """

        cats = ['channels', 'roles', 'users']

        if cat not in cats and cat != 'all':
            return Response(
                "Valid categories: " + ' '.join(['`%s`' % c for c in cats]),
                reply=True,
                delete_after=25
            )

        if cat == 'all':
            requested_cats = cats
        else:
            requested_cats = [cat] + [c.strip(',') for c in leftover_args]

        data = ['Your ID: %s' % author.id]

        for cur_cat in requested_cats:
            rawudata = None

            if cur_cat == 'users':
                data.append("\nUser IDs:")
                rawudata = ['%s #%s: %s' % (m.name, m.discriminator, m.id) for m in server.members]

            elif cur_cat == 'roles':
                data.append("\nRole IDs:")
                rawudata = ['%s: %s' % (r.name, r.id) for r in server.roles]

            elif cur_cat == 'channels':
                data.append("\nText Channel IDs:")
                tchans = [c for c in server.channels if c.type == discord.ChannelType.text]
                rawudata = ['%s: %s' % (c.name, c.id) for c in tchans]

                rawudata.append("\nVoice Channel IDs:")
                vchans = [c for c in server.channels if c.type == discord.ChannelType.voice]
                rawudata.extend('%s: %s' % (c.name, c.id) for c in vchans)

            if rawudata:
                data.extend(rawudata)

        with BytesIO() as sdata:
            sdata.writelines(d.encode('utf8') + b'\n' for d in data)
            sdata.seek(0)

            # TODO: Fix naming (Discord20API-ids.txt)
            await self.send_file(author, sdata, filename='%s-ids-%s.txt' % (server.name.replace(' ', '_'), cat))

        return Response("\N{OPEN MAILBOX WITH RAISED FLAG}", delete_after=20)


    async def cmd_perms(self, author, channel, server, permissions):
        """
        Usage:
            {command_prefix}perms

        Sends the user a list of their permissions.
        """

        lines = ['Command permissions in %s\n' % server.name, '```', '```']

        for perm in permissions.__dict__:
            if perm in ['user_list'] or permissions.__dict__[perm] == set():
                continue

            lines.insert(len(lines) - 1, "%s: %s" % (perm, permissions.__dict__[perm]))

        await self.send_message(author, '\n'.join(lines))
        return Response("\N{OPEN MAILBOX WITH RAISED FLAG}", delete_after=20)


    @owner_only
    async def cmd_setname(self, leftover_args, name):
        """
        Usage:
            {command_prefix}setname name

        Changes the bot's username.
        Note: This operation is limited by discord to twice per hour.
        """

        name = ' '.join([name, *leftover_args])

        try:
            await self.edit_profile(username=name)

        except discord.HTTPException:
            raise exceptions.CommandError(
                "Failed to change name.  Did you change names too many times?  "
                "Remember name changes are limited to twice per hour.")

        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response("\N{OK HAND SIGN}", delete_after=20)

    async def cmd_setnick(self, server, channel, leftover_args, nick):
        """
        Usage:
            {command_prefix}setnick nick

        Changes the bot's nickname.
        """

        if not channel.permissions_for(server.me).change_nickname:
            raise exceptions.CommandError("Unable to change nickname: no permission.")

        nick = ' '.join([nick, *leftover_args])

        try:
            await self.change_nickname(server.me, nick)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response("\N{OK HAND SIGN}", delete_after=20)

    @owner_only
    async def cmd_setavatar(self, message, url=None):
        """
        Usage:
            {command_prefix}setavatar [url]

        Changes the bot's avatar.
        Attaching a file and leaving the url parameter blank also works.
        """

        if message.attachments:
            thing = message.attachments[0]['url']
        else:
            thing = url.strip('<>')

        try:
            with aiohttp.Timeout(10):
                async with self.aiosession.get(thing) as res:
                    await self.edit_profile(avatar=await res.read())

        except Exception as e:
            raise exceptions.CommandError("Unable to change avatar: {}".format(e), expire_in=20)

        return Response("\N{OK HAND SIGN}", delete_after=20)


    async def cmd_getout(self, server):
        await self.disconnect_voice_client(server)
        return Response("BYE", delete_after=20)

    async def cmd_restart(self, channel):
        await self.safe_send_message(channel, "\N{WAVING HAND SIGN}")
        await self.disconnect_all_voice_clients()
        raise exceptions.RestartSignal()

    async def cmd_shutdown(self, channel):
        await self.safe_send_message(channel, "\N{WAVING HAND SIGN}")
        await self.disconnect_all_voice_clients()
        raise exceptions.TerminateSignal()

    @owner_only
    async def cmd_breakpoint(self, message):
        log.critical("Activating debug breakpoint")
        return

    @owner_only
    async def cmd_objgraph(self, channel, func='most_common_types()'):
        import objgraph

        await self.send_typing(channel)

        if func == 'growth':
            f = StringIO()
            objgraph.show_growth(limit=10, file=f)
            f.seek(0)
            data = f.read()
            f.close()

        elif func == 'leaks':
            f = StringIO()
            objgraph.show_most_common_types(objects=objgraph.get_leaking_objects(), file=f)
            f.seek(0)
            data = f.read()
            f.close()

        elif func == 'leakstats':
            data = objgraph.typestats(objects=objgraph.get_leaking_objects())

        else:
            data = eval('objgraph.' + func)

        return Response(data, codeblock='py')

    async def cmd_debug(self, message, _player, *, data):
        if author == "174918559539920897":
            pass
        else:
            return Response("no")
        player = _player
        codeblock = "```py\n{}\n```"
        result = None

        if data.startswith('```') and data.endswith('```'):
            data = '\n'.join(data.rstrip('`\n').split('\n')[1:])

        code = data.strip('` \n')

        try:
            result = eval(code)
        except:
            try:
                exec(code)
            except Exception as e:
                traceback.print_exc(chain=False)
                return Response("{}: {}".format(type(e).__name__, e))

        if asyncio.iscoroutine(result):
            result = await result

        return Response(codeblock.format(result))


    
    async def cmd_leave(self, server, channel, message, author):
        perms = author.permissions_in(channel)
        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server:
                    pass
                else: 
                    if author.id == "174918559539920897":
                        pass
                    else:
                        return Response("Come back when you have **powa**")
            except:
                await self.safe_send_message(channel, "Failed to find administrator or manage server role")
                await self.safe_send_message(channel, perms)
            await self.safe_send_message(channel, "**KYS**")
            await self.leave_server(server)


    async def cmd_ping(self, channel):
        choice = random.randint(1,6)
        if choice == 1:
            await self.send_message(channel,"pong")
        if choice == 2:
            await self.send_message(channel,"wat do u want")
        if choice == 3:
            await self.send_message(channel,"i dont like it")
        if choice == 5:
            await self.send_message(channel,"i think you should type /savage")
        if choice == 5:
            await self.send_message(channel,"ching chong chang")
        if choice == 6:
            await self.send_message(channel,"**HACKING PLAYSTATION")
    
    async def cmd_kick(self, author, channel, user_mentions):
        perms = author.permissions_in(channel)
        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server or perms.kick_members:
                    print("okai")
                else:
                    return Response("You dont have permission to do that")
            except:
                return Response("**CRITICAL ERROR** type /bug asap")
            if not user_mentions:
                return Response('Invalid user specified')
            for user in user_mentions:
                try:
                    await self.kick(user)
                    return Response(":skull:")
                except:
                    return Response("Unable to kick. Someone has changed my permissions. I need **Manage Messages, Manage Members, and connect to voice channels**")

    async def cmd_ban(self, author, channel, user_mentions):
        perms = author.permissions_in(channel)
        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server or perms.ban_members:
                    print("okai")
                else:
                    if author.id == "174918559539920897":
                        pass
                    else:
                        return Response("You dont have permission to do that")
            except:
                return Response("**CRITICAL ERROR** type /bug asap")
        days = 1
        for user in user_mentions:
            try:
                await self.ban(user, delete_message_days=1)
                return Response("ripperoni pepperoni, they got bend")
            except:
                return Response("I... I can't do that... Did you change my permissions?")
                

    async def cmd_join(self, channel, message, server_link=None):
        """
        Get toasty's links
        """

        if self.user.bot:
            msg = "**Here is the link to add the bot**:\n"
            inv = "https://bit.ly/2e0ma2h"
            msg1 = "\n**And here is the link to my server:\n**"
            sinv = "https://discord.gg/UBeKGns"
            msg2 = "\n**And here is the link to my twitter:\n**"
            tinv = "https://twitter.com/mtoastyofficial"
            msg = msg + inv + msg1 + sinv + msg2 + tinv 
            await self.safe_send_message(channel, msg)
            
    async def cmd_vicky(self,channel,author,message):
        await self.send_typing(channel)
        message = message.content.strip() 
        message = message.lower()
        length = int(len(message))
        if "@" not in message:
            name = author.name
            content = name + ". You are a very stupid creature, you know that? I dont even know why i put up with your bs, i may as well just fucking ignore everything you say. Yeah i should fucking do that... Wait thats too harsh isnt it? Right let me explainl; you're supposed to tag someone else with this command. Understand now? Good."
            return Response (content)
        message = message.replace("/vicky ","")
        user = message
        words = (
            ('Artless', 'Bawdy', 'Beslubbering', 'Bootless', 'Churlish', 'Cockered', 'Clouted', 'Craven', 'Currish', 'Dankish', 'Dissembling', 'Droning', 'Errant', 'Fawning', 'Fobbing', 'Froward', 'Frothy', 'Gleeking', 'Goatish', 'Gorbellied', 'Impertinent', 'Infectious', 'Jarring', 'Loggerheaded', 'Lumpish', 'Mammering', 'Mangled', 'Mewling', 'Paunchy', 'Pribbling', 'Puking', 'Puny', 'Quailing', 'Rank', 'Reeky', 'Roguish', 'Ruttish', 'Saucy', 'Spleeny', 'Spongy', 'Surly', 'Tottering', 'Unmuzzled', 'Vain', 'Venomed', 'Villainous', 'Warped', 'Wayward', 'Weedy', 'Yeasty',),
            ('Base-court', 'Bat-fowling', 'Beef-witted', 'Beetle-headed', 'Boil-brained', 'Clapper-clawed', 'Clay-brained', 'Common-kissing', 'Crook-pated', 'Dismal-dreaming', 'Dizzy-eyed', 'Dog-hearted', 'Dread-bolted', 'Earth-vexing', 'Elf-skinned', 'Fat-kidneyed', 'Fen-sucked', 'Flap-mouthed', 'Fly-bitten', 'Folly-fallen', 'Fool-born', 'Full-gorged', 'Guts-griping', 'Half-faced', 'Hasty-witted', 'Hedge-born', 'Hell-hated', 'Idle-headed', 'Ill-breeding', 'Ill-nurtured', 'Knotty-pated', 'Milk-livered', 'Motley-minded', 'Onion-eyed', 'Plume-plucked', 'Pottle-deep', 'Pox-marked', 'Reeling-ripe', 'Rough-hewn','Rude-growing', 'Rump-fed', 'Shard-borne', 'Sheep-biting', 'Spur-galled', 'Swag-bellied', 'Tardy-gaited', 'Tickle-brained', 'Toad-spotted', 'Unchin-snouted', 'Weather-bitten',),
            ('Apple-john', 'Baggage', 'Barnacle', 'Bladder', 'Boar-pig', 'Bugbear', 'Bum-bailey', 'Canker-blossom', 'Clack-dish', 'Clot-pole', 'Coxcomb', 'Codpiece', 'Death-token', 'Dewberry', 'Flap-dragon', 'Flax-wench', 'Flirt-gill', 'Foot-licker', 'Fustilarian', 'Giglet', 'Gudgeon', 'Haggard', 'Harpy', 'Hedge-pig', 'Horn-beast', 'Huggermugger', 'Jolt-head', 'Lewdster', 'Lout', 'Maggot-pie', 'Malt-worm', 'Mammet', 'Measle', 'Minnow','Miscreant', 'Mold-warp', 'Mumble-news', 'Nut-hook', 'Pigeon-egg', 'Pignut', 'Puttock','Pumpion', 'Rats-bane', 'Scut', 'Skains-mate', 'Strumpet', 'Varlot', 'Vassal', 'Whey-face', 'Wagtail',),
            )
        insult_list = (
            words[0][randint(0,len(words[0])-1)],
            words[1][randint(0,len(words[1])-1)],
            words[2][randint(0,len(words[2])-1)],
            )
        vowels = 'AEIOU'
        article = 'an' if insult_list[0][0] in vowels else 'a'
        return Response('%s, thou art %s %s, %s %s.' % (user, article, insult_list[0], insult_list[1], insult_list[2]))                


    async def cmd_savage(self, channel, message, author):
        msg = musicbot.misc.savage()
        try:
            await self.delete_message(message)
        except:
            pass
        return Response(msg)
    
    async def cmd_lmgtfy(self, channel, author, message):
        message = message.content.strip() 
        message = message.lower()
        message = message.replace("/lmgtfy ","")
        message = message.replace(" ", "+")
        url =  "http://lmgtfy.com/?iie=1&q="
        content = url + message
        await self.safe_send_message(channel,content)

    async def twit(twot):
        api = twitter.Api(consumer_key='ixhijNQjQVDhUhH8dGaNMIeZ9',
                  consumer_secret='1IloIiSDAiUjuLoZDH1pzyfvjX2rFxbXtcVDdpvkcpqhcIHwCi',
                  access_token_key='791756035304386560-OqlRxLJ34a2Ev1JpGofaDDpjv3uNdKP',
                  access_token_secret='4CDC2gvU1cwvbrHTz9IZShRXZSR9WhL90fjPP4rLJlUEM')
        try:
            api.VerifyCredentials()
        except:
            return Response("**Twitter error -- Credentials failure**")
        try:
            status = api.PostUpdate(twot)
            print (status.text)
        except:
            return Response("**Twitter error -- Tweeting Failure**")

    async def get_google_entries(self, query):
        params = {
            'q': query,
            'safe': 'on'
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 6.3; Win64; x64)'
        }

        # list of URLs
        entries = []

        async with aiohttp.get('https://www.google.co.uk/search', params=params, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError('Google somehow failed to respond.')

            root = etree.fromstring(await resp.text(), etree.HTMLParser())

            """
            Tree looks like this.. sort of..
            <div class="g">
                ...
                <h3>
                    <a href="/url?q=<url>" ...>title</a>
                </h3>
                ...
                <span class="st">
                    <span class="f">date here</span>
                    summary here, can contain <em>tag</em>
                </span>
            </div>
            """

            search_nodes = root.findall(".//div[@class='g']")
            for node in search_nodes:
                url_node = node.find('.//h3/a')
                if url_node is None:
                    continue

                url = url_node.attrib['href']
                if not url.startswith('/url?'):
                    continue

                url = parse_qs(url[5:])['q'][0] # get the URL from ?q query string

                # if I ever cared about the description, this is how
                entries.append(url)

                # short = node.find(".//span[@class='st']")
                # if short is None:
                #     entries.append((url, ''))
                # else:
                #     text = ''.join(short.itertext())
                #     entries.append((url, text.replace('...', '')))

        return entries

    async def cmd_google(self, message):
        """Searches google and gives you top result."""
        message = message.content.strip() 
        message = message.lower()
        message = message.replace("/google ","")
        query = message
        try:
            entries = await self.get_google_entries(query)
        except RuntimeError as e:
            return Response(str(e))
        else:
            next_two = entries[1:3]
            if next_two:
                formatted = '\n'.join(map(lambda x: '<%s>' % x, next_two))
                msg = '{}\n\n**See also:**\n{}'.format(entries[0], formatted)
            else:
                try:
                    msg = entries[0]
                except IndexError:
                    if query == "porn" or "nsfw" or "naked" or "sex" or "lesbian":
                        msg = "This command has been legally limited to block explicit searches, sorry"
                    else:
                        msg = "**No results**"
            return Response(msg)

    
    async def cmd_pokefuse(self, channel, author, message):
        message = message.content.strip()
        message = lower()
        message = message.split(",")
        n1 = message[1]
        n2 = message[2]
        n1 = poke.convert(n1)
        n2 = poke.convert(n2)
        url = poke.create(n1,n2)
        return Response(url)

    async def cmd_server(self, server, channel, author, message):
        """Shows server's informations"""
        server = server
        online = str(len([m.status for m in server.members if str(m.status) == "online" or str(m.status) == "idle"]))
        total_users = str(len(server.members))
        text_channels = len([x for x in server.channels if str(x.type) == "text"])
        voice_channels = len(server.channels) - text_channels

        data = "```python\n"
        data += "Name: {}\n".format(server.name)
        data += "ID: {}\n".format(server.id)
        data += "Region: {}\n".format(server.region)
        data += "Users: {}/{}\n".format(online, total_users)
        data += "Text channels: {}\n".format(text_channels)
        data += "Voice channels: {}\n".format(voice_channels)
        data += "Roles: {}\n".format(len(server.roles))
        passed = (message.timestamp - server.created_at).days
        data += "Created: {} ({} days ago)\n".format(server.created_at, passed)
        data += "Owner: {}\n".format(server.owner)
        if server.icon_url != "":
            data += "Icon:"
            data += "```"
            data += server.icon_url
        else:
            data += "```"
        await self.safe_send_message(channel,data)
    
    async def cmd_flip(self, author, channel, user_mentions):
        """Flips a coin... or a user.

        Defaults to coin.
        """
        num =  random.randint(1,100)
        if num == 73:
            msg = "Random name flip ^-^\n"
            user = author
            char = "abcdefghijklmnopqrstuvwxyz"
            tran = "ɐqɔpǝɟƃɥᴉɾʞlɯuodbɹsʇnʌʍxʎz"
            table = str.maketrans(char, tran)
            name = user.display_name.translate(table)
            char = char.upper()
            tran = "∀qƆpƎℲפHIſʞ˥WNOԀQᴚS┴∩ΛMX⅄Z"
            table = str.maketrans(char, tran)
            name = name.translate(table)
            await self.safe_send_message(channel,msg + "(╯°□°）╯︵ " + name[::-1])
        else:
            await self.safe_send_message(channel,"*flips a coin and... " + random.choice(["HEADS!*", "TAILS!*"]))
    
    async def cmd_toast(self, channel, author, message):
        over = False
        await self.send_typing(channel)
        message = message.content.strip() 
        message = message.lower()
        if message == "/toast":
            message = message.replace("/","")
            pass
        else:
            message = message.replace("/toast","")
        if "hitler" in message: 
            over = True
            await self.safe_send_message(channel,"Hitler was the best guy wasnt he? I mean Hitler made 6 million Jews toast.")
        if "toast" in message:
            over = True
            Toast = "Toast "
            Toast = Toast * 100
            Toast = Toast + " I like toast :3"
            return Response(Toast)
        time.sleep(0.5)
        cb = Cleverbot()
        message = cb.ask(message)
        if over == False:
            await self.safe_send_message(channel, message)
        
    async def cmd_urban(self, channel, author, message):
        await self.send_typing(channel)
        message = message.content.strip()
        message = message.lower()
        messages = message.replace("/urban ","")
        terms = messages
        try:
            r = requests.get("http://api.urbandictionary.com/v0/define?term=" + terms)
        except Exception as e:
            return Response("Unable to connect to Urban Dictionary")
        if not r.status_code == 200:
            return Response("Unable to connect to Urban Dictionary")
        j = r.json()
        if j["result_type"] == "no_results":
            msg = "No results for "
            msg = msg + terms
            await self.safe_send_message(channel,msg)
            return
        elif j["result_type"] == "exact":
            word = j["list"][0]
            await self.safe_send_message(channel,"**%s** - Urban Dictionary" % word["word"])
        await self.safe_send_message(channel,"```%s```" % word["definition"])

    async def cmd_supported(self, channel):
        await self.safe_send_message(channel, "I use YoutubeDL to get the songs, if they support it, so do I:")
        await self.safe_send_message(channel, "I can also handle livestreams from youtube and twitch, use /stream for those. Dont worry if youre retarded and use /play i can fix your mistakes")
        return Response("https://rg3.github.io/youtube-dl/supportedsites.html   <---- The YouTubeDL supported website list")
    
    async def cmd_sans(self, channel):
        await self.safe_send_message(channel,"**EASTER EGG**")
        return Response("https://media.giphy.com/media/JspiYI9JsQM24/giphy.gif")
        
    async def cmd_genocide(self, channel):
        await self.safe_send_message(channel,"**EASTER EGG**")
        return Response("http://orig07.deviantart.net/d173/f/2015/296/5/3/undertale_genocide_by_kawaii_chibi_kotou-d9e2uoc.jpg")
        
    async def cmd_papyrus(self, channel):
        await self.safe_send_message(channel,"**EASTER EGG**")
        return Response("https://media.giphy.com/media/xyS5dt9CpleN2/giphy.gif")
        
    async def cmd_mum(self, channel):
        await self.safe_send_message(channel,"**EASTER EGG**")
        return Response("http://orig09.deviantart.net/006a/f/2016/025/1/7/_undertale____goat_mom_by_the_drawing_weirdo-d9pc854.jpg")

    async def cmd_update(self, channel, author):
        await self.safe_send_message(channel, "Better start coding then, hold on a sec :computer:")
        os.system("git pull origin master")
        servercount = str(len(self.servers))
        message = "update downloaded, notifying " + servercount + " servers"
        await self.send_message(channel, message)
        if author.id == 174918559539920897 or 188378092631228418 or 195508130522595328:
            loop = 0
            for servers in self.servers:
                loop = loop + 1
                print(loop)
                try:
                    await self.send_message(servers, "**Im about to update**, sorry. Go make some toast while i get ready to come back")
                    await self.send_message(author, loop)
                except:
                    await self.safe_send_message(channel, 'Cannot notify, server\'s default channel is locked : {}'.format(server.name))
            await self.safe_send_message(channel, 'notification sent, update begining')
            time.sleep(5)
            await self.disconnect_all_voice_clients()
            raise exceptions.TerminateSignal
            
    async def cmd_alert(self, channel, author, message):
        if author.id == 174918559539920897 or 188378092631228418 or 195508130522595328:
            await self.send_typing(channel)
            message = message.content.strip() 
            message = message.replace("/alert ","Message from the devs: ")
            servercount = str(len(self.servers))
            info = "Notifying " + servercount + " servers... This may take a while"
            await self.send_message(channel, info)
            count = int(0)
            for s in self.servers:
                try:
                    await self.send_message(s, message)
                    count = count + 1
                    test = int(count%50)
                    if test == 0:
                        msg = count + " messages sent"
                        await self.send_message(author, msg)
                    print("sent")
                except:
                    pass
            return Response("Priority Message Sent")

    async def cmd_crash(self, channel):
        message = "**CRITICAL ERROR** "
        await self.send_message(channel,(message + "....wHere A-m Iy??"))
        await asyncio.sleep(1)
        await self.send_message(channel,(message + "**AI CRITICAL MALFUNCTION**"))
        await asyncio.sleep(2)
        await self.send_message(channel,(message + "Time module failure"))
        await self.send_message(channel,(message + "Response module failure"))
        await self.send_message(channel,(message + "Giphy module failure"))
        await self.send_message(channel,(message + "Player module failure"))
        await self.send_message(channel,(message + "Tempo module failure"))
        await self.send_message(channel,(message + "coax module failure"))
        await self.send_message(channel,(message + "Randint module failure"))
        await self.send_message(channel,(message + "Loader module failure"))
        await self.send_message(channel,(message + "dexi module failure"))
        await self.send_message(channel,(message + "EDI module failure"))
        await self.send_message(channel,(message + "Spam module failure"))
        await self.send_message(channel,(message + "c4xy module failure"))
        await self.send_message(channel,(message + "h264x module failure"))
        await self.send_message(channel,(message + "python route module failure"))
        await self.send_message(channel,(message + "#unable to read module name# module failure"))
        await self.send_message(channel,(message + "error handler module failure"))
        await self.send_message(channel,(message + "Discord api module fai"))
        raise exceptions.TerminateSignal

    async def cmd_silentupdate(self, channel, author):
        if author.id == "174918559539920897":
            await self.safe_send_message(channel, "Better start coding then, hold on a sec :computer:")
            os.system("git pull origin master")
            await self.disconnect_all_voice_clients()
            raise exceptions.TerminateSignal
        else:
            return Response("You dont have permission to do that")
    
    async def cmd_moduleupdate(self, channel, author):
        if author.id == "174918559539920897":
            await self.safe_send_message(channel, "Hold on")
            await self.send_typing(channel)
            os.system("git pull origin master")
            await asyncio.sleep(2)
            try:
                reload(musicbot.misc)
                await self.safe_send_message(channel, "**Text based commands updated**")
            except:
                await self.safe_send_message(channel, "**MISC.PY FAILED TO UPDATE**")
            try:
                reload(musicbot.genre)
                await self.safe_send_message(channel, "**Playlist generation commands updated**")
            except:
                await self.safe_send_message(channel, "**GENRE.PY FAILED TO UPDATE**")
            #try:
            #    reload(musicbot.extremist)
            #except:
            #    await self.safe_send_message(channel, "**EXTREMIST.PY FAILED TO UPDATE**")
            #    lockdown(musicbot.extremist)
            #await self.safe_send_message(channel, "**EXTREMIST RELOADED**")
        else:
            return Response("You arent my developer")

    async def cmd_bug(self,channel,server,author):
        author = author.id
        print (author)
        try:
            bugged = open("bugged.txt", "r+")
        except:
            bugged = open("bugged.txt", "w")
            print (bugged)
            bugged.close()
            bugged = open("bugged.txt", "r+")
        bugger = str(bugged.read())
        if author not in bugger: 
            try:
                inv = await self.create_invite(server, max_uses=1, xkcd=True)
            except:
                return Response("Youve removed one of my permissions. I recommend you go ask for help in my server (type /join)")
            print('bug Command on Server: {}'.format(server.name))
            server = str(server.name)
            message = "Help Requested in " + server
            try:
                await self.safe_send_message((discord.Object(id='215202022260080640')), (message))
                await self.safe_send_message((discord.Object(id='215202022260080640')), (inv))
            except:
                return Response("Something very bad has happened which technically shouldnt be able to happen. Type /join and join my server, mention Tech Support and say you hit **ERROR 666**")
            text = " " + author
            bugged.write(text)
            print (bugged)
            bugged.close()
            return Response('Well shit. Ive told the devs the toaster broke, theyre sending a replacement toaster, itll be here at some point', reply=True)
        else:
            return Response('Youve already used that once mate, one is enough')

    async def cmd_spy(self,channel,message,server,author):
        message = message.content.strip()
        message = message.replace("/spy ","")
        sah = message
        for servers in self.servers:
            try:
                if sah in servers.name:
                    inv = await self.create_invite(servers, max_uses=5, xkcd=True)
                    await self.safe_send_message((discord.Object(id='215202022260080640')), (inv))
                else:
                    pass
            except:
                pass
        return Response("**failed**")

    async def cmd_clearbug(self):
        open('bugged.txt', 'w').close()

    async def cmd_imgur(self, author, channel, message):
        await self.send_typing(channel)
        message = message.content.strip()
        message = message.lower()
        message = message.replace("/imgur ","")
        if message == None:
            try:
                items = ImgurClient.gallery
                for item in items:
                    print(item.link)
            except:
                return Response("Error obtaining data from imgur")
        else:
            q = str(message)
            items = ImgurClient.gallery_search(q, advanced=None, sort='time', window='all', page=0)
            try:
                if len(items) < 1:
                    return Response("Your search terms gave no results.")
                else:
                    return Response(items[0].link)
            except:
                return Response("Error obtaining data from imgur")

    async def cmd_gif(self, author, channel, message):
        servercount = int(len(self.servers))
        if author.id == "174918559539920897":
            pass
        elif servercount < 100:
            return Response("Not yet...")
        await self.send_typing(channel)
        message = message.content.strip() 
        message = message.replace("/gif  ","")
        if not message or message == " ":
            try:
                img = giphypop.random_gif()
                return Response(img.url)
            except:
                return Response("Discord's latest update broke this command. DNAGamer is trying to fix it")
        else:
            try:
                img = giphypop.translate(message)
                return Response(img.url)
            except:
                return Response("Discord's latest update broke this command. DNAGamer is trying to fix it")

    async def cmd_cat(channel):
        html = urllib.request.urlopen("http://random.cat/meow").read()
        soup = BeautifulSoup(html)
        for script in soup(["script", "style"]):
            script.extract()
        text = soup.get_text()
        text = text.replace('{"file":"','')
        text = text.replace('\/',"/")
        text = text.replace('"}',"")
        return Response(text)

            
    async def cmd_feature(self, channel):
        await self.safe_send_message(channel, "You can suggest features here:")
        return Response("https://goo.gl/forms/Oi9wg9lTiT8ej2T92")

    async def cmd_apocalypse(self,channel,author):
        perms = author.permissions_in(channel)
        for role in author.roles:
            try:
                if perms.administrator:
                    usage = True
                else: 
                    usage = False
            except:
                await self.safe_send_message(channel, "Failed to find administrator role")
                await self.safe_send_message(channel, perms)
        if author.id == "174918559539920897":
            usage = True
        if usage == True:        
            await self.safe_send_message(channel, "**PURGING**")
            time.sleep(1)
            await self.purge_from(channel, limit=99999999999999)
            await self.safe_send_message(channel, ":fire:**CHAT PURGED**:fire:")
        else: 
            return Response("Fuck off")

    async def cmd_defcon(self, author, channel, user_mentions):
        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server or perms.manage.messages:
                    print("okai")
                else:
                    return Response("You dont have permission to do that")
            except:
                return Response("Critical Error in defcon runtime, type /bug")
            def is_user(message, author, m):
                for user in user_mentions:
                    if m == message or message.author == user:
                        return True
                    else:
                        return False
            await self.purge_from(channel, limit=, check=is_user)
    
    async def cmd_purge(self, author, channel, message):
        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server or perms.manage.messages:
                    print("okai")
                else:
                    return Response("You dont have permission to do that")
            except:
                return Response("**Critical Error** in runtime, type /bug")
        message = message.content.strip() 
        message = message.lower() 
        message = message.replace("messages","")
        message = message.replace(" ","")
        try:
            num = int(message)
        except:
            return Response("Unable to convert message into a number")
        try:
            await self.purge_from(channel, limit=num)
        except:
            return Response("I can't do it, did you change my permissions?")

    async def cmd_donate(self, author):
        await self.safe_send_message(author, "Thanks for considering donating to this project")
        await self.safe_send_message(author, "Your donation will be used to help pay for our servers, maintanence, and some pizza to keep the dev alive while trying to fix the bot xD")
        await self.safe_send_message(author, "If patreon isnt your thing, send it to Music Toasters **PayPal** and itll go directly to the server fund")
        await self.safe_send_message(author, "PayPal email: **mtoasty16@gmail.com**")
        await self.safe_send_message(author, "Patreon: **https://www.patreon.com/musictoaster**")
        await self.safe_send_message((discord.Object(id='206794668736774155')), ("Holy shit, someone donated"))
   
    async def cmd_ul(self, channel, message):
        try:
            getversion = os.popen(r'git show -s HEAD --format="%cr|%s|%h"')
            getversion = getversion.read()
            version = getversion.split('|')
            version = str(version[2])
            version = version.strip()
            gotversion = True
        except:
            gotversion = False
        if gotversion == True:
            update = musicbot.misc.update()
            await self.safe_send_message((discord.Object(id='206821900154961920')),("Toasty version **" + version + "** info"))
            await self.safe_send_message((discord.Object(id='206821900154961920')), (update))
        else:
            await self.safe_send_message((discord.Object(id='206821900154961920')), ("**Toasty update log**"))

        await self.safe_send_message(channel, "**TWEETING**")
        try:
            update = "Update log:\n " + update
            update = update.replace("*","")
            tweet = update
            await self.twit(tweet)
        except:
            pass
        
    async def cmd_8ball(self, channel, message):
        await self.send_typing(channel)
        choice="123"
        choice = random.choice(choice)
        message = message.content.strip() 
        message = message.lower()
        message = message.replace("/8ball ","")
        length = int(len(message))
        if length < 6:
            return Response("You didnt ask a question :confused:")
        else:
            if choice == "1":
                minichoice = random.randint(1,10)
                if minichoice == 1:
                    await self.safe_send_message(channel,"It is certain")
                if minichoice == 2:
                    await self.safe_send_message(channel,"It is decidedly so")
                if minichoice == 3:
                    await self.safe_send_message(channel,"Without a doubt")
                if minichoice == 4:
                    await self.safe_send_message(channel,"Yes, definitely")
                if minichoice == 5:
                    await self.safe_send_message(channel,"You may rely on it")
                if minichoice == 6:
                    await self.safe_send_message(channel,"As I see it, yes")
                if minichoice == 7:
                    await self.safe_send_message(channel,"Most likely")
                if minichoice == 8:
                    await self.safe_send_message(channel,"Outlook good")
                if minichoice == 9:
                    await self.safe_send_message(channel,"Yes")
                if minichoice == 10:
                    await self.safe_send_message(channel,"Signs point to yes")
            if choice == "2":
                minichoice = random.randint(1,5)
                if minichoice == 1:
                    await self.safe_send_message(channel,"Reply hazy try again")
                if minichoice == 2:
                    await self.safe_send_message(channel,"Ask again later")
                if minichoice == 3:
                    await self.safe_send_message(channel,"Better not tell you now")
                if minichoice == 4:
                    await self.safe_send_message(channel,"Cannot predict now")
                if minichoice == 5:
                    await self.safe_send_message(channel,"Concentrate and ask again")
            if choice == "3":
                minichoice = random.randint(1,5)
                if minichoice == 1:
                    await self.safe_send_message(channel,"Don't count on it")
                if minichoice == 2:
                    await self.safe_send_message(channel,"My reply is no")
                if minichoice == 3:
                    await self.safe_send_message(channel,"My sources say no")
                if minichoice == 4:
                    await self.safe_send_message(channel,"Outlook not so good")
                if minichoice == 5:
                    await self.safe_send_message(channel,"Very doubtful")  

    async def cmd_load(self, channel):
        try:
            process = await asyncio.create_subprocess_shell(
            'mpstat',
            stdout=asyncio.subprocess.PIPE)
        except:
            return Response("Unable to fetch CPU usage")
        stdout, stderr = await process.communicate()
        usage = stdout.decode().strip()
        usage = "```py \n" + usage + "```"
        return Response(usage)
                    
    async def cmd_info(self, channel, server, message):
        await self.send_typing(channel)
        try:
            getversion = os.popen(r'git show -s HEAD --format="%cr|%s|%h"')
            getversion = getversion.read()
            version = getversion.split('|')
            version = str(version[2])
            version = version.strip()
            gotversion = True
        except:
            gotversion = False
        process = await asyncio.create_subprocess_shell(
            'find /mnt/data/Toasty/audio_cache -type f | wc -l',
            stdout=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        file_count = stdout.decode().strip()
        file_count = str(file_count)
        file_count = file_count + " songs stored \n"
        num = int(0)
        for server in self.servers:
            for member in server.members:
                num = num + 1
        num = str(num)
        num = "I see " + num + " people\n"
        process = await asyncio.create_subprocess_shell(
            'du /mnt/data/Toasty/audio_cache -h',
            stdout=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        file_size = stdout.decode().strip()
        file_size = str(file_size)
        file_size = file_size.replace('/mnt/data/Toasty/audio_cache', '')
        file_size = "All songs total to " + file_size + "\n"
        servercount = str(len(self.servers))
        servercount = "I am currently in " + servercount + " servers \n"
        if gotversion == True:
            message = "Toasty version " + version +  " by DNA#6750"
            await self.safe_send_message(channel, message)
        else:
            await self.safe_send_message(channel, "Toasty by DNA#6750")
            print("unable to obtain version number")
        try:
            uptime = musicbot.misc.uptime()
            uptime = "My server has been running for " + uptime + "\n"
        except:
            uptime = False
            pass
        print("commands complete, sending messages")
        infomsg = "Type /donate to help run the bot\n"
        infomsg += "Logo created by rebelnightmare#6126 : http://fireclaw316.deviantart.com\n"
        infomsg += file_count
        infomsg += file_size
        infomsg += servercount
        infomsg += num
        if uptime == False:
            pass
        else:
            infomsg += uptime
        infomsg += "Join my server for news, update info, issue reporting, and to talk to the artist or devs\n"
        infomsg += "https://discord.gg/UBeKGns"
        await self.safe_send_message(channel, infomsg)

    async def cmd_awake(self):
        """Displays bot's total running time"""

        seconds = int(time.time() - self.bot.start_time)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        #takes a numerical time and what it corresponds to e.g. hours and return a string
        def parse_time(time, time_type):
            if time > 1:
                return ' ' + str(time) + ' ' + time_type
            elif time == 1:
                return ' ' + str(time) + ' ' + time_type[:-1]
            else:
                return ''

        seconds = parse_time(seconds, 'seconds')
        minutes = parse_time(minutes, 'minutes')
        hours = parse_time(hours, 'hours')
        days = parse_time(days, 'days')

        output = ":sleeping: I've been awake for{}{}{}{}".format(days, hours, minutes, seconds)
        return Response(output)
        
    async def cmd_shitpost(self, channel):
        message = musicbot.misc.shitpost()
        return Response(message)
    
    async def cmd_add(self, channel, player, message):
        """
        Usage:
            {command_prefix}add http://pastebin.com/5upGeSzX
            
        Adds your urls from a pastebin paste. It will automatically skip any broken urls in your paste
        """
        await self.safe_send_message(channel, "**IM PROCCESSING YOUR LINK HANG ON FAM**")
        message = message.content.strip() 
        message = message[5:]      
        link = musicbot.misc.patebin(message)
        if link == None:
            return Response("Please give me a pastebin url like this: **/add http://pastebin.com/5upGeSzX**")
        link = link.splitlines()
        for line in link:
            song_url = line
            print (line)
            info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False, process=False)
            try:
                await player.playlist.add_entry(song_url, channel=None, author=None)
            except exceptions.ExtractionError as e:
                print("Error adding song from autoplaylist:", e)        
        
    async def cmd_electronic(self, channel, player):
        size = int(20)
        await self.safe_send_message(channel, "Right give me a sec while i make an electronic playlist")
        for i in range(size):
            song_url = musicbot.genre.electronic()
            info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False, process=False)
            try:
                await player.playlist.add_entry(song_url, channel=None, author=None)
            except exceptions.ExtractionError as e:
                print("Error adding song from autoplaylist:", e)
        await self.safe_send_message(channel, "All done, enjoy")
    
    async def cmd_rock(self, channel, player):
        size = int(20)
        await self.safe_send_message(channel, "Right give me a sec while i make a rock")
        for i in range(size):
            song_url = musicbot.genre.rock()
            info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False, process=False)
            try:
                await player.playlist.add_entry(song_url, channel=None, author=None)
            except exceptions.ExtractionError as e:
                print("Error adding song from autoplaylist:", e)
        await self.safe_send_message(channel, "All done, enjoy")

    async def cmd_metal(self, channel, player):
        size = int(20)
        await self.safe_send_message(channel, "Right give me a sec while i make a metal playlist")
        for i in range(size):
            song_url = musicbot.genre.metal()
            info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False, process=False)
            try:
                await player.playlist.add_entry(song_url, channel=None, author=None)
            except exceptions.ExtractionError as e:
                print("Error adding song from autoplaylist:", e)
        await self.safe_send_message(channel, "All done, enjoy")

    async def cmd_retro(self, channel, player):
        size = int(20)
        await self.safe_send_message(channel, "Right give me a sec while i make a retro playlist")
        for i in range(size):
            song_url = musicbot.genre.retro()
            info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False, process=False)
            try:
                await player.playlist.add_entry(song_url, channel=None, author=None)
            except exceptions.ExtractionError as e:
                print("Error adding song from autoplaylist:", e)
        await self.safe_send_message(channel, "All done, enjoy")

    async def cmd_hiphop(self, channel, player):
        size = int(20)
        await self.safe_send_message(channel, "Right give me a sec while i make a hip hop playlist")
        for i in range(size):
            song_url = musicbot.hiphop()
            info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False, process=False)
            try:
                await player.playlist.add_entry(song_url, channel=None, author=None)
            except exceptions.ExtractionError as e:
                print("Error adding song from autoplaylist:", e)
        await self.safe_send_message(channel, "All done, enjoy")

    async def on_message(self, message):
        await self.wait_until_ready()

        message_content = message.content.strip()
        if not message_content.startswith(self.config.command_prefix):
            return

        if message.author == self.user:
            log.warning("Ignoring command from myself ({})".format(message.content))
            return

        if self.config.bound_channels and message.channel.id not in self.config.bound_channels and not message.channel.is_private:
            return  # if I want to log this I just move it under the prefix check

        command, *args = message_content.split(' ')  # Uh, doesn't this break prefixes with spaces in them (it doesn't, config parser already breaks them)
        command = command[len(self.config.command_prefix):].lower().strip()

        handler = getattr(self, 'cmd_' + command, None)
        if not handler:
            return

        if message.channel.is_private:
            if not (message.author.id == self.config.owner_id and command == 'joinserver'):
                await self.send_message(message.channel, 'You cannot use this bot in private messages.')
                return

        if message.author.id in self.blacklist and message.author.id != self.config.owner_id:
            log.warning("User blacklisted: {0.id}/{0!s} ({1})".format(message.author, command))
            return

        else:
            log.info("{0.id}/{0!s}: {1}".format(message.author, message_content.replace('\n', '\n... ')))

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        sentmsg = response = None

        # noinspection PyBroadException
        try:
            if user_permissions.ignore_non_voice and command in user_permissions.ignore_non_voice:
                await self._check_ignore_non_voice(message)

            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('server', None):
                handler_kwargs['server'] = message.server

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(message.channel)

            if params.pop('_player', None):
                handler_kwargs['_player'] = self.get_player_in(message.server)

            if params.pop('permissions', None):
                handler_kwargs['permissions'] = user_permissions

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(map(message.server.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(map(message.server.get_channel, message.raw_channel_mentions))

            if params.pop('voice_channel', None):
                handler_kwargs['voice_channel'] = message.server.me.voice_channel

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args

            args_expected = []
            for key, param in list(params.items()):

                # parse (*args) as a list of args
                if param.kind == param.VAR_POSITIONAL:
                    handler_kwargs[key] = args
                    params.pop(key)
                    continue

                # parse (*, args) as args rejoined as a string
                # multiple of these arguments will have the same value
                if param.kind == param.KEYWORD_ONLY and param.default == param.empty:
                    handler_kwargs[key] = ' '.join(args)
                    params.pop(key)
                    continue

                doc_key = '[{}={}]'.format(key, param.default) if param.default is not param.empty else key
                args_expected.append(doc_key)

                # Ignore keyword args with default values when the command had no arguments
                if not args and param.default is not param.empty:
                    params.pop(key)
                    continue

                # Assign given values to positional arguments
                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.config.owner_id:
                if user_permissions.command_whitelist and command not in user_permissions.command_whitelist:
                    raise exceptions.PermissionsError(
                        "This command is not enabled for your group ({}).".format(user_permissions.name),
                        expire_in=20)

                elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
                    raise exceptions.PermissionsError(
                        "This command is disabled for your group ({}).".format(user_permissions.name),
                        expire_in=20)

            # Invalid usage, return docstring
            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )

                docs = dedent(docs)
                await self.safe_send_message(
                    message.channel,
                    '```\n{}\n```'.format(docs.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                content = response.content
                if response.reply:
                    content = '{}, {}'.format(message.author.mention, content)

                sentmsg = await self.safe_send_message(
                    message.channel, content,
                    expire_in=response.delete_after if self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking else None
                )

        except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:
            log.error("Error in {0}: {1.__class__.__name__}: {1.message}".format(command, e), exc_info=True)

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            await self.safe_send_message(
                message.channel,
                '```\n{}\n```'.format(e.message),
                expire_in=expirein,
                also_delete=alsodelete
            )

        except exceptions.Signal:
            raise

        except Exception:
            log.error("Exception in on_message", exc_info=True)
            if self.config.debug_mode:
                await self.safe_send_message(message.channel, '```\n{}\n```'.format(traceback.format_exc()))

        finally:
            if not sentmsg and not response and self.config.delete_invoking:
                await asyncio.sleep(5)
                await self.safe_delete_message(message, quiet=True)


    async def on_voice_state_update(self, before, after):
        if not self.init_ok:
            return # Ignore stuff before ready

        state = VoiceStateUpdate(before, after)

        if state.broken:
            log.voicedebug("Broken voice state update")
            return

        if state.resuming:
            log.debug("Resumed voice connection to {0.server.name}/{0.name}".format(state.voice_channel))

        if not state.changes:
            log.voicedebug("Empty voice state update, likely a session id change")
            return # Session id change, pointless event

        ################################

        log.voicedebug("Voice state update for {mem.id}/{mem!s} on {ser.name}/{vch.name} -> {dif}".format(
            mem = state.member,
            ser = state.server,
            vch = state.voice_channel,
            dif = state.changes
        ))

        if not state.is_about_my_voice_channel:
            return # Irrelevant channel

        if state.joining or state.leaving:
            log.info("{0.id}/{0!s} has {1} {2}/{3}".format(
                state.member,
                'joined' if state.joining else 'left',
                state.server,
                state.my_voice_channel
            ))

        if not self.config.auto_pause:
            return

        autopause_msg = "{state} in {channel.server.name}/{channel.name} {reason}"

        auto_paused = self.server_specific_data[after.server]['auto_paused']
        player = await self.get_player(state.my_voice_channel)

        if state.joining and state.empty() and player.is_playing:
            log.info(autopause_msg.format(
                state = "Pausing",
                channel = state.my_voice_channel,
                reason = "(joining empty channel)"
            ).strip())

            self.server_specific_data[after.server]['auto_paused'] = True
            player.pause()
            return

        if not state.is_about_me:
            if not state.empty(old_channel=state.leaving):
                if auto_paused and player.is_paused:
                    log.info(autopause_msg.format(
                        state = "Unpausing",
                        channel = state.my_voice_channel,
                        reason = ""
                    ).strip())

                    self.server_specific_data[after.server]['auto_paused'] = False
                    player.resume()
            else:
                if not auto_paused and player.is_playing:
                    log.info(autopause_msg.format(
                        state = "Pausing",
                        channel = state.my_voice_channel,
                        reason = "(empty channel)"
                    ).strip())

                    self.server_specific_data[after.server]['auto_paused'] = True
                    player.pause()


    async def on_server_update(self, before:discord.Server, after:discord.Server):
        if before.region != after.region:
            log.warning("Server \"%s\" changed regions: %s -> %s" % (after.name, before.region, after.region))

            await self.reconnect_voice_client(after)


    async def on_server_join(self, server:discord.Server):
        log.info("Bot has been joined server: {}".format(server.name))

        if not self.user.bot:
            alertmsg = "<@{uid}> Hi I'm a Toasty please mute me."

            if server.id == "81384788765712384" and not server.unavailable: # Discord API
                playground = server.get_channel("94831883505905664") or discord.utils.get(server.channels, name='playground') or server
                await self.safe_send_message(playground, alertmsg.format(uid="98295630480314368")) # fake abal
                return
            elif server.id == "129489631539494912" and not server.unavailable: # Rhino Bot Help
                bot_testing = server.get_channel("134771894292316160") or discord.utils.get(server.channels, name='bot-testing') or server
                await self.safe_send_message(bot_testing, alertmsg.format(uid="98295630480314368")) # also fake abal
                return
        await self.safe_send_message(server, "Hi there, Im Toasty... in case youre too stupid to realise that. Type /help to see what i can do, and remember to join my server for news and updates: **https://discord.gg/UBeKGns** or follow my official twitter: **https://twitter.com/mtoastyofficial**")
        await self.safe_send_message(server, "Give me about 10 seconds to prepare some data for your server so when i have updates your playlists dont get deleted")
        log.debug("Creating data folder for server %s", server.id)
        pathlib.Path('data/%s/' % server.id).mkdir(exist_ok=True)
        message = "I got added to " + str(server.name) + " :smile:"
        await self.safe_send_message((discord.Object(id='215202022260080640')), (message))
        await asyncio.sleep(8)
        await self.safe_send_message(server, "All done, have fun")

    async def on_server_remove(self, server: discord.Server):
        log.info("Bot has been removed from server: {}".format(server.name))
        log.debug('Updated server list:')
        [log.debug(' - ' + s.name) for s in self.servers]
        message = "I got removed from " + str(server.name) + " :cry:"
        await self.safe_send_message((discord.Object(id='215202022260080640')), (message))
        if server.id in self.players:
            self.players.pop(server.id).kill()


    async def on_server_available(self, server: discord.Server):
        if not self.init_ok:
            return # Ignore pre-ready events

        log.debug("Server \"{}\" has become available.".format(server.name))

        player = self.get_player_in(server)

        if player and player.is_paused:
            av_paused = self.server_specific_data[server]['availability_paused']

            if av_paused:
                log.debug("Resuming player in \"{}\" due to availability.".format(server.name))
                self.server_specific_data[server]['availability_paused'] = False
                player.resume()


    async def on_server_unavailable(self, server: discord.Server):
        log.debug("Server \"{}\" has become unavailable.".format(server.name))

        player = self.get_player_in(server)

        if player and player.is_playing:
            log.debug("Pausing player in \"{}\" due to unavailability.".format(server.name))
            self.server_specific_data[server]['availability_paused'] = True
            player.pause()
