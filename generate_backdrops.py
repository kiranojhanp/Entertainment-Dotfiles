import os
import re
import json
import random
import requests
import concurrent.futures
import math
import argparse
from io import BytesIO
from PIL import Image, ImageDraw, ImageOps

# Grab API keys from the environment
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TRAKT_CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID")  # Required for Trakt lists
JSON_PATH = "nuvio/streaming-services-collections.json"

# Poster dimensions and configuration
UNIT_W, UNIT_H = 220, 330
PADDING = 14
MIN_POSTERS = 10
TARGET_POSTERS = 35  # unique posters we try to gather per folder before we stop fetching

# Multi-region fallback mapping. TMDB provider IDs are global concepts;
# TMDB just uses the 'region' parameter to decide if it returns any results.
PROVIDER_ID_FALLBACK = {
    "Netflix": 8,
    "Apple TV+": 350,
    "Apple TV": 350,
    "Hulu": 15,
    "Prime Video": 9,
    "Amazon Prime Video": 9,
    "AMC+": 526,
    "Criterion": 258,
    "Criterion Channel": 258,
    "Curiosity Stream": 190,
    "Discovery+": 520,
    "Disney+": 337,
    "Disney Plus": 337,
    "HBO Max": 1899,
    "MGM+": 34,
    "Mubi": 11,
    "MUBI": 11,
    "Paramount+": 531,
    "Paramount Plus": 531,
    "Peacock": 386,
    "Pluto TV": 300,
    "Shudder": 99,
    "Starz": 43,
    "Tubi TV": 73,
    "Tubi": 73,
    "Stan": 21,
    "BINGE": 385,
    "Crave": 230,
    "Crunchyroll": 283,
    "Hayu": 296,
    "Magellan TV": 551,
    "Magellan": 551,
    "Rakuten Viki": 344
}

# Cache of TMDB watch/providers lookups so we never re-check the same title twice in one run
_verify_cache = {}


def sanitize_filename(title):
    """Turn an arbitrary platform title into a safe path component."""
    name = title.replace(" ", "_").replace("+", "Plus")
    return re.sub(r"[^\w\-]", "_", name)


def get_provider_id(folder):
    folder_title = folder.get("title", "Unknown Folder")
    
    # Priority 1: Extract from JSON (support both schema variants)
    for source in folder.get("sources", []):
        is_tmdb = (source.get("provider") == "tmdb" or source.get("type") == "tmdb")
        is_discover = (source.get("tmdbSourceType") == "DISCOVER" or source.get("action") == "discover")
        
        if is_tmdb and is_discover:
            filters = source.get("filters", {})
            wp = filters.get("withWatchProviders") or filters.get("with_watch_providers")
            if wp is not None:
                try:
                    return int(str(wp).split(",")[0])
                except (ValueError, TypeError):
                    pass

    # Priority 2: Fallback map
    if folder_title in PROVIDER_ID_FALLBACK:
        return PROVIDER_ID_FALLBACK[folder_title]

    # Priority 3: Skip / Error clearly
    raise ValueError(
        f"Missing Provider ID. Folder '{folder_title}' has no TMDB DISCOVER provider ID "
        f"in JSON and no matching entry in PROVIDER_ID_FALLBACK."
    )


def get_region(folder):
    """Prefer the region embedded in the folder's own DISCOVER filters, default to US if missing."""
    for source in folder.get("sources", []):
        region = source.get("filters", {}).get("watchRegion")
        if region:
            return region
    return "US"


def is_available_on_provider(tmdb_id, media_type, provider_id, region):
    """Check TMDB watch/providers for this title; True only if provider_id
    shows up in that region's flatrate/free/ads/buy/rent listings."""
    cache_key = (tmdb_id, media_type)
    if cache_key in _verify_cache:
        data = _verify_cache[cache_key]
    else:
        url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/watch/providers?api_key={TMDB_API_KEY}"
        try:
            resp = requests.get(url, timeout=5)
            data = resp.json().get("results", {}) if resp.status_code == 200 else {}
        except Exception:
            data = {}
        _verify_cache[cache_key] = data

    region_data = data.get(region, {})
    all_providers = []
    for tier in ("flatrate", "free", "ads", "buy", "rent"):
        all_providers.extend(p["provider_id"] for p in region_data.get(tier, []))
    return provider_id in all_providers


def get_candidates_from_source(source):
    """Fetches candidate titles from either a TMDB or Trakt source.
    Returns a list of dicts: {tmdb_id, media_type, poster_url, needs_verification}."""
    source_title = source.get("title", "").lower()
    is_top10 = ("top 10" in source_title or "top ten" in source_title)
    limit = 10 if is_top10 else 25

    provider = source.get("provider")
    tmdb_source_type = source.get("tmdbSourceType")
    candidates = []

    # 1. TMDB LIST
    if provider == "tmdb" and tmdb_source_type == "LIST" and source.get("tmdbId"):
        tmdb_list_id = source["tmdbId"]
        url = f"https://api.themoviedb.org/3/list/{tmdb_list_id}?api_key={TMDB_API_KEY}"
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                response = resp.json()
                for item in response.get('items', [])[:limit]:
                    if not item.get('poster_path'):
                        continue
                    media_type = item.get('media_type') or (
                        "movie" if source.get("mediaType") == "MOVIE" else "tv"
                    )
                    candidates.append({
                        "tmdb_id": item.get("id"),
                        "media_type": media_type,
                        "poster_url": f"https://image.tmdb.org/t/p/w500{item['poster_path']}",
                        "needs_verification": True,
                    })
        except Exception as e:
            print(f"    Failed to fetch TMDB list {tmdb_list_id}: {e}")

    # 2. TMDB DISCOVER (already provider-filtered, no verification needed)
    elif provider == "tmdb" and tmdb_source_type == "DISCOVER":
        media_type = "movie" if source.get("mediaType") == "MOVIE" else "tv"
        endpoint = "discover/movie" if media_type == "movie" else "discover/tv"
        
        filters = source.get("filters", {})
        params = {
            "api_key": TMDB_API_KEY, 
            "sort_by": source.get("sortBy", "popularity.desc")
        }
        
        # Dynamically map JSON filters to TMDB params without enforcing a region if missing
        if filters.get("watchRegion"):
            params["watch_region"] = filters["watchRegion"]
        if filters.get("withWatchProviders"):
            params["with_watch_providers"] = filters["withWatchProviders"]
        if filters.get("withGenres"):
            params["with_genres"] = filters["withGenres"]
        if filters.get("voteCountGte"):
            params["vote_count.gte"] = filters["voteCountGte"]
        
        try:
            resp = requests.get(f"https://api.themoviedb.org/3/{endpoint}", params=params, timeout=5)
            if resp.status_code == 200:
                results = resp.json().get("results", [])[:limit]
                for item in results:
                    if not item.get("poster_path"):
                        continue
                    candidates.append({
                        "tmdb_id": item.get("id"),
                        "media_type": media_type,
                        "poster_url": f"https://image.tmdb.org/t/p/w500{item['poster_path']}",
                        "needs_verification": False,
                    })
        except Exception as e:
            print(f"    Failed to fetch TMDB discover for {endpoint}: {e}")

    # 3. Trakt Lists
    elif provider == "trakt" and source.get("traktListId"):
        trakt_list_id = source["traktListId"]
        if TRAKT_CLIENT_ID:
            url = f"https://api.trakt.tv/lists/{trakt_list_id}/items"
            headers = {
                "Content-Type": "application/json",
                "trakt-api-version": "2",
                "trakt-api-key": TRAKT_CLIENT_ID
            }
            try:
                resp = requests.get(url, headers=headers, timeout=5)
                if resp.status_code == 200:
                    items = resp.json()[:limit]
                    tmdb_lookups = []
                    for entry in items:
                        media_obj = entry.get('movie') or entry.get('show')
                        if media_obj and 'ids' in media_obj:
                            tmdb_id = media_obj['ids'].get('tmdb')
                            media_type = 'movie' if 'movie' in entry else 'tv'
                            if tmdb_id:
                                tmdb_lookups.append((tmdb_id, media_type))

                    def _fetch_trakt_tmdb_poster(pair):
                        tid, mtype = pair
                        api_url = f"https://api.themoviedb.org/3/{mtype}/{tid}?api_key={TMDB_API_KEY}"
                        r = requests.get(api_url, timeout=5)
                        if r.status_code == 200:
                            data = r.json()
                            if data.get('poster_path'):
                                return {
                                    "tmdb_id": tid,
                                    "media_type": mtype,
                                    "poster_url": f"https://image.tmdb.org/t/p/w500{data['poster_path']}",
                                    "needs_verification": True,
                                }
                        return None

                    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                        futures = [executor.submit(_fetch_trakt_tmdb_poster, pair) for pair in tmdb_lookups]
                        for future in concurrent.futures.as_completed(futures):
                            try:
                                result = future.result()
                                if result:
                                    candidates.append(result)
                            except Exception:
                                pass
            except Exception as e:
                print(f"    Failed to fetch Trakt list {trakt_list_id}: {e}")

    return candidates, is_top10


def gather_folder_posters(folder, provider_id, region, target=TARGET_POSTERS):
    """Aggregates unique, provider-verified poster URLs across every source."""
    sources = sorted(
        folder.get("sources", []),
        key=lambda s: not ("top 10" in s.get("title", "").lower() or "top ten" in s.get("title", "").lower())
    )

    seen_urls = set()
    pool = []

    for source in sources:
        if len(pool) >= target:
            break

        candidates, _ = get_candidates_from_source(source)
        if not candidates:
            continue

        to_verify = [c for c in candidates if c["needs_verification"]]
        no_check = [c for c in candidates if not c["needs_verification"]]

        verified_ok = set()
        if to_verify:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                future_to_cand = {
                    executor.submit(is_available_on_provider, c["tmdb_id"], c["media_type"], provider_id, region): c
                    for c in to_verify if c.get("tmdb_id")
                }
                for future in concurrent.futures.as_completed(future_to_cand):
                    cand = future_to_cand[future]
                    try:
                        if future.result():
                            verified_ok.add(cand["poster_url"])
                    except Exception:
                        pass

        for c in no_check + [c for c in to_verify if c["poster_url"] in verified_ok]:
            url = c["poster_url"]
            if url not in seen_urls:
                seen_urls.add(url)
                pool.append(url)
                if len(pool) >= target:
                    break

    return pool


def download_image(url):
    """Downloads an image and returns a raw PIL Image object."""
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            return Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception:
        pass
    return None


def process_block(img, width, height, radius=16):
    """Crops/resizes the image to fit the block size and applies rounded corners."""
    img = ImageOps.fit(img, (width, height), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, width, height), radius=radius, fill=255)
    img.putalpha(mask)
    return img


class SmartCycler:
    """Yields images from a pool and cycles smoothly if more slots need filling."""
    def __init__(self, images):
        self.images = list(images)
        self.idx = 0

    def next(self):
        if not self.images:
            return None
        img = self.images[self.idx % len(self.images)]
        self.idx += 1
        return img


def create_column_grid(poster_urls, save_path):
    """Creates a tilted grid backdrop from a single pool of unique, pre-verified
    poster URLs. Priority order (Top 10 first) is preserved from the input list."""
    CANVAS_W, CANVAS_H = 3600, 2600
    OUTPUT_W, OUTPUT_H = 1920, 1080
    ANGLE = 12

    unique_urls = list(dict.fromkeys(poster_urls))
    if not unique_urls:
        return

    print(f"  Downloading {len(unique_urls)} unique posters concurrently...")
    url_to_img = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        future_to_url = {executor.submit(download_image, url): url for url in unique_urls}
        for future in concurrent.futures.as_completed(future_to_url):
            u = future_to_url[future]
            try:
                img = future.result()
                if img:
                    url_to_img[u] = img
            except Exception:
                pass

    ordered_images = [url_to_img[u] for u in unique_urls if u in url_to_img]
    if not ordered_images:
        return

    cycler = SmartCycler(ordered_images)
    canvas = Image.new('RGBA', (CANVAS_W, CANVAS_H), (0, 0, 0, 255))

    cols = math.ceil(CANVAS_W / (UNIT_W + PADDING)) + 2
    rows = math.ceil(CANVAS_H / (UNIT_H + PADDING)) + 3

    slots = []
    for c in range(cols):
        y_offset = (UNIT_H // 2) if c % 2 != 0 else 0
        for r in range(rows):
            x = c * (UNIT_W + PADDING)
            y = (r * (UNIT_H + PADDING)) + y_offset - (UNIT_H // 2)
            slots.append((c, r, x, y))

    slots.sort(key=lambda s: (-s[0], s[1]))

    print("  Generating layout (priority posters foreground, full pool cycling elsewhere)...")
    for c, r, x, y in slots:
        img_raw = cycler.next()
        if not img_raw:
            continue

        processed_img = process_block(img_raw, UNIT_W, UNIT_H, radius=16)
        canvas.paste(processed_img, (x, y), processed_img)

    print("  Applying tilt and clean dark gradient...")
    canvas = canvas.rotate(ANGLE, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(0, 0, 0, 255))

    left = (CANVAS_W - OUTPUT_W) // 2
    top = (CANVAS_H - OUTPUT_H) // 2
    final_image = canvas.crop((left, top, left + OUTPUT_W, top + OUTPUT_H)).convert('RGBA')

    gradient = Image.new('RGBA', (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
    draw_grad = ImageDraw.Draw(gradient)

    for x in range(OUTPUT_W):
        if x < 400:
            alpha = 235
        elif x < 1500:
            progress = (x - 400) / 1100.0
            alpha = int(235 * (math.cos(progress * math.pi) + 1) / 2)
        else:
            alpha = 0
        draw_grad.line([(x, 0), (x, OUTPUT_H)], fill=(0, 0, 0, alpha))

    final_image = Image.alpha_composite(final_image, gradient)
    global_dark_wash = Image.new('RGBA', (OUTPUT_W, OUTPUT_H), (0, 0, 0, 25))
    final_image = Image.alpha_composite(final_image, global_dark_wash)

    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    final_image.convert("RGB").save(save_path, "JPEG", quality=95)


def main():
    parser = argparse.ArgumentParser(description="Generate streaming service backdrops.")
    parser.add_argument(
        "-p", "--providers",
        nargs="+",
        help="List of specific provider folder titles to process (e.g. Netflix 'Apple TV+'). If omitted, runs all."
    )
    args = parser.parse_args()

    if not TMDB_API_KEY:
        print("Error: TMDB_API_KEY environment variable is not set.")
        exit(1)

    try:
        with open(JSON_PATH, 'r') as f:
            config = json.load(f)[0]
    except Exception as e:
        print(f"Error loading JSON configuration: {e}")
        exit(1)

    # Convert provided arguments to lowercase for case-insensitive matching
    target_providers = [p.lower() for p in args.providers] if args.providers else None

    for folder in config.get("folders", []):
        folder_title = folder.get("title", "Unknown")
        
        # Skip this folder if target_providers is set and the title isn't in the list
        if target_providers and folder_title.lower() not in target_providers:
            continue

        platform_name = sanitize_filename(folder_title)
        print(f"\nProcessing {platform_name}...")

        try:
            # 1. Strict extraction: Resolves ID or raises ValueError
            provider_id = get_provider_id(folder)
            
            # 2. Extract specific region or default to US
            region = get_region(folder)

            # 3. Gather Posters
            posters = gather_folder_posters(folder, provider_id, region)

            if len(posters) < MIN_POSTERS:
                print(f"  Skipped {platform_name}: Only found {len(posters)} verified posters (need {MIN_POSTERS}+).")
                continue

            # 4. Generate Composite Image
            save_path = f"nuvio/images/{platform_name}/heroBackdropUrl.jpg"
            create_column_grid(posters, save_path)
            print(f"  Successfully saved {save_path}")

        except ValueError as ve:
            print(f"  Skipping folder: {ve}")
        except Exception as e:
            print(f"  Error processing folder: {e}")


if __name__ == "__main__":
    main()