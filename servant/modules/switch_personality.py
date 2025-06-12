# async def switch_personality(discord_message: discord.Message, personality: str) -> JSONDict:
#     channel_id = str(discord_message.channel.id)
#     if personality not in config.personalities:
#         return { 'error': f'Personality "{personality}" not found.' }
#     jeeves_state.channel_personality[channel_id] = personality
#     return { 'message': f'Personality switched to "{personality}".' }

# tools.register(
#     name='switch_personality',
#     schema={
#         'type': 'function',
#         'function': {
#             'name': 'switch_personality',
#             'description': 'Change your own personality for the current channel.',
#             'parameters': {
#                 'type': 'object',
#                 'properties': {
#                     'personality': {
#                         'type': 'string',
#                         # FIXME
#                         'description': 'The name of the personality to switch to. Available personalities: Jeeves, Fumiko, Dio Brando.'
#                     }
#                 },
#                 'required': ['personality']
#             }
#         }
#     },
#     function=lambda obj: switch_personality(obj['discord_message'], obj['personality'])
# )
