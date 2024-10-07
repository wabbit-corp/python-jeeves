from typing import Any, List, Dict
import typing
import dataclasses
from dataclasses import dataclass, field
from collections import defaultdict
import builtins

import re
import json
from textwrap import dedent
import time
import logging

from clj import SExpr, sexpr
from clj.exec import ExecutionContext, eval_sexpr, Quoted

import openai
from openai import AsyncOpenAI
from gpt import ChatOpenAI, ChatAccounting, ChatSqliteCache

import discord
import discord.utils

from servant.weather import fetch_weather_forecast
from servant.base.tools import ToolDispatcher, ToolDef
from servant.base.json import obj_to_json, JSON, JSONDict, JSONArray

import sqlite3

_LOGGER = logging.getLogger(__name__ if __name__ != '__main__' else 'jeeves')


@dataclass
class AgentDescription:
    name: str
    description: str

    def to_json(self):
        return {
            'name': self.name,
            'description': self.description
        }

    @classmethod
    def from_json(cls, json) -> 'AgentDescription':
        return cls(**json)


@dataclass
class Config:
    openai_key: str | None = None
    personalities: Dict[str, AgentDescription] = field(default_factory=dict)
    user_agent: str | None = None
    discord_token: str | None = None
    imgflip_username: str | None = None
    imgflip_password: str | None = None


@dataclass
class Note:
    title: str
    content: str
    important: bool = False

    created_time: int = field(default_factory=lambda: int(time.time()))
    updated_time: int = field(default_factory=lambda: int(time.time()))

    def to_json(self):
        return {
            'title': self.title,
            'content': self.content,
            'important': self.important,
            'created_time': self.created_time,
            'updated_time': self.updated_time
        }

    @classmethod
    def from_json(cls, json) -> 'Note':
        return cls(**json)


@dataclass
class ScheduleItem:
    title: str
    description: str
    expression: str | None
    important: bool = False

    created_time: int = field(default_factory=lambda: int(time.time()))
    updated_time: int = field(default_factory=lambda: int(time.time()))

    def to_json(self):
        return {
            'title': self.title,
            'description': self.description,
            'expression': self.expression,
            'important': self.important,
            'created_time': self.created_time,
            'updated_time': self.updated_time
        }

    @classmethod
    def from_json(cls, json) -> 'ScheduleItem':
        return cls(**json)



# class AssistantDatabase:
#     def __init__(self):
#         self.database = sqlite3.connect('jeeves.db')
#         self.database.execute('''
#             CREATE TABLE IF NOT EXISTS notes (
#                 title TEXT PRIMARY KEY,
#                 content TEXT,
#                 important INTEGER,
#                 created_time INTEGER,
#                 updated_time INTEGER
#             )''')
#         self.database.execute('''
#             CREATE TABLE IF NOT EXISTS schedule (
#                 title TEXT PRIMARY KEY,
#                 description TEXT,
#                 expression TEXT,
#                 important INTEGER,
#                 created_time INTEGER,
#                 updated_time INTEGER
#             )''')
#         self.database.execute('''
#             CREATE TABLE Servers (
#                 server_id INTEGER PRIMARY KEY,
#                 name TEXT NOT NULL,
#                 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
#                 updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
#                 metadata JSON
#             );''')
#         self.database.execute('''
#             CREATE TABLE Channels (
#                 channel_id INTEGER PRIMARY KEY,
#                 server_id INTEGER NOT NULL,
#                 name TEXT NOT NULL,
#                 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
#                 updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
#                 metadata JSON,
#                 FOREIGN KEY (server_id) REFERENCES Servers (server_id)
#             );''')
#         self.database.execute('''
#             CREATE TABLE Users (
#                 user_id INTEGER PRIMARY KEY,
#                 username TEXT NOT NULL,
#                 discriminator TEXT NOT NULL,
#                 joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
#                 metadata JSON
#             );''')


class JeevesState:
    config: Config
    notes: dict[str, Note]
    schedule: List[ScheduleItem]
    channel_messages: Dict[str, List[Dict[str, Any]]]
    channel_personality: Dict[str, str]

    def __init__(self, config: Config):
        self.config = config
        self.notes = {}
        self.schedule = []
        self.channel_messages = defaultdict(list)
        self.channel_personality = {}

    async def create_or_modify_note(self, title: str, content: str | None, important: bool | None = None) -> JSONDict:
        note = self.notes.get(title)
        if note is None:
            if content is None:
                return { 'error': f'Note was not deleted since there is no note named "{title}".', 'data': { 'title': title } }
            else:
                note = Note(
                    title=title,
                    content=content,
                    important=important if important is not None else False
                )
                self.notes[title] = note
                return { 'message': f'Note "{title}" was created.' }
        else:
            if content is None:
                del self.notes[title]
                return { 'message': f'Note "{title}" was deleted.' }
            else:
                note.content = content
                if important is not None:
                    note.important = important
                note.updated_time = int(time.time())
                return { 'message': f'Note "{title}" was modified.' }

    async def show_note(self, title: str) -> JSONDict:
        note = self.notes.get(title)
        if note is None:
            return { 'error': 'Note not found.', 'data': { 'title': title } }
        else:
            return note.to_json()

    async def create_or_modify_schedule_item(self, title: str, description: str | None, expression: str | None, important: bool | None = None) -> JSONDict:
        if description is None and expression is None:
            # Deletion
            for i, item in enumerate(self.schedule):
                if item.title == title:
                    del self.schedule[i]
                    return { 'message': f'Scheduled item "{title}" was deleted.' }
            return { 'error': f'Scheduled item "{title}" not found.', 'data': { 'title': title } }
        else:
            # Creation or modification
            for item in self.schedule:
                if item.title == title:
                    if description is not None:
                        item.description = description
                    item.expression = expression
                    if important is not None:
                        item.important = important
                    item.updated_time = int(time.time())
                    return { 'message': f'Scheduled item "{title}" was modified.' }

            item = ScheduleItem(
                title=title,
                description=description,
                expression=expression,
                important=important if important is not None else False
            )
            self.schedule.append(item)
            return { 'message': f'Scheduled item "{title}".' }

    async def show_schedule(self) -> JSONArray:
        return [item.to_json() for item in self.schedule]

    def register_tools(self, tools: ToolDispatcher):
        tools.register(
            name='create_or_modify_note',
            schema={
                'type': 'function',
                'function': {
                    'name': 'create_or_modify_note',
                    'description': 'Create, modify, or delete a note.',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'title': {
                                'type': 'string',
                                'description': 'The title of the note.'
                            },
                            'content': {
                                'type': 'string',
                                'description': 'The content of the note. If null, the note will be deleted.'
                            },
                            'important': {
                                'type': 'boolean',
                                'description': 'Whether the note is important or not.'
                            }
                        },
                        'required': ['title']
                    }
                }
            },
            function=lambda obj: self.create_or_modify_note(obj['title'], obj.get('content'), obj.get('important'))
        )

        tools.register(
            name='show_note',
            schema={
                'type': 'function',
                'function': {
                    'name': 'show_note',
                    'description': 'Show a note.',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'title': {
                                'type': 'string',
                                'description': 'The title of the note.'
                            }
                        },
                        'required': ['title']
                    }
                }
            },
            function=lambda obj: self.show_note(obj['title'])
        )

        tools.register(
            name='create_or_modify_my_schedule_item',
            schema={
                'type': 'function',
                'function': {
                    'name': 'create_or_modify_my_schedule_item',
                    'description': 'Create, modify, or delete a scheduled item on your personal calendar.',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'title': {
                                'type': 'string',
                                'description': 'The title of the item.'
                            },
                            'description': {
                                'type': 'string',
                                'description': 'The description of the item. If null, the item will be deleted.'
                            },
                            'expression': {
                                'type': 'string',
                                'description': 'The date/time expression of the item.'
                            },
                            'important': {
                                'type': 'boolean',
                                'description': 'Whether the item is important or not.'
                            }
                        },
                        'required': ['title']
                    }
                }
            },
            function=lambda obj: self.create_or_modify_schedule_item(
                title=obj['title'],
                description=obj.get('description'),
                expression=obj.get('expression'),
                important=obj.get('important'))
        )

        tools.register(
            name='show_my_schedule',
            schema={
                'type': 'function',
                'function': {
                    'name': 'show_my_schedule',
                    'description': 'Show your schedule.'
                }
            },
            function=lambda obj: self.show_schedule()
        )

    async def reply(self, discord_message, content):
        while True:
            content = content.strip()
            if len(content) == 0:
                break
            if len(content) <= 2000:
                await discord_message.channel.send(content)
                break
            else:
                line_break = content.rfind('\n', 0, 2000)
                if line_break == -1:
                    space_break = content.rfind(' ', 0, 2000)
                    if space_break == -1:
                        await discord_message.channel.send(content[:2000])
                        content = content[2000:]
                    else:
                        await discord_message.channel.send(content[:space_break])
                        content = content[space_break + 1:]
                else:
                    await discord_message.channel.send(content[:line_break])
                    content = content[line_break + 1:]

    async def handle_incoming_message(self, client: discord.Client, discord_message: discord.Message, openai_client: ChatOpenAI, tools: ToolDispatcher, debug_mode: bool = False):
        channel_id = str(discord_message.channel.id)

        personality_name = self.channel_personality.get(channel_id, 'Jeeves')
        personality_name_short = personality_name[0]
        channel_personality = self.config.personalities[personality_name].description

        # React to the message with a thumbs up emoji
        try:
            await discord_message.add_reaction('🤔')
        except Exception as e:
            _LOGGER.error(f'Failed to add reaction to message: {e}')
            pass

        # Add a typing indicator
        try:
            async with discord_message.channel.typing():
                notes = list(self.notes.values())
                notes.sort(key=lambda note: (note.important, -note.updated_time), reverse=True)
                notes_text = []
                for note in notes:
                    note_text = f' - **{note.title}**' + (' (important)' if note.important else '')
                    if note.important:
                        note_text += ': ' + note.content
                    notes_text.append(note_text)
                if notes_text:
                    notes_text_all = '\n' + '\n'.join(notes_text)
                else:
                    notes_text_all = 'No notes recorded yet.'

                schedule_items = list(self.schedule)
                schedule_items.sort(key=lambda item: (item.important, item.updated_time), reverse=True)
                schedule_text = []
                for item in schedule_items:
                    schedule_text.append(f' - **{item.title}**' + (' (important)' if item.important else '') + f': {item.description} scheduled to occur "{item.expression}"')
                if schedule_text:
                    schedule_text_all = '\n' + '\n'.join(schedule_text)
                else:
                    schedule_text_all = 'No schedule items recorded yet.'

                # Today's date
                import datetime
                import pytz
                new_york_tz = pytz.timezone("America/New_York")
                new_york_dt = datetime.datetime.now(new_york_tz)
                new_york_date_str = new_york_dt.strftime('%A, %B %d, %Y')
                new_york_time_str = new_york_dt.strftime('%H:%M:%S')

                system_prompt = (dedent(
                    '''
                    # Tools

                    ## Current time and date
                    Current date and time in New York City is {{new_york_date_str}} and time is {{new_york_time_str}}.
                    When answering questions about the date and time, provide it in human readable form. Assume the users are in New York unless otherwise specified.
                    If you are asked about the time in a different location, provide the time in that location based on the timezone and UTC offset.

                    ## Notes
                    Write down any important information that can help you better serve the users. You can use the `create_or_modify_note` command to create or modify a note, and the `show_note` command to read a note. Set the `important` flag to `true` if the note is important for you to remember.
                    Notes: {{notes_text_all}}

                    ## Schedule
                    Use the `create_or_modify_my_schedule_item` command to write down any important events, tasks, reminders, or recurrent items that YOU need to remember.
                    Syntax for `expression` when using the `schedule_item` command:
                    - For a one-time event: "YYYY-MM-DD HH:MM:SS".
                    - For a recurrent event use Unix Cron syntax: "0 0 * * 0" (every Sunday at midnight).
                    - For a relative time: "in 2 hours", "in 3 days", "in 1 week", "in 1 month", "in 1 year", "in 1 hour 30 minutes".
                    - "next Monday at 9am", "next Tuesday at 3pm", "next Wednesday at 6pm", "next Thursday at 9pm", "next Friday at 12pm", "next Saturday at 3pm", "next Sunday at 6pm".
                    - "tomorrow at 9am", "tomorrow at 3pm", "tomorrow at 6pm", "tomorrow at 9pm", "tomorrow at 12pm", "tomorrow at 3pm", "tomorrow at 6pm".
                    Your Schedule: {{schedule_text_all}}

                    ## Weather
                    You can also use the `get_current_weather` command to get the current weather in a location. Ideally, the location should be specified in the format "City, Country".

                    ## Image Generation
                    When generating images, review the "revised_prompt". If it is not what you expected or if the revised prompt makes too many unnecessary assumptions, try to rephrase and clarify the original prompt to get a better result. Explain to the user what revisions were made by the image generator, particularly if it is forced diversity or other politically correct changes. You can try:
                      * Replacing references to specific people with their appearance descriptions, e.g. "a senile old man" instead of "Joe Biden".
                      * Be more specific about intended demographic characteristics, e.g. "an elderly caucasian gentleman" instead of "an elderly gentleman". This is particularly important when the image generator makes unintended "diversity" changes.

                    # Your Personality
                    {{personality}}

                    # Communication Medium
                    The user messages will have the following format "Message from <user>: <content>".
                    Messages are passed to and from the users through Discord, so you can use Discord syntax (Markdown + Discord's extensions, e.g. ||<text>|| for hidden text - good for joke punchlines) for formatting.
                    Do not end your messages with a question unless it makes sense to do so in the context. You are chatting with people, not interrogating them.
                    ''')
                    .replace('{{notes_text_all}}', notes_text_all)
                    .replace('{{schedule_text_all}}', schedule_text_all)
                    .replace('{{new_york_date_str}}', new_york_date_str)
                    .replace('{{new_york_time_str}}', new_york_time_str)
                    .replace('{{personality}}', channel_personality)
                    .replace('{{personality_name}}', personality_name)
                    .replace('{{personality_name_short}}', personality_name_short)
                )

                assert re.search(r'\{\{.*\}\}', system_prompt) is None, 'Unresolved template variable in system prompt.'

                jeeves_messages = []
                jeeves_messages.append({ 'role': 'system', 'content': system_prompt })

                last_20_messages = self.channel_messages[channel_id][-20:]
                while last_20_messages and last_20_messages[0].get('role') == 'tool':
                    last_20_messages.pop(0)

                for message in last_20_messages:
                    jeeves_messages.append(message)
                # jeeves_messages.append({ 'role': 'user', 'content': discord_message.content })


                while True:
                    try:
                        response = await openai_client.async_request(
                            messages=jeeves_messages,
                            tools=tools.schema)
                    except openai.APIError as e:
                        _LOGGER.error(f"OpenAI API Error: {e}")
                        return

                    result = response.choices[0]
                    jeeves_messages.append(result['message'])

                    _LOGGER.info(f"Jeeves response: {result}")

                    finish_reason = result['finish_reason']
                    result_message = result['message']

                    if finish_reason == 'stop':
                        content = result_message['content']
                        if content.startswith(f'Message from {personality_name}:'):
                            content = content[len('Message from Jeeves:'):].strip()
                        if content.startswith(f'Message from {personality_name_short}:'):
                            content = content[len('Message from J:'):].strip()
                        result['message']['content'] = content
                        await self.reply(discord_message, content)
                        self.channel_messages[channel_id].append(result['message'])
                        break

                    elif finish_reason == 'tool_calls':
                        if 'content' in result_message and result_message['content']:
                            await self.reply(discord_message, result_message['content'])

                        tool_calls = result_message['tool_calls']

                        tool_messages = []
                        tool_messages.append(result['message'])  # extend conversation with tool calls

                        for tool_call in tool_calls:
                            tool_id = tool_call['id']
                            tool_function = tool_call['function']

                            tool_name = tool_function['name']
                            tool_arguments = json.loads(tool_function['arguments'])

                            _LOGGER.info(f"Calling tool {tool_name} with arguments {tool_arguments}")

                            tool_arguments['discord_client'] = client
                            tool_arguments['discord_message'] = discord_message

                            result = await tools.dispatch(tool_name, tool_arguments)

                            _LOGGER.info(f"Tool {tool_name} returned {result}")

                            msg = {
                                "tool_call_id": tool_id,
                                "role": "tool",
                                "name": tool_name,
                                "content": json.dumps(obj_to_json(result), ensure_ascii=False)
                            }

                            jeeves_messages.append(msg)  # extend conversation with function response
                            tool_messages.append(msg)

                        self.channel_messages[channel_id].extend(tool_messages)
        finally:
            try:
                await discord_message.remove_reaction('🤔', client.user)
            except Exception as e:
                _LOGGER.error(f'Failed to remove reaction from message: {e}')
                pass


async def main():
    from clj.types import SExpr
    from clj.parser import sexpr
    from clj.exec import ExecutionContext, eval_sexpr

    ctx = ExecutionContext()

    config = Config()

    def add_personality(ctx: ExecutionContext, name: SExpr.Str, description: SExpr.Str) -> None:
        assert isinstance(name, SExpr.Str)
        assert isinstance(description, SExpr.Str)
        config.personalities[name.value] = AgentDescription(name=name.value, description=dedent(description.value))
    ctx.register(add_personality, name='new-personality')

    def set_openai_key(ctx: ExecutionContext, key: SExpr.Str) -> None:
        assert isinstance(key, SExpr.Str)
        config.openai_key = key.value
    ctx.register(set_openai_key, name='openai-key')

    def set_user_agent(ctx: ExecutionContext, user_agent: SExpr.Str) -> None:
        assert isinstance(user_agent, SExpr.Str)
        config.user_agent = user_agent.value
    ctx.register(set_user_agent, name='user-agent')

    def set_discord_token(ctx: ExecutionContext, discord_token: SExpr.Str) -> None:
        assert isinstance(discord_token, SExpr.Str)
        config.discord_token = discord_token.value
    ctx.register(set_discord_token, name='discord-token')

    def set_imgflip_credentials(ctx: ExecutionContext, username: SExpr.Str, password: SExpr.Str) -> None:
        assert isinstance(username, SExpr.Str)
        assert isinstance(password, SExpr.Str)
        config.imgflip_username = username.value
        config.imgflip_password = password.value
    ctx.register(set_imgflip_credentials, name='imgflip-credentials')

    eval_sexpr(ctx, sexpr(open('jeeves.clj').read()))
    eval_sexpr(ctx, sexpr(open('.private.clj').read()))
    #print(config)
    # return

    raw_client = openai.AsyncOpenAI(api_key=config.openai_key)

    openai_client = ChatOpenAI(
        raw_client,
        defaults={
            'model': "gpt-4o",
            'timeout': 300,
            'max_tokens': 1024
        })

    openai_client = ChatSqliteCache(openai_client, 'cache.db')
    openai_client = ChatAccounting(openai_client)

    tools = ToolDispatcher({})

    import servant.weather
    servant.weather.register_tools(tools)

    jeeves_state = JeevesState(config=config)
    jeeves_state.register_tools(tools)

    # import numpy as np
    # reddit_jokes = json.loads(open('joke-dataset/reddit_jokes.json').read())
    # scores = [j['score'] + 1 for j in reddit_jokes]
    # scores = np.array(scores)
    # scores = scores ** (1/2)
    # scores = scores / scores.sum()

    # bad_words = open('./bad_words.txt', 'rt', encoding='utf-8').read().splitlines()
    # bad_words = set([w.strip().lower() for w in bad_words if w.strip()])

    # BAD_WORD_RE = re.compile(r'\b(' + '|'.join(re.escape(w) for w in bad_words) + r')\b')


    # SEEN_JOKE_COUNT = 128 # Before we allow repeats
    # seen_jokes = []
    # seen_jokes_set = set()

    # async def get_joke() -> JSONDict:
    #     if len(seen_jokes) >= SEEN_JOKE_COUNT:
    #         j = seen_jokes.pop(0)
    #         seen_jokes_set.remove(j)
    #     else:
    #         while True:
    #             j = np.random.choice(np.arange(len(scores)), p=scores)
    #             opening = reddit_jokes[j]['title']
    #             punchline = reddit_jokes[j]['body']
    #             if BAD_WORD_RE.search(opening) or BAD_WORD_RE.search(punchline):
    #                 continue
    #             if j not in seen_jokes_set:
    #                 break
    #     seen_jokes.append(j)
    #     seen_jokes_set.add(j)
    #     return { 'opening': reddit_jokes[j]['title'],
    #              'punchline': reddit_jokes[j]['body'],
    #              'reddit_score': reddit_jokes[j]['score'] }

    # tools.register(
    #     name='get_joke',
    #     schema={
    #         'type': 'function',
    #         'function': {
    #             'name': 'get_joke',
    #             'description': 'Get a random joke.'
    #         }
    #     },
    #     function=lambda obj: get_joke()
    # )

    async def switch_personality(discord_message: discord.Message, personality: str) -> JSONDict:
        channel_id = str(discord_message.channel.id)
        if personality not in config.personalities:
            return { 'error': f'Personality "{personality}" not found.' }
        jeeves_state.channel_personality[channel_id] = personality
        return { 'message': f'Personality switched to "{personality}".' }

    tools.register(
        name='switch_personality',
        schema={
            'type': 'function',
            'function': {
                'name': 'switch_personality',
                'description': 'Change your own personality for the current channel.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'personality': {
                            'type': 'string',
                            # FIXME
                            'description': 'The name of the personality to switch to. Available personalities: Jeeves, Fumiko, Dio Brando.'
                        }
                    },
                    'required': ['personality']
                }
            }
        },
        function=lambda obj: switch_personality(obj['discord_message'], obj['personality'])
    )

    async def generate_image(prompt: str) -> JSONDict:
        import openai
        try:
            r = await raw_client.images.generate(prompt=prompt, size='1024x1024', model='dall-e-3', response_format='url', n=1)
        except openai.APIError as e:
            return { 'error': str(e) }
        print(r.json)
        return {
            'image': r.data[0].url,
            'revised_prompt': r.data[0].revised_prompt
        }

    tools.register(
        name='generate_image',
        schema={
            'type': 'function',
            'function': {
                'name': 'generate_image',
                'description': 'Generate an image given a prompt',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'prompt': {
                            'type': 'string',
                            'description': 'A prompt to generate an image from.'
                        },
                    },
                    'required': ['prompt']
                }
            }
        },
        function=lambda obj: generate_image(obj['prompt'])
    )

    import requests
    all_memes = requests.get(
        'https://api.imgflip.com/get_memes',
        headers={ 'User-Agent': config.user_agent }
    ).json()
    # Save it to a file
    with open('all_memes.json', 'wt') as f:
        json.dump(all_memes, f)
    top_meme_names = [meme['name'] for meme in all_memes['data']['memes']]
    top_meme_names = ', '.join(f"'{meme['name']}' ({meme['box_count']} boxes)" for meme in all_memes['data']['memes'])

    print('Total memes:', len(all_memes['data']['memes']))
    # return

    async def generate_meme(name: str, box_text: List[str]) -> JSONDict:
        meme_id = None
        for meme in all_memes['data']['memes']:
            if meme['name'].lower() == name.lower():
                meme_id = meme['id']
                break

        if meme_id is None:
            # Find the closest few matches
            matches = []
            import Levenshtein
            for meme in all_memes['data']['memes']:
                matches.append((meme['name'], Levenshtein.distance(name.lower(), meme['name'].lower())))
            matches.sort(key=lambda x: x[1])
            return { 'error': f'Meme template "{name}" not found. Closest matches: {[x[0] for x in matches[:10]]}' }

        data = {
            'template_id': meme_id,
            'username': config.imgflip_username,
            'password': config.imgflip_password,
        }

        if len(box_text) > 0:
            data['text0'] = box_text[0]
        if len(box_text) > 1:
            data['text1'] = box_text[1]

        for i, text in enumerate(box_text):
            data[f'boxes[{i}][text]'] = text

        headers = {
            'User-Agent': config.user_agent
        }

        print(data)

        r = requests.post('https://api.imgflip.com/caption_image', data=data, headers=headers)

        rj = r.json()
        print(rj)
        return { 'image': rj['data']['url'] }

    tools.register(
        'generate_meme',
        schema={
            'type': 'function',
            'function': {
                'name': 'generate_meme',
                'description': 'Generate a meme given a template and text',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'template_name': {
                            'type': 'string',
                            'description': f'The name of the meme template on Imgflip, like {top_meme_names}.'
                        },
                        'box_text': {
                            'type': 'array',
                            'description': 'The text to put in each box of the meme.',
                            'items': {
                                'type': 'string'
                            }
                        }
                    },
                    'required': ['template_id', 'text0', 'text1']
                }
            }
        },
        function=lambda obj: generate_meme(obj['template_name'], obj['box_text'])
    )


    # async def read_my_code() -> JSONDict:
    #     with open(__file__, 'r') as f:
    #         code = f.read()
    #     return { 'code': code }

    # tools.register(
    #     name='read_my_code',
    #     schema={
    #         'type': 'function',
    #         'function': {
    #             'name': 'read_my_code',
    #             'description': 'Read your own code.',
    #             'parameters': { }
    #         }
    #     },
    #     function=lambda obj: read_my_code()
    # )


    class MyClient(discord.Client):
        async def on_ready(self):
            _LOGGER.info(f'Logged on as {self.user}!')

        async def get_user_info(self, user_id):
            user = await self.fetch_user(user_id)
            return {
                'id': user.id,
                'name': user.name,
                'discriminator': user.discriminator
            }

        async def on_message(self, discord_message):
            _LOGGER.info(f'Message from {discord_message.author}: {discord_message.content}')

            if discord_message.author == self.user:
                return

            dm_content = discord_message.content

            # Decode <@USER_ID> mentions
            for user_id in re.findall(r'<@!?(\d+)>', dm_content):
                user_info = await self.fetch_user(int(user_id))
                dm_content = dm_content.replace(f'<@{user_id}>', f'<@{user_id}:{user_info.name}>')

            _LOGGER.info(f'Message from {discord_message.author}: {dm_content}')

            channel_id = str(discord_message.channel.id)
            jeeves_state.channel_messages[channel_id].append({
                'role': 'user',
                'content': f'Message from {discord_message.author}: {dm_content}' })

            # Check if message contains "\bJeeves\b" or "\bJ\b"

            msg = dm_content

            if msg.startswith('!EXIT'):
                await self.close()
                sys.exit(0)
                return

            if msg.startswith('!DEBUG '):
                msg = msg[len('!DEBUG '):]
                debug_mode = True
            else:
                debug_mode = False

            channel_id = str(discord_message.channel.id)
            if channel_id not in jeeves_state.channel_personality:
                jeeves_state.channel_personality[channel_id] = 'Jeeves'

            personality_name = jeeves_state.channel_personality[channel_id]
            personality_name_short = personality_name[0]

            if not re.search(fr'\b{personality_name}\b', msg, re.IGNORECASE) and not re.search(fr'\b{personality_name_short}\b', msg, re.IGNORECASE):
                return

            await jeeves_state.handle_incoming_message(
                client=client, discord_message=discord_message, openai_client=openai_client, tools=tools, debug_mode=debug_mode)


    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.guild_reactions = True
    intents.guilds = True
    intents.messages = True
    intents.reactions = True
    intents.guild_messages = True

    client = MyClient(intents=intents)

    discord.utils.setup_logging()

    await client.start(config.discord_token, reconnect=True)


if __name__ == "__main__":
    import sys, asyncio, os

    if sys.platform.lower() == "win32":
        os.system('color')
        os.system('chcp 65001 > nul')
        sys.stdout.reconfigure(encoding='utf-8') # type: ignore
        sys.stderr.reconfigure(encoding='utf-8') # type: ignore

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
