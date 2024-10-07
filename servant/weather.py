import aiohttp
import asyncio
from typing import Optional
from dataclasses import dataclass, field
from typing import List
from servant.base.tools import ToolDef, ToolDispatcher
from servant.base.json import JSON, JSONDict, obj_to_json

# https://open-meteo.com/

from servant.geo import geocode


@dataclass
class CurrentWeather:
    time: str
    temperature_2m: float
    wind_speed_10m: float


@dataclass
class HourlyWeather:
    time: List[str]
    wind_speed_10m: List[float]
    temperature_2m: List[float]
    relative_humidity_2m: List[float]


@dataclass
class WeatherForecast:
    current: CurrentWeather
    hourly: HourlyWeather


async def fetch_weather_forecast(latitude: float, longitude: float) -> JSONDict:
    api_url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}"
    api_url += f"&current=temperature_2m,apparent_temperature,is_day,precipitation,rain,showers,snowfall,cloud_cover,wind_speed_10m,wind_gusts_10m"

    async with aiohttp.ClientSession() as session:
        async with session.get(api_url) as response:
            r = await response.json()

            # {
            #     "latitude": 43.646603,
            #     "longitude": -79.38269,
            #     "generationtime_ms": 0.0940561294555664,
            #     "utc_offset_seconds": 0,
            #     "timezone": "GMT",
            #     "timezone_abbreviation": "GMT",
            #     "elevation": 97.0,
            #     "current_units": {
            #     "time": "iso8601",
            #     "interval": "seconds",
            #     "temperature_2m": "\u00b0C",
            #     "apparent_temperature": "\u00b0C",
            #     "is_day": "",
            #     "precipitation": "mm",
            #     "rain": "mm",
            #     "showers": "mm",
            #     "snowfall": "cm",
            #     "cloud_cover": "%",
            #     "wind_speed_10m": "km/h",
            #     "wind_gusts_10m": "km/h"
            #     },
            #     "current": {
            #     "time": "2024-03-21T20:30",
            #     "interval": 900,
            #     "temperature_2m": -1.5,
            #     "apparent_temperature": -8.2,
            #     "is_day": 1,
            #     "precipitation": 0.0,
            #     "rain": 0.0,
            #     "showers": 0.0,
            #     "snowfall": 0.0,
            #     "cloud_cover": 100,
            #     "wind_speed_10m": 21.9,
            #     "wind_gusts_10m": 34.9
            #     }
            # }

            result = {
                'latitude': r['latitude'],
                'longitude': r['longitude'],
                'temperature_2m': str(r['current']['temperature_2m']) + ' ' + r['current_units']['temperature_2m'],
                'apparent_temperature': str(r['current']['apparent_temperature']) + ' ' + r['current_units']['apparent_temperature'],
                'is_day': True if r['current']['is_day'] == 1 else False,
                'precipitation': str(r['current']['precipitation']) + ' ' + r['current_units']['precipitation'],
                'rain': str(r['current']['rain']) + ' ' + r['current_units']['rain'],
                'showers': str(r['current']['showers']) + ' ' + r['current_units']['showers'],
                'snowfall': str(r['current']['snowfall']) + ' ' + r['current_units']['snowfall'],
                'cloud_cover': str(r['current']['cloud_cover']) + ' ' + r['current_units']['cloud_cover'],
                'wind_speed_10m': str(r['current']['wind_speed_10m']) + ' ' + r['current_units']['wind_speed_10m'],
                'wind_gusts_10m': str(r['current']['wind_gusts_10m']) + ' ' + r['current_units']['wind_gusts_10m']
            }

            return result


async def get_current_weather(latitude: float, longitude: float) -> JSON:
    # g = await geocode(location)
    # if g is None:
    #     return {'error': 'Could not find geocode for location', 'data': {'location': location}}
    r = await fetch_weather_forecast(latitude, longitude)
    r['location'] = obj_to_json({'latitude': latitude, 'longitude': longitude})
    return r



def register_tools(tools: ToolDispatcher) -> None:
    tools.register(
        name="get_current_weather",
        schema={
            "type": "function",
            "function": {
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
                                    "description": "The latitude of the location."
                                },
                                "longitude": {
                                    "type": "number",
                                    "description": "The longitude of the location."
                                }
                            },
                            "required": ["latitude", "longitude"]
                        }
                    },
                    "required": ["location"],
                },
            },
        },
        function=lambda obj: get_current_weather(obj['location']['latitude'], obj['location']['longitude'])
    )
