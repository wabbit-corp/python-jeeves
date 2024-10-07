from typing import Tuple, Optional
from dataclasses import dataclass

import time
import json
import asyncio

from servant.base.rate_limiting import SimpleRateLimiter
from servant.base.tools import ToolDef
from servant.base.install import install_package

install_package(pip_package_name='geopy', module_name='geopy')
import geopy.distance

install_package(pip_package_name='geocoder', module_name='geocoder')
import geocoder

install_package(pip_package_name='requests', module_name='requests')
import requests


@dataclass
class GeocoderResult:
    location: str
    longitude: float
    latitude: float
    country: str
    state: str
    city: str
    street: str


_GEOCODER_RATE_LIMITER = SimpleRateLimiter(1.0)
async def geocode(location: str) -> Optional[GeocoderResult]:
    await _GEOCODER_RATE_LIMITER()
    g = geocoder.osm(location)
    lat, lng = g.latlng
    # return g.x, g.y
    return GeocoderResult(
        location=location, longitude=lng, latitude=lat,
        country=g.country, state=g.state, city=g.city,
        street=g.street)


_DRIVING_DISTANCE_RATE_LIMITER = SimpleRateLimiter(1.0)
async def driving_distance(p1: GeocoderResult, p2: GeocoderResult) -> Optional[float]:
    await _DRIVING_DISTANCE_RATE_LIMITER()
    url = 'http://router.project-osrm.org/route/v1/driving/'

    # LONGITUDE FIRST
    o1 = str(p1.longitude) + ',' + str(p1.latitude)
    o2 = str(p2.longitude) + ',' + str(p2.latitude)
    x = o1 + ';' + o2

    response = requests.get(url+x)
    data = json.loads(response.content)

    if response.status_code == 200:
        return data['routes'][0]['distance']*0.001 #in km
    else:
        return None


async def distance(p1, p2):
    if isinstance(p1, str):
        g = geocoder.osm(p1)
        print('%s resolved to %s (long=%s lat=%s)' % (p1, g, g.x, g.y))
        p1 = (g.x, g.y)

    if isinstance(p2, str):
        g = geocoder.osm(p2)
        print('%s resolved to %s (long=%s lat=%s)' % (p2, g, g.x, g.y))
        p2 = (g.x, g.y)

    geodesic_km = geopy.distance.geodesic(reversed(p1), reversed(p2)).km
    driving_km = driving_distance(p1, p2)
    return geodesic_km, driving_km

# print(distance('Milwaukee', 'Toronto'))
