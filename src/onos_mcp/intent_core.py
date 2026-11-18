#!/usr/bin/env python3
"""Intent translation, submission, and conflict detection for ONOS.

The module talks to the ONOS northbound REST API only; it can be used from a
CLI, from a web UI, or from the end-to-end test script.
"""

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_ONOS_URL = "http://127.0.0.1:8181"
DEFAULT_ONOS_USER = "onos"
DEFAULT_ONOS_PASSWORD = "rocks"
DEFAULT_APP_ID = "org.onosproject.cli"
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
DEVICE_ID_RE = re.compile(r"^(?:of:)?([0-9a-fA-F]+)$")

CONNECT_RE = re.compile(r"^connect\s+(\S+)\s+(?:to\s+)?(\S+)\s*$", re.IGNORECASE)
DISCONNECT_RE = re.compile(
    r"^disconnect\s+(\S+)\s+(?:from\s+)?(\S+)\s*$", re.IGNORECASE
)


class OnosError(Exception):
    """Raised when ONOS cannot satisfy a request."""


class ConflictError(OnosError):
    """Raised when an intent conflicts with an existing intent."""

    def __init__(self, message, conflicts):
        super().__init__(message)
        self.conflicts = conflicts


def parse_command(text):
    """Translate a small text command into an action dictionary."""
    text = (text or "").strip()
    if not text:
        raise OnosError("empty command")
    lowered = text.lower()
    if lowered in ("list", "list intents", "show intents"):
        return {"action": "list"}
    if lowered in ("hosts", "list hosts"):
        return {"action": "hosts"}
    if lowered in ("help", "?"):
        return {"action": "help"}
    if lowered in ("quit", "exit"):
        return {"action": "quit"}

    match = CONNECT_RE.match(text)
    if match:
        src, dst = match.group(1), match.group(2)
        if src.lower() == dst.lower():
            raise OnosError("cannot connect a host to itself: %s" % src)
        return {"action": "connect", "src": src, "dst": dst}

    match = DISCONNECT_RE.match(text)
    if match:
        src, dst = match.group(1), match.group(2)
        if src.lower() == dst.lower():
            raise OnosError("cannot disconnect a host from itself: %s" % src)
        return {"action": "disconnect", "src": src, "dst": dst}

    raise OnosError(
        "unrecognised command %r; expected 'connect h1 to h2' or "
        "'disconnect h1 from h2'" % text
    )


def host_to_host_payload(one, two, app_id=DEFAULT_APP_ID, priority=100):
    """Build the ONOS REST payload for a HostToHostIntent."""
    return {
        "type": "HostToHostIntent",
        "appId": app_id,
        "priority": int(priority),
        "one": one,
        "two": two,
    }


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def intent_endpoints(intent):
    """Return (one, two) host IDs from an ONOS intent JSON object."""
    containers = [intent]
    for key in ("key", "intent"):
        value = intent.get(key)
        if isinstance(value, dict):
            containers.append(value)
    one = two = None
    for container in containers:
        one = _first(
            container.get("one"),
            container.get("oneHostId"),
            container.get("oneHost"),
        )
        two = _first(
            container.get("two"),
            container.get("twoHostId"),
            container.get("twoHost"),
        )
        if one or two:
            break
    if not (one and two):
        resources = intent.get("resources")
        if isinstance(resources, list):
            hosts = [
                item
                for item in resources
                if isinstance(item, str) and "/" in item
            ]
            if len(hosts) >= 2:
                one, two = hosts[0], hosts[1]
    return one, two


def intent_id(intent):
    value = intent.get("id")
    if value:
        return value
    key = intent.get("key")
    if isinstance(key, dict):
        return key.get("id")
    return None


def intent_app_id(intent):
    value = intent.get("appId")
    if value:
        return value
    key = intent.get("key")
    if isinstance(key, dict):
        return key.get("appId")
    return None


def intent_state(intent):
    return intent.get("state") or intent.get("status") or "UNKNOWN"


def is_active_intent(intent):
    state = str(intent_state(intent)).upper()
    return state not in ("WITHDRAWN", "FAILED", "WITHDRAWING")


def detect_conflicts(one, two, intents):
    """Find intents whose host scope overlaps the requested (one, two) pair.

    A duplicate pair is a hard conflict.  Sharing only one endpoint is an
    overlap conflict: the two intents may match the same traffic on part of
    their paths, so the caller must explicitly force it.
    """
    conflicts = []
    requested = {one, two}
    for intent in intents:
        if intent.get("type") not in (None, "HostToHostIntent"):
            continue
        if not is_active_intent(intent):
            continue
        other_one, other_two = intent_endpoints(intent)
        if not other_one or not other_two:
            continue
        other = {other_one, other_two}
        if requested == other:
            kind = "duplicate"
        elif one in other or two in other:
            kind = "overlap"
        else:
            continue
        conflicts.append(
            {
                "kind": kind,
                "id": intent_id(intent),
                "appId": intent_app_id(intent),
                "state": intent_state(intent),
                "one": other_one,
                "two": other_two,
                "intent": intent,
            }
        )
    return conflicts


def parse_aliases(value):
    """Parse ``name=value,name=value`` aliases from a string or mapping."""
    if not value:
        return {}
    if isinstance(value, dict):
        items = value.items()
    else:
        items = []
        for chunk in re.split(r"[,;]", str(value)):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise OnosError(
                    "alias %r must look like name=value" % chunk
                )
            name, target = chunk.split("=", 1)
            items.append((name.strip(), target.strip()))
    result = {}
    for name, target in items:
        name = str(name).strip()
        target = str(target).strip()
        if not name or not target:
            raise OnosError("alias names and values must not be empty")
        result[name] = target
    return result


def load_env_file(path, override=False):
    """Load KEY=VALUE pairs from a file. Existing environment wins by default."""
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise OnosError("cannot read env file %s: %s" % (path, error))
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise OnosError(
                "%s:%d: expected KEY=VALUE" % (path, number)
            )
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            raise OnosError("%s:%d: empty variable name" % (path, number))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if override or name not in os.environ:
            os.environ[name] = value
    return path


def load_env(explicit=None):
    """Load an explicit env file, ONOS_ENV_FILE, or the nearest .env."""
    if explicit:
        return load_env_file(explicit)
    configured = os.environ.get("ONOS_ENV_FILE")
    if configured:
        return load_env_file(configured)
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    for candidate in candidates:
        if candidate.is_file():
            return load_env_file(candidate)
    return None


def _first_ipv4(host):
    for ip in host.get("ipAddresses") or []:
        if IPV4_RE.match(str(ip)):
            return str(ip)
    return None


def _device_number(device_id):
    if not device_id:
        return None
    match = DEVICE_ID_RE.match(str(device_id))
    if not match:
        return None
    return int(match.group(1), 16)


def _unique_auto_names(items, name_for):
    counts = {}
    names = {}
    for item in items:
        name = name_for(item)
        if name:
            counts[name] = counts.get(name, 0) + 1
            names[item.get("id")] = name
    return {
        item_id: name
        for item_id, name in names.items()
        if counts.get(name) == 1
    }


class NodeResolver:
    """Resolve user-facing host/switch names against live ONOS inventory.

    Explicit aliases win.  Without aliases, hosts get ``h<last IPv4 octet>``
    when that name is unique, and switches get ``s<hex datapath number>`` when
    that name is unique.  Full ONOS host IDs, MAC addresses, IP addresses, and
    device IDs are always accepted.
    """

    def __init__(
        self,
        hosts,
        devices=None,
        host_aliases=None,
        switch_aliases=None,
    ):
        self.hosts = list(hosts or [])
        self.devices = list(devices or [])
        self.host_aliases = dict(host_aliases or {})
        self.switch_aliases = dict(switch_aliases or {})

        self.host_by_id = {
            host.get("id"): host for host in self.hosts if host.get("id")
        }
        self.host_by_ip = {}
        self.host_by_mac = {}
        for host in self.hosts:
            for ip in host.get("ipAddresses") or []:
                self.host_by_ip[str(ip)] = host
            mac = str(host.get("mac") or "").lower()
            if mac:
                self.host_by_mac[mac] = host
        self.device_by_id = {
            device.get("id"): device
            for device in self.devices
            if device.get("id")
        }

        self.name_to_host = {}
        self.name_to_switch = {}
        self.host_names = {}
        self.switch_names = {}
        self._build_hosts()
        self._build_switches()

    def _build_hosts(self):
        auto = _unique_auto_names(
            self.hosts,
            lambda host: (
                "h" + _first_ipv4(host).rsplit(".", 1)[1]
                if _first_ipv4(host)
                else None
            ),
        )
        for host in self.hosts:
            name = auto.get(host.get("id"))
            if name:
                self.name_to_host[name.lower()] = host
                self.host_names[host.get("id")] = name

        for alias, target in self.host_aliases.items():
            host = self._find_host_raw(target)
            if host is None:
                raise OnosError(
                    "host alias %r points to unknown host %r"
                    % (alias, target)
                )
            key = alias.lower()
            existing = self.name_to_host.get(key)
            if existing is not None and existing is not host:
                raise OnosError(
                    "host alias %r conflicts with an inferred host name"
                    % alias
                )
            self.name_to_host[key] = host
            self.host_names[host.get("id")] = alias

    def _build_switches(self):
        auto = _unique_auto_names(
            self.devices,
            lambda device: (
                "s%d" % _device_number(device.get("id"))
                if _device_number(device.get("id")) is not None
                else None
            ),
        )
        for device in self.devices:
            name = auto.get(device.get("id"))
            if name:
                self.name_to_switch[name.lower()] = device
                self.switch_names[device.get("id")] = name

        for alias, target in self.switch_aliases.items():
            device = self._find_switch_raw(target)
            if device is None:
                raise OnosError(
                    "switch alias %r points to unknown switch %r"
                    % (alias, target)
                )
            key = alias.lower()
            existing = self.name_to_switch.get(key)
            if existing is not None and existing is not device:
                raise OnosError(
                    "switch alias %r conflicts with an inferred switch name"
                    % alias
                )
            self.name_to_switch[key] = device
            self.switch_names[device.get("id")] = alias

    def _find_host_raw(self, value):
        value = str(value).strip()
        if value in self.host_by_id:
            return self.host_by_id[value]
        if value in self.host_by_ip:
            return self.host_by_ip[value]
        return self.host_by_mac.get(value.lower())

    def _find_switch_raw(self, value):
        value = str(value).strip()
        if value in self.device_by_id:
            return self.device_by_id[value]
        return self.name_to_switch.get(value.lower())

    def find_host(self, name):
        value = str(name or "").strip()
        if not value:
            return None
        if value in self.host_by_id:
            return self.host_by_id[value]
        if value in self.host_by_ip:
            return self.host_by_ip[value]
        if value.lower() in self.host_by_mac:
            return self.host_by_mac[value.lower()]
        return self.name_to_host.get(value.lower())

    def find_switch(self, name):
        value = str(name or "").strip()
        if not value:
            return None
        if value in self.device_by_id:
            return self.device_by_id[value]
        return self.name_to_switch.get(value.lower())

    def host_name(self, host):
        return self.host_names.get(host.get("id"), host.get("id"))

    def switch_name(self, device):
        return self.switch_names.get(device.get("id"), device.get("id"))

    def device_name(self, device_id):
        return self.switch_names.get(device_id, device_id)

    def known_hosts(self):
        return sorted(self.name_to_host) or sorted(self.host_by_id)

    def known_switches(self):
        return sorted(self.name_to_switch) or sorted(self.device_by_id)

    def resolve_host(self, name):
        host = self.find_host(name)
        if host is None:
            raise OnosError(
                "unknown host %r; known hosts: %s"
                % (name, ", ".join(self.known_hosts()))
            )
        return host, self.host_name(host)

    def resolve_switch(self, name):
        device = self.find_switch(name)
        if device is None:
            raise OnosError(
                "unknown switch %r; known switches: %s"
                % (name, ", ".join(self.known_switches()))
            )
        return device, self.switch_name(device)

    def resolve_node(self, name):
        host = self.find_host(name)
        device = self.find_switch(name)
        if host is not None and device is not None:
            raise OnosError(
                "node %r is ambiguous: it matches both a host and a switch"
                % name
            )
        if host is not None:
            return "host", host, self.host_name(host)
        if device is not None:
            return "switch", device, self.switch_name(device)
        raise OnosError(
            "unknown node %r; known hosts: %s; known switches: %s"
            % (
                name,
                ", ".join(self.known_hosts()),
                ", ".join(self.known_switches()),
            )
        )


class OnosClient:
    """Small ONOS v1 REST client using only the Python standard library."""

    def __init__(
        self,
        base_url=DEFAULT_ONOS_URL,
        user=DEFAULT_ONOS_USER,
        password=DEFAULT_ONOS_PASSWORD,
        timeout=20,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        token = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        self.auth_header = "Basic " + token

    def request(self, method, path, payload=None, headers=None, timeout=None):
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        request_headers = {"Accept": "application/json"}
        request_headers["Authorization"] = self.auth_header
        if headers:
            request_headers.update(headers)

        data = None
        if payload is not None:
            if isinstance(payload, (dict, list)):
                data = json.dumps(payload).encode("utf-8")
                request_headers.setdefault("Content-Type", "application/json")
            elif isinstance(payload, bytes):
                data = payload
            else:
                data = str(payload).encode("utf-8")

        request = urllib.request.Request(
            url, data=data, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.timeout
            ) as response:
                body = response.read().decode("utf-8", "replace")
                return response.status, dict(response.headers), body
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            return error.code, dict(error.headers), body
        except urllib.error.URLError as error:
            raise OnosError("cannot reach ONOS at %s: %s" % (self.base_url, error))

    def get_json(self, path):
        status, _headers, body = self.request("GET", path)
        if status != 200:
            raise OnosError("GET %s returned HTTP %s: %s" % (path, status, body[:300]))
        try:
            return json.loads(body)
        except ValueError:
            raise OnosError("GET %s did not return JSON: %s" % (path, body[:300]))

    def applications(self):
        return self.get_json("/onos/v1/applications").get("applications", [])

    def app_state(self, app_name):
        for app in self.applications():
            if app.get("name") == app_name:
                return app.get("state")
        return None

    def hosts(self):
        return self.get_json("/onos/v1/hosts").get("hosts", [])

    def intents(self):
        return self.get_json("/onos/v1/intents").get("intents", [])

    def flows(self):
        return self.get_json("/onos/v1/flows").get("flows", [])

    def create_flow(self, device_id, payload, app_id=DEFAULT_APP_ID):
        """Install one flow rule and return the REST response details."""
        quoted_device = urllib.parse.quote(device_id, safe="")
        quoted_app = urllib.parse.quote(app_id, safe="")
        path = "/onos/v1/flows/%s?appId=%s" % (
            quoted_device,
            quoted_app,
        )
        status, headers, body = self.request("POST", path, payload)
        parsed = None
        if body.strip():
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
        return status, headers, parsed, body

    def delete_flow(self, device_id, flow_id):
        """Delete one flow rule by the device and id returned by ONOS."""
        quoted_device = urllib.parse.quote(device_id, safe="")
        quoted_flow = urllib.parse.quote(str(flow_id), safe="")
        path = "/onos/v1/flows/%s/%s" % (quoted_device, quoted_flow)
        status, headers, body = self.request("DELETE", path)
        return status, headers, body, path

    def links(self):
        return self.get_json("/onos/v1/links").get("links", [])

    def devices(self):
        return self.get_json("/onos/v1/devices").get("devices", [])

    def ports(self, device_id):
        quoted = urllib.parse.quote(device_id, safe="")
        return self.get_json("/onos/v1/devices/%s/ports" % quoted).get("ports", [])

    def host_by_ip(self, ip):
        for host in self.hosts():
            if ip in (host.get("ipAddresses") or []):
                return host
        return None

    def host_by_id(self, host_id):
        for host in self.hosts():
            if host.get("id") == host_id:
                return host
        return None

    def resolve_host(self, name, host_ips=None):
        """Resolve a host name, IP, MAC, alias, or ONOS host ID."""
        resolver = NodeResolver(
            self.hosts(),
            host_aliases=host_ips,
        )
        host, _canonical = resolver.resolve_host(name)
        return host["id"], host

    def resolve_switch(self, name, switch_aliases=None):
        """Resolve a switch name, alias, or ONOS device ID."""
        resolver = NodeResolver(
            [],
            self.devices(),
            switch_aliases=switch_aliases,
        )
        device, _canonical = resolver.resolve_switch(name)
        return device["id"], device

    def resolve_node(self, name, host_aliases=None, switch_aliases=None):
        """Resolve a node name to (kind, object, canonical name)."""
        resolver = NodeResolver(
            self.hosts(),
            self.devices(),
            host_aliases=host_aliases,
            switch_aliases=switch_aliases,
        )
        return resolver.resolve_node(name)

    def create_intent(self, payload):
        status, headers, body = self.request("POST", "/onos/v1/intents", payload)
        parsed = None
        if body.strip():
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
        return status, headers, parsed, body

    def delete_intent(self, app_id, intent_id_value):
        quoted_app = urllib.parse.quote(app_id or "", safe="")
        quoted_id = urllib.parse.quote(intent_id_value, safe="")
        candidates = [
            "/onos/v1/intents/%s/%s" % (quoted_app, quoted_id),
            "/onos/v1/intents/%s" % quoted_id,
        ]
        last = None
        for path in candidates:
            status, headers, body = self.request("DELETE", path)
            last = (status, headers, body, path)
            if status in (200, 202, 204):
                return last
        return last

    def get_intent(self, app_id, intent_id_value):
        quoted_app = urllib.parse.quote(app_id or "", safe="")
        quoted_id = urllib.parse.quote(intent_id_value, safe="")
        candidates = [
            "/onos/v1/intents/%s/%s" % (quoted_app, quoted_id),
            "/onos/v1/intents/%s" % quoted_id,
        ]
        last = None
        for path in candidates:
            status, headers, body = self.request("GET", path)
            last = (status, headers, body, path)
            if status == 200:
                try:
                    return json.loads(body), last
                except ValueError:
                    return body, last
        return None, last


class IntentManager:
    """Text-to-intent translator with conflict detection."""

    def __init__(
        self,
        client=None,
        host_ips=None,
        app_id=DEFAULT_APP_ID,
        priority=100,
        switch_aliases=None,
    ):
        self.client = client or OnosClient()
        self.host_aliases = dict(host_ips or {})
        self.switch_aliases = dict(switch_aliases or {})
        self.app_id = app_id
        self.priority = priority

    def resolver(self):
        devices = []
        devices_method = getattr(self.client, "devices", None)
        if callable(devices_method):
            devices = devices_method()
        return NodeResolver(
            self.client.hosts(),
            devices,
            host_aliases=self.host_aliases,
            switch_aliases=self.switch_aliases,
        )

    def translate(self, text):
        command = parse_command(text)
        if command["action"] not in ("connect", "disconnect"):
            raise OnosError(
                "%s is not an intent command" % command.get("action")
            )
        resolver = self.resolver()
        one_host, one_name = resolver.resolve_host(command["src"])
        two_host, two_name = resolver.resolve_host(command["dst"])
        one = one_host["id"]
        two = two_host["id"]
        payload = host_to_host_payload(
            one, two, app_id=self.app_id, priority=self.priority
        )
        return {
            "command": command,
            "one": one,
            "two": two,
            "oneHost": one_host,
            "twoHost": two_host,
            "oneName": one_name,
            "twoName": two_name,
            "payload": payload,
        }

    def conflicts_for(self, one, two):
        return detect_conflicts(one, two, self.client.intents())

    def submit(self, text, force=False, dry_run=False):
        translated = self.translate(text)
        if dry_run:
            result = dict(translated)
            result["dryRun"] = True
            return result

        conflicts = self.conflicts_for(translated["one"], translated["two"])
        hard = [item for item in conflicts if item["kind"] == "duplicate"]
        soft = [item for item in conflicts if item["kind"] == "overlap"]
        if hard:
            raise ConflictError(
                "duplicate intent already exists for %s <-> %s"
                % (translated["command"]["src"], translated["command"]["dst"]),
                hard,
            )
        if soft and not force:
            raise ConflictError(
                "intent overlaps an existing intent (shared host); "
                "use --force to submit anyway",
                soft,
            )

        status, headers, parsed, body = self.client.create_intent(
            translated["payload"]
        )
        if status not in (200, 201):
            raise OnosError(
                "ONOS rejected the intent (HTTP %s): %s" % (status, body[:400])
            )
        result = dict(translated)
        result.update(
            {
                "status": status,
                "headers": headers,
                "response": parsed,
                "body": body,
                "conflicts": conflicts,
                "forced": bool(soft and force),
            }
        )
        return result

    def disconnect(self, text):
        translated = self.translate(text)
        wanted = {translated["one"], translated["two"]}
        deleted = []
        for intent in self.client.intents():
            if intent.get("type") not in (None, "HostToHostIntent"):
                continue
            if not is_active_intent(intent):
                continue
            one, two = intent_endpoints(intent)
            if not one or not two or {one, two} != wanted:
                continue
            app_id = intent_app_id(intent) or ""
            ident = intent_id(intent)
            if not ident:
                continue
            status, headers, body, path = self.client.delete_intent(app_id, ident)
            deleted.append(
                {
                    "id": ident,
                    "appId": app_id,
                    "status": status,
                    "path": path,
                    "body": body,
                }
            )
        if not deleted:
            raise OnosError(
                "no matching intent for %s <-> %s"
                % (translated["command"]["src"], translated["command"]["dst"])
            )
        return {"translated": translated, "deleted": deleted}

    def list_intents(self):
        rows = []
        hosts = {host.get("id"): host for host in self.client.hosts()}
        resolver = self.resolver()
        for intent in self.client.intents():
            if intent.get("type") not in (None, "HostToHostIntent"):
                continue
            one, two = intent_endpoints(intent)
            if not one or not two:
                continue
            one_host = hosts.get(one)
            two_host = hosts.get(two)
            rows.append(
                {
                    "id": intent_id(intent),
                    "appId": intent_app_id(intent),
                    "state": intent_state(intent),
                    "type": intent.get("type"),
                    "one": one,
                    "two": two,
                    "oneName": (
                        resolver.host_name(one_host) if one_host else one
                    ),
                    "twoName": (
                        resolver.host_name(two_host) if two_host else two
                    ),
                    "oneIps": (one_host or {}).get("ipAddresses", []),
                    "twoIps": (two_host or {}).get("ipAddresses", []),
                }
            )
        return rows

    def describe_hosts(self):
        rows = []
        for host in self.client.hosts():
            rows.append(
                {
                    "id": host.get("id"),
                    "mac": host.get("mac"),
                    "vlan": host.get("vlan"),
                    "ips": host.get("ipAddresses") or [],
                    "locations": host.get("locations") or [],
                }
            )
        return rows
