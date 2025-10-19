import requests
import folium
import json
import sys
import time
import atexit
from datetime import datetime
from typing import List, Dict, Tuple, Optional, Union
from collections import Counter
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from geopy.distance import geodesic
from textblob import TextBlob
from colorama import Fore, Style, init
from rich.console import Console
from rich.table import Table
from folium import FeatureGroup, LayerControl, Marker, Popup, Icon, PolyLine
from folium.plugins import MarkerCluster

# --- Constants ---
OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"
DEFAULT_SEARCH_RADIUS_M = 2500
MAX_HISTORY_SIZE = 50

# --- Initialize ---
init(autoreset=True)
console = Console()

MOOD_TAGS_EXTENDED: Dict[str, List[Tuple[str, str]]] = {
    "happy": [("amenity", "cafe"), ("amenity", "restaurant"), ("bar", "yes"), ("shop", "bakery")],
    "sad": [("leisure", "park"), ("amenity", "library"), ("place_of_worship", "church")],
    "adventurous": [("leisure", "amusement_park"), ("sport", "climbing"), ("leisure", "sports_centre")]
}
MOOD_EMOJIS: Dict[str, str] = {"happy": "😊", "sad": "😔", "adventurous": "🧗"}
MOOD_COLOR: Dict[str, str] = {"happy": "lightgreen", "sad": "blue", "adventurous": "orange"}
MOOD_MARKER_ICONS: Dict[str, str] = {"happy": "coffee", "sad": "leaf", "adventurous": "flag"}

MEMORY_FILE = "hangai_user_profile.json"
CACHE_FILE = "hangai_cache.json"

# In-memory cache
CACHE: Dict[Tuple[float, float, str, int], List[Dict]] = {}

# --- Cache Handling ---
def save_cache() -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump({str(k): v for k, v in CACHE.items()}, f)

def load_cache() -> None:
    global CACHE
    try:
        with open(CACHE_FILE, "r") as f:
            tmp = json.load(f)
            CACHE = {eval(k): v for k, v in tmp.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        CACHE = {}

atexit.register(save_cache)
load_cache()

# --- Profile & Usage Handling ---
def load_user_profile() -> Dict:
    try:
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"history": [], "favorites": {}, "mood_usage": {}, "place_usage": {}}

def save_user_profile(profile: Dict) -> None:
    with open(MEMORY_FILE, "w") as f:
        json.dump(profile, f, indent=2)

def update_usage_counters(profile: Dict, mood: str, place_name: str) -> None:
    profile.setdefault("mood_usage", {})
    profile.setdefault("place_usage", {})
    profile["mood_usage"][mood] = profile["mood_usage"].get(mood, 0) + 1
    profile["place_usage"][place_name] = profile["place_usage"].get(place_name, 0) + 1

# --- Robust Geocode with retry and timeout ---
def retry_geocode(address: str, retries: int = 5, initial_delay: float = 2.0, timeout: float = 10.0) -> Optional[Tuple[float, float]]:
    geolocator = Nominatim(user_agent="HangAI_Robust")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1, max_retries=1)
    delay = initial_delay
    for attempt in range(1, retries +1):
        try:
            location = geocode(address, timeout=timeout)
            if location:
                return (location.latitude, location.longitude)
            else:
                console.print(f"[yellow]Attempt {attempt}: Location not found, retrying...[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Attempt {attempt}: Geocoding error: {e}, retrying after {delay} seconds...[/yellow]")
        time.sleep(delay)
        delay *= 2
    console.print(f"[red]Failed to geocode address '{address}' after {retries} attempts.[/red]")
    return None

# --- Mood Detection ---
def detect_moods_from_text(user_input: str) -> List[str]:
    lowered = user_input.lower()
    moods = [mood for mood in MOOD_TAGS_EXTENDED if mood in lowered]
    if not moods:
        polarity = TextBlob(user_input).sentiment.polarity
        if polarity > 0.4:
            moods = ["happy"]
        elif polarity < -0.2:
            moods = ["sad"]
        else:
            moods = ["adventurous"]
    return list(set(moods))

def manual_mood_selection(available_moods: List[str]) -> List[str]:
    console.print("Select moods by number or name (comma separated), or press Enter for automatic:")
    for i, mood in enumerate(available_moods, 1):
        console.print(f"{i}. {mood}")
    user_input = input("Your selection: ").strip().lower()
    if not user_input:
        return available_moods[:1]  # fallback to first mood automatically
    selected = set()
    for token in user_input.replace(',', ' ').split():
        if token.isdigit():
            idx = int(token) -1
            if 0 <= idx < len(available_moods):
                selected.add(available_moods[idx])
        elif token in available_moods:
            selected.add(token)
    return list(selected) if selected else available_moods[:1]

# --- Overpass API fetch ---
def fetch_overpass(coords: Tuple[float, float], mood: str, radius_m: int) -> List[Dict]:
    lat, lon = coords
    cache_key = (round(lat, 4), round(lon, 4), mood, radius_m)
    if cache_key in CACHE:
        console.print(f"[cyan]Using cached results for mood '{mood}'[/cyan]")
        return CACHE[cache_key]
    tags = MOOD_TAGS_EXTENDED[mood]
    query = "[out:json];("
    for key, val in tags:
        query += f"node['{key}'='{val}'](around:{radius_m},{lat},{lon});"
        query += f"way['{key}'='{val}'](around:{radius_m},{lat},{lon});"
    query += ");out center;"
    console.print(f"[cyan]Fetching places for mood '{mood}'...[/cyan]")

    data = None
    retries = 3
    for attempt in range(1,retries+1):
        try:
            response = requests.get(OVERPASS_API_URL, params={"data": query}, timeout=30)
            response.raise_for_status()
            data = response.json()
            break
        except requests.RequestException as e:
            console.print(f"[red]Attempt {attempt} failed: {e}[/red]")
            if attempt < retries:
                time.sleep(2 ** attempt)
    if data is None:
        console.print("[red]Failed to retrieve places after retries.[/red]")
        return []
    places = []
    for el in data.get("elements", []):
        name = el.get("tags", {}).get("name", "Unnamed")
        lat_p = el.get("lat") or el.get("center", {}).get("lat")
        lon_p = el.get("lon") or el.get("center", {}).get("lon")
        tags_el = el.get("tags", {})
        if lat_p and lon_p:
            dist = geodesic(coords, (lat_p, lon_p)).km
            places.append({"name": name, "lat": lat_p, "lon": lon_p, "distance": dist, "tags": tags_el})
    places.sort(key=lambda x: x["distance"])
    CACHE[cache_key] = places
    return places

def MoodInPlace(place_tags: Dict[str,str], mood: str) -> bool:
    for key, val in MOOD_TAGS_EXTENDED[mood]:
        if place_tags.get(key) == val:
            return True
    return False

# --- Visualization ---
def create_map(user_loc: Tuple[float,float], moods: List[str], places: List[Dict], radius_m: int, favorites: Optional[Dict[str,List[Dict]]] = None) -> str:
    m = folium.Map(location=user_loc, zoom_start=14)
    folium.Marker(user_loc, popup="You are here", icon=Icon(color="red", icon="user")).add_to(m)
    folium.Circle(location=user_loc, radius=radius_m, color="purple", fill=True, fill_opacity=0.1,
                  popup=f"Search radius: {radius_m/1000:.2f} km").add_to(m)

    marker_clusters = {mood: MarkerCluster(name=f"{mood.title()} {MOOD_EMOJIS[mood]}").add_to(m) for mood in moods}
    dash_styles = {"happy": "5,5", "sad": "1,5", "adventurous": "10,5"}

    for mood in moods:
        mood_places = [p for p in places if MoodInPlace(p["tags"], mood)]
        for idx, place in enumerate(mood_places):
            is_favorite = favorites and any(fav.get("name") == place["name"] for fav in favorites.get(mood, []) if isinstance(fav, dict))
            popup_text = f"{MOOD_EMOJIS[mood]} <b>{place['name']}</b><br>Distance: {place['distance']:.2f} km"
            popup = Popup(popup_text, max_width=300)
            marker = Marker(location=(place["lat"], place["lon"]),
                            popup=popup,
                            icon=Icon(color=MOOD_COLOR[mood], icon=MOOD_MARKER_ICONS[mood], prefix='fa' if not is_favorite else 'glyphicon'))
            marker.add_to(marker_clusters[mood])

            if idx < 3:
                PolyLine(locations=[user_loc, (place["lat"], place["lon"])],
                         color=MOOD_COLOR[mood], weight=3, dash_array=dash_styles[mood]).add_to(m)

    LayerControl(collapsed=False).add_to(m)
    filename = "hangai_map.html"
    m.save(filename)
    console.print(f"[green]🗺️ Map saved as {filename} - open it in your browser![/green]")
    return filename

# --- Display Table ---
def display_places_table(places: List[Dict]) -> None:
    table = Table(title="Nearby Places")
    table.add_column("Name", style="cyan")
    table.add_column("Distance (km)", justify="right", style="magenta")
    table.add_column("Type/Tags", style="yellow")

    if not places:
        console.print("[yellow]No places found to show.[/yellow]")
        return

    closest_dist = places[0]["distance"]
    for place in places[:10]:
        style = "bold green" if place["distance"] == closest_dist else ""
        tags_str = ", ".join(set(place["tags"].values())) if place["tags"] else "-"
        table.add_row(place["name"], f"{place['distance']:.2f}", tags_str, style=style)

    console.print(table)

# --- History & Favorites ---
def save_history(profile: Dict, entry: Dict) -> None:
    profile["history"].append(entry)
    if len(profile["history"]) > MAX_HISTORY_SIZE:
        profile["history"] = profile["history"][-MAX_HISTORY_SIZE:]
    save_user_profile(profile)

def remove_favorite(profile: Dict, mood: str, place_name: str) -> None:
    if mood in profile.get("favorites", {}):
        profile["favorites"][mood] = [fav for fav in profile["favorites"][mood] if fav.get("name") != place_name]
        save_user_profile(profile)
        console.print(f"[green]Removed favorite '{place_name}' from mood '{mood}'[/green]")

def select_favorite_place(favorites: Dict[str, List[Dict]]) -> Optional[Tuple[str, str]]:
    if not favorites:
        console.print("[yellow]No favorites saved yet.[/yellow]")
        return None
    console.print("[bold underline]Choose mood category for favorites:[/bold underline]")
    moods = list(favorites.keys())
    for i, mood in enumerate(moods, 1):
        console.print(f"{i}. {mood.title()} ({len(favorites[mood])} places)")
    try:
        choice = int(input("Select mood by number (0 to cancel): "))
        if choice == 0:
            return None
        selected_mood = moods[choice - 1]
    except Exception:
        console.print("[red]Invalid selection.[/red]")
        return None
    places = favorites[selected_mood]
    console.print(f"[bold]Places under {selected_mood.title()} mood:[/bold]")
    for i, fav in enumerate(places, 1):
        console.print(f"{i}. {fav['name']}")
    try:
        place_choice = int(input("Select a place by number (0 cancel, -1 to remove): "))
        if place_choice == 0:
            return None
        elif place_choice == -1:
            rem_choice = int(input("Enter number to remove: "))
            if 1 <= rem_choice <= len(places):
                remove_favorite(favorites, selected_mood, places[rem_choice - 1]["name"])
            return None
        elif 1 <= place_choice <= len(places):
            return (selected_mood, places[place_choice - 1]["name"])
        else:
            console.print("[red]Invalid choice.[/red]")
            return None
    except Exception:
        console.print("[red]Invalid input.[/red]")
        return None

# --- Main ---
def main() -> None:
    console.print("[bold magenta]🌍 Welcome to HangAI — Enhanced Mood-Based Hangout Recommender![/bold magenta]")
    profile = load_user_profile()

    user_loc = None
    while user_loc is None:
        loc_input = input("Enter your location (address or city): ")
        user_loc = retry_geocode(loc_input)

    while True:
        console.print("\n[bold cyan]Options:[/bold cyan]")
        console.print("1️⃣ Get Recommendations")
        console.print("2️⃣ View History")
        console.print("3️⃣ Manage Favorites")
        console.print("4️⃣ Exit")
        choice = input("Choose (1/2/3/4): ").strip()

        if choice == "1":
            user_input = input("Describe your mood(s): ")
            auto_moods = detect_moods_from_text(user_input)
            moods = manual_mood_selection(list(MOOD_TAGS_EXTENDED.keys()))
            if not moods:
                moods = auto_moods
            console.print(f"[green]Using moods:[/green] {', '.join(f'{m} {MOOD_EMOJIS[m]}' for m in moods)}")
            try:
                radius_input = input(f"Enter search radius km (default {DEFAULT_SEARCH_RADIUS_M/1000}): ")
                radius_m = int(float(radius_input) * 1000) if radius_input else DEFAULT_SEARCH_RADIUS_M
            except Exception:
                radius_m = DEFAULT_SEARCH_RADIUS_M

            all_places = []
            for mood in moods:
                all_places.extend(fetch_overpass(user_loc, mood, radius_m))

            unique_places = {}
            for p in all_places:
                if p["name"] not in unique_places or p["distance"] < unique_places[p["name"]]["distance"]:
                    unique_places[p["name"]] = p
            combined_places = sorted(unique_places.values(), key=lambda x: x["distance"])

            if not combined_places:
                console.print("[yellow]No places found. Try increasing radius or selecting different moods.[/yellow]")
                continue

            display_places_table(combined_places)
            create_map(user_loc, moods, combined_places, radius_m, favorites=profile.get("favorites"))

            history_entry = {"input": user_input, "detected_moods": moods, "timestamp": datetime.now().isoformat()}
            save_history(profile, history_entry)

            for mood in moods:
                fav_list = profile.setdefault("favorites", {}).setdefault(mood, [])
                for place in combined_places:
                    if MoodInPlace(place["tags"], mood):
                        if not any(fav["name"] == place["name"] for fav in fav_list if isinstance(fav, dict)):
                            fav_list.append({"name": place["name"], "tags": place["tags"]})
                            update_usage_counters(profile, mood, place["name"])
                            break

            save_user_profile(profile)

        elif choice == "2":
            if not profile.get("history"):
                console.print("[yellow]No history yet.[/yellow]")
                continue
            console.print("[bold underline]Search History:[/bold underline]")
            for idx, entry in enumerate(profile["history"], 1):
                ts = datetime.fromisoformat(entry["timestamp"]).strftime("%Y-%m-%d %H:%M")
                moods_str = ', '.join(entry.get("detected_moods", []))
                console.print(f"{idx}. [{ts}] Moods: {moods_str} - Input: {entry['input']}")
            sel = input("Enter history number to repeat search or press Enter to continue: ")
            if sel.isdigit():
                index = int(sel) - 1
                if 0 <= index < len(profile["history"]):
                    search = profile["history"][index]
                    moods = search.get("detected_moods", [])
                    combined_places = []
                    radius_m = DEFAULT_SEARCH_RADIUS_M
                    for mood in moods:
                        combined_places.extend(fetch_overpass(user_loc, mood, radius_m))
                    display_places_table(combined_places)
                    create_map(user_loc, moods, combined_places, radius_m, favorites=profile.get("favorites"))

        elif choice == "3":
            if not profile.get("favorites"):
                console.print("[yellow]No favorites saved.[/yellow]")
                continue
            sel = select_favorite_place(profile["favorites"])
            if sel:
                selected_mood, place_name = sel
                coordinates = retry_geocode(place_name)
                if coordinates:
                    radius_m = DEFAULT_SEARCH_RADIUS_M
                    places = fetch_overpass(coordinates, selected_mood, radius_m)
                    if places:
                        display_places_table(places)
                        create_map(coordinates, [selected_mood], places, radius_m, favorites=profile.get("favorites"))
                    else:
                        console.print("[yellow]No places found for the selected favorite location and mood.[/yellow]")
                else:
                    console.print("[red]Could not find coordinates for selected place.[/red]")

        elif choice == "4":
            save_cache()
            console.print("[green]Goodbye! Enjoy your hangouts! 👋[/green]")
            break

        else:
            console.print("[red]Invalid input. Please select 1, 2, 3, or 4.[/red]")

if __name__ == "__main__":
    main()
