var Vue = require('vue');
var twemoji = require('twemoji');

(function (WebSocket, Vue) {
    'use strict';

    new Vue({
        el: '#chat-container',
        data() {
            var wsUrl = this.get_websocket_url();
            var messages = [];
            var socket = new WebSocket(wsUrl);

            return {
                messages: messages,
                url: wsUrl,
                socket: socket,
                attempts: 0,
                socket_interval: null,
                message_clear_interval: -1,
                message_decay_interval: -1,
                messagesLimit: 30,

                style_message: {},
                style_text: {},
                style_background: {}
            }
        },
        computed: {
            style_message: function () {
                return this.style_message;
            },
            style_text: function () {
                return this.style_text;
            }
        },
        created: function () {
            var self = this;

            this.socket.onmessage = this.onmessage;
            this.socket.onopen = this.onopen;
            this.socket.onclose = this.onclose;

            self.get(self.get_base_path() + 'api/get_window_settings', (err, response) => {
                if (err) {
                    console.log('Error: Bad response from server')
                }

                console.log(JSON.stringify(response));

                self.messagesClearInterval = response.clear_timer * 1000 || -1;
                if (self.messagesClearInterval > 0) {
                    setInterval(self.clear, 500);
                }

                self.messagesDecayInterval = response.decay_timer * 1000 || -1;
                if (self.messagesDecayInterval > 0) {
                    setInterval(self.decay, 500);
                }

                this.messagesLimit = response.message_limit;

                for (let [key, value] of Object.entries(response.style)) {
                    this.$set(this, `style_${key}`, value);
                }
            });
        },
        methods: {
            get_websocket_url: function () {
                return 'ws://' + window.location.host + window.location.pathname + 'ws';
            },
            get_base_path: function () {
                var path_array = window.location.pathname.split('/');
                path_array.splice(path_array.length);
                return path_array.join('/');
            },
            mouseenter: function (message) {
                message.deleteButton = true;
            },
            mouseleave: function (message) {
                message.deleteButton = false;
            },
            clear: function () {
                var that = this;
                var time = new Date();

                this.messages = this.messages.filter(function (message) {
                    return Math.abs(time - message.time) < that.message_clear_interval;
                });
            },
            decay: function () {
                var that = this;
                var time = new Date();

                this.messages = this.messages.filter(function (message) {
                    if (message.old) return message;
                    if (Math.abs(time - message.time) > that.message_decay_interval) {
                        message.old = true
                    }
                    return message;
                });
            },
            remove: function (message) {
                var index = this.messages.indexOf(message);
                if (index >= 0) this.messages.splice(index, 1);
            },
            sanitize: function (message) {
                var clean = this.replaceEmotions(message.text, message.emotes);
                if (!clean) this.remove(message);
                return clean;
            },
            replaceEmotions: function (message, emotes) {
                var tw_message = twemoji.parse(message);
                return emotes.reduce(function (m, emote) {
                        var regex = new RegExp(emote.id, 'g');
                        return m.replace(regex, '<img class="smile" src="' + emote.url + '"  alt=""/>')
                    },
                    tw_message);
            },
            removeByIds: function (ids) {
                this.messages = this.messages.filter(function (message) {
                    return ids.indexOf(message.id) < 0;
                });
            },
            removeByUsernames: function (usernames) {
                usernames = usernames.map(function (value) {
                    return value.toLowerCase();
                });

                this.messages = this.messages.filter(function (message) {
                    var user = message.user.toLowerCase();
                    return usernames.indexOf(user) < 0;
                });
            },
            replaceByUsernames: function (usernames, text) {
                var usernames_lc = usernames.map(function (value) {
                    return value.toLowerCase();
                });

                this.messages = this.messages.map(function (message) {
                    var user = message.user.toLowerCase();
                    var index = usernames_lc.indexOf(user);

                    if (index >= 0) {
                        message.text = text;
                        message.emotes = [];
                    }

                    return message;
                });
            },
            replaceByIds: function (ids, text) {
                this.messages = this.messages.map(function (message) {
                    var index = ids.indexOf(message.id);

                    if (index >= 0) {
                        message.text = text;
                        message.emotes = [];
                        message.pm = false;
                    }
                    return message;
                });
            },
            run: function (message) {
                switch (message.command) {
                    case 'reload':
                        window.location.reload();
                        break;
                    case 'remove_by_users':
                        this.removeByUsernames(message.users);
                        break;
                    case 'remove_by_ids':
                        this.removeByIds(message.messages);
                        break;
                    case 'replace_by_users':
                        this.replaceByUsernames(message.users, message.text);
                        break;
                    case 'replace_by_ids':
                        this.replaceByIds(message.messages, message.text);
                        break;
                    default:
                        console.log('Got unknown command ', message.command);
                }
            },
            onmessage: function (event) {
                var message = JSON.parse(event.data);
                if (!message.type)
                    return;

                switch (message.type) {
                    case 'command':
                        this.run(message.payload);
                        break;
                    default:
                        message.payload.time = new Date();
                        message.payload.deleteButton = false;
                        message.payload.old = false;
                        message.payload.style = 'style_' + message.type;

                        this.messages.push(message.payload);
                        if (this.messages.length > this.messagesLimit) {
                            this.remove(this.messages[0]);
                        }
                }
            },
            onopen: function () {
                console.log('connection opened');
                this.attempts = 0;
                if (this.socket_interval) {
                    clearInterval(this.socket_interval);
                    this.socketInterval = null;
                }
            },
            onclose: function () {
                if (!this.socket_interval) this.socketInterval = setInterval(this.reconnect, 1000);
            },
            reconnect: function () {
                this.attempts++;

                this.socket = null;
                this.socket = new WebSocket('ws://' + window.location.host + window.location.pathname + 'ws');
                this.socket.onmessage = this.onmessage;
                this.socket.onopen = this.onopen;
                this.socket.onclose = this.onclose;
            },
            load: function (method, url, callback, data) {
                var xhr = new XMLHttpRequest();
                xhr.onload = function () {
                    if (xhr.responseText) {
                        var obj = JSON.parse(xhr.responseText);
                        callback(null, obj);
                    }
                };
                xhr.onerror = function () {
                    if (xhr.responseText) {
                        var obj = JSON.parse(xhr.responseText);
                        callback(obj);
                    }
                };

                xhr.open(method, url);
                xhr.send(data);
            },
            get: function (url, callback, data) {
                return this.load('GET', url, callback, data);
            },
            post: function (url, data, callback) {
                return this.load('POST', url, callback, data);
            },
            del: function (url, callback) {
                return this.load('DELETE', url, callback);
            }
        },
        filters: {}
    });
})(window.WebSocket, Vue);
