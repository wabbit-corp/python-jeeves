from typing import Any
import hashlib
import ujson as json
import sqlite3
import time
import asyncio
import openai

from textwrap import indent, dedent

class MagicDict(dict):
    # implements __getattr__ and __setattr__ for a dictionary
    def __getattr__(self, key):
        r = self[key]
        if isinstance(r, dict) and not isinstance(r, MagicDict):
            return MagicDict(r)
        return r

    def __getitem__(self, key):
        r = super().__getitem__(key)
        if isinstance(r, dict) and not isinstance(r, MagicDict):
            return MagicDict(r)
        return r

    def __setattr__(self, key, value):
        self[key] = value

    def __delattr__(self, key):
        del self[key]

    def __repr__(self):
        return f'MagicDict({super().__repr__()})'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

def obj_to_dict(obj, emit_null=True):
    if isinstance(obj, list):
        return [obj_to_dict(o) for o in obj]
    elif isinstance(obj, dict):
        return {k: obj_to_dict(v) for k, v in obj.items() if emit_null or v is not None}
    elif isinstance(obj, int) or isinstance(obj, float) or isinstance(obj, str) or isinstance(obj, bool) or obj is None:
        return obj
    else:
        return obj_to_dict(obj.__dict__)

def json_hash(obj: Any) -> str:
    request = json.dumps(obj, sort_keys=True)
    return hashlib.sha256(request.encode('utf-8')).hexdigest()

def indent(text: str, prefix: str = '    '):
    return '\n'.join(prefix + line for line in text.splitlines())


class ChatBackend:
    async def async_request(self, **kwargs): ...

    TIMING_FIELD = '__timing__'


class ChatSqliteCache(ChatBackend):
    def __init__(self, backend: ChatBackend, db_path: str, table_name: str = 'chat_cache'):
        self.backend = backend
        self.table_name = table_name
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

        self.cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS {self.table_name} (
                request_hash TEXT PRIMARY KEY,
                request TEXT,
                request_start REAL,
                request_end REAL,
                response TEXT
            )
        ''')

    async def async_request(self, **kwargs) -> MagicDict:
        request_hash = json_hash(kwargs)
        self.cursor.execute('SELECT response FROM chat_cache WHERE request_hash=?', (request_hash,))
        result = self.cursor.fetchone()
        if result is not None:
            print(kwargs)
            return MagicDict(json.loads(result[0]))

        t0 = time.time()
        response = await self.backend.async_request(**kwargs)
        t1 = time.time()

        self.cursor.execute('INSERT INTO chat_cache VALUES (?, ?, ?, ?, ?)',
            (request_hash, json.dumps(kwargs), t0, t1, json.dumps(response)))
        self.conn.commit()

        return response


class ChatAccounting(ChatBackend):
    total_request_count: int = 0
    total_request_time: float = 0
    total_request_cost: float = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    def __init__(self, backend: ChatBackend):
        self.backend = backend

    async def async_request(self, **kwargs):
        response = await self.backend.async_request(**kwargs)

        assert 'usage' in response
        prompt_tokens = response.usage.prompt_tokens
        completion_tokens = response.usage.completion_tokens

        assert ChatBackend.TIMING_FIELD in response
        start = response[ChatBackend.TIMING_FIELD].start
        end = response[ChatBackend.TIMING_FIELD].end

        self.total_request_count += 1
        self.total_input_tokens += prompt_tokens
        self.total_output_tokens += completion_tokens
        self.total_request_time += start - end
        self.total_request_cost += prompt_tokens * 0.01 / 1000.0 + completion_tokens * 0.03 / 1000.0

        return response


class ChatOpenAI(ChatBackend):
    def __init__(self, openai_client: openai.AsyncOpenAI, defaults: dict = {}):
        self.openai_client = openai_client
        self.defaults = defaults

    async def async_request(self, **kwargs) -> MagicDict:
        for k, v in self.defaults.items():
            kwargs.setdefault(k, v)

        retry_count = 0
        while True:
            try:
                t0 = time.time()
                response = await self.openai_client.chat.completions.create(**kwargs)
                t1 = time.time()
                break
            except KeyboardInterrupt:
                raise
            except BaseException as e:
                import traceback
                if retry_count >= 10: raise
                print(f"OpenAI API Error (retrying in 5s): {type(e)}: {e}")
                # traceback.print_exc()
                import random
                await asyncio.sleep(5 * 1.5 ** retry_count + 5 * random.random())
                retry_count += 1

                # OpenAI API Error (retrying in 5s): <class 'openai.error.APIError'>: HTTP code 502 from API (<html>
                # <head><title>502 Bad Gateway</title></head>
                # <body>
                # <center><h1>502 Bad Gateway</h1></center>
                # <hr><center>cloudflare</center>
                # </body>
                # </html>
                # )

        result = obj_to_dict(response, emit_null=False)
        result[ChatBackend.TIMING_FIELD] = { 'start': t0, 'end': t1 }

        for choice in result['choices']:
            message = choice['message']
            if 'function_call' in message and message['function_call'] is None:
                del message['function_call']
            if 'tool_calls' in message and message['tool_calls'] is None:
                del message['tool_calls']

        return MagicDict(result)


class ChatWithDefaults(ChatBackend):
    def __init__(self, backend: ChatBackend, defaults: dict = {}):
        self.backend = backend
        self.defaults = defaults

    async def async_request(self, **kwargs) -> MagicDict:
        for k, v in self.defaults.items():
            kwargs.setdefault(k, v)

        return await self.backend.async_request(**kwargs)
