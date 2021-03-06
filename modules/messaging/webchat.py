# Copyright (C) 2016   CzT/Vladislav Ivanov
import html
import queue
import datetime
import json

import jinja2
import os
import socket
import threading

import cherrypy
from cherrypy.lib.static import serve_file
from scss import Compiler
from scss.namespace import Namespace
from scss.types import Color, Boolean, String, Number, List
from ws4py.server.cherrypyserver import WebSocketPlugin, WebSocketTool
from ws4py.websocket import WebSocket

from modules.helper.functions import get_themes
from modules.helper.html_template import HTML_TEMPLATE
from modules.helper.message import TextMessage, CommandMessage, SystemMessage, RemoveMessageByIDs, \
    get_system_message_types, RemoveMessageByUsers
from modules.helper.module import MessagingModule
from modules.helper.parser import save_settings, convert_to_dict, update
from modules.helper.system import THREADS, CONF_FOLDER, EMOTE_FORMAT, HTTP_FOLDER, TRANSLATIONS, TRANSLATION_FILETYPE, \
    SPLIT_TRANSLATION
from modules.interface.types import *

GUI_CHAT = 'gui_chat'
BROWSER_CHAT = 'server_chat'
CHAT_TYPES = [GUI_CHAT, BROWSER_CHAT]

logging.getLogger('ws4py').setLevel(logging.ERROR)
DEFAULT_STYLE = 'default'
DEFAULT_GUI_STYLE = 'default'
HISTORY_SIZE = 50
s_queue = queue.Queue()
log = logging.getLogger('webchat')
REMOVED_TRIGGER = '%%REMOVED%%'

SETTINGS_FILE = 'settings.json'
SETTINGS_GUI_FILE = 'settings_ui.json'
SETTINGS_FORMAT_FILE = 'settings_format.json'

WS_THREADS = THREADS + 3

CONF_DICT = LCPanel()
CONF_DICT['server'] = LCStaticBox()
CONF_DICT['server']['host'] = LCDropdown('127.0.0.1', ['127.0.0.1', '0.0.0.0'])
CONF_DICT['server']['port'] = LCText('8080')

for g_ui_type in CHAT_TYPES:
    CONF_DICT[g_ui_type] = LCPanel()
    CONF_DICT[g_ui_type]['style'] = LCChooseSingle(DEFAULT_GUI_STYLE, available_list=get_themes(), empty_label=True)
    CONF_DICT[g_ui_type]['style_settings'] = LCStaticBox()
    CONF_DICT[g_ui_type]['style_settings']['show_system_msg'] = LCChooseMultiple(
        available_list=get_system_message_types(), addable=False, value=get_system_message_types())
    CONF_DICT[g_ui_type]['style_settings']['show_history'] = LCBool(True)

MANDATORY_KEYS = ['show_system_msg', 'show_history']

TYPE_DICT = {
    TextMessage: 'message',
    CommandMessage: 'command'
}


def merge_lc_dicts(dst, src):
    for k, v in src.items():
        if isinstance(v, LCDict):
            dst[k] = update(dst[k], v)
        else:
            dst[k] = src[k]


def process_emotes(emotes):
    return [{'id': EMOTE_FORMAT.format(emote.id), 'url': emote.url} for emote in emotes]


def process_badges(badges):
    return [{'badge': badge.id, 'url': badge.url} for badge in badges]


def process_platform(platform):
    return {'id': platform.id, 'icon': platform.icon}


def prepare_message(message, style_settings):
    json_message = message.json()

    payload = json_message['payload']

    if 'text' in payload and payload['text'] == REMOVED_TRIGGER:
        payload['text'] = str(style_settings.get('remove_text'))

    if json_message['type'] == 'command':
        if payload['command'].startswith('remove'):
            if not style_settings['keys']['replace_message']:
                payload['text'] = html.escape(style_settings['keys']['replace_text'].value)
                payload['command'] = payload['command'].replace('remove', 'replace')
        return json_message

    if 'levels' in payload:
        if '?' not in payload['levels']['url']:
            payload['levels']['url'] = f"{payload['levels']['url']}?{style_settings['style_name']}"

    payload['emotes'] = process_emotes(payload.get('emotes', {}))
    payload['badges'] = process_badges(payload.get('badges', {}))
    payload['platform'] = process_platform(payload.get('platform', {}))

    payload['text'] = html.escape(payload['text'])
    return json_message


def add_to_history(message):
    cherrypy.engine.publish('add-history', message)


def process_command(message):
    cherrypy.engine.publish('process-command', message.command, message)


class MessagingThread(threading.Thread):
    def __init__(self, settings):
        super(self.__class__, self).__init__()
        self.daemon = True
        self.settings = settings
        self.running = True

    def system_message_types(self, chat_type):
        return self.settings[chat_type]['keys']['show_system_msg'].value

    def run(self):
        while self.running:
            message = s_queue.get()

            if isinstance(message, dict):
                raise Exception(f"Got dict message {message}")

            if isinstance(message, TextMessage):
                add_to_history(message)

            if isinstance(message, CommandMessage):
                process_command(message)

            if not message.only_gui:
                self.send_message(message, BROWSER_CHAT)
            self.send_message(message, GUI_CHAT)
        log.info("Messaging thread stopping")

    def stop(self):
        self.running = False

    def send_message(self, message, chat_type):
        if isinstance(message, SystemMessage) and message.category not in self.system_message_types(chat_type):
            return

        send_message = prepare_message(message, self.settings[chat_type])
        c_req = cherrypy.engine.publish('get-clients', chat_type)
        if not c_req:
            return

        ws_list = c_req[0]
        for ws in ws_list:
            try:
                ws.send(json.dumps(send_message))
            except Exception as exc:
                log.exception(exc)
                log.info(send_message)


class FireFirstMessages(threading.Thread):
    def __init__(self, ws, history, settings):
        super(self.__class__, self).__init__()
        self.daemon = True
        self.ws = ws  # type: WebChatSocketServer
        self.history = history
        self.settings = settings

    def run(self):
        show_system_msg = self.settings['keys'].get('show_system_msg', get_system_message_types())
        if self.ws.stream:
            for item in self.history:
                if isinstance(item, SystemMessage) and item.category not in show_system_msg:
                    continue
                timedelta = datetime.datetime.now() - item.timestamp
                timer = self.settings['keys'].get('clear_timer', LCSpin(-1)).simple()
                if timer > 0:
                    if timedelta > datetime.timedelta(seconds=timer):
                        continue

                message = prepare_message(item, self.settings)
                self.ws.send(json.dumps(message))


class WebChatSocketServer(WebSocket):
    def __init__(self, sock, protocols=None, extensions=None, environ=None, heartbeat_freq=None):
        WebSocket.__init__(self, sock)
        self.daemon = True
        self.clients = []
        self.settings = cherrypy.engine.publish('get-settings', 'server_chat')[0]
        self.type = 'server_chat'

    def opened(self):
        cherrypy.engine.publish('add-client', self.peer_address, self)
        if self.settings['keys'].get('show_history'):
            timer = threading.Timer(0.3, self.fire_history)
            timer.start()

    def closed(self, code, reason=None):
        cherrypy.engine.publish('del-client', self.peer_address, self)

    def fire_history(self):
        send_history = FireFirstMessages(self, cherrypy.engine.publish('get-history')[0],
                                         self.settings)
        send_history.start()


class WebChatGUISocketServer(WebChatSocketServer):
    def __init__(self, sock, protocols=None, extensions=None, environ=None, heartbeat_freq=None):
        WebSocket.__init__(self, sock)
        self.clients = []
        self.settings = cherrypy.engine.publish('get-settings', 'gui_chat')[0]
        self.type = 'gui_chat'


class WebChatPlugin(WebSocketPlugin):
    def __init__(self, bus, settings):
        WebSocketPlugin.__init__(self, bus)
        self.daemon = True
        self.clients = []
        self.style_settings = settings
        self.history = []
        self.history_size = HISTORY_SIZE

    def start(self):
        WebSocketPlugin.start(self)
        self.bus.subscribe('get-settings', self.get_settings)
        self.bus.subscribe('add-client', self.add_client)
        self.bus.subscribe('del-client', self.del_client)
        self.bus.subscribe('get-clients', self.get_clients)
        self.bus.subscribe('add-history', self.add_history)
        self.bus.subscribe('get-history', self.get_history)
        self.bus.subscribe('del-history', self.del_history)
        self.bus.subscribe('process-command', self.process_command)

    def stop(self):
        WebSocketPlugin.stop(self)
        self.bus.unsubscribe('get-settings', self.get_settings)
        self.bus.unsubscribe('add-client', self.add_client)
        self.bus.unsubscribe('del-client', self.del_client)
        self.bus.unsubscribe('get-clients', self.get_clients)
        self.bus.unsubscribe('add-history', self.add_history)
        self.bus.unsubscribe('get-history', self.get_history)
        self.bus.unsubscribe('process-command', self.process_command)

    def add_client(self, addr, websocket):
        self.clients.append({'ip': addr[0], 'port': addr[1], 'websocket': websocket})

    def del_client(self, addr, websocket):
        try:
            self.clients.remove({'ip': addr[0], 'port': addr[1], 'websocket': websocket})
        except Exception as exc:
            log.exception("Exception %s", exc)
            log.info('Unable to delete client %s', addr)

    def get_clients(self, client_type):
        ws_list = []
        for client in self.clients:
            ws = client['websocket']
            if ws.type == client_type:
                ws_list.append(client['websocket'])
        return ws_list

    def add_history(self, message):
        self.history.append(message)
        if len(self.history) > self.history_size:
            self.history.pop(0)

    def del_history(self, msg_id):
        if len(msg_id) > 1:
            return

        for index, item in enumerate(self.history):
            if str(item.id) == msg_id[0]:
                self.history.pop(index)

    def get_settings(self, style_type):
        return self.style_settings[style_type]

    def get_history(self):
        return self.history

    def process_command(self, command, values):
        if command == 'remove_by_ids':
            self._remove_by_id(values.messages)
        elif command == 'remove_by_users':
            self._remove_by_user(values.users)
        elif command == 'replace_by_ids':
            self._replace_by_id(values.messages)
        elif command == 'replace_by_users':
            self._replace_by_user(values.users)

    def _remove_by_id(self, ids):
        for item in ids:
            for message in self.history:
                if message.id == item:
                    self.history.remove(message)

    def _remove_by_user(self, users):
        for item in users:
            for message in reversed(self.history):
                if message.user == item:
                    self.history.remove(message)

    def _replace_by_id(self, ids):
        for item in ids:
            for index, message in enumerate(self.history):
                if message.id == item:
                    self.history[index]['text'] = REMOVED_TRIGGER
                    if 'emotes' in self.history[index]:
                        del self.history[index]['emotes']

    def _replace_by_user(self, users):
        for item in users:
            for index, message in enumerate(self.history):
                if message.user == item:
                    self.history[index]['text'] = REMOVED_TRIGGER
                    if 'emotes' in self.history[index]:
                        del self.history[index]['emotes']


class RestRoot(object):
    def __init__(self, settings, modules):
        self.settings = settings
        self._rest_modules = {}

        for name, module in modules.items():
            if module:
                api = module.rest_api()
                if api:
                    self._rest_modules[name] = api

    @cherrypy.expose
    def default(self, *args, **kwargs):
        # Default error state
        message = 'Incorrect api call'
        error_code = 400

        body = cherrypy.request.body
        if cherrypy.request.method in cherrypy.request.methods_with_bodies:
            try:
                data = json.load(body)
                kwargs.update(data)
            except:
                pass

        cherrypy.response.headers["Expires"] = -1
        cherrypy.response.headers["Pragma"] = "no-cache"
        cherrypy.response.headers["Cache-Control"] = "private, max-age=0, no-cache, no-store, must-revalidate"

        if len(args) > 1:
            module_name = args[0]
            rest_path = args[1]
            query = args[2:] if len(args) > 2 else None
            if module_name in self._rest_modules:
                method = cherrypy.request.method
                api = self._rest_modules[module_name]
                if method in api:
                    if rest_path in api[method]:
                        return api[method][rest_path](query, **kwargs)
                error_code = 404
                message = 'Method not found'
        cherrypy.response.status = error_code
        return json.dumps({'error': 'Bad Request',
                           'status': error_code,
                           'message': message})


class CssRoot(object):
    def __init__(self, settings):
        self.css_map = {
            'css': self.style_css,
            'scss': self.style_scss
        }
        self.settings = settings

    @cherrypy.expose
    def default(self, *args, **kwargs):
        cherrypy.response.headers['Content-Type'] = 'text/css'
        path = ['css']
        path.extend(args)
        file_type = args[-1].split('.')[-1]
        if file_type in self.css_map:
            return self.css_map[file_type](*path)
        return

    def apply_headers(self):
        cherrypy.response.headers["Expires"] = -1
        cherrypy.response.headers["Pragma"] = "no-cache"
        cherrypy.response.headers["Cache-Control"] = "private, max-age=0, no-cache, no-store, must-revalidate"

    def style_css(self, *path):
        cherrypy.response.headers['Content-Type'] = 'text/css'
        self.apply_headers()
        with open(os.path.join(self.settings['location'], *path), 'r', encoding='utf-8') as css:
            return css.read()

    def style_scss(self, *path):
        self.apply_headers()
        css_namespace = Namespace()
        for key, value in self.settings['keys'].items():
            s_value = value.value
            if isinstance(value, LCText):
                css_value = String(s_value)
            elif isinstance(value, LCColour):
                css_value = Color.from_hex(s_value)
            elif isinstance(value, LCBool):
                css_value = Boolean(s_value)
            elif isinstance(value, LCSpin):
                css_value = Number(s_value)
            elif isinstance(value, LCChooseMultiple):
                css_value = List([String(item) for item in s_value])
            elif isinstance(value, LCObject):
                css_value = String(s_value)
            else:
                raise ValueError("Unable to find comparable values")
            css_namespace.set_variable(f'${key}', css_value)

        cherrypy.response.headers['Content-Type'] = 'text/css'
        with open(os.path.join(self.settings['location'], *path), 'r', encoding='utf-8') as css:
            css_content = css.read()
            compiler = Compiler(namespace=css_namespace)
            # Something wrong with PyScss,
            #  Syntax error: Found u'100%' but expected one of ADD.
            # Doesn't happen on next attempt, so we are doing bad thing
            attempts = 0
            while attempts < 100:
                try:
                    attempts += 1
                    ret_string = compiler.compile_string(css_content)
                    return ret_string
                except Exception as exc:
                    if attempts == 100:
                        log.info(exc)


class HttpRoot(object):
    def __init__(self, style_settings):
        self.settings = style_settings

    @cherrypy.expose
    def index(self):
        cherrypy.response.headers["Expires"] = -1
        cherrypy.response.headers["Pragma"] = "no-cache"
        cherrypy.response.headers["Cache-Control"] = "private, max-age=0, no-cache, no-store, must-revalidate"
        if os.path.exists(self.settings['location']):
            return serve_file(os.path.join(self.settings['location'], 'index.html'), 'text/html')
        else:
            return "Style not found"

    @cherrypy.expose
    def ws(self):
        pass


class SocketThread(threading.Thread):
    def __init__(self, host, port, root_folder, **kwargs):
        super(self.__class__, self).__init__()
        self.daemon = True
        self.host = host
        self.port = port
        self.root_folder = root_folder
        self.style_settings = kwargs['style_settings']
        self.modules = kwargs.pop('modules')

        self.root_config = None
        self.css_config = None
        self.gui_root_config = None
        self.gui_css_config = None

        self.rest_config = None

        cherrypy.config.update({'server.socket_port': int(self.port), 'server.socket_host': self.host,
                                'engine.autoreload.on': False})
        self.websocket = WebChatPlugin(cherrypy.engine, self.style_settings)
        self.websocket.subscribe()
        cherrypy.tools.websocket = WebSocketTool()

    def update_settings(self):
        server_static_dir = self.style_settings[BROWSER_CHAT]['location']
        server_folders = [folder for folder in os.listdir(server_static_dir)
                          if os.path.isdir(os.path.join(server_static_dir, folder))]

        gui_static_dir = self.style_settings[GUI_CHAT]['location']
        gui_folders = [folder for folder in os.listdir(gui_static_dir)
                       if os.path.isdir(os.path.join(gui_static_dir, folder))]

        self.root_config = {
            '/ws': {'tools.websocket.on': True,
                    'tools.websocket.handler_cls': WebChatSocketServer}
        }
        self.root_config.update({
            f'/{folder}': {
                'tools.staticdir.on': True,
                'tools.staticdir.dir': os.path.join(server_static_dir, folder),
                'tools.expires.on': True,
                'tools.expires.secs': 1
            } for folder in server_folders if folder not in ['css']
        })

        self.css_config = {
            '/': {}
        }
        self.rest_config = {
            '/': {}
        }

        self.gui_root_config = {
            '/ws': {'tools.websocket.on': True,
                    'tools.websocket.handler_cls': WebChatGUISocketServer},
        }
        self.gui_root_config.update({
            f'/{folder}': {
                'tools.staticdir.on': True,
                'tools.staticdir.dir': os.path.join(gui_static_dir, folder),
                'tools.expires.on': True,
                'tools.expires.secs': 1
            } for folder in gui_folders if folder not in ['css']
        })
        self.gui_css_config = {'/': {}}

    def run(self):
        cherrypy.log.access_file = ''
        cherrypy.log.error_file = ''
        cherrypy.log.screen = False

        # Removing Access logs
        cherrypy.log.access_log.propagate = False
        cherrypy.log.error_log.setLevel(logging.ERROR)

        self.update_settings()
        self.mount_dirs()
        try:
            cherrypy.engine.start()
        except Exception as exc:
            log.error('Unable to start webchat: %s', exc)

    def mount_dirs(self):
        cherrypy.tree.mount(CssRoot(self.style_settings[GUI_CHAT]), '/gui/css', self.gui_css_config)
        cherrypy.tree.mount(CssRoot(self.style_settings[BROWSER_CHAT]), '/css', self.css_config)

        cherrypy.tree.mount(HttpRoot(self.style_settings[GUI_CHAT]), '/gui', self.gui_root_config)
        cherrypy.tree.mount(HttpRoot(self.style_settings[BROWSER_CHAT]), '', self.root_config)

        cherrypy.tree.mount(RestRoot(self.style_settings, self.modules), '/rest', self.rest_config)


def socket_open(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    return sock.connect_ex((host, int(port)))


class Webchat(MessagingModule):
    def __init__(self, *args, **kwargs):
        MessagingModule.__init__(self, config=CONF_DICT, gui=self._gui_settings(), hidden=True, *args, **kwargs)
        self._load_priority = 9001
        self._category = 'main'
        self._language = kwargs['main_settings'].language
        self._translations = TRANSLATIONS

        self.style_settings = {}
        self.prepare_style_settings()

        self.host = self.get_config('server', 'host').simple()
        self.port = self.get_config('server', 'port').simple()

        self.socket_thread = None
        self.queue = kwargs.get('queue')
        self.message_threads = []

        # Rest Api Settings
        self.rest_add('GET', 'style', self.rest_get_style_settings)
        self.rest_add('GET', 'style_gui', self.rest_get_style_settings)
        self.rest_add('GET', 'history', self.rest_get_history)
        self.rest_add('DELETE', 'chat', self.rest_delete_history)

    def load_module(self, *args, **kwargs):
        MessagingModule.load_module(self, *args, **kwargs)
        self.start_webserver()

    def start_webserver(self):
        if socket_open(self.host, self.port):
            try:
                self.socket_thread = SocketThread(self.host, self.port, CONF_FOLDER,
                                                  style_settings=self.style_settings,
                                                  modules=self._loaded_modules)
                self.socket_thread.start()
            except:
                log.error('Unable to bind at %s:%s', self.host, self.port)

            for thread in range(WS_THREADS):
                self.message_threads.append(MessagingThread(self.style_settings))
                self.message_threads[thread].start()
        else:
            log.error("Port is already used, please change webchat port")

    @staticmethod
    def get_style_path(style):
        path = os.path.abspath(os.path.join(HTTP_FOLDER, style))
        if os.path.exists(path):
            return path
        elif os.path.exists(os.path.join(HTTP_FOLDER, DEFAULT_STYLE)):
            return os.path.join(HTTP_FOLDER, DEFAULT_STYLE)
        else:
            dirs = os.listdir(HTTP_FOLDER)
            if dirs:
                return os.path.join(HTTP_FOLDER, dirs[0])

    def reload_chat(self):
        self.queue.put(CommandMessage('reload'))

    def apply_settings(self, **kwargs):
        save_settings(self.conf_params, ignored_sections=self.conf_params['gui'].get('ignored_sections', ()))
        html_template = jinja2.Template(HTML_TEMPLATE)
        with open(f'{HTTP_FOLDER}/index.html', 'w') as template_file:
            template_file.write(html_template.render(port=self.port))
        if 'system_exit' in kwargs:
            return

        changes = [item.split(MODULE_KEY)[1] for item in kwargs.get('changes').keys()]
        changed_chat_type = [item for item in changes if item in self.style_settings]
        for chat in changed_chat_type:
            style_name = self.get_config(chat, 'style').value
            self.style_settings[chat]['style_name'] = style_name
            self.style_settings[chat]['location'] = self.get_style_path(style_name)

        if changes:
            self.socket_thread.update_settings()
            self.socket_thread.mount_dirs()
        self.write_style_to_file(self.get_config('server_chat', 'style').value, BROWSER_CHAT)
        self.write_style_to_file(self.get_config('gui_chat', 'style').value, GUI_CHAT)

        self.reload_chat()

        for module in self._dependencies:
            self._loaded_modules[module].apply_settings(from_depend='webchat')

    def _process_message(self, message, **kwargs):
        if not hasattr(message, 'hidden'):
            s_queue.put(message)
        return message

    def rest_get_style_settings(self, *args):
        chat_path = args[0][0]
        if chat_path == 'gui':
            chat_type = GUI_CHAT
        else:
            chat_type = BROWSER_CHAT

        return json.dumps(convert_to_dict(self.style_settings[chat_type]['keys']))

    def rest_get_history(self, *args, **kwargs):
        return json.dumps([
            prepare_message(message, self.style_settings['chat'])
            for message in cherrypy.engine.publish('get-history')[0]
        ])

    @staticmethod
    def rest_delete_history(path, **kwargs):
        cherrypy.engine.publish('del-history', path)
        cherrypy.engine.publish('websocket-broadcast',
                                RemoveMessageByIDs(list(path)).json())

    def get_style_from_file(self, style_name, style_type):
        file_name = SETTINGS_GUI_FILE if style_type == 'gui_chat' else SETTINGS_FILE

        file_path = os.path.join(self.get_style_path(style_name), file_name)
        if file_path and os.path.exists(file_path):
            with open(file_path, 'r') as style_file:
                try:
                    return json.load(style_file, object_pairs_hook=OrderedDict)
                except ValueError as exc:
                    return OrderedDict()
        return OrderedDict

    def write_style_to_file(self, style_name, style_type):
        file_name = SETTINGS_GUI_FILE if style_type == 'gui_chat' else SETTINGS_FILE
        file_path = os.path.join(self.get_style_path(style_name), file_name)
        data = self.style_settings[style_type]['keys']
        if not data:
            log.error('Unable to get data for style %s', style_name)
            return

        with open(file_path, 'w') as style_file:
            json.dump(convert_to_dict(data, ordered=True), style_file, indent=2)

    def get_style_format_from_file(self, style_name):
        file_path = os.path.join(self.get_style_path(style_name), SETTINGS_FORMAT_FILE)
        if file_path and os.path.exists(file_path):
            with open(file_path, 'r') as gui_file:
                return json.load(gui_file)
        return {}

    def load_style_keys(self, style_name, style_type=None, keys=None):
        # Key check for GUI Redraw
        if keys:
            style_type = keys['all_settings']['type']

        lc_settings = alter_data_to_lc_style(self.get_style_from_file(style_name, style_type),
                                             self.get_style_format_from_file(style_name))

        current_config = self.get_config(style_type, 'style_settings')
        missing_keys = [item for item in current_config.value.keys()
                        if item not in lc_settings.value.keys() and item not in MANDATORY_KEYS]
        for key in missing_keys:
            current_config.value.pop(key)

        merge_lc_dicts(current_config, lc_settings)

        self.update_translations(style_name, style_type)
        return current_config.value

    def prepare_style_settings(self):
        for ui_type in CHAT_TYPES:
            style_config = self.get_config(ui_type).value
            style_name = style_config['style'].value
            self.style_settings[ui_type] = {
                'style_name': style_name,
                'location': self.get_style_path(style_name),
                'keys': self.load_style_keys(style_name, ui_type)
            }

    def _gui_settings(self):
        redraw = {
            ui_type: {
                'redraw': {
                    'style_settings': {
                        'redraw_trigger': ['style'],
                        'type': ui_type,
                        'get_config': self.load_style_keys,
                        'get_gui': self.get_style_format_from_file
                    },
                }
            } for ui_type in CHAT_TYPES
        }
        redraw.update({
            'non_dynamic': ['server.*'],
            'system': {'hidden': ['enabled']},
            'ignored_sections': ['gui_chat.style_settings', 'server_chat.style_settings']
        })
        return redraw

    def update_translations(self, style_name, style_type):
        locale_file = os.path.join(
            self.get_style_path(style_name), 'translations', f'{self._language}')

        with open(locale_file, 'r', encoding='utf-8') as f:
            try:
                for line in f.readlines():
                    if not line:
                        continue

                    key, translation = map(str.strip, line.split(SPLIT_TRANSLATION))
                    style_key = MODULE_KEY.join(['webchat', style_type, 'style_settings', key])
                    self._translations[style_key] = translation
            except Exception as exc:
                log.info(f'Skipping file {locale_file} due to {exc}')
