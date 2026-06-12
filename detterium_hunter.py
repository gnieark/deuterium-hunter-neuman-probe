#!/usr/bin/env python3
"""
Bot d'exemple pour l'API https://neumann-probe.net/.

Objectif: explorer les secteurs jusqu'a trouver un asteroide contenant du
deuterium. Le script lit la clef d'API dans le fichier ".secret" du dossier.

Le code privilegie la lisibilite et les messages explicites: il montre chaque
decision, attend les taches asynchrones, puis repart explorer.
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

# Grille hexagonale/cubique exposee par l'API: un voisin a deux coordonnees qui
# changent de +/-1 et une coordonnee stable. Cela garde x+y+z pair.
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
    """Erreur HTTP transformee en exception Python lisible."""

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
    """Petit client HTTP sans dependance externe."""

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
            raise RuntimeError(f"Impossible de joindre {url}: {exc}") from exc


def log(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def sleep_countdown(seconds: float, reason: str, step: int = POLL_SECONDS) -> None:
    """Attend en rafraichissant une seule ligne de compte a rebours."""

    remaining = max(0, int(seconds))
    if remaining <= 0:
        return

    while remaining > 0:
        minutes, secs = divmod(remaining, 60)
        print(f"\r{reason}: reprise dans {minutes:02d}:{secs:02d}", end="", flush=True)
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
        raise SystemExit(f"Le fichier {path} est vide.")
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
    """Reconnaissance permissive des ressources sur les objets de secteur.

    Le YAML documente le minage mais ne fige pas le format exact de la
    composition des asteroides. Cette fonction accepte les formes courantes:
    {"resources": {"metals": 0.2}}, {"composition": [...]}, champs directs,
    ou un resume textuel.
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
        return any(value_mentions_resource(item, resource) for item in value.values())
    return False


def asteroid_has_resource(asteroid: dict[str, Any], resource: str) -> bool:
    searchable_parts = [
        asteroid.get(resource),
        asteroid.get("resources"),
        asteroid.get("composition"),
        asteroid.get("resourceComposition"),
        asteroid.get("materials"),
        asteroid.get("mineableResources"),
        asteroid.get("summary"),
        asteroid.get("name"),
    ]
    return any(value_mentions_resource(part, resource) for part in searchable_parts)


def asteroids_with_resource(sector: dict[str, Any], resource: str) -> list[dict[str, Any]]:
    return [
        obj
        for obj in sector.get("objects", [])
        if obj.get("type") == "asteroid" and obj.get("id") and asteroid_has_resource(obj, resource)
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
            log("Sonde disponible dans le secteur courant.")
            return probe

        remaining = int(movement.get("secondsRemaining") or seconds_until(movement.get("arrivalAt")) or POLL_SECONDS)
        phase = movement.get("phase") or status
        target = movement.get("target")
        log(f"Sonde en transit ({phase}) vers {target}.")
        sleep_countdown(max(POLL_SECONDS, remaining), "Transit en cours")


def wait_for_manny_tasks(client: NeumannClient) -> None:
    while True:
        mannies = client.get("/api/probe/mannies")["mannies"]
        busy = busy_mannies(mannies)
        if not busy:
            return

        next_end = min((seconds_until(manny.get("taskEstimatedEndTime")) for manny in busy), default=POLL_SECONDS)
        descriptions = ", ".join(f"{m.get('name')}:{m.get('currentTask')}" for m in busy)
        log(f"Taches en cours: {descriptions}.")
        sleep_countdown(max(POLL_SECONDS, next_end), "Mannys au travail")


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
        log("Integrite a 100%, aucune reparation necessaire.")
        return False
    if not available:
        log(f"Integrite a {integrity:.1f}%, mais aucun Manny n'est disponible pour reparer.")
        return False
    if metals <= 0:
        log(f"Integrite a {integrity:.1f}%, mais il n'y a pas de metaux pour reparer.")
        return False

    repairable = min(missing, metals / REPAIR_METALS_PER_PERCENT_ECE)
    if repairable <= 0.001:
        return False

    parts = distribute_amount(repairable, len(available))
    log(f"Reparation: {repairable:.2f}% repartis sur {len(parts)} Manny(s).")
    started = False
    for manny, percent in zip(available, parts):
        try:
            client.post(f"/api/probe/mannies/{manny['id']}/repair", {"integrityPercent": percent})
            log(f"  - {manny.get('name')} repare {percent:.2f}% d'integrite.")
            started = True
        except ApiError as exc:
            log(f"  - Reparation refusee pour {manny.get('name')}: {exc}")
    return started


def start_metal_mining(
    client: NeumannClient,
    sector: dict[str, Any],
    inventory: dict[str, Any],
    mannies: list[dict[str, Any]],
) -> bool:
    metals = resource_stock_amount(inventory, "metals")
    if metals >= METAL_RESERVE_TARGET_ECE:
        log(f"Reserve de metaux suffisante: {metals:.3f} ECE.")
        return False

    targets = asteroids_with_resource(sector, "metals")
    available = idle_mannies(mannies)
    if not targets:
        log("Reserve de metaux basse, mais aucun asteroide metallique visible ici.")
        return False
    if not available:
        log("Reserve de metaux basse, mais aucun Manny disponible pour miner.")
        return False

    target_amount = min(MANNY_TRIP_CAPACITY_ECE, METAL_RESERVE_TARGET_ECE - metals, free_capacity(inventory))
    if target_amount <= 0:
        log("Reserve de metaux basse, mais la soute est pleine.")
        return False

    manny = available[0]
    asteroid = targets[0]
    client.post(
        f"/api/probe/mannies/{manny['id']}/mine",
        {"objectId": asteroid["id"], "resources": ["metals"], "targetAmount": round(target_amount, 3)},
    )
    log(f"{manny.get('name')} mine {target_amount:.3f} ECE de metaux sur {asteroid.get('name') or asteroid['id']}.")
    return True


def wait_for_scan_window(sector: dict[str, Any]) -> None:
    scan = sector.get("scan", {})
    residence = int(scan.get("currentSectorResidenceSeconds", 0) or 0)
    missing = SCAN_DELAY_SECONDS - residence
    if missing > 0:
        log(f"Capteurs voisins pas encore murs: {residence}s/{SCAN_DELAY_SECONDS}s dans ce secteur.")
        sleep_countdown(missing, "Scan passif des secteurs voisins")
    else:
        log("Le delai de scan passif est atteint.")


def observe_neighbors(client: NeumannClient, current: dict[str, Any]) -> list[dict[str, Any]]:
    neighbors = []
    for offset in NEIGHBOR_OFFSETS:
        coords = add_vectors(current, offset)
        try:
            sector = client.get("/api/sector", coords)["sector"]
            neighbors.append(sector)
            label = coords_label(coords)
            danger = "trou noir possible" if has_black_hole(sector) else "pas de trou noir detecte"
            astral = "avec astres" if has_astral_signal(sector) else "vide ou pauvre"
            log(f"  - {label}: {sector.get('knowledgeLevel')} / {danger} / {astral}")
        except ApiError as exc:
            log(f"  - {coords_label(coords)}: scan indisponible ({exc})")
    return neighbors


def coords_label(coords: dict[str, Any]) -> str:
    return f"({int(coords['x'])}, {int(coords['y'])}, {int(coords['z'])})"


def visited_sector_keys(client: NeumannClient) -> set[tuple[int, int, int]]:
    """Recupere la cartographie des secteurs deja traverses par la sonde."""

    visited_payload = client.get("/api/probe/visited-sectors")
    return {vector_key(item["relativeCoordinates"]) for item in visited_payload["visitedSectors"]}


def choose_destination(neighbors: list[dict[str, Any]], visited: set[tuple[int, int, int]]) -> dict[str, int]:
    candidates = [sector for sector in neighbors if vector_key(sector["relativeCoordinates"]) not in visited]
    skipped_count = len(neighbors) - len(candidates)
    if skipped_count:
        log(f"{skipped_count} secteur(s) voisin(s) deja visite(s) ignore(s).")
    if not candidates:
        raise RuntimeError("Tous les secteurs voisins observes ont deja ete visites; aucun retour ne sera lance.")

    def score(sector: dict[str, Any]) -> tuple[int, int, float]:
        no_black_hole = 1 if not has_black_hole(sector) else 0
        astral = 1 if has_astral_signal(sector) else 0
        confidence = float(sector.get("confidence", 0) or 0)
        return (no_black_hole, astral, confidence)

    ranked = sorted(candidates, key=score, reverse=True)
    return {key: int(ranked[0]["relativeCoordinates"][key]) for key in ("x", "y", "z")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Explore Neumann Probe jusqu'a trouver du deuterium.")
    parser.add_argument("--base-url", default=BASE_URL, help="URL de base de l'API.")
    parser.add_argument("--secret", default=SECRET_FILE, help="Fichier contenant la clef d'API.")
    args = parser.parse_args()

    client = NeumannClient(args.base_url, load_token(Path(args.secret)))
    log("Bot demarre. Recherche d'un asteroide contenant du deuterium.")

    while True:
        probe = wait_for_probe_idle(client)

        sector_payload = client.get("/api/probe/sector")
        sector = sector_payload["sector"]
        inventory = inventory_from_probe_or_sector(probe, sector_payload)
        fill = deuterium_fill_percent(inventory)
        if fill is not None:
            log(f"Cuve de deuterium: {fill:.1f}%.")

        deuterium_asteroids = asteroids_with_resource(sector, "deuterium")
        if deuterium_asteroids:
            names = ", ".join(obj.get("name") or obj["id"] for obj in deuterium_asteroids)
            log(f"Victoire: asteroide(s) avec deuterium dans le secteur courant: {names}.")
            return 0

        mannies = client.get("/api/probe/mannies")["mannies"]
        metals = resource_stock_amount(inventory, "metals")
        log(
            f"Secteur {coords_label(sector['relativeCoordinates'])}: "
            f"{len(mannies)} Manny(s), {metals:.3f} ECE de metaux."
        )

        repair_started = start_repairs(client, probe, mannies, metals)

        # On recharge l'etat des Mannys: ceux qui viennent de partir reparer ne
        # doivent pas etre choisis pour miner dans la meme boucle.
        mannies = client.get("/api/probe/mannies")["mannies"]
        mining_started = start_metal_mining(client, sector, inventory, mannies)

        if repair_started or mining_started:
            wait_for_manny_tasks(client)
            continue

        wait_for_scan_window(sector)
        visited = visited_sector_keys(client)
        log("Scan des secteurs voisins.")
        neighbors = observe_neighbors(client, sector["relativeCoordinates"])
        try:
            destination = choose_destination(neighbors, visited)
        except RuntimeError as exc:
            log(str(exc))
            return 1

        log(f"Depart vers {coords_label(destination)}.")
        try:
            movement = client.post("/api/probe/move", {"target": destination})["movement"]
        except ApiError as exc:
            log(f"Depart refuse: {exc}")
            sleep_countdown(POLL_SECONDS, "Pause avant nouvelle tentative")
            continue

        remaining = int(movement.get("secondsRemaining") or seconds_until(movement.get("arrivalAt")) or POLL_SECONDS)
        sleep_countdown(remaining, "Transit en cours")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterruption utilisateur.")
        raise SystemExit(130)
