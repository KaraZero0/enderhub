"""Docker Compose stack definitions managed by the EnderHub Telegram controller."""

from dataclasses import dataclass
import os
from typing import Optional

TAILSCALE_IP = os.environ.get("ENDERHUB_TAILSCALE_IP", "100.x.y.z")
BASE_HOME = os.environ.get("ENDERHUB_HOME", "/home/server")
NEXTCLOUD_DIR = os.environ.get("NEXTCLOUD_DIR", "/mnt/nextcloud")


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    description: str


@dataclass(frozen=True)
class Stack:
    id: str
    category: str
    name: str
    path: str
    containers: tuple[str, ...]
    endpoint: str
    description: str
    exclusive_group: Optional[str] = None
    ram_note: Optional[str] = None
    tailscale_check_port: Optional[int] = None


CATEGORIES: tuple[Category, ...] = (
    Category(
        id="utility",
        label="🛠️ Utility & Cloud",
        description="Personal cloud, file sharing, and observability stack",
    ),
    Category(
        id="ai",
        label="🤖 AI & ML",
        description="Local GPU LLM inference (Ollama + Open WebUI) and ML jobs",
    ),
    Category(
        id="earn",
        label="💸 Background Nodes",
        description="Lightweight always-on network monitoring nodes",
    ),
    Category(
        id="gaming",
        label="🎮 Gaming & Servers",
        description="On-demand game streaming and dedicated game servers",
    ),
)

CATEGORY_BY_ID: dict[str, Category] = {c.id: c for c in CATEGORIES}

STACKS: tuple[Stack, ...] = (
    Stack(
        id="nextcloud",
        category="utility",
        name="Nextcloud Hub",
        path=NEXTCLOUD_DIR,
        containers=("nextcloud_app", "nextcloud_db", "nextcloud_redis"),
        endpoint=f"http://{TAILSCALE_IP}",
        description="Private cloud, MariaDB 11, Redis & AI Worker",
        tailscale_check_port=80,
    ),
    Stack(
        id="monitoraggio",
        category="utility",
        name="Monitoring",
        path=f"{BASE_HOME}/monitoraggio",
        containers=("grafana", "prometheus", "node-exporter", "dcgm-exporter"),
        endpoint=f"http://{TAILSCALE_IP}:3000",
        description="Grafana, Prometheus, Node Exporter & NVIDIA DCGM",
        tailscale_check_port=3000,
    ),
    Stack(
        id="pairdrop",
        category="utility",
        name="PairDrop",
        path=f"{BASE_HOME}/pairdrop",
        containers=("pairdrop",),
        endpoint=f"http://{TAILSCALE_IP}:3002",
        description="Local/VPN AirDrop-style file sharing",
        tailscale_check_port=3002,
    ),
    Stack(
        id="ai_stack",
        category="ai",
        name="AI Stack (Ollama + WebUI)",
        path=f"{BASE_HOME}/ai-stack",
        containers=("ollama", "open-webui"),
        endpoint=f"http://{TAILSCALE_IP}:3001",
        description="CUDA LLM inference + Open WebUI + local RAG",
        tailscale_check_port=3001,
    ),
    Stack(
        id="grass",
        category="earn",
        name="Grass Desktop",
        path=f"{BASE_HOME}/grass",
        containers=("grass_node",),
        endpoint=f"http://{TAILSCALE_IP}:5800",
        description="Headless Xvfb + noVNC node container",
        tailscale_check_port=5800,
    ),
    Stack(
        id="bitping",
        category="earn",
        name="Bitping Node",
        path=f"{BASE_HOME}/bitping",
        containers=("bitping_node",),
        endpoint="https://app.bitping.com",
        description="Network probe node (256 MB RAM limit)",
    ),
    Stack(
        id="pokemmo",
        category="gaming",
        name="Cloud Gaming Host",
        path=f"{BASE_HOME}/pokemmo-gaming",
        containers=("pokemmo-gaming",),
        endpoint="noVNC :6080 | Sunshine :47990",
        description="GPU Sunshine + NVENC streaming host with auto-sleep",
    ),
    Stack(
        id="mc_enigmatica",
        category="gaming",
        name="MC: Enigmatica 10",
        path=f"{BASE_HOME}/minecraft-server/enigmatica",
        containers=("enigmatica10",),
        endpoint=f"{TAILSCALE_IP}:25565",
        description="CurseForge modpack with ZGC tuning",
        exclusive_group="mc_25565",
        ram_note="8 GB RAM recommended",
    ),
    Stack(
        id="mc_statech",
        category="gaming",
        name="MC: StaTech Industry",
        path=f"{BASE_HOME}/minecraft-server/statech-industry",
        containers=("statech_server",),
        endpoint=f"{TAILSCALE_IP}:25565",
        description="Fabric modpack server",
        exclusive_group="mc_25565",
        ram_note="8 GB RAM recommended",
    ),
    Stack(
        id="mc_techacademy",
        category="gaming",
        name="MC: TechAcademy",
        path=f"{BASE_HOME}/minecraft-server/techacademy",
        containers=("minecraft-server-techacademy",),
        endpoint=f"{TAILSCALE_IP}:25565",
        description="Custom Minecraft modpack server",
        exclusive_group="mc_25565",
    ),
    Stack(
        id="ninja_arena",
        category="gaming",
        name="Game Server A",
        path=f"{BASE_HOME}/ninja-arena-server",
        containers=("ninja-arena-server",),
        endpoint=f"{TAILSCALE_IP}:8888",
        description="Custom Node.js WebSocket game backend",
    ),
    Stack(
        id="ninja_village",
        category="gaming",
        name="Game Server B",
        path=f"{BASE_HOME}/ninja-village-server",
        containers=("ninja-village-server",),
        endpoint=f"{TAILSCALE_IP}:8889",
        description="Custom Python game backend",
    ),
)

STACK_BY_ID: dict[str, Stack] = {s.id: s for s in STACKS}


def stacks_in_category(category_id: str) -> list[Stack]:
    """Return all stacks belonging to a given category."""
    return [s for s in STACKS if s.category == category_id]
