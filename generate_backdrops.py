import os
import re
import json
import random
import requests
import concurrent.futures
import math
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


def sanitize_filename(title):
    """Turn an arbitrary platform title into a safe path component."""
    name = title.replace(" ", "_").replace("+", "Plus")
    return re.sub(r"[^\w\-]", "_", name)


def get_posters_from_source(source):
    """Fetches vertical poster URLs from either TMDB or Trakt catalogs.
    Top 10 lists fetch up to 10 items; general lists fetch up to 25 items for background variety."""
    source_title = source.get("title", "").lower()
    is_top10 = ("top 10" in source_title or "top ten" in source_title)
    limit = 10 if is_top10 else 25

    provider = source.get("provider")
    posters = []

    # 1. Handle TMDB Lists
    if provider == "tmdb" and source.get("tmdbId"):
        tmdb_list_id = source["tmdbId"]
        url = f"https://api.themoviedb.org/3/list/{tmdb_list_id}?api_key={TMDB_API_KEY}"
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                response = resp.json()
                for item in response.get('items', [])[:limit]:
                    if item.get('poster_path'):
                        posters.append(f"https://image.tmdb.org/t/p/w500{item['poster_path']}")
        except Exception as e:
            print(f"Failed to fetch TMDB list {tmdb_list_id}: {e}")

    # 2. Handle Trakt Lists
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
                    # Collect (tmdb_id, media_type) pairs for concurrent lookup
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
                                return f"https://image.tmdb.org/t/p/w500{data['poster_path']}"
                        return None

                    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                        futures = {executor.submit(_fetch_trakt_tmdb_poster, pair): pair for pair in tmdb_lookups}
                        for future in concurrent.futures.as_completed(futures):
                            try:
                                result = future.result()
                                if result:
                                    posters.append(result)
                            except Exception:
                                pass
            except Exception as e:
                print(f"Failed to fetch Trakt list {trakt_list_id}: {e}")

    return posters, is_top10


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


def create_column_grid(top10_urls, other_urls, save_path):
    """Creates a grid where unique Top 10 posters fill the visible right side first,
    and other library posters fill the rest of the dark background."""
    
    CANVAS_W, CANVAS_H = 3600, 2600
    OUTPUT_W, OUTPUT_H = 1920, 1080
    ANGLE = 12

    # Deduplicate pools
    unique_top10 = list(dict.fromkeys(top10_urls))
    unique_others = [u for u in dict.fromkeys(other_urls) if u not in unique_top10]
    random.shuffle(unique_others)
    
    # Cap background posters to keep download count low (~25 max)
    unique_others = unique_others[:25]

    all_urls_to_download = unique_top10 + unique_others
    if not all_urls_to_download:
        return

    print(f"  Downloading {len(all_urls_to_download)} unique posters concurrently...")
    url_to_img = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        future_to_url = {executor.submit(download_image, url): url for url in all_urls_to_download}
        for future in concurrent.futures.as_completed(future_to_url):
            u = future_to_url[future]
            try:
                img = future.result()
                if img:
                    url_to_img[u] = img
            except Exception:
                pass

    top10_images = [url_to_img[u] for u in unique_top10 if u in url_to_img]
    other_images = [url_to_img[u] for u in unique_others if u in url_to_img]

    if not top10_images:
        return

    # Fallback cycler for the background / left side
    other_cycler = SmartCycler(other_images) if other_images else SmartCycler(top10_images)

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

    # Sort slots from right to left so the visible right side gets filled first
    slots.sort(key=lambda s: (-s[0], s[1]))

    print("  Generating layout (Unique Top 10 on right, others filling the dark left side)...")
    top10_iter = iter(top10_images)

    for c, r, x, y in slots:
        try:
            # Assign unique Top 10 posters to the right-hand side slots first without repeating
            img_raw = next(top10_iter)
        except StopIteration:
            # Once unique Top 10 posters run out, fill the rest of the canvas with other posters
            img_raw = other_cycler.next()

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
    if not TMDB_API_KEY:
        print("Error: TMDB_API_KEY environment variable is not set.")
        exit(1)

    with open(JSON_PATH, 'r') as f:
        config = json.load(f)[0]

    for folder in config.get("folders", []):
        try:
            platform_name = sanitize_filename(folder["title"])
            print(f"\nProcessing {platform_name}...")
            
            top10_posters = []
            other_posters = []

            for source in folder.get("sources", []):
                posters, is_top10 = get_posters_from_source(source)
                if is_top10:
                    top10_posters.extend(posters)
                else:
                    other_posters.extend(posters)

            if len(top10_posters) < MIN_POSTERS:
                print(f"Skipped {platform_name}: Only found {len(top10_posters)} Top 10 posters (need {MIN_POSTERS}+).")
                continue

            save_path = f"nuvio/images/{platform_name}/heroBackdropUrl.jpg"
            create_column_grid(top10_posters, other_posters, save_path)
            print(f"Successfully saved {save_path}")

        except Exception as e:
            print(f"Error processing folder {folder.get('title', '<unknown>')}: {e}")
            continue


if __name__ == "__main__":
    main()