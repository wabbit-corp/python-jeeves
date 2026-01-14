import requests
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views import View


class Home(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, "home.html")


class Stats(View):
    _context: dict[str, object] = {}
    # FIXME this is a quick hack to obtain locally contrasting colors.
    #  better separation can be obtained systematically with complementary colors. Use them instead.
    _color_palette = [
        "#962319",
        "#689C98",
        "#DF763D",
        "#EBCB77",
        "#9ECD7D",
        "#68B58C",
        "#C9462C",
        "#587F8F",
        "#E1974A",
        "#7550AB",
    ]

    @staticmethod
    def _get_conversations(community: dict[str, object]) -> dict[str, object]:
        conversations_dict: dict[str, object] = {"channels": []}
        channels_list: list[dict[str, object]] = []
        conversations_dict["channels"] = channels_list

        channels_value = community.get("channels")
        if not isinstance(channels_value, list):
            return conversations_dict

        for channel in channels_value:
            if not isinstance(channel, dict):
                continue
            labels: list[int] = []
            conversations: list[dict[str, object]] = []
            channels_list.append({"conversations": conversations, "labels": labels})

            messages_value = channel.get("messages")
            if not isinstance(messages_value, list):
                continue
            time_sorted_messages = sorted(
                (msg for msg in messages_value if isinstance(msg, dict)),
                key=lambda msg: int(str(msg.get("timestamp", 0))),
            )
            channel["time_sorted_messages"] = time_sorted_messages
            for message in time_sorted_messages:
                conversation_value = message.get("conversationId")
                if isinstance(conversation_value, str):
                    conversation_id = int(conversation_value.replace("T", ""))
                elif isinstance(conversation_value, int):
                    conversation_id = conversation_value
                else:
                    continue

                if conversation_id in labels:
                    entry = conversations[conversation_id - 1]
                    entry_messages = entry.get("messages")
                    if isinstance(entry_messages, list):
                        entry_messages.append(message)
                else:
                    conversations.append({"messages": [message]})
                    labels.append(conversation_id)

        return conversations_dict

    def get(self, request: HttpRequest, *args: object, **kwargs: str) -> HttpResponse:
        response = requests.get(f'http://127.0.0.1:8000/api/statistics/{kwargs["operation"]}')

        if response.status_code == 200:
            community_obj = response.json()
            if not isinstance(community_obj, dict):
                self._context["status"] = response.status_code
                self._context["type"] = kwargs["operation"]
                return render(request, "stats.html", context={"context": self._context})

            community: dict[str, object] = community_obj

            # Assign a color for each conversation + postprocessing
            channels_value = community.get("channels")
            if isinstance(channels_value, list):
                for channels in channels_value:
                    if not isinstance(channels, dict):
                        continue
                    messages = channels.get("messages")
                    if not isinstance(messages, list):
                        continue
                    for message in messages:
                        if not isinstance(message, dict):
                            continue
                        conversation_id = int(str(message.get("conversationId", 0)).replace("T", ""))
                        message["conversationId"] = conversation_id
                        message["color"] = self._color_palette[conversation_id % len(self._color_palette)]

            if community["type"] == "validation":
                gold_messages = {}

                # Assign a color for each conversation + postprocessing
                gold = community.get("gold")
                gold_channels = gold.get("channels") if isinstance(gold, dict) else None
                if isinstance(gold_channels, list):
                    for channels in gold_channels:
                        if not isinstance(channels, dict):
                            continue
                        messages = channels.get("messages")
                        if not isinstance(messages, list):
                            continue
                        for message in messages:
                            if not isinstance(message, dict):
                                continue
                            conversation_id = int(str(message.get("conversationId", 0)).replace("T", ""))
                            message["conversationId"] = conversation_id
                            message["color"] = self._color_palette[conversation_id % len(self._color_palette)]

                # Cluster the messages based on their conversation label
                for key, channel in enumerate(gold_channels or []):
                    conversations: dict[int, list[dict[str, object]]] = {}
                    if not isinstance(channel, dict):
                        continue

                    messages = channel.get("messages")
                    if not isinstance(messages, list):
                        continue
                    for message in messages:
                        if not isinstance(message, dict):
                            continue
                        conv_id = message.get("conversationId")
                        if not isinstance(conv_id, int):
                            continue
                        if conv_id in conversations:
                            conversations[conv_id].append(message)
                        else:
                            conversations[conv_id] = [message]

                    gold_messages[key] = conversations

                self._context["gold"] = gold_messages

                ordered_channels: list[list[list[dict[str, object]]]] = []
                for channel_conversations in gold_messages.values():
                    channel_groups: list[list[dict[str, object]]] = []
                    for key in sorted(channel_conversations):
                        channel_groups.append(channel_conversations[key])
                    ordered_channels.append(channel_groups)

                self._context["gold"] = ordered_channels

            self._context["community"] = community
            self._context["conversations"] = self._get_conversations(community)
            self._context["statistics"] = community.get("statistics")
            community_type = community.get("type")
            if isinstance(community_type, str):
                self._context["type"] = community_type.title()
                self._context["type_raw"] = community_type
            self._context["status"] = 200
        elif response.status_code == 500:
            self._context["status"] = 500
            self._context["type"] = kwargs["operation"]

        return render(request, "stats.html", context={"context": self._context})


class Annotate(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, "annotate.html")
