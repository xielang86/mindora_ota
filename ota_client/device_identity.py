"""Device identifiers attached to OTA requests."""
import re
from pathlib import Path


def read_hardware_mac(net_root=Path('/sys/class/net')):
    """Read a permanent MAC from a physical network interface, preferring Wi-Fi."""
    try:
        interfaces = sorted(net_root.iterdir())
    except OSError:
        return None

    interfaces.sort(key=lambda interface: not (interface / 'wireless').is_dir()
                    and not (interface / 'phy80211').exists())
    for interface in interfaces:
        if not (interface / 'device').exists():
            continue
        try:
            if (interface / 'addr_assign_type').read_text().strip() != '0':
                continue
            mac = (interface / 'address').read_text().strip().lower()
        except OSError:
            continue
        if re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
            if mac != '00:00:00:00:00:00' and not int(mac[:2], 16) & 1:
                return mac
    return None


def ota_device_headers(serial_number, mac, current_version):
    headers = {}
    for name, value in (
        ('X-OTA-SN', serial_number),
        ('X-OTA-MAC', mac),
        ('X-OTA-Version', current_version),
    ):
        if isinstance(value, str) and re.fullmatch(r'[!-~][ -~]{0,127}', value):
            headers[name] = value
    return headers
