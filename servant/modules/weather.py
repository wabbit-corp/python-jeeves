import aiohttp
import asyncio
from typing import Optional
from dataclasses import dataclass, field
from typing import List
from servant.defs import ToolDef, GlobalContext
from servant.json import JSONDict, obj_to_json

# https://open-meteo.com/

MODULE_PROMPT = f"""
## Weather
You can also use the `get_current_weather` command to get the current weather in a location.
"""


async def fetch_weather_forecast(latitude: float, longitude: float) -> JSONDict:
    api_url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}"
    api_url += f"&current=temperature_2m,apparent_temperature,is_day,precipitation,rain,showers,snowfall,cloud_cover,wind_speed_10m,wind_gusts_10m"

    async with aiohttp.ClientSession() as session:
        async with session.get(api_url) as response:
            r = await response.json()

            result = {
                "latitude": r["latitude"],
                "longitude": r["longitude"],
                "temperature_2m": str(r["current"]["temperature_2m"])
                + " "
                + r["current_units"]["temperature_2m"],
                "apparent_temperature": str(r["current"]["apparent_temperature"])
                + " "
                + r["current_units"]["apparent_temperature"],
                "is_day": True if r["current"]["is_day"] == 1 else False,
                "precipitation": str(r["current"]["precipitation"])
                + " "
                + r["current_units"]["precipitation"],
                "rain": str(r["current"]["rain"]) + " " + r["current_units"]["rain"],
                "showers": str(r["current"]["showers"])
                + " "
                + r["current_units"]["showers"],
                "snowfall": str(r["current"]["snowfall"])
                + " "
                + r["current_units"]["snowfall"],
                "cloud_cover": str(r["current"]["cloud_cover"])
                + " "
                + r["current_units"]["cloud_cover"],
                "wind_speed_10m": str(r["current"]["wind_speed_10m"])
                + " "
                + r["current_units"]["wind_speed_10m"],
                "wind_gusts_10m": str(r["current"]["wind_gusts_10m"])
                + " "
                + r["current_units"]["wind_gusts_10m"],
            }

            return result


async def get_current_weather(latitude: float, longitude: float) -> JSONDict:
    # g = await geocode(location)
    # if g is None:
    #     return {'error': 'Could not find geocode for location', 'data': {'location': location}}
    r = await fetch_weather_forecast(latitude, longitude)
    r["location"] = obj_to_json({"latitude": latitude, "longitude": longitude})
    return r


get_current_weather_schema: ToolDef = ToolDef(
    name="get_current_weather",
    function=lambda ctx, obj: get_current_weather(
        obj["location"]["latitude"], obj["location"]["longitude"]
    ),
    schema={
        "name": "get_current_weather",
        "description": "Get the current weather in a given location. Specify the location as precisely as possible.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "object",
                    "description": "The location that will be used to get the weather forecast.",
                    "properties": {
                        "latitude": {
                            "type": "number",
                            "description": "The latitude of the location.",
                        },
                        "longitude": {
                            "type": "number",
                            "description": "The longitude of the location.",
                        },
                    },
                    "required": ["latitude", "longitude"],
                }
            },
            "required": ["location"],
        },
    },
)
