# Today's date
import datetime

import pytz
import timezonefinder

from servant.defs import GlobalContext, ToolDef
from typed_json import JSON, JSONDict, coerce_float_strict

tf = timezonefinder.TimezoneFinder()


async def get_current_datetime(longitude: float, latitude: float) -> JSONDict:
    """
    Get the current date and time in New York.
    """

    tzname = tf.certain_timezone_at(lat=latitude, lng=longitude)
    if not tzname:
        raise ValueError("Could not determine timezone for the given coordinates.")
    # Get the current date and time in New York timezone
    # Ensure the timezone is valid

    timezone = pytz.timezone(tzname)
    dt = datetime.datetime.now(timezone)

    date_str = dt.strftime("%A, %B %d, %Y")
    time_str = dt.strftime("%H:%M:%S")
    return {"date": date_str, "time": time_str, "timezone": tzname}


def _extract_location(obj: JSON) -> tuple[float, float]:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")
    location = obj.get("location")
    if not isinstance(location, dict):
        raise ValueError("location must be an object.")
    longitude = coerce_float_strict(location.get("longitude"), "longitude")
    latitude = coerce_float_strict(location.get("latitude"), "latitude")
    return longitude, latitude


async def _get_current_datetime_tool(_ctx: GlobalContext, obj: JSON) -> JSONDict:
    longitude, latitude = _extract_location(obj)
    return await get_current_datetime(longitude, latitude)


get_current_weather_schema: ToolDef = ToolDef(
    name="get_current_datetime",
    function=_get_current_datetime_tool,
    schema={
        "name": "get_current_datetime",
        "description": "Get the current date & time in a given location. Specify the location as precisely as possible.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "object",
                    "description": "The location that will be used to get the current date and time.",
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
