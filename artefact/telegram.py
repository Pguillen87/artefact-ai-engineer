"""Telegram Bot API client with an injectable transport."""
import json
import urllib.error
import urllib.request

class TelegramError(RuntimeError):
    pass

class TelegramClient:
    def __init__(self, token, transport=None):
        if not token: raise ValueError('TELEGRAM_BOT_TOKEN required')
        self._token=token
        self._transport=transport or self._post

    def _post(self, method, payload):
        request=urllib.request.Request('https://api.telegram.org/bot'+self._token+'/'+method,
            data=json.dumps(payload).encode('utf-8'),headers={'Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.urlopen(request,timeout=30) as response:
                return json.load(response)
        except (OSError,ValueError,urllib.error.URLError):
            raise TelegramError('Telegram request failed') from None

    def _call(self, method, payload):
        result=self._transport(method,payload)
        if not isinstance(result,dict) or result.get('ok') is not True:
            raise TelegramError('Telegram request failed')
        return result.get('result')

    def get_updates(self, offset):
        result=self._call('getUpdates',{'timeout':20,'offset':offset})
        if not isinstance(result,list): raise TelegramError('Invalid Telegram response')
        return result

    def send(self, chat_id, text):
        return self._call('sendMessage',{'chat_id':chat_id,'text':text[:4000]})
