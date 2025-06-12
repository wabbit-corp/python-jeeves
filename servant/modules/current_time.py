from servant.json import JSONDict, obj_to_json
from servant.defs import ToolDef

# Today's date
import datetime
import timezonefinder, pytz
from tzwhere import tzwhere

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


get_current_weather_schema: ToolDef = ToolDef(
    name="get_current_datetime",
    function=lambda ctx, obj: get_current_datetime(
        obj["location"]["latitude"], obj["location"]["longitude"]
    ),
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
