"""Telegram Bot API client with an injectable transport."""
import json
import urllib.error
import urllib.request

class TelegramError(RuntimeError):
    pass


def _message_chunks(text, limit=4000):
    """Split long answers at paragraph/line boundaries without dropping text."""
    if not isinstance(text, str):
        raise TelegramError('Telegram message must be text')
    chunks = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind('\n\n', 0, limit - 1)
        delimiter = 2
        if split_at < limit // 2:
            split_at = remaining.rfind('\n', 0, limit - 1)
            delimiter = 1
        if split_at < limit // 2:
            split_at = remaining.rfind(' ', 0, limit - 1)
            delimiter = 1
        split_at = limit if split_at < 0 else split_at + delimiter
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining or not chunks:
        chunks.append(remaining)
    return tuple(chunks)

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
        try:
            result=self._transport(method,payload)
        except (OSError, ValueError, urllib.error.URLError):
            raise TelegramError('Telegram request failed') from None
        if not isinstance(result,dict) or result.get('ok') is not True:
            raise TelegramError('Telegram request failed')
        return result.get('result')

    def get_updates(self, offset):
        result=self._call('getUpdates',{'timeout':20,'offset':offset})
        if not isinstance(result,list) or any(not isinstance(item,dict) or type(item.get('update_id')) is not int for item in result):
            raise TelegramError('Invalid Telegram response')
        return result

    def send(self, chat_id, text):
        results = [self._call('sendMessage', {'chat_id':chat_id, 'text':chunk})
                   for chunk in _message_chunks(text)]
        return results[0] if len(results) == 1 else results[-1]
