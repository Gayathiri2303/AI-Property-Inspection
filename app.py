import os
import sys
import json
import math
import hashlib
import base64
import logging
import requests
from io import BytesIO
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory

# ─── Logging (critical for debugging on cPanel) ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ─── Config / Secrets ───
try:
    import config
except ImportError:
    config = None

def get_setting(name):
    val = os.environ.get(name)
    if val:
        return val
    if config is not None:
        return getattr(config, name, "")
    return ""

app = Flask(__name__, static_folder="static", static_url_path="")
application = app  # WSGI alias for cPanel Passenger

IMAGGA_API_KEY = get_setting("IMAGGA_API_KEY")
IMAGGA_API_SECRET = get_setting("IMAGGA_API_SECRET")
OCRSPACE_API_KEY = get_setting("OCRSPACE_API_KEY")
GEMINI_API_KEY = get_setting("GEMINI_API_KEY")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "log2-property-inspection/1.0"}

# ─── Optional Libraries ───
try:
    from PIL import Image, ExifTags
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False
    logger.warning("Pillow not installed. Photo analysis will be limited.")

try:
    import imagehash
    IMAGEHASH_AVAILABLE = True
except ImportError:
    IMAGEHASH_AVAILABLE = False
    logger.warning("imagehash not installed. Visual comparison disabled.")

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def dms_to_decimal(dms, ref):
    decimal = float(dms[0]) + float(dms[1]) / 60 + float(dms[2]) / 3600
    if ref in ['S', 'W']:
        decimal = -decimal
    return decimal


def extract_gps_coords(image):
    """Robust GPS extraction supporting Pillow 9.x/10.x."""
    gps_coords = None
    exif_dict = None

    # Legacy API (most reliable for raw GPS dict access)
    try:
        exif_dict = image._getexif()
    except Exception:
        pass

    if not exif_dict:
        return None

    gps_info = None
    if isinstance(exif_dict, dict):
        for tag_id, value in exif_dict.items():
            tag_name = ExifTags.TAGS.get(tag_id, tag_id)
            if tag_name == "GPSInfo" and isinstance(value, dict):
                gps_info = value
                break

    if not gps_info:
        return None

    try:
        lat = dms_to_decimal(gps_info[2], gps_info[1])
        lon = dms_to_decimal(gps_info[4], gps_info[3])
        gps_coords = {"lat": round(lat, 6), "lng": round(lon, 6)}
    except Exception as e:
        logger.warning(f"GPS parsing failed: {e}")

    return gps_coords


def get_color_signature(file_bytes):
    try:
        image = Image.open(BytesIO(file_bytes)).convert('RGB').resize((8, 8))
        pixels = list(image.getdata())
        avg_r = sum(p[0] for p in pixels) / 64.0
        avg_g = sum(p[1] for p in pixels) / 64.0
        avg_b = sum(p[2] for p in pixels) / 64.0
        return (avg_r, avg_g, avg_b)
    except Exception:
        return None


def color_distance(c1, c2):
    return math.sqrt((c1[0]-c2[0])**2 + (c1[1]-c2[1])**2 + (c1[2]-c2[2])**2)


def hamming_to_real_estate_similarity(avg_h, max_h):
    if avg_h is None:
        return None
    if avg_h <= 12:
        return 0.98
    elif avg_h <= 18:
        return 0.90
    elif avg_h <= 24:
        return 0.82
    elif avg_h <= 30:
        return 0.72
    elif avg_h <= 36:
        return 0.60
    elif avg_h <= 42:
        return 0.45
    elif avg_h <= 50:
        return 0.25
    else:
        return max(0.0, 1.0 - (avg_h / 64.0))


# ─────────────────────────────────────────────
# GEOCODING
# ─────────────────────────────────────────────

def census_geocode(address):
    try:
        url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
        params = {"address": address, "benchmark": "4", "format": "json"}
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        matches = data.get("result", {}).get("addressMatches", [])
        if not matches:
            return None, "Address not found in US Census database."

        match = matches[0]
        coords = match.get("coordinates", {})
        return {
            "lat": float(coords.get("y")),
            "lng": float(coords.get("x")),
            "display_name": match.get("matchedAddress", address),
            "osm_id": None,
            "osm_type": None,
            "address_details": {},
        }, None
    except Exception as e:
        return None, f"Census geocoding failed: {e}"


def nominatim_geocode(address):
    try:
        r = requests.get(
            NOMINATIM_URL,
            params={"q": address, "format": "json", "limit": 1, "addressdetails": 1, "countrycodes": "us"},
            headers=HEADERS,
            timeout=15,
        )
        data = r.json()
        if data:
            return {
                "lat": float(data[0]["lat"]),
                "lng": float(data[0]["lon"]),
                "display_name": data[0]["display_name"],
                "osm_id": data[0].get("osm_id"),
                "osm_type": data[0].get("osm_type"),
                "address_details": data[0].get("address", {}),
            }, None
    except Exception as e:
        logger.warning(f"Nominatim failed: {e}")
    return census_geocode(address)


# ─────────────────────────────────────────────
# OSM PROPERTY DETAILS
# ─────────────────────────────────────────────

def get_osm_property_details(lat, lon, osm_id=None, osm_type=None):
    details = {
        "class": "unknown", "type": "unknown", "is_residential": False,
        "building_type": "Unknown", "confidence": "low"
    }
    try:
        if osm_id and osm_type:
            short = "way" if osm_type == "W" else ("node" if osm_type == "N" else "relation")
            query = f"[out:json][timeout:15];{short}(id:{osm_id});out body;>;out skel qt;"
        else:
            query = f"""
            [out:json][timeout:15];
            (way["building"](around:50,{lat},{lon});way["place"](around:50,{lat},{lon}););
            out body;>;out skel qt;
            """
        r = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=20)
        data = r.json()
        buildings = []
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            if tags.get("building"):
                buildings.append(tags["building"])
            if tags.get("place"):
                details["type"] = tags["place"]
        if buildings:
            from collections import Counter
            mc = Counter(buildings).most_common(1)[0][0]
            details["building_type"] = mc.replace("_", " ").title()
            details["class"] = "building"
            res = {"house", "residential", "apartments", "detached", "semidetached_house",
                   "terrace", "bungalow", "cabin", "static_caravan", "townhouse", "duplex"}
            if mc in res:
                details["is_residential"] = True
                details["confidence"] = "high"
            elif mc in {"commercial", "retail", "industrial", "warehouse", "office"}:
                details["is_residential"] = False
                details["confidence"] = "high"
            else:
                details["confidence"] = "medium"
        elif details["type"] in {"house", "residential", "apartments", "suburb", "neighbourhood"}:
            details["is_residential"] = True
            details["confidence"] = "medium"
    except Exception as e:
        logger.warning(f"OSM property details failed: {e}")
    return details


# ─────────────────────────────────────────────
# OVERPASS NEARBY FEATURES
# ─────────────────────────────────────────────

def overpass_nearby_features(lat, lon):
    radius = 3000
    query = f"""
    [out:json][timeout:25];
    (
      way["highway"~"motorway|trunk|primary|secondary"](around:{radius},{lat},{lon});
      way["railway"~"rail|subway|station"](around:{radius},{lat},{lon});
      node["aeroway"="aerodrome"](around:{radius*2},{lat},{lon});
      way["leisure"="golf_course"](around:{radius},{lat},{lon});
      way["natural"="water"](around:{radius},{lat},{lon});
      relation["natural"="water"](around:{radius},{lat},{lon});
      way["landuse"="industrial"](around:{radius},{lat},{lon});
      way["leisure"="park"](around:{radius},{lat},{lon});
      way["barrier"="gate"](around:500,{lat},{lon});
      way["power"~"line|cable|minor_line"](around:500,{lat},{lon});
      way["landuse"="landfill"](around:{radius},{lat},{lon});
      node["landuse"="landfill"](around:{radius},{lat},{lon});
      way["landuse"="commercial"](around:{radius},{lat},{lon});
      way["landuse"="retail"](around:{radius},{lat},{lon});
      way["tourism"="resort"](around:{radius},{lat},{lon});
      node["tourism"="resort"](around:{radius},{lat},{lon});
      node["natural"="peak"](around:{radius*2},{lat},{lon});
      node["natural"="volcano"](around:{radius*2},{lat},{lon});
      way["building"="vacant"](around:500,{lat},{lon});
      way["building"="abandoned"](around:500,{lat},{lon});
    );
    out center;
    """
    results = {
        "highway": {"count": 0, "min_dist": None},
        "railroad": {"count": 0, "min_dist": None},
        "airport": {"count": 0, "min_dist": None},
        "golf_course": {"count": 0, "min_dist": None},
        "waterfront": {"count": 0, "min_dist": None},
        "industrial": {"count": 0, "min_dist": None},
        "park": {"count": 0, "min_dist": None},
        "gated": {"count": 0, "min_dist": None},
        "power_lines": {"count": 0, "min_dist": None},
        "landfill": {"count": 0, "min_dist": None},
        "commercial": {"count": 0, "min_dist": None},
        "resort": {"count": 0, "min_dist": None},
        "mountain": {"count": 0, "min_dist": None},
        "vacant_building": {"count": 0, "min_dist": None},
    }
    try:
        r = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=25)
        data = r.json()
        for el in data.get("elements", []):
            if "center" in el:
                elat, elon = el["center"]["lat"], el["center"]["lon"]
            elif "lat" in el:
                elat, elon = el["lat"], el["lon"]
            else:
                continue
            dist = haversine(lat, lon, elat, elon)
            tags = el.get("tags", {})
            if "highway" in tags and tags["highway"] in ("motorway", "trunk", "primary", "secondary"):
                results["highway"]["count"] += 1
                if results["highway"]["min_dist"] is None or dist < results["highway"]["min_dist"]:
                    results["highway"]["min_dist"] = round(dist)
            elif "railway" in tags:
                results["railroad"]["count"] += 1
                if results["railroad"]["min_dist"] is None or dist < results["railroad"]["min_dist"]:
                    results["railroad"]["min_dist"] = round(dist)
            elif "aeroway" in tags and tags["aeroway"] == "aerodrome":
                results["airport"]["count"] += 1
                if results["airport"]["min_dist"] is None or dist < results["airport"]["min_dist"]:
                    results["airport"]["min_dist"] = round(dist)
            elif tags.get("leisure") == "golf_course":
                results["golf_course"]["count"] += 1
                if results["golf_course"]["min_dist"] is None or dist < results["golf_course"]["min_dist"]:
                    results["golf_course"]["min_dist"] = round(dist)
            elif tags.get("natural") == "water" or tags.get("water"):
                results["waterfront"]["count"] += 1
                if results["waterfront"]["min_dist"] is None or dist < results["waterfront"]["min_dist"]:
                    results["waterfront"]["min_dist"] = round(dist)
            elif tags.get("landuse") == "industrial":
                results["industrial"]["count"] += 1
                if results["industrial"]["min_dist"] is None or dist < results["industrial"]["min_dist"]:
                    results["industrial"]["min_dist"] = round(dist)
            elif tags.get("leisure") == "park":
                results["park"]["count"] += 1
                if results["park"]["min_dist"] is None or dist < results["park"]["min_dist"]:
                    results["park"]["min_dist"] = round(dist)
            elif tags.get("barrier") == "gate":
                results["gated"]["count"] += 1
                if results["gated"]["min_dist"] is None or dist < results["gated"]["min_dist"]:
                    results["gated"]["min_dist"] = round(dist)
            elif "power" in tags and tags["power"] in ("line", "cable", "minor_line"):
                results["power_lines"]["count"] += 1
                if results["power_lines"]["min_dist"] is None or dist < results["power_lines"]["min_dist"]:
                    results["power_lines"]["min_dist"] = round(dist)
            elif tags.get("landuse") == "landfill":
                results["landfill"]["count"] += 1
                if results["landfill"]["min_dist"] is None or dist < results["landfill"]["min_dist"]:
                    results["landfill"]["min_dist"] = round(dist)
            elif tags.get("landuse") in ("commercial", "retail"):
                results["commercial"]["count"] += 1
                if results["commercial"]["min_dist"] is None or dist < results["commercial"]["min_dist"]:
                    results["commercial"]["min_dist"] = round(dist)
            elif tags.get("tourism") == "resort":
                results["resort"]["count"] += 1
                if results["resort"]["min_dist"] is None or dist < results["resort"]["min_dist"]:
                    results["resort"]["min_dist"] = round(dist)
            elif tags.get("natural") in ("peak", "volcano"):
                results["mountain"]["count"] += 1
                if results["mountain"]["min_dist"] is None or dist < results["mountain"]["min_dist"]:
                    results["mountain"]["min_dist"] = round(dist)
            elif tags.get("building") in ("vacant", "abandoned"):
                results["vacant_building"]["count"] += 1
                if results["vacant_building"]["min_dist"] is None or dist < results["vacant_building"]["min_dist"]:
                    results["vacant_building"]["min_dist"] = round(dist)
    except Exception as e:
        logger.error(f"Overpass error: {e}")
        return results, str(e)
    return results, None


def classify_area_type(address_details, place_results):
    place = address_details.get("place", "").lower()
    if place in ("city", "town", "borough"):
        return "Urban"
    if place in ("suburb", "village"):
        return "Suburban"
    if place in ("hamlet", "isolated_dwelling", "farm"):
        return "Rural"
    total = sum(v["count"] for v in place_results.values())
    if total >= 15:
        return "Urban"
    if total >= 5:
        return "Suburban"
    return "Suburban"


# ─────────────────────────────────────────────
# CROSS-PHOTO CONSISTENCY
# ─────────────────────────────────────────────

def analyze_cross_photo_consistency(processed_images):
    results = {
        "same_property_likely": "unknown",
        "gps_variance_m": None,
        "visual_similarity_max": None,
        "warnings": [],
        "details": [],
        "photo_count": len(processed_images),
    }

    gps_points = []
    for img in processed_images:
        if img.get("gps"):
            gps_points.append(img["gps"])

    if len(gps_points) >= 2:
        max_dist = 0
        for i in range(len(gps_points)):
            for j in range(i + 1, len(gps_points)):
                d = haversine(gps_points[i]["lat"], gps_points[i]["lng"],
                              gps_points[j]["lat"], gps_points[j]["lng"])
                max_dist = max(max_dist, d)

        results["gps_variance_m"] = round(max_dist)

        if max_dist > 500:
            results["same_property_likely"] = "no"
            results["warnings"].append(
                f"🚨 DIFFERENT PROPERTIES DETECTED: Photo GPS locations are {int(max_dist)} meters apart. "
                f"These photos were taken at different locations."
            )
        elif max_dist > 100:
            results["same_property_likely"] = "maybe"
            results["warnings"].append(
                f"⚠️ GPS locations vary by {int(max_dist)}m. This may indicate different properties "
                f"or different sides of a large property."
            )
        else:
            results["same_property_likely"] = "yes"
            results["details"].append(
                f"✅ GPS consistent: All photos within {int(max_dist)}m — same property confirmed."
            )
    elif len(gps_points) == 1:
        results["details"].append("ℹ️ Only 1 photo has GPS metadata. Cannot verify consistency across photos.")
    else:
        results["warnings"].append(
            "ℹ️ No GPS metadata found in any photo. Cannot automatically verify all photos are from the same property. "
            "Please use visual comparison below."
        )

    if IMAGEHASH_AVAILABLE and len(processed_images) >= 2:
        hashes = [img.get("phash") for img in processed_images if img.get("phash")]
        if len(hashes) >= 2:
            max_hamming = 0
            for i in range(len(hashes)):
                for j in range(i + 1, len(hashes)):
                    h1 = imagehash.hex_to_hash(hashes[i])
                    h2 = imagehash.hex_to_hash(hashes[j])
                    dist = h1 - h2
                    max_hamming = max(max_hamming, dist)

            results["visual_similarity_max"] = max_hamming

            if max_hamming > 45:
                results["warnings"].append(
                    f"🚨 Photos are visually VERY different (similarity score: {max_hamming}). "
                    f"They may be from completely different properties."
                )
                if results["same_property_likely"] == "unknown":
                    results["same_property_likely"] = "no"
            elif max_hamming > 32:
                results["warnings"].append(
                    f"⚠️ Photos show moderate visual differences (score: {max_hamming}). "
                    f"Likely different angles of the same property, but verify."
                )
            else:
                results["details"].append(
                    f"✅ Photos are visually consistent (score: {max_hamming}) — likely same property."
                )

    return results


# ─────────────────────────────────────────────
# SINGLE-PASS IMAGE PROCESSING
# ─────────────────────────────────────────────

def process_all_images(images):
    photo_auth_results = []
    processed_images = []
    all_objects = []
    ocr_text = ""
    api_errors = []

    image_data = []
    for img in images:
        try:
            img.seek(0)
            file_bytes = img.read()
            img.seek(0)
            image_data.append({
                "filename": getattr(img, 'filename', 'unknown'),
                "bytes": file_bytes,
                "size_kb": len(file_bytes) / 1024
            })
        except Exception as e:
            logger.error(f"Reading uploaded file failed: {e}")
            continue

    for data in image_data:
        result = {"filename": data["filename"], "suspicious_score": 0, "warnings": [], "positives": []}
        proc = {"filename": data["filename"], "gps": None, "phash": None}

        if PILLOW_AVAILABLE:
            try:
                image = Image.open(BytesIO(data["bytes"]))
                
                # EXIF / GPS extraction
                gps_coords = extract_gps_coords(image)
                if gps_coords:
                    result["positives"].append(f"GPS embedded: {gps_coords['lat']:.5f}, {gps_coords['lng']:.5f}")
                    proc["gps"] = gps_coords

                # Legacy EXIF for other tags
                exif_dict = None
                try:
                    exif_dict = image._getexif()
                except Exception:
                    pass

                if exif_dict:
                    result["positives"].append("Has EXIF metadata (likely from real camera)")
                    for tag_id, value in exif_dict.items():
                        tag = ExifTags.TAGS.get(tag_id, tag_id)
                        if tag == "DateTimeOriginal":
                            result["positives"].append(f"Photo taken: {value}")
                        elif tag == "Make":
                            result["positives"].append(f"Camera: {value}")
                else:
                    result["warnings"].append("No EXIF metadata — could be a screenshot or downloaded image")
                    result["suspicious_score"] += 25

                # Perceptual hash
                if IMAGEHASH_AVAILABLE:
                    try:
                        phash = str(imagehash.phash(image))
                        result["perceptual_hash"] = phash
                        proc["phash"] = phash
                    except Exception:
                        result["perceptual_hash"] = None

                # Resolution check
                width, height = image.size
                mp = (width * height) / 1000000
                if mp < 1:
                    result["warnings"].append(f"Very low resolution ({width}x{height})")
                    result["suspicious_score"] += 20
                elif mp < 3:
                    result["warnings"].append(f"Low resolution ({width}x{height})")
                    result["suspicious_score"] += 10
                else:
                    result["positives"].append(f"Good resolution: {width}x{height} ({mp:.1f} MP)")

                # Transparency check
                try:
                    if image.mode == "P" and image.info.get("transparency"):
                        result["warnings"].append("Has transparency — unusual for camera photos")
                        result["suspicious_score"] += 10
                except Exception:
                    pass

                # Compression check
                expected_min = (width * height * 3) / 1024 / 10
                if data["size_kb"] < expected_min * 0.3:
                    result["warnings"].append("Heavily compressed — possible re-upload from web")
                    result["suspicious_score"] += 15

            except Exception as e:
                result["warnings"].append(f"Could not analyze image: {e}")
                result["suspicious_score"] += 50
                logger.error(f"Image analysis error: {e}")
        else:
            result["warnings"].append("Pillow not installed — limited photo analysis")
            result["positives"].append("File received for analysis")
            if data["size_kb"] < 50:
                result["warnings"].append(f"Very small file ({int(data['size_kb'])}KB)")
                result["suspicious_score"] += 30
            elif data["size_kb"] < 100:
                result["warnings"].append(f"Small file ({int(data['size_kb'])}KB)")
                result["suspicious_score"] += 15
            else:
                result["positives"].append(f"File size: {int(data['size_kb'])}KB")

        # Verdict
        if result["suspicious_score"] >= 60:
            result["verdict"] = "HIGHLY SUSPICIOUS"
            result["verdict_color"] = "red"
        elif result["suspicious_score"] >= 30:
            result["verdict"] = "QUESTIONABLE"
            result["verdict_color"] = "orange"
        else:
            result["verdict"] = "LIKELY AUTHENTIC"
            result["verdict_color"] = "green"

        photo_auth_results.append(result)
        processed_images.append(proc)

    cross_photo = analyze_cross_photo_consistency(processed_images)

    tag_set = set()
    
    # Imagga tagging
    if IMAGGA_API_KEY and IMAGGA_API_SECRET:
        for data in image_data:
            try:
                r = requests.post(
                    "https://api.imagga.com/v2/tags",
                    auth=(IMAGGA_API_KEY, IMAGGA_API_SECRET),
                    files={"image": (data["filename"], data["bytes"])},
                    timeout=30,
                )
                resp = r.json()
                if resp.get("status", {}).get("type") == "error":
                    api_errors.append("Imagga: " + str(resp["status"].get("text")))
                    continue
                for t in resp.get("result", {}).get("tags", []):
                    tag = t["tag"]["en"].lower()
                    conf = round(t["confidence"], 2)
                    all_objects.append({"tag": tag, "confidence": conf})
                    tag_set.add(tag)
            except Exception as e:
                api_errors.append(f"Imagga error: {e}")
    else:
        api_errors.append("Imagga not configured — using fallback analysis.")

    # OCR
    if OCRSPACE_API_KEY:
        for data in image_data:
            try:
                r = requests.post(
                    "https://api.ocr.space/parse/image",
                    files={"file": (data["filename"], data["bytes"])},
                    data={"apikey": OCRSPACE_API_KEY, "language": "eng"},
                    timeout=30,
                )
                resp = r.json()
                parsed = resp.get("ParsedResults", [])
                if parsed:
                    ocr_text += " " + parsed[0].get("ParsedText", "")
            except Exception as e:
                logger.warning(f"OCR error: {e}")
    ocr_text = ocr_text.lower()

    # Fallback local analysis if no tags
    if not tag_set and PILLOW_AVAILABLE:
        for data in image_data:
            try:
                image = Image.open(BytesIO(data["bytes"]))
                gray = image.convert("L")
                brightness = sum(gray.getdata()) / (gray.width * gray.height)
                if brightness < 60:
                    tag_set.add("dark_image")
                if brightness > 220:
                    tag_set.add("bright_image")
                if image.width >= 1920:
                    tag_set.add("high_resolution")
            except Exception:
                pass
        if tag_set:
            api_errors.append("Using fallback local analysis (Imagga unavailable).")

    return photo_auth_results, cross_photo, tag_set, all_objects, ocr_text, api_errors, image_data


# ─────────────────────────────────────────────
# GEMINI AI VISION ANALYSIS
# ─────────────────────────────────────────────

def analyze_with_gemini(images_data, address, osm_context):
    if not GEMINI_API_KEY:
        return None, "Gemini API key not configured"

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
    parts = []

    for data in images_data[:3]:
        b64 = base64.b64encode(data["bytes"]).decode("utf-8")
        mime = "image/png" if data["filename"].lower().endswith(".png") else "image/jpeg"
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})

    prompt = f"""You are a certified property inspector. Analyze the uploaded property photos for the address: {address}

OSM Location Context: {json.dumps(osm_context)}

Based ONLY on what you can see in these photos and the location context above, return a single JSON object with this exact structure and no markdown formatting:

{{
  "property_condition": "Excellent|Above Average|Average|Fair|Poor",
  "location_type": "Urban|Suburban|Rural",
  "property_use": "Single Family - 1 Unit|Multi-Unit|Condo|Townhome/Row House|Modular|Mobile/Manufactured Home|Vacant Lot|Other",
  "conforms_to_neighborhood": true/false,
  "able_to_view_property": true,
  "for_sale_sign": true/false,
  "garage_present": true/false,
  "under_construction": true/false,
  "repairs_required": true/false,
  "high_tension_wires": true/false,
  "vacant_homes_in_area": true/false,
  "landfill_nearby": true/false,
  "commercial_nearby": true/false,
  "railroad_nearby": true/false,
  "highway_nearby": true/false,
  "airport_nearby": true/false,
  "gated_community": true/false,
  "resort_nearby": true/false,
  "golf_course_nearby": true/false,
  "waterfront": true/false,
  "park_nearby": true/false,
  "lake_view": true/false,
  "mountain_view": true/false,
  "general_comments": "2-4 sentences summarizing visible condition and concerns.",
  "reasoning": "1 sentence explaining your confidence."
}}

Rules:
- Answer true ONLY if visually evident or strongly implied.
- For location-based features (highway, railroad, airport, etc.), trust the OSM context provided above.
- property_condition must be based on visible upkeep, materials, and landscaping.
- general_comments must be professional and concise.
"""

    payload = {
        "contents": [{"parts": parts + [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
            "maxOutputTokens": 2048
        }
    }

    try:
        r = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=60)
        resp = r.json()
        if "error" in resp:
            return None, f"Gemini API error: {resp['error']['message']}"

        text = resp["candidates"][0]["content"]["parts"][0]["text"]
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        result = json.loads(text)
        return result, None
    except Exception as e:
        logger.error(f"Gemini analysis failed: {e}")
        return None, f"Gemini analysis failed: {e}"


# ─────────────────────────────────────────────
# INSPECTION ENGINE
# ─────────────────────────────────────────────

def build_inspection_answers(tag_set, all_text, location_data, place_results, area_type, osm_details):
    def ans(val, conf, source, note=""):
        return {"value": "Yes" if val else "No", "confidence": conf, "source": source,
                "editable": True, "note": note}

    def str_ans(val, conf, source, note=""):
        return {"value": val, "confidence": conf, "source": source,
                "editable": True, "note": note}

    answers = {}

    # Condition
    condition_score = 50
    pos_tags = {"tree", "plant", "yard", "garden", "lawn", "flower", "shrub", "landscaping", "high_resolution", "bright_image"}
    neg_tags = {"damage", "debris", "rust", "decay", "ruin", "broken", "crack", "construction", "dark_image"}
    pos_hits = len(tag_set & pos_tags)
    neg_hits = len(tag_set & neg_tags)
    condition_score += pos_hits * 8
    condition_score -= neg_hits * 12
    condition_score = max(0, min(100, condition_score))

    if condition_score >= 80:
        condition = "Excellent"
    elif condition_score >= 65:
        condition = "Above Average"
    elif condition_score >= 50:
        condition = "Average"
    elif condition_score >= 35:
        condition = "Fair"
    else:
        condition = "Poor"

    answers["property_condition"] = {
        "value": condition,
        "confidence": 0.55,
        "source": "Heuristic scoring from detected features",
        "editable": True,
        "note": "⚠️ AI estimate based on visible features. Not a professional appraisal."
    }

    answers["location_type"] = {
        "value": area_type, "confidence": 0.85,
        "source": "OpenStreetMap / Nominatim",
        "editable": True, "note": "Based on address geocoding and nearby feature density."
    }

    # Property Use
    building_type = osm_details.get("building_type", "").lower()
    if building_type in ("house", "detached", "bungalow", "cabin", "semidetached_house"):
        property_use = "Single Family - 1 Unit"
    elif building_type in ("apartments",):
        property_use = "Multi-Unit"
    elif building_type in ("terrace", "townhouse"):
        property_use = "Townhome/Row House"
    elif building_type in ("static_caravan", "mobile_home"):
        property_use = "Mobile/Manufactured Home"
    elif "commercial" in building_type or "retail" in building_type or "office" in building_type:
        property_use = "Other: Commercial"
    else:
        if "apartment" in tag_set or "condominium" in tag_set or "condo" in tag_set:
            property_use = "Condo"
        elif "townhouse" in tag_set:
            property_use = "Townhome/Row House"
        elif "mobile_home" in tag_set or "trailer" in tag_set:
            property_use = "Mobile/Manufactured Home"
        elif "construction" in tag_set or "framework" in tag_set:
            property_use = "Vacant Lot"
        else:
            property_use = "Single Family - 1 Unit"

    answers["property_use"] = str_ans(
        property_use, 0.65, "OSM + Image AI",
        "Best guess based on building classification and visual tags."
    )

    # Conforms
    residential_types = {"house", "residential", "apartments", "detached", "semidetached_house",
                         "terrace", "bungalow", "cabin", "static_caravan", "townhouse", "duplex"}
    conforms = False
    if building_type in residential_types and area_type in ("Urban", "Suburban", "Rural"):
        conforms = True
    elif building_type in {"commercial", "retail", "industrial", "warehouse", "office"} and area_type == "Urban":
        conforms = True
    elif building_type == "Unknown":
        conforms = False

    answers["conforms_to_neighborhood"] = ans(
        conforms, 0.6 if building_type != "Unknown" else 0.4,
                "OSM + Heuristic",
        "Compares building type with area classification."
    )

    answers["able_to_view_property"] = {
        "value": "Yes", "confidence": 1.0, "source": "Photo Upload",
        "editable": True, "note": "Photos were successfully uploaded and analyzed."
    }

    sale_keywords = {"for sale", "for lease", "sold", "listing", "real estate"}
    sale_detected = any(k in all_text for k in sale_keywords)
    answers["for_sale_sign"] = ans(sale_detected, 0.85 if sale_detected else 0.7,
        "OCR Text Detection", "Text extracted from photos.")

    garage_keywords = {"garage", "carport", "driveway", "shed"}
    garage_detected = bool(tag_set & garage_keywords) or "garage" in all_text
    answers["garage_present"] = ans(garage_detected, 0.75 if garage_detected else 0.6,
        "Image AI", "Detected from visual features.")

    construction_keywords = {"construction", "scaffolding", "crane", "excavator", "framework", "building site"}
    construction_detected = bool(tag_set & construction_keywords) or "construction" in all_text
    answers["under_construction"] = ans(construction_detected, 0.8 if construction_detected else 0.65,
        "Image AI", "Visual detection of construction materials.")

    damage_keywords = {"damage", "debris", "rust", "decay", "ruin", "broken", "crack", "peeling", "mold"}
    damage_detected = bool(tag_set & damage_keywords) or any(k in all_text for k in damage_keywords)
    answers["repairs_required"] = {
        "value": "Yes" if damage_detected else "No",
        "confidence": 0.6, "source": "Image AI (heuristic)",
        "editable": True,
        "note": "⚠️ AI cannot reliably assess repair needs from photos alone. Manual verification required."
    }

    power = place_results.get("power_lines", {})
    answers["high_tension_wires"] = ans(
        power["count"] > 0, 0.9 if power["count"] > 0 else 0.85,
        f"OSM ({power['min_dist']}m)" if power.get("min_dist") else "OpenStreetMap",
        "Power transmission lines within 500m."
    )

    vacant = place_results.get("vacant_building", {})
    answers["vacant_homes_in_area"] = ans(
        vacant["count"] > 0, 0.75 if vacant["count"] > 0 else 0.6,
        "OpenStreetMap", "Vacant or abandoned buildings nearby."
    )

    landfill = place_results.get("landfill", {})
    answers["landfill_nearby"] = ans(
        landfill["count"] > 0, 0.9 if landfill["count"] > 0 else 0.85,
        f"OSM ({landfill['min_dist']}m)" if landfill.get("min_dist") else "OpenStreetMap",
        "Landfill or waste facility within 3km."
    )

    comm = place_results.get("commercial", {})
    ind = place_results.get("industrial", {})
    commercial_detected = comm["count"] > 0 or ind["count"] > 0
    source_parts = []
    if comm.get("min_dist"): source_parts.append(f"Commercial {comm['min_dist']}m")
    if ind.get("min_dist"): source_parts.append(f"Industrial {ind['min_dist']}m")
    source_str = " / ".join(source_parts) if source_parts else "OpenStreetMap"
    answers["commercial_nearby"] = ans(
        commercial_detected, 0.9 if commercial_detected else 0.85,
        source_str, "Commercial or industrial zoning nearby."
    )

    r = place_results.get("railroad", {})
    answers["railroad_nearby"] = {
        "value": "Yes" if r["count"] > 0 else "No",
        "confidence": 0.95 if r["count"] > 0 else 0.9,
        "source": f"OSM ({r['min_dist']}m)" if r.get("min_dist") else "OpenStreetMap",
        "editable": True, "note": ""
    }

    h = place_results.get("highway", {})
    answers["highway_nearby"] = {
        "value": "Yes" if h["count"] > 0 else "No",
        "confidence": 0.95 if h["count"] > 0 else 0.9,
        "source": f"OSM ({h['min_dist']}m)" if h.get("min_dist") else "OpenStreetMap",
        "editable": True, "note": ""
    }

    a = place_results.get("airport", {})
    answers["airport_nearby"] = {
        "value": "Yes" if a["count"] > 0 else "No",
        "confidence": 0.95 if a["count"] > 0 else 0.9,
        "source": f"OSM ({a['min_dist']}m)" if a.get("min_dist") else "OpenStreetMap",
        "editable": True, "note": ""
    }

    gt = place_results.get("gated", {})
    answers["gated_community"] = {
        "value": "Yes" if gt["count"] > 0 else "No",
        "confidence": 0.5,
        "source": "OSM (barrier gates nearby)",
        "editable": True,
        "note": "⚠️ Very unreliable via OSM. Please verify manually."
    }

    resort = place_results.get("resort", {})
    answers["resort_nearby"] = ans(
        resort["count"] > 0, 0.85 if resort["count"] > 0 else 0.7,
        f"OSM ({resort['min_dist']}m)" if resort.get("min_dist") else "OpenStreetMap",
        "Resort or hotel complex nearby."
    )

    g = place_results.get("golf_course", {})
    answers["golf_course_nearby"] = {
        "value": "Yes" if g["count"] > 0 else "No",
        "confidence": 0.9 if g["count"] > 0 else 0.85,
        "source": f"OSM ({g['min_dist']}m)" if g.get("min_dist") else "OpenStreetMap",
        "editable": True, "note": ""
    }

    w = place_results.get("waterfront", {})
    answers["waterfront"] = {
        "value": "Yes" if w["count"] > 0 else "No",
        "confidence": 0.9 if w["count"] > 0 else 0.85,
        "source": f"OSM ({w['min_dist']}m)" if w.get("min_dist") else "OpenStreetMap",
        "editable": True, "note": ""
    }

    p = place_results.get("park", {})
    answers["park_nearby"] = {
        "value": "Yes" if p["count"] > 0 else "No",
        "confidence": 0.9 if p["count"] > 0 else 0.85,
        "source": f"OSM ({p['min_dist']}m)" if p.get("min_dist") else "OpenStreetMap",
        "editable": True, "note": ""
    }

    has_water_tags = bool(tag_set & {"lake", "water", "river", "ocean", "reflection", "shore", "coast", "pond"})
    lake_view = w["count"] > 0 and has_water_tags
    answers["lake_view"] = ans(
        lake_view, 0.7 if lake_view else 0.5,
        "Image AI + OSM",
        "Requires both water body nearby AND visual evidence of water in photos."
    )

    mountain = place_results.get("mountain", {})
    has_mountain_tags = bool(tag_set & {"mountain", "hill", "peak", "valley", "cliff", "landscape", "panoramic"})
    mountain_view = mountain["count"] > 0 and has_mountain_tags
    answers["mountain_view"] = ans(
        mountain_view, 0.7 if mountain_view else 0.5,
        "Image AI + OSM",
        "Requires both mountain terrain nearby AND visual evidence in photos."
    )

    comments = []
    if answers["under_construction"]["value"] == "Yes":
        comments.append("Property appears to be under construction.")
    if answers["repairs_required"]["value"] == "Yes":
        comments.append("Visible repairs or maintenance may be needed based on photo analysis.")
    if answers["property_condition"]["value"] in ("Poor", "Fair"):
        comments.append(f"Overall condition assessed as {answers['property_condition']['value']}.")
    if answers["for_sale_sign"]["value"] == "Yes":
        comments.append("For sale sign detected in uploaded photos.")
    if answers["high_tension_wires"]["value"] == "Yes":
        comments.append("High tension electric wires observed in the immediate area.")
    if answers["vacant_homes_in_area"]["value"] == "Yes":
        comments.append("Vacant or abandoned structures noted in the neighborhood.")
    if answers["landfill_nearby"]["value"] == "Yes":
        comments.append("Waste management or landfill facility identified nearby.")
    if answers["commercial_nearby"]["value"] == "Yes":
        comments.append("Commercial or industrial activity detected in proximity.")
    if answers["waterfront"]["value"] == "Yes":
        comments.append("Waterfront or lake access identified nearby.")
    if answers["mountain_view"]["value"] == "Yes":
        comments.append("Mountain views may be available from the property.")
    if answers["golf_course_nearby"]["value"] == "Yes":
        comments.append("Golf course in proximity to the property.")
    if answers["resort_nearby"]["value"] == "Yes":
        comments.append("Resort or hospitality complex nearby.")
    if not comments:
        comments.append("No significant adverse issues detected from available imagery and location data.")

    answers["general_comments"] = str_ans(
        " ".join(comments), 0.5, "AI Summary",
        "Auto-generated from inspection findings. Please review and edit as needed."
    )

    return answers


# ─────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────

def compute_legacy_score(tag_set, place_results):
    score = 50
    breakdown = []
    def add(points, reason):
        nonlocal score
        score += points
        breakdown.append({"points": points, "reason": reason})

    if any(k in tag_set for k in ("tree", "plant", "yard", "garden", "lawn")):
        add(10, "Landscaping visible in photo")
    if any(k in tag_set for k in ("car", "vehicle", "automobile")):
        add(5, "Vehicle visible in photo")
    if any(k in tag_set for k in ("damage", "debris", "rust", "decay", "ruin")):
        add(-15, "Possible damage visible in photo")
    if any(k in tag_set for k in ("construction", "scaffolding", "crane")):
        add(-10, "Possible construction/incomplete work visible")

    for place, data in (place_results or {}).items():
        count = data.get("count", 0) if isinstance(data, dict) else data
        if not count:
            continue
        weights = {"park": 8, "school": 6, "restaurant": 4, "shopping": 4,
                   "hospital": 3, "golf_course": 8, "airport": -8,
                   "railroad": -5, "highway": -3, "industrial": -6,
                   "resort": 8, "waterfront": 8, "mountain": 6,
                   "landfill": -10, "commercial": -4, "vacant_building": -5}
        w = weights.get(place, 0)
        if w:
            add(w, f"{count} {place.replace('_', ' ')}(s) nearby")

    score = max(0, min(100, score))
    return score, breakdown


def score_to_rating(score):
    if score >= 80: return "Excellent"
    if score >= 65: return "Above Average"
    if score >= 50: return "Average"
    if score >= 35: return "Fair"
    return "Poor"


def format_question(key):
    mapping = {
        "property_condition": "1. General — Condition Rating",
        "location_type": "1. General — Location (Urban/Suburban/Rural)",
        "property_use": "1. General — Property Use",
        "conforms_to_neighborhood": "2. Property Details — Conforms to Neighborhood",
        "able_to_view_property": "2. Property Details — Able to View Property",
        "for_sale_sign": "2. Property Details — For Sale Sign Posted",
        "garage_present": "2. Property Details — Garage Present",
        "under_construction": "2. Property Details — Dwelling Under Construction",
        "repairs_required": "2. Property Details — Repairs Required",
        "general_comments": "3. General Comments",
        "high_tension_wires": "4. Neighborhood — High Tension Electric Wires",
        "vacant_homes_in_area": "4. Neighborhood — Vacant Homes in Area",
        "landfill_nearby": "4. Neighborhood — Landfill/Waste Management Nearby",
        "commercial_nearby": "4. Neighborhood — Commercial/Industrial Nearby",
        "railroad_nearby": "4. Neighborhood — Railroad Track/Station Nearby",
        "highway_nearby": "4. Neighborhood — Interstate/Freeway/Highway Nearby",
        "airport_nearby": "4. Neighborhood — Private or Public Airport Nearby",
        "gated_community": "4. Neighborhood — Private Gated Community",
        "resort_nearby": "5. Positive Factors — Resort",
        "golf_course_nearby": "5. Positive Factors — Golf Course",
        "waterfront": "5. Positive Factors — Waterfront",
        "park_nearby": "5. Positive Factors — Park",
        "lake_view": "5. Positive Factors — Lake View",
        "mountain_view": "5. Positive Factors — Mountain View",
    }
    return mapping.get(key, key.replace("_", " ").title())


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def home():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/debug-env")
def debug_env():
    return jsonify({
        "IMAGGA_API_KEY_set": bool(IMAGGA_API_KEY),
        "IMAGGA_API_SECRET_set": bool(IMAGGA_API_SECRET),
        "OCRSPACE_API_KEY_set": bool(OCRSPACE_API_KEY),
        "GEMINI_API_KEY_set": bool(GEMINI_API_KEY),
        "LOCATIONIQ_API_KEY_set": False,
        "using_nominatim": True,
        "using_census_fallback": True,
        "photo_authenticity": True,
        "cross_photo_check": True,
        "pillow_available": PILLOW_AVAILABLE,
        "imagehash_available": IMAGEHASH_AVAILABLE,
    })


@app.route("/api/test-gemini")
def test_gemini():
    if not GEMINI_API_KEY:
        return jsonify({"configured": False, "error": "GEMINI_API_KEY not set"}), 400

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
    payload = {
        "contents": [{"parts": [{"text": "Say 'Gemini API key is working' and nothing else."}]}],
        "generationConfig": {"maxOutputTokens": 50}
    }
    try:
        r = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=20)
        data = r.json()
        if "error" in data:
            return jsonify({"configured": True, "working": False, "error": data["error"]["message"]}), 400
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return jsonify({"configured": True, "working": True, "response": text})
    except Exception as e:
        return jsonify({"configured": True, "working": False, "error": str(e)}), 500


@app.route("/api/verify-address", methods=["POST"])
def verify_address():
    address = request.json.get("address", "").strip()
    if not address:
        return jsonify({"valid": False, "error": "No address provided"}), 400

    location, error = nominatim_geocode(address)
    if error:
        return jsonify({
            "valid": False,
            "error": error,
            "note": "This address may not be in our databases yet. Try a simpler format like 'Gaithersburg, MD' or check the spelling."
        }), 404

    lat, lng = location["lat"], location["lng"]
    osm_details = get_osm_property_details(lat, lng, location.get("osm_id"), location.get("osm_type"))
    place_results, _ = overpass_nearby_features(lat, lng)
    area_type = classify_area_type(location.get("address_details", {}), place_results)

    return jsonify({
        "valid": True,
        "location": location,
        "geocoder_used": "nominatim" if location.get("osm_id") else "census_fallback",
        "property_verification": {
            "address_exists": True,
            "osm_class": osm_details.get("class", "unknown"),
            "osm_type": osm_details.get("type", "unknown"),
            "is_likely_residential": osm_details.get("is_residential", False),
            "building_type": osm_details.get("building_type", "Unknown"),
            "confidence": osm_details.get("confidence", "low"),
        },
        "area_type": area_type,
        "nearby_features": place_results,
        "street_view": {
            "available": False,
            "viewer_url": f"https://www.mapillary.com/app/?lat={lat}&lng={lng}&z=17",
        },
        "warning": (
            "AI cannot verify that uploaded photos were taken at this address. "
            "Photos without GPS metadata are just images — they could be from anywhere. "
            "You must visually confirm the match using satellite and street view."
        ),
    })


@app.route("/api/check-photos", methods=["POST"])
def check_photos():
    images = request.files.getlist("images")
    if not images:
        return jsonify({"error": "No images uploaded."}), 400

    photo_auth, cross_photo, _, _, _, _, _ = process_all_images(images)
    has_gps = any(any("GPS embedded" in p for p in r["positives"]) for r in photo_auth)

    return jsonify({
        "photo_count": len(images),
        "authenticity_results": photo_auth,
        "cross_photo_check": cross_photo,
        "pillow_available": PILLOW_AVAILABLE,
        "summary": {
            "total_suspicious": sum(1 for r in photo_auth if r["suspicious_score"] >= 30),
            "total_clean": sum(1 for r in photo_auth if r["suspicious_score"] < 30),
            "has_gps": has_gps,
            "same_property": cross_photo.get("same_property_likely", "unknown"),
        }
    })


@app.route("/api/compare-photos", methods=["POST"])
def compare_photos():
    images = request.files.getlist("images")
    if len(images) < 2:
        return jsonify({
            "same_property": None,
            "error": "Need at least 2 images to compare",
            "visual_similarity": None,
            "gps_spread_m": None,
            "architecture_match": None,
            "color_match": None,
            "mismatched_pairs": []
        }), 400

    processed = []
    for img in images:
        try:
            img.seek(0)
            file_bytes = img.read()
            img.seek(0)
            proc = {"filename": getattr(img, 'filename', 'unknown'), "gps": None, "phash": None, "color_sig": None}

            if PILLOW_AVAILABLE:
                try:
                    image = Image.open(BytesIO(file_bytes))
                    gps_coords = extract_gps_coords(image)
                    if gps_coords:
                        proc["gps"] = gps_coords

                    if IMAGEHASH_AVAILABLE:
                        try:
                            proc["phash"] = str(imagehash.phash(image))
                        except Exception:
                            pass

                    proc["color_sig"] = get_color_signature(file_bytes)
                except Exception:
                    pass
            processed.append(proc)
        except Exception as e:
            logger.error(f"Compare photos read error: {e}")

    gps_points = [p for p in processed if p.get("gps")]
    max_gps_dist = 0
    for i in range(len(gps_points)):
        for j in range(i + 1, len(gps_points)):
            d = haversine(gps_points[i]["lat"], gps_points[i]["lng"],
                          gps_points[j]["lat"], gps_points[j]["lng"])
            max_gps_dist = max(max_gps_dist, d)

    max_hamming = 0
    avg_hamming = None
    phash_mismatches = []
    if IMAGEHASH_AVAILABLE:
        hashes = [(i, p) for i, p in enumerate(processed) if p.get("phash")]
        if len(hashes) >= 2:
            total_dist = 0
            pair_count = 0
            for i in range(len(hashes)):
                for j in range(i + 1, len(hashes)):
                    h1 = imagehash.hex_to_hash(hashes[i][1]["phash"])
                    h2 = imagehash.hex_to_hash(hashes[j][1]["phash"])
                    dist = h1 - h2
                    total_dist += dist
                    pair_count += 1
                    max_hamming = max(max_hamming, dist)
                    if dist > 38:
                        phash_mismatches.append({
                            "file_a": hashes[i][1]["filename"],
                            "file_b": hashes[j][1]["filename"],
                            "reason": f"Visually very different (perceptual distance: {dist})"
                        })
            avg_hamming = total_dist / pair_count if pair_count > 0 else 0

    color_sigs = [p for p in processed if p.get("color_sig")]
    min_color_dist = float('inf')
    if len(color_sigs) >= 2:
        for i in range(len(color_sigs)):
            for j in range(i + 1, len(color_sigs)):
                d = color_distance(color_sigs[i]["color_sig"], color_sigs[j]["color_sig"])
                min_color_dist = min(min_color_dist, d)

    color_similarity = None
    if min_color_dist != float('inf'):
        color_similarity = max(0.0, 1.0 - (min_color_dist / 80.0))

    same_property = None
    mismatched_pairs = []

    if max_gps_dist > 500:
        same_property = False
        for i in range(len(gps_points)):
            for j in range(i + 1, len(gps_points)):
                d = haversine(gps_points[i]["lat"], gps_points[i]["lng"],
                              gps_points[j]["lat"], gps_points[j]["lng"])
                if d > 500:
                    mismatched_pairs.append({
                        "file_a": gps_points[i]["filename"],
                        "file_b": gps_points[j]["filename"],
                        "reason": f"GPS locations are {int(d)} meters apart"
                    })

    elif IMAGEHASH_AVAILABLE and max_hamming > 45:
        same_property = False
        mismatched_pairs = phash_mismatches

    elif IMAGEHASH_AVAILABLE and max_hamming > 32:
        same_property = None

    else:
        same_property = True

    if same_property is None and color_similarity is not None:
        if color_similarity > 0.6 and (not IMAGEHASH_AVAILABLE or max_hamming < 40):
            same_property = True
        elif color_similarity < 0.2 and IMAGEHASH_AVAILABLE and max_hamming > 40:
            same_property = False

    visual_sim = hamming_to_real_estate_similarity(avg_hamming, max_hamming) if avg_hamming is not None else None

    return jsonify({
        "same_property": same_property,
        "visual_similarity": round(visual_sim, 2) if visual_sim is not None else None,
        "gps_spread_m": round(max_gps_dist) if gps_points else None,
        "architecture_match": max_hamming < 38 if IMAGEHASH_AVAILABLE else None,
        "color_match": color_similarity > 0.5 if color_similarity is not None else None,
        "mismatched_pairs": mismatched_pairs,
        "error": None
    })


@app.route("/api/address-suggestions", methods=["GET"])
def address_suggestions():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 3:
        return jsonify({"suggestions": []})

    try:
        r = requests.get(
            NOMINATIM_URL,
            params={
                "q": q,
                "format": "json",
                "limit": 5,
                "addressdetails": 1,
                "countrycodes": "us",
            },
            headers=HEADERS,
            timeout=10,
        )
        data = r.json()
        suggestions = []
        for item in data:
            suggestions.append({
                "address": item.get("display_name", ""),
                "source": "OpenStreetMap"
            })
        return jsonify({"suggestions": suggestions})
    except Exception as e:
        logger.error(f"Address suggestions error: {e}")
        return jsonify({"suggestions": [], "error": str(e)}), 500


@app.route("/api/inspect", methods=["POST"])
def inspect():
    images = request.files.getlist("images")
    address = request.form.get("address", "").strip()

    if not images:
        return jsonify({"error": "No images uploaded."}), 400

    photo_auth, cross_photo, tag_set, all_objects, ocr_text, api_errors, image_data = process_all_images(images)

    best = {}
    for o in all_objects:
        if o["tag"] not in best or o["confidence"] > best[o["tag"]]:
            best[o["tag"]] = o["confidence"]
    objects_detected = sorted([{"tag": t, "confidence": c} for t, c in best.items()], key=lambda x: -x["confidence"])

    location = None
    place_results = {}
    area_type = "Unknown"
    location_error = None
    osm_details = {"building_type": "Unknown", "is_residential": False}

    if address:
        location, location_error = nominatim_geocode(address)
        if location:
            lat, lng = location["lat"], location["lng"]
            osm_details = get_osm_property_details(lat, lng, location.get("osm_id"), location.get("osm_type"))
            place_results, overpass_error = overpass_nearby_features(lat, lng)
            if overpass_error:
                api_errors.append(f"Overpass error: {overpass_error}")
            area_type = classify_area_type(location.get("address_details", {}), place_results)

    gemini_result, gemini_error = None, None
    if GEMINI_API_KEY and image_data:
        gemini_result, gemini_error = analyze_with_gemini(
            image_data,
            address or "Unknown",
            {
                "area_type": area_type,
                "building_type": osm_details.get("building_type"),
                "nearby_features": place_results
            }
        )
        if gemini_error:
            api_errors.append(gemini_error)

    gps_mismatch = False
    gps_distance = None
    if location:
        for r in photo_auth:
            for p in r["positives"]:
                if "GPS embedded" in p:
                    try:
                        parts = p.replace("GPS embedded: ", "").split(", ")
                        photo_lat = float(parts[0])
                        photo_lng = float(parts[1])
                        dist = haversine(location["lat"], location["lng"], photo_lat, photo_lng)
                        if dist > 500:
                            gps_mismatch = True
                        gps_distance = dist
                    except Exception:
                        pass

    answers = build_inspection_answers(tag_set, ocr_text, location, place_results, area_type, osm_details)

    if gemini_result:
        bool_map = {
            "conforms_to_neighborhood": "conforms_to_neighborhood",
            "able_to_view_property": "able_to_view_property",
            "for_sale_sign": "for_sale_sign",
            "garage_present": "garage_present",
            "under_construction": "under_construction",
            "repairs_required": "repairs_required",
            "high_tension_wires": "high_tension_wires",
            "vacant_homes_in_area": "vacant_homes_in_area",
            "landfill_nearby": "landfill_nearby",
            "commercial_nearby": "commercial_nearby",
            "railroad_nearby": "railroad_nearby",
            "highway_nearby": "highway_nearby",
            "airport_nearby": "airport_nearby",
            "gated_community": "gated_community",
            "resort_nearby": "resort_nearby",
            "golf_course_nearby": "golf_course_nearby",
            "waterfront": "waterfront",
            "park_nearby": "park_nearby",
            "lake_view": "lake_view",
            "mountain_view": "mountain_view",
        }

        for gemini_key, ans_key in bool_map.items():
            if gemini_key in gemini_result and ans_key in answers:
                if ans_key in ["high_tension_wires", "vacant_homes_in_area", "landfill_nearby",
                               "commercial_nearby", "railroad_nearby", "highway_nearby",
                               "airport_nearby", "gated_community", "resort_nearby",
                               "golf_course_nearby", "waterfront", "park_nearby"]:
                    if answers[ans_key].get("confidence", 0) < 0.8:
                        answers[ans_key] = {
                            "value": "Yes" if gemini_result[gemini_key] else "No",
                            "confidence": 0.85,
                            "source": "Gemini 1.5 Flash Vision",
                            "editable": True,
                            "note": "AI vision analysis of uploaded photos + location context."
                        }
                else:
                    answers[ans_key] = {
                        "value": "Yes" if gemini_result[gemini_key] else "No",
                        "confidence": 0.85,
                        "source": "Gemini 1.5 Flash Vision",
                        "editable": True,
                        "note": "AI vision analysis of uploaded photos."
                    }

        text_map = {
            "property_condition": "property_condition",
            "location_type": "location_type",
            "property_use": "property_use",
            "general_comments": "general_comments",
        }
        for gemini_key, ans_key in text_map.items():
            if gemini_key in gemini_result and gemini_result[gemini_key] and ans_key in answers:
                answers[ans_key] = {
                    "value": gemini_result[gemini_key],
                    "confidence": 0.82,
                    "source": "Gemini 1.5 Flash Vision",
                    "editable": True,
                    "note": "AI visual assessment of property photos."
                }

    score, breakdown = compute_legacy_score(tag_set, place_results)
    rating = score_to_rating(score)

    return jsonify({
        "truthful": True,
        "disclaimer": "AI-generated estimates. All answers must be reviewed before use.",
        "address_analyzed": bool(location),
        "location": location,
        "location_error": location_error,
        "area_type": area_type,
        "score": score,
        "rating": rating,
        "score_breakdown": breakdown,
        "objects_detected": objects_detected,
        "nearby_places": place_results,
        "answers": answers,
        "gemini_analysis": {
            "available": gemini_result is not None,
            "reasoning": gemini_result.get("reasoning") if gemini_result else None,
            "error": gemini_error,
        },
        "photo_authenticity": {
            "results": photo_auth,
            "pillow_available": PILLOW_AVAILABLE,
            "gps_match": {
                "checked": any(any("GPS embedded" in p for p in r["positives"]) for r in photo_auth),
                "mismatch": gps_mismatch,
                "distance_m": round(gps_distance) if gps_distance else None,
            }
        },
        "cross_photo_check": cross_photo,
        "api_errors": api_errors,
        "generated_at": datetime.now().isoformat(),
    })


@app.route("/api/report", methods=["POST"])
def generate_report():
    data = request.json
    address = data.get("address", "Unknown")
    location = data.get("location", {})
    answers = data.get("answers", {})
    reviewed_by = data.get("reviewed_by", "Inspector")
    photo_auth = data.get("photo_authenticity", {})
    cross_photo = data.get("cross_photo_check", {})

    report = {
        "report_id": f"PI-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "generated_at": datetime.now().isoformat(),
        "property_address": address,
        "coordinates": location,
        "reviewed_by": reviewed_by,
        "photo_authenticity_summary": photo_auth,
        "cross_photo_check_summary": cross_photo,
        "disclaimer": (
            "This report was generated using AI-assisted analysis and human review. "
            "It does not replace a professional property inspection. "
            "Photo location verification is limited to GPS metadata analysis and user affirmation."
        ),
        "findings": [],
    }

    for key, ans in answers.items():
        report["findings"].append({
            "question": format_question(key),
            "ai_answer": ans.get("original_value", ans.get("value")),
            "final_answer": ans.get("value"),
            "confidence": ans.get("confidence"),
            "source": ans.get("source"),
            "verified": ans.get("value") == ans.get("original_value", ans.get("value")),
        })

    return jsonify(report)


if __name__ == "__main__":
    app.run(debug=True, port=5000)