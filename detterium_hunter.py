#!/usr/bin/env python3
"""Example bot for the API https://neumann-probe.net/.

Objective: explore sectors until finding an asteroid containing deuterium.
The script reads the API key from the ".secret" file in the folder.

The code prioritizes readability and explicit messages: it shows each decision,
awaits asynchronous tasks, then resumes exploring.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://neumann-probe.net"
SECRET_FILE = ".secret"
SCAN_DELAY_SECONDS = 5 * 60
METAL_RESERVE_TARGET_ECE = 0.5
MANNY_TRIP_CAPACITY_ECE = 0.05
REPAIR_METALS_PER_PERCENT_ECE = 0.01
POLL_SECONDS = 30

# Hexagonal/cubic grid exposed by API: a neighbor changes two coordinates
# by +/-1 and keeps one stable. This keeps x+y+z even.
NEIGHBOR_OFFSETS = (
    {"x": 1, "y": 1, "z": 0},
    {"x": 1, "y": -1, "z": 0},
    {"x": -1, "y": 1, "z": 0},
    {"x": -1, "y": -1, "z": 0},
    {"x": 1, "y": 0, "z": 1},
    {"x": 1, "y": 0, "z": -1},
    {"x": -1, "y": 0, "z": 1},
    {"x": -1, "y": 0, "z": -1},
    {"x": 0, "y": 1, "z": 1},
    {"x": 0, "y": 1, "z": -1},
    {"x": 0, "y": -1, "z": 1},
    {"x": 0, "y": -1, "z": -1},
)


class ApiError(RuntimeError):
    """HTTP error converted to readable Python exception."""

    def __init__(self, status: int, method: str, path: str, payload: Any):
        self.status = status
        self.method = method
        self.path = path
        self.payload = payload
        message = payload
        if isinstance(payload, dict):
            message = payload.get("error", payload)
        super().__init__(f"{method} {path} -> HTTP {status}: {message}")


class NeumannClient:
    """Lightweight HTTP client with no external dependencies."""

    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if params:
            path = f"{path}?{urlencode(params)}"
        return self.request("GET", path)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, body)

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "neumann-deuterium-hunter/1.0",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw
            raise ApiError(exc.code, method, path, payload) from exc
        except URLError as exc:
            raise RuntimeError(f"Unable to reach {url}: {exc}") from exc


def log(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def sleep_countdown(seconds: float, reason: str, step: int = POLL_SECONDS) -> None:
    """Wait while refreshing a single countdown line."""

    remaining = max(0, int(seconds))
    if remaining <= 0:
        return

    while remaining > 0:
        minutes, secs = divmod(remaining, 60)
        print(f"\r{reason}: resuming in {minutes:02d}:{secs:02d}", end="", flush=True)
        delay = min(step, remaining)
        time.sleep(delay)
        remaining -= delay
    print("\r" + " " * 80 + "\r", end="", flush=True)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def seconds_until(value: str | None) -> int:
    end = parse_datetime(value)
    if not end:
        return 0
    return max(0, int((end - datetime.now(timezone.utc)).total_seconds()))


def load_token(path: Path) -> str:
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"File {path} is empty.")
    return token


def vector_key(vector: dict[str, Any]) -> tuple[int, int, int]:
    return (int(vector["x"]), int(vector["y"]), int(vector["z"]))


def add_vectors(a: dict[str, Any], b: dict[str, Any]) -> dict[str, int]:
    return {"x": int(a["x"]) + b["x"], "y": int(a["y"]) + b["y"], "z": int(a["z"]) + b["z"]}


def inventory_from_probe_or_sector(probe: dict[str, Any], sector_payload: dict[str, Any] | None) -> dict[str, Any]:
    if sector_payload and sector_payload.get("inventory"):
        return sector_payload["inventory"]
    return probe.get("inventory", {})


def resource_stock_amount(inventory: dict[str, Any], resource_type: str) -> float:
    for stock in inventory.get("resourceStocks", []):
        if stock.get("type") == resource_type:
            return float(stock.get("amount", 0))
    return 0.0


def free_capacity(inventory: dict[str, Any]) -> float:
    return float(inventory.get("freeCapacity", 0))


def deuterium_fill_percent(inventory: dict[str, Any]) -> float | None:
    for tank in inventory.get("externalTanks", []):
        if tank.get("type") == "deuterium":
            return float(tank.get("fillPercent", 0))
    return None


def idle_mannies(mannies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        manny
        for manny in mannies
        if manny.get("canReceiveOrders")
        and manny.get("currentTask") is None
        and manny.get("location", {}).get("type") == "probe"
    ]


def busy_mannies(mannies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [manny for manny in mannies if manny.get("currentTask") is not None]


def value_mentions_resource(value: Any, resource: str) -> bool:
    """Permissive resource recognition on sector objects.

    The YAML documents mining but does not fix the exact format of asteroid
    composition. This function accepts common forms: {"resources": {"metals": 0.2}},
    {"composition": [...]}, direct fields, or text summaries.
    """

    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return resource.replace("_", " ") in value.lower() or resource in value.lower()
    if isinstance(value, list):
        return any(value_mentions_resource(item, resource) for item in value)
    if isinstance(value, dict):
        if resource in value and value_mentions_resource(value[resource], resource):
            return True
        for key in ("type", "resource", "name"):
            if str(value.get(key, "")).lower() == resource:
                amount = value.get("amount", value.get("quantity", value.get("ratio", 1)))
                return value_mentions_resource(amount, resource)
        return any(
            value_mentions_resource(item, resource)
            for item in value.values()
            if isinstance(item, (dict, list, str))
        )
    return False


def asteroid_has_resource(asteroid: dict[str, Any], resource: str) -> bool:
    searchable_parts = [
        asteroid.get(resource),
        asteroid.get("resources"),
        asteroid.get("resourceTypes"),
        asteroid.get("composition"),
        asteroid.get("resourceComposition"),
        asteroid.get("resourceAmounts"),
        asteroid.get("materials"),
        asteroid.get("mineableResources"),
        asteroid.get("summary"),
        asteroid.get("name"),
    ]
    return any(value_mentions_resource(part, resource) for part in searchable_parts)


def iter_sector_asteroids(sector: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns visible asteroids, including those nested in a solar system."""

    asteroids = []
    seen_ids = set()
    nested_target_fields = ("minableTargets", "mineableTargets", "bookmarkTargets")

    def add_if_asteroid(obj: dict[str, Any]) -> None:
        if obj.get("type") != "asteroid" or not obj.get("id"):
            return
        if obj["id"] in seen_ids:
            return
        seen_ids.add(obj["id"])
        asteroids.append(obj)

    for obj in sector.get("objects", []):
        add_if_asteroid(obj)
        for field in nested_target_fields:
            for target in obj.get(field, []):
                add_if_asteroid(target)

    return asteroids


def asteroids_with_resource(sector: dict[str, Any], resource: str) -> list[dict[str, Any]]:
    return [
        asteroid
        for asteroid in iter_sector_asteroids(sector)
        if asteroid_has_resource(asteroid, resource)
    ]


def has_black_hole(sector: dict[str, Any]) -> bool:
    if any(obj.get("type") == "black_hole" for obj in sector.get("objects", [])):
        return True
    estimated = sector.get("estimatedObjects") or {}
    probability = float(estimated.get("blackHoleProbability", 0) or 0)
    return probability > 0


def has_astral_signal(sector: dict[str, Any]) -> bool:
    objects = sector.get("objects", [])
    if any(obj.get("type") in {"star", "planet", "asteroid", "solar_system"} for obj in objects):
        return True

    estimated = sector.get("estimatedObjects") or {}
    if estimated.get("star"):
        return True
    if int(estimated.get("planetCountMax", 0) or 0) > 0:
        return True

    possible = " ".join(str(item) for item in sector.get("possibleObjects", []))
    return any(word in possible for word in ("stellar", "planet", "asteroid"))


def wait_for_probe_idle(client: NeumannClient) -> dict[str, Any]:
    while True:
        probe = client.get("/api/probe")["probe"]
        status = probe.get("status")
        movement = probe.get("movement") or {}
        movement_status = movement.get("status")
        if status in {"idle", "orbiting"} and movement_status in {None, "arrived", "failed"}:
            log("Probe available in current sector.")
            return probe

        remaining = int(movement.get("secondsRemaining") or seconds_until(movement.get("arrivalAt")) or POLL_SECONDS)
        phase = movement.get("phase") or status
        target = movement.get("target")
        log(f"Probe in transit ({phase}) to {target}.")
        sleep_countdown(max(POLL_SECONDS, remaining), "Transit in progress")


def wait_for_manny_tasks(client: NeumannClient) -> None:
    while True:
        mannies = client.get("/api/probe/mannies")["mannies"]
        busy = busy_mannies(mannies)
        if not busy:
            return

        next_end = min((seconds_until(manny.get("taskEstimatedEndTime")) for manny in busy), default=POLL_SECONDS)
        descriptions = ", ".join(f"{m.get('name')}:{m.get('currentTask')}" for m in busy)
        log(f"Tasks in progress: {descriptions}.")
        sleep_countdown(max(POLL_SECONDS, next_end), "Mannys working")


def distribute_amount(total: float, count: int, precision: int = 3) -> list[float]:
    if count <= 0 or total <= 0:
        return []
    base = round(total / count, precision)
    parts = [base for _ in range(count)]
    parts[-1] = round(total - sum(parts[:-1]), precision)
    return [part for part in parts if part > 0]


def start_repairs(client: NeumannClient, probe: dict[str, Any], mannies: list[dict[str, Any]], metals: float) -> bool:
    integrity = float(probe.get("systems", {}).get("integrityPercent", 100))
    missing = max(0.0, 100.0 - integrity)
    available = idle_mannies(mannies)
    if missing <= 0.001:
        log("Integrity at 100%, no repair needed.")
        return False
    if not available:
        log(f"Integrity at {integrity:.1f}%, but no Manny available to repair.")
        return False
    if metals <= 0:
        log(f"Integrity at {integrity:.1f}%, but no metals to repair.")
        return False

    repairable = min(missing, metals / REPAIR_METALS_PER_PERCENT_ECE)
    if repairable <= 0.001:
        return False

    parts = distribute_amount(repairable, len(available))
    log(f"Repairs: {repairable:.2f}% distributed over {len(parts)} Manny(s).")
    started = False
    for manny, percent in zip(available, parts):
        try:
            client.post(f"/api/probe/mannies/{manny['id']}/repair", {"integrityPercent": percent})
            log(f"  - {manny.get('name')} repairs {percent:.2f}% integrity.")
            started = True
        except ApiError as exc:
            log(f"  - Repair refused for {manny.get('name')}: {exc}")
    return started


def start_metal_mining(
    client: NeumannClient,
    sector: dict[str, Any],
    inventory: dict[str, Any],
    mannies: list[dict[str, Any]],
) -> bool:
    metals = resource_stock_amount(inventory, "metals")
    if metals >= METAL_RESERVE_TARGET_ECE:
        log(f"Sufficient metal reserve: {metals:.3f} ECE.")
        return False

    targets = asteroids_with_resource(sector, "metals")
    available = idle_mannies(mannies)
    if not targets:
        log("Metal reserve low, but no metallic asteroid visible here.")
        return False
    if not available:
        log("Metal reserve low, but no Manny available to mine.")
        return False

    target_amount = min(MANNY_TRIP_CAPACITY_ECE * len(available), METAL_RESERVE_TARGET_ECE - metals, free_capacity(inventory))
    if target_amount <= 0:
        log("Metal reserve low, but cargo hold is full.")
        return False

    parts = distribute_amount(target_amount, len(available))
    asteroid = targets[0]
    log(f"Mining: {target_amount:.3f} ECE of metals distributed over {len(parts)} Manny(s).")
    started = False
    for manny, amount in zip(available, parts):
        try:
            client.post(
                f"/api/probe/mannies/{manny['id']}/mine",
                {"objectId": asteroid["id"], "resources": ["metals"], "targetAmount": amount},
            )
            log(f"  - {manny.get('name')} mines {amount:.3f} ECE of metals on {asteroid.get('name') or asteroid['id']}.")
            started = True
        except ApiError as exc:
            log(f"  - Mining refused for {manny.get('name')}: {exc}")
    return started


def wait_for_scan_window(sector: dict[str, Any]) -> None:
    scan = sector.get("scan", {})
    residence = int(scan.get("currentSectorResidenceSeconds", 0) or 0)
    missing = SCAN_DELAY_SECONDS - residence
    if missing > 0:
        log(f"Neighbor sensors not yet mature: {residence}s/{SCAN_DELAY_SECONDS}s in this sector.")
        sleep_countdown(missing, "Passive scan of neighboring sectors")
    else:
        log("Passive scan delay reached.")


def observe_neighbors(client: NeumannClient, current: dict[str, Any]) -> list[dict[str, Any]]:
    neighbors = []
    for offset in NEIGHBOR_OFFSETS:
        coords = add_vectors(current, offset)
        try:
            sector = client.get("/api/sector", coords)["sector"]
            neighbors.append(sector)
            label = coords_label(coords)
            danger = "possible black hole" if has_black_hole(sector) else "no black hole detected"
            astral = "with celestial bodies" if has_astral_signal(sector) else "empty or sparse"
            log(f"  - {label}: {sector.get('knowledgeLevel')} / {danger} / {astral}")
        except ApiError as exc:
            log(f"  - {coords_label(coords)}: scan unavailable ({exc})")
    return neighbors


def coords_label(coords: dict[str, Any]) -> str:
    return f"({int(coords['x'])}, {int(coords['y'])}, {int(coords['z'])})"


def visited_sector_keys(client: NeumannClient) -> set[tuple[int, int, int]]:
    """Retrieve the map of sectors already visited by the probe."""

    visited_payload = client.get("/api/probe/visited-sectors")
    return {vector_key(item["relativeCoordinates"]) for item in visited_payload["visitedSectors"]}


def choose_destination(neighbors: list[dict[str, Any]], visited: set[tuple[int, int, int]]) -> dict[str, int]:
    candidates = [sector for sector in neighbors if vector_key(sector["relativeCoordinates"]) not in visited]
    skipped_count = len(neighbors) - len(candidates)
    if skipped_count:
        log(f"{skipped_count} neighboring sector(s) already visited, ignored.")
    if not candidates:
        raise RuntimeError("All observed neighboring sectors have been visited; no departure will be launched.")

    def score(sector: dict[str, Any]) -> tuple[int, int, float]:
        no_black_hole = 1 if not has_black_hole(sector) else 0
        astral = 1 if has_astral_signal(sector) else 0
        confidence = float(sector.get("confidence", 0) or 0)
        return (no_black_hole, astral, confidence)

    ranked = sorted(candidates, key=score, reverse=True)
    return {key: int(ranked[0]["relativeCoordinates"][key]) for key in ("x", "y", "z")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Explore Neumann Probe to find deuterium.")
    parser.add_argument("--base-url", default=BASE_URL, help="Base URL of the API.")
    parser.add_argument("--secret", default=SECRET_FILE, help="File containing the API key.")
    args = parser.parse_args()

    client = NeumannClient(args.base_url, load_token(Path(args.secret)))
    log("Bot started. Searching for an asteroid containing deuterium.")

    while True:
        probe = wait_for_probe_idle(client)

        sector_payload = client.get("/api/probe/sector")
        sector = sector_payload["sector"]
        inventory = inventory_from_probe_or_sector(probe, sector_payload)
        fill = deuterium_fill_percent(inventory)
        if fill is not None:
            log(f"Deuterium tank: {fill:.1f}%.")

        deuterium_asteroids = asteroids_with_resource(sector, "deuterium")
        if deuterium_asteroids:
            names = ", ".join(obj.get("name") or obj["id"] for obj in deuterium_asteroids)
            log(f"Victory: asteroid(s) with deuterium in current sector: {names}.")
            return 0

        mannies = client.get("/api/probe/mannies")["mannies"]
        metals = resource_stock_amount(inventory, "metals")
        log(
            f"Sector {coords_label(sector['relativeCoordinates'])}: "
            f"{len(mannies)} Manny(s), {metals:.3f} ECE of metals."
        )

        repair_started = start_repairs(client, probe, mannies, metals)

        # Reload Manny state: those that just left for repairs should not be
        # chosen for mining in the same loop.
        mannies = client.get("/api/probe/mannies")["mannies"]
        mining_started = start_metal_mining(client, sector, inventory, mannies)

        if repair_started or mining_started:
            wait_for_manny_tasks(client)
            continue

        wait_for_scan_window(sector)
        visited = visited_sector_keys(client)
        log("Scanning neighboring sectors.")
        neighbors = observe_neighbors(client, sector["relativeCoordinates"])
        try:
            destination = choose_destination(neighbors, visited)
        except RuntimeError as exc:
            log(str(exc))
            return 1

        log(f"Departing to {coords_label(destination)}.")
        try:
            movement = client.post("/api/probe/move", {"target": destination})["movement"]
        except ApiError as exc:
            log(f"Departure refused: {exc}")
            sleep_countdown(POLL_SECONDS, "Pause before retry")
            continue

        remaining = int(movement.get("secondsRemaining") or seconds_until(movement.get("arrivalAt")) or POLL_SECONDS)
        sleep_countdown(remaining, "Transit in progress")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nUser interrupt.")
        raise SystemExit(130)
