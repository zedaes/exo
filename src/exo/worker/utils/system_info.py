import json
import socket
import sys
from functools import lru_cache
from subprocess import CalledProcessError

import psutil
from anyio import run_process

from exo.shared.types.profiling import NetworkInterfaceInfo


def _get_interface_type_fallback(ifname: str) -> str:
    """
    Fallback interface type detection using naming conventions.
    Works on non-macOS systems or when system_profiler fails.
    """
    ifname_lower = ifname.lower()

    # Local container/virtual interfaces
    if ifname_lower.startswith(("docker", "br-", "veth", "cni", "flannel", "calico", "weave")) or "bridge" in ifname_lower:
        return "Container Virtual"

    # Loopback interface
    if ifname_lower.startswith("lo"):
        return "Loopback"

    # Thunderbolt detection - common naming patterns
    if ifname_lower.startswith(("tb", "nx", "ten")):
        return "Thunderbolt"

    # WiFi detection
    if ifname_lower.startswith(("wlan", "wifi", "wl")) or ifname_lower in ("en0", "en1"):
        return "WiFi"

    # Ethernet detection
    if ifname_lower.startswith(("eth", "en")) and ifname_lower not in ("en0", "en1"):
        return "Ethernet"

    # VPN/tunnel interfaces
    if ifname_lower.startswith(("tun", "tap", "vtun", "utun", "gif", "stf", "awdl", "llw")):
        return "Virtual"

    return "Other"


@lru_cache(maxsize=1)
def _get_macos_interface_types() -> dict[str, str]:
    """
    Query macOS system_profiler for network interface types.
    Returns a dict mapping interface name to type.
    Cached since this data rarely changes.
    """
    if sys.platform != "darwin":
        return {}

    import subprocess

    try:
        result = subprocess.run(
            ["system_profiler", "SPNetworkDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, OSError):
        return {}

    interface_types: dict[str, str] = {}

    for interface in data.get("SPNetworkDataType", []):
        ifname = interface.get("interface")
        if not ifname:
            continue

        hardware = interface.get("hardware", "").lower()
        type_name = interface.get("type", "").lower()
        name = interface.get("_name", "").lower()

        # Determine type based on hardware/type/name
        if "thunderbolt" in name:
            interface_types[ifname] = "Thunderbolt"
        elif hardware == "ethernet" or type_name == "ethernet":
            if "usb" in name:
                interface_types[ifname] = "Ethernet [USB]"
            else:
                interface_types[ifname] = "Ethernet"
        elif hardware == "airport" or type_name == "airport" or "wi-fi" in name:
            interface_types[ifname] = "WiFi"
        elif type_name == "vpn":
            interface_types[ifname] = "Virtual"

    return interface_types


def get_interface_type(ifname: str, ip_address: str | None = None) -> str:
    """
    Get the type of a network interface.
    Uses macOS system_profiler when available, falls back to naming conventions.
    Also detects Thunderbolt from link-local IP addresses (169.254.x.x).
    """
    # Thunderbolt bridge connections use link-local addresses
    if ip_address and ip_address.startswith("169.254"):
        return "Thunderbolt"

    # Try macOS-specific detection first
    if sys.platform == "darwin":
        macos_types = _get_macos_interface_types()
        if ifname in macos_types:
            return macos_types[ifname]

    # Fall back to name-based detection
    return _get_interface_type_fallback(ifname)


async def get_friendly_name() -> str:
    """
    Asynchronously gets the 'Computer Name' (friendly name) of a Mac.
    e.g., "John's MacBook Pro"
    Returns the name as a string, or None if an error occurs or not on macOS.
    """
    hostname = socket.gethostname()

    # TODO: better non mac support
    if sys.platform != "darwin":  # 'darwin' is the platform name for macOS
        return hostname

    try:
        process = await run_process(["scutil", "--get", "ComputerName"])
    except CalledProcessError:
        return hostname

    return process.stdout.decode("utf-8", errors="replace").strip() or hostname


def get_network_interfaces() -> list[NetworkInterfaceInfo]:
    """
    Retrieves detailed network interface information.
    Parses interface names, IP addresses, and types (Thunderbolt, Ethernet, WiFi, etc.).
    Returns a list of NetworkInterfaceInfo objects.
    """
    interfaces_info: list[NetworkInterfaceInfo] = []

    for iface, services in psutil.net_if_addrs().items():
        for service in services:
            match service.family:
                case socket.AF_INET | socket.AF_INET6:
                    interface_type = get_interface_type(iface, service.address)
                    interfaces_info.append(
                        NetworkInterfaceInfo(
                            name=iface,
                            ip_address=service.address,
                            interface_type=interface_type,
                        )
                    )
                case _:
                    pass

    return interfaces_info


async def get_model_and_chip() -> tuple[str, str]:
    """Get Mac system information using system_profiler."""
    model = "Unknown Model"
    chip = "Unknown Chip"

    # TODO: better non mac support
    if sys.platform != "darwin":
        return (model, chip)

    try:
        process = await run_process(
            [
                "system_profiler",
                "SPHardwareDataType",
            ]
        )
    except CalledProcessError:
        return (model, chip)

    # less interested in errors here because this value should be hard coded
    output = process.stdout.decode().strip()

    model_line = next(
        (line for line in output.split("\n") if "Model Name" in line), None
    )
    model = model_line.split(": ")[1] if model_line else "Unknown Model"

    chip_line = next((line for line in output.split("\n") if "Chip" in line), None)
    chip = chip_line.split(": ")[1] if chip_line else "Unknown Chip"

    return (model, chip)
