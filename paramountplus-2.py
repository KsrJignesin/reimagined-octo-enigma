# Created on: 01-01-2024
# Version: 3.2.0

import re
import sys
import logging
from typing import Any, Optional
from urllib.parse import urljoin
from pathlib import Path

import click
import httpx
import yaml

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.objects.tracks import AudioTrack, MenuTrack, TextTrack, VideoTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.widevine.device import LocalDevice
from requests.adapters import HTTPAdapter, Retry
from vinetrimmer.config import config

logging = __import__('logging')
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)


class ParamountPlus(BaseService):
    """
    Service code for Paramount+ streaming service (https://paramountplus.com).
    
    \b
    Authorization: Credentials (Android Login)
    Security Levels:
    - WV L3: up to sd 540p eac3
    - PR SL3000/SL2000: up to 4K

    \b
    Example:
    Test countries ：DE 
    vt dl PMTP https://www.paramountplus.com/shows/dutton-ranch/
    vt dl -al en -sl en -w S01E01 PMTP https://www.paramountplus.com/shows/dutton-ranch/
    vt dl -al ja -sl en -w S01E01 PMTP https://www.paramountplus.com/movies/video/jP6__HtMzdrdmapPqhH_ZpzxP4r8UgtT/
    
    4K test
    vt dl -v H265 -r HDR -al en -sl en -w S01E01 PMTP https://www.paramountplus.com/shows/dutton-ranch/
    """

    ALIASES = ["PMTP", "paramountplus", "paramount+"]
    TITLE_RE = r"https?://(?:www\.)?paramountplus\.com(?:/[a-z]{2})?/(?P<type>movies|shows)/(?P<p1>[a-zA-Z0-9_-]+)(?:/(?P<p2>[a-zA-Z0-9_-]+))?"

    @staticmethod
    @click.command(name="ParamountPlus", short_help="https://paramountplus.com")
    @click.argument("title", type=str)
    @click.option("-r", "--region", default=None, help="Specify region (us, intl, fr)")
    @click.option("-c", "--clips", is_flag=True, default=False, help="Download clips instead of full episodes")
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a Movie")
    @click.pass_context
    def cli(ctx: click.Context, **kwargs):
        return ParamountPlus(ctx, **kwargs)
    
    def __init__(self, ctx: click.Context, title: str, region: str = None, clips: bool = False, movie: bool = False):
        super().__init__(ctx)
        self.title = title
        self.region = region
        self.clips = clips
        self.movie = movie
        
        # Load config first so we can use user_agent
        self._load_config()
        
        # Initialize HTTP client with config user_agent
        self.client = httpx.Client(
            headers={
                "User-Agent": self.config.get("user_agent", "Paramount+/16.13.0 (com.cbs.ca; build:420000599; Android SDK 34; androidphone; sdk_gphone64_x86_64) okhttp/5.3.2"),
                "Accept": "application/json, text/plain, */*",
            },
            timeout=30.0,
            follow_redirects=True,
        )
        
        # Detect or set region
        self._setup_region()
        
        # P0: irdeto session cache (content_id → response dict)
        self._session_cache: dict = {}
        
        # P2: cached user info from status response
        self._user_info: dict = {}
        
        # Authentication
        self._authenticate()

    def _load_config(self):
        """Load configuration from YAML file."""
        config_path = Path(__file__).resolve().parents[1] / "config" / "services" / "paramountplus.yml"
        
        if not config_path.exists():
            raise self.log.exit(f" - Configuration file not found at: {config_path}")
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)
            self.log.debug(f" + Loaded config from: {config_path}")
        except Exception as e:
            raise self.log.exit(f" - Error loading config from {config_path}: {e}")

    def _setup_region(self):
        """Setup region configuration."""
        if not self.region:
            # Auto-detect region
            try:
                ip_info = self.client.get("https://ipinfo.io/json").json()
                country = ip_info.get("country", "US").upper()
                self.log.info(f" + Detected region: {country}")
                
                if country == "US":
                    self.region = "US"
                elif country == "FR":
                    self.region = "FR"
                else:
                    self.region = "INTL"
            except Exception as e:
                self.log.debug(f" + Region detection failed: {e}")
                self.log.warning(" + Could not detect region, using US as default.")
                self.region = "US"
        else:
            self.region = self.region.upper()
        
        # Get region config
        regions = self.config.get("regions", {})
        
        if "regions" not in self.config:
            raise self.log.exit(" - No 'regions' section found in configuration file")
            
        if self.region in regions:
            region_key = self.region
        elif self.region == "FR":
            region_key = "FR"
        else:
            region_key = "INTL" if "INTL" in regions else "US" if "US" in regions else list(regions.keys())[0]
        
        self.log.info(f" + Using configuration for: {region_key}")
        self.region_config = regions[region_key]
        
        if not self.region_config:
            raise self.log.exit(f" - No configuration found for region {region_key}")
            
        self.at_token = self.region_config.get("at_token")
        
        if not self.at_token:
            raise self.log.exit(" - at_token not found in region configuration")

    def _authenticate(self):
        """Authenticate with Paramount+."""
        if not self.credentials:
            if self.region != "US":
                self.log.warning(" - INTL/FR regions usually require credentials")
            return
        
        self.log.info(" + Logging in...")
        
        login_url = self.region_config["endpoints"]["login"]

        # FIX: 'at' goes as query param; credentials go as form-encoded body
        response = self.client.post(
            login_url,
            params={"at": self.at_token},
            data={
                "j_username": self.credentials.username,
                "j_password": self.credentials.password,
                "j_rememberMe": "true",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        
        if response.status_code != 200:
            self.log.error(f" - Login error: {response.status_code}")
            if response.text:
                try:
                    error_json = response.json()
                    self.log.error(f" - Error: {error_json.get('errorCode', 'Unknown')} - {error_json.get('message', '')}")
                except Exception:
                    self.log.error(f" - Response: {response.text[:200]}")
            raise self.log.exit(" - Login failed. Check your credentials.")

        data = response.json()
        if not data.get("success"):
            self.log.error(f" - Login rejected: {data.get('message', data.get('errorMessage', 'Unknown'))}")
            raise self.log.exit(" - Login failed. Check your credentials.")

        self.log.info(" + Login successful")
        
        status_url = self.region_config["endpoints"]["status"]
        status_response = self.client.get(status_url, params={"at": self.at_token})
        
        if status_response.status_code != 200:
            raise self.log.exit(" - Failed to verify login status")
        
        status_data = status_response.json()
        
        if not status_data.get("isLoggedIn") and not status_data.get("success"):
            raise self.log.exit(" - Failed to verify login status (not authenticated).")

        name = (
            status_data.get("firstName")
            or status_data.get("username")
            or status_data.get("email")
            or "Unknown"
        )
        self.log.info(f" + Session started for: {name}")
        self.logged_in = True
        
        # P2: Cache user info for later use (playability _clientRegion, etc.)
        self._cache_user_info(status_data)

    def _cache_user_info(self, status_data: dict):
        ent = status_data.get("entitlement", {})
        
        # packageCode may be directly under entitlement, or under packageInfo (list or dict)
        package_code = ent.get("packageCode", "")
        if not package_code:
            pkg_info = ent.get("packageInfo", {})
            if isinstance(pkg_info, dict):
                # Could be a dict directly, or a dict with numeric keys
                if "packageCode" in pkg_info:
                    package_code = pkg_info["packageCode"]
                else:
                    # Try first value if dict-of-dicts
                    for v in (pkg_info.values() if pkg_info else []):
                        if isinstance(v, dict) and v.get("packageCode"):
                            package_code = v["packageCode"]
                            break
            elif isinstance(pkg_info, list) and pkg_info:
                package_code = pkg_info[0].get("packageCode", "") if isinstance(pkg_info[0], dict) else ""
        
        # planType similarly
        plan_type = ""
        prod_info = ent.get("productInfo", {})
        if isinstance(prod_info, dict):
            plan_type = prod_info.get("planType", "")
            if not plan_type:
                for v in (prod_info.values() if prod_info else []):
                    if isinstance(v, dict) and v.get("planType"):
                        plan_type = v["planType"]
                        break
        elif isinstance(prod_info, list) and prod_info:
            plan_type = prod_info[0].get("planType", "") if isinstance(prod_info[0], dict) else ""
        
        # activeProfile may be dict or list
        active_profile = status_data.get("activeProfile", {})
        if isinstance(active_profile, list):
            active_profile = active_profile[0] if active_profile else {}
        active_profile_id = str(active_profile.get("id", "")) if isinstance(active_profile, dict) else ""
        
        self._user_info = {
            "user_id": str(status_data.get("userId", "")),
            "user_registration_country": status_data.get("userRegistrationCountry", ""),
            "package_code": package_code,
            "plan_type": plan_type,
            "active_profile_id": active_profile_id,
            "sl": status_data.get("sl", ""),
        }
        self.log.debug(f" + Cached user info: uid={self._user_info['user_id']} country={self._user_info['user_registration_country']} pkg={self._user_info['package_code']}")
        
    def _get_params(self, extra: dict = None) -> dict:
        """Get base parameters for API calls."""
        params = {"at": self.at_token}
        if extra:
            params.update(extra)
        return params

    def get_titles(self):
        """Get title(s) from URL."""
        # If movie flag is set, treat as movie
        if self.movie:
            match = re.search(self.TITLE_RE, self.title)
            if match:
                p1 = match.group("p1")
                p2 = match.group("p2")
                content_id = p2 if p2 else p1
            else:
                content_id = self.title
            return self._get_movie(content_id)
        
        # Otherwise parse as show
        match = re.search(self.TITLE_RE, self.title)
        if not match:
            raise self.log.exit(f" - Could not parse URL: {self.title}")
        
        kind = match.group("type")
        p1 = match.group("p1")
        p2 = match.group("p2")
        
        if kind == "movies":
            content_id = p2 if p2 else p1
            return self._get_movie(content_id)
        else:  # shows
            content_id = p1
            return self._get_series(content_id)

    def _get_movie(self, content_id: str):
        """Get movie information."""
        url = self.region_config["endpoints"]["movie"].format(title_id=content_id)
        
        params = self._get_params({
            "includeTrailerInfo": "true",
            "includeContentInfo": "true",
            "locale": "en-us"
        })
        
        response = self.client.get(url, params=params)
        
        if response.status_code == 404:
            raise self.log.exit(f" - Movie not found with ID: {content_id}")
        
        response.raise_for_status()
        data = response.json()
        
        if not data.get("success"):
            raise self.log.exit(f" - Error getting movie: {data.get('message', 'Unknown error')}")
        
        movie_data = data.get("movie", {}).get("movieContent", data)
        content_id = movie_data.get("contentId") or movie_data.get("content_id")
        
        title_name = movie_data.get("title") or movie_data.get("label", "Unknown")
        year = movie_data.get("_airDateISO", "")[:4]
        
        self.log.info(f" + Movie found: {title_name} ({year}) - ID: {content_id}")
        
        return [
            Title(
                id_=content_id,
                type_=Title.Types.MOVIE,
                name=title_name,
                year=year,
                original_lang="en",
                source=self.ALIASES[0],
                service_data=movie_data,
            )
        ]

    def _get_series(self, content_id: str):
        """Get series episodes or clips."""
        # P0: Support numeric show ID in addition to slug
        endpoints = self.region_config["endpoints"]
        if content_id.isdigit():
            url = endpoints.get("shows_by_id", endpoints["shows"]).format(title=content_id)
        else:
            url = endpoints["shows"].format(title=content_id)
        
        response = self.client.get(url, params=self._get_params())
        
        if response.status_code == 404:
            raise self.log.exit(f" - Show not found with ID: {content_id}")
        
        response.raise_for_status()
        data = response.json()
        
        if not data.get("success"):
            raise self.log.exit(f" - Error getting show: {data.get('message', 'Unknown error')}")
        
        # Get show_id
        show_id = None
        show_info = None
        
        show_results = data.get("show", {}).get("results", [])
        for result in show_results:
            if result.get("type") == "show":
                show_id = result.get("show_id") or result.get("id")
                show_info = result
                break
        
        if not show_id:
            show_id = data.get("show_id") or data.get("id")
        
        if not show_id:
            for key in ["cbsShowId", "showId", "seriesId"]:
                if data.get(key):
                    show_id = data[key]
                    break
        
        if not show_id:
            raise self.log.exit(" - Could not find show_id")
        
        self.log.info(f" + Found show_id: {show_id}")
        
        # Get seasons
        seasons = self._get_seasons(data, show_id)
        self.log.info(f" + Seasons to fetch: {seasons[:10]}{'...' if len(seasons) > 10 else ''}")
        
        # Determine content type based on clips flag
        clips_mode = getattr(self, 'clips', False)
        
        if clips_mode:
            self.log.info(" + Clip mode enabled - searching for clips")
        
        titles = []
        
        # P0: FIRST — try independent menu endpoint
        menu_configs = []
        menu_data = self._fetch_menu(show_id)
        if menu_data:
            menu_configs = self._extract_configs_from_menu(menu_data, clips_mode)
        
        # Fallback: try show data's showMenu (usually empty for INTL but may work for US)
        if not menu_configs:
            menu_configs = self._extract_configs_from_menu(data, clips_mode)
        
        if menu_configs:
            self.log.debug(f" + Found menu configs: {menu_configs}")
            titles = self._fetch_from_configs(show_id, menu_configs, seasons, show_info, clips_mode)
        
        # SECOND: If no titles found, try fallback configs
        if not titles:
            fallback_configs = self._get_fallback_configs(clips_mode)
            self.log.debug(f" + Trying fallback configs: {fallback_configs}")
            titles = self._fetch_from_configs(show_id, fallback_configs, seasons, show_info, clips_mode)
        
        if not titles:
            if not clips_mode:
                self.log.warning(" + No full episodes found. Try using --clips flag for clips")
            else:
                self.log.warning(" + No clips found. Try without --clips flag for full episodes")
            raise self.log.exit(" - No content found")

        # FIX: final deduplication pass by content ID before returning
        seen_ids = set()
        unique_titles = []
        for t in titles:
            if t.id not in seen_ids:
                seen_ids.add(t.id)
                unique_titles.append(t)

        if len(unique_titles) < len(titles):
            self.log.debug(f" + Deduplicated {len(titles) - len(unique_titles)} duplicate title(s)")

        titles = unique_titles
        
        # Sort by season and episode
        titles.sort(key=lambda x: (x.season or 0, x.episode or 0))
        
        # Log season statistics
        season_counts = {}
        for t in titles:
            season_counts[t.season] = season_counts.get(t.season, 0) + 1
        
        season_info = ", ".join([f"{s} ({c})" for s, c in sorted(season_counts.items())])
        
        return titles

    def _fetch_menu(self, show_id: str) -> Optional[dict]:
        """P0: Fetch show menu from independent endpoint."""
        menu_url_template = self.region_config["endpoints"].get("menu")
        if not menu_url_template:
            return None
        
        menu_url = menu_url_template.format(show_id=show_id)
        
        try:
            resp = self.client.get(menu_url, params=self._get_params({"locale": "en-us"}))
            if resp.status_code == 200:
                data = resp.json()
                if data.get("showMenu"):
                    self.log.debug(f" + Got menu from independent endpoint ({len(data['showMenu'])} items)")
                    return data
        except Exception as e:
            self.log.debug(f" + Menu endpoint failed: {e}")
        
        return None

    def _extract_configs_from_menu(self, data: dict, clips_mode: bool) -> list:
        """Extract video config names from show menu data."""
        configs = []
        show_menu = data.get("showMenu", [])
        
        # Keywords for episodes vs clips
        episode_keywords = ["Episodes", "Full Episodes", "Episodios", "Fight Selector"]
        clip_keywords = ["Clips", "Match Replays", "Highlights", "Most Recent Clips"]
        
        target_keywords = clip_keywords if clips_mode else episode_keywords
        
        for menu_item in show_menu:
            links = menu_item.get("links", [])
            for link in links:
                title_text = link.get("title", "").strip()
                
                # Check if title matches any target keyword
                for keyword in target_keywords:
                    if keyword.lower() in title_text.lower():
                        config_name = link.get("videoConfigUniqueName")
                        if config_name and config_name not in configs:
                            configs.append(config_name)
                            self.log.debug(f" + Found config from menu: {title_text} -> {config_name}")
        
        return configs

    def _get_fallback_configs(self, clips_mode: bool) -> list:
        """Get fallback config names to try."""
        if clips_mode:
            return [
                "DEFAULT_APPS_MOST_RECENT_CLIPS",
                "SPORTS_SHOW_LANDING_CLIPS",
                "clips",
                "CLIPS",
                "most-recent-clips"
            ]
        else:
            # P0: INTL_SHOW_LANDING first for non-US regions
            base = []
            if self.region != "US":
                base.append("INTL_SHOW_LANDING")
            base.extend([
                "FULL_EPISODES",
                "full-episodes",
                "fullepisodes",
                "episodes",
                "EPISODES",
                "DEFAULT_APPS_MOST_RECENT_EPISODES",
                "DEFAULT_APPS_FULL_EPISODES",
                "SPORTS_SHOW_LANDING_EPISODES",
            ])
            return base

    def _get_seasons(self, data: dict, show_id: str) -> list:
        """Get available seasons."""
        seasons = []
        
        # Method 1: available_video_seasons
        available_seasons = data.get("available_video_seasons", {})
        if isinstance(available_seasons, dict):
            items = available_seasons.get("itemList", [])
            for season in items:
                if season.get("seasonNum"):
                    seasons.append(str(season["seasonNum"]))
        
        # Method 2: availability API (P1: v3.0)
        if not seasons:
            try:
                device_type = self.config.get("device_type", "androidphone")
                season_endpoint = f"/apps-api/v3.0/{device_type}/shows/{show_id}/video/season/availability.json"
                season_url = urljoin(self.region_config["base_url"], season_endpoint)
                
                season_resp = self.client.get(season_url, params=self._get_params())
                if season_resp.status_code == 200:
                    season_data = season_resp.json()
                    if season_data.get("success") and "video_available_season" in season_data:
                        season_items = season_data["video_available_season"].get("itemList", [])
                        seasons = [str(item.get("seasonNum")) for item in season_items if item.get("seasonNum")]
            except Exception as e:
                self.log.debug(f" + Error fetching seasons from API: {e}")
        
        if not seasons:
            self.log.warning(" + No seasons list found, will discover from content")
            seasons = ["all"]
        
        return seasons

    def _is_full_episode(self, ep: dict) -> bool:
        full_ep = ep.get("fullEpisode")
        if full_ep is True or (isinstance(full_ep, str) and full_ep.lower() == "true") or full_ep == 1:
            return True

        media_type = str(ep.get("mediaType", "")).lower().replace(" ", "").replace("-", "")
        if media_type in ("fullepisode", "episode", "full"):
            return True

        ep_type = str(ep.get("type", "")).lower().replace(" ", "").replace("-", "")
        if ep_type in ("fullepisode", "episode", "full"):
            return True

        if ep.get("seasonNum") and ep.get("episodeNum"):
            clip_flag = ep.get("isClip") or ep.get("clip")
            if clip_flag is not True and str(clip_flag).lower() != "true":
                return True

        return False

    def _is_clip(self, ep: dict) -> bool:
        full_ep = ep.get("fullEpisode")
        if full_ep is False or (isinstance(full_ep, str) and full_ep.lower() == "false") or full_ep == 0:
            return True

        clip_flag = ep.get("isClip") or ep.get("clip")
        if clip_flag is True or (isinstance(clip_flag, str) and clip_flag.lower() == "true"):
            return True

        media_type = str(ep.get("mediaType", "")).lower().replace(" ", "").replace("-", "")
        if media_type in ("clip", "highlight", "replay"):
            return True

        return False

    def _fetch_from_configs(self, show_id: str, configs: list, seasons: list, show_info: dict, clips_mode: bool):
        titles = []
        device_type = self.config.get("device_type", "androidphone")
        
        content_filter = self._is_clip if clips_mode else self._is_full_episode

        seen_ids = set()
        
        first_page_ids = None
        api_ignores_season = False
        
        for config in configs:
            self.log.debug(f" + Trying config: {config}")
            
            config_url = f"/apps-api/v2.0/{device_type}/shows/{show_id}/videos/config/{config}.json"
            config_full_url = urljoin(self.region_config["base_url"], config_url)
            
            try:
                config_resp = self.client.get(
                    config_full_url,
                    params=self._get_params({"platformType": "apps", "rows": "1", "begin": "0"})
                )
                
                if config_resp.status_code != 200:
                    continue
                
                config_data = config_resp.json()
                if not config_data.get("success", True):
                    continue
                
                section_ids = config_data.get("sectionIds", [])
                if not section_ids:
                    for result in config_data.get("results", []):
                        if result.get("id"):
                            section_ids.append(result["id"])
                
                if not section_ids:
                    continue
                
                self.log.debug(f" + Found section_ids: {section_ids} with config {config}")
                
                section_metadata = config_data.get("videoSectionMetadata", [])
                display_seasons = False
                for meta in section_metadata:
                    if meta.get("sectionId") in section_ids:
                        display_seasons = meta.get("display_seasons", False)
                
                for section_id in section_ids:
                    if display_seasons and seasons and seasons[0] != "all":
                        all_items = []
                        
                        for i, season in enumerate(seasons):
                            if season == "all":
                                continue
                            
                            items = self._fetch_items_from_section(section_id, show_id, season)
                            
                            if i == 0:
                                first_page_ids = {item.get("contentId") or item.get("content_id") or item.get("id") for item in items}
                            elif not api_ignores_season and first_page_ids:
                                current_ids = {item.get("contentId") or item.get("content_id") or item.get("id") for item in items}
                                overlap = len(first_page_ids & current_ids)
                                if len(first_page_ids) > 0 and overlap / len(first_page_ids) > 0.8:
                                    api_ignores_season = True
                                    self.log.debug(f" + API appears to ignore seasonNum param (overlap: {overlap}/{len(first_page_ids)}), switching to client-side filter")
                                    all_items = self._fetch_items_from_section(section_id, show_id, None)
                                    break
                            
                            all_items.extend(items)
                        
                        if api_ignores_season:
                            season_set = set(str(s) for s in seasons if s != "all")
                            for item in all_items:
                                ep_id = item.get("contentId") or item.get("content_id") or item.get("id")
                                item_season = str(item.get("seasonNum") or item.get("seasonNumber") or "")
                                if ep_id and ep_id not in seen_ids and item_season in season_set and content_filter(item):
                                    seen_ids.add(ep_id)
                                    titles.append(self._create_title_from_episode(item, show_info))
                        else:
                            for item in all_items:
                                ep_id = item.get("contentId") or item.get("content_id") or item.get("id")
                                if ep_id and ep_id not in seen_ids and content_filter(item):
                                    seen_ids.add(ep_id)
                                    titles.append(self._create_title_from_episode(item, show_info))
                    else:
                        items = self._fetch_items_from_section(section_id, show_id, None)
                        for item in items:
                            ep_id = item.get("contentId") or item.get("content_id") or item.get("id")
                            if ep_id and ep_id not in seen_ids and content_filter(item):
                                seen_ids.add(ep_id)
                                titles.append(self._create_title_from_episode(item, show_info))
                
                if titles:
                    return titles
                    
            except Exception as e:
                self.log.debug(f" + Error with config {config}: {e}")
                import traceback
                self.log.debug(traceback.format_exc())
                continue
        
        return titles
              
    def _fetch_items_from_section(self, section_id: str, show_id: str, season_num: str = None):
        """Fetch items (episodes/clips) from a section ID."""
        items = []
        device_type = self.config.get("device_type", "androidphone")
        
        section_url = f"/apps-api/v2.0/{device_type}/videos/section/{section_id}.json"
        full_url = urljoin(self.region_config["base_url"], section_url)
        
        page = 0
        # P1: rows=30 (matches APP behavior; API response has no itemCount)
        rows = self.config.get("streaming", {}).get("rows", 30)
        
        while True:
            params = self._get_params({
                "begin": str(page * rows),
                "rows": str(rows),
                "locale": "en-us"
            })
            
            # P1: Double-pass seasonNum (both direct param and params=)
            if season_num:
                params["seasonNum"] = season_num
                params["params"] = f"seasonNum={season_num}"
            
            self.log.debug(f" + Fetching section {section_id} page {page}" + (f" season {season_num}" if season_num else ""))
            
            try:
                resp = self.client.get(full_url, params=params)
                if resp.status_code != 200:
                    self.log.debug(f" + Section request failed: {resp.status_code}")
                    break
                
                data = resp.json()
                page_items = []
                
                # Method 1: results array (INTL)
                if "results" in data:
                    for result in data["results"]:
                        section_items = result.get("sectionItems", {})
                        item_list = section_items.get("itemList", [])
                        if item_list:
                            page_items.extend(item_list)
                
                # Method 2: sectionItems direct
                elif "sectionItems" in data:
                    item_list = data["sectionItems"].get("itemList", [])
                    if item_list:
                        page_items.extend(item_list)
                
                # Method 3: itemList direct
                elif "itemList" in data:
                    page_items = data["itemList"]
                
                if not page_items:
                    self.log.debug(f" + No items found on page {page}")
                    break
                
                items.extend(page_items)
                
                # P1: itemCount may not exist in response; rely on page_items < rows
                total_count = None
                if "results" in data and data["results"]:
                    total_count = data["results"][0].get("sectionItems", {}).get("itemCount", 0)
                elif "sectionItems" in data:
                    total_count = data["sectionItems"].get("itemCount", 0)
                elif "itemCount" in data:
                    total_count = data["itemCount"]
                
                if page == 0:
                    self.log.debug(f" + Section {section_id} itemCount={total_count}, page_items={len(page_items)}")
                
                # Prefer itemCount if available
                if total_count and len(items) >= total_count:
                    self.log.debug(f" + Reached total count: {total_count}")
                    break
                
                # P1: Fallback — last page when fewer items than rows
                if len(page_items) < rows:
                    self.log.debug(f" + Last page reached (got {len(page_items)} < {rows})")
                    break
                
                page += 1
                
                if page > 50:
                    self.log.warning(f" + Too many pages, stopping at {page}")
                    break
                
            except Exception as e:
                self.log.error(f" + Error fetching section {section_id}: {e}")
                import traceback
                self.log.debug(traceback.format_exc())
                break
        
        self.log.debug(f" + Returning {len(items)} items from section {section_id}")
        return items
        
    def _get_section_from_config(self, show_id: str, config: str):
        """Get section ID from config name."""
        device_type = self.config.get("device_type", "androidphone")
        config_url = f"/apps-api/v2.0/{device_type}/shows/{show_id}/videos/config/{config}.json"
        config_full_url = urljoin(self.region_config["base_url"], config_url)
        
        try:
            resp = self.client.get(
                config_full_url,
                params=self._get_params({"platformType": "apps", "rows": "1", "begin": "0"})
            )
            if resp.status_code == 200:
                data = resp.json()
                section_ids = data.get("sectionIds", [])
                if section_ids:
                    return str(section_ids[0])
        except Exception:
            pass
        
        return None

    def _create_title_from_episode(self, ep: dict, show_info: dict):
        """Create a Title object from episode/clip data."""
        ep_id = ep.get("contentId") or ep.get("content_id") or ep.get("id")
        
        season_num = ep.get("seasonNum") or ep.get("seasonNumber") or 0
        episode_num = ep.get("episodeNum") or ep.get("episodeNumber") or 0
        
        try:
            season_int = int(season_num) if season_num else 0
            episode_int = int(episode_num) if episode_num else 0
        except (ValueError, TypeError):
            season_int = 0
            episode_int = 0
        
        series_title = (
            ep.get("seriesTitle") or 
            ep.get("series_title") or 
            ep.get("showTitle") or
            (show_info.get("title") if show_info else "Unknown")
        )
        
        episode_name = (
            ep.get("label") or 
            ep.get("title") or 
            ep.get("episodeName") or 
            (f"Clip {episode_num}" if not ep.get("fullEpisode", False) else f"Episode {episode_num}")
        )
        
        # P2: Filter empty strings from premiumFeatures
        pf = [f for f in ep.get("premiumFeatures", []) if f]
        
        return Title(
            id_=ep_id,
            type_=Title.Types.TV,
            name=series_title,
            season=season_int,
            episode=episode_int,
            episode_name=episode_name,
            original_lang="en",
            source=self.ALIASES[0],
            service_data=ep,
        )

    def get_tracks(self, title: Title):
        """Get tracks for the title."""
        self.log.info(f" + Getting tracks for: {title.name}")
        
        manifest_url = self._get_manifest_url(title)
        
        if not manifest_url:
            raise self.log.exit(f" - Could not get manifest for content ID: {title.id}")
        
        self.log.info(f" + Using manifest: {manifest_url}")
        self.log.debug(f" + Manifest URL: {manifest_url}")
        
        if manifest_url.endswith('.mpd') or 'mpd' in manifest_url.lower():
            tracks = Tracks.from_mpd(
                url=manifest_url,
                source=self.ALIASES[0],
                session=self.session
            )
        else:
            tracks = Tracks.from_hls(
                url=manifest_url,
                source=self.ALIASES[0],
                session=self.session
            )
        
        if not tracks.videos and not tracks.audios:
            raise self.log.exit(f" - No tracks could be obtained for this title.")
        
        self._fix_hdr_detection(tracks)
        
        if tracks.videos:
            tracks.videos.sort(key=lambda x: (x.height or 0, x.bitrate or 0), reverse=True)
            self.log.debug(f" + Found {len(tracks.videos)} video tracks (all qualities)")
        
        if tracks.audios:
            tracks.audios.sort(key=lambda x: x.bitrate or 0, reverse=True)
            self.log.debug(f" + Found {len(tracks.audios)} audio tracks (all languages)")
        
        if tracks.subtitles:
            self.log.debug(f" + Found {len(tracks.subtitles)} subtitle tracks")
            for subtitle in tracks.subtitles:
                codec_info = subtitle.codec if subtitle.codec else 'unknown'
                self.log.debug(f"   - {subtitle.language}: {codec_info}")
                
                if subtitle.codec and subtitle.codec.lower() == 'wvtt':
                    self.log.debug(f"   - WVTT subtitle detected for {subtitle.language}, will convert to SRT")
                
                if not subtitle.url and hasattr(subtitle, 'extra') and subtitle.extra:
                    self.log.debug(f"   - Subtitle with embedded data: {subtitle.language}")
        
        return tracks
        
    def _fix_hdr_detection(self, tracks: Tracks):
        """Fix DV vs HDR10/HDR10+ detection."""
        for track in tracks.videos:
            url = getattr(track, 'url', '') or ''
            
            raw_codec = ''
            extra = getattr(track, 'extra', None)
            if extra:
                items = extra if isinstance(extra, (list, tuple)) else [extra]
                for item in items:
                    if hasattr(item, 'attrib'):
                        raw_codec = item.attrib.get('codecs', '')
                        if raw_codec:
                            break
                    elif isinstance(item, dict):
                        raw_codec = item.get('codecs', '')
                        if raw_codec:
                            break
            
            if not raw_codec:
                for attr in ('codecs', 'raw_codec', 'codec_str'):
                    val = getattr(track, attr, None)
                    if val and isinstance(val, str) and len(val) >= 4:
                        raw_codec = val
                        break
            
            codec_prefix = raw_codec[:4].lower() if len(raw_codec) >= 4 else ''
            
            is_dv = 'DoVi' in url or codec_prefix in ('dvhe', 'dvh1')
            is_hdr10plus = 'HDR10plus' in url
            
            if is_dv:
                track.dv = True
                track.hdr10 = False
                track.hdr = 'DV'
            elif is_hdr10plus:
                track.dv = False
                track.hdr10 = True
                track.hdr = 'HDR10+'      

    def _get_manifest_url(self, title: Title) -> Optional[str]:
        """Get manifest URL from content metadata."""
        content_id = title.service_data.get("contentId") or title.service_data.get("content_id") or title.id
        
        # ── Primary: streamingUrl from episode/movie metadata ──
        raw_url = title.service_data.get("streamingUrl", "")
        if raw_url:
            clean_url = self._replace_asset_type(raw_url)
            return clean_url
        
        # ── Fallback: theplatform.com ──
        link_url = "http://link.theplatform.com/s/dJ5BDC/media/guid/2198311517/{video_id}"
        base_url = link_url.format(video_id=content_id)
        self.log.debug(f" + Platform URL fallback: {base_url}")
        
        asset_groups = [
            ["DASH_CENC_HDR10"],
            ["HLS_AES", "DASH_LIVE", "DASH_CENC_HDR10", "DASH_TA", "DASH_CENC", "DASH_CENC_PRECON"],
            []
        ]
        
        for assets in asset_groups:
            params = self._get_params({
                "format": "redirect",
                "formats": "MPEG-DASH",
                "manifest": "M3U",
                "Tracking": "true",
                "mbr": "true"
            })
            
            if assets:
                params["assetTypes"] = "|".join(assets)
            
            try:
                response = self.client.get(base_url, params=params, follow_redirects=False)
                
                if response.status_code in (301, 302) and 'location' in response.headers:
                    location = response.headers['location']
                    return self._replace_asset_type(location)
                            
            except Exception as e:
                self.log.debug(f" + Error with asset group {assets}: {e}")
                continue
        
        return None
                                  
    def _replace_asset_type(self, url: str) -> str:
        """Normalize manifest URL to vod.pplus.paramount.tech for best content."""
        if "vod.pplus.paramount.tech" in url:
            return url
        
        if "bakery.pplus.paramount.tech" in url:
            idx = url.find("/paramountplus/")
            if idx != -1:
                path = url[idx:]  
                url = f"https://vod.pplus.paramount.tech{path}"
                self.log.debug(f" + Domain: bakery.pplus → vod.pplus (stripped locale prefix)")
            else:
                url = url.replace("bakery.pplus.paramount.tech", "vod.pplus.paramount.tech")
                self.log.debug(f" + Domain: bakery.pplus → vod.pplus")
            return url
        
        # theplatform.com fallback: replace precon asset types
        codec = self.config.get("streaming", {}).get("codec", "avc")
        
        target_map = {
            "avc": "cenc_dash",
            "hevc": "cenc_hevc_dash",
            "hdr": "cenc_hdr_dash",
            "hdr_hevc": "cenc_hdr_hevc_dash",
        }
        target = target_map.get(codec, "cenc_dash")
        
        precon_patterns = [
            "cenc_precon_dash",
            "cenc_precon_hevc_dash",
            "cenc_precon_hdr_dash",
            "cenc_precon_hdr_hevc_dash",
        ]
        
        for old in precon_patterns:
            if old in url:
                url = url.replace(old, target)
                self.log.debug(f" + Asset type: {old} → {target}")
                break
        
        return url

    def _check_playability(self, content_id: str) -> bool:
        """P2: Check if content is playable. Returns True if allowed."""
        playability_url = self.region_config["endpoints"].get("playability")
        if not playability_url:
            return True  # endpoint not configured, skip check
        
        params = self._get_params({"contentId": content_id, "locale": "en-us"})
        
        # Add _clientRegion from cached user info
        rc = self._user_info.get("user_registration_country", "")
        if rc:
            params["_clientRegion"] = rc
        
        try:
            resp = self.client.get(playability_url, params=params)
            if resp.status_code != 200:
                self.log.debug(f" + Playability check returned {resp.status_code}, skipping")
                return True
            
            data = resp.json()
            
            if not data.get("allowedToPlayContentOnGivenDeviceBasedOnProduct", False):
                reasons = data.get("deniedPlaybackReasons", [])
                self.log.warning(f" + Playability denied: {reasons}")
                return False
            
            if data.get("parentalControl") != "allowed":
                self.log.warning(f" + Parental control: {data.get('parentalControl')}")
                return False
            
            self.log.debug(f" + Playability OK: {content_id}")
            return True
            
        except Exception as e:
            self.log.debug(f" + Playability check failed: {e}, continuing")
            return True


    def certificate(self, **_):
        """Get license certificate (not needed for this service)."""
        return None

    def license(self, challenge: bytes, title: Title, track, session_id, **_):
        """Get license for the content."""
        if isinstance(challenge, str):
            challenge = challenge.encode('utf-8')

        content_id = title.service_data.get("contentId") or title.service_data.get("content_id") or title.id

        if not content_id:
            raise ValueError(" - No contentId found in title data.")

        session_data = self._get_irdeto_session(content_id)
        token = session_data.get("ls_session")

        if not token:
            raise self.log.exit(" - Could not get session token (ls_session)")

        is_playready = challenge.strip().startswith(b'<?xml') or challenge.strip().startswith(b'<soap')

        if is_playready:
            license_url = self.config.get("license_playready",
                "https://cbsi.live.ott.irdeto.com/playready/rightsmanager.asmx")
            content_type = "text/xml; charset=utf-8"
        else:
            license_url = self.config.get("license_widevine",
                "https://cbsi.live.ott.irdeto.com/widevine/getlicense")
            content_type = "application/octet-stream"

        params = {
            "CrmId": "cbsi",
            "AccountId": "cbsi",
            "SubContentType": "Default",
            "ContentId": content_id,
        }

        dalvik_ua = self.config.get("dalvik_user_agent",
            "Dalvik/2.1.0 (Linux; U; Android 14; sdk_gphone64_x86_64 Build/UE1A.230829.050)")
        device_caps = self.config.get("license_device_caps", "GAAAGA")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "User-Agent": dalvik_ua,
            "X-DEVICE-CAPS": device_caps,
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
        }

        self.log.debug(f" + License request: {'PlayReady' if is_playready else 'Widevine'} → {license_url}")

        try:
            response = self.client.post(license_url, params=params, headers=headers, content=challenge, timeout=30)

            if response.status_code != 200:
                error_text = response.text[:500] if response.text else "No response body"
                raise ValueError(f"License error ({response.status_code}): {error_text}")

            if is_playready:
                return bytearray(response.content)

            return response.content

        except httpx.TimeoutException:
            raise self.log.exit(" - Timeout in license request")
        except httpx.ConnectError as e:
            raise self.log.exit(f" - Connection error in license: {e}")
        except Exception as e:
            if "License error" in str(e):
                raise self.log.exit(f" - {e}")
            raise self.log.exit(f" - License error: {e}")
                        
    def _get_irdeto_session(self, content_id: str) -> dict:
        # Return cached if available
        if content_id in self._session_cache:
            return self._session_cache[content_id]
        
        irdeto_url = self.region_config["endpoints"].get("irdeto_session")
        if not irdeto_url:
            raise self.log.exit(" - irdeto_session endpoint not configured in YAML")
        
        device_cfg = self.config.get("device", {})
        platform = device_cfg.get("platform", "PPINTL_AndroidApp")
        app_version = device_cfg.get("app_version", "16.13.0")
        
        params = self._get_params({
            "contentId": content_id,
            "model": device_cfg.get("model", "sdk_gphone64_x86_64"),
            "firmwareVersion": device_cfg.get("firmware_version", "14"),
            "version": f"{platform} {app_version}",
            "platform": platform,
            "locale": "en-us",
        })
        
        response = self.client.get(irdeto_url, params=params)
        
        if response.status_code != 200:
            raise self.log.exit(f" - Failed to get irdeto session: {response.status_code}")
        
        data = response.json()
        
        if not data.get("success"):
            raise self.log.exit(f" - irdeto session error: {data.get('message', 'Unknown error')}")
        
        # Cache for reuse by license()
        self._session_cache[content_id] = data
        self.log.debug(f" + irdeto session OK for {content_id} (JWT ~2h TTL)")
        
        return data

    def get_chapters(self, title: Title):
        """Get chapters (markers) from the video."""
        chapters = []
        events = title.service_data.get("playbackEvents", {})
        
        if not events:
            return chapters
        
        event_titles = {
            "endCreditChapterTimeMs": "Credits",
            "previewStartTimeMs": "Preview Start",
            "previewEndTimeMs": "Preview End",
            "openCreditEndTimeMs": "Opening Credits End",
            "openCreditStartTime": "Opening Credits Start",
        }
        
        for name, time_ms in events.items():
            if time_ms and isinstance(time_ms, (int, float)):
                chapters.append(
                    MenuTrack(
                        number=len(chapters) + 1,
                        title=event_titles.get(name, name.replace("TimeMs", "").replace("Time", "")),
                        timecode=self._ms_to_timecode(time_ms),
                    )
                )
        
        return chapters

    def _ms_to_timecode(self, ms: int) -> str:
        """Convert milliseconds to HH:MM:SS.mmm format."""
        total_seconds = ms / 1000
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = total_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"