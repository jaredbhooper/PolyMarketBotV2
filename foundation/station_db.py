"""ICAO -> (lat, lon, timezone) for every airport station Polymarket uses.

These are the *real* coordinates of the airport weather station, not the
city center. Add a new entry whenever the universe expansion (audit_stations
script) flags an unknown ICAO. The values are sourced from public ICAO
records - never guess from the city name. Polymarket markets resolve on
the named station, so if the coord drifts, the forecast/truth pair drifts
with it and the model goes out of calibration.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    icao: str
    name: str
    lat: float
    lon: float
    timezone: str


STATIONS: dict[str, Station] = {
    # ---- Europe ----
    "EGLC": Station("EGLC", "London City Airport",          51.5053,   0.0553, "Europe/London"),
    "LFPB": Station("LFPB", "Paris-Le Bourget",             48.9694,   2.4414, "Europe/Paris"),
    "EHAM": Station("EHAM", "Amsterdam Schiphol",           52.3105,   4.7683, "Europe/Amsterdam"),
    "EFHK": Station("EFHK", "Helsinki Vantaa",              60.3172,  24.9633, "Europe/Helsinki"),
    "LEMD": Station("LEMD", "Madrid Barajas",               40.4719,  -3.5626, "Europe/Madrid"),
    "LIMC": Station("LIMC", "Milan Malpensa",               45.6306,   8.7281, "Europe/Rome"),
    "EDDM": Station("EDDM", "Munich Franz Josef Strauss",   48.3538,  11.7861, "Europe/Berlin"),
    "EPWA": Station("EPWA", "Warsaw Chopin",                52.1657,  20.9671, "Europe/Warsaw"),
    "LTAC": Station("LTAC", "Ankara Esenboga",              40.1281,  32.9951, "Europe/Istanbul"),

    # ---- North America (US) ----
    "KATL": Station("KATL", "Atlanta Hartsfield-Jackson",   33.6407, -84.4277, "America/New_York"),
    "KAUS": Station("KAUS", "Austin-Bergstrom",             30.1945, -97.6699, "America/Chicago"),
    "KORD": Station("KORD", "Chicago O'Hare",               41.9786, -87.9048, "America/Chicago"),
    "KDAL": Station("KDAL", "Dallas Love Field",            32.8471, -96.8518, "America/Chicago"),
    "KBKF": Station("KBKF", "Buckley SFB (Denver)",         39.7017,-104.7517, "America/Denver"),
    "KHOU": Station("KHOU", "Houston Hobby",                29.6454, -95.2789, "America/Chicago"),
    "KLAX": Station("KLAX", "Los Angeles Intl",             33.9425,-118.4081, "America/Los_Angeles"),
    "KMIA": Station("KMIA", "Miami Intl",                   25.7959, -80.2870, "America/New_York"),
    "KLGA": Station("KLGA", "LaGuardia",                    40.7769, -73.8740, "America/New_York"),
    "KSFO": Station("KSFO", "San Francisco Intl",           37.6213,-122.3790, "America/Los_Angeles"),
    "KSEA": Station("KSEA", "Seattle-Tacoma",               47.4502,-122.3088, "America/Los_Angeles"),

    # ---- North America (other) ----
    "CYYZ": Station("CYYZ", "Toronto Pearson",              43.6777, -79.6248, "America/Toronto"),
    "MMMX": Station("MMMX", "Mexico City Benito Juarez",    19.4361, -99.0719, "America/Mexico_City"),
    "MPMG": Station("MPMG", "Panama City Albrook",           8.9733, -79.5556, "America/Panama"),

    # ---- South America ----
    "SAEZ": Station("SAEZ", "Buenos Aires Ezeiza",         -34.8222, -58.5358, "America/Argentina/Buenos_Aires"),
    "SBGR": Station("SBGR", "Sao Paulo Guarulhos",         -23.4356, -46.4731, "America/Sao_Paulo"),

    # ---- Asia (China) ----
    "ZBAA": Station("ZBAA", "Beijing Capital",              40.0801, 116.5846, "Asia/Shanghai"),
    "ZUUU": Station("ZUUU", "Chengdu Shuangliu",            30.5784, 103.9471, "Asia/Shanghai"),
    "ZUCK": Station("ZUCK", "Chongqing Jiangbei",           29.7194, 106.6417, "Asia/Shanghai"),
    "ZGGG": Station("ZGGG", "Guangzhou Baiyun",             23.3924, 113.2988, "Asia/Shanghai"),
    "ZSJN": Station("ZSJN", "Jinan Yaoqiang",               36.8572, 117.2161, "Asia/Shanghai"),
    "ZSQD": Station("ZSQD", "Qingdao Jiaodong",             36.3617, 120.0925, "Asia/Shanghai"),
    "ZSPD": Station("ZSPD", "Shanghai Pudong",              31.1443, 121.8083, "Asia/Shanghai"),
    "ZGSZ": Station("ZGSZ", "Shenzhen Bao'an",              22.6393, 113.8108, "Asia/Shanghai"),
    "ZHHH": Station("ZHHH", "Wuhan Tianhe",                 30.7838, 114.2081, "Asia/Shanghai"),
    "ZHCC": Station("ZHCC", "Zhengzhou Xinzheng",           34.5197, 113.8408, "Asia/Shanghai"),

    # ---- Asia (Japan / Korea) ----
    "RJTT": Station("RJTT", "Tokyo Haneda",                 35.5494, 139.7798, "Asia/Tokyo"),
    "RKSI": Station("RKSI", "Seoul Incheon",                37.4602, 126.4407, "Asia/Seoul"),
    "RKPK": Station("RKPK", "Busan Gimhae",                 35.1795, 128.9382, "Asia/Seoul"),

    # ---- Asia (SE / South Asia) ----
    "WSSS": Station("WSSS", "Singapore Changi",              1.3644, 103.9915, "Asia/Singapore"),
    "WMKK": Station("WMKK", "Kuala Lumpur Intl",             2.7456, 101.7099, "Asia/Kuala_Lumpur"),
    "RPLL": Station("RPLL", "Manila Ninoy Aquino",          14.5086, 121.0194, "Asia/Manila"),
    "RCSS": Station("RCSS", "Taipei Songshan",              25.0697, 121.5519, "Asia/Taipei"),
    "VILK": Station("VILK", "Lucknow Chaudhary Charan Singh", 26.7606, 80.8893, "Asia/Kolkata"),
    "OPKC": Station("OPKC", "Karachi Jinnah",               24.9065,  67.1608, "Asia/Karachi"),
    "OEJN": Station("OEJN", "Jeddah King Abdulaziz",        21.6796,  39.1565, "Asia/Riyadh"),

    # ---- Africa ----
    "FACT": Station("FACT", "Cape Town Intl",              -33.9648,  18.6017, "Africa/Johannesburg"),

    # ---- Oceania ----
    "NZWN": Station("NZWN", "Wellington Intl",             -41.3272, 174.8053, "Pacific/Auckland"),
}


def lookup(icao: str) -> Station | None:
    return STATIONS.get(icao.upper())
